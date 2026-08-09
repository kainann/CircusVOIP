# -*- coding: utf-8 -*-
# =============================================
#  CircusVOIP Audio Server (PATCHE SECURITE)
# =============================================
# Serveur WebSocket qui relaie les flux audio entre clients.
# Port 8889 (different du serveur positions sur 8888).
#
# Chaque client envoie des trames audio PCM float32 mono 48kHz.
# Le serveur les relaie a tous les autres clients (pas a l'emetteur lui-meme).
# Le client destinataire applique le volume localement selon la distance.
#
# Lance : py -3.14 circusvoip_audio_server.py
# Deps  : pip install websockets numpy
#
# PATCHES SECURITE appliques :
#   [P1] compare_digest sur le token (anti-timing-attack)
#   [P2] Audio refuse tant que le client n'a pas envoye son join
#   [P3] Cap MAX_AUDIO_CLIENTS pour eviter le DoS par flood de connexions
# =============================================

import asyncio
import json
import logging
import os
import secrets
import time
import threading
import sys
from datetime import datetime
from pathlib import Path

# Tkinter n'est pas necessaire en mode headless. On l'importe seulement
# si on n'est pas explicitement en mode --headless, pour permettre le
# deploiement sur des VPS Linux sans tkinter installe.
if "--headless" not in sys.argv:
    import tkinter as tk
else:
    tk = None  # Sentinelle : ServerUI n'est jamais instanciee en headless

try:
    import websockets
except ImportError:
    print("Installez websockets : py -3.14 -m pip install websockets")
    exit(1)


# ─────────────────────────────────────────────
#  Nettoyage des logs de handshake WebSocket
# ─────────────────────────────────────────────
# Quand une connexion NON-WebSocket tape le port audio (scan de ports, sonde
# de monitoring, navigateur/curl en http://...:PORT), la lib `websockets`
# refuse au handshake et logge un TRACEBACK complet ("opening handshake
# failed" + InvalidUpgrade: missing Connection header). C'est benin — le
# serveur rejette correctement, aucun joueur n'est affecte — mais ca pollue le
# journal et fait croire a un incident lors des analyses (observe 3x le
# 20/07/2026). Ce filtre transforme ces rejets en UNE ligne WARNING lisible,
# sans stack trace, sans toucher a la logique de connexion.
# InvalidHandshake est la classe de base de tous les echecs de handshake
# (dont InvalidUpgrade). On l'importe explicitement : `websockets.exceptions`
# n'est pas resolu par le simple `import websockets` (imports paresseux de la
# lib). Import defensif pour ne jamais casser le demarrage si l'API bouge.
try:
    from websockets.exceptions import InvalidHandshake as _InvalidHandshake
    _BENIGN_HANDSHAKE = (_InvalidHandshake,)
except Exception:  # pragma: no cover
    _BENIGN_HANDSHAKE = ()


class _WsHandshakeNoiseFilter(logging.Filter):
    _BENIGN = _BENIGN_HANDSHAKE

    def filter(self, record):
        exc = record.exc_info[1] if record.exc_info else None
        is_benign = isinstance(exc, self._BENIGN)
        if not is_benign and exc is None:
            # certaines versions loggent sans exc_info : repli sur le message
            is_benign = "opening handshake failed" in record.getMessage()
        if is_benign:
            detail = type(exc).__name__ if exc is not None else "requete non-WebSocket"
            record.msg = ("handshake rejete (connexion non-WebSocket sur le "
                          f"port audio : {detail}) - ignore")
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True  # on garde la ligne (nettoyee), on ne supprime jamais


def _install_ws_handshake_log_filter():
    """Attache le filtre au logger que websockets utilise pour les connexions
    serveur. Idempotent (ne s'ajoute qu'une fois)."""
    lg = logging.getLogger("websockets.server")
    if not any(isinstance(f, _WsHandshakeNoiseFilter) for f in lg.filters):
        lg.addFilter(_WsHandshakeNoiseFilter())

from circusvoip_server_config import get_token

# [SECURITE] Module commun (lockout, rate limiting, registre de tickets,
# helper TLS). Doit etre present dans le meme dossier que ce fichier.
from circusvoip_security import AuthLockout, RateLimiter, AuthRegistry

# ---------------------------------------------
#  Config
# ---------------------------------------------

AUDIO_PORT = 8889
SERVER_TOKEN = get_token()  # meme token que serveur positions

