#!/usr/bin/env python3
"""
Monitor Aste — raccoglie aste da più portali, le filtra secondo config.yaml,
aggiorna docs/data/aste.json (letto dalla dashboard) e invia avvisi Telegram
per i lotti nuovi che rispettano le regole.

Fonti:
  - PVP  (Portale Vendite Pubbliche, Ministero della Giustizia) — API JSON pubblica
  - Gobid (gobid.it) — letto dal PC di casa (cartella pc/), caricato in gobid/gobid.json
  - Agenzia del Demanio, Industrial Discount, AsteBusiness

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
from bs4 import BeautifulSoup

import extra
from cifra import cifra, decifra, e_cifrato

ROOT = Path(__file__).resolve().parent
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
    ("macchinari", r"macchinar|macchine|attrezzatur|tornio|fresatric|centr[oi] di lavoro|escavator|muletto|carrell[oi] elevator|gru\b|compressor|pressa|impianto|utensil|trattor|rimorchi|semirimorchi|camion"),
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


def classifica_testo(testo: str, mobile: bool = False) -> str:
    """mobile=True: il bene è sicuramente mobile, quindi mai 'immobili'."""
    for cat, rx in CLASSIFICA:
        if mobile and cat == "immobili":
            continue
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
PVP_VENDITA = PVP_BASE + "/ve-3f723b85-986a1b71/ve-ms/vendite/{id}/restricted"


def pvp_portali(s, pvp_id: str) -> list:
    """Portali di un'asta PVP: V = dove si fa l'offerta (gestore), P = pubblicità, A = altri siti."""
    r = s.get(PVP_VENDITA.format(id=pvp_id), timeout=30, headers={"Accept": "application/json"})
    r.raise_for_status()
    j = r.json()
    b = j.get("body", j) if isinstance(j, dict) else {}
    out, visti = [], set()
    for x in sorted(b.get("siti") or [], key=lambda x: 0 if x.get("flagTipoSito") == "V" else 1):
        url = (x.get("urlInserizione") or x.get("link") or "").replace("//it/", "/it/")
        tipo = "V" if x.get("flagTipoSito") == "V" else "P"
        dom = re.sub(r"^https?://(www\.)?", "", x.get("link") or url).split("/")[0]
        if url and (tipo, dom) not in visti:
            visti.add((tipo, dom))
            out.append({"tipo": tipo, "nome": dom, "url": url})
    for x in b.get("altriSiti") or []:
        url = x.get("urlSito") or ""
        dom = re.sub(r"^https?://(www\.)?", "", url).split("/")[0]
        if url and not any(o["nome"] == dom for o in out):
            out.append({"tipo": "A", "nome": dom, "url": url})
    return out


# ---------------------------------------------------------------- foto di anteprima dai portali

FOTO_HOST = ("fallcoaste.it", "astegiudiziarie.it")   # portali che ammettono la lettura e hanno og:image
UA_FOTO = "MonitorAste/1.0 (uso personale; anteprime)"
_robots: dict = {}


def robots_ok(url: str) -> bool:
    from urllib import robotparser
    from urllib.parse import urlsplit
    u = urlsplit(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _robots:
        rp = robotparser.RobotFileParser()
        try:
            r = requests.get(base + "/robots.txt", timeout=15, headers={"User-Agent": UA_FOTO})
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except requests.RequestException:
            rp.parse([])
        _robots[base] = rp
    return _robots[base].can_fetch(UA_FOTO, url)


def foto_portale(a) -> str | None:
    cand = [p["url"] for p in sorted(a.get("portali") or [], key=lambda p: 0 if p["tipo"] == "V" else 1)
            if any(p["nome"].endswith(h) for h in FOTO_HOST) and "/" in p["url"].split("://", 1)[-1].strip("/")]
    for url in cand[:2]:
        if not robots_ok(url):
            continue
        try:
            r = requests.get(url, timeout=25, headers={"User-Agent": UA_FOTO, "Accept-Language": "it-IT"})
        except requests.RequestException:
            continue
        if r.status_code != 200:
            continue
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', r.text) or \
            re.search(r'<meta[^>]+content="([^"]+)"[^>]+property="og:image"', r.text)
        if m and not re.search(r"logo|default|placeholder", m.group(1), re.I):
            return html.unescape(m.group(1))
    return None


def cerca_foto(aste: list, massimo: int = 600):
    from concurrent.futures import ThreadPoolExecutor
    oggi_iso = oggi().isoformat()
    da_fare = [a for a in aste if a["fonte"] == "PVP" and a.get("portali") and not a.get("immagine")
               and not a.get("foto_cercata") and (a.get("scadenza") or "9999")[:10] >= oggi_iso]
    da_fare.sort(key=lambda a: (0 if (a.get("lista") or a.get("occasione") or a.get("parole")) else 1, a.get("scadenza") or "9999"))
    da_fare = da_fare[:massimo]
    trovate = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for a, img in zip(da_fare, ex.map(foto_portale, da_fare)):
            a["foto_cercata"] = True
            if img:
                a["immagine"] = img
                trovate += 1
    log(f"  Foto: cercate {len(da_fare)}, trovate {trovate}")


def pvp_arricchisci(aste: list, max_richieste: int = 1200):
    """Aggiunge i portali alle aste PVP che non li hanno ancora: prima le nuove, poi quelle che chiudono prima."""
    oggi_iso = oggi().isoformat()
    da_fare = [a for a in aste if a["fonte"] == "PVP" and "portali" not in a
               and (a.get("scadenza") or "9999")[:10] >= oggi_iso]
    da_fare.sort(key=lambda a: (a.get("visto_il") or "") , reverse=True)
    nuove = [a for a in da_fare if a.get("_nuova")]
    resto = sorted((a for a in da_fare if not a.get("_nuova")), key=lambda a: a.get("scadenza") or "9999")
    s = session()
    fatte = errori = 0
    for a in (nuove + resto)[:max_richieste]:
        try:
            a["portali"] = pvp_portali(s, a["id"].split("-", 1)[1])
            fatte += 1
        except Exception as e:  # noqa: BLE001
            errori += 1
            if errori >= 15:
                log("  PVP portali: troppi errori, riprovo al prossimo giro", e)
                break
        time.sleep(0.2)
    log(f"  PVP portali: aggiunti a {fatte} aste, {max(0, len(da_fare) - fatte)} da fare nei prossimi giri")


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
        cat = PVP_CAT.get(cat_pvp) or classifica_testo(testo, mobile=True)
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


GOBID = "https://www.gobid.it"


def vuoto(fonte: str, id_: str, titolo: str, url: str) -> dict:
    """Scheletro di un lotto con tutti i campi."""
    return {
        "id": id_, "fonte": fonte, "titolo": titolo, "descrizione": "", "categoria": "altro",
        "categoria_portale": "", "prezzo_base": None, "offerta_minima": None, "rialzo_minimo": None,
        "ribasso_pct": None, "citta": "", "provincia": "", "regione": "", "lat": None, "lon": None,
        "scadenza": None, "pubblicata": None, "tribunale": "", "procedura": None, "lotto": None,
        "immagine": None, "url": url,
    }


MESI = {m: i for i, m in enumerate(["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio",
                                    "agosto", "settembre", "ottobre", "novembre", "dicembre"], 1)}
REGIONI = ["Abruzzo", "Basilicata", "Calabria", "Campania", "Emilia Romagna", "Friuli Venezia Giulia", "Lazio",
           "Liguria", "Lombardia", "Marche", "Molise", "Piemonte", "Puglia", "Sardegna", "Sicilia", "Toscana",
           "Trentino Alto Adige", "Umbria", "Valle d'Aosta", "Veneto"]


def regione_canonica(testo: str) -> str:
    t = (testo or "").lower().replace("-", " ")
    for r in REGIONI:
        if r.lower() in t:
            return r
    return ""


def data_it(s: str):
    """'22 ottobre 2026 16:00' o '22/10/2026' -> ISO."""
    s = (s or "").strip().lower()
    m = re.search(r"(\d{1,2})\s+([a-z]+)\s+(\d{4})(?:\s+(\d{1,2}:\d{2}))?", s)
    if m and m.group(2) in MESI:
        d = dt.date(int(m.group(3)), MESI[m.group(2)], int(m.group(1))).isoformat()
        return d + ("T" + m.group(4).zfill(5) if m.group(4) else "")
    m = re.search(r"(\d{2})/(\d{2})/(\d{4})(?:\D+(\d{1,2}:\d{2}))?", s)
    if m:
        d = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
        return d + ("T" + m.group(4).zfill(5) if m.group(4) else "")
    return None


# ---------------------------------------------------------------- Agenzia del Demanio (veicoli e beni confiscati)

DEMANIO = "https://www.agenziademanio.it"
DEMANIO_LISTA = DEMANIO + "/it/gare-aste/beni-mobili-e-veicoli-confiscati/"


def demanio_bando(s, url_gara: str) -> dict:
    """Apre la pagina della gara, scarica il bando PDF più recente e lo legge."""
    r = get_retry(lambda: s.get(url_gara, timeout=60))
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    pdf = []
    for l in soup.select("main a[href$='.pdf'], a[href$='.pdf']"):
        etich = l.get_text(" ", strip=True)
        if re.search(r"bando|avviso", etich, re.I) and not re.search(r"annullament|rinvio|revoca|determin|esito|graduatori", etich, re.I):
            pdf.append((1 if re.search(r"aggiornat|rettific", etich, re.I) else 0, etich, l["href"]))
    if not pdf:
        return {"pdf": None}
    pdf.sort(key=lambda x: x[0], reverse=True)
    url = pdf[0][2] if pdf[0][2].startswith("http") else DEMANIO + pdf[0][2]
    b = get_retry(lambda: s.get(url, timeout=120)).content
    try:
        testo = extra.testo_pdf(b)
    except Exception:  # noqa: BLE001
        testo = ""
    if len(testo.strip()) < 200:
        return {"pdf": url, "scansione": True}
    info = extra.leggi_bando(testo)
    info["pdf"] = url
    return info


def demanio_scarica(s, dal: dt.date, cache: dict, max_pagine=15):
    oggi_iso = oggi().isoformat()
    for p in range(1, max_pagine + 1):
        r = get_retry(lambda: s.get(DEMANIO_LISTA, params={"p": p}, timeout=60))
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.card-body")
        cards = [c for c in cards if c.select_one("h2.card-title") and c.select_one("a[href*='/gara/']")]
        log(f"  Demanio pagina {p}: {len(cards)} gare")
        if not cards:
            break
        vecchie = 0
        for c in cards:
            campi = {}
            for par in c.select("p.color_text_gare"):
                testo = par.get_text(" ", strip=True)
                if ":" in testo:
                    k, v = testo.split(":", 1)
                    campi[k.strip().lower()] = v.strip()
            pub = data_it(campi.get("data pubblicazione bando", ""))
            scad = data_it(campi.get("termine per partecipare", ""))
            if pub and pub[:10] < dal.isoformat():
                vecchie += 1
                continue
            if scad and scad[:10] < oggi_iso:
                continue
            titolo = c.select_one("h2.card-title").get_text(" ", strip=True)
            desc = campi.get("descrizione", "")
            if re.search(r"selezione di operatori|servizio triennale|demolizione e radiazione", titolo + desc, re.I) \
                    and not re.search(r"vendita", titolo, re.I):
                continue  # gare di servizi, non vendite
            href = c.select_one("a[href*='/gara/']")["href"]
            a = vuoto("Demanio", "demanio-" + re.sub(r"[^a-z0-9]+", "-", href.split("/gara/")[-1].lower())[:120],
                      titolo, DEMANIO + href if href.startswith("/") else href)
            a["descrizione"] = desc[:400]
            a["pubblicata"] = pub[:10] if pub else None
            a["scadenza"] = scad
            a["tribunale"] = "Agenzia del Demanio"
            a["categoria_portale"] = campi.get("tipo procedura", "")
            testo = titolo + " " + desc
            a["regione"] = regione_canonica(testo)
            m = re.search(r"provincia di ([A-Z][\w' ]+?)(?:[,.;]| ed | e |$)", testo)
            if m:
                a["provincia"] = m.group(1).strip()
            cat = classifica_testo(testo)
            a["categoria"] = cat if cat != "altro" else ("auto_moto" if re.search(r"veicol|autovett", testo, re.I) else "altro")
            if href not in cache:
                try:
                    cache[href] = demanio_bando(s, a["url"])
                    log(f"   bando letto: {len(cache[href].get('lotti') or [])} lotti")
                except Exception as e:  # noqa: BLE001
                    log("   bando non leggibile:", e)
                    cache[href] = {"pdf": None}
            info = {k: v for k, v in cache[href].items() if k != "lotti"}
            a["bando"] = info
            lotti = [x for x in cache[href].get("lotti") or [] if not x.get("revocato") and x.get("prezzo")]
            if not lotti:
                yield a
                continue
            for x in lotti:
                b = dict(a)
                b["id"] = f"{a['id']}-l{x['n']}"
                b["titolo"] = x["titolo"] + (f" ({x['anno']})" if x.get("anno") else "")
                det = [f"Lotto {x['n']}"] + [v for v in (x.get("tipo"), x.get("condizioni") and "condizioni " + x["condizioni"].lower(),
                                                         x.get("anno") and "immatricolazione " + x["anno"]) if v]
                if x.get("dettagli_nel_bando"):
                    det.append("dettagli del veicolo nel bando PDF")
                b["descrizione"] = " · ".join(det) + " — " + titolo
                b["prezzo_base"] = x["prezzo"]
                b["lotto"] = f"Lotto {x['n']}"
                if x.get("sigla"):
                    b["provincia"] = x["sigla"]
                tb = b["titolo"] + " " + (x.get("tipo") or "")
                cat = classifica_testo(tb, mobile=True)
                b["categoria"] = cat if cat != "altro" else a["categoria"]
                yield b
        if vecchie == len(cards):
            break
        time.sleep(0.5)


# ---------------------------------------------------------------- Industrial Discount (vendite giudiziarie e da aziende)

INDDISC = "https://www.industrialdiscount.it"
INDDISC_STATO = {}


def inddisc_scarica(s, ultimo_id: int, primo_avvio: bool, max_schede=250):
    """Legge la mappa del sito, prende i lotti con ID più alto di quelli già visti, apre le loro schede."""
    r = get_retry(lambda: s.get(INDDISC + "/sitemap/", timeout=90))
    r.raise_for_status()
    lotti = {}
    for u in re.findall(r"<loc>([^<]*/lotti/[^<]+)</loc>", r.text):
        m = re.search(r"-(\d+)/?$", u)
        if m:
            lotti[int(m.group(1))] = u
    ids = sorted(lotti, reverse=True)
    if ids:
        INDDISC_STATO["max_id"] = ids[0]
    nuovi = [i for i in ids if i > ultimo_id] if not primo_avvio else ids[:max_schede]
    nuovi = nuovi[:max_schede]
    log(f"  Industrial Discount: {len(lotti)} lotti in mappa, {len(nuovi)} da aprire")
    oggi_iso = oggi().isoformat()
    for i in nuovi:
        try:
            p = get_retry(lambda: s.get(lotti[i], timeout=45), tentativi=2)
            if p.status_code != 200:
                continue
            a = inddisc_normalizza(i, lotti[i], p.text)
        except Exception as e:  # noqa: BLE001
            log("   scheda saltata", i, e)
            continue
        if a and a["scadenza"] and a["scadenza"][:10] >= oggi_iso:
            yield a
        time.sleep(0.4)


def inddisc_normalizza(lid: int, url: str, h: str):
    def meta(prop):
        m = re.search(r'<meta[^>]+(?:property|name)="%s"[^>]+content="([^"]*)"' % re.escape(prop), h)
        return html.unescape(m.group(1)).strip() if m else ""
    titolo = meta("og:title")
    if not titolo:
        return None
    a = vuoto("Industrial Discount", f"inddisc-{lid}", titolo, url)
    a["descrizione"] = meta("description")[:400]
    a["immagine"] = meta("og:image") or None
    m = re.search(r'itemprop="price"\s+content="([\d.]+)"', h)
    a["prezzo_base"] = float(m.group(1)) if m else None
    m = re.search(r'id="expiry_date_\d+_%d"[^>]*>\s*(\d{2}/\d{2}/\d{4})\s*</span>\s*</div>\s*'
                  r'<div class="expiry_time"><div class="hours">(\d{1,2}:\d{2})' % lid, h)
    if m:
        a["scadenza"] = data_it(f"{m.group(1)} {m.group(2)}")
    else:
        m = re.search(r'id="expiry_date_\d+_%d"[^>]*>\s*(\d{2}/\d{2}/\d{4})' % lid, h)
        a["scadenza"] = data_it(m.group(1)) if m else None
    seo = dict(re.findall(r'(macroCategoria|microCategoria|regione|provincia|comune|codiceAsta|codiceLotto):"([^"]*)"', h))
    a["regione"] = regione_canonica(seo.get("regione", ""))
    a["provincia"] = seo.get("provincia", "").title()
    a["citta"] = seo.get("comune", "").title()
    a["categoria_portale"] = " / ".join(x for x in [seo.get("macroCategoria", ""), seo.get("microCategoria", "")] if x).title()
    a["lotto"] = f"Asta {seo.get('codiceAsta')} · lotto {seo.get('codiceLotto')}" if seo.get("codiceAsta") else None
    a["tribunale"] = "Vendita da azienda" if re.search(r"VENDITA DA AZIENDA", h[:200000]) else "Industrial Discount"
    macro = seo.get("macroCategoria", "").lower()
    if macro == "nautico":
        a["categoria"] = "nautica"
    elif macro in ("trasporti",) and re.search(r"automobil|motocicl", seo.get("microCategoria", ""), re.I):
        a["categoria"] = "auto_moto"
    else:
        cat = classifica_testo(titolo + " " + a["descrizione"])
        a["categoria"] = cat if cat != "altro" else ("macchinari" if macro not in ("complesso aziendale", "quote societarie", "proprieta intellettuali") else "altro")
    return a


# ---------------------------------------------------------------- AsteBusiness

ASTEBUS = "https://www.astebusiness.it"


def astebusiness_scarica(s):
    p, ultima = 1, 1
    while p <= ultima:
        r = get_retry(lambda: s.get(ASTEBUS + "/it/online-auction", timeout=60, params={
            "page": p, "onlyAuctions": "true", "status": 1, "reservePrice": 0, "fromPrice": 0, "toPrice": 0},
            headers={"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}))
        r.raise_for_status()
        j = r.json()
        j = j[0] if isinstance(j, list) else j
        ultima = int(j.get("last_page") or 1)
        dati = j.get("data") or []
        log(f"  AsteBusiness pagina {p}/{ultima}: {len(dati)} aste")
        yield from dati
        p += 1
        time.sleep(0.5)


def astebusiness_normalizza(x: dict) -> dict:
    a = vuoto("AsteBusiness", f"astebus-{x['id']}", html.unescape(x.get("auctionName") or ""), x.get("url") or ASTEBUS)
    a["descrizione"] = html.unescape(x.get("auctionDescription") or "")[:400]
    a["citta"] = x.get("auctionMunicipality") or ""
    a["provincia"] = x.get("auctionProvince") or ""
    a["regione"] = regione_canonica(x.get("auctionRegion") or "") or (x.get("auctionRegion") or "")
    a["scadenza"] = (x.get("experimentDateStopFull") or "").replace(" ", "T")[:16] or None
    a["categoria_portale"] = x.get("category") or ""
    a["tribunale"] = x.get("salesTypeName") or ""
    a["lotto"] = f"Asta {x.get('auctionNumber')}"
    if x.get("experimentReducedPrice"):
        try:
            a["prezzo_base"] = float(x["experimentReducedPrice"])
        except (TypeError, ValueError):
            pass
    a["immagine"] = ASTEBUS + x["imagePath"] if x.get("imagePath") else None
    a["pvp_id"] = x.get("idInserzioneEspVendita")
    try:
        a["esperimento"] = int(x.get("experimentNumber") or 0) or None
    except (TypeError, ValueError):
        pass
    cat = (x.get("category") or "").lower()
    a["categoria"] = ("immobili" if "immobili" in cat else "nautica" if "nautica" in cat
                      else "orologi_gioielli" if "oreficeria" in cat
                      else classifica_testo(a["titolo"] + " " + a["descrizione"]))
    return a


# ---------------------------------------------------------------- filtri e avvisi

_DATA = r"\D{0,25}?(?:\d{1,2}\s*[/.-]\s*\d{1,2}\s*[/.-]\s*)?((?:19[5-9]|20[0-3])\d)\b"
RX_ANNO_FORTE = re.compile(r"(?:immatricolat\w*|immatr\.?|anno(?: di)?(?: costruzione| fabbricazione| immatricolazione| modello)?|prima volta nel|costruit\w*)" + _DATA, re.I)
RX_ANNO_DEBOLE = re.compile(r"\b(?:del|dal)\s+((?:19[5-9]|20[0-3])\d)\b", re.I)
RX_PROCEDURA = re.compile(r"(?:r\.?\s?g\.?|n\.|nr\.?|numero|procedura|esecuzione|fallimento|liquidazione|lotto)\s*[\w./-]{0,12}\s+(?:del|dal)\s+(?:19|20)\d\d", re.I)


def anno_bene(testo: str) -> int | None:
    """Anno di immatricolazione/costruzione (es. 'immatricolata per la prima volta nel 2019').
    Le parole sicure (immatricolata, anno…) vincono sul semplice 'del 2019', che può essere l'anno della procedura."""
    oggi_a = dt.date.today().year
    forti = [int(m.group(1)) for m in RX_ANNO_FORTE.finditer(testo or "") if 1950 <= int(m.group(1)) <= oggi_a]
    if forti:
        return max(forti)
    pulito = RX_PROCEDURA.sub(" ", testo or "")
    deboli = [int(m.group(1)) for m in RX_ANNO_DEBOLE.finditer(pulito) if 1950 <= int(m.group(1)) <= oggi_a]
    return min(deboli) if deboli else None


