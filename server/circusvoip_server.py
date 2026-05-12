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

# ─────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────

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
_debug_log_fp   = None
_last_pos_time: dict = {}  # dernier timestamp par joueur (pour dt)

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
    """Ouvre/reinitialise le fichier de log debug serveur."""
    global _debug_log_fp
    try:
        _DEBUG_DIR.mkdir(exist_ok=True)
        _debug_log_fp = open(DEBUG_LOG_FILE, "w", encoding="utf-8", buffering=1)
        _debug_log_fp.write(
            f"=== Session demarree : {datetime.now():%Y-%m-%d %H:%M:%S} ===\n"
        )
    except Exception as e:
        print(f"[DEBUG LOG] Echec ouverture fichier : {e}")
        _debug_log_fp = None


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


def _load_profiles() -> list:
    """Charge la liste des profils depuis circusvoip_profiles.json.
    Retourne [] si absent (les profils sont optionnels).
    Migration : si l'ancien fichier circusvoip_channels.json contenait des
    entries avec is_profile=true, on les recupere ici (migration auto)."""
    profiles = []
    seen = set()
    try:
        if _PROFILES_FILE.exists():
            with open(_PROFILES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    if isinstance(item, str):
                        n = item.strip()
                        if n and n not in seen:
                            profiles.append(n)
                            seen.add(n)
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
                            profiles.append(n)
                            seen.add(n)
                            migrated.append(n)
                if migrated:
                    print(f"[PROFILES] Migration auto depuis channels.json : {migrated}")
    except Exception:
        pass
    return profiles


def _save_profiles():
    """Persiste la liste des profils dans circusvoip_profiles.json."""
    try:
        with open(_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(_profiles, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[PROFILES] Echec sauvegarde {_PROFILES_FILE.name} : {e}")


# Initialisation : charger au demarrage du module
_channels = _load_channels()
_profiles = _load_profiles()
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
        clients.pop(ws, None)
    await _broadcast_admins(message)


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
            await _broadcast_all(json.dumps({"type": "leave", "name": name}))


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
            "profiles": list(_profiles),
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
                }
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
                await ws.send(json.dumps({
                    "type": "welcome",
                    "players": existing,
                    "anonymous_mode": _anonymous_mode,
                    "channels": list(_channels),
                    "profiles": list(_profiles),
                    "my_channel": initial_channel,
                    "my_profile": None,
                }))
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

    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if ws in clients:
            clients.pop(ws)
            _log(f"LEAVE : {name}  ({len(clients)} connecté(s))", ORANGE)
            if _ui:
                _ui.remove_player(name)
            await _broadcast_all(json.dumps({"type": "leave", "name": name}))


_loop: asyncio.AbstractEventLoop | None = None
_stop_event: asyncio.Event | None = None

async def _server_main():
    global _stop_event, _server_running
    _stop_event = asyncio.Event()
    _server_running = True
    _debug_log_init()
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
    async with websockets.serve(handler, HOST, PORT):
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
    """Broadcast la liste des profils a tous les clients."""
    if _loop and _server_running:
        async def _do():
            await _broadcast_all(json.dumps({
                "type": "profiles_list",
                "profiles": list(_profiles),
            }))
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass


def add_profile(name: str) -> bool:
    """Ajoute un profil (tag de faction)."""
    name = (name or "").strip()
    if not name or name in _profiles:
        return False
    _profiles.append(name)
    _save_profiles()
    _broadcast_profiles_list_threadsafe()
    _log(f"Profil ajoute : {name}", GREEN)
    return True


def rename_profile(old: str, new: str) -> bool:
    """Renomme un profil. Met a jour les joueurs qui l'avaient assigne."""
    new = (new or "").strip()
    if not new or old == new or new in _profiles or old not in _profiles:
        return False
    idx = _profiles.index(old)
    _profiles[idx] = new
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
                        await ws_.send(json.dumps({
                            "type": "my_profile",
                            "profile": new,
                        }))
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
    if name not in _profiles:
        return False
    _profiles.remove(name)
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
                        await ws_.send(json.dumps({
                            "type": "my_profile",
                            "profile": fallback,
                        }))
                    except Exception:
                        pass
        try:
            asyncio.run_coroutine_threadsafe(_do(), _loop)
        except Exception:
            pass
    _log(f"Profil supprime : {name} (joueurs concernes : {len(affected)})", ORANGE)
    return True


def assign_profile(player_name: str, profile_name) -> bool:
    """Assigne un profil a un joueur connecte (admin uniquement)."""
    if profile_name is not None and profile_name not in _profiles:
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
                        await ws.send(json.dumps({
                            "type": "my_profile",
                            "profile": profile_name,
                        }))
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
                "CircusVOIP.Server.0.1"
            )
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("CircusVOIP — Serveur 0.1")
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
        for prof_name in _profiles:
            row = tk.Frame(self._profiles_list_frame, bg=BG_ROW, pady=2, padx=6)
            row.pack(fill="x", pady=1, padx=6)
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
        self._refresh_all_players_profile_select()

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
        profile_names = list(_profiles)
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
    print(f"Profils        : {_profiles}")
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
        global _debug_log_fp
        if _debug_log_fp:
            try:
                _debug_log_fp.close()
            except Exception:
                pass
            _debug_log_fp = None


if __name__ == "__main__":
    import sys
    if "--headless" in sys.argv:
        _run_headless()
    else:
        ServerUI()