# [P3] Limite le nombre de connexions audio simultanees pour eviter qu'un
# attaquant ouvre des milliers de WS et sature la machine (broadcast O(N)).
# 64 = large pour une communaute de 10-50, sans laisser la porte ouverte.
MAX_AUDIO_CLIENTS = 64

# [P2 - lockout brute-force] Le serveur audio n'avait AUCUNE protection
# anti-bruteforce (contrairement au serveur positions). Une IP qui rate
# le token 5 fois en 60s est maintenant bannie 600s.
_auth_lockout = AuthLockout(max_failures=5, window_sec=60, ban_sec=600)

# [P5 - rate limiting] Quota de trames audio par client authentifie.
# L'audio tourne a ~50 trames/s en regime normal (20 ms/trame). En mode
# CircusPhone HP avec voisin proximity, le client envoie en DOUBLE
# (0x03 telephone + 0x00 proximity) = ~100 trames/s.
#
# Historique des valeurs :
#   v0.1.x : rate=60.0, burst=120.0 (calibre pour mono-envoi seul)
#   25/05/2026 : double a rate=120.0, burst=240.0 apres analyse logs
#     session 25/05 (debug crackling) - 5 joueurs sur 7 subissaient des
#     drops massifs (jusqu'a 3000+ drops cumules pour hugolisoir). Les
#     pics observes vont jusqu'a ~70 trames/s en pointe sur 3 minutes.
#     Marge : tolere 100 trames/s en permanent + 2s de burst a 150 trames/s.
_audio_rate = RateLimiter(rate=120.0, burst=240.0)

# [P4 - auth partagee] Registre de tickets partage avec le serveur
# positions. Un client doit presenter un ticket emis par le serveur
# positions pour etre accepte ici. Meme fichier des deux cotes.
_AUTH_REGISTRY_FILE = Path(__file__).resolve().parent / "circusvoip_auth_tickets.json"
_auth_registry = AuthRegistry(_AUTH_REGISTRY_FILE, ttl_sec=120.0)

# === Log fichier debug crackling (ajout 25/05/2026) ===
# Tous les logs ui.log() ainsi que update_stats() sont aussi ecrits dans
# un fichier dedie /var/log/circusvoip-audio/audio_YYYYMMDD_HHMMSS.log.
# Un fichier par demarrage du service (Option 3 : facile a correler avec
# les logs debug clients qui suivent le meme schema).
# Fallback silencieux : si le dossier n'existe pas ou n'est pas accessible
# en ecriture, on log uniquement sur stdout comme aujourd'hui. Pas de crash.
_LOG_DIR = Path("/var/log/circusvoip-audio")
_log_file_handle = None     # writeable text handle ou None si echec ouverture
_log_file_path   = None     # Path pour info au demarrage

def _open_audio_log_file():
    """Tente d'ouvrir le fichier de log fichier. Retourne True si OK, False
    sinon. Stocke le handle dans _log_file_handle (None si echec). Pas
    d'exception remontee : on degrade silencieusement vers stdout."""
    global _log_file_handle, _log_file_path
    try:
        # Cree le dossier si possible. Si pas les droits, on capte plus bas.
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        _log_file_path = _LOG_DIR / f"audio_{ts}.log"
        # line buffering (=1) pour que les logs soient flushes ligne par
        # ligne. Important pour debug live (tail -f).
        _log_file_handle = open(_log_file_path, "w", encoding="utf-8",
                                buffering=1)
        return True
    except Exception as e:
        _log_file_handle = None
        _log_file_path = None
        # Affiche sur stdout que l'ouverture a echoue (visible journalctl)
        # mais ne crash pas le serveur audio pour autant.
        print(f"[WARN] Impossible d'ouvrir le log fichier audio : "
              f"{type(e).__name__}: {e}. Fallback stdout uniquement.",
              flush=True)
        return False

def _audio_log_write(line: str):
    """Ecrit une ligne dans le fichier de log audio (si ouvert). Pas de
    timestamp ajoute ici : c'est l'appelant qui doit prefixer si voulu.
    No-op si le fichier n'a pas pu etre ouvert."""
    h = _log_file_handle
    if h is None:
        return
    try:
        h.write(line)
        if not line.endswith("\n"):
            h.write("\n")
    except Exception:
        # Disque plein, fichier supprime, etc. : on ignore. stdout reste OK.
        pass

