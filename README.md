# CircusVOIP

VOIP de proximité privée pour Star Citizen. La voix des joueurs autour
de vous s'entend plus ou moins fort selon leur distance dans le jeu,
calculée à partir de la position lue par OCR sur l'écran du joueur.

## Pour les joueurs

La plupart des joueurs ont uniquement besoin de l'installeur **client** :

➡️ **[Télécharger CircusVOIP_Client_Setup_v0.2.0.exe](https://github.com/kainann/CircusVOIP/releases/tag/client-v0.2.0)**

Lancez l'installeur, configurez votre micro, configurez la zone OCR,
définissez les raccourcis radio et profil, connectez-vous au serveur de
votre groupe et c'est parti.

⚠️ **Le serveur de votre groupe doit être en v0.2** pour profiter des
nouveautés du client v0.2 (CircusPhone, soundboard, photos de profil).
Mettez à jour le serveur en même temps que le client.

## Pour le serveur

Un groupe a besoin d'**un seul serveur**, hébergé soit :

### Sur le PC d'un joueur (le plus simple)

➡️ **[Télécharger CircusVOIP_Server_Setup_v0.2.0.exe](https://github.com/kainann/CircusVOIP/releases/tag/server-v0.2.0)**

Lancez l'installeur, le serveur démarre et génère un mot de passe
automatiquement. Communiquez l'IP de la machine + le mot de passe aux
joueurs.

⚠️ **Ouvrir les ports 8888 et 8889 sur Internet expose le serveur aux
scans automatiques.** Le code utilise un certificat TLS auto-signé
généré au premier lancement (chiffrement actif par défaut), mais si tu
veux limiter l'exposition, héberger sur un VPS dédié (3-5 €/mois) est
plus prudent que sur ton PC perso.

### Sur une machine dédiée (VPS, PC secondaire, etc.)

**Python 3.10 ou supérieur requis.** Cloner le repo et lancer les
sources :

```bash
git clone https://github.com/kainann/CircusVOIP.git
cd CircusVOIP/server
pip install -e .                  # installe websockets + cryptography
python circusvoip_server.py       # serveur positions (port 8888, wss://)
python circusvoip_audio_server.py # serveur audio (port 8889, wss://)
```

Sur un VPS Linux sans GUI, ajouter `--headless` aux deux commandes
pour eviter la dependance a tkinter.

Le mot de passe d'accès est généré automatiquement au premier
lancement dans `circusvoip_server_config.json`. Communiquez-le aux
joueurs avec l'IP publique de la machine.
Le certificat TLS (`cert.pem` et `key.pem`) est également généré
automatiquement au premier lancement et réutilisé ensuite.

### Avec Docker (serveur)

Le serveur peut être lancé en conteneurs Docker. Depuis `server/` :

```bash
docker compose up -d
```

Les deux serveurs (positions et audio) tournent dans des conteneurs
distincts qui partagent un volume Docker nommé `state`. Le
certificat TLS, le token joueur, le token admin et les tickets
d'auth y sont stockés et persistent entre redémarrages.

Pour récupérer le mot de passe et le token admin générés au premier
lancement :

```bash
docker compose exec positions cat /data/circusvoip_server_config.json
docker compose exec positions cat /data/circusvoip_admin_token.json
```

> ℹ️ **Sous Git Bash (Windows)**, MSYS réécrit les chemins commençant
> par `/` et les commandes ci-dessus échouent avec
> `cat: 'C:/Program Files/Git/data/...': No such file or directory`.
> Préfixer avec `MSYS_NO_PATHCONV=1` ou lancer depuis PowerShell, CMD
> ou WSL. Pas de souci sur Linux (la cible normale du déploiement
> Docker).

## Configuration requise

- **Windows 10 ou 11** (côté client uniquement ; le serveur tourne
  aussi sous Linux dans Docker)
- **Python 3.10 ou supérieur** pour les installations à partir des
  sources
- **Star Citizen** lancé pour l'OCR (le client peut tourner sans, mais
  sans position ni proximité ni radio fonctionnels)
- **Un micro**
- **Carte graphique NVIDIA recommandée** : l'OCR (EasyOCR) tourne
  nettement plus vite avec CUDA. Sans GPU NVIDIA, ça fonctionne en CPU
  mais c'est plus lent et plus gourmand.

### Dépendances Python

- **Serveur** : `websockets`, `cryptography`.
- **Client** : `PySide6`, `websockets`, `numpy`, `scipy`, `mss`,
  `opencv-python`, `easyocr` (qui installe `torch` automatiquement),
  `sounddevice`, `cryptography`, `pynput`.

Les dépendances exactes sont déclarées dans `server/pyproject.toml`
et `client/pyproject.toml`. Pour l'installation en mode développement
côté client : `pip install -e client/`.

## Fonctionnalités

### Audio de proximité
Vous entendez les autres joueurs en fonction de leur distance dans Star
Citizen : proche = fort, lointain = faible, inaudible au-delà d'une
portée réglable. La position est lue par OCR sur le HUD du jeu.

### CircusPhone — messagerie et appels intégrés
Un téléphone virtuel s'ouvre en surimpression par-dessus Star Citizen.
Il permet d'**appeler** et d'**envoyer des messages écrits** aux autres
joueurs, directement depuis le jeu.

- **Navigation entièrement au clavier** (flèches directionnelles +
  Entrée) : la souris reste captée par Star Citizen quand l'overlay est
  ouvert, vous gardez donc le contrôle de votre personnage. Les flèches
  naviguent dans les contacts et les conversations, Entrée valide, Échap
  revient en arrière.
- **Conversations privées** avec historique horodaté.
- **Tri intelligent des contacts** : les conversations les plus récentes
  remontent en haut de la liste.
- **Annuaire automatique** : les joueurs connectés au serveur en même
  temps que vous sont ajoutés à vos contacts.
- **Sonneries** lors des appels et notification sonore à la réception
  d'un message.

### Photos de profil
Vous pouvez associer une photo à votre profil. Elle s'affiche en face de
votre pseudo pour les autres joueurs, qui la reçoivent automatiquement.

### Soundboard
Une planche de sons permet de déclencher un effet audio diffusé aux
autres joueurs. **Un seul son est disponible pour le moment** (alarme) ;
d'autres pourront être ajoutés par la suite. L'admin peut autoriser ou
non chaque profil à utiliser le soundboard.

### Radio par canaux
Communication longue distance par-dessus la proximité.
L'admin du serveur crée des canaux (ex : « Combat », « Marchand »,
« Général »), chaque joueur en choisit un, et la **touche radio (PTT)**
émet vers tous les joueurs du même canal, peu importe la distance en
jeu. Une touche dédiée permet aussi de **cycler entre les canaux**
rapidement en plein combat.

