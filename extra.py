"""
Funzioni aggiuntive del monitor aste:
distanze da casa, doppioni tra portali, archivio dei lotti chiusi, promemoria,
allarmi sulle fonti, riepilogo del mattino, lettura dei bandi PDF del Demanio.
"""
from __future__ import annotations

import datetime as dt
import html
import io
import math
import re

# ---------------------------------------------------------------- distanze

SIGLA_NOME = {
    "AG": "Agrigento", "AL": "Alessandria", "AN": "Ancona", "AO": "Aosta", "AP": "Ascoli Piceno", "AQ": "L'Aquila",
    "AR": "Arezzo", "AT": "Asti", "AV": "Avellino", "BA": "Bari", "BG": "Bergamo", "BI": "Biella", "BL": "Belluno",
    "BN": "Benevento", "BO": "Bologna", "BR": "Brindisi", "BS": "Brescia", "BT": "Barletta-Andria-Trani", "BZ": "Bolzano",
    "CA": "Cagliari", "CB": "Campobasso", "CE": "Caserta", "CH": "Chieti", "CL": "Caltanissetta", "CN": "Cuneo",
    "CO": "Como", "CR": "Cremona", "CS": "Cosenza", "CT": "Catania", "CZ": "Catanzaro", "EN": "Enna",
    "FC": "Forlì-Cesena", "FE": "Ferrara", "FG": "Foggia", "FI": "Firenze", "FM": "Fermo", "FR": "Frosinone",
    "GE": "Genova", "GO": "Gorizia", "GR": "Grosseto", "IM": "Imperia", "IS": "Isernia", "KR": "Crotone",
    "LC": "Lecco", "LE": "Lecce", "LI": "Livorno", "LO": "Lodi", "LT": "Latina", "LU": "Lucca", "MB": "Monza e Brianza",
    "MC": "Macerata", "ME": "Messina", "MI": "Milano", "MN": "Mantova", "MO": "Modena", "MS": "Massa-Carrara",
    "MT": "Matera", "NA": "Napoli", "NO": "Novara", "NU": "Nuoro", "OR": "Oristano", "PA": "Palermo", "PC": "Piacenza",
    "PD": "Padova", "PE": "Pescara", "PG": "Perugia", "PI": "Pisa", "PN": "Pordenone", "PO": "Prato", "PR": "Parma",
    "PT": "Pistoia", "PU": "Pesaro e Urbino", "PV": "Pavia", "PZ": "Potenza", "RA": "Ravenna", "RC": "Reggio Calabria",
    "RE": "Reggio Emilia", "RG": "Ragusa", "RI": "Rieti", "RM": "Roma", "RN": "Rimini", "RO": "Rovigo", "SA": "Salerno",
    "SI": "Siena", "SO": "Sondrio", "SP": "La Spezia", "SR": "Siracusa", "SS": "Sassari", "SU": "Sud Sardegna",
    "SV": "Savona", "TA": "Taranto", "TE": "Teramo", "TN": "Trento", "TO": "Torino", "TP": "Trapani", "TR": "Terni",
    "TS": "Trieste", "TV": "Treviso", "UD": "Udine", "VA": "Varese", "VB": "Verbano-Cusio-Ossola", "VC": "Vercelli",
    "VE": "Venezia", "VI": "Vicenza", "VR": "Verona", "VT": "Viterbo", "VV": "Vibo Valentia",
}


def _norm(s: str) -> str:
    s = (s or "").lower().replace("-", " ").replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()