# Format audio utilise par les clients (pour info, pas enforced)
# - 48000 Hz
# - mono
# - float32
# - blocs de 960 samples (20ms)

# ---------------------------------------------
#  Theme
# ---------------------------------------------

BG      = "#0d1117"
BG_PANEL= "#161b22"
BG_ROW  = "#21262d"
BORDER  = "#30363d"
TEXT    = "#c9d1d9"
MUTED   = "#6e7681"
GREEN   = "#3fb950"
ORANGE  = "#d29922"
BLUE    = "#58a6ff"
PURPLE  = "#bc8cff"
RED     = "#f85149"

# ---------------------------------------------
#  Etat
# ---------------------------------------------

class State:
    clients = {}   # websocket -> name
    # [BROADCAST_ALL] Capabilities par-client extraites du ticket positions.
    # Dict separe (plutot que de transformer clients en {ws: {name,caps}})
    # pour ne pas casser les lecteurs existants qui parcourent clients comme
    # un simple ws -> name.
    client_caps = {}   # websocket -> {"can_broadcast": bool}
    # [BROADCAST_ALL] Dernier timestamp ou un refus 0x04 a ete loggue, par ws.
    # Sert a limiter le spam de logs si un client malveillant tient la touche.
    _last_refusal_log = {}
    running = False
    bytes_total     = 0
    frames_total    = 0
    last_report_ts  = 0.0

    # === Stats debug crackling (ajout 25/05/2026) ===
    # Compteurs par nom de client (= pseudo). Cumulatifs depuis le demarrage
    # du service. La boucle _report_stats consulte et logge les deltas
    # toutes les 30s (Niveau A).
    rate_limit_drops_by_name: dict = {}     # {pseudo: count}
    broadcast_dead_by_name: dict   = {}     # {pseudo: count} - websocket mort
                                            # detecte pendant un broadcast OUT
    # Niveau B : throttle 30s pour le log "premier drop" par client.
    rate_limit_first_drop_logged: dict = {} # {pseudo: monotonic_ts}

state = State()

# [BROADCAST_ALL] Flag audio reserve a la PTT diffusion globale (tous canaux).
# Le serveur impose can_broadcast=True (lu dans le ticket) pour relayer
# une trame portant ce flag. Sinon la trame est jetee silencieusement (avec
# un log rate-limite pour aider au debug sans flooder).
FLAG_BROADCAST_ALL = 0x04
_BROADCAST_REFUSAL_LOG_INTERVAL_SEC = 60.0

# ---------------------------------------------
#  Serveur WebSocket
# ---------------------------------------------

