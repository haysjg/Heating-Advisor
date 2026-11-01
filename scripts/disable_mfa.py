"""
Désactive la double authentification (téléphone perdu et plus de code de secours).

Retire le secret TOTP et les codes de secours de config_override.json : la connexion
redevient protégée par le seul mot de passe. Les sessions et appareils de confiance
existants sont déconnectés. Redémarrer le conteneur pour que l'appli relise la config.

Usage (sur le NAS) :
    docker exec heating-advisor python scripts/disable_mfa.py
    docker restart heating-advisor
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from modules.overrides import patch_override

_MFA_KEYS = ("totp_secret", "totp_last_step", "recovery_codes")


def main() -> int:
    found = []

    def _apply(data):
        auth = data.get("AUTH", {})
        for key in _MFA_KEYS:
            if key in auth:
                found.append(key)
                del auth[key]

    patch_override(_apply)
    if not found:
        print("La double authentification n'était pas active.")
        return 0
    print("Double authentification désactivée. Redémarrez le conteneur : docker restart heating-advisor")
    return 0


if __name__ == "__main__":
    sys.exit(main())