def haversine(a, b) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def assegna_km(aste: list, casa: tuple | None, memoria: dict):
    """Calcola a['km'] dalla casa. Usa le coordinate del PVP; per le altre aste
    la media delle coordinate già viste per lo stesso comune o provincia (memoria persistente)."""
    citta, prov = memoria.setdefault("citta", {}), memoria.setdefault("prov", {})
    for a in aste:
        if a.get("lat") and a.get("lon"):
            for d, k in ((citta, _norm(a.get("citta"))), (prov, _norm(a.get("provincia")))):
                if k:
                    v = d.get(k) or [0.0, 0.0, 0]
                    if v[2] < 50:   # bastano 50 punti per una buona media
                        d[k] = [(v[0] * v[2] + a["lat"]) / (v[2] + 1), (v[1] * v[2] + a["lon"]) / (v[2] + 1), v[2] + 1]
    if not casa:
        return
    for a in aste:
        p = None
        if a.get("lat") and a.get("lon"):
            p = (a["lat"], a["lon"])
        else:
            pr = a.get("provincia") or ""
            pr = SIGLA_NOME.get(pr.upper(), pr) if len(pr) == 2 else pr
            for d, k in ((citta, _norm(a.get("citta"))), (prov, _norm(pr))):
                if k and k in d:
                    p = (d[k][0], d[k][1])
                    break
        a["km"] = round(haversine(casa, p)) if p else None


# ---------------------------------------------------------------- doppioni tra portali

STOP = set("della delle dello degli dalla dalle nella nelle con per una uno sul sulla lotto lotti vendita asta beni bene "
           "marca modello tipo anno targa telaio presso come meglio descritto descritti inventario".split())


def _parole(t: str) -> set:
    return {w for w in re.findall(r"[a-z0-9]{4,}", (t or "").lower()) if w not in STOP}


def togli_doppioni(aste: list) -> list:
    """Se un'asta di Gobid/Industrial Discount/AsteBusiness è anche sul PVP (stessa chiusura ±1 giorno,
    titolo molto simile, stessa zona) tiene quella del PVP e ci aggiunge il link dell'altro portale."""
    pvp_per_giorno = {}
    for a in aste:
        if a["fonte"] == "PVP" and a.get("scadenza"):
            pvp_per_giorno.setdefault(a["scadenza"][:10], []).append(a)
    tenute, tolti = [], 0
    for a in aste:
        if a["fonte"] == "PVP" or not a.get("scadenza") or a["fonte"] == "Demanio":
            tenute.append(a)
            continue
        d = dt.date.fromisoformat(a["scadenza"][:10])
        pa = _parole(a["titolo"] + " " + a.get("descrizione", "")[:200])
        doppio = None
        for g in (d - dt.timedelta(days=1), d, d + dt.timedelta(days=1)):
            for b in pvp_per_giorno.get(g.isoformat(), []):
                if a.get("regione") and b.get("regione") and a["regione"] != b["regione"]:
                    continue
                pb = _parole(b["titolo"] + " " + b.get("descrizione", "")[:200])
                if pa and pb and len(pa & pb) / len(pa | pb) >= 0.5:
                    doppio = b
                    break
            if doppio:
                break
        if doppio:
            port = doppio.setdefault("portali", [])
            if not any(p.get("url") == a["url"] for p in port):
                port.append({"tipo": "A", "nome": a["fonte"], "url": a["url"]})
            if not doppio.get("immagine") and a.get("immagine"):
                doppio["immagine"] = a["immagine"]
            tolti += 1
        else:
            tenute.append(a)
    return tenute, tolti


# ---------------------------------------------------------------- archivio dei lotti chiusi

def archivia(archivio: list, chiuse: list, chiave_fn, oggi_iso: str, giorni: int = 365, massimo: int = 60000):
    """Aggiunge all'archivio le aste chiuse. Esito 'deserta' se lo stesso lotto torna all'asta."""
    gia = {x.get("id") for x in archivio}
    for a in chiuse:
        if a["id"] in gia:
            continue
        archivio.append({
            "id": a["id"], "k": chiave_fn(a), "t": a["titolo"][:140], "c": a["categoria"], "p": a.get("prezzo_base"),
            "d": (a.get("scadenza") or oggi_iso)[:10], "f": a["fonte"], "r": a.get("regione") or "",
            "luogo": a.get("citta") or "", "e": "?",
        })
    soglia = (dt.date.fromisoformat(oggi_iso) - dt.timedelta(days=giorni)).isoformat()
    archivio[:] = [x for x in archivio if x["d"] >= soglia][-massimo:]


def segna_deserte(archivio: list, aste_attive: list, chiave_fn):
    """Un lotto archiviato la cui procedura/lotto torna all'asta con una data successiva era andato deserto."""
    attive = {}
    for a in aste_attive:
        k = chiave_fn(a)
        if k:
            attive.setdefault(k, []).append((a.get("scadenza") or "")[:10])
    for x in archivio:
        if x.get("k") and x["e"] == "?" and any(d > x["d"] for d in attive.get(x["k"], [])):
            x["e"] = "deserta"


