#!/usr/bin/env python3
"""
Monitor Aste — comandi Telegram.

Gira su GitHub Actions ogni 5 minuti: legge i messaggi arrivati al bot dalla TUA chat
(TELEGRAM_CHAT_ID), esegue i comandi, salva le modifiche in preferenze.json
(che il monitor applica sopra config.yaml) e risponde.

Scrive in $GITHUB_OUTPUT:
  modificato=true|false   -> preferenze.json cambiato, va fatto il commit
  aggiorna=true|false     -> rilanciare subito il monitor
  conferma=<offset>       -> da confermare a Telegram dopo il commit
"""
from __future__ import annotations

import copy
import html
import json
import os
import re
import sys
from pathlib import Path

import requests
import yaml

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.yaml"
PREF = ROOT / "preferenze.json"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT = str(os.environ.get("TELEGRAM_CHAT_ID", "")).strip()
API = f"https://api.telegram.org/bot{TOKEN}"
DATI_URL = os.environ.get("DATI_URL", "")

CATEGORIE = ["auto_moto", "immobili", "macchinari", "orologi_gioielli", "moda_lusso", "sport_outdoor", "nautica"]
COMANDI = [
    ("aiuto", "Elenco dei comandi"),
    ("stato", "Ultimo controllo, fonti e impostazioni principali"),
    ("cerca", "Cerca tra le aste attive: /cerca rolex"),
    ("lista", "La tua lista desideri"),
    ("aggiungi", "Nuova voce: /aggiungi Nome | parole | prezzo | regioni"),
    ("togli", "Togli una voce: /togli 3"),
    ("prezzo", "Prezzo massimo di una voce: /prezzo 3 5000"),
    ("zona", "Regioni di una voce: /zona 3 Abruzzo, Marche"),
    ("parole", "Le parole chiave"),
    ("parola", "Aggiungi parole chiave: /parola omega, tudor"),
    ("toglparola", "Togli parole chiave: /toglparola tudor"),
    ("escludi", "Parole che escludono un lotto: /escludi rottame"),
    ("includi", "Togli un'esclusione: /includi rottame"),
    ("casa", "Regioni vicino a casa: /casa Abruzzo, Marche"),
    ("punti", "Segnali minimi per un'Occasione: /punti 3"),
    ("fonti", "Fonti attive"),
    ("fonte", "Accendi o spegni una fonte: /fonte demanio off"),
    ("pausa", "Sospendi gli avvisi"),
    ("riprendi", "Riattiva gli avvisi"),
    ("aggiorna", "Controlla i portali adesso"),
    ("reset", "Torna alle impostazioni di config.yaml"),
]


def tg(metodo, **dati):
    r = requests.post(f"{API}/{metodo}", data=dati, timeout=30)
    try:
        return r.json()
    except ValueError:
        return {"ok": False}


def rispondi(testo):
    for i in range(0, len(testo), 3800):
        tg("sendMessage", chat_id=CHAT, text=testo[i:i + 3800], parse_mode="HTML", disable_web_page_preview="true")


def e(s):
    return html.escape(str(s))


def lista_parole(s):
    return [x.strip() for x in re.split(r"[,;\n]", s or "") if x.strip()]


def euro(v):
    return "nessun limite" if not v else "€ " + f"{float(v):,.0f}".replace(",", ".")


class Impostazioni:
    """config.yaml + preferenze.json: le chiavi in preferenze.json vincono."""

    def __init__(self):
        self.base = yaml.safe_load(CONFIG.read_text(encoding="utf-8")) or {}
        self.pref = json.loads(PREF.read_text(encoding="utf-8")) if PREF.exists() else {}
        self.cambiato = False

    def get(self, k, default=None):
        if k in self.pref:
            return copy.deepcopy(self.pref[k])
        return copy.deepcopy(self.base.get(k, default))

    def set(self, k, v):
        self.pref[k] = v
        self.cambiato = True

    def salva(self):
        if self.cambiato:
            PREF.write_text(json.dumps(self.pref, ensure_ascii=False, indent=1), encoding="utf-8")


def voce_txt(i, v):
    righe = [f"<b>{i}. {e(v.get('nome'))}</b>", "   " + e(", ".join(v.get("parole") or []))]
    extra = []
    if v.get("prezzo_max"):
        extra.append("max " + euro(v["prezzo_max"]))
    if v.get("regioni"):
        extra.append(", ".join(v["regioni"]))
    if v.get("escludi"):
        extra.append("escludi: " + ", ".join(v["escludi"]))
    if extra:
        righe.append("   <i>" + e(" · ".join(extra)) + "</i>")
    return "\n".join(righe)


