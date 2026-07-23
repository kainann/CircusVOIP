"""CircusPhone - App "Blueprints" : liste les plans (blueprints) recus.

Calquee sur l'app Portefeuille : meme presentation (liste + date/heure), meme
filtre de periode (24h / 7j / 30j / Tout) et meme navigation D-pad. Lit le
Game.log de Star Citizen : chaque notification
    Added notification "Received Blueprint: <nom>: "
cree une entree. Persistance dans circusphone_blueprints.json.

Reutilise l'infrastructure de tail du module Portefeuille (find_gamelog,
GameLogRawTailThread) pour ne pas la dupliquer.

[v0.3 build 60] Scan des anciens logs (dossier logbackups) :
Star Citizen archive automatiquement chaque Game.log dans le dossier
LIVE\\logbackups\\ au lancement suivant (mecanisme officiel CIG, ligne
"BackupNameAttachment=... -- used by backup system" en tete de fichier).
Au demarrage de l'app, un thread dedie (LogBackupsScanThread, infra
partagee definie dans circusvoip_phone_wallet.py) parcourt ces backups
pour retrouver les blueprints recus lors de sessions ou CircusVOIP ne
tournait pas.
Filtres par fichier (lus dans l'en-tete, ~100 premieres lignes) :
  - Channel : "[Trace] Environment:   PUB" (= LIVE). Repli sur \\LIVE\\
    dans la ligne "Executable:" si Environment absent. PTU/EPTU rejetes
    (donnees de test, wipees).
  - Version : "Branch: sc-alpha-X.Y..." avec (X, Y) >= (4, 7). Les
    blueprints existent depuis la 4.7 et ont survecu au wipe 4.8.
Incremental : les fichiers deja traites (nom + taille memorises dans
circusphone_blueprints.json, cle "logbackups_scanned") ne sont pas relus.
"""

from __future__ import annotations

import os
import re
import json
import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QTimer, QThread, Signal
from PySide6.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton, QScrollArea,
    QFrame, QStackedWidget,
)

from circusvoip_phone_apps import PhoneApp
# [build 61] Icone vectorielle (fin des emoji : rendu identique sur tous
# les PC). Import defensif : un vieux circusvoip_phone_apps sans fabrique
# ne doit pas faire tomber l'app du registre -> repli glyphe.
try:
    from circusvoip_phone_apps import LazyPhoneIcon as _LazyPhoneIcon
except Exception:
    _LazyPhoneIcon = None


# Infrastructure Game.log partagee avec le Portefeuille (tail + recherche
# + [build 60] scanner des anciens logs). Imports SEPARES : si le wallet
# present est une version anterieure au build 60 (sans scanner), on perd
# seulement le scan des anciens logs, PAS le tail ni le rattrapage.
try:
    from circusvoip_phone_wallet import find_gamelog, GameLogRawTailThread
except Exception:
    find_gamelog = None
    GameLogRawTailThread = None
try:
    from circusvoip_phone_wallet import (
        LogBackupsScanThread, find_logbackups_dir,
    )
except Exception:
    LogBackupsScanThread = None
    find_logbackups_dir = None

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


# ======================================================================
#  [build 61] Recettes de fabrication (blueprints_materials.json)
# ======================================================================
# Fichier de DONNEES remplacable a chaque patch SC, place a cote du module
# (meme convention que sounds/ et screenshots/), versionnable sur GitHub et
# distribue par l'updater. Structure attendue (dict indexe par cle bp) :
#   { "bp_craft_...": { "category": "...", "produced_item": "...",
#       "craft_time": "0d 0h 0m 10s",
#       "materials": [{"name": "...", "quantity": "0.03", "unit": "SCU"}] } }
# Etat des donnees (constat sur le fichier du 17/07/2026) :
#   - beaucoup de materiaux sont des GUID bruts (non resolus) -> affiches
#     "Non référencé" en attendant un fichier plus complet ;
#   - les cles sont des noms TECHNIQUES (bp_craft_qdrv_just_s02_huracan_
#     scitem) alors que le Game.log capte des noms d'AFFICHAGE ("Huracan")
#     -> matching par sous-chaine normalisee, qui peut donner 0 resultat
#     (donnees pas a jour : message dedie) ou PLUSIEURS (variantes
#     small/medium : toutes affichees, avec leur libelle de variante).
RECIPES_FILENAME = "blueprints_materials.json"
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
                      r"[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)