# ---------------------------------------------------------------- promemoria sulle aste che segui

SEGUITE = {"valutare", "sopralluogo", "offerta"}
SOGLIE = (48, 24, 2)


def promemoria(aste: list, utente: dict, inviati: dict, adesso: dt.datetime, invia, euro):
    """Messaggio a 48h, 24h e 2h dalla chiusura per le aste con la stella o con stato Da valutare/Sopralluogo/Offerta."""
    stati = {k: v.get("s") for k, v in (utente.get("stati") or {}).items() if isinstance(v, dict)}
    fav = {k for k, v in (utente.get("fav") or {}).items() if (v.get("v") if isinstance(v, dict) else v)}
    note = {k: v.get("t") for k, v in (utente.get("note") or {}).items() if isinstance(v, dict) and v.get("t")}
    per_id = {a["id"]: a for a in aste}
    n = 0
    for id_ in set(fav) | {k for k, s in stati.items() if s in SEGUITE}:
        a = per_id.get(id_)
        if not a or not a.get("scadenza"):
            continue
        try:
            fine = dt.datetime.fromisoformat(a["scadenza"] if len(a["scadenza"]) > 10 else a["scadenza"] + "T12:00")
        except ValueError:
            continue
        ore = (fine - adesso).total_seconds() / 3600
        if ore <= 0:
            continue
        gia = set(inviati.get(id_, []))
        dovute = [s for s in SOGLIE if ore <= s and s not in gia]
        if not dovute:
            continue
        inviati[id_] = sorted(gia | {s for s in SOGLIE if ore <= s})
        g = next((p for p in a.get("portali") or [] if p.get("tipo") == "V"), None)
        quando = f"{int(ore)} ore" if ore >= 2 else f"{max(1, int(ore * 60))} minuti"
        righe = [f"⏰ <b>Mancano {quando}</b> alla chiusura", f"<b>{html.escape(a['titolo'][:150])}</b>"]
        if stati.get(id_):
            righe.append("Stato: " + {"valutare": "Da valutare", "sopralluogo": "Sopralluogo", "offerta": "Offerta fatta"}.get(stati[id_], stati[id_]))
        if a.get("prezzo_base"):
            righe.append("Prezzo base " + euro(a["prezzo_base"]))
        if note.get(id_):
            righe.append("Nota: " + html.escape(note[id_][:300]))
        righe.append(f"<a href=\"{html.escape((g or {}).get('url') or a['url'])}\">Apri l'asta</a>")
        if invia("\n".join(righe)):
            n += 1
    for k in list(inviati):
        if k not in per_id:
            del inviati[k]
    return n


# ---------------------------------------------------------------- allarmi sulle fonti

def controlla_fonti(esito: dict, errori: dict, invia, soglia: int = 3):
    for nome, e in esito.items():
        n = errori.get(nome, 0)
        if e.get("ok"):
            if n >= soglia:
                invia(f"✅ <b>{html.escape(nome)}</b> funziona di nuovo dopo {n} controlli falliti.")
            errori[nome] = 0
        else:
            errori[nome] = n + 1
            if n + 1 == soglia:
                invia(f"⚠️ <b>{html.escape(nome)}</b> non funziona da {soglia} controlli di fila.\n"
                      f"Errore: {html.escape(str(e.get('errore', ''))[:300])}\n"
                      "Le altre fonti continuano a funzionare. Se dura, scrivimi.")


# ---------------------------------------------------------------- riepilogo del mattino

