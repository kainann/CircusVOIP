# -*- coding: utf-8 -*-
"""
circusvoip_phone_wallet
=======================

Application « Portefeuille » du CircusPhone (v0.3), au contrat `PhoneApp`.

Portage du prototype `circusphone_wallet_test.py` vers une vraie app
intégrable dans l'overlay : on GARDE le parser validé sur Game.log réel
(automate multi-lignes, dédup sur « Added notification », split net/taxe,
achats/ventes boutique) et la carte `TransferRow`, on STRIPPE le châssis
(peint par l'overlay), et on ajoute deux choses propres à la v0.3 :

  1. PERSISTANCE JSON : SC réécrit Game.log à chaque lancement, donc le log
     ne contient que la session courante. On accumule les opérations dans
     `circusphone_wallet.json` pour garder la mémoire entre sessions, avec
     déduplication (clé ts+type+montant) pour ne jamais compter deux fois.

  2. BRANCHEMENT sur le tail existant via `services.gamelog` : en intégré,
     on s'abonne aux lignes du thread `c2-gamelog-tail-smart` déjà en place
     plutôt que d'ouvrir un second tail. En autonome (services.gamelog
     absent), on retombe sur notre propre tail (repli pour le harnais).

Contrat attendu de `services.gamelog` (côté overlay/client.py) :
    .subscribe(callback)    callback(line: str) appelé pour chaque ligne
    .unsubscribe(callback)
Optionnel : .current_path() -> str  (chemin du Game.log actif). Si absent,
on retombe sur find_gamelog() pour le rattrapage one-shot de la session.

Le bloc __main__ est un HARNAIS DE TEST VISUEL supprimable : il monte la
WalletApp dans un châssis CircusPhone (services.gamelog=None -> repli tail
autonome) pour l'œilleter avant intégration.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, QThread, Signal, QTimer
from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QVBoxLayout, QHBoxLayout, QPushButton,
    QScrollArea, QFileDialog, QFrame, QMenu,
)

from circusvoip_phone_apps import PhoneApp, PhoneServices


# ======================================================================
#  Palette (sous-ensemble repris du CircusPhone, pour coherence)
# ======================================================================
PHONE_SCREEN_BG  = "#ffffff"   # fond de l'ecran
PHONE_NAME_DARK  = "#1a1a1a"   # texte sombre lisible
PHONE_NAME_GREY  = "#9aa0a6"   # texte secondaire gris
PHONE_ACCENT     = "#2f6fed"   # accent (selection de menu)
COLOR_SENT       = "#f85149"   # montant sortant (rouge)
COLOR_RECV       = "#3fb950"   # montant entrant (vert)
ROW_FLASH_BG     = "#fff3cd"   # surbrillance breve d'une nouvelle ligne


# ======================================================================
#  Resolution du chemin Game.log (repli autonome / rattrapage one-shot)
# ======================================================================
def _common_gamelog_paths() -> list:
    """Quelques chemins habituels d'installation SC (Windows). Liste
    courte volontaire."""
    paths = []
    drives = ["C:", "D:", "E:", "F:", "G:", "H:"]
    roots = [
        r"\Program Files\Roberts Space Industries\StarCitizen",
        r"\Roberts Space Industries\StarCitizen",
        r"\Star Citizen\StarCitizen",
        r"\Games\StarCitizen",
        r"\StarCitizen",
    ]
    channels = ["LIVE", "PTU", "EPTU", "TECH-PREVIEW"]
    for d in drives:
        for r in roots:
            for c in channels:
                paths.append(f"{d}{r}\\{c}\\Game.log")
    return paths


def find_gamelog(saved_path: Optional[str] = None) -> Optional[str]:
    """1) chemin sauve, 2) chemins habituels. psutil n'est pas requis ici :
    en intégré, le chemin vient du tail existant ; ce helper sert surtout
    au repli autonome et au rattrapage one-shot."""
    if saved_path and os.path.exists(saved_path):
        return saved_path
    for p in _common_gamelog_paths():
        if os.path.exists(p):
            return p
    return None


# ======================================================================
#  Parser de transferts (VALIDE sur Game.log reel — repris a l'identique)
# ======================================================================
_TS_RE = re.compile(r"<(?P<ts>[^>]+)>")
_AMOUNT_RE = re.compile(r"(?P<amt>[\d.,]+)\s*aUEC")
# On declenche UNIQUEMENT sur "Added notification" (le 1er event de la
# notif), jamais sur les "UpdateNotificationItem" qui repetent le texte.
_TRIGGER_SENT = 'Added notification "You sent'
# Reception : la CONFIRMATION reelle est '<nom> has sent you:' (verifie sur
# log 04/07/2026). On IGNORE la simple DEMANDE '<nom> wants to send you' /
# 'Currency Transfer Request' (pas encore de credit).
_TRIGGER_RECV = "has sent you"
# Noms de joueur dans les notifs de transfert (montant souvent sur la ligne
# suivante -> le nom est capture ici, sur la ligne "Added notification").
_PEER_SENT_RE = re.compile(r'You sent (?P<peer>.+?):')
_PEER_RECV_RE = re.compile(r'"(?P<peer>[^"]+?) has sent you')
# Achats PNJ : "SShopBuyRequest" couvre SendShopBuyRequest et
# SendStandardItemBuyRequest. Ventes : "SellRequest".
_TRIGGER_BUY = "SShopBuyRequest"
_TRIGGER_SELL = "SellRequest"
# Commodities (cargo) : achat/vente au kiosque marchandises. Le message est
# "SShopCommodityBuyRequest" / "...CommoditySellRequest" — NB : ne contient
# PAS "SShopBuyRequest", d'ou un trigger dedie. Particularites : montant
# TOTAL dans price[...] (pas client_price), quantite en centi-SCU avec
# unite dans les crochets, et resourceGUID au lieu d'un itemName (nom non
# resolvable sans table -> libelle generique). La VENTE est extrapolee
# (token a confirmer sur un vrai log de vente commodity).
_TRIGGER_COMMODITY_BUY = "CommodityBuyRequest"
_TRIGGER_COMMODITY_SELL = "CommoditySellRequest"
# Achat en VITRINE (quick buy) : le jeu n'emet PAS de message financier, juste
# un <AttachmentReceived> (l'objet arrive dans les mains). Or ce meme event
# survient aussi quand on prend un objet depuis l'INVENTAIRE. On distingue :
#   - inventaire OUVERT (entre 'list_CategoryItemList' et 'Close Inventory
#     Grid') -> manip d'inventaire, ignoree ;
#   - un 'OnDragInventoryItemModifyTarget' au meme instant -> glisser-deposer
#     d'inventaire, ignore aussi (garde-fou) ;
#   - sinon -> achat vitrine, ligne SANS prix (le montant n'existe pas).
# Location de vehicule/vaisseau : 'SendRentalRequest' porte prix/nom/duree,
# validee seulement par le 'RmShopFlowResponse ... result[Success]' qui suit.
_TRIGGER_RENTAL   = "SendRentalRequest"
_TRIGGER_SHOP_RESP = "RmShopFlowResponse"
# offering[N] = duree de location.
_RENTAL_DURATIONS = {"0": "1 jour", "1": "3 jours", "2": "7 jours"}
# Prefixe constructeur du itemName -> marque lisible (a etoffer au besoin).
_BRAND_PREFIXES = {
    "DRAK": "Drake", "GRIN": "Greycat", "AEGS": "Aegis", "ANVL": "Anvil",
    "ORIG": "Origin", "RSI": "RSI", "CNOU": "Crusader", "MISC": "MISC",
    "CRUS": "Crusader", "ARGO": "Argo", "BANU": "Banu", "ESPR": "Esperia",
    "GAMA": "Gatac", "KRIG": "Kruger", "MRAI": "Mirai", "TMBL": "Tumbril",
    "VNCL": "Vanduul", "XIAN": "Xi'an", "XNAA": "Xi'an",
}
_TRIGGER_INV_OPEN  = "list_CategoryItemList"
_TRIGGER_INV_CLOSE = "Close Inventory Grid"
_TRIGGER_DRAG      = "OnDragInventoryItemModifyTarget"
_TRIGGER_ATTACH    = "AttachmentReceived"
# 2e champ de Attachment[full_id, base_name, guid] = nom de base de l'objet.
_ATTACH_RE = re.compile(r"Attachment\[[^,]+,\s*(?P<item>[^,\]]+)")

# Frais de service Wallet observe : 0,5 %, arrondi a l'entier superieur.
SERVICE_FEE_RATE = 0.005


def _logfield(name: str, line: str) -> Optional[str]:
    """Extrait la valeur d'un champ 'name[valeur]' d'une ligne de log."""
    m = re.search(re.escape(name) + r"\[(?P<v>[^\]]*)\]", line)
    return m.group("v") if m else None


def _pretty_shop(s: Optional[str]) -> str:
    """'SCShop_H_Pharmacy_Levski' -> 'H Pharmacy Levski'."""
    s = (s or "")
    if s.startswith("SCShop_"):
        s = s[len("SCShop_"):]
    return s.replace("_", " ").strip()


def _pretty_item(s: Optional[str]) -> str:
    """'crlf_consumable_adrenaline_01' -> 'crlf consumable adrenaline 01'."""
    return (s or "").replace("_", " ").strip()


def _parse_amount(raw: str) -> Optional[int]:
    """'1,234' / '1 234' / '101' -> int. None si non parsable."""
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def _fmt_amount(n: int) -> str:
    """101 -> '101', 1234 -> '1 234'. Separateur = espace insecable normal
    (\\u00a0), plus lisible que l'espace fine sur les gros montants."""
    return f"{n:,}".replace(",", "\u00a0")