### Profils
L'admin peut créer et assigner à chaque joueur un **profil** (ex :
« Pilote », « Mineur », « Pirate », « Modo »). Le profil sert à deux
choses :

- **Identification** : vous voyez qui est qui dans la liste des joueurs.
- **Radio par profil** : une touche dédiée émet vers tous les joueurs
  ayant le même profil que vous, peu importe leur canal et leur position.

### Diffusion globale (rôle broadcaster)
Un joueur à qui l'admin a accordé le rôle **broadcaster** dispose d'une
touche PTT supplémentaire qui diffuse sa voix sur **tous les canaux
radio simultanément**, peu importe le canal des destinataires. Utile
pour les modérateurs et organisateurs d'événements RP qui doivent
adresser tout le serveur sans imposer un canal commun.

L'admin gère la liste via la console admin (commandes
`grant_broadcaster <nom>` / `revoke_broadcaster <nom>` /
`list_broadcasters`). La capability est embarquée dans le ticket
d'authentification audio : la révocation devient effective au prochain
ticket (≤ 2 min en pratique). Un client connecté à un serveur trop
ancien (sans `server_caps: ["broadcast_all"]` dans le welcome) voit
simplement la touche sans effet — aucune trame n'est émise.

### Mode RP (Roleplay)
Quand activé, le filtre radio est appliqué sur la voip de proximité
**uniquement si les deux joueurs portent leur casque dans Star
Citizen**. Si l'un des deux porte un casque, le filtre radio est
toujours là, il faut que vous et le joueur en face de vous ne portiez
pas de casque pour enlever le filtre radio.
La détection du casque se fait par OCR du HUD et lecture du gamelog.

