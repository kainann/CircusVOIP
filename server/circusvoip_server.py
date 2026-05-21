"""
CircusVOIP Server (PATCHE SECURITE)
====================================
Serveur de partage de positions pour CircusVOIP.
Un joueur héberge, les autres s'y connectent via IP locale.

Lancement :
  py -3.13 circusvoip_server.py

Dépendances :
  pip install websockets

PATCHES SECURITE appliques :
  [P1] compare_digest sur tokens (joueur + admin) - anti-timing-attack
  [P2] Cap MAX_CLIENTS sur le nombre de joueurs simultanes
  [P3] Lockout brute force par IP (5 echecs en 60s -> ban 10 min)
  [P4] Validation typee des positions (anti-crash UI sur payload malforme)
  [P5] Tokens masques dans les logs et hors de admin_welcome
"""

import asyncio
import hashlib
import json
import secrets
import socket
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

# Tkinter n'est pas necessaire en mode headless. Import conditionnel
# pour permettre le deploiement sur des VPS Linux sans tkinter installe.
if "--headless" not in sys.argv:
    # DPI awareness avant tkinter (ecrans haute resolution)
    try:
        import circusvoip_dpi
        circusvoip_dpi.enable_dpi_awareness()
    except Exception:
        pass
    import tkinter as tk
    from tkinter import font as tkfont
else:
    tk = None
    tkfont = None

import websockets

from circusvoip_server_config import get_token, set_password

# [SECURITE] Module commun (lockout, rate limiting, registre de tickets,
# helper TLS). Doit etre present dans le meme dossier que ce fichier.
# Note : l'anti-bruteforce IP est deja gere par _record_auth_failure /
# _auth_failures / _auth_banned plus bas (legacy mais fonctionnel), donc
# on n'utilise PAS AuthLockout ici - juste AuthRegistry (auth partagee
# avec le serveur audio) et RateLimiter (anti-flood messages).
from circusvoip_security import AuthRegistry, RateLimiter

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

# [P1 - TLS] HOST reste en 0.0.0.0 pour un usage LAN classique.
# Si tu mets un reverse proxy (Caddy/nginx) devant pour le TLS, passe
# cette valeur a "127.0.0.1" pour que SEUL le proxy local puisse parler
# au serveur (voir le bloc TLS dans _server_main()).
HOST = "0.0.0.0"
PORT = 8888
CLIENT_TIMEOUT = 30.0
SERVER_TOKEN = get_token()  # charge ou cree le token joueur

# [P2] Limite le nombre de joueurs simultanes (DoS guard).
MAX_CLIENTS = 64

# [P3] Parametres du lockout brute force sur l'authentification.
# Une IP qui rate l'auth AUTH_MAX_FAILURES fois dans AUTH_WINDOW_SEC
# secondes est bannie pendant AUTH_BAN_SEC secondes.
AUTH_MAX_FAILURES = 5
AUTH_WINDOW_SEC   = 60
AUTH_BAN_SEC      = 600

# Log debug serveur (meme dossier que le client)
_BASE_DIR       = Path(__file__).resolve().parent
_DEBUG_DIR      = _BASE_DIR / "circusvoip_debug"
DEBUG_LOG_FILE  = _DEBUG_DIR / "circusvoip_server_debug.log"

# === Log debug enrichi (ajout 25/05/2026) ===
# Pour debug crackling audio / stats serveur positions, on prefere un
# fichier par session dans /var/log/circusvoip-positions/positions_<TS>.log
# (cohabite avec /var/log/circusvoip-audio/). Si le dossier n'est pas
# accessible (pas root, droits manquants), on garde le comportement
# historique (DEBUG_LOG_FILE ecrase a chaque demarrage).
_POS_LOG_DIR = Path("/var/log/circusvoip-positions")
_debug_log_actual_path = None  # chemin reellement utilise (rempli par _debug_log_init)
_debug_log_fp   = None
_last_pos_time: dict = {}  # dernier timestamp par joueur (pour dt)

# [P4 - auth partagee] Registre de tickets partage avec le serveur audio.
# A chaque joueur authentifie, le serveur positions emet un ticket court
# (TTL 120s) ecrit dans circusvoip_auth_tickets.json. Le serveur audio lit
# ce fichier pour verifier qu'un client est bien passe par ici d'abord.
_AUTH_REGISTRY_FILE = _BASE_DIR / "circusvoip_auth_tickets.json"
_auth_registry = AuthRegistry(_AUTH_REGISTRY_FILE, ttl_sec=120.0)

# [P5 - rate limiting] Quota de messages par client deja authentifie.
# 50 messages/s en regime permanent, 100 de reserve pour les rafales.
# Protege contre un membre malveillant qui flood pos/ping/etc.
_msg_rate = RateLimiter(rate=50.0, burst=100.0)

# [D5] Photos de profil : le serveur stocke un JPEG par pseudo + un index
# JSON {pseudo: {hash, ts}}. Distribution a la demande (request avec hash
# if-none-match pour epargner la bande passante). Limite stricte sur la
# taille du JPEG pour eviter qu'un client malveillant ne sature le disque
# ou la bande passante. Pas de broadcast au login : un client demande la
# photo d'un pair uniquement quand il a besoin de l'afficher (ouverture
# MP, sonnerie d'appel, ecran contacts).
_PROFILE_PHOTOS_DIR        = _BASE_DIR / "circusvoip_profile_photos"
_PROFILE_PHOTOS_INDEX_FILE = _BASE_DIR / "circusvoip_profiles.json"
# 200 Ko de bytes JPEG. Cote client on compresse en 200x200 q80, ce qui
# donne typiquement 15-30 Ko. 200 Ko laisse de la marge pour des photos
# riches en details. Le base64 transmis pese ~4/3 de cette taille.
_PROFILE_PHOTO_MAX_BYTES   = 200_000
# Cap defensif sur le pseudo (evite path traversal via clients malveillants
# avant validation par _is_safe_pseudo).
_PROFILE_PSEUDO_MAX_LEN    = 64

# Token admin (distinct du token joueur, pour qu'un joueur ne puisse pas
# devenir admin avec son seul mdp). Stocke dans un fichier separe.
_ADMIN_TOKEN_FILE = _BASE_DIR / "circusvoip_admin_token.json"


def _generate_admin_token() -> str:
    """Genere un token admin aleatoire (16 chars hex = 64 bits)."""
    return secrets.token_hex(16)