class Lista:
    """Lista desideri: oggetti precisi da cercare, ognuno con parole, esclusioni, prezzo e zona."""
    def __init__(self, voci):
        self.voci = []
        for v in voci or []:
            if not v or not v.get("parole"):
                continue
            self.voci.append({
                "nome": v.get("nome") or v["parole"][0],
                "parole": kw_regex(v["parole"]),
                "anche": kw_regex(v.get("anche")),          # se presente, serve anche una di queste
                "escludi": kw_regex(v.get("escludi")),
                "categorie": set(v.get("categorie") or []),
                "immobili": bool(v.get("immobili")),
                "prezzo_max": v.get("prezzo_max"),
                "regioni": {r.lower() for r in v.get("regioni") or []},
                "anno_min": int(v["anno_min"]) if v.get("anno_min") else None,
            })

    def trova(self, a) -> list[str]:
        testo = f"{a.get('titolo', '')} {a.get('descrizione', '')}"
        out = []
        for v in self.voci:
            if not v["parole"].search(testo):
                continue
            if v["anche"] and not v["anche"].search(testo):
                continue
            if v["escludi"] and v["escludi"].search(testo):
                continue
            if v["categorie"] and a.get("categoria") not in v["categorie"]:
                continue
            if a.get("categoria") == "immobili" and not v["immobili"] and "immobili" not in v["categorie"]:
                continue
            if v["prezzo_max"] and a.get("prezzo_base") and a["prezzo_base"] > v["prezzo_max"]:
                continue
            if v["anno_min"]:
                anno = anno_bene(testo)
                if anno and anno < v["anno_min"]:
                    continue   # anno sconosciuto: meglio avvisare che perdere l'asta
            if v["regioni"] and (a.get("regione") or "").lower() not in v["regioni"]:
                continue   # se la voce ha delle regioni, un lotto senza regione nota non passa
            out.append(v["nome"])
        return out


