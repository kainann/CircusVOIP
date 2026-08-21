"""
circusvoip_opus.py — encapsulation du codec Opus pour CircusVOIP.

Pourquoi ce module
------------------
Jusqu'a la v0.3, une trame audio circulait en PCM float32 brut :
3840 octets pour 20 ms, quel que soit le contenu (silence compris).
Mesure du 26/07/2026 sur le serveur : 3841 o/trame flag inclus, soit
1536 kbit/s par parleur en flux continu. A 25 joueurs actifs le serveur
aurait pousse ~220 Mbit/s, ce qui rendait impossible la cible 50-100
joueurs.

Opus a 24 kbps ramene la trame a ~58 octets en voix et ~23 en silence.

Ou se place la frontiere codec / PCM
------------------------------------
Le codec est confine aux deux extremites du chemin reseau :

    capture -> [encode ICI] -> reseau -> [decode ICI] -> effet radio,
    mix proximite/radio/telephone, volumes, echo grotte, sortie

Tout le traitement interne continue de manipuler du float32, exactement
comme avant. On utilise volontairement l'API float d'Opus
(encode_float / decode_float) plutot que l'API int16 : ca evite un
aller-retour de conversion et garde le pipeline homogene.

Le serveur audio n'est PAS concerne : il relaie des octets opaques et
ne lit jamais le contenu des trames (verifie dans le code du 26/07).
Aucun deploiement VPS n'est necessaire pour ce chantier.

Chargement de la bibliotheque native
------------------------------------
opuslib est un binding ctypes qui appelle find_library('opus') AU
MOMENT DE L'IMPORT et leve une exception si la DLL est absente. Sous
Windows il n'y a pas d'opus.dll par defaut : on la distribue a cote des
modules du client (RELEASE_FILES) et on prepare le PATH ici avant
d'importer opuslib. C'est pour ca que ce module ne fait jamais
"import opuslib" au niveau du fichier.
"""

import os
import sys
import threading
from pathlib import Path

# ---------------------------------------------
#  Parametres — alignes sur circusvoip_audio_io
# ---------------------------------------------
SAMPLE_RATE = 48000
CHANNELS    = 1
BLOCK_SIZE  = 960              # 20 ms a 48 kHz
PCM_BYTES   = BLOCK_SIZE * 4   # 3840 octets en float32

# Debit cible. Opus est en VBR : c'est une moyenne, pas un plafond dur.
#
# Le premier essai a 24000 a ete juge moins bon que le float32 a l'oreille,
# ce qui est attendu : 24 kbps est le bas de la fourchette Opus. Mesure du
# compromis sur une voix synthetique (o/trame, flag inclus) :
#     24k ->  60 o  (x64 vs float32)
#     32k ->  82 o  (x47)
#     48k -> 124 o  (x31)
#     64k -> 164 o  (x23)
#     96k -> 241 o  (x16)
# Opus atteint la transparence perceptuelle sur une voix mono vers 64 kbps :
# au-dela on paie du debit sans rien gagner a l'oreille.
#
# 64000 est retenu : valide a l'oreille sur la radio le 29/07/2026. La valeur
# etait restee a 48000 jusqu'au 02/08/2026 par erreur d'edition, donc les
# builds 63 a 68 ont tous tourne a 48k, y compris les tests audio du b67.
#
# Surcharge possible sans toucher au code, pratique pour comparer deux
# clients lances cote a cote :
#     set CIRCUSVOIP_OPUS_BITRATE=96000
def _env_int(nom, defaut):
    try:
        v = int(os.environ.get(nom, ""))
        return v if v > 0 else defaut
    except Exception:
        return defaut


BITRATE = _env_int("CIRCUSVOIP_OPUS_BITRATE", 64000)

# Profil d'encodage. "voip" optimise l'intelligibilite de la parole et
# coupe plus franchement ce qui l'entoure ; "audio" rend un son plus
# large et plus naturel, souvent juge meilleur sur une voix de casque.
# A comparer a l'oreille :  set CIRCUSVOIP_OPUS_APP=audio
APPLICATION = (os.environ.get("CIRCUSVOIP_OPUS_APP", "voip") or "voip").lower()

