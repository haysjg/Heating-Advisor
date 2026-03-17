"""
Script de notification email — Recommandation chauffage du lendemain.
À lancer chaque soir à 20h via le planificateur de tâches Synology :
    docker exec heating-advisor python notify.py
"""

import logging
import smtplib
import sys
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import config
from modules.overrides import load as load_overrides
from modules.crypto import decrypt_password
from modules.weather import get_tomorrow_weather
from modules.tempo import get_tempo_info
from modules.advisor import analyze_tomorrow

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

load_overrides(config)


# ── Helpers ───────────────────────────────────────────────────

def _system_icon(system: str) -> str:
    return {"clim": "❄️", "poele": "🔥", "none": "✅"}.get(system, "⚠️")

def _level_color(level: str) -> str:
    return {
        "success": "#22c55e",
        "warning": "#f59e0b",
        "danger":  "#ef4444",
        "info":    "#3b82f6",
    }.get(level, "#8892a4")

def _tempo_color_fr(color: str) -> str:
    return {"BLUE": "Bleu 🔵", "WHITE": "Blanc ⚪", "RED": "Rouge 🔴"}.get(color, "Inconnu ❓")


# ── Construction du mail HTML ─────────────────────────────────

def build_email(data: dict, tempo: dict) -> tuple[str, str]:
    """Retourne (sujet, corps HTML) du mail."""
    _jours = ["lundi","mardi","mercredi","jeudi","vendredi","samedi","dimanche"]
    _mois  = ["janvier","février","mars","avril","mai","juin","juillet","août","septembre","octobre","novembre","décembre"]
    _d = datetime.now() + timedelta(days=1)
    tomorrow_date = f"{_jours[_d.weekday()].capitalize()} {_d.day} {_mois[_d.month - 1]} {_d.year}"
    rec = data["recommendation"]
    weather = data.get("weather", {})
    estimate = data.get("daily_estimate")
    tempo_color = tempo["tomorrow"]["color"]
    icon = _system_icon(rec["system"])
    color = _level_color(rec["level"])

    subject = f"{icon} Chauffage demain ({tomorrow_date}) — {rec['title']}"

    # Bloc météo
    if weather.get("temperature") is not None:
        meteo_bloc = f"""
        <p style="margin:4px 0">🌡️ Température prévue :
            <strong>{weather['temp_min']}°C – {weather['temp_max']}°C</strong>
            (moy. {weather['temperature']}°C)
        </p>"""
    else:
        meteo_bloc = "<p style='margin:4px 0'>🌡️ Météo indisponible</p>"

    # Bloc estimation
    if estimate and estimate.get("clim") is not None:
        estimate_bloc = f"""
        <table style="width:100%;border-collapse:collapse;margin-top:12px">
          <tr>
            <td style="padding:8px;background:#1a1d27;border-radius:8px 0 0 8px;text-align:center">
              ❄️ Clim<br><strong style="font-size:1.3em">{estimate['clim']:.2f} €</strong>
            </td>
            <td style="padding:8px;background:#1a1d27;border-radius:0 8px 8px 0;text-align:center;border-left:1px solid #2a2d3e">
              🔥 Poêle<br><strong style="font-size:1.3em">{estimate['poele']:.2f} €</strong>
            </td>
          </tr>
        </table>
        <p style="margin:6px 0;font-size:0.8em;color:#8892a4">Estimation sur {estimate['hours']}h ({config.HP_START}h–{config.HP_END}h)</p>"""
    else:
        estimate_bloc = ""

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0">
  <div style="max-width:520px;margin:32px auto;background:#1a1d27;border-radius:16px;overflow:hidden;border:1px solid #2a2d3e">

    <!-- Header -->
    <div style="background:#0f1117;padding:20px 24px;border-bottom:1px solid #2a2d3e">
      <p style="margin:0;font-size:0.85em;color:#8892a4">🏠 Conseiller Chauffage — {config.LOCATION['city']}</p>
      <h1 style="margin:4px 0 0;font-size:1.2em">Recommandation pour demain</h1>
      <p style="margin:2px 0 0;font-size:0.85em;color:#8892a4">{tomorrow_date}</p>
    </div>

    <!-- Recommandation -->
    <div style="padding:20px 24px;border-left:4px solid {color}">
      <div style="font-size:2.5em;line-height:1;margin-bottom:8px">{icon}</div>
      <h2 style="margin:0 0 8px;font-size:1.1em;color:{color}">{rec['title']}</h2>
      <p style="margin:0;color:#8892a4;line-height:1.5;font-size:0.9em">{rec['explanation']}</p>
      {f'<p style="margin:8px 0 0;font-size:0.85em;color:#22c55e">💰 Économie : {rec["savings_per_hour"]:.3f} €/h</p>' if rec.get("savings_per_hour", 0) > 0 else ""}
    </div>

    <!-- Conditions -->
    <div style="padding:16px 24px;background:#0f1117;border-top:1px solid #2a2d3e">
      <p style="margin:0 0 8px;font-size:0.8em;font-weight:600;color:#8892a4;text-transform:uppercase;letter-spacing:.05em">Conditions du jour</p>
      {meteo_bloc}
      <p style="margin:4px 0">⚡ Tarif Tempo : <strong>{_tempo_color_fr(tempo_color)}</strong></p>
      {estimate_bloc}
    </div>

    <!-- Footer -->
    <div style="padding:14px 24px;border-top:1px solid #2a2d3e">
      <p style="margin:0;font-size:0.75em;color:#8892a4;text-align:center">
        Envoyé automatiquement le {datetime.now().strftime('%d/%m/%Y à %Hh%M')} —
        <a href="http://{config.LOCATION.get('nas_ip','localhost')}:{config.LOCATION.get('nas_port',8888)}" style="color:#3b82f6;text-decoration:none">Voir le dashboard</a>
      </p>
    </div>

  </div>
