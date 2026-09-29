# -*- coding: utf-8 -*-
# =============================================
#  CircusVOIP Admin
# =============================================
# Console d'administration distante pour CircusVOIP Server.
# Se connecte au serveur via WebSocket port 8888 avec un message
# auth_admin + token admin (different du token joueur).
#
# Permet de :
#   - Voir les joueurs connectes (positions, canaux, profils)
#   - Gerer les canaux radio (creer, renommer, supprimer)
#   - Gerer les profils (creer, renommer, supprimer)
#   - Assigner / retirer des profils aux joueurs
#   - Activer / desactiver le mode anonyme
#   - Kicker un joueur
#   - Voir les logs serveur en temps reel
#
# Lancement : py -3 circusvoip_admin.py
# Deps      : pip install websockets
# =============================================

import asyncio
import json
import queue
import threading
import time

# DPI awareness avant tkinter (ecrans haute resolution)
try:
    import circusvoip_dpi
    circusvoip_dpi.enable_dpi_awareness()
except Exception:
    pass

import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont, simpledialog, messagebox, ttk

import websockets

# ---------------------------------------------
#  Config (persiste IP + token admin entre lancements)
# ---------------------------------------------

_BASE_DIR   = Path(__file__).resolve().parent
CONFIG_FILE = _BASE_DIR / "circusvoip_admin_config.json"

# [RELEASE 29/09/2026] Version de l'admin : LUE, jamais ecrite en dur.
#
# Elle etait "0.1" en dur dans root.title(), et elle y est restee
# pendant tout le developpement de 0.2, 0.3 et 0.4. Y remettre "0.4.0"
# en dur ne corrigerait rien : ca rebranche le meme piege pour 0.5.
#
# Source unique : circusvoip_version.json, le fichier que build_update
# incremente deja. Le client le lit de la meme facon
# (_load_version_info) ; l'admin n'introduit donc aucun numero de plus
# dans le projet.
#
# Cherche a cote du script PUIS dans le dossier parent : l'admin vit
# dans "fichiers serveurs\", le version.json a la racine du dev.
#
# Introuvable -> chaine VIDE, et l'interface n'affiche AUCUNE version.
# C'est voulu : sur la machine d'administration, ou seul l'admin est
# copie, mieux vaut ne rien annoncer qu'annoncer un numero faux -- c'est
# exactement ce qu'a fait "0.1" pendant trois versions.
#
# Le canal et le build sont repris tels quels s'ils sont la : un admin
# lance depuis le dossier de dev affiche "0.4.0 Alpha Build 80", comme
# le titre du client, ce qui evite de confondre les deux fenetres
# pendant un test.
def _lire_version() -> str:
    for dossier in (_BASE_DIR, _BASE_DIR.parent):
        try:
            # utf-8-sig : tolere le BOM que l'ISPP laisse parfois dans le
            # fichier, meme motif que cote client.
            with open(dossier / "circusvoip_version.json", "r",
                      encoding="utf-8-sig") as f:
                d = json.load(f)
        except Exception:
            continue
        v = str(d.get("version") or "").strip()
        if not v:
            continue
        canal = str(d.get("channel") or "").strip()
        if canal and canal.lower() != "stable":
            return f"{v} {canal.capitalize()} Build {int(d.get('build') or 0)}"
        return v
    return ""


VERSION = _lire_version()
# [PORTS CONFIGURABLES 30/07/2026] Valeur de REPLI uniquement. L'admin
# partage le port du serveur de positions, qui n'est plus fige a 8888
# depuis le 26/07 (serveur de dev sur 5746). L'adresse se saisit donc
# sous la forme "ip:port" comme dans le client ; sans port, on retombe
# sur 8888.
# Piege constate : l'admin n'avait pas ete revu lors du chantier des
# ports et tapait dans le vide, exactement comme le mannequin reste sur
# 8888. Verifier TOUS les composants qui se connectent, pas seulement
# ceux qu'on modifie.
SERVER_PORT = 8888  # repli si l'adresse ne precise pas de port

# ---------------------------------------------
#  Theme (identique au serveur)
# ---------------------------------------------

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


def _split_host_port(saisie: str, port_defaut: int) -> tuple[str, int]:
    """Decoupe "ip:port" en (hote, port). Retombe sur port_defaut si la
    saisie ne precise rien, ou si le port est invalide -- refuser de se
    connecter sur une faute de frappe serait pire que le probleme.

    IPv6 non gere : la saisie contient alors plusieurs ":" et on la rend
    telle quelle, comme le fait le client.
    """
    s = (saisie or "").strip()
    if not s or s.count(":") != 1:
        return s, port_defaut
    host, _, port = s.partition(":")
    host = host.strip()
    try:
        p = int(port.strip())
        if not (1 <= p <= 65535):
            return host, port_defaut
    except Exception:
        return host, port_defaut
    return host, p


def _load_cfg() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cfg(cfg: dict):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f"[CONFIG] Echec sauvegarde : {e}")


# ---------------------------------------------
#  Etat global
# ---------------------------------------------

class State:
    # Connexion
    server_ip       = "127.0.0.1"
    admin_token     = ""
    ws              = None        # websockets connection
    ws_loop         = None        # event loop du thread WS
    connected       = False
    # Etat repliquant celui du serveur (recu via admin_welcome + push events)
    channels        = []          # list[str]
    profiles        = []          # list[str]
    players         = {}          # {name: {pos, channel, profile, helmet_on, prox_short}}
    anonymous_mode  = False
    server_token    = ""          # token joueur (pour l'afficher dans l'UI admin)
    # [OBS1] Historique des echantillons pour la moyenne glissante.
    # Le serveur pousse toutes les 5s ; 12 echantillons = 60s. Sans ca
    # l'affichage saute (mesure : 203 -> 674 kbit/s d'un tick a l'autre)
    # et n'est pas lisible en direct.
    stats_hist     = {"pos_in": [], "pos_out": [], "aud_in": [], "aud_out": []}
    STATS_WINDOW   = 12
    # Dernier echantillon de debit pousse par le serveur (5s).
    # {"pos": {...}, "audio": {...}} ; audio vide = serveur audio muet
    # ou fichier de stats perime (cf. _read_audio_stats cote serveur).
    stats           = {}

state = State()


def _ws_send_safe(payload: dict) -> bool:
    """Envoi WS thread-safe : appelable depuis le thread Tkinter.

    [21/09/2026] Cette fonction BLOQUE le thread Tk jusqu'a une seconde,
    le temps que la boucle asyncio envoie. C'est sans consequence tant
    que la boucle ne depend jamais du thread Tk -- et c'est la que ca
    cassait : _handle_message (qui tourne DANS la boucle) appelait
    directement show_annuaire() et afficher_chefs(). Tkinter fait alors
    executer chaque appel de widget par le thread Tk et attend qu'il
    l'ait fait.

    Les deux fils s'attendaient donc l'un l'autre : le thread Tk dans
    fut.result(), la boucle dans son premier appel de widget. Rien ne
    bougeait jusqu'a l'expiration du delai d'une seconde, l'envoi etait
    compte comme rate, _envoyer_avec_reprise recommencait... Plusieurs
    secondes de gel a CHAQUE connexion : c'est la que partent, coup sur
    coup, annuaire_list, urgence_chefs et annonces_admin_list pendant
    que les reponses arrivent.

    Regle : depuis _handle_message, un widget ne se touche QUE par
    ui._safe_after(...). Les methodes refresh_* le font d'elles-memes.
    """
    if not state.connected or state.ws is None or state.ws_loop is None:
        return False
    try:
        msg = json.dumps(payload)
        fut = asyncio.run_coroutine_threadsafe(state.ws.send(msg), state.ws_loop)
        try:
            fut.result(timeout=1.0)
            return True
        except Exception:
            return False
    except Exception:
        return False


# ---------------------------------------------
#  Client WebSocket (thread separe)
# ---------------------------------------------

async def _ws_client(ui):
    # [P1 - TLS] Connexion CHIFFREE en wss:// (alignee avec le client v0.1.1+).
    # Le serveur positions ecoute uniquement en wss:// depuis v0.1.1 (cert
    # auto-signe genere automatiquement). build_client_ssl_context_insecure()
    # construit un contexte SSL qui accepte le cert sans verifier l'identite
    # (chiffrement OK, pas d'auth stricte du cert). L'auth admin se fait
    # ensuite via le token dans le message "auth_admin" (compare_digest
    # cote serveur).
    from circusvoip_security import build_client_ssl_context_insecure
    _host, _port = _split_host_port(state.server_ip, SERVER_PORT)
    uri = f"wss://{_host}:{_port}"
    _ssl_ctx = build_client_ssl_context_insecure()
    try:
        async with websockets.connect(uri, ssl=_ssl_ctx) as ws:
            state.ws        = ws
            state.ws_loop   = asyncio.get_event_loop()
            state.connected = True
            ui.set_status(True, "Authentification...")
            # Envoyer auth_admin
            await ws.send(json.dumps({
                "type": "auth_admin",
                "token": state.admin_token,
            }))
            async for raw in ws:
                try:
                    data = json.loads(raw)
                except Exception:
                    continue
                _handle_message(ui, data)
    except websockets.exceptions.ConnectionClosed as e:
        if getattr(e, "code", None) == 1008:
            ui.set_status(False, "Token admin invalide")
        else:
            ui.set_status(False, "Connexion fermee")
    except Exception as e:
        ui.set_status(False, f"Erreur : {str(e)[:50]}")
    finally:
        state.connected = False
        state.ws        = None
        state.ws_loop   = None
        ui.set_status(False, "Deconnecte")


def _normalize_profiles_list(raw) -> list:
    """Normalise la liste de profils recue du serveur en list[dict].
    Accepte les 2 formats :
      - list[str] : vieux serveur, ex ["Pilote", "Mecano"].
        On cree des dicts avec permissions a False.
      - list[dict] : nouveau serveur, ex
        [{"name": "Pilote", "soundboard_allowed": true}].
    Retourne toujours list[dict]."""
    result = []
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, str):
            n = item.strip()
            if n:
                # Cree dict avec permissions defaults (toutes False).
                result.append({
                    "name": n,
                    "soundboard_allowed": False,
                })
        elif isinstance(item, dict):
            n = (item.get("name") or "").strip()
            if not n:
                continue
            d = {"name": n}
            # Recopie les cles connues (False si absentes).
            d["soundboard_allowed"] = bool(item.get("soundboard_allowed", False))
            result.append(d)
    return result


def _profile_names() -> list:
    """Retourne la liste des noms de profils (list[str]) extraite de
    state.profiles (qui est list[dict])."""
    return [p["name"] for p in state.profiles if isinstance(p, dict) and p.get("name")]


def _push_sample(key: str, value: float):
    """[OBS1] Ajoute un echantillon a la fenetre glissante."""
    buf = state.stats_hist.setdefault(key, [])
    buf.append(float(value))
    if len(buf) > state.STATS_WINDOW:
        del buf[:-state.STATS_WINDOW]


def _avg(key: str) -> float:
    """[OBS1] Moyenne de la fenetre, 0.0 si pas encore d'echantillon."""
    buf = state.stats_hist.get(key) or []
    return (sum(buf) / len(buf)) if buf else 0.0


