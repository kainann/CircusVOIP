# CircusVOIP — 0.4.0 Alpha Build 69

**Statut** : préparé — non publié
**Type** : mise à jour client **+ déploiement serveur** (les deux sont nécessaires)

> Aucune purge, aucune perte de données. Vos contacts, conversations et
> photos sont conservés tels quels.

---

## Le téléphone répond même quand vous n'êtes pas là

### ✉️ Messages et images en différé

- Écrire à quelqu'un qui n'est **pas connecté** ne fait plus disparaître le
  message. Il est conservé sur le serveur et **délivré à sa prochaine
  connexion**. Idem pour les images.
- Les **appels manqués** sont conservés aussi : si on vous appelle pendant
  votre absence, la trace apparaît dans votre historique à votre retour.
- Le rappel se fait **conversation par conversation**, chacune complète avant
  la suivante. Un fil mélangé serait illisible à l'arrivée.
- Les messages sont conservés **30 jours**, comptés depuis leur envoi.

### 📱 Ce que ça change à l'usage

Rien à activer, rien à configurer. Vous vous connectez, et ce qui vous
attendait arrive. Les images sont espacées d'une demi-seconde pour ne pas
saturer la connexion à l'arrivée.

### 🛡️ Limites, et pourquoi elles existent

- **20 Mo par joueur.** Au-delà, les nouveaux messages vers cette personne
  sont refusés — et l'expéditeur est prévenu. Les anciens ne sont **jamais**
  effacés pour faire de la place : sinon quelqu'un pourrait noyer votre file
  pour faire disparaître les vrais messages avant que vous ne les lisiez.
- **Anti-spam automatique**, avec deux compteurs distincts : un en volume,
  qui protège le serveur, et un en nombre de messages, qui vous protège
  vous. Dix mégaoctets de texte, ce sont dix-sept mille messages : le serveur
  n'en souffrirait pas, la personne visée si.
- Les seuils sont **très au-dessus d'un usage normal**. Partager une série de
  captures ne les déclenche pas.
- En cas de dépassement répété : refus, puis silence de 10 minutes, puis
  déconnexion, puis blocage temporaire du compte. Toujours **borné dans le
  temps** — un faux positif ne doit jamais exclure quelqu'un définitivement
  sans décision humaine.
- Les **appels ne sont pas concernés** par ces quotas.

---

## Le reste

### 🔊 Audio — la qualité annoncée, enfin appliquée

Le débit Opus était resté à **48 kbps** alors que la documentation annonçait
64 depuis le 29/07. Les builds 63 à 68 ont donc tous tourné en dessous de ce
qui avait été validé à l'oreille — y compris les tests audio du build 67.
Corrigé : **64 kbps**.

### 💬 Messagerie — 50 messages par conversation

La limite passe de 20 à **50 messages conservés par contact**. Vingt était un
chiffre posé au hasard : une soirée d'échanges effaçait son propre début.

### 🖼️ Photos — deux onglets

L'application Photos se divise en **Mes Photos** et **Photos Reçues**.

Les images reçues en message vivaient jusqu'ici uniquement dans le fil de
conversation. Quand la bulle sortait de l'historique, le fichier restait sur
le disque mais **plus rien n'y donnait accès**. Le nouvel onglet est leur
porte d'entrée permanente.

- Grille de deux colonnes, plus récentes en haut.
- On change d'onglet en remontant sur la barre, comme dans Appels.
- Les images que **vous** avez envoyées n'y figurent pas : l'original est
  déjà dans « Mes Photos ».

---

## Sous le capot

- **Nouveau module serveur** `circusvoip_phone_queue.py` : files différées et
  anti-spam. **À déposer sur le VPS** — il n'est pas distribué aux clients.
- **Import gardé** : si le module manque, le serveur démarre et tourne
  exactement comme avant (message perdu si la cible est hors ligne). Ça permet
  un déploiement en deux temps, mais l'absence est silencieuse.