# Complexite de l'encodeur, 0 a 10. Le cout mesure est de 0,15 ms par
# trame sur un budget de 20 ms : autant prendre le maximum de qualite.
COMPLEXITY = _env_int("CIRCUSVOIP_OPUS_COMPLEXITY", 10)

# Taille max d'un paquet encode, passee a l'encodeur comme borne de
# securite. 4000 est la valeur recommandee par la doc Opus.
_MAX_PACKET = 4000

_BASE_DIR = Path(__file__).resolve().parent

# Etat du module. _available reste None tant qu'on n'a pas tente
# l'initialisation (elle est paresseuse : inutile de charger la DLL si
# le client tourne sans audio).
_available = None
_reason    = ""
_opuslib   = None
_enc_api   = None
_dec_api   = None
_ctl       = None

# [SONNERIE 05/08/2026] Un encodeur PAR FLUX, et non plus un seul.
#
# Opus est un codec a etat : chaque trame depend de la precedente. Tant
# que les trames emises sous plusieurs flags portaient le MEME contenu
# (voix radio 0x01 + la meme voix en proximite 0x00), un encodeur unique
# suffisait -- core encodait une fois et collait deux flags dessus.
#
# La sonnerie sur son propre flag change ca : voix et sonnerie sont deux
# contenus DIFFERENTS au meme instant. Les alterner dans un encodeur
# unique le desynchroniserait, exactement le symptome corrige cote
# DECODAGE le 31/07 en indexant sur (emetteur, flag). C'est ici la
# symetrie de ce correctif.
#
# Cle None = flux par defaut (voix), donc comportement inchange pour tout
# le code existant.
_encoders     = {}
_encoder_lock = threading.Lock()

# Un decodeur par emetteur : Opus est un codec a etat, deux flux ne
# peuvent pas partager le meme decodeur sans se corrompre mutuellement.
_decoders      = {}
_decoders_lock = threading.Lock()


def _prepare_native_lookup():
    """Rend opus.dll trouvable par ctypes si elle est livree avec le client.

    Sous Windows, find_library('opus') cherche 'opus.dll' dans le PATH.
    On ajoute le dossier du module (la ou l'updater depose les fichiers)
    avant l'import d'opuslib. Sans effet sur Linux, ou la bibliotheque
    vient du systeme.
    """
    if os.name != "nt":
        return
    try:
        if not list(_BASE_DIR.glob("opus*.dll")):
            return
        os.environ["PATH"] = str(_BASE_DIR) + os.pathsep + os.environ.get("PATH", "")
        # Python 3.8+ : le PATH seul ne suffit plus pour les DLL locales.
        try:
            os.add_dll_directory(str(_BASE_DIR))
        except Exception:
            pass
    except Exception:
        pass


def _init():
    """Initialise le codec. Idempotent, sans exception vers l'appelant.

    En cas d'echec on memorise la raison : elle sera affichee une fois
    dans les logs plutot que de faire crasher la boucle audio a chaque
    trame.
    """
    global _available, _reason, _opuslib, _enc_api, _dec_api, _ctl
    if _available is not None:
        return _available

    _prepare_native_lookup()
    try:
        # opuslib-next fournit un wheel py3-none-any (donc installable
        # par l'updater sans compilation) ; opuslib n'existe qu'en
        # archive source. On accepte les deux, l'API est identique.
        try:
            import opuslib_next as _ol
            import opuslib_next.api.encoder as _e
            import opuslib_next.api.decoder as _d
            import opuslib_next.api.ctl as _c
        except ImportError:
            import opuslib as _ol
            import opuslib.api.encoder as _e
            import opuslib.api.decoder as _d
            import opuslib.api.ctl as _c
    except Exception as exc:
        _available = False
        _reason = f"{type(exc).__name__}: {exc}"
        return False

    _opuslib, _enc_api, _dec_api, _ctl = _ol, _e, _d, _c
    _available = True
    _reason = ""
    return True


def is_available() -> bool:
    """True si le codec est utilisable."""
    return _init()


def unavailable_reason() -> str:
    """Message d'erreur si is_available() est faux. Vide sinon."""
    _init()
    return _reason


# ---------------------------------------------
#  Emission
# ---------------------------------------------

