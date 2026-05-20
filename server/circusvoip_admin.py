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
from tkinter import simpledialog, messagebox

import websockets

# ---------------------------------------------
#  Config (persiste IP + token admin entre lancements)
# ---------------------------------------------

_BASE_DIR   = Path(__file__).resolve().parent
CONFIG_FILE = _BASE_DIR / "circusvoip_admin_config.json"
SERVER_PORT = 8888  # le port joueur, l'admin partage le meme port

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

state = State()


def _ws_send_safe(payload: dict) -> bool:
    """Envoi WS thread-safe : appelable depuis le thread Tkinter."""
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
    uri = f"wss://{state.server_ip}:{SERVER_PORT}"
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


def _handle_message(ui, data: dict):
    """Traite un message recu du serveur."""
    msg_type = data.get("type")

    if msg_type == "admin_welcome":
        # Etat initial
        state.channels        = list(data.get("channels", []))
        # v0.2 alpha 035 : profiles peut etre list[str] (vieux serveur)
        # ou list[dict] avec permissions (nouveau serveur). Normalise
        # toujours en list[dict].
        state.profiles        = _normalize_profiles_list(data.get("profiles", []))
        state.anonymous_mode  = bool(data.get("anonymous_mode", False))
        state.server_token    = data.get("server_token", "")
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

