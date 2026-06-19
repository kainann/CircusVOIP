"""
circusvoip_audio_rx_logger - Log audio detaille pour diagnostic crackling
========================================================================

Module autonome qui ecrit dans un fichier CSV separe tout ce qui passe par
la reception audio (cote receveur). Pas de bruit dans le log debug
principal : tout part dans :

    circusvoip_debug/audio_rx/audio_rx_<Pseudo>_<DDMMYYYY>_<HHMMSS>.csv

Activable / desactivable a chaud via enable(pseudo) / disable(). Le log
n'est PAS toujours actif : seulement quand la case a cocher "Activer le
log audio detaille" est cochee dans les Parametres.

Format CSV (1 entete + N lignes) :

    timestamp_ms,event,sender,type,size,delta_ms,q_before,q_after,outcome,
    callback_period_ms,senders_state,mix_peak_pre_tanh,mix_peak_post_tanh,
    trunc_total,silence_impl_total,cave_echo,beep,soundboard,sonnerie,
    phone_in_call,phone_peer

4 types d'events (colonne 'event') :
    RX    : 1 ligne par trame audio recue (dans feed_remote_frame)
    OUT   : 1 ligne par callback sounddevice (_on_output_block, ~50/s)
    STATS : 1 ligne par agregation 30s par sender
    CTX   : 1 ligne par transition de contexte (appel decroche, mute, etc.)

Conception :
    - Le thread sounddevice et la reception WebSocket POUSSENT dans une
      queue interne (en memoire) ; un thread writer dedie VIDE la queue
      vers le fichier en batch. Garantit zero I/O bloquante dans le path
      audio temps-reel.
    - Si la queue interne sature (> 50000 entrees), on jette les nouvelles
      lignes (on prefere perdre du log que de bloquer l'audio).
    - Rotation : si le fichier depasse 500 MB, on cree un suffixe
      _part2.csv (puis _part3, etc.).
    - Volume estime : ~50-250 events/s selon nombre de senders et modes,
      soit 80-160 MB / heure. CSV se compresse ~10x.

Volume estime :
    - 2 personnes en appel telephone : ~150 events/s = ~80 MB/h
    - 5 personnes en proximite       : ~300 events/s = ~160 MB/h
    - Session typique 2-3h           : 200-500 MB

API publique :
    enable(pseudo: str, debug_dir: Path) -> bool
        Ouvre le fichier CSV, demarre le thread writer.
        Retourne True si OK, False si echec (disque plein, perms, etc).
    disable() -> None
        Flush + ferme le fichier + arrete le thread writer.
    is_enabled() -> bool
    log_rx(sender, msg_type, size, q_before, q_after, outcome) -> None
    log_out(callback_period_ms, senders_state, mix_peak_pre, mix_peak_post,
            trunc_total, silence_impl_total, flags) -> None
    log_stats(sender, agg_json) -> None
    log_ctx(ctx_msg) -> None

Toutes les fonctions log_* sont NO-OP si is_enabled() est False : on peut
donc les appeler sans test prealable cote audio_io / core.
"""

from __future__ import annotations

import csv
import json
import queue
import threading
import time
from pathlib import Path
from typing import Optional


# ----------------------------------------------------------------------
# Constantes
# ----------------------------------------------------------------------

# Taille max d'un fichier CSV avant rotation (en octets). 500 MB est
# raisonnable : Excel ouvre ca, et c'est ~3-6h de log selon le scenario.
_MAX_FILE_SIZE = 500 * 1024 * 1024

# Taille max de la queue interne (en nombre d'events). Si on depasse, on
# jette les nouvelles lignes (back-pressure) plutot que de bloquer le path
# audio. 50000 events = ~5 minutes de log a 150 events/s.
_MAX_QUEUE_SIZE = 50000

# Periodicite de flush du writer (en secondes). Flush trop frequent = I/O
# couteuse pour rien, trop rare = perte en cas de crash. 1s est un bon
# compromis.
_FLUSH_INTERVAL_S = 1.0