def _get_encoder(cle=None):
    """Encodeur du flux 'cle'. Cree a la demande, un par flux.

    A appeler sous _encoder_lock.
    """
    _encoder = _encoders.get(cle)
    if _encoder is None:
        # [SONNERIE 06/08/2026] Le flux "ring" est encode en mode MUSIQUE,
        # pas en mode voix.
        #
        # Une sonnerie de telephone est un signal TONAL : quelques
        # frequences pures qui tiennent, sans les formants ni les
        # transitoires de la parole. Un encodeur regle en VOIP +
        # SIGNAL_VOICE alloue ses bits pour la voix et rend ce genre de
        # signal metallique. Tant que la sonnerie etait sommee a la voix,
        # le masquage rendait l'artefact inaudible ; separee dans son
        # propre flux, elle s'entend seule et le defaut ressort.
        #
        # APPLICATION_AUDIO + SIGNAL_MUSIC est le reglage prevu par Opus
        # pour ce cas. Il ne coute rien en debit -- meme BITRATE -- et ne
        # touche QUE ce flux : la voix garde son reglage.
        _ring = (cle == "ring")
        if _ring:
            app = _opuslib.APPLICATION_AUDIO
        else:
            app = (_opuslib.APPLICATION_AUDIO if APPLICATION == "audio"
                   else _opuslib.APPLICATION_VOIP)
        _encoder = _enc_api.create_state(SAMPLE_RATE, CHANNELS, app)
        # Sans reglage explicite du debit, Opus encode bien plus gros
        # (~216 o/trame mesure). Chaque reglage est tente separement :
        # si l'un echoue, les autres doivent quand meme s'appliquer.
        for ctl_name, valeur in (("set_bitrate",    BITRATE),
                                 ("set_complexity", COMPLEXITY),
                                 ("set_signal",     None)):
            try:
                if ctl_name == "set_signal":
                    # Indique a l'encodeur la nature du signal : il alloue
                    # mieux ses bits. MUSIC pour la sonnerie (tonale),
                    # VOICE pour tout le reste.
                    if _ring:
                        _enc_api.encoder_ctl(_encoder, _ctl.set_signal,
                                             _opuslib.SIGNAL_MUSIC)
                    elif APPLICATION != "audio":
                        _enc_api.encoder_ctl(_encoder, _ctl.set_signal,
                                             _opuslib.SIGNAL_VOICE)
                    continue
                _enc_api.encoder_ctl(_encoder, getattr(_ctl, ctl_name), valeur)
            except Exception:
                pass
        _encoders[cle] = _encoder
    return _encoder


def set_bitrate(bps: int):
    """Change le debit a chaud. L'encodeur est recree au prochain envoi.

    Sert a comparer deux reglages sans relancer le client.
    """
    global BITRATE
    BITRATE = int(bps)
    with _encoder_lock:
        # Tous les flux, pas seulement la voix : un flux oublie ici
        # continuerait d'encoder a l'ancien debit sans que rien ne le dise.
        _encoders.clear()


def encode(frame_np, cle=None) -> "bytes | None":
    """Encode une trame float32 de BLOCK_SIZE samples.

    `cle` identifie le FLUX. Deux contenus differents emis au meme instant
    (voix et sonnerie de telephone) doivent passer par des encodeurs
    distincts, sans quoi l'etat Opus se desynchronise. cle=None = flux
    voix, comportement historique.

    Retourne le paquet Opus, ou None si le codec est indisponible ou si
    l'encodage echoue (l'appelant doit alors ne rien emettre plutot que
    d'envoyer du PCM brut : les recepteurs v0.4 ne sauraient pas le lire).
    """
    if not _init():
        return None
    try:
        data = frame_np.tobytes()
        if len(data) != PCM_BYTES:
            return None
        with _encoder_lock:
            return _enc_api.encode_float(_get_encoder(cle), data,
                                         BLOCK_SIZE, _MAX_PACKET)
    except Exception:
        return None


# ---------------------------------------------
#  Reception
# ---------------------------------------------

