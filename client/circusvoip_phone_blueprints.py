"""CircusPhone - App "Blueprints" : liste les plans (blueprints) recus.

Calquee sur l'app Portefeuille : meme presentation (liste + date/heure), meme
filtre de periode (24h / 7j / 30j / Tout) et meme navigation D-pad. Lit le
Game.log de Star Citizen : chaque notification
    Added notification "Received Blueprint: <nom>: "
cree une entree. Persistance dans circusphone_blueprints.json.

Reutilise l'infrastructure de tail du module Portefeuille (find_gamelog,
GameLogRawTailThread) pour ne pas la dupliquer.
"""

from __future__ import annotations

import os
import re
import json
import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
    QFrame,
)

from circusvoip_phone_apps import PhoneApp

# Infrastructure Game.log partagee avec le Portefeuille (tail + recherche).
try:
    from circusvoip_phone_wallet import find_gamelog, GameLogRawTailThread
except Exception:
    find_gamelog = None
    GameLogRawTailThread = None

# --- Palette (identique au Portefeuille) ---
PHONE_SCREEN_BG = "#ffffff"
PHONE_NAME_DARK = "#1a1a1a"
PHONE_NAME_GREY = "#9aa0a6"
_ACCENT_BLUE = "#2f6fed"

_BASE_DIR = Path(__file__).resolve().parent
BLUEPRINTS_FILE = _BASE_DIR / "circusphone_blueprints.json"
BLUEPRINTS_MAX = 500

_TS_RE = re.compile(r"<(?P<ts>[^>]+)>")
# Notification reelle (verifiee sur Game.log) :
#   Added notification "Received Blueprint: Huracan: " [22] ...
# On ancre sur 'Added notification' pour ne declencher qu'UNE fois (pas sur
# les echos Next / StartFade / Remove).
_TRIGGER_BP = 'Added notification "Received Blueprint:'
_BP_NAME_RE = re.compile(r'Received Blueprint:\s*(?P<name>.+?)\s*:\s*"')


def _load_blueprints() -> dict:
    if BLUEPRINTS_FILE.exists():
        try:
            with open(BLUEPRINTS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("items"), list):
                return data
        except Exception:
            pass
    return {"items": []}


def _save_blueprints(data: dict) -> None:
    try:
        tmp = BLUEPRINTS_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, BLUEPRINTS_FILE)
    except Exception:
        pass


def _bp_key(bp: dict) -> tuple:
    return (bp.get("name"), bp.get("ts"))


class BlueprintParser:
    """Automate minimal : feed(line) -> liste de {name, ts}."""

    def feed(self, line: str) -> list:
        out = []
        if _TRIGGER_BP in line:
            tsm = _TS_RE.search(line)
            ts = tsm.group("ts") if tsm else ""
            m = _BP_NAME_RE.search(line)
            name = m.group("name").strip() if m else ""
            if name:
                out.append({"name": name, "ts": ts or ""})
        return out


class _BlueprintRow(QFrame):
    """Une ligne : nom du blueprint + date/heure (comme une ligne du wallet)."""

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self._op = entry
        self.setObjectName("BlueprintRow")
        self._base_qss = ("QFrame#BlueprintRow { background:transparent; "
                          "border-bottom:1px solid #eceef0; }")
        self.setStyleSheet(self._base_qss)
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel(entry.get("name", "") or "Blueprint")
        title.setStyleSheet(
            f"color:{PHONE_NAME_DARK}; font-size:11pt; font-weight:600; "
            "background:transparent;")
        title.setWordWrap(True)
        left.addWidget(title)
        lbl_ts = QLabel(self._fmt_ts(entry.get("ts", "")))
        lbl_ts.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:8pt; background:transparent;")
        left.addWidget(lbl_ts)
        h.addLayout(left, 1)

        tag = QLabel("Reçu")
        tag.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        tag.setStyleSheet(
            f"color:{_ACCENT_BLUE}; font-size:9pt; font-weight:bold; "
            "background:transparent;")
        h.addWidget(tag, 0)

    @staticmethod
    def _fmt_ts(ts: str) -> str:
        m = re.match(r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})", ts or "")
        if not m:
            return ""
        y, mo, d, hh, mm, ss = m.groups()
        return f"{d}/{mo} {hh}:{mm}:{ss}"

    def set_nav_selected(self, sel: bool):
        self.setStyleSheet(
            "QFrame#BlueprintRow { background:%s; border-radius:8px; "
            "border-bottom:1px solid #eceef0; }"
            % ("rgba(47,111,237,0.14)" if sel else "transparent"))