# Header CSV (doit matcher l'ordre des champs dans _write_row).
_CSV_HEADER = [
    "timestamp_ms",
    "event",
    "sender",
    "type",
    "size",
    "delta_ms",
    "q_before",
    "q_after",
    "outcome",
    "callback_period_ms",
    "senders_state",
    "mix_peak_pre_tanh",
    "mix_peak_post_tanh",
    "trunc_total",
    "silence_impl_total",
    "cave_echo",
    "beep",
    "soundboard",
    "sonnerie",
    "phone_in_call",
    "phone_peer",
]


# ----------------------------------------------------------------------
# Etat global du module (singleton, pas une classe : 1 logger / processus)
# ----------------------------------------------------------------------

# Queue interne : les producteurs (audio_io, core) pushent ici, le writer
# thread pop et ecrit. queue.Queue est thread-safe par construction.
_event_queue: "queue.Queue[tuple] | None" = None

# Thread writer dedie. None tant que disable.
_writer_thread: Optional[threading.Thread] = None

# Flag d'arret du thread writer (set par disable()).
_stop_event: Optional[threading.Event] = None

# Etat global
_enabled: bool = False

# Chemins (utilises par le writer thread, definis dans enable()).
_current_file_path: Optional[Path] = None
_current_part_index: int = 1
_current_pseudo: str = ""
_current_ts_str: str = ""
_current_debug_dir: Optional[Path] = None

# Compteurs internes pour observabilite (lus par get_stats()).
_events_logged: int = 0
_events_dropped_queue_full: int = 0


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def _sanitize_pseudo(name: str) -> str:
    """Nettoie un pseudo pour en faire un fragment de nom de fichier."""
    if not name:
        return "Joueur"
    import re
    clean = re.sub(r"[^A-Za-z0-9_\-]", "_", name.strip())
    return clean or "Joueur"


def _make_csv_filename(pseudo: str, ts_str: str, part_index: int) -> str:
    """Construit le nom du fichier CSV pour une session/part donnee."""
    safe = _sanitize_pseudo(pseudo)
    if part_index <= 1:
        return f"audio_rx_{safe}_{ts_str}.csv"
    return f"audio_rx_{safe}_{ts_str}_part{part_index}.csv"


def _open_new_file(audio_rx_dir: Path, pseudo: str, ts_str: str,
                   part_index: int):
    """Ouvre un nouveau fichier CSV (cree le dossier si besoin) et y ecrit
    le header. Retourne (file_handle, csv_writer, path) ou (None, None,
    None) si echec.
    """
    try:
        audio_rx_dir.mkdir(parents=True, exist_ok=True)
        fname = _make_csv_filename(pseudo, ts_str, part_index)
        fpath = audio_rx_dir / fname
        # newline='' : recommande Python pour csv (evite les doubles \r\n
        # sur Windows). encoding utf-8 : pseudos UTF-8 safe.
        f = open(fpath, "w", newline="", encoding="utf-8")
        w = csv.writer(f)
        w.writerow(_CSV_HEADER)
        f.flush()
        return f, w, fpath
    except Exception as e:
        # Pas d'acces a un logger ici : on print stderr. Le caller
        # decidera quoi faire.
        import sys
        print(f"[AUDIO RX LOG] Echec ouverture fichier : {e}",
              file=sys.stderr)
        return None, None, None