class Filtri:
    def __init__(self, cfg):
        self.cfg = cfg
        self.lista = Lista(cfg.get("lista"))
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
        a["lista"] = self.lista.trova(a) or None
        if a["lista"]:
            return True
        if a["categoria"] == "immobili":
            if "immobili" not in self.cat_attive and not a["parole"]:
                return False
            if self.imm_max and a["prezzo_base"] and a["prezzo_base"] > self.imm_max:
                return False
            if self.imm_regioni and a["regione"].lower() not in self.imm_regioni:
                return False
            return True
        return a["categoria"] in self.cat_attive or bool(a["parole"])


# ---------------------------------------------------------------- occasioni

ORDINALI = {"secondo": 2, "seconda": 2, "terzo": 3, "terza": 3, "quarto": 4, "quarta": 4, "quinto": 5, "quinta": 5,
            "sesto": 6, "sesta": 6, "settimo": 7, "settima": 7, "ottavo": 8, "ottava": 8, "nono": 9, "nona": 9,
            "decimo": 10, "decima": 10}
RX_TENT = re.compile(r"\b(?:vendita|esperimento|tentativo)\s*(?:n\.?|nr\.?|numero|n°)?\s*(\d{1,2})\b", re.I)
RX_TENT_ORD = re.compile(r"\b(%s)\s+(?:esperimento|tentativo|vendita|asta|incanto)\b" % "|".join(ORDINALI), re.I)