def _load_admin_token() -> str:
    """Charge le token admin depuis le fichier JSON. Si absent, en genere
    un nouveau et le sauvegarde. Retourne le token (string)."""
    try:
        if _ADMIN_TOKEN_FILE.exists():
            with open(_ADMIN_TOKEN_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            t = data.get("token")
            if isinstance(t, str) and t.strip():
                return t.strip()
    except Exception as e:
        print(f"[ADMIN TOKEN] Echec lecture {_ADMIN_TOKEN_FILE.name} : {e}")
    # Generer + sauvegarder
    new_token = _generate_admin_token()
    try:
        with open(_ADMIN_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": new_token}, f, indent=2)
    except Exception as e:
        print(f"[ADMIN TOKEN] Echec sauvegarde : {e}")
    return new_token


def _save_admin_token(new_token: str) -> bool:
    """Sauvegarde un nouveau token admin (utilise par la commande set_admin_token).
    Met aussi a jour la variable globale ADMIN_TOKEN."""
    global ADMIN_TOKEN
    new_token = (new_token or "").strip()
    if not new_token:
        return False
    try:
        with open(_ADMIN_TOKEN_FILE, "w", encoding="utf-8") as f:
            json.dump({"token": new_token}, f, indent=2)
        ADMIN_TOKEN = new_token
        return True
    except Exception as e:
        print(f"[ADMIN TOKEN] Echec sauvegarde : {e}")
        return False


# Charger au demarrage (genere si inexistant)
ADMIN_TOKEN: str = _load_admin_token()


# ─────────────────────────────────────────────
#  [P5] Helper de masquage des tokens dans les logs
# ─────────────────────────────────────────────

def _masked(token: str) -> str:
    """Retourne les 4 premiers caracteres + '***' pour log.
    Le token complet reste visible dans l'UI et dans le fichier de config,
    mais on evite qu'il finisse dans journalctl ou dans le push admin."""
    if not token or len(token) < 4:
        return "***"
    return token[:4] + "***"


# ─────────────────────────────────────────────
#  [P3] Lockout brute force
# ─────────────────────────────────────────────

_auth_failures: dict = {}   # ip -> [timestamp1, timestamp2, ...]
_auth_banned:   dict = {}   # ip -> timestamp_unban


def _check_auth_ban(ip: str) -> bool:
    """Retourne True si l'IP est actuellement bannie."""
    until = _auth_banned.get(ip)
    if until is None:
        return False
    if time.time() >= until:
        _auth_banned.pop(ip, None)
        _auth_failures.pop(ip, None)
        return False
    return True


def _record_auth_failure(ip: str):
    """Enregistre un echec d'auth pour cette IP. Bannit si seuil atteint."""
    now = time.time()
    fails = _auth_failures.get(ip, [])
    # Ne garder que les echecs dans la fenetre glissante
    fails = [t for t in fails if now - t < AUTH_WINDOW_SEC]
    fails.append(now)
    _auth_failures[ip] = fails
    if len(fails) >= AUTH_MAX_FAILURES:
        _auth_banned[ip] = now + AUTH_BAN_SEC
        _log(f"BAN auth : {ip} pour {AUTH_BAN_SEC}s "
             f"({len(fails)} echecs en {AUTH_WINDOW_SEC}s)", RED)


def _record_auth_success(ip: str):
    """Reset les compteurs apres une auth reussie."""
    _auth_failures.pop(ip, None)


# ─────────────────────────────────────────────
#  [P4] Validation typee des positions
# ─────────────────────────────────────────────

def _validate_pos(pos) -> bool:
    """Valide qu'une position contient bien des nombres pour x, y, z.
    Rejette aussi NaN et Inf qui feraient crasher l'UI quand elle
    fait pos.get('x', 0) / 1000."""
    if not isinstance(pos, dict):
        return False
    for k in ("x", "y", "z"):
        v = pos.get(k)
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return False
        # NaN != NaN, donc v != v detecte NaN
        if v != v or v in (float("inf"), float("-inf")):
            return False
    return True


def _debug_log_init():
    """Ouvre/reinitialise le fichier de log debug serveur.

    Modif 25/05/2026 : tente d'abord d'ouvrir un fichier par session dans
    /var/log/circusvoip-positions/positions_YYYYMMDD_HHMMSS.log (cohabite
    avec /var/log/circusvoip-audio/, permet de garder l'historique entre
    redemarrages). Si le dossier n'est pas accessible (pas root / droits
    manquants), fallback sur le DEBUG_LOG_FILE historique (ecrase a chaque
    demarrage)."""
    global _debug_log_fp, _debug_log_actual_path
    # 1) Tentative /var/log/circusvoip-positions/positions_<TS>.log
    try:
        _POS_LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        target_path = _POS_LOG_DIR / f"positions_{ts}.log"
        _debug_log_fp = open(target_path, "w", encoding="utf-8", buffering=1)
        _debug_log_fp.write(
            f"=== Session demarree : {datetime.now():%Y-%m-%d %H:%M:%S} ===\n"
        )
        _debug_log_actual_path = target_path
        print(f"[DEBUG LOG] Fichier : {target_path}", flush=True)
        return
    except Exception as e_varlog:
        # /var/log inaccessible (pas root, FS readonly, etc.) : on tombe sur
        # le chemin historique.
        print(f"[DEBUG LOG] /var/log indisponible ({type(e_varlog).__name__}: "
              f"{e_varlog}), fallback {DEBUG_LOG_FILE}", flush=True)

    # 2) Fallback historique (DEBUG_LOG_FILE unique, ecrase a chaque demarrage)
    try:
        _DEBUG_DIR.mkdir(exist_ok=True)
        _debug_log_fp = open(DEBUG_LOG_FILE, "w", encoding="utf-8", buffering=1)
        _debug_log_fp.write(
            f"=== Session demarree : {datetime.now():%Y-%m-%d %H:%M:%S} ===\n"
        )
        _debug_log_actual_path = DEBUG_LOG_FILE
    except Exception as e:
        print(f"[DEBUG LOG] Echec ouverture fichier : {e}")
        _debug_log_fp = None
        _debug_log_actual_path = None


def _debug_log(msg: str):
    """Ecrit une ligne dans le fichier de log debug serveur."""
    if _debug_log_fp is None:
        return
    try:
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        _debug_log_fp.write(f"[{ts}] {msg}\n")
    except Exception:
        pass


def _debug_log_pos(name: str, pos: dict, ts_capture: float = None):
    """Log d'une position recue avec le delta depuis la derniere pos de ce joueur.
    Si ts_capture est fourni, calcule aussi le dt_net (latence reseau client->serveur)."""
    if _debug_log_fp is None:
        return
    try:
        now = time.time()
        prev_t = _last_pos_time.get(name)
        _last_pos_time[name] = now
        x = pos.get("x", 0)
        y = pos.get("y", 0)
        z = pos.get("z", 0)
        zone = pos.get("zone", "?")
        # Latence reseau (si ts_capture fourni)
        net_str = ""
        if ts_capture:
            try:
                dt_net = int((now - float(ts_capture)) * 1000)
                net_str = f" dt_net={dt_net}ms"
            except Exception:
                pass
        if prev_t:
            dt_ms = int((now - prev_t) * 1000)
            _debug_log(f"[POS] {name} x={x} y={y} z={z} zone={zone} (dt={dt_ms}ms{net_str})")
        else:
            _debug_log(f"[POS] {name} x={x} y={y} z={z} zone={zone} (dt=first{net_str})")
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Couleurs
# ─────────────────────────────────────────────

BG       = "#0d1117"
BG_PANEL = "#161b22"
BG_ROW   = "#21262d"
BORDER   = "#30363d"
TEXT     = "#c9d1d9"
MUTED    = "#6e7681"
GREEN    = "#3fb950"
ORANGE   = "#d29922"
BLUE     = "#58a6ff"
RED      = "#f85149"
PURPLE   = "#bc8cff"

# ─────────────────────────────────────────────
#  État global
# ─────────────────────────────────────────────

clients: dict = {}
# Liste des connexions admin authentifiees. dict {ws: {"connected_at": ts}}
# Les admins recoivent les push events (logs, joueurs join/leave/move, etc.)
# et peuvent envoyer des commandes (add_channel, assign_profile, etc.).
admins: dict = {}
_ui: "ServerUI | None" = None
_server_task = None
_server_running = False
# Mode anonyme : quand actif, demande aux clients de masquer dans leur UI
# la zone, les coordonnees X Y Z, et la distance des autres joueurs.
# Le serveur continue de broadcaster les positions normalement (la VOIP
# positionnelle continue de fonctionner) ; c'est juste l'affichage cote
# client qui est filtre. Pour du jeu de role.
_anonymous_mode: bool = False

# === Stats debug crackling (ajout 25/05/2026) ===
# Compteurs cumulatifs par nom de client. Lus toutes les 30s par
# _cleanup_loop pour produire [POS STATS DEBUG]. Niveau B : throttle 30s
# pour log "premier drop" / "premier dead broadcast" par client.
#
# Pourquoi le serveur positions est utile pour le debug audio :
# - Il route les volumes / autorisations HP (haut-parleur CircusPhone),
#   les channels radio, les profils. Un client qui floode peut etre
#   shoote ici (rate limiter pos), ce qui perturbe le calcul des
#   autorisations VOIP cote autres clients.
# - Les broadcasts pos vers clients morts permettent de detecter qu'un
#   joueur a une WS pos qui lache (peut-etre correle a sa WS audio).
_pos_rate_limit_drops_by_name: dict = {}   # {pseudo: count}
_pos_rate_limit_first_drop_logged: dict = {}  # {pseudo: monotonic_ts}
_pos_broadcast_dead_by_name: dict = {}     # {pseudo: count} - cumule sur les
                                            # 4 fonctions _broadcast_* (sauf admins)
_pos_broadcast_dead_first_logged: dict = {}  # {pseudo: monotonic_ts}

# ─────────────────────────────────────────────
#  CircusPhone - Feature 4 (D1 : table d'appels serveur)
# ─────────────────────────────────────────────
# Table des appels en cours. Cle = call_id (hex 8 chars genere a la
# demande d'appel). Valeur = dict :
#   {
#     "caller":      str,            # pseudo de l'appelant
#     "callee":      str,            # pseudo de l'appele
#     "state":       str,            # "ringing" | "active"
#     "created_at":  float,          # time.time() a la creation
#     "accepted_at": float | None,   # time.time() quand l'appel a ete decroche
#     "ring_task":   asyncio.Task | None,  # timer 45s (annule au decroche)
#   }
# Vit uniquement en RAM : un restart serveur purge tout (les clients
# encore en appel verront leur WS se couper -> appel coupe cote client).
# En parallele, chaque transition d'etat est journalisee dans
# phone_calls.log (un objet JSON par ligne) pour le debug post-mortem.
active_calls: dict = {}

# Duree maximale de sonnerie avant "appel non abouti" (spec : 45 s).
PHONE_RING_TIMEOUT_S = 45.0

# Log debug dedie aux appels (separe du log serveur generique).
PHONE_LOG_FILE = _DEBUG_DIR / "phone_calls.log"
_phone_log_fp = None

# ─────────────────────────────────────────────
#  Canaux radio
# ─────────────────────────────────────────────
# Liste de canaux radio nommes (ex: ["General", "Pont", "Tourelles"]).
# Chaque joueur connecte choisit UN canal (stocke dans clients[ws]["channel"]).
# La radio (PTT) ne sera audible que par les joueurs sur le MEME canal.
# La voix de proximite n'est PAS affectee par les canaux.

_CHANNELS_FILE  = _BASE_DIR / "circusvoip_channels.json"
_DEFAULT_CHANNEL = "General"
_channels: list = []

# Liste de profils (factions/tags). Assignes par l'admin via assign_profile,
# le client ne peut PAS se les attribuer lui-meme. Independants des canaux.
_PROFILES_FILE = _BASE_DIR / "circusvoip_profiles.json"
_profiles: list = []

# Broadcasters : joueurs autorises a parler simultanement sur TOUS les canaux
# radio (PTT diffusion globale, flag audio 0x04). L'admin gere la liste via
# grant_broadcaster / revoke_broadcaster. La capability est propagee au serveur
# audio via le ticket (cf. AuthRegistry.issue(can_broadcast=)).
#
# Modele d'auth (cf. feat/broadcaster-token-auth) :
#   _broadcasters : dict {name: sha256_hex_du_token}
#   Le token clair est genere au grant, hashe ici, et pushed une seule fois au
#   client cible via sa WebSocket. Le client le sauvegarde dans son config et
#   le presente au join (champ "broadcaster_token"). Le serveur compare en
#   temps constant. Sans le bon token, le nom est REFUSE au join (anti-impersonation).
_BROADCASTERS_FILE = _BASE_DIR / "circusvoip_broadcasters.json"
_broadcasters: dict = {}


def _hash_broadcaster_token(token: str) -> str:
    """Hash SHA-256 hexa du token broadcaster.
    On ne stocke jamais le token en clair cote serveur ; on ne peut donc pas
    le re-emettre apres le grant initial (re-grant requis pour le renvoyer)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _verify_broadcaster_token(name: str, token: str) -> bool:
    """Verifie en temps constant que `token` correspond au hash stocke pour `name`.
    Renvoie False si le nom n'est pas dans _broadcasters, si le token est vide,
    ou si le hash ne correspond pas."""
    if not name or not token or not isinstance(token, str):
        return False
    stored = _broadcasters.get(name)
    if not stored:
        return False
    candidate = _hash_broadcaster_token(token)
    return secrets.compare_digest(candidate, stored)


def _load_channels() -> list:
    """Charge la liste des canaux depuis le fichier JSON (liste de strings).
    Retourne au moins ['General'] si le fichier est absent/invalide."""
    try:
        if _CHANNELS_FILE.exists():
            with open(_CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                cleaned = []
                seen = set()
                for item in data:
                    # Retro-compat : ancien format dict avec is_profile=true
                    # est ignore ici (les profils sont dans _profiles maintenant).
                    if isinstance(item, str):
                        n = item.strip()
                        if n and n not in seen:
                            cleaned.append(n)
                            seen.add(n)
                    elif isinstance(item, dict) and isinstance(item.get("name"), str):
                        if not item.get("is_profile", False):
                            n = item["name"].strip()
                            if n and n not in seen:
                                cleaned.append(n)
                                seen.add(n)
                if cleaned:
                    return cleaned
    except Exception as e:
        print(f"[CHANNELS] Echec chargement {_CHANNELS_FILE.name} : {e}")
    return [_DEFAULT_CHANNEL]


def _save_channels():
    """Persiste la liste des canaux dans le fichier JSON (liste de strings)."""
    try:
        with open(_CHANNELS_FILE, "w", encoding="utf-8") as f:
            json.dump(_channels, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[CHANNELS] Echec sauvegarde {_CHANNELS_FILE.name} : {e}")


# Cle des permissions supportees sur un profil. Pour ajouter une
# nouvelle permission, ajouter ici sa cle ET sa valeur par defaut.
# Toutes les permissions sont des booleens. False = pas autorise.
_PROFILE_PERM_DEFAULTS = {
    "soundboard_allowed": False,
    # Futurs : "phone_allowed": False, etc.
}


def _profile_default_dict(name: str) -> dict:
    """Cree un dict profil avec les permissions par defaut (toutes a False)."""
    d = {"name": name}
    d.update(_PROFILE_PERM_DEFAULTS)
    return d


def _profile_normalize(item) -> dict | None:
    """Convertit une entree de fichier en dict profil normalise. Accepte
    deux formats :
      - str "Nom" : ancien format, on cree un dict avec permissions
        par defaut (toutes a False).
      - dict {"name": "Nom", "soundboard_allowed": bool, ...} : format
        actuel, on garde et on complete les permissions manquantes
        avec leur defaut.
    Retourne None si l'entree est invalide (vide, mal formee)."""
    if isinstance(item, str):
        n = item.strip()
        if not n:
            return None
        return _profile_default_dict(n)
    if isinstance(item, dict):
        n = (item.get("name") or "").strip()
        if not n:
            return None
        d = {"name": n}
        # Pour chaque permission connue : prendre la valeur du fichier
        # si presente et valide, sinon le defaut.
        for perm_key, default in _PROFILE_PERM_DEFAULTS.items():
            v = item.get(perm_key, default)
            d[perm_key] = bool(v)
        return d
    return None


def _load_profiles() -> list:
    """Charge la liste des profils depuis circusvoip_profiles.json.
    Format actuel (v0.2 alpha 035) : liste de dicts avec permissions.
    Format ancien (pre-0.2) : liste de strings -> converti auto au boot.
    Retourne [] si absent (les profils sont optionnels).
    Migration : si l'ancien fichier circusvoip_channels.json contenait
    des entries avec is_profile=true, on les recupere aussi."""
    profiles = []
    seen = set()
    needs_resave = False
    try:
        if _PROFILES_FILE.exists():
            with open(_PROFILES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        # Ancien format detecte : sera resauvegarde en
                        # format dict ci-dessous.
                        needs_resave = True
                    norm = _profile_normalize(item)
                    if norm is None:
                        continue
                    if norm["name"] in seen:
                        continue
                    profiles.append(norm)
                    seen.add(norm["name"])
    except Exception as e:
        print(f"[PROFILES] Echec chargement {_PROFILES_FILE.name} : {e}")
    # Migration depuis l'ancien format channels (is_profile=true)
    try:
        if _CHANNELS_FILE.exists():
            with open(_CHANNELS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                migrated = []
                for item in data:
                    if isinstance(item, dict) and item.get("is_profile", False):
                        n = (item.get("name") or "").strip()
                        if n and n not in seen:
                            profiles.append(_profile_default_dict(n))
                            seen.add(n)
                            migrated.append(n)
                            needs_resave = True
                if migrated:
                    print(f"[PROFILES] Migration auto depuis channels.json : {migrated}")
    except Exception:
        pass
    # Si on a converti l'ancien format string -> dict, on resauve
    # pour persister la migration (sinon on referait la conversion
    # a chaque boot).
    if needs_resave:
        try:
            with open(_PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(profiles, f, ensure_ascii=False, indent=2)
            print(f"[PROFILES] Migration auto vers format dict effectuee dans {_PROFILES_FILE.name}")
        except Exception as e:
            print(f"[PROFILES] Migration auto KO (sauvegarde) : {e}")
    return profiles


def _save_profiles():
    """Persiste la liste des profils dans circusvoip_profiles.json
    (format dict avec permissions)."""
    try:
        with open(_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(_profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PROFILES] Echec sauvegarde {_PROFILES_FILE.name} : {e}")


def _profile_names() -> list:
    """Retourne la liste des noms de profils (pour les anciens callers
    qui s'attendent a une list[str]). _profiles est maintenant list[dict]."""
    return [p["name"] for p in _profiles if isinstance(p, dict) and p.get("name")]


def _profile_find(name: str) -> dict | None:
    """Retourne le dict profil pour le nom donne, ou None si non trouve."""
    if not name:
        return None
    for p in _profiles:
        if isinstance(p, dict) and p.get("name") == name:
            return p
    return None


def _profile_has_perm(name: str, perm_key: str) -> bool:
    """Verifie si un profil a une permission donnee. Retourne False
    si le profil n'existe pas, n'a pas la permission, ou si la permission
    n'est pas dans la liste connue."""
    p = _profile_find(name)
    if p is None:
        return False
    return bool(p.get(perm_key, False))


def _build_my_profile_msg(profile_name) -> dict:
    """Construit le payload du message my_profile envoye a un client.
    Inclut :
      - profile : nom du profil (ou None si pas assigne).
      - une cle par permission (ex: soundboard_allowed=bool).
    Si profile_name est None, toutes les permissions sont False
    (= pas de profil = aucune permission, cf. spec Q1)."""
    msg = {"type": "my_profile", "profile": profile_name}
    for perm_key in _PROFILE_PERM_DEFAULTS.keys():
        if profile_name is None:
            msg[perm_key] = False
        else:
            msg[perm_key] = _profile_has_perm(profile_name, perm_key)
    return msg


def _load_broadcasters() -> dict:
    """Charge le mapping {name: hash} depuis circusvoip_broadcasters.json.
    Retourne {} si absent (la capability est opt-in).

    Migration : l'ancien format etait une liste de noms (pas de token). Si on
    detecte ce format, on log un warning et on retourne un dict vide : l'admin
    doit re-grant chaque broadcaster pour generer leurs tokens. On ne migre
    PAS en silence (cela donnerait l'illusion d'un setup sûr alors qu'aucun
    token n'aurait ete distribue aux joueurs concernes)."""
    try:
        if _BROADCASTERS_FILE.exists():
            with open(_BROADCASTERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                # Ancien format detecte : pas de tokens, re-grant requis.
                old_names = [n for n in data if isinstance(n, str) and n.strip()]
                if old_names:
                    print(
                        f"[BROADCASTERS] Ancien format detecte dans "
                        f"{_BROADCASTERS_FILE.name} ({len(old_names)} entree(s) : "
                        f"{old_names}). Les broadcasters DOIVENT etre re-grant "
                        f"pour generer leurs tokens. Liste videe."
                    )
                return {}
            if isinstance(data, dict):
                # Format actuel : {name: sha256_hex}.
                clean = {}
                for name, h in data.items():
                    if (isinstance(name, str) and name.strip()
                            and isinstance(h, str) and len(h) == 64):
                        clean[name.strip()] = h
                return clean
    except Exception as e:
        print(f"[BROADCASTERS] Echec chargement {_BROADCASTERS_FILE.name} : {e}")
    return {}


def _save_broadcasters():
    """Persiste le mapping {name: hash} dans circusvoip_broadcasters.json.
    Le hash uniquement est persiste, jamais le token en clair."""
    try:
        with open(_BROADCASTERS_FILE, "w", encoding="utf-8") as f:
            json.dump(_broadcasters, f, ensure_ascii=False, indent=2, sort_keys=True)
    except Exception as e:
        print(f"[BROADCASTERS] Echec sauvegarde {_BROADCASTERS_FILE.name} : {e}")


# Initialisation : charger au demarrage du module
_channels = _load_channels()
_profiles = _load_profiles()
_broadcasters = _load_broadcasters()
if _profiles and not _PROFILES_FILE.exists():
    _save_profiles()


def _now():
    return datetime.now().strftime("%H:%M:%S.") + f"{int(time.time() * 1000) % 1000:03d}"


def _log(msg: str, color: str = TEXT):
    ts = _now()
    print(f"[{ts}] {msg}")
    if _ui:
        _ui.add_log(f"[{ts}] {msg}", color)
    # Aussi dans le fichier debug
    _debug_log(msg)
    # Push aux admins connectes (via WS)
    _broadcast_admins_threadsafe(json.dumps({
        "type": "log",
        "msg": msg,
        "color": color,
        "ts": ts,
    }))


# ─────────────────────────────────────────────
#  WebSocket
# ─────────────────────────────────────────────

async def _broadcast(sender_ws, message: str):
    """Envoie un message a tous les joueurs sauf l'emetteur, et a tous
    les admins (les admins recoivent toujours, ils ne sont pas dans clients)."""
    dead = []
    for ws in list(clients.keys()):
        if ws is sender_ws:
            continue
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _track_dead_broadcast(ws, "_broadcast")
        clients.pop(ws, None)
    # Broadcast aussi aux admins (push event)
    await _broadcast_admins(message)


async def _broadcast_all(message: str):
    """Envoie un message a tous les joueurs ET tous les admins."""
    dead = []
    for ws in list(clients.keys()):
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _track_dead_broadcast(ws, "_broadcast_all")
        clients.pop(ws, None)
    await _broadcast_admins(message)


async def _broadcast_clients_only(message: str):
    """Envoie un message a tous les joueurs (pas aux admins).
    Utile quand on veut envoyer un format different aux admins (qui
    peuvent recevoir plus d'infos), comme pour profiles_list ou les
    permissions de profils."""
    dead = []
    for ws in list(clients.keys()):
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _track_dead_broadcast(ws, "_broadcast_clients_only")
        clients.pop(ws, None)


async def _broadcast_channel(channel, message: str):
    """Envoie un message a tous les joueurs du canal donne (et seulement
    eux). L'emetteur recoit aussi : utile pour le soundboard ou
    l'emetteur veut s'entendre. Les admins recoivent aussi via
    _broadcast_admins (push event).

    channel = None ou un id de canal. Si None, on broadcast aux joueurs
    qui n'ont pas de canal (rare cas)."""
    dead = []
    for ws, info in list(clients.items()):
        if info.get("channel") != channel:
            continue
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        _track_dead_broadcast(ws, "_broadcast_channel")
        clients.pop(ws, None)
    await _broadcast_admins(message)


def _track_dead_broadcast(ws, fn_name: str):
    """Helper : incremente le compteur de morts en broadcast pour le
    client donne et logge "premier dead" avec throttle 30s. Appele
    par les 4 fonctions _broadcast_* (pas _broadcast_admins, moins
    critique pour le bug audio). Ajout 25/05/2026.

    Note : ws peut etre une socket pas (ou plus) dans clients{}, dans
    quel cas on enregistre sous "<unknown>". C'est rare (race condition
    cleanup vs broadcast) mais possible."""
    try:
        info = clients.get(ws, {}) or {}
        name = info.get("name") or "<unknown>"
        _pos_broadcast_dead_by_name[name] = (
            _pos_broadcast_dead_by_name.get(name, 0) + 1
        )
        now_log = time.monotonic()
        last_logged = _pos_broadcast_dead_first_logged.get(name, 0.0)
        if (now_log - last_logged) >= 30.0:
            _debug_log(
                f"[BROADCAST DEAD] client {name!r} deconnecte pendant "
                f"un broadcast (fonction: {fn_name}, total: "
                f"{_pos_broadcast_dead_by_name[name]})"
            )
            _pos_broadcast_dead_first_logged[name] = now_log
    except Exception:
        # Robustesse : si pour une raison X le log echoue, on ne casse
        # pas la chaine de cleanup. Le clients.pop() qui suit DOIT marcher.
        pass


async def _broadcast_admins(message: str):
    """Envoie un message a tous les admins authentifies."""
    dead = []
    for ws in list(admins.keys()):
        try:
            await ws.send(message)
        except Exception:
            dead.append(ws)
    for ws in dead:
        admins.pop(ws, None)


def _broadcast_admins_threadsafe(message: str):
    """Version thread-safe pour appeler _broadcast_admins depuis un thread
    qui n'est pas la loop asyncio (UI Tkinter, fonctions admin synchrones)."""
    if _loop and _server_running:
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_admins(message), _loop)
        except Exception:
            pass


async def _cleanup_loop():
    # === Stats debug crackling (25/05/2026) ===
    # Compteur de ticks 5s pour produire un log [POS STATS DEBUG] toutes
    # les 30s (6 ticks). Snapshots pour calculer les deltas.
    _stats_tick = 0
    _prev_rl_drops: dict = {}
    _prev_bc_dead:  dict = {}
    while True:
        await asyncio.sleep(5)
        now = time.time()
        dead = [ws for ws, info in list(clients.items())
                if now - info.get("last_seen", 0) > CLIENT_TIMEOUT]
        for ws in dead:
            info = clients.pop(ws, {})
            name = info.get("name", "?")
            _log(f"Timeout : {name}", ORANGE)
            if _ui:
                _ui.remove_player(name)
            # CircusPhone : un joueur qui timeout en plein appel doit
            # voir ses appels coupes proprement (sinon appel zombie).
            # Le cleanup_loop fait clients.pop() directement sans passer
            # par le finally du handler, d'ou cet appel explicite.
            await _phone_drop_calls_for(name, reason="peer_timeout")
            await _broadcast_all(json.dumps({"type": "leave", "name": name}))

        # Stats agregees toutes les 30s (= 6 ticks de 5s). Affichees dans
        # le fichier debug serveur (pas dans le log UI qui serait trop
        # spamme pour rien). Niveau A : si tout est OK, ligne "RAS" pour
        # confirmer que le mecanisme tourne. Si anomalies, deltas affiches.
        _stats_tick += 1
        if _stats_tick >= 6:
            _stats_tick = 0
            try:
                # Calcul des deltas rate limit drops
                rl_delta = {}
                for name, total in _pos_rate_limit_drops_by_name.items():
                    delta = total - _prev_rl_drops.get(name, 0)
                    if delta > 0:
                        rl_delta[name] = delta
                    _prev_rl_drops[name] = total
                # Calcul des deltas broadcast dead
                bc_delta = {}
                for name, total in _pos_broadcast_dead_by_name.items():
                    delta = total - _prev_bc_dead.get(name, 0)
                    if delta > 0:
                        bc_delta[name] = delta
                    _prev_bc_dead[name] = total

                n_clients = len(clients)
                client_names = ",".join(
                    sorted(info.get("name", "?")
                           for info in clients.values())
                ) or "(aucun)"
                if rl_delta or bc_delta:
                    parts = []
                    if rl_delta:
                        parts.append("rate_drops=" + ",".join(
                            f"{n}:{c}" for n, c in sorted(rl_delta.items())
                        ))
                    if bc_delta:
                        parts.append("broadcast_dead=" + ",".join(
                            f"{n}:{c}" for n, c in sorted(bc_delta.items())
                        ))
                    _debug_log(
                        f"[POS STATS DEBUG] clients={n_clients} "
                        f"({client_names}) | " + " | ".join(parts)
                    )
                else:
                    _debug_log(
                        f"[POS STATS DEBUG] clients={n_clients} "
                        f"({client_names}) | rate_drops=0 | "
                        f"broadcast_dead=0 | RAS"
                    )
            except Exception as e:
                # Robustesse : le log stats ne doit jamais casser la
                # boucle cleanup (qui detecte les timeouts joueurs).
                try:
                    _debug_log(
                        f"[POS STATS DEBUG] erreur snapshot : "
                        f"{type(e).__name__}: {e}"
                    )
                except Exception:
                    pass


# ─────────────────────────────────────────────
#  CircusPhone - helpers (Feature 4, D1)
# ─────────────────────────────────────────────

def _phone_log_init():
    """Ouvre (append) le fichier de log dedie aux appels CircusPhone.
    Best-effort : si l'ouverture echoue, _phone_log_event devient un
    no-op silencieux, le serveur continue de tourner normalement."""
    global _phone_log_fp
    try:
        _DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        _phone_log_fp = open(PHONE_LOG_FILE, "a", encoding="utf-8")
        _phone_log_event("log_init", call_id=None,
                         note="phone_calls.log ouvert")
    except Exception as e:
        _phone_log_fp = None
        print(f"[PHONE] Echec ouverture {PHONE_LOG_FILE.name} : {e}")


def _phone_log_event(event: str, call_id, **fields):
    """Journalise un evenement d'appel : un objet JSON par ligne.
    event   : nom de l'evenement (request, ringing, accept, decline,
              hangup, missed, busy, ended, drop_disconnect, ...).
    call_id : id de l'appel concerne (ou None pour les events globaux).
    fields  : champs additionnels (caller, callee, reason, ...).
    Best-effort : jamais d'exception remontee a l'appelant."""
    if _phone_log_fp is None:
        return
    try:
        rec = {
            "ts": datetime.now().isoformat(timespec="milliseconds"),
            "event": event,
            "call_id": call_id,
        }
        rec.update(fields)
        _phone_log_fp.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _phone_log_fp.flush()
    except Exception:
        # On n'interrompt jamais la logique d'appel pour un souci de log.
        pass


# ─────────────────────────────────────────────
#  [D5] Photos de profil : stockage + index
# ─────────────────────────────────────────────
#
# Modele :
#   - Un fichier <pseudo>.jpg par photo dans _PROFILE_PHOTOS_DIR.
#   - Un index JSON {pseudo: {"hash": "...", "ts": float}} dans
#     _PROFILE_PHOTOS_INDEX_FILE. Le hash est un SHA-256 hex calcule
#     cote client sur le JPEG (bytes finals apres compression), retransmis
#     dans l'upload et utilise par tous les clients pour le if-none-match.
#   - Pas de notification automatique : les pairs decouvrent la nouvelle
#     photo au prochain request (a l'affichage). C'est volontaire :
#     evite le broadcast au login et le tempete de trafic.

_profile_photos_index: dict = {}     # {pseudo: {"hash": str, "ts": float}}


def _is_safe_pseudo_for_file(name) -> bool:
    """Filtre defensif sur les pseudos avant tout acces disque a base de
    leur nom (path traversal, caracteres reserves Windows, longueur)."""
    if not isinstance(name, str) or not name:
        return False
    if len(name) > _PROFILE_PSEUDO_MAX_LEN:
        return False
    # Pas de separateur, pas de '..', pas de NUL ni autres caracteres bizarres
    bad = set('\\/:*?"<>|\x00')
    for ch in name:
        if ch in bad or ord(ch) < 32:
            return False
    if name in (".", "..") or name.startswith("."):
        return False
    return True


def _profile_photos_load_index():
    """Charge l'index des photos de profil depuis le disque. Recree le
    dossier de stockage si absent. Best-effort : un index manquant ou
    corrompu repart d'un dict vide."""
    global _profile_photos_index
    try:
        _PROFILE_PHOTOS_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[PROFILE] Echec mkdir {_PROFILE_PHOTOS_DIR} : {e}")
    if not _PROFILE_PHOTOS_INDEX_FILE.exists():
        _profile_photos_index = {}
        return
    try:
        with open(_PROFILE_PHOTOS_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # Nettoyage defensif : on vire les entrees malformees.
            cleaned = {}
            for pseudo, entry in data.items():
                if (isinstance(pseudo, str)
                        and isinstance(entry, dict)
                        and isinstance(entry.get("hash"), str)):
                    cleaned[pseudo] = {
                        "hash": entry["hash"],
                        "ts": float(entry.get("ts") or 0.0),
                    }
            _profile_photos_index = cleaned
        else:
            _profile_photos_index = {}
    except Exception as e:
        print(f"[PROFILE] Index corrompu, reset : {e}")
        _profile_photos_index = {}


def _profile_photos_save_index():
    """Sauvegarde l'index sur disque. Best-effort, jamais bloquant."""
    try:
        with open(_PROFILE_PHOTOS_INDEX_FILE, "w", encoding="utf-8") as f:
            json.dump(_profile_photos_index, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[PROFILE] Echec sauvegarde index : {e}")


def _profile_photo_path(pseudo: str):
    """Chemin disque du JPEG pour ce pseudo (ne valide PAS le pseudo : a
    appeler uniquement apres _is_safe_pseudo_for_file)."""
    return _PROFILE_PHOTOS_DIR / f"{pseudo}.jpg"


def _find_ws_by_name(name: str):
    """Retourne le ws du joueur portant ce pseudo, ou None s'il n'est pas
    connecte. Les pseudos sont uniques cote serveur (un joueur = un ws)."""
    if not name:
        return None
    for ws, info in clients.items():
        if info.get("name") == name:
            return ws
    return None


def _find_active_call_for(name: str):
    """Cherche un appel en cours (ringing OU active) impliquant ce joueur,
    qu'il soit caller ou callee. Retourne (call_id, call_dict) ou None.
    Sert a la regle 'occupe' : un joueur deja dans un appel ne peut pas
    en recevoir/passer un autre."""
    for call_id, call in active_calls.items():
        if call["caller"] == name or call["callee"] == name:
            return call_id, call
    return None


async def _send_to_name(name: str, payload: dict) -> bool:
    """Envoie un message JSON cible a un joueur designe par son pseudo.
    Retourne True si l'envoi a reussi, False si le joueur est introuvable
    ou si l'envoi a echoue (socket morte)."""
    ws = _find_ws_by_name(name)
    if ws is None:
        return False
    try:
        await ws.send(json.dumps(payload))
        return True
    except Exception:
        return False


def _phone_clear_call(call_id: str):
    """Retire un appel de la table et annule son timer de sonnerie s'il
    est encore actif. Idempotent : appeler 2x ne pose pas de probleme.

    D4b : notifie aussi tous les voisins HP courants que le HP est OFF
    (pour qu'ils nettoient leur etat 'autorise a entendre' cote client),
    et notifie le peer que la liste de voisins HP devient vide. Ainsi on
    n'a aucun cas de leak d'autorisation HP cote client meme si le call
    se termine brutalement (timeout, deco, etc.).

    Note technique : la fonction est sync (appelee depuis _ring_timeout
    qui n'est plus async, ainsi que depuis des handlers async). On utilise
    asyncio.get_running_loop() pour detecter si on est dans un contexte
    async ; si oui on schedule les notifs via create_task, sinon on les
    skippe silencieusement (cas exceptionnel, normalement le call_id n'a
    pas de HP a ce moment-la)."""
    call = active_calls.pop(call_id, None)
    if call is None:
        return
    task = call.get("ring_task")
    if task is not None and not task.done():
        task.cancel()
    # D4b : cleanup HP si actif sur cet appel.
    hp = call.get("hp") or {}
    hp_owner = hp.get("owner")             # le joueur qui avait active son HP
    hp_neighbors = hp.get("neighbors") or set()
    caller, callee = call.get("caller"), call.get("callee")
    if not hp_owner or not (caller or callee):
        return
    # Determiner si on peut scheduler des notifs async (on doit etre dans
    # une loop asyncio active : _phone_clear_call est presque toujours
    # appele depuis un context async, sauf _ring_timeout qui n'a pas de HP
    # de toute facon - pas de HP pendant le ringing).
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # Hors loop : pas de notif (rare, et le client gere son cleanup
        # via call_ended/peer_disconnect en parallele).
        return
    # Le peer de l'owner du HP est l'autre partie de l'appel.
    hp_peer = callee if hp_owner == caller else caller
    # Notifier les anciens voisins (ils n'entendent plus le peer en HP)
    for nb in hp_neighbors:
        asyncio.create_task(_send_to_name(nb, {
            "type": "phone_hp_inactive",
            "call_id": call_id,
            "owner": hp_owner,
            "peer": hp_peer,
        }))
    # Notifier le peer (sa liste de voisins entendables tombe a vide)
    if hp_peer:
        asyncio.create_task(_send_to_name(hp_peer, {
            "type": "phone_hp_neighbors_update",
            "call_id": call_id,
            "owner": hp_owner,
            "neighbors": [],
        }))


async def _phone_hp_apply_state(call_id: str, owner_name: str,
                                new_neighbors: set) -> None:
    """D4b : applique un nouvel etat HP pour un appel.

    owner_name : pseudo du joueur qui a active son HP (cote A dans la spec).
                 Ses voisins =5m vont pouvoir entendre le peer en 0x03 et
                 leur prox 0x00 sera entendue par le peer.
    new_neighbors : ensemble des pseudos voisins =5m fournis par le client.
                    Set vide = HP desactive (raccroche / toggle OFF).

    La fonction calcule les diffs avec l'ancienne liste stockee dans
    active_calls[call_id]["hp"] :
      - 'arrives' : nouveaux voisins -> phone_hp_active
      - 'partis'  : anciens voisins qui ne sont plus la -> phone_hp_inactive
    Et notifie le peer (l'autre partie de l'appel) avec la liste complete
    mise a jour via phone_hp_neighbors_update.

    Si call_id n'existe pas ou si owner_name n'est pas partie de l'appel,
    on ignore silencieusement (best-effort, le serveur reste autoritaire).
    """
    call = active_calls.get(call_id)
    if call is None:
        return
    if owner_name not in (call.get("caller"), call.get("callee")):
        return
    if call.get("state") != "active":
        # Pas de HP pendant la sonnerie : seulement quand l'appel est decroche.
        return

    # Le peer est l'autre partie de l'appel
    peer = call["callee"] if owner_name == call["caller"] else call["caller"]

    # On ne s'autorise pas soi-meme ni le peer dans la liste des voisins
    # (cas tordus : le peer est trop loin par definition, et soi-meme c'est
    # une absurdite). Tres important pour la securite/coherence cote serveur.
    new_neighbors = set(new_neighbors) - {owner_name, peer}

    old = call.get("hp") or {}
    old_owner = old.get("owner")
    old_neighbors = old.get("neighbors") or set()

    # Si un autre owner avait deja active son HP sur cet appel (cas tordu :
    # les 2 parties activent leur HP en meme temps), on rejette : un seul
    # HP a la fois par appel. Le 2eme owner ne peut pas s'imposer sans que
    # le 1er ne raccroche d'abord.
    if old_owner and old_owner != owner_name and new_neighbors:
        _log(f"[PHONE] {owner_name} : phone_speaker_state ignore "
             f"(HP deja actif par {old_owner})", ORANGE)
        return

    arrives = new_neighbors - old_neighbors
    partis = old_neighbors - new_neighbors

    # Mettre a jour l'etat dans active_calls
    if new_neighbors:
        call["hp"] = {"owner": owner_name, "neighbors": set(new_neighbors)}
    else:
        # HP eteint : on retire l'entree (et on garde owner=None implicitement)
        call.pop("hp", None)

    # Notifier les nouveaux voisins
    for nb in arrives:
        await _send_to_name(nb, {
            "type": "phone_hp_active",
            "call_id": call_id,
            "owner": owner_name,
            "peer": peer,
        })

    # Notifier les voisins partis
    for nb in partis:
        await _send_to_name(nb, {
            "type": "phone_hp_inactive",
            "call_id": call_id,
            "owner": owner_name,
            "peer": peer,
        })

    # Notifier le peer avec la liste complete mise a jour
    await _send_to_name(peer, {
        "type": "phone_hp_neighbors_update",
        "call_id": call_id,
        "owner": owner_name,
        "neighbors": sorted(new_neighbors),
    })

    _log(f"[PHONE] HP {owner_name} (peer={peer}) : "
         f"+{len(arrives)} -{len(partis)} (total {len(new_neighbors)})",
         PURPLE)


async def _ring_timeout(call_id: str):
    """Timer de sonnerie : attend PHONE_RING_TIMEOUT_S secondes ; si
    l'appel est toujours en 'ringing' a l'expiration, il devient un
    appel non abouti. On notifie les deux parties (phone_call_missed)
    puis on purge la table.

    Annule par _phone_clear_call() des que l'appel est decroche, refuse
    ou raccroche -> dans ce cas la CancelledError sort proprement."""
    try:
        await asyncio.sleep(PHONE_RING_TIMEOUT_S)
    except asyncio.CancelledError:
        # Appel decroche / refuse / raccroche avant la fin : rien a faire.
        return
    call = active_calls.get(call_id)
    if call is None or call["state"] != "ringing":
        return
    caller, callee = call["caller"], call["callee"]
    _log(f"[PHONE] Appel non abouti : {caller} -> {callee} "
         f"(pas de reponse en {int(PHONE_RING_TIMEOUT_S)}s)", ORANGE)
    _phone_log_event("missed", call_id, caller=caller, callee=callee)
    # Cleanup centralise (idempotent, gere aussi le HP - mais pas de HP
    # en ringing, donc no-op pour cette partie). On utilise la version
    # avec helper plutot que pop direct pour la coherence avec les
    # autres chemins de sortie d'un appel.
    _phone_clear_call(call_id)
    missed_msg = {"type": "phone_call_missed", "call_id": call_id,
                  "caller": caller, "callee": callee}
    # Cote appelant : "appel non abouti" ; cote appele : stoppe la sonnerie.
    await _send_to_name(caller, missed_msg)
    await _send_to_name(callee, missed_msg)


async def _phone_drop_calls_for(name: str, reason: str):
    """Coupe tous les appels (ringing ou active) impliquant ce joueur.
    Appele quand le joueur se deconnecte ou timeout : l'autre partie
    recoit phone_call_ended pour revenir a l'etat repos.
    reason : 'peer_disconnect' (WS ferme) ou 'peer_timeout' (silence)."""
    # Copie de la liste : on modifie active_calls pendant l'iteration.
    concerned = [
        (cid, call) for cid, call in list(active_calls.items())
        if call["caller"] == name or call["callee"] == name
    ]
    for call_id, call in concerned:
        caller, callee = call["caller"], call["callee"]
        # L'autre partie = celle qui n'est pas 'name'.
        other = callee if caller == name else caller
        _log(f"[PHONE] Appel coupe ({reason}) : {caller} <-> {callee} "
             f"(declencheur : {name})", ORANGE)
        _phone_log_event("ended", call_id, caller=caller, callee=callee,
                         reason=reason, trigger=name)
        _phone_clear_call(call_id)
        await _send_to_name(other, {
            "type": "phone_call_ended",
            "call_id": call_id,
            "reason": reason,
        })


# ─────────────────────────────────────────────
#  Session admin (port 8888 + auth_admin)
# ─────────────────────────────────────────────

async def _admin_session(ws):
    """Boucle de session admin (apres auth_admin reussi)."""
    admins[ws] = {"connected_at": time.time()}
    _log(f"ADMIN : connexion ({len(admins)} admin(s) connecte(s))", GREEN)
    try:
        # Welcome admin : etat complet
        players_state = []
        for w, info in clients.items():
            players_state.append({
                "name": info["name"],
                "pos": info.get("pos"),
                "channel": info.get("channel"),
                "profile": info.get("assigned_profile"),
                "helmet_on": info.get("helmet_on", False),
                "prox_short": info.get("prox_short", False),
                "sc_online": info.get("sc_online", True),
            })
        # [P5] On NE pousse PLUS le token serveur dans admin_welcome.
        # L'admin peut le lire via la commande get_server_token (a ajouter
        # explicitement si besoin) ou directement dans l'UI serveur.
        await ws.send(json.dumps({
            "type": "admin_welcome",
            "channels": list(_channels),
            # Envoie les dicts complets aux admins (avec permissions).
            # Les clients normaux recoivent juste les noms via welcome.
            "profiles": [dict(p) for p in _profiles if isinstance(p, dict)],
            "broadcasters": sorted(_broadcasters),
            "players": players_state,
            "anonymous_mode": _anonymous_mode,
            # "server_token" volontairement retire pour ne pas l'exposer
            # dans tous les push admin.
        }))

        # Boucle commandes
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            cmd = data.get("cmd")
            req_id = data.get("req_id")  # optionnel, echo dans la reponse
            ok, reason = await _admin_handle_cmd(ws, cmd, data)
            try:
                await ws.send(json.dumps({
                    "type": "admin_response",
                    "req_id": req_id,
                    "cmd": cmd,
                    "ok": ok,
                    "reason": reason,
                }))
            except Exception:
                pass

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        admins.pop(ws, None)
        _log(f"ADMIN : deconnexion ({len(admins)} admin(s) connecte(s))", ORANGE)


async def _admin_handle_cmd(ws, cmd: str, data: dict) -> tuple:
    """Dispatch une commande admin. Retourne (ok: bool, reason: str)."""
    try:
        if cmd == "add_channel":
            ok = add_channel(data.get("name", ""))
            return (ok, "" if ok else "nom invalide ou deja existant")
        if cmd == "rename_channel":
            ok = rename_channel(data.get("old", ""), data.get("new", ""))
            return (ok, "" if ok else "renommage refuse")
        if cmd == "remove_channel":
            ok = remove_channel(data.get("name", ""))
            return (ok, "" if ok else "canal introuvable")
        if cmd == "add_profile":
            ok = add_profile(data.get("name", ""))
            return (ok, "" if ok else "nom invalide ou deja existant")
        if cmd == "rename_profile":
            ok = rename_profile(data.get("old", ""), data.get("new", ""))
            return (ok, "" if ok else "renommage refuse")
        if cmd == "remove_profile":
            ok = remove_profile(data.get("name", ""))
            return (ok, "" if ok else "profil introuvable")
        if cmd == "assign_profile":
            ok = assign_profile(data.get("player", ""),
                                data.get("profile"))
            return (ok, "" if ok else "joueur introuvable ou profil invalide")
        if cmd == "set_profile_permission":
            # v0.2 alpha 035 : admin modifie une permission d'un profil.
            # Payload : {profile: str, perm_key: str, value: bool}.
            # Verifie que la perm est connue, met a jour, sauve,
            # broadcast aux admins (profiles_list complet) et push
            # my_profile aux clients qui ont ce profil (pour effet
            # immediat cote UI).
            profile_name = (data.get("profile") or "").strip()
            perm_key     = (data.get("perm_key") or "").strip()
            value        = bool(data.get("value", False))
            if perm_key not in _PROFILE_PERM_DEFAULTS:
                return (False, f"permission inconnue : {perm_key}")
            p = _profile_find(profile_name)
            if p is None:
                return (False, "profil introuvable")
            p[perm_key] = value
            _save_profiles()
            _log(
                f"Profil '{profile_name}' : {perm_key} = {value}",
                BLUE
            )
            # Broadcast profiles_list (admins recoivent full dict, clients
            # juste les noms qu'ils avaient deja)
            _broadcast_profiles_list_threadsafe()
            # Push my_profile aux clients qui ont ce profil assigne, pour
            # qu'ils mettent leur UI a jour immediatement (afficher/cacher
            # le bouton soundboard sans attendre une reconnexion).
            async def _push_my_profile_updates():
                for ws_, info_ in list(clients.items()):
                    if info_.get("assigned_profile") == profile_name:
                        try:
                            await ws_.send(json.dumps(
                                _build_my_profile_msg(profile_name)
                            ))
                        except Exception:
                            pass
            try:
                await _push_my_profile_updates()
            except Exception:
                pass
            return (True, "")
        if cmd == "grant_broadcaster":
            ok, reason = await grant_broadcaster(data.get("name", ""))
            return (ok, reason)
        if cmd == "revoke_broadcaster":
            ok, reason = await revoke_broadcaster(data.get("name", ""))
            return (ok, reason)
        if cmd == "list_broadcasters":
            try:
                await ws.send(json.dumps({
                    "type": "admin_response",
                    "cmd": "list_broadcasters",
                    "ok": True,
                    "broadcasters": list_broadcasters(),
                }))
            except Exception:
                pass
            return (True, "")
        if cmd == "set_anonymous_mode":
            target = bool(data.get("active", False))
            global _anonymous_mode
            if _anonymous_mode != target:
                toggle_anonymous_mode()
            return (True, "")
        if cmd == "kick_player":
            pname = data.get("name", "")
            target_ws = None
            for w, info in clients.items():
                if info.get("name") == pname:
                    target_ws = w
                    break
            if target_ws is None:
                return (False, "joueur introuvable")
            try:
                await target_ws.close(code=1008, reason="kicked_by_admin")
            except Exception:
                pass
            _log(f"ADMIN : kick {pname}", ORANGE)
            return (True, "")
        if cmd == "set_server_token":
            new_t = data.get("token", "").strip()
            if not new_t:
                return (False, "token vide")
            global SERVER_TOKEN
            try:
                set_password(new_t)
                SERVER_TOKEN = new_t
                # [P5] On log uniquement le fait qu'il a change, pas la valeur.
                _log(f"ADMIN : token serveur change ({_masked(new_t)})", BLUE)
                return (True, "")
            except Exception as e:
                return (False, f"echec : {e}")
        if cmd == "set_admin_token":
            new_t = data.get("token", "").strip()
            if not new_t:
                return (False, "token vide")
            ok = _save_admin_token(new_t)
            if ok:
                _log(f"ADMIN : token admin change ({_masked(new_t)})", BLUE)
            return (ok, "" if ok else "echec sauvegarde")
        return (False, f"commande inconnue : {cmd}")
    except Exception as e:
        return (False, f"exception : {e}")


async def handler(ws):
    name = "?"

    # [P3] Recuperer l'IP pour le suivi des echecs d'auth et le ban.
    peer_ip = "?"
    try:
        peer_ip = ws.remote_address[0] if ws.remote_address else "?"
    except Exception:
        pass
    # Refus immediat si l'IP est dans le ban-list
    if _check_auth_ban(peer_ip):
        _log(f"REFUSE : IP {peer_ip} bannie temporairement", ORANGE)
        try:
            await ws.close(code=1008, reason="banned")
        except Exception:
            pass
        return

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type")

            # [P5 - rate limiting] Pour les clients DEJA authentifies, on
            # applique un quota de messages. Le tout premier message
            # (join/auth_admin) n'est pas limite ici : il est deja couvert
            # par le lockout brute-force par IP plus bas.
            # === Stats debug crackling (25/05/2026) ===
            # Avant : drop silencieux. Maintenant : compteur + log "premier
            # drop" avec throttle 30s par client. Permet de detecter qu'un
            # client floode et que le serveur jette ses messages (typique
            # OCR a haute frequence si client mal configure).
            if ws in clients:
                if not _msg_rate.allow(ws):
                    # Quota depasse : on jette ce message. On ne FERME PAS
                    # la connexion - un pic ponctuel ne doit pas kicker un
                    # joueur legitime, juste ignorer le surplus.
                    client_name = clients.get(ws, {}).get("name", "?")
                    _pos_rate_limit_drops_by_name[client_name] = (
                        _pos_rate_limit_drops_by_name.get(client_name, 0) + 1
                    )
                    now_log = time.monotonic()
                    last_logged = _pos_rate_limit_first_drop_logged.get(
                        client_name, 0.0
                    )
                    if (now_log - last_logged) >= 30.0:
                        _debug_log(
                            f"[RATE LIMIT POS] message jete de {client_name!r} "
                            f"(total drops: "
                            f"{_pos_rate_limit_drops_by_name[client_name]}, "
                            f"rate={_msg_rate.rate}, burst={_msg_rate.burst})"
                        )
                        _pos_rate_limit_first_drop_logged[client_name] = now_log
                    continue

            # Premier message : on distingue connexion admin vs joueur.
            if msg_type == "auth_admin":
                token = data.get("token", "")
                # [P1] compare_digest = comparaison en temps constant.
                if not secrets.compare_digest(token, ADMIN_TOKEN):
                    _record_auth_failure(peer_ip)
                    _log(f"REFUSE admin : token invalide (ip: {peer_ip})", RED)
                    try:
                        await ws.send(json.dumps({
                            "type": "error",
                            "reason": "invalid_admin_token",
                            "message": "Token admin invalide"
                        }))
                    except Exception:
                        pass
                    await ws.close(code=1008, reason="invalid_admin_token")
                    return
                _record_auth_success(peer_ip)
                # Auth OK : delegue a la boucle admin et termine le handler
                # apres deconnexion (l'admin n'est pas un joueur).
                await _admin_session(ws)
                return

            if msg_type == "join":
                # [P1] Verifier le token en temps constant
                token = data.get("token", "")
                if not secrets.compare_digest(token, SERVER_TOKEN):
                    _record_auth_failure(peer_ip)
                    _log(f"REFUSE : token invalide "
                         f"(client: {data.get('name', '?')}, ip: {peer_ip})", RED)
                    try:
                        await ws.send(json.dumps({
                            "type": "error",
                            "reason": "invalid_token",
                            "message": "Token invalide"
                        }))
                    except Exception:
                        pass
                    await ws.close(code=1008, reason="invalid_token")
                    return

                # [P2] Cap du nombre de clients. On refuse avec un code
                # WebSocket explicite si le serveur est plein.
                if len(clients) >= MAX_CLIENTS:
                    _log(f"REFUSE : serveur plein ({MAX_CLIENTS} clients) - "
                         f"ip {peer_ip}", ORANGE)
                    try:
                        await ws.send(json.dumps({
                            "type": "error",
                            "reason": "server_full",
                            "message": "Serveur plein"
                        }))
                    except Exception:
                        pass
                    await ws.close(code=1013, reason="server_full")
                    return

                _record_auth_success(peer_ip)

                name = data.get("name", f"Player_{len(clients)+1}")

                # [UNIQUE NAME] Refuse si un client deja connecte porte le
                # meme nom. Sans cela, n'importe qui peut se faire passer
                # pour un autre joueur dans le chat / les canaux. Combine
                # avec la verification du broadcaster_token, cela ferme
                # aussi le scenario "deconnecte, reconnecte sous le nom
                # d'un broadcaster connu pour usurper le role".
                if _find_client_ws_by_name(name) is not None:
                    _log(f"REFUSE : nom deja utilise '{name}' (ip {peer_ip})", ORANGE)
                    try:
                        await ws.send(json.dumps({
                            "type": "error",
                            "reason": "name_in_use",
                            "message": "Ce nom est deja utilise sur ce serveur",
                        }))
                    except Exception:
                        pass
                    await ws.close(code=1008, reason="name_in_use")
                    return

                # [BROADCASTER AUTH] Si le nom figure dans _broadcasters, le
                # client DOIT presenter le broadcaster_token correct. Cela
                # protege a la fois :
                #   - l'octroi du role (sans token, can_broadcast=False)
                #   - l'identite (un non-broadcaster ne peut pas usurper le
                #     nom d'un broadcaster connu)
                # Distinction stricte entre les deux cas n'apporte rien :
                # dans les deux cas le join est refuse, le client clarifie
                # son setup et ressaie.
                client_bcast_token = data.get("broadcaster_token", "")
                if name in _broadcasters:
                    if not _verify_broadcaster_token(name, client_bcast_token):
                        _log(f"REFUSE : broadcaster_token invalide pour '{name}' "
                             f"(ip {peer_ip})", RED)
                        try:
                            await ws.send(json.dumps({
                                "type": "error",
                                "reason": "broadcaster_token_invalid",
                                "message": "Ce nom est reserve a un broadcaster ; "
                                           "broadcaster_token manquant ou invalide",
                            }))
                        except Exception:
                            pass
                        await ws.close(code=1008, reason="broadcaster_token_invalid")
                        return

                # [P4 - auth partagee] Genere un ticket court pour ce
                # joueur. Le client le recevra dans le welcome et devra le
                # presenter au serveur audio. Sans ce ticket, le serveur
                # audio refusera la connexion.
                audio_ticket = secrets.token_hex(16)

                # Canal initial : celui demande par le client si valide, sinon None.
                requested_ch = data.get("channel")
                if requested_ch in _channels:
                    initial_channel = requested_ch
                else:
                    initial_channel = None
                clients[ws] = {
                    "name": name,
                    "pos": None,
                    "last_seen": time.time(),
                    "helmet_on": False,
                    "channel": initial_channel,
                    "assigned_profile": None,
                    "prox_short": False,
                    "audio_ticket": audio_ticket,
                }
                # [P4] Enregistre le ticket dans le fichier partage avec
                # le serveur audio. Le serveur audio relira ce fichier au
                # moment du join audio pour valider le ticket presente.
                # can_broadcast est lu cote audio pour autoriser les frames
                # avec flag 0x04 (PTT diffusion globale) ; n'est True que
                # si le nom est broadcaster ET que le token a ete verifie
                # ci-dessus. Si le role est
                # revoque pendant que le joueur est connecte, la revocation
                # ne s'applique qu'au prochain ticket (TTL <= 120s).
                _auth_registry.issue(
                    name,
                    audio_ticket,
                    can_broadcast=(name in _broadcasters),
                )
                _log(f"JOIN : {name}  ({len(clients)} connecté(s))", GREEN)
                if _ui:
                    _ui.add_player(name)

                # Envoyer la liste complete des autres joueurs.
                existing = [
                    {
                        "name": info["name"],
                        "pos": info["pos"],
                        "helmet_on": info.get("helmet_on", False),
                        "channel": info.get("channel"),
                        "profile": info.get("assigned_profile"),
                        "prox_short": info.get("prox_short", False),
                    }
                    for other_ws, info in clients.items()
                    if other_ws is not ws
                ]
                # Au welcome, le joueur n'a pas encore de profil assigne
                # (None). Les permissions sont donc toutes a False. C'est
                # l'admin qui assignera un profil ensuite et qui pushera
                # my_profile separement.
                welcome_msg = {
                    "type": "welcome",
                    "players": existing,
                    "anonymous_mode": _anonymous_mode,
                    "channels": list(_channels),
                    "profiles": _profile_names(),
                    "my_channel": initial_channel,
                    "my_profile": None,
                    # [P4] Ticket a renvoyer au serveur audio lors du join audio.
                    "audio_ticket": audio_ticket,
                    # Capabilities serveur. Permet au client de detecter
                    # qu'il parle a un serveur recent et d'activer/desactiver
                    # les fonctionnalites correspondantes (ex: griser la
                    # touche PTT diffusion globale sur les vieux serveurs).
                    "server_caps": ["broadcast_all"],
                    # Indique si CE joueur a actuellement le role broadcaster.
                    # Le client peut ainsi activer/desactiver son UI sans avoir
                    # a interroger un admin.
                    "is_broadcaster": (name in _broadcasters),
                }
                # Ajout des permissions a False (cf. _build_my_profile_msg
                # qui renvoie False pour profile=None).
                for perm_key in _PROFILE_PERM_DEFAULTS.keys():
                    welcome_msg[perm_key] = False
                await ws.send(json.dumps(welcome_msg))
                # Annoncer aux autres l'arrivee du nouveau
                await _broadcast(ws, json.dumps({
                    "type": "join",
                    "name": name,
                    "channel": initial_channel,
                    "profile": None,
                    "prox_short": False,
                }))

            elif msg_type == "pos":
                if ws not in clients:
                    continue
                pos = data.get("pos")
                # [P4] Validation typee : rejette les payloads malformes
                # (x non-numerique, NaN, Inf, etc.) qui faisaient crasher
                # l'UI dans pos.get("x", 0) / 1000.
                if not _validate_pos(pos):
                    continue
                ts_capture = data.get("ts_capture")
                clients[ws]["pos"] = pos
                clients[ws]["last_seen"] = time.time()
                _debug_log_pos(clients[ws]["name"], pos, ts_capture)
                if _ui:
                    _ui.update_player(clients[ws]["name"], pos)
                await _broadcast(ws, json.dumps({
                    "type": "pos",
                    "name": clients[ws]["name"],
                    "pos": pos,
                    "ts_capture": ts_capture,
                }))

            elif msg_type == "ping":
                if ws in clients:
                    clients[ws]["last_seen"] = time.time()
                await ws.send(json.dumps({"type": "pong"}))

            elif msg_type == "sc_offline":
                if ws in clients:
                    clients[ws]["sc_online"] = False
                    _log(f"{clients[ws]['name']} : SC ferme (OCR inactif)", ORANGE)
                    await _broadcast(ws, json.dumps({
                        "type": "sc_offline",
                        "name": clients[ws]["name"]
                    }))

            elif msg_type == "sc_online":
                if ws in clients:
                    clients[ws]["sc_online"] = True
                    _log(f"{clients[ws]['name']} : SC actif (OCR ok)", GREEN)
                    await _broadcast(ws, json.dumps({
                        "type": "sc_online",
                        "name": clients[ws]["name"]
                    }))

            elif msg_type == "helmet":
                if ws in clients:
                    helmet_on = bool(data.get("helmet_on", False))
                    clients[ws]["helmet_on"] = helmet_on
                    cname = clients[ws]["name"]
                    status = "ON" if helmet_on else "OFF"
                    _log(f"{cname} : casque {status}", BLUE)
                    await _broadcast(ws, json.dumps({
                        "type": "helmet",
                        "name": cname,
                        "helmet_on": helmet_on,
                    }))

            elif msg_type == "set_channel":
                if ws in clients:
                    new_ch = data.get("channel")
                    if new_ch is None or new_ch in _channels:
                        clients[ws]["channel"] = new_ch
                        cname = clients[ws]["name"]
                        _log(f"{cname} : canal -> {new_ch or '(aucun)'}", BLUE)
                        await _broadcast_all(json.dumps({
                            "type": "player_channel",
                            "name": cname,
                            "channel": new_ch,
                        }))
                        if _ui:
                            _ui.refresh_player_channel(cname)

            elif msg_type == "prox_short":
                if ws in clients:
                    active = bool(data.get("active", False))
                    clients[ws]["prox_short"] = active
                    cname = clients[ws]["name"]
                    _log(f"{cname} : proximity_short -> {'5m' if active else '30m'}", BLUE)
                    await _broadcast_all(json.dumps({
                        "type": "player_prox_short",
                        "name": cname,
                        "active": active,
                    }))

            elif msg_type == "soundboard_play":
                # v0.2 alpha 029/035 : un client declenche un son du
                # soundboard. Verification de la permission profil
                # `soundboard_allowed` (alpha 035) :
                #   - Si le joueur n'a pas de profil assigne -> refus.
                #   - Si son profil n'a pas la perm soundboard_allowed
                #     -> refus.
                # En cas de refus : ignore silencieusement + log cote
                # serveur. On ne renvoie pas d'erreur au client (pas la
                # peine d'alourdir le protocole, le client n'aurait pas
                # du afficher le bouton de toutes facons).
                if ws in clients:
                    sound_id  = data.get("sound_id")
                    cname     = clients[ws]["name"]
                    cchan     = clients[ws].get("channel")
                    cprofile  = clients[ws].get("assigned_profile")
                    # Verifier permission
                    if not cprofile:
                        _log(
                            f"{cname} : soundboard_play '{sound_id}' "
                            f"REFUSE (pas de profil assigne)",
                            ORANGE
                        )
                    elif not _profile_has_perm(cprofile, "soundboard_allowed"):
                        _log(
                            f"{cname} : soundboard_play '{sound_id}' "
                            f"REFUSE (profil '{cprofile}' n'a pas la perm "
                            f"soundboard_allowed)",
                            ORANGE
                        )
                    elif isinstance(sound_id, str) and sound_id:
                        _log(
                            f"{cname} : soundboard_play '{sound_id}' "
                            f"canal={cchan or '(aucun)'}",
                            BLUE
                        )
                        await _broadcast_channel(cchan, json.dumps({
                            "type":     "soundboard_play",
                            "name":     cname,
                            "sound_id": sound_id,
                        }))

            # ─────────────────────────────────────────────
            #  CircusPhone (Feature 4, D1) : cycle de vie d'appel
            # ─────────────────────────────────────────────
            elif msg_type == "phone_call_request":
                # L'appelant veut joindre 'target' (pseudo).
                if ws in clients:
                    caller = clients[ws]["name"]
                    target = data.get("target")

                    # Garde-fous basiques. Le client ne devrait pas
                    # envoyer ces requetes invalides, mais le serveur
                    # reste autoritaire et ne fait jamais confiance.
                    if not isinstance(target, str) or not target:
                        _log(f"[PHONE] {caller} : phone_call_request "
                             f"ignore (target invalide)", ORANGE)
                    elif target == caller:
                        # S'appeler soi-meme : on ignore.
                        _log(f"[PHONE] {caller} : phone_call_request "
                             f"ignore (auto-appel)", ORANGE)
                    elif _find_active_call_for(caller) is not None:
                        # L'appelant est deja dans un appel : on ignore
                        # (le client n'aurait pas du proposer le bouton).
                        _log(f"[PHONE] {caller} : phone_call_request "
                             f"ignore (deja en appel)", ORANGE)
                    elif _find_ws_by_name(target) is None:
                        # Cible hors ligne -> 'occupe' (le client D1 ne
                        # distingue pas hors-ligne et occupe ; un seul
                        # retour suffit pour revenir au repos).
                        _log(f"[PHONE] {caller} -> {target} : cible hors "
                             f"ligne", ORANGE)
                        _phone_log_event("busy", call_id=None,
                                         caller=caller, callee=target,
                                         cause="offline")
                        await ws.send(json.dumps({
                            "type": "phone_call_busy",
                            "target": target,
                            "cause": "offline",
                        }))
                    elif _find_active_call_for(target) is not None:
                        # Cible deja en appel -> 'occupe'.
                        _log(f"[PHONE] {caller} -> {target} : occupe "
                             f"(deja en appel)", ORANGE)
                        _phone_log_event("busy", call_id=None,
                                         caller=caller, callee=target,
                                         cause="in_call")
                        await ws.send(json.dumps({
                            "type": "phone_call_busy",
                            "target": target,
                            "cause": "in_call",
                        }))
                    else:
                        # OK : on cree l'appel et on lance la sonnerie.
                        call_id = secrets.token_hex(4)
                        ring_task = asyncio.create_task(
                            _ring_timeout(call_id)
                        )
                        active_calls[call_id] = {
                            "caller":      caller,
                            "callee":      target,
                            "state":       "ringing",
                            "created_at":  time.time(),
                            "accepted_at": None,
                            "ring_task":   ring_task,
                        }
                        _log(f"[PHONE] Appel : {caller} -> {target} "
                             f"(call_id={call_id})", PURPLE)
                        _phone_log_event("request", call_id,
                                         caller=caller, callee=target)
                        # Accuse de reception a l'appelant (ca sonne).
                        await ws.send(json.dumps({
                            "type": "phone_call_ringing",
                            "call_id": call_id,
                            "target": target,
                        }))
                        # Notification d'appel entrant a l'appele.
                        await _send_to_name(target, {
                            "type": "phone_call_incoming",
                            "call_id": call_id,
                            "caller": caller,
                        })

            elif msg_type == "phone_call_accept":
                # L'appele decroche.
                if ws in clients:
                    cname = clients[ws]["name"]
                    call_id = data.get("call_id")
                    call = active_calls.get(call_id)
                    if call is None:
                        _log(f"[PHONE] {cname} : phone_call_accept "
                             f"ignore (call_id inconnu : {call_id})", ORANGE)
                    elif call["callee"] != cname:
                        # Seul l'appele peut decrocher.
                        _log(f"[PHONE] {cname} : phone_call_accept "
                             f"REFUSE (pas l'appele de {call_id})", ORANGE)
                    elif call["state"] != "ringing":
                        # Deja decroche / etat incoherent : on ignore.
                        _log(f"[PHONE] {cname} : phone_call_accept "
                             f"ignore (etat={call['state']})", ORANGE)
                    else:
                        # Decroche valide : on annule le timer de sonnerie
                        # et on passe l'appel en 'active'.
                        rt = call.get("ring_task")
                        if rt is not None and not rt.done():
                            rt.cancel()
                        call["ring_task"] = None
                        call["state"] = "active"
                        call["accepted_at"] = time.time()
                        caller, callee = call["caller"], call["callee"]
                        _log(f"[PHONE] Decroche : {caller} <-> {callee} "
                             f"(call_id={call_id})", GREEN)
                        _phone_log_event("accept", call_id,
                                         caller=caller, callee=callee)
                        # Les DEUX parties recoivent phone_call_accepted :
                        # l'appelant passe de 'sonne' a 'en appel',
                        # l'appele confirme son propre passage en appel.
                        accepted_msg = {
                            "type": "phone_call_accepted",
                            "call_id": call_id,
                            "caller": caller,
                            "callee": callee,
                        }
                        await _send_to_name(caller, accepted_msg)
                        await _send_to_name(callee, accepted_msg)

            elif msg_type == "phone_call_decline":
                # L'appele refuse l'appel entrant.
                if ws in clients:
                    cname = clients[ws]["name"]
                    call_id = data.get("call_id")
                    call = active_calls.get(call_id)
                    if call is None:
                        _log(f"[PHONE] {cname} : phone_call_decline "
                             f"ignore (call_id inconnu : {call_id})", ORANGE)
                    elif call["callee"] != cname:
                        _log(f"[PHONE] {cname} : phone_call_decline "
                             f"REFUSE (pas l'appele de {call_id})", ORANGE)
                    elif call["state"] != "ringing":
                        # On ne peut refuser qu'un appel qui sonne encore.
                        _log(f"[PHONE] {cname} : phone_call_decline "
                             f"ignore (etat={call['state']})", ORANGE)
                    else:
                        caller, callee = call["caller"], call["callee"]
                        _log(f"[PHONE] Refuse : {callee} a refuse l'appel "
                             f"de {caller} (call_id={call_id})", ORANGE)
                        _phone_log_event("decline", call_id,
                                         caller=caller, callee=callee)
                        _phone_clear_call(call_id)
                        # Seul l'appelant a besoin d'etre notifie (l'appele
                        # sait qu'il vient de refuser).
                        await _send_to_name(caller, {
                            "type": "phone_call_declined",
                            "call_id": call_id,
                        })

            elif msg_type == "phone_call_hangup":
                # Une des deux parties raccroche. Valable pendant la
                # sonnerie (l'appelant annule) OU pendant l'appel actif
                # (l'un des deux met fin a la conversation).
                if ws in clients:
                    cname = clients[ws]["name"]
                    call_id = data.get("call_id")
                    call = active_calls.get(call_id)
                    if call is None:
                        _log(f"[PHONE] {cname} : phone_call_hangup "
                             f"ignore (call_id inconnu : {call_id})", ORANGE)
                    elif cname not in (call["caller"], call["callee"]):
                        # Le raccrocheur doit faire partie de l'appel.
                        _log(f"[PHONE] {cname} : phone_call_hangup "
                             f"REFUSE (pas partie de {call_id})", ORANGE)
                    else:
                        caller, callee = call["caller"], call["callee"]
                        other = callee if cname == caller else caller
                        _log(f"[PHONE] Raccroche par {cname} : "
                             f"{caller} <-> {callee} (call_id={call_id}, "
                             f"etat={call['state']})", ORANGE)
                        _phone_log_event("hangup", call_id,
                                         caller=caller, callee=callee,
                                         by=cname, prev_state=call["state"])
                        _phone_clear_call(call_id)
                        # L'autre partie est notifiee de la fin d'appel.
                        await _send_to_name(other, {
                            "type": "phone_call_ended",
                            "call_id": call_id,
                            "reason": "hangup",
                        })

            elif msg_type == "phone_speaker_state":
                # D4b : un joueur en appel active/maintient/desactive son HP.
                # Charge utile : {call_id, neighbors: [pseudo1, pseudo2, ...]}
                # neighbors = liste vide -> HP eteint (ou raccroche).
                # Le serveur calcule la diff avec l'etat precedent et notifie
                # les voisins concernes + le peer. Voir _phone_hp_apply_state.
                if ws in clients:
                    cname = clients[ws]["name"]
                    call_id = data.get("call_id")
                    raw_neighbors = data.get("neighbors") or []
                    # Filtrer : que des chaines non vides
                    new_neighbors = {
                        n for n in raw_neighbors
                        if isinstance(n, str) and n
                    }
                    await _phone_hp_apply_state(call_id, cname, new_neighbors)

            # ─────────────────────────────────────────────
            #  CircusPhone (Feature 4, D4 etape 3) : messagerie privee
            # ─────────────────────────────────────────────
            elif msg_type == "phone_message_send":
                # Un joueur veut envoyer un MP texte a un autre. Le serveur
                # se contente de router : aucun stockage cote serveur, le
                # stockage est local cote client (10 envoyes + 10 recus par
                # contact, max 500c par message). Si le destinataire est
                # hors ligne, le message est perdu (la spec ne prevoit pas
                # de boite differee). Aucun controle de profil : la
                # messagerie est universelle.
                if ws in clients:
                    sender = clients[ws]["name"]
                    target = data.get("target")
                    body   = data.get("body")
                    # Garde-fous : champs valides + longueur + auto-msg.
                    if not isinstance(target, str) or not target:
                        _log(f"[PHONE-MSG] {sender} : ignore "
                             f"(target invalide)", ORANGE)
                    elif target == sender:
                        _log(f"[PHONE-MSG] {sender} : ignore (auto-msg)",
                             ORANGE)
                    elif not isinstance(body, str) or not body:
                        _log(f"[PHONE-MSG] {sender} : ignore (body vide)",
                             ORANGE)
                    elif len(body) > 500:
                        # Le serveur valide la limite cote serveur aussi
                        # (defense en profondeur, le client la valide deja).
                        _log(f"[PHONE-MSG] {sender} -> {target} : ignore "
                             f"(body > 500 char : {len(body)})", ORANGE)
                    elif _find_ws_by_name(target) is None:
                        _log(f"[PHONE-MSG] {sender} -> {target} : cible "
                             f"hors ligne, message perdu", ORANGE)
                        _phone_log_event("msg_lost", call_id=None,
                                         sender=sender, target=target,
                                         len=len(body))
                    else:
                        # Route au destinataire. Timestamp serveur (source
                        # de verite : evite les triches d'horloge client).
                        ts = time.time()
                        _log(f"[PHONE-MSG] {sender} -> {target} ({len(body)} "
                             f"char)", PURPLE)
                        _phone_log_event("msg_sent", call_id=None,
                                         sender=sender, target=target,
                                         len=len(body))
                        await _send_to_name(target, {
                            "type":   "phone_message_received",
                            "sender": sender,
                            "body":   body,
                            "ts":     ts,
                        })

            # ─────────────────────────────────────────────
            #  [D5] Photos de profil : upload + request
            # ─────────────────────────────────────────────
            elif msg_type == "profile_photo_upload":
                # Un joueur envoie sa photo de profil (JPEG compresse cote
                # client). Champs attendus :
                #   - data_b64 : str, base64 du JPEG
                #   - hash     : str, SHA-256 hex du JPEG (calcule par le
                #                client, sera renvoye aux pairs pour le
                #                if-none-match). Pas de validation forte
                #                cote serveur : on stocke ce qu'on recoit.
                # Aucune reponse particuliere n'est renvoyee : si l'upload
                # passe les garde-fous, c'est qu'il est OK. Le client n'a
                # pas besoin de confirmation pour fonctionner. En cas de
                # rejet, on log et on ignore.
                if ws in clients:
                    sender = clients[ws]["name"]
                    data_b64 = data.get("data_b64")
                    new_hash = data.get("hash")
                    if not _is_safe_pseudo_for_file(sender):
                        # Ne devrait pas arriver (pseudo deja filtre au join),
                        # garde-fou supplementaire.
                        _log(f"[PROFILE] {sender} : upload ignore (pseudo "
                             f"non sur pour fichier)", ORANGE)
                    elif (not isinstance(data_b64, str)
                          or not isinstance(new_hash, str)
                          or not data_b64 or not new_hash):
                        _log(f"[PROFILE] {sender} : upload ignore (champs "
                             f"manquants ou invalides)", ORANGE)
                    else:
                        # Cap defensif sur la taille base64 avant decodage.
                        # 200 Ko bytes => ~270 Ko base64. On tolere 280 Ko.
                        if len(data_b64) > 280_000:
                            _log(f"[PROFILE] {sender} : upload rejete "
                                 f"(base64 trop volumineux : {len(data_b64)} "
                                 f"chars)", ORANGE)
                        else:
                            try:
                                import base64 as _b64
                                jpeg_bytes = _b64.b64decode(
                                    data_b64, validate=True
                                )
                            except Exception as e:
                                jpeg_bytes = None
                                _log(f"[PROFILE] {sender} : upload rejete "
                                     f"(base64 invalide : {e})", ORANGE)
                            if jpeg_bytes is not None:
                                if len(jpeg_bytes) > _PROFILE_PHOTO_MAX_BYTES:
                                    _log(f"[PROFILE] {sender} : upload "
                                         f"rejete (JPEG > {_PROFILE_PHOTO_MAX_BYTES} "
                                         f"bytes : {len(jpeg_bytes)})", ORANGE)
                                else:
                                    # Sanity check : signature JPEG (FFD8FF).
                                    if (len(jpeg_bytes) >= 3
                                            and jpeg_bytes[0] == 0xFF
                                            and jpeg_bytes[1] == 0xD8
                                            and jpeg_bytes[2] == 0xFF):
                                        try:
                                            path = _profile_photo_path(sender)
                                            with open(path, "wb") as f:
                                                f.write(jpeg_bytes)
                                            _profile_photos_index[sender] = {
                                                "hash": new_hash,
                                                "ts": time.time(),
                                            }
                                            _profile_photos_save_index()
                                            _log(f"[PROFILE] {sender} : photo "
                                                 f"mise a jour "
                                                 f"({len(jpeg_bytes)} bytes, "
                                                 f"hash {new_hash[:8]}...)",
                                                 PURPLE)
                                        except Exception as e:
                                            _log(f"[PROFILE] {sender} : "
                                                 f"echec ecriture disque "
                                                 f"({e})", RED)
                                    else:
                                        _log(f"[PROFILE] {sender} : upload "
                                             f"rejete (pas une signature "
                                             f"JPEG)", ORANGE)

            elif msg_type == "profile_photo_request":
                # Un client demande la photo d'un pair. Champs attendus :
                #   - target         : str, pseudo du proprietaire vise
                #   - if_none_match  : str|None, hash deja en cache cote
                #                      demandeur (pour eviter de retransmettre
                #                      si rien n'a change).
                # Reponse : profile_photo_response avec un de ces statuts :
                #   - status="none"      : aucune photo connue pour ce pseudo
                #   - status="unchanged" : meme hash, garde ton cache
                #   - status="ok"        : data_b64 + hash a jour
                if ws in clients:
                    requester     = clients[ws]["name"]
                    target        = data.get("target")
                    if_none_match = data.get("if_none_match")
                    if not isinstance(target, str) or not target:
                        # Reponse minimale "none" pour debloquer le client
                        # sans causer d'erreur.
                        await ws.send(json.dumps({
                            "type": "profile_photo_response",
                            "target": target if isinstance(target, str) else "",
                            "status": "none",
                        }))
                    elif not _is_safe_pseudo_for_file(target):
                        await ws.send(json.dumps({
                            "type": "profile_photo_response",
                            "target": target,
                            "status": "none",
                        }))
                    else:
                        entry = _profile_photos_index.get(target)
                        if not entry:
                            await ws.send(json.dumps({
                                "type": "profile_photo_response",
                                "target": target,
                                "status": "none",
                            }))
                        else:
                            stored_hash = entry.get("hash") or ""
                            if (isinstance(if_none_match, str)
                                    and if_none_match
                                    and if_none_match == stored_hash):
                                # Le demandeur a deja la bonne version.
                                await ws.send(json.dumps({
                                    "type": "profile_photo_response",
                                    "target": target,
                                    "status": "unchanged",
                                    "hash": stored_hash,
                                }))
                            else:
                                # Relire le JPEG depuis le disque. Si le
                                # fichier a disparu (cas pathologique), on
                                # nettoie l'entree et on repond "none".
                                path = _profile_photo_path(target)
                                jpeg_bytes = None
                                try:
                                    with open(path, "rb") as f:
                                        jpeg_bytes = f.read()
                                except Exception:
                                    jpeg_bytes = None
                                if jpeg_bytes is None:
                                    _profile_photos_index.pop(target, None)
                                    _profile_photos_save_index()
                                    await ws.send(json.dumps({
                                        "type": "profile_photo_response",
                                        "target": target,
                                        "status": "none",
                                    }))
                                else:
                                    import base64 as _b64
                                    data_b64 = _b64.b64encode(
                                        jpeg_bytes
                                    ).decode("ascii")
                                    await ws.send(json.dumps({
                                        "type": "profile_photo_response",
                                        "target": target,
                                        "status": "ok",
                                        "hash": stored_hash,
                                        "data_b64": data_b64,
                                    }))
                                    # Log discret (les requests sont
                                    # frequentes, on evite le bruit).
                                    _log(f"[PROFILE] {requester} <- {target} "
                                         f"({len(jpeg_bytes)} bytes)", MUTED)

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        # [P5] Libere le bucket de rate limiting (que le ws soit un joueur
        # authentifie ou non - forget est sans effet si la cle est absente).
        _msg_rate.forget(ws)
        if ws in clients:
            # [P4] Revoque le ticket audio du joueur qui part, pour qu'il
            # ne puisse plus etre utilise sur le serveur audio.
            _leaving = clients.get(ws)
            if _leaving and _leaving.get("audio_ticket"):
                _auth_registry.revoke(_leaving["audio_ticket"])
            clients.pop(ws)
            _log(f"LEAVE : {name}  ({len(clients)} connecté(s))", ORANGE)
            if _ui:
                _ui.remove_player(name)
            # CircusPhone : couper les appels en cours de ce joueur
            # AVANT d'annoncer son leave. L'autre partie recevra
            # phone_call_ended (reason=peer_disconnect).
            await _phone_drop_calls_for(name, reason="peer_disconnect")
            # D4b : si ce joueur faisait partie de listes HP d'autres appels
            # (en tant que voisin d'un owner), le retirer pour eviter que le
            # peer croie pouvoir l'entendre encore. On parcourt les calls
            # ayant un HP actif, on retire 'name' de leur set 'neighbors'
            # s'il y est, et on resynchronise via _phone_hp_apply_state.
            for cid, call in list(active_calls.items()):
                hp = call.get("hp") or {}
                neighbors = hp.get("neighbors") or set()
                if name in neighbors:
                    owner = hp.get("owner")
                    if owner:
                        new_set = set(neighbors) - {name}
                        await _phone_hp_apply_state(cid, owner, new_set)
            await _broadcast_all(json.dumps({"type": "leave", "name": name}))


_loop: asyncio.AbstractEventLoop | None = None
_stop_event: asyncio.Event | None = None

async def _server_main():
    global _stop_event, _server_running
    _stop_event = asyncio.Event()
    _server_running = True
    _debug_log_init()
    _phone_log_init()
    # [D5] Charge l'index des photos de profil (cree le dossier si absent).
    _profile_photos_load_index()
    _log(f"Serveur démarré sur port {PORT}", BLUE)
    # [P5] Les tokens ne sont plus affiches en clair dans les logs.
    # Ils restent visibles dans l'UI Tkinter (saisis par l'admin) et
    # dans les fichiers de config.
    _log(f"TOKEN JOUEUR : {_masked(SERVER_TOKEN)} "
         f"(visible complet dans l'UI)", BLUE)
    _log(f"TOKEN ADMIN  : {_masked(ADMIN_TOKEN)} "
         f"(visible dans circusvoip_admin_token.json)", PURPLE)
    _log(f"Cap clients  : {MAX_CLIENTS} simultanes", MUTED)
    _log(f"Lockout auth : {AUTH_MAX_FAILURES} echecs / {AUTH_WINDOW_SEC}s "
         f"-> ban {AUTH_BAN_SEC}s", MUTED)
    _log(f"Log debug : circusvoip_debug/{DEBUG_LOG_FILE.name}", MUTED)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        _log(f"IP locale : {ip}:{PORT}", BLUE)
        if _ui:
            _ui.set_ip(f"{ip}:{PORT}")
            _ui.set_status(True)
    except Exception:
        pass
    cleanup_task = asyncio.create_task(_cleanup_loop())

    # ─────────────────────────────────────────────
    #  [P1 - TLS] Chiffrement de la connexion (auto)
    # ─────────────────────────────────────────────
    # Le serveur ecoute en wss:// (chiffre). Au premier demarrage,
    # ensure_self_signed_cert() genere automatiquement cert.pem et key.pem
    # a cote du fichier server.py. Aux demarrages suivants, ces fichiers
    # sont reutilises.
    #
    # Le certificat est auto-signe, donc les clients ne verifient pas
    # l'identite du serveur (impossible sans CA publique). Mais la
    # connexion est CHIFFREE : un attaquant qui ecoute le reseau ne peut
    # plus lire le token ni les positions.
    #
    # En cas d'echec de generation (lib cryptography absente, disque
    # plein, droits insuffisants), le serveur REFUSE de demarrer plutot
    # que de tomber en clair (ws://) silencieusement.
    #
    # Pour utiliser un VRAI certificat valide CA (Let's Encrypt) :
    # remplace cert.pem et key.pem par les tiens dans le dossier du
    # serveur. Idealement avec un reverse proxy Caddy/nginx devant qui
    # gere les renouvellements.
    from circusvoip_security import ensure_self_signed_cert, build_ssl_context
    _cert_file = _BASE_DIR / "cert.pem"
    _key_file  = _BASE_DIR / "key.pem"
    _ok, _detail = ensure_self_signed_cert(
        _cert_file, _key_file, common_name="circusvoip-server"
    )
    if not _ok:
        _log(f"[FATAL] Impossible de generer le certificat TLS. "
             f"Detail : {_detail}", RED)
        _server_running = False
        return
    _ssl_ctx = build_ssl_context(str(_cert_file), str(_key_file))
    _log(f"TLS active : serveur en wss:// (cert {_detail})", GREEN)

    async with websockets.serve(handler, HOST, PORT, ssl=_ssl_ctx):
        await _stop_event.wait()
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    # Déconnecter tous les clients
    for ws in list(clients.keys()):
        try:
            await ws.close()
        except Exception:
            pass
    clients.clear()
    _server_running = False
    _log("Serveur arrêté", ORANGE)
    if _ui:
        _ui.set_status(False)
        _ui.clear_players()

def _run_server():
    global _loop, _server_running
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    try:
        _loop.run_until_complete(_server_main())
    finally:
        _loop.close()
        _server_running = False

def start_server():
    if not _server_running:
        threading.Thread(target=_run_server, daemon=True).start()

def stop_server():
    if _loop and _stop_event:
        _loop.call_soon_threadsafe(_stop_event.set)


def toggle_anonymous_mode():
    """Bascule le mode anonyme et notifie tous les clients connectes."""
    global _anonymous_mode
    _anonymous_mode = not _anonymous_mode
    new_state = _anonymous_mode
    if _loop and _server_running:
        async def _broadcast_anon():
            await _broadcast_all(json.dumps({
                "type": "anonymous_mode",
                "active": new_state,
            }))
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_anon(), _loop)
        except Exception:
            pass
    _log(f"Mode anonyme : {'ON' if new_state else 'OFF'}",
         BLUE if new_state else MUTED)
    return new_state


# ---- Canaux : fonctions thread-safe appelees depuis l'UI ----

def _broadcast_channels_list_threadsafe():
    """Broadcast la liste des canaux a tous les clients connectes."""
    if _loop and _server_running:
        async def _do():
            await _broadcast_all(json.dumps({
                "type": "channels_list",
                "channels": list(_channels),
            }))
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass


def add_channel(name: str) -> bool:
    """Ajoute un canal radio (libre, choisi par les clients)."""
    name = (name or "").strip()
    if not name or name in _channels:
        return False
    _channels.append(name)
    _save_channels()
    _broadcast_channels_list_threadsafe()
    _log(f"Canal ajoute : {name}", GREEN)
    return True


def rename_channel(old: str, new: str) -> bool:
    """Renomme un canal. Met a jour les clients qui etaient sur l'ancien nom."""
    new = (new or "").strip()
    if not new or old == new or new in _channels or old not in _channels:
        return False
    idx = _channels.index(old)
    _channels[idx] = new
    _save_channels()
    affected = []
    for ws, info in clients.items():
        if info.get("channel") == old:
            info["channel"] = new
            affected.append(info["name"])
    _broadcast_channels_list_threadsafe()
    if _ui:
        for n in affected:
            _ui.refresh_player_channel(n)
    if _loop and _server_running:
        async def _broadcast_player_changes():
            for info in clients.values():
                if info.get("channel") == new:
                    await _broadcast_all(json.dumps({
                        "type": "player_channel",
                        "name": info["name"],
                        "channel": new,
                    }))
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_player_changes(), _loop)
        except Exception:
            pass
    _log(f"Canal renomme : {old} -> {new}", BLUE)
    return True


def remove_channel(name: str) -> bool:
    """Supprime un canal. Les clients qui y etaient repassent a None."""
    if name not in _channels:
        return False
    _channels.remove(name)
    _save_channels()
    fallback = None
    affected = []
    for ws, info in clients.items():
        if info.get("channel") == name:
            info["channel"] = fallback
            affected.append(info["name"])
    _broadcast_channels_list_threadsafe()
    if affected and _loop and _server_running:
        async def _broadcast_reassigned():
            for n in affected:
                await _broadcast_all(json.dumps({
                    "type": "player_channel",
                    "name": n,
                    "channel": fallback,
                }))
        try:
            asyncio.run_coroutine_threadsafe(_broadcast_reassigned(), _loop)
        except Exception:
            pass
    if _ui:
        for n in affected:
            _ui.refresh_player_channel(n)
    _log(f"Canal supprime : {name} (joueurs reassignes : {len(affected)})", ORANGE)
    return True


# ---- Profils : fonctions thread-safe appelees depuis l'UI admin ----

def _broadcast_profiles_list_threadsafe():
    """Broadcast la liste des profils.
    - Aux clients normaux : juste les noms (compat, pas besoin des
      permissions cote client autre que celle qui le concerne, envoyee
      via my_profile).
    - Aux admins : la liste complete avec permissions (pour le panneau
      d'edition des profils)."""
    if _loop and _server_running:
        async def _do():
            # Clients : noms uniquement
            await _broadcast_clients_only(json.dumps({
                "type": "profiles_list",
                "profiles": _profile_names(),
            }))
            # Admins : full dicts avec permissions
            await _broadcast_admins(json.dumps({
                "type": "profiles_list",
                "profiles": [dict(p) for p in _profiles if isinstance(p, dict)],
            }))
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass


def add_profile(name: str) -> bool:
    """Ajoute un profil (tag de faction). Le profil est cree avec
    toutes ses permissions a False par defaut."""
    name = (name or "").strip()
    if not name or _profile_find(name) is not None:
        return False
    _profiles.append(_profile_default_dict(name))
    _save_profiles()
    _broadcast_profiles_list_threadsafe()
    _log(f"Profil ajoute : {name}", GREEN)
    return True


def rename_profile(old: str, new: str) -> bool:
    """Renomme un profil. Met a jour les joueurs qui l'avaient assigne.
    Les permissions du profil sont conservees (on modifie juste le nom)."""
    new = (new or "").strip()
    if not new or old == new:
        return False
    if _profile_find(new) is not None:
        return False
    p = _profile_find(old)
    if p is None:
        return False
    p["name"] = new
    _save_profiles()
    for ws, info in clients.items():
        if info.get("assigned_profile") == old:
            info["assigned_profile"] = new
    _broadcast_profiles_list_threadsafe()
    if _loop and _server_running:
        async def _do():
            for info in clients.values():
                if info.get("assigned_profile") == new:
                    await _broadcast_all(json.dumps({
                        "type": "player_profile",
                        "name": info["name"],
                        "profile": new,
                    }))
            for ws_, info_ in clients.items():
                if info_.get("assigned_profile") == new:
                    try:
                        await ws_.send(json.dumps(_build_my_profile_msg(new)))
                    except Exception:
                        pass
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass
    _log(f"Profil renomme : {old} -> {new}", BLUE)
    return True


def remove_profile(name: str) -> bool:
    """Supprime un profil. Les joueurs qui l'avaient n'en ont plus."""
    p = _profile_find(name)
    if p is None:
        return False
    _profiles.remove(p)
    _save_profiles()
    fallback = None
    affected = []
    for ws, info in clients.items():
        if info.get("assigned_profile") == name:
            info["assigned_profile"] = fallback
            affected.append(info["name"])
    _broadcast_profiles_list_threadsafe()
    if affected and _loop and _server_running:
        async def _do():
            for n in affected:
                await _broadcast_all(json.dumps({
                    "type": "player_profile",
                    "name": n,
                    "profile": fallback,
                }))
            for ws_, info_ in clients.items():
                if info_.get("name") in affected:
                    try:
                        await ws_.send(json.dumps(_build_my_profile_msg(fallback)))
                    except Exception:
                        pass
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass
    _log(f"Profil supprime : {name} (joueurs concernes : {len(affected)})", ORANGE)
    return True


def _broadcast_broadcasters_list_threadsafe():
    """Pousse la liste des broadcasters aux admins (et seulement aux admins).
    Contrairement aux canaux/profils, la liste des broadcasters est une
    info admin : pas la peine de l'exposer a tous les clients."""
    if _loop and _server_running:
        async def _do():
            await _broadcast_admins(json.dumps({
                "type": "broadcasters_list",
                "broadcasters": sorted(_broadcasters),
            }))
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass


def _find_client_ws_by_name(player_name: str):
    """Cherche dans `clients` la WebSocket associee au nom donne.
    Renvoie None si aucune connexion active ne porte ce nom."""
    for ws_, info in clients.items():
        if info.get("name") == player_name:
            return ws_
    return None


async def grant_broadcaster(player_name: str) -> tuple:
    """Accorde le role broadcaster a `player_name` et lui push son token
    via sa WebSocket. Renvoie (ok, reason).

    Le joueur DOIT etre connecte au moment du grant. C'est le seul canal
    par lequel le token est transmis (jamais affiche en clair, jamais
    persiste cote serveur autrement que sous forme de hash). Si le joueur
    se deconnecte avant d'avoir sauvegarde le token cote client, le grant
    doit etre refait."""
    player_name = (player_name or "").strip()
    if not player_name:
        return (False, "nom_vide")
    target_ws = _find_client_ws_by_name(player_name)
    if target_ws is None:
        return (False, "player_must_be_connected")
    # Token clair : 32 hex chars (16 octets d'entropie). Stocke uniquement
    # son hash cote serveur ; le clair part vers le client une seule fois.
    token = secrets.token_hex(16)
    _broadcasters[player_name] = _hash_broadcaster_token(token)
    _save_broadcasters()
    # Push au client cible. On ne capture pas l'echec : si la WS est cassee
    # entre temps, le grant reste valide cote serveur mais le client n'aura
    # pas le token (re-grant requis). L'admin verra le succes dans la
    # reponse mais le joueur ne pourra pas broadcast tant qu'il n'a pas
    # le token sauvegarde et re-presente au join.
    try:
        await target_ws.send(json.dumps({
            "type": "broadcaster_token_granted",
            "token": token,
        }))
    except Exception as e:
        _log(f"Broadcaster grant : echec push WS a {player_name} : {e}", ORANGE)
    _broadcast_broadcasters_list_threadsafe()
    _log(f"Broadcaster accorde : {player_name} (token pushed)", GREEN)
    return (True, "")


async def revoke_broadcaster(player_name: str) -> tuple:
    """Retire le role broadcaster a `player_name`. Si le joueur est connecte,
    lui push aussi un message broadcaster_revoked pour qu'il efface son
    token cote client. La capability cessera d'etre accordee au prochain
    ticket (TTL <= 120s). Idempotent."""
    player_name = (player_name or "").strip()
    if not player_name:
        return (False, "nom_vide")
    if player_name in _broadcasters:
        del _broadcasters[player_name]
        _save_broadcasters()
        target_ws = _find_client_ws_by_name(player_name)
        if target_ws is not None:
            try:
                await target_ws.send(json.dumps({
                    "type": "broadcaster_revoked",
                }))
            except Exception:
                pass
        _broadcast_broadcasters_list_threadsafe()
        _log(f"Broadcaster revoque : {player_name}", ORANGE)
    return (True, "")


def list_broadcasters() -> list:
    """Retourne la liste triee des noms de broadcasters actuels (hashes
    masques)."""
    return sorted(_broadcasters.keys())


def assign_profile(player_name: str, profile_name) -> bool:
    """Assigne un profil a un joueur connecte (admin uniquement)."""
    if profile_name is not None and _profile_find(profile_name) is None:
        return False
    target_ws = None
    for ws, info in clients.items():
        if info.get("name") == player_name:
            target_ws = ws
            break
    if target_ws is None:
        return False
    clients[target_ws]["assigned_profile"] = profile_name
    _log(f"Profil assigne : {player_name} -> {profile_name or '(aucun)'}", BLUE)
    if _loop and _server_running:
        async def _do():
            await _broadcast_all(json.dumps({
                "type": "player_profile",
                "name": player_name,
                "profile": profile_name,
            }))
            for ws, info in clients.items():
                if info.get("name") == player_name:
                    try:
                        await ws.send(json.dumps(_build_my_profile_msg(profile_name)))
                    except Exception:
                        pass
                    break
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass
    return True


# ─────────────────────────────────────────────
#  Interface tkinter
# ─────────────────────────────────────────────

class ServerUI:
    def __init__(self):
        global _ui
        _ui = self

        # Forcer un AppUserModelID distinct sur Windows AVANT tk.Tk().
        # Sans ca, Windows groupe toutes les fenetres Python sous la meme
        # icone (icone Python generique dans la taskbar). Avec un ID
        # explicite, notre icone StarCircus_Server.ico sera utilisee
        # pour la barre des taches au lieu de l'icone Python par defaut.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CircusVOIP.Server.0.2"
            )
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("CircusVOIP — Serveur 0.2")
        self.root.configure(bg=BG)
        # Icone de la fenetre + barre des taches : StarCircus_Server.ico
        # qui est dans le meme dossier que le script. Fallback silencieux
        # si le fichier est absent (pas critique).
        try:
            from pathlib import Path as _Path
            _ico_path = _Path(__file__).resolve().parent / "StarCircus_Server.ico"
            if _ico_path.exists():
                self.root.iconbitmap(default=str(_ico_path))
                self.root.wm_iconbitmap(str(_ico_path))
        except Exception:
            pass
        try:
            import circusvoip_dpi
            circusvoip_dpi.apply_tk_scaling(self.root)
        except Exception:
            pass
        self.root.geometry("900x600")
        self.root.minsize(700, 400)

        self._players: dict[str, dict] = {}
        self._closing = False
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        """Arret propre du serveur quand l'utilisateur ferme la fenetre."""
        self._closing = True
        try:
            global _server_running, _stop_event, _loop
            _server_running = False
            if _stop_event is not None and _loop is not None:
                _loop.call_soon_threadsafe(_stop_event.set)
        except Exception:
            pass
        try:
            global _debug_log_fp
            if _debug_log_fp:
                _debug_log_fp.close()
                _debug_log_fp = None
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        import os
        os._exit(0)

    def _safe_after(self, callback):
        if getattr(self, "_closing", False):
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def _build_ui(self):
        # ── Titre ──
        header = tk.Frame(self.root, bg=BG, pady=8)
        header.pack(fill="x", padx=12)

        tk.Label(header, text="◉  CircusVOIP Server", bg=BG, fg=BLUE,
                 font=("Courier", 14, "bold")).pack(side="left")

        self._lbl_ip = tk.Label(header, text="démarrage…", bg=BG, fg=MUTED,
                                font=("Courier", 10))
        self._lbl_ip.pack(side="right")

        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # ── Corps : log + joueurs ──
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # Panel gauche — logs
        left = tk.Frame(body, bg=BG_PANEL, bd=0, relief="flat")
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))

        tk.Label(left, text="LOGS", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 9, "bold"), anchor="w", padx=8, pady=4).pack(fill="x")
        tk.Frame(left, bg=BORDER, height=1).pack(fill="x")

        log_frame = tk.Frame(left, bg=BG_PANEL)
        log_frame.pack(fill="both", expand=True)

        self._log_text = tk.Text(log_frame, bg=BG_PANEL, fg=TEXT,
                                 font=("Courier", 9), state="disabled",
                                 wrap="word", bd=0, padx=8, pady=6,
                                 insertbackground=TEXT)
        sb = tk.Scrollbar(log_frame, command=self._log_text.yview)
        self._log_text.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self._log_text.pack(fill="both", expand=True)

        # Tags couleur
        for tag, color in [("green", GREEN), ("orange", ORANGE),
                            ("blue", BLUE), ("red", RED), ("muted", MUTED)]:
            self._log_text.tag_configure(tag, foreground=color)

        # Panel droit — joueurs
        right = tk.Frame(body, bg=BG_PANEL, bd=0, width=260)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        tk.Label(right, text="JOUEURS CONNECTÉS", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 9, "bold"), anchor="w", padx=8, pady=4).pack(fill="x")
        tk.Frame(right, bg=BORDER, height=1).pack(fill="x")

        self._player_list = tk.Frame(right, bg=BG_PANEL)
        self._player_list.pack(fill="both", expand=True, padx=6, pady=6)

        self._lbl_empty = tk.Label(self._player_list, text="Aucun joueur connecté",
                                   bg=BG_PANEL, fg=MUTED, font=("Courier", 9),
                                   anchor="w")
        self._lbl_empty.pack(fill="x", pady=4)

        # Compteur
        self._lbl_count = tk.Label(right, text="0 connecté(s)", bg=BG_PANEL,
                                   fg=MUTED, font=("Courier", 9), pady=4)
        self._lbl_count.pack(fill="x", padx=8)

        # Champ mot de passe serveur
        tk.Label(right, text="MOT DE PASSE SERVEUR", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 9, "bold"), anchor="w", padx=8, pady=4).pack(fill="x", pady=(8, 2))
        pwd_frame = tk.Frame(right, bg=BG_PANEL, padx=6)
        pwd_frame.pack(fill="x")
        self._entry_pwd = tk.Entry(pwd_frame, bg=BG_ROW, fg=TEXT,
                                   font=("Courier", 10), insertbackground=TEXT,
                                   relief="flat", bd=4, show="*")
        self._entry_pwd.insert(0, SERVER_TOKEN)
        self._entry_pwd.pack(side="left", fill="x", expand=True)
        self._pwd_visible = False
        self._btn_show_pwd = tk.Label(
            pwd_frame, text="👁", bg=BORDER, fg=TEXT,
            font=("Courier", 10), padx=8, pady=2, cursor="hand2",
        )
        self._btn_show_pwd.pack(side="right", padx=(4, 0))
        self._btn_show_pwd.bind("<Button-1>", lambda e: self._toggle_pwd_visibility())

        tk.Label(right, text="Modifier avant de demarrer", bg=BG_PANEL,
                 fg=MUTED, font=("Courier", 8), padx=8).pack(fill="x")

        # Boutons start/stop
        btn_frame = tk.Frame(right, bg=BG_PANEL, pady=6)
        btn_frame.pack(fill="x", padx=6)

        self._btn_start = tk.Label(btn_frame, text="▶  DÉMARRER", bg=GREEN,
                                   fg=BG, font=("Courier", 10, "bold"),
                                   pady=8, cursor="hand2")
        self._btn_start.pack(fill="x", pady=2)
        self._btn_start.bind("<Button-1>", lambda e: self._on_start())

        self._btn_stop = tk.Label(btn_frame, text="■  ARRÊTER", bg=BORDER,
                                  fg=MUTED, font=("Courier", 10, "bold"),
                                  pady=8, cursor="hand2")
        self._btn_stop.pack(fill="x", pady=2)
        self._btn_stop.bind("<Button-1>", lambda e: self._on_stop())

        self._btn_anon = tk.Label(btn_frame, text="🎭  Mode anonyme : OFF",
                                  bg=BORDER, fg=MUTED,
                                  font=("Courier", 10, "bold"),
                                  pady=8, cursor="hand2")
        self._btn_anon.pack(fill="x", pady=2)
        self._btn_anon.bind("<Button-1>", lambda e: self._on_toggle_anon())

        self._lbl_status = tk.Label(right, text="⬤  Arrêté", bg=BG_PANEL,
                                    fg=RED, font=("Courier", 9), pady=4)
        self._lbl_status.pack(fill="x", padx=8)

        # ─────────── CANAUX RADIO ───────────
        ch_section = tk.Label(right, text="CANAUX RADIO",
                              bg=BG_PANEL, fg=MUTED,
                              font=("Courier", 8, "bold"), padx=8)
        ch_section.pack(fill="x", pady=(12, 0))

        ch_container = tk.Frame(right, bg=BG_PANEL)
        ch_container.pack(fill="both", expand=True)

        self._btn_add_channel = tk.Label(
            ch_container, text="+  Ajouter canal", bg=BORDER, fg=TEXT,
            font=("Courier", 9, "bold"), pady=6, padx=8,
            cursor="hand2",
        )
        self._btn_add_channel.pack(side="bottom", fill="x", padx=8, pady=2)
        self._btn_add_channel.bind("<Button-1>", lambda e: self._on_add_channel())

        ch_list_outer = tk.Frame(ch_container, bg=BG_PANEL, height=180)
        ch_list_outer.pack(fill="both", expand=True)
        ch_list_outer.pack_propagate(False)

        self._ch_canvas = tk.Canvas(ch_list_outer, bg=BG_PANEL, bd=0,
                                    highlightthickness=0)
        ch_scroll = tk.Scrollbar(ch_list_outer, orient="vertical",
                                 command=self._ch_canvas.yview,
                                 bg=BORDER, troughcolor=BG_PANEL,
                                 activebackground=BLUE,
                                 width=12)
        self._ch_canvas.configure(yscrollcommand=ch_scroll.set)
        ch_scroll.pack(side="right", fill="y")
        self._ch_canvas.pack(side="left", fill="both", expand=True)

        self._channels_list_frame = tk.Frame(self._ch_canvas, bg=BG_PANEL)
        self._ch_window = self._ch_canvas.create_window(
            (0, 0), window=self._channels_list_frame, anchor="nw"
        )

        def _on_canvas_resize(event):
            self._ch_canvas.itemconfig(self._ch_window, width=event.width)
        self._ch_canvas.bind("<Configure>", _on_canvas_resize)

        def _on_inner_resize(_event):
            self._ch_canvas.configure(scrollregion=self._ch_canvas.bbox("all"))
        self._channels_list_frame.bind("<Configure>", _on_inner_resize)

        def _on_mousewheel(event):
            self._ch_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._ch_canvas.bind("<Enter>",
            lambda e: self._ch_canvas.bind_all("<MouseWheel>", _on_mousewheel))
        self._ch_canvas.bind("<Leave>",
            lambda e: self._ch_canvas.unbind_all("<MouseWheel>"))

        self._refresh_channels_ui()

        # ─────────── PROFILS ───────────
        prof_section = tk.Label(right, text="PROFILS",
                                bg=BG_PANEL, fg=MUTED,
                                font=("Courier", 8, "bold"), padx=8)
        prof_section.pack(fill="x", pady=(12, 0))

        prof_container = tk.Frame(right, bg=BG_PANEL, height=160)
        prof_container.pack(fill="x")
        prof_container.pack_propagate(False)

        self._btn_add_profile = tk.Label(
            prof_container, text="+  Ajouter profil", bg=BORDER, fg="#bc8cff",
            font=("Courier", 9, "bold"), pady=6, padx=8,
            cursor="hand2",
        )
        self._btn_add_profile.pack(side="bottom", fill="x", padx=8, pady=2)
        self._btn_add_profile.bind("<Button-1>", lambda e: self._on_add_profile())

        prof_list_outer = tk.Frame(prof_container, bg=BG_PANEL)
        prof_list_outer.pack(fill="both", expand=True)
        prof_list_outer.pack_propagate(False)

        self._prof_canvas = tk.Canvas(prof_list_outer, bg=BG_PANEL, bd=0,
                                      highlightthickness=0)
        prof_scroll = tk.Scrollbar(prof_list_outer, orient="vertical",
                                   command=self._prof_canvas.yview,
                                   bg=BORDER, troughcolor=BG_PANEL,
                                   activebackground=BLUE,
                                   width=12)
        self._prof_canvas.configure(yscrollcommand=prof_scroll.set)
        prof_scroll.pack(side="right", fill="y")
        self._prof_canvas.pack(side="left", fill="both", expand=True)

        self._profiles_list_frame = tk.Frame(self._prof_canvas, bg=BG_PANEL)
        self._prof_window = self._prof_canvas.create_window(
            (0, 0), window=self._profiles_list_frame, anchor="nw"
        )

        def _on_prof_canvas_resize(event):
            self._prof_canvas.itemconfig(self._prof_window, width=event.width)
        self._prof_canvas.bind("<Configure>", _on_prof_canvas_resize)

        def _on_prof_inner_resize(_event):
            self._prof_canvas.configure(scrollregion=self._prof_canvas.bbox("all"))
        self._profiles_list_frame.bind("<Configure>", _on_prof_inner_resize)

        def _on_prof_mousewheel(event):
            self._prof_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._prof_canvas.bind("<Enter>",
            lambda e: self._prof_canvas.bind_all("<MouseWheel>", _on_prof_mousewheel))
        self._prof_canvas.bind("<Leave>",
            lambda e: self._prof_canvas.unbind_all("<MouseWheel>"))

        self._refresh_profiles_ui()

    def _refresh_profiles_ui(self):
        """Reconstruit la liste des profils + rafraichit les selecteurs joueurs."""
        for w in self._profiles_list_frame.winfo_children():
            w.destroy()
        if not _profiles:
            tk.Label(self._profiles_list_frame,
                     text="(aucun profil)\nAjoutez-en pour pouvoir les\nassigner aux joueurs.",
                     bg=BG_PANEL, fg=MUTED, font=("Courier", 8),
                     anchor="w", justify="left").pack(fill="x", padx=4)
            self._refresh_all_players_profile_select()
            return
        for prof_name in _profile_names():
            # v0.2 alpha 036 : un bloc englobant par profil pour grouper
            # le nom et ses cases de permission. Permet d'afficher la
            # case "Soundboard autorise" directement dans l'UI serveur,
            # sans passer par l'admin (utile quand le serveur tourne en
            # local et qu'on n'utilise pas l'admin distant).
            block = tk.Frame(self._profiles_list_frame, bg=BG_ROW, pady=2, padx=6)
            block.pack(fill="x", pady=1, padx=6)
            # Ligne 1 : nom + boutons rename/delete
            row = tk.Frame(block, bg=BG_ROW)
            row.pack(fill="x")
            tk.Label(row, text=prof_name, bg=BG_ROW, fg="#bc8cff",
                     font=("Courier", 9, "bold"), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            btn_ren = tk.Label(row, text="✎", bg=BG_ROW, fg=BLUE,
                               font=("Courier", 9, "bold"), cursor="hand2",
                               padx=4)
            btn_ren.pack(side="left")
            btn_ren.bind("<Button-1>",
                         lambda e, n=prof_name: self._on_rename_profile(n))
            btn_del = tk.Label(row, text="✕", bg=BG_ROW, fg=RED,
                               font=("Courier", 9, "bold"), cursor="hand2",
                               padx=4)
            btn_del.pack(side="left")
            btn_del.bind("<Button-1>",
                         lambda e, n=prof_name: self._on_delete_profile(n))
            # Ligne 2 : case a cocher "Soundboard autorise". L'etat est
            # lu directement du dict _profiles via _profile_has_perm.
            # Au toggle, on appelle _set_profile_perm_local qui :
            #   - met a jour le dict
            #   - sauvegarde _profiles -> JSON
            #   - broadcast aux admins (profiles_list)
            #   - push my_profile aux clients ayant ce profil
            perm_row = tk.Frame(block, bg=BG_ROW)
            perm_row.pack(fill="x", pady=(2, 0))
            sb_var = tk.BooleanVar(
                value=_profile_has_perm(prof_name, "soundboard_allowed")
            )
            cb = tk.Checkbutton(
                perm_row,
                text="🔊 Soundboard autorise",
                variable=sb_var,
                bg=BG_ROW, fg=TEXT, font=("Courier", 8),
                activebackground=BG_ROW, activeforeground=TEXT,
                selectcolor=BG_PANEL,
                relief="flat", bd=0, highlightthickness=0,
                cursor="hand2", anchor="w",
                command=lambda n=prof_name, v=sb_var:
                    self._on_toggle_profile_perm_local(n, "soundboard_allowed", v.get()),
            )
            cb.pack(side="left", padx=(12, 0))
        self._refresh_all_players_profile_select()

    def _on_toggle_profile_perm_local(self, profile_name: str, perm_key: str, value: bool):
        """Toggle d'une permission profil depuis l'UI serveur directe
        (v0.2 alpha 036). Equivalent local du dispatch admin
        set_profile_permission, sans passer par le reseau.

        Met a jour le dict _profiles, persiste, broadcast aux admins
        (s'il y en a), et push my_profile aux clients ayant ce profil
        pour effet immediat cote UI."""
        if perm_key not in _PROFILE_PERM_DEFAULTS:
            _log(f"_on_toggle_profile_perm_local : perm inconnue {perm_key}", ORANGE)
            return
        p = _profile_find(profile_name)
        if p is None:
            _log(f"_on_toggle_profile_perm_local : profil introuvable {profile_name}", ORANGE)
            return
        p[perm_key] = bool(value)
        _save_profiles()
        _log(
            f"Profil '{profile_name}' : {perm_key} = {value} (UI serveur)",
            BLUE
        )
        # Broadcast aux admins + clients (deja gere par
        # _broadcast_profiles_list_threadsafe qui envoie 2 messages
        # distincts)
        _broadcast_profiles_list_threadsafe()
        # Push my_profile aux clients qui ont ce profil assigne, pour
        # mise a jour immediate de leur UI (afficher/cacher le bouton
        # soundboard).
        if _loop and _server_running:
            async def _do():
                for ws_, info_ in list(clients.items()):
                    if info_.get("assigned_profile") == profile_name:
                        try:
                            await ws_.send(json.dumps(
                                _build_my_profile_msg(profile_name)
                            ))
                        except Exception:
                            pass
            try:
                asyncio.run_coroutine_threadsafe(_do(), _loop)
            except Exception:
                pass

    def _on_add_profile(self):
        from tkinter import simpledialog, messagebox
        new_name = simpledialog.askstring(
            "Ajouter profil",
            "Nom du nouveau profil :",
            parent=self.root,
        )
        if new_name:
            ok = add_profile(new_name)
            if ok:
                self._refresh_profiles_ui()
            else:
                messagebox.showwarning(
                    "CircusVOIP",
                    "Profil invalide ou deja existant",
                    parent=self.root,
                )

    def _on_rename_profile(self, old_name: str):
        from tkinter import simpledialog, messagebox
        new_name = simpledialog.askstring(
            "Renommer profil",
            f"Nouveau nom pour '{old_name}' :",
            initialvalue=old_name,
            parent=self.root,
        )
        if new_name and new_name != old_name:
            ok = rename_profile(old_name, new_name)
            if ok:
                self._refresh_profiles_ui()
            else:
                messagebox.showwarning(
                    "CircusVOIP",
                    "Renommage refuse (nom invalide ou deja pris)",
                    parent=self.root,
                )

    def _on_delete_profile(self, name: str):
        from tkinter import messagebox
        if messagebox.askyesno(
            "Supprimer profil",
            f"Supprimer le profil '{name}' ?\n\n"
            f"Les joueurs assignes a ce profil n'en auront plus.",
            parent=self.root,
        ):
            ok = remove_profile(name)
            if ok:
                self._refresh_profiles_ui()

    def _refresh_channels_ui(self):
        """Reconstruit la liste des canaux dans l'UI."""
        for w in self._channels_list_frame.winfo_children():
            w.destroy()
        if not _channels:
            tk.Label(self._channels_list_frame, text="(aucun canal)",
                     bg=BG_PANEL, fg=MUTED, font=("Courier", 8),
                     anchor="w").pack(fill="x")
            return
        for ch_name in _channels:
            row = tk.Frame(self._channels_list_frame, bg=BG_ROW, pady=2, padx=6)
            row.pack(fill="x", pady=1, padx=6)
            tk.Label(row, text=ch_name, bg=BG_ROW, fg=TEXT,
                     font=("Courier", 9), anchor="w"
                     ).pack(side="left", fill="x", expand=True)
            btn_ren = tk.Label(row, text="✎", bg=BG_ROW, fg=BLUE,
                               font=("Courier", 9, "bold"), cursor="hand2",
                               padx=4)
            btn_ren.pack(side="left")
            btn_ren.bind("<Button-1>",
                         lambda e, n=ch_name: self._on_rename_channel(n))
            btn_del = tk.Label(row, text="✕", bg=BG_ROW, fg=RED,
                               font=("Courier", 9, "bold"), cursor="hand2",
                               padx=4)
            btn_del.pack(side="left")
            btn_del.bind("<Button-1>",
                         lambda e, n=ch_name: self._on_delete_channel(n))

    def _on_add_channel(self):
        from tkinter import simpledialog, messagebox
        new_name = simpledialog.askstring(
            "Ajouter canal",
            "Nom du nouveau canal :",
            parent=self.root,
        )
        if new_name:
            ok = add_channel(new_name)
            if ok:
                self._refresh_channels_ui()
            else:
                messagebox.showwarning(
                    "CircusVOIP",
                    "Canal invalide ou deja existant",
                    parent=self.root,
                )

    def _on_rename_channel(self, old_name: str):
        from tkinter import simpledialog, messagebox
        new_name = simpledialog.askstring(
            "Renommer canal",
            f"Nouveau nom pour '{old_name}' :",
            initialvalue=old_name,
            parent=self.root,
        )
        if new_name and new_name != old_name:
            ok = rename_channel(old_name, new_name)
            if ok:
                self._refresh_channels_ui()
            else:
                messagebox.showwarning(
                    "CircusVOIP",
                    "Renommage refuse (nom invalide ou deja pris)",
                    parent=self.root,
                )

    def _on_delete_channel(self, name: str):
        from tkinter import messagebox
        if messagebox.askyesno(
            "Supprimer canal",
            f"Supprimer le canal '{name}' ?\n\n"
            f"Les joueurs connectes sur ce canal seront reassignes.",
            parent=self.root,
        ):
            ok = remove_channel(name)
            if ok:
                self._refresh_channels_ui()

    def _toggle_pwd_visibility(self):
        self._pwd_visible = not self._pwd_visible
        self._entry_pwd.config(show="" if self._pwd_visible else "*")

    def _on_start(self):
        if not _server_running:
            global SERVER_TOKEN
            new_pwd = self._entry_pwd.get().strip()
            if new_pwd and new_pwd != SERVER_TOKEN:
                set_password(new_pwd)
                SERVER_TOKEN = new_pwd
                # [P5] log du changement, pas de la valeur
                _log(f"Mot de passe mis a jour ({_masked(new_pwd)})", BLUE)
            elif not new_pwd:
                set_password("")
                SERVER_TOKEN = get_token()
                self._entry_pwd.delete(0, "end")
                self._entry_pwd.insert(0, SERVER_TOKEN)
                _log(f"Mot de passe regenere ({_masked(SERVER_TOKEN)})", BLUE)
            start_server()

    def _on_stop(self):
        if _server_running:
            stop_server()

    def _on_toggle_anon(self):
        new_state = toggle_anonymous_mode()
        if new_state:
            self._btn_anon.config(text="🎭  Mode anonyme : ON",
                                  bg=BLUE, fg=BG)
        else:
            self._btn_anon.config(text="🎭  Mode anonyme : OFF",
                                  bg=BORDER, fg=MUTED)

    def set_status(self, running: bool):
        def _do():
            if running:
                self._lbl_status.config(text="⬤  En ligne", fg=GREEN)
                self._btn_start.config(bg=BORDER, fg=MUTED)
                self._btn_stop.config(bg=RED, fg="white")
            else:
                self._lbl_status.config(text="⬤  Arrêté", fg=RED)
                self._btn_start.config(bg=GREEN, fg=BG)
                self._btn_stop.config(bg=BORDER, fg=MUTED)
                self._lbl_ip.config(text="—", fg=MUTED)
        self._safe_after(_do)

    def clear_players(self):
        def _do():
            for name in list(self._players.keys()):
                self._players[name]["frame"].destroy()
            self._players.clear()
            self._lbl_empty.pack(fill="x", pady=4)
            self._update_count()
        self._safe_after(_do)

    def set_ip(self, ip_str: str):
        self._safe_after(lambda: self._lbl_ip.config(text=f"🌐  {ip_str}", fg=GREEN))

    def add_log(self, msg: str, color: str = TEXT):
        tag = {GREEN: "green", ORANGE: "orange", BLUE: "blue",
               RED: "red", MUTED: "muted"}.get(color, None)

        def _do():
            self._log_text.configure(state="normal")
            if tag:
                self._log_text.insert("end", msg + "\n", tag)
            else:
                self._log_text.insert("end", msg + "\n")
            self._log_text.see("end")
            self._log_text.configure(state="disabled")

        self._safe_after(_do)

    def add_player(self, name: str):
        def _do():
            if name in self._players:
                return
            self._lbl_empty.pack_forget()

            frame = tk.Frame(self._player_list, bg=BG_ROW, pady=4, padx=8)
            frame.pack(fill="x", pady=2)

            tk.Label(frame, text=f"● {name}", bg=BG_ROW, fg=GREEN,
                     font=("Courier", 10, "bold"), anchor="w").pack(fill="x")

            lbl_pos = tk.Label(frame, text="position en attente…",
                               bg=BG_ROW, fg=MUTED,
                               font=("Courier", 8), anchor="w")
            lbl_pos.pack(fill="x")

            lbl_channel = tk.Label(frame, text="Canal : (aucun)",
                                   bg=BG_ROW, fg=MUTED,
                                   font=("Courier", 8), anchor="w")
            lbl_channel.pack(fill="x")

            prof_frame = tk.Frame(frame, bg=BG_ROW)
            prof_frame.pack(fill="x", pady=(2, 0))

            self._players[name] = {
                "frame": frame,
                "lbl_pos": lbl_pos,
                "lbl_channel": lbl_channel,
                "prof_frame": prof_frame,
            }
            self._update_count()
            self._build_player_profile_selector(name)
            self._refresh_player_channel(name)

        self._safe_after(_do)

    def _build_player_profile_selector(self, name: str):
        info = self._players.get(name)
        if not info:
            return
        prof_frame = info["prof_frame"]
        for w in prof_frame.winfo_children():
            w.destroy()
        profile_names = _profile_names()
        if not profile_names:
            return
        current = None
        for ws_, cinfo in clients.items():
            if cinfo.get("name") == name:
                current = cinfo.get("assigned_profile")
                break
        NO_PROF = "(aucun)"
        var = tk.StringVar(value=current if current else NO_PROF)
        tk.Label(prof_frame, text="Profil :", bg=BG_ROW, fg=MUTED,
                 font=("Courier", 8)).pack(side="left", padx=(0, 4))
        menu = tk.OptionMenu(prof_frame, var, NO_PROF, *profile_names)
        menu.config(bg=BG_PANEL, fg="#bc8cff", font=("Courier", 8),
                    activebackground=BORDER, activeforeground=TEXT,
                    relief="flat", bd=0, padx=4, pady=0,
                    highlightthickness=0)
        menu["menu"].config(bg=BG_PANEL, fg=TEXT, font=("Courier", 8),
                            activebackground=BORDER, activeforeground=TEXT)
        menu.pack(side="left", fill="x", expand=True)

        info["_prof_setting_initial"] = True

        def _on_change(*_):
            if info.get("_prof_setting_initial"):
                info["_prof_setting_initial"] = False
                return
            sel = var.get()
            new_prof = None if sel == NO_PROF else sel
            ok = assign_profile(name, new_prof)
            if not ok:
                info["_prof_setting_initial"] = True
                var.set(current if current else NO_PROF)
        var.trace_add("write", _on_change)
        info["_prof_setting_initial"] = False

    def _refresh_all_players_profile_select(self):
        for name in list(self._players.keys()):
            self._build_player_profile_selector(name)

    def _refresh_player_channel(self, name: str):
        info = self._players.get(name)
        if not info:
            return
        ch = None
        for ws_, cinfo in clients.items():
            if cinfo.get("name") == name:
                ch = cinfo.get("channel")
                break
        text = f"Canal : {ch}" if ch else "Canal : (aucun)"
        color = TEXT if ch else MUTED
        info["lbl_channel"].config(text=text, fg=color)

    def refresh_player_channel(self, name: str):
        self._safe_after(lambda: self._refresh_player_channel(name))

    def remove_player(self, name: str):
        def _do():
            if name not in self._players:
                return
            self._players[name]["frame"].destroy()
            del self._players[name]
            if not self._players:
                self._lbl_empty.pack(fill="x", pady=4)
            self._update_count()

        self._safe_after(_do)

    def update_player(self, name: str, pos: dict):
        def _do():
            if name not in self._players:
                return
            x = pos.get("x", 0) / 1000
            y = pos.get("y", 0) / 1000
            z = pos.get("z", 0) / 1000
            self._players[name]["lbl_pos"].config(
                text=f"X:{x:.3f}  Y:{y:.3f}  Z:{z:.3f} km",
                fg=TEXT
            )

        self._safe_after(_do)

    def _update_count(self):
        n = len(self._players)
        self._lbl_count.config(text=f"{n} connecté(s)")


# ─────────────────────────────────────────────
#  Point d'entrée
# ─────────────────────────────────────────────

def _run_headless():
    """Lance le serveur sans UI Tkinter."""
    print("=" * 60)
    print("CircusVOIP Server - mode headless")
    print(f"Port positions : {PORT}")
    print(f"Token serveur  : {_masked(SERVER_TOKEN)}  "
          f"(valeur complete dans circusvoip_server_config.json)")
    print(f"Token admin    : {_masked(ADMIN_TOKEN)}  "
          f"(valeur complete dans circusvoip_admin_token.json)")
    print(f"Cap clients    : {MAX_CLIENTS} simultanes")
    print(f"Lockout auth   : {AUTH_MAX_FAILURES} echecs / {AUTH_WINDOW_SEC}s "
          f"-> ban {AUTH_BAN_SEC}s")
    print(f"Canaux         : {_channels}")
    print(f"Profils        : {_profile_names()}")
    print(f"Mode anonyme   : {'ON' if _anonymous_mode else 'OFF'}")
    print("=" * 60)
    print("Logs (Ctrl+C pour arreter) :")
    print()
    try:
        _debug_log_init()
        _run_server()
    except KeyboardInterrupt:
        print("\n[INFO] Arret demande par l'utilisateur (Ctrl+C)")
    finally:
        global _debug_log_fp, _phone_log_fp
        if _debug_log_fp:
            try:
                _debug_log_fp.close()
            except Exception:
                pass
            _debug_log_fp = None
        if _phone_log_fp:
            try:
                _phone_log_fp.close()
            except Exception:
                pass
            _phone_log_fp = None


if __name__ == "__main__":
    import sys
    if "--headless" in sys.argv:
        _run_headless()
    else:
        ServerUI()