def _writer_loop():
    """Boucle principale du thread writer. Vide la queue et ecrit en
    batch dans le CSV. Tourne tant que _stop_event n'est pas set ET que
    la queue n'est pas vide (pour ne pas perdre les events en fin de
    session).
    """
    global _current_file_path, _current_part_index

    if _event_queue is None or _stop_event is None:
        return
    if _current_debug_dir is None:
        return

    audio_rx_dir = _current_debug_dir / "audio_rx"

    # Ouverture du premier fichier
    fh, csv_writer, fpath = _open_new_file(
        audio_rx_dir, _current_pseudo, _current_ts_str, _current_part_index
    )
    if fh is None:
        return  # echec ouverture, on abandonne silencieusement
    _current_file_path = fpath

    # Compteur d'octets ecrits (approximatif, pour declencher la rotation).
    # On ne fait pas un os.path.getsize() a chaque write : trop couteux.
    # On accumule la taille des lignes ecrites (lossy mais suffisant).
    bytes_written = sum(len(s) + 1 for s in _CSV_HEADER)  # header

    last_flush = time.monotonic()

    while True:
        # Critere de sortie : stop demande ET queue vide. Tant que la
        # queue contient encore des events, on continue meme apres stop
        # pour ne pas perdre les derniers events.
        if _stop_event.is_set() and _event_queue.empty():
            break

        try:
            # Timeout court pour pouvoir verifier stop_event et flush
            # periodique.
            row = _event_queue.get(timeout=0.2)
        except queue.Empty:
            # Pas d'event, on regarde si flush periodique a faire.
            now = time.monotonic()
            if (now - last_flush) >= _FLUSH_INTERVAL_S:
                try:
                    fh.flush()
                except Exception:
                    pass
                last_flush = now
            continue

        # row = tuple aligne sur _CSV_HEADER (cf. _push_row)
        try:
            csv_writer.writerow(row)
            # Approximation taille : on prend la longueur stringifiee de
            # chaque champ + separateurs. Suffisant pour declencher
            # rotation a 500 MB +- quelques %.
            bytes_written += sum(len(str(c)) for c in row) + len(row)

            # Rotation si seuil atteint
            if bytes_written >= _MAX_FILE_SIZE:
                try:
                    fh.flush()
                    fh.close()
                except Exception:
                    pass
                _current_part_index += 1
                fh, csv_writer, fpath = _open_new_file(
                    audio_rx_dir, _current_pseudo, _current_ts_str,
                    _current_part_index
                )
                if fh is None:
                    return  # echec rotation, on abandonne
                _current_file_path = fpath
                bytes_written = sum(len(s) + 1 for s in _CSV_HEADER)
        except Exception:
            # Erreur write : on continue (peut-etre transitoire), on ne
            # crashe pas le thread writer.
            pass

        # Flush periodique
        now = time.monotonic()
        if (now - last_flush) >= _FLUSH_INTERVAL_S:
            try:
                fh.flush()
            except Exception:
                pass
            last_flush = now

    # Sortie de boucle : flush et close finaux
    try:
        fh.flush()
        fh.close()
    except Exception:
        pass


def _push_row(row: tuple):
    """Push une ligne dans la queue interne. Si la queue est pleine, on
    incremente le compteur de drops et on ignore (back-pressure). Cette
    fonction DOIT etre non-bloquante : appelee depuis le callback
    sounddevice temps-reel.
    """
    global _events_dropped_queue_full, _events_logged
    if _event_queue is None:
        return
    try:
        _event_queue.put_nowait(row)
        _events_logged += 1
    except queue.Full:
        _events_dropped_queue_full += 1


# ----------------------------------------------------------------------
# API publique
# ----------------------------------------------------------------------

def enable(pseudo: str, debug_dir: Path) -> bool:
    """Active le logger audio RX. Ouvre un nouveau fichier CSV, demarre
    le thread writer.

    pseudo    : nom du joueur (utilise dans le nom de fichier).
    debug_dir : dossier 'circusvoip_debug' du client. Le sous-dossier
                'audio_rx/' sera cree dedans automatiquement.

    Retourne True si OK, False si deja actif ou echec.
    """
    global _event_queue, _writer_thread, _stop_event, _enabled
    global _current_pseudo, _current_ts_str, _current_part_index
    global _current_debug_dir
    global _events_logged, _events_dropped_queue_full

    if _enabled:
        return False  # deja actif

    _event_queue = queue.Queue(maxsize=_MAX_QUEUE_SIZE)
    _stop_event = threading.Event()
    _current_pseudo = pseudo or "Joueur"
    _current_ts_str = time.strftime("%d%m%Y_%H%M%S")
    _current_part_index = 1
    _current_debug_dir = Path(debug_dir)
    _events_logged = 0
    _events_dropped_queue_full = 0

    _writer_thread = threading.Thread(
        target=_writer_loop,
        name="audio-rx-logger-writer",
        daemon=True,
    )
    _writer_thread.start()

    _enabled = True
    return True


