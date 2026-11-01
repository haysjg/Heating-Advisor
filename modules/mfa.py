"""
Double authentification TOTP (Google Authenticator, Aegis, Bitwarden…).

- Secret base32 généré par pyotp, stocké chiffré (modules.crypto) dans AUTH.totp_secret
- Codes à 6 chiffres, pas de 30 s, tolérance d'un pas avant/après (décalage d'horloge)
- Anti-rejeu : le pas de temps du dernier code accepté est mémorisé, un code déjà
  utilisé (ou plus ancien) est refusé
- Codes de secours à usage unique, stockés hachés (SHA-256 : ils sont aléatoires et
  longs, un hachage lent n'apporterait rien)
"""

import hashlib
import hmac
import secrets
import time

import pyotp
import segno

ISSUER = "Heating Advisor"
ACCOUNT = "admin"
RECOVERY_CODE_COUNT = 8
_RECOVERY_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # sans 0/o, 1/l/i


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str) -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=ACCOUNT, issuer_name=ISSUER)


def qr_svg(uri: str) -> str:
    """QR code à scanner avec l'application d'authentification (SVG inline)."""
    return segno.make(uri, error="m").svg_inline(scale=5, border=2, dark="#000", light="#fff")


def normalize_code(code: str) -> str:
    return "".join(str(code or "").split()).replace("-", "").lower()


def is_totp_format(code: str) -> bool:
    code = normalize_code(code)
    return len(code) == 6 and code.isdigit()


def verify_totp(secret: str, code: str, last_step: int = 0) -> int | None:
    """Vérifie un code TOTP. Renvoie le pas de temps accepté, ou None.

    Un code dont le pas est inférieur ou égal à last_step est refusé (rejeu).
    """
    code = normalize_code(code)
    if not secret or not is_totp_format(code):
        return None
    totp = pyotp.TOTP(secret)
    now_step = int(time.time()) // totp.interval
    for step in (now_step - 1, now_step, now_step + 1):
        if step <= (last_step or 0):
            continue
        if hmac.compare_digest(totp.generate_otp(step), code):
            return step
    return None


def generate_recovery_codes() -> tuple[list[str], list[str]]:
    """Renvoie (codes en clair à montrer une fois, hachés à stocker)."""
    codes = []
    for _ in range(RECOVERY_CODE_COUNT):
        raw = "".join(secrets.choice(_RECOVERY_ALPHABET) for _ in range(10))
        codes.append(f"{raw[:5]}-{raw[5:]}")
    return codes, [_hash_recovery(c) for c in codes]


def consume_recovery_code(hashes: list, code: str) -> list | None:
    """Si code correspond à un code de secours, renvoie la liste sans lui ; sinon None."""
    candidate = _hash_recovery(code)
    for h in hashes or []:
        if hmac.compare_digest(h, candidate):
            return [x for x in hashes if x != h]
    return None


def _hash_recovery(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()