Mode RP OFF : filtre radio seulement sur les communications radio et
profil.

### Masque OCR intelligent
Le client détecte désormais quand votre mobiGlas ou le menu options est
ouvert et adapte la capture en conséquence, ce qui évite les faux
relevés de position pendant ces moments.

### Overlays
Petites fenêtres flottantes par-dessus Star Citizen pour donner des
informations sur CircusVOIP.
Il faut d'abord passer par **Overlay Edition** pour déplacer,
redimensionner et activer les fenêtres souhaitées.

- **Mutes** : état Micro / Proximité / Radio
- **Channel** : canal radio actuel et profil
- **Prox range** : portée de proximité en mètres

Indispensable en fullscreen pour avoir l'info sans alt-tab. Position et
taille sont sauvegardées entre les sessions.

### Mode anonyme (Écran serveur ou Admin)
Permet de masquer son pseudo aux autres joueurs (utile pour des
événements RP).

## Prise en main

### Premier lancement
1. Installez le client et lancez-le.
2. Saisissez l'**adresse IP** et le **mot de passe** du serveur de votre groupe.
3. Choisissez votre **micro** et votre **sortie audio** dans l'onglet Audio.
4. Lancez Star Citizen.

### Calibration de l'OCR
Au premier lancement, définissez la zone soit de façon auto, soit en la
calibrant à la main. **Obligatoire pour faire fonctionner la VOIP.**

Pour calibrer manuellement, vous devez prendre la 1ʳᵉ ligne du
`r_displayinfo 1` comme l'image ci-dessous :

![Zone OCR correcte](docs/ocr_ok.png)
![Zone OCR incorrecte](docs/ocr_ko.png)

Il faut prendre une zone vide sur la gauche pour anticiper les zones avec les noms longs, conseillé de prendre un peu plus que la longueur du solarsystem

### Touches radio et profil
Dans les paramètres du client, définissez :

- **Touche radio (PTT)** : à maintenir pour parler sur votre canal radio
- **Touche profile radio** : à maintenir pour parler à tous les joueurs
  de votre profil
- **Touche diffusion globale (PTT)** : à maintenir pour diffuser à tous
  les canaux (réservée aux broadcasters — sans le rôle, la touche reste
  silencieuse côté serveur)
- **Cycle channel key** : pour changer rapidement de canal sans alt-tab

N'importe quelle touche ou combinaison de 2 touches clavier ou bouton
souris peut être assignée.

## Architecture

Le serveur expose deux services WebSocket distincts :

```
                    ┌────────────────────────┐
                    │      Serveur           │
   ┌──────────┐     │  ┌──────────────────┐  │
   │ Client A │ ◀─▶│  │ Positions  :8888 │  │
   │          │     │  └──────────────────┘  │
   │          │     │  ┌──────────────────┐  │
   │          │ ◀─▶│  │ Audio      :8889 │  │
   └──────────┘     │  └──────────────────┘  │
                    └────────────────────────┘
   ┌──────────┐             ▲    ▲
   │ Client B │ ◀───────────┘───┘
   └──────────┘
```

- **Port 8888** : serveur de positions. Chaque client envoie sa
  position (lue par OCR) et reçoit celles des autres joueurs. Gère
  aussi les canaux radio, les profils joueurs, le CircusPhone (appels et
  messages), les photos de profil et la console admin.
- **Port 8889** : serveur audio. Relais des trames audio PCM (48 kHz,
  mono, float32) entre clients. Le volume de chaque pair est calculé
  localement par chaque client en fonction de la distance.

Les deux serveurs partagent le même token d'authentification stocké
dans `circusvoip_server_config.json` (généré au premier lancement).

## Crédits

Projet de **Kainan** ([@kainann](https://github.com/kainann)) —
développement assisté par Claude IA ([Anthropic](https://www.anthropic.com)).
