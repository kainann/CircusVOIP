# CircusVOIP — 0.4.0 Alpha Build 67

**Statut** : préparé — non publié
**Type** : correctifs client **+ déploiement serveur**

> Build de correction. Il répare trois défauts introduits ou révélés par les
> builds 65 et 66, dont deux qui rendaient le client inutilisable pour un
> nouveau joueur.

---

## Corrections importantes

### 🎙 La radio ne se dégrade plus quand l'interlocuteur est proche

- Une même trame de parole est envoyée **sous plusieurs canaux à la fois** :
  radio *et* proximité quand un auditeur est à portée, appel *et* proximité
  pendant une conversation téléphonique. Le destinataire recevait donc deux
  copies et les traitait comme un flux unique — ce qui désynchronisait le
  décodage en permanence.
- Symptôme : **radio sale quand l'émetteur est proche, nette quand il est
  loin** (une seule copie arrivait alors). Même chose pour la proximité
  pendant un appel, et pour les sonneries.
- Présent depuis le passage à Opus le 26/07, invisible avant : l'ancien format
  audio n'avait pas de mémoire, recevoir deux fois la même trame ne dérangeait
  rien.
- Chaque flux a désormais son propre décodeur.

### 🔈 Un périphérique audio disparu ne bloque plus tout le client

- Si le micro ou la sortie enregistrés n'existent plus au lancement — casque
  éteint, périphérique renommé par le pilote, nom tronqué différemment — le
  client **retombe automatiquement** sur le périphérique par défaut de Windows,
  puis sur le premier disponible.
- Avant, il n'en sélectionnait aucun et n'essayait même pas : le champ restait
  sur *(aucun)*, l'audio ne démarrait pas — et **ni l'OCR, ni la position, ni
  le téléphone, ni la proximité** ne démarraient non plus, pour une raison
  purement audio.
- Ces fonctions démarrent maintenant **dans tous les cas**. Un joueur sans
  audio apparaît sur la carte et peut utiliser son téléphone.

### 🔌 Un casque débranché en cours de partie se signale

- Nouveau message rouge **AUDIO PERDU** dans la barre du haut quand un
  périphérique cesse de répondre en cours de session.
- Rien n'est bloqué : le joueur reste connecté, garde sa position, sa radio et
  son téléphone. Le message disparaît de lui-même si le matériel revient.
- Jusqu'ici, un casque débranché rendait simplement muet, sans aucun signal :
  on croyait que plus personne ne parlait.
- Aucune reconnexion automatique — rouvrir un flux pendant que le joueur parle
  serait pire que le problème. L'infobulle indique la marche à suivre.

---

## Améliorations

### 📞 Sonnerie : le volume choisi est enfin respecté

- La sonnerie diffusée aux joueurs autour suit désormais **votre réglage de
  volume**. Baisser sa sonnerie la baisse aussi pour les voisins.
- Avant, l'émission partait du son brut et ignorait ce réglage : on pouvait
  avoir une sonnerie discrète chez soi et forte pour tout le monde autour, sans
  moyen de s'en rendre compte.
- Niveau de diffusion abaissé de **70 % à 50 %** : les 70 % avaient été validés
  au mannequin, seul en cabine ; en session réelle, avec de la voix et du bruit
  ambiant, c'était trop fort.

### 🏷 Le numéro de build s'affiche dans le titre

- La fenêtre indique `CircusVOIP Client — 0.4.0 Build 67`. Auparavant le build
  n'apparaissait que si un canal était renseigné : impossible de savoir, sur la
  capture d'écran d'un testeur, quelle version il fait tourner — alors que
  c'est la première question qu'on se pose.

### 🔗 Textes de liaison Discord

- Les mentions du numéro de téléphone ont été retirées de la fenêtre de liaison
  et du message de refus : Discord sert à **identifier**, le numéro relève de
  l'annuaire et arrive à la connexion.

---

## Sécurité

### 🔐 Les comptes de service exigent un secret dédié

- La création d'un compte sans Discord — réservée à l'outillage — se
  contentait du **mot de passe joueur**, que tous les joueurs connaissent.
  N'importe lequel d'entre eux pouvait donc se fabriquer des comptes et
  contourner entièrement la liaison obligatoire.
- Un secret distinct (`service_secret`) est désormais requis, en plus du
  drapeau d'activation. Deux verrous plutôt qu'un.

### 🚫 Retrait de la vérification de pseudo à distance

- Le message `pseudo_check` permettait de tester en boucle l'existence d'un
  pseudo, donc d'énumérer les joueurs. Il n'avait plus d'usage depuis que le
  pseudo se valide à la connexion.

---

## Notes internes

- **Décodeurs indexés sur `(émetteur, flag)`** dans `circusvoip_opus.py`, au
  lieu de l'émetteur seul. `drop_sender()` purge tous les flags d'un émetteur —
  sinon une reconnexion hériterait de l'état des flux non nettoyés.
  `decode_lost()` prend le flag ; `audio_io` mémorise celui du dernier flux reçu
  (`_remote_last_flag`), faute de quoi le PLC natif ne trouverait plus aucun
  décodeur et retomberait toujours sur le rejeu maison, perdant le gain du
  26/07. Approximation assumée pour un émetteur qui alterne deux flux : le PLC
  vise le dernier reçu. `core` et `audio_io` retombent sur l'ancienne signature
  en cas de déploiement partiel — mieux vaut du son dégradé que pas de son.
- **Repli de périphérique** : le test `findText(...) >= 0` échouait
  silencieusement et le repli vivait dans le `else`, donc n'était **jamais**
  atteint quand un libellé était enregistré mais introuvable. Remplacé par un
  drapeau explicite. Une ligne de log indique ce qui était cherché.
- **`check_devices()`** est un capteur, pas un correcteur : il rend la liste des
  flux inactifs et ne tente rien. Appelé depuis le tick du VU-mètre mais espacé
  à 2 s, et l'affichage n'est mis à jour que sur changement d'état.
  Cas non couvert : un périphérique qui reste *actif* mais ne délivre plus rien
  (pilote figé, Bluetooth en veille) — il faudrait compter les trames.
- **`PHONE_RING_TX_FACTOR`** s'applique désormais **par-dessus**
  `_phone_ring_volume_factor`. Conséquence : le mannequin ne peut plus mettre
  son volume local à 0 pour ne pas s'entendre, il deviendrait muet pour tout le
  monde. Nouveau mode `set_phone_ring_tx_only()`.
- **Jetons du mannequin** indexés par nom de service (`account_tokens`) : avec
  une clé unique, deux mannequins lancés depuis le même dossier s'écrasaient
  mutuellement leur jeton.
- **Config serveur** : ajouter `service_secret`. Sans lui, les comptes de
  service sont inutilisables même avec `allow_service_accounts` à vrai.

---

## Reste ouvert

- Le jeton de compte est stocké **en clair** dans la config client, et
  n'expire jamais : qui obtient le fichier devient ce joueur.
- Atténuation verticale intermittente, à diagnostiquer par le log `[POS]` du
  récepteur.
- `allow_service_accounts` doit repasser à `false` avant toute ouverture
  publique.
