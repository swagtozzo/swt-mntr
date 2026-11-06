"""
Cifratura dei dati personali del monitor (lista, soglie, dati della dashboard, stati).

AES-256-GCM con chiave derivata dalla password (PBKDF2-SHA256).
Lo stesso formato è decifrato nel browser dalla dashboard (WebCrypto).

Formato file: JSON {"v":1,"kdf":"pbkdf2-sha256","iter":N,"salt":b64,"iv":b64,"gz":bool,"ct":b64}
"""
from __future__ import annotations

import base64
import gzip
import json
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITER = 250_000
_cache: dict = {}


def _chiave(password: str, salt: bytes, iterazioni: int) -> bytes:
    k = (password, salt, iterazioni)
    if k not in _cache:
        _cache[k] = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=iterazioni).derive(password.encode())
    return _cache[k]


def cifra(dati, password: str, comprimi: bool = True) -> str:
    grezzo = json.dumps(dati, ensure_ascii=False, separators=(",", ":")).encode()
    if comprimi:
        grezzo = gzip.compress(grezzo, 9, mtime=0)
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_chiave(password, salt, ITER)).encrypt(iv, grezzo, None)
    b = lambda x: base64.b64encode(x).decode()  # noqa: E731
    return json.dumps({"v": 1, "kdf": "pbkdf2-sha256", "iter": ITER, "salt": b(salt), "iv": b(iv), "gz": comprimi, "ct": b(ct)})


def decifra(testo: str, password: str):
    d = json.loads(testo)
    salt, iv, ct = (base64.b64decode(d[k]) for k in ("salt", "iv", "ct"))
    grezzo = AESGCM(_chiave(password, salt, int(d["iter"]))).decrypt(iv, ct, None)
    if d.get("gz"):
        grezzo = gzip.decompress(grezzo)
    return json.loads(grezzo)


def e_cifrato(testo: str) -> bool:
    try:
        d = json.loads(testo)
        return isinstance(d, dict) and d.get("v") == 1 and "ct" in d
    except ValueError:
        return False