def tentativo(a) -> int | None:
    if a.get("esperimento"):
        return a["esperimento"]
    testo = f"{a.get('titolo', '')} {a.get('descrizione', '')} {a.get('tribunale', '')}"
    n = [int(m.group(1)) for m in RX_TENT.finditer(testo) if 1 < int(m.group(1)) < 30]
    n += [ORDINALI[m.group(1).lower()] for m in RX_TENT_ORD.finditer(testo)]
    return max(n) if n else None


def chiave_storico(a) -> str | None:
    if a["fonte"] != "PVP" or not a.get("procedura"):
        return None
    return "|".join(str(x or "").lower() for x in (a.get("tribunale"), a.get("procedura"), a.get("lotto"), a.get("citta")))


def valuta_occasione(a, storico: dict, occ_cfg: dict, oggi_iso: str):
    """Assegna a["occasione"] = {"punti", "motivi"} e aggiorna lo storico prezzi."""
    motivi = []
    t = tentativo(a)
    if t and t >= int(occ_cfg.get("tentativi_min", 3)):
        motivi.append(f"{t}° tentativo di vendita: le precedenti sono andate deserte")
    if a.get("ribasso_pct") and a["ribasso_pct"] >= int(occ_cfg.get("ribasso_min", 40)):
        motivi.append(f"ribasso del {a['ribasso_pct']}% sul prezzo iniziale")
    k = chiave_storico(a)
    if k and a.get("prezzo_base"):
        h = storico.get(k)
        if h and h[0] and a["prezzo_base"] < h[0] * (1 - float(occ_cfg.get("calo_min_pct", 20)) / 100):
            calo = round(100 * (1 - a["prezzo_base"] / h[0]))
            motivi.append(f"prezzo sceso del {calo}% (era {euro(h[0])} il {h[1][8:10]}/{h[1][5:7]}/{h[1][:4]})")
        if not h or a["prezzo_base"] > h[0]:
            storico[k] = [a["prezzo_base"], oggi_iso] if not h else [max(h[0], a["prezzo_base"]), h[1]]
    soglie = occ_cfg.get("prezzo_basso") or {}
    sog = soglie.get(a["categoria"])
    if sog and a.get("prezzo_base") and a["prezzo_base"] <= sog:
        motivi.append(f"prezzo base sotto {euro(sog)} per {CATEGORIE_LABEL.get(a['categoria'], a['categoria']).lower()}")
    raggio = occ_cfg.get("raggio_casa_km")
    casa = {r.lower() for r in occ_cfg.get("regioni_casa") or []}
    if raggio and a.get("km") is not None:
        if a["km"] <= float(raggio):
            motivi.append(f"a {a['km']} km da casa")
    elif casa and a.get("regione", "").lower() in casa:
        motivi.append(f"vicino a casa ({a['regione']})")
    if a.get("parole"):
        motivi.append("contiene: " + ", ".join(a["parole"][:4]))
    punti = len(motivi)
    a["occasione"] = {"punti": punti, "motivi": motivi} if punti >= int(occ_cfg.get("punti_min", 2)) else None


