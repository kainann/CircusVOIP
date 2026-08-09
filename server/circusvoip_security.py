# -*- coding: utf-8 -*-
"""
CircusVOIP - Module de securite partage
========================================
Logique de securite commune aux deux serveurs (positions 8888 et audio 8889).
Importe par circusvoip_server.py ET circusvoip_audio_server.py pour eviter
la duplication et garantir un comportement identique.

Contenu :
  - AuthLockout    : lockout brute-force par IP (anti-bruteforce)
  - RateLimiter    : quota de messages/seconde par client (anti-flood)
  - AuthRegistry   : registre d'auth partage entre processus via fichier
                     (point 4 : un client connu du serveur positions est
                     reconnu par le serveur audio, et inversement)
  - build_ssl_context : helper pour activer TLS/WSS cote serveur

Aucune dependance hors stdlib.
"""

import json
import os
import ssl
import time
from pathlib import Path


# =====================================================================
#  [P3 generalise] Lockout brute-force par IP
# =====================================================================

class AuthLockout:
    """Suit les echecs d'authentification par IP et bannit temporairement
    une IP qui depasse un seuil dans une fenetre glissante.

    Thread-safety : les deux serveurs sont mono-thread cote asyncio pour
    la logique reseau (un seul event loop traite les handlers), donc les
    acces a ces dicts se font tous depuis la meme task loop. Pas de lock
    necessaire tant que AuthLockout n'est utilise que depuis les handlers
    WebSocket. Ne PAS appeler ces methodes depuis un autre thread.
    """

    def __init__(self, max_failures: int = 5,
                 window_sec: int = 60,
                 ban_sec: int = 600):
        self.max_failures = max_failures
        self.window_sec = window_sec
        self.ban_sec = ban_sec
        self._failures: dict = {}   # ip -> [ts, ts, ...]
        self._banned: dict = {}     # ip -> ts_unban

    def is_banned(self, ip: str) -> bool:
        """True si l'IP est actuellement bannie. Purge le ban s'il a expire."""
        until = self._banned.get(ip)
        if until is None:
            return False
        if time.time() >= until:
            self._banned.pop(ip, None)
            self._failures.pop(ip, None)
            return False
        return True

    def record_failure(self, ip: str) -> bool:
        """Enregistre un echec d'auth pour cette IP.
        Retourne True si cet echec a declenche un ban (pour log cote appelant).
        """
        now = time.time()
        fails = self._failures.get(ip, [])
        # Ne garder que les echecs dans la fenetre glissante.
        fails = [t for t in fails if now - t < self.window_sec]
        fails.append(now)
        self._failures[ip] = fails
        if len(fails) >= self.max_failures:
            self._banned[ip] = now + self.ban_sec
            return True
        return False

    def record_success(self, ip: str):
        """Reset les compteurs apres une auth reussie."""
        self._failures.pop(ip, None)

    def failure_count(self, ip: str) -> int:
        """Nombre d'echecs actuellement comptabilises pour cette IP
        (utile pour les logs)."""
        now = time.time()
        fails = [t for t in self._failures.get(ip, [])
                 if now - t < self.window_sec]
        return len(fails)


# =====================================================================
#  [P6 nouveau] Rate limiting par client authentifie
# =====================================================================