</body>
</html>"""

    return subject, html


# ── Envoi ─────────────────────────────────────────────────────

def send_email(subject: str, html: str) -> bool:
    cfg = config.EMAIL
    if not cfg.get("enabled"):
        logger.info("Notifications email désactivées (EMAIL.enabled = False)")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = cfg["sender"]
    msg["To"] = ", ".join(cfg["recipients"])
    msg.attach(MIMEText(html, "html", "utf-8"))

    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.ehlo()
            server.starttls()
            smtp_login = cfg.get("smtp_login") or cfg["sender"]
            server.login(smtp_login, decrypt_password(cfg["app_password"]))
            server.sendmail(cfg["sender"], cfg["recipients"], msg.as_string())
        logger.info("Mail envoyé à %s", cfg["recipients"])
        return True
    except Exception as e:
        logger.error("Échec envoi mail : %s", e)
        return False


# ── Point d'entrée ────────────────────────────────────────────

def main() -> bool:
    """Prépare et envoie la notification. Retourne True si succès."""
    logger.info("Préparation de la notification email…")

    cfg_dict = {
        "TEMPO_PRICES": config.TEMPO_PRICES,
        "CLIM": config.CLIM,
        "POELE": config.POELE,
        "HP_START": config.HP_START,
        "HP_END": config.HP_END,
        "TARGET_TEMP": config.TARGET_TEMP,
        "NO_HEATING_AT_NIGHT": config.NO_HEATING_AT_NIGHT,
    }

    tomorrow_weather = get_tomorrow_weather({
        **config.LOCATION,
        "hp_start": config.HP_START,
        "hp_end": config.HP_END,
    })
    tempo = get_tempo_info(config.HP_START, config.HP_END)
    data = analyze_tomorrow(tomorrow_weather, tempo, cfg_dict)

    subject, html = build_email(data, tempo)
    logger.info("Sujet : %s", subject)

    return send_email(subject, html)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