def regole_avviso(cfg):
    regole = []
    for r in cfg.get("avvisi") or []:
        regole.append({
            "nome": r.get("nome", "Avviso"),
            "categorie": set(r.get("categorie") or []),
            "parole": kw_regex(r.get("parole")),
            "regioni": {x.lower() for x in r.get("regioni") or []},
            "prezzo_max": r.get("prezzo_max"),
            "solo_occasioni": bool(r.get("solo_occasioni")),
        })
    return regole


def regola_che_scatta(a, regole):
    if a.get("lista"):
        return "Nella tua lista: " + ", ".join(a["lista"])
    testo = f"{a['titolo']} {a['descrizione']}"
    for r in regole:
        if r["categorie"] and a["categoria"] not in r["categorie"]:
            continue
        if r["parole"] and not r["parole"].search(testo):
            continue
        if r["regioni"] and a["regione"].lower() not in r["regioni"]:
            continue
        if r.get("solo_occasioni") and not a.get("occasione"):
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


GG_IT = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]
MM_IT = ["gen", "feb", "mar", "apr", "mag", "giu", "lug", "ago", "set", "ott", "nov", "dic"]


def quando_it(sc):
    if not sc:
        return "—"
    try:
        d = dt.datetime.fromisoformat(sc if len(sc) > 10 else sc + "T00:00")
    except ValueError:
        return sc
    return f"{GG_IT[d.weekday()]} {d.day} {MM_IT[d.month - 1]}" + (f" ore {sc[11:16]}" if len(sc) > 10 else "")