class RateLimiter:
    """Token-bucket par client : limite le nombre de messages qu'un client
    DEJA authentifie peut envoyer par seconde. Protege contre un membre
    malveillant (ou un client bugge) qui floode le serveur de pos/audio.

    Un bucket se remplit a `rate` jetons/seconde, plafonne a `burst`.
    Chaque message consomme 1 jeton. Si le bucket est vide, le message
    doit etre rejete par l'appelant.

    Usage typique (cote handler, apres auth) :
        if not rl.allow(client_key):
            # trop de messages : on ignore ce message (ou on close)
            continue
    """

    def __init__(self, rate: float = 50.0, burst: float = 100.0):
        # rate  : messages/seconde en regime permanent
        # burst : reserve maximale (tolere une rafale courte)
        self.rate = float(rate)
        self.burst = float(burst)
        self._buckets: dict = {}   # key -> [tokens, last_refill_ts]

    def allow(self, key) -> bool:
        """Consomme 1 jeton pour `key`. True si autorise, False si quota
        depasse. `key` peut etre n'importe quoi de hashable (ws, ip, nom)."""
        now = time.time()
        bucket = self._buckets.get(key)
        if bucket is None:
            # Premier message : bucket plein moins le jeton consomme.
            self._buckets[key] = [self.burst - 1.0, now]
            return True
        tokens, last = bucket
        # Recharge proportionnelle au temps ecoule.
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1.0:
            bucket[0] = tokens
            bucket[1] = now
            return False
        bucket[0] = tokens - 1.0
        bucket[1] = now
        return True

    def forget(self, key):
        """Oublie un client (a appeler a la deconnexion pour ne pas
        accumuler des buckets morts en memoire)."""
        self._buckets.pop(key, None)

    def sweep(self, max_idle_sec: float = 300.0):
        """Purge les buckets inactifs depuis longtemps. A appeler
        periodiquement (ex: depuis la cleanup loop) si beaucoup de
        connexions ephemeres."""
        now = time.time()
        dead = [k for k, (_, last) in self._buckets.items()
                if now - last > max_idle_sec]
        for k in dead:
            self._buckets.pop(k, None)


# =====================================================================
#  [P7 nouveau] Registre d'auth partage entre les deux processus
# =====================================================================