class BlueprintsApp(PhoneApp):
    APP_ID = "blueprints"
    APP_NAME = "Blueprints"
    APP_ICON = "\U0001F4D0"          # 📐

    _PERIOD_ORDER = ("24h", "7j", "30j", "all")

    def __init__(self, screen_w, screen_h, screen_radius, services,
                 parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)
        self.setObjectName("BlueprintsScreen")
        self.setStyleSheet(
            "QWidget#BlueprintsScreen { background:" + PHONE_SCREEN_BG + "; "
            f"border-radius:{self._screen_rad}px; }}")

        self._parser = BlueprintParser()
        self._catchup_parser: Optional[BlueprintParser] = None
        self._data = _load_blueprints()
        self._items: list = list(self._data.get("items", []))
        self._items.sort(key=lambda o: (o.get("ts", "") or ""), reverse=True)
        self._seen = {_bp_key(b) for b in self._items}
        self._rows = []
        self._period = "all"
        self._nav_index = 0
        self._raw_thread = None
        self._subscribed = False
        self._bulk_loading = False

        self._build_screen()
        self._populate()
        self._update_summary()
        QTimer.singleShot(80, self._start_sources)

    # ---- UI ----
    def _build_screen(self):
        sv = QVBoxLayout(self)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        header = QWidget()
        header.setStyleSheet("background:transparent;")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(16, 14, 12, 10)
        hv.setSpacing(2)

        lbl_app = QLabel("Blueprints")
        lbl_app.setStyleSheet(
            f"color:{PHONE_NAME_DARK}; font-size:14pt; font-weight:bold; "
            "background:transparent;")
        hv.addWidget(lbl_app)

        # Filtre segmente 24h / 7j / 30j / Tout (defaut Tout).
        seg = QWidget()
        seg.setStyleSheet("background:#f1f3f5; border-radius:14px;")
        sh = QHBoxLayout(seg)
        sh.setContentsMargins(3, 3, 3, 3)
        sh.setSpacing(2)
        self._period_btns = {}
        for key, lbl in (("24h", "24h"), ("7j", "7j"),
                         ("30j", "30j"), ("all", "Tout")):
            b = QPushButton(lbl)
            b.setCursor(Qt.PointingHandCursor)
            b.setFixedHeight(24)
            b.clicked.connect(lambda _=False, k=key: self._set_period(k))
            sh.addWidget(b, 1)
            self._period_btns[key] = b
        hv.addWidget(seg)

        # Resume : nombre de blueprints sur la periode.
        self.lbl_summary = QLabel("")
        self.lbl_summary.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:9pt; background:transparent; "
            "padding-top:4px;")
        hv.addWidget(self.lbl_summary)
        sv.addWidget(header)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#eceef0; border:none;")
        sv.addWidget(sep)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }")
        self.list_host = QWidget()
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)
        self.lbl_empty = QLabel("Aucun blueprint reçu pour le moment.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:11pt; background:transparent; "
            "padding:40px;")
        self.list_layout.addWidget(self.lbl_empty)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_host)
        sv.addWidget(self.scroll, 1)
        self._restyle_period()

    def _populate(self):
        for b in self._items:
            self._add_row(b, at_top=False)

    def _add_row(self, entry: dict, at_top: bool):
        self.lbl_empty.hide()
        row = _BlueprintRow(entry)
        if at_top:
            self.list_layout.insertWidget(0, row)
            self._rows.insert(0, row)
        else:
            # inserer avant lbl_empty + stretch (2 derniers items)
            self.list_layout.insertWidget(self.list_layout.count() - 2, row)
            self._rows.append(row)

    # ---- Filtre periode ----
    def _period_start(self):
        delta = {"24h": datetime.timedelta(hours=24),
                 "7j":  datetime.timedelta(days=7),
                 "30j": datetime.timedelta(days=30)}.get(self._period)
        if delta is None:
            return None
        return datetime.datetime.now(datetime.timezone.utc) - delta

    @staticmethod
    def _op_dt(op):
        ts = (op.get("ts") or "").strip()
        if not ts:
            return None
        try:
            dt = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt
        except Exception:
            return None

    def _op_in_period(self, op, start):
        if start is None:
            return True
        dt = self._op_dt(op)
        return dt is not None and dt >= start

    def _set_period(self, key):
        self._period = key
        self._update_summary()

    def _restyle_period(self):
        for k, b in self._period_btns.items():
            if k == self._period:
                b.setStyleSheet(
                    "QPushButton { background:#2f6fed; color:#ffffff; "
                    "border:none; border-radius:12px; font-size:9pt; "
                    "font-weight:bold; }")
            else:
                b.setStyleSheet(
                    "QPushButton { background:transparent; color:#5a6068; "
                    "border:none; border-radius:12px; font-size:9pt; }"
                    "QPushButton:hover { color:#1a1a1a; }")

    def _update_summary(self):
        start = self._period_start()
        n = 0
        for i in range(self.list_layout.count()):
            it = self.list_layout.itemAt(i)
            w = it.widget() if it is not None else None
            if isinstance(w, _BlueprintRow):
                vis = self._op_in_period(w._op, start)
                w.setVisible(vis)
                if vis:
                    n += 1
        unit = "blueprint" if n <= 1 else "blueprints"
        self.lbl_summary.setText(f"{n} {unit}")
        self._restyle_period()

    # ---- Ingestion ----
    def _ingest(self, bp: dict, flash: bool = True):
        key = _bp_key(bp)
        if key in self._seen:
            return
        self._seen.add(key)
        # insertion triee (newest-first) par ts
        ts = bp.get("ts", "") or ""
        idx = 0
        while idx < len(self._items) and (self._items[idx].get("ts", "") or "") >= ts:
            idx += 1
        self._items.insert(idx, bp)
        if len(self._items) > BLUEPRINTS_MAX:
            self._items = self._items[:BLUEPRINTS_MAX]
        # ligne UI a la meme position
        self.lbl_empty.hide()
        row = _BlueprintRow(bp)
        self.list_layout.insertWidget(idx, row)
        self._rows.insert(idx, row)
        if not self._bulk_loading:
            self._data["items"] = self._items
            _save_blueprints(self._data)
            self._update_summary()

    # ---- Sources Game.log (reprend le schema du Portefeuille) ----
    def _start_sources(self):
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "subscribe"):
            try:
                gamelog.subscribe(self._on_raw_line)
                self._subscribed = True
            except Exception:
                pass
            self._catch_up_session()
        elif find_gamelog is not None:
            path = find_gamelog(self._data.get("gamelog_path"))
            if path:
                self._data["gamelog_path"] = path
                _save_blueprints(self._data)
                self._start_raw_tail(path, history=True, oneshot=False,
                                     handler=self._on_raw_line)

    def _catch_up_session(self):
        path = None
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "current_path"):
            try:
                path = gamelog.current_path()
            except Exception:
                path = None
        if not path and find_gamelog is not None:
            path = find_gamelog(self._data.get("gamelog_path"))
        if path:
            self._catchup_parser = BlueprintParser()
            self._start_raw_tail(path, history=True, oneshot=True,
                                 handler=self._on_catchup_line)

    def _start_raw_tail(self, path, history, oneshot, handler):
        if GameLogRawTailThread is None:
            return
        if self._raw_thread is not None:
            try:
                self._raw_thread.stop()
                self._raw_thread.wait(1500)
            except Exception:
                pass
            self._raw_thread = None
        self._raw_thread = GameLogRawTailThread(path, history=history,
                                                oneshot=oneshot)
        self._raw_thread.sig_line.connect(handler)
        self._raw_thread.sig_status.connect(self._on_tail_status)
        self._raw_thread.start()

    def _on_tail_status(self, msg: str):
        if msg == "Lecture de l'historique...":
            self._bulk_loading = True
        elif msg == "Historique lu" or msg.startswith("En direct"):
            if self._bulk_loading:
                self._bulk_loading = False
                self._data["items"] = self._items
                _save_blueprints(self._data)
                self._update_summary()

    def _on_raw_line(self, line: str):
        for bp in self._parser.feed(line):
            self._ingest(bp, flash=not self._bulk_loading)

    def _on_catchup_line(self, line: str):
        if self._catchup_parser is None:
            return
        for bp in self._catchup_parser.feed(line):
            self._ingest(bp, flash=False)

    # ---- Cycle de vie ----
    def on_show(self):
        try:
            self.scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def on_hide(self):
        pass

    def shutdown(self):
        try:
            if self._subscribed:
                gl = getattr(self.services, "gamelog", None)
                if gl is not None and hasattr(gl, "unsubscribe"):
                    gl.unsubscribe(self._on_raw_line)
        except Exception:
            pass
        if self._raw_thread is not None:
            try:
                self._raw_thread.stop()
                self._raw_thread.wait(1000)
            except Exception:
                pass

    # ---- Navigation D-pad (comme le Portefeuille) ----
    def handle_nav(self, direction: str) -> bool:
        if direction in ("left", "right"):
            order = self._PERIOD_ORDER
            try:
                i = order.index(self._period)
            except ValueError:
                i = len(order) - 1
            i = (i + (1 if direction == "right" else -1)) % len(order)
            self._set_period(order[i])
            return True
        if direction in ("up", "down"):
            try:
                bar = self.scroll.verticalScrollBar()
                step = max(40, bar.pageStep() // 2)
                bar.setValue(bar.value() + (step if direction == "down"
                                            else -step))
            except Exception:
                pass
            return True
        return False
