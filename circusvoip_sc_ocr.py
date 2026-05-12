"""
circusvoip_sc_ocr - Module OCR Star Citizen autonome
====================================================

Lit le HUD de Star Citizen pour en extraire la position du joueur :
zone (nom de l'objet stellaire / station) et coordonnees X, Y, Z.

Module independant, reutilisable. Aucune dependance a une UI ou a un
etat global d'application. Convient pour CircusVOIP (VOIP positionnel)
ou pour toute autre application ayant besoin de la position du joueur
dans Star Citizen (course, telemetrie, mods, etc.).

Dependances : mss, numpy, opencv-python, easyocr, pytesseract (optionnel),
              torch (recommande, pour GPU EasyOCR).

Utilisation typique :

    import circusvoip_sc_ocr as scocr

    def on_pos(pos):
        # pos = {"zone": "Levski_v2_middeck", "x": 371.0, "y": -102.0, "z": -434.0}
        print(pos)

    reader = scocr.SCOCRReader(on_position=on_pos)
    reader.start()
    # ... le reader tourne en thread daemon ...
    reader.stop()


API publique
============

Classe :
    SCOCRReader(on_position, monitor=None, force_cpu=False, freq_hz=10)
        .start()
        .stop()
        .set_zone(zone_dict)
        .set_force_cpu(bool)

Fonctions :
    list_monitors() -> list[dict]
        Retourne les ecrans connectes vus par mss (coords en pixels physiques).

    auto_ocr_zone(monitor=None) -> dict
        Calcule une zone de capture HUD par defaut (en bas a droite de l'ecran).
        monitor : un dict mss optionnel (sinon l'ecran principal). Coords physiques.

    parse_ocr_text(text) -> dict | None
        Parse une ligne OCR brute en {zone, x, y, z}. None si pas de match.
        Utile pour debug ou test sans reader.

    distance(p1, p2) -> float
        Distance euclidienne 3D entre deux positions (en metres).
        p1 et p2 sont des dicts {x, y, z} (zone optionnelle, ignoree).

    set_logger(fn)
        Branche un callback de log custom : fn(line: str). Defaut : print stderr.

    set_cache_dir(path)
        Definit le dossier de cache pour les modeles EasyOCR. A appeler avant
        toute initialisation. Defaut : ./cache/easyocr.


Format de la position retournee
================================

Dict avec les cles :
    zone : str   (nom de la zone, ex: "Levski_v2_middeck", "ArcCorp", ...)
    x    : float (coordonnees physique en metres)
    y    : float
    z    : float


Internes
========

Le pipeline OCR :
    1. Capture mss de la zone HUD (coords physiques)
    2. Pre-traitement : crop, gamma, threshold
    3. OCR principal : EasyOCR (GPU si dispo)
    4. Fallback : Tesseract si EasyOCR ne trouve rien
    5. Parsing : regex / heuristiques pour extraire zone + coords
    6. Validation : rejet des sauts impossibles (vitesse > seuil)
    7. Callback : on_position(pos) si valide

La frequence OCR est configurable (default 10 Hz). Le reader tourne dans
un thread daemon dedie. EasyOCR est initialise au .start() pour ne pas
bloquer l'instanciation.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional


# ======================================================================
# Constantes (rendues publiques pour cohérence avec applis utilisatrices)
# ======================================================================

RADIUS_TRIGGER  = 5.0    # metres : seuil "tres proche" (audible 100%)
AUDIBLE_RANGE_M = 30.0   # metres : portee max audible en proximite

DEFAULT_FREQ_HZ = 10     # frequence OCR par defaut (10 lectures/seconde)


# ======================================================================
# Logging
# ======================================================================

_logger: Callable[[str], None] = lambda line: None  # silent par defaut

def set_logger(fn: Callable[[str], None]) -> None:
    """Branche un callback de log. Recoit une ligne par evenement notable
    (init OCR, erreur, fallback Tesseract, etc.). Si non appele, le module
    est silencieux."""
    global _logger
    _logger = fn


# ======================================================================
# Cache (pour modeles EasyOCR)
# ======================================================================

_cache_dir: Path = Path(".") / "cache" / "easyocr"

def set_cache_dir(path) -> None:
    """Definit le dossier ou EasyOCR stocke ses modeles. A appeler AVANT
    toute autre fonction du module. Si non appele : ./cache/easyocr."""
    global _cache_dir
    _cache_dir = Path(path)


# ======================================================================
# Moniteurs et zones
# ======================================================================

def list_monitors() -> list[dict]:
    """Retourne la liste des moniteurs vus par mss.

    Chaque dict : {"left": int, "top": int, "width": int, "height": int}
    en COORDONNEES PHYSIQUES (pixels reels). L'ecran 0 (mss[0]) est ignore
    car c'est l'union de tous les ecrans, on retourne uniquement les ecrans
    individuels (mss[1:]).

    Note : l'application appelante doit etre DPI-aware pour avoir les bons
    pixels physiques (Windows : SetProcessDpiAwarenessContext(-4) avant tout
    import Qt/Tk)."""
    try:
        import mss
        with mss.mss() as sct:
            # monitors[0] = bounding box globale (tous ecrans)
            # monitors[1..N] = ecrans individuels
            return list(sct.monitors[1:])
    except Exception:
        # Fallback : un faux ecran 1920x1080 a (0,0)
        return [{"left": 0, "top": 0, "width": 1920, "height": 1080}]


def _get_screen_resolution() -> tuple[int, int]:
    """Retourne (width, height) du moniteur principal en pixels physiques.
    Helper interne pour auto_ocr_zone."""
    try:
        import mss
        with mss.mss() as sct:
            mon = sct.monitors[1]
            return mon["width"], mon["height"]
    except Exception:
        return 1920, 1080


def get_screen_resolution() -> tuple[int, int]:
    """Alias public de _get_screen_resolution. Retourne (width, height)
    du moniteur principal en pixels physiques."""
    return _get_screen_resolution()


def auto_ocr_zone(monitor: Optional[dict] = None) -> dict:
    """Calcule une zone de capture HUD par defaut sur le moniteur donne
    (ou ecran principal si None). Retourne un dict mss-compatible :
    {"left": int, "top": int, "width": int, "height": int, "gamma": float}
    en pixels physiques. La cle "gamma" est une valeur de pre-traitement
    (entre 0.3 et 0.5) que le pipeline OCR utilise.

    L'heuristique se base sur les ratios HUD courants (en haut a droite
    de l'ecran) et a ete ajustee empiriquement sur 1920x1080, 2560x1440,
    3440x1440 ultrawide, 3840x2160."""
    if monitor is None:
        sw, sh = _get_screen_resolution()
        mon_left = 0
        mon_top = 0
    else:
        sw = monitor["width"]
        sh = monitor["height"]
        mon_left = monitor["left"]
        mon_top = monitor["top"]

    aspect_ratio = sw / sh if sh > 0 else 16 / 9
    is_ultrawide = aspect_ratio > 2.0  # 21:9 ~= 2.33, 32:9 ~= 3.55

    if sh >= 1800:
        # 4K et au-dela : formule relative
        width = int(sw * 0.35)
        height = int(sh * 0.0135)   # ~29 px en 4K
    elif sh >= 1300:
        # 1440p : QHD 2560x1440 (16:9) OU ultrawide 3440x1440 (21:9)
        if is_ultrawide:
            # Ultrawide 21:9 (3440x1440) : zone manuelle 23.7% qui marche
            width = int(sw * 0.237)
        else:
            # QHD 16:9 classique (2560x1440)
            width = int(sw * 0.288)
        height = 18
    else:
        # 1080p et moins
        if is_ultrawide:
            width = int(sw * 0.237)
        else:
            width = int(sw * 0.35)
        height = 16  # 1 ligne + interligne, 2e ligne exclue

    top = mon_top                    # colle en haut
    left = mon_left + sw - width     # colle au bord droit (pas de marge)

    # Gamma du pretraitement : 0.3 pour 4K+ (testé OK), 0.5 sinon (plus doux)
    gamma = 0.3 if (sw >= 3000 or sh >= 1800) else 0.5

    return {
        "left":   left,
        "top":    top,
        "width":  width,
        "height": height,
        "gamma":  gamma,
    }



# ======================================================================
# Bloc parsing OCR (extrait du client1, autonome)
# ======================================================================
import re

_PAT_XYPOS = re.compile(r"[Xx][Yy][Pp]os\s*[:\s]*(-?[\d.]+)\s+(-?[\d.]+)", re.I)
# Autres formats

_NUM     = r"-?\s*[\d][\d\s]*(?:[.,]\d+)?"
_PAT_XYZ = [
    re.compile(rf"[Xx]\s*[:\s]\s*({_NUM})\s*[Yy]\s*[:\s]\s*({_NUM})\s*[Zz]\s*[:\s]\s*({_NUM})", re.I),
    re.compile(rf"({_NUM})\s*/\s*({_NUM})\s*/\s*({_NUM})"),
]

_PAT_AXIS = {a: re.compile(rf"[{a.upper()}{a.lower()}]\s*[:\s]\s*({_NUM})") for a in "xyz"}

def _normalize_zone_name(zone_name: str) -> str:
    """
    Normalise le nom de zone pour que 2 joueurs au meme endroit
    aient la meme valeur malgre les variations OCR.

    Exemples :
      "Levski all-001"        -> "levski"
      "levski all-9001"       -> "levski"
      "levski_all-001"        -> "levski"
      "tevski all-001"        -> "levski"  (OCR: t -> l)
      "ievski"                -> "levski"  (OCR: i -> l)
      "llevski"               -> "levski"  (OCR: double l)
      "levskit"               -> "levski"  (OCR: t parasite)
      "evski"                 -> "levski"  (OCR: 1ere lettre tombee)
      "area18 all-003"        -> "area18"
      "porttressler all-001"  -> "porttressler"
      "ruin_station all-002"  -> "ruin_station"
    """
    if not zone_name:
        return ""
    # Lowercase
    name = zone_name.lower()
    # Enlever tout a partir de "all-" (variante OCR : all-001, all-901, all-9001...)
    name = re.sub(r"\s*[_\s]?\s*all[-_\s]+\d+.*$", "", name)
    # Nettoyer les espaces/underscores en fin
    name = name.strip().rstrip("_ ")
    # Remplacer espaces par underscore pour la comparaison
    name = re.sub(r"\s+", "_", name)
    # Enlever les underscores suivis de 1-2 caracteres en fin
    # (OCR parasite type "levski_l" ou "levski_a")
    name = re.sub(r"_[a-z]{1,2}$", "", name)

    # ---- Correction OCR : zones connues ----
    # On compare contre une liste de noms canoniques et on accepte les
    # variantes proches (caracteres OCR-confondus : l<->t<->i<->1, o<->0, etc.)
    name = _correct_ocr_zone(name)
    return name


# Mapping de caracteres OCR-confondus (EasyOCR donne souvent ces erreurs)
_OCR_CHAR_EQUIV = {
    "l": "lti1|!",
    "i": "ilt1|!",
    "o": "o0",
    "s": "s5",
    "e": "ec",  # parfois e lu comme c
    "v": "vy",
    "a": "a4",
    # 2 <-> z : confusion observee 07/05/2026 sur "pyro2" lu "pyroz" en boucle
    # (HUD Pyro avec petit rendu, l'OCR voit z au lieu du 2). Sans cette
    # equivalence, la fuzzy match resolvait pyroz indifferemment vers pyro1,
    # pyro2, pyro3... et prenait pyro1 (premier de la liste, distance egale).
    # Avec l'equivalence : pyroz match exactement pyro2 (distance 0), donc
    # priorite absolue. Aucune zone connue ne contient un z legitime au
    # milieu d'un mot ou un 2 causerait conflit, donc l'ajout est safe.
    "2": "2z",
    # 6 <-> g : confusion observee sur "pyro6" lu "pyrog" (cf commentaires
    # historiques dans la liste _KNOWN_ZONES_STATIONS).
    "6": "6g",
    # 7 <-> z : confusion observee 09/05/2026 sur "ANVL_Hornet_F7CM_Mk2"
    # lu "anvl_hornet_fzcm_mk2" en boucle (16 variantes du meme vaisseau
    # dans une session de test). Le 7 et le Z se ressemblent dans le HUD
    # SC en petit rendu, surtout en gras. Sans cette equivalence, le
    # container_id du Hornet F7CM est instable et change a chaque frame
    # entre f7cm et fzcm -> 2 joueurs dans le meme Hornet auraient des
    # container_id differents -> plus de proximity audio.
    "7": "7z",
    # r <-> n : confusion observee sur "inferno" lu "irferno" (CRUS Starfighter
    # Inferno). Moins frequente que les autres mais peut casser le matching
    # de noms longs. Risque de faux positif minime : peu de zones ont une
    # paire r/n adjacente (anvil/anvl, hangar/hangan).
    "r": "rn",
    "n": "nr",
    # k <-> h : confusion observee sur "hornkt" au lieu de "hornet" (1
    # variante OCR sur le Hornet). Tres ponctuel.
    "k": "kh",
    "h": "hk",
}

# Liste des zones connues (a enrichir selon les stations/vaisseaux frequentes)
# Categories :
#   - stations : noms canoniques des stations et lunes
#   - ships    : noms de vaisseaux tels qu'ils apparaissent dans l'overlay
#                (le suffixe numerique type "9917714663391" est strip avant match)
#   - interiors: sous-zones (decks, hangars) avec hierarchie complete
_KNOWN_ZONES_STATIONS = [
    "levski",
    "levski_all-001",        # variante affichee dans le HUD SC ("levski all-001")
    "area18",
    "area18_central-001",    # variante affichee dans le HUD SC ("Area18 central-001")
    "lorville",
    "newbabbage",
    "port_olisar",
    "grimhex",
    "everus_harbor",
    "port_tressler",
    "porttressler",
    "baijini_point",
    "seraphim_station",
    "microtech",
    "arccorp",
    "hurston",
    "crusader",
    "delamar",
    "solarsystem",
    # ---- Systeme Pyro (Stanton+Pyro sont les 2 systemes ouverts en 4.0+) ----
    # Liste complete des 6 planetes + 6 lunes de Pyro V.
    # Les noms internes SC sont numeriques (pyro1..6, pyro5a..f) et le HUD les
    # affiche tels quels. Tester avec toutes les planetes ajoutees : le fuzzy
    # matcher + _OCR_CHAR_EQUIV (0/o, 5/s, 6/g) resout correctement les erreurs
    # OCR typiques : "pyros"->"pyro5", "pyr06"->"pyro6", "pyrog"->"pyro6".
    "pyro1",                        # Pyro I
    "pyro2",                        # Monox
    "pyro3",                        # Bloom
    "pyro4",                        # Pyro IV
    "pyro5",                        # Pyro V (gazeuse, avec les 6 lunes ci-dessous)
    "pyro6",                        # Terminus
    "pyro5a",                       # Ignis
    "pyro5b",                       # Vatra
    "pyro5c",                       # Adir
    "pyro5d",                       # Fairo
    "pyro5e",                       # Fuego
    "pyro5f",                       # Vuur
]

_KNOWN_ZONES_SHIPS = [
    # Famille MISC Hull : cargo modulaire en 5 tailles (A a E).
    # SC affiche en realite "MISC_Hull" (sans la lettre) dans le nom du
    # container, peu importe la taille reelle du vaisseau. La lettre A/B/C/D/E
    # n'est pas dans le nom de container HUD.
    #
    # IMPORTANT : la differenciation entre 2 Hulls (A vs C) se fait via le
    # `container_id` numerique unique (ex: "9979823193652") extrait du HUD SC,
    # PAS via le nom. Donc 2 joueurs dans 2 Hulls differents auront des
    # container_id numeriques differents -> pas de confusion audio. Chaque
    # spawn de vaisseau = 1 container_id numerique unique.
    #
    # On garde donc :
    #   - "misc_hull" : nom le plus frequent dans les lectures HUD
    #   - "misc_hull_a" a "misc_hull_e" : pour le cas hypothetique ou SC
    #     afficherait la lettre dans le HUD (pas observe en pratique mais
    #     conserves pour robustesse).
    "misc_hull",
    "misc_hull_a",
    "misc_hull_b",
    "misc_hull_c",
    "misc_hull_d",
    "misc_hull_e",
    "misc_starfarer",
    "misc_starfarer_teach",  # Starfarer Gemini (variante militaire) ou Tonker
    "misc_freelancer",
    "misc_prospector",
    "misc_reliant",
    "rsi_aurora",
    "rsi_constellation",
    "rsi_constellation_phoenix",  # variante luxe
    "rsi_constellation_andromeda",  # variante de base, conservee pour completude
    "rsi_constellation_aquila",     # variante exploration
    "rsi_constellation_taurus",     # variante cargo
    "rsi_perseus",           # ajoute apres session test 27/04/2026
    "rsi_polaris",
    "rsi_scorpius",
    "aegs_avenger",          # AEGS = Aegis Dynamics (abreviation SC)
    "aegs_gladius",
    "aegs_hammerhead",
    "aegs_redeemer",
    "aegs_retaliator",
    "aegs_vanguard",
    "aegs_idris",            # Idris-M / Idris-P
    "aegs_idris_p",
    "aegs_idris_m",
    # Anciens alias "aegis_*" conserves pour compat
    "aegis_avenger",
    "aegis_gladius",
    "aegis_hammerhead",
    "aegis_redeemer",
    "aegis_retaliator",
    "aegis_vanguard",
    "anvil_carrack",
    "anvil_hornet",
    "anvil_terrapin",
    "anvil_valkyrie",
    # ANVL = Anvil Aerospace (forme courte HUD SC, remplace anvil_* dans certains cas)
    "anvl_paladin",          # ANVL Paladin (vaisseau cargo/ingenierie)
    "anvl_carrack",
    "anvl_hornet",
    # Variantes Hornet : SC affiche les sous-modeles complets dans le HUD
    # (ex: "ANVL_Hornet_F7CM_Mk2_852401931548"). On ajoute les principaux
    # sous-modeles pour que le fuzzy matcher puisse canonicaliser.
    # _OCR_CHAR_EQUIV gere la confusion 7<->z donc "fzcm" matchera f7cm.
    "anvl_hornet_f7a_mk2",          # F7A = militaire Mk2
    "anvl_hornet_f7c_mk2",          # F7C = civil Mk2
    "anvl_hornet_f7c_s_mk2",        # F7C-S Hornet Ghost Mk2
    "anvl_hornet_f7c_r_mk2",        # F7C-R Hornet Tracker Mk2
    "anvl_hornet_f7c_m_mk2",        # F7C-M Super Hornet Mk2
    "anvl_hornet_f7cm_mk2",         # F7CM Hornet Heartseeker Mk2 (vu en test 09/05)
    "anvl_hornet_super",
    "anvl_hornet_tracker",
    "anvl_hornet_ghost",
    "anvl_valkyrie",
    "anvl_terrapin",
    # Vaisseaux vus dans le showroom A18 ASOP (test 09/05/2026)
    "anvl_c8r_pisces",              # C8R Pisces (medical), forme courte HUD
    "anvl_c8x_pisces",
    "anvl_pisces",                  # Pisces tout court (forme legacy)
    "anvl_asgard",                  # Asgard (vaisseau cargo lourd)
    "anvl_arrow",                   # Arrow (intercepteur)
    "anvl_gladiator",
    # MISC ships
    "misc_freelancer",
    "misc_freelancer_dur",
    "misc_freelancer_max",
    "misc_freelancer_mis",
    "misc_freelancer_dis",
    "misc_starlancer_max",
    "misc_starlancer_taci",
    "misc_prospector",
    "misc_razor",
    "misc_razor_lx",
    "misc_razor_ex",
    "misc_reliant",
    "misc_reliant_kore",
    "misc_reliant_sen",
    "misc_reliant_tana",
    "misc_reliant_mako",
    "misc_fury",                     # Fury (chasseur leger)
    "misc_fury_lx",                  # Fury LX (variante luxe)
    "misc_fury_miru",                # Fury Miru (variante mining, vu test 09/05)
    "misc_hull_a",
    "misc_hull_b",
    "misc_hull_c",
    "misc_endeavor",
    "misc_genesis",
    "misc_odyssey",
    # AEGS ships - variantes manquantes
    "aegs_eclipse",                  # Eclipse (bombardier furtif), vu test 09/05
    "aegs_javelin",
    "aegs_nautilus",
    "aegs_pioneer",
    "aegs_reclaimer",
    "aegs_sabre",
    "aegs_sabre_comet",
    "aegs_sabre_raven",
    "aegs_starkiller",
    # Tumbril (vehicules terrestres) - alias TMBL court avec sous-variantes
    "tmbl_storm",                    # Tumbril Storm (tank d'assaut)
    "tmbl_storm_aa",                 # Storm AA (anti-aerien), vu test 09/05
    "tmbl_ranger",
    "tmbl_ranger_cv",
    "tmbl_ranger_rc",
    "tmbl_ranger_tr",
    # Greycat (vehicules / GRIN court)
    "grin_mdc",                      # MDC (Mining Drill Cart), vu test 09/05
    "grin_ptv",                      # PTV (Personal Transport Vehicle)
    "grin_roc",                      # ROC (Remote Operated Cart, mining)
    "grin_roc_ds",
    # Mirai (MRAI court)
    "mrai_pulse",                    # Pulse (hoverbike), vu test 09/05
    "mrai_pulse_lx",
    "mrai_razor",
    "mrai_razor_lx",
    "mrai_razor_ex",
    "mrai_fury",
    "mrai_fury_lx",
    # Origin (suite)
    "origin_85x",
    "origin_100i",
    "origin_300i",
    "origin_315p",
    "origin_325a",
    "origin_350r",
    "origin_400i",
    "origin_600i",
    "origin_890jump",
    "drak_cutlass",          # DRAK = Drake Interplanetary (abreviation SC)
    "drak_cutlass_black",    # variante combat (vue 30x en tests)
    "drak_cutlass_red",      # variante medical
    "drak_cutlass_blue",     # variante police
    "drak_cutlass_steel",    # variante transport troupes
    "drak_buccaneer",
    "drak_caterpillar",
    "drak_command_module",   # module de pilotage (cockpit) du Caterpillar - container_id distinct du vaisseau
    "drak_corsair",
    "drak_corsair_elevator_platform",  # plateforme d'ascenseur du Corsair
    "drak_cutter",
    "drak_cutter_rambler",   # variante exploration
    "drak_cutter_scout",     # variante reconnaissance
    "drak_dragonfly",
    "drak_herald",
    "drak_vulture",
    # DRAK ships vus en test 09/05/2026 (showroom A18)
    "drak_mule",             # Mule (cargo leger, hover)
    "drak_clipper",          # Clipper (cargo medium)
    "drak_kraken",           # Kraken (capital ship)
    "drak_kraken_privateer",
    "crusader_ares",
    "crusader_mercury",
    "crusader_a1_spirit",
    "crusader_a2_hercules",
    "crusader_c1_spirit",
    "crusader_c2_hercules",
    "crusader_e1_spirit",
    "crusader_m2_hercules",
    # Forme courte CRUS Spirit : SC affiche parfois juste "CRUS Spirit C1" au lieu
    # de "CRUS_Spirit_C1" (espace au lieu d'underscore). L'OCR lit en plus souvent
    # "CRUS Soirit" (p -> o) ou "CRUS Soicit" (p -> o, r -> c). La canonicalisation
    # via fuzzy rattrape les variantes.
    "crus_spirit_a1",
    "crus_spirit_c1",
    "crus_spirit_e1",
    "crus_starfighter_ion",      # Crusader Ares Ion (chasseur, vu 14x en tests)
    "crus_starfighter_inferno",  # Crusader Ares Inferno (variante de l'Ion)
    "argo_mpuv",
    "argo_mole",
    "argo_raft",
    "argo_srv",
    "consolidated_mustang",
    "consolidated_pisces",
    "tumbril_cyclone",
    "tumbril_nova",
    "tumbril_ranger",
    # Aliases courts Tumbril : SC affiche parfois "TMBL_Nova", "TMBL_Cyclone"
    # (constructor abbreviation visible dans le HUD lors de l'entree dans
    # un vaisseau au dealership). On garde aussi les formes longues
    # "tumbril_*" pour compat avec d'autres contextes.
    "tmbl_cyclone",
    "tmbl_nova",
    # Greycat Industrial : STV (Small Terrestrial Vehicle), buggy de surface
    # offert avec certains packages. SC affiche "GRIN_STV" dans le HUD.
    "grin_stv",
]

_KNOWN_ZONES_INTERIORS = [
    # Levski - decks
    "levski_v2_middeck",
    "levski_v2_topdeck",
    "levski_v2_lowerdeck",
    "levski_v2_refindeck",        # refinery deck
    # Hangars Levski Nyx (tailles : small/medium/large/xl - top/front)
    "hangar_xltop_levski_nyx",
    "hangar_xlfront_levski_nyx",
    "hangar_largetop_levski_nyx",
    "hangar_largefront_levski_nyx",
    "hangar_mediumtop_levski_nyx",
    "hangar_mediumfront_levski_nyx",
    "hangar_smalltop_levski_nyx",
    "hangar_smallfront_levski_nyx",
    # Reststops (hors stations) - le format reel en jeu est "RestStop"
    # (CamelCase) mais on canonicalise en lowercase.
    "hangar_xltop_reststop",
    "hangar_largetop_reststop",
    "hangar_largefront_reststop",
    "hangar_mediumtop_reststop",
    "hangar_mediumfront_reststop",
    "hangar_smalltop_reststop",
    "hangar_smallfront_reststop",
    # Reststops Pyro (variantes avec suffixe _pyro, observees a Pyro)
    "hangar_xltop_reststop_pyro",
    "hangar_largetop_reststop_pyro",
    "hangar_largefront_reststop_pyro",
    "hangar_mediumtop_reststop_pyro",
    "hangar_mediumfront_reststop_pyro",
    "hangar_smalltop_reststop_pyro",
    "hangar_smallfront_reststop_pyro",
    # Reststops Nyx (forme courte "_rest_nyx" observee en jeu).
    # SC affiche "Hangar_MediumFront_Rest_NYX" avec "Rest" (pas "RestStop")
    # et "NYX" (avec Y majuscule). Observe sur le serveur via logs multi-joueurs.
    # IMPORTANT : la canonicalisation fuzzy rattrape "haroar" -> "hangar"
    # (r/n confusion) et "nvy" -> "nyx" (v/y confusion).
    "hangar_xltop_rest_nyx",
    "hangar_largetop_rest_nyx",
    "hangar_largefront_rest_nyx",
    "hangar_mediumtop_rest_nyx",
    "hangar_mediumfront_rest_nyx",
    "hangar_smalltop_rest_nyx",
    "hangar_smallfront_rest_nyx",
    "reststop_cargo_occu_0001",    # cargo habitat reststop (plusieurs instances)
    "reststop_cargo_occu_0002",
    "reststop_cargo_occu_0003",
    "reststop_cargo_occu_0004",
    # Cargo deck Pyro reststops (forme courte "rs_cargo_NNN" observee
    # en jeu, distincte du "reststop_cargo_occu_*" ci-dessus). Suffixe
    # "_001" sera probablement decline en _002, _003... si SC en cree
    # plusieurs instances, a observer dans les logs et enrichir.
    "rs_cargo_001",
    # Transit carriages
    "transitcarriage_levskilarge",
    "transitcarriage_levskismall",
    "transitcarriage_levskimedium",
    "transitcarriage_elev_util",   # ascenseur utilitaire
    "transitcarriage_elev_util_securityclearance",  # ascenseur security clearance
    "transitcarriage_lorville_tram",   # tram Lorville (gare centrale)
    "transitcarriage_ugfacilitylta",  # ascenseur UGF (underground facility)
    "transitcarriage_reststop_small",  # tram intra-reststop (Pyro R&R notamment)
    # Shuttle A18 (Area18, ArcCorp)
    "transitcarriage_a18_shuttle_a",
    "transitcarriage_a18_shuttle_b",
    "transitcarriage_a18_shuttle_c",
    "transitcarriage_a18_shuttle_d",
    # Seraphim Station - entrees Crusader LEO (Low Earth Orbit)
    # Le "1" final est un CHIFFRE (leo1, leo2...), pas une lettre L
    "rs_entry_cru-leo1",
    "rs_entry_cru-leo2",
    "rs_entry_cru-leo3",
    # Underground Facilities (bunkers) - le "_0001_int" est le format observe
    # en jeu (_int = interieur). Si d'autres numeros apparaissent dans les logs
    # on pourra enrichir ou ajouter un strip du numero interne.
    "objectcontainer-ugf_ita_a_0001_int",
    "objectcontainer-ugf_ita_b_0001_int",
    "objectcontainer-ugf_ita_c_0001_int",
    "objectcontainer-ugf_cor_a_0001_int",
    "objectcontainer-ugf_dls_a_0001_int",
    # POI (Points of Interest) dans l'espace
    "tsg_gascloud_001",            # gas cloud events (plusieurs instances)
    "tsg_gascloud_002",
    "tsg_gascloud_003",
    "tsg_gascloud_004",
    # Stations/outposts sur planetes/lunes
    "keeger_segment_social_001",   # outpost type Keeger
    "keeger_segment_social_002",
    "keeger_segment_social_003",
    # Stations Nyx - layout Keeger (intérieur station)
    # Le "_01", "_02", "_03" designe la station physique (1ere, 2e, 3e station Keeger
    # du systeme). On garde le numero dans la whitelist pour que 2 joueurs dans 2
    # stations DIFFERENTES aient un container_id DIFFERENT (pas de fausse proximite).
    # La forme sans suffixe est aussi observee (OCR qui coupe le numero final,
    # ou zone affichee sans numero dans certains cas). Observations logs :
    # - tester A (4K 16:9) : 100% "rs_int_layout_keeger" (sans suffixe)
    # - tester B (2K 21:9) / tester C (1080p) : melange rs_/ts_ (r->t confusion OCR), avec/sans _02
    "rs_int_layout_keeger",
    "rs_int_layout_keeger_01",
    "rs_int_layout_keeger_02",
    "rs_int_layout_keeger_03",
    # Numéros 04 à 10 ajoutés préventivement : on ne les a pas encore observés
    # en jeu, mais si SC les utilise pour de nouvelles stations Keeger, la
    # whitelist les reconnaitra sans mise a jour manuelle. Si le nombre passe
    # un jour a >10, il faudra etendre ou passer a une detection par pattern.
    "rs_int_layout_keeger_04",
    "rs_int_layout_keeger_05",
    "rs_int_layout_keeger_06",
    "rs_int_layout_keeger_07",
    "rs_int_layout_keeger_08",
    "rs_int_layout_keeger_09",
    "rs_int_layout_keeger_10",
    # Object Containers (surfaces/orbites planetaires Stanton)
    # Les 4 planetes principales + les 12 lunes.
    # Structure OOC_Stanton_<N><lettre>_<nom> :
    #   N = numero de la planete (1..4)
    #   lettre = identifiant de la lune (a, b, c, d)
    #   nom = nom propre
    "ooc_stanton_1_hurston",
    "ooc_stanton_1a_ariel",
    "ooc_stanton_1b_aberdeen",
    "ooc_stanton_1c_magda",
    "ooc_stanton_1d_ita",
    "ooc_stanton_2_crusader",
    "ooc_stanton_2a_cellin",
    "ooc_stanton_2b_daymar",
    "ooc_stanton_2c_yela",
    "ooc_stanton_3_arccorp",
    "ooc_stanton_3a_lyria",
    "ooc_stanton_3b_wala",
    "ooc_stanton_4_microtech",
    "ooc_stanton_4a_calliope",
    "ooc_stanton_4b_clio",
    "ooc_stanton_4c_euterpe",
    # Note sur glaciemring : NE PAS ajouter "glaciemring_segment_mission_genrl"
    # ici. Ce serait canonicaliser le suffixe "_001-024" (segment precis) en
    # le PERDANT -> tous les joueurs des rings auraient le meme container_id
    # -> fausse proximite entre segments eloignes. Laissant la whitelist sans
    # entree, le code utilise "name:glaciemring_segment_mission_genrl_001-024"
    # comme container_id en fallback, ce qui preserve le numero de segment.
    # Area18
    "area18_shuttle",
    "area18_metro",
    # Area18 - Object Containers internes (zones publiques de la ville).
    # SC affiche ces noms avec le prefixe "OC_" (ObjectContainer) suivi
    # de l'abreviation de la ville (a18) et du sous-type (sp = spaceport,
    # cbd = central business district, etc.). Ajout 09/05/2026 apres
    # observation de variantes OCR critiques :
    #   - oc_a18_sp_int (canonique) lu comme oc_al8_5p_int, 0c_018_sp_int,
    #     dc_a18_5p_int, oc_al8_sq_int, etc.
    #   - Sans entree dans _KNOWN_ZONES, le fuzzy match retournait juste
    #     la valeur normalisee bruitee, donc 2 joueurs dans le MEME spaceport
    #     d'A18 avaient des container_id differents -> coupure VOIP totale.
    # _OCR_CHAR_EQUIV gere les confusions 1<->l, s<->5, o<->0, donc
    # toutes les variantes OCR vont matcher la canonique.
    "oc_a18_sp_int",                      # Spaceport interieur (CRITIQUE - hub de groupe)
    "oc_a18",                              # Forme courte (lecture OCR tronquee, vue 1x)
    "oc_a18_cbd_int",                     # Central Business District
    "oc_a18_riker_memorial_spaceport",
    "oc_a18_central_int",
    "oc_a18_commercial_int",
    "oc_a18_residential_int",
    # Hangars Area18 (XL, large, medium, small ; top et front)
    "hangar_xltop_areal8",                # Hangar XL top A18 (test 09/05)
    "hangar_xlfront_areal8",
    "hangar_largetop_areal8",
    "hangar_largefront_areal8",
    "hangar_mediumtop_areal8",
    "hangar_mediumfront_areal8",
    "hangar_smalltop_areal8",
    "hangar_smallfront_areal8",
    # Variantes "area18" (sans "l" en plus) au cas ou SC change la
    # convention de nommage. Le fuzzy mappera de toute facon.
    "hangar_xltop_area18",
    "hangar_largetop_area18",
    "hangar_mediumtop_area18",
    "hangar_smalltop_area18",
    # Lorville
    "lorville_shuttle",
    "lorville_metro",
    # Lorville - buildings interiors (Level 19 etc., ObjectContainer avec les zones int)
    # Forme canonique : "l19" avec lettre L (level 19), l'OCR peut le lire "119"
    # (l minuscule ressemble a 1), mais _OCR_CHAR_EQUIV["l"] contient "1" donc
    # le fuzzy fusionne les deux variantes.
    "lorville_l19_int",
    "objectcontainer-lorville_cbd_int",   # Central Business District
    "objectcontainer-lorville_5p_int",    # Five Points (commerce)
    "hangar_smalltop_lorville",
    "hangar_mediumtop_lorville",
    "hangar_largetop_lorville",
    "hangar_xltop_lorville",
    # New Babbage
    "newbabbage_shuttle",
    "newbabbage_metro",
    "objectcontainer-newbab_domes_int_001",  # Dome Newbab (interieur)
    # Containers generiques de zones de stations
    # SC affiche "ObjectContainer Entry" / "ObjectContainer Commercial"
    # comme zones generiques pour les entrees / zones commerce des stations.
    "objectcontainer_entry",       # zone d'entree de station
    "objectcontainer_commercial",  # zone commerce de station
    # Pyro - containers generiques de batiments
    # ATTENTION : SC expose le MEME nom+ID pour plusieurs batiments distincts
    # (cf point 6 de la roadmap). On stabilise juste le nom canonique ici,
    # le probleme de differenciation multi-batiment est a resoudre autrement.
    "rastarinteriorgridhost",
    # Pyro - rock outposts (exploration/salvage surface)
    # Le "091" et "001" sont des IDs numeriques stables du container SC
    # (differents de celui du vaisseau). "o" et "0" sont confondus par OCR,
    # on garde la forme "091" / "001" (chiffres) car c'est ce que SC affiche
    # en realite dans le HUD.
    "rockol_occu_091_size03_001_int",
    "rockol_occu_091_size03_002_int",
    "rockol_occu_091_size03_003_int",
    # Dealerships Pyro - showrooms vaisseaux/vehicules en surface.
    # SC affiche "dealership_rundown_001" (Rundown station, Pyro). Le suffixe
    # numerique distingue les multiples instances du meme dealership.
    "dealership_rundown_001",
    # Contested zones Pyro - zones PvP a points d'interet (Pyro V Lagrange 2,
    # etc.). Format observe : "p<num>l<num>_contestedzone".
    # IMPORTANT : "p5l2" contient bien la lettre "l" (Lagrange), pas le
    # chiffre "1". L'OCR peut confondre l/1, mais le canonique est avec "l".
    "p5l2_contestedzone",
    # Interieurs de stations Pyro Lagrange (format "rs_int_p<num>l<num>").
    # Comme pour p5l2_contestedzone, le "l" est un L (Lagrange), pas un 1.
    # L'OCR le lit souvent en "p214" (l->1), mais l'equivalence OCR
    # _OCR_CHAR_EQUIV["l"] = "lti1|!" rattrape la confusion via le fuzzy.
    # Seuls p2l4 a ete observe en jeu pour l'instant ; ajouter les autres
    # combinaisons p<n>l<n> au fil des observations.
    "rs_int_p2l4",
    # Refinery deck Pyro (process raffinage minerai). Variante non-pyro
    # (rs_refinery tout court) non observee a date.
    "rs_refinery_pyro",
]

# Liste unifiee (conservee pour compat + utilisee par le fuzzy match)
_KNOWN_ZONES = _KNOWN_ZONES_STATIONS + _KNOWN_ZONES_SHIPS + _KNOWN_ZONES_INTERIORS

# Precompute : version normalisee (separateurs collapses en _) de chaque zone.
# Liste de tuples (zone_canonique, zone_normalisee, len_normalisee)
_KNOWN_ZONES_NORM = [
    (z, re.sub(r"[_\s-]+", "_", z), len(re.sub(r"[_\s-]+", "_", z)))
    for z in _KNOWN_ZONES
]


def _ocr_distance_threshold(name_len: int) -> int:
    """
    Seuil adaptatif de distance OCR selon la longueur du nom candidat.
    Les noms longs ont statistiquement plus d'erreurs OCR.
      <=  6 chars : 1 erreur  (stations courtes : levski, area18, hurston...)
       7-15 chars : 2 erreurs
      16-25 chars : 3 erreurs
      26-32 chars : 4 erreurs
       > 32 chars : 6 erreurs (fix tester B / 2K 21:9 ultrawide, 27/04/2026)

    Les zones courtes (<=10 chars) ont une distance mutuelle min de 3,
    donc un seuil de 2 sur 7-9 chars reste sans ambiguite.

    MAJ 27/04/2026 : sur ultrawide 21:9, l'OCR cumule plus d'erreurs (3-4
    confusions par lecture : r->t, n->u, s->y, v->n) sur les noms tres longs
    type "hangar_mediumfront_rest_nyx" (27 chars). Le seuil etait 4 mais les
    distances reelles atteignent 7-9. On passe a 6 pour les noms 26-32 chars
    et 7 pour les > 32 chars. Verifie : aucune zone connue de la whitelist
    n'est a moins de 7 d'une autre zone connue de meme famille -> pas de
    confusion intra-whitelist (testé : medium vs large vs small, rest vs
    reststop, nyx vs pyro vs levski_nyx : toujours dist >= 7).
    """
    if name_len <= 6:
        return 1
    if name_len <= 15:
        return 2
    if name_len <= 25:
        return 3
    if name_len <= 32:
        return 6
    return 7

def _ocr_char_equal(ca: str, cb: str) -> bool:
    """True si ca et cb sont identiques OU confondus par OCR."""
    if ca == cb:
        return True
    # Separateurs equivalents
    if ca in "_- " and cb in "_- ":
        return True
    # Confusions OCR
    for canon, variants in _OCR_CHAR_EQUIV.items():
        if ca in variants and cb in variants:
            return True
    return False


def _ocr_distance(a: str, b: str) -> int:
    """
    Distance d'edition (Levenshtein) entre deux chaines, ou les substitutions
    de caracteres OCR-confondus et de separateurs coutent 0.

    Gere donc au meme niveau : substitutions OCR (t<->l, o<->0), separateurs
    decales (_, -, espace), insertions et suppressions.

    Coupure rapide si difference de longueur trop importante.
    """
    la, lb = len(a), len(b)
    if a == b:
        return 0
    # Coupure rapide pour gain de perf
    if abs(la - lb) > 6:
        return 99

    # Programmation dynamique (Wagner-Fischer avec coup 0 sur substitutions OCR)
    # dp[i][j] = distance entre a[:i] et b[:j]
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            sub_cost = 0 if _ocr_char_equal(a[i-1], b[j-1]) else 1
            curr[j] = min(
                prev[j] + 1,           # suppression de a[i-1]
                curr[j-1] + 1,         # insertion de b[j-1]
                prev[j-1] + sub_cost,  # substitution (ou match gratuit)
            )
        prev = curr
    return prev[lb]


# Cache LRU simple pour _correct_ocr_zone : l'OCR lit souvent les memes zones
# (meme nom plusieurs fois par seconde). Un hit cache evite tout le fuzzy match.
# Limite a 2048 entrees pour eviter les fuites si l'OCR produit beaucoup de bruit.
_ZONE_CORRECT_CACHE = {}
_ZONE_CORRECT_CACHE_MAX = 2048


def _correct_ocr_zone(name: str) -> str:
    """
    Si le nom est proche d'une zone connue (distance OCR <= seuil adaptatif),
    retourne la zone canonique. Sinon retourne le nom tel quel.

    Strategie :
      1) Cache LRU (hit immediat sur les zones deja vues)
      2) Strip des suffixes numeriques (IDs de vaisseaux type "9917714663391")
         et des IDs bruites par OCR (mix lettres/chiffres long type "841541787am8")
      3) Normalisation separateurs (espace/tiret -> underscore, collapse)
      4) Match fuzzy contre la whitelist avec seuil adaptatif
      5) Fallback "pivot" : zone connue contenue (fuzzy) dans le candidat
         ou candidat contenu (fuzzy) dans une zone connue
    """
    if not name:
        return name

    # Cache
    cached = _ZONE_CORRECT_CACHE.get(name)
    if cached is not None:
        return cached

    result = _correct_ocr_zone_impl(name)

    # Store in cache (bornee)
    if len(_ZONE_CORRECT_CACHE) >= _ZONE_CORRECT_CACHE_MAX:
        # Drop un element arbitraire (pas de vrai LRU, trop complexe pour le gain)
        _ZONE_CORRECT_CACHE.pop(next(iter(_ZONE_CORRECT_CACHE)), None)
    _ZONE_CORRECT_CACHE[name] = result
    return result


def _correct_ocr_zone_impl(name: str) -> str:
    """Implementation sans cache (voir _correct_ocr_zone)."""
    # Match exact direct (cas le plus frequent : zone deja canonique)
    if name in _KNOWN_ZONES:
        return name

    # --- Step 1 : strip des suffixes d'ID ---
    # (a) suffixe purement numerique separe long (>= 6 chiffres) :
    #     "_9917714663391", "_841555881873".
    #     Les suffixes courts type "_001", "_002" font partie du nom canonique
    #     (tsg_gascloud_002, keeger_segment_social_001) et NE doivent PAS etre strip.
    stripped = re.sub(r"[_\s-]+\d{6,}\b.*$", "", name)
    # (a bis) suffixe numerique colle sans separateur : "misc_prospector841541787448"
    #        On exige >= 6 chiffres consecutifs pour ne pas couper des noms
    #        legitimes comme "levski_v2" (seulement 1 chiffre) ou "area18"
    #        (qui est entierement dans _KNOWN_ZONES donc geree avant).
    stripped = re.sub(r"\d{6,}\b.*$", "", stripped)
    # (b) suffixe alphanumerique COMMENÇANT par un chiffre long (ID bruite par OCR).
    #     Ex: "_841541787am8" (chiffres+lettres melanges), "_3415417374g6"
    #     On exige au moins 5 chars dont au moins 4 chiffres pour eviter de
    #     happer des suffixes legitimes type "_001", "_002", "_prospert0r".
    stripped = re.sub(r"[_\s-]+\d[a-z0-9]{4,}\b.*$", "", stripped, flags=re.IGNORECASE)
    # (c) suffixe de >= 6 chars contenant un "o" qui est probablement un "0" OCR
    #     (IDs type "o9ool1", "oo9ool1") : sequence longue qui ressemble
    #     a un nombre avec substitutions OCR (o->0, l->1, i->1, z->2, b->6, s->5).
    #     Seuil 6 pour eviter de happer "_001" (3 chars) ou "_oo02" (4 chars).
    stripped = re.sub(
        r"[_\s-]+[o0-9bilszg]{6,}\b.*$",
        "", stripped,
        flags=re.IGNORECASE
    )
    stripped = stripped.strip("_ -")
    # Normaliser les separateurs : espace/tiret/underscore -> underscore simple
    normalized = re.sub(r"[_\s-]+", "_", stripped)

    if normalized in _KNOWN_ZONES:
        return normalized

    # --- Step 2 : fuzzy match avec seuil adaptatif ---
    # Utilise _KNOWN_ZONES_NORM precalcule
    candidate = normalized if normalized else name
    lc = len(candidate)
    threshold = _ocr_distance_threshold(lc)
    best_zone = None
    best_dist = threshold + 1
    for z, z_norm, lz_norm in _KNOWN_ZONES_NORM:
        # Skip rapide si difference de longueur trop importante
        if abs(lz_norm - lc) > threshold:
            continue
        d = _ocr_distance(candidate, z_norm)
        if d < best_dist:
            best_dist = d
            best_zone = z
            if d == 0:
                break
    if best_zone is not None and best_dist <= threshold:
        return best_zone

    # --- Step 3 : fallback "pivot" ---
    # Ce fallback est couteux (sliding window x Levenshtein), on le borne :
    #   - candidat >= 8 chars
    #   - debordement max 40% de la longueur de la zone
    #   - pour chaque zone, on cherche la MEILLEURE fenetre (pas la premiere)
    best_zone = None
    best_score = 99
    if lc >= 8:
        for z, z_norm, lz in _KNOWN_ZONES_NORM:
            if lz < 8:
                continue
            # Debordement max autorise : 40% de la longueur de la zone canonique
            max_overhang = max(4, lz * 4 // 10)

            # Cas 1 : z_norm est (fuzzy) inclus dans candidate (suffix/prefix parasite)
            if lc >= lz and (lc - lz) <= max_overhang:
                max_err = 2 if lz <= 15 else 3
                # Meilleure distance sur toutes les fenetres
                best_win_dist = max_err + 1
                for i in range(lc - lz + 1):
                    d = _ocr_distance(candidate[i:i+lz], z_norm)
                    if d < best_win_dist:
                        best_win_dist = d
                        if d == 0:
                            break
                if best_win_dist <= max_err:
                    score = best_win_dist * 100 + (lc - lz)
                    if score < best_score:
                        best_score = score
                        best_zone = z
            # Cas 2 : candidate est (fuzzy) inclus dans z_norm (debut/fin tombe)
            elif lz > lc and (lz - lc) <= max_overhang:
                max_err = 2 if lc <= 15 else 3
                best_win_dist = max_err + 1
                for i in range(lz - lc + 1):
                    d = _ocr_distance(candidate, z_norm[i:i+lc])
                    if d < best_win_dist:
                        best_win_dist = d
                        if d == 0:
                            break
                if best_win_dist <= max_err:
                    score = best_win_dist * 100 + (lz - lc)
                    if score < best_score:
                        best_score = score
                        best_zone = z
    if best_zone is not None:
        return best_zone

    # Aucun match fuzzy : retourner le nom NORMALISE (separateurs uniformises)
    # plutot que le nom brut. Ainsi "Hangar MediumFront Rest Nyx" devient
    # "hangar_mediumfront_rest_nyx" meme si le fuzzy match a echoue. Permet
    # que des lectures consecutives "Hangar Mediumfront Rest Nyx" et
    # "Hangar  MediumFront  Rest  Nyx" (espaces variables) donnent le meme
    # container_id, au lieu de noms differents qui casseraient la proximity.
    if normalized:
        return normalized
    return name


# Corrections OCR pour l'affichage UI des container_name.
# Ces remplacements sont appliques sur le nom brut de l'OCR pour produire
# un nom lisible. NE PAS les utiliser pour l'identification (le container_id
# numerique sert deja a ca) - uniquement pour l'affichage humain.
#
# Ordre important : corriger les mots les plus longs avant les plus courts
# pour eviter que "Hangar" soit corrige depuis "Hanaar" puis re-casse.
_OCR_NAME_FIXES = [
    # "Ob ject" / "ob ject" : espace parasite OCR dans "Object" (tres frequent).
    # Applique EN TOUT PREMIER pour que les autres fixes ObjectContainer/Object
    # matchent ensuite les variantes corrigees. Sans ca, on se retrouve avec
    # des container_id type "name:ob jectcontainer-lorville cbd int" qui restent
    # differents du bon "name:objectcontainer-lorville cbd int".
    ("Ob ject",         "Object"),
    ("ob ject",         "object"),
    ("OB ject",         "Object"),
    ("Ob  ject",        "Object"),   # double espace
    ("ob  ject",        "object"),
    # Mots techniques SC recurrents
    ("Obiectcontainer", "ObjectContainer"),
    ("ObiectContainer", "ObjectContainer"),
    ("Obiect",          "Object"),
    ("TransitCarriade", "TransitCarriage"),
    ("Hanaar",          "Hangar"),
    # "Haroar" / "haroar" : confusion OCR n -> r dans "Hangar" (observee sur
    # les zones Hangar_MediumFront_Rest_NYX en perspective : le "n" devient "r"
    # sur certaines resolutions/angles de camera).
    ("Haroar",          "Hangar"),
    ("haroar",          "hangar"),
    ("HAROAR",          "Hangar"),
    # "ts_int_layout_keeger" et "s_int_layout_keeger" : confusions OCR
    # du vrai prefixe "rs_" (rest stop). Observees chez tester B (2K 21:9) et tester C (1080p).
    # Sans ce fix, 2 joueurs dans la MEME station ont des container_id
    # differents ("name:rs_..." vs "name:ts_...") -> pas de proximity possible.
    # Le prefixe "rs_" est le canonique (seul lu par tester A / 4K 16:9 qui a un OCR stable).
    ("Ts_int_layout_keeger", "Rs_int_layout_keeger"),
    ("ts_int_layout_keeger", "rs_int_layout_keeger"),
    ("TS_int_layout_keeger", "Rs_int_layout_keeger"),
    # Forme avec le "r" coupe en bord de zone OCR (s_int_layout_keeger)
    # observee chez tester B (2K 21:9 ultrawide). On prefixe avec "r" pour matcher la forme rs_.
    # Attention : il faut que ce fix soit appele AVANT celui "ts_" car
    # "ts_int..." contient aussi "s_int..." et on ne veut pas le double-patcher.
    # L'ordre ci-dessus (ts_ avant s_) est donc important.
    ("S_int_layout_keeger", "Rs_int_layout_keeger"),
    ("Solarsvstem",     "Solarsystem"),
    ("Solar_svstem",    "Solar_System"),
    # Microtech mal lu (M -> N)
    ("Nicrotech",       "Microtech"),
    ("nicrotech",       "microtech"),
    # Variantes de "OOC" (Out Of Container) au debut.
    # IMPORTANT : toutes sont normalisees en "OOc" (pas "OOC ") pour garder la
    # coherence avec la casse naturelle du HUD SC et eviter qu'on ait 2 variantes
    # canoniques differentes qui cassent la stabilite du container_id.
    ("OOC ",            "OOc "),
    ("OOc ",            "OOc "),   # canonique
    ("0Oc ",            "OOc "),
    ("0oc ",            "OOc "),
    ("Ooc ",            "OOc "),
    ("0OC ",            "OOc "),
    ("O0C ",            "OOc "),   # O majuscule + 0 chiffre + C majuscule
    ("O0c ",            "OOc "),
    ("OGc ",            "OOc "),
    ("OGC ",            "OOc "),
    ("GOc ",            "OOc "),
    # Suffixes frequents
    (" Entrv",          " Entry"),
    (" entrv",          " Entry"),
    (" Entrv ",         " Entry "),
    # "GS" (ship designation) mal lu
    (" G5 ",            " GS "),
    (" G5S ",           " GS "),
    # Lorville / Loreville variantes (lu en 1080p CPU)
    ("Lorvile",         "Lorville"),     # l manquant
    ("lorvile",         "lorville"),
    ("Loreville",       "Lorville"),     # e parasite entre r et v
    ("loreville",       "lorville"),
    # MediumTop variantes (m -> n, casse T)
    ("MediunTop",       "MediumTop"),
    ("Mediumtop",       "MediumTop"),
    ("mediumtop",       "MediumTop"),
    ("mediuntop",       "MediumTop"),
    # SmallTop / BigTop variantes (casse)
    ("Smalltop",        "SmallTop"),
    ("smalltop",        "SmallTop"),
    ("SmallTop",        "SmallTop"),   # canonique
    ("Bigtop",          "BigTop"),
    ("bigtop",          "BigTop"),
    # SecurityClearance (casse C)
    ("Securityclearance", "SecurityClearance"),
    ("securityclearance", "SecurityClearance"),
    # ANVL Pisces designation - c8R est la variante SC canonique
    (" cbr Pisces",     " C8R Pisces"),   # 8 lu comme b
    (" cbR Pisces",     " C8R Pisces"),
    (" c8R Pisces",     " C8R Pisces"),   # canonique minuscule -> majuscule
    (" c8r Pisces",     " C8R Pisces"),
    (" CBR Pisces",     " C8R Pisces"),
    # "5p int" / "5P int" - le 5 souvent lu "s" ou "S"
    (" sp int",         " 5p int"),
    (" Sp int",         " 5p int"),
    (" SP int",         " 5p int"),
    # ObjectContainer - casse premiere lettre instable
    ("objectContainer", "ObjectContainer"),
    ("objectcontainer", "ObjectContainer"),
    # ObjectContainer_Entry : forme canonique EST avec underscore
    # Variantes observees normalisees (sans _, avec espace, avec ~, avec L colle, avec 'a' parasite...)
    # ORDRE IMPORTANT : les variantes les plus specifiques (avec parasites) d'abord
    ("ObjectContainera _Entry",  "ObjectContainer_Entry"),
    ("ObjectContainera_Entry",   "ObjectContainer_Entry"),
    ("Objectcontainera _Entry",  "ObjectContainer_Entry"),
    ("Objectcontainera_Entry",   "ObjectContainer_Entry"),
    ("ObjectContainer __Entry",  "ObjectContainer_Entry"),
    ("ObjectContainer__Entry",   "ObjectContainer_Entry"),
    ("Objectcontainer __Entry",  "ObjectContainer_Entry"),
    ("Objectcontainer__Entry",   "ObjectContainer_Entry"),
    ("ObjectContainer _Entry",   "ObjectContainer_Entry"),   # espace avant _
    ("Objectcontainer _Entry",   "ObjectContainer_Entry"),
    ("ObjectContainer LEntry",   "ObjectContainer_Entry"),
    ("Objectcontainer LEntry",   "ObjectContainer_Entry"),
    ("Objectcontainer lentry",   "ObjectContainer_Entry"),
    ("ObjectContainer ~Entry",   "ObjectContainer_Entry"),
    ("Objectcontainer ~Entry",   "ObjectContainer_Entry"),
    ("Objectcontainer Entry",    "ObjectContainer_Entry"),   # espace au lieu de _
    ("ObjectContainer Entry",    "ObjectContainer_Entry"),
    ("Objectcontainer_Entry",    "ObjectContainer_Entry"),   # c minuscule
    ("ObjectContainerEntry",     "ObjectContainer_Entry"),   # sans separateur
    ("Objectcontainerentry",     "ObjectContainer_Entry"),
    ("ObjectContainerentry",     "ObjectContainer_Entry"),
    # Hangar sans espace vs avec (canonique : avec espaces/underscores separateurs)
    ("HangarSmallTop",  "Hangar SmallTop"),
    ("HangarMediumTop", "Hangar MediumTop"),
    ("HangarBigTop",    "Hangar BigTop"),
]

def _pretty_container_name(raw_name: str) -> str:
    """
    Corrige un nom de container lu par l'OCR pour l'affichage UI.
    N'affecte PAS l'identification (qui utilise container_id numerique).
    """
    if not raw_name:
        return raw_name
    name = raw_name
    # Appliquer les corrections texte simples
    for wrong, right in _OCR_NAME_FIXES:
        name = name.replace(wrong, right)
    # Correction "Oc Stanton" en debut de nom (un O manquant au debut) :
    # utiliser regex avec ancre ^ pour eviter de matcher dans "OOc Stanton"
    name = re.sub(r"^Oc Stanton", "OOc Stanton", name)
    # Nettoyer les parasites OCR : caracteres isoles colles en fin du nom.
    # Ex: "ObjectContainer_Entry S", "ObjectContainer_Entry 48", "ObjectContainer_Entry I"
    # Applique 2 fois pour gerer "... S I" -> "..."
    #
    # ATTENTION : avant CHAQUE passe de strip, on verifie si le nom termine par
    # un suffixe numerique LEGITIME (rs_int_layout_keeger_NN, tsg_gascloud_NNN...).
    # Si oui, on colle le suffixe au nom avec un underscore pour qu'il survive
    # au strip. Ce check est fait dans la boucle pour gerer les cas type
    # "rs int layout keeger 04 X" : la 1ere passe enleve le X parasite, puis
    # la 2e passe verifie le 04 et le preserve.
    for _ in range(2):
        # Check suffixe legitime
        m_legit = re.match(
            r"^(.+?)[\s_]+(-?\d{1,3})\s*$",
            name,
            flags=re.IGNORECASE,
        )
        if m_legit:
            base_raw = m_legit.group(1)
            suffix_num = m_legit.group(2).lstrip("-")
            base_norm = re.sub(r"[\s_-]+", "_", base_raw.strip().lower())
            candidate = f"{base_norm}_{suffix_num.zfill(2)}"
            # Acceptable si :
            #  (a) candidate exact dans la whitelist
            #  (b) candidate canonicalise via _correct_ocr_zone est dans la
            #      whitelist (couvre les variantes OCR : rs_int_zayout_keeger,
            #      rs_intlayout_keeger, rs_int_layout_keegeri, etc.).
            #      _correct_ocr_zone fait du fuzzy match Levenshtein contre
            #      _KNOWN_ZONES_NORM avec seuil adaptatif.
            # Sans ce check, le strip parasite mange le _04 pour toute variante
            # OCR du nom.
            is_legit = candidate in _KNOWN_ZONES
            if not is_legit:
                canonical = _correct_ocr_zone(candidate)
                is_legit = canonical in _KNOWN_ZONES
            if is_legit:
                # Suffixe legitime -> coller au nom avec underscore
                name = f"{base_raw.strip()}_{suffix_num.zfill(2)}"
                # Le nom est maintenant "<base>_<NN>" donc le strip ci-dessous
                # ne le touchera pas (pas d'espace avant le NN).
        name = re.sub(r"\s+[A-Za-z0-9]{1,2}\s*$", "", name).strip()
    # Retirer le suffixe "P<digits>" colle au nom (prefixe persistence SC).
    # SC affiche parfois "AEGS Idris P9875352433452" au lieu de "AEGS Idris 9875352433452",
    # ce qui fait varier le container_id et rompt la proximity. On retire tout
    # ce qui est " P<10+ chiffres>" ou "P<10+ chiffres>" colle au nom.
    # Applique APRES le nettoyage parasite (pour que "Idris P123 S" -> "Idris P123" -> "Idris").
    name = re.sub(r"\s*P\s*\d{10,}\s*$", "", name).strip()
    # Normaliser les noms "Solarsystem Zoooooo...<i|1>" / "Zoooooo...oi" qui varient
    # selon l'OCR (nombre de 'o' different, 'i' ou '1' final).
    # Regle : si le nom se termine par Z + une sequence de o + i|1|l, on canonise
    # en "Z" + "o" * N + "1" (N fixe arbitraire) pour stabiliser le container_id.
    name = re.sub(
        r"([Zz])(o+)[il1]\b",
        lambda m: m.group(1) + "o" * 10 + "1",
        name
    )
    # Normalisation prefixe grotte : SC utilise "rock01_" ou "sand01_" suivi de
    # "occu" ou "unoc" + suffixes numeriques. L'OCR varie entre "rock01", "rocko1",
    # "rockol", "rocko01", "Rock01" etc. a chaque lecture -> container_id instable
    # -> les joueurs se perdent mutuellement entre 2 lectures.
    # Regle : normaliser le prefixe grotte en "rock01"/"sand01" tout en preservant
    # le reste (suffixes _NNN_sizeNN_NNN_int[-NNN] identifiant l'instance precise).
    # Le pattern matche uniquement en tete de chaine, juste avant "_occu" ou "_unoc"
    # (avec espace ou underscore comme separateur) pour etre sur de ne toucher qu'au
    # prefixe et pas au milieu d'autres noms.
    name = re.sub(
        r"^(rock|sand)[o0O]*[l1I]+(?=[\s_](?:occu|unoc)[\s_])",
        lambda m: m.group(1) + "01",
        name,
        flags=re.IGNORECASE,
    )
    # Adoption du nom canonique : si le nom (espaces -> underscores, lowercase)
    # correspond exactement a une zone connue dont le nom canonique est tout
    # en underscores, on remplace les espaces par des underscores dans le nom
    # affiche. Cela corrige l'affichage UI pour qu'il montre le vrai nom SC
    # ("rs_int_layout_keeger_04") au lieu de la version avec espaces parasites
    # OCR ("rs int layout keeger_04").
    # On preserve la casse originale du nom (ne touche qu'aux espaces).
    # Limite : on ne touche pas aux noms qui contiennent un espace mais ne
    # sont pas dans la whitelist (ex: "OOc Stanton 4 Microtech" est legitime
    # avec espaces).
    name_underscored = re.sub(r"\s+", "_", name.strip()).lower()
    if name_underscored in _KNOWN_ZONES:
        # Le nom est dans la whitelist sous forme tout-en-underscores ->
        # remplacer les espaces par des underscores tout en preservant la casse
        name = re.sub(r"\s+", "_", name.strip())
    return name
_PAT_POS_ANY = re.compile(
    r"[Pp][o0O][sS]\s*[:\;\s]\s*"
    # Apres chaque chiffre : accepter soit l'unite canonique (m/km), soit
    # jusqu'a 4 caracteres alpha/parentheses/crochets parasites (artefacts OCR :
    # "Omi", "ui", "u)", "uu]", "u1", "l" etc.). Ces residus apparaissent quand
    # une source lumineuse (ex: etoile de Nyx derriere le HUD) deforme le
    # rendu des lettres m/km. Le groupe capturant unite est conserve (utilise
    # en aval pour detecter km vs m) ; si l'unite est polluee, elle sera vide
    # et le fallback "assume km" sera utilise.
    r"(-?[\d.]+)\s*(k[mn]?|km|m|[a-zA-Z\])]{0,4})?[\s,]+"
    r"(-?[\d.]+)\s*(k[mn]?|km|m|[a-zA-Z\])]{0,4})?[\s,\n]+"
    r"[a-zA-Z]{0,2}(-?[\d.]+)\s*(k[mn]?|km|m|[a-zA-Z\])]{0,4})?",
    re.IGNORECASE
)
# Nom de zone sur la meme ligne que Pos:
# La classe de caracteres accepte lettres, chiffres, underscore, tirets et espaces.
# Le tiret est necessaire pour les noms de grottes avec sous-index comme
# "rock01_unoc_001_size04_002_int-001" (le -001 est un sous-numero d'instance).
_PAT_ZONE_LINE = re.compile(
    r"[Zz][A-Za-z0-9]{1,4}\s*[:\;]\s*([A-Za-z0-9][A-Za-z0-9_\- ]*?)\s+[Pp][o0O][sS]",
    re.IGNORECASE
)
# Fallback : capturer le premier mot seulement si pas de "Pos:" sur la meme ligne
_PAT_ZONE_LINE_SIMPLE = re.compile(
    r"[Zz][A-Za-z0-9]{1,4}\s*[:\;]\s*([A-Za-z0-9][A-Za-z0-9_\-]*)",
    re.IGNORECASE
)

# Server ID patterns
# Pattern server ID : deux-points obligatoires (evite de matcher "Server FPS:")
# Tolere les espaces parasites OCR entre segments et le point a la place du tiret.
# re.MULTILINE pour que $ matche fin de ligne et pas fin de fichier entier.
_PAT_SERVER  = re.compile(
    r"[Ss]erver\s*:\s*(pub[-\s][a-zA-Z0-9\-\.\s]+?)(?:\s*,|\s*restarts|\s*$)",
    re.IGNORECASE | re.MULTILINE
)
_PAT_SHARD   = re.compile(r"\[shard\s+(\d+)", re.IGNORECASE)
_PAT_ALTITUDE = re.compile(r"[Aa]ltitude\s+([\d.]+)")



_SYSTEM_ZONE_PREFIXES = ("solarsystem", "solar_system", "root")
# Note: "stanton", "pyro", "nyx" retires   "OOC Stanton 4 Microtech" contient "stanton"

# Pattern regex tolerant aux erreurs OCR sur "SolarSystem" :
# - Premier bloc "solar" : tolere chiffre/lettre similaires (o->0/q, l->1/i, a->4)
# - Separateur optionnel : espace, _, -, ou rien
# - Second bloc "system" : tolere variantes
#     y -> v (Solarsvstem - cas reel observe)
#     m -> rn (Solarsystern)
#     manque de lettre (Solarsystm, Solarsystern)
# Cas couverts : Solarsystem, SolarSystem, Solarsvstem, So1arSystem,
# Solar System, Solar_System, S0larSystem, So-larSystem, etc.
_SYSTEM_ZONE_REGEX = re.compile(
    r"s[o0q][l1i][a4o]r[\s_\-]?s[yvz][s5]t[ae3][mn]+",
    re.IGNORECASE,
)




def _fix_server_id(raw: str) -> str:
    """Corrige les erreurs OCR dans le server ID SC."""
    def _levenshtein(a, b):
        if len(a) < len(b): return _levenshtein(b, a)
        if not b: return len(a)
        prev = list(range(len(b) + 1))
        for ca in a:
            curr = [prev[0] + 1]
            for j, cb in enumerate(b):
                curr.append(min(prev[j+1]+1, curr[j]+1, prev[j]+(ca!=cb)))
            prev = curr
        return prev[-1]
    raw = re.split(r"[,\s]", raw)[0].strip()
    parts = raw.split("-")
    corrected = []
    known_regions = ("euw1b", "use1b", "usw1b", "apse1b", "euc1b", "sgp1b")
    for i, part in enumerate(parts):
        if i == 0: corrected.append("pub")
        elif i == 1:
            p = part.lower().replace("l","1").replace("i","1")
            best, best_score = p, 0
            for known in known_regions:
                matches = sum(a==b for a,b in zip(p.ljust(len(known)), known))
                if matches > best_score and matches >= len(known)-2:
                    best_score, best = matches, known
            corrected.append(best)
        elif i == 2: corrected.append("sc")
        elif i == 3:
            corrected.append("alpha" if _levenshtein(part.lower(), "alpha") <= 3 else part.lower())
        else:
            corrected.append(part.replace("l","1").replace("L","1").replace("I","1").replace("O","0").replace("o","0"))
    return "-".join(corrected)


# Pattern global pour la premiere ligne de l'overlay SC
# (compile une fois au lieu d'etre recompile a chaque appel)
_PAT_FIRST_CONTAINER = re.compile(
    # Accepte "Zone", "Zole", "Zore", "Zorie" etc. : Z + o + 1-3 alpha + separateur.
    # Les lettres n/o/r/l/e peuvent etre confondues par EasyOCR selon le preprocessing.
    r"[Zz][o0O][A-Za-z]{1,3}\s*[:\s]\s*"
    # Premier caractere du nom : lettre OU chiffre suivi d'au moins une lettre.
    # Le cas "chiffre suivi de lettre" couvre les erreurs OCR frequentes type
    # "1evski" (1 lu au lieu de l), "0lisar" (0 au lieu de O), "5tarfarer"
    # (5 au lieu de S). Le _correct_ocr_zone fera la correction fuzzy ensuite.
    # On exclut les noms purement numeriques (ship IDs) en exigeant au moins
    # une lettre dans les 5 premiers chars.
    r"(?P<n>(?:[A-Za-z]|[0-9](?=[A-Za-z]))[A-Za-z0-9_\- ]*?)"
    # ID numerique optionnel, avec prefixe P optionnel (SC affiche parfois "AEGS Idris P 9875352433452").
    # Le P est l'initiale du prefix persistence SC, on l'ignore pour le container_id.
    r"(?:\s+[Pp]?\s*(?P<id>\d{10,}))?"
    r"\s*[- ]?\s*"
    r"[Pp][o0O]s\s*[:\s]\s*"
    r"(?P<x>-?[\d.]+)\s*(?P<ux>k?m)?[\s]+"
    r"(?P<y>-?[\d.]+)\s*(?P<uy>k?m)?[\s\n]+"
    r"[a-zA-Z]{0,2}(?P<z>-?[\d.]+)\s*(?P<uz>k?m)?",
    re.IGNORECASE
)


# ---------------------------------------------
#  Lock du suffixe Keeger (rs_int_layout_keeger_NN)
# ---------------------------------------------
# SC ne fournit pas de container_id numerique pour les batiments Keeger
# (bizarrement, contrairement aux TransitCarriage et Hangars qui ont un id
# stable). On retombe donc en fallback "name:..." base sur le nom OCR.
#
# Probleme : selon la lecture EasyOCR, le suffixe "_04" est tantot present,
# tantot absent (le "-04" est mal segmente, ou le "04" est avale par le regex
# Pos:). Resultat : le meme batiment alterne entre :
#   - name:rs_int_layout_keeger      (suffixe absent)
#   - name:rs_int_layout_keeger_04   (suffixe present)
# Ce yo-yo declenche un [JUMP CONFIRMED] a chaque oscillation et casse la
# proximity entre joueurs.
#
# Solution : "lock du suffixe". Quand on lit pour la 1ere fois rs_int_layout_keeger
# AVEC un suffixe numerique (_04), on memorise ce suffixe. Tant qu'on reste
# dans la famille rs_int_layout_keeger* (avec ou sans suffixe), on applique
# le suffixe locke. Des qu'on sort vers un autre container (TransitCarriage,
# Hangar, etc.), le lock est reset pour repartir frais a la prochaine entree.
#
# Limitation : si on entre directement dans Keeger sans jamais lire de suffixe
# (cas peu probable, mais possible si OCR systematiquement degrade), le lock
# reste vide et on garde le comportement actuel (id sans suffixe). Pas pire.

# Pattern pour detecter la famille Keeger sur nom DEJA canonicalise.
# Ce pattern est strict : il s'applique apres _correct_ocr_zone qui aura
# transforme les variantes OCR ("rs_int_zayout_keeger", "rs_intlayout_keeger",
# "rs_int_layout_keegeri" etc.) vers le nom canonique "rs_int_layout_keeger".
# On capture le suffixe numerique _NN s'il est present.
# Avant simplification, ce pattern etait tolerant aux variantes OCR mais c'etait
# redondant avec _correct_ocr_zone qui les gere deja via fuzzy match Levenshtein
# contre _KNOWN_ZONES_NORM.
_KEEGER_RE = re.compile(
    r"^rs_int_layout_keeger(?:_(\d{1,3}))?$",
    re.IGNORECASE
)

class _KeegerSuffixLock:
    """Memorise le dernier suffixe Keeger vu pour stabiliser le container_id."""
    def __init__(self):
        self.suffix = None   # str ex: "04" ou None si pas encore lock

    def apply(self, normalized_name: str) -> str:
        """
        Si le nom est dans la famille Keeger :
          - avec suffixe -> on lock ce suffixe et on retourne le nom canonique
          - sans suffixe -> on applique le suffixe locke (s'il existe)
        Si le nom n'est PAS dans la famille Keeger :
          - reset du lock et retour du nom inchange
        """
        m = _KEEGER_RE.match(normalized_name)
        if not m:
            # Sortie de la famille Keeger -> reset
            if self.suffix is not None:
                self.suffix = None
            return normalized_name
        # On est dans la famille Keeger
        suffix_seen = m.group(1)
        if suffix_seen:
            # Lecture avec suffixe -> on lock (si nouveau suffixe, on l'adopte)
            self.suffix = suffix_seen.zfill(2)   # "4" -> "04" pour stabiliser
            return f"rs_int_layout_keeger_{self.suffix}"
        # Lecture sans suffixe
        if self.suffix is not None:
            # On a un suffixe locke -> on l'applique
            return f"rs_int_layout_keeger_{self.suffix}"
        # Pas de lock encore -> retour brut
        return "rs_int_layout_keeger"

_keeger_lock = _KeegerSuffixLock()




# ======================================================================
# Bloc validation et logique metier (extrait du client1)
# ======================================================================

# --- _INTERIOR_KEYWORDS et _ZONE_MAP ---
_INTERIOR_KEYWORDS = [
    "hangar", "reststop", "station", "ship", "interior",
    "solarsystem", "solar_system", "spaceport", "platform",
]


# Completez au fur et a mesure des tests
_ZONE_MAP = {
    #    Stanton                               
    "stanton":    ("Stanton", "",          ""),

    # Hurston   aliases numeriques SC
    "hurston":    ("Stanton", "Hurston",   "Hurston"),
    "stanton1":   ("Stanton", "Hurston",   "Hurston"),
    "arial":      ("Stanton", "Hurston",   "Arial"),
    "stanton1a":  ("Stanton", "Hurston",   "Arial"),
    "aberdeen":   ("Stanton", "Hurston",   "Aberdeen"),
    "stanton1b":  ("Stanton", "Hurston",   "Aberdeen"),
    "magda":      ("Stanton", "Hurston",   "Magda"),
    "stanton1c":  ("Stanton", "Hurston",   "Magda"),
    "ita":        ("Stanton", "Hurston",   "Ita"),
    "stanton1d":  ("Stanton", "Hurston",   "Ita"),
    # Crusader   aliases numeriques SC
    "crusader":   ("Stanton", "Crusader",  "Crusader"),
    "stanton2":   ("Stanton", "Crusader",  "Crusader"),
    "cellin":     ("Stanton", "Crusader",  "Cellin"),
    "stanton2a":  ("Stanton", "Crusader",  "Cellin"),
    "daymar":     ("Stanton", "Crusader",  "Daymar"),
    "stanton2b":  ("Stanton", "Crusader",  "Daymar"),
    "yela":       ("Stanton", "Crusader",  "Yela"),
    "stanton2c":  ("Stanton", "Crusader",  "Yela"),
    # ArcCorp   aliases numeriques SC
    "arccorp":    ("Stanton", "ArcCorp",   "ArcCorp"),
    "stanton3":   ("Stanton", "ArcCorp",   "ArcCorp"),
    "lyria":      ("Stanton", "ArcCorp",   "Lyria"),
    "stanton3a":  ("Stanton", "ArcCorp",   "Lyria"),
    "wala":       ("Stanton", "ArcCorp",   "Wala"),
    "stanton3b":  ("Stanton", "ArcCorp",   "Wala"),
    # MicroTech   aliases numeriques SC (stanton4, stanton4a, etc.)
    "microtech":  ("Stanton", "MicroTech", "MicroTech"),
    "stanton4":   ("Stanton", "MicroTech", "MicroTech"),
    "calliope":   ("Stanton", "MicroTech", "Calliope"),
    "stanton4a":  ("Stanton", "MicroTech", "Calliope"),
    "clio":       ("Stanton", "MicroTech", "Clio"),
    "stanton4b":  ("Stanton", "MicroTech", "Clio"),
    "euterpe":    ("Stanton", "MicroTech", "Euterpe"),
    "stanton4c":  ("Stanton", "MicroTech", "Euterpe"),
    #    Pyro                                  
    # Noms techniques SC (debug overlay Zone:) :
    # pyro1 = Pyro I        pyro2 = Monox (planete)
    # pyro3 = Bloom (planete) pyro4 = Pyro IV
    # pyro5 = Pyro V (gaz)  pyro6 = Terminus (planete)
    # pyro5a=Ignis  pyro5b=Vatra  pyro5c=Adir
    # pyro5d=Fairo  pyro5e=Fuego  pyro5f=Vuur
    "pyro":       ("Pyro",    "",          ""),
    "pyro1":      ("Pyro",    "Pyro I",    "Pyro I"),
    "pyro2":      ("Pyro",    "Pyro II",   "Monox"),
    "pyro3":      ("Pyro",    "Pyro III",  "Bloom"),
    "pyro4":      ("Pyro",    "Pyro IV",   "Pyro IV"),
    "pyro5":      ("Pyro",    "Pyro V",    "Pyro V"),
    "pyro6":      ("Pyro",    "Pyro VI",   "Terminus"),
    # Lunes Pyro V
    "pyro5a":     ("Pyro",    "Pyro V",    "Ignis"),
    "pyro5b":     ("Pyro",    "Pyro V",    "Vatra"),
    "pyro5c":     ("Pyro",    "Pyro V",    "Adir"),
    "pyro5d":     ("Pyro",    "Pyro V",    "Fairo"),
    "pyro5e":     ("Pyro",    "Pyro V",    "Fuego"),
    "pyro5f":     ("Pyro",    "Pyro V",    "Vuur"),
    # Alias noms litteraux (retrocompatibilite)
    "monox":      ("Pyro",    "Pyro II",   "Monox"),
    "bloom":      ("Pyro",    "Pyro III",  "Bloom"),
    "terminus":   ("Pyro",    "Pyro VI",   "Terminus"),
    "ignis":      ("Pyro",    "Pyro V",    "Ignis"),
    "vatra":      ("Pyro",    "Pyro V",    "Vatra"),
    "adir":       ("Pyro",    "Pyro V",    "Adir"),
    "fairo":      ("Pyro",    "Pyro V",    "Fairo"),
    "fuego":      ("Pyro",    "Pyro V",    "Fuego"),
    "vuur":       ("Pyro",    "Pyro V",    "Vuur"),
    #    Nyx                                   
    "nyx":        ("Nyx",     "",          ""),
    "delamar":    ("Nyx",     "Delamar",   "Delamar"),
}


# --- _PAT_ZONE_POS et autres patterns ---
_PAT_ZONE_POS = re.compile(
    # "Zone:"   tolere Z0ne, Zone :, Zone:
    r"[Zz][o0O]ne\s*[:\s]\s*"
    # nom de zone   tout jusqu'a Pos: (ex: "OOC Stanton 4b Clio")
    r"(.+?)"
    r"\s*[- ]?\s*(?=[Pp][o0O]s\s*[:\s])"
    # "Pos:"   tolere P0s, Pos :, espaces variables
    r"[Pp][o0O]s\s*[:\s]\s*"
    # X   nombre   avec decimales, suivi de km/kn/m ou rien
    r"(-?[\d.]+)\s*k?[mn]?\s+"
    # Y
    r"(-?[\d.]+)\s*k?[mn]?\s+"
    # Z
    r"(-?[\d.]+)\s*k?[mn]?",
    re.IGNORECASE
)
# Fallback : ancien format XYPos
_PAT_XYPOS = re.compile(r"[Xx][Yy][Pp]os\s*[:\s]*(-?[\d.]+)\s+(-?[\d.]+)", re.I)
# Autres formats
_NUM     = r"-?\s*[\d][\d\s]*(?:[.,]\d+)?"
_PAT_XYZ = [
    re.compile(rf"[Xx]\s*[:\s]\s*({_NUM})\s*[Yy]\s*[:\s]\s*({_NUM})\s*[Zz]\s*[:\s]\s*({_NUM})", re.I),
    re.compile(rf"({_NUM})\s*/\s*({_NUM})\s*/\s*({_NUM})"),
]
_PAT_AXIS = {a: re.compile(rf"[{a.upper()}{a.lower()}]\s*[:\s]\s*({_NUM})") for a in "xyz"}

# --- Fonctions de validation ---
def _num(s: str):
    try: return float(s.replace(" ", "").replace(",", ".").replace("O", "0"))
    except: return None

def _is_interior_zone(zone_name: str) -> bool:
    """Retourne True si la zone est un interieur (station, vaisseau, etc.)."""
    low = zone_name.lower()

    if re.search(r'\d{6,}', zone_name):
        return True
    # Mots-cles interieur
    for kw in _INTERIOR_KEYWORDS:
        if kw in low:
            return True
    return False

def _zone_to_location(zone_name: str):
    """
    Convertit un nom de zone SC en (system, planet, body).
    Gere tous les formats SC :
      - "microtech"
      - "OOC Stanton 4 Microtech"
      - "OOC_Stanton_4_Microtech" (underscores)
      - "pyro5a"
    """
    if not zone_name:
        return None
    if _is_interior_zone(zone_name):
        return None

    def _try_key(k):
        k = k.lower().strip()
        k = re.sub(r'\s+', '', k)
        if not k or k in ("root",) or k.isdigit():
            return None
        # Essai direct
        r = _ZONE_MAP.get(k)
        if r: return r

        k2 = k.replace('0', 'o')
        r = _ZONE_MAP.get(k2)
        if r: return r

        k3 = k2.replace('1', 'l')
        r = _ZONE_MAP.get(k3)
        if r: return r
        return None

    # Essai 1 : nom complet tel quel
    result = _try_key(zone_name)
    if result:
        return result

    # Normaliser : remplacer underscores par espaces
    normalized = zone_name.replace("_", " ")

    # Essai 2 : nom normalise complet
    result = _try_key(normalized)
    if result:
        return result

    # Essai 3 : tester chaque mot (du dernier au premier)

    words = [w for w in normalized.strip().split() if w and not w.isdigit()]
    for word in reversed(words):
        result = _try_key(word)
        if result:
            return result

    return None


def _is_system_zone(zone_name: str) -> bool:
    """Retourne True si la zone est au niveau systeme (pas un corps celeste minable).

    Accepte :
    - Match exact ou prefixe pour les noms canoniques (solarsystem, root)
    - Variantes OCR via regex tolerant (Solarsvstem, So1arSystem, etc.)
    Ce filtre est important : quand le joueur est dans le menu de SC, la zone
    apparait souvent comme "SolarSystem_<ID>" et il ne faut pas envoyer la
    position au serveur (sinon tous les joueurs en menu se retrouvent dans le
    meme container).
    """
    if not zone_name:
        return False
    low = zone_name.lower().strip()
    # Match canonique (rapide)
    for prefix in _SYSTEM_ZONE_PREFIXES:
        if low == prefix or low.startswith(prefix + "_") or low.startswith(prefix + " "):
            return True
    # Match tolerant OCR : si le nom contient une variante de "SolarSystem"
    # n'importe ou (avec ou sans separateur), on filtre. Plus permissif que
    # le prefixe exact pour rattraper les cas ou l'OCR a corrompu certains
    # caracteres (s -> 5, o -> 0, l -> 1, a -> 4, m -> rn, etc.).
    if _SYSTEM_ZONE_REGEX.search(zone_name):
        return True
    return False



def _are_containers_similar(cid_a: str, cid_b: str) -> bool:
    """
    Detecte si 2 container_id sont des variantes OCR du meme container.

    Retourne True si :
    - Les 2 chaines ont la meme longueur (+/- 2 chars)
    - Elles different de 1 ou 2 caracteres max (distance de Hamming/Levenshtein)

    Typique :
      'name:levski v2 middeck' vs 'name:levski 2 middeck'   -> True (v manque)
      '9917714663391' vs '9917714563391'                      -> True (6 vs 5)
      '9910233194158' vs '9919233194158'                      -> True (0 vs 9)
      '9910233194158' vs '9917714658230'                      -> False (trop different)
    """
    if cid_a is None or cid_b is None:
        return False
    if cid_a == cid_b:
        return True

    # Longueurs doivent etre proches
    la, lb = len(cid_a), len(cid_b)
    if abs(la - lb) > 2:
        return False

    # Chaines trop courtes : ignorer (risque faux positif)
    if min(la, lb) < 6:
        return False

    # Distance de Levenshtein simple (limitee a 2)
    # On utilise une implementation iterative 2-rows
    if la == 0: return lb <= 2
    if lb == 0: return la <= 2

    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        curr = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if cid_a[i-1] == cid_b[j-1] else 1
            curr[j] = min(curr[j-1] + 1,       # insertion
                          prev[j] + 1,           # deletion
                          prev[j-1] + cost)      # substitution
        # Early exit : si tous les curr > 2, distance sera > 2
        if min(curr) > 2:
            return False
        prev = curr

    return prev[lb] <= 2


# =============================================================================
# MEMOIRE DE SIGNE PAR CONTAINER
# =============================================================================
#
# Probleme observe : EasyOCR rate frequemment le '-' devant un nombre, surtout
# quand le texte est petit ou bruite. La premiere lecture peut etre correcte
# (-30) mais les suivantes lisent (+30) pendant plusieurs secondes avant de
# revenir a (-30).
#
# Solution : on memorise le signe de chaque axe quand la valeur est
# suffisamment LOIN de zero (>= SIGN_NEAR_ZERO). Si une lecture suivante
# donne un signe oppose alors que la magnitude est encore loin de zero,
# on FORCE le signe memorise (correction du - mange par OCR).
#
# Reset de la memoire :
# - Changement de container : tout reset (nouveau contexte spatial)
# - Valeur < SIGN_NEAR_ZERO : on accepte que le signe puisse basculer
#   librement car on s'approche de l'origine et un vrai passage de l'autre
#   cote est plausible
#
# Anticipation : SIGN_NEAR_ZERO=8m permet de relaxer la memoire AVANT que
# le joueur n'atteigne reellement 0, ce qui evite le delai de plusieurs
# lectures lors d'un vrai passage par l'origine.
#
# Garde-fou : si la memoire devient fausse (cas rare, ex: changement de
# coordonnees Star Citizen suite a un patch), le systeme de vote existant
# (SIGN_FLIP_VOTE_TARGET lectures consecutives identiques) finit par
# basculer la memoire vers la nouvelle realite.

SIGN_NEAR_ZERO = 8.0  # metres : seuil sous lequel le signe peut flipper librement

# ANTI-VERROUILLAGE : si la memoire a ete posee a l'envers des le debut
# (cas tester D : OCR rate systematiquement le '-', donc 1ere lecture
# memorisee comme positive alors que la vraie valeur est negative), elle
# va corriger des centaines de fois dans le meme sens sans qu'aucun vote
# bascule ne vienne la corriger (parce que _apply_sign_memory inverse la
# valeur AVANT que le filtre _is_sign_flip ne s'execute).
#
# Garde-fou : on compte les corrections consecutives par (container, axe).
# Si on depasse SIGN_LOCK_RESET_THRESHOLD corrections sans bascule, on
# considere que la memoire est probablement fausse et on la reset pour
# cet axe. La prochaine lecture posera une nouvelle memoire (peut-etre
# correcte cette fois, ou l'auto-reset se redeclenchera dans 30 lectures
# si toujours faux).
SIGN_LOCK_RESET_THRESHOLD = 30  # nb de corrections consecutives avant verification
# Reset partiel apres verification de coherence : si les valeurs corrigees
# forment un deplacement physique coherent, on ne reset pas a 0 mais a ce
# palier intermediaire pour re-verifier plus tard.
SIGN_LOCK_PARTIAL_THRESHOLD = 15
# Seuil de coherence : si l'ecart-type des deltas entre lectures consecutives
# est sous ce seuil (en metres), on considere le deplacement comme continu
# (= correction probablement bonne, marche normale).
SIGN_COHERENCE_STD_MAX = 5.0
# Nombre de valeurs corrigees gardees en buffer pour le calcul de coherence
SIGN_COHERENCE_BUFFER_SIZE = 6

# Seuil de "memoire stable" pour la garde anti-faux-positif visuel :
# si un axe a une memoire de signe + un streak de corrections >= ce seuil,
# on considere la memoire comme TRES fiable (au moins N lectures consecutives
# concordantes du meme signe). Dans ce cas, si _restore_minus_signs marque
# un tiret restaure mais que le signe lu est OPPOSE a cette memoire stable,
# on suspecte un faux positif du scan visuel et on applique quand meme la
# memoire au lieu de bypasser.
#
# Bug observe (06/05/2026, tester B / 2K 21:9 ultrawide) : sur cet ecran,
# _restore_minus_signs detectait des tirets fantomes (artefacts de pixels
# interpretes comme des '-'), ce qui posait _minus_was_restored=True et
# bypassait _apply_sign_memory. La valeur OCR fausse etait alors acceptee
# alors que la memoire (30+ lectures concordantes) connaissait le bon signe.
# Resultat : oscillation +101/-101 en boucle, voix entendue a tort en
# proximite avec mauvaise position.
#
# Avec cette garde : la memoire stable l'emporte sur le scan visuel d'une
# seule frame. Si plus tard le signe change vraiment (le joueur traverse
# l'origine), le mecanisme de vote (SIGN_FLIP_VOTE_TARGET=3 lectures
# consecutives identiques) finira par basculer la memoire.
SIGN_RESTORE_TRUST_MIN_STREAK = 5

# Memoire des signes par container_id : { cid: {"x": -1, "y": +1, "z": -1} }
# -1 = signe negatif memorise
# +1 = signe positif memorise
# absent = pas encore de signe fiable pour cet axe
_sign_memory_per_container: dict = {}

# Compteur de corrections consecutives par (container_id, axe). Reset par
# bascule de vote ou par changement de container.
# Format : { (cid, axis): nb_corrections_consecutives }
_sign_correction_streak: dict = {}

# Buffer des dernieres valeurs corrigees par (container_id, axe) pour evaluer
# la coherence physique du deplacement avant un eventuel auto-reset.
# Format : { (cid, axis): [val_corrigee_1, val_corrigee_2, ...] }
# Limite a SIGN_COHERENCE_BUFFER_SIZE entrees, FIFO.
_sign_correction_history: dict = {}

# Compteur de detections consecutives "scan visuel oppose a memoire stable".
# Cle = (cid, axis), valeur = nb de frames consecutives ou la SIGN RESTORE GUARD
# a tire pour cet axe. Bug observe le 07/05/2026 (Levski middeck tester A/B) :
# le user change d'endroit dans le meme container, l'OCR lit correctement
# la nouvelle position avec son signe, mais la memoire a ete apprise dans
# l'endroit precedent avec le signe oppose. La SIGN RESTORE GUARD se
# declenche en boucle (constate dans le log : 30+ frames consecutives).
# Au dela de SIGN_RESTORE_GUARD_OVERRIDE_THRESHOLD detections consecutives
# du conflit, on considere que la memoire est fausse (pas le scan visuel)
# et on l'inverse.
# Format : { (cid, axis): nb_conflits_consecutifs }
_sign_restore_guard_streak: dict = {}

# Seuil au-dela duquel le SIGN RESTORE GUARD considere que la memoire est
# fausse et l'inverse, plutot que de continuer a corriger en boucle.
# Choix de 15 frames : a ~5 frames/s, ca fait 3 secondes de conflit
# permanent avant d'agir. Assez long pour ne pas etre faux positif
# (1-2 frames d'OCR foireux), assez court pour rattraper rapidement
# une mauvaise memoire (3s d'audio inversee est tolerable, 30s ne l'est pas).
SIGN_RESTORE_GUARD_OVERRIDE_THRESHOLD = 15


def _apply_sign_memory(pos: dict) -> tuple:
    """Applique la memoire de signe a la position pour corriger un eventuel
    '-' mange par OCR. Met a jour la memoire avec les signes confirmes.

    Retourne (pos_corrigee, corrections_log) ou corrections_log est une liste
    de tuples (axe, ancienne_val, nouvelle_val) pour chaque correction
    appliquee. Vide si aucune correction.

    La memoire est par container_id : 2 vaisseaux differents = 2 contextes
    spatiaux differents.

    FIX 1 (court-circuit minus) : si la frame courante a vu un tiret
    restaure visuellement par _restore_minus_signs (drapeau global
    _minus_was_restored=True), on bypass la correction. La detection
    visuelle pixel-perfect est plus fiable que la memoire de signe :
    si le scan pixel a vu un '-', il est reellement la, donc la valeur
    OCR contient deja le bon signe et on ne doit pas l'inverser.

    FIX 3 (anti-verrouillage) : compte les corrections consecutives par
    (container, axe). Si on depasse SIGN_LOCK_RESET_THRESHOLD, on reset
    la memoire de cet axe parce que c'est probablement une memoire posee
    a l'envers (cas observe : OCR rate systematiquement le '-', la 1ere
    lecture memorise un signe positif a la place du negatif, et toutes
    les lectures suivantes sont inversees a tort).
    """
    cid = pos.get("container_id")
    if not cid:
        # Pas de container : on ne peut pas memoriser ni appliquer
        return pos, []

    # Fix 1 : si un tiret a ete restaure visuellement sur cette frame,
    # on fait CONFIANCE au scan pixel direct -- SAUF si la memoire de signe
    # est tres stable (>= SIGN_RESTORE_TRUST_MIN_STREAK corrections concordantes)
    # ET que le signe lu pour un axe est l'oppose de cette memoire stable.
    # Dans ce cas-la, on suspecte un faux positif du scan visuel (artefact
    # de pixels) et on traite cet axe normalement (correction par memoire).
    # Les axes coherents avec leur memoire (ou sans memoire stable) suivent
    # le bypass classique.
    #
    # Pourquoi par axe et non en bloc : les artefacts visuels sont localises
    # sur une seule coord. Si on a un faux positif sur y, ca ne disqualifie
    # pas la confiance qu'on peut avoir sur x et z.
    if _minus_was_restored:
        mem = _sign_memory_per_container.setdefault(cid, {})
        # Compter les axes en conflit avec une memoire stable
        suspicious_axes = []
        for axis in ("x", "y", "z"):
            val = pos.get(axis)
            if val is None:
                continue
            if abs(val) < SIGN_NEAR_ZERO:
                continue
            memorized_sign = mem.get(axis)
            if memorized_sign is None or memorized_sign == 0:
                continue
            # Streak = nb de corrections concordantes consecutives sur cet axe.
            # Un streak eleve signifie que la memoire a ete validee plusieurs
            # fois de suite, donc tres fiable.
            streak = _sign_correction_streak.get((cid, axis), 0)
            if streak < SIGN_RESTORE_TRUST_MIN_STREAK:
                continue
            current_sign = -1 if val < 0 else (+1 if val > 0 else 0)
            if current_sign != 0 and current_sign != memorized_sign:
                # Conflit : scan visuel et memoire stable disent l'inverse.
                # On considere que c'est un faux positif visuel sur cet axe.
                suspicious_axes.append(axis)

        if not suspicious_axes:
            # Aucun conflit avec une memoire stable -> bypass classique :
            # on fait confiance au scan visuel et on met la memoire a jour
            # avec les signes lus.
            for axis in ("x", "y", "z"):
                val = pos.get(axis)
                if val is None:
                    continue
                if abs(val) >= SIGN_NEAR_ZERO:
                    current_sign = -1 if val < 0 else (+1 if val > 0 else 0)
                    mem[axis] = current_sign
                    # Reset le streak car on vient d'observer un signe via tiret
                    # restaure (donc plus reliable qu'une simple correction)
                    _sign_correction_streak.pop((cid, axis), None)
                    _sign_correction_history.pop((cid, axis), None)
                    # Reset aussi le compteur SIGN RESTORE GUARD (le scan visuel
                    # n'est plus en conflit avec la memoire)
                    _sign_restore_guard_streak.pop((cid, axis), None)
            return pos, []
        # Sinon : on a au moins un axe en conflit avec une memoire stable.
        # On incremente le compteur de conflits consecutifs par axe. Si on
        # depasse le seuil, on considere que la memoire est fausse et on
        # l'inverse, plutot que de continuer a faire confiance a la memoire.
        # Bug observe le 07/05/2026 sur Levski middeck : le user change
        # d'endroit dans le meme container, OCR lit correctement le signe
        # de la nouvelle position, memoire est fausse depuis l'ancien
        # endroit. Le scan visuel detecte le bon signe pendant 30+ frames
        # consecutives, mais le code precedent ignorait ce signal.
        axes_to_invert = []
        for axis in suspicious_axes:
            key = (cid, axis)
            n_conflicts = _sign_restore_guard_streak.get(key, 0) + 1
            _sign_restore_guard_streak[key] = n_conflicts
            if n_conflicts >= SIGN_RESTORE_GUARD_OVERRIDE_THRESHOLD:
                axes_to_invert.append(axis)

        if axes_to_invert:
            # Memoire jugee fausse pour ces axes : on l'inverse et on reset
            # les compteurs associes pour repartir sur des bases saines.
            for axis in axes_to_invert:
                old_sign = mem.get(axis)
                val = pos.get(axis)
                if val is None:
                    continue
                new_sign = -1 if val < 0 else (+1 if val > 0 else 0)
                mem[axis] = new_sign
                # Reset complet des compteurs : nouvelle memoire = nouveau depart
                _sign_correction_streak.pop((cid, axis), None)
                _sign_correction_history.pop((cid, axis), None)
                _sign_restore_guard_streak.pop((cid, axis), None)
                try:
                    _logger(
                        f"[SIGN MEMORY OVERRIDE] axe={axis} memoire inversee "
                        f"({old_sign:+d} -> {new_sign:+d}) apres "
                        f"{SIGN_RESTORE_GUARD_OVERRIDE_THRESHOLD} frames de "
                        f"conflit scan-visuel/memoire. La memoire etait "
                        f"probablement apprise sur un endroit different du "
                        f"meme container."
                    )
                except Exception:
                    pass
            # Tous les axes inverses sont maintenant alignes avec le scan
            # visuel : on retourne sans correction (la valeur OCR brute
            # est la bonne).
            # Pour les axes encore suspects mais pas encore overridee
            # (n_conflicts < seuil), on continue dans la logique normale.
            still_suspicious = [a for a in suspicious_axes if a not in axes_to_invert]
            if not still_suspicious:
                # Tout a ete resolu par override, on fait confiance au scan
                return pos, []

        try:
            _logger(
                f"[SIGN RESTORE GUARD] tiret restaure suspect sur {suspicious_axes} : "
                f"valeurs lues opposees a memoire stable (streak >= "
                f"{SIGN_RESTORE_TRUST_MIN_STREAK}). Bypass desactive, "
                f"correction par memoire appliquee."
            )
        except Exception:
            pass
        # On laisse continuer dans la logique de correction classique.

    mem = _sign_memory_per_container.setdefault(cid, {})
    corrections = []

    for axis in ("x", "y", "z"):
        val = pos.get(axis)
        if val is None:
            continue

        memorized_sign = mem.get(axis)
        # Le "signe" courant : -1 si negatif, +1 si positif, 0 si nul
        current_sign = -1 if val < 0 else (+1 if val > 0 else 0)

        # CORRECTION : si on a une memoire fiable ET le signe lu est l'oppose
        # ET la magnitude est suffisante pour exclure une oscillation pres de 0
        if (memorized_sign is not None
                and memorized_sign != 0
                and current_sign != 0
                and current_sign != memorized_sign
                and abs(val) >= SIGN_NEAR_ZERO):
            # Fix 3 : avant de corriger, verifier le streak.
            # Si on a deja correctione N fois consecutivement cet axe sans
            # qu'aucune bascule de vote ne soit venue valider, c'est suspect :
            # la memoire est probablement fausse depuis le debut.
            #
            # Fix 3.1 : on ne reset PAS aveuglement. On verifie d'abord la
            # coherence physique des valeurs corrigees. Cas typique observe :
            # joueur marche en ligne droite pendant 30+ lectures, l'OCR rate
            # systematiquement le tiret (signe negatif), la memoire corrige
            # correctement. Sans verif, le reset abandonnerait une correction
            # qui etait juste, et bascule la position du joueur du mauvais
            # cote (effet desastreux pour la VOIP de proximite).
            #
            # Verification : si les valeurs corrigees forment un deplacement
            # continu (ecart-type des deltas faible), on garde la memoire mais
            # on rabaisse le streak a SIGN_LOCK_PARTIAL_THRESHOLD pour
            # reverifier plus tard. Sinon, on reset comme avant.
            streak_key = (cid, axis)
            current_streak = _sign_correction_streak.get(streak_key, 0)
            if current_streak >= SIGN_LOCK_RESET_THRESHOLD:
                # Calcul de coherence sur les dernieres valeurs corrigees
                hist = _sign_correction_history.get(streak_key, [])
                coherent = False
                std_dev = None
                if len(hist) >= 3:
                    deltas = [hist[i] - hist[i-1] for i in range(1, len(hist))]
                    mean_d = sum(deltas) / len(deltas)
                    var = sum((d - mean_d) ** 2 for d in deltas) / len(deltas)
                    std_dev = var ** 0.5
                    coherent = std_dev <= SIGN_COHERENCE_STD_MAX

                if coherent:
                    # Deplacement continu detecte : la correction est bonne.
                    # On garde la memoire et on rabaisse le streak pour
                    # reverifier dans SIGN_LOCK_PARTIAL_THRESHOLD lectures.
                    _sign_correction_streak[streak_key] = SIGN_LOCK_PARTIAL_THRESHOLD
                    try:
                        _logger(f"[SIGN MEMORY KEEP] axe={axis} apres "
                                 f"{current_streak} corrections consecutives, "
                                 f"deplacement coherent (std={std_dev:.2f}m), "
                                 f"memoire conservee (streak rabaisse a "
                                 f"{SIGN_LOCK_PARTIAL_THRESHOLD})")
                    except Exception:
                        pass
                    # On applique tout de meme la correction de cette frame
                    corrections.append((axis, val, -val))
                    pos[axis] = -val
                    # MAJ buffer
                    hist = _sign_correction_history.setdefault(streak_key, [])
                    hist.append(-val)
                    if len(hist) > SIGN_COHERENCE_BUFFER_SIZE:
                        hist.pop(0)
                    continue

                # Pas coherent : reset comme avant (memoire posee a l'envers
                # tres probablement, valeurs corrigees chaotiques).
                mem.pop(axis, None)
                _sign_correction_streak.pop(streak_key, None)
                _sign_correction_history.pop(streak_key, None)
                _sign_restore_guard_streak.pop(streak_key, None)
                # Memoriser le signe actuel comme nouvelle reference
                mem[axis] = current_sign
                # On n'ajoute PAS de correction pour cette frame : la valeur
                # OCR brute est gardee telle quelle.
                try:
                    _logger(f"[SIGN MEMORY AUTO-RESET] axe={axis} apres "
                             f"{current_streak} corrections consecutives "
                             f"(std={std_dev if std_dev is not None else 'n/a'}"
                             f", incoherent), nouvelle reference "
                             f"signe={current_sign:+d} (val={val:.2f})")
                except Exception:
                    pass
                continue
            # Sinon : appliquer la correction comme avant et incrementer
            # le compteur de corrections consecutives
            corrections.append((axis, val, -val))
            pos[axis] = -val
            _sign_correction_streak[streak_key] = current_streak + 1
            # Memoriser la valeur corrigee dans le buffer pour la verif
            # de coherence si on atteint le seuil
            hist = _sign_correction_history.setdefault(streak_key, [])
            hist.append(-val)
            if len(hist) > SIGN_COHERENCE_BUFFER_SIZE:
                hist.pop(0)
            # Note : on ne met PAS a jour la memoire ici (la valeur originale
            # etait supposee fausse, pas de raison de polluer la memoire avec)
            continue

        # MISE A JOUR MEMOIRE : seulement si la valeur est claire (loin de 0).
        # On ne memorise pas les signes des valeurs sub-seuil car ils sont
        # trop bruites par OCR pour etre fiables.
        if abs(val) >= SIGN_NEAR_ZERO:
            mem[axis] = current_sign
            # Reset le streak : on vient d'observer une lecture coherente
            # avec la memoire, donc on remet a zero le compteur.
            _sign_correction_streak.pop((cid, axis), None)
            _sign_correction_history.pop((cid, axis), None)
            # Reset aussi le compteur SIGN RESTORE GUARD : la lecture est
            # coherente avec la memoire, donc plus de conflit.
            _sign_restore_guard_streak.pop((cid, axis), None)
        # Sinon on ne touche pas a la memoire (la valeur est dans la zone
        # de transition autour de 0, le signe peut legitimement flipper)

    return pos, corrections


def _reset_sign_memory(container_id: str | None = None):
    """Reset la memoire de signe.
    - container_id=None : reset toute la memoire (changement de session, etc.)
    - container_id="..." : reset uniquement ce container
    On reset aussi les compteurs de corrections consecutives associes.
    """
    global _sign_memory_per_container
    if container_id is None:
        _sign_memory_per_container = {}
        _sign_correction_streak.clear()
        _sign_correction_history.clear()
        _sign_restore_guard_streak.clear()
    else:
        _sign_memory_per_container.pop(container_id, None)
        # Retirer toutes les entrees de streak qui referencent ce container
        for key in list(_sign_correction_streak.keys()):
            if key[0] == container_id:
                _sign_correction_streak.pop(key, None)
        # Idem pour le buffer de coherence
        for key in list(_sign_correction_history.keys()):
            if key[0] == container_id:
                _sign_correction_history.pop(key, None)
        # Idem pour le compteur SIGN RESTORE GUARD
        for key in list(_sign_restore_guard_streak.keys()):
            if key[0] == container_id:
                _sign_restore_guard_streak.pop(key, None)


def _is_sign_flip(pos_a: dict, pos_b: dict,
                  tolerance_flipped: float = 5.0,
                  tolerance_unflipped: float = 15.0,
                  min_magnitude: float = 2.0) -> bool:
    """
    Detecte si pos_b est une variante "sign-flip" de pos_a.

    Un sign-flip est quand l'OCR a rate un '-' (ou en a ajoute un parasite),
    ce qui donne une position avec les memes coords en valeur absolue mais
    un signe different sur 1, 2 ou 3 axes.

    Retourne True si :
    - Meme container_id (ou les 2 None)
    - Au moins 1 axe a change de signe ET valeur absolue similaire
      (a tolerance_flipped pres) ET |valeur| >= min_magnitude
    - Les axes NON flippes restent dans une plage de mouvement realiste
      (a tolerance_unflipped pres = vitesse joueur ~10 m/s * intervalle ~1.5s)

    Typique :
      pos_a = {x:345, y:151, z:433}
      pos_b = {x:345, y:-151, z:-433}  <- flip sur Y et Z
      -> True (pollution OCR)

    Cas EXCLU par min_magnitude (sinon fausse detection massive) :
      pos_a = {x:-0.05, y:30, z:3.77}
      pos_b = {x:0.04, y:30, z:3.77}
      -> False (le X oscille a 10cm autour de zero, c'est l'OCR qui hesite
                a lire le '-' sur un petit chiffre, PAS un sign-flip)

    MAJ 27/04/2026 (1) : tolerance differenciee pour ne pas rater les sign-flips
    quand le joueur marche (l'axe non-flippe peut bouger de 5-10m entre 2
    lectures OCR a cadence 1-2/s sur ultrawide). Avant : tolerance unique
    1.5m -> tous les sign-flips manques quand tester B (2K 21:9) bougeait.

    MAJ 27/04/2026 (2) : ajout de min_magnitude (2m) pour eviter le faux positif
    massif quand un axe oscille entre +0.05 et -0.05 (immobile pres de l'origine).
    Constate dans les logs : 3732 SIGN FLIP IGNORE chez tester A (4K 16:9) dont 80% etaient
    des oscillations sub-metriques autour de x=0 dans son vaisseau.
    """
    # Meme container requis
    cid_a = pos_a.get("container_id")
    cid_b = pos_b.get("container_id")
    if cid_a != cid_b:
        return False

    xa, ya, za = pos_a.get("x", 0), pos_a.get("y", 0), pos_a.get("z", 0)
    xb, yb, zb = pos_b.get("x", 0), pos_b.get("y", 0), pos_b.get("z", 0)

    # Compter les axes avec flip (signe change) ET valeur absolue similaire
    # ET valeur absolue suffisamment grande pour etre un vrai flip de coord.
    flips = 0
    for va, vb in [(xa, xb), (ya, yb), (za, zb)]:
        if (va * vb < 0
                and abs(abs(va) - abs(vb)) < tolerance_flipped
                and max(abs(va), abs(vb)) >= min_magnitude):
            # Axe flippe LEGITIME : signes opposes + |val| identique a tolerance_flipped pres
            # + |val| >= min_magnitude (sinon c'est de l'oscillation sub-metrique)
            flips += 1
        elif abs(va - vb) > tolerance_unflipped:
            # Axe non-flippe avec ecart > tolerance mouvement -> pas un sign-flip
            return False
        # Sinon : axe non-flippe avec ecart raisonnable (le joueur a bouge un peu)
    # Au moins 1 axe doit avoir flippe
    return flips >= 1


def _is_cave_container(container_id: str, container_name: str) -> bool:
    """True si le container correspond a une grotte (active l'echo audio).

    On teste les 2 champs pour resister aux deux formats possibles :
      - container_id       = "name:rock01 occu 001 size03 001 int"
                             (fallback par nom, avec espaces)
      - container_name     = "rock01 occu 001 size03 001 int"
                             (nom brut lu par OCR)
    Le test ignore la casse et les separateurs (espaces, underscores, tirets).
    Tolere aussi les erreurs OCR typiques sur les chiffres :
      - "rocko1" au lieu de "rock01" (0 lu comme o)
      - "sand0l" au lieu de "sand01" (1 lu comme l)
    """
    for raw in (container_id, container_name):
        if not raw:
            continue
        # Normaliser : supprimer prefixe "name:", mettre en lowercase, remplacer
        # separateurs par _ pour homogeneiser
        s = raw.lower()
        if s.startswith("name:"):
            s = s[5:]
        s = re.sub(r"[\s\-]+", "_", s).strip("_")
        # Normaliser les erreurs OCR frequentes sur les chiffres du prefixe :
        # o/0 et l/1 sont tres facilement confondus. On applique sur les 6
        # premiers caracteres uniquement pour ne pas affecter le reste du nom.
        head = s[:7]   # "rock01_" ou "rocko1_" etc.
        head = head.replace("o", "0").replace("l", "1")
        s_norm = head + s[7:]
        for prefix in _CAVE_CONTAINER_PREFIXES:
            if s_norm.startswith(prefix):
                return True
    return False


# --- _CAVE_CONTAINER_PREFIXES ---
_CAVE_CONTAINER_PREFIXES = ("rock01_", "sand01_")

# ======================================================================
# Bloc parser principal (extrait du client1)
# ======================================================================

# Stub de log dedup (le bloc d'origine utilise une version cache; ici
# on delegue simplement au _logger global du module).
# Etat interne de _logger_dedup : memorise le dernier log par cle pour
# pouvoir throttler. Cle = nom logique (ex "ocr_engine", "easyocr_parse_fail").
# Valeur = (timestamp_du_dernier_log, derniere_value_logguee).
_logger_dedup_last = {}


def _logger_dedup(key, msg, value=None, min_interval=5.0):
    """Log un message via _logger uniquement si :
      - le 'value' change par rapport au precedent pour cette cle, OU
      - 'min_interval' secondes se sont ecoulees depuis le dernier log

    Sans ca, des messages comme "[OCR] Fallback Tesseract" spammeraient
    le log a 5-10 Hz quand SC n'est pas lance (l'OCR tourne en boucle a
    vide et tape Tesseract qui ne trouve rien). Avec ce throttling, on
    aura 1 message au demarrage puis 1 toutes les 60s tant que la
    situation ne change pas."""
    import time as _t
    now = _t.time()
    prev = _logger_dedup_last.get(key)
    should_log = False
    if prev is None:
        should_log = True
    else:
        prev_ts, prev_val = prev
        if value is not None and value != prev_val:
            should_log = True
        elif (now - prev_ts) >= min_interval:
            should_log = True
    if should_log:
        _logger_dedup_last[key] = (now, value)
        _logger(msg)

def _parse_coords(text: str):
    """
    Parse le texte OCR de la zone coordonnees.

    Priorites :




    """
    # (Neutralise pour ce module : le debug d'ecriture last_coords.txt
    # est utile dans CircusVOIP mais polluerait un module reutilisable.
    # Pour le reactiver, brancher un logger via set_logger().)

    # Tronquer au 1er '|' : EasyOCR insere ce caractere entre deux lignes quand
    # la zone OCR capture 2 lignes du HUD SC (Zone/Pos principale + sous-zone).
    # La 2e ligne est partiellement visible et pollue le parse :
    # ex. "Pos: 345.1m 151.0m 433.24m | Zone; evski 2 Mioueck Pos: 345.1 -151.0 -433.24"
    # Les signes '-' de la 2e ligne peuvent flipper les signes de la 1ere.
    if "|" in text:
        text = text.split("|", 1)[0].strip()

    result = {
        "x": None, "y": None, "z": None,
        "zone": None, "location": None,
        "surface": False,
        "server_id": None,
        "altitude": None,
        "container_id": None,   # ID numerique du container le + local (vaisseau, hangar, ascenseur)
        "container_name": None, # Nom de zone lisible ("TransitCarriage_LevskiLarge", "daymar", ...)
    }

    #    Altitude                                                  
    m_alt = _PAT_ALTITUDE.search(text)
    if m_alt:
        try:
            result["altitude"] = float(m_alt.group(1))
        except Exception:
            pass

    #    Container : 1ere ligne Zone:/Pos: avec ID numerique
    # Format typique :
    #   "Zone: TransitCarriage_LevskiLarge 9911606301781 Pos: -1.09m -2.77m 1.75m"
    #   "Zone: VRIU_12Ja 9894044829300 Pos: 0.44m 0.04m 0.05m"
    #   "Zone: daymar Pos: 295km 12km 300km"   (pas d'ID, nom de planete)
    # On prend la PREMIERE ligne qui contient a la fois "Zone:" et "Pos:"
    m_first = _PAT_FIRST_CONTAINER.search(text)
    if m_first:
        try:
            groups = m_first.groupdict()
            cname = (groups.get("n") or "").strip()
            cid   = groups.get("id")
            cx    = groups.get("x")
            cy    = groups.get("y")
            cz    = groups.get("z")
            cux   = (groups.get("ux") or "").lower()
            cuy   = (groups.get("uy") or "").lower()
            cuz   = (groups.get("uz") or "").lower()
        except Exception:
            # Fallback sur les groupes numerotes si les groupes nommes posent probleme
            cname = (m_first.group(1) or "").strip()
            try:
                cid = m_first.group(2)
            except Exception:
                cid = None
            cx = cy = cz = None
            cux = cuy = cuz = ""

        if cname:
            # Normaliser le nom via _pretty_container_name pour que le container_id
            # soit stable entre lectures (ex: Microtech lu tantot Nicrotech tantot Microtech).
            cname_clean = _pretty_container_name(cname)
            result["container_name"] = cname_clean
            if cid:
                result["container_id"] = cid
                # On a un id numerique stable -> reset du lock Keeger SI on a
                # quitte la famille Keeger. Si une lecture parasite (ex: 2eme ligne
                # du HUD lue par accident) capture un id numerique TransitCarriage
                # alors qu'on est encore dans Keeger, on ne reset PAS le lock,
                # sinon la prochaine lecture Keeger sans suffixe perd le _04.
                # On normalise le nom avant de tester (espaces -> underscores).
                if _keeger_lock.suffix is not None:
                    name_norm = _correct_ocr_zone(cname_clean.lower().strip())
                    if not _KEEGER_RE.match(name_norm):
                        _keeger_lock.suffix = None
            else:
                # Pas d'ID numerique -> utiliser le nom normalise comme identifiant
                # (ex: "daymar", "microtech" en surface planetaire, "OOc Stanton 4 Microtech")
                normalized = _correct_ocr_zone(cname_clean.lower().strip())
                # Lock du suffixe Keeger : stabilise rs_int_layout_keeger_NN entre
                # les lectures OCR ou le suffixe est tantot present tantot absent.
                normalized = _keeger_lock.apply(normalized)
                result["container_id"] = f"name:{normalized}"

            # Extraire aussi les coords directement depuis cette 1ere ligne
            try:
                if cx is not None and cy is not None and cz is not None:
                    x_val = float(cx)
                    y_val = float(cy)
                    z_val = float(cz)
                    # Convertir km en m si besoin
                    if cux.startswith("k"): x_val *= 1000.0
                    if cuy.startswith("k"): y_val *= 1000.0
                    if cuz.startswith("k"): z_val *= 1000.0

                    # GARDE MENU : si la zone est une zone systeme racine
                    # ("SolarSystem_<big_number>") avec coords petites, c'est
                    # qu'on est dans le menu principal de SC (frontend), pas en jeu.
                    # Rejeter pour ne pas envoyer de position (et pour ne pas
                    # mettre tout le monde dans le meme container "menu").
                    if _is_system_zone(cname_clean) and abs(x_val) < 1000 and abs(y_val) < 1000 and abs(z_val) < 1000:
                        # Dedup : pas besoin de logger des milliers de fois la
                        # meme info pendant que le joueur reste sur le menu.
                        # 1 log par minute suffit a savoir qu'on est en menu.
                        # Cle dedup en lowercase pour eviter que les variations
                        # de casse de l'OCR (Solarsystem vs SolarSystem) creent
                        # des cles distinctes qui contournent le dedup.
                        _logger_dedup(
                            "menu_ignore",
                            f"[MENU IGNORE] zone systeme detectee avec coords petites : "
                            f"{cname_clean} pos=({x_val},{y_val},{z_val}) -> position ignoree",
                            value=cname_clean.lower(),
                            min_interval=60.0,
                        )
                        return None

                    result["x"] = x_val
                    result["y"] = y_val
                    result["z"] = z_val
                    # zone : utiliser le nom canonicalise pour la coherence avec
                    # container_id et l'affichage UI. Sans ca, des erreurs OCR
                    # comme "fs_int_layout_keeger_04" (f au lieu de r) appraitraient
                    # dans les logs et l'UI alors que container_id est correct.
                    result["zone"] = _correct_ocr_zone(
                        cname_clean.lower().replace(" ", "_")
                    )
                    # On a tout : container_id + coords -> retour direct
                    # (bypass des anciens patterns all-001/solarsystem)
                    return result
            except (ValueError, TypeError):
                pass

    #    Server ID                                                 
    m_srv = _PAT_SERVER.search(text)
    if m_srv:
        raw_srv = m_srv.group(1).strip()
        # Nettoyage OCR avant fix :


        import re as _re2
        raw_srv = _re2.sub(r'\s+', '', raw_srv)           # supprimer espaces

        fixed   = _fix_server_id(raw_srv)
        result["server_id"] = fixed
        # Extraire et logger le game_id (segment [5]) et game_num (segment [7])
        parts = fixed.split("-")
        game_id  = parts[5] if len(parts) > 5 else None
        game_num = parts[7] if len(parts) > 7 else None
        if game_id or game_num:
            pass  # log desactive
        else:
            pass  # log desactive
    else:
        # Fallback : numero de shard "[shard 357, ...]"
        m_shard = _PAT_SHARD.search(text)
        if m_shard:
            result["server_id"] = f"shard-{m_shard.group(1)}"
            _logger(f"[SERVER SHARD] {result['server_id']}")

    # CamDir supprime   heading via azimut HUD OCR


    #    Zone: NOM Pos: Xkm Ykm Zkm                             
    candidates = []
    for m in _PAT_POS_ANY.finditer(text):
        x = _num(m.group(1)); y = _num(m.group(3)); z = _num(m.group(5))
        if None in (x, y, z): continue

        unit_x = (m.group(2) or "").lower()
        unit_y = (m.group(4) or "").lower()
        unit_z = (m.group(6) or "").lower()

        # Convertit chaque valeur individuellement selon son unite.
        # Detection robuste : on regarde si l'unite commence par 'k' (km) ou
        # contient 'm' (avec tolerance pour le bruit OCR : "mi", "mu1", "m1"...).
        # Si totalement illisible, on assume 'm' par defaut car les coordonnees
        # en 'km' apparaissent presque uniquement dans les zones "Root" ou
        # "SolarSystem" qui sont filtrees ailleurs. En zone container (station,
        # vaisseau), l'unite est TOUJOURS 'm'.
        def _to_meters(val, unit):
            if not unit:
                return val       # pas d'unite : assume metre (format HUD SC)
            u = unit.lstrip()
            if u.startswith("k"):
                return val * 1000.0
            # 'm', 'mi', 'mu', 'm1', 'u1', 'ui', etc. -> metre
            return val

        x_m = _to_meters(x, unit_x)
        y_m = _to_meters(y, unit_y)
        z_m = _to_meters(z, unit_z)

        # Sanity check Z : si Z n'a pas d'unite explicite, l'OCR a peut-etre
        # rate le "m" ou "km" final. On teste les deux interpretations et on
        # garde celle qui donne une magnitude coherente avec le rayon du corps actif.
        # Plage dynamique : R   5% (altitude miniere max ~5% du rayon)
        # Uniquement actif en surface planetaire (x et y en km), pas en station.
        if not unit_z and unit_x.startswith("k") and unit_y.startswith("k"):
            # Estimer le rayon depuis x_m/y_m (on ne connait pas encore z_m)
            _r_est = math.sqrt(x_m*x_m + y_m*y_m)  # rayon equatorial minimum
            if _r_est > 5_000:  # seulement si on est clairement a la surface
                _BODY_MIN = _r_est * 0.90
                _BODY_MAX = _r_est * 1.15
                for z_candidate in [z_m, z]:  # z_m = km assume, z = valeur brute (m)
                    mag = math.sqrt(x_m*x_m + y_m*y_m + z_candidate*z_candidate)
                    if _BODY_MIN <= mag <= _BODY_MAX:
                        if z_candidate != z_m:
                            _logger(f"[COORDS] Z corrige sans unite: {z_candidate:.2f}m "
                                     f"(magnitude {mag:.0f}m = {mag/1000:.0f}km)")
                        z_m = z_candidate
                        break

        # Ignorer si toutes les valeurs sont en metres et tres petites (ship interior)
        if all(u == "m" for u in [unit_x, unit_y, unit_z] if u):
            continue

        magnitude = math.sqrt(x_m*x_m + y_m*y_m + z_m*z_m) / 1000

        # Nom de zone : cherche sur la meme ligne ET la ligne precedente
        # SC peut ecrire "Zone: OOC Stanton 4 Microtech\nPos: 884km..."
        line_start = text.rfind("\n", 0, m.start())
        line_text  = text[line_start:m.start()]
        # Ligne precedente (au cas ou Zone: est separe de Pos:)
        prev_line_start = text.rfind("\n", 0, max(0, line_start - 1))
        prev_line_text  = text[prev_line_start:line_start]

        # Chercher Zone: sur la ligne courante ET les 3 lignes precedentes
        zone_m = None
        search_lines = [line_text]
        _pos = line_start
        for _ in range(3):
            _prev_start = text.rfind("\n", 0, max(0, _pos - 1))
            _prev_text  = text[_prev_start:_pos]
            search_lines.append(_prev_text)
            _pos = _prev_start
            if _pos <= 0:
                break

        for _sl in search_lines:
            zone_m = _PAT_ZONE_LINE.search(_sl + " Pos:")
            if not zone_m:
                zone_m = _PAT_ZONE_LINE_SIMPLE.search(_sl)
            if zone_m:
                break

        zone_name = zone_m.group(1).strip() if zone_m else None

        # Filtrer zones interieures ET zones systeme
        if zone_name and (
            zone_name.lower() == "root"
            or _is_interior_zone(zone_name)
            or _is_system_zone(zone_name)
        ):
            continue

        candidates.append((magnitude, x_m, y_m, z_m, zone_name))

    if candidates:
        candidates.sort(key=lambda c: c[0])
        BODY_MIN =     10.0   # km   exclut ship interior
        BODY_MAX = 50_000.0   # km   exclut SolarSystem/Root (~48M km)
        for mag, x, y, z, zn in candidates:
            if BODY_MIN <= mag <= BODY_MAX:
                result.update(x=x, y=y, z=z, surface=True)
                if zn and not result.get("zone"):
                    normalized = _normalize_zone_name(zn)
                    result["zone"] = normalized if normalized else zn
                    loc = _zone_to_location(zn)
                    if loc: result["location"] = loc
                return result


    # Fallback : cas des stations (Levski, Area18, etc.)
    # L'overlay SC affiche plusieurs lignes :
    #   Zone: TransitCarriage_LevskiLarge... Pos: ...m ...m ...m   (transit, rejete)
    #   Zone: levski_v2 middeck Pos: ...m ...m ...m                (sous-zone, rejete si trop petit)
    #   Zone: levski all-001 Pos: 1300m 2720m 2450m                 (LA bonne position)
    #   Zone: SolarSystem_XXX Pos: -9641669km ...                   (systeme, rejete)
    #   Zone: Root Pos: ...                                         (root, rejete)
    # On cherche une ligne de type "XXX all-NNN" (vraie position station)
    # dans les 5 lignes au-dessus de SolarSystem.
    # Pattern tolerant OCR : accepte "SolarSystem", "Solarsystem", "Solarsvstem",
    # "SolarSvstem" (confusion v/y par EasyOCR).
    _pat_solarsystem = re.compile(
        r"[ZL][o0O]ne\s*[:\s]\s*[Ss]olar[_\s]?[SsVv][yvw]stem",
        re.IGNORECASE
    )
    # Ligne de type "XXX all-NNN" ou "XXX_ALL-NNN" : identifie la vraie station
    _pat_station_zone = re.compile(
        r"[ZL][o0O]ne\s*[:\s]\s*([A-Za-z][A-Za-z0-9_ ]*?\s*all[-_]\d+)",
        re.IGNORECASE
    )
    m_ss = _pat_solarsystem.search(text)
    if m_ss:
        # Remonter jusqu'a 5 lignes au-dessus de SolarSystem pour trouver
        # une ligne "XXX all-NNN" qui correspond a la vraie zone station
        ss_line_start = text.rfind("\n", 0, m_ss.start())
        _pos = ss_line_start
        above_lines = []
        for _ in range(5):
            _prev_start = text.rfind("\n", 0, max(0, _pos - 1))
            above_lines.append(text[_prev_start:_pos])
            _pos = _prev_start
            if _pos <= 0:
                break

        # Chercher parmi ces lignes celle qui contient "XXX all-NNN"
        target_line = None
        target_zone = None
        for line in above_lines:
            m_sta = _pat_station_zone.search(line)
            if m_sta:
                target_line = line
                target_zone = m_sta.group(1).strip()
                break

        if target_line:
            # Le texte OCR a souvent des espaces dans les nombres (ex: "958 . 92m")
            # _normalize_numbers corrige ces artefacts
            target_line_clean = _normalize_numbers(target_line)
            m_pos = _PAT_POS_ANY.search(target_line_clean)
            if m_pos:
                x = _num(m_pos.group(1)); y = _num(m_pos.group(3)); z = _num(m_pos.group(5))
                if None not in (x, y, z):
                    unit_x = (m_pos.group(2) or "").lower()
                    unit_y = (m_pos.group(4) or "").lower()
                    unit_z = (m_pos.group(6) or "").lower()

                    def _to_m(val, unit):
                        if unit.startswith("k"): return val * 1000.0
                        if unit == "m":          return val
                        return val * 1000.0

                    x_m = _to_m(x, unit_x)
                    y_m = _to_m(y, unit_y)
                    z_m = _to_m(z, unit_z)

                    result.update(x=x_m, y=y_m, z=z_m, surface=True)
                    # Normaliser le zone_name pour la comparaison entre clients
                    normalized = _normalize_zone_name(target_zone)
                    result["zone"] = normalized if normalized else target_zone
                    loc = _zone_to_location(target_zone)
                    if loc: result["location"] = loc
                    # Log uniquement quand la zone change (evite le spam)
                    _logger_dedup(
                        "coords_station",
                        f"[COORDS STATION] zone={result['zone']} x={x_m:.0f}m y={y_m:.0f}m z={z_m:.0f}m",
                        value=result["zone"],
                        min_interval=30.0,
                    )
                    return result

    return None


def _normalize_numbers(text: str) -> str:
    """Corrige les erreurs OCR typiques dans les nombres."""
    # ============================================================
    # ETAPE 1 : conversions OCR de caracteres -> chiffres
    # ============================================================
    # Doit etre fait AVANT les regles de reconstitution decimale car
    # celles-ci dependent du fait que les nombres soient lisibles comme
    # tels. Ex: "5lm" doit devenir "51m" avant que "(\d+)\s+(\d{1,3})(m|km)"
    # puisse matcher "193 51m" -> "193.51m". Sinon le point decimal entre
    # "193" et "51" n'est pas reconstruit et le parsing donne x=193 y=51
    # au lieu du vrai x=193.51.
    text = re.sub(r"(?<=\d)O(?=\d)", "0", text)
    # O/o/Q entre chiffre et unite (ex: 0.0om -> 0.00m, 0Q0m -> 000m, 0Om -> 00m).
    # Le cas "0Om" (majuscule O apres un 0) est typique : EasyOCR lit le 2e zero
    # de "0.00m" comme un O majuscule. Sans ce fix, "165 0Om" reste tel quel et
    # le parser interprete "0" comme z et ignore le reste, donnant z=0 au lieu
    # de la vraie valeur du z (sur la coord suivante).
    text = re.sub(r"(?<=\d)[oOQ](?=m|k)", "0", text)
    text = re.sub(r"(?<=\d)[oQ](?=\d)", "0", text)
    # Q en debut de nombre apres espace (ex: "0 Q0m" -> "0 00m")
    text = re.sub(r"(?<=\s)Q(?=\d)", "0", text)
    # l   1 dans les nombres (ex: 546.268lkm   546.2681km, "5lm" -> "51m")
    text = re.sub(r"(?<=\d)l(?=\d|k|m)", "1", text)
    text = re.sub(r"(?<=\d)l(?=\b)", "1", text)

    # ============================================================
    # ETAPE 2 : reconstitution des separateurs decimaux
    # ============================================================
    # Corriger "958 . 92" ou "958 , 92" -> "958.92" (espaces autour separateur decimal)
    text = re.sub(r"(\d)\s+[.,]\s+(\d)", r"\1.\2", text)
    # Corriger "1122 .56" ou "1122,56" avec juste 1 espace avant
    text = re.sub(r"(\d)\s+([.,])(\d)", r"\1\2\3", text)
    # Corriger "40, 72m" -> "40.72m" : virgule puis espace puis chiffre
    # (Tesseract et EasyOCR produisent parfois cette forme, espace insere apres
    # la virgule decimale au lieu d'avant). Le ".," matche les 2 separateurs OCR.
    text = re.sub(r"(\d)[.,]\s+(\d)", r"\1.\2", text)
    # Corriger les decimales eclatees par OCR. Cas typiques :
    #   "2365 45m"      -> "2365.45m"     (1 espace = 1 separateur decimal)
    #   "420 5 9m"      -> "420.59m"      (2 espaces = decimale eclatee en 3)
    #   "420 5 9 12m"   -> "420.5912m"    (3 espaces, rare mais possible)
    #
    # Pour les coords en km on tolere 4 chiffres apres le point (ex: "42 1473km"
    # = "42.1473km"). Les coords en m sont typiquement des coords intra-zone
    # avec 1-3 chiffres de precision (ex: "420.59m").
    #
    # IMPORTANT : on applique les patterns LES PLUS LARGES en premier, sinon
    # la 1ere regle consommerait les espaces les plus a droite et empecherait
    # les suivantes de matcher.
    # Ex: sur "420 5 9m", si on applique d'abord (\d+)\s+(\d{1,3})m,
    # on obtient "420 5.9m" et la regle pour "X Y Z m" ne matche plus.
    #
    # On inclut kM (M majuscule) en plus de km : OCR confond parfois la casse.
    # La normalisation kM->km plus loin (ligne ~2056) ne suffit pas a elle
    # seule car elle s'execute APRES nos regles : si elles n'ont pas matche
    # a cause de la casse, l'occasion est perdue.
    # ============================================================
    # ETAPE 2a : Normaliser les unites OCR erronees AVANT la reconstruction
    # decimale, pour que les regles "(\d+)\s+(\d{1,4})(m|km|kM)\b" matchent
    # meme quand l'OCR a foire le m/km final.
    # Variantes observees :
    #   - "kJ", "kI", "kIl", "kil", "kii", "kIi", "k0l", "kml", "kmi" -> "km"
    #   - "92ii", "92il", "92Ii" -> "92m" (le "m" segmente en 2 jambages)
    # ============================================================
    text = re.sub(r"(\d\.?\d*)\s*[er][mn]\b", r"\1km", text)
    # k suivi de 1-4 chars douteux (I, i, L, l, J, j, ., m) -> km
    text = re.sub(r"(\d\.?\d*)\s*k[IiLlJj\.m]{1,4}\b", r"\1km", text)
    # Variantes OCR de "km" : kM (majuscule), k0/k9 (chiffre lu a la place de m/n),
    # kN (n lu N), k suivi de 1-2 chars douteux
    text = re.sub(r"(\d\.?\d*)\s*k[MN0-9]{1,2}\b", r"\1km", text)
    text = re.sub(r"(\d\.?\d*)\s*k\b(?!m)", r"\1km", text)
    # Unite "m" lue par OCR comme "ii", "il", "Ii", "iL" : observe sur Pyro2
    # 07/05/2026 (frame 14:47:53 : "92ii" au lieu de "92m"). Le "m" final est
    # mal segmente en 2 jambages verticaux que l'OCR voit comme 2 lettres.
    # Pas de \b a la fin car l'OCR colle parfois les ii au caractere suivant.
    text = re.sub(r"(\d\.\d{1,4}|\d+)\s*[iIlL]{2,3}(?=\s|\Z)", r"\1m", text)

    # ============================================================
    # ETAPE 2b : reconstitution des separateurs decimaux
    # ============================================================
    # Corriger "958 . 92" ou "958 , 92" -> "958.92" (espaces autour separateur decimal)
    text = re.sub(r"(\d)\s+[.,]\s+(\d)", r"\1.\2", text)
    # Corriger "1122 .56" ou "1122,56" avec juste 1 espace avant
    text = re.sub(r"(\d)\s+([.,])(\d)", r"\1\2\3", text)
    # Corriger "40, 72m" -> "40.72m" : virgule puis espace puis chiffre
    text = re.sub(r"(\d)[.,]\s+(\d)", r"\1.\2", text)
    # Corriger les decimales eclatees par OCR (1-3 espaces internes).
    # Note 09/05/2026 : on inclut "M" majuscule SIMPLE (sans k) en plus de
    # m/km/kM. EasyOCR en 1080p lit parfois l'unite "m" comme "M" (sensible
    # a la casse selon le rendu du HUD). Sans cette extension, "18 76M" reste
    # tel quel et le parser interprete "18" et "76" comme 2 nombres separes
    # -> bug "x=18 y=76 z=-18" au lieu de "x=18.76 y=-18.46 z=-114".
    text = re.sub(r"(\d+)\s+(\d)\s+(\d)\s+(\d{1,2})(m|M|km|kM)\b", r"\1.\2\3\4\5", text)
    text = re.sub(r"(\d+)\s+(\d{1,2})\s+(\d{1,2})(m|M|km|kM)\b",  r"\1.\2\3\4",   text)
    text = re.sub(r"(\d+)\s+(\d{1,4})(m|M|km|kM)\b",              r"\1.\2\3",     text)

    # Fix 1080p (09/05/2026) : EasyOCR lit parfois le "m" final comme un "0"
    # (zero), notamment quand le caractere "m" est mal segmente en bas resolution.
    # Cas observe : "313.13m -113.75m" lu comme "313 130 -113 75m" :
    #   - le "." du milieu est devenu un espace (deja gere par les regles ci-dessus)
    #   - le "m" est devenu un "0", donc "313.130" au lieu de "313.13m"
    # Resultat : 4 nombres au lieu de 3 -> parseur prend les 3 premiers et le z
    # -113.75m est ignore.
    # Solution : si on voit "<digits> <digits>0 " suivi par un "-" ou un autre
    # nombre, c'est que le "0" final est probablement un "m" mal lu. On fusionne
    # en "<digits>.<digits>m".
    # Conditions strictes pour eviter les faux positifs :
    #   - Le 2e fragment fait 2-4 chiffres dont le DERNIER est "0"
    #   - Pas de "." avant les chiffres (ne pas casser les vrais decimaux)
    #   - Suivi d'un caractere de fin de coord (espace puis "-", ou fin de ligne)
    text = re.sub(
        r"(?<![\d.])(\d+)\s+(\d{1,3})0(?=\s+[-\d])",
        r"\1.\2m",
        text
    )
    # Supprimer espace apres point decimal (ex: "808. 4356"   "808.4356")
    text = re.sub(r"(\d\.)\ +(\d)", r"\1\2", text)
    text = re.sub(r"(\d)\)(\d)", r"\1.\2", text)
    text = re.sub(r"(\d),(\d)",  r"\1.\2", text)
    # Caracteres parasites entre chiffres : / et \ sont souvent un point decimal
    # mal lu par OCR (ex: "7//74m" qui est 7.74m). Le remplacement par "." est
    # plus sur que par rien, car supprimer transformerait "7//74m" en "774m"
    # (erreur de facteur 100 sur la coord).
    text = re.sub(r"(\d)[|/\\]{1,2}(\d{1,3})(?=m|k|\b)", r"\1.\2", text)
    # Pour les cas "562/78/9" (plusieurs / au milieu), on supprime les / internes
    # APRES avoir traite le cas "simple" ci-dessus.
    text = re.sub(r"(\d)[|/\\]{1,2}(\d)", r"\1\2", text)
    # Supprimer / et \ en debut de nombre apres virgule/espace
    text = re.sub(r"([,\s])[/\\](\d)", r"\1\2", text)
    # Nombres entiers melanges chiffre/lettre finissant par km/m (OCR agressif) :
    # "618.0ol8km" -> "618.0018km" (o->0, l->1 dans le nombre)
    # Applique APRES la normalisation des unites km/kM/k0 -> km, pour etre sur
    # que le regex matche (?:km|m). On ne touche qu'aux sequences qui ressemblent
    # deja a un nombre (au moins 1 chiffre) suivi de km/m, pour ne pas casser
    # les noms de zones.
    def _fix_mixed_number(m):
        s = m.group(0)
        return s.translate(str.maketrans("oOQlI", "00011"))
    text = re.sub(r"\b[\d.oOQlI]*\d[\d.oOQlI]*(?:km|m)\b", _fix_mixed_number, text)
    # Corriger "808 , 4524km"   "808.4524km" (espace + virgule OCR)
    text = re.sub(r"(\d)\s*,\s*(\d{4}k)", r"\1.\2", text)
    # Corriger "808 . 4524km"   "808.4524km" (espace + point OCR)
    text = re.sub(r"(\d)\s*\.\s*(\d{4}k)", r"\1.\2", text)
    # Corriger "562 7800km"   "562.7800km" (espace seul)
    text = re.sub(r"(\d) (\d{4}k)", r"\1.\2", text)
    # Caracteres parasites entre chiffres (barre de lumiere SC, guillemets, degres...)
    # NE PAS inclure \s dans la classe : les espaces entre chiffres a l'interieur
    # d'un nom de zone (ex: "size04 002 int-001") ne doivent pas etre transformes
    # en points. Les cas d'espaces entre coords sont deja geres par la regle
    # "(\d+)\s+(\d{1,3})(m|km)\b" plus haut qui requiert l'unite m/km derriere.
    text = re.sub(r'(\d)["\u00b0\u00ae\u00a9|!]{1,3}(\d)', r'\1.\2', text)
    text = re.sub(r"(\d)-(\d{3,})", r"\1.\2", text)
    # Underscore parasite OCR entre 2 nombres (ex: "97 _ 72m" -> "97.72m")
    text = re.sub(r"(\d)\s*_\s*(\d)", r"\1.\2", text)
    # Supprimer les espaces dans les grands nombres (km, solar system coords)
    # IMPORTANT : on ne touche PAS aux coords en m pour eviter "1085 1.47m" -> "10851.47m"
    # Applique uniquement aux nombres qui finissent par "km" (coordonnees solar system).
    # Execute 2 fois pour gerer "9641670 6501 2km" -> "9641670.6501.2km"
    for _ in range(2):
        text = re.sub(r"(\d+)\s+(\d{3,4}(?:\.\d+)?)\s*km", r"\1.\2km", text)

    # ---- Reconstruction des points decimaux absorbes par OCR ----
    # L'overlay SC affiche les coords en m avec 2 decimales : "0.97m", "12.52m".
    # Parfois l'OCR absorbe completement le point : "0.97m" -> "97m", "7.74m" -> "774m".
    # Dans une ligne Pos: ...m, un entier sans decimale est anormal :
    #   - dans un vaisseau/interior, les coords sont < ~300m et toujours avec decimales
    #   - un entier a 2-3 chiffres (>= 10) est donc probablement une decimale perdue
    # Heuristique : on cible les entiers entre 10 et 999 immediatement suivis de 'm'
    # (pas km) dans un contexte "Pos:". On divise par 100 et on insere le point.
    # Ne pas toucher aux coords deja decimales, ni aux coords < 10 (ambigu).
    # Ne pas toucher aux km (coords planetaires qui peuvent etre de gros entiers).
    def _fix_missing_decimal(m):
        full = m.group(0)
        value = int(m.group(1))
        # Valeur de 2 chiffres (10-99) : probablement X.XX -> X.Y+ separe differemment
        # Ex: "97m" -> "0.97m"  (1 chiffre + 2 decimales coincees)
        # Valeur de 3 chiffres (100-999) : probablement Y.YY ou XY.Z
        # Ex: "774m" -> "7.74m"
        if value < 100:
            # 2 chiffres : 0.XX
            return f"0.{value:02d}m"
        else:
            # 3 chiffres : X.YY (centaine = partie entiere, reste = decimales)
            return f"{value // 100}.{value % 100:02d}m"
    # On cherche dans le texte apres un "Pos:" pour ne toucher que les coords
    # et pas d'autres nombres parasites.
    # D'abord, extraire la zone Pos:...jusqu'au prochain | ou fin de ligne
    def _fix_pos_line(pos_match):
        prefix = pos_match.group(1)
        pos_content = pos_match.group(2)
        # Dans ce contenu, chercher les "<entier>m" (sans point/virgule avant) et fix
        # On ne veut pas toucher "12.34m" ni "295km"
        fixed = re.sub(
            r"(?<![.\d])(\b\d{2,3})m\b",   # entier 2-3 chiffres suivi de m (pas km)
            _fix_missing_decimal,
            pos_content
        )
        return prefix + fixed
    # Appliquer uniquement dans une ligne "Pos:..."
    text = re.sub(
        r"([Pp][o0O]s\s*[:\s]\s*)([^|]*)",
        _fix_pos_line,
        text
    )

    return text

#                                              
#  OCR   moteurs disponibles (par priorite)
#  1. EasyOCR    meilleur sur HUD de jeux
#  2. RapidOCR   bon, leger, pure Python
#  3. Tesseract   fallback toujours present
#                                              

_easy_ocr   = None

# ======================================================================
# Parsing OCR
# ======================================================================

def parse_ocr_text(text: str) -> Optional[dict]:
    """Parse une ligne brute OCR pour en extraire zone + coords.

    Retourne {"zone": str, "x": float, "y": float, "z": float} ou None
    si aucune correspondance valide.

    Wrapper public au-dessus de _parse_coords (qui retourne un dict plus
    riche avec container_id, location, etc., utiles pour CircusVOIP).
    Pour acceder a ces infos supplementaires, appeler _parse_coords()
    directement.

    Le module utilise toutes les heuristiques de correction OCR :
    caracteres confondus (0/O/o, l/i, t/r), suffixes _NN aleatoires,
    formes tronquees au pipe |, normalisation des noms de zone vs
    whitelist de zones SC connues, memoire de signe pour corriger
    les '-' rates par EasyOCR.

    Exemples :
      'Zone: levski_v2_middeck Pos: 371 -102 -434'
        -> {'zone': 'levski_v2_middeck', 'x': 371.0, 'y': -102.0, 'z': -434.0}

      'Zone: arccorp Pos: 1.234km 5.678km 9.012km'
        -> {'zone': 'arccorp', 'x': 1234.0, 'y': 5678.0, 'z': 9012.0}

      'Zole: tevski Pos: 100 200 300'  (OCR errors corrected)
        -> {'zone': 'levski', 'x': 100.0, 'y': 200.0, 'z': 300.0}
    """
    if not text:
        return None
    try:
        result = _parse_coords(text)
    except Exception:
        return None
    if not result:
        return None
    if not isinstance(result, dict):
        return None
    # Extraire les cles minimales attendues
    if result.get("x") is None or result.get("y") is None or result.get("z") is None:
        return None
    return {
        "zone": result.get("zone", ""),
        "x": float(result.get("x", 0)),
        "y": float(result.get("y", 0)),
        "z": float(result.get("z", 0)),
    }


# ======================================================================
# Utilitaires geometriques
# ======================================================================

def distance(a: dict, b: dict) -> float:
    """Distance euclidienne 3D entre deux positions {x, y, z}, en metres.
    La cle 'zone' (si presente) est ignoree.

    Note : ne considere PAS les containers / vaisseaux. Pour CircusVOIP
    qui a une logique container-aware, utilisez votre propre fonction
    distance qui regarde container_id avant d'appeler celle-ci."""
    import math as _m
    dx = a.get("x", 0) - b.get("x", 0)
    dy = a.get("y", 0) - b.get("y", 0)
    dz = a.get("z", 0) - b.get("z", 0)
    return _m.sqrt(dx * dx + dy * dy + dz * dz)


def compute_proximity_volume(
    d: float,
    audible_range: float = AUDIBLE_RANGE_M,
    force_short: bool = False,
) -> float:
    """Calcule un volume normalise [0.0, 1.0] selon la distance.

    Mode normal (force_short=False) : zone trigger 100% jusqu'a
    RADIUS_TRIGGER, puis fondu QUADRATIQUE entre RADIUS_TRIGGER et
    audible_range. Au-dela : silence.

    Mode chuchotement (force_short=True) : 1.0 jusqu'a 5m, 0.0 au-dela
    (coupure nette). Utile pour modeliser les conversations discretes.

    Historique de la courbe :
    - v1 (linear) : (audible_range - d) / (audible_range - trigger)
      Trop douce : a 10m on etait encore a 0.80, ce qui donnait
      l'impression d'entendre les autres trop loin. Tests joueurs
      "le son ne diminue pas assez vite" (06/05/2026).
    - v2 (quadratique, actuelle) : t = (audible_range - d) / (audible_range - trigger)
      retourne t**2. Decroissance plus rapide au debut (a 10m -> 0.64,
      a 15m -> 0.36) tout en gardant une longue queue audible jusqu'a
      30m. Plus naturelle car proche de la perception humaine du volume.

    Note : contrairement a la version d'origine de CircusVOIP, cette
    fonction ne consulte pas d'etat global. Le caller est responsable
    de passer force_short=True si l'emetteur est en chuchotement.

    IMPORTANT : si on modifie cette formule, modifier aussi
    _proximity_volume dans circusvoip_mannequin.py pour que les volumes
    calcules par le mannequin et le client restent identiques."""
    if force_short:
        return 1.0 if d <= 5.0 else 0.0
    if d <= RADIUS_TRIGGER:
        return 1.0
    if d >= audible_range:
        return 0.0
    t = (audible_range - d) / (audible_range - RADIUS_TRIGGER)
    return t * t


# ======================================================================
# Reader principal
# ======================================================================


# Zone OCR courante en pixels physiques. Lue par _process_coords_img pour
# obtenir le gamma. Mise a jour par SCOCRReader.start() ou directement
# par le code utilisateur.
_zone_coords_external = None


# ======================================================================
# Pipeline OCR : capture mss + EasyOCR + Tesseract + preprocessing
# ======================================================================
# Ces fonctions composent le pipeline de capture+OCR. Elles utilisent
# mss, opencv-python (cv2), numpy. EasyOCR est lazy-loaded au 1er
# appel pour ne pas bloquer l'import du module.

import math

# Lazy imports : cv2/np/mss/easyocr ne sont pas obligatoires pour
# utiliser parse_ocr_text() seul. Ils ne sont importes qu'au 1er
# appel d'une fonction qui en a besoin.
_cv2 = None
_np = None
_mss = None
_easyocr_module = None

def _ensure_imaging():
    """Lazy-load cv2, numpy, mss au 1er besoin."""
    global _cv2, _np, _mss
    if _cv2 is None:
        import cv2 as _c
        _cv2 = _c
    if _np is None:
        import numpy as _n
        _np = _n
    if _mss is None:
        import mss as _m
        _mss = _m

# _dbg_save : sauvegarde des images du pipeline OCR pour diagnostic.
# Reste en no-op tant que enable_debug_screens() n'est pas appele
# (typiquement par le client via --debug-ocr en ligne de commande).
#
# Comportement par defaut (debug OFF) : appel = pas de cout (early return).
# Comportement debug (apres enable_debug_screens(path)) :
#   - Sauve l'image dans <path>/<HHMMSS_mmm>_<label>.png
#   - Throttling : 5s minimum entre 2 sauvegardes (toutes etiquettes
#     confondues, evite de saturer le disque a 5-6 OCR/s)
#   - Rotation FIFO : 50 fichiers max par label
#   - Sauvegarde TOUTES les etapes (raw, easy_in, tess_in, easyocr) pour
#     pouvoir analyser le preprocessing (utile pour debug du `-` perdu
#     en 1080p par ex).

import time as _dbg_time
from pathlib import Path as _DbgPath

_DEBUG_SCREENS_ENABLED = False
_DEBUG_SCREENS_DIR: "_DbgPath | None" = None
_DEBUG_SCREENS_INTERVAL = 5.0   # secondes minimum entre 2 sauvegardes
_DEBUG_SCREENS_MAX_PER_LABEL = 50
_last_dbg_save_ts = 0.0


def enable_debug_screens(directory: "str | _DbgPath" = None) -> None:
    """Active la sauvegarde des images du pipeline OCR.
    
    Args:
        directory: dossier de destination. Si None, utilise
            ./circusvoip_debug/ a cote du script. Cree le dossier
            si necessaire.
    """
    global _DEBUG_SCREENS_ENABLED, _DEBUG_SCREENS_DIR
    if directory is None:
        directory = _DbgPath(__file__).resolve().parent / "circusvoip_debug"
    else:
        directory = _DbgPath(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        print(f"[DEBUG_SCREENS] Impossible de creer {directory} : {e}")
        return
    _DEBUG_SCREENS_DIR = directory
    _DEBUG_SCREENS_ENABLED = True
    print(f"[DEBUG_SCREENS] Active. Sauvegarde dans : {directory}")


def _dbg_save(img, label, text=""):
    """Sauvegarde une image debug si _DEBUG_SCREENS_ENABLED.
    
    Throttling : une sauvegarde maximum toutes les 5s, toutes etiquettes
    confondues. Limite : 50 fichiers max par label (rotation FIFO).
    """
    global _last_dbg_save_ts
    if not _DEBUG_SCREENS_ENABLED or _DEBUG_SCREENS_DIR is None:
        return
    if img is None:
        return
    now = _dbg_time.monotonic()
    if (now - _last_dbg_save_ts) < _DEBUG_SCREENS_INTERVAL:
        return
    _last_dbg_save_ts = now
    # Rotation : garder les N derniers fichiers par label
    try:
        existing = sorted(_DEBUG_SCREENS_DIR.glob(f"*_{label}.png"))
        while len(existing) >= _DEBUG_SCREENS_MAX_PER_LABEL:
            old_file = existing.pop(0)
            try:
                old_file.unlink()
            except Exception:
                pass
    except Exception:
        pass
    ts_str = _dbg_time.strftime("%H%M%S") + f"_{int(_dbg_time.time() * 1000) % 1000:03d}"
    out_path = _DEBUG_SCREENS_DIR / f"{ts_str}_{label}.png"
    try:
        if _cv2 is None:
            return
        _cv2.imwrite(str(out_path), img)
    except Exception as e:
        # Silencieux : ne pas spammer la console si le disque est plein
        # ou autre. Le throttling 5s limite de toute facon la frequence.
        pass

# Globals utilises par le pipeline
_easy_ocr = None
_ocr_force_cpu_flag = False  # peut etre force par SCOCRReader
_minus_was_restored = False
_minus_debug_save_count = 0
# Debug interne : sauvegarde des mini-zones scannees pour la detection
# de tiret. NE FAIT RIEN tant que --debug-ocr n'est pas passe en CLI
# (la condition `_DEBUG_SCREENS_DIR is not None` est seulement vraie
# avec ce flag). En production, aucun cout meme si la valeur est > 0.
# Ce flag plafonne le nombre de captures lors d'une session debug.
# Mis a 50 par defaut : suffisant pour diagnostiquer un bug en 30s
# de test sans saturer le disque.
_MINUS_DEBUG_MAX_SAVES = 50
_NUMBER_BBOX_RX = re.compile(r'\d')

# Hook de log systeme metrics. Branche par circusvoip_core via
# set_log_system_metrics(). En mode autonome (module utilise sans le
# core), reste un no-op silencieux.
_log_system_metrics: Callable[[str], None] = lambda label='': None

def set_log_system_metrics(fn: Callable[[str], None]) -> None:
    """Branche le callback de metriques systeme. Appele par le core pour
    que les snapshots BASELINE/POST-OCR autour de l'init EasyOCR soient
    logges dans le fichier debug unifie."""
    global _log_system_metrics
    _log_system_metrics = fn

def capture_region(region):
    with _mss.mss() as sct:
        raw = sct.grab(region)
        img = _np.frombuffer(raw.bgra, dtype=_np.uint8).reshape(raw.height, raw.width, 4)
        return _cv2.cvtColor(img, _cv2.COLOR_BGRA2BGR)


# ----------------------------------------------------------------------
# Capture impossible (BitBlt: Acces refuse, etc.) : etat + backoff
# ----------------------------------------------------------------------
# Quand Windows refuse la capture d'ecran, mss leve une ScreenShotError
# ("...BitBlt...Acces refuse..."). Cause exacte non identifiee en
# production (vu en test : Skywat 10 098 erreurs en 2 min, TheMaster
# 2 814 en 1 min). Hypotheses possibles non confirmees : Fullscreen
# Exclusive, session Windows verrouillee, anti-cheat, autre logiciel de
# capture. Sans backoff, la boucle OCR retentait en continu et generait :
#   - 302 tentatives/sec mesurees en test
#   - ~10 000 lignes "[COORDS ERR] BitBlt: Acces refuse" en 2 minutes
# Cet etat module-level gere :
#   - Detection des erreurs de capture repetees (mot-cle "BitBlt" ou
#     "screenshot" dans le message d'exception)
#   - Sleep adaptatif : 100ms apres 1er echec, 500ms apres 3, 1s apres 10,
#     2s apres 30 (cap a 2s pour reprendre rapidement quand la capture
#     redevient disponible)
#   - Log dedup : un seul message "[CAPTURE INDISPONIBLE]" au debut, un
#     seul "[CAPTURE OK]" a la reprise, plus un rappel toutes les 30s
#     pendant l'incident pour ne pas perdre la trace
_capture_fail_streak = 0     # nombre d'echecs consecutifs
_capture_fail_logged = False # True quand le 1er log d'echec a ete emis
_capture_fail_t0 = 0.0       # timestamp du 1er echec de la serie
_capture_fail_last_reminder = 0.0  # timestamp du dernier rappel periodique

def _is_capture_unavailable_error(exc: Exception) -> bool:
    """Detecte les erreurs typiques de 'capture refusee' Windows :
    BitBlt (mss/gdi32), screenshot, GetDC, etc. Insensible a la casse,
    insensible aux variantes de message (FR/EN, '...refuse', '...denied')."""
    msg = str(exc).lower()
    # Mots-cle qui apparaissent dans les erreurs mss/Windows quand la
    # capture echoue pour raison d'acces : on est tolerant aux variantes
    # de wording entre versions de mss et locales Windows.
    keywords = ("bitblt", "screenshot", "getdc", "createcompatible")
    return any(k in msg for k in keywords)

def _capture_with_backoff(region):
    """Wrapper sur capture_region avec gestion des echecs Windows
    (BitBlt: Acces refuse, etc.). Sleep adaptatif et log dedup.
    Reste compatible : retourne l'image en cas de succes, leve l'exception
    en cas d'echec NON lie a la capture (pour ne pas masquer d'autres bugs)."""
    global _capture_fail_streak, _capture_fail_logged, _capture_fail_t0
    global _capture_fail_last_reminder

    try:
        img = capture_region(region)
    except Exception as e:
        if not _is_capture_unavailable_error(e):
            # Erreur differente (region invalide, mss casse, etc.) : on ne
            # silence pas, on laisse remonter pour ne pas cacher un vrai bug.
            raise

        # Erreur de capture identifiee : applique le backoff
        _capture_fail_streak += 1
        now = _dbg_time.time()
        if _capture_fail_streak == 1:
            _capture_fail_t0 = now
            _capture_fail_last_reminder = now

        # Log : 1x au 1er echec, puis rappel toutes les 30s d'incident
        if not _capture_fail_logged:
            _capture_fail_logged = True
            _logger(
                f"[CAPTURE INDISPONIBLE] {e} - backoff actif, "
                f"retentatives a cadence reduite."
            )
        elif now - _capture_fail_last_reminder >= 30.0:
            _capture_fail_last_reminder = now
            elapsed = int(now - _capture_fail_t0)
            _logger(
                f"[CAPTURE INDISPONIBLE] toujours bloque depuis {elapsed}s "
                f"({_capture_fail_streak} echecs)"
            )

        # Sleep adaptatif : monte progressivement pour ne pas bouffer le CPU
        # mais reste reactif quand l'utilisateur repasse en Borderless.
        if _capture_fail_streak < 3:
            sleep_s = 0.1
        elif _capture_fail_streak < 10:
            sleep_s = 0.5
        elif _capture_fail_streak < 30:
            sleep_s = 1.0
        else:
            sleep_s = 2.0
        _dbg_time.sleep(sleep_s)
        raise  # laisse l'appelant savoir que la capture a echoue
    else:
        # Succes : reset la machine d'etat si on etait en panne
        if _capture_fail_logged:
            now = _dbg_time.time()
            elapsed = now - _capture_fail_t0
            _logger(
                f"[CAPTURE OK] capture restauree apres {elapsed:.0f}s "
                f"({_capture_fail_streak} echecs cumules)"
            )
            _capture_fail_logged = False
            _capture_fail_t0 = 0.0
            _capture_fail_last_reminder = 0.0
        _capture_fail_streak = 0
        return img


def _get_easy_ocr():
    """Lazy-load EasyOCR (peut bloquer ~3-5s au 1er appel le temps de
    charger les modeles). Cache global : appels suivants gratuits.
    Honore _ocr_force_cpu_flag (pour tests CPU/GPU)."""
    global _easy_ocr
    if _easy_ocr is None:
        try:
            import easyocr
            force_cpu = bool(_ocr_force_cpu_flag)
            try:
                import torch
                cuda_ok = torch.cuda.is_available()
                if cuda_ok and force_cpu:
                    _logger(f"[OCR INIT] PyTorch {torch.__version__} - CUDA dispo MAIS mode CPU force")
                    cuda_ok = False
                elif cuda_ok:
                    device_name = torch.cuda.get_device_name(0)
                    _logger(f"[OCR INIT] PyTorch {torch.__version__} - CUDA OK - Device: {device_name}")
                else:
                    _logger(f"[OCR INIT] PyTorch {torch.__version__} - CUDA INDISPONIBLE - EasyOCR sera en CPU (lent !)")
            except Exception as e:
                _logger(f"[OCR INIT] Impossible de verifier PyTorch : {e}")
                cuda_ok = False
            # METRICS BASELINE : snapshot avant l'init OCR. Permet de comparer
            # avec POST-OCR pour mesurer le cout reel d'EasyOCR sur la machine
            # de l'utilisateur (utile pour debug perf chez les amis).
            # Effet de bord utile : amorce les compteurs psutil (cpu_percent
            # retourne 0 au 1er appel et amorce la fenetre de mesure). Sans
            # ce 1er appel ici, le 1er [METRICS] de la boucle stats (T+30s)
            # afficherait CPU=0% car amorcage et mesure colles.
            try:
                _log_system_metrics(label="BASELINE")
            except Exception:
                pass

            try:
                _easy_ocr = easyocr.Reader(['en'], gpu=cuda_ok, quantize=True, verbose=False)
                _logger(f"[OCR INIT] EasyOCR initialise (GPU={cuda_ok}, quantize=True)")
            except Exception as e_quant:
                _logger(f"[OCR INIT] Echec quantize ({e_quant}), fallback FP32")
                _easy_ocr = easyocr.Reader(['en'], gpu=cuda_ok, verbose=False)
                _logger(f"[OCR INIT] EasyOCR initialise (GPU={cuda_ok}, quantize=False)")

            # METRICS POST-OCR : snapshot apres init. La difference avec
            # BASELINE montre ce qu'EasyOCR consomme (CPU, RAM, VRAM) sur
            # cette machine.
            try:
                _log_system_metrics(label="POST-OCR")
            except Exception:
                pass
        except Exception as e:
            _logger(f"[OCR INIT] Echec EasyOCR : {e}")
            _easy_ocr = False
    return _easy_ocr if _easy_ocr else None

def _restore_minus_signs(img_bgr: _np.ndarray, results: list) -> list:
    """Pour chaque bounding box contenant un nombre, regarde les pixels
    juste a gauche du bord gauche pour detecter un '-' que EasyOCR aurait
    rate. Si tiret detecte, prefixe le texte avec '-'.

    EasyOCR a tendance a manquer les '-' dans les coords negatives car ce
    sont des glyphes courts et fins. On detecte visuellement leur presence
    par scan de pixels et on corrige le texte.

    Retourne une nouvelle liste avec les textes corriges. Les bboxes sont
    inchangees (on n'agrandit pas la box, juste on prefixe le texte).

    Adaptatif a la resolution : la mini-zone scannee est dimensionnee en
    fonction de la hauteur de la box (= hauteur du texte). En 4K une box
    de coord fait ~20px de haut, en 1080p ~10px. La detection s'adapte.
    """
    if not results:
        return results
    h_img, w_img = img_bgr.shape[:2]
    # Conversion en gris pour toutes les analyses, en gerant tous les formats
    # d'entree possibles. En 1080p ou apres certains preprocess, l'image
    # arrivait deja en niveaux de gris (1 canal), ce qui faisait planter
    # _cv2.cvtColor(BGR2GRAY) avec "Bad number of channels" et tuait toute la
    # detection de tiret silencieusement (l'exception etait catchee par le
    # try/except global de la boucle).
    if img_bgr.ndim == 2:
        # Deja en niveaux de gris (1 canal, shape = (H, W))
        gray = img_bgr
    elif img_bgr.ndim == 3 and img_bgr.shape[2] == 1:
        # Niveaux de gris avec dimension explicite (H, W, 1)
        gray = img_bgr[:, :, 0]
    elif img_bgr.ndim == 3 and img_bgr.shape[2] == 4:
        # BGRA (avec canal alpha) -> on enleve l'alpha
        gray = _cv2.cvtColor(img_bgr, _cv2.COLOR_BGRA2GRAY)
    elif img_bgr.ndim == 3 and img_bgr.shape[2] == 3:
        # BGR standard
        gray = _cv2.cvtColor(img_bgr, _cv2.COLOR_BGR2GRAY)
    else:
        # Format inconnu, on ne peut pas analyser
        return results

    # Pre-calcul des x_max de toutes les boxes pour pouvoir limiter la
    # mini-zone de chaque box a la fin de la box precedente. Sinon notre
    # zone de scan elargie pourrait empieter sur le nombre de gauche et
    # faussement detecter ses pixels comme un tiret.
    all_box_x_max = []
    for bbox, _t, _c in results:
        xs = [int(p[0]) for p in bbox]
        all_box_x_max.append(max(xs))

    out = []
    for box_idx, (bbox, text, conf) in enumerate(results):
        # Si le texte ne contient aucun chiffre, c'est un mot ("Zone", "Pos:",
        # nom de container...) -> jamais precede d'un tiret, on skip.
        if not _NUMBER_BBOX_RX.search(text):
            out.append((bbox, text, conf))
            continue
        # Si le texte commence deja par '-', EasyOCR l'a vu, rien a faire
        stripped = text.lstrip()
        if stripped.startswith("-"):
            out.append((bbox, text, conf))
            continue
        # Calculer le rectangle de la bbox (peut etre legerement non-orthogonal,
        # on prend les min/max des 4 coins)
        xs = [int(p[0]) for p in bbox]
        ys = [int(p[1]) for p in bbox]
        x_min, x_max = max(0, min(xs)), min(w_img, max(xs))
        y_min, y_max = max(0, min(ys)), min(h_img, max(ys))
        box_h = y_max - y_min
        if box_h < 6:
            # Box trop petite pour avoir un tiret detectable
            out.append((bbox, text, conf))
            continue
        # Mini-zone a gauche de la box, dimensionnee proportionnellement
        # a la hauteur du texte.
        # Largeur : 60% de la hauteur (un tiret SC fait environ 0.6 * hauteur
        #           de texte en largeur)
        # Hauteur : zone CENTRALE de la box (40% de la hauteur), pas toute la
        #           hauteur. Sinon le ratio de pixels clairs (tiret) devient
        #           tres faible (1-2%) car dilue par tout le fond noir au-
        #           dessus et en-dessous du tiret. En zoomant sur la bande
        #           centrale, le tiret occupe une part bien plus significative
        #           de la zone -> ratio mesurable et seuil 5% pertinent.
        # Decalage : on commence a x_min - mini_w (juste a gauche du bord gauche)
        mini_w = max(8, int(box_h * 0.6))
        mini_x_min = max(0, x_min - mini_w - 1)
        # Limiter mini_x_min pour ne pas empieter sur la bbox precedente
        # (sinon on detecterait les pixels d'un autre nombre comme un tiret).
        for prev_idx, prev_x_max in enumerate(all_box_x_max):
            if prev_idx >= box_idx:
                continue
            if prev_x_max < x_min and prev_x_max + 3 > mini_x_min:
                mini_x_min = prev_x_max + 3
        # Petit grignotage de 10% de la hauteur dans la box, pour capter
        # la fin du tiret quand EasyOCR l'a inclus dans sa bbox.
        mini_x_max = min(w_img, x_min + int(box_h * 0.10))
        if mini_x_max - mini_x_min < 4:
            out.append((bbox, text, conf))
            continue
        # Restreindre la HAUTEUR de la mini-zone a la bande centrale (40% de
        # la hauteur de box). Le tiret est toujours au milieu vertical, et
        # restreindre evite que le fond noir au-dessus et en-dessous dilue
        # le ratio de pixels clairs.
        mini_y_min = y_min + int(box_h * 0.30)
        mini_y_max = y_min + int(box_h * 0.70)
        if mini_y_max - mini_y_min < 3:
            out.append((bbox, text, conf))
            continue
        # Securite : pour les boxes en debut de ligne ("Zone:" est tout a
        # gauche, sa box pourrait avoir x_min ~ 0), on s'assure qu'il y a
        # de la place a gauche pour scanner.
        zone = gray[mini_y_min:mini_y_max, mini_x_min:mini_x_max]
        if zone.size == 0:
            out.append((bbox, text, conf))
            continue

        # Helper interne : sauvegarde de l'image debug avec le motif de
        # rejet/acceptation dans le nom de fichier. Permet de diagnostiquer
        # facilement quel critere echoue (ex: "ratio_low", "cy_off", etc).
        # Pour ne pas saturer le disque, on plafonne a _MINUS_DEBUG_MAX_SAVES.
        # Note 09/05/2026 : on a inverse la logique - avant on sauvait avant
        # analyse (= plein de cas inutiles), maintenant on sauve UNIQUEMENT
        # apres avec le verdict, ce qui rend les images directement utiles.
        def _save_debug(verdict: str, extra: str = ""):
            global _minus_debug_save_count
            if (_minus_debug_save_count < _MINUS_DEBUG_MAX_SAVES
                    and _DEBUG_SCREENS_DIR is not None):
                try:
                    _DEBUG_SCREENS_DIR.mkdir(parents=True, exist_ok=True)
                    ts = time.strftime("%H%M%S")
                    wide_crop = gray[y_min:y_max,
                                      max(0, mini_x_min - 5):min(w_img, x_max + 5)]
                    safe_text = "".join(c if c.isalnum() else "_" for c in text[:12])
                    suffix = f"_{extra}" if extra else ""
                    _cv2.imwrite(
                        str(_DEBUG_SCREENS_DIR /
                            f"{ts}_minus_{_minus_debug_save_count:02d}_"
                            f"{verdict}_{safe_text}{suffix}_zone.png"),
                        zone,
                    )
                    _cv2.imwrite(
                        str(_DEBUG_SCREENS_DIR /
                            f"{ts}_minus_{_minus_debug_save_count:02d}_"
                            f"{verdict}_{safe_text}{suffix}_wide.png"),
                        wide_crop,
                    )
                    _minus_debug_save_count += 1
                except Exception:
                    pass

        # Detection adaptative fond clair / fond sombre :
        # SC affiche le HUD coords sur fond variable (sombre dans les hangars,
        # clair sur les planetes lumineuses comme Microtech). Le tiret est
        # toujours dans la teinte opposee au fond. On regarde la luminosite
        # moyenne pour determiner le sens du contraste.
        mean_brightness = float(zone.mean())
        if mean_brightness < 128:
            # Fond sombre, texte clair : on cherche les pixels > 100
            bright = (zone > 100).astype(_np.uint8)
        else:
            # Fond clair, texte sombre : on inverse (on cherche les pixels < 155)
            bright = (zone < 155).astype(_np.uint8)
        bright_count = int(bright.sum())
        total = zone.size
        if total == 0:
            out.append((bbox, text, conf))
            continue
        ratio = bright_count / total
        # Seuils heuristiques d'un "tiret" :
        #  - Ratio >= 5% : par defaut, le tiret occupe au moins 5% de la
        #    bande centrale de la mini-zone.
        #
        # Cas observe le 10/05/2026 (1080p hangar XL) : EasyOCR place parfois
        # sa bbox du nombre TROP A GAUCHE, ce qui dilue le ratio en agrandissant
        # la mini-zone scannee. Resultat : un `-` parfaitement visible (8x4 px)
        # donne un ratio de 4.71% (28 pixels sur 594), juste sous le seuil 5%.
        # Le `-` est rate alors qu'il est clairement la dans l'image.
        #
        # Solution : si le ratio est faible mais > 1.5%, on regarde la
        # CONCENTRATION verticale des pixels clairs. Un vrai tiret a une
        # signature tres caracteristique : un bloc compact de 2-6 lignes
        # consecutives avec >80% des pixels clairs, et 0 pixel au-dessus et
        # en-dessous (le fond est noir car pas de chiffre voisin). Un parasite
        # (anti-aliasing, bord de chiffre voisin) est plus disperse.
        ratio_too_low = ratio < 0.05
        if ratio_too_low and ratio >= 0.015:
            # Tentative de rattrapage par concentration verticale
            h_proj = bright.sum(axis=1)  # nombre de pixels clairs par ligne
            # Trouver le pic max et la "bande" de lignes consecutives au-dessus
            # de 50% du pic. Un tiret produit une bande de 2-6 lignes.
            if h_proj.sum() > 0:
                peak = int(h_proj.max())
                threshold = peak * 0.5
                # Lignes "in band" : pixels clairs >= threshold
                in_band = h_proj >= threshold
                # Trouver le plus long run de lignes consecutives in_band
                max_run = 0
                cur_run = 0
                for v in in_band:
                    if v:
                        cur_run += 1
                        max_run = max(max_run, cur_run)
                    else:
                        cur_run = 0
                # Et compter les pixels concentres dans cette bande
                in_band_pixels = int(h_proj[in_band].sum())
                concentration = in_band_pixels / max(1, h_proj.sum())
                # Critere : la bande est compacte (2-6 lignes) et concentre
                # >70% des pixels clairs. Avec ces criteres, on accepte un
                # tiret meme si le ratio global est juste sous 5%.
                if 2 <= max_run <= 6 and concentration >= 0.70:
                    # Concentration suffisante : on continue les autres checks
                    ratio_too_low = False
                    # Dedup : ce log apparait sur quasi chaque frame en setup
                    # 4K + gamma 3 (ex: 145 lignes en 30s observees en test).
                    # Le fix #5 du build 32 est valide ; ce log devient du bruit.
                    # On garde 1 occurrence toutes les 30s pour preserver la
                    # tracabilite (savoir que le mecanisme tourne) sans spammer.
                    _logger_dedup(
                        "minus_low_recovery",
                        f"[MINUS LOW RATIO RECOVERY] ratio={ratio*100:.2f}% "
                        f"mais concentration={concentration*100:.0f}% sur "
                        f"{max_run} lignes -> on continue les checks",
                        value="recovery",
                        min_interval=30.0,
                    )
        if ratio_too_low:
            _save_debug("REJECT", f"ratio_low_{int(ratio*1000):03d}per1000")
            out.append((bbox, text, conf))
            continue
        if ratio > 0.50:
            _save_debug("REJECT", f"ratio_high_{int(ratio*100):02d}per100")
            out.append((bbox, text, conf))
            continue
        ys_bright, xs_bright = _np.where(bright > 0)
        if len(ys_bright) == 0:
            _save_debug("REJECT", "no_pixels")
            out.append((bbox, text, conf))
            continue
        # Centre de masse vertical : un tiret est centre verticalement
        # (au milieu du texte). Ici la zone fait 40% de la hauteur de box,
        # centree verticalement, donc le tiret theoriquement au milieu de la
        # zone. Tolerance large : 40% de la hauteur de zone (soit ~16% de la
        # hauteur de box).
        cy = float(ys_bright.mean())
        center_target = bright.shape[0] / 2.0
        if abs(cy - center_target) > bright.shape[0] * 0.40:
            _save_debug("REJECT", f"cy_off_{int(abs(cy-center_target))}px")
            out.append((bbox, text, conf))
            continue
        x_span = xs_bright.max() - xs_bright.min() + 1
        y_span = ys_bright.max() - ys_bright.min() + 1
        if y_span >= x_span:
            # Forme verticale = pas un tiret (peut-etre un I ou une barre verticale)
            _save_debug("REJECT", f"vertical_y{y_span}_x{x_span}")
            out.append((bbox, text, conf))
            continue
        if x_span < y_span * 1.5:
            # Pas assez horizontal
            _save_debug("REJECT", f"notwide_y{y_span}_x{x_span}")
            out.append((bbox, text, conf))
            continue
        # Tous les criteres passent : c'est probablement un tiret rate
        _save_debug("ACCEPT", f"y{y_span}_x{x_span}_r{int(ratio*100):02d}")
        new_text = "-" + text
        # On reduit legerement la confiance pour signaler la modification
        new_conf = conf * 0.95
        out.append((bbox, new_text, new_conf))
        # Marquer qu'on a restaure au moins un tiret dans cette lecture.
        # Le code de la boucle OCR principale lit ce flag pour bypasser
        # le filtre _is_sign_flip qui considererait la nouvelle lecture
        # comme une aberration (alors que c'est juste une correction OCR).
        global _minus_was_restored
        _minus_was_restored = True
    return out


def _easy_ocr_image(img_bgr: _np.ndarray) -> str:
    """OCR via EasyOCR   regroupe les blocs par ligne (meme Y) pour reconstruire les lignes completes."""
    reader = _get_easy_ocr()
    if reader is None:
        return ""
    try:
        # Reset le flag de tiret restaure avant chaque lecture
        global _minus_was_restored
        _minus_was_restored = False

        # Optimisation EasyOCR :
        #
        # min_size=8 : on accepte les bounding boxes a partir de 8 px de haut
        # (au lieu du default 10). Notre zone OCR fait 16-29 px de haut donc
        # les caracteres font ~10-15 px : 8 nous laisse une marge.
        #
        # NOTE : on a TENTE l'option allowlist (filtrer aux ~70 caracteres du
        # HUD SC) mais ca a casse l'OCR : 0% de parse contre 97% avant.
        # On garde donc l'OCR sans restriction de caracteres.
        results = reader.readtext(img_bgr, detail=1,
                                  paragraph=False,
                                  contrast_ths=0.1,
                                  adjust_contrast=0.5,
                                  min_size=8)
        if not results:
            return ""

        # ---- DETECTION VISUELLE DES TIRETS '-' RATES PAR EASYOCR ----
        # EasyOCR rate frequemment le '-' au debut des coordonnees negatives.
        # On compense en regardant directement les pixels juste a gauche de
        # chaque bounding box d'un nombre : un trait horizontal ~ tiret.
        # Adaptatif a la resolution : la taille de la mini-zone est calculee
        # a partir de la hauteur de la bounding box (proportionnelle a la
        # hauteur du texte).
        try:
            results = _restore_minus_signs(img_bgr, results)
        except Exception as e:
            _logger(f"[MINUS DETECT ERR] {e}")
            # Pas critique : on continue avec les resultats EasyOCR bruts
        # -------------------------------------------------------------
        # Trier par Y croissant
        results.sort(key=lambda r: r[0][0][1])

        # Grouper les blocs dont le centre Y est proche (meme ligne visuelle)
        # Tolerance : 30% de la hauteur moyenne des blocs
        avg_h = sum(abs(r[0][2][1] - r[0][0][1]) for r in results) / max(len(results), 1)
        tol   = max(avg_h * 0.5, 8)

        lines = []
        current_line = [results[0]]
        for r in results[1:]:
            cy = (r[0][0][1] + r[0][2][1]) / 2
            cy_prev = (current_line[-1][0][0][1] + current_line[-1][0][2][1]) / 2
            if abs(cy - cy_prev) <= tol:
                current_line.append(r)
            else:
                lines.append(current_line)
                current_line = [r]
        lines.append(current_line)

        # Pour chaque ligne, trier par X et joindre avec espace
        text_lines = []
        for line in lines:
            line.sort(key=lambda r: r[0][0][0])
            text_lines.append(" ".join(item[1] for item in line if item[1]))

        return "\n".join(text_lines)
    except Exception as e:
        _logger(f"[EASYOCR ERR] {e}")
        return ""

def _process_coords_img(img):
    """Traitement OCR d'une image capturee."""
    try:
        h, w = img.shape[:2]

        # (Ancien rognage 15% du haut retire - la zone ne couvre qu'1 ligne maintenant)

        # ---- Preprocessing adaptatif selon resolution SC ----
        # Pipeline 4K (gamma=0.3) : gamma -> resize x4. Rapide, marche bien
        # car il y a beaucoup de pixels par caractere.
        #
        # Pipeline 1080p/1440p (gamma=0.5) : denoise couleur -> gray -> resize x4
        # -> gamma 0.5. Le denoise lisse les artefacts (fond lumineux Microtech,
        # compression, anti-aliasing) et le gamma plus doux preserve les details
        # des caracteres fins.
        gamma_val = (_zone_coords_external or {}).get("gamma", 0.5)

        def _apply_gamma(arr, g):
            inv = 1.0 / g
            lut = _np.array([((i / 255.0) ** inv) * 255
                            for i in range(256)], dtype=_np.uint8)
            return _cv2.LUT(arr, lut)

        if gamma_val <= 0.35:
            # Pipeline 4K : gamma puis resize x4
            img_gamma = _apply_gamma(img, gamma_val)
            easy_img = _cv2.resize(
                img_gamma, (w * 4, h * 4),
                interpolation=_cv2.INTER_CUBIC
            )
            # Tesseract : gamma -> Otsu -> resize x4
            gray = _cv2.cvtColor(img_gamma, _cv2.COLOR_BGR2GRAY)
            _, otsu = _cv2.threshold(
                gray, 0, 255, _cv2.THRESH_BINARY + _cv2.THRESH_OTSU
            )
            tess_img = _cv2.resize(
                otsu, (w * 4, h * 4),
                interpolation=_cv2.INTER_CUBIC
            )
        else:
            # Pipeline 1080p/1440p : denoise -> gray -> resize x4 -> gamma
            # fastNlMeansDenoisingColored : h=10 = denoise force moyenne
            # (preserve les bords des lettres, lisse les artefacts de fond).
            denoised = _cv2.fastNlMeansDenoisingColored(img, None, 10, 10, 7, 21)
            gray = _cv2.cvtColor(denoised, _cv2.COLOR_BGR2GRAY)
            big = _cv2.resize(
                gray, (w * 4, h * 4),
                interpolation=_cv2.INTER_CUBIC
            )
            easy_img = _apply_gamma(big, gamma_val)
            # Tesseract : meme image + Otsu pour binariser
            _, tess_img = _cv2.threshold(
                easy_img, 0, 255, _cv2.THRESH_BINARY + _cv2.THRESH_OTSU
            )

        texts = []

        # Sauvegarde debug des pretraitements (_dbg_save verifie lui-meme
        # DEBUG_SCREENS, donc appels inconditionnels ici : c'est le toggle
        # du bouton Debug dans l'UI qui controle l'enregistrement effectif).
        _dbg_save(img,       "coords_raw",      "image brute capturee")
        _dbg_save(easy_img,  "coords_easy_in",  f"EasyOCR input (gamma={gamma_val} x4)")
        _dbg_save(tess_img,  "coords_tess_in",  f"Tesseract input (gamma={gamma_val}+Otsu x4)")

        # EasyOCR en priorite avec early-exit si la 1ere passe suffit
        primary_success = False
        easy_texts = []
        easy = _get_easy_ocr()
        if easy:
            # Passe 1 : image complete preprocessee (gamma + x4)
            t0 = _easy_ocr_image(easy_img)
            _dbg_save(easy_img, "coords_easyocr", t0)
            if t0.strip():
                easy_texts.append(t0)
                texts.append(t0)
                # Test immediat : si passe 1 donne deja un bon resultat, on s'arrete
                _r_test = _parse_coords(_normalize_numbers(t0))
                if _r_test and _r_test.get("x") is not None and _r_test.get("container_id"):
                    primary_success = True

            # Passes de secours uniquement si la 1ere n'a pas suffi
            # (decoupe en 2 crops milieu de l'image pour economiser du temps)
            if not primary_success:
                eh, ew = easy_img.shape[:2]
                n_lines = 2
                line_h  = max(1, eh // n_lines)
                for i in range(n_lines):
                    y1 = i * line_h
                    y2 = min(eh, y1 + line_h)
                    crop = easy_img[y1:y2, :]
                    if crop.shape[0] >= 5:
                        t = _easy_ocr_image(crop)
                        if t.strip():
                            easy_texts.append(t)
                            texts.append(t)

            # DEBUG : dump du texte EasyOCR combine (dedupe sur contenu)
            if easy_texts:
                combined_preview = " | ".join(t.replace("\n", " ") for t in easy_texts)[:300]
                # Detecter les textes typiques des ecrans d'attente / menus SC
                # qui produisent un OCR variable mais sans interet :
                #  1. Loading screens / chargement : contiennent "FPS" + ms
                #     (compteur de perf affiche pendant les chargements et
                #     les ecrans de transition)
                #  2. Menus/spawn : zone "Solarsystem" avec coords ~0,0,0
                #     (le joueur n'a pas encore spawn dans le jeu)
                # Dans ces 2 cas, on groupe sous une cle unique pour avoir 1
                # log par minute peu importe les micro-variations OCR.
                lower_preview = combined_preview.lower()
                # Detection FPS : accepte les variantes OCR "fps 58,3", "fps 58 ,2"
                # mais on filtre sur la presence de "fps" suivi de chiffres dans
                # les ~10 caracteres suivants (evite faux positifs).
                is_loading = False
                if "fps" in lower_preview:
                    fps_idx = lower_preview.find("fps")
                    # Apres "fps" il doit y avoir des chiffres rapidement
                    after_fps = lower_preview[fps_idx + 3:fps_idx + 13]
                    if any(c.isdigit() for c in after_fps):
                        is_loading = True
                is_menu_spawn = (
                    "solarsystem" in lower_preview and
                    ("0,00m" in lower_preview or "0 , 00m" in lower_preview
                     or "0.00m" in lower_preview)
                )
                if is_loading or is_menu_spawn:
                    _logger_dedup(
                        "easyocr_text_idle",
                        f"[EASYOCR TEXT] {combined_preview}",
                        value="idle_screen",
                        min_interval=60.0,
                    )
                else:
                    _logger_dedup(
                        "easyocr_text",
                        f"[EASYOCR TEXT] {combined_preview}",
                        value=combined_preview,
                        min_interval=30.0,
                    )

            # Si passe 1 n'a pas suffi, tester la concatenation de toutes les passes
            if not primary_success and easy_texts:
                combined = "\n".join(easy_texts)
                _r_test = _parse_coords(_normalize_numbers(combined))
                if _r_test and _r_test.get("x") is not None and _r_test.get("container_id"):
                    primary_success = True
                    texts.append(combined)

            if primary_success:
                _logger_dedup(
                    "ocr_engine",
                    f"[OCR] EasyOCR ({len(easy_texts)} passes) - Tesseract ignore",
                    value="easy",
                    min_interval=60.0,
                )
            elif easy_texts:
                _logger_dedup(
                    "easyocr_parse_fail",
                    "[EASYOCR PARSE FAIL] parse sur toutes passes echoue",
                    value="fail",
                    min_interval=30.0,
                )

        # Tesseract   uniquement si le moteur principal n'a pas trouve
        if not primary_success:
            _logger_dedup(
                "ocr_engine",
                "[OCR] Fallback Tesseract (EasyOCR n'a pas trouve)",
                value="tesseract",
                min_interval=60.0,
            )
            try:
                t = pytesseract.image_to_string(tess_img, config="--psm 6 --oem 1")
                if t.strip():
                    texts.append(t)
            except Exception:
                pass

        # Collecter tous les resultats valides de tous les moteurs OCR
        all_results = []
        for raw_text in texts:
            text = _normalize_numbers(raw_text)
            r    = _parse_coords(text)
            # Accepter si on a des coords valides ET un container_id
            # (nouvelle logique : container_id remplace le filtrage par surface)
            if r and r.get("x") is not None and r.get("container_id"):
                x_abs = abs(r["x"])
                y_abs = abs(r.get("y", 0))
                z_abs = abs(r.get("z", 0))
                mag_m = math.sqrt(x_abs**2 + y_abs**2 + z_abs**2)

                # Filtre : coords aberrantes (plus de 1 million en absolu = bug OCR)
                # EXCEPTION : Solarsystem a des coords en milliards de metres
                # (position dans le systeme solaire, x en milliards, y idem, z proche de 0)
                # EasyOCR lit parfois "Solarsvstem" (y -> v) : on tolere les 2.
                zone_name = (r.get("zone") or "").lower()
                is_solarsystem = ("solarsystem"  in zone_name or
                                  "solar_system" in zone_name or
                                  "solarsvstem"  in zone_name or   # OCR : y -> v
                                  "solar_svstem" in zone_name)
                if not is_solarsystem and (x_abs > 1_000_000 or y_abs > 1_000_000 or z_abs > 1_000_000):
                    _logger_dedup(
                        "reject_huge",
                        f"[REJECT] coords aberrantes x={x_abs:.0f} y={y_abs:.0f} z={z_abs:.0f}",
                        value=f"{int(x_abs)},{int(y_abs)},{int(z_abs)}",
                        min_interval=10.0,
                    )
                    continue
                all_results.append((mag_m / 1000, r))

        if not all_results:
            return None

        # Si on a une derniere position connue, garder le resultat le plus proche
        # Sinon prendre celui dont la magnitude est la plus frequente (vote majoritaire)
        if len(all_results) == 1:
            best = all_results[0][1]
        else:
            from collections import Counter
            # Arrondir les magnitudes a 50km pres pour regrouper les lectures coherentes
            mag_groups = Counter(round(mag / 50) for mag, _ in all_results)
            dominant_mag = mag_groups.most_common(1)[0][0]
            coherent = [(mag, r) for mag, r in all_results
                        if round(mag / 50) == dominant_mag]
            # Parmi les coherents, preferer celui avec une zone nommee
            with_zone = [(mag, r) for mag, r in coherent if r.get("zone")]
            pool = with_zone if with_zone else coherent
            best = pool[0][1]

        if best:
            # Log uniquement quand la position change de bucket 50m, quand
            # la zone change, OU quand le container_id change. Bucket suffit
            # a dedup sans bruit :
            #   - immobile (cockpit, station) : oscillations OCR sub-50m
            #     restent dans le meme bucket -> 1 log au lieu de spam
            #   - mouvement reel : ~1 log par tranche de 50m parcourus
            #   - frame OCR parasite (signe flippe sur x ou z, parser foireux
            #     sur unite km/m...) : bucket completement different
            #     -> log immediat, ce qu'on veut pour diagnostic
            #   - container_id change (ex: OCR confond 3 et 8 sur 1 chiffre,
            #     vs vrai cid different) : log immediat aussi pour pouvoir
            #     diagnostiquer les coupures audio dues a la derive cid
            # Le min_interval est mis tres haut (effectivement infini) pour
            # ne pas re-logger la meme position quand on est immobile, sans
            # casser l'API de _logger_dedup qui exige une valeur numerique.
            # Avant fix : min_interval=30s -> les parasites en cockpit etaient
            # masquees pendant 30s, on n'avait pas le ratio reel de bugs OCR.
            #
            # Note 10/05/2026 : on inclut le container_id COMPLET dans la cle
            # de dedup et dans le log. Pas un suffixe : un changement d'un
            # seul chiffre du cid (ex: 852561216340 vs 852561216840) peut
            # casser la proximity audio, donc tracable au caractere pres.
            x = int(best['x'] / 50) * 50
            y = int(best.get('y', 0) / 50) * 50
            z = int(best.get('z', 0) / 50) * 50
            zn = best.get('zone')
            cid = best.get('container_id') or "?"
            coord_key = f"{zn}_{cid}_{x}_{y}_{z}"
            _logger_dedup(
                "coords_ok",
                f"[COORDS OK] x={best['x']:.0f}m y={best['y']:.0f}m "
                f"z={best.get('z',0):.0f}m zone={zn} cid={cid}",
                value=coord_key,
                min_interval=999999.0,  # effectivement infini
            )
        return best

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        _logger(f"[COORDS ERR] {e}")
        if not hasattr(_process_coords_img, "_logged_tb"):
            _process_coords_img._logged_tb = set()
        if str(e) not in _process_coords_img._logged_tb:
            _process_coords_img._logged_tb.add(str(e))
            _logger(f"[COORDS ERR TRACE]\n{tb}")
        return None



#                                              

# Altitude max consideree comme "a la surface / en train de miner" (metres)
ALTITUDE_MAX_MINING = 10_000.0



_INTERIOR_KEYWORDS = [
    "hangar", "reststop", "station", "ship", "interior",
    "solarsystem", "solar_system", "spaceport", "platform",
]


# Completez au fur et a mesure des tests
_ZONE_MAP = {
    #    Stanton                               
    "stanton":    ("Stanton", "",          ""),

    # Hurston   aliases numeriques SC
    "hurston":    ("Stanton", "Hurston",   "Hurston"),
    "stanton1":   ("Stanton", "Hurston",   "Hurston"),
    "arial":      ("Stanton", "Hurston",   "Arial"),
    "stanton1a":  ("Stanton", "Hurston",   "Arial"),
    "aberdeen":   ("Stanton", "Hurston",   "Aberdeen"),
    "stanton1b":  ("Stanton", "Hurston",   "Aberdeen"),
    "magda":      ("Stanton", "Hurston",   "Magda"),
    "stanton1c":  ("Stanton", "Hurston",   "Magda"),
    "ita":        ("Stanton", "Hurston",   "Ita"),
    "stanton1d":  ("Stanton", "Hurston",   "Ita"),
    # Crusader   aliases numeriques SC
    "crusader":   ("Stanton", "Crusader",  "Crusader"),
    "stanton2":   ("Stanton", "Crusader",  "Crusader"),
    "cellin":     ("Stanton", "Crusader",  "Cellin"),
    "stanton2a":  ("Stanton", "Crusader",  "Cellin"),
    "daymar":     ("Stanton", "Crusader",  "Daymar"),
    "stanton2b":  ("Stanton", "Crusader",  "Daymar"),
    "yela":       ("Stanton", "Crusader",  "Yela"),
    "stanton2c":  ("Stanton", "Crusader",  "Yela"),
    # ArcCorp   aliases numeriques SC
    "arccorp":    ("Stanton", "ArcCorp",   "ArcCorp"),
    "stanton3":   ("Stanton", "ArcCorp",   "ArcCorp"),
    "lyria":      ("Stanton", "ArcCorp",   "Lyria"),
    "stanton3a":  ("Stanton", "ArcCorp",   "Lyria"),
    "wala":       ("Stanton", "ArcCorp",   "Wala"),
    "stanton3b":  ("Stanton", "ArcCorp",   "Wala"),
    # MicroTech   aliases numeriques SC (stanton4, stanton4a, etc.)
    "microtech":  ("Stanton", "MicroTech", "MicroTech"),
    "stanton4":   ("Stanton", "MicroTech", "MicroTech"),
    "calliope":   ("Stanton", "MicroTech", "Calliope"),
    "stanton4a":  ("Stanton", "MicroTech", "Calliope"),
    "clio":       ("Stanton", "MicroTech", "Clio"),
    "stanton4b":  ("Stanton", "MicroTech", "Clio"),
    "euterpe":    ("Stanton", "MicroTech", "Euterpe"),
    "stanton4c":  ("Stanton", "MicroTech", "Euterpe"),
    #    Pyro                                  
    # Noms techniques SC (debug overlay Zone:) :
    # pyro1 = Pyro I        pyro2 = Monox (planete)
    # pyro3 = Bloom (planete) pyro4 = Pyro IV
    # pyro5 = Pyro V (gaz)  pyro6 = Terminus (planete)
    # pyro5a=Ignis  pyro5b=Vatra  pyro5c=Adir
    # pyro5d=Fairo  pyro5e=Fuego  pyro5f=Vuur
    "pyro":       ("Pyro",    "",          ""),
    "pyro1":      ("Pyro",    "Pyro I",    "Pyro I"),
    "pyro2":      ("Pyro",    "Pyro II",   "Monox"),
    "pyro3":      ("Pyro",    "Pyro III",  "Bloom"),
    "pyro4":      ("Pyro",    "Pyro IV",   "Pyro IV"),
    "pyro5":      ("Pyro",    "Pyro V",    "Pyro V"),
    "pyro6":      ("Pyro",    "Pyro VI",   "Terminus"),
    # Lunes Pyro V
    "pyro5a":     ("Pyro",    "Pyro V",    "Ignis"),
    "pyro5b":     ("Pyro",    "Pyro V",    "Vatra"),
    "pyro5c":     ("Pyro",    "Pyro V",    "Adir"),
    "pyro5d":     ("Pyro",    "Pyro V",    "Fairo"),
    "pyro5e":     ("Pyro",    "Pyro V",    "Fuego"),
    "pyro5f":     ("Pyro",    "Pyro V",    "Vuur"),
    # Alias noms litteraux (retrocompatibilite)
    "monox":      ("Pyro",    "Pyro II",   "Monox"),
    "bloom":      ("Pyro",    "Pyro III",  "Bloom"),
    "terminus":   ("Pyro",    "Pyro VI",   "Terminus"),
    "ignis":      ("Pyro",    "Pyro V",    "Ignis"),
    "vatra":      ("Pyro",    "Pyro V",    "Vatra"),
    "adir":       ("Pyro",    "Pyro V",    "Adir"),
    "fairo":      ("Pyro",    "Pyro V",    "Fairo"),
    "fuego":      ("Pyro",    "Pyro V",    "Fuego"),
    "vuur":       ("Pyro",    "Pyro V",    "Vuur"),
    #    Nyx                                   
    "nyx":        ("Nyx",     "",          ""),
    "delamar":    ("Nyx",     "Delamar",   "Delamar"),
}

# Pattern principal : Zone: NOM Pos: Xkm Ykm Zkm
# On exclut les noms contenant uniquement des chiffres (ship IDs)
# et le mot "Root"

def read_coords(region):
    """Capture la zone de l'ecran et lance le pipeline OCR."""
    """Retourne le dict avec zone/x/y/z/etc., ou None."""
    _ensure_imaging()
    try:
        img = _capture_with_backoff(region)
        return _process_coords_img(img)
    except Exception as e:
        # Si c'est une erreur de capture deja loggee par _capture_with_backoff
        # (BitBlt, etc.), on ne re-logge pas en [COORDS ERR] : ce serait du
        # bruit redondant. On retourne juste None silencieusement.
        if _is_capture_unavailable_error(e):
            return None
        _logger(f'[COORDS ERR] {e}')
        return None

class SCOCRReader:
    """Lit en continu le HUD de Star Citizen et appelle un callback a
    chaque position lue avec succes.

    Tourne dans un thread daemon. Initialise EasyOCR au .start() pour
    ne pas bloquer l'instanciation. Si le GPU n'est pas dispo, fallback
    automatique sur CPU (avec un log d'avertissement).

    Parametres
    ----------
    on_position : callable(dict) -> None
        Appele a chaque position valide. Le dict contient zone, x, y, z.
        Peut etre appele depuis un thread non-Qt/non-Tk : si l'application
        utilise une UI, le callback doit faire le marshalling lui-meme.
    monitor : dict | None
        Moniteur a capturer (format mss). None = ecran principal (auto).
    force_cpu : bool
        True force EasyOCR en mode CPU (utile si GPU sature). False = auto
        (GPU si dispo).
    freq_hz : int
        Frequence cible des lectures (defaut 10). La frequence reelle peut
        etre plus basse si l'OCR met plus de temps qu'un frame.

    Methodes
    --------
    start() : demarre le thread (initialise EasyOCR au passage). Bloque
              jusqu'a la fin de l'init OCR (~3-5s).
    stop()  : signale l'arret du thread. Le thread se termine apres le
              prochain cycle.
    set_zone(zone_dict) : change la zone de capture en cours d'execution.
                          zone_dict = {"left", "top", "width", "height"}.
    set_force_cpu(bool) : force CPU (necessite stop()/start() pour relancer
                          EasyOCR avec le nouveau mode).
    """

    def __init__(
        self,
        on_position: Callable[[dict], None],
        monitor: Optional[dict] = None,
        zone: Optional[dict] = None,
        force_cpu: bool = False,
        freq_hz: int = DEFAULT_FREQ_HZ,
    ) -> None:
        self._on_position = on_position
        self._monitor = monitor
        self._force_cpu = bool(force_cpu)
        self._freq_hz = max(1, int(freq_hz))
        # Etat interne
        self._zone: Optional[dict] = dict(zone) if zone else None
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._easyocr_reader = None        # cree a start()
        self._tess_available: bool = False # detecte a start()
        # Pour la validation des sauts impossibles
        self._last_pos: Optional[dict] = None
        self._last_ts: float = 0.0

    def start(self) -> None:
        """Demarre le thread OCR. Initialise EasyOCR au 1er appel
        (peut bloquer ~3-5s le temps de charger les modeles).

        Le module fournit son propre pipeline OCR autonome (capture mss
        + EasyOCR + correction des signes -) qui ne depend pas du module
        circusvoip_client. Utilisable depuis n'importe quel projet."""
        if self.is_running():
            return
        self._stop_evt.clear()

        # Initialiser cv2/numpy/mss
        try:
            _ensure_imaging()
        except Exception as e:
            raise RuntimeError(
                f"Impossible d'initialiser cv2/numpy/mss : {e}\n"
                "Installer : pip install opencv-python numpy mss easyocr"
            )

        # Resoudre la zone de capture
        if self._zone is None:
            self._zone = auto_ocr_zone(self._monitor)

        # Communiquer au pipeline OCR la zone courante (pour gamma) et le
        # flag force CPU (lu par _get_easy_ocr au lazy init)
        global _zone_coords_external, _ocr_force_cpu_flag
        _zone_coords_external = dict(self._zone)
        _ocr_force_cpu_flag = bool(self._force_cpu)

        def _loop():
            interval = 1.0 / self._freq_hz
            while not self._stop_evt.is_set():
                t0 = time.time()
                try:
                    pos = read_coords(self._zone)
                    if pos and isinstance(pos, dict):
                        if pos.get("x") is not None and pos.get("y") is not None and pos.get("z") is not None:
                            public_pos = {
                                "zone": pos.get("zone", ""),
                                "x": float(pos.get("x", 0)),
                                "y": float(pos.get("y", 0)),
                                "z": float(pos.get("z", 0)),
                            }
                            try:
                                self._on_position(public_pos)
                            except Exception as e:
                                _logger(f"on_position callback error: {e}")
                except Exception as e:
                    _logger(f"OCR loop iteration error: {e}")
                # Compense le temps OCR pour respecter la frequence cible
                elapsed = time.time() - t0
                sleep_for = max(0.001, interval - elapsed)
                self._stop_evt.wait(sleep_for)

        self._thread = threading.Thread(
            target=_loop, name="SCOCRReader", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Signale l'arret au thread. Ne bloque pas."""
        self._stop_evt.set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def set_zone(self, zone: dict) -> None:
        """Change la zone de capture a chaud. Le prochain frame utilise la
        nouvelle zone. zone = {"left", "top", "width", "height", "gamma"}
        en pixels physiques."""
        if not isinstance(zone, dict):
            raise TypeError("zone doit etre un dict mss-compatible")
        self._zone = dict(zone)
        # Mettre a jour aussi le global lu par le pipeline pour le gamma
        global _zone_coords_external
        _zone_coords_external = dict(zone)

    def set_force_cpu(self, force: bool) -> None:
        """Active/desactive le mode force CPU. Necessite stop() puis start()
        pour reinitialiser EasyOCR avec le nouveau mode."""
        self._force_cpu = bool(force)


# ======================================================================
# Note sur les coordonnees physiques vs logiques
# ======================================================================
#
# mss travaille en pixels PHYSIQUES (resolution reelle de l'ecran).
# Sur Windows avec scaling DPI (par exemple 150% sur un 4K), les pixels
# logiques utilises par les frameworks UI (Qt logical pixels, Tk units)
# ne correspondent pas aux pixels physiques.
#
# Toutes les coordonnees ecran exposees ou attendues par ce module
# (auto_ocr_zone, list_monitors, set_zone) sont en PIXELS PHYSIQUES.
#
# Si vous integrez ce module avec Qt :
#     phys = round(logical * QScreen.devicePixelRatio())
#     logical = phys / QScreen.devicePixelRatio()
#
# Cote application, il faut etre PER_MONITOR_AWARE_V2 pour que mss
# voie correctement les pixels physiques sur tous les ecrans.