async def handler(ws, ui):
    name = None
    # Recuperer l'IP pour les logs de refus
    try:
        peer_ip = ws.remote_address[0] if ws.remote_address else "?"
    except Exception:
        peer_ip = "?"

    # [P2 - lockout brute-force] Refus immediat si l'IP est bannie.
    if _auth_lockout.is_banned(peer_ip):
        ui.log(f"REFUSE audio : IP {peer_ip} bannie temporairement")
        try:
            await ws.close(code=1008, reason="banned")
        except Exception:
            pass
        return

    try:
        async for msg in ws:
            # Les messages sont soit du JSON (controle), soit du binaire (audio)
            if isinstance(msg, bytes):
                # [P2] Refuser toute trame binaire tant que le client ne
                # s'est pas authentifie. Avant, le serveur ignorait
                # silencieusement mais continuait a recevoir des octets, ce
                # qui permettait a un client non-auth de flooder le socket
                # et de fausser les stats. Maintenant : close direct.
                if name is None:
                    ui.log(f"REFUSE audio : trame binaire avant join "
                           f"(ip {peer_ip})")
                    await ws.close(code=1008, reason="not_authenticated")
                    return
                # [P5 - rate limiting] Jette les trames excedentaires d'un
                # client qui flood. On ne ferme pas la connexion : un pic
                # ponctuel ne doit pas couper un joueur legitime.
                # === Stats debug crackling (25/05/2026) ===
                # Auparavant silencieux : le client ignorait quand le serveur
                # jetait ses trames (typique en mode telephone avec double
                # envoi 0x03+0x00 = ~100 trames/s ; cf. _audio_rate plus haut
                # pour les valeurs courantes). On compte et on logue le
                # premier drop par client + un toutes les 30s (anti-spam).
                if not _audio_rate.allow(ws):
                    state.rate_limit_drops_by_name[name] = (
                        state.rate_limit_drops_by_name.get(name, 0) + 1
                    )
                    now_log = time.monotonic()
                    last_logged = state.rate_limit_first_drop_logged.get(
                        name, 0.0
                    )
                    if (now_log - last_logged) >= 30.0:
                        ui.log(
                            f"[RATE LIMIT] trame jetee de {name!r} "
                            f"(total drops: "
                            f"{state.rate_limit_drops_by_name[name]}, "
                            f"rate={_audio_rate.rate}, "
                            f"burst={_audio_rate.burst})"
                        )
                        state.rate_limit_first_drop_logged[name] = now_log
                    continue
                # [BROADCAST_ALL] Drop des trames flag 0x04 si l'emetteur
                # n'a pas la capability. Sans ce filtre, n'importe quel
                # client pourrait fabriquer une trame 0x04 et etre entendu
                # sur tous les canaux radio simultanement (cf filtrage
                # cote receveur dans circusvoip_core.py qui accepte 0x04
                # sans verifier le canal). On enforce ici parce que c'est
                # le seul endroit ou on a a la fois la trame ET l'identite
                # authentifiee de l'emetteur (via le ticket).
                if (len(msg) >= 1 and msg[0] == FLAG_BROADCAST_ALL
                        and not state.client_caps.get(ws, {}).get("can_broadcast")):
                    _maybe_log_broadcast_refusal(ws, name, peer_ip, ui)
                    continue
                # Trame audio : relayer a tous les autres clients
                state.bytes_total  += len(msg)
                state.frames_total += 1
                # Prefixer avec le nom de l'emetteur (2 octets taille + nom utf8)
                name_bytes = name.encode("utf-8")
                header     = len(name_bytes).to_bytes(2, "big") + name_bytes
                packet     = header + msg
                await _broadcast_binary(packet, exclude=ws)
            else:
                # Message JSON (controle)
                try:
                    data = json.loads(msg)
                except Exception:
                    continue

                if data.get("type") == "join":
                    # Relire le mdp du fichier (au cas ou il a ete modifie)
                    current_token = get_token()
                    token = data.get("token", "")
                    # [P1] compare_digest = comparaison en temps constant.
                    # `token != current_token` court-circuite des le 1er
                    # caractere different, ce qui permet en theorie une
                    # timing attack pour deduire le token caractere par
                    # caractere via mesure de latence reseau.
                    if not secrets.compare_digest(token, current_token):
                        # [P2] Comptabilise l'echec : 5 echecs en 60s -> ban.
                        banned_now = _auth_lockout.record_failure(peer_ip)
                        ui.log(f"REFUSE audio : token invalide "
                               f"(client: {data.get('name', '?')}, ip: {peer_ip})")
                        if banned_now:
                            ui.log(f"BAN audio : {peer_ip} pour 600s "
                                   f"(5 echecs en 60s)")
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

                    # [P4 - auth partagee] Exiger un ticket emis par le
                    # serveur positions. Empeche un client d'arriver sur
                    # l'audio sans etre passe par le serveur principal.
                    # verify_full() retourne aussi les capabilities embarquees
                    # dans le ticket (cf. AuthRegistry.issue(can_broadcast=)).
                    ticket = data.get("audio_ticket", "")
                    ticket_entry = _auth_registry.verify_full(ticket)
                    ticket_name = (ticket_entry or {}).get("name")
                    if ticket_name is None:
                        banned_now = _auth_lockout.record_failure(peer_ip)
                        ui.log(f"REFUSE audio : ticket invalide ou expire "
                               f"(client: {data.get('name', '?')}, ip: {peer_ip})")
                        if banned_now:
                            ui.log(f"BAN audio : {peer_ip} pour 600s "
                                   f"(5 echecs en 60s)")
                        try:
                            await ws.send(json.dumps({
                                "type": "error",
                                "reason": "invalid_ticket",
                                "message": "Ticket invalide - reconnecte-toi "
                                           "au serveur principal d'abord"
                            }))
                        except Exception:
                            pass
                        await ws.close(code=1008, reason="invalid_ticket")
                        return

                    # [P3] Cap du nombre de clients audio. Une fois plein,
                    # on refuse nettement avec un code WS clair plutot que
                    # de laisser saturer la machine.
                    if len(state.clients) >= MAX_AUDIO_CLIENTS:
                        ui.log(f"REFUSE audio : serveur plein "
                               f"({MAX_AUDIO_CLIENTS} clients) - ip {peer_ip}")
                        try:
                            await ws.send(json.dumps({
                                "type": "error",
                                "reason": "server_full",
                                "message": "Serveur audio plein"
                            }))
                        except Exception:
                            pass
                        await ws.close(code=1013, reason="server_full")
                        return

                    # [P2] Auth reussie : reset le compteur d'echecs de l'IP.
                    _auth_lockout.record_success(peer_ip)

                    # [P4] Nom autoritaire : on utilise le nom lie au ticket
                    # (emis par le serveur positions), pas celui que le
                    # client annonce. Ferme l'usurpation de pseudo cote audio.
                    name = ticket_name
                    state.clients[ws] = name
                    # [BROADCAST_ALL] Stocke la capability au moment du join.
                    # Pas de re-evaluation pendant la session : si l'admin
                    # revoke pendant qu'un broadcaster est connecte, la
                    # revocation s'applique au prochain ticket (apres son
                    # prochain join au serveur positions).
                    state.client_caps[ws] = {
                        "can_broadcast": bool(ticket_entry.get("can_broadcast")),
                    }
                    ui.log(f"JOIN audio : {name}  ({len(state.clients)} client(s))")
                    ui.refresh_clients()

                elif data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        ui.log(f"Erreur handler: {e}")
    finally:
        # [P5] Libere le bucket de rate limiting de ce client.
        _audio_rate.forget(ws)
        # [BROADCAST_ALL] Libere la capability stockee + l'etat du log
        # rate-limit pour ce ws. Sans pop, accumulation lente en memoire.
        state.client_caps.pop(ws, None)
        state._last_refusal_log.pop(ws, None)
        if ws in state.clients:
            n = state.clients.pop(ws)
            ui.log(f"LEAVE audio : {n}  ({len(state.clients)} client(s))")
            ui.refresh_clients()