def _abbrev_k(n: int) -> str:
    """Montant compact pour le RESUME (colonnes etroites) : abrege des
    10 000 (k) et 1 000 000 (M). Ex : 142300 -> '142k', 98450 -> '98,5k',
    1080000 -> '1,08M'. En dessous de 10 000 : nombre complet."""
    a = abs(int(n))
    if a >= 1_000_000:
        s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return s.replace(".", ",") + "M"
    if a >= 100_000:
        return f"{round(n / 1000)}k"
    if a >= 10_000:
        s = f"{n / 1000:.1f}".rstrip("0").rstrip(".")
        return s.replace(".", ",") + "k"
    return _fmt_amount(n)


def _abbrev_list(n: int) -> str:
    """Montant pour la LISTE : complet, abrege seulement au-dela de 100 000
    (k) et 1 000 000 (M) pour ne pas casser l'alignement des grosses ventes."""
    a = abs(int(n))
    if a >= 1_000_000:
        s = f"{n / 1_000_000:.2f}".rstrip("0").rstrip(".")
        return s.replace(".", ",") + "M"
    if a >= 100_000:
        return f"{round(n / 1000)}k"
    return _fmt_amount(n)


def _split_transfer(total: int) -> tuple:
    """Le Game.log journalise le COUT TOTAL debite (net + taxe), pas le
    montant envoye. On retrouve (net, taxe) : total = net + ceil(net*0.5%)
    est strictement croissant en net, donc le net est unique."""
    if total <= 0:
        return total, 0
    est = round(total / (1.0 + SERVICE_FEE_RATE))
    for net in range(max(1, est - 5), est + 6):
        fee = math.ceil(net * SERVICE_FEE_RATE)
        if net + fee == total:
            return net, fee
    net = round(total / (1.0 + SERVICE_FEE_RATE))
    return net, total - net


