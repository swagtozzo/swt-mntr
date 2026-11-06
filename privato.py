#!/usr/bin/env python3
"""
Impostazioni personali del monitor aste.

  python privato.py chiudi   -> legge privato.yaml e crea privato.enc (cifrato con la tua password)
  python privato.py apri     -> decifra privato.enc e ricrea privato.yaml per modificarlo

La password deve essere la stessa del secret CHIAVE_DATI su GitHub.
privato.yaml resta solo sul PC (è in .gitignore); su GitHub va solo privato.enc.
"""
import getpass
import sys
from pathlib import Path

import yaml

from cifra import cifra, decifra

ROOT = Path(__file__).resolve().parent
YAML_F, ENC_F = ROOT / "privato.yaml", ROOT / "privato.enc"


def chiedi_password(conferma: bool) -> str:
    p = getpass.getpass("Password delle impostazioni (la stessa del secret CHIAVE_DATI): ")
    if len(p) < 10:
        print("Usa una password di almeno 10 caratteri.")
        sys.exit(1)
    if conferma and getpass.getpass("Ripetila: ") != p:
        print("Le due password non coincidono.")
        sys.exit(1)
    return p


def main():
    azione = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if azione == "chiudi":
        if not YAML_F.exists():
            print("Non trovo privato.yaml in questa cartella.")
            sys.exit(1)
        try:
            dati = yaml.safe_load(YAML_F.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as e:
            print("privato.yaml contiene un errore di scrittura:\n", e)
            sys.exit(1)
        pw = chiedi_password(conferma=not ENC_F.exists())
        if ENC_F.exists():
            try:
                decifra(ENC_F.read_text(encoding="utf-8"), pw)
            except Exception:  # noqa: BLE001
                print("La password non è quella usata finora per privato.enc. Nessuna modifica fatta.")
                sys.exit(1)
        ENC_F.write_text(cifra(dati, pw), encoding="utf-8")
        print(f"Fatto: creato privato.enc ({len(dati.get('lista') or [])} voci in lista).")
        print("Ora in GitHub Desktop fai Commit e Push: su GitHub va solo il file cifrato.")
    elif azione == "apri":
        if not ENC_F.exists():
            print("Non c'è ancora privato.enc: modifica privato.yaml e usa 'chiudi'.")
            sys.exit(1)
        pw = chiedi_password(conferma=False)
        try:
            dati = decifra(ENC_F.read_text(encoding="utf-8"), pw)
        except Exception:  # noqa: BLE001
            print("Password sbagliata.")
            sys.exit(1)
        YAML_F.write_text("# Impostazioni personali (solo sul PC). Dopo le modifiche: Cifra impostazioni.bat\n" +
                          yaml.safe_dump(dati, allow_unicode=True, sort_keys=False, width=120), encoding="utf-8")
        print("Fatto: privato.yaml aggiornato con le impostazioni attuali (comprese quelle cambiate dal bot).")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
