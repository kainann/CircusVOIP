# CircusVOIP — Changelog build 64

> **Version** : 0.4.0 **alpha** build 64
> **Statut** : publié sur le serveur de développement — *à marquer « publié le JJ/MM »*
> **Type** : mise à jour client + **instrumentation serveur déjà déployée** (26/07/2026)
> **Particularité** : **rupture de compatibilité audio** avec les clients 0.3.
> Un client 0.4 et un client 0.3 ne s'entendent pas.

---

## Nouveautés joueur

### 🎙 Le son passe en Opus — 32 fois moins de données

Jusqu'ici la voix circulait en **PCM float32 brut** : 3 841 octets par tranche
de 20 ms, quel que soit le contenu, silences compris. C'est un format de
calcul, pas de transport — aucune application de VOIP ne l'utilise.

Le son est désormais encodé en **Opus à 64 kbps**.

| Mesure réelle, 2 joueurs en conversation | Avant | Après |
|---|---|---|
| Pic audio | 1 796 kbit/s | 47 kbit/s |
| Médiane audio | 1 120 kbit/s | 42 kbit/s |
| Total avec positions | 1 807 kbit/s | 57 kbit/s |

Ce que ça change concrètement :

- Les joueurs en ADSL ne saturent plus leur ligne montante dès qu'ils parlent.
- Les soirées à forte affluence deviennent possibles : à 25 joueurs actifs, le
  serveur passe d'environ 336 Mbit/s à 13.
- Les silences ne coûtent presque plus rien (23 octets par trame au lieu de
  3 841).

Le débit de 64 kbps a été retenu après essais à l'oreille : les valeurs plus
basses dégradaient la **radio**, dont l'effet de distorsion est appliqué après
décodage et amplifie les artefacts du codec.

> ⚠ **Un client 0.4 ne peut pas parler avec un client 0.3.** Les trames sont
> incompatibles et sont ignorées de part et d'autre. Tout le monde doit
> passer à cette version.

### 📻 Coupures moins audibles quand un paquet se perd

- La reconstruction des trames perdues utilise désormais le mécanisme natif
  d'Opus, qui prolonge l'enveloppe et la hauteur de la voix, au lieu de
  rejouer la dernière trame atténuée de moitié.
- Le niveau sonore reste constant pendant la reconstruction : plus de petit
  trou de volume à chaque perte.
- L'ancien mécanisme est conservé en repli si le codec ne peut pas
  reconstruire.

### 🗺 Position enfin lue hors des vaisseaux et des bâtiments

- **Correctif important** : un joueur situé directement dans une zone
  planétaire — **à pied à la surface hors bâtiment, ou en EVA dans l'espace** —
  n'était **pas localisé du tout**. Toutes ses positions étaient rejetées.
- Conséquence jusqu'ici : il n'apparaissait pas dans la liste des joueurs,
  n'entendait personne en proximité et n'était entendu de personne, sans
  aucun message d'erreur — seulement « En attente de position OCR ».
- La cause : un garde-fou anti-erreur de lecture rejetait toute coordonnée
  dépassant 1 000 km. Or les coordonnées planétaires valent légitimement
  plusieurs milliers de kilomètres. Le seuil dépend maintenant du type de
  zone.

### 🚪 Lieux instanciés reconnus

- Les lieux dont le nom contient leur identifiant d'instance
  (`ObjectContainerBrokeredInstance_...`) sont désormais correctement
  identifiés : le nom et l'identifiant sont séparés, et deux joueurs d'une
  **même** instance s'entendent normalement tandis que deux instances
  distinctes restent séparées.
- Correction connexe : selon la façon dont la lecture d'écran interprétait le
  séparateur, un même hangar pouvait produire deux noms de zone différents et
  couper la proximité entre deux joueurs pourtant côte à côte.

### 🔌 Ports du serveur configurables

- L'administrateur d'un serveur peut choisir ses ports d'écoute dans
  `circusvoip_server_config.json`.
- Côté joueur, il suffit de saisir l'adresse sous la forme **`adresse:port`**
  dans le champ Serveur. Sans deux-points, le port habituel est utilisé —
  **rien ne change pour ceux qui ne touchent à rien**.
- Le port audio n'a plus à être connu : le serveur l'annonce à la connexion.

### 🎤 Le client démarre même sans micro

- Si le micro refuse de s'ouvrir, le client ne restait **pas** simplement
  muet : il s'arrêtait avant de lancer la lecture de position, la proximité et
  la radio. Le joueur se retrouvait invisible sur la carte sans comprendre
  pourquoi, et un simple redémarrage semblait « réparer » le problème.
- Le reste de l'application démarre maintenant dans tous les cas, avec un
  message explicite dans le journal.
- Si le périphérique choisi échoue, le client **essaie automatiquement les
  autres** et retient le premier qui fonctionne.

### 🎧 Liste de micros nettoyée

- Windows expose souvent le même micro sous trois ou quatre noms selon
  l'interface audio utilisée. La liste en affichait autant d'entrées.
- Les doublons sont regroupés, et les entrées **WDM-KS** sont écartées : cette
  interface réserve le micro à une seule application, ce qui le rendait
  indisponible dès qu'un autre programme l'utilisait.

---

## Côté serveur (déployé le 26/07/2026)

- Mesure du débit réel entrant **et sortant**, sur les positions comme sur
  l'audio. L'ancien compteur ne mesurait que l'entrant et affichait donc un
  chiffre jusqu'à dix fois inférieur à la charge réelle.
- Nouvelles métriques : nombre de destinataires par trame, octets par trame,
  répartition par joueur, et une ligne de synthèse combinant les deux flux.
- Le tableau de bord de la console d'administration affiche ces valeurs avec
  une moyenne glissante sur 60 secondes.

Aucune de ces modifications ne change le comportement du serveur : il relaie
toujours la voix sans en connaître le format, et n'a donc eu **aucun
changement à faire** pour Opus.

---

## Notes internes

- `opus.dll` et le module `circusvoip_opus.py` sont distribués par la mise à
  jour, ainsi que le paquet Python `opuslib-next`.
- Le débit est réglable sans recompiler via `CIRCUSVOIP_OPUS_BITRATE`, utile
  pour comparer deux réglages côte à côte.
- Le mannequin de test a été aligné sur Opus ; sans cela il aurait été
  inaudible et aurait produit du bruit chez les clients.
