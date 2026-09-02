"""
Suivi de la consommation électrique via parsing des emails "flash élec" (Lite),
transférés depuis Gmail vers une boîte tierce consultée en IMAP (cf. modules/imap_client.py).

Deux familles de fonctions :
- parsing (pures, sans I/O) : extraction kWh / période / delta depuis un email.message.Message
- historisation + orchestration : SQLite (table electricity_readings, réutilise data/history.db)
  et fetch_new_readings() appelé par le scheduler / le bouton "Synchroniser maintenant"
"""

import logging
import re
from datetime import datetime, timedelta
from email.message import Message
from email.utils import parsedate_to_datetime

from modules import imap_client
from modules.history import _connect

logger = logging.getLogger(__name__)

_CAMPAIGN_RE = re.compile(r"WEEKLY_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")
_KWH_RE = re.compile(r"([\d.,\s ]+)\s*kWh</span>", re.IGNORECASE)
_DELTA_RE = re.compile(r"([\d.,]+)\s*%\s+de\s+(plus|moins)", re.IGNORECASE)


def extract_campaign_period(msg: Message) -> tuple | None:
    """Extrait (period_start, period_end) depuis le header X-CampaignID: WEEKLY_YYYY-MM-DD_YYYY-MM-DD.

    Souvent absent : un transfert Gmail (filtre "Transférer à") ne préserve pas les headers
    custom du mail d'origine. La lecture reste historisée, juste sans période labellisée.
    """
    if not msg:
        return None
    value = msg.get("X-CampaignID", "")
    m = _CAMPAIGN_RE.search(value)
    if m:
        return m.group(1), m.group(2)
    return None


def decode_html_part(msg: Message) -> str | None:
    """Parcourt le message MIME et retourne le corps text/html décodé (gère le multipart imbriqué
    créé par le transfert Gmail : le HTML d'origine reste présent en clair dans le blockquote)."""
    if not msg:
        return None
    parts = msg.walk() if msg.is_multipart() else [msg]
    for part in parts:
        if part.get_content_type() != "text/html":
            continue
        try:
            payload = part.get_payload(decode=True)
        except Exception:
            continue
        if payload is None:
            continue
        charset = part.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")
    return None


def parse_kwh(html: str) -> float | None:
    """Extrait la valeur kWh (virgule française → point)."""
    if not html:
        return None
    m = _KWH_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip().replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(raw)
    except ValueError:
        return None


def parse_delta(html: str) -> tuple | None:
    """Extrait (delta_percent, 'plus'|'moins') si présent."""
    if not html:
        return None
    m = _DELTA_RE.search(html)
    if not m:
        return None
    try:
        percent = float(m.group(1).replace(",", "."))
    except ValueError:
        return None
    return percent, m.group(2).lower()


def parse_message(msg: Message) -> dict | None:
    """Parse un email.message.Message complet. None si aucune valeur kWh trouvée."""
    if not msg:
        return None
    html = decode_html_part(msg)
    kwh = parse_kwh(html) if html else None
    if kwh is None:
        return None
    period = extract_campaign_period(msg)
    delta = parse_delta(html)
    message_id = (msg.get("Message-ID") or "").strip().strip("<>") or None
    return {
        "message_id": message_id,
        "period_start": period[0] if period else None,
        "period_end": period[1] if period else None,
        "kwh": kwh,
        "delta_percent": delta[0] if delta else None,
        "delta_direction": delta[1] if delta else None,
    }


def extract_email_date(msg: Message) -> str | None:
    """Extrait la date d'envoi de l'email (header Date, format RFC 2822) en ISO 8601."""
    if not msg:
        return None
    raw = msg.get("Date")
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw).isoformat()
    except (TypeError, ValueError):
        return None


def is_message_processed(message_id: str) -> bool:
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM electricity_readings WHERE message_id = ?", (message_id,)
            ).fetchone()
        return row is not None
    except Exception as e:
        logger.error("Électricité : erreur vérification message traité : %s", e)
        return False


def record_reading(message_id, period_start, period_end, kwh, delta_percent, delta_direction) -> bool:
    """Insère une lecture (idempotent via UNIQUE(message_id)). Retourne True si insérée."""
    try:
        with _connect() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO electricity_readings
                   (message_id, period_start, period_end, kwh, delta_percent, delta_direction, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (message_id, period_start, period_end, kwh, delta_percent, delta_direction,
                 datetime.now().isoformat(timespec="seconds")),
            )
        return cur.rowcount > 0
    except Exception as e:
        logger.error("Électricité : erreur enregistrement lecture : %s", e)
        return False