class AuthRegistry:
    """Partage l'etat d'authentification entre le serveur positions et le
    serveur audio, qui sont deux processus distincts sans memoire commune.

    Principe : quand un client s'authentifie sur le serveur positions, il
    recoit (dans son welcome) un ticket aleatoire a courte duree de vie.
    Le serveur positions ecrit ce ticket dans un petit fichier JSON partage.
    Le client presente ensuite ce ticket au serveur audio, qui verifie sa
    presence dans le fichier. Resultat : on ne peut pas etre sur l'audio
    sans etre passe par le serveur positions d'abord.

    C'est volontairement simple (fichier local, pas de base de donnees) :
    les deux serveurs tournent sur la meme machine. Le fichier vit a cote
    des autres fichiers de config.

    Limites assumees :
      - Les deux processus doivent tourner sur la meme machine (meme FS).
      - Pas de revocation instantanee : un ticket reste valide jusqu'a son
        expiration (TTL court pour limiter la fenetre).
      - L'ecriture concurrente est geree par ecriture atomique (rename).
    """

    def __init__(self, path: Path, ttl_sec: float = 120.0):
        self.path = Path(path)
        self.ttl_sec = ttl_sec

    # ---- cote serveur positions : emission ----

    def issue(self, name: str, ticket: str, **extra):
        """Enregistre un ticket valide pour `name`. Appele par le serveur
        positions juste avant d'envoyer le welcome au client.

        Si un ou plusieurs tickets etaient deja actifs pour ce meme `name`
        (cas typique : reconnexion rapide avant que le 'finally' du
        precedent join ait revoque l'ancien ticket), ils sont
        automatiquement revoques avant d'enregistrer le nouveau. Cela
        evite qu'un thread audio fantome se reconnecte avec un ticket
        obsolete et entre en course avec le nouveau.

        Les kwargs supplementaires (ex: `can_broadcast=True`) sont stockes
        tels quels dans l'entree du ticket et lisibles cote serveur audio
        via verify_full(). Permet de propager des capabilities par-joueur
        sans schema fixe."""
        data = self._read_all()
        # Revoque les tickets actifs pour ce nom (cas reconnexion rapide).
        to_remove = [t for t, e in data.items()
                     if isinstance(e, dict) and e.get("name") == name]
        for t in to_remove:
            data.pop(t, None)
        data[ticket] = {
            "name": name,
            "expires": time.time() + self.ttl_sec,
            **extra,
        }
        self._prune(data)
        self._write_all(data)

    def revoke(self, ticket: str):
        """Invalide un ticket (a appeler quand le client se deconnecte du
        serveur positions). Best-effort."""
        if not ticket:
            return
        data = self._read_all()
        if ticket in data:
            data.pop(ticket, None)
            self._write_all(data)

    # ---- cote serveur audio : verification ----

    def verify(self, ticket: str):
        """Verifie un ticket presente au serveur audio. Retourne le nom
        associe si le ticket est valide et non expire, sinon None."""
        entry = self.verify_full(ticket)
        return None if entry is None else entry.get("name")

    def verify_full(self, ticket: str):
        """Comme verify() mais retourne l'entree complete (nom, expires,
        + tout champ supplementaire ajoute via issue(**extra)) ou None si
        invalide/expire. Utilise quand le serveur audio doit lire des
        capabilities par-joueur (ex: can_broadcast)."""
        if not ticket or not isinstance(ticket, str):
            return None
        data = self._read_all()
        entry = data.get(ticket)
        if entry is None:
            return None
        if time.time() >= entry.get("expires", 0):
            return None
        return entry

    # ---- interne ----

    def _read_all(self) -> dict:
        try:
            if self.path.exists():
                with open(self.path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            # Fichier corrompu / illisible : on repart d'un dict vide
            # plutot que de crasher. Au pire les clients en cours devront
            # se reauthentifier.
            pass
        return {}

    def _prune(self, data: dict):
        """Retire les tickets expires du dict (mutation en place)."""
        now = time.time()
        dead = [t for t, e in data.items()
                if now >= e.get("expires", 0)]
        for t in dead:
            data.pop(t, None)

    def _write_all(self, data: dict):
        """Ecriture atomique : on ecrit dans un fichier temporaire puis on
        rename (atomique sur le meme FS). Evite qu'un lecteur tombe sur un
        fichier a moitie ecrit."""
        try:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort : si l'ecriture echoue, l'audio refusera les
            # tickets recents, mais le serveur positions continue.
            pass


# =====================================================================
#  [P1 generalise] Helper TLS / WSS
# =====================================================================

def build_ssl_context(certfile: str, keyfile: str) -> ssl.SSLContext:
    """Construit un contexte SSL pour `websockets.serve(..., ssl=ctx)`.

    certfile / keyfile : chemins vers le certificat et la cle privee
    (format PEM). Pour un usage entre amis tu peux generer un certificat
    auto-signe ; les clients devront alors accepter ce certificat
    explicitement. Pour un usage propre, un reverse proxy (Caddy/nginx)
    qui gere Let's Encrypt reste l'option recommandee — dans ce cas tu
    n'utilises PAS cette fonction, le proxy fait le TLS et parle au
    serveur en clair sur localhost.

    Cette fonction est fournie pour le cas "tout en Python" si tu y tiens.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)
    # Pas de TLS < 1.2.
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


# =====================================================================
#  Generation automatique de certificat auto-signe
# =====================================================================
#
#  Pourquoi cette fonction ?
#  -------------------------
#  Pour que le serveur communique en wss:// (chiffre), il a besoin de
#  deux fichiers :
#    - cert.pem  : le "certificat" public (la carte d'identite du serveur)
#    - key.pem   : la cle privee correspondante (le secret du serveur)
#
#  Normalement, c'est l'admin qui les genere a la main avec openssl. Mais
#  pour un outil distribue au grand public via un installeur Inno, on ne
#  peut pas demander a chaque utilisateur d'ouvrir une console et de taper
#  des commandes openssl. Donc on les genere AUTOMATIQUEMENT au premier
#  lancement du serveur, et on les reutilise apres.
#
#  Limites assumees :
#  - Le certificat est "auto-signe" : il n'est pas valide pour les CA
#    publiques (Let's Encrypt, etc). Le client doit etre configure pour
#    l'accepter sans verification d'identite. Resultat : chiffrement OK,
#    mais pas d'authentification stricte du serveur. C'est suffisant
#    contre le sniffing passif, mais pas contre un MITM determine.
#  - Le certificat est valide 10 ans (3650 jours). Au-dela il expire et
#    il faudra le regenerer en supprimant cert.pem + key.pem.
#  - Si tu veux un VRAI certificat valide CA, mets un reverse proxy
#    Caddy/nginx devant et utilise build_ssl_context() avec ses fichiers.
#

def ensure_self_signed_cert(cert_path: Path,
                            key_path: Path,
                            common_name: str = "circusvoip",
                            days_valid: int = 3650) -> tuple:
    """Verifie que cert.pem et key.pem existent. Si non, les genere.

    Retourne un tuple (ok: bool, detail: str) :
      - (True, "")                    : tout OK
      - (True, "existing")            : fichiers deja presents, reutilises
      - (False, "ImportError: ...")   : lib cryptography absente -> il faut
                                        l'ajouter aux hiddenimports PyInstaller
      - (False, "PermissionError")    : dossier d'install non accessible
      - (False, "<autre>")            : autre erreur, message brut

    Le detail (str) permet a l'appelant d'afficher un message explicite
    plutot qu'un "Impossible de generer" generique. Tres utile pour
    debugger un build PyInstaller qui n'embarque pas cryptography.

    Parametres :
      cert_path   : chemin du fichier certificat (sera cree si absent)
      key_path    : chemin du fichier cle privee (sera cree si absent)
      common_name : nom inscrit dans le certificat (informatif, sans impact
                    fonctionnel pour un auto-signe)
      days_valid  : duree de validite du certificat en jours (defaut 10 ans)

    Effet de bord : ecrit deux fichiers sur disque si necessaire.
    """
    cert_path = Path(cert_path)
    key_path = Path(key_path)

    # Deja la ? Ne pas regenerer (sinon les clients existants devraient
    # re-accepter le nouveau cert a chaque demarrage du serveur).
    if cert_path.exists() and key_path.exists():
        return (True, "existing")

    # Tenter d'importer cryptography. Si absent, on ne peut pas generer.
    # PIEGE PyInstaller : la lib doit etre dans les hiddenimports, sinon
    # le .exe ne l'embarque pas et on tombe ici a chaque demarrage.
    try:
        from cryptography import x509
        from cryptography.x509.oid import NameOID
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from datetime import datetime, timedelta, timezone
    except ImportError as e:
        return (False, f"ImportError: {e}. La lib 'cryptography' n'est pas "
                       f"disponible. Si tu es en .exe PyInstaller, ajoute "
                       f"'cryptography' aux hiddenimports ou utilise "
                       f"--collect-all cryptography au build.")

    try:
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=2048,
        )

        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, common_name),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "CircusVOIP"),
        ])

        now = datetime.now(timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + timedelta(days=days_valid))
            .sign(private_key, hashes.SHA256())
        )

        cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        cert_path.write_bytes(cert_pem)

        key_pem = private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
        key_path.write_bytes(key_pem)

        return (True, "generated")

    except Exception as e:
        # On evite de laisser un fichier partiellement ecrit.
        try:
            if cert_path.exists():
                cert_path.unlink()
            if key_path.exists():
                key_path.unlink()
        except Exception:
            pass
        return (False, f"{type(e).__name__}: {e}")


def build_client_ssl_context_insecure() -> ssl.SSLContext:
    """Construit un contexte SSL CLIENT qui accepte les certificats
    auto-signes SANS verification.

    A utiliser cote client (websockets.connect(uri, ssl=ctx)) quand le
    serveur utilise un certificat auto-signe genere par
    ensure_self_signed_cert(). On ne peut PAS verifier l'identite du
    serveur dans ce cas (puisqu'aucune CA ne le signe), donc on
    desactive explicitement les checks.

    ATTENTION : ce contexte n'authentifie PAS le serveur. Il chiffre la
    connexion mais ne protege pas contre un attaquant qui se ferait
    passer pour le serveur (MITM). Pour ton cas (VoIP entre potes via
    un token partage), c'est acceptable : meme avec un MITM, l'attaquant
    aurait toujours besoin du token pour parler au vrai serveur derriere.

    Pour un usage plus serieux, utilise un certificat signe par une CA
    publique (Let's Encrypt via Caddy) et ssl.create_default_context()
    cote client.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx
