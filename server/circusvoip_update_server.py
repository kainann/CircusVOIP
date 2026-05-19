# -*- coding: utf-8 -*-
# =============================================
#  CircusVOIP Update Server
# =============================================
# Serveur HTTP statique qui sert les fichiers de mise a jour pour le client
# CircusVOIP. Le client interroge :
#   - GET http://<server>:8080/manifest.json    -> meta-donnees de version
#   - GET http://<server>:8080/files/<nom>.py   -> fichiers a telecharger
#   - GET http://<server>:8080/pip_packages/... -> wheels python optionnels
#
# Lancement : py -3 circusvoip_update_server.py [--headless]
# Le port 8080 doit etre ouvert dans le firewall du serveur (ufw allow 8080).
#
# Securite :
#   - Pas d'authentification : les fichiers sont publics. Si tu pousses du
#     code sensible, mets-le ailleurs.
#   - Pas de chiffrement (HTTP clair). Acceptable pour usage prive entre amis,
#     a renforcer avec HTTPS+domaine si tu publies plus largement.
#   - Restriction au dossier UPDATES_DIR : impossible de telecharger en
#     dehors via path traversal (../) grace a SimpleHTTPRequestHandler qui
#     normalise les chemins.
# =============================================

import json
import sys
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from datetime import datetime

# ---------------------------------------------
#  Config
# ---------------------------------------------

import os

_BASE_DIR    = Path(__file__).resolve().parent

# Dossier des fichiers a servir. Par defaut, on cherche un dossier 'updates'
# a cote du script. En production sur le VPS Hetzner, on l'override via
# variable d'environnement CIRCUSVOIP_UPDATES_DIR pour pointer vers
# /home/circusvoip/updates/ (a cote de app/, pas dedans). Ca separe
# proprement les fichiers de l'app et les fichiers de release.
_default_updates_dir = _BASE_DIR / "updates"
_env_updates_dir = os.environ.get("CIRCUSVOIP_UPDATES_DIR", "").strip()
if _env_updates_dir:
    UPDATES_DIR = Path(_env_updates_dir).resolve()
else:
    UPDATES_DIR = _default_updates_dir
HTTP_PORT    = 8080

# ---------------------------------------------
#  Tkinter (mode UI seulement, optionnel)
# ---------------------------------------------

if "--headless" not in sys.argv:
    try:
        import circusvoip_dpi
        circusvoip_dpi.enable_dpi_awareness()
    except Exception:
        pass
    import tkinter as tk
else:
    tk = None  # mode headless

# Theme (identique aux autres binaires CircusVOIP)
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


# ---------------------------------------------
#  Etat global
# ---------------------------------------------

class State:
    server      = None  # ThreadingHTTPServer instance
    server_thr  = None  # thread qui run le ThreadingHTTPServer
    running     = False
    request_log = []    # liste de tuples (ts, ip, path) pour l'UI
    request_max = 200   # taille max du log en memoire

state = State()


# ---------------------------------------------
#  HTTP Handler
# ---------------------------------------------

class _UpdateHandler(SimpleHTTPRequestHandler):
    """Sert les fichiers de UPDATES_DIR, log les requetes pour l'UI.
    Heritage de SimpleHTTPRequestHandler -> protection automatique contre
    path traversal (../../etc/passwd impossible).

    Timeout sur la requete : sans ca, une connexion qui ouvre le socket
    sans envoyer de requete HTTP complete bloque le thread handler
    indefiniment. En 2 jours, on a vu le serveur freezer avec 6 connexions
    en backlog (ss -ltnp Recv-Q=6) : un client zombie a paralyse tout. Avec
    ThreadingHTTPServer, ce blocage ne paralyse plus que SON thread, mais
    sans timeout les threads zombies s'accumuleraient lentement et
    finiraient par saturer la machine. Le timeout coupe court a ces
    connexions mortes apres 30s d'inactivite.
    """

    # 30 secondes : assez long pour gerer une connexion lente sur reseau
    # mobile/satellite, assez court pour ne pas laisser pourrir un zombie.
    timeout = 30

    # On surcharge directory pour servir UPDATES_DIR (par defaut SimpleHTTP
    # sert le cwd, pas pratique avec systemd).
    def __init__(self, *args, **kwargs):
        kwargs["directory"] = str(UPDATES_DIR)
        super().__init__(*args, **kwargs)

    def log_message(self, format, *args):
        """Surcharge le logging : on stocke en memoire pour l'UI au lieu
        de bombarder stderr."""
        try:
            ip = self.client_address[0] if self.client_address else "?"
            ts = datetime.now().strftime("%H:%M:%S")
            msg = format % args
            entry = (ts, ip, msg)
            state.request_log.append(entry)
            # Garder seulement les N derniers
            if len(state.request_log) > state.request_max:
                state.request_log = state.request_log[-state.request_max:]
            # Print aussi en console (utile en headless).
            # flush=True : indispensable pour que journalctl voie les logs
            # en temps reel. Sans ca, stdout est buffered par defaut quand
            # le process tourne sans terminal (cas systemd) et journalctl
            # affiche -- No entries -- meme si le service tourne et traite
            # des requetes (bug observe le 06/05/2026 : 2j13h sans aucun
            # log dans journalctl).
            print(f"[{ts}] {ip} {msg}", flush=True)
            # Notifier l'UI s'il existe
            if state.ui:
                try:
                    state.ui.on_request(ts, ip, msg)
                except Exception:
                    pass
        except Exception:
            pass