def riepilogo(aste: list, adesso: dt.datetime, euro) -> str:
    ieri = (adesso - dt.timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M")
    entro48 = (adesso + dt.timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M")
    ora = adesso.strftime("%Y-%m-%dT%H:%M")
    nuove = [a for a in aste if (a.get("visto_il") or "") >= ieri]
    lista = [a for a in nuove if a.get("lista")]
    occ = [a for a in nuove if a.get("occasione") and not a.get("lista")]
    chiudono = [a for a in aste if (a.get("lista") or a.get("occasione")) and ora <= (a.get("scadenza") or "") <= entro48]

    def riga(a):
        g = next((p for p in a.get("portali") or [] if p.get("tipo") == "V"), None)
        prezzo = euro(a["prezzo_base"]) if a.get("prezzo_base") else (f"−{a['ribasso_pct']}%" if a.get("ribasso_pct") else "")
        return f"• <a href=\"{html.escape((g or {}).get('url') or a['url'])}\">{html.escape(a['titolo'][:80])}</a> {html.escape(prezzo)}"

    out = [f"☀️ <b>Buongiorno! Le aste delle ultime 24 ore</b>", f"Nuove: <b>{len(nuove)}</b> · nella tua lista: <b>{len(lista)}</b> · occasioni: <b>{len(occ)}</b>"]
    if lista:
        out += ["", "<b>Nella tua lista</b>"] + [riga(a) for a in lista[:8]]
    if occ:
        out += ["", "<b>Occasioni</b>"] + [riga(a) for a in occ[:6]]
    if chiudono:
        out += ["", "<b>Chiudono entro 48 ore</b>"] + [riga(a) for a in sorted(chiudono, key=lambda a: a["scadenza"])[:6]]
    return "\n".join(out)


# ---------------------------------------------------------------- bandi PDF del Demanio

MESI = {m: i for i, m in enumerate(["gennaio", "febbraio", "marzo", "aprile", "maggio", "giugno", "luglio", "agosto",
                                    "settembre", "ottobre", "novembre", "dicembre"], 1)}


def _pulisci_pdf(t: str) -> str:
    t = t.replace(" ", " ").replace("’", "'")
    # "1. 0 00 ,00" -> "1.000,00" ; "1 1 novembre 2 0 2 6" -> "11 novembre 2026"
    for _ in range(3):
        t = re.sub(r"(?<=\d)[ \t]+(?=[\d.,])|(?<=\d[.,])[ \t]+(?=\d)", "", t)
    return t


def _euro(s: str) -> float | None:
    s = s.strip().rstrip(",.#")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def leggi_bando(testo: str) -> dict:
    t = _pulisci_pdf(testo)
    info = {}
    m = re.search(r"giorno\s+(\d{1,2})\s+([a-zà]+)\s+(\d{4}).{0,40}?ore\s*(\d{1,2})\s*[.:]\s*(\d{2})", t, re.I | re.S)
    if m and m.group(2).lower() in MESI:
        info["apertura"] = f"{m.group(3)}-{MESI[m.group(2).lower()]:02d}-{int(m.group(1)):02d}T{int(m.group(4)):02d}:{m.group(5)}"
    if re.search(r"offerte\s+segrete|busta\s+chiusa|plico", t, re.I):
        info["modalita"] = "Offerte segrete in busta chiusa (cartacea)"
    if re.search(r"modalit[aà]\s+telematica|piattaforma\s+telematica|asta\s+telematica", t, re.I):
        info["modalita"] = "Telematica"
    m = re.search(r"cauzional[ei].{0,160}?pari\s+al\s+(\d{1,2})\s*%", t, re.I | re.S) or \
        re.search(r"pari\s+al\s+(\d{1,2})\s*%\s*del(?:l'importo|\s+prezzo)", t, re.I)
    if m:
        info["cauzione_pct"] = int(m.group(1))
    m = re.search(r"entro\s+(\d{1,2})\s*(?:\(\w+\)\s*)?giorni\s+(?:lavorativi\s+)?(?:dalla|dal)", t, re.I)
    if m:
        info["pagamento_giorni"] = int(m.group(1))
    if re.search(r"visionabil", t, re.I):
        m = re.search(r"visionabil[^.]{0,200}\.", t, re.I | re.S)
        info["visione"] = re.sub(r"\s+", " ", m.group(0)).strip()[:220] if m else None

    lotti = []
    teste = list(re.finditer(r"LOTTO\s*(?:N\.?|N°|NR\.?)?\s*(\d{1,3})\b", t))
    for i, h in enumerate(teste):
        seg = t[h.end(): teste[i + 1].start() if i + 1 < len(teste) else len(t)]
        prima = t[max(0, h.start() - 700): h.start()]
        pm = re.search(r"PREZZO\s+(?:A\s+)?BASE\s*(?:D'?\s*ASTA)?\s*:?\s*€?\s*([\d.,]+)", seg, re.I)
        if not pm:
            continue
        n = int(h.group(1))
        ordinato = bool(re.search(r"MARCA|MODELLO|TIPOLOGIA", seg[:pm.start()], re.I))
        blocco = seg[:pm.start()] if ordinato else ""

        def campo(nome, testo=blocco):
            mm = re.search(nome + r"\s+([^\n]{2,60})", testo, re.I)
            return re.sub(r"\s+", " ", mm.group(1)).strip(" -–:") if mm else ""
        marca, modello = campo("MARCA"), campo("MODELLO")
        anno = ""
        mm = re.search(r"immatricolazione\s*:?\s*(?:\d{1,2}\s*/\s*\d{1,2}\s*/\s*)?(\d{4})", blocco, re.I)
        if mm:
            anno = mm.group(1)
        cond = campo(r"CONDIZIONI\s+GENERALI") or campo(r"Stato\s+d'uso:?")
        tipo = campo("TIPOLOGIA") or campo(r"Tipo\s+veicolo")
        dep = re.search(r"\(([A-Z]{2})\)", blocco)
        lotti.append({
            "n": n,
            "titolo": " ".join(x for x in [marca.title(), modello.title()] if x) or f"Lotto {n}",
            "anno": anno, "condizioni": cond.split("  ")[0][:40], "tipo": tipo[:40].title(),
            "prezzo": _euro(pm.group(1)),
            "sigla": dep.group(1) if dep else "",
            "revocato": bool(re.search(r"REVOCAT", seg[:pm.start() + 200], re.I)),
            "dettagli_nel_bando": not ordinato,
        })
    # un lotto può comparire più volte (es. nell'intestazione della busta): tengo il primo con prezzo
    visti, uniq = set(), []
    for x in lotti:
        if x["n"] not in visti:
            visti.add(x["n"])
            uniq.append(x)
    info["lotti"] = uniq
    return info


def testo_pdf(dati: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(dati))
    return "\n".join((p.extract_text() or "") for p in r.pages[:40])


# ---------------------------------------------------------------- stima del valore di oro e argento

ONCIA = 31.1035
# monete d'oro: grammi di oro FINO contenuti
MONETE_ORO = [  # (regex, grammi fini, nome)
    (r"krugerrand", ONCIA, "Krugerrand"), (r"maple ?leaf", ONCIA, "Maple Leaf"), (r"american eagle", ONCIA, "Eagle"),
    (r"filarmonica|philharmoniker", ONCIA, "Filarmonica"), (r"britannia", ONCIA, "Britannia"),
    (r"50 pesos", 37.5, "50 pesos"), (r"mezz[ae] sterlin[ae]|1/2 sterlin[ae]", 3.66, "mezza sterlina"),
    (r"sterlin[ae]", 7.32, "sterlina"), (r"marengh[io]|20 franchi|20 lire", 5.81, "marengo"),
    (r"10 franchi|10 lire", 2.90, "10 franchi"), (r"ducat[oi]", 3.44, "ducato"),
]
TITOLI_ORO = [  # (regex, purezza)
    (r"\b24\s?(?:k|kt|ct|carati)\b|\b999(?:[.,]9)?\b|oro (?:puro|fino)|lingott", 0.999),
    (r"\b22\s?(?:k|kt|ct|carati)\b|\b916\b", 0.916),
    (r"\b18\s?(?:k|kt|ct|carati)\b|\b750\b", 0.750),
    (r"\b14\s?(?:k|kt|ct|carati)\b|\b585\b", 0.585),
    (r"\b9\s?(?:k|kt|ct|carati)\b|\b375\b", 0.375),
]
_NUM = r"\d{1,3}(?:\.\d{3})+(?:,\d{1,3})?|\d+(?:[.,]\d{1,3})?"
RX_NUMERO = re.compile(_NUM)
RX_UNITA_PRIMA = re.compile(r"(?:\bpeso|\bpesa|\bgr|\bgrammi|\bg)\b[\s.:]*(?:(?:lordo|netto|complessivo|totale|circa|di|pari a|ca)\b[\s.:]*){0,3}$", re.I)
RX_UNITA_DOPO = re.compile(r"^\s*(?:gr\b\.?|grammi\b|g\b)", re.I)
RX_KG_PRIMA = re.compile(r"\bkg\b[\s.:]*$", re.I)
RX_KG_DOPO = re.compile(r"^\s*(?:kg|chili|chilogrammi)\b", re.I)
RX_CARATI = re.compile(r"(?:\bct|carat\w*|\bkt|\bk)[\s.:]*$", re.I)
RX_CARATI_DOPO = re.compile(r"^\s*(?:ct|kt|k|carat\w*)\b", re.I)
RX_COMPLESSIVO = re.compile(r"(?:complessiv\w*|totale)", re.I)


def pesi_nel_testo(t: str, anche_kg: bool):
    """Pesi in grammi scritti nel testo ("gr 25,4", "peso lordo 112 g", "kg 3,5")."""
    pesi = []
    for m in RX_NUMERO.finditer(t):
        prima, dopo = t[max(0, m.start() - 40):m.start()], t[m.end():m.end() + 12]
        if RX_CARATI.search(prima[-6:]) or RX_CARATI_DOPO.match(dopo):
            continue   # "ct 0,50" / "18 kt": carati, non peso
        v = _num(m.group(0))
        if v is None:
            continue
        if RX_UNITA_DOPO.match(dopo) or RX_UNITA_PRIMA.search(prima):
            pesi.append((v, bool(RX_COMPLESSIVO.search(prima[-30:]))))
        elif anche_kg and (RX_KG_DOPO.match(dopo) or RX_KG_PRIMA.search(prima)):
            pesi.append((v * 1000, bool(RX_COMPLESSIVO.search(prima[-30:]))))
    tot = [p for p, c in pesi if c]
    pesi = tot[:1] if tot else [p for p, _ in pesi]
    # "argento 925 gr 2.300": il 925 è il titolo, non un peso
    if len(pesi) > 1:
        pesi = [p for p in pesi if p not in (375, 585, 750, 800, 916, 925, 999)] or pesi
    return pesi


RX_ORO = re.compile(r"\boro\b|\bgold\b|lingott|sterlin|marengh|krugerrand", re.I)
RX_ARGENTO = re.compile(r"\bargent(?:o|eria|ato)\b|\bsilver\b", re.I)
RX_FALSO = re.compile(r"placcat|dorat|argentat|laminat|bigiotteria|similoro|\bplaqu|gold filled|vermeil|color oro|colore oro|tono oro", re.I)
RX_OROLOGIO = re.compile(r"orolog|cronograf", re.I)


def _num(s):
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+(?:,\d+)?", s):
        s = s.replace(".", "").replace(",", ".")      # 1.250 / 2.300,5
    else:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def prezzi_metalli(cfg: dict, stato: dict, get=None) -> dict:
    """Prezzo spot in €/grammo di oro e argento. Fonti gratuite senza chiave; se non rispondono si usa
    l'ultimo prezzo buono salvato, poi quello scritto a mano in privato.yaml (preziosi.oro_euro_grammo)."""
    import requests
    get = get or (lambda u: requests.get(u, timeout=20, headers={"User-Agent": "MonitorAste/1.0"}).json())
    out = {}
    try:
        j = get("https://xaus.com/api/v1/spot?currency=EUR&unit=gram")
        v = float((j.get("xau") or {}).get("price") or 0)
        if 20 < v < 1000:
            out["oro"] = v
    except Exception:  # noqa: BLE001
        pass
    try:
        usd_eur = float(get("https://api.frankfurter.app/latest?from=USD&to=EUR")["rates"]["EUR"])
        if "oro" not in out:
            v = float(get("https://api.gold-api.com/price/XAU")["price"]) * usd_eur / ONCIA
            if 20 < v < 1000:
                out["oro"] = v
        v = float(get("https://api.gold-api.com/price/XAG")["price"]) * usd_eur / ONCIA
        if 0.1 < v < 20:
            out["argento"] = v
    except Exception:  # noqa: BLE001
        pass
    salvati = stato.setdefault("prezzi_metalli", {})
    man = cfg.get("preziosi") or {}
    for k, chiave_man in (("oro", "oro_euro_grammo"), ("argento", "argento_euro_grammo")):
        if out.get(k):
            salvati[k] = round(out[k], 3)
        elif salvati.get(k):
            out[k] = salvati[k]
        elif man.get(chiave_man):
            out[k] = float(man[chiave_man])
    return out


def stima_preziosi(testo: str, prezzi: dict) -> dict | None:
    """Stima rapida del valore del metallo contenuto (oro o argento) leggendo peso e titolo dal testo.
    Non conta pietre, lavorazione e marca: è il valore 'a peso' (quello che pagherebbe un compro oro, più o meno)."""
    t = testo or ""
    if RX_FALSO.search(t) or RX_OROLOGIO.search(t):
        return None
    oro, argento = RX_ORO.search(t), RX_ARGENTO.search(t)
    if not (oro or argento):
        return None
    # monete d'oro: contenuto noto
    if oro and prezzi.get("oro"):
        for rx, fino, nome in MONETE_ORO:
            m = re.search(r"(?:\b(\d{1,4})\s*(?:x\s*)?(?:monet[ae]\s*(?:d.oro\s*)?(?:da\s*)?)?)?(?:" + rx + ")", t, re.I)
            if m:
                n = int(m.group(1)) if m.group(1) and 0 < int(m.group(1)) < 5000 else 1
                g = n * fino
                return {"valore": round(g * prezzi["oro"]), "grammi_fini": round(g, 1), "metallo": "oro",
                        "dettaglio": f"{n} × {nome} = {g:.1f} g di oro fino a € {prezzi['oro']:.0f}/g"}
    pesi = pesi_nel_testo(t, anche_kg=bool(argento and not oro))
    pesi = [p for p in pesi if p and 0.3 <= p <= 100000]
    if not pesi:
        return None
    grammi = sum(pesi)
    metallo = "oro" if oro else "argento"
    if metallo == "oro":
        purezza, presunto = 0.750, True
        for rx, pu in TITOLI_ORO:
            if re.search(rx, t, re.I):
                purezza, presunto = pu, False
                break
    else:
        purezza, presunto = (0.925, False) if re.search(r"\b925\b|sterling", t, re.I) else \
                            (0.800, not re.search(r"\b800\b", t))
    prezzo = prezzi.get(metallo)
    if not prezzo:
        return None
    fino = grammi * purezza
    tit = f"{round(purezza * 1000)}‰" + (" presunto" if presunto else "")
    return {"valore": round(fino * prezzo), "grammi_fini": round(fino, 1), "metallo": metallo,
            "dettaglio": f"{grammi:g} g di {metallo} {tit} a € {prezzo:.2f}/g" + (" · peso lordo, con pietre" if "lordo" in t.lower() else "")}


# ---------------------------------------------------------------- dintorni di un immobile (OpenStreetMap)

OVERPASS = ["https://overpass-api.de/api/interpreter", "https://overpass.kumi.systems/api/interpreter"]
_IMPIANTI = "chair_lift|gondola|cable_car|drag_lift|t-bar|j-bar|platter|mixed_lift"


def _query_dintorni(lat: float, lon: float) -> str:
    p = f"{lat:.6f},{lon:.6f}"
    return (f'[out:json][timeout:40];'
            f'way(around:4000,{p})[natural=coastline]->.c;'
            f'nwr(around:1500,{p})[natural=beach]->.b;'
            f'way(around:6000,{p})[aerialway~"^({_IMPIANTI})$"]->.a;'
            f'(.c;.b;.a;);out geom;'
            f'nwr(around:3000,{p})[amenity=university]->.u;'
            f'nwr(around:1500,{p})[railway=station][station!=subway]->.s;'
            f'nwr(around:1000,{p})[station=subway]->.m;'
            f'(.u;.s;.m;);out center;')


def _metri(a, b) -> float:
    """Distanza approssimata in metri (equirettangolare: va benissimo entro qualche km)."""
    k = math.cos(math.radians((a[0] + b[0]) / 2))
    return math.hypot((a[0] - b[0]) * 111_195, (a[1] - b[1]) * 111_195 * k)


def _al_segmento(p, a, b) -> float:
    k = math.cos(math.radians(p[0]))
    ax, ay, bx, by, px, py = a[1] * k, a[0], b[1] * k, b[0], p[1] * k, p[0]
    dx, dy = bx - ax, by - ay
    t = 0 if dx == dy == 0 else max(0, min(1, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return _metri(p, (ay + t * dy, (ax + t * dx) / k))


def _distanza(p, el) -> float | None:
    if el.get("geometry"):
        g = [(q["lat"], q["lon"]) for q in el["geometry"] if q]
        if len(g) == 1:
            return _metri(p, g[0])
        return min(_al_segmento(p, g[i], g[i + 1]) for i in range(len(g) - 1)) if g else None
    if el.get("members"):
        ds = [_distanza(p, m) for m in el["members"] if m.get("geometry")]
        ds = [d for d in ds if d is not None]
        return min(ds) if ds else None
    c = el.get("center") or ({"lat": el["lat"], "lon": el["lon"]} if "lat" in el else None)
    return _metri(p, (c["lat"], c["lon"])) if c else None


def dintorni(lat: float, lon: float, post=None) -> dict | None:
    """Cosa c'è vicino a un immobile: mare, impianti da sci, università, stazione (distanze in metri)."""
    import requests
    post = post or (lambda url, q: requests.post(url, data={"data": q}, timeout=60,
                                                 headers={"User-Agent": "MonitorAste/1.0 (uso personale)"}).json())
    q = _query_dintorni(lat, lon)
    j = None
    for url in OVERPASS:
        try:
            j = post(url, q)
            break
        except Exception:  # noqa: BLE001
            continue
    if j is None:
        return None
    p = (lat, lon)
    out = {}

    def migliore(chiave, d, nome=None):
        if d is not None and (chiave not in out or d < out[chiave]["m"]):
            out[chiave] = {"m": round(d), **({"nome": nome} if nome else {})}

    for el in j.get("elements", []):
        t = el.get("tags") or {}
        d = _distanza(p, el)
        if t.get("natural") in ("coastline", "beach"):
            migliore("mare", d)
        elif t.get("aerialway"):
            migliore("impianti", d, t.get("name"))
        elif t.get("amenity") == "university":
            migliore("universita", d, t.get("name"))
        elif t.get("station") == "subway":
            migliore("metro", d, t.get("name"))
        elif t.get("railway") == "station":
            migliore("stazione", d, t.get("name"))
    return out


def _dist(m: int) -> str:
    return f"{round(m / 10) * 10} m" if m < 1000 else f"{m / 1000:.1f} km".replace(".", ",")


def perche_interessa(a: dict, anno=None) -> list[str]:
    """Motivi brevi e concreti per cui un lotto può interessare (per la didascalia degli avvisi)."""
    r = []
    d = a.get("dintorni") or {}
    if d.get("mare") and d["mare"]["m"] <= 3000:
        r.append(f"{_dist(d['mare']['m'])} dal mare")
    if d.get("impianti") and d["impianti"]["m"] <= 5000:
        r.append(f"{_dist(d['impianti']['m'])} dagli impianti da sci")
    if d.get("universita") and d["universita"]["m"] <= 2500:
        nome = (d["universita"].get("nome") or "università")
        nome = nome if len(nome) <= 38 else "università"
        r.append(f"{_dist(d['universita']['m'])} da {nome}")
    if d.get("metro") and d["metro"]["m"] <= 800:
        r.append(f"metro {d['metro'].get('nome') or ''} a {_dist(d['metro']['m'])}".replace("  ", " "))
    elif d.get("stazione") and d["stazione"]["m"] <= 1000:
        r.append(f"stazione a {_dist(d['stazione']['m'])}")
    if a.get("localita_turistica") and not (d.get("mare") or d.get("impianti")):
        r.append(a["localita_turistica"])
    if anno and a.get("categoria") in ("auto_moto", "nautica"):
        r.insert(0, f"anno {anno}")
    return r[:3]
