"""
Module d'apprentissage du COP réel de la climatisation.
Système de tagging manuel pour enregistrer les événements ON/OFF et calculer le COP réel.
"""

import sqlite3
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "history.db")


def _connect() -> sqlite3.Connection:
    """Connexion à la base SQLite avec initialisation du schéma."""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Créer les tables si elles n'existent pas
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS cop_tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            tag TEXT NOT NULL,
            outdoor_temp REAL,
            total_power REAL,
            heater_power REAL,
            base_consumption REAL,
            deduced_ac_power REAL,
            notes TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_base_profile (
            hour_of_day INTEGER PRIMARY KEY,
            avg_base_watts REAL NOT NULL,
            sample_count INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_measurements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            outdoor_temp REAL NOT NULL,
            ac_power_watts REAL NOT NULL,
            thermal_kw REAL NOT NULL DEFAULT 4.0,
            calculated_cop REAL NOT NULL,
            confidence_score REAL DEFAULT 1.0,
            tag_id INTEGER,
            FOREIGN KEY (tag_id) REFERENCES cop_tags(id)
        );

        CREATE TABLE IF NOT EXISTS cop_curve_learned (
            temp_bin_center REAL PRIMARY KEY,
            avg_cop REAL NOT NULL,
            sample_count INTEGER NOT NULL,
            std_deviation REAL,
            last_updated TEXT
        );

        CREATE TABLE IF NOT EXISTS cop_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_cop_tags_ts ON cop_tags(ts);
        CREATE INDEX IF NOT EXISTS idx_cop_measurements_temp ON cop_measurements(outdoor_temp);
        CREATE INDEX IF NOT EXISTS idx_cop_measurements_ts ON cop_measurements(ts);
    """)
    conn.commit()
    return conn


def get_base_consumption(hour_of_day: int) -> float:
    """Retourne la consommation de base pour une heure donnée (0-23)."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT avg_base_watts FROM cop_base_profile WHERE hour_of_day = ?",
            (hour_of_day,)
        ).fetchone()
        if row:
            return row["avg_base_watts"]
        # Valeur par défaut si aucune donnée
        return 200.0
    finally:
        conn.close()


def update_base_profile(hour: int, total_power: float, heater_power: float) -> None:
    """Met à jour le profil de base pour l'heure donnée (moyenne glissante)."""
    conn = _connect()
    try:
        base = total_power - heater_power
        if base < 0:
            base = 0

        row = conn.execute(
            "SELECT avg_base_watts, sample_count FROM cop_base_profile WHERE hour_of_day = ?",
            (hour,)
        ).fetchone()

        if row:
            old_avg = row["avg_base_watts"]
            count = row["sample_count"]
            new_avg = (old_avg * count + base) / (count + 1)
            conn.execute(
                """UPDATE cop_base_profile
                   SET avg_base_watts = ?, sample_count = ?, last_updated = ?
                   WHERE hour_of_day = ?""",
                (new_avg, count + 1, datetime.now().isoformat(), hour)
            )
        else:
            conn.execute(
                """INSERT INTO cop_base_profile (hour_of_day, avg_base_watts, sample_count, last_updated)
                   VALUES (?, ?, 1, ?)""",
                (hour, base, datetime.now().isoformat())
            )
        conn.commit()
        logger.info(f"Profil de base mis à jour : heure {hour}h → {base:.0f}W")
    finally:
        conn.close()


def validate_deduced_power(total: float, heater: float, base: float, min_ac: float = 500, max_ac: float = 3000) -> Optional[tuple]:
    """
    Valide la puissance clim déduite.
    Retourne (ac_power, validation_message) si valide, sinon (None, error_message).
    """
    ac_power = total - heater - base

    if ac_power < 0:
        return None, f"Puissance clim négative ({ac_power:.0f}W) — la consommation de base ({base:.0f}W) est peut-être surestimée"

    if ac_power < min_ac:
        return None, f"Puissance clim trop faible ({ac_power:.0f}W < {min_ac}W) — probablement clim éteinte ou erreur"

    if ac_power > max_ac:
        return None, f"Puissance clim trop élevée ({ac_power:.0f}W > {max_ac}W) — vérifiez les valeurs"

    return ac_power, "OK"