def _cle(sender: str, flag=None):
    """[OPUS 31/07/2026] Cle d'un decodeur : (emetteur, flag).

    Auparavant indexee sur le seul emetteur. Or core envoie la MEME
    trame encodee sous plusieurs flags a la fois : PTT radio (0x01) +
    proximite (0x00) quand quelqu'un est a portee, appel (0x03) +
    proximite, etc. Un destinataire a la fois sur le canal ET a moins de
    30 m recevait donc DEUX copies de chaque trame, poussees dans le
    MEME decodeur.

    Opus est un codec a etat : chaque trame depend de la precedente.
    Lui donner deux fois le meme paquet, ou alterner deux contenus
    differents, le desynchronise -> son degrade et sacade. Symptome
    constate le 31/07 : radio sale quand l'emetteur est proche, propre
    quand il est loin (une seule copie arrive alors).

    Present depuis le passage a Opus le 26/07. Invisible avant, le PCM
    brut etant sans etat : recevoir deux fois la meme trame ne genait
    personne.

    flag=None garde l'ancien comportement, pour les appelants qui ne le
    fournissent pas encore.
    """
    return (sender, flag)


def _get_decoder(sender: str, flag=None):
    cle = _cle(sender, flag)
    with _decoders_lock:
        dec = _decoders.get(cle)
        if dec is None:
            dec = _dec_api.create_state(SAMPLE_RATE, CHANNELS)
            _decoders[cle] = dec
        return dec


def decode(sender: str, packet: bytes, flag=None) -> "bytes | None":
    """Decode un paquet Opus en PCM float32 brut (PCM_BYTES octets).

    `sender` et `flag` identifient le flux : Opus est un codec a etat, et
    un meme emetteur peut alimenter PLUSIEURS flux simultanes (radio +
    proximite, appel + proximite). Chacun a besoin de son propre
    decodeur -- cf _cle().

    Garde de securite : un paquet de exactement PCM_BYTES octets est du
    float32 d'un client v0.3 qui n'a pas migre. On le rejette au lieu de
    le passer a Opus, qui produirait du bruit fort. L'appelant logge une
    fois et ignore la trame.
    """
    if not _init():
        return None
    if not packet:
        return None
    if len(packet) == PCM_BYTES:
        return None
    try:
        return _dec_api.decode_float(_get_decoder(sender, flag), packet,
                                     len(packet), BLOCK_SIZE, False,
                                     channels=CHANNELS)
    except Exception:
        return None


def decode_lost(sender: str, flag=None) -> "bytes | None":
    """Genere une trame de remplacement pour un paquet perdu (PLC Opus).

    Opus reconstruit une trame plausible a partir de son etat interne,
    ce qui sonne nettement mieux que le PLC maison (qui rejouait la
    derniere trame telle quelle). N'a de sens que si ce sender a deja
    decode au moins une trame.
    """
    if not _init():
        return None
    try:
        with _decoders_lock:
            dec = _decoders.get(_cle(sender, flag))
        if dec is None:
            return None
        return _dec_api.decode_float(dec, None, 0, BLOCK_SIZE, False,
                                     channels=CHANNELS)
    except Exception:
        return None


def drop_sender(sender: str):
    """Libere le decodeur d'un emetteur qui s'est deconnecte.

    Sans ca, le dict grossirait a chaque join/leave sur une longue
    session, et un joueur qui se reconnecte reprendrait un decodeur
    portant l'etat de sa session precedente.
    """
    with _decoders_lock:
        # Tous les flux de cet emetteur, quel que soit le flag : la cle
        # est un couple depuis le 31/07.
        for cle in [k for k in _decoders if k[0] == sender]:
            _decoders.pop(cle, None)


def reset():
    """Remet le codec a zero (changement de serveur, reconnexion audio).

    [SONNERIE 05/08/2026] Vide _encoders, et non plus une globale
    _encoder devenue fantome. Le passage a un encodeur PAR FLUX avait
    laisse cette fonction derriere : elle remettait a None une variable
    que plus personne ne lisait, et les encodeurs reels survivaient a la
    reinitialisation avec l'etat de la session precedente. Exactement le
    defaut que drop_sender() evite cote decodage.
    """
    with _encoder_lock:
        _encoders.clear()
    with _decoders_lock:
        _decoders.clear()


def stats() -> dict:
    """Petit etat pour les logs / la page Reglages."""
    _init()
    with _decoders_lock:
        n = len(_decoders)
    return {
        "available":  bool(_available),
        "reason":     _reason,
        "bitrate":    BITRATE,
        "application": APPLICATION,
        "complexity": COMPLEXITY,
        "decoders":   n,
        "pcm_bytes":  PCM_BYTES,
    }