def disable() -> None:
    """Desactive le logger. Flush + ferme le fichier + stoppe le thread
    writer. Bloque jusqu'a ce que le thread soit termine (timeout 3s)."""
    global _event_queue, _writer_thread, _stop_event, _enabled
    global _current_file_path

    if not _enabled:
        return

    _enabled = False  # bloque les futurs log_*
    if _stop_event is not None:
        _stop_event.set()
    if _writer_thread is not None:
        _writer_thread.join(timeout=3.0)

    _event_queue = None
    _writer_thread = None
    _stop_event = None
    _current_file_path = None


def is_enabled() -> bool:
    """True si le logger est actif."""
    return _enabled


def get_current_file_path() -> Optional[Path]:
    """Chemin du fichier CSV en cours d'ecriture (ou None si pas actif).
    Note : peut changer en cours de session si rotation."""
    return _current_file_path


def get_stats() -> dict:
    """Compteurs internes pour observabilite (a logger dans le log debug
    principal toutes les 30s par exemple)."""
    qsize = _event_queue.qsize() if _event_queue is not None else 0
    return {
        "enabled": _enabled,
        "logged": _events_logged,
        "dropped_queue_full": _events_dropped_queue_full,
        "queue_size": qsize,
        "queue_max": _MAX_QUEUE_SIZE,
        "current_file": str(_current_file_path) if _current_file_path else None,
    }


# ----------------------------------------------------------------------
# Fonctions de log (les producteurs appellent celles-ci)
# ----------------------------------------------------------------------

def log_rx(sender: str,
           msg_type: int,
           size: int,
           delta_ms: float,
           q_before: int,
           q_after: int,
           outcome: str) -> None:
    """Log d'une trame audio RECUE (cote feed_remote_frame).

    sender   : pseudo emetteur
    msg_type : flag (0x00 prox, 0x01 radio canal, 0x02 radio profil, 0x03 phone)
    size     : taille du payload (bytes), normalement 3840
    delta_ms : delta temps depuis trame precedente du MEME sender (ms),
               -1 si premiere trame de ce sender dans la session
    q_before : taille de la queue receveur avant push (0..MAX_QUEUE_LEN)
    q_after  : taille de la queue receveur apres push
    outcome  : "OK" | "DROP_QUEUE_FULL" (queue pleine, oldest jete)
    """
    if not _enabled:
        return
    ts_ms = time.time() * 1000.0
    row = (
        f"{ts_ms:.3f}",
        "RX",
        sender,
        f"0x{msg_type:02x}",
        size,
        f"{delta_ms:.3f}" if delta_ms >= 0 else "",
        q_before,
        q_after,
        outcome,
        "",   # callback_period_ms (vide pour RX)
        "",   # senders_state
        "",   # mix_peak_pre_tanh
        "",   # mix_peak_post_tanh
        "",   # trunc_total
        "",   # silence_impl_total
        "",   # cave_echo
        "",   # beep
        "",   # soundboard
        "",   # sonnerie
        "",   # phone_in_call
        "",   # phone_peer
    )
    _push_row(row)


