"""Notifications push via Ntfy."""

import logging
import urllib.request

logger = logging.getLogger(__name__)


def send(title: str, message: str, ntfy_cfg: dict) -> None:
    """Envoie une notification push via Ntfy. Silencieux en cas d'erreur."""
    if not ntfy_cfg or not ntfy_cfg.get("enabled"):
        return
    url = ntfy_cfg.get("url", "").rstrip("/")
    topic = ntfy_cfg.get("topic", "heating-advisor")
    token = ntfy_cfg.get("token", "")
    if not url or not topic:
        return
    try:
        req = urllib.request.Request(
            f"{url}/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                **({"Authorization": f"Bearer {token}"} if token else {}),
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5):
            pass
        logger.info("Ntfy : notification envoyée — %s", title)
    except Exception as e:
        logger.warning("Ntfy : échec envoi notification — %s", e)