def messaggio(a, regola):
    e = html.escape
    luogo = ", ".join(dict.fromkeys(x for x in [a["citta"], a["provincia"], a["regione"]] if x)) or a["tribunale"] or ""
    if a.get("km") is not None:
        luogo += f" · {a['km']} km da casa"
    g = next((p for p in a.get("portali") or [] if p.get("tipo") == "V"), None)
    righe = [
        f"🔔 <b>{e(regola)}</b>",
        f"<b>{e(a['titolo'])}</b>",
        f"💶 Base {euro(a['prezzo_base'])}" + (f" · min {euro(a['offerta_minima'])}" if a["offerta_minima"] else "")
        + (f" · ribasso {a['ribasso_pct']}%" if a["ribasso_pct"] else ""),
        f"📍 {e(luogo)}" if luogo else None,
        f"⏰ chiude {quando_it(a['scadenza'])}",
        f"🏷 {e(CATEGORIE_LABEL.get(a['categoria'], a['categoria']))} · {a['fonte']}" + (f" › {e(g['nome'])}" if g else ""),
        ("⚡ <b>Occasione</b>: " + e("; ".join(a["occasione"]["motivi"]))) if a.get("occasione") else None,
        (f"<a href=\"{e(g['url'])}\">Fai l'offerta su {e(g['nome'])}</a> · " if g else "") + f"<a href=\"{e(a['url'])}\">Scheda {e(a['fonte'])}</a>",
    ]
    return "\n".join(r for r in righe if r)


# ---------------------------------------------------------------- main

class _Spenta(Exception):
    pass


def carica_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


# ---------------------------------------------------------------- dati e impostazioni private (cifrati)

CHIAVE = os.environ.get("CHIAVE_DATI", "").strip()
DATI_DIR = ROOT / "docs" / "data"


def leggi_dato(nome: str, default):
    """docs/data/<nome>.enc (cifrato) oppure <nome>.json."""
    enc, js = DATI_DIR / f"{nome}.enc", DATI_DIR / f"{nome}.json"
    if CHIAVE and enc.exists() and enc.stat().st_size > 0:
        try:
            return decifra(enc.read_text(encoding="utf-8"), CHIAVE)
        except Exception as e:  # noqa: BLE001
            log(f"ATTENZIONE: {nome}.enc non si decifra ({e.__class__.__name__}): la password è cambiata?")
            return default
    return carica_json(js, default)