def calculate_cop(ac_power: float, thermal_kw: float) -> float:
    """Calcule le COP : COP = puissance thermique / puissance électrique."""
    if ac_power <= 0:
        return 0.0
    return (thermal_kw * 1000) / ac_power


def update_cop_curve(config: dict) -> None:
    """Reconstruit la courbe COP apprise par bins de température."""
    conn = _connect()
    try:
        cop_cfg = getattr(config, "COP_LEARNING", {})
        temp_bin_size = cop_cfg.get("temp_bin_size", 5)
        min_samples = cop_cfg.get("min_samples_per_bin", 3)

        # Récupérer toutes les mesures
        rows = conn.execute(
            "SELECT outdoor_temp, calculated_cop FROM cop_measurements ORDER BY outdoor_temp"
        ).fetchall()

        if not rows:
            return

        # Grouper par bins
        bins = {}
        for row in rows:
            temp = row["outdoor_temp"]
            cop = row["calculated_cop"]
            bin_center = round(temp / temp_bin_size) * temp_bin_size
            if bin_center not in bins:
                bins[bin_center] = []
            bins[bin_center].append(cop)

        # Calculer moyenne et écart-type pour chaque bin
        conn.execute("DELETE FROM cop_curve_learned")
        for bin_center, cops in bins.items():
            if len(cops) >= min_samples:
                import statistics
                avg_cop = statistics.mean(cops)
                std_dev = statistics.stdev(cops) if len(cops) > 1 else 0.0
                conn.execute(
                    """INSERT INTO cop_curve_learned
                       (temp_bin_center, avg_cop, sample_count, std_deviation, last_updated)
                       VALUES (?, ?, ?, ?, ?)""",
                    (bin_center, avg_cop, len(cops), std_dev, datetime.now().isoformat())
                )
        conn.commit()
        logger.info(f"Courbe COP reconstruite : {len(bins)} bins")
    finally:
        conn.close()