_recipes_cache = {"mtime": None, "path": None, "data": {}}


def _recipes_path() -> Path:
    base = Path(os.path.dirname(os.path.abspath(__file__)))
    p = base / RECIPES_FILENAME
    if p.exists():
        return p
    return Path(os.getcwd()) / RECIPES_FILENAME


def _load_recipes() -> dict:
    """Charge (avec cache sur mtime) le fichier de recettes. {} si absent
    ou illisible — l'app affiche alors 'recette non disponible'."""
    p = _recipes_path()
    try:
        mtime = p.stat().st_mtime
    except Exception:
        _recipes_cache.update(mtime=None, path=None, data={})
        return {}
    if (_recipes_cache["mtime"] == mtime
            and _recipes_cache["path"] == str(p)):
        return _recipes_cache["data"]
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    _recipes_cache.update(mtime=mtime, path=str(p), data=data)
    return data


def _norm_token(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _find_recipes(display_name: str) -> list:
    """Retourne [(cle_bp, recette)] correspondant au nom d'affichage capte
    dans le Game.log. Strategie :
      1. Match EXACT normalise sur produced_display (le nom commercial
         exact affiche en jeu) — cas nominal, evite les faux positifs de
         sous-chaine (ex. "Antium Helmet Jet" vs "...Jet Mk2").
      2. Repli : sous-chaine normalisee sur produced_display, puis sur la
         cle technique / produced_item (vieux fichiers sans
         produced_display, ou composants type Huracan/Salvation).
    Liste vide si rien ne matche (donnees perimees -> message dedie)."""
    tok = _norm_token(display_name)
    if len(tok) < 3:
        return []
    recipes = _load_recipes()
    exact = [(k, v) for k, v in recipes.items()
             if _norm_token(v.get("produced_display", "")) == tok]
    if exact:
        exact.sort(key=lambda kv: kv[0])
        return exact
    out = []
    for k, v in recipes.items():
        try:
            disp = _norm_token(v.get("produced_display", ""))
            if disp and (tok in disp or disp in tok):
                out.append((k, v))
            elif tok in _norm_token(k) or tok in _norm_token(
                    v.get("produced_item", "")):
                out.append((k, v))
        except Exception:
            continue
    out.sort(key=lambda kv: kv[0])
    return out


def _fmt_craft_time(raw: str) -> str:
    """'0d 0h 0m 10s' -> '10 s' ; '0d 2h 30m 0s' -> '2 h 30 min' ;
    '1d 4h 0m 0s' -> '1 j 4 h'. Unites nulles omises."""
    m = re.match(r"\s*(\d+)d\s+(\d+)h\s+(\d+)m\s+(\d+)s\s*$", raw or "")
    if not m:
        return raw or "?"
    d, h, mn, s = (int(x) for x in m.groups())
    parts = []
    if d:
        parts.append(f"{d} j")
    if h:
        parts.append(f"{h} h")
    if mn:
        parts.append(f"{mn} min")
    if s and len(parts) < 2:
        parts.append(f"{s} s")
    return " ".join(parts) if parts else "instantané"


# Noms francais des materiaux "reels" (le reste — minerais propres a SC
# comme agricium, bexalite, quantanium — garde son nom d'origine, juste
# capitalise). Cles en minuscules, telles que dans blueprints_materials.json
# (version du 17/07/2026 : 36 materiaux distincts, tous resolus).
_MATERIAL_FR = {
    "iron": "Fer",
    "gold": "Or",
    "copper": "Cuivre",
    "tin": "Étain",
    "silicon": "Silicium",
    "titanium": "Titane",
    "tungsten": "Tungstène",
    "aluminium": "Aluminium",
    "quartz": "Quartz",
    "pressurized_ice": "Glace pressurisée",
}


def _fmt_material_name(raw: str) -> str:
    """Nom lisible : GUID -> 'Non référencé (xxxxxxxx…)' ; traduction FR
    des materiaux communs ; prefixes harvestable_* retires ; underscores
    -> espaces + capitalisation."""
    raw = (raw or "").strip()
    if _GUID_RE.match(raw):
        return f"Non référencé ({raw[:8]}…)"
    fr = _MATERIAL_FR.get(raw.lower())
    if fr:
        return fr
    name = raw
    name = re.sub(r"^harvestable_(mineral|ore)_1h_", "", name)
    name = re.sub(r"ore$", " (minerai)", name)
    name = name.replace("_", " ").strip()
    return name[:1].upper() + name[1:] if name else raw


def _fmt_quantity(mat: dict) -> str:
    """SCU -> '0.03 SCU' (zeros de queue retires) ; unit -> '×2'."""
    q = str(mat.get("quantity", "")).strip()
    unit = (mat.get("unit") or "").strip()
    try:
        f = float(q)
        q = f"{f:g}"
    except Exception:
        pass
    if unit.upper() == "SCU":
        return f"{q} SCU"
    return f"×{q}"


def _variant_label(bp_key: str, display_name: str) -> str:
    """Libelle court de variante quand plusieurs recettes matchent :
    la partie de la cle qui n'est pas le nom cherche ni le prefixe
    bp_craft_. Ex. ('bp_craft_salvage_modifier_scraper_salvation_medium',
    'Salvation') -> 'salvage modifier scraper medium'."""
    k = re.sub(r"^bp_craft_", "", bp_key or "")
    tok = _norm_token(display_name)
    parts = [w for w in k.split("_")
             if w and _norm_token(w) != tok and w != "scitem"]
    return " ".join(parts)


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


# ======================================================================
#  [build 60] Scan des anciens logs (dossier logbackups)
# ======================================================================
# L'infrastructure du scan (lecture d'en-tete, filtres channel/version,
# thread LogBackupsScanThread) vit dans circusvoip_phone_wallet.py, module
# d'infra Game.log partagee (comme find_gamelog et GameLogRawTailThread).
# Ici, on ne definit que le SEUIL propre aux blueprints :
# ils existent depuis la 4.7 et ont survecu au wipe 4.8. Si un futur wipe
# efface les blueprints, remonter ce seuil a la version du wipe.
LOGBACKUPS_MIN_VERSION = (4, 7)



class _BlueprintRow(QFrame):
    """Une ligne : nom du blueprint + date/heure (comme une ligne du wallet).
    [build 61] Cliquable : ouvre la page recette du blueprint."""

    def __init__(self, entry: dict, on_click=None, parent=None):
        super().__init__(parent)
        self._op = entry
        self._on_click = on_click
        if on_click is not None:
            self.setCursor(Qt.PointingHandCursor)
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

    def mousePressEvent(self, ev):
        if self._on_click is not None:
            try:
                self._on_click(self._op)
            except Exception:
                pass
        super().mousePressEvent(ev)


class BlueprintsApp(PhoneApp):
    APP_ID = "blueprints"
    APP_NAME = "Blueprints"
    APP_ICON = (_LazyPhoneIcon("blueprints", "\U0001F4D0")
                if _LazyPhoneIcon is not None else "\U0001F4D0")

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
        # [build 61] Le curseur n'est pas encore materialise a l'ouverture :
        # le PREMIER up/down doit REVELER la selection sur la 1re ligne (le
        # dernier blueprint recu), pas sauter directement a la 2e.
        self._nav_shown = False
        self._raw_thread = None
        self._subscribed = False
        self._bulk_loading = False
        # [build 60] scan logbackups
        self._backups_thread: Optional[LogBackupsScanThread] = None
        self._backups_dirty = False   # au moins 1 bp ingere par le scan

        self._build_screen()
        self._populate()
        self._update_summary()
        QTimer.singleShot(80, self._start_sources)

    # ---- UI ----
    def _build_screen(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        # [build 61] Deux pages : liste (existante) et recette plein ecran.
        self._page_stack = QStackedWidget()
        root.addWidget(self._page_stack)
        self._page_list = QWidget()
        self._page_list.setStyleSheet("background:transparent;")
        self._page_stack.addWidget(self._page_list)
        self._build_detail_page()
        self._page_stack.setCurrentWidget(self._page_list)

        sv = QVBoxLayout(self._page_list)
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

        # [build 60] statut du scan des anciens logs (masque par defaut).
        self.lbl_scan = QLabel("")
        self.lbl_scan.setStyleSheet(
            f"color:{_ACCENT_BLUE}; font-size:8pt; background:transparent;")
        self.lbl_scan.hide()
        hv.addWidget(self.lbl_scan)
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

    # ---- [build 61] Page recette plein ecran ----
    def _build_detail_page(self):
        self._page_detail = QWidget()
        self._page_detail.setStyleSheet("background:transparent;")
        dv = QVBoxLayout(self._page_detail)
        dv.setContentsMargins(0, 0, 0, 0)
        dv.setSpacing(0)

        head = QWidget()
        head.setStyleSheet("background:transparent;")
        hv = QVBoxLayout(head)
        hv.setContentsMargins(16, 14, 12, 10)
        hv.setSpacing(4)
        top = QHBoxLayout()
        top.setSpacing(8)
        btn_back = QPushButton("‹ Retour")
        btn_back.setCursor(Qt.PointingHandCursor)
        btn_back.setFixedHeight(24)
        btn_back.setStyleSheet(
            "QPushButton { background:transparent; color:#2f6fed; "
            "border:none; font-size:10pt; text-align:left; }")
        btn_back.clicked.connect(self._close_detail)
        top.addWidget(btn_back, 0, Qt.AlignLeft)
        top.addStretch(1)
        hv.addLayout(top)
        # Nom du blueprint EN GROS en haut de l'ecran.
        self._detail_title = QLabel("")
        self._detail_title.setWordWrap(True)
        self._detail_title.setStyleSheet(
            f"color:{PHONE_NAME_DARK}; font-size:18pt; font-weight:bold; "
            "background:transparent;")
        hv.addWidget(self._detail_title)
        dv.addWidget(head)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#eceef0; border:none;")
        dv.addWidget(sep)

        self._detail_scroll = QScrollArea()
        self._detail_scroll.setWidgetResizable(True)
        self._detail_scroll.setFrameShape(QFrame.NoFrame)
        self._detail_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._detail_scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }")
        host = QWidget()
        self._detail_layout = QVBoxLayout(host)
        self._detail_layout.setContentsMargins(16, 10, 16, 16)
        self._detail_layout.setSpacing(8)
        self._detail_scroll.setWidget(host)
        dv.addWidget(self._detail_scroll, 1)
        self._page_stack.addWidget(self._page_detail)

    def _clear_detail(self):
        lay = self._detail_layout
        while lay.count():
            it = lay.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()

    @staticmethod
    def _detail_line(text: str, *, size: int = 10, bold: bool = False,
                     grey: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        color = PHONE_NAME_GREY if grey else PHONE_NAME_DARK
        weight = "font-weight:600;" if bold else ""
        lbl.setStyleSheet(f"color:{color}; font-size:{size}pt; {weight} "
                          "background:transparent;")
        return lbl

    def _add_material_row(self, mat: dict):
        row = QFrame()
        row.setStyleSheet("QFrame { background:#f6f7f9; border-radius:8px; }")
        h = QHBoxLayout(row)
        h.setContentsMargins(10, 7, 10, 7)
        h.setSpacing(8)
        name = QLabel(_fmt_material_name(
            mat.get("display") or mat.get("name", "")))
        name.setWordWrap(True)
        name.setStyleSheet(f"color:{PHONE_NAME_DARK}; font-size:10pt; "
                           "background:transparent;")
        h.addWidget(name, 1)
        qty = QLabel(_fmt_quantity(mat))
        qty.setStyleSheet(f"color:{_ACCENT_BLUE}; font-size:10pt; "
                          "font-weight:bold; background:transparent;")
        h.addWidget(qty, 0, Qt.AlignRight)
        self._detail_layout.addWidget(row)

    def _open_detail(self, entry: dict):
        name = entry.get("name", "") or "Blueprint"
        self._detail_title.setText(name)
        self._clear_detail()
        recipes = _find_recipes(name)
        if not recipes:
            has_file = bool(_load_recipes())
            msg = ("Recette introuvable dans les données actuelles.\n"
                   "(blueprints_materials.json pas encore à jour pour ce "
                   "blueprint)") if has_file else (
                   "Données de recettes absentes.\n"
                   "(fichier blueprints_materials.json non trouvé)")
            lbl = self._detail_line(msg, grey=True)
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setStyleSheet(lbl.styleSheet() + " padding:40px 10px;")
            self._detail_layout.addWidget(lbl)
        else:
            multi = len(recipes) > 1
            for bp_key, rec in recipes:
                if multi:
                    variant = _variant_label(bp_key, name) or bp_key
                    self._detail_layout.addWidget(self._detail_line(
                        variant.capitalize(), size=11, bold=True))
                self._detail_layout.addWidget(self._detail_line(
                    "Temps de fabrication : "
                    + _fmt_craft_time(rec.get("craft_time", "")), size=10))
                mats = rec.get("materials") or []
                self._detail_layout.addWidget(self._detail_line(
                    "Matériaux :" if mats else "Aucun matériau requis.",
                    size=10, grey=True))
                for mat in mats:
                    self._add_material_row(mat)
                if multi:
                    gap = QFrame()
                    gap.setFixedHeight(8)
                    gap.setStyleSheet("background:transparent;")
                    self._detail_layout.addWidget(gap)
        self._detail_layout.addStretch(1)
        try:
            self._detail_scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass
        self._page_stack.setCurrentWidget(self._page_detail)

    def _close_detail(self):
        self._page_stack.setCurrentWidget(self._page_list)

    def _populate(self):
        for b in self._items:
            self._add_row(b, at_top=False)

    def _add_row(self, entry: dict, at_top: bool):
        self.lbl_empty.hide()
        row = _BlueprintRow(entry, on_click=self._open_detail)
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
        # [build 61] Le filtre change l'ensemble des lignes visibles :
        # le curseur repart en haut pour ne pas pointer hors liste.
        self._nav_index = 0

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
        row = _BlueprintRow(bp, on_click=self._open_detail)
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
        # [build 60] Dans tous les cas (integre ou autonome), tenter le
        # scan des anciens logs. Il vit dans son propre thread et ne
        # depend pas du tail temps reel.
        self._start_backups_scan()

    # ---- [build 60] Scan des anciens logs (logbackups\) ----
    def _start_backups_scan(self):
        if self._backups_thread is not None:
            return  # deja en cours
        if LogBackupsScanThread is None or find_logbackups_dir is None:
            return  # infra wallet indisponible (import KO)
        path = self._resolve_gamelog_path()
        if not path:
            return  # pas de Game.log connu -> pas de logbackups derivable
        backups_dir = find_logbackups_dir(path)
        if backups_dir is None:
            return  # dossier absent : rien a faire, zero regression
        already = self._data.get("logbackups_scanned")
        if not isinstance(already, dict):
            already = {}
        self._backups_thread = LogBackupsScanThread(
            backups_dir, already,
            parser_factory=BlueprintParser,
            min_version=LOGBACKUPS_MIN_VERSION)
        self._backups_thread.sig_event.connect(self._on_backup_blueprint)
        self._backups_thread.sig_progress.connect(self._on_backup_progress)
        self._backups_thread.sig_done.connect(self._on_backup_done)
        self._backups_thread.start()

    def _on_backup_blueprint(self, bp: dict):
        n_before = len(self._items)
        # flash=False : import historique, pas une notif temps reel.
        # On force le mode bulk le temps de l'ingest pour eviter une
        # sauvegarde JSON par blueprint (la sauvegarde unique a lieu
        # dans _on_backup_done).
        was_bulk = self._bulk_loading
        self._bulk_loading = True
        try:
            self._ingest(bp, flash=False)
        finally:
            self._bulk_loading = was_bulk
        if len(self._items) != n_before:
            self._backups_dirty = True

    def _on_backup_progress(self, done: int, total: int):
        if total <= 0:
            return
        self.lbl_scan.setText(
            f"Lecture des anciens logs\u2026 {done}/{total}")
        self.lbl_scan.show()

    def _on_backup_done(self, result: dict):
        thread = self._backups_thread
        self._backups_thread = None
        if thread is not None:
            try:
                thread.wait(1000)
            except Exception:
                pass
        # Memoriser les fichiers traites (incremental) + sauver les items
        # si le scan a apporte quelque chose. Une seule ecriture JSON.
        scanned = result.get("scanned")
        if isinstance(scanned, dict):
            self._data["logbackups_scanned"] = scanned
        if self._backups_dirty:
            self._data["items"] = self._items
        _save_blueprints(self._data)
        self._update_summary()
        found = int(result.get("found", 0))
        files = int(result.get("files", 0))
        if found > 0:
            unit = "blueprint retrouve" if found == 1 else "blueprints retrouves"
            sess = "session" if files == 1 else "sessions"
            self.lbl_scan.setText(
                f"{found} {unit} dans {files} {sess} d'anciens logs")
            self.lbl_scan.show()
            QTimer.singleShot(8000, self.lbl_scan.hide)
        else:
            self.lbl_scan.hide()
        self._backups_dirty = False

    def _resolve_gamelog_path(self) -> Optional[str]:
        """Chemin du Game.log courant : feed integre si dispo, sinon
        repli autonome (meme logique que le rattrapage one-shot)."""
        path = None
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "current_path"):
            try:
                path = gamelog.current_path()
            except Exception:
                path = None
        if not path and find_gamelog is not None:
            path = find_gamelog(self._data.get("gamelog_path"))
        return path

    def _catch_up_session(self):
        path = self._resolve_gamelog_path()
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
        # [build 61] A chaque ouverture, le curseur repart en haut (dernier
        # blueprint recu) et redevient invisible : le prochain up/down le
        # revelera sur la 1re ligne au lieu de reprendre une position ancienne.
        self._nav_index = 0
        self._nav_shown = False
        for r in self._rows:
            r.set_nav_selected(False)
        # [build 61] Re-scan des anciens logs a CHAQUE ouverture de l'app :
        # couvre le cas d'une session SC jouee SANS CircusVOIP pendant que
        # le client restait ouvert (le backup de cette session apparait
        # dans logbackups apres coup, sans redemarrage du client). Quasi
        # gratuit grace a l'incremental (aucun nouveau fichier -> le thread
        # liste le dossier et se termine sans rien lire) ; le garde
        # _backups_thread evite les scans concurrents si on ouvre/ferme
        # l'app rapidement.
        self._start_backups_scan()

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
        if self._backups_thread is not None:
            try:
                self._backups_thread.stop()
                self._backups_thread.wait(2000)
            except Exception:
                pass

    # ---- Navigation D-pad (comme le Portefeuille) ----
    def _visible_rows(self) -> list:
        # isHidden() (etat explicite pose par le filtre periode) et non
        # isVisible() : ce dernier est faux pour TOUTES les lignes tant que
        # l'app n'est pas encore affichee (construction, tests offscreen).
        return [r for r in self._rows if not r.isHidden()]

    def _apply_highlight(self):
        vis = self._visible_rows()
        if not vis:
            return
        self._nav_index = max(0, min(self._nav_index, len(vis) - 1))
        for r in self._rows:
            r.set_nav_selected(False)
        sel = vis[self._nav_index]
        sel.set_nav_selected(True)
        try:
            self.scroll.ensureWidgetVisible(sel, 0, 40)
        except Exception:
            pass

    def handle_nav(self, direction: str) -> bool:
        # [build 61] Page recette : haut/bas font defiler, le reste est
        # consomme (seul Retour ferme, gere par handle_back).
        if self._page_stack.currentWidget() is self._page_detail:
            if direction in ("up", "down"):
                try:
                    bar = self._detail_scroll.verticalScrollBar()
                    step = max(40, bar.pageStep() // 2)
                    bar.setValue(bar.value() + (step if direction == "down"
                                                else -step))
                except Exception:
                    pass
            return True
        if direction in ("left", "right"):
            order = self._PERIOD_ORDER
            try:
                i = order.index(self._period)
            except ValueError:
                i = len(order) - 1
            i = (i + (1 if direction == "right" else -1)) % len(order)
            self._set_period(order[i])
            self._apply_highlight()
            return True
        # [build 61] Haut/bas : curseur de selection sur les lignes
        # VISIBLES (le filtre periode masque les autres), comme dans
        # l'app Photos. Enter : ouvre la recette de la ligne selectionnee.
        vis = self._visible_rows()
        if direction in ("up", "down"):
            if not vis:
                return True
            if not self._nav_shown:
                # 1er appui : on materialise le curseur sur la ligne
                # courante (index 0 = 1re/derniere recue) sans se deplacer.
                self._nav_shown = True
                self._nav_index = min(self._nav_index, len(vis) - 1)
            else:
                self._nav_index = ((self._nav_index
                                    + (1 if direction == "down" else -1))
                                   % len(vis))
            self._apply_highlight()
            return True
        if direction == "enter":
            if vis:
                self._nav_shown = True
                self._apply_highlight()
                self._open_detail(vis[self._nav_index]._op)
            return True
        return False

    def handle_back(self) -> bool:
        # Depuis la page recette -> revenir a la liste (consomme).
        if self._page_stack.currentWidget() is self._page_detail:
            self._close_detail()
            self._apply_highlight()
            return True
        return False    # depuis la liste -> l'overlay revient au home