def _maybe_log_broadcast_refusal(ws, name: str, peer_ip: str, ui):
    """Loggue le refus d'une trame 0x04 (PTT diffusion globale) d'un client
    non-broadcaster, en limitant a 1 log par minute par ws pour ne pas
    flooder si un client tient la touche en continu (50 trames/s)."""
    now = time.time()
    last = state._last_refusal_log.get(ws, 0.0)
    if now - last < _BROADCAST_REFUSAL_LOG_INTERVAL_SEC:
        return
    state._last_refusal_log[ws] = now
    ui.log(f"REFUSE broadcast_all : {name} (ip {peer_ip}) - pas de role broadcaster")

async def _broadcast_binary(data: bytes, exclude=None):
    """Envoie une trame audio a tous les clients sauf l'emetteur."""
    dead = []
    for ws in list(state.clients.keys()):
        if ws is exclude:
            continue
        try:
            await ws.send(data)
        except Exception:
            dead.append(ws)
    for ws in dead:
        # === Stats debug crackling (25/05/2026) ===
        # Avant : suppression silencieuse. Maintenant : on tracke le pseudo
        # et on incremente un compteur. Permet de detecter qu'un client
        # decroche (sa websocket lache pendant un broadcast) sans message
        # WS proprement clos. Le LEAVE qui suivra naturellement loggue
        # mais ici on capte AVANT (qui = combien d'essais ?).
        dead_name = state.clients.get(ws, "<unknown>")
        state.broadcast_dead_by_name[dead_name] = (
            state.broadcast_dead_by_name.get(dead_name, 0) + 1
        )
        state.clients.pop(ws, None)