# State.ui reference (init plus tard)
State.ui = None


def _ensure_updates_dir():
    """Cree le dossier updates/ s'il n'existe pas, avec un manifest vide."""
    UPDATES_DIR.mkdir(exist_ok=True)
    (UPDATES_DIR / "files").mkdir(exist_ok=True)
    (UPDATES_DIR / "pip_packages").mkdir(exist_ok=True)
    manifest_path = UPDATES_DIR / "manifest.json"
    if not manifest_path.exists():
        # Manifest minimal par defaut
        manifest = {
            "version": "0.0.0",
            "channel": "alpha",
            "build": 0,
            "release_date": datetime.now().strftime("%Y-%m-%d"),
            "release_notes": "Aucune mise a jour disponible.",
            "files": [],
            "pip_packages": [],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2),
                                 encoding="utf-8")
        print(f"[INFO] Cree manifest.json par defaut a {manifest_path}")


def _start_http_server():
    """Demarre le serveur HTTP dans un thread.

    Utilise ThreadingHTTPServer (et non HTTPServer) : chaque connexion
    entrante est traitee dans son propre thread. Critique pour eviter
    le freeze observe le 06/05/2026 ou une connexion zombie a bloque
    serve_forever() pendant 2j13h, empechant tout traitement des MAJ.
    Avec ThreadingHTTPServer + handler.timeout = 30s, une connexion qui
    n'envoie pas de requete HTTP propre est tuee apres 30s sans paralyser
    les autres clients."""
    _ensure_updates_dir()
    try:
        state.server = ThreadingHTTPServer(("0.0.0.0", HTTP_PORT), _UpdateHandler)
        # daemon_threads=True : les threads handler ne bloquent pas l'arret
        # du process si une requete est en cours au moment d'un SIGTERM.
        state.server.daemon_threads = True
    except OSError as e:
        msg = f"Impossible d'ouvrir le port {HTTP_PORT} : {e}"
        print(f"[ERREUR] {msg}", flush=True)
        if state.ui:
            state.ui.log(msg, RED)
        return False
    state.running = True

    def _serve():
        try:
            state.server.serve_forever()
        except Exception as e:
            print(f"[ERREUR] serve_forever : {e}", flush=True)
        state.running = False

    state.server_thr = threading.Thread(target=_serve, daemon=True)
    state.server_thr.start()
    msg = f"Serveur HTTP demarre sur port {HTTP_PORT}, sert {UPDATES_DIR}"
    print(f"[INFO] {msg}", flush=True)
    if state.ui:
        state.ui.log(msg, GREEN)
    return True


def _stop_http_server():
    """Arrete proprement le serveur HTTP."""
    if state.server is None:
        return
    try:
        state.server.shutdown()
        state.server.server_close()
    except Exception:
        pass
    state.running = False
    state.server     = None
    state.server_thr = None


# ---------------------------------------------
#  UI Tkinter (mode non-headless)
# ---------------------------------------------