# ======================================================================
#  Automate de detection : pur, sans Qt. feed(line) -> liste d'operations.
# ======================================================================
class TransferParser:
    """Detecteur stateful d'operations a partir des lignes du Game.log.
    Decouple de la SOURCE : on lui pousse des lignes (qu'elles viennent du
    tail existant ou d'un tail autonome), il renvoie les operations
    detectees. C'est l'ancien `feed()` du proto extrait en classe.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        """Reinitialise l'automate (ex : troncature du Game.log)."""
        self._awaiting = None
        self._pending_ts = None
        self._pending_peer = None
        self._wait_count = 0
        self._inv_open = False       # inventaire ouvert ? (achats vitrine)
        self._last_drag_ts = None    # ts du dernier drag d'inventaire
        self._pending_rental = None  # location en attente de result[Success]

    def feed(self, line: str) -> list:
        """Consomme une ligne, renvoie 0 ou 1 operation (dict)."""
        out = []
        tsm = _TS_RE.search(line)
        ts = tsm.group("ts") if tsm else None

        # Suivi de l'etat inventaire (pour isoler les achats vitrine).
        if _TRIGGER_INV_OPEN in line:
            self._inv_open = True
            return out
        if _TRIGGER_INV_CLOSE in line:
            self._inv_open = False
            return out
        if _TRIGGER_DRAG in line:
            self._last_drag_ts = ts
            return out
        if _TRIGGER_ATTACH in line:
            # Detection d'achat vitrine DESACTIVEE : 'AttachmentReceived' se
            # declenche pour trop d'objets non-achetes (mobiGlas et items du
            # chargement de depart au spawn, prises en main...), meme
            # inventaire ferme. En attendant un critere fiable (probablement
            # le Port[...] ou un vrai message financier), on ignore l'event.
            self._last_drag_ts = self._last_drag_ts   # no-op (etat inchange)
            return out

        if _TRIGGER_COMMODITY_BUY in line:
            op = self._make_commodity(ts, line, "sent")
            if op:
                out.append(op)
            return out
        if _TRIGGER_COMMODITY_SELL in line:
            op = self._make_commodity(ts, line, "received")
            if op:
                out.append(op)
            return out
        if _TRIGGER_RENTAL in line:
            # On memorise la location ; elle ne sera validee qu'au Success.
            self._pending_rental = self._make_rental(ts, line)
            return out
        if _TRIGGER_SHOP_RESP in line:
            # Reponse du shop : valide (ou annule) la location en attente.
            if self._pending_rental is not None:
                if "result[Success]" in line:
                    out.append(self._pending_rental)
                self._pending_rental = None
            return out

        if _TRIGGER_BUY in line:
            op = self._make_purchase(ts, line)
            if op:
                out.append(op)
            return out
        if _TRIGGER_SELL in line:
            op = self._make_sale(ts, line)
            if op:
                out.append(op)
            return out
        if _TRIGGER_SENT in line:
            self._awaiting, self._pending_ts, self._wait_count = "sent", ts, 0
            pm = _PEER_SENT_RE.search(line)
            self._pending_peer = pm.group("peer").strip() if pm else None
            m = _AMOUNT_RE.search(line)
            if m:
                op = self._make_transfer(self._pending_ts, "sent",
                                         m.group("amt"), self._pending_peer)
                self._awaiting = None
                if op:
                    out.append(op)
            return out
        if 'Added notification' in line and _TRIGGER_RECV in line:
            self._awaiting, self._pending_ts, self._wait_count = "received", ts, 0
            pm = _PEER_RECV_RE.search(line)
            self._pending_peer = pm.group("peer").strip() if pm else None
            return out
        if self._awaiting:
            m = _AMOUNT_RE.search(line)
            if m:
                op = self._make_transfer(self._pending_ts, self._awaiting,
                                         m.group("amt"), self._pending_peer)
                self._awaiting = None
                if op:
                    out.append(op)
            else:
                self._wait_count += 1
                if self._wait_count > 4:   # garde-fou
                    self._awaiting = None
        return out

    @staticmethod
    def _make_transfer(ts, direction, raw_amt, peer=None) -> Optional[dict]:
        n = _parse_amount(raw_amt)
        if n is None:
            return None
        # 'amount' = montant LOGGE = ce que recoit le destinataire (net).
        # Taxe 0,5 % (arrondi superieur) UNIQUEMENT sur les envois ; elle
        # s'ajoute au debit reel de l'expediteur (net + taxe).
        fee = math.ceil(n * SERVICE_FEE_RATE) if direction == "sent" else 0
        return {
            "kind": "transfer", "ts": ts or "", "direction": direction,
            "amount": n, "net": n, "fee": fee,
            "peer": (peer or "").strip(), "raw": raw_amt,
        }

    @staticmethod
    def _pretty_vehicle(item_name: Optional[str]) -> str:
        """'DRAK_Cutter' -> 'Drake Cutter' ; 'GRIN_STV' -> 'Greycat STV'.
        Prefixe constructeur developpe si connu, sinon laisse tel quel."""
        s = (item_name or "").strip()
        if "_" in s:
            pre, rest = s.split("_", 1)
            brand = _BRAND_PREFIXES.get(pre.upper())
            if brand:
                return f"{brand} {rest.replace('_', ' ')}"
        return s.replace("_", " ")

    @staticmethod
    def _make_rental(ts, line) -> Optional[dict]:
        """Location de vehicule : sortie (on paie). Prix=client_price,
        nom=itemName (marque developpee), duree=offering[N]."""
        price = _logfield("client_price", line)
        if price is None:
            return None
        try:
            amount = int(round(float(price)))
        except ValueError:
            return None
        offering = _logfield("offering", line)
        duration = _RENTAL_DURATIONS.get((offering or "").strip(),
                                         "durée inconnue")
        return {
            "kind": "rental", "ts": ts or "", "direction": "sent",
            "amount": amount,
            "item": TransferParser._pretty_vehicle(_logfield("itemName", line)),
            "duration": duration,
            "shop": _pretty_shop(_logfield("shopName", line)),
            "currency": _logfield("currencyType", line) or "aUEC",
        }

    @staticmethod
    def _make_showcase_purchase(ts, line) -> Optional[dict]:
        """Achat en vitrine : ligne informative SANS prix (amount=None). Le
        jeu ne journalise aucun montant, on affiche donc juste l'objet."""
        m = _ATTACH_RE.search(line)
        item = _pretty_item(m.group("item")) if m else ""
        return {
            "kind": "purchase", "ts": ts or "", "direction": "sent",
            "amount": None, "item": item, "qty": 1,
            "shop": "Vitrine", "currency": "aUEC", "no_price": True,
        }

    @staticmethod
    def _make_purchase(ts, line) -> Optional[dict]:
        price = _logfield("client_price", line)
        if price is None:
            return None
        try:
            amount = int(round(float(price)))
        except ValueError:
            return None
        try:
            qty = int(_logfield("quantity", line) or "1")
        except ValueError:
            qty = 1
        return {
            "kind": "purchase", "ts": ts or "", "direction": "sent",
            "amount": amount, "item": _pretty_item(_logfield("itemName", line)),
            "qty": qty, "shop": _pretty_shop(_logfield("shopName", line)),
            "currency": _logfield("currencyType", line) or "aUEC",
        }

    @staticmethod
    def _make_sale(ts, line) -> Optional[dict]:
        # Les ventes au kiosque (SendShopSellRequest) emettent un champ
        # client_price[...] comme les achats, et PAS amount[...]. On accepte
        # les deux : amount[...] (ancien format observe) sinon, en repli,
        # client_price[...] — sans quoi la vente etait silencieusement jetee.
        raw = _logfield("amount", line)
        if raw is None:
            raw = _logfield("client_price", line)
        if raw is None:
            return None
        try:
            amount = int(round(float(raw)))
        except ValueError:
            return None
        try:
            qty = int(_logfield("quantity", line) or "1")
        except ValueError:
            qty = 1
        item = _pretty_item(_logfield("itemName", line)) or "Marchandise"
        return {
            "kind": "sale", "ts": ts or "", "direction": "received",
            "amount": amount, "item": item, "qty": qty,
            "shop": _pretty_shop(_logfield("shopName", line)),
        }

    @staticmethod
    def _make_commodity(ts, line, direction) -> Optional[dict]:
        """Achat/vente de marchandises (cargo) au kiosque commodities. Le
        montant total (achat) dans price[...] OU amount[...] (vente) ; la
        quantite est en centi-SCU (ex. quantity[800.000000 cSCU] -> 8 SCU),
        ou en entites (quantity[1] + transactionMode[Entities]). Pas de
        itemName exploitable (seul un resourceGUID, non resolvable sans
        table) -> libelle generique 'Marchandise (cargo)'."""
        # Achat commodity : price[...] ; vente commodity : amount[...].
        raw = _logfield("price", line)
        if raw is None:
            raw = _logfield("amount", line)
        if raw is None:
            return None
        try:
            amount = int(round(float(raw)))
        except (TypeError, ValueError):
            return None
        # Quantite : centi-SCU -> SCU (/100) si l'unite 'cSCU' est presente,
        # sinon valeur brute. Repli sur boxSize si quantity absent.
        scu = None
        qraw = _logfield("quantity", line) or ""
        m = re.search(r"[-+]?\d*\.?\d+", qraw)
        if m:
            try:
                val = float(m.group(0))
                scu = val / 100.0 if "cscu" in qraw.lower() else val
            except ValueError:
                scu = None
        if scu is None:
            braw = _logfield("boxSize", line)
            if braw is not None:
                try:
                    scu = float(braw)
                except ValueError:
                    scu = None
        qty = int(round(scu)) if scu is not None else 1
        return {
            "kind": "purchase" if direction == "sent" else "sale",
            "ts": ts or "", "direction": direction,
            "amount": amount, "item": "Marchandise (cargo)", "qty": qty,
            "shop": _pretty_shop(_logfield("shopName", line)),
            "currency": "aUEC",
        }


