#!/usr/bin/env python3
"""
Monitor Aste — lettore Gobid da eseguire sul PC di casa.

Gobid blocca i server di GitHub, ma risponde normalmente dal PC di casa.
Questo programma:
  1. legge la mappa del sito di Gobid (ammessa dal loro robots.txt);
  2. apre la scheda delle aste nuove (ritmo tranquillo, una pagina alla volta);
  3. carica l'elenco delle aste Gobid attive nel repository GitHub (file gobid/gobid.json);
  4. GitHub rilancia il monitor, che le mette in dashboard e manda gli avvisi Telegram.

Uso:  python gobid_locale.py
Serve il file token.txt nella stessa cartella (vedi ISTRUZIONI.txt).
"""
from __future__ import annotations

import base64
import datetime as dt
import html
import json
import re
import sys
import time
from pathlib import Path

import requests

# ------------------------------------------------------------------ impostazioni
REPO = "swagtozzo/swt-mntr"          # repository GitHub del monitor
PERCORSO_NEL_REPO = "gobid/gobid.json"
PRIMO_GIRO_ASTE = 2500                # al primo avvio: quante aste recenti controllare
MAX_SCHEDE_PER_GIRO = 400             # nei giri successivi
PAUSA = 1.0                           # secondi tra una pagina e l'altra (non sovraccaricare Gobid)

CARTELLA = Path(__file__).resolve().parent
STATO = CARTELLA / "stato_gobid.json"
LOG = CARTELLA / "gobid_log.txt"
GOBID = "https://www.gobid.it"
UA = "MonitorAste/1.0 (uso personale; lettura sitemap pubblica)"

SIGLE = {
    "Abruzzo": "AQ CH PE TE", "Basilicata": "MT PZ", "Calabria": "CS CZ KR RC VV",
    "Campania": "AV BN CE NA SA", "Emilia Romagna": "BO FC FE MO PC PR RA RE RN",
    "Friuli Venezia Giulia": "GO PN TS UD", "Lazio": "FR LT RI RM VT", "Liguria": "GE IM SP SV",
    "Lombardia": "BG BS CO CR LC LO MB MI MN PV SO VA", "Marche": "AN AP FM MC PU", "Molise": "CB IS",
    "Piemonte": "AL AT BI CN NO TO VB VC", "Puglia": "BA BR BT FG LE TA",
    "Sardegna": "CA NU OR SS SU", "Sicilia": "AG CL CT EN ME PA RG SR TP",
    "Toscana": "AR FI GR LI LU MS PI PO PT SI", "Trentino Alto Adige": "BZ TN", "Umbria": "PG TR",
    "Valle d'Aosta": "AO", "Veneto": "BL PD RO TV VE VI VR",
}
SIGLA_REGIONE = {s: r for r, ss in SIGLE.items() for s in ss.split()}


def log(*a):
    riga = time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(x) for x in a)
    print(riga, flush=True)
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(riga + "\n")
        if LOG.stat().st_size > 2_000_000:
            LOG.write_text(LOG.read_text(encoding="utf-8")[-500_000:], encoding="utf-8")
    except OSError:
        pass


def id_asta(url: str) -> int:
    m = re.search(r"-(\d+)$", url)
    if not m:
        return 0
    s = m.group(1)
    # lo slug a volte attacca "Vendita-3" al numero d'asta (es. ...Vendita-328324 = vendita 3, asta 28324)
    return int(s[-5:]) if len(s) > 5 else int(s)


def slug_testo(s: str) -> str:
    s = re.sub(r"^\d+-", "", s)
    return html.unescape(requests.utils.unquote(s)).replace("-", " ").strip()


def leggi_sitemap(sess) -> dict[int, dict]:
    """{id_asta: {"url":..., "lotti": [titoli]}}"""
    r = sess.get(f"{GOBID}/lib/site-1-map.xml", timeout=60)
    r.raise_for_status()
    parti = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)
    aste: dict[int, dict] = {}
    for p in parti:
        t = sess.get(p, timeout=90)
        t.raise_for_status()
        for u in re.findall(r"<loc>\s*(https://www\.gobid\.it/it/aste/[^<\s]+)\s*</loc>", t.text):
            resto = u.split("/it/aste/", 1)[1]
            if "/" in resto:
                base, lotto = resto.split("/", 1)
                a = aste.setdefault(id_asta(base), {"url": f"{GOBID}/it/aste/{base}", "lotti": []})
                a["lotti"].append(slug_testo(lotto))
            else:
                i = id_asta(resto)
                if i:
                    aste.setdefault(i, {"url": u, "lotti": []})["url"] = u
        time.sleep(PAUSA)
    return aste


