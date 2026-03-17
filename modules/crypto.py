"""
Chiffrement du mot de passe SMTP.

- Clé maître 32 octets stockée dans /app/data/.secret_key (auto-générée au premier démarrage)
- Chaque mot de passe est chiffré avec un sel aléatoire 16 octets (PBKDF2-SHA256 → Fernet/AES-128)
- Format stocké : "enc:v1:<base64(sel || token_fernet)>"
- Les valeurs non préfixées sont traitées comme du texte clair (rétrocompatibilité)
"""

import base64
import logging
import os
import secrets

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

_KEY_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", ".secret_key")
_PREFIX = "enc:v1:"
_PBKDF2_ITERATIONS = 260_000


def _get_master_key() -> bytes:
    """Charge ou génère la clé maître (32 octets, encodée en base64 url-safe)."""
    if os.path.exists(_KEY_FILE):
        with open(_KEY_FILE, "rb") as f:
            return f.read().strip()
    raw = secrets.token_bytes(32)
    encoded = base64.urlsafe_b64encode(raw)
    os.makedirs(os.path.dirname(_KEY_FILE), exist_ok=True)
    with open(_KEY_FILE, "wb") as f:
        f.write(encoded)
    logger.info("Clé maître générée dans %s", _KEY_FILE)
    return encoded


def _derive_fernet_key(master: bytes, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(master))


def encrypt_password(plaintext: str) -> str:
    """Chiffre un mot de passe. Retourne la valeur telle quelle si déjà chiffrée."""
    if not plaintext or plaintext.startswith(_PREFIX):
        return plaintext
    master = _get_master_key()
    salt = secrets.token_bytes(16)
    fernet_key = _derive_fernet_key(master, salt)
    token = Fernet(fernet_key).encrypt(plaintext.encode())
    payload = base64.urlsafe_b64encode(salt + token).decode()
    return f"{_PREFIX}{payload}"


def decrypt_password(stored: str) -> str:
    """Déchiffre un mot de passe. Retourne la valeur telle quelle si non chiffrée."""
    if not stored or not stored.startswith(_PREFIX):
        return stored
    try:
        master = _get_master_key()
        raw = base64.urlsafe_b64decode(stored[len(_PREFIX):])
        salt, token = raw[:16], raw[16:]
        fernet_key = _derive_fernet_key(master, salt)
        return Fernet(fernet_key).decrypt(token).decode()
    except (InvalidToken, Exception) as e:
        logger.error("Impossible de déchiffrer le mot de passe SMTP : %s", e)
        return ""


def is_configured(stored: str) -> bool:
    """Retourne True si un mot de passe réel est configuré (chiffré ou en clair non-placeholder)."""
    placeholder = "REMPLACER_PAR_MOT_DE_PASSE_APP"
    return bool(stored) and stored != placeholder
