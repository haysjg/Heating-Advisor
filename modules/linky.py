"""
Compteur Linky : courbe de charge (consommation toutes les 30 min) via la passerelle MyElectricalData,
et comparatif du coût réel en Tempo avec les options Base et Heures Creuses du Tarif Bleu.

- Récupération : API MyElectricalData sans le cache de la passerelle (les données ne font que transiter).
  Enedis limite la courbe de charge à 7 jours par appel ; MyElectricalData à 50 appels par jour sans cache.
  Chaque passage rattrape d'abord les jours récents, puis remonte l'historique (LINKY.backfill_days)
  par tranches de 7 jours, dans la limite de DAILY_CALL_BUDGET appels par jour.
- Stockage : table linky_load_curve de data/history.db (début de l'intervalle en heure locale, kWh).
- Comparatif (compare_options, pur) : chaque demi-heure est facturée selon les trois options ; la
  couleur Tempo d'une heure creuse avant 6 h est celle de la veille (le jour Tempo va de 6 h à 6 h).
"""

import json
import logging
import os
import re
import time as time_module
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta

from modules import jsonstore
from modules.crypto import decrypt_password
from modules.history import _connect

logger = logging.getLogger(__name__)

API_BASE = "https://www.myelectricaldata.fr"
HEADERS = {"User-Agent": "heating-advisor/1.0", "Accept": "application/json"}
STATE_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "linky_state.json")

CHUNK_DAYS = 7              # limite Enedis par appel
DAILY_CALL_BUDGET = 40      # sous la limite MyElectricalData (50/jour sans cache)
CALL_PAUSE_SECONDS = 1.0    # bien sous la limite de 5 appels/s
REQUEST_TIMEOUT = 30
OPTIONS = ("TEMPO", "BASE", "HPHC")

_PDL_RE = re.compile(r"\d{14}")
_INTERVAL_RE = re.compile(r"PT(\d+)M")