class AdminUI:
    def __init__(self):
        self._cfg = _load_cfg()
        state.server_ip   = self._cfg.get("server_ip", "127.0.0.1")
        state.admin_token = self._cfg.get("admin_token", "")

        # Forcer un AppUserModelID distinct sur Windows AVANT tk.Tk().
        # Permet a Windows d'utiliser notre icone StarCircus_Admin.ico
        # dans la taskbar au lieu de l'icone Python par defaut.
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "CircusVOIP.Admin.0.1"
            )
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("CircusVOIP - Admin 0.1")
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
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    # ----- Construction UI -----

    def _build_ui(self):
        # Header (connexion)
        header = tk.Frame(self.root, bg=BG, pady=8)
        header.pack(fill="x", padx=12)
        tk.Label(header, text="CircusVOIP Admin", bg=BG, fg=PURPLE,
                 font=("Courier", 13, "bold")).pack(side="left")
        self._lbl_status = tk.Label(header, text="Deconnecte", bg=BG, fg=MUTED,
                                    font=("Courier", 9))
        self._lbl_status.pack(side="right")

        # Barre connexion : IP + token + bouton
        conn = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=6)
        conn.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(conn, text="Serveur IP :", bg=BG_PANEL, fg=TEXT,
                 font=("Courier", 9)).pack(side="left")
        self._entry_ip = tk.Entry(conn, bg=BG_ROW, fg=TEXT, font=("Courier", 9),
                                  insertbackground=TEXT, relief="flat", bd=4, width=20)
        self._entry_ip.insert(0, state.server_ip)
        self._entry_ip.pack(side="left", padx=(4, 12))

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

        # Body : 3 colonnes (logs gauche, joueurs centre, canaux/profils droite)
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        # Colonne gauche : logs
        left = tk.Frame(body, bg=BG_PANEL, padx=8, pady=8)
        left.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._section(left, "LOGS")
        log_frame = tk.Frame(left, bg=BG_PANEL)
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
        # Bouton clear
        clear_frame = tk.Frame(left, bg=BG_PANEL)
        clear_frame.pack(fill="x", pady=(4, 0))
        btn_clear = tk.Label(clear_frame, text="Clear logs", bg=BORDER, fg=MUTED,
                             font=("Courier", 8), cursor="hand2", padx=8, pady=2)
        btn_clear.pack(side="right")
        btn_clear.bind("<Button-1>", lambda e: self._clear_logs())

        # Colonne centre : joueurs
        center = tk.Frame(body, bg=BG_PANEL, padx=8, pady=8, width=400)
        center.pack(side="left", fill="both", expand=True, padx=6)
        center.pack_propagate(False)
        self._section(center, "JOUEURS CONNECTES")

        # Bandeau actions globales (mode anonyme)
        actions = tk.Frame(center, bg=BG_PANEL, pady=4)
        actions.pack(fill="x")
        self._btn_anon = tk.Label(actions, text="Mode anonyme : OFF",
                                  bg=BORDER, fg=MUTED,
                                  font=("Courier", 9, "bold"), cursor="hand2",
                                  padx=10, pady=4)
        self._btn_anon.pack(side="left")
        self._btn_anon.bind("<Button-1>", lambda e: self._toggle_anonymous())

        # Liste joueurs scrollable
        plist_outer = tk.Frame(center, bg=BG_PANEL)
        plist_outer.pack(fill="both", expand=True, pady=(4, 0))
        self._p_canvas = tk.Canvas(plist_outer, bg=BG_PANEL, bd=0, highlightthickness=0)
        p_scroll = tk.Scrollbar(plist_outer, orient="vertical",
                                command=self._p_canvas.yview,
                                bg=BORDER, troughcolor=BG_PANEL,
                                activebackground=BLUE, width=12)
        self._p_canvas.configure(yscrollcommand=p_scroll.set)
        p_scroll.pack(side="right", fill="y")
        self._p_canvas.pack(side="left", fill="both", expand=True)
        self._players_frame = tk.Frame(self._p_canvas, bg=BG_PANEL)
        self._p_window = self._p_canvas.create_window((0, 0),
                                                      window=self._players_frame,
                                                      anchor="nw")

        def _on_p_resize(event):
            self._p_canvas.itemconfig(self._p_window, width=event.width)
        self._p_canvas.bind("<Configure>", _on_p_resize)

        def _on_p_inner_resize(_e):
            self._p_canvas.configure(scrollregion=self._p_canvas.bbox("all"))
        self._players_frame.bind("<Configure>", _on_p_inner_resize)

        def _on_p_wheel(event):
            self._p_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._p_canvas.bind("<Enter>",
            lambda e: self._p_canvas.bind_all("<MouseWheel>", _on_p_wheel))
        self._p_canvas.bind("<Leave>",
            lambda e: self._p_canvas.unbind_all("<MouseWheel>"))

        self._lbl_no_players = tk.Label(self._players_frame,
                                         text="(aucun joueur connecte)",
                                         bg=BG_PANEL, fg=MUTED,
                                         font=("Courier", 8), pady=6)
        self._lbl_no_players.pack(fill="x")

        # Colonne droite : canaux + profils
        right = tk.Frame(body, bg=BG_PANEL, padx=8, pady=8, width=280)
        right.pack(side="left", fill="y", padx=(6, 0))
        right.pack_propagate(False)

        # Canaux radio
        self._section(right, "CANAUX RADIO")
        self._channels_frame = tk.Frame(right, bg=BG_PANEL)
        self._channels_frame.pack(fill="x", pady=2)
        btn_add_ch = tk.Label(right, text="+  Ajouter canal", bg=BORDER, fg=BLUE,
                              font=("Courier", 9, "bold"), pady=6, padx=8,
                              cursor="hand2")
        btn_add_ch.pack(fill="x", pady=(2, 8))
        btn_add_ch.bind("<Button-1>", lambda e: self._add_channel())

        # Profils
        self._section(right, "PROFILS")
        self._profiles_frame = tk.Frame(right, bg=BG_PANEL)
        self._profiles_frame.pack(fill="x", pady=2)
        btn_add_prof = tk.Label(right, text="+  Ajouter profil",
                                bg=BORDER, fg=PURPLE,
                                font=("Courier", 9, "bold"), pady=6, padx=8,
                                cursor="hand2")
        btn_add_prof.pack(fill="x", pady=(2, 8))
        btn_add_prof.bind("<Button-1>", lambda e: self._add_profile())

        # Token serveur en bas (info admin)
        self._section(right, "TOKEN JOUEUR")
        self._lbl_server_token = tk.Label(right, text="(non connecte)",
                                           bg=BG_ROW, fg=BLUE,
                                           font=("Courier", 8), pady=4, padx=6,
                                           anchor="w", justify="left", wraplength=240)
        self._lbl_server_token.pack(fill="x", pady=(0, 4))
        btn_change_token = tk.Label(right, text="Modifier token joueur",
                                     bg=BORDER, fg=MUTED,
                                     font=("Courier", 8), cursor="hand2",
                                     padx=8, pady=4)
        btn_change_token.pack(fill="x")
        btn_change_token.bind("<Button-1>", lambda e: self._change_server_token())

    def _section(self, parent, title: str):
        tk.Label(parent, text=title, bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 8, "bold"), anchor="w"
                 ).pack(fill="x", pady=(2, 4))

    # ----- Connexion -----

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
        self._safe_after(_do)

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
        try:
            self.root.after(0, callback)
        except Exception:
            pass

    # ----- Refresh -----

    def refresh_all(self):
        self.refresh_players()
        self.refresh_channels()
        self.refresh_profiles()
        self.refresh_anonymous()
        self._lbl_server_token.config(text=state.server_token or "(non recu)")

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
            self.add_log("Echec envoi commande (WS pas pret)", RED)

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

    def _change_server_token(self):
        n = simpledialog.askstring("Modifier token joueur",
                                   "Nouveau token (mot de passe joueur) :",
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
