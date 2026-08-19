"""
Suivi de la consommation électrique via parsing des emails "flash élec" Gmail (Lite).

Deux familles de fonctions :
- parsing (pures, sans I/O) : extraction kWh / période / delta depuis un message Gmail
- historisation + orchestration : SQLite (table electricity_readings, réutilise data/history.db)
  et fetch_new_readings() appelé par le scheduler / le bouton "Synchroniser maintenant"
"""

import base64
import binascii
import logging
import quopri
import re
from datetime import datetime, timedelta

from modules import gmail_client
from modules.history import _connect

logger = logging.getLogger(__name__)

_CAMPAIGN_RE = re.compile(r"WEEKLY_(\d{4}-\d{2}-\d{2})_(\d{4}-\d{2}-\d{2})")
_KWH_RE = re.compile(r"([\d.,\s ]+)\s*kWh</span>", re.IGNORECASE)
_DELTA_RE = re.compile(r"([\d.,]+)\s*%\s+de\s+(plus|moins)", re.IGNORECASE)
_QP_SNIFF_RE = re.compile(r"=[0-9A-Fa-f]{2}")


def extract_campaign_period(headers: list) -> tuple | None:
    """Extrait (period_start, period_end) depuis le header X-CampaignID: WEEKLY_YYYY-MM-DD_YYYY-MM-DD."""
    for h in headers or []:
        if h.get("name", "").lower() == "x-campaignid":
            m = _CAMPAIGN_RE.search(h.get("value", ""))
            if m:
                return m.group(1), m.group(2)
    return None


def _decode_part_data(data: str) -> str | None:
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))
    except (binascii.Error, ValueError):
        return None
    text = raw.decode("utf-8", errors="replace")
    # Défensif : l'API Gmail décode généralement déjà le quoted-printable, mais pas garanti.
    if _QP_SNIFF_RE.search(text):
        try:
            text = quopri.decodestring(text.encode("utf-8", errors="replace")).decode("utf-8", errors="replace")
        except Exception:
            pass
    return text


def decode_html_part(payload: dict) -> str | None:
    """Parcourt récursivement le payload MIME et retourne le corps text/html décodé."""
    if not payload:
        return None
    if payload.get("mimeType") == "text/html":
        data = payload.get("body", {}).get("data")
        if data:
            return _decode_part_data(data)
    for part in payload.get("parts", []) or []:
        html = decode_html_part(part)
        if html:
            return html
    return None


def parse_kwh(html: str) -> float | None:
    """Extrait la valeur kWh (virgule française → point)."""
    if not html:
        return None
    m = _KWH_RE.search(html)
    if not m:
        return None
    raw = m.group(1).strip().replace(" ", "").replace(" ", "").replace(",", ".")
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


def parse_message(message: dict) -> dict | None:
    """Parse un message Gmail API complet. None si aucune valeur kWh trouvée."""
    if not message:
        return None
    payload = message.get("payload", {})
    html = decode_html_part(payload)
    kwh = parse_kwh(html) if html else None
    if kwh is None:
        return None
    period = extract_campaign_period(payload.get("headers", []))
    delta = parse_delta(html)
    return {
        "message_id": message.get("id"),
        "period_start": period[0] if period else None,
        "period_end": period[1] if period else None,
        "kwh": kwh,
        "delta_percent": delta[0] if delta else None,
        "delta_direction": delta[1] if delta else None,
    }


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


def get_readings(weeks: int = 52) -> list:
    """Retourne les lectures des N dernières semaines, triées par période."""
    since = (datetime.now() - timedelta(weeks=weeks)).strftime("%Y-%m-%d")
    try:
        with _connect() as conn:
            rows = conn.execute(
                """SELECT message_id, period_start, period_end, kwh, delta_percent, delta_direction, fetched_at
                   FROM electricity_readings
                   WHERE period_start >= ? OR period_start IS NULL
                   ORDER BY COALESCE(period_start, fetched_at)""",
                (since,),
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
    if not electricity_cfg.get("enabled") or not electricity_cfg.get("gmail_refresh_token"):
        return {"status": "skipped"}
    try:
        access_token = gmail_client.refresh_access_token(electricity_cfg)
        if not access_token:
            logger.error("Électricité : échec rafraîchissement du token Gmail")
            return {"status": "error", "reason": "auth"}

        message_ids = gmail_client.list_messages(
            access_token,
            electricity_cfg.get("sender_filter", "bonjour@lite.eco"),
            electricity_cfg.get("subject_filter", "flash élec"),
            electricity_cfg.get("lookback_days", 90),
        )

        new_count = 0
        for message_id in message_ids:
            if is_message_processed(message_id):
                continue
            message = gmail_client.get_message(access_token, message_id)
            parsed = parse_message(message)
            if parsed and record_reading(**parsed):
                new_count += 1

        logger.info("Électricité : synchronisation terminée — %d vérifiés, %d nouveaux", len(message_ids), new_count)
        return {"status": "ok", "checked": len(message_ids), "new": new_count}
    except Exception as e:
        logger.error("Électricité : erreur synchronisation : %s", e)
        return {"status": "error", "reason": str(e)}
