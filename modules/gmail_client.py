"""
Client OAuth2 + API Gmail (lecture seule) pour le suivi électricité.

Implémenté en urllib.request stdlib (comme homeassistant.py / ntfy_push.py)
pour éviter les dépendances lourdes google-auth-oauthlib / google-api-python-client.
Aucun access token n'est jamais persisté — uniquement gardé en mémoire le temps
d'un job de synchronisation.
"""

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

from modules.crypto import decrypt_password

logger = logging.getLogger(__name__)

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"


def build_auth_url(electricity_cfg: dict, redirect_uri: str, state: str) -> str:
    """Construit l'URL de consentement Google OAuth."""
    params = {
        "client_id": electricity_cfg.get("gmail_client_id", ""),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return f"{_AUTH_URL}?{urllib.parse.urlencode(params)}"


def _post_token_request(payload: dict) -> dict | None:
    try:
        data = urllib.parse.urlencode(payload).encode()
        req = urllib.request.Request(_TOKEN_URL, data=data, method="POST")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("Gmail OAuth : échec requête token (%s) — %s", e.code, e.read().decode(errors="replace"))
        return None
    except Exception as e:
        logger.warning("Gmail OAuth : échec requête token — %s", e)
        return None


def exchange_code_for_tokens(electricity_cfg: dict, code: str, redirect_uri: str) -> dict | None:
    """Échange le code d'autorisation contre access_token + refresh_token."""
    client_secret = decrypt_password(electricity_cfg.get("gmail_client_secret", ""))
    return _post_token_request({
        "code": code,
        "client_id": electricity_cfg.get("gmail_client_id", ""),
        "client_secret": client_secret,
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    })


def refresh_access_token(electricity_cfg: dict) -> str | None:
    """Échange le refresh_token stocké contre un access_token frais (non persisté)."""
    refresh_token = decrypt_password(electricity_cfg.get("gmail_refresh_token", ""))
    client_secret = decrypt_password(electricity_cfg.get("gmail_client_secret", ""))
    if not refresh_token or not client_secret:
        return None
    result = _post_token_request({
        "refresh_token": refresh_token,
        "client_id": electricity_cfg.get("gmail_client_id", ""),
        "client_secret": client_secret,
        "grant_type": "refresh_token",
    })
    if not result:
        return None
    return result.get("access_token")


def is_connected(electricity_cfg: dict) -> bool:
    """True si un refresh_token est stocké (statut affiché côté UI, jamais le token)."""
    return bool(electricity_cfg.get("gmail_refresh_token"))


def _gmail_get(access_token: str, path: str, params: dict | None = None) -> dict | None:
    url = f"{_GMAIL_API_BASE}{path}"
    if params:
        url += f"?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        logger.warning("Gmail API : erreur HTTP %s sur %s — %s", e.code, path, e.read().decode(errors="replace"))
        return None
    except Exception as e:
        logger.warning("Gmail API : erreur sur %s — %s", path, e)
        return None


def list_messages(access_token: str, sender_filter: str, subject_filter: str, lookback_days: int) -> list[str]:
    """Liste les IDs de messages correspondant au filtre expéditeur/sujet sur les N derniers jours."""
    query = f'from:{sender_filter} subject:"{subject_filter}" newer_than:{int(lookback_days)}d'
    message_ids: list[str] = []
    page_token = None
    while True:
        params = {"q": query}
        if page_token:
            params["pageToken"] = page_token
        result = _gmail_get(access_token, "/messages", params)
        if not result:
            break
        message_ids.extend(m["id"] for m in result.get("messages", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return message_ids


def get_message(access_token: str, message_id: str) -> dict | None:
    """Récupère le message complet (format=full) — payload MIME + headers."""
    return _gmail_get(access_token, f"/messages/{message_id}", {"format": "full"})