def _handle_message(ui, data: dict):
    """Traite un message recu du serveur."""
    msg_type = data.get("type")

    if msg_type == "annuaire_list":
        # [DISCORD 30/07/2026] Reponse a annuaire_list, ET push spontane
        # apres une suppression : la fenetre se rafraichit toute seule,
        # sa vue ne peut donc pas diverger du serveur.
        entries = data.get("entries", [])
        # [URGENCE 15/08/2026] L'annuaire alimente aussi les listes de
        # chefs. On le memorise meme si la fenetre annuaire n'est pas
        # ouverte : c'est la meme donnee, inutile de la redemander.
        try:
            ui._chef_annuaire = list(entries)
            # Meme raison que pour admin_welcome : on sort de la boucle
            # asyncio avant d'emettre. _safe_after est le patron deja
            # utilise dans ce fichier pour repasser sur le thread Tk.
            ui._safe_after(ui.demander_chefs)
        except Exception:
            pass
        # [21/09/2026] Via _safe_after, comme tout ce qui touche un
        # widget depuis ce fil. L'appel DIRECT faisait geler l'admin
        # plusieurs secondes a la connexion -- cf. _ws_send_safe.
        ui._safe_after(lambda: ui.show_annuaire(entries))
        return

    if msg_type == "urgence_chefs":
        ui._safe_after(lambda: ui.afficher_chefs(data))
        return

    if msg_type == "bans_list":
        bans = list(data.get("bans") or [])
        ui._safe_after(lambda: ui.show_bans(bans))
        return

    if msg_type == "logs_list":
        fichiers = list(data.get("fichiers") or [])
        ui._safe_after(lambda: ui.show_fichiers_logs(fichiers))
        return

    if msg_type in ("logs_morceau", "logs_fin"):
        # [LOGS ADMIN 21/09/2026] Ecrit ICI, dans le fil reseau : ce sont
        # des ecritures disque, pas des widgets. Seul l'affichage de la
        # progression repasse par _safe_after.
        try:
            ui._recevoir_log(data)
        except Exception as e:
            ui.add_log(f"[LOGS] reception KO : {e!r}", RED)
        return

    if msg_type == "travail_admin":
        # [MODERATION 21/09/2026] Meme patron que les annonces.
        missions = list(data.get("missions") or [])
        ui._safe_after(lambda: ui.show_missions(missions))
        return

    if msg_type == "annonces_admin":
        # [ANNONCES 21/09/2026] Reponse a annonces_admin_list ET poussee
        # spontanee apres chaque publication / retrait : l'onglet se met
        # a jour tout seul, comme l'annuaire.
        annonces = list(data.get("annonces") or [])
        ui._safe_after(lambda: ui.show_annonces(annonces))
        return

    if msg_type == "admin_welcome":
        # Etat initial
        state.channels        = list(data.get("channels", []))
        # v0.2 alpha 035 : profiles peut etre list[str] (vieux serveur)
        # ou list[dict] avec permissions (nouveau serveur). Normalise
        # toujours en list[dict].
        state.profiles        = _normalize_profiles_list(data.get("profiles", []))
        state.anonymous_mode  = bool(data.get("anonymous_mode", False))
        state.server_token    = data.get("server_token", "")
        # [07/09/2026] Toute nouvelle connexion repart MASQUE. Sans ca,
        # un oeil ouvert une fois le restait pour la session suivante :
        # on rouvrait l'admin avec le mot de passe en clair a l'ecran
        # sans l'avoir demande.
        ui._show_srv_token = False
        state.players         = {}
        for p in data.get("players", []):
            state.players[p["name"]] = {
                "pos":         p.get("pos"),
                "channel":     p.get("channel"),
                "profile":     p.get("profile"),
                "helmet_on":   bool(p.get("helmet_on", False)),
                "prox_short":  bool(p.get("prox_short", False)),
                "sc_online":   bool(p.get("sc_online", True)),
            }
        ui.set_status(True, "Connecte")
        ui.refresh_all()
        # [URGENCE 15/08/2026] L'annuaire est demande des la connexion :
        # il alimente les listes de chefs, qui doivent etre lisibles sans
        # que l'admin ait a ouvrir quoi que ce soit. Sa reponse declenche
        # a son tour la demande des chefs.
        #
        # Via root.after et non directement : _handle_message tourne DANS
        # la boucle asyncio, et _ws_send_safe y planifie une coroutine
        # puis attend son resultat une seconde. Appelee depuis la boucle
        # elle-meme, elle attend donc un travail que la boucle ne peut
        # pas executer -- elle est bloquee a attendre. Resultat : "WS pas
        # pret" systematique, une seconde apres chaque connexion.
        # Un court delai en plus du changement de thread : au moment de
        # admin_welcome, la connexion vient d'aboutir et l'envoi peut
        # encore echouer.
        # [22/09/2026] Les root.after(...) sont eux-memes deposes via
        # _safe_after : appeles d'ici, depuis le fil reseau, ils
        # bloquaient comme n'importe quel appel Tk.
        ui._safe_after(lambda: ui.root.after(400, lambda: ui._envoyer_avec_reprise(
            {"cmd": "annuaire_list"})))
        # [ANNONCES 21/09/2026] Meme patron, decale : deux envois dans la
        # meme milliseconde n'apportent rien et compliquent la lecture
        # du journal en cas d'echec.
        ui._safe_after(lambda: ui.root.after(700, lambda: ui._envoyer_avec_reprise(
            {"cmd": "annonces_admin_list"})))
        ui._safe_after(lambda: ui.root.after(1000, lambda: ui._envoyer_avec_reprise(
            {"cmd": "travail_admin_list"})))

    elif msg_type == "server_token":
        # [08/09/2026] Le serveur repousse le mot de passe joueur apres
        # une modification. Avant, il n'arrivait que dans admin_welcome :
        # l'ecran gardait l'ancienne valeur jusqu'a la reconnexion, et on
        # croyait la modification perdue.
        #
        # L'etat masque/revele n'est PAS reinitialise ici : c'est une
        # modification volontaire de l'admin, pas une nouvelle session.
        # Le remasquer d'office cacherait la valeur au moment ou il veut
        # justement verifier ce qu'il vient de saisir.
        state.server_token = data.get("server_token", "")
        ui._safe_after(ui._peindre_mdp_serveur)

    elif msg_type == "stats":
        # [OBS1] Push periodique de debit (positions + audio relaye).
        state.stats = {"pos": data.get("pos", {}), "audio": data.get("audio", {})}
        # [OBS1] Alimente la moyenne glissante. L'audio n'est ajoute que
        # si le pont a renvoye des donnees fraiches : sinon un serveur
        # audio arrete tirerait la moyenne vers zero au lieu d'afficher
        # "n/a".
        _p = state.stats["pos"] or {}
        if _p:
            _push_sample("pos_in",  _p.get("kbps_in", 0.0))
            _push_sample("pos_out", _p.get("kbps_out", 0.0))
        _a = state.stats["audio"] or {}
        if _a:
            _push_sample("aud_in",  _a.get("kbps_in", 0.0))
            _push_sample("aud_out", _a.get("kbps_out", 0.0))
        ui.refresh_stats()

    elif msg_type == "log":
        # Log serveur push
        ui.add_log(data.get("msg", ""), data.get("color", TEXT), data.get("ts"))

    elif msg_type == "error":
        # Erreur d'auth notamment
        reason = data.get("reason", "")
        msg = data.get("message", reason)
        ui.set_status(False, f"Refuse : {msg}")

    elif msg_type == "admin_response":
        # Reponse a une commande admin
        ok = bool(data.get("ok"))
        cmd = data.get("cmd", "?")
        reason = data.get("reason", "")
        if not ok:
            ui.add_log(f"[ADMIN] {cmd} : echec ({reason})", RED)

    elif msg_type == "join":
        n = data.get("name")
        if n:
            state.players[n] = {
                "pos":        None,
                "channel":    data.get("channel"),
                "profile":    data.get("profile"),
                "helmet_on":  False,
                "prox_short": bool(data.get("prox_short", False)),
                "sc_online":  True,  # joueur qui (re)joint = par defaut SC suppose actif
            }
            ui.refresh_players()

    elif msg_type == "leave":
        n = data.get("name")
        state.players.pop(n, None)
        ui.refresh_players()

    elif msg_type == "sc_offline":
        # Un joueur a ferme Star Citizen (OCR inactif). On garde sa connexion
        # serveur ouverte mais on l'affiche comme hors-jeu.
        n = data.get("name")
        if n in state.players:
            state.players[n]["sc_online"] = False
            ui.refresh_players()

    elif msg_type == "sc_online":
        # Un joueur a relance Star Citizen (OCR actif a nouveau)
        n = data.get("name")
        if n in state.players:
            state.players[n]["sc_online"] = True
            ui.refresh_players()

    elif msg_type == "pos":
        n = data.get("name")
        if n in state.players:
            state.players[n]["pos"] = data.get("pos")
            ui.refresh_players()

    elif msg_type == "player_channel":
        n = data.get("name")
        if n in state.players:
            state.players[n]["channel"] = data.get("channel")
            ui.refresh_players()

    elif msg_type == "player_profile":
        n = data.get("name")
        if n in state.players:
            state.players[n]["profile"] = data.get("profile")
            ui.refresh_players()

    elif msg_type == "helmet":
        n = data.get("name")
        if n in state.players:
            state.players[n]["helmet_on"] = bool(data.get("helmet_on", False))

    elif msg_type == "player_prox_short":
        n = data.get("name")
        if n in state.players:
            state.players[n]["prox_short"] = bool(data.get("active", False))

    elif msg_type == "channels_list":
        # Reception du nouveau format (liste de strings)
        raw = data.get("channels", [])
        state.channels = [c if isinstance(c, str) else c.get("name", "") for c in raw]
        state.channels = [c for c in state.channels if c]
        ui.refresh_channels()

    elif msg_type == "profiles_list":
        # v0.2 alpha 035 : nouveau format possible : list[dict] avec
        # permissions (envoye par le serveur quand on est admin auth).
        # Ancien format possible : list[str] (vieux serveur). On
        # normalise toujours en list[dict] cote admin.
        raw = data.get("profiles", [])
        state.profiles = _normalize_profiles_list(raw)
        ui.refresh_profiles()

    elif msg_type == "anonymous_mode":
        state.anonymous_mode = bool(data.get("active", False))
        ui.refresh_anonymous()


def _run_ws(ui):
    """Lance le client WS dans un thread."""
    asyncio.run(_ws_client(ui))


# ---------------------------------------------
#  UI
# ---------------------------------------------

# ---------------------------------------------
#  Annuaire : mise en forme d'une fiche
# ---------------------------------------------
#
# [ANNUAIRE 07/09/2026] `list_accounts` transmettait deja tout -- seuls
# les hashs de jeton sont retires. Trois champs sur sept etaient
# affiches. Colonnes a largeur fixe (Courier) : les valeurs se lisent
# verticalement, et une chaine unique par ligne coute un seul widget au
# lieu de cinq.

# Largeurs en CARACTERES. Courier est a chasse fixe, donc une largeur
# en caracteres se traduit exactement en pixels -- c'est ce qui permet
# aux colonnes de s'aligner d'une ligne a l'autre sans grille partagee.
_ANNUAIRE_GOUTTIERE = 8   # pixels entre deux colonnes

_ANNUAIRE_COLS = (
    ("NUMERO",   9),
    ("PSEUDO",  18),
    ("DISCORD", 20),
    ("ROLE",    12),
    ("METIERS", 24),
    ("VU LE",   13),
)


def _age_court(ts) -> str:
    """Anciennete lisible. La precision suit l'echelle.

    A trois semaines, l'heure exacte n'informe personne ; a deux
    minutes, elle dit si le joueur vient de partir.
    """
    try:
        ts = float(ts or 0)
    except Exception:
        return "jamais"
    if ts <= 0:
        return "jamais"
    d = max(0.0, time.time() - ts)
    if d < 60:
        return "a l'instant"
    if d < 3600:
        return f"il y a {int(d // 60)} min"
    if d < 86400:
        return f"il y a {int(d // 3600)} h"
    if d < 30 * 86400:
        return f"il y a {int(d // 86400)} j"
    return datetime.fromtimestamp(ts).strftime("%d/%m/%Y")


def _cellules_annuaire(e: dict) -> tuple:
    """Les six valeurs d'une fiche, dans l'ordre des colonnes.

    [ANNUAIRE 07/09/2026] Rendu en CELLULES et non en une chaine unique
    a largeur fixe. Avec une chaine, une valeur plus longue que sa
    colonne ne tronque pas : elle DECALE tout ce qui suit. Constate sur
    "service:Mannequin_01", 20 caracteres pour une colonne de 18 -- les
    colonnes ROLE, METIERS et VU LE partaient de travers sur ces deux
    lignes seulement.

    Rien n'est tronque : une valeur trop longue passe a la ligne DANS sa
    cellule (wraplength). La ligne grandit, les colonnes restent en
    face.
    """
    role = str(e.get("role") or "")
    # Le chef est signale par une etoile : c'est une distinction DANS le
    # role, pas un role de plus.
    if role and e.get("chef"):
        role = f"* {role}"
    # [BANS 22/09/2026] Le ban n'est PAS ecrit dans la colonne ROLE : un
    # banni peut etre medecin, et « BANNI (medecin) » melangeait deux
    # informations sans rapport. Il est signale par la couleur de la
    # ligne (cf. _peindre_ligne_annuaire).
    mets = e.get("metiers") or []
    if isinstance(mets, (list, tuple)):
        mets = ", ".join(str(m) for m in mets)
    return (
        str(e.get("numero") or "-"),
        str(e.get("pseudo") or "?"),
        str(e.get("discord_username") or e.get("discord_id") or "-"),
        role or "-",
        str(mets) or "-",
        _age_court(e.get("last_seen")),
    )


# ---------------------------------------------
#  Annonces : moderation
# ---------------------------------------------
#
# [ANNONCES 21/09/2026] Meme rendu en cellules que l'annuaire. Le TEXTE
# passe a la ligne dans sa cellule ; les autres colonnes restent en face.
_ANNONCES_COLS = (
    ("CATEGORIE", 13),
    ("NUMERO",     8),
    ("PSEUDO",    16),
    ("TEL",        4),
    ("PUBLIEE",   13),
    ("TEXTE",     48),
)

_ANNONCES_LIBELLES = {
    "recrutement": "Recrutement",
    "evenement":   "Evenement",
    "vente":       "Vente/Achat",
}


def _cellules_annonce(a: dict) -> tuple:
    """Les six valeurs d'une annonce, dans l'ordre des colonnes.

    TEL dit si le numero est AFFICHE aux joueurs. L'admin voit l'auteur
    dans tous les cas -- c'est l'objet de la moderation -- mais doit
    savoir ce que les joueurs, eux, en voient.
    """
    cat = str(a.get("categorie") or "")
    return (
        _ANNONCES_LIBELLES.get(cat, cat or "-"),
        str(a.get("auteur") or "-"),
        str(a.get("pseudo") or "?"),
        "oui" if a.get("avec_numero") else "non",
        _age_court(a.get("cree_le")),
        str(a.get("description") or ""),
    )


# ---------------------------------------------
#  Missions (app Travail) : moderation
# ---------------------------------------------
#
# [MODERATION 21/09/2026] Meme rendu que les annonces. MISSION regroupe
# titre, paiement et description : c'est ce qu'on lit pour decider.
_MISSIONS_COLS = (
    ("METIER",    12),
    ("ETAT",       7),
    ("AUTEUR",    20),
    ("PRISE PAR", 20),
    ("PUBLIEE",   13),
    ("MISSION",   40),
)


def _qui(numero, pseudo) -> str:
    if not numero:
        return "-"
    return f"{numero} {pseudo}" if pseudo else str(numero)


def _cellules_mission(m: dict) -> tuple:
    texte = str(m.get("titre") or "")
    pay = str(m.get("paiement") or "").strip()
    if pay:
        texte += f"  [{pay}]"
    desc = str(m.get("description") or "").strip()
    if desc:
        texte += f"\n{desc}"
    return (
        str(m.get("metier") or "-"),
        str(m.get("etat") or "-"),
        _qui(m.get("auteur"), m.get("pseudo_auteur")),
        _qui(m.get("executant"), m.get("pseudo_executant")),
        _age_court(m.get("cree_le")),
        texte,
    )


# ---------------------------------------------
#  Bannis
# ---------------------------------------------
_BANS_COLS = (
    ("PSEUDO",   18),
    ("NUMERO",    8),
    ("DISCORD",  20),
    ("BANNI LE", 13),
    ("MOTIF",    40),
)


def _cellules_ban(e: dict) -> tuple:
    return (
        str(e.get("pseudo") or "?"),
        str(e.get("numero") or "-"),
        str(e.get("discord_username") or e.get("discord_id") or "-"),
        _age_court(e.get("banned_at")),
        str(e.get("raison") or "-"),
    )