class ServerUI:
    """UI minimaliste : etat du serveur + journal des requetes."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("CircusVOIP - Update Server")
        self.root.configure(bg=BG)
        try:
            import circusvoip_dpi
            circusvoip_dpi.apply_tk_scaling(self.root)
        except Exception:
            pass
        self.root.geometry("720x500")
        self.root.minsize(600, 400)
        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        State.ui = self
        # Demarrer auto au lancement
        _start_http_server()
        self._refresh_state()
        self.root.mainloop()

    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG, pady=8)
        header.pack(fill="x", padx=12)
        tk.Label(header, text="CircusVOIP Update Server", bg=BG, fg=BLUE,
                 font=("Courier", 13, "bold")).pack(side="left")
        self._lbl_status = tk.Label(header, text="...", bg=BG, fg=MUTED,
                                    font=("Courier", 9))
        self._lbl_status.pack(side="right")

        info = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=6)
        info.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(info, text=f"Port : {HTTP_PORT}", bg=BG_PANEL, fg=TEXT,
                 font=("Courier", 9)).pack(side="left", padx=(0, 12))
        tk.Label(info, text=f"Dossier : {UPDATES_DIR}", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 9)).pack(side="left")

        # Manifest actuel
        mf = tk.Frame(self.root, bg=BG_PANEL, padx=10, pady=6)
        mf.pack(fill="x", padx=12, pady=(0, 8))
        tk.Label(mf, text="MANIFEST", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 8, "bold")).pack(anchor="w")
        self._lbl_manifest = tk.Label(mf, text="(non charge)",
                                       bg=BG_PANEL, fg=TEXT,
                                       font=("Courier", 9), anchor="w",
                                       justify="left")
        self._lbl_manifest.pack(fill="x", pady=(2, 0))

        # Journal des requetes
        log_frame = tk.Frame(self.root, bg=BG_PANEL, padx=8, pady=8)
        log_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        tk.Label(log_frame, text="REQUETES", bg=BG_PANEL, fg=MUTED,
                 font=("Courier", 8, "bold")).pack(anchor="w")
        txt_frame = tk.Frame(log_frame, bg=BG_PANEL)
        txt_frame.pack(fill="both", expand=True, pady=(4, 0))
        self._txt = tk.Text(txt_frame, bg=BG_ROW, fg=TEXT,
                            font=("Courier", 8), bd=0, relief="flat",
                            wrap="word")
        self._txt.pack(side="left", fill="both", expand=True)
        scroll = tk.Scrollbar(txt_frame, command=self._txt.yview,
                              bg=BORDER, troughcolor=BG_PANEL,
                              activebackground=BLUE, width=12)
        scroll.pack(side="right", fill="y")
        self._txt.config(yscrollcommand=scroll.set, state="disabled")

        self._refresh_manifest()

    def _refresh_state(self):
        if state.running:
            self._lbl_status.config(text=f"En ligne sur :{HTTP_PORT}", fg=GREEN)
        else:
            self._lbl_status.config(text="Hors ligne", fg=RED)
        self.root.after(2000, self._refresh_state)  # auto-refresh toutes les 2s

    def _refresh_manifest(self):
        try:
            with open(UPDATES_DIR / "manifest.json", "r", encoding="utf-8") as f:
                m = json.load(f)
            ver = f"{m.get('version','?')} {m.get('channel','?')} {int(m.get('build',0)):03d}"
            n_files = len(m.get("files", []))
            n_pip = len(m.get("pip_packages", []))
            txt = (f"Version : {ver}\n"
                   f"Date    : {m.get('release_date','?')}\n"
                   f"Fichiers: {n_files}    Pip packages: {n_pip}\n"
                   f"Notes   : {m.get('release_notes','')[:80]}")
            self._lbl_manifest.config(text=txt)
        except Exception as e:
            self._lbl_manifest.config(text=f"Erreur lecture manifest : {e}",
                                       fg=RED)

    def log(self, msg, color=TEXT):
        ts = datetime.now().strftime("%H:%M:%S")
        self._append(f"[{ts}] {msg}", color)

    def on_request(self, ts, ip, msg):
        """Callback appele depuis le handler HTTP a chaque requete."""
        self._append(f"[{ts}] {ip} {msg}", BLUE)

    def _append(self, line, color):
        try:
            self._txt.config(state="normal")
            tag = f"c_{color.replace('#', '')}"
            self._txt.tag_config(tag, foreground=color)
            self._txt.insert("end", line + "\n", tag)
            self._txt.see("end")
            self._txt.config(state="disabled")
        except Exception:
            pass

    def _on_close(self):
        _stop_http_server()
        try:
            self.root.destroy()
        except Exception:
            pass
        import os
        os._exit(0)


# ---------------------------------------------
#  Mode headless (pour systemd / VPS sans X)
# ---------------------------------------------

def _run_headless():
    print("=" * 60)
    print("CircusVOIP Update Server - mode headless")
    print(f"Port HTTP : {HTTP_PORT}")
    print(f"Dossier   : {UPDATES_DIR}")
    print("=" * 60)
    if not _start_http_server():
        sys.exit(1)
    print("Serveur en ecoute. Ctrl+C pour arreter.")
    try:
        # Bloquer indefiniment
        while state.running:
            try:
                state.server_thr.join(timeout=1.0)
            except Exception:
                break
    except KeyboardInterrupt:
        print("\n[INFO] Arret demande par l'utilisateur (Ctrl+C)")
        _stop_http_server()


# ---------------------------------------------
#  Point d'entree
# ---------------------------------------------

if __name__ == "__main__":
    if "--headless" in sys.argv:
        _run_headless()
    else:
        try:
            ServerUI()
        except Exception as e:
            import traceback
            traceback.print_exc()
            input("Appuyez sur Entree pour fermer...")
