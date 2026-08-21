#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
[DISCORD 30/07/2026] Liaison du compte Discord -- CLIENT (Qt).

Boite de dialogue + worker. Isole dans un module a part plutot qu'ajoute
aux 19 000 lignes de circusvoip_client.py : c'est un ecran ponctuel, vu
une fois par joueur et par machine, sans interaction avec le reste de
l'UI.

--- Ce que le joueur voit ---

  1. Il clique "Relier mon compte Discord". Le navigateur s'ouvre.
  2. Il autorise, revient, c'est termine.

Le PSEUDO n'est PAS demande ici (decision du 30/07) : la liaison
n'etablit que l'identite et attribue le numero. Le pseudo reste celui du
champ Nom du client, et son unicite est verifiee a la CONNEXION
(authenticate_join cote serveur) -- c'est le seul moment ou elle a un
sens, puisque c'est la qu'on entre en jeu sous un nom.

Ensuite, PLUS JAMAIS : le jeton local rendu par le serveur suffit aux
connexions suivantes. Discord n'est resollicite que si le joueur change
de machine ou perd sa config.

--- Connexion utilisee ---

Le worker ouvre sa PROPRE connexion WebSocket, courte, et la ferme a la
fin. Il ne passe pas par NetWorker : celui-ci envoie un "join" des
l'ouverture, or la liaison doit justement avoir lieu AVANT d'avoir un
compte avec lequel joindre. Les deux ne peuvent pas partager le meme
socket.
"""

from __future__ import annotations

import asyncio
import json

from PySide6.QtCore import Qt, QObject, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QDialogButtonBox,
)

import circusvoip_discord_auth as _discord

class _LinkWorker(QObject):
    """Execute l'echange complet dans un thread : WebSocket + navigateur.

    Tout est fait ici parce que le flux est bloquant par nature (on
    attend que le joueur clique dans son navigateur, jusqu'a 3 minutes).
    Le faire dans le thread UI figerait la fenetre.
    """

    sig_step = Signal(str)              # message d'avancement
    sig_done = Signal(dict)             # {"pseudo","numero","account_token"}
    sig_error = Signal(str)

    def __init__(self, server_ip: str, token: str, port: int):
        super().__init__()
        self._ip = server_ip
        self._token = token
        self._port = port

    # --- utilitaires ------------------------------------------------

    async def _open(self):
        import websockets
        from circusvoip_security import build_client_ssl_context_insecure
        uri = f"wss://{self._ip}:{self._port}"
        return await websockets.connect(
            uri, ssl=build_client_ssl_context_insecure())

    async def _ask(self, ws, payload: dict, expect: tuple[str, ...]) -> dict:
        """Envoie un message et attend une reponse d'un des types attendus.

        Les messages non attendus sont IGNORES et non traites comme des
        erreurs : le serveur peut pousser autre chose entre-temps.
        """
        await ws.send(json.dumps(payload))
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=20.0)
            try:
                data = json.loads(raw)
            except Exception:
                continue
            t = data.get("type")
            if t in expect:
                return data
            if t == "account_error":
                return data

    # --- liaison complete -------------------------------------------

    def run_link(self):
        try:
            asyncio.run(self._run_link_async())
        except Exception as e:
            self.sig_error.emit(str(e))

    async def _run_link_async(self):
        self.sig_step.emit("Connexion au serveur...")
        try:
            ws = await self._open()
        except Exception as e:
            self.sig_error.emit(f"Serveur injoignable : {e}")
            return
        try:
            # 1) Le client_id vient du SERVEUR et n'est pas code en dur :
            # changer d'application Discord ne demande alors aucune mise
            # a jour client.
            cfg = await self._ask(ws, {"type": "discord_config"},
                                  ("discord_config",))
            if cfg.get("type") == "account_error":
                self.sig_error.emit(cfg.get("message", "Liaison indisponible"))
                return
            client_id = cfg.get("client_id", "")
            port = int(cfg.get("redirect_port", _discord.REDIRECT_PORT))

            if not _discord.is_redirect_port_free(port):
                self.sig_error.emit(
                    f"Le port {port} est occupé. Fermez l'autre instance de "
                    "CircusVOIP puis réessayez.")
                return

            # 2) Navigateur. Bloquant : le joueur doit cliquer.
            self.sig_step.emit(
                "Autorisez CircusVOIP dans votre navigateur...")
            loop = asyncio.get_running_loop()
            try:
                code, verifier = await loop.run_in_executor(
                    None, lambda: _discord.start_link_flow(client_id, port))
            except _discord.DiscordAuthError as e:
                self.sig_error.emit(str(e))
                return

            # 3) Le SERVEUR echange le code : c'est lui qui detient le
            # secret, et c'est Discord qui affirme l'identite -- pas nous.
            self.sig_step.emit("Vérification auprès de Discord...")
            res = await self._ask(ws, {
                "type": "discord_link",
                "token": self._token,
                "code": code,
                "code_verifier": verifier,
            }, ("discord_linked",))

            if res.get("type") == "account_error":
                self.sig_error.emit(res.get("message", "Liaison refusée"))
                return
            self.sig_done.emit({
                "pseudo": res.get("pseudo", ""),
                "numero": res.get("numero"),
                "account_token": res.get("account_token", ""),
                "existing": bool(res.get("existing")),
            })
        except Exception as e:
            self.sig_error.emit(str(e))
        finally:
            try:
                await ws.close()
            except Exception:
                pass


class DiscordLinkDialog(QDialog):
    """Fenetre de liaison. Retourne le resultat via .result_data."""

    _sig_link = Signal()

    def __init__(self, server_ip: str, token: str, port: int,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Relier mon compte Discord")
        self.setModal(True)
        self.result_data: dict | None = None
        self._busy = False

        v = QVBoxLayout(self)
        v.setSpacing(10)

        intro = QLabel(
            "Votre compte Discord sert uniquement à vous identifier.\n"
            "Cette opération n'est demandée qu'une seule fois."
        )
        intro.setWordWrap(True)
        v.addWidget(intro)

        self.lbl_step = QLabel("")
        self.lbl_step.setWordWrap(True)
        v.addWidget(self.lbl_step)

        self.btn_link = QPushButton("Relier mon compte Discord")
        self.btn_link.clicked.connect(self._on_link)
        v.addWidget(self.btn_link)

        bb = QDialogButtonBox(QDialogButtonBox.Cancel)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)
        self._bb = bb

        # Worker dans son thread : le flux bloque jusqu'a 3 minutes.
        self._thread = QThread(self)
        self._worker = _LinkWorker(server_ip, token, port)
        self._worker.moveToThread(self._thread)
        self._sig_link.connect(self._worker.run_link)
        self._worker.sig_step.connect(self.lbl_step.setText)
        self._worker.sig_done.connect(self._on_done)
        self._worker.sig_error.connect(self._on_error)
        self._thread.start()

    # --- liaison -----------------------------------------------------

    def _on_link(self):
        if self._busy:
            return
        self._busy = True
        self.btn_link.setEnabled(False)
        self.lbl_step.setText("Ouverture du navigateur...")
        self._sig_link.emit()

    def _on_done(self, data: dict):
        self.result_data = data
        self.accept()

    def _on_error(self, msg: str):
        self._busy = False
        self.btn_link.setEnabled(True)
        self.lbl_step.setText(f"Échec : {msg}")

    # --- cycle de vie ------------------------------------------------

    def _stop_thread(self):
        """Arrete le worker. Idempotent : appele par tous les chemins de
        sortie, et un QThread deja arrete accepte quit()/wait()."""
        th = getattr(self, "_thread", None)
        if th is None:
            return
        try:
            th.quit()
            # Le worker peut etre au milieu d'un appel reseau : on laisse
            # le temps de rendre la main plutot que de terminer de force.
            th.wait(3000)
        except Exception:
            pass

    def done(self, r):
        """Sortie UNIQUE de la fenetre : accept(), reject() et la croix y
        passent tous.

        C'est ici et pas dans closeEvent qu'il faut arreter le thread :
        accept() ferme la fenetre SANS declencher closeEvent. Le thread
        survivait donc a la liaison reussie, et Qt detruisait un QThread
        encore en cours -> plantage du client juste apres le message
        "Compte relie" (constate le 30/07).
        """
        self._stop_thread()
        super().done(r)

    def closeEvent(self, ev):
        # Le thread ne doit pas survivre a la fenetre : sinon un serveur
        # HTTP local resterait a l'ecoute sur la machine du joueur.
        self._stop_thread()
        super().closeEvent(ev)