def trova_voce(lista, rif):
    rif = (rif or "").strip()
    if rif.isdigit() and 1 <= int(rif) <= len(lista):
        return int(rif) - 1
    for i, v in enumerate(lista):
        if (v.get("nome") or "").lower() == rif.lower():
            return i
    for i, v in enumerate(lista):
        if rif and rif.lower() in (v.get("nome") or "").lower():
            return i
    return None


def separa_voce(lista, arg):
    """'3 5000' oppure 'Seiko SKX Abruzzo, Marche' -> (indice, resto). Col nome vince la corrispondenza più lunga."""
    arg = (arg or "").strip()
    m = re.match(r"(\d+)\s*(.*)$", arg, re.S)
    if m and 1 <= int(m.group(1)) <= len(lista):
        return int(m.group(1)) - 1, m.group(2).strip()
    low = arg.lower()
    migliore = None
    for i, v in enumerate(lista):
        n = (v.get("nome") or "").lower()
        if n and (low == n or low.startswith(n + " ") or low.startswith(n + ",")) and (migliore is None or len(n) > migliore[1]):
            migliore = (i, len(n))
    if migliore:
        return migliore[0], arg[migliore[1]:].lstrip(" ,").strip()
    return None, arg


def esegui(cmd, arg, imp: Impostazioni, flag: dict) -> str:
    if cmd in ("start", "aiuto", "help"):
        return "<b>Comandi del monitor aste</b>\n" + "\n".join(f"/{c} – {e(d)}" for c, d in COMANDI) + \
               "\n\nLe modifiche valgono dal prossimo controllo dei portali (o subito con /aggiorna)."

    if cmd == "stato":
        righe = []
        try:
            d = requests.get(DATI_URL, timeout=60).json() if DATI_URL else {}
            righe.append(f"Ultimo controllo: <b>{e(d.get('aggiornato', '?').replace('T', ' '))}</b>")
            righe.append(f"Aste in dashboard: <b>{len(d.get('aste', []))}</b>")
            n_occ = sum(1 for a in d.get("aste", []) if a.get("occasione"))
            n_lis = sum(1 for a in d.get("aste", []) if a.get("lista"))
            righe.append(f"Occasioni: <b>{n_occ}</b> · nella tua lista: <b>{n_lis}</b>")
            for k, v in (d.get("fonti") or {}).items():
                righe.append(("🟢 " if v.get("ok") else "🔴 ") + e(k) + ("" if v.get("ok") else " – " + e(v.get("errore", ""))))
        except Exception:  # noqa: BLE001
            righe.append("Dati della dashboard non raggiungibili in questo momento.")
        righe.append("")
        righe.append("Avvisi: " + ("<b>in pausa</b>" if imp.get("avvisi_in_pausa") else "attivi"))
        righe.append(f"Voci in lista: {len(imp.get('lista') or [])} · parole chiave: {len(imp.get('parole_chiave') or [])}")
        occ = imp.get("occasioni") or {}
        righe.append(f"Occasione da {occ.get('punti_min', 2)} segnali · casa: {', '.join(occ.get('regioni_casa') or []) or '—'}")
        return "\n".join(righe)

    if cmd == "cerca":
        if not arg:
            return "Scrivi cosa cercare, per esempio: /cerca rolex"
        try:
            d = requests.get(DATI_URL, timeout=60).json()
        except Exception:  # noqa: BLE001
            return "Dati della dashboard non raggiungibili in questo momento."
        parole = arg.lower().split()
        trov = [a for a in d.get("aste", []) if all(p in (a.get("titolo", "") + " " + a.get("descrizione", "") + " " +
                                                          a.get("citta", "") + " " + a.get("regione", "")).lower() for p in parole)]
        trov.sort(key=lambda a: a.get("scadenza") or "9999")
        if not trov:
            return f"Nessuna asta attiva con «{e(arg)}»."
        out = [f"<b>{len(trov)}</b> aste con «{e(arg)}» (le prime 8 per chiusura):"]
        for a in trov[:8]:
            g = next((p for p in a.get("portali") or [] if p.get("tipo") == "V"), None)
            prezzo = euro(a["prezzo_base"]) if a.get("prezzo_base") else (f"ribasso {a['ribasso_pct']}%" if a.get("ribasso_pct") else "prezzo nella scheda")
            out.append(f"\n• <a href=\"{e(g['url'] if g else a['url'])}\">{e(a['titolo'][:90])}</a>\n  {e(prezzo)} · chiude {e((a.get('scadenza') or '?').replace('T', ' '))} · {e(a.get('citta') or a.get('regione') or '')}")
        return "\n".join(out)

    if cmd == "lista":
        lista = imp.get("lista") or []
        if not lista:
            return "La lista è vuota. Aggiungi con /aggiungi Nome | parole | prezzo | regioni"
        return "<b>La tua lista</b>\n\n" + "\n".join(voce_txt(i + 1, v) for i, v in enumerate(lista)) + \
               "\n\nModifica con /prezzo, /zona, /togli e il numero della voce."

    if cmd == "aggiungi":
        parti = [p.strip() for p in arg.split("|")]
        if not parti or not parti[0]:
            return ("Formato: /aggiungi Nome | parole separate da virgola | prezzo massimo | regioni\n"
                    "Esempio: /aggiungi Seiko SKX | skx007, skx009, skx 007 | 350 | \n"
                    "Solo il nome è obbligatorio: se non scrivi parole, uso il nome.")
        nome = parti[0]
        parole = lista_parole(parti[1]) if len(parti) > 1 and parti[1] else [nome]
        voce = {"nome": nome, "parole": parole}
        if len(parti) > 2 and parti[2]:
            try:
                voce["prezzo_max"] = int(float(re.sub(r"[^\d.,]", "", parti[2]).replace(".", "").replace(",", ".")))
            except ValueError:
                return "Il prezzo massimo deve essere un numero, per esempio 5000."
        if len(parti) > 3 and parti[3]:
            voce["regioni"] = lista_parole(parti[3])
        lista = imp.get("lista") or []
        if trova_voce(lista, nome) is not None and lista[trova_voce(lista, nome)]["nome"].lower() == nome.lower():
            return f"«{e(nome)}» è già nella lista. Usa /prezzo o /zona per modificarla."
        lista.append(voce)
        imp.set("lista", lista)
        return "Aggiunta alla lista:\n" + voce_txt(len(lista), voce)

    if cmd == "togli":
        lista = imp.get("lista") or []
        i = trova_voce(lista, arg)
        if i is None:
            return "Non trovo quella voce. Scrivi /lista per vedere i numeri."
        v = lista.pop(i)
        imp.set("lista", lista)
        return f"Tolta dalla lista: <b>{e(v['nome'])}</b>"

    if cmd in ("prezzo", "zona"):
        lista = imp.get("lista") or []
        i, val = separa_voce(lista, arg)
        if i is None:
            return f"Non trovo quella voce. Formato: /{cmd} numero_voce valore (scrivi /lista per i numeri)."
        if cmd == "prezzo":
            try:
                n = int(float(re.sub(r"[^\d.,]", "", val).replace(".", "").replace(",", "."))) if val else 0
            except ValueError:
                return "Il prezzo deve essere un numero, per esempio /prezzo 3 5000 (0 = nessun limite)."
            if n:
                lista[i]["prezzo_max"] = n
            else:
                lista[i].pop("prezzo_max", None)
        else:
            reg = lista_parole(val)
            if reg:
                lista[i]["regioni"] = reg
            else:
                lista[i].pop("regioni", None)
        imp.set("lista", lista)
        return "Aggiornata:\n" + voce_txt(i + 1, lista[i])

    if cmd == "parole":
        p = imp.get("parole_chiave") or []
        return "<b>Parole chiave</b> (" + str(len(p)) + ")\n" + e(", ".join(p)) + "\n\nAggiungi con /parola, togli con /toglparola."

    if cmd in ("parola", "toglparola", "escludi", "includi"):
        chiave = "parole_chiave" if cmd in ("parola", "toglparola") else "parole_escluse"
        nuove = lista_parole(arg)
        if not nuove:
            return f"Scrivi le parole dopo il comando, separate da virgola: /{cmd} omega, tudor"
        cur = imp.get(chiave) or []
        low = [x.lower() for x in cur]
        if cmd in ("parola", "escludi"):
            agg = [w for w in nuove if w.lower() not in low]
            cur += agg
            imp.set(chiave, cur)
            fatto = "Aggiunte" if cmd == "parola" else "Ora escludo"
            return f"{fatto}: {e(', '.join(agg)) or 'nessuna (c’erano già)'}"
        tolte = [w for w in nuove if w.lower() in low]
        cur = [x for x in cur if x.lower() not in {w.lower() for w in nuove}]
        imp.set(chiave, cur)
        return f"Tolte: {e(', '.join(tolte)) or 'nessuna (non c’erano)'}"

    if cmd in ("casa", "punti"):
        occ = imp.get("occasioni") or {}
        if cmd == "casa":
            occ["regioni_casa"] = lista_parole(arg)
            imp.set("occasioni", occ)
            return "Regioni vicino a casa: " + (e(", ".join(occ["regioni_casa"])) or "nessuna")
        if not (arg or "").strip().isdigit():
            return "Scrivi un numero, per esempio /punti 3"
        occ["punti_min"] = int(arg.strip())
        imp.set("occasioni", occ)
        return f"Ora un lotto è un'Occasione con almeno <b>{occ['punti_min']}</b> segnali."

    if cmd in ("fonti", "fonte"):
        fonti = imp.get("fonti") or {}
        if cmd == "fonti" or not arg:
            return "<b>Fonti</b>\n" + "\n".join(("🟢 " if v else "⚪ ") + e(k) for k, v in fonti.items()) + \
                   "\n\nAccendi o spegni con /fonte nome on oppure off"
        m = re.match(r"\s*(.+?)\s+(on|off|si|sì|no|accesa|spenta)\s*$", arg, re.I)
        if not m:
            return "Formato: /fonte demanio off"
        nome = m.group(1).lower().replace(" ", "_")
        if nome not in fonti:
            return "Fonte sconosciuta. Quelle disponibili: " + e(", ".join(fonti))
        fonti[nome] = m.group(2).lower() in ("on", "si", "sì", "accesa")
        imp.set("fonti", fonti)
        return f"{e(nome)}: {'accesa' if fonti[nome] else 'spenta'}"

    if cmd == "pausa":
        imp.set("avvisi_in_pausa", True)
        return "Avvisi in pausa. La dashboard continua ad aggiornarsi. /riprendi per riattivarli."
    if cmd == "riprendi":
        imp.set("avvisi_in_pausa", False)
        return "Avvisi riattivati."

    if cmd == "aggiorna":
        flag["aggiorna"] = True
        return "Avvio un controllo dei portali adesso: ci vogliono alcuni minuti."

    if cmd == "reset":
        if not imp.pref:
            return "Stai già usando le impostazioni di config.yaml."
        imp.pref = {}
        imp.cambiato = True
        return "Fatto: tutte le modifiche fatte dal bot sono state tolte, valgono di nuovo quelle di config.yaml."

    return "Comando sconosciuto. Scrivi /aiuto per l'elenco."


