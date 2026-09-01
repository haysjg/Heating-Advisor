"""
Client IMAP (lecture seule) pour le suivi électricité — remplace l'ancien flow OAuth Gmail,
bloqué par la Protection avancée Google sur le compte Gmail personnel (aucun "continuer
quand même" possible sur les scopes sensibles pour un compte APP, même app perso).

Les emails "flash élec" Lite sont transférés (filtre Gmail) vers une boîte tierce (ex. FAI)
consultée ici en IMAP standard. stdlib uniquement (imaplib + email), comme le reste du projet.
"""

import email
import imaplib
import logging
from datetime import datetime, timedelta
from email.header import decode_header
from email.message import Message

from modules.crypto import decrypt_password

logger = logging.getLogger(__name__)

_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _imap_date(days_ago: int) -> str:
    """Formate une date au format IMAP SEARCH (ex. '01-Jan-2021'), toujours en anglais."""
    d = datetime.now() - timedelta(days=days_ago)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def is_connected(electricity_cfg: dict) -> bool:
    """True si des identifiants IMAP sont stockés (statut affiché côté UI, jamais le mot de passe)."""
    return bool(electricity_cfg.get("imap_user") and electricity_cfg.get("imap_password"))


def open_connection(electricity_cfg: dict) -> imaplib.IMAP4_SSL | None:
    """Ouvre et authentifie une connexion IMAP en lecture seule. None si échec."""
    host = electricity_cfg.get("imap_host", "").strip()
    user = electricity_cfg.get("imap_user", "").strip()
    password = decrypt_password(electricity_cfg.get("imap_password", ""))
    if not host or not user or not password:
        return None
    try:
        conn = imaplib.IMAP4_SSL(host, int(electricity_cfg.get("imap_port", 993)), timeout=15)
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        return conn
    except Exception as e:
        logger.warning("IMAP électricité : échec connexion — %s", e)
        return None


def close_connection(conn: imaplib.IMAP4_SSL | None) -> None:
    if not conn:
        return
    try:
        conn.logout()
    except Exception:
        pass


def test_connection(electricity_cfg: dict) -> tuple[bool, str]:
    """Teste la connexion IMAP avec les identifiants fournis. Retourne (succès, message)."""
    conn = open_connection(electricity_cfg)
    if not conn:
        return False, "Connexion échouée — vérifiez host/port/identifiants"
    close_connection(conn)
    return True, "Connexion réussie"


def _decode_subject(raw: str) -> str:
    if not raw:
        return ""
    decoded = ""
    for text, enc in decode_header(raw):
        if isinstance(text, bytes):
            decoded += text.decode(enc or "utf-8", errors="replace")
        else:
            decoded += text
    return decoded


def list_matching_messages(conn: imaplib.IMAP4_SSL, sender_filter: str, subject_filter: str, lookback_days: int) -> list[dict]:
    """Liste [{'uid', 'message_id'}] des messages du sender donné sur les N derniers jours,
    dont le sujet (décodé) contient subject_filter.

    Le filtre sujet est appliqué côté client (après décodage MIME) plutôt que dans la requête
    IMAP SEARCH pour éviter les soucis d'encodage des accents ("flash élec") selon les serveurs.
    """
    since = _imap_date(int(lookback_days))
    criteria = f'(FROM "{sender_filter}" SINCE "{since}")'
    try:
        status, data = conn.search(None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        subject_lower = subject_filter.lower()
        matches = []
        for uid in uids:
            status, hdata = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT MESSAGE-ID)])")
            if status != "OK" or not hdata or not hdata[0] or not isinstance(hdata[0], tuple):
                continue
            header_msg = email.message_from_bytes(hdata[0][1])
            subject = _decode_subject(header_msg.get("Subject", ""))
            if subject_lower not in subject.lower():
                continue
            message_id = (header_msg.get("Message-ID") or "").strip().strip("<>") or uid.decode()
            matches.append({"uid": uid, "message_id": message_id})
        return matches
    except Exception as e:
        logger.warning("IMAP électricité : erreur listage messages — %s", e)
        return []


def fetch_message(conn: imaplib.IMAP4_SSL, uid: bytes) -> Message | None:
    """Récupère le message complet (headers + corps MIME) pour un UID donné."""
    try:
        status, mdata = conn.fetch(uid, "(BODY.PEEK[])")
        if status != "OK" or not mdata or not mdata[0] or not isinstance(mdata[0], tuple):
            return None
        return email.message_from_bytes(mdata[0][1])
    except Exception as e:
        logger.warning("IMAP électricité : erreur récupération message %s — %s", uid, e)
        return None