def get_readings(weeks: int | None = 52) -> list:
    """Retourne les lectures des N dernières semaines, triées par période.
    weeks=None (ou <= 0) retourne l'historique complet, sans filtre de date
    (utilisé par la comparaison annuelle sur /electricity)."""
    try:
        with _connect() as conn:
            if weeks is not None and weeks > 0:
                since = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
                rows = conn.execute(
                    """SELECT message_id, period_start, period_end, kwh, delta_percent, delta_direction, fetched_at
                       FROM electricity_readings
                       WHERE period_start >= ? OR period_start IS NULL
                       ORDER BY COALESCE(period_start, fetched_at)""",
                    (since,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT message_id, period_start, period_end, kwh, delta_percent, delta_direction, fetched_at
                       FROM electricity_readings
                       ORDER BY COALESCE(period_start, fetched_at)"""
                ).fetchall()
        return [
            {
                "message_id": r[0], "period_start": r[1], "period_end": r[2],
                "kwh": r[3], "delta_percent": r[4], "delta_direction": r[5], "fetched_at": r[6],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Électricité : erreur lecture historique : %s", e)
        return []


def record_import_log(message_id: str | None, email_date: str | None, kwh: float | None, status: str, reason: str | None = None) -> None:
    """Trace une tentative de sync email (succès ou échec), pour le widget de statut d'import."""
    try:
        with _connect() as conn:
            conn.execute(
                """INSERT INTO electricity_import_log
                   (message_id, email_date, imported_at, kwh, status, reason)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (message_id, email_date, datetime.now().isoformat(timespec="seconds"), kwh, status, reason),
            )
    except Exception as e:
        logger.error("Électricité : erreur enregistrement log import : %s", e)


def get_import_log(limit: int = 50) -> list:
    """Retourne les N dernières tentatives de sync email, plus récentes en premier."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT message_id, email_date, imported_at, kwh, status, reason
                   FROM electricity_import_log
                   ORDER BY imported_at DESC
                   LIMIT ?""",
                (limit,),
            ).fetchall()
        return [
            {
                "message_id": r[0], "email_date": r[1], "imported_at": r[2],
                "kwh": r[3], "status": r[4], "reason": r[5],
            }
            for r in rows
        ]
    except Exception as e:
        logger.error("Électricité : erreur lecture log import : %s", e)
        return []


def get_latest_reading() -> dict | None:
    """Retourne la lecture la plus récente (pour la carte dashboard)."""
    try:
        with _connect() as conn:
            row = conn.execute(
                """SELECT message_id, period_start, period_end, kwh, delta_percent, delta_direction, fetched_at
                   FROM electricity_readings
                   ORDER BY COALESCE(period_start, fetched_at) DESC, fetched_at DESC
                   LIMIT 1"""
            ).fetchone()
        if not row:
            return None
        return {
            "message_id": row[0], "period_start": row[1], "period_end": row[2],
            "kwh": row[3], "delta_percent": row[4], "delta_direction": row[5], "fetched_at": row[6],
        }
    except Exception as e:
        logger.error("Électricité : erreur lecture dernière valeur : %s", e)
        return None


def fetch_new_readings(electricity_cfg: dict) -> dict:
    """Job de synchronisation : liste les emails Lite non traités et les historise."""
    if not electricity_cfg.get("enabled") or not imap_client.is_connected(electricity_cfg):
        return {"status": "skipped"}
    conn = imap_client.open_connection(electricity_cfg)
    if not conn:
        logger.error("Électricité : échec connexion IMAP")
        return {"status": "error", "reason": "auth"}
    try:
        matches = imap_client.list_matching_messages(
            conn,
            electricity_cfg.get("sender_filter", "bonjour@lite.eco"),
            electricity_cfg.get("subject_filter", "flash élec"),
            electricity_cfg.get("lookback_days", 90),
        )

        new_count = 0
        for match in matches:
            if is_message_processed(match["message_id"]):
                continue
            message = imap_client.fetch_message(conn, match["uid"])
            if message is None:
                record_import_log(match["message_id"], None, None, "error", "fetch_failed")
                continue

            email_date = extract_email_date(message)
            parsed = parse_message(message)
            if not parsed:
                record_import_log(match["message_id"], email_date, None, "error", "kwh_introuvable")
                continue

            if record_reading(**parsed):
                new_count += 1
                record_import_log(parsed["message_id"], email_date, parsed["kwh"], "success")
            else:
                record_import_log(parsed["message_id"], email_date, parsed["kwh"], "error", "insertion_echouee")

        logger.info("Électricité : synchronisation terminée — %d vérifiés, %d nouveaux", len(matches), new_count)
        return {"status": "ok", "checked": len(matches), "new": new_count}
    except Exception as e:
        logger.error("Électricité : erreur synchronisation : %s", e)
        return {"status": "error", "reason": str(e)}
    finally:
        imap_client.close_connection(conn)