async def _report_stats(ui):
    """Affiche les stats de bande passante toutes les 5s.

    Modif 25/05/2026 : tous les 30s (= 6 ticks de 5s), log additionnel
    [AUDIO STATS DEBUG] avec les compteurs de drops rate limiter et
    de broadcasts vers clients morts (par pseudo). Permet d'identifier
    si des trames sont jetees silencieusement pendant un test groupe.
    """
    tick = 0
    # Snapshot pour calcul de delta sur 30s
    prev_rl_drops = {}
    prev_bc_dead  = {}
    while state.running:
        await asyncio.sleep(5)
        # Mettre a jour les stats meme si pas de trafic, sinon le compteur de
        # clients reste fige quand tout le monde se deconnecte.
        if state.bytes_total > 0:
            kbs    = state.bytes_total / 5 / 1024
            frames = state.frames_total / 5
        else:
            kbs    = 0.0
            frames = 0.0
        ui.update_stats(kbs, frames, len(state.clients))
        state.bytes_total  = 0
        state.frames_total = 0

        # Toutes les 30s = 6 ticks : log debug audio enrichi.
        tick += 1
        if tick >= 6:
            tick = 0
            # Calcul des deltas sur 30s pour chaque client.
            rl_delta = {}
            for name, total in state.rate_limit_drops_by_name.items():
                delta = total - prev_rl_drops.get(name, 0)
                if delta > 0:
                    rl_delta[name] = delta
                prev_rl_drops[name] = total
            bc_delta = {}
            for name, total in state.broadcast_dead_by_name.items():
                delta = total - prev_bc_dead.get(name, 0)
                if delta > 0:
                    bc_delta[name] = delta
                prev_bc_dead[name] = total

            # Log AUDIO STATS DEBUG. Si tout est OK (aucun drop, aucun dead),
            # on log quand meme pour avoir un signe de vie dans le log
            # (rassure : oui le mecanisme tourne, et NON aucun drop).
            n_clients = len(state.clients)
            client_names = ",".join(sorted(state.clients.values())) or "(aucun)"
            if rl_delta or bc_delta:
                # Anomalies detectees pendant les 30 dernieres secondes
                parts = []
                if rl_delta:
                    parts.append("rate_drops=" + ",".join(
                        f"{n}:{c}" for n, c in sorted(rl_delta.items())
                    ))
                if bc_delta:
                    parts.append("broadcast_dead=" + ",".join(
                        f"{n}:{c}" for n, c in sorted(bc_delta.items())
                    ))
                ui.log(
                    f"[AUDIO STATS DEBUG] clients={n_clients} ({client_names}) | "
                    + " | ".join(parts)
                )
            else:
                ui.log(
                    f"[AUDIO STATS DEBUG] clients={n_clients} ({client_names}) | "
                    f"rate_drops=0 | broadcast_dead=0 | RAS"
                )

async def _serve(ui):
    state.running = True

    # ─────────────────────────────────────────────
    #  [P1 - TLS] Chiffrement de la connexion audio (auto)
    # ─────────────────────────────────────────────
    # Le serveur audio ecoute en wss:// (chiffre), avec le MEME certificat
    # que le serveur positions (cert.pem / key.pem partages dans le meme
    # dossier). Si circusvoip_server.py a deja demarre, le cert existe ;
    # sinon ensure_self_signed_cert le cree.
    #
    # En cas d'echec de generation, le serveur audio REFUSE de demarrer
    # plutot que d'accepter en clair (la VoIP serait alors lisible par
    # quiconque ecoute le reseau).
    from circusvoip_security import ensure_self_signed_cert, build_ssl_context
    _base_dir = Path(__file__).resolve().parent
    _cert_file = _base_dir / "cert.pem"
    _key_file = _base_dir / "key.pem"
    _ok, _detail = ensure_self_signed_cert(_cert_file, _key_file, common_name="circusvoip-audio")
    if not _ok:
        ui.log(f"[FATAL] Impossible de generer le certificat TLS. Detail : {_detail}")
        state.running = False
        return
    _ssl_ctx = build_ssl_context(str(_cert_file), str(_key_file))
    ui.log(f"TLS active : serveur audio en wss:// (cert {_detail})")

    # Rend les rejets de handshake non-WebSocket (scans de ports) lisibles :
    # une ligne WARNING au lieu d'un traceback complet dans journalctl.
    _install_ws_handshake_log_filter()

    async with websockets.serve(
        lambda ws: handler(ws, ui),
        "0.0.0.0",
        AUDIO_PORT,
        ssl=_ssl_ctx,
        max_size=2 * 1024 * 1024   # trames audio pas enormes mais marge de securite
    ):
        ui.log(f"Serveur AUDIO demarre sur port {AUDIO_PORT}")
        # [P1+] On n'affiche plus le token complet en console. Il reste
        # visible dans l'UI Tkinter (saisi par l'admin) et dans le fichier
        # de config. Eviter qu'il se retrouve dans journalctl.
        masked = SERVER_TOKEN[:4] + "***" if len(SERVER_TOKEN) >= 4 else "***"
        ui.log(f"TOKEN : {masked}  (visible complet dans la config)")
        asyncio.create_task(_report_stats(ui))
        await asyncio.Future()

