"""
CircusVOIP - Config serveur partagee
=====================================
Gere le mot de passe/token d'authentification commun aux serveurs positions + audio.
Le mdp est stocke dans circusvoip_server_config.json.
Si vide ou absent, un token aleatoire est genere au 1er lancement.
"""

import json
import os
import secrets
import string
from pathlib import Path


def get_data_dir() -> Path:
    """Dossier ou sont stockes le token, le certificat TLS, les tickets
    d'auth, la liste des canaux/profils, etc.

    Par defaut : meme dossier que les sources serveur. En conteneur Docker,
    on definit CIRCUSVOIP_DATA_DIR=/data pour rediriger l'etat vers un
    volume monte (le code source reste read-only dans l'image)."""
    env = os.environ.get("CIRCUSVOIP_DATA_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p
    return Path(__file__).resolve().parent


CONFIG_FILE = get_data_dir() / "circusvoip_server_config.json"


def _generate_token(length: int = 16) -> str:
    """Genere un token alphanumerique aleatoire."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def load_or_create_token() -> str:
    """
    Charge le token/mdp depuis le fichier config, ou en genere un nouveau
    si le fichier n'existe pas.
    """
    if CONFIG_FILE.exists():
        try:
            cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            token = cfg.get("token")
            if token:
                return token
        except Exception:
            pass

    # Generer un nouveau token par defaut
    token = _generate_token()
    save_token(token)
    return token


def save_token(token: str):
    """Sauvegarde le token/mdp dans le fichier config."""
    cfg = {"token": token}
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def set_password(password: str):
    """Definit un nouveau mot de passe serveur (ecrase l'existant)."""
    if not password or not password.strip():
        # Si vide, generer un nouveau token aleatoire
        save_token(_generate_token())
    else:
        save_token(password.strip())


def get_token() -> str:
    """Raccourci pour charger/creer le token/mdp."""
    return load_or_create_token()
