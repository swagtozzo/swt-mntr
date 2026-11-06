#!/usr/bin/env python3
"""
Monitor Aste — raccoglie aste da più portali, le filtra secondo config.yaml,
aggiorna docs/data/aste.json (letto dalla dashboard) e invia avvisi Telegram
per i lotti nuovi che rispettano le regole.

Fonti:
  - PVP  (Portale Vendite Pubbliche, Ministero della Giustizia) — API JSON pubblica
  - Gobid (gobid.it) — aste giudiziarie e private (leasing, banche, aziende)

Variabili d'ambiente (segreti GitHub):
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  (se mancano, gli avvisi vengono saltati)
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
DATA_FILE = ROOT / "docs" / "data" / "aste.json"
CONFIG_FILE = ROOT / "config.yaml"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TZ = dt.timezone(dt.timedelta(hours=1))  # Italia (approssimato; sufficiente per date di vendita)

CATEGORIE_LABEL = {
    "auto_moto": "Auto e moto",
    "immobili": "Immobili",
    "macchinari": "Macchinari e attrezzature",
    "orologi_gioielli": "Orologi, gioielli, arte",
    "moda_lusso": "Moda e lusso",
    "sport_outdoor": "Sport e outdoor",
    "nautica": "Nautica",
    "altro": "Altro",
}

# Mappatura categorie PVP -> categorie del monitor
PVP_CAT = {
    "AUTOVEICOLI_E_CICLI": "auto_moto",
    "NAUTICA": "nautica",
    "ARTE_OREF_OROL_ANTIQ": "orologi_gioielli",
    "ABBIGLIAMENTO_E_CALZATURE": "moda_lusso",
    "MACCHINARI_UT_MAT_PRIME": "macchinari",
}

# Parole che classificano il testo libero (usate per Gobid e per lotti PVP generici)
CLASSIFICA = [
    ("orologi_gioielli", r"orolog|rolex|omega|seiko|tudor|cartier|patek|audemars|panerai|breitling|gioiell|preziosi|\boro\b|diamant|brillant|dipint|opere? d.arte|antiquariat"),
    ("moda_lusso", r"borse?\b|louis vuitton|gucci|prada|herm[eè]s|chanel|dior|balenciaga|moncler|abbigliament|calzatur|scarpe|sneaker|pellett|capi d"),
    ("nautica", r"\bbarca|\bbarche|gommon|natante|imbarcazion|motoscaf|fuoribordo|veliero|yacht|\bgozzo|cabinato"),
    ("sport_outdoor", r"\bsurf|\bsup\b|kitesurf|windsurf|\bbici|biciclett|e-?bike|casco|tuta moto|dainese|alpinestars|palestra|fitness|tapis|attrezzatura sportiv|\bski\b|\bsci\b|snowboard"),
    ("auto_moto", r"\bauto\b|autovettur|automobil|\bmoto\b|motocicl|motoveicol|scooter|ciclomotor|furgon|fiat|bmw|audi|mercedes|volkswagen|\bvw\b|ford|toyota|renault|peugeot|alfa romeo|porsche|ferrari|lamborghini|maserati|jeep|land rover|ducati|yamaha|honda|kawasaki|harley|vespa|piaggio|quad\b"),
    ("macchinari", r"macchinar|attrezzatur|tornio|fresatric|centr[oi] di lavoro|escavator|muletto|carrell[oi] elevator|gru\b|compressor|pressa|impianto|utensil|trattor|rimorchi|semirimorchi|camion"),
    ("immobili", r"immobil|appartament|villa\b|villino|terreno|capannon|negozio|abitazion|fabbricat|box auto|garage"),
]
CLASSIFICA = [(c, re.compile(p, re.I)) for c, p in CLASSIFICA]


# ---------------------------------------------------------------- utilità

def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def oggi() -> dt.date:
    return dt.datetime.now(TZ).date()


def kw_regex(parole):
    parole = [p.strip() for p in parole or [] if p and p.strip()]
    if not parole:
        return None
    pat = "|".join(r"(?<!\w)" + re.escape(p) + r"(?!\w)" for p in sorted(parole, key=len, reverse=True))
    return re.compile(pat, re.I)


def classifica_testo(testo: str) -> str:
    for cat, rx in CLASSIFICA:
        if rx.search(testo or ""):
            return cat
    return "altro"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})
    return s


def get_retry(fn, tentativi=3, attesa=4):
    for i in range(tentativi):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if i == tentativi - 1:
                raise
            log("  errore, riprovo:", e)
            time.sleep(attesa * (i + 1))


# ---------------------------------------------------------------- PVP

PVP_BASE = "https://pvp.giustizia.it"
PVP_API = PVP_BASE + "/ric-496b258c-986a1b71/ric-ms/ricerca/vendite"
PVP_FILTRI = PVP_BASE + "/bo-5897bc47-986a1b71/bo-ms/filtriRicercaVendite"
PVP_DETTAGLIO = PVP_BASE + "/pvp/it/detail_annuncio.page?idAnnuncio={id}"


def pvp_mappa_regioni(s) -> dict:
    """provincia (descrizione) -> regione."""
    r = get_retry(lambda: s.get(PVP_FILTRI, timeout=60))
    r.raise_for_status()
    mappa = {}
    for naz in r.json()["body"]["nazioni"]:
        for reg in naz.get("regioni", []):
            for prov in reg.get("province", []):
                mappa[prov["descProvincia"].strip().lower()] = reg["descRegione"]
    return mappa


def pvp_scarica(s, tipo: str, dal: dt.date, categoria: str | None = None):
    """Scarica lotti PVP pubblicati da `dal` in poi (ordinati per pubblicazione decrescente)."""
    body = {"tipoLotto": tipo, "nazione": "ITA"}
    if categoria:
        body["categoriaLotto"] = categoria
    page, size = 0, 500
    while True:
        url = f"{PVP_API}?language=it&page={page}&size={size}&sort=dataPubblicazione,desc"
        r = get_retry(lambda: s.post(url, json=body, timeout=90,
                                     headers={"Accept": "application/json"}))
        r.raise_for_status()
        b = r.json()["body"]
        contenuto = b.get("content") or []
        vecchi = False
        for x in contenuto:
            pub = x.get("dataPubblicazione")
            if pub and dt.date.fromisoformat(pub) < dal:
                vecchi = True
                continue
            yield x
        log(f"  PVP {tipo}{'/' + categoria if categoria else ''} pagina {page}: {len(contenuto)} lotti")
        if vecchi or b.get("last") or not contenuto:
            break
        page += 1
        time.sleep(0.5)


def pvp_normalizza(x: dict, regioni: dict) -> dict:
    ind = x.get("indirizzo") or {}
    prov = (ind.get("provincia") or "").strip()
    cat_pvp = x.get("categoriaLotto") or ""
    testo = x.get("descLotto") or ""
    if x.get("tipoLotto") == "IMMOBILI":
        cat = "immobili"
    else:
        cat = PVP_CAT.get(cat_pvp) or classifica_testo(testo)
    vendita = x.get("dataOraVendita") or x.get("dataVendita")
    return {
        "id": f"pvp-{x['id']}",
        "fonte": "PVP",
        "titolo": (testo[:140] + "…") if len(testo) > 140 else testo,
        "descrizione": testo[:400] if len(testo) > 140 else "",
        "categoria": cat,
        "categoria_portale": cat_pvp.replace("_", " ").title(),
        "prezzo_base": x.get("prezzoBaseAsta"),
        "offerta_minima": x.get("offertaMinima"),
        "rialzo_minimo": x.get("rialzoMinimo"),
        "ribasso_pct": None,
        "citta": ind.get("citta") or "",
        "provincia": prov,
        "regione": regioni.get(prov.lower(), ""),
        "lat": (ind.get("coordinate") or {}).get("latitudine"),
        "lon": (ind.get("coordinate") or {}).get("longitudine"),
        "scadenza": vendita,
        "pubblicata": x.get("dataPubblicazione"),
        "tribunale": (x.get("tribunale") or "").replace("Tribunale di ", "Trib. ").title(),
        "procedura": x.get("procedura"),
        "lotto": x.get("numeroLotto"),
        "immagine": None,
        "url": PVP_DETTAGLIO.format(id=x["id"]),
    }


# ---------------------------------------------------------------- Gobid

GOBID = "https://www.gobid.it"


def gobid_scarica(s):
    r = get_retry(lambda: s.get(GOBID + "/it/aste/", timeout=60))
    r.raise_for_status()
    m = re.search(r"ajaxHash\s*=\s*'([0-9a-f]+)'", r.text)
    if not m:
        raise RuntimeError("Gobid: chiave di ricerca non trovata (pagina cambiata o bloccata)")
    h = m.group(1)
    p = 1
    while True:
        data = {
            "tipoRicerca": "aste", "tipoVis": "griglia", "p": p, "orderBy": 0, "pageSize": 48,
            "idCatForzata": "", "idAsta": "", "idAnnuncioPVP": 0, "idLivello": "",
            "sezione": "aste", "sezione2": "", "aEsclusi[]": "", "idCombinazione": "",
            "testo": "", "ribassoMin": 0, "ribassoMax": 100, "azione": "ajaxSearch",
        }
        rr = get_retry(lambda: s.post(f"{GOBID}/controller/ricerca.php?ajaxSearch={h}", data=data,
                                      timeout=60, headers={"X-Requested-With": "XMLHttpRequest",
                                                           "Referer": GOBID + "/it/aste/"}))
        rr.raise_for_status()
        j = rr.json()
        items = j.get("items") or []
        log(f"  Gobid pagina {p}/{j.get('nPagine')}: {len(items)} aste")
        yield from items
        if not items or p >= int(j.get("nPagine") or 0):
            break
        p += 1
        time.sleep(0.7)


def gobid_normalizza(x: dict) -> dict:
    titolo = html.unescape(x.get("titolo") or "")
    sott = html.unescape(x.get("sottotitolo") or "")
    fine = int(x.get("tsFine") or 0)
    scad = dt.datetime.fromtimestamp(fine, TZ).strftime("%Y-%m-%dT%H:%M") if fine else None
    img = x.get("imgPath1") or x.get("imgPath")
    rib = x.get("percRibasso") or 0
    return {
        "id": f"gobid-{x['id']}",
        "fonte": "Gobid",
        "titolo": titolo,
        "descrizione": f"{titolo} — {sott}".strip(" —"),
        "categoria": classifica_testo(titolo + " " + sott),
        "categoria_portale": sott,
        "prezzo_base": None,
        "offerta_minima": None,
        "rialzo_minimo": None,
        "ribasso_pct": int(rib) if rib else None,
        "citta": "", "provincia": "", "regione": "",
        "lat": None, "lon": None,
        "scadenza": scad,
        "pubblicata": None,
        "tribunale": sott,
        "procedura": None,
        "lotto": f"{x.get('nLotti')} lotti" if x.get("nLotti") else None,
        "immagine": f"{GOBID}/{img}" if img else None,
        "url": f"{GOBID}/it/aste/{x.get('urlAsta')}",
    }


# ---------------------------------------------------------------- filtri e avvisi

class Filtri:
    def __init__(self, cfg):
        self.cfg = cfg
        self.cat_attive = {k for k, v in (cfg.get("categorie") or {}).items() if v}
        self.kw = kw_regex(cfg.get("parole_chiave"))
        self.escluse = [p.lower() for p in cfg.get("parole_escluse") or []]
        imm = cfg.get("immobili") or {}
        self.imm_max = imm.get("prezzo_max")
        self.imm_regioni = {r.lower() for r in imm.get("regioni") or []}

    def parole_trovate(self, a) -> list[str]:
        if not self.kw:
            return []
        testo = f"{a['titolo']} {a['descrizione']}"
        return sorted({m.group(0).lower() for m in self.kw.finditer(testo)})

    def tieni(self, a) -> bool:
        testo = f"{a['titolo']} {a['descrizione']}".lower()
        if any(p in testo for p in self.escluse):
            return False
        a["parole"] = self.parole_trovate(a)
        if a["categoria"] == "immobili":
            if "immobili" not in self.cat_attive and not a["parole"]:
                return False
            if self.imm_max and a["prezzo_base"] and a["prezzo_base"] > self.imm_max:
                return False
            if self.imm_regioni and a["regione"].lower() not in self.imm_regioni:
                return False
            return True
        return a["categoria"] in self.cat_attive or bool(a["parole"])


def regole_avviso(cfg):
    regole = []
    for r in cfg.get("avvisi") or []:
        regole.append({
            "nome": r.get("nome", "Avviso"),
            "categorie": set(r.get("categorie") or []),
            "parole": kw_regex(r.get("parole")),
            "regioni": {x.lower() for x in r.get("regioni") or []},
            "prezzo_max": r.get("prezzo_max"),
        })
    return regole


def regola_che_scatta(a, regole):
    testo = f"{a['titolo']} {a['descrizione']}"
    for r in regole:
        if r["categorie"] and a["categoria"] not in r["categorie"]:
            continue
        if r["parole"] and not r["parole"].search(testo):
            continue
        if r["regioni"] and a["regione"].lower() not in r["regioni"]:
            continue
        if r["prezzo_max"] and (a["prezzo_base"] or 0) > r["prezzo_max"]:
            continue
        return r["nome"]
    return None


def euro(v):
    if v is None:
        return "—"
    return "€ " + f"{v:,.0f}".replace(",", ".")


def telegram_invia(testo: str):
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage", timeout=30, data={
        "chat_id": chat, "text": testo, "parse_mode": "HTML", "disable_web_page_preview": "true"})
    if r.status_code == 429:
        time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
        return telegram_invia(testo)
    if not r.ok:
        log("  Telegram errore:", r.status_code, r.text[:200])
    return r.ok


def messaggio(a, regola):
    e = html.escape
    luogo = ", ".join(dict.fromkeys(x for x in [a["citta"], a["provincia"], a["regione"]] if x)) or a["tribunale"] or ""
    righe = [
        f"🔔 <b>{e(regola)}</b>",
        f"<b>{e(a['titolo'])}</b>",
        f"💶 Base {euro(a['prezzo_base'])}" + (f" · min {euro(a['offerta_minima'])}" if a["offerta_minima"] else "")
        + (f" · ribasso {a['ribasso_pct']}%" if a["ribasso_pct"] else ""),
        f"📍 {e(luogo)}" if luogo else None,
        f"⏰ {a['scadenza'].replace('T', ' ore ') if a['scadenza'] else '—'}",
        f"🏷 {e(CATEGORIE_LABEL.get(a['categoria'], a['categoria']))} · {a['fonte']}",
        f"<a href=\"{e(a['url'])}\">Apri la scheda</a>",
    ]
    return "\n".join(r for r in righe if r)


# ---------------------------------------------------------------- main

def carica_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def main():
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    filtri = Filtri(cfg)
    regole = regole_avviso(cfg)
    dati = carica_json(DATA_FILE, {"aste": []})
    esistenti = {a["id"]: a for a in dati.get("aste", [])}
    ultimo = dati.get("aggiornato")
    primo_avvio = not ultimo

    oggi_d = oggi()
    if primo_avvio:
        dal = oggi_d - dt.timedelta(days=int(cfg.get("backfill_giorni", 30)))
    else:
        dal = dt.date.fromisoformat(ultimo[:10]) - dt.timedelta(days=2)

    s = session()
    raccolti, esito_fonti = [], {}

    # --- PVP
    try:
        regioni = pvp_mappa_regioni(s)
        n = 0
        cat_mobili = None if (filtri.kw or "sport_outdoor" in filtri.cat_attive) else list(PVP_CAT)
        for tipo in ["MOBILI", "IMMOBILI"]:
            if tipo == "IMMOBILI" and "immobili" not in filtri.cat_attive:
                continue
            sottocat = cat_mobili if tipo == "MOBILI" and cat_mobili else [None]
            for c in sottocat:
                for x in pvp_scarica(s, tipo, dal, c):
                    raccolti.append(pvp_normalizza(x, regioni))
                    n += 1
        esito_fonti["PVP"] = {"ok": True, "letti": n}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        esito_fonti["PVP"] = {"ok": False, "errore": str(e)[:300]}

    # --- Gobid (scarica sempre tutto: ~2.500 aste attive)
    gobid_visti = set()
    try:
        n = 0
        for x in gobid_scarica(session()):
            a = gobid_normalizza(x)
            gobid_visti.add(a["id"])
            raccolti.append(a)
            n += 1
        esito_fonti["Gobid"] = {"ok": True, "letti": n}
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        esito_fonti["Gobid"] = {"ok": False, "errore": str(e)[:300]}

    # --- filtra, unisci, individua i nuovi
    nuovi = []
    adesso = dt.datetime.now(TZ).strftime("%Y-%m-%dT%H:%M")
    for a in raccolti:
        if not filtri.tieni(a):
            continue
        prec = esistenti.get(a["id"])
        if prec:
            a["visto_il"] = prec.get("visto_il", adesso)
        else:
            a["visto_il"] = adesso
            nuovi.append(a)
        esistenti[a["id"]] = a

    # --- rimuovi scadute e aste Gobid non più in elenco
    soglia = (oggi_d - dt.timedelta(days=1)).isoformat()
    tenute = []
    for a in esistenti.values():
        if a["scadenza"] and a["scadenza"][:10] < soglia:
            continue
        if a["fonte"] == "Gobid" and esito_fonti.get("Gobid", {}).get("ok") and a["id"] not in gobid_visti:
            continue
        a["parole"] = filtri.parole_trovate(a)
        tenute.append(a)
    tenute.sort(key=lambda a: (a.get("scadenza") or "9999"))

    # --- avvisi Telegram (non al primo avvio: sarebbero migliaia)
    inviati = 0
    if not primo_avvio:
        da_avvisare = [(a, regola_che_scatta(a, regole)) for a in nuovi]
        da_avvisare = [(a, r) for a, r in da_avvisare if r]
        massimo = int(cfg.get("max_avvisi_per_giro", 25))
        for a, r in da_avvisare[:massimo]:
            if telegram_invia(messaggio(a, r)):
                inviati += 1
            time.sleep(1.1)
        resto = len(da_avvisare) - massimo
        if resto > 0:
            telegram_invia(f"➕ Altri <b>{resto}</b> lotti nuovi corrispondono ai tuoi avvisi: li trovi nella dashboard.")
    elif os.environ.get("TELEGRAM_BOT_TOKEN"):
        telegram_invia(f"✅ Monitor aste attivo. Primo caricamento completato: <b>{len(tenute)}</b> aste in dashboard. "
                       "Da ora ti scrivo solo per i lotti nuovi che rispettano i tuoi avvisi.")

    # --- salva
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    out = {
        "aggiornato": adesso,
        "fonti": esito_fonti,
        "categorie": CATEGORIE_LABEL,
        "nuovi_ultimo_giro": len(nuovi),
        "aste": tenute,
    }
    DATA_FILE.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    log(f"Fatto. In dashboard: {len(tenute)} · nuovi: {len(nuovi)} · avvisi inviati: {inviati} · fonti: {esito_fonti}")
    if not any(f.get("ok") for f in esito_fonti.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