# ======================================================================
#  Source de repli autonome : tail Game.log emettant des lignes BRUTES.
#  (En intégré, c'est services.gamelog qui fournit les lignes ; ce thread
#   ne sert qu'au mode autonome / rattrapage one-shot.)
# ======================================================================
class GameLogRawTailThread(QThread):
    """Tail le Game.log et emet chaque ligne brute (sig_line). Le parsing
    est fait en aval par TransferParser, comme la source integree.

    history  : relit tout le fichier au demarrage (sinon live-only).
    oneshot  : apres l'historique, s'arrete (rattrapage de session) au lieu
               de continuer a tailer. Utilise en mode integre.
    """
    sig_line = Signal(str)
    sig_status = Signal(str)

    def __init__(self, path: str, history: bool = True,
                 oneshot: bool = False, parent=None):
        super().__init__(parent)
        self._path = path
        self._history = history
        self._oneshot = oneshot
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        path = self._path
        if not path or not os.path.exists(path):
            self.sig_status.emit("Game.log introuvable")
            return
        try:
            initial_size = os.path.getsize(path)
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                if self._history:
                    self.sig_status.emit("Lecture de l'historique...")
                    for line in f:
                        if self._stop:
                            return
                        self.sig_line.emit(line)
                else:
                    f.seek(0, 2)   # fin de fichier (live only)
                if self._oneshot:
                    self.sig_status.emit("Historique lu")
                    return
                self.sig_status.emit(f"En direct : {os.path.basename(path)}")
                while not self._stop:
                    line = f.readline()
                    if not line:
                        time.sleep(0.2)
                        try:
                            cur = os.path.getsize(path)
                            if cur < initial_size:   # SC a reecrit le log
                                self.sig_status.emit("Game.log reinitialise, reprise")
                                f.seek(0)
                                initial_size = cur
                        except Exception:
                            pass
                        continue
                    self.sig_line.emit(line)
        except Exception as e:
            self.sig_status.emit(f"Erreur tail : {e}")


# ======================================================================
#  Une ligne de l'historique (carte transfert) — reprise a l'identique.
# ======================================================================
class TransferRow(QFrame):
    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self._op = entry          # conserve pour le filtre par periode
        self.setObjectName("TransferRow")
        kind = entry.get("kind", "transfer")
        sent = entry["direction"] == "sent"
        color = COLOR_SENT if sent else COLOR_RECV
        sign = "\u2212" if sent else "+"     # − / +

        if kind == "purchase":
            title = f"Achat  ·  ×{entry.get('qty', 1)}"
            sub1 = entry.get("shop", "") or "Boutique"
            sub2 = entry.get("item", "")
            amount = entry.get("amount", 0)
        elif kind == "sale":
            title = f"Vente  ·  ×{entry.get('qty', 1)}"
            sub1 = entry.get("shop", "") or "Boutique"
            sub2 = entry.get("item", "")
            amount = entry.get("amount", 0)
        elif kind == "rental":
            title = f"Location  ·  {entry.get('duration', '')}".strip()
            sub1 = entry.get("item", "") or "Véhicule"
            sub2 = entry.get("shop", "") or None
            amount = entry.get("amount", 0)
        else:
            peer = entry.get("peer") or "?"
            fee = entry.get("fee", 0)
            if sent:
                title = "Transfert envoyé"
                sub1 = f"à {peer}"
                sub2 = (f"dont taxe {_fmt_amount(fee)} aUEC" if fee else None)
                amount = entry.get("amount", 0) + fee   # debit reel (net+taxe)
            else:
                title = "Transfert reçu"
                sub1 = f"de {peer}"
                sub2 = None
                amount = entry.get("amount", 0)

        self.setStyleSheet(
            "QFrame#TransferRow { background: transparent; "
            "border-bottom: 1px solid #eceef0; }"
        )
        h = QHBoxLayout(self)
        h.setContentsMargins(14, 10, 14, 10)
        h.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(2)
        lbl_title = QLabel(title)
        lbl_title.setStyleSheet(
            f"color:{PHONE_NAME_DARK}; font-size:10pt; font-weight:600; "
            "background:transparent;"
        )
        left.addWidget(lbl_title)
        for sub in (sub1, sub2):
            if not sub:
                continue
            lbl_sub = QLabel(sub)
            lbl_sub.setWordWrap(True)
            lbl_sub.setStyleSheet(
                f"color:{PHONE_NAME_GREY}; font-size:8pt; background:transparent;"
            )
            left.addWidget(lbl_sub)
        lbl_time = QLabel(self._fmt_ts(entry.get("ts", "")))
        lbl_time.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:8pt; background:transparent;"
        )
        left.addWidget(lbl_time)
        h.addLayout(left, 1)

        if amount is None:            # achat vitrine sans prix connu
            lbl_amt = QLabel("—")
        else:
            lbl_amt = QLabel(f"{sign}{_abbrev_list(amount)} aUEC")
        lbl_amt.setAlignment(Qt.AlignRight | Qt.AlignTop)
        lbl_amt.setStyleSheet(
            f"color:{color}; font-size:11pt; font-weight:700; "
            "background:transparent;"
        )
        h.addWidget(lbl_amt, 0)

    @staticmethod
    def _fmt_ts(ts: str) -> str:
        """'2026-05-29T15:36:41.454Z' -> '29/05 15:36:41'. Best-effort."""
        m = re.match(
            r"(?P<y>\d{4})-(?P<mo>\d{2})-(?P<d>\d{2})T"
            r"(?P<h>\d{2}):(?P<mi>\d{2}):(?P<s>\d{2})", ts or ""
        )
        if not m:
            return ts or ""
        g = m.groupdict()
        return f"{g['d']}/{g['mo']} {g['h']}:{g['mi']}:{g['s']}"

    def flash(self):
        """Surbrillance breve a l'arrivee (puis retour transparent)."""
        self.setStyleSheet(
            "QFrame#TransferRow { background:" + ROW_FLASH_BG + "; "
            "border-bottom:1px solid #eceef0; }"
        )
        QTimer.singleShot(1500, lambda: self.setStyleSheet(
            "QFrame#TransferRow { background: transparent; "
            "border-bottom:1px solid #eceef0; }"
        ))