def output(k, v):
    f = os.environ.get("GITHUB_OUTPUT")
    if f:
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"{k}={v}\n")
    print(f"{k}={v}")


def main():
    if not TOKEN or not CHAT:
        print("TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID mancanti")
        sys.exit(0)
    r = tg("getUpdates", timeout=0, allowed_updates=json.dumps(["message"]))
    upd = r.get("result") or []
    if not upd:
        output("modificato", "false"); output("aggiorna", "false"); output("conferma", "")
        return
    # menù dei comandi (una volta ogni tanto non fa male)
    tg("setMyCommands", commands=json.dumps([{"command": c, "description": d[:250]} for c, d in COMANDI]))
    imp = Impostazioni()
    flag = {"aggiorna": False}
    for u in upd:
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT:
            continue  # messaggi da altre chat: ignorati
        testo = (msg.get("text") or "").strip()
        m = re.match(r"^/([A-Za-zàèéìòù_]+)(?:@\w+)?\s*(.*)$", testo, re.S)
        if not m:
            rispondi("Usa i comandi: scrivi /aiuto per l'elenco.")
            continue
        try:
            risposta = esegui(m.group(1).lower(), m.group(2).strip(), imp, flag)
        except Exception as ex:  # noqa: BLE001
            risposta = "Errore nell'eseguire il comando: " + e(ex)
        rispondi(risposta)
    imp.salva()
    if imp.cambiato:
        flag["aggiorna"] = True
    output("modificato", "true" if imp.cambiato else "false")
    output("aggiorna", "true" if flag["aggiorna"] else "false")
    output("conferma", str(upd[-1]["update_id"] + 1))


if __name__ == "__main__":
    main()
