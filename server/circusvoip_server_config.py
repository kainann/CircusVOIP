"""
CircusVOIP - Config serveur partagee
=====================================
Gere le mot de passe/token d'authentification ET les ports d'ecoute,
communs aux serveurs positions + audio + mise a jour.
Tout est stocke dans circusvoip_server_config.json.
Si le fichier est vide ou absent, un token aleatoire est genere au 1er
lancement et les ports prennent leurs valeurs par defaut.

Format du fichier :
    {
      "token": "...",
      "port_positions": 8888,
      "port_audio": 8889,
      "port_update": 8080
    }

L'admin qui auto-heberge edite ce fichier et redemarre les services.
Le port des positions est le point de RENDEZ-VOUS : c'est le seul que le
client doit connaitre a l'avance (saisi sous la forme ip:port). Les
autres lui sont annonces par le serveur apres connexion.
"""

import json
import secrets
import string
from pathlib import Path

CONFIG_FILE = Path(__file__).resolve().parent / "circusvoip_server_config.json"

# Valeurs historiques, conservees comme defauts pour que rien ne change
# tant que l'admin ne touche pas au fichier.
DEFAULT_PORTS = {
    "port_positions": 8888,
    "port_audio":     8889,
    "port_update":    8080,
}


def _generate_token(length: int = 16) -> str:
    """Genere un token alphanumerique aleatoire."""
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _load_cfg() -> dict:
    """Lit le fichier config. Renvoie {} s'il est absent ou illisible."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _save_cfg(cfg: dict):
    """Ecrit le fichier config en PRESERVANT les cles existantes.

    L'ancienne save_token() ecrivait {"token": ...} et ecrasait donc tout
    le reste : ajouter des ports au fichier les aurait fait disparaitre
    des qu'un admin changeait le mot de passe.
    """
    base = _load_cfg()
    base.update(cfg)
    CONFIG_FILE.write_text(json.dumps(base, indent=2), encoding="utf-8")


def get_ports() -> dict:
    """Retourne {"port_positions": int, "port_audio": int, "port_update": int}.

    Les cles absentes sont ecrites avec leur valeur par defaut, pour que
    l'admin decouvre le format en ouvrant le fichier au lieu d'avoir a
    le deviner.
    Une valeur invalide (texte, port hors plage) est ignoree au profit du
    defaut : un serveur qui refuse de demarrer a cause d'une faute de
    frappe serait pire que le probleme qu'on resout.
    """
    cfg = _load_cfg()
    ports = {}
    manquant = False
    for cle, defaut in DEFAULT_PORTS.items():
        val = cfg.get(cle)
        try:
            val = int(val)
            if not (1 <= val <= 65535):
                raise ValueError(val)
        except Exception:
            if cle in cfg:
                print(f"[CONFIG] {cle} invalide ({cfg.get(cle)!r}), "
                      f"defaut {defaut} utilise", flush=True)
            val = defaut
            manquant = True
        ports[cle] = val
    if manquant or any(c not in cfg for c in DEFAULT_PORTS):
        try:
            _save_cfg(ports)
        except Exception:
            pass
    return ports


# [DISCORD 30/07/2026] Identifiants de l'application Discord.
# client_id est PUBLIC (il transite jusqu'au client, qui en a besoin pour
# construire l'URL d'autorisation). client_secret ne quitte JAMAIS le
# serveur : il ne doit figurer ni dans RELEASE_FILES, ni dans aucun
# fichier livre au joueur.
def get_discord() -> dict:
    """Retourne {"client_id": str, "client_secret": str}.

    Absents par defaut : tant qu'ils ne sont pas renseignes, la liaison
    Discord repond "non configuree" au lieu de planter. Le serveur
    demarre donc normalement sur une installation qui ne s'en sert pas.
    """
    cfg = _load_cfg()
    return {
        "client_id": str(cfg.get("discord_client_id", "") or ""),
        "client_secret": str(cfg.get("discord_client_secret", "") or ""),
        # Comptes SANS Discord, pour l'outillage. Desactive par defaut :
        # active, c'est un contournement de la liaison obligatoire.
        "allow_service_accounts": bool(cfg.get("allow_service_accounts", False)),
        # Secret propre a l'outillage. DISTINCT du mot de passe joueur :
        # celui-ci est connu de tous les joueurs, s'en servir revenait a
        # laisser n'importe qui creer des comptes sans Discord.
        "service_secret": str(cfg.get("service_secret", "") or ""),
    }


def load_or_create_token() -> str:
    """
    Charge le token/mdp depuis le fichier config, ou en genere un nouveau
    si le fichier n'existe pas.
    """
    token = _load_cfg().get("token")
    if token:
        return token

    # Generer un nouveau token par defaut
    token = _generate_token()
    save_token(token)
    return token


def save_token(token: str):
    """Sauvegarde le token/mdp SANS ecraser les autres cles (ports...)."""
    _save_cfg({"token": token})


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