def leggi_asta(sess, i: int, info: dict) -> dict | None:
    r = sess.get(info["url"], timeout=45)
    if r.status_code != 200:
        return None
    h = r.text
    m = re.search(r'<div class="scheda-asta[^"]*"([^>]*)>', h)
    if not m:
        return None
    attr = dict(re.findall(r'data-([a-z]+)="([^"]*)"', m.group(1)))
    fine = int(attr.get("fine") or 0)
    if fine and fine < time.time() - 3600:
        return {"scaduta": True}
    def tag(t):
        mm = re.search(r"<%s>(.*?)</%s>" % (t, t), h[m.start():m.start() + 6000], re.S)
        return re.sub(r"\s+", " ", html.unescape(re.sub(r"<br\s*/?>", " · ", mm.group(1)))).strip() if mm else ""
    titolo = re.sub(r"<[^>]+>", " ", tag("h1"))
    titolo = re.sub(r"\s+", " ", titolo).strip()
    trib = re.sub(r"<[^>]+>", "", tag("h3"))
    loc = re.search(r'class="locazione".*?</i>\s*(?:<a[^>]*>)?([^<]+)', h[m.start():m.start() + 8000], re.S)
    luogo = html.unescape(loc.group(1)).strip() if loc else ""
    sig = re.search(r"\(([A-Z]{2})\)", luogo)
    citta = re.sub(r"\s*\([A-Z]{2}\).*$", "", luogo).strip()
    og = re.search(r'<meta property="og:image" content="([^"]+)"', h)
    rib = re.search(r"Ribasso\s*-?(\d+)%", h)
    scad = dt.datetime.fromtimestamp(fine).strftime("%Y-%m-%dT%H:%M") if fine else None
    lotti = info.get("lotti") or []
    return {
        "id": f"gobid-{i}",
        "fonte": "Gobid",
        "titolo": titolo or f"Asta Gobid {i}",
        "descrizione": ("Lotti: " + "; ".join(lotti))[:600] if lotti else "",
        "categoria": "",               # la assegna il monitor su GitHub
        "categoria_portale": "",
        "prezzo_base": None, "offerta_minima": None, "rialzo_minimo": None,
        "ribasso_pct": int(rib.group(1)) if rib else None,
        "citta": citta,
        "provincia": sig.group(1) if sig else "",
        "regione": SIGLA_REGIONE.get(sig.group(1), "") if sig else "",
        "lat": None, "lon": None,
        "scadenza": scad,
        "pubblicata": None,
        "tribunale": trib,
        "procedura": None,
        "lotto": f"{len(lotti)} lotti" if lotti else None,
        "immagine": og.group(1) if og else None,
        "url": info["url"],
    }


def carica_su_github(token: str, contenuto: dict):
    api = f"https://api.github.com/repos/{REPO}/contents/{PERCORSO_NEL_REPO}"
    hdr = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
           "X-GitHub-Api-Version": "2022-11-28"}
    sha = None
    r = requests.get(api, headers=hdr, timeout=30)
    if r.status_code == 200:
        sha = r.json().get("sha")
    elif r.status_code not in (404,):
        raise RuntimeError(f"GitHub {r.status_code}: {r.text[:200]}")
    corpo = {
        "message": f"Gobid dal PC: {len(contenuto['aste'])} aste attive",
        "content": base64.b64encode(json.dumps(contenuto, ensure_ascii=False).encode()).decode(),
    }
    if sha:
        corpo["sha"] = sha
    r = requests.put(api, headers=hdr, json=corpo, timeout=60)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Caricamento su GitHub fallito ({r.status_code}): {r.text[:300]}")


def main():
    tok_file = CARTELLA / "token.txt"
    if not tok_file.exists() or not tok_file.read_text().strip():
        log("Manca token.txt: segui ISTRUZIONI.txt")
        sys.exit(1)
    token = tok_file.read_text().strip()

    stato = {"ultimo_id": 0, "aste": {}}
    if STATO.exists():
        try:
            stato = json.loads(STATO.read_text(encoding="utf-8"))
        except ValueError:
            pass
    attive: dict = stato.get("aste", {})

    sess = requests.Session()
    sess.headers.update({"User-Agent": UA, "Accept-Language": "it-IT,it;q=0.9"})

    log("Leggo la mappa del sito Gobid…")
    aste = leggi_sitemap(sess)
    ids = sorted(aste, reverse=True)
    ultimo = int(stato.get("ultimo_id") or 0)
    if not ultimo:
        da_aprire = ids[:PRIMO_GIRO_ASTE]
        log(f"Primo avvio: controllo le {len(da_aprire)} aste più recenti (può richiedere ~{len(da_aprire) * 2 // 60} minuti)")
    else:
        da_aprire = [i for i in ids if i > ultimo][:MAX_SCHEDE_PER_GIRO]
        log(f"{len(aste)} aste in mappa, {len(da_aprire)} nuove da aprire")

    aperte = 0
    for i in da_aprire:
        try:
            a = leggi_asta(sess, i, aste[i])
        except requests.RequestException as e:
            log("  errore su", i, e)
            continue
        aperte += 1
        if a and not a.get("scaduta"):
            attive[str(i)] = a
        if aperte % 100 == 0:
            log(f"  {aperte}/{len(da_aprire)} schede lette, {len(attive)} aste attive")
        time.sleep(PAUSA)

    # aggiorna i titoli dei lotti delle aste già note e togli le scadute
    adesso = dt.datetime.now().strftime("%Y-%m-%dT%H:%M")
    for k in list(attive):
        if attive[k].get("scadenza") and attive[k]["scadenza"] < adesso:
            del attive[k]
        elif int(k) in aste and aste[int(k)]["lotti"]:
            lotti = aste[int(k)]["lotti"]
            attive[k]["descrizione"] = ("Lotti: " + "; ".join(lotti))[:600]
            attive[k]["lotto"] = f"{len(lotti)} lotti"

    if da_aprire:
        stato["ultimo_id"] = max([ultimo] + da_aprire)
    stato["aste"] = attive

    contenuto = {"aggiornato": adesso, "aste": sorted(attive.values(), key=lambda a: a.get("scadenza") or "")}
    STATO.write_text(json.dumps(stato, ensure_ascii=False), encoding="utf-8")
    log(f"Carico {len(attive)} aste Gobid attive su GitHub…")
    carica_su_github(token, contenuto)
    log("Fatto.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log("ERRORE:", repr(e))
        sys.exit(1)
