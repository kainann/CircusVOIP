# CircusVOIP — 0.4.0 Alpha Build 68

**Statut** : préparé — non publié
**Type** : mise à jour client **+ déploiement serveur** (les deux sont nécessaires)

> ⚠ **Vos contacts, messages, images et historique d'appels du CircusPhone
> seront effacés** au premier lancement. C'est volontaire : l'ancien répertoire
> se remplissait tout seul de pseudos, sans numéros, et rien de tout cela n'est
> utilisable maintenant qu'on appelle un numéro. L'effacement n'a lieu
> qu'une fois.

---

## Le CircusPhone fonctionne désormais par numéro

### 📞 Appels — un vrai clavier

- L'application **Appels** contient un **clavier de composition** et
  l'**historique**. On compose un numéro reçu de vive voix et on appelle,
  sans avoir eu besoin de l'enregistrer au préalable.
- L'historique affiche le numéro, et chaque ligne porte deux actions : un
  bouton d'appel pour rappeler, et **Ajouter** pour enregistrer le contact.
- Ajouter un contact **renomme rétroactivement toutes les lignes** de ce
  numéro, passées comme futures. Le supprimer les fait redevenir des numéros.

### 📇 Contacts — votre carnet, et rien d'autre

- L'application **Contacts** ne liste plus les joueurs connectés au serveur :
  uniquement ce que **vous** avez enregistré vous-même.
- Ajout par nom + numéro, avec contrôle de saisie : six chiffres commençant
  par 42, espaces et tirets tolérés.
- Chaque contact peut être appelé ou supprimé directement depuis la liste.

### ✉️ Messagerie

- Liste des **conversations existantes**, avec pastille de non-lu, et un
  bouton **Nouvelle conversation** pour écrire à quelqu'un qu'on n'a jamais
  contacté — au choix depuis la liste déroulante de vos contacts, ou en
  saisissant un numéro.
- Les messages et les images sont routés par numéro. L'expéditeur s'affiche
  sous son numéro, remplacé par un nom si vous l'avez dans votre carnet.

### 🔢 Le numéro, et pas le nom

- Un appel entrant affiche le **numéro** de l'appelant. Son nom n'apparaît que
  si vous l'avez enregistré : c'est **votre** carnet qui fait la traduction,
  personne d'autre.
- **La photo de profil s'affiche toujours**, même pour un numéro inconnu. Le
  RP protège le nom, pas le visage — croiser quelqu'un sans savoir qui il est,
  c'est exactement la situation qu'on veut reproduire.
- **Composer un numéro au hasard n'apprend rien** : un numéro qui n'existe pas
  sonne dans le vide exactement comme un joueur hors ligne, jusqu'à
  expiration. Les deux cas sont indistinguables.
- Plus aucun indicateur de présence : savoir qui est en ligne à partir d'un
  simple numéro n'a pas à être possible.

### ⌨️ Navigation au clavier

- Les trois applications se pilotent aux flèches, comme le reste du téléphone.
- **La sélection est visible dès l'ouverture** et un seul appui la déplace.
- **Gauche / droite choisit l'action** sur la ligne visée — appeler ou
  ajouter, appeler ou supprimer — avec un halo sur le bouton concerné.
- Les onglets se rejoignent **en remontant** au-dessus de la première ligne ;
  la barre s'encadre alors de bleu.
- **Entrée** valide et enchaîne : nom, puis numéro, puis le bouton. Le
  **retour arrière** efface tant qu'il reste du texte, et ressort du champ
  une fois celui-ci vide.
- Sur la liste déroulante des contacts, Entrée **ouvre** la liste.

---

## Corrections

### 🚪 On peut de nouveau quitter une conversation

- Après l'envoi d'un message, le champ est vide : le retour arrière n'avait
  plus rien à effacer et n'était pas transmis non plus. **On restait bloqué
  dans le champ**, donc dans la conversation. Seule la touche Échap
  fonctionnait encore, ce que rien n'indiquait.

### 🔙 Fin d'appel et sortie de conversation