def _run_server(ui):
    try:
        asyncio.run(_serve(ui))
    except Exception as e:
        ui.log(f"Serveur arrete: {e}")
        state.running = False

# ---------------------------------------------
#  UI
# ---------------------------------------------

class ServerUI:
    def __init__(self):
        # Forcer un AppUserModelID distinct sur Windows AVANT tk.Tk().
        # Permet a Windows d'utiliser notre icone StarCircus_Server.ico
        # dans la taskbar au lieu de l'icone Python par defaut.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CircusVOIP.AudioServer.0.1"
            )
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("CircusVOIP - Audio Server 0.1")
        self.root.configure(bg=BG)
        # Icone : meme que le serveur positions (bandeau bleu SERVER) car
        # l'audio server est un composant du serveur, pas un produit distinct.
        try:
            from pathlib import Path as _Path
            _ico_path = _Path(__file__).resolve().parent / "StarCircus_Server.ico"
            if _ico_path.exists():
                self.root.iconbitmap(default=str(_ico_path))
                self.root.wm_iconbitmap(str(_ico_path))
        except Exception:
            pass
        self.root.geometry("680x480")
        self.root.minsize(600, 360)
        self._closing = False   # passe a True dans _on_close pour bloquer les updates UI
        self._build_ui()
        threading.Thread(target=_run_server, args=(self,), daemon=True).start()
        # Handler de fermeture propre
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    def _on_close(self):
        """Arret propre du serveur audio quand l'utilisateur ferme la fenetre."""
        self._closing = True
        state.running = False
        try:
            self.root.destroy()
        except Exception:
            pass
        # Forcer la sortie : les threads daemon (asyncio serveur) ne se terminent
        # pas toujours proprement sur Python 3.14
        import os
        os._exit(0)

    def _safe_after(self, callback):
        """Planifie un callback UI dans le main thread, en ignorant les erreurs
        si la fenetre est en train d'etre detruite."""
        if getattr(self, "_closing", False):
            return
        try:
            self.root.after(0, callback)
        except (RuntimeError, tk.TclError):
            pass

    def _build_ui(self):
        # Header
        header = tk.Frame(self.root, bg=BG, pady=8)
        header.pack(fill="x", padx=12)
        tk.Label(header, text="CircusVOIP Audio Server", bg=BG, fg=PURPLE,
                 font=("Courier", 13, "bold")).pack(side="left")
        self._lbl_port = tk.Label(header, text=f"Port {AUDIO_PORT}", bg=BG,
                                  fg=GREEN, font=("Courier", 10))
        self._lbl_port.pack(side="right")
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")

        # Body
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=8)

        # Stats
        stats = tk.Frame(body, bg=BG_PANEL, padx=10, pady=8)
        stats.pack(fill="x", pady=(0, 8))
        self._section(stats, "STATS")
        sf = tk.Frame(stats, bg=BG_PANEL)
        sf.pack(fill="x", pady=4)
        self._lbl_clients = tk.Label(sf, text="Clients : 0", bg=BG_PANEL, fg=BLUE,
                                     font=("Courier", 10, "bold"))
        self._lbl_clients.pack(side="left", padx=(0, 20))
        self._lbl_kbs = tk.Label(sf, text="Debit : 0 KB/s", bg=BG_PANEL, fg=ORANGE,
                                 font=("Courier", 10))
        self._lbl_kbs.pack(side="left", padx=(0, 20))
        self._lbl_fps = tk.Label(sf, text="Frames/s : 0", bg=BG_PANEL, fg=GREEN,
                                 font=("Courier", 10))
        self._lbl_fps.pack(side="left")

        # Clients connectes
        cf = tk.Frame(body, bg=BG_PANEL)
        cf.pack(fill="x", pady=(0, 8))
        self._section(cf, "CLIENTS CONNECTES")
        self._clients_frame = tk.Frame(cf, bg=BG_PANEL)
        self._clients_frame.pack(fill="x", padx=8, pady=4)
        self._lbl_empty = tk.Label(self._clients_frame, text="Aucun client",
                                   bg=BG_PANEL, fg=MUTED, font=("Courier", 9))
        self._lbl_empty.pack(anchor="w")

        # Logs
        lf = tk.Frame(body, bg=BG_PANEL)
        lf.pack(fill="both", expand=True)
        self._section(lf, "LOGS")
        self._log_box = tk.Text(lf, bg=BG_ROW, fg=TEXT,
                                font=("Courier", 9), relief="flat", bd=4, wrap="word")
        self._log_box.pack(fill="both", expand=True, padx=8, pady=4)
        self._log_box.config(state="disabled")

    def _section(self, parent, title):
        tk.Label(parent, text=title, bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 8, "bold"), anchor="w").pack(fill="x", padx=2)

    def log(self, msg: str):
        def _do():
            ts = time.strftime("%H:%M:%S")
            self._log_box.config(state="normal")
            self._log_box.insert("end", f"[{ts}] {msg}\n")
            self._log_box.see("end")
            self._log_box.config(state="disabled")
        self._safe_after(_do)

    def refresh_clients(self):
        def _do():
            for w in self._clients_frame.winfo_children():
                w.destroy()
            if not state.clients:
                self._lbl_empty = tk.Label(self._clients_frame, text="Aucun client",
                                           bg=BG_PANEL, fg=MUTED, font=("Courier", 9))
                self._lbl_empty.pack(anchor="w")
            else:
                for ws, name in state.clients.items():
                    tk.Label(self._clients_frame, text=f"* {name}",
                             bg=BG_PANEL, fg=GREEN,
                             font=("Courier", 9, "bold"), anchor="w").pack(fill="x")
        self._safe_after(_do)

    def update_stats(self, kbs: float, fps: float, clients: int):
        def _do():
            self._lbl_clients.config(text=f"Clients : {clients}")
            self._lbl_kbs.config(text=f"Debit : {kbs:.1f} KB/s")
            self._lbl_fps.config(text=f"Frames/s : {fps:.1f}")
        self._safe_after(_do)

