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
import secrets
import time
import threading
import sys
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

from circusvoip_server_config import get_token

# ---------------------------------------------
#  Config
# ---------------------------------------------

AUDIO_PORT = 8889
SERVER_TOKEN = get_token()  # meme token que serveur positions

# [P3] Limite le nombre de connexions audio simultanees pour eviter qu'un
# attaquant ouvre des milliers de WS et sature la machine (broadcast O(N)).
# 64 = large pour une communaute de 10-50, sans laisser la porte ouverte.
MAX_AUDIO_CLIENTS = 64

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
    running = False
    bytes_total     = 0
    frames_total    = 0
    last_report_ts  = 0.0

state = State()

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
                        ui.log(f"REFUSE audio : token invalide "
                               f"(client: {data.get('name', '?')}, ip: {peer_ip})")
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

                    name = data.get("name", "Anonymous")
                    state.clients[ws] = name
                    ui.log(f"JOIN audio : {name}  ({len(state.clients)} client(s))")
                    ui.refresh_clients()

                elif data.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))

    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        ui.log(f"Erreur handler: {e}")
    finally:
        if ws in state.clients:
            n = state.clients.pop(ws)
            ui.log(f"LEAVE audio : {n}  ({len(state.clients)} client(s))")
            ui.refresh_clients()

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
        state.clients.pop(ws, None)

async def _report_stats(ui):
    """Affiche les stats de bande passante toutes les 5s."""
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

async def _serve(ui):
    state.running = True
    async with websockets.serve(
        lambda ws: handler(ws, ui),
        "0.0.0.0",
        AUDIO_PORT,
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
    console (stdout) au lieu d'une fenetre Tkinter."""

    def log(self, msg: str):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {msg}", flush=True)

    def refresh_clients(self):
        # Pas d'affichage de liste en headless (les events sont logges)
        pass

    def update_stats(self, kbs: float, frames: int, n_clients: int):
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] [STATS] {n_clients} client(s) | "
              f"{frames} trames/s | {kbs:.1f} kB/s", flush=True)


def _run_headless():
    """Lance l'audio server sans UI Tkinter."""
    masked = SERVER_TOKEN[:4] + "***" if len(SERVER_TOKEN) >= 4 else "***"
    print("=" * 60)
    print("CircusVOIP Audio Server - mode headless")
    print(f"Port audio   : {AUDIO_PORT}")
    print(f"Token        : {masked}  (visible complet dans la config)")
    print(f"Cap clients  : {MAX_AUDIO_CLIENTS}")
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