def scrivi_dato(nome: str, dati):
    DATI_DIR.mkdir(parents=True, exist_ok=True)
    enc, js = DATI_DIR / f"{nome}.enc", DATI_DIR / f"{nome}.json"
    if CHIAVE:
        enc.write_text(cifra(dati, CHIAVE), encoding="utf-8")
        js.unlink(missing_ok=True)       # in chiaro non deve finire online
    else:
        js.write_text(json.dumps(dati, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def carica_impostazioni() -> dict:
    """config.yaml (pubblico) + impostazioni personali: privato.enc se c'è la password,
    altrimenti privato_iniziale.yaml e preferenze.json (vecchio formato, in chiaro)."""
    cfg = yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    priv_enc = ROOT / "privato.enc"
    if CHIAVE and priv_enc.exists():
        try:
            cfg.update(decifra(priv_enc.read_text(encoding="utf-8"), CHIAVE))
            return cfg
        except Exception as e:  # noqa: BLE001
            log(f"ATTENZIONE: privato.enc non si decifra ({e.__class__.__name__})")
    ini = ROOT / "privato_iniziale.yaml"
    if ini.exists():
        cfg.update(yaml.safe_load(ini.read_text(encoding="utf-8")) or {})
    pref = ROOT / "preferenze.json"
    if pref.exists():
        cfg.update(carica_json(pref, {}))
    return cfg


def carica_utente() -> dict:
    """Stelle, stati e note sincronizzati dalla dashboard (utente.enc nel repository)."""
    f = ROOT / "utente.enc"
    if not f.exists():
        return {}
    try:
        t = f.read_text(encoding="utf-8")
        return decifra(t, CHIAVE) if (CHIAVE and e_cifrato(t)) else json.loads(t)
    except Exception:  # noqa: BLE001
        return {}


def main():
    cfg = carica_impostazioni()
    filtri = Filtri(cfg)
    regole = regole_avviso(cfg)
    dati = leggi_dato("aste", {"aste": []})
    esistenti = {a["id"]: a for a in dati.get("aste", [])}
    ultimo = dati.get("aggiornato")
    primo_avvio = not ultimo

    oggi_d = oggi()
    adesso_dt = dt.datetime.now(TZ).replace(tzinfo=None)
    if primo_avvio:
        dal = oggi_d - dt.timedelta(days=int(cfg.get("backfill_giorni", 30)))
    else:
        dal = dt.date.fromisoformat(ultimo[:10]) - dt.timedelta(days=2)

    s = session()
    raccolti, esito_fonti = [], {}
    stato = dati.get("stato") or {}

    attive = {k.lower(): bool(v) for k, v in (cfg.get("fonti") or {}).items()}

    def attiva(nome):
        return attive.get(nome.lower(), True)

    # --- PVP
    try:
        if not attiva("pvp"):
            raise _Spenta()
        regioni = pvp_mappa_regioni(s)
        n = 0
        cat_mobili = None if (filtri.kw or filtri.lista.voci or "sport_outdoor" in filtri.cat_attive) else list(PVP_CAT)
        # Recupero profondo (una volta sola): aste pubblicate molti mesi fa ma ancora aperte.
        # Al primo avvio si guardavano solo gli ultimi `backfill_giorni`: così se ne perdevano.
        recupero = not stato.get("pvp_recupero_fatto")
        dal_pvp = min(dal, oggi_d - dt.timedelta(days=int(cfg.get("recupero_giorni", 240)))) if recupero else dal
        for tipo in ["MOBILI", "IMMOBILI"]:
            if tipo == "IMMOBILI" and "immobili" not in filtri.cat_attive:
                continue
            sottocat = cat_mobili if tipo == "MOBILI" and cat_mobili else [None]
            for c in sottocat:
                for x in pvp_scarica(s, tipo, dal_pvp, c):
                    a = pvp_normalizza(x, regioni)
                    pub = x.get("dataPubblicazione")
                    if recupero and pub and dt.date.fromisoformat(pub[:10]) < dal:
                        if (a.get("scadenza") or "9999")[:10] < oggi_d.isoformat():
                            continue            # già chiusa: nel recupero interessano solo le aste ancora aperte
                        a["_recupero"] = True   # niente avviso "nuovo" (la tua lista arriva col recupero avvisi)
                    raccolti.append(a)
                    n += 1
        if recupero:
            stato["pvp_recupero_fatto"] = True
            log(f"  PVP: recupero profondo dal {dal_pvp}")
        esito_fonti["PVP"] = {"ok": True, "letti": n}
    except _Spenta:
        pass
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        esito_fonti["PVP"] = {"ok": False, "errore": str(e)[:300]}

    # --- Gobid: lo legge il PC di casa (gobid/gobid.json), perché Gobid blocca i server di GitHub
    gobid_visti = set()
    if attiva("gobid"):
        f = ROOT / "gobid" / "gobid.json"
        if f.exists():
            try:
                g = json.loads(f.read_text(encoding="utf-8"))
                for a in g.get("aste", []):
                    a["categoria"] = classifica_testo(a["titolo"] + " " + a.get("descrizione", ""))
                    raccolti.append(a)
                    gobid_visti.add(a["id"])
                eta = (adesso_dt - dt.datetime.fromisoformat(g["aggiornato"])).total_seconds() / 3600
                esito_fonti["Gobid"] = {"ok": True, "letti": len(gobid_visti), "dal_pc": g["aggiornato"]}
                if eta > 30:
                    esito_fonti["Gobid"] = {"ok": False, "errore": f"il PC non aggiorna Gobid da {int(eta)} ore (acceso?). Restano le aste già raccolte."}
                    gobid_visti = set()
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                esito_fonti["Gobid"] = {"ok": False, "errore": str(e)[:300]}

    # --- fonti aggiuntive. Una fonte al suo primo giro non manda avvisi (caricherebbe tutto lo storico).
    inizializzate = set(stato.get("fonti_inizializzate") or (["PVP"] if not primo_avvio else []))
    if not stato.get("gobid_ricevuto"):
        inizializzate.discard("Gobid")
    if esito_fonti.get("Gobid", {}).get("ok"):
        stato["gobid_ricevuto"] = True
    elenco_completo = {}

    def raccogli(nome, generatore, completo=False):
        if not attiva(nome.replace(" ", "_")) or not attiva(nome):
            return
        visti, n = set(), 0
        try:
            for a in generatore:
                raccolti.append(a)
                visti.add(a["id"])
                n += 1
            esito_fonti[nome] = {"ok": True, "letti": n}
            if completo:
                elenco_completo[nome] = visti
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            esito_fonti[nome] = {"ok": False, "errore": str(e)[:300]}

    cache_bandi = stato.setdefault("demanio_bandi", {})
    raccogli("Demanio", demanio_scarica(session(), dal if "Demanio" in inizializzate
                                        else oggi_d - dt.timedelta(days=120), cache_bandi))
    ultimo_id = int(stato.get("inddisc_ultimo_id") or 0)
    raccogli("Industrial Discount", inddisc_scarica(session(), ultimo_id, "Industrial Discount" not in inizializzate))
    raccogli("AsteBusiness", (astebusiness_normalizza(x) for x in astebusiness_scarica(session())), completo=True)
    if gobid_visti and esito_fonti.get("Gobid", {}).get("ok"):
        elenco_completo["Gobid"] = gobid_visti
    if INDDISC_STATO.get("max_id"):
        stato["inddisc_ultimo_id"] = max(INDDISC_STATO["max_id"], ultimo_id)

    # --- doppioni dichiarati (AsteBusiness indica il numero PVP)
    pvp_ids = {a["id"] for a in raccolti if a["fonte"] == "PVP"} | {k for k in esistenti if k.startswith("pvp-")}
    raccolti = [a for a in raccolti if not (a.get("pvp_id") and f"pvp-{a['pvp_id']}" in pvp_ids)]

    # --- filtra, unisci, individua i nuovi
    nuovi = []
    adesso = adesso_dt.strftime("%Y-%m-%dT%H:%M")
    for a in raccolti:
        da_recupero = a.pop("_recupero", False)
        if not filtri.tieni(a):
            continue
        prec = esistenti.get(a["id"])
        if prec:
            a["visto_il"] = prec.get("visto_il", adesso)
            for k in ("portali", "immagine", "foto_cercata", "bando"):
                if prec.get(k) and not a.get(k):
                    a[k] = prec[k]
        else:
            a["visto_il"] = adesso
            a["_nuova"] = True
            if a["fonte"] in inizializzate and not da_recupero:
                nuovi.append(a)
        esistenti[a["id"]] = a

    if attiva("pvp") and esito_fonti.get("PVP", {}).get("ok"):
        try:
            pvp_arricchisci(list(esistenti.values()), int(cfg.get("pvp_portali_per_giro", 1200)))
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    try:
        cerca_foto(list(esistenti.values()), int(cfg.get("foto_per_giro", 600)))
    except Exception:  # noqa: BLE001
        traceback.print_exc()
    for a in esistenti.values():
        a.pop("_nuova", None)

    storico = stato.get("storico_prezzi") or {}
    occ_cfg = cfg.get("occasioni") or {}
    casa = cfg.get("casa") or {}
    casa_xy = (float(casa["lat"]), float(casa["lon"])) if casa.get("lat") and casa.get("lon") else None

    # --- separa attive e chiuse
    soglia = (oggi_d - dt.timedelta(days=1)).isoformat()
    tenute, chiuse = [], []
    for a in esistenti.values():
        if a["scadenza"] and a["scadenza"][:10] < soglia:
            chiuse.append(a)
            continue
        if a["fonte"] in elenco_completo and a["id"] not in elenco_completo[a["fonte"]]:
            continue
        if a.get("pvp_id") and f"pvp-{a['pvp_id']}" in pvp_ids:
            continue
        tenute.append(a)

    tenute, n_doppi = extra.togli_doppioni(tenute)
    if n_doppi:
        log(f"  Doppioni tra portali tolti: {n_doppi}")
    extra.assegna_km(tenute, casa_xy, stato.setdefault("coordinate", {}))
    for a in tenute:
        a["parole"] = filtri.parole_trovate(a)
        a["lista"] = filtri.lista.trova(a) or None
        valuta_occasione(a, storico, occ_cfg, oggi_d.isoformat())
    tenute.sort(key=lambda a: (a.get("scadenza") or "9999"))

    # --- archivio dei lotti chiusi (per vedere cosa è già passato e a che prezzo)
    archivio = leggi_dato("archivio", [])
    extra.archivia(archivio, chiuse, chiave_storico, oggi_d.isoformat())
    extra.segna_deserte(archivio, tenute, chiave_storico)

    # --- avvisi Telegram
    inviati = 0
    modalita = cfg.get("avvisi_modalita", "subito")
    if cfg.get("avvisi_in_pausa"):
        log("Avvisi in pausa (comando /pausa)")
    elif not primo_avvio:
        in_dashboard = {a["id"] for a in tenute}
        gia_lista = set(stato.get("lista_avvisati") or [])
        id_nuovi = {a["id"] for a in nuovi}
        # lotti della lista mai segnalati (anche se pubblicati prima dell'avvio o prima di aggiungere la voce)
        arretrati = [a for a in tenute if a.get("lista") and a["id"] not in gia_lista and a["id"] not in id_nuovi]
        candidati = sorted((a for a in nuovi if a["id"] in in_dashboard), key=lambda a: 0 if a.get("lista") else 1)
        candidati = [a for a in candidati if a.get("lista")] + arretrati + [a for a in candidati if not a.get("lista")]
        stato["lista_avvisati"] = sorted((gia_lista | {a["id"] for a in tenute if a.get("lista")}) & in_dashboard)
        if modalita == "riepilogo":
            candidati = [a for a in candidati if a.get("lista")]   # il resto arriva nel riepilogo del mattino
        da_avvisare = [(a, regola_che_scatta(a, regole)) for a in candidati]
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

    if not cfg.get("avvisi_in_pausa") and not primo_avvio:
        # promemoria sulle aste che segui
        try:
            n = extra.promemoria(tenute, carica_utente(), stato.setdefault("promemoria", {}), adesso_dt, telegram_invia, euro)
            if n:
                log(f"  Promemoria inviati: {n}")
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        # riepilogo del mattino
        ora_riep = cfg.get("riepilogo_ora", 8)
        if ora_riep is not None and adesso_dt.hour == int(ora_riep) and stato.get("ultimo_riepilogo") != oggi_d.isoformat():
            telegram_invia(extra.riepilogo(tenute, adesso_dt, euro))
            stato["ultimo_riepilogo"] = oggi_d.isoformat()
    # allarme se una fonte smette di funzionare
    extra.controlla_fonti(esito_fonti, stato.setdefault("errori_fonti", {}), telegram_invia)

    # --- salva
    out = {
        "aggiornato": adesso,
        "fonti": esito_fonti,
        "categorie": CATEGORIE_LABEL,
        "nuovi_ultimo_giro": len(nuovi),
        "casa": bool(casa_xy),
        "diritti": cfg.get("diritti_portali") or {},
        "stato": {**stato, "storico_prezzi": {k: v for k, v in storico.items()
                                              if v[1] >= (oggi_d - dt.timedelta(days=730)).isoformat()},
                  "demanio_bandi": dict(list(cache_bandi.items())[-300:]),
                  "fonti_inizializzate": sorted(inizializzate | {k for k, v in esito_fonti.items() if v.get("ok")})},
        "aste": tenute,
    }
    scrivi_dato("aste", out)
    scrivi_dato("archivio", archivio)

    log(f"Fatto. In dashboard: {len(tenute)} · nuovi: {len(nuovi)} · avvisi inviati: {inviati} · archivio: {len(archivio)} · fonti: {esito_fonti}")
    if not any(f.get("ok") for f in esito_fonti.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