# ---------------------------------------------
#  Main
# ---------------------------------------------

class _HeadlessUI:
    """Stub UI pour le mode headless (deploiement sur VPS sans display).
    Implemente les memes methodes que ServerUI mais affiche tout dans la
    console (stdout) au lieu d'une fenetre Tkinter.

    Modif 25/05/2026 : tous les logs sont aussi ecrits dans un fichier
    dedie /var/log/circusvoip-audio/audio_YYYYMMDD_HHMMSS.log pour
    debug ulterieur (fallback silencieux si le dossier n'est pas
    accessible en ecriture)."""

    def log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        _audio_log_write(line)

    def refresh_clients(self):
        # Pas d'affichage de liste en headless (les events sont logges)
        pass

    def update_stats(self, kbs: float, frames: int, n_clients: int):
        ts = datetime.now().strftime("%H:%M:%S")
        line = (f"[{ts}] [STATS] {n_clients} client(s) | "
                f"{frames} trames/s | {kbs:.1f} kB/s")
        print(line, flush=True)
        _audio_log_write(line)


def _run_headless():
    """Lance l'audio server sans UI Tkinter."""
    masked = SERVER_TOKEN[:4] + "***" if len(SERVER_TOKEN) >= 4 else "***"
    print("=" * 60)
    print("CircusVOIP Audio Server - mode headless")
    print(f"Port audio   : {AUDIO_PORT}")
    print(f"Token        : {masked}  (visible complet dans la config)")
    print(f"Cap clients  : {MAX_AUDIO_CLIENTS}")
    # Ouverture du fichier log (debug crackling, ajout 25/05/2026).
    # Si echec : on continue sur stdout uniquement.
    if _open_audio_log_file():
        print(f"Log fichier  : {_log_file_path}")
    else:
        print("Log fichier  : DESACTIVE (echec ouverture, stdout uniquement)")
    print("=" * 60)
    print("Logs (Ctrl+C pour arreter) :")
    print()
    headless_ui = _HeadlessUI()
    try:
        asyncio.run(_serve(headless_ui))
    except KeyboardInterrupt:
        print("\n[INFO] Arret demande par l'utilisateur (Ctrl+C)")


if __name__ == "__main__":
    import sys
    if "--headless" in sys.argv:
        _run_headless()
    else:
        try:
            ServerUI()
        except Exception as e:
            import traceback
            traceback.print_exc()
            input("Appuyez sur Entree pour fermer...")