def get_cop_curve_learned() -> list:
    """Retourne la courbe COP apprise au format [(temp, cop), ...]."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT temp_bin_center, avg_cop FROM cop_curve_learned ORDER BY temp_bin_center"
        ).fetchall()
        return [(row["temp_bin_center"], row["avg_cop"]) for row in rows]
    finally:
        conn.close()


def get_cop_curve_comparison(config_curve: list) -> dict:
    """Compare courbe théorique vs apprise."""
    learned = get_cop_curve_learned()

    if not learned:
        return {
            "theoretical": config_curve,
            "learned": [],
            "avg_difference": None
        }

    # Calculer écart moyen pour les températures communes
    diffs = []
    for temp_theo, cop_theo in config_curve:
        # Trouver le bin le plus proche dans la courbe apprise
        closest = min(learned, key=lambda x: abs(x[0] - temp_theo), default=None)
        if closest and abs(closest[0] - temp_theo) <= 5:
            diffs.append(abs(cop_theo - closest[1]))

    avg_diff = sum(diffs) / len(diffs) if diffs else None

    return {
        "theoretical": config_curve,
        "learned": learned,
        "avg_difference": round(avg_diff, 2) if avg_diff else None
    }


def get_current_sensors(ha_config: dict, cop_config: dict) -> Optional[dict]:
    """Lit les capteurs Shelly actuels via Home Assistant."""
    try:
        from modules import homeassistant as ha

        if not ha.is_configured(ha_config):
            return None

        total_entity = cop_config.get("shelly_total_power_entity_id")
        heater_entity = cop_config.get("shelly_heater_power_entity_id")

        if not total_entity or not heater_entity:
            return None

        total_state = ha.get_entity_state(ha_config, total_entity)
        heater_state = ha.get_entity_state(ha_config, heater_entity)

        if not total_state or not heater_state:
            return None

        try:
            total_power = float(total_state.get("state", 0))
            heater_power = float(heater_state.get("state", 0))
        except (ValueError, TypeError):
            return None

        return {
            "total_power": total_power,
            "heater_power": heater_power,
            "timestamp": datetime.now().isoformat()
        }
    except Exception as e:
        logger.error(f"Erreur lecture capteurs Shelly : {e}")
        return None


def record_tag(tag: str, outdoor_temp: Optional[float], total_power: float, heater_power: float,
               notes: str, config: dict) -> dict:
    """
    Enregistre un tag ON/OFF et calcule le COP si applicable.
    Retourne un dict avec status, tag_id, deduced_ac_power, calculated_cop, validation_message.
    """
    conn = _connect()
    try:
        now = datetime.now()
        hour = now.hour

        cop_cfg = getattr(config, "COP_LEARNING", {})
        thermal_kw = cop_cfg.get("nominal_thermal_kw", 4.0)
        min_ac = cop_cfg.get("min_ac_power", 500)
        max_ac = cop_cfg.get("max_ac_power", 3000)

        # Récupérer la consommation de base
        base = get_base_consumption(hour)

        # Valider la puissance déduite
        validation = validate_deduced_power(total_power, heater_power, base, min_ac, max_ac)
        ac_power, validation_msg = validation if validation else (None, "Validation échouée")

        # Calculer COP si tag ON et puissance valide
        calculated_cop = None
        measurement_id = None

        if tag == "off":
            # Tag OFF : mettre à jour le profil de base
            update_base_profile(hour, total_power, heater_power)

        elif tag == "on" and ac_power is not None and outdoor_temp is not None:
            # Tag ON : calculer COP et enregistrer measurement
            calculated_cop = calculate_cop(ac_power, thermal_kw)

            # Enregistrer d'abord le tag
            cursor = conn.execute(
                """INSERT INTO cop_tags
                   (ts, tag, outdoor_temp, total_power, heater_power, base_consumption, deduced_ac_power, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (now.isoformat(), tag, outdoor_temp, total_power, heater_power, base, ac_power, notes)
            )
            tag_id = cursor.lastrowid

            # Enregistrer measurement
            conn.execute(
                """INSERT INTO cop_measurements
                   (ts, outdoor_temp, ac_power_watts, thermal_kw, calculated_cop, confidence_score, tag_id)
                   VALUES (?, ?, ?, ?, ?, 1.0, ?)""",
                (now.isoformat(), outdoor_temp, ac_power, thermal_kw, calculated_cop, tag_id)
            )

            conn.commit()

            # Mettre à jour la courbe COP
            update_cop_curve(config)

            logger.info(f"Tag ON enregistré : {ac_power:.0f}W → COP {calculated_cop:.2f} à {outdoor_temp:.1f}°C")

            return {
                "status": "ok",
                "tag": "on",
                "tag_id": tag_id,
                "deduced_ac_power": round(ac_power, 0),
                "calculated_cop": round(calculated_cop, 2),
                "validation_message": validation_msg
            }

        # Enregistrer le tag (OFF ou ON invalide)
        cursor = conn.execute(
            """INSERT INTO cop_tags
               (ts, tag, outdoor_temp, total_power, heater_power, base_consumption, deduced_ac_power, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (now.isoformat(), tag, outdoor_temp, total_power, heater_power, base, ac_power, notes)
        )
        tag_id = cursor.lastrowid
        conn.commit()

        if tag == "off":
            logger.info(f"Tag OFF enregistré : profil base mis à jour pour {hour}h")
            return {
                "status": "ok",
                "tag": "off",
                "tag_id": tag_id,
                "deduced_ac_power": round(ac_power, 0) if ac_power else None,
                "calculated_cop": None,
                "validation_message": "Profil de base mis à jour"
            }
        else:
            logger.warning(f"Tag ON enregistré mais invalide : {validation_msg}")
            return {
                "status": "warning",
                "tag": "on",
                "tag_id": tag_id,
                "deduced_ac_power": round(ac_power, 0) if ac_power else None,
                "calculated_cop": None,
                "validation_message": validation_msg
            }

    except Exception as e:
        logger.error(f"Erreur enregistrement tag : {e}")
        return {
            "status": "error",
            "validation_message": str(e)
        }
    finally:
        conn.close()


def get_recent_tags(limit: int = 20) -> list:
    """Liste des derniers tags enregistrés."""
    conn = _connect()
    try:
        rows = conn.execute(
            """SELECT id, ts, tag, outdoor_temp, total_power, heater_power,
                      base_consumption, deduced_ac_power, notes
               FROM cop_tags
               ORDER BY ts DESC
               LIMIT ?""",
            (limit,)
        ).fetchall()

        result = []
        for row in rows:
            # Récupérer le COP associé si tag ON
            cop = None
            if row["tag"] == "on":
                cop_row = conn.execute(
                    "SELECT calculated_cop FROM cop_measurements WHERE tag_id = ?",
                    (row["id"],)
                ).fetchone()
                if cop_row:
                    cop = cop_row["calculated_cop"]

            result.append({
                "id": row["id"],
                "ts": row["ts"],
                "tag": row["tag"],
                "outdoor_temp": row["outdoor_temp"],
                "total_power": row["total_power"],
                "heater_power": row["heater_power"],
                "base_consumption": row["base_consumption"],
                "deduced_ac_power": row["deduced_ac_power"],
                "calculated_cop": cop,
                "notes": row["notes"]
            })

        return result
    finally:
        conn.close()


def get_statistics() -> dict:
    """Statistiques globales sur l'apprentissage."""
    conn = _connect()
    try:
        # Nombre total de tags
        total_tags = conn.execute("SELECT COUNT(*) as cnt FROM cop_tags").fetchone()["cnt"]

        # Nombre de mesures valides
        valid_measurements = conn.execute("SELECT COUNT(*) as cnt FROM cop_measurements").fetchone()["cnt"]

        # COP moyen
        avg_cop_row = conn.execute("SELECT AVG(calculated_cop) as avg FROM cop_measurements").fetchone()
        avg_cop = avg_cop_row["avg"] if avg_cop_row["avg"] else None

        # Plage de dates
        date_range = conn.execute(
            "SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM cop_tags"
        ).fetchone()

        # Nombre de points dans la courbe
        curve_points = conn.execute("SELECT COUNT(*) as cnt FROM cop_curve_learned").fetchone()["cnt"]

        # Score de confiance
        confidence = get_confidence_score()

        return {
            "total_tags": total_tags,
            "valid_measurements": valid_measurements,
            "avg_cop": round(avg_cop, 2) if avg_cop else None,
            "date_range": {
                "min": date_range["min_ts"],
                "max": date_range["max_ts"]
            } if date_range["min_ts"] else None,
            "curve_points": curve_points,
            "confidence": confidence
        }
    finally:
        conn.close()


def get_confidence_score() -> float:
    """
    Calcule un score de confiance 0-1 basé sur :
    - Nombre de mesures valides
    - Couverture température
    - Cohérence (faible écart-type)
    - Durée de collecte
    """
    conn = _connect()
    try:
        # Nombre de mesures
        valid_measurements = conn.execute("SELECT COUNT(*) as cnt FROM cop_measurements").fetchone()["cnt"]
        if valid_measurements == 0:
            return 0.0

        # Score basé sur le nombre de mesures (max à 50 mesures)
        measurement_score = min(valid_measurements / 50.0, 1.0)

        # Couverture température (bins avec données / bins totaux possibles)
        curve_points = conn.execute("SELECT COUNT(*) as cnt FROM cop_curve_learned").fetchone()["cnt"]
        expected_bins = 7  # -10, -5, 0, 5, 10, 15, 20
        coverage_score = min(curve_points / expected_bins, 1.0)

        # Cohérence (écart-type moyen des bins)
        avg_std = conn.execute(
            "SELECT AVG(std_deviation) as avg FROM cop_curve_learned WHERE sample_count > 1"
        ).fetchone()["avg"]
        consistency_score = 1.0
        if avg_std is not None:
            # Écart-type faible = bon (< 0.5 = excellent, > 1.0 = médiocre)
            consistency_score = max(0.0, 1.0 - (avg_std / 1.0))

        # Durée de collecte
        date_range = conn.execute(
            "SELECT MIN(ts) as min_ts, MAX(ts) as max_ts FROM cop_tags"
        ).fetchone()
        duration_score = 0.5
        if date_range["min_ts"] and date_range["max_ts"]:
            try:
                min_date = datetime.fromisoformat(date_range["min_ts"])
                max_date = datetime.fromisoformat(date_range["max_ts"])
                days = (max_date - min_date).days
                duration_score = min(days / 30.0, 1.0)  # max à 30 jours
            except:
                pass

        # Score global (pondéré)
        confidence = (
            measurement_score * 0.3 +
            coverage_score * 0.3 +
            consistency_score * 0.2 +
            duration_score * 0.2
        )

        return round(confidence, 2)
    finally:
        conn.close()


def calibrate_base_consumption(watts: float, hour: Optional[int] = None) -> None:
    """Calibration manuelle du profil de base."""
    conn = _connect()
    try:
        if hour is not None:
            # Calibration pour une heure spécifique
            conn.execute(
                """INSERT OR REPLACE INTO cop_base_profile (hour_of_day, avg_base_watts, sample_count, last_updated)
                   VALUES (?, ?, 1, ?)""",
                (hour, watts, datetime.now().isoformat())
            )
            logger.info(f"Calibration manuelle : {hour}h → {watts}W")
        else:
            # Calibration pour toutes les heures
            for h in range(24):
                conn.execute(
                    """INSERT OR REPLACE INTO cop_base_profile (hour_of_day, avg_base_watts, sample_count, last_updated)
                       VALUES (?, ?, 1, ?)""",
                    (h, watts, datetime.now().isoformat())
                )
            logger.info(f"Calibration manuelle globale : {watts}W pour toutes les heures")
        conn.commit()
    finally:
        conn.close()


def get_base_profile() -> list:
    """Retourne le profil de base horaire."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT hour_of_day, avg_base_watts, sample_count FROM cop_base_profile ORDER BY hour_of_day"
        ).fetchall()

        # Remplir les heures manquantes avec valeur par défaut
        profile = {}
        for row in rows:
            profile[row["hour_of_day"]] = {
                "avg_base_watts": row["avg_base_watts"],
                "sample_count": row["sample_count"]
            }

        result = []
        for h in range(24):
            if h in profile:
                result.append({
                    "hour": h,
                    "base_watts": profile[h]["avg_base_watts"],
                    "sample_count": profile[h]["sample_count"]
                })
            else:
                result.append({
                    "hour": h,
                    "base_watts": 200.0,
                    "sample_count": 0
                })

        return result
    finally:
        conn.close()


def purge_old(days: int = 90) -> None:
    """Supprime les tags et measurements de plus de N jours."""
    conn = _connect()
    try:
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        conn.execute("DELETE FROM cop_tags WHERE ts < ?", (cutoff,))
        conn.execute("DELETE FROM cop_measurements WHERE ts < ?", (cutoff,))
        conn.commit()
        logger.info(f"Purge COP : données > {days} jours supprimées")
    finally:
        conn.close()


def delete_tag(tag_id: int) -> bool:
    """Supprime un tag spécifique et ses measurements associés, puis recalcule la courbe."""
    conn = _connect()
    try:
        # Vérifier que le tag existe
        row = conn.execute("SELECT id FROM cop_tags WHERE id = ?", (tag_id,)).fetchone()
        if not row:
            return False

        # Supprimer les measurements associés
        conn.execute("DELETE FROM cop_measurements WHERE tag_id = ?", (tag_id,))
        # Supprimer le tag
        conn.execute("DELETE FROM cop_tags WHERE id = ?", (tag_id,))
        conn.commit()

        # Recalculer la courbe
        update_cop_curve()

        logger.info(f"Tag {tag_id} supprimé avec succès")
        return True
    except Exception as e:
        logger.error(f"Erreur suppression tag {tag_id}: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def get_last_on_tag() -> Optional[dict]:
    """Retourne le dernier tag ON enregistré."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM cop_tags WHERE tag = 'on' ORDER BY ts DESC LIMIT 1"
        ).fetchone()
        if row:
            return dict(row)
        return None
    finally:
        conn.close()


def clear_all(keep_config: bool = True) -> None:
    """Efface toutes les données d'apprentissage."""
    conn = _connect()
    try:
        conn.execute("DELETE FROM cop_tags")
        conn.execute("DELETE FROM cop_measurements")
        conn.execute("DELETE FROM cop_curve_learned")
        if not keep_config:
            conn.execute("DELETE FROM cop_base_profile")
            conn.execute("DELETE FROM cop_config")
        conn.commit()
        logger.info("Données COP effacées (config préservée)" if keep_config else "Toutes données COP effacées")
    finally:
        conn.close()