def log_out(callback_period_ms: float,
            senders_state: dict,
            mix_peak_pre_tanh: float,
            mix_peak_post_tanh: float,
            trunc_total: int,
            silence_impl_total: int,
            flags: dict) -> None:
    """Log d'un appel callback sounddevice (_on_output_block).

    callback_period_ms : ms ecoulees depuis le precedent callback (~20ms
                         nominal a 48kHz / 960 samples)
    senders_state      : dict {sender_name: {q: int, vol: float, under: bool}}
                         pour chaque sender ayant un buffer non-vide.
                         Serialise en JSON inline dans la cellule CSV.
    mix_peak_pre_tanh  : max(abs(mixed)) AVANT le tanh (peut > 1.0)
    mix_peak_post_tanh : max(abs(mixed)) APRES le tanh (toujours <= 1.0)
    trunc_total        : cumul truncations (frame > frames demandees)
    silence_impl_total : cumul silences implicites (frame < frames)
    flags              : dict {cave_echo, beep, soundboard, sonnerie} = bool
    """
    if not _enabled:
        return
    ts_ms = time.time() * 1000.0
    try:
        senders_json = json.dumps(senders_state, separators=(",", ":"))
    except Exception:
        senders_json = ""
    row = (
        f"{ts_ms:.3f}",
        "OUT",
        "",   # sender (vide pour OUT, info dans senders_state)
        "",   # type
        "",   # size
        "",   # delta_ms
        "",   # q_before
        "",   # q_after
        "",   # outcome
        f"{callback_period_ms:.3f}",
        senders_json,
        f"{mix_peak_pre_tanh:.4f}",
        f"{mix_peak_post_tanh:.4f}",
        trunc_total,
        silence_impl_total,
        "1" if flags.get("cave_echo") else "0",
        "1" if flags.get("beep") else "0",
        "1" if flags.get("soundboard") else "0",
        "1" if flags.get("sonnerie") else "0",
        "",   # phone_in_call (vide pour OUT)
        "",   # phone_peer
    )
    _push_row(row)


def log_stats(sender: str, agg: dict) -> None:
    """Log d'un agregat 30s pour un sender donne.

    sender : pseudo
    agg    : dict d'agregats (recv, expected, loss_pct, jitter_p50/p95/max,
             underruns, drop_queue_full, etc.). Serialise en JSON inline.
    """
    if not _enabled:
        return
    ts_ms = time.time() * 1000.0
    try:
        agg_json = json.dumps(agg, separators=(",", ":"))
    except Exception:
        agg_json = ""
    row = (
        f"{ts_ms:.3f}",
        "STATS",
        sender,
        "",   # type
        "",   # size
        "",   # delta_ms
        "",   # q_before
        "",   # q_after
        "",   # outcome
        "",   # callback_period_ms
        agg_json,  # on reutilise senders_state pour porter l'agg JSON
        "",   # mix_peak_pre_tanh
        "",   # mix_peak_post_tanh
        "",   # trunc_total
        "",   # silence_impl_total
        "",   # cave_echo
        "",   # beep
        "",   # soundboard
        "",   # sonnerie
        "",   # phone_in_call
        "",   # phone_peer
    )
    _push_row(row)


def log_ctx(ctx_msg: str,
            phone_in_call: bool = False,
            phone_peer: str = "") -> None:
    """Log d'une transition de contexte (appel decroche/raccroche,
    changement de canal radio, mute toggle, etc.).

    ctx_msg : message libre court decrivant la transition (ex:
              "PHONE CALL START", "MUTE PROX ON", "CHANNEL=42")
    phone_in_call / phone_peer : etat courant (utile pour correlation post-hoc)
    """
    if not _enabled:
        return
    ts_ms = time.time() * 1000.0
    row = (
        f"{ts_ms:.3f}",
        "CTX",
        "",   # sender
        "",   # type
        "",   # size
        "",   # delta_ms
        "",   # q_before
        "",   # q_after
        ctx_msg,  # outcome porte le message
        "",   # callback_period_ms
        "",   # senders_state
        "",   # mix_peak_pre_tanh
        "",   # mix_peak_post_tanh
        "",   # trunc_total
        "",   # silence_impl_total
        "",   # cave_echo
        "",   # beep
        "",   # soundboard
        "",   # sonnerie
        "1" if phone_in_call else "0",
        phone_peer or "",
    )
    _push_row(row)