# ======================================================================
#  Persistance : circusphone_wallet.json (cfg + operations cumulees)
# ======================================================================
_BASE_DIR = Path(__file__).resolve().parent
WALLET_FILE = _BASE_DIR / "circusphone_wallet.json"
# Cap : on conserve les N operations les plus recentes (evite la croissance
# infinie du fichier). 500 = large pour un usage normal.
WALLET_MAX_OPS = 500


def _load_wallet() -> dict:
    """Charge le fichier wallet ({version, gamelog_path, operations})."""
    if WALLET_FILE.exists():
        try:
            with open(WALLET_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                data.setdefault("operations", [])
                return data
        except Exception:
            pass
    return {"version": 1, "gamelog_path": "", "operations": []}


def _save_wallet(data: dict) -> None:
    """Ecrit le fichier wallet de maniere best-effort (ecriture atomique)."""
    try:
        tmp = WALLET_FILE.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp, WALLET_FILE)
    except Exception:
        pass


def _op_key(op: dict) -> tuple:
    """Cle de deduplication. ts a la milliseconde dans SC -> collisions
    quasi impossibles pour de vraies operations distinctes."""
    return (
        op.get("kind"), op.get("ts"), op.get("direction"),
        op.get("amount"), op.get("item"),
    )


# ======================================================================
#  WalletApp : l'application Portefeuille au contrat PhoneApp.
# ======================================================================
class WalletApp(PhoneApp):
    """Portefeuille du CircusPhone. Affiche l'historique cumule des
    mouvements aUEC (envois/receptions entre joueurs avec split net/taxe,
    achats et ventes boutique), persiste entre sessions, et se met a jour
    en direct via le feed Game.log."""

    APP_ID = "wallet"
    APP_NAME = "Portefeuille"
    APP_ICON = "\u20B5"            # ₵
    CAPTURES_KEYBOARD = False      # app non-jeu : nav D-pad standard

    def __init__(self, screen_w, screen_h, screen_radius, services, parent=None):
        super().__init__(screen_w, screen_h, screen_radius, services, parent)

        # Fond blanc arrondi : la page du stack remplit la zone ecran.
        self.setObjectName("WalletScreen")
        self.setStyleSheet(
            "QWidget#WalletScreen { background:" + PHONE_SCREEN_BG + "; "
            f"border-radius:{self._screen_rad}px; }}"
        )

        # --- Etat ---
        self._parser = TransferParser()
        self._catchup_parser: Optional[TransferParser] = None
        self._data = _load_wallet()
        self._ops: list = list(self._data.get("operations", []))
        # Tri par timestamp (newest-first) : garantit un ordre coherent meme
        # si le JSON a ete sauve dans un ordre d'insertion imparfait.
        self._ops.sort(key=lambda o: (o.get("ts", "") or ""), reverse=True)
        self._seen = {_op_key(op) for op in self._ops}
        self._sim_counter = 0
        self._raw_thread: Optional[GameLogRawTailThread] = None
        self._subscribed = False
        # Flag : True pendant le rattrapage one-shot pour ne pas re-sauver a
        # chaque ligne (on sauve une fois a la fin).
        self._bulk_loading = False

        self._build_screen()
        self._populate_from_ops()
        self._update_total()

        # Branchement de la source (live + rattrapage), apres construction UI.
        QTimer.singleShot(80, self._start_sources)

    # ------------------------------------------------------------------
    #  UI
    # ------------------------------------------------------------------
    def _build_screen(self):
        sv = QVBoxLayout(self)
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(0)

        # En-tete : titre + bouton menu, total, statut.
        header = QWidget()
        header.setStyleSheet("background:transparent;")
        hv = QVBoxLayout(header)
        hv.setContentsMargins(16, 14, 12, 10)
        hv.setSpacing(2)

        top = QHBoxLayout()
        top.setSpacing(6)
        lbl_app = QLabel("Portefeuille")
        lbl_app.setStyleSheet(
            f"color:{PHONE_NAME_DARK}; font-size:14pt; font-weight:bold; "
            "background:transparent;"
        )
        top.addWidget(lbl_app, 1)
        self.btn_menu = QPushButton("\u22ef")   # ⋯
        self.btn_menu.setFixedSize(26, 26)
        self.btn_menu.setCursor(Qt.PointingHandCursor)
        self.btn_menu.setStyleSheet(
            "QPushButton { color:#5a6068; background:#f1f3f5; border:none; "
            "  border-radius:13px; font-size:13pt; font-weight:700; }"
            "QPushButton:hover { background:#e2e6ea; color:#1a1a1a; }"
        )
        self.btn_menu.clicked.connect(self._open_menu)
        top.addWidget(self.btn_menu, 0)
        hv.addLayout(top)

        # --- Filtre par periode (segmente) : 24h / 7j / 30j / Tout --------
        self._period = "all"
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

        # --- Resume : Entre (+) / Sorti (-) / Solde (net) ----------------
        summ = QHBoxLayout()
        summ.setContentsMargins(0, 6, 0, 2)
        summ.setSpacing(4)

        def _col(label_txt):
            c = QVBoxLayout()
            c.setSpacing(1)
            lab = QLabel(label_txt)
            lab.setAlignment(Qt.AlignCenter)
            lab.setStyleSheet("color:#9aa0a6; font-size:7pt; "
                              "font-weight:bold; background:transparent;")
            val = QLabel("+0")
            val.setAlignment(Qt.AlignCenter)
            c.addWidget(lab)
            c.addWidget(val)
            return c, val

        col_in, self.lbl_in = _col("REÇU")
        col_out, self.lbl_out = _col("DÉPENSÉ")
        col_net, self.lbl_net = _col("SOLDE")
        self.lbl_in.setStyleSheet(
            f"color:{COLOR_RECV}; font-size:14pt; font-weight:bold; "
            "background:transparent;")
        self.lbl_out.setStyleSheet(
            f"color:{COLOR_SENT}; font-size:14pt; font-weight:bold; "
            "background:transparent;")
        self.lbl_net.setStyleSheet(
            f"color:{COLOR_RECV}; font-size:15pt; font-weight:bold; "
            "background:transparent;")
        summ.addLayout(col_in)
        summ.addLayout(col_out)
        # Fin separateur vertical : isole le detail (entrees/sorties) du
        # resultat (solde).
        vsep = QFrame()
        vsep.setFrameShape(QFrame.VLine)
        vsep.setFixedWidth(1)
        vsep.setStyleSheet("background:#eceef0; border:none; margin:4px 6px;")
        summ.addWidget(vsep)
        summ.addLayout(col_net)
        hv.addLayout(summ)

        self.lbl_status = QLabel("Initialisation...")
        self.lbl_status.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:8pt; background:transparent;"
        )
        hv.addWidget(self.lbl_status)
        sv.addWidget(header)

        sep = QFrame()
        sep.setFixedHeight(1)
        sep.setStyleSheet("background:#eceef0; border:none;")
        sv.addWidget(sep)

        # Liste scrollable.
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setStyleSheet(
            "QScrollArea { background:transparent; border:none; }"
            "QScrollBar:vertical { width:6px; background:transparent; }"
            "QScrollBar::handle:vertical { background:#d4d8dc; border-radius:3px; }"
        )
        self.list_host = QWidget()
        self.list_host.setStyleSheet("background:transparent;")
        self.list_layout = QVBoxLayout(self.list_host)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(0)

        self.lbl_empty = QLabel("Aucun mouvement enregistré pour l'instant.")
        self.lbl_empty.setAlignment(Qt.AlignCenter)
        self.lbl_empty.setWordWrap(True)
        self.lbl_empty.setStyleSheet(
            f"color:{PHONE_NAME_GREY}; font-size:9pt; padding:26px 14px; "
            "background:transparent;"
        )
        self.list_layout.addWidget(self.lbl_empty)
        self.list_layout.addStretch(1)

        self.scroll.setWidget(self.list_host)
        sv.addWidget(self.scroll, 1)

    # ------------------------------------------------------------------
    #  Cycle de vie (contrat PhoneApp)
    # ------------------------------------------------------------------
    def on_show(self):
        """Devient l'ecran courant : on remonte la liste en haut. Le feed
        reste actif en permanence (on ne veut PAS manquer un transfert quand
        l'app n'est pas affichee), donc rien de lourd a (re)demarrer ici."""
        try:
            self.scroll.verticalScrollBar().setValue(0)
        except Exception:
            pass

    def on_hide(self):
        """Quitte l'ecran : no-op. Contrairement aux jeux, le wallet doit
        continuer a capter les mouvements en fond (cout negligeable)."""
        pass

    # --- Navigation D-pad (app non-jeu) -------------------------------
    _PERIOD_ORDER = ("24h", "7j", "30j", "all")

    def handle_nav(self, direction: str) -> bool:
        """Gauche/Droite : change le filtre de periode (applique aussitot).
        Haut/Bas : fait defiler la liste des transactions. Retourne True si
        l'action a ete consommee."""
        if direction in ("left", "right"):
            order = self._PERIOD_ORDER
            try:
                i = order.index(self._period)
            except ValueError:
                i = len(order) - 1        # "all" par defaut
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

    def shutdown(self):
        """Liberation a la destruction de l'app (appelable par l'overlay) :
        arrete le tail autonome et se desabonne du feed integre."""
        if self._raw_thread is not None:
            try:
                self._raw_thread.stop()
                self._raw_thread.wait(1500)
            except Exception:
                pass
            self._raw_thread = None
        self._unsubscribe_feed()

    # ------------------------------------------------------------------
    #  Sources de lignes : feed integre (prioritaire) + rattrapage one-shot,
    #  ou tail autonome en repli.
    # ------------------------------------------------------------------
    def _start_sources(self):
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "subscribe"):
            # Mode INTEGRE : on s'abonne au tail existant (lignes brutes en
            # direct), et on lit une fois l'historique de la SESSION courante
            # pour rattraper ce qui a precede l'abonnement (deduplique).
            try:
                gamelog.subscribe(self._on_raw_line)
                self._subscribed = True
                self._set_status("En direct (tail partagé)", live=True)
            except Exception as e:
                self._set_status(f"Abonnement feed KO : {e}", err=True)
            self._catch_up_session()
        else:
            # Mode AUTONOME (harnais / app séparée) : notre propre tail,
            # historique + live.
            path = find_gamelog(self._data.get("gamelog_path"))
            if not path:
                self._set_status("Game.log introuvable — menu « ⋯ »")
                return
            self._data["gamelog_path"] = path
            _save_wallet(self._data)
            self._start_raw_tail(path, history=True, oneshot=False)

    def _catch_up_session(self):
        """Lecture one-shot du Game.log courant pour rattraper la session
        avant l'abonnement. Deduplique contre le JSON via _seen."""
        path = None
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "current_path"):
            try:
                path = gamelog.current_path()
            except Exception:
                path = None
        path = path or find_gamelog(self._data.get("gamelog_path"))
        if path:
            # Parser DEDIE au rattrapage : ne pas partager l'automate avec
            # le feed live (sinon une ligne live intercalee au milieu d'une
            # notif d'historique corromprait l'etat _awaiting).
            self._catchup_parser = TransferParser()
            self._start_raw_tail(path, history=True, oneshot=True,
                                 line_handler=self._on_catchup_line)

    def _start_raw_tail(self, path: str, history: bool, oneshot: bool,
                        line_handler=None):
        if line_handler is None:
            line_handler = self._on_raw_line
        if self._raw_thread is not None:
            self._raw_thread.stop()
            self._raw_thread.wait(1500)
            self._raw_thread = None
        self._raw_thread = GameLogRawTailThread(path, history=history, oneshot=oneshot)
        self._raw_thread.sig_line.connect(line_handler)
        self._raw_thread.sig_status.connect(self._on_tail_status)
        self._raw_thread.start()

    def _unsubscribe_feed(self):
        if not self._subscribed:
            return
        gamelog = getattr(self.services, "gamelog", None)
        if gamelog is not None and hasattr(gamelog, "unsubscribe"):
            try:
                gamelog.unsubscribe(self._on_raw_line)
            except Exception:
                pass
        self._subscribed = False

    # ------------------------------------------------------------------
    #  Reception des lignes -> parsing -> ingestion
    # ------------------------------------------------------------------
    def _on_raw_line(self, line: str):
        for op in self._parser.feed(line):
            self._ingest(op, flash=not self._bulk_loading)

    def _on_catchup_line(self, line: str):
        """Lignes du rattrapage one-shot : parser dedie, jamais de flash
        (chargement en masse), dedup via _seen contre le JSON."""
        for op in self._catchup_parser.feed(line):
            self._ingest(op, flash=False)

    def _on_tail_status(self, msg: str):
        # Gestion du flag bulk : True pendant la lecture d'historique, remis
        # a False (avec sauvegarde unique) soit a la fin du one-shot
        # ("Historique lu"), soit au passage en live ("En direct...") du tail
        # autonome — ce dernier n'emet jamais "Historique lu".
        if msg == "Lecture de l'historique...":
            self._bulk_loading = True
        elif msg == "Historique lu" or msg.startswith("En direct"):
            if self._bulk_loading:
                self._bulk_loading = False
                self._data["operations"] = self._ops
                _save_wallet(self._data)
        self._set_status(
            msg,
            live=msg.startswith("En direct"),
            err=("introuvable" in msg or "Erreur" in msg),
        )

    # ------------------------------------------------------------------
    #  Ingestion d'une operation (dedup + totaux + persistance + UI)
    # ------------------------------------------------------------------
    def _ingest(self, op: dict, flash: bool = True):
        key = _op_key(op)
        if key in self._seen:
            return                      # deja connue (JSON ou doublon log)
        # Pre-calcul net/taxe pour les transferts (stocke dans l'op pour
        # que TransferRow et les totaux le lisent au rechargement).
        if op.get("kind") == "transfer" and "fee" not in op:
            # Op rechargee depuis un ancien JSON sans taxe : on complete selon
            # le modele actuel (montant logge = net ; taxe 0,5 % sur envois).
            amt = op.get("amount", 0)
            op["net"] = amt
            op["fee"] = (math.ceil(amt * SERVICE_FEE_RATE)
                         if op.get("direction") == "sent" else 0)

        self._seen.add(key)
        # Insertion TRIEE par timestamp (newest-first). Les ts sont des
        # chaines ISO -> l'ordre lexical = l'ordre chronologique. Necessaire
        # car les evenements multi-lignes (transfert, location) se terminent
        # avec un decalage : leur op arrive apres des ops plus recentes, il
        # faut donc la replacer a sa vraie position et pas juste en tete.
        op_ts = op.get("ts", "") or ""
        idx = 0
        while idx < len(self._ops) and (self._ops[idx].get("ts", "") or "") >= op_ts:
            idx += 1
        self._ops.insert(idx, op)
        if len(self._ops) > WALLET_MAX_OPS:
            removed = self._ops[WALLET_MAX_OPS:]
            self._ops = self._ops[:WALLET_MAX_OPS]
            for old in removed:
                self._seen.discard(_op_key(old))

        # UI : carte inseree a la MEME position (la zone de liste commence a
        # l'index 0 ; lbl_empty + stretch sont en fin de layout).
        self.lbl_empty.hide()
        row = TransferRow(op)
        self.list_layout.insertWidget(idx, row)
        if flash:
            row.flash()

        self._update_total()
        # Persistance : pendant le bulk one-shot on attend la fin
        # (_on_tail_status) ; sinon on sauve a chaque mouvement (rare).
        if not self._bulk_loading:
            self._data["operations"] = self._ops
            _save_wallet(self._data)

    # ------------------------------------------------------------------
    #  Filtre par periode + resume Entre / Sorti / Solde
    # ------------------------------------------------------------------
    def _period_start(self):
        """Debut de la fenetre courante (datetime UTC), ou None pour 'Tout'."""
        delta = {"24h": datetime.timedelta(hours=24),
                 "7j":  datetime.timedelta(days=7),
                 "30j": datetime.timedelta(days=30)}.get(self._period)
        if delta is None:
            return None
        return datetime.datetime.now(datetime.timezone.utc) - delta

    @staticmethod
    def _op_dt(op):
        """Parse le timestamp ISO d'une op -> datetime UTC, ou None."""
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
        """True si l'op tombe dans la fenetre (start=None -> toujours)."""
        if start is None:
            return True
        dt = self._op_dt(op)
        return dt is not None and dt >= start

    def _recompute_totals(self, start) -> tuple:
        """(entrees, sorties) sur la periode. Entrees = ventes + transferts
        recus ; sorties = achats + transferts envoyes (montant + taxe)."""
        inc = out = 0
        for op in self._ops:
            if not self._op_in_period(op, start):
                continue
            kind = op.get("kind")
            direction = op.get("direction")
            if kind == "transfer" and direction == "sent":
                out += op.get("amount", 0) + op.get("fee", 0)
            elif kind == "transfer" and direction == "received":
                inc += op.get("amount", 0)
            elif kind == "sale":
                inc += op.get("amount", 0)
            elif kind in ("purchase", "rental"):
                amt = op.get("amount")
                if amt is not None:
                    out += amt
        return inc, out

    def _set_period(self, key):
        self._period = key
        self._update_total()

    def _restyle_period(self):
        for k, b in self._period_btns.items():
            if k == self._period:
                b.setStyleSheet(
                    "QPushButton { background:#2f6fed; color:#ffffff; "
                    "border:none; border-radius:12px; font-size:9pt; "
                    "font-weight:700; }")
            else:
                b.setStyleSheet(
                    "QPushButton { background:transparent; color:#5a6068; "
                    "border:none; border-radius:12px; font-size:9pt; "
                    "font-weight:600; }"
                    "QPushButton:hover { color:#1a1a1a; }")

    def _update_total(self):
        """Rafraichit le resume (Entre/Sorti/Solde) pour la periode courante
        ET filtre l'affichage de la liste. Appele a chaque changement d'ops
        ou de periode."""
        start = self._period_start()
        inc, out = self._recompute_totals(start)
        net = inc - out
        self.lbl_in.setText("+" + _abbrev_k(inc))
        self.lbl_out.setText("\u2212" + _abbrev_k(out))
        self.lbl_net.setText(("+" if net >= 0 else "\u2212")
                             + _abbrev_k(abs(net)))
        self.lbl_net.setStyleSheet(
            f"color:{COLOR_RECV if net >= 0 else COLOR_SENT}; "
            "font-size:15pt; font-weight:bold; background:transparent;")
        # Filtre la liste : n'afficher que les ops de la periode.
        for i in range(self.list_layout.count()):
            it = self.list_layout.itemAt(i)
            w = it.widget() if it is not None else None
            if isinstance(w, TransferRow):
                w.setVisible(self._op_in_period(w._op, start))
        self._restyle_period()

    def _populate_from_ops(self):
        """Construit les cartes depuis le JSON au demarrage (sans flash,
        sans re-sauver). Les ops sont newest-first -> addWidget en ordre."""
        if not self._ops:
            return
        self.lbl_empty.hide()
        for op in self._ops:
            self.list_layout.insertWidget(self.list_layout.count() - 2,
                                          TransferRow(op))

    # ------------------------------------------------------------------
    #  Menu « ⋯ »
    # ------------------------------------------------------------------
    def _open_menu(self):
        m = QMenu(self)
        m.setStyleSheet(
            "QMenu { background:#1f242b; color:#e6e6e6; border:1px solid #30363d; "
            "  border-radius:8px; padding:4px; font-size:9pt; }"
            "QMenu::item { padding:6px 14px; border-radius:6px; }"
            f"QMenu::item:selected {{ background:{PHONE_ACCENT}; }}"
        )
        standalone = getattr(self.services, "gamelog", None) is None
        act_pick = m.addAction("Choisir Game.log…") if standalone else None
        act_clear = m.addAction("Vider l'historique")
        pos = self.btn_menu.mapToGlobal(self.btn_menu.rect().bottomLeft())
        chosen = m.exec(pos)
        if chosen is None:
            return
        if act_pick is not None and chosen is act_pick:
            self._on_pick()
        elif chosen is act_clear:
            self._on_clear()

    def _on_pick(self):
        """Mode autonome : pointer manuellement le Game.log."""
        start_dir = ""
        cur = self._data.get("gamelog_path")
        if cur and os.path.exists(os.path.dirname(cur)):
            start_dir = os.path.dirname(cur)
        path, _ = QFileDialog.getOpenFileName(
            self, "Selectionner Game.log", start_dir,
            "Game.log (Game.log);;Tous les fichiers (*.*)"
        )
        if path:
            self._data["gamelog_path"] = path
            _save_wallet(self._data)
            self._parser.reset()
            self._start_raw_tail(path, history=True, oneshot=False)

    def _on_simulate(self):
        """Injecte un faux mouvement (cycle transfert/transfert/achat/vente)
        pour juger le visuel sans jouer. ts unique pour ne pas etre dedupe."""
        self._sim_counter += 1
        step = self._sim_counter % 4
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{self._sim_counter:03d}Z"
        if step == 1:
            self._ingest({"kind": "transfer", "ts": ts, "direction": "sent",
                          "amount": 101, "raw": "101"})
        elif step == 2:
            self._ingest({"kind": "transfer", "ts": ts, "direction": "sent",
                          "amount": 1005, "raw": "1005"})
        elif step == 3:
            self._ingest({"kind": "purchase", "ts": ts, "direction": "sent",
                          "amount": 2915, "qty": 11, "shop": "H Pharmacy Levski",
                          "item": "crlf consumable radiation 01", "currency": "aUEC"})
        else:
            self._ingest({"kind": "sale", "ts": ts, "direction": "received",
                          "amount": 4520, "qty": 1,
                          "shop": "Pyro RestStop Rund Admin", "item": "Marchandise"})

    def _on_clear(self):
        """Vide l'historique persiste et la liste affichee."""
        self._ops = []
        self._seen = set()
        self._data["operations"] = []
        _save_wallet(self._data)
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            w = item.widget()
            if isinstance(w, TransferRow):
                w.deleteLater()
            elif w is not None:
                w.setParent(None)
        self.list_layout.addWidget(self.lbl_empty)
        self.list_layout.addStretch(1)
        self.lbl_empty.show()
        self._update_total()

    # ------------------------------------------------------------------
    #  Statut
    # ------------------------------------------------------------------
    def _set_status(self, msg: str, live: bool = False, err: bool = False):
        if err:
            color = COLOR_SENT
        elif live:
            color = COLOR_RECV
        else:
            color = PHONE_NAME_GREY
        self.lbl_status.setText(msg)
        self.lbl_status.setStyleSheet(
            f"color:{color}; font-size:8pt; background:transparent;"
        )