class LinkyError(Exception):
    """Échec d'un appel à MyElectricalData. status : code HTTP (None = réseau)."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status

    @property
    def retryable(self) -> bool:
        """Panne passagère (réseau, quota, serveur) : à retenter, ce n'est pas une fin d'historique."""
        return self.status is None or self.status in (401, 403, 429) or self.status >= 500


def is_configured(linky_cfg: dict) -> bool:
    return bool(linky_cfg.get("enabled") and valid_pdl(linky_cfg.get("usage_point_id"))
                and linky_cfg.get("token"))


def normalize_pdl(value) -> str:
    """PDL tel que saisi (espaces, tirets, points tolérés) → chiffres seuls."""
    return re.sub(r"[\s.\-]", "", str(value or ""))


def valid_pdl(value) -> bool:
    return bool(_PDL_RE.fullmatch(str(value or "")))


def _error_message(payload) -> str:
    if isinstance(payload, dict):
        for key in ("detail", "error", "message", "description"):
            if payload.get(key):
                value = payload[key]
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return "réponse inattendue de MyElectricalData"


def _get(linky_cfg: dict, path: str) -> dict:
    token = decrypt_password(linky_cfg.get("token", ""))
    req = urllib.request.Request(API_BASE + path, headers={**HEADERS, "Authorization": token})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read())
        except (ValueError, OSError):
            payload = None
        raise LinkyError(f"HTTP {e.code} : {_error_message(payload)}", e.code) from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise LinkyError(f"MyElectricalData injoignable : {e}") from e


def parse_load_curve(payload: dict, start: date, end: date) -> list:
    """Lignes (début de l'intervalle « AAAA-MM-JJTHH:MM:SS », kWh) d'une réponse courbe de charge.

    Les valeurs sont des puissances moyennes (W) sur l'intervalle. Enedis horodate la fin de
    l'intervalle ; si la réponse commence pile à minuit du premier jour, c'est le début qui est daté.
    """
    meter = payload.get("meter_reading") if isinstance(payload, dict) else None
    if not isinstance(meter, dict):
        raise LinkyError(_error_message(payload))
    points = []
    for r in meter.get("interval_reading") or []:
        try:
            stamp = datetime.fromisoformat(str(r["date"]))
            watts = float(r["value"])
        except (KeyError, TypeError, ValueError):
            continue
        m = _INTERVAL_RE.fullmatch(str(r.get("interval_length", "PT30M")))
        points.append((stamp, int(m.group(1)) if m else 30, watts))
    marks_start = any(stamp == datetime.combine(start, time()) for stamp, _, _ in points)
    rows = []
    for stamp, minutes, watts in points:
        begin = stamp if marks_start else stamp - timedelta(minutes=minutes)
        rows.append((begin.isoformat(timespec="seconds"), round(watts * minutes / 60000, 4)))
    return rows


def fetch_load_curve(linky_cfg: dict, start: date, end: date) -> list:
    """Courbe de charge de [start, end[ (au plus 7 jours)."""
    pdl = linky_cfg.get("usage_point_id", "")
    payload = _get(linky_cfg, f"/consumption_load_curve/{pdl}/start/{start.isoformat()}/end/{end.isoformat()}")
    return parse_load_curve(payload, start, end)


def test_access(linky_cfg: dict) -> tuple[bool, str]:
    """Vérifie la clé et le consentement auprès de MyElectricalData (1 appel)."""
    if not linky_cfg.get("usage_point_id"):
        return False, "Numéro PDL non renseigné"
    if not valid_pdl(linky_cfg.get("usage_point_id")):
        return False, f"Numéro PDL invalide : {len(str(linky_cfg['usage_point_id']))} caractères, 14 chiffres attendus"
    if not linky_cfg.get("token"):
        return False, "Clé d'accès MyElectricalData non renseignée"
    try:
        payload = _get(linky_cfg, f"/valid_access/{linky_cfg['usage_point_id']}")
    except LinkyError as e:
        return False, str(e)
    if not isinstance(payload, dict) or not payload.get("valid", False):
        info = payload.get("information") if isinstance(payload, dict) else None
        return False, f"Accès refusé : {info or 'consentement Enedis à renouveler ?'}"
    details = []
    expiry = str(payload.get("consent_expiration_date") or "")[:10]
    if expiry:
        details.append(f"consentement jusqu'au {date.fromisoformat(expiry).strftime('%d/%m/%Y')}")
    if payload.get("call_number") is not None and payload.get("quota_limit"):
        details.append(f"{payload['call_number']}/{payload['quota_limit']} appels aujourd'hui")
    return True, "Accès MyElectricalData valide" + (f" ({', '.join(details)})" if details else "")


# ── Stockage ──────────────────────────────────────────────────

def store(rows: list) -> int:
    if not rows:
        return 0
    with _connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO linky_load_curve (ts, kwh) VALUES (?, ?)", rows)
    return len(rows)


def stored_range() -> tuple[date | None, date | None]:
    """Premier et dernier jour présents dans la courbe stockée."""
    with _connect() as conn:
        first, last = conn.execute("SELECT MIN(ts), MAX(ts) FROM linky_load_curve").fetchone()
    if not first:
        return None, None
    return date.fromisoformat(first[:10]), date.fromisoformat(last[:10])


def get_curve(since: date, until: date) -> list:
    """Intervalles (ts, kWh) dont le début tombe entre since et until inclus, par ordre chronologique."""
    with _connect() as conn:
        return conn.execute(
            "SELECT ts, kwh FROM linky_load_curve WHERE ts >= ? AND ts < ? ORDER BY ts",
            (since.isoformat(), (until + timedelta(days=1)).isoformat()),
        ).fetchall()


def get_state() -> dict:
    state = jsonstore.load(STATE_FILE, {})
    return state if isinstance(state, dict) else {}


# ── Synchronisation ───────────────────────────────────────────

def sync(linky_cfg: dict, today: date | None = None, sleep=time_module.sleep, now: datetime | None = None) -> dict:
    """Récupère les jours manquants : d'abord les plus récents, puis l'historique en remontant.

    Respecte DAILY_CALL_BUDGET (compteur du jour dans data/linky_state.json). La fin de l'historique
    disponible (collecte de la courbe de charge activée récemment) est détectée par une réponse vide
    ou une erreur définitive ; les pannes passagères sont retentées au passage suivant.
    """
    if not is_configured(linky_cfg):
        return {"status": "disabled"}
    now = now or datetime.now()
    today = today or now.date()
    state = get_state()
    if state.get("calls_day") != today.isoformat():
        state["calls_day"], state["calls"] = today.isoformat(), 0
    target = today - timedelta(days=int(linky_cfg.get("backfill_days", 365)))
    added = 0

    def _call(start: date, end: date) -> list:
        if state["calls"]:
            sleep(CALL_PAUSE_SECONDS)
        state["calls"] += 1
        return fetch_load_curve(linky_cfg, start, end)

    try:
        first, last = stored_range()
        # 1. Jours récents (jusqu'à hier : Enedis publie la veille dans la matinée)
        start = max(last + timedelta(days=1) if last else today - timedelta(days=CHUNK_DAYS), target)
        while start < today and state["calls"] < DAILY_CALL_BUDGET:
            end = min(start + timedelta(days=CHUNK_DAYS), today)
            added += store(_call(start, end))
            start = end

        # 2. Historique, en remontant depuis le jour le plus ancien
        first, _ = stored_range()
        oldest = first or today
        while not state.get("backfill_done") and oldest > target and state["calls"] < DAILY_CALL_BUDGET:
            start = max(oldest - timedelta(days=CHUNK_DAYS), target)
            try:
                rows = _call(start, oldest)
            except LinkyError as e:
                if e.retryable:
                    raise
                logger.info("Linky : historique disponible jusqu'au %s (%s)", oldest, e)
                rows = []
            if not rows:
                state["backfill_done"] = True
                break
            added += store(rows)
            oldest = start
        if oldest <= target:
            state["backfill_done"] = True
        state["last_error"] = None
        status = "ok"
    except LinkyError as e:
        logger.warning("Linky : synchronisation interrompue : %s", e)
        state["last_error"] = str(e)
        state["last_error_at"] = now.isoformat(timespec="seconds")
        status = "error"

    state["last_sync"] = now.isoformat(timespec="seconds")
    jsonstore.save(STATE_FILE, state)
    first, last = stored_range()
    result = {"status": status, "added": added, "calls": state["calls"],
              "first_day": first.isoformat() if first else None,
              "last_day": last.isoformat() if last else None,
              "backfill_done": bool(state.get("backfill_done"))}
    if status == "error":
        result["error"] = state["last_error"]
    logger.info("Linky : synchronisation — %s", result)
    return result


# ── Comparatif des options ────────────────────────────────────

def tempo_day(ts: datetime, day_start_hour: int = 6) -> date:
    """Jour Tempo d'un instant : il commence à 6 h, la nuit appartient à la veille."""
    return ts.date() if ts.hour >= day_start_hour else ts.date() - timedelta(days=1)


def compare_options(curve: list, colors: dict, tempo_prices: dict, options: dict,
                    hp_start: int, hp_end: int) -> dict:
    """Coût de la même consommation en Tempo, Base et Heures Creuses (abonnement compris).

    curve  : [(ts début d'intervalle ISO, kWh)]
    colors : {"AAAA-MM-JJ": "BLUE"|"WHITE"|"RED"} (jours Tempo)
    Les intervalles dont la couleur est inconnue sont écartés des trois options (unpriced_kwh).
    """
    base_price = options["BASE_PRICE"]
    hphc = options["HPHC_PRICES"]
    subs = options["subscription_monthly"]
    months: dict = {}
    breakdown = {c: {"HP": 0.0, "HC": 0.0} for c in ("BLUE", "WHITE", "RED")}
    unpriced = 0.0
    days = set()

    for ts, kwh in curve:
        stamp = datetime.fromisoformat(ts)
        period = "HP" if hp_start <= stamp.hour < hp_end else "HC"
        color = colors.get(tempo_day(stamp, hp_start).isoformat())
        if color not in breakdown:
            unpriced += kwh
            continue
        day = stamp.date().isoformat()
        days.add(day)
        m = months.setdefault(ts[:7], {"kwh": 0.0, "TEMPO": 0.0, "BASE": 0.0, "HPHC": 0.0, "days": set()})
        m["kwh"] += kwh
        m["TEMPO"] += kwh * tempo_prices[color][period]
        m["BASE"] += kwh * base_price
        m["HPHC"] += kwh * hphc[period]
        m["days"].add(day)
        breakdown[color][period] += kwh

    def _subscription(option: str, n_days: int) -> float:
        return subs[option] * 12 / 365 * n_days

    month_rows = []
    totals = {o: 0.0 for o in OPTIONS}
    energy = {o: 0.0 for o in OPTIONS}
    for month in sorted(months):
        m = months[month]
        row = {"month": month, "kwh": round(m["kwh"], 1), "days": len(m["days"])}
        for o in OPTIONS:
            cost = m[o] + _subscription(o, len(m["days"]))
            energy[o] += m[o]
            totals[o] += cost
            row[o] = round(cost, 2)
        month_rows.append(row)

    total_kwh = sum(m["kwh"] for m in months.values())
    return {
        "first_day": min(days) if days else None,
        "last_day": max(days) if days else None,
        "days": len(days),
        "kwh": round(total_kwh, 1),
        "costs": {o: round(totals[o], 2) for o in OPTIONS},
        "energy": {o: round(energy[o], 2) for o in OPTIONS},
        "subscription": {o: round(totals[o] - energy[o], 2) for o in OPTIONS},
        "savings": {o: round(totals[o] - totals["TEMPO"], 2) for o in ("BASE", "HPHC")},
        "best": min(OPTIONS, key=lambda o: totals[o]) if days else None,
        "breakdown": {c: {p: round(v, 1) for p, v in b.items()} for c, b in breakdown.items()},
        "months": month_rows,
        "unpriced_kwh": round(unpriced, 1),
    }