- Terminer un appel, quitter une conversation ou sortir des réglages photo
  ramenait sur l'ancien écran « Annuaire vide », qui n'a plus de raison
  d'exister. On revient désormais à l'application d'où l'on vient, ou à
  l'accueil.

### 🧹 Nettoyage

- **826 lignes supprimées** : l'écran natif Contacts/Appels et tout ce qui en
  dépendait. Il listait les joueurs connectés et permettait de les appeler par
  pseudo, ce que le nouveau modèle interdit.

---

## Notes internes

- **Répartition des fichiers.** `circusvoip_phone_contacts.py` (carnet local +
  purge) et `circusvoip_phone_annuaire.py` (les trois apps) sont ajoutés à
  `RELEASE_FILES`. Sans eux, l'accueil du téléphone n'a plus ni Appels, ni
  Contacts, ni Messagerie : l'écran natif qui servait de repli a été supprimé.
  Toujours interdits de livraison : `circusvoip_accounts*.py` et
  `circusvoip_admin.py`.
- **Substitution nom/numéro.** `Repertoire.nom_pour()` est le point d'entrée
  unique. Rien ne stocke de nom — ni l'historique, ni les conversations — d'où
  le renommage rétroactif sans mise à jour à propager.
- **Serveur.** `phone_call_request`, `phone_message_send` et `phone_image_send`
  acceptent `target_numero` ; `_numero_to_pseudo()` résout en interne et le
  pseudo ne sort **jamais**. Les cinq réponses de `profile_photo_request` sont
  reclées sur le numéro. Un numéro inattribué reçoit un pseudo de remplacement
  `#<numero>` qui sonne dans le vide plutôt qu'un refus, sans quoi le clavier
  deviendrait un outil de recensement. L'ancien routage par pseudo reste
  accepté pour les clients non encore mis à jour.
- **`WA_StyledBackground`.** Les lignes de liste sont des `QWidget` : sans cet
  attribut, Qt **ne peint pas** le `background` d'un stylesheet. La sélection
  se déplaçait correctement mais restait invisible — d'où l'impression qu'on ne
  pouvait rien sélectionner. Le motif est repris de `_PhoneContactRow` v0.3,
  couleur comprise.
- **Saisie clavier.** Première tentative abandonnée : elle interceptait les
  chiffres à la main et échouait en AZERTY (la ligne du haut ne renvoie pas de
  chiffres) tout en rendant les lettres impossibles. Le mécanisme retenu est
  celui de l'écran conversation — forçage de la fenêtre au premier plan puis
  focus Qt, `is_text_field` élargi aux champs des apps.
- **Retour arrière.** Le listener rendait cette touche au champ sans jamais la
  transmettre. La règle est maintenant : champ non vide → efface, champ vide →
  ressort. Prédicat `champ_courant_vide()` côté app, `_app_champ_vide()` côté
  overlay, appliqué **aussi** à l'écran conversation.
- **Purge.** `purge_donnees_0_4_0()`, gardée par `phone_data_purged_040`
  (clé *core-managed*, sinon la purge rejouerait à chaque démarrage). Elle
  efface l'annuaire, les messages, les images **et** `call_history` — ce
  dernier vit dans la config et non dans un fichier, ce qui lui avait fait
  échapper au premier ménage.
- **Auto-enrichissement coupé** aux trois endroits où il opérait (welcome,
  join, réception de message). Sans cette coupure la purge n'aurait servi à
  rien : l'annuaire se serait reconstitué à la connexion suivante.
- **Tests** : 138 (apps), 27 (carnet et purge), 19 (liste déroulante),
  13 (sélection visible), 5 (sortie de conversation). Exécutés sous PySide6 en
  mode hors écran, avec contrôle du rendu réel — visibilité du popup, style
  effectivement appliqué — et non du seul état interne.

---

## Reste ouvert

- L'app Messagerie n'a **jamais tourné en jeu**.
- Conversations de groupe et renommage de conversation : conçus, non écrits.
- Messagerie différée vers les joueurs hors ligne : conçue, non écrite.
- `allow_service_accounts` doit repasser à `false` avant toute ouverture
  publique.