# ======================================================================
#  HARNAIS DE TEST VISUEL — supprimable. Monte la WalletApp dans un
#  chassis CircusPhone (services.gamelog=None -> repli tail autonome).
#    ⋯ : menu (simuler / vider / choisir Game.log)    Echap : quitter
# ======================================================================
def _harness_main() -> int:
    import sys
    from PySide6.QtGui import (
        QColor, QFont, QFontMetrics, QPainter, QGuiApplication,
    )

    app = QApplication(sys.argv)
    screen = QGuiApplication.primaryScreen()
    try:
        scr_h = screen.availableGeometry().height()
    except Exception:
        scr_h = 1080
    body_h = max(420, min(760, int(scr_h * 0.62)))
    body_w = int(body_h * (200.0 / 440.0))
    sx, sy = body_w / 200.0, body_h / 440.0
    radius   = int(28 * sx)
    banner_h = int(56 * sy)
    screen_x = int(12 * sx)
    screen_y = banner_h
    screen_w = body_w - 2 * screen_x
    screen_h = body_h - banner_h - int(16 * sy)
    screen_rad = int(14 * sx)

    class _Chassis(QWidget):
        def __init__(self):
            super().__init__(
                None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
            )
            self.setAttribute(Qt.WA_TranslucentBackground, True)
            self.setFixedSize(body_w, body_h)
            self._drag = None
            self.app = WalletApp(screen_w, screen_h, screen_rad,
                                 PhoneServices(), self)
            self.app.move(screen_x, screen_y)
            self.app.resize(screen_w, screen_h)
            self.app.on_show()

        def paintEvent(self, _ev):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing, True)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#1a1a1a"))
            p.drawRoundedRect(0, 0, body_w, body_h, radius, radius)
            p.setBrush(QColor("#0a0a0a"))
            bw = max(2, int(body_w * 0.015))
            p.drawRoundedRect(0, int(body_h * 0.25), bw, int(body_h * 0.05), 1, 1)
            p.drawRoundedRect(0, int(body_h * 0.33), bw, int(body_h * 0.09), 1, 1)
            p.drawRoundedRect(body_w - bw, int(body_h * 0.36), bw, int(body_h * 0.14), 1, 1)
            f1 = QFont(); f1.setPointSizeF(max(7.0, body_w * 0.05))
            f2 = QFont(); f2.setPointSizeF(max(10.0, body_w * 0.085)); f2.setBold(True)
            fm1 = QFontMetrics(f1)
            t1, t2 = "Circus", "Phone"
            total = fm1.horizontalAdvance(t1) + QFontMetrics(f2).horizontalAdvance(t2)
            x0 = (body_w - total) / 2.0
            base = banner_h * 0.66
            p.setFont(f1); p.setPen(QColor("#888888"))
            p.drawText(int(x0), int(base), t1)
            p.setFont(f2); p.setPen(QColor("#ffffff"))
            p.drawText(int(x0 + fm1.horizontalAdvance(t1)), int(base), t2)
            p.end()

        def keyPressEvent(self, ev):
            if ev.key() == Qt.Key_Escape:
                self.app.shutdown()
                self.close()

        def mousePressEvent(self, ev):
            if ev.button() == Qt.LeftButton:
                self._drag = ev.globalPosition().toPoint() - self.frameGeometry().topLeft()
        def mouseMoveEvent(self, ev):
            if self._drag is not None and (ev.buttons() & Qt.LeftButton):
                self.move(ev.globalPosition().toPoint() - self._drag)
        def mouseReleaseEvent(self, _ev):
            self._drag = None

    win = _Chassis()
    win.show()
    win.raise_()
    win.activateWindow()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(_harness_main())
