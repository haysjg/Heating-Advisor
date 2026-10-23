"""
Persistance JSON robuste pour les fichiers d'état de data/.

- Écriture atomique : fichier temporaire + fsync + os.replace (jamais de JSON tronqué après une coupure)
- Copie .bak de la version précédente, utilisée si le fichier principal est illisible
- Fichier illisible mis de côté en .corrupt-<horodatage> au lieu d'être écrasé silencieusement
- Verrou par fichier pour les lecture-modification-écriture concurrentes (jobs + requêtes Flask)
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime

logger = logging.getLogger(__name__)

_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def lock_for(path: str) -> threading.RLock:
    """Verrou réentrant propre à un fichier."""
    path = os.path.abspath(path)
    with _locks_guard:
        return _locks.setdefault(path, threading.RLock())


def _read(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(path: str, default):
    """Lit un fichier JSON. Fichier absent → default. Fichier illisible → mis de côté, repli sur .bak puis default."""
    if not os.path.exists(path):
        return default
    try:
        return _read(path)
    except (OSError, ValueError) as e:
        aside = f"{path}.corrupt-{datetime.now():%Y%m%d-%H%M%S}"
        logger.error("Fichier %s illisible (%s) — mis de côté dans %s", path, e, aside)
        try:
            os.replace(path, aside)
        except OSError:
            pass
    bak = path + ".bak"
    if os.path.exists(bak):
        try:
            data = _read(bak)
            logger.warning("Restauration de %s depuis %s", path, bak)
            save(path, data)
            return data
        except (OSError, ValueError) as e:
            logger.error("Sauvegarde %s illisible elle aussi : %s", bak, e)
    return default


def save(path: str, data, indent: int | None = 2) -> None:
    """Écrit data de façon atomique, en gardant la version précédente en .bak."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = f"{path}.tmp-{os.getpid()}-{threading.get_ident()}"
    with lock_for(path):
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if os.path.exists(path):
            try:
                shutil.copyfile(path, path + ".bak")
            except OSError as e:
                logger.warning("Copie de sauvegarde %s.bak impossible : %s", path, e)
        os.replace(tmp, path)


def update(path: str, updater, default):
    """Lecture-modification-écriture sous verrou. updater(data) modifie data en place ou retourne la nouvelle valeur."""
    with lock_for(path):
        data = load(path, default)
        result = updater(data)
        if result is not None:
            data = result
        save(path, data)
        return data
