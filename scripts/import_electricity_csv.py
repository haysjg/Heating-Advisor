"""
Import ponctuel d'un historique de consommation électrique depuis un CSV "large"
(1 colonne Semaine + 1 colonne par année, cellules vides autorisées).

Format attendu (export Google Sheets/Excel) :
    Semaine,2020 (kwh),2021 (kwh),2022 (kwh),...
    1,,"577,5","538,3",...
    ...

Chaque cellule (semaine, année) devient une lecture electricity_readings avec
message_id synthétique "csv-import:<année>-W<semaine>" (idempotent — un rerun
n'insère pas de doublons). period_start/end = semaine ISO (lundi→dimanche).

Usage :
    python scripts/import_electricity_csv.py chemin/vers/fichier.csv
"""

import csv
import re
import sys
from datetime import date

import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.electricity import record_reading, is_message_processed

_YEAR_RE = re.compile(r"(\d{4})")


def _parse_kwh(raw: str) -> float | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return None


def import_csv(path: str) -> dict:
    inserted = 0
    skipped_existing = 0
    skipped_empty = 0
    errors = 0

    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        header = next(reader)
        year_columns = []
        for idx, col in enumerate(header[1:], start=1):
            m = _YEAR_RE.search(col)
            if m:
                year_columns.append((idx, int(m.group(1))))

        for row in reader:
            if not row or not row[0].strip():
                continue
            try:
                week = int(row[0].strip())
            except ValueError:
                continue

            for idx, year in year_columns:
                if idx >= len(row):
                    continue
                kwh = _parse_kwh(row[idx])
                if kwh is None:
                    skipped_empty += 1
                    continue
                try:
                    period_start = date.fromisocalendar(year, week, 1).isoformat()
                    period_end = date.fromisocalendar(year, week, 7).isoformat()
                except ValueError as e:
                    print(f"  ⚠ semaine ISO invalide {year}-W{week} : {e}")
                    errors += 1
                    continue

                message_id = f"csv-import:{year}-W{week:02d}"
                if is_message_processed(message_id):
                    skipped_existing += 1
                    continue
                ok = record_reading(message_id, period_start, period_end, kwh, None, None)
                if ok:
                    inserted += 1
                else:
                    errors += 1

    return {
        "inserted": inserted,
        "skipped_existing": skipped_existing,
        "skipped_empty": skipped_empty,
        "errors": errors,
    }


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage : python scripts/import_electricity_csv.py fichier.csv")
        sys.exit(1)

    result = import_csv(sys.argv[1])
    print(
        f"Import terminé — {result['inserted']} lectures insérées, "
        f"{result['skipped_existing']} déjà présentes, "
        f"{result['skipped_empty']} cellules vides ignorées, "
        f"{result['errors']} erreurs."
    )