class AdminUI:
    # Onglet actif / inactif : la taille porte l'etat, la couleur le
    # confirme. Meme famille pour que la ligne de base ne bouge pas.
    _ONGLET_ACTIF   = ("Courier", 12, "bold")
    _ONGLET_INACTIF = ("Courier", 9)

    def __init__(self):
        self._cfg = _load_cfg()
        state.server_ip   = self._cfg.get("server_ip", "127.0.0.1")
        state.admin_token = self._cfg.get("admin_token", "")

        # Forcer un AppUserModelID distinct sur Windows AVANT tk.Tk().
        # Permet a Windows d'utiliser notre icone StarCircus_Admin.ico
        # dans la taskbar au lieu de l'icone Python par defaut.
        try:
            import ctypes
            # SANS la version : cet identifiant est une IDENTITE
            # d'application, pas un numero de version. Le changer a
            # chaque release ferait de l'admin une nouvelle application
            # pour Windows -- un raccourci epingle perdrait son icone et
            # sa fenetre se grouperait a part dans la barre des taches.
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CircusVOIP.Admin"
            )
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("CircusVOIP - Admin" + (f" {VERSION}" if VERSION
                                                else ""))
        self.root.configure(bg=BG)
        # Icone Admin (bandeau rouge ADMIN) : fichier StarCircus_Admin.ico
        # a cote du script. Fallback silencieux si absent.
        try:
            from pathlib import Path as _Path
            _ico_path = _Path(__file__).resolve().parent / "StarCircus_Admin.ico"
            if _ico_path.exists():
                self.root.iconbitmap(default=str(_ico_path))
                self.root.wm_iconbitmap(str(_ico_path))
        except Exception:
            pass
        # DPI scaling pour les ecrans haute resolution
        try:
            import circusvoip_dpi
            circusvoip_dpi.apply_tk_scaling(self.root)
        except Exception:
            pass
        self.root.geometry("1100x700")
        self.root.minsize(900, 550)

        self._players_rows: dict[str, dict] = {}  # name -> {row, lbl_*, prof_var, ...}
        # [22/09/2026] File des rappels deposes par le fil reseau (cf.
        # _safe_after). Videe par le thread Tk lui-meme.
        self._file_ui = queue.Queue()
        self._build_ui()
        self.root.after(30, self._vider_file_ui)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    # ----- Construction UI -----

    def _build_ui(self):
        # Header (connexion)
        header = tk.Frame(self.root, bg=BG, pady=8)
        header.pack(fill="x", padx=12)
        tk.Label(header, text="CircusVOIP Admin", bg=BG, fg=PURPLE,
                 font=("Courier", 13, "bold")).pack(side="left")
        # Version VISIBLE a l'ecran et pas seulement dans la barre de
        # titre : c'est la zone qu'un testeur capture, et la barre de
        # titre est souvent hors du cadre de la capture.
        #
        # Aucun libelle si le version.json n'a pas ete trouve : pas de
        # "v?" ni de "version inconnue", qui attireraient l'oeil sur un
        # non-probleme a chaque lancement.
        if VERSION:
            tk.Label(header, text=f"v{VERSION}", bg=BG, fg=MUTED,
                     font=("Courier", 9)).pack(side="left", padx=(8, 0))
        self._lbl_status = tk.Label(header, text="Deconnecte", bg=BG, fg=MUTED,
                                    font=("Courier", 9))
        self._lbl_status.pack(side="right")

        # Barre connexion : IP + token + bouton
        conn = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=6)
        conn.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(conn, text="Serveur IP :", bg=BG_PANEL, fg=TEXT,
                 font=("Courier", 9)).pack(side="left")
        # [ADMIN 31/07/2026] Adresse masquee par defaut, comme le champ
        # Serveur du client. Motif : l'admin est l'outil qu'on montre en
        # partage d'ecran ou en capture, et l'adresse du serveur n'a pas a
        # y figurer en clair. La saisie reste normale, seul l'affichage
        # est masque.
        self._entry_ip = tk.Entry(conn, bg=BG_ROW, fg=TEXT, font=("Courier", 9),
                                  insertbackground=TEXT, relief="flat", bd=4,
                                  width=20, show="*")
        self._entry_ip.insert(0, state.server_ip)
        self._entry_ip.pack(side="left", padx=(4, 4))

        self._show_ip = False
        self._btn_show_ip = tk.Label(conn, text="\U0001f441", bg=BG_PANEL,
                                     fg=MUTED, font=("Courier", 11),
                                     cursor="hand2", padx=4)
        self._btn_show_ip.pack(side="left", padx=(0, 12))
        self._btn_show_ip.bind("<Button-1>",
                               lambda e: self._toggle_show_ip())

        tk.Label(conn, text="Token admin :", bg=BG_PANEL, fg=TEXT,
                 font=("Courier", 9)).pack(side="left")
        self._entry_token = tk.Entry(conn, bg=BG_ROW, fg=TEXT, font=("Courier", 9),
                                     insertbackground=TEXT, relief="flat", bd=4,
                                     width=36, show="*")
        self._entry_token.insert(0, state.admin_token)
        self._entry_token.pack(side="left", padx=(4, 12))

        # Toggle visibilite token
        self._show_token = False
        self._btn_show = tk.Label(conn, text="👁", bg=BG_PANEL, fg=MUTED,
                                  font=("Courier", 11), cursor="hand2", padx=4)
        self._btn_show.pack(side="left")
        self._btn_show.bind("<Button-1>", lambda e: self._toggle_show_token())

        self._btn_connect = tk.Label(conn, text="CONNECTER", bg=GREEN, fg=BG,
                                     font=("Courier", 9, "bold"),
                                     padx=12, pady=4, cursor="hand2")
        self._btn_connect.pack(side="left", padx=12)
        self._btn_connect.bind("<Button-1>", lambda e: self._connect())

        self._btn_disconnect = tk.Label(conn, text="DECONNECTER", bg=BORDER, fg=MUTED,
                                        font=("Courier", 9, "bold"),
                                        padx=12, pady=4, cursor="hand2")
        self._btn_disconnect.pack(side="left", padx=4)
        self._btn_disconnect.bind("<Button-1>", lambda e: self._disconnect())

        # ============================================================
        #  [REFONTE 07/09/2026] Onglets
        # ============================================================
        #
        # Trois colonnes se disputaient la largeur. `pack(side="left")`
        # sert dans l'ORDRE et rogne le dernier servi : la colonne de
        # droite (canaux / profils / mot de passe) etait tronquee des que
        # la fenetre approchait du minsize(900), et on lisait "PROFIL",
        # "MOT DE", "(non ". Constate en capture le 07/09.
        #
        # ttk.Notebook plutot qu'une autre bibliotheque : Tkinter vient
        # avec le runtime Python embarque de l'installeur SERVEUR, pas
        # PySide6 (~60-80 Mo de plus). Contrainte notee dans
        # CIRCUSVOIP_PROJET.md.
        #
        # Le bandeau CHEFS se disait lui-meme "TEMPORAIRE dans cette
        # forme, l'interface admin sera refaite" -- c'est ici.
        #
        # Repartition : Joueurs (qui est la), Annuaire (qui existe), Jeu
        # (ce qui regle le jeu : canaux, profils, chefs, mot de passe),
        # Logs (supervision, avec les debits).

        # Barre d'onglets faite de tk.Label, pas de ttk.Notebook.
        #
        # Deux raisons. ttk dessine un CADRE autour de chaque onglet
        # (l'element "Notebook.tab") qu'on ne peut pas retirer sans
        # reecrire la mise en page du theme. Et la police n'est pas une
        # option cartographiable par etat : `style.map(font=...)` est
        # ignore, donc impossible de grossir l'onglet actif.
        #
        # Des Labels donnent les deux gratuitement, et c'est deja le
        # patron du reste du fichier -- tous les boutons de cet outil
        # sont des Labels cliquables.
        self._pages = {}
        self._onglet_labels = {}
        self._onglet_traits = {}
        self._onglet_actif = None

        barre = tk.Frame(self.root, bg=BG, height=36)
        barre.pack(fill="x", padx=12, pady=(2, 0))
        barre.pack_propagate(False)   # la hauteur ne suit pas la police,
        # sinon la barre sautait a chaque changement d'onglet.

        # Ligne continue sous la barre + trait epais sous l'onglet actif.
        #
        # [07/09/2026] Sans cadre, des libelles poses cote a cote ne se
        # lisent pas comme des onglets -- constate en capture. Le
        # soulignement est l'affordance qui les designe comme tels sans
        # redessiner les boites qu'on voulait retirer : la ligne de base
        # dit "barre d'onglets", le trait epais dit "vous etes ici".
        self._sep_onglets = tk.Frame(self.root, bg=BORDER, height=1)
        self._sep_onglets.pack(fill="x", padx=12)

        self._zone_pages = tk.Frame(self.root, bg=BG_PANEL)
        self._zone_pages.pack(fill="both", expand=True, padx=12, pady=(6, 12))

        def _page(nom):
            f = tk.Frame(self._zone_pages, bg=BG_PANEL, padx=10, pady=10)
            self._pages[nom] = f
            # Une colonne par onglet : le libelle, puis son trait.
            col = tk.Frame(barre, bg=BG)
            col.pack(side="left", padx=(0, 4))
            lbl = tk.Label(col, text=nom, bg=BG, fg=MUTED,
                           font=self._ONGLET_INACTIF, cursor="hand2",
                           padx=14)
            lbl.pack(side="top", fill="x", expand=True)
            trait = tk.Frame(col, bg=BG, height=3)
            trait.pack(side="bottom", fill="x")
            for w in (col, lbl, trait):
                w.bind("<Button-1>", lambda e, n=nom: self.montrer_onglet(n))
            self._onglet_labels[nom] = lbl
            self._onglet_traits[nom] = trait
            return f

        # ----- Onglet JOUEURS ---------------------------------------
        p_joueurs = _page("JOUEURS")

        actions = tk.Frame(p_joueurs, bg=BG_PANEL, pady=2)
        actions.pack(fill="x")
        self._btn_anon = tk.Label(actions, text="Mode anonyme : OFF",
                                  bg=BORDER, fg=MUTED,
                                  font=("Courier", 9, "bold"), cursor="hand2",
                                  padx=10, pady=4)
        self._btn_anon.pack(side="left")
        self._btn_anon.bind("<Button-1>", lambda e: self._toggle_anonymous())

        plist_outer = tk.Frame(p_joueurs, bg=BG_PANEL)
        plist_outer.pack(fill="both", expand=True, pady=(8, 0))
        self._p_canvas = tk.Canvas(plist_outer, bg=BG_PANEL, bd=0,
                                   highlightthickness=0)
        p_scroll = tk.Scrollbar(plist_outer, orient="vertical",
                                command=self._p_canvas.yview, bg=BORDER,
                                troughcolor=BG_PANEL, activebackground=BLUE,
                                width=12)
        self._players_frame = tk.Frame(self._p_canvas, bg=BG_PANEL)
        self._p_win = self._p_canvas.create_window(
            (0, 0), window=self._players_frame, anchor="nw")
        self._p_canvas.configure(yscrollcommand=p_scroll.set)
        self._p_canvas.pack(side="left", fill="both", expand=True)
        p_scroll.pack(side="right", fill="y")

        def _on_p_resize(event):
            self._p_canvas.itemconfig(self._p_win, width=event.width)
        self._p_canvas.bind("<Configure>", _on_p_resize)

        def _on_p_inner_resize(_e):
            self._p_canvas.configure(
                scrollregion=self._p_canvas.bbox("all"))
        self._players_frame.bind("<Configure>", _on_p_inner_resize)

        def _on_p_wheel(event):
            self._p_canvas.yview_scroll(
                int(-1 * (event.delta / 120)), "units")
        self._p_canvas.bind("<Enter>", lambda e: self._p_canvas.bind_all(
            "<MouseWheel>", _on_p_wheel))
        self._p_canvas.bind("<Leave>", lambda e: self._p_canvas.unbind_all(
            "<MouseWheel>"))

        self._lbl_no_players = tk.Label(self._players_frame,
                                        text="(aucun joueur connecte)",
                                        bg=BG_PANEL, fg=MUTED,
                                        font=("Courier", 8), pady=6)
        self._lbl_no_players.pack(fill="x")

        # ----- Onglet ANNUAIRE --------------------------------------
        #
        # [DISCORD 30/07/2026] Reserve a l'admin, jamais consultable par
        # les joueurs. Seul point d'acces a discord_id -> {pseudo,
        # numero}.
        #
        # Etait une fenetre separee, ouverte sur clic. Elle ne s'ouvrait
        # qu'a l'ARRIVEE des donnees, deliberement : "une fenetre vide
        # qui se remplit apres coup laisse croire que l'annuaire est
        # vide". Un onglet existe toujours, il lui faut donc un etat
        # explicite -- d'ou "Chargement..." plutot qu'un vide ambigu.
        p_annuaire = _page("ANNUAIRE")

        a_head = tk.Frame(p_annuaire, bg=BG_PANEL)
        a_head.pack(fill="x")
        # Meme largeur en caracteres que les cellules, plus le decalage
        # du cadre des lignes (padx=8) pour que l'entete tombe pile
        # au-dessus.
        tk.Frame(a_head, bg=BG_PANEL, width=8).pack(side="left")
        for titre, largeur in _ANNUAIRE_COLS:
            tk.Label(a_head, text=titre, width=largeur, anchor="w",
                     bg=BG_PANEL, fg=MUTED,
                     font=("Courier", 9, "bold")).pack(
                         side="left", padx=(0, _ANNUAIRE_GOUTTIERE))
        self._annuaire_count = tk.Label(a_head, text="", bg=BG_PANEL,
                                        fg=MUTED, font=("Courier", 9))
        self._annuaire_count.pack(side="right")

        a_outer = tk.Frame(p_annuaire, bg=BG_PANEL)
        a_outer.pack(fill="both", expand=True, pady=(6, 0))
        self._a_canvas = tk.Canvas(a_outer, bg=BG_PANEL, bd=0,
                                   highlightthickness=0)
        a_scroll = tk.Scrollbar(a_outer, orient="vertical",
                                command=self._a_canvas.yview, bg=BORDER,
                                troughcolor=BG_PANEL, activebackground=BLUE,
                                width=12)
        self._annuaire_body = tk.Frame(self._a_canvas, bg=BG_PANEL)
        self._a_win = self._a_canvas.create_window(
            (0, 0), window=self._annuaire_body, anchor="nw")
        self._a_canvas.configure(yscrollcommand=a_scroll.set)
        self._a_canvas.pack(side="left", fill="both", expand=True)
        a_scroll.pack(side="right", fill="y")
        self._a_canvas.bind(
            "<Configure>",
            lambda e: self._a_canvas.itemconfig(self._a_win, width=e.width))
        self._annuaire_body.bind(
            "<Configure>",
            lambda e: self._a_canvas.configure(
                scrollregion=self._a_canvas.bbox("all")))

        # Etat de depart explicite : ni fiches, ni "aucune fiche".
        tk.Label(self._annuaire_body, text="(non connecte)",
                 bg=BG_PANEL, fg=MUTED, font=("Courier", 9),
                 pady=8).pack(fill="x")

        # ----- Onglet MODERATION ------------------------------------
        #
        # [MODERATION 22/09/2026] Regroupe ce qui etait deux onglets
        # (ANNONCES, MISSIONS) et ajoute les BANNIS. Trois listes du meme
        # genre -- voir, retirer -- sous un seul onglet, avec une petite
        # barre pour passer de l'une a l'autre. La barre d'onglets
        # principale restait sinon a s'allonger a chaque app moderable.
        p_mod = _page("MODERATION")
        sous = tk.Frame(p_mod, bg=BG_PANEL)
        sous.pack(fill="x", pady=(0, 6))
        self._mod_pages = {}
        self._mod_btns = {}
        self._mod_actif = None
        zone_mod = tk.Frame(p_mod, bg=BG_PANEL)
        zone_mod.pack(fill="both", expand=True)
        for cle, lib in (("annonces", "Annonces"), ("missions", "Missions"),
                         ("bannis", "Bannis")):
            b_ = tk.Label(sous, text=lib, bg=BORDER, fg=MUTED,
                          font=("Courier", 9, "bold"), cursor="hand2",
                          padx=12, pady=3)
            b_.pack(side="left", padx=(0, 4))
            b_.bind("<Button-1>", lambda e, c=cle: self._montrer_mod(c))
            self._mod_btns[cle] = b_
            self._mod_pages[cle] = tk.Frame(zone_mod, bg=BG_PANEL)

        # Annonces des joueurs. Premier espace public en texte libre du
        # projet : il faut pouvoir retirer sans attendre l'expiration.
        self._annonces_body, self._annonces_count = self._liste_moderation(
            self._mod_pages["annonces"], _ANNONCES_COLS)
        # Missions de l'app Travail : ouvertes et prises. Retirer une
        # mission prise libere aussi son executant.
        self._missions_body, self._missions_count = self._liste_moderation(
            self._mod_pages["missions"], _MISSIONS_COLS)
        # Comptes Discord bannis. On bannit depuis l'ANNUAIRE (bouton B de
        # chaque fiche) ; on leve le ban ici.
        self._bans_body, self._bans_count = self._liste_moderation(
            self._mod_pages["bannis"], _BANS_COLS)
        self._montrer_mod("annonces")

        # ----- Onglet JEU -------------------------------------------
        p_jeu = _page("JEU")

        # [URGENCE 15/08/2026] CHEFS. Seul endroit du dispositif ou un
        # chef se designe : un chef distribue son role, mais ne peut pas
        # en creer un autre. Ici plutot qu'en permanence : c'est du
        # reglage de jeu, au meme titre que les canaux et les profils.
        self._section(p_jeu, "CHEFS")
        chefs_bar = tk.Frame(p_jeu, bg=BG_PANEL)
        chefs_bar.pack(fill="x", pady=(0, 10))
        self._chef_combos = {}
        self._chef_annuaire = []
        for role, libelle in (("medecin", "Médical"),
                              ("securite", "Sécurité")):
            tk.Label(chefs_bar, text=f"{libelle} :", bg=BG_PANEL, fg=TEXT,
                     font=("Courier", 9)).pack(side="left", padx=(0, 2))
            combo = ttk.Combobox(chefs_bar, state="readonly", width=22,
                                 values=["(aucun)"])
            combo.set("(aucun)")
            combo.pack(side="left", padx=(0, 16))
            combo.bind("<<ComboboxSelected>>",
                       lambda e, r=role: self._designer_chef(r))
            self._chef_combos[role] = combo

        # Canaux et profils COTE A COTE : c'est leur empilement dans une
        # colonne de 280 px qui les rendait illisibles.
        deux = tk.Frame(p_jeu, bg=BG_PANEL)
        deux.pack(fill="both", expand=True)

        col_ch = tk.Frame(deux, bg=BG_PANEL)
        col_ch.pack(side="left", fill="both", expand=True, padx=(0, 8))
        self._section(col_ch, "CANAUX RADIO")
        self._channels_frame = tk.Frame(col_ch, bg=BG_PANEL)
        self._channels_frame.pack(fill="x", pady=2)
        btn_add_ch = tk.Label(col_ch, text="+  Ajouter canal", bg=BORDER,
                              fg=BLUE, font=("Courier", 9, "bold"),
                              pady=6, padx=8, cursor="hand2")
        btn_add_ch.pack(fill="x", pady=(2, 8))
        btn_add_ch.bind("<Button-1>", lambda e: self._add_channel())

        col_pr = tk.Frame(deux, bg=BG_PANEL)
        col_pr.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self._section(col_pr, "PROFILS")
        self._profiles_frame = tk.Frame(col_pr, bg=BG_PANEL)
        self._profiles_frame.pack(fill="x", pady=2)
        btn_add_prof = tk.Label(col_pr, text="+  Ajouter profil",
                                bg=BORDER, fg=PURPLE,
                                font=("Courier", 9, "bold"), pady=6, padx=8,
                                cursor="hand2")
        btn_add_prof.pack(fill="x", pady=(2, 8))
        btn_add_prof.bind("<Button-1>", lambda e: self._add_profile())

        # Mot de passe joueur : reglage de serveur, mais consulte au meme
        # moment que les canaux -- quand on prepare une session.
        bas = tk.Frame(p_jeu, bg=BG_PANEL)
        bas.pack(fill="x", pady=(10, 0))
        self._section(bas, "MOT DE PASSE JOUEUR")
        ligne_mdp = tk.Frame(bas, bg=BG_PANEL)
        ligne_mdp.pack(fill="x")
        self._lbl_server_token = tk.Label(ligne_mdp, text="(non connecte)",
                                          bg=BG_ROW, fg=BLUE,
                                          font=("Courier", 9), pady=6, padx=8,
                                          anchor="w", justify="left")
        self._lbl_server_token.pack(side="left", fill="x", expand=True)
        # [07/09/2026] Masque par defaut, revele par l'oeil -- meme
        # mecanique que l'IP et le jeton admin en haut de fenetre. Le
        # mot de passe est de nouveau envoye par le serveur (cf.
        # admin_welcome) ; ce qu'on evite ici, c'est de l'avoir en clair
        # a l'ecran en permanence, notamment sur une capture.
        self._show_srv_token = False
        self._btn_show_srv = tk.Label(ligne_mdp, text="\U0001f441",
                                      bg=BG_PANEL, fg=MUTED,
                                      font=("Courier", 11), cursor="hand2",
                                      padx=8)
        self._btn_show_srv.pack(side="left")
        self._btn_show_srv.bind(
            "<Button-1>", lambda e: self._toggle_show_srv_token())

        self._btn_copier_mdp = tk.Label(ligne_mdp, text="Copier",
                                        bg=BORDER, fg=MUTED,
                                        font=("Courier", 9), cursor="hand2",
                                        padx=12, pady=6)
        self._btn_copier_mdp.pack(side="left", padx=(8, 0))
        self._btn_copier_mdp.bind("<Button-1>",
                                  lambda e: self._copier_mdp_serveur())

        btn_change_token = tk.Label(ligne_mdp, text="Modifier",
                                    bg=BORDER, fg=MUTED,
                                    font=("Courier", 9), cursor="hand2",
                                    padx=12, pady=6)
        btn_change_token.pack(side="left", padx=(8, 0))
        btn_change_token.bind("<Button-1>",
                              lambda e: self._change_server_token())

        # [ADMIN 07/09/2026] Redemarrage du serveur.
        #
        # Redemarrer SEULEMENT. "Arreter" et "demarrer" ne sont pas
        # offerts, et ce n'est pas un oubli : l'unite systemd porte
        # Restart=always, donc un arret serait annule au bout de cinq
        # secondes ; et demarrer est impossible par construction, l'admin
        # parlant au serveur VIA le serveur. Les proposer demanderait un
        # agent separe sur le VPS, avec sa propre authentification et une
        # regle sudoers -- une surface d'attaque pour un besoin rare.
        self._section(bas, "SERVEUR")
        self._btn_restart = tk.Label(
            bas, text="Redemarrer le serveur", bg=BORDER, fg=ORANGE,
            font=("Courier", 9, "bold"), cursor="hand2", padx=12, pady=6)
        self._btn_restart.pack(fill="x")
        self._btn_restart.bind("<Button-1>",
                               lambda e: self._redemarrer_serveur())

        # ----- Onglet LOGS ------------------------------------------
        p_logs = _page("LOGS")

        # [OBS1] Debits : positions (mesure locale du 8888) et audio
        # (relaye depuis le 8889 via fichier partage). Le fanout est le
        # chiffre a surveiller pour la diffusion par zone, les o/trame
        # pour Opus. Ici parce qu'on les lit en meme temps que le
        # journal : c'est la page de supervision.
        self._section(p_logs, "DEBIT")
        stats_bar = tk.Frame(p_logs, bg=BG_ROW, padx=10, pady=6)
        stats_bar.pack(fill="x", pady=(0, 10))
        # Colonnes a largeur fixe (Courier) pour que les chiffres se
        # lisent verticalement ; la moyenne 60s est la reference,
        # l'instantane est entre parentheses.
        self._lbl_stats_head = tk.Label(
            stats_bar,
            text=f"{'':6}{'IN moy(inst)':>15}{'OUT moy(inst)':>15}   detail",
            bg=BG_ROW, fg=MUTED, font=("Courier", 9), anchor="w")
        self._lbl_stats_head.pack(fill="x")
        self._lbl_stats_pos = tk.Label(
            stats_bar, text="POS      en attente", bg=BG_ROW, fg=MUTED,
            font=("Courier", 9), anchor="w")
        self._lbl_stats_pos.pack(fill="x")
        self._lbl_stats_audio = tk.Label(
            stats_bar, text="AUDIO    en attente", bg=BG_ROW, fg=MUTED,
            font=("Courier", 9), anchor="w")
        self._lbl_stats_audio.pack(fill="x")
        self._lbl_stats_total = tk.Label(
            stats_bar, text="", bg=BG_ROW, fg=ORANGE,
            font=("Courier", 9, "bold"), anchor="w")
        self._lbl_stats_total.pack(fill="x")

        # [LOGS ADMIN 21/09/2026] Journaux du serveur a rapatrier. Evite
        # scp et recuperer_logs_joueurs.ps1 -- et surtout, rend la chose
        # possible a un hebergeur tiers, qui n'a ni l'un ni l'autre.
        self._section(p_logs, "FICHIERS DU SERVEUR")
        fbar = tk.Frame(p_logs, bg=BG_PANEL)
        fbar.pack(fill="x")
        self._logs_source = "joueurs"
        self._logs_btn_src = {}
        for src, lib in (("joueurs", "Joueurs"), ("positions", "Positions"),
                         ("audio", "Audio")):
            b = tk.Label(fbar, text=lib, bg=BORDER, fg=MUTED,
                         font=("Courier", 9, "bold"), cursor="hand2",
                         padx=10, pady=3)
            b.pack(side="left", padx=(0, 4))
            b.bind("<Button-1>", lambda e, s_=src: self._choisir_source_logs(s_))
            self._logs_btn_src[src] = b
        for texte, action, coul in (
                ("Ouvrir le dossier", self._ouvrir_dossier_logs, MUTED),
                ("Telecharger", self._telecharger_logs, GREEN),
                ("Rafraichir", self._demander_logs, BLUE)):
            b = tk.Label(fbar, text=texte, bg=BORDER, fg=coul,
                         font=("Courier", 9, "bold"), cursor="hand2",
                         padx=10, pady=3)
            b.pack(side="right", padx=(4, 0))
            b.bind("<Button-1>", lambda e, f=action: f())
        lb_frame = tk.Frame(p_logs, bg=BG_PANEL)
        lb_frame.pack(fill="x", pady=(4, 2))
        # Listbox et non des Labels : selection multiple native (Ctrl /
        # Maj), et 150 lignes sans cout de construction.
        self._lb_logs = tk.Listbox(
            lb_frame, height=7, selectmode="extended", bg=BG_ROW, fg=TEXT,
            font=("Courier", 9), bd=0, relief="flat", highlightthickness=0,
            selectbackground=BLUE, selectforeground=BG, activestyle="none")
        self._lb_logs.pack(side="left", fill="x", expand=True)
        lb_scroll = tk.Scrollbar(lb_frame, command=self._lb_logs.yview,
                                 bg=BORDER, troughcolor=BG_PANEL, width=12)
        lb_scroll.pack(side="right", fill="y")
        self._lb_logs.config(yscrollcommand=lb_scroll.set)
        self._lb_logs.bind("<Double-Button-1>",
                           lambda e: self._telecharger_logs())
        self._lbl_logs_etat = tk.Label(
            p_logs, text="(non connecte)", bg=BG_PANEL, fg=MUTED,
            font=("Courier", 8), anchor="w")
        self._lbl_logs_etat.pack(fill="x", pady=(0, 8))
        self._logs_fichiers = []
        self._logs_affiches = []
        self._logs_dl = {}          # req -> transfert en cours
        self._logs_n = 0
        self._peindre_sources_logs()

        self._section(p_logs, "JOURNAL")
        log_frame = tk.Frame(p_logs, bg=BG_PANEL)
        log_frame.pack(fill="both", expand=True)
        self._txt_log = tk.Text(log_frame, bg=BG_ROW, fg=TEXT,
                                font=("Courier", 8), bd=0, relief="flat",
                                insertbackground=TEXT, wrap="word")
        self._txt_log.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(log_frame, command=self._txt_log.yview,
                              bg=BORDER, troughcolor=BG_PANEL,
                              activebackground=BLUE, width=12)
        scroll.pack(side="right", fill="y")
        self._txt_log.config(yscrollcommand=scroll.set)
        self._txt_log.config(state="disabled")

        clear_frame = tk.Frame(p_logs, bg=BG_PANEL)
        clear_frame.pack(fill="x", pady=(4, 0))
        btn_clear = tk.Label(clear_frame, text="Clear logs", bg=BORDER,
                             fg=MUTED, font=("Courier", 8), cursor="hand2",
                             padx=8, pady=2)
        btn_clear.pack(side="right")
        btn_clear.bind("<Button-1>", lambda e: self._clear_logs())

        self.montrer_onglet("JOUEURS")

    def _section(self, parent, title: str):
        tk.Label(parent, text=title, bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 8, "bold"), anchor="w"
                 ).pack(fill="x", pady=(2, 4))

    # ----- Connexion -----

    def _toggle_show_ip(self):
        """Revele ou masque l'adresse du serveur. Meme mecanique que le
        token : on change l'affichage, jamais le contenu."""
        self._show_ip = not self._show_ip
        self._entry_ip.config(show="" if self._show_ip else "*")
        self._btn_show_ip.config(fg=BLUE if self._show_ip else MUTED)

    def _toggle_show_srv_token(self):
        """Revele ou masque le mot de passe joueur.

        Le champ est un Label et non un Entry : l'option `show` des
        Entry n'existe pas ici, on reecrit donc le texte.
        """
        self._show_srv_token = not getattr(self, "_show_srv_token", False)
        self._peindre_mdp_serveur()

    def _peindre_mdp_serveur(self):
        """Ecrit le mot de passe joueur, masque ou en clair.

        Recale aussi la couleur de l'oeil : le drapeau peut avoir ete
        remis a False depuis le fil WebSocket (nouvelle connexion), qui
        n'a pas le droit de toucher un widget.
        """
        revele = getattr(self, "_show_srv_token", False)
        try:
            self._btn_show_srv.config(fg=BLUE if revele else MUTED)
        except Exception:
            pass
        tok = state.server_token or ""
        if not tok:
            texte = "(non connecte)" if not state.connected else "(non recu)"
        elif getattr(self, "_show_srv_token", False):
            texte = tok
        else:
            texte = "*" * len(tok)
        try:
            self._lbl_server_token.config(text=texte)
        except Exception:
            pass

    def _toggle_show_token(self):
        self._show_token = not self._show_token
        self._entry_token.config(show="" if self._show_token else "*")
        self._btn_show.config(fg=BLUE if self._show_token else MUTED)

    def _connect(self):
        if state.connected:
            return
        ip = self._entry_ip.get().strip() or "127.0.0.1"
        token = self._entry_token.get().strip()
        if not token:
            messagebox.showwarning("CircusVOIP Admin",
                                   "Token admin requis", parent=self.root)
            return
        state.server_ip   = ip
        state.admin_token = token
        # Persister
        self._cfg["server_ip"]   = ip
        self._cfg["admin_token"] = token
        _save_cfg(self._cfg)
        self.set_status(True, "Connexion...")
        threading.Thread(target=_run_ws, args=(self,), daemon=True).start()

    def _disconnect(self):
        if not state.connected:
            return
        # Fermer la WS via le loop dedie
        if state.ws is not None and state.ws_loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(state.ws.close(), state.ws_loop)
            except Exception:
                pass

    def _on_close(self):
        try:
            self._disconnect()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass
        import os
        os._exit(0)

    # ----- Status / logs -----

    def set_status(self, connected: bool, text: str = ""):
        def _do():
            if connected:
                self._lbl_status.config(text=text or "Connecte", fg=GREEN)
                self._btn_connect.config(bg=BORDER, fg=MUTED)
                self._btn_disconnect.config(bg=RED, fg="white")
            else:
                self._lbl_status.config(text=text or "Deconnecte", fg=MUTED)
                self._btn_connect.config(bg=GREEN, fg=BG)
                self._btn_disconnect.config(bg=BORDER, fg=MUTED)
                self._vider_affichage()
        self._safe_after(_do)

    def _vider_affichage(self):
        """Efface tout ce qui vient du serveur. Thread Tk.

        [08/09/2026] A la deconnexion, l'ecran gardait la derniere image
        recue : joueurs, canaux, profils, annuaire, mot de passe joueur.
        Deux problemes distincts.

        Le mot de passe et l'annuaire -- pseudos, identifiants Discord,
        numeros -- restaient lisibles sur un ecran qui ne l'est plus par
        personne. Et le reste devenait un etat PERIME presente comme
        courant : une liste de joueurs qui ne sont plus la, des canaux
        qui ont pu changer. Rien ne distinguait "voici l'etat" de "voici
        l'etat d'il y a une heure".

        Les journaux ne sont PAS effaces : ils sont l'historique de la
        session, c'est justement apres une deconnexion qu'on les relit.
        """
        state.channels = []
        state.profiles = []
        state.players = {}
        state.anonymous_mode = False
        state.server_token = ""
        # Repartir masque : la prochaine connexion ne doit pas afficher
        # un mot de passe en clair sans qu'on l'ait demande.
        self._show_srv_token = False
        try:
            self._chef_annuaire = []
            self._chef_ids = [None]
            for combo in self._chef_combos.values():
                combo.config(values=["(aucun)"])
                combo.set("(aucun)")
        except Exception:
            pass
        for f in (self.refresh_players, self.refresh_channels,
                  self.refresh_profiles, self.refresh_anonymous,
                  self._peindre_mdp_serveur):
            try:
                f()
            except Exception:
                pass
        # L'annuaire a sa propre signature : la remettre a None force la
        # reconstruction au lieu d'un "identique, rien a faire".
        self._annuaire_sig = None
        try:
            self.show_annuaire([])
            self._annuaire_count.config(text="")
        except Exception:
            pass
        # [ANNONCES 21/09/2026] Meme raison que l'annuaire : la liste de
        # moderation montre l'auteur de chaque annonce.
        self._annonces_sig = None
        try:
            self.show_annonces([])
            self._annonces_count.config(text="")
        except Exception:
            pass
        # Transferts en cours abandonnes : fichiers .part supprimes, sinon
        # ils resteraient ouverts et tronques.
        for t in list(getattr(self, "_logs_dl", {}).values()):
            try:
                if t.get("fp") is not None:
                    t["fp"].close()
                t["tmp"].unlink()
            except Exception:
                pass
        try:
            self._logs_dl = {}
            self._logs_fichiers = []
            self._logs_affiches = []
            self._lb_logs.delete(0, "end")
            self._lbl_logs_etat.config(text="(non connecte)", fg=MUTED)
        except Exception:
            pass
        self._missions_sig = None
        try:
            self.show_missions([])
            self._missions_count.config(text="")
        except Exception:
            pass

        # [22/09/2026] Etat EXPLICITE « non connecte » dans les trois
        # listes, au lieu de « Aucune fiche » / « Aucune annonce » : ces
        # textes affirment quelque chose sur le serveur, alors qu'on ne
        # sait plus rien de lui. Les signatures sont remises a zero pour
        # que la prochaine liste recue reconstruise tout.
        for corps_attr in ("_annuaire_body", "_annonces_body",
                           "_missions_body", "_bans_body"):
            corps = getattr(self, corps_attr, None)
            if corps is None:
                continue
            try:
                for w in corps.winfo_children():
                    w.destroy()
                tk.Label(corps, text="(non connecte)", bg=BG_PANEL,
                         fg=MUTED, font=("Courier", 9),
                         pady=8).pack(fill="x")
            except Exception:
                pass
        self._annuaire_rows = []
        self._annuaire_sig = None
        self._annonces_sig = None
        self._missions_sig = None
        try:
            self._bans_count.config(text="")
        except Exception:
            pass

        # [22/09/2026] Debits : restaient figes sur la derniere mesure,
        # presentee comme courante. Meme raison que la liste des joueurs.
        state.stats = {}
        for k in list(state.stats_hist.keys()):
            state.stats_hist[k] = []
        try:
            self._lbl_stats_pos.config(text="POS      (non connecte)",
                                       fg=MUTED)
            self._lbl_stats_audio.config(text="AUDIO    (non connecte)",
                                         fg=MUTED)
            self._lbl_stats_total.config(text="")
        except Exception:
            pass

    def add_log(self, msg: str, color: str = TEXT, ts: str = None):
        def _do():
            if ts:
                line = f"[{ts}] {msg}\n"
            else:
                ts2 = datetime.now().strftime("%H:%M:%S")
                line = f"[{ts2}] {msg}\n"
            self._txt_log.config(state="normal")
            tag = f"c_{color.replace('#', '')}"
            try:
                self._txt_log.tag_config(tag, foreground=color)
            except Exception:
                pass
            self._txt_log.insert("end", line, tag)
            self._txt_log.see("end")
            self._txt_log.config(state="disabled")
        self._safe_after(_do)

    def _clear_logs(self):
        self._txt_log.config(state="normal")
        self._txt_log.delete("1.0", "end")
        self._txt_log.config(state="disabled")

    def _safe_after(self, callback):
        """Execute `callback` sur le thread Tk. Appelable de n'importe ou.

        [22/09/2026] Depuis le fil RESEAU, on ne touche plus du tout a Tk,
        pas meme a root.after : on depose dans une file que le thread Tk
        vide lui-meme (_vider_file_ui).

        Pourquoi : root.after appele depuis un autre thread n'est pas un
        simple depot. Tkinter fait executer l'appel par le thread Tk et
        ATTEND qu'il l'ait fait. Si le thread Tk est a ce moment dans
        _ws_send_safe -- qui attend, lui, que la boucle reseau envoie --
        chacun attend l'autre jusqu'au delai d'une seconde. Le correctif
        de la veille avait supprime les appels de WIDGETS depuis le fil
        reseau, mais pas root.after lui-meme : le gel a la connexion
        restait, plus court. Une file ne bloque personne.
        """
        if threading.current_thread() is threading.main_thread():
            try:
                self.root.after(0, callback)
            except Exception:
                pass
            return
        try:
            self._file_ui.put_nowait(callback)
        except Exception:
            pass

    def _vider_file_ui(self):
        """Execute ce que le fil reseau a depose. Thread Tk, toutes les
        30 ms. Borne a 200 elements par tour : un afflux (transfert de
        journal) ne doit pas figer l'interface le temps de tout vider."""
        n = 0
        while n < 200:
            try:
                cb = self._file_ui.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                cb()
            except Exception as e:
                try:
                    print(f"[UI] rappel KO : {e!r}", flush=True)
                except Exception:
                    pass
        try:
            self.root.after(30, self._vider_file_ui)
        except Exception:
            pass

    # ----- Refresh -----

    def refresh_stats(self):
        """[OBS1] Met a jour le bandeau de debit. Tolere l'absence de la
        partie audio (serveur audio arrete ou fichier de stats perime) :
        on affiche 'n/a' plutot que des zeros trompeurs."""
        def _do():
            p = state.stats.get("pos") or {}
            a = state.stats.get("audio") or {}
            def _u(v, dec=1):
                # Bascule automatique en Mbit/s au-dela de 10 000 kbit/s :
                # a 100 joueurs le sortant depasse 3 800 000 kbit/s, ce qui
                # ferait exploser la largeur de colonne.
                return f"{v / 1000.0:.{dec}f}M" if v >= 10000 else f"{v:.{dec}f}"

            def _cell(moy, inst):
                # Largeur fixe : sans ca les colonnes se decalent des que
                # le debit change d'ordre de grandeur.
                return f"{f'{_u(moy)}({_u(inst, 0)})':>15}"

            if p:
                self._lbl_stats_pos.config(
                    fg=TEXT,
                    text=(f"{'POS':6}"
                          f"{_cell(_avg('pos_in'),  p.get('kbps_in', 0))}"
                          f"{_cell(_avg('pos_out'), p.get('kbps_out', 0))}"
                          f"   {p.get('clients', 0)} joueurs · "
                          f"{p.get('msgs_in', 0):.0f}->"
                          f"{p.get('msgs_out', 0):.0f} msg/s · "
                          f"fanout x{p.get('fanout', 0):.1f}"))
            if a:
                self._lbl_stats_audio.config(
                    fg=TEXT,
                    text=(f"{'AUDIO':6}"
                          f"{_cell(_avg('aud_in'),  a.get('kbps_in', 0))}"
                          f"{_cell(_avg('aud_out'), a.get('kbps_out', 0))}"
                          f"   {a.get('clients', 0)} clients · "
                          f"{a.get('speakers', 0)} parleur(s) · "
                          f"{a.get('bytes_frame', 0):.0f} o/trame · "
                          f"fanout x{a.get('fanout', 0):.1f}"))
            else:
                self._lbl_stats_audio.config(
                    fg=MUTED, text=f"{'AUDIO':6}{'n/a':>15}{'n/a':>15}")

            # TOTAL : c'est ce chiffre qui se compare au debit du VPS.
            # L'audio n'y entre que s'il est disponible (pont frais).
            t_in  = _avg('pos_in')  + (_avg('aud_in')  if a else 0.0)
            t_out = _avg('pos_out') + (_avg('aud_out') if a else 0.0)
            suffix = "" if a else "  (positions seules)"
            self._lbl_stats_total.config(
                text=(f"{'TOTAL':6}{_u(t_in):>15}{_u(t_out):>15}   "
                      f"kbit/s moyen 60s (M = Mbit/s){suffix}"))
        self._safe_after(_do)

    def refresh_all(self):
        self.refresh_players()
        self.refresh_channels()
        self.refresh_profiles()
        self.refresh_anonymous()
        # [21/09/2026] Les quatre precedentes se replacent d'elles-memes
        # sur le thread Tk ; celle-ci non, et refresh_all est appele
        # depuis le fil WebSocket (admin_welcome).
        self._safe_after(self._peindre_mdp_serveur)

    def refresh_anonymous(self):
        def _do():
            if state.anonymous_mode:
                self._btn_anon.config(text="Mode anonyme : ON",
                                      bg=ORANGE, fg=BG)
            else:
                self._btn_anon.config(text="Mode anonyme : OFF",
                                      bg=BORDER, fg=MUTED)
        self._safe_after(_do)

    def refresh_channels(self):
        def _do():
            for w in self._channels_frame.winfo_children():
                w.destroy()
            if not state.channels:
                tk.Label(self._channels_frame, text="(aucun canal)",
                         bg=BG_PANEL, fg=MUTED, font=("Courier", 8),
                         anchor="w", padx=4).pack(fill="x")
                self._refresh_all_player_profile_select()
                return
            for ch in state.channels:
                row = tk.Frame(self._channels_frame, bg=BG_ROW, pady=2, padx=6)
                row.pack(fill="x", pady=1)
                tk.Label(row, text=ch, bg=BG_ROW, fg=TEXT,
                         font=("Courier", 9), anchor="w"
                         ).pack(side="left", fill="x", expand=True)
                btn_ren = tk.Label(row, text="✎", bg=BG_ROW, fg=BLUE,
                                   font=("Courier", 9, "bold"),
                                   cursor="hand2", padx=4)
                btn_ren.pack(side="left")
                btn_ren.bind("<Button-1>",
                             lambda e, n=ch: self._rename_channel(n))
                btn_del = tk.Label(row, text="✕", bg=BG_ROW, fg=RED,
                                   font=("Courier", 9, "bold"),
                                   cursor="hand2", padx=4)
                btn_del.pack(side="left")
                btn_del.bind("<Button-1>",
                             lambda e, n=ch: self._remove_channel(n))
        self._safe_after(_do)

    def refresh_profiles(self):
        def _do():
            for w in self._profiles_frame.winfo_children():
                w.destroy()
            if not state.profiles:
                tk.Label(self._profiles_frame,
                         text="(aucun profil)\nAjoute un profil pour pouvoir\nl'assigner aux joueurs.",
                         bg=BG_PANEL, fg=MUTED, font=("Courier", 8),
                         anchor="w", justify="left", padx=4).pack(fill="x")
                self._refresh_all_player_profile_select()
                return
            for prof in state.profiles:
                # prof est maintenant un dict {"name": str, "soundboard_allowed": bool}
                prof_name = prof.get("name") if isinstance(prof, dict) else str(prof)
                if not prof_name:
                    continue
                # Bloc englobant pour ce profil (nom + permissions)
                block = tk.Frame(self._profiles_frame, bg=BG_ROW, pady=2, padx=6)
                block.pack(fill="x", pady=1)
                # Ligne 1 : nom du profil + boutons rename/delete
                row = tk.Frame(block, bg=BG_ROW)
                row.pack(fill="x")
                tk.Label(row, text=prof_name, bg=BG_ROW, fg=PURPLE,
                         font=("Courier", 9, "bold"), anchor="w"
                         ).pack(side="left", fill="x", expand=True)
                btn_ren = tk.Label(row, text="✎", bg=BG_ROW, fg=BLUE,
                                   font=("Courier", 9, "bold"),
                                   cursor="hand2", padx=4)
                btn_ren.pack(side="left")
                btn_ren.bind("<Button-1>",
                             lambda e, n=prof_name: self._rename_profile(n))
                btn_del = tk.Label(row, text="✕", bg=BG_ROW, fg=RED,
                                   font=("Courier", 9, "bold"),
                                   cursor="hand2", padx=4)
                btn_del.pack(side="left")
                btn_del.bind("<Button-1>",
                             lambda e, n=prof_name: self._remove_profile(n))
                # Ligne 2 : case a cocher "Soundboard autorise"
                # v0.2 alpha 035. Toggle envoie set_profile_permission
                # au serveur, qui sauve + push my_profile aux clients.
                perm_row = tk.Frame(block, bg=BG_ROW)
                perm_row.pack(fill="x", pady=(2, 0))
                sb_allowed = bool(prof.get("soundboard_allowed", False)) if isinstance(prof, dict) else False
                sb_var = tk.BooleanVar(value=sb_allowed)
                # On stocke la var pour pouvoir l'inspecter, et un flag
                # pour ignorer le 1er trigger du var lors de la creation.
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
                        self._on_toggle_profile_perm(n, "soundboard_allowed", v.get()),
                )
                cb.pack(side="left", padx=(12, 0))
            self._refresh_all_player_profile_select()
        self._safe_after(_do)

    def _on_toggle_profile_perm(self, profile_name: str, perm_key: str, value: bool):
        """Envoie au serveur la modification d'une permission profil.
        Le serveur sauve, broadcast aux admins (profiles_list) et push
        my_profile aux clients concernes (v0.2 alpha 035)."""
        self._send_cmd({
            "cmd":      "set_profile_permission",
            "profile":  profile_name,
            "perm_key": perm_key,
            "value":    bool(value),
        })

    def refresh_players(self):
        def _do():
            # Synchroniser : ajouter les nouveaux, retirer les partis
            current_names = set(state.players.keys())
            for n in list(self._players_rows.keys()):
                if n not in current_names:
                    self._players_rows[n]["row"].destroy()
                    del self._players_rows[n]
            if current_names:
                self._lbl_no_players.pack_forget()
            else:
                self._lbl_no_players.pack(fill="x")
            for name, info in state.players.items():
                if name not in self._players_rows:
                    self._build_player_row(name)
                self._update_player_row(name)
        self._safe_after(_do)

    def _build_player_row(self, name: str):
        row = tk.Frame(self._players_frame, bg=BG_ROW, pady=4, padx=8)
        row.pack(fill="x", pady=2, padx=2)

        # Header : nom + bouton kick
        hdr = tk.Frame(row, bg=BG_ROW)
        hdr.pack(fill="x")
        lbl_name = tk.Label(hdr, text=f"● {name}", bg=BG_ROW, fg=GREEN,
                            font=("Courier", 10, "bold"), anchor="w")
        lbl_name.pack(side="left", fill="x", expand=True)
        btn_kick = tk.Label(hdr, text="Kick", bg=BORDER, fg=RED,
                            font=("Courier", 8, "bold"),
                            cursor="hand2", padx=6, pady=2)
        btn_kick.pack(side="right")
        btn_kick.bind("<Button-1>", lambda e, n=name: self._kick_player(n))

        lbl_pos = tk.Label(row, text="position : ?", bg=BG_ROW, fg=MUTED,
                           font=("Courier", 8), anchor="w")
        lbl_pos.pack(fill="x")

        lbl_channel = tk.Label(row, text="Canal : (aucun)", bg=BG_ROW, fg=MUTED,
                               font=("Courier", 8), anchor="w")
        lbl_channel.pack(fill="x")

        # Selecteur profil
        prof_frame = tk.Frame(row, bg=BG_ROW)
        prof_frame.pack(fill="x", pady=(2, 0))

        self._players_rows[name] = {
            "row":          row,
            "lbl_name":     lbl_name,
            "lbl_pos":      lbl_pos,
            "lbl_channel":  lbl_channel,
            "prof_frame":   prof_frame,
            "prof_var":     None,
            "prof_setting": False,  # flag anti-recursion sur trace_add
        }
        self._build_profile_selector(name)

    def _update_player_row(self, name: str):
        row = self._players_rows.get(name)
        info = state.players.get(name)
        if not row or not info:
            return
        sc_online = info.get("sc_online", True)
        # Nom + indicateur hors-jeu si Star Citizen est ferme cote joueur.
        # En hors-jeu : nom en gris (au lieu de vert), suffixe "(hors-jeu)",
        # position remplacee par "SC ferme".
        if sc_online:
            row["lbl_name"].config(text=f"● {name}", fg=GREEN)
        else:
            row["lbl_name"].config(text=f"● {name}  (hors-jeu)", fg=MUTED)
        # Position
        pos = info.get("pos")
        if not sc_online:
            row["lbl_pos"].config(text="SC ferme", fg=MUTED)
        elif pos:
            x = pos.get("x", 0) / 1000
            y = pos.get("y", 0) / 1000
            z = pos.get("z", 0) / 1000
            row["lbl_pos"].config(text=f"X:{x:.3f} Y:{y:.3f} Z:{z:.3f} km",
                                   fg=TEXT)
        else:
            row["lbl_pos"].config(text="position en attente...", fg=MUTED)
        # Canal
        ch = info.get("channel")
        row["lbl_channel"].config(
            text=f"Canal : {ch}" if ch else "Canal : (aucun)",
            fg=TEXT if ch else MUTED,
        )
        # Profil : reflechir l'etat dans le selecteur
        prof = info.get("profile")
        var = row.get("prof_var")
        if var is not None:
            target = prof if prof else "(aucun)"
            if var.get() != target:
                row["prof_setting"] = True
                try:
                    var.set(target)
                finally:
                    row["prof_setting"] = False

    def _build_profile_selector(self, name: str):
        """Cree (ou recree) le menu deroulant de profil pour ce joueur."""
        row = self._players_rows.get(name)
        info = state.players.get(name)
        if not row or not info:
            return
        prof_frame = row["prof_frame"]
        for w in prof_frame.winfo_children():
            w.destroy()
        if not state.profiles:
            return  # rien a afficher
        current = info.get("profile") or "(aucun)"
        var = tk.StringVar(value=current)
        row["prof_var"] = var
        row["prof_setting"] = True

        tk.Label(prof_frame, text="Profil :", bg=BG_ROW, fg=MUTED,
                 font=("Courier", 8)).pack(side="left", padx=(0, 4))
        menu = tk.OptionMenu(prof_frame, var, "(aucun)", *_profile_names())
        menu.config(bg=BG_PANEL, fg=PURPLE, font=("Courier", 8),
                    activebackground=BORDER, activeforeground=TEXT,
                    relief="flat", bd=0, padx=4, pady=0,
                    highlightthickness=0)
        menu["menu"].config(bg=BG_PANEL, fg=TEXT, font=("Courier", 8),
                             activebackground=BORDER, activeforeground=TEXT)
        menu.pack(side="left", fill="x", expand=True)

        def _on_change(*_):
            if row.get("prof_setting"):
                return
            sel = var.get()
            new_prof = None if sel == "(aucun)" else sel
            self._send_cmd({"cmd": "assign_profile", "player": name,
                            "profile": new_prof})

        var.trace_add("write", _on_change)
        row["prof_setting"] = False

    def _refresh_all_player_profile_select(self):
        """Reconstruit le selecteur de profil pour chaque joueur (utile
        quand state.profiles change)."""
        for name in list(self._players_rows.keys()):
            self._build_profile_selector(name)
            self._update_player_row(name)

    # ----- Commandes admin -----

    def _send_cmd(self, payload: dict):
        if not state.connected:
            messagebox.showwarning("CircusVOIP Admin",
                                   "Pas connecte au serveur",
                                   parent=self.root)
            return
        ok = _ws_send_safe(payload)
        if not ok:
            # [15/08/2026] Le nom de la commande est journalise : avec
            # plusieurs envois automatiques a la connexion, un message
            # generique ne dit pas lequel a echoue -- et donc pas quelle
            # partie de l'interface restera vide.
            self.add_log(
                f"Echec envoi commande {payload.get('cmd', '?')} "
                f"(WS pas pret)", RED)
        return ok

    # ---------------------------------------------
    #  Annuaire (admin uniquement)
    # ---------------------------------------------

    # ---------------------------------------------
    #  Urgence : chefs
    # ---------------------------------------------

    def demander_chefs(self):
        """Demande l'etat des chefs au serveur."""
        self._envoyer_avec_reprise({"cmd": "urgence_chefs"})

    def _envoyer_avec_reprise(self, payload, essais=5, delai=1000):
        """Envoie une commande, et REESSAIE si le WS n'est pas pret.

        [15/08/2026] Les commandes declenchees par un CLIC partent
        toujours : au moment ou l'admin clique, la connexion est etablie
        depuis longtemps. Celles envoyees automatiquement a la connexion
        n'ont pas ce luxe -- elles arrivent dans la seconde qui suit
        l'authentification, et un echec les perd definitivement.

        Le bandeau CHEFS resterait alors vide en silence, ce qui
        ressemble a "aucun chef n'est designe" alors que la question n'a
        simplement pas ete posee.
        """
        # _ws_send_safe directement, PAS _send_cmd : celui-ci ouvre une
        # boite de dialogue quand la connexion manque. Repetee cinq fois
        # a la connexion, elle serait insupportable -- et ces envois sont
        # automatiques, l'admin ne les a pas demandes.
        if _ws_send_safe(payload):
            return
        if essais <= 1:
            self.add_log(
                f"Abandon apres reprises : {payload.get('cmd', '?')}", RED)
            return
        try:
            self.root.after(delai, lambda: self._envoyer_avec_reprise(
                payload, essais - 1, delai))
        except Exception:
            pass

    def afficher_chefs(self, data):
        """Remplit les listes deroulantes. Thread Tk.

        Les listes sont alimentees par l'ANNUAIRE : on choisit un joueur
        existant plutot que de taper un identifiant. Ca evite les fautes
        de frappe, et surtout ca evite de deviner -- l'admin voit.
        """
        chefs = (data or {}).get("chefs") or {}
        entrees = list(self._chef_annuaire)
        libelles = ["(aucun)"]
        ids = [None]
        for e in entrees:
            pseudo = e.get("pseudo") or "?"
            num = e.get("numero")
            libelles.append(f"{pseudo} ({num})" if num else f"{pseudo}")
            ids.append(str(e.get("discord_id")))
        self._chef_ids = ids
        for role, combo in self._chef_combos.items():
            try:
                combo["values"] = libelles
                actuel = chefs.get(role) or {}
                did = str(actuel.get("discord_id") or "")
                if did and did in ids:
                    combo.set(libelles[ids.index(did)])
                else:
                    combo.set("(aucun)")
            except Exception:
                pass

    def _designer_chef(self, role):
        combo = self._chef_combos.get(role)
        if combo is None:
            return
        try:
            idx = list(combo["values"]).index(combo.get())
        except Exception:
            return
        did = (getattr(self, "_chef_ids", []) or [None])[idx] \
            if idx < len(getattr(self, "_chef_ids", [])) else None
        # L'entree "(aucun)" destitue sans remplacer : sans elle, le seul
        # moyen de retirer un chef serait d'en nommer un autre.
        self._send_cmd({"cmd": "urgence_set_chef", "role": role,
                        "discord_id": did or ""})

    def _open_annuaire(self):
        """Redemande l'annuaire au serveur.

        [REFONTE 07/09/2026] Le bouton ANNUAIRE a disparu au profit d'un
        onglet. Cette methode reste appelee a l'AFFICHAGE de l'onglet
        (cf. _sur_changement_onglet) : c'est le geste de rafraichissement
        manuel, pour l'admin qui veut etre sur de voir l'etat courant.

        La demande initiale, elle, part deja a la connexion depuis le
        15/08 (admin_welcome), et le serveur repousse la liste apres
        chaque ecriture. Cet appel n'est donc pas necessaire au
        fonctionnement -- il ne coute rien et evite de se demander si
        l'affichage est frais.
        """
        self._send_cmd({"cmd": "annuaire_list"})

    def montrer_onglet(self, nom: str):
        """Affiche une page et met la barre a jour. Thread Tk."""
        page = self._pages.get(nom)
        if page is None or nom == self._onglet_actif:
            return
        for autre in self._pages.values():
            autre.pack_forget()
        page.pack(fill="both", expand=True)
        for n, lbl in self._onglet_labels.items():
            actif = (n == nom)
            lbl.config(fg=BLUE if actif else MUTED,
                       font=self._ONGLET_ACTIF if actif
                       else self._ONGLET_INACTIF)
            # Le trait garde sa hauteur meme inactif, sinon les libelles
            # se decalent verticalement d'un onglet a l'autre.
            self._onglet_traits[n].config(bg=BLUE if actif else BG)
        self._onglet_actif = nom
        # Ouvrir l'annuaire le rafraichit : le serveur le pousse deja a
        # chaque ecriture, mais un geste explicite leve le doute.
        if nom == "ANNUAIRE" and state.connected:
            try:
                self._open_annuaire()
            except Exception:
                pass
        if nom == "MODERATION" and state.connected:
            try:
                self._demander_mod(self._mod_actif)
            except Exception:
                pass
        if nom == "LOGS" and state.connected:
            try:
                self._demander_logs()
            except Exception:
                pass

    def show_annuaire(self, entries):
        """Remplit l'onglet Annuaire. Thread Tk.

        Le serveur pousse cette liste apres CHAQUE ecriture de l'annuaire
        (AccountStore.on_change) : liaison Discord, renommage, numero,
        rotation de jeton, blocage, chef, suppression. L'onglet se met
        donc a jour tout seul.

        [PERF 07/09/2026] Deux garde-fous, parce que ce flux est
        frequent et que detruire puis recreer les lignes coute cher en
        Tkinter -- mesure : 108 ms pour 60 fiches, 276 ms pour 150,
        ressenti comme un a-coup a l'ouverture de l'onglet.

        1. Signature : si la liste est identique a la precedente, on ne
           touche a rien. C'est le cas le PLUS frequent -- ouvrir
           l'onglet redemande la liste, et la plupart des ecritures ne
           changent pas ce qu'on affiche (rotation de jeton, blocage).
        2. Reemploi : a structure identique, on reecrit les libelles au
           lieu de reconstruire les widgets. Meme patron que
           _build_player_row / _update_player_row pour les joueurs.
        """
        corps = getattr(self, "_annuaire_body", None)
        if corps is None:
            return
        entries = list(entries or [])

        signature = [_cellules_annuaire(e) for e in entries]
        # Le ban entre dans la signature : il change la couleur de la
        # ligne sans changer aucune cellule.
        bannis = [bool(e.get("banni")) for e in entries]
        signature = list(zip(signature, bannis))
        if signature == getattr(self, "_annuaire_sig", None):
            return
        anciennes = getattr(self, "_annuaire_sig", None)
        self._annuaire_sig = signature
        self._annuaire_entries = entries

        try:
            self._annuaire_count.config(text=f"{len(entries)} fiche(s)")
        except Exception:
            pass

        lignes = getattr(self, "_annuaire_rows", [])
        # Reemploi possible tant que le NOMBRE de lignes ne change pas :
        # l'ordre vient du serveur et reste stable, seul le contenu
        # bouge (un pseudo renomme, un numero attribue).
        if anciennes is not None and len(lignes) == len(entries) and entries:
            for ref, e, (valeurs, banni) in zip(lignes, entries, signature):
                for cell, txt in zip(ref["cells"], valeurs):
                    cell.config(text=txt)
                ref["entry"] = e
                self._peindre_ligne_annuaire(ref, banni)
            return

        for w in corps.winfo_children():
            w.destroy()
        self._annuaire_rows = []

        if not entries:
            tk.Label(corps, text="Aucune fiche.",
                     bg=BG_PANEL, fg=MUTED,
                     font=("Courier", 9)).pack(pady=20)
            return

        # Largeur d'un caractere, mesuree une fois : sert a convertir les
        # largeurs de colonne (en caracteres) en pixels pour wraplength.
        police = ("Courier", 9)
        try:
            larg_car = tkfont.Font(font=police).measure("0") or 7
        except Exception:
            larg_car = 7

        for e, (valeurs, banni) in zip(entries, signature):
            row = tk.Frame(corps, bg=BG_ROW, padx=8, pady=4)
            row.pack(fill="x", pady=1)
            ref = {"entry": e, "cells": [], "row": row}
            for (titre, largeur), txt in zip(_ANNUAIRE_COLS, valeurs):
                cell = tk.Label(row, text=txt, width=largeur, anchor="nw",
                                justify="left",
                                wraplength=largeur * larg_car,
                                bg=BG_ROW, fg=TEXT, font=police)
                cell.pack(side="left", fill="y",
                          padx=(0, _ANNUAIRE_GOUTTIERE))
                ref["cells"].append(cell)
            croix = tk.Label(row, text="X", bg=BG_ROW, fg=RED,
                             font=("Courier", 10, "bold"),
                             cursor="hand2", padx=6)
            croix.pack(side="right")
            # La croix lit la fiche COURANTE de sa ligne, pas celle
            # capturee a la creation : avec le reemploi, la ligne peut
            # decrire quelqu'un d'autre plus tard.
            croix.bind("<Button-1>",
                       lambda ev, r=ref: self._delete_annuaire_entry(
                           r["entry"]))
            # [BANS 22/09/2026] Bannir. Meme lecture de la fiche courante
            # que la croix.
            ban_l = tk.Label(row, text="B", bg=BG_ROW, fg=ORANGE,
                             font=("Courier", 10, "bold"),
                             cursor="hand2", padx=6)
            ban_l.pack(side="right")
            ban_l.bind("<Button-1>",
                       lambda ev, r=ref: self._bannir_fiche(r["entry"]))
            ref["croix"] = croix
            ref["ban"] = ban_l
            self._peindre_ligne_annuaire(ref, banni)
            self._annuaire_rows.append(ref)

    # Fond d'une ligne de banni : rouge sombre, assez franc pour sauter
    # aux yeux dans la liste, assez fonce pour que le texte clair reste
    # lisible.
    _ROW_BANNI = "#4a1a1d"

    def _peindre_ligne_annuaire(self, ref, banni):
        """Couleur de la ligne et bouton B selon l'etat de ban. Thread Tk.

        Appele a la creation ET au reemploi d'une ligne : avec le
        reemploi, une ligne peut passer d'un joueur banni a un autre qui
        ne l'est pas.
        """
        fond = self._ROW_BANNI if banni else BG_ROW
        for w in [ref.get("row")] + ref.get("cells", []) + [
                ref.get("croix"), ref.get("ban")]:
            if w is not None:
                try:
                    w.config(bg=fond)
                except Exception:
                    pass
        b = ref.get("ban")
        if b is None:
            return
        # Pas de B sur un banni : le ban se leve dans Moderation > Bannis,
        # un bouton qui ne ferait rien ici serait trompeur.
        try:
            if banni:
                b.pack_forget()
            elif not b.winfo_ismapped():
                b.pack(side="right", after=ref.get("croix"))
        except Exception:
            pass

    def _delete_annuaire_entry(self, entry: dict):
        """Suppression d'une fiche, avec confirmation explicite.

        Le message nomme le pseudo ET le numero : deux fiches peuvent
        avoir des pseudos proches, et c'est le numero qu'on efface
        vraiment -- il retournera dans le tirage et pourra etre
        reattribue a quelqu'un d'autre.
        """
        pseudo = entry.get("pseudo", "?")
        numero = entry.get("numero", "?")
        did = str(entry.get("discord_id", ""))
        if not did:
            messagebox.showwarning("Annuaire", "Fiche sans identifiant.",
                                   parent=self.root)
            return
        if not messagebox.askyesno(
                "Supprimer la fiche",
                f"Supprimer definitivement la fiche de '{pseudo}' "
                f"(numero {numero}) ?\n\n"
                "Le joueur devra relier son compte Discord pour rejouer, "
                "et son numero pourra etre reattribue a un autre joueur.",
                parent=self.root, default="no"):
            return
        self._send_cmd({"cmd": "annuaire_delete", "discord_id": did})

    # ---------------------------------------------
    #  Journaux du serveur (telechargement)
    # ---------------------------------------------

    _LOGS_DOSSIER = _BASE_DIR / "logs_serveur"

    def _demander_logs(self):
        if state.connected:
            self._send_cmd({"cmd": "logs_list"})

    def _choisir_source_logs(self, source):
        self._logs_source = source
        self._peindre_sources_logs()
        self.show_fichiers_logs(self._logs_fichiers)

    def _peindre_sources_logs(self):
        for src, b in self._logs_btn_src.items():
            actif = (src == self._logs_source)
            b.config(bg=BLUE if actif else BORDER, fg=BG if actif else MUTED)

    @staticmethod
    def _taille_lisible(n) -> str:
        n = float(n or 0)
        if n < 1024:
            return f"{int(n)} o"
        if n < 1024 * 1024:
            return f"{n / 1024:.0f} Ko"
        return f"{n / (1024 * 1024):.1f} Mo"

    def show_fichiers_logs(self, fichiers):
        """Remplit la liste pour la source choisie. Thread Tk."""
        self._logs_fichiers = list(fichiers or [])
        self._logs_affiches = [f for f in self._logs_fichiers
                               if f.get("source") == self._logs_source]
        lb = self._lb_logs
        lb.delete(0, "end")
        for f in self._logs_affiches:
            try:
                quand = datetime.fromtimestamp(
                    float(f.get("mtime") or 0)).strftime("%d/%m %H:%M")
            except Exception:
                quand = "?"
            lb.insert("end", f"{quand}  {self._taille_lisible(f.get('taille')):>8}"
                             f"  {f.get('nom')}")
        if not self._logs_dl:
            self._lbl_logs_etat.config(
                text=(f"{len(self._logs_affiches)} fichier(s). Double-clic ou "
                      f"Telecharger. Destination : {self._LOGS_DOSSIER}")
                if self._logs_affiches else "Aucun fichier pour cette source.",
                fg=MUTED)

    def _telecharger_logs(self):
        sel = list(self._lb_logs.curselection())
        if not sel:
            messagebox.showinfo("Journaux", "Selectionnez un ou plusieurs "
                                "fichiers (Ctrl / Maj pour plusieurs).",
                                parent=self.root)
            return
        for i in sel:
            if i >= len(self._logs_affiches):
                continue
            f = self._logs_affiches[i]
            self._logs_n += 1
            req = f"dl{self._logs_n}"
            dest_dir = self._LOGS_DOSSIER / str(f.get("source"))
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Journaux",
                                     f"Dossier impossible a creer :\n{e}",
                                     parent=self.root)
                return
            # Ecrit sous un nom temporaire : un transfert interrompu ne
            # doit pas laisser un fichier tronque qu'on prendrait pour le
            # vrai.
            final = dest_dir / str(f.get("nom"))
            self._logs_dl[req] = {"final": final,
                                  "tmp": final.with_name(final.name + ".part"),
                                  "fp": None, "recus": 0,
                                  "nom": f.get("nom")}
            if not self._send_cmd({"cmd": "logs_get", "req": req,
                                   "source": f.get("source"),
                                   "nom": f.get("nom")}):
                self._logs_dl.pop(req, None)
        self._maj_etat_logs()

    @staticmethod
    def _nom_libre(chemin: Path) -> Path:
        """Premier chemin libre : 'x.log', puis 'x (2).log', 'x (3).log'...

        On n'ECRASE JAMAIS un journal deja present : une copie annotee
        ou triee a la main disparaitrait sans un mot. Et pas de boite
        "remplacer / garder les deux" : avec la selection multiple
        (Ctrl / Maj), dix fichiers feraient dix questions d'affilee.

        Meme numerotation que l'explorateur Windows -- elle se lit sans
        explication.

        Le compteur s'arrete a 999 : passe ce point ce n'est plus un
        telechargement mais une boucle, et il vaut mieux ecraser que
        tourner sans fin sur le fil reseau.
        """
        if not chemin.exists():
            return chemin
        tige, ext = chemin.stem, chemin.suffix
        for n in range(2, 1000):
            candidat = chemin.with_name(f"{tige} ({n}){ext}")
            if not candidat.exists():
                return candidat
        return chemin

    def _recevoir_log(self, data):
        """Morceau ou fin de transfert. Fil RESEAU (ecritures disque)."""
        import base64 as _b64
        req = str(data.get("req") or "")
        t = self._logs_dl.get(req)
        if t is None:
            return
        if data.get("type") == "logs_morceau":
            if t["fp"] is None:
                t["fp"] = open(t["tmp"], "wb")
            t["fp"].write(_b64.b64decode(data.get("data") or ""))
            t["recus"] = int(data.get("index", 0)) + 1
            t["total"] = int(data.get("total", 1))
            self._safe_after(self._maj_etat_logs)
            return
        # logs_fin
        self._logs_dl.pop(req, None)
        try:
            if t["fp"] is not None:
                t["fp"].close()
        except Exception:
            pass
        if not data.get("ok"):
            try:
                t["tmp"].unlink()
            except Exception:
                pass
            self.add_log(f"[LOGS] {t['nom']} : echec "
                         f"({data.get('raison', '?')})", RED)
        else:
            if t["fp"] is None:
                # Fichier vide : aucun morceau n'est arrive.
                open(t["tmp"], "wb").close()
            final = t["final"]
            # Les journaux joueurs arrivent compresses : on les rend
            # lisibles directement, sans l'etape de decompression que
            # faisait recuperer_logs_joueurs.ps1.
            #
            # Le nom definitif n'est choisi qu'ICI, apres la
            # decompression : c'est seulement maintenant qu'on connait
            # l'extension reelle du fichier ecrit (.log et non .log.gz).
            if final.suffix == ".gz":
                import gzip as _gz
                try:
                    with _gz.open(t["tmp"], "rb") as src:
                        contenu = src.read()
                except Exception:
                    # Pas un gzip lisible : on garde l'archive telle
                    # quelle plutot que de ne rien livrer.
                    final = self._nom_libre(final)
                    t["tmp"].replace(final)
                else:
                    final = self._nom_libre(final.with_suffix(""))
                    final.write_bytes(contenu)
                    try:
                        t["tmp"].unlink()
                    except Exception:
                        pass
            else:
                final = self._nom_libre(final)
                t["tmp"].replace(final)
            self.add_log(f"[LOGS] telecharge : {final}", GREEN)
        self._safe_after(self._maj_etat_logs)

    def _maj_etat_logs(self):
        """Ligne d'etat sous la liste. Thread Tk."""
        try:
            if self._logs_dl:
                parts = []
                for t in list(self._logs_dl.values())[:3]:
                    tot = t.get("total")
                    parts.append(f"{t['nom']} "
                                 + (f"{100 * t['recus'] // tot}%" if tot
                                    else "en attente"))
                reste = len(self._logs_dl) - 3
                self._lbl_logs_etat.config(
                    text="Telechargement : " + ", ".join(parts)
                         + (f" (+{reste})" if reste > 0 else ""), fg=ORANGE)
            else:
                self._lbl_logs_etat.config(
                    text=f"Termine. Dossier : {self._LOGS_DOSSIER}", fg=GREEN)
        except Exception:
            pass

    def _ouvrir_dossier_logs(self):
        d = self._LOGS_DOSSIER
        try:
            d.mkdir(parents=True, exist_ok=True)
            import os as _os
            if hasattr(_os, "startfile"):
                _os.startfile(str(d))
            else:
                import subprocess as _sp
                _sp.Popen(["xdg-open", str(d)])
        except Exception as e:
            messagebox.showinfo("Journaux", f"Dossier : {d}\n({e})",
                                parent=self.root)

    def _montrer_mod(self, cle):
        """Affiche une sous-page de MODERATION. Thread Tk."""
        if cle not in self._mod_pages:
            return
        for c, f in self._mod_pages.items():
            f.pack_forget()
            actif = (c == cle)
            self._mod_btns[c].config(bg=BLUE if actif else BORDER,
                                     fg=BG if actif else MUTED)
        self._mod_pages[cle].pack(fill="both", expand=True)
        self._mod_actif = cle
        if state.connected and self._onglet_actif == "MODERATION":
            self._demander_mod(cle)

    def _demander_mod(self, cle):
        cmd = {"annonces": "annonces_admin_list",
               "missions": "travail_admin_list",
               "bannis": "bans_list"}.get(cle)
        if cmd:
            self._send_cmd({"cmd": cmd})

    def show_bans(self, bans):
        """Remplit la liste des bannis. Thread Tk."""
        corps = getattr(self, "_bans_body", None)
        if corps is None:
            return
        bans = list(bans or [])
        try:
            self._bans_count.config(text=f"{len(bans)} banni(s)")
        except Exception:
            pass
        for w in corps.winfo_children():
            w.destroy()
        if not bans:
            tk.Label(corps, text="Aucun compte banni.", bg=BG_PANEL,
                     fg=MUTED, font=("Courier", 9)).pack(pady=20)
            return
        police = ("Courier", 9)
        try:
            larg_car = tkfont.Font(font=police).measure("0") or 7
        except Exception:
            larg_car = 7
        for e in bans:
            row = tk.Frame(corps, bg=BG_ROW, padx=8, pady=4)
            row.pack(fill="x", pady=1)
            for (titre, largeur), txt in zip(_BANS_COLS, _cellules_ban(e)):
                tk.Label(row, text=txt, width=largeur, anchor="nw",
                         justify="left", wraplength=largeur * larg_car,
                         bg=BG_ROW, fg=TEXT, font=police).pack(
                             side="left", fill="y",
                             padx=(0, _ANNUAIRE_GOUTTIERE))
            lever = tk.Label(row, text="Lever le ban", bg=BORDER, fg=GREEN,
                             font=("Courier", 9, "bold"), cursor="hand2",
                             padx=8, pady=2)
            lever.pack(side="right")
            lever.bind("<Button-1>",
                       lambda ev, ee=dict(e): self._lever_ban(ee))

    def _lever_ban(self, e: dict):
        did = str(e.get("discord_id") or "")
        if not did:
            return
        if not messagebox.askyesno(
                "Lever le ban",
                f"Lever le ban de '{e.get('pseudo') or '?'}' "
                f"({e.get('numero') or '-'}) ?\n\nIl pourra se reconnecter "
                "immediatement.", parent=self.root, default="no"):
            return
        self._send_cmd({"cmd": "unban_account", "discord_id": did})

    def _bannir_fiche(self, entry: dict):
        """Ban depuis une fiche de l'annuaire, avec motif facultatif."""
        did = str(entry.get("discord_id") or "")
        if not did:
            return
        pseudo = entry.get("pseudo") or "?"
        if entry.get("banni"):
            messagebox.showinfo(
                "Bannir", f"'{pseudo}' est deja banni. Le ban se leve dans "
                "Moderation > Bannis.", parent=self.root)
            return
        raison = simpledialog.askstring(
            "Bannir",
            f"Bannir '{pseudo}' ({entry.get('numero') or '-'}) ?\n\n"
            "Le ban porte sur son COMPTE DISCORD : changer de pseudo ou "
            "relier a nouveau ne le leve pas.\nS'il est connecte, il est "
            "expulse tout de suite.\n\nMotif (facultatif, montre au "
            "joueur) :", parent=self.root)
        if raison is None:
            return      # Annuler
        self._send_cmd({"cmd": "ban_account", "discord_id": did,
                        "raison": raison})

    def _liste_moderation(self, page, colonnes):
        """Entete + zone defilante d'une liste de moderation.

        Rend (corps, compteur). Meme structure que l'onglet ANNONCES,
        ecrite une fois ici pour les onglets qui suivent.
        """
        head = tk.Frame(page, bg=BG_PANEL)
        head.pack(fill="x")
        tk.Frame(head, bg=BG_PANEL, width=8).pack(side="left")
        for titre, largeur in colonnes:
            tk.Label(head, text=titre, width=largeur, anchor="w",
                     bg=BG_PANEL, fg=MUTED,
                     font=("Courier", 9, "bold")).pack(
                         side="left", padx=(0, _ANNUAIRE_GOUTTIERE))
        compteur = tk.Label(head, text="", bg=BG_PANEL, fg=MUTED,
                            font=("Courier", 9))
        compteur.pack(side="right")

        outer = tk.Frame(page, bg=BG_PANEL)
        outer.pack(fill="both", expand=True, pady=(6, 0))
        canvas = tk.Canvas(outer, bg=BG_PANEL, bd=0, highlightthickness=0)
        scroll = tk.Scrollbar(outer, orient="vertical", command=canvas.yview,
                              bg=BORDER, troughcolor=BG_PANEL,
                              activebackground=BLUE, width=12)
        corps = tk.Frame(canvas, bg=BG_PANEL)
        win = canvas.create_window((0, 0), window=corps, anchor="nw")
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(win, width=e.width))
        corps.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        tk.Label(corps, text="(non connecte)", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 9), pady=8).pack(fill="x")
        return corps, compteur

    def show_missions(self, missions):
        """Remplit l'onglet Missions. Thread Tk. Meme logique que
        show_annonces (signature, puis reconstruction)."""
        corps = getattr(self, "_missions_body", None)
        if corps is None:
            return
        missions = list(missions or [])
        signature = [(m.get("id"),) + _cellules_mission(m) for m in missions]
        if signature == getattr(self, "_missions_sig", None):
            return
        self._missions_sig = signature
        try:
            self._missions_count.config(text=f"{len(missions)} mission(s)")
        except Exception:
            pass
        for w in corps.winfo_children():
            w.destroy()
        if not missions:
            tk.Label(corps, text="Aucune mission en cours.",
                     bg=BG_PANEL, fg=MUTED,
                     font=("Courier", 9)).pack(pady=20)
            return
        police = ("Courier", 9)
        try:
            larg_car = tkfont.Font(font=police).measure("0") or 7
        except Exception:
            larg_car = 7
        for m in missions:
            row = tk.Frame(corps, bg=BG_ROW, padx=8, pady=4)
            row.pack(fill="x", pady=1)
            for (titre, largeur), txt in zip(_MISSIONS_COLS,
                                             _cellules_mission(m)):
                tk.Label(row, text=txt, width=largeur, anchor="nw",
                         justify="left", wraplength=largeur * larg_car,
                         bg=BG_ROW, fg=TEXT, font=police).pack(
                             side="left", fill="y",
                             padx=(0, _ANNUAIRE_GOUTTIERE))
            croix = tk.Label(row, text="X", bg=BG_ROW, fg=RED,
                             font=("Courier", 10, "bold"),
                             cursor="hand2", padx=6)
            croix.pack(side="right")
            croix.bind("<Button-1>",
                       lambda ev, mm=dict(m): self._retirer_mission(mm))

    def _retirer_mission(self, m: dict):
        """Retrait d'une mission par l'admin, avec confirmation.

        Le message dit si la mission est PRISE : dans ce cas, un joueur
        est peut-etre deja en route, et il perd sa mission en cours.
        """
        mid = str(m.get("id") or "")
        if not mid:
            return
        prise = ""
        if m.get("executant"):
            prise = (f"\n\nElle est PRISE par "
                     f"{_qui(m.get('executant'), m.get('pseudo_executant'))}"
                     f" : il perdra sa mission en cours.")
        if not messagebox.askyesno(
                "Retirer la mission",
                f"Retirer la mission « {m.get('titre') or '?'} » de "
                f"{_qui(m.get('auteur'), m.get('pseudo_auteur'))} ?"
                f"{prise}\n\nElle disparait immediatement chez tous les "
                "joueurs. Personne n'est prevenu.",
                parent=self.root, default="no"):
            return
        self._send_cmd({"cmd": "travail_admin_retirer", "id": mid})

    def show_annonces(self, annonces):
        """Remplit l'onglet Annonces. Thread Tk.

        Reconstruction complete, sans reemploi : contrairement a
        l'annuaire, ce flux ne part qu'a une publication ou un retrait
        (30 s minimum entre deux publications d'un meme joueur, 200
        annonces au plus). Seule la signature evite de tout refaire pour
        une liste identique -- le cas de l'ouverture de l'onglet.
        """
        corps = getattr(self, "_annonces_body", None)
        if corps is None:
            return
        annonces = list(annonces or [])
        signature = [(a.get("id"),) + _cellules_annonce(a) for a in annonces]
        if signature == getattr(self, "_annonces_sig", None):
            return
        self._annonces_sig = signature
        try:
            self._annonces_count.config(text=f"{len(annonces)} annonce(s)")
        except Exception:
            pass
        for w in corps.winfo_children():
            w.destroy()
        if not annonces:
            tk.Label(corps, text="Aucune annonce en cours.",
                     bg=BG_PANEL, fg=MUTED,
                     font=("Courier", 9)).pack(pady=20)
            return
        police = ("Courier", 9)
        try:
            larg_car = tkfont.Font(font=police).measure("0") or 7
        except Exception:
            larg_car = 7
        for a in annonces:
            row = tk.Frame(corps, bg=BG_ROW, padx=8, pady=4)
            row.pack(fill="x", pady=1)
            for (titre, largeur), txt in zip(_ANNONCES_COLS,
                                             _cellules_annonce(a)):
                tk.Label(row, text=txt, width=largeur, anchor="nw",
                         justify="left", wraplength=largeur * larg_car,
                         bg=BG_ROW, fg=TEXT, font=police).pack(
                             side="left", fill="y",
                             padx=(0, _ANNUAIRE_GOUTTIERE))
            croix = tk.Label(row, text="X", bg=BG_ROW, fg=RED,
                             font=("Courier", 10, "bold"),
                             cursor="hand2", padx=6)
            croix.pack(side="right")
            croix.bind("<Button-1>",
                       lambda ev, aa=dict(a): self._retirer_annonce(aa))

    def _retirer_annonce(self, a: dict):
        """Retrait d'une annonce par l'admin, avec confirmation.

        Le message cite le debut du texte et l'auteur : c'est ce qu'on
        retire, et deux annonces du meme joueur se ressemblent souvent.
        """
        aid = str(a.get("id") or "")
        if not aid:
            return
        extrait = " ".join(str(a.get("description") or "").split())
        if len(extrait) > 80:
            extrait = extrait[:77] + "..."
        if not messagebox.askyesno(
                "Retirer l'annonce",
                f"Retirer l'annonce de '{a.get('pseudo') or '?'}' "
                f"({a.get('auteur') or '?'}) ?\n\n« {extrait} »\n\n"
                "Elle disparait immediatement chez tous les joueurs. "
                "L'auteur n'est pas prevenu.",
                parent=self.root, default="no"):
            return
        self._send_cmd({"cmd": "annonces_admin_retirer", "id": aid})

    def _add_channel(self):
        n = simpledialog.askstring("Ajouter canal", "Nom du nouveau canal :",
                                   parent=self.root)
        if n:
            self._send_cmd({"cmd": "add_channel", "name": n})

    def _rename_channel(self, old: str):
        n = simpledialog.askstring("Renommer canal",
                                   f"Nouveau nom pour '{old}' :",
                                   initialvalue=old, parent=self.root)
        if n and n != old:
            self._send_cmd({"cmd": "rename_channel", "old": old, "new": n})

    def _remove_channel(self, name: str):
        if messagebox.askyesno("Supprimer canal",
                               f"Supprimer le canal '{name}' ?",
                               parent=self.root):
            self._send_cmd({"cmd": "remove_channel", "name": name})

    def _add_profile(self):
        n = simpledialog.askstring("Ajouter profil", "Nom du nouveau profil :",
                                   parent=self.root)
        if n:
            self._send_cmd({"cmd": "add_profile", "name": n})

    def _rename_profile(self, old: str):
        n = simpledialog.askstring("Renommer profil",
                                   f"Nouveau nom pour '{old}' :",
                                   initialvalue=old, parent=self.root)
        if n and n != old:
            self._send_cmd({"cmd": "rename_profile", "old": old, "new": n})

    def _remove_profile(self, name: str):
        if messagebox.askyesno("Supprimer profil",
                               f"Supprimer le profil '{name}' ?\n"
                               f"Les joueurs assignes le perdront.",
                               parent=self.root):
            self._send_cmd({"cmd": "remove_profile", "name": name})

    def _toggle_anonymous(self):
        self._send_cmd({"cmd": "set_anonymous_mode",
                        "active": not state.anonymous_mode})

    def _kick_player(self, name: str):
        if messagebox.askyesno("Kick joueur",
                               f"Kicker le joueur '{name}' ?",
                               parent=self.root):
            self._send_cmd({"cmd": "kick_player", "name": name})

    def _redemarrer_serveur(self):
        """Demande au serveur de sortir. systemd le relance.

        Confirmation obligatoire, valeur par defaut sur Non : l'action
        coupe la voix de tous les joueurs connectes, et rien ne permet de
        l'annuler une fois partie. Meme regle que le retrait d'une
        mission dans le CircusPhone.
        """
        if not state.connected:
            messagebox.showwarning("Redemarrage",
                                   "Pas de connexion au serveur.",
                                   parent=self.root)
            return
        nb = len(state.players)
        detail = (f"\n\n{nb} joueur(s) connecte(s) seront deconnectes."
                  if nb else "")
        if not messagebox.askyesno(
                "Redemarrer le serveur",
                "Redemarrer CircusVOIP Server ?" + detail +
                "\n\nLe service repart tout seul en quelques secondes "
                "(systemd). La voix est coupee pendant ce temps.",
                parent=self.root, default="no"):
            return
        self._send_cmd({"cmd": "restart_server"})
        self.add_log("Redemarrage demande.", ORANGE)

    def _copier_mdp_serveur(self):
        """Copie le mot de passe joueur dans le presse-papiers.

        Copie la VALEUR meme si l'affichage est masque : le masquage
        protege l'ecran, pas le presse-papiers -- c'est justement pour
        transmettre le mot de passe sans l'afficher qu'on copie.

        update() apres append : sous Windows, le presse-papiers Tk n'est
        transmis au systeme qu'au prochain passage dans la boucle
        d'evenements. Sans cet appel, coller dans une autre application
        juste apres le clic rend l'ancien contenu.
        """
        tok = state.server_token or ""
        if not tok:
            self.add_log("Aucun mot de passe a copier.", MUTED)
            return
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(tok)
            self.root.update()
            self._btn_copier_mdp.config(text="Copie !", fg=GREEN)
            self.root.after(
                1500,
                lambda: self._btn_copier_mdp.config(text="Copier", fg=MUTED))
        except Exception as e:
            self.add_log(f"Copie impossible : {e}", RED)

    def _change_server_token(self):
        n = simpledialog.askstring("Modifier le mot de passe joueur",
                                   "Nouveau mot de passe (champ MDP du client) :",
                                   parent=self.root)
        if n:
            self._send_cmd({"cmd": "set_server_token", "token": n})


# ---------------------------------------------
#  Main
# ---------------------------------------------

if __name__ == "__main__":
    try:
        AdminUI()
    except Exception as e:
        import traceback
        traceback.print_exc()
        input("Appuyez sur Entree pour fermer...")
