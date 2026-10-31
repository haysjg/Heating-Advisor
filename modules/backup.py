"""
Sauvegarde locale nocturne du répertoire data/.

Chaque nuit (job APScheduler de app.py) :
- les bases SQLite (*.db) sont copiées à chaud via sqlite3.Connection.backup()
  (copie cohérente même pendant une écriture, WAL compris) ;
- les autres fichiers (JSON, clés, .bak) sont copiés tels quels ;
- le tout va dans data/backups/AAAA-MM-JJ/ (dossier 0700, fichiers 0600 : clés et mots de passe chiffrés) ;
- les sauvegardes de plus de RETENTION_DAYS jours sont supprimées.

Ces copies restent sur le même disque : elles protègent d'une corruption ou d'une erreur de
manipulation, pas d'une panne du NAS (inclure ./data dans Hyper Backup pour cela).
"""
import logging
import os
import shutil
import sqlite3
from datetime import datetime, date

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
BACKUP_SUBDIR = "backups"
RETENTION_DAYS = 7

# Fichiers annexes de SQLite : leur contenu est déjà inclus dans la copie via backup()
_SQLITE_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")


def _backup_sqlite(src_path: str, dst_path: str) -> None:
    """Copie cohérente d'une base SQLite, même si elle est en cours d'utilisation."""
    src = sqlite3.connect(src_path, timeout=5)
    try:
        dst = sqlite3.connect(dst_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _rotate(backup_root: str, today: date, retention_days: int) -> list[str]:
    """Supprime les dossiers de sauvegarde datés d'au moins retention_days jours. Retourne les noms supprimés."""
    removed = []
    for name in sorted(os.listdir(backup_root)):
        path = os.path.join(backup_root, name)
        if not os.path.isdir(path):
            continue
        try:
            day = datetime.strptime(name, "%Y-%m-%d").date()
        except ValueError:
            continue  # dossier non géré par la sauvegarde : on n'y touche pas
        if (today - day).days >= retention_days:
            shutil.rmtree(path)
            removed.append(name)
    return removed


def run(data_dir: str | None = None, now: datetime | None = None,
        retention_days: int = RETENTION_DAYS) -> str:
    """Sauvegarde data_dir dans data_dir/backups/AAAA-MM-JJ/ puis applique la rotation.

    Un fichier en échec n'interrompt pas la sauvegarde des autres. Retourne le dossier créé.
    """
    data_dir = data_dir or DATA_DIR
    now = now or datetime.now()
    backup_root = os.path.join(data_dir, BACKUP_SUBDIR)
    dest = os.path.join(backup_root, now.strftime("%Y-%m-%d"))
    os.makedirs(dest, mode=0o700, exist_ok=True)
    os.chmod(backup_root, 0o700)
    os.chmod(dest, 0o700)

    copied, failed = 0, 0
    for name in sorted(os.listdir(data_dir)):
        src = os.path.join(data_dir, name)
        # Les sous-dossiers (dont backups/ lui-même) ne sont pas sauvegardés
        if not os.path.isfile(src) or name.endswith(_SQLITE_SIDE_SUFFIXES):
            continue
        dst = os.path.join(dest, name)
        try:
            if os.path.exists(dst):
                os.remove(dst)  # nouvelle sauvegarde le même jour : on remplace
            if name.endswith(".db"):
                _backup_sqlite(src, dst)
            else:
                shutil.copy2(src, dst)
            os.chmod(dst, 0o600)
            copied += 1
        except Exception as e:
            failed += 1
            logger.error("Sauvegarde de %s échouée : %s", name, e)

    removed = _rotate(backup_root, now.date(), retention_days)
    logger.info(
        "Sauvegarde data/ → %s : %d fichier(s) copié(s), %d échec(s), %d ancienne(s) sauvegarde(s) supprimée(s)",
        dest, copied, failed, len(removed),
    )
    return dest