- **Files classées par numéro**, jamais par pseudo. Le pseudo est un
  affichage : une file `Kainan.json` qui attend trois semaines deviendrait
  introuvable après un renommage. C'est le défaut connu des photos de profil,
  qu'on ne reproduit pas ici. Le pseudo est résolu **au rejeu**.
- **Rien n'est retiré avant acquittement** du client (`phone_queue_ack`) :
  une déconnexion en plein rappel ne perd rien, la reprise se fait à la
  connexion suivante.
- **Repli pour les clients antérieurs** : ils ne connaissent pas
  l'acquittement et ne l'enverront jamais. Au bout de trois envois,
  l'événement est abandonné — mieux vaut le perdre que le resservir
  indéfiniment. Ils affichent en revanche les messages différés normalement,
  sans rien savoir du mécanisme.
- **Un numéro inattribué ne crée aucune file.** « Numéro inconnu » et « joueur
  hors ligne » tombaient dans la même branche silencieuse ; ils se séparent
  ici, sinon on matérialiserait sur disque des numéros inexistants.
- **Blocage de compte** ajouté à `circusvoip_accounts.py` (`block_numero`,
  `unblock`, `blocked_until`). Il vit là parce qu'il doit survivre à un
  `systemctl restart` — un blocage en RAM se lèverait au redémarrage suivant,
  et il suffirait de l'attendre. Les compteurs de fenêtre, eux, restent en
  mémoire.
- **Comptes de service exemptés** de quota. Le mannequin et le test de charge
  émettent à un rythme non humain par nature ; sans exemption ils se feraient
  bloquer par leur propre serveur.
- **Purge de rétention** par boucle asyncio : au démarrage **puis** toutes les
  24 h. Le passage au démarrage rattrape les arrêts prolongés. Elle logue ce
  qu'elle supprime — sans trace, on ne saurait jamais si elle tourne.
- **`_pseudo_to_numero()` n'est plus appelé à chaque message.** Le numéro est
  mémorisé sur la fiche client à la connexion. La fonction parcourait tout
  l'annuaire, et elle était déjà sollicitée à chaque message et à chaque appel
  entrant.
- **App Photos** : la barre d'onglets `_Onglets` est **reprise** de
  `circusvoip_phone_annuaire` au lieu d'être redéfinie, pour que les deux
  écrans ne divergent pas. Nouvelle dépendance entre les deux modules — les
  deux sont dans `RELEASE_FILES`.
- **Commentaires corrigés** : trois blocs décrivaient encore « 10 envoyés +
  10 reçus » (régime d'avant mai), et celui du bitrate annonçait 32000 alors
  que la constante valait 48000. C'est cette dérive commentaire/constante qui
  a masqué le bug audio pendant onze jours.
- **Tests** : 47 (files différées et anti-spam), 16 (blocage de compte),
  27 (app Photos, sous PySide6 hors écran avec contrôle du rendu réel).

---

## Reste ouvert

- La messagerie différée **n'a pas encore tourné en jeu**. Le test qui compte :
  deux clients, un déconnecté, un message et une image, puis reconnexion.
- L'acquittement part **dès réception**, avant l'écriture disque du client.
  La fenêtre est d'un tour de boucle d'événements ; un plantage pile dedans
  perdrait le message.
- Conversations de groupe et renommage : conçus, non écrits. Le point non
  tranché reste qu'un groupe distribuerait les numéros de tous les membres,
  ce qui contourne la règle RP.
- `allow_service_accounts` doit repasser à `false` avant toute ouverture
  publique — et la création d'un compte de service depuis l'admin en est le
  préalable, sinon le mannequin et le test de charge deviennent inutilisables.
- Le jeton client est toujours en clair dans la config et n'expire jamais.
  Ça compte davantage maintenant que le blocage de compte est le mécanisme
  de défense principal : usurper un jeton permet de faire bannir sa victime.
- `INSTALL_SERVEUR.md` annonçait encore les ports 8888/8889 et une liste de
  fichiers serveur antérieure aux comptes Discord. Corrigé dans ce build.
