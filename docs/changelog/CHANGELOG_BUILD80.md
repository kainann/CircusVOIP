# CircusVOIP v0.4.0 — Build 80

*29/09/2026 — build de RELEASE STABLE de la v0.4.0*

Dernier build du cycle 0.4 : il ne contient aucune nouvelle app, mais la
modération, les bannissements et tout ce qu'il faut pour diagnostiquer un
problème de voix sans demander un fichier à un joueur.

## Côté joueur

### Nouvel encart « Logs » dans les réglages

Entre **Audio** et **OCR**, trois choses qui vivaient jusqu'ici dispersées :

- **Journal audio détaillé** (inchangé, simplement déplacé) ;
- **Envoyer mon journal au serveur à la fermeture**, **décochée par
  défaut** : rien ne part sans un geste explicite ;
- **« Envoyer mon journal maintenant »**, sans attendre de fermer le
  client. 60 s entre deux envois.

La ligne « envoyer mon journal » était dans la section Audio, où elle
n'avait rien à faire : ce n'est pas un réglage de son.

Ce que le journal contient : périphériques, positions en jeu, zones
traversées, pseudos croisés, erreurs. Ce qu'il ne contient **jamais** :
vos messages et vos contacts.

### Atténuation verticale toujours active

La case a disparu des réglages. Validée au facteur 10 depuis le 28/07,
elle n'avait plus de raison d'être désactivable — et une moitié de
serveur avec et une moitié sans rend toute plainte sur la portée
incompréhensible. La clé de configuration est ignorée.

### Bouton « Vérifier les MAJ » masqué

Bascule normale d'une release stable (§5.7 #4). L'auto-update au
démarrage continue de fonctionner.

### Le journal envoyé n'est plus rejeté en silence

Le plafond portait sur **4 Mo de texte brut**, en pariant sur la compression.
Un journal dominé par des lignes `[POS]` ne compresse qu'à ~3,6× : 1497 Ko de
base64, au-delà de la limite de trame de websockets (1 Mio). Le serveur
fermait la connexion en 1009 **après** que le client ait affiché
« Envoyé à HH:MM » — faux succès suivi d'une déconnexion inexpliquée, et le
journal qui aurait expliqué la panne était celui qui ne passait pas.

Le plafond porte désormais sur le paquet **compressé** : 700 Ko de base64,
la fenêtre gardée divisée par deux jusqu'à six fois si besoin. La fin du
journal — ce qui vient de se passer — est toujours conservée. Mesuré après
correctif : 375 Ko de base64 sur 83 Mo de positions denses.

Les deux bornes n'étaient pas alignées : le serveur autorise 800 Ko de gzip,
la trame n'en laisse passer que 768 Ko. Toute borne posée sur du texte brut,
en amont d'une compression, est une borne qui ne borne rien.

### Message de bannissement lisible

Un joueur banni voyait « Vous êtes temporairement bloqué », le message
prévu pour un blocage anti-force-brute. Il lit maintenant le vrai motif :
« Vous avez été banni de ce serveur. Motif : … ».

## Côté serveur

### Bannissements liés au compte Discord

- Bannir **déconnecte immédiatement** le joueur s'il est en ligne.
- Le bannissement porte sur le **compte Discord**, pas sur la fiche : il
  **survit à la suppression de la fiche d'annuaire**. Supprimer la fiche
  d'un banni ne le débannit pas, et il ne peut pas revenir en reliant à
  nouveau son compte.
- Vérifié à **deux** endroits : à la liaison Discord et à la connexion au
  serveur, avant toute rotation de jeton.
- Un motif est enregistré avec la date.

### Modération des missions Travail

L'admin peut retirer une mission, y compris **prise** par un exécutant.
Le retrait est immédiat chez tous les joueurs concernés (détenteurs du
métier, auteur, exécutant). Personne n'est prévenu, comme pour les
annonces.

### Journaux du serveur téléchargeables

Les journaux (joueurs, positions, audio) partent vers l'admin par
morceaux de 256 Ko, lus hors de la boucle réseau. Un verrou par admin
évite deux transferts concurrents. Les chemins sont vérifiés : ni `..`,
ni lien symbolique, ni sortie des trois dossiers autorisés.

### Plafond sur les journaux remontés

4 journaux par connexion au maximum, avec une ligne de refus explicite
dans les logs. Sans ça, l'envoi manuel ouvrait la porte à un client
modifié remplissant le disque.

## Côté admin

### Page MODERATION

Les onglets **ANNONCES** et **MISSIONS** sont remplacés par une page
**MODERATION** à trois sous-pages : *Annonces*, *Missions*, *Bannis*.
Trois listes proches côte à côte valaient mieux que trois onglets à
chercher.

### Bannir depuis l'annuaire

Bouton **B** sur chaque ligne, avec saisie du motif et confirmation. La
ligne d'un joueur banni passe **en rouge** et son bouton B disparaît :
lisible au premier coup d'œil, sans écrire « BANNI » dans la colonne du
rôle — qui sert à afficher médecin ou sécurité. La sous-page *Bannis*
liste les bannissements et permet de les lever.

### Onglet LOGS

- Liste des fichiers du serveur par source (joueurs / positions / audio),
  avec date et taille.
- **Sélection multiple aux raccourcis Windows** : Ctrl+clic, Maj+clic.
  Un `logs_get` par fichier sélectionné.
- **Aucun écrasement** : un fichier déjà téléchargé donne
  `nom (2).log`, `nom (3).log`… comme l'explorateur Windows. Une copie
  annotée à la main ne peut plus disparaître sans un mot. Pas de boîte
  « remplacer / garder les deux » : avec dix fichiers sélectionnés, ce
  serait dix questions d'affilée.
- Les journaux joueurs arrivent compressés et sont **décompressés à
  l'arrivée** — plus besoin de `recuperer_logs_joueurs.ps1`.
- Un transfert interrompu laisse un `.part` et **ne détruit pas** la
  copie précédente.

### Version affichée

La barre de titre annonçait « Admin 0.1 » depuis trois versions. Elle est
maintenant **lue** dans `circusvoip_version.json` (le fichier que
`build_update.py` incrémente déjà, celui que le client lit), cherché à
côté du script puis dans le dossier parent. Affichée aussi dans le
bandeau, où une capture d'écran la montre.

Fichier introuvable → **aucune version affichée**, plutôt qu'un numéro
faux : c'est précisément ce qu'a fait « 0.1 ». L'identifiant Windows de
l'application ne porte plus la version, pour ne pas casser l'icône d'un
raccourci épinglé à chaque release.

### Deux gels à la connexion corrigés

L'admin se figeait quelques secondes en se connectant. Deux causes
distinctes, la seconde masquée par la première :

1. des appels Tk faits depuis le fil réseau, qui s'interbloquaient avec
   l'attente d'un envoi websocket ;
2. `_safe_after`, censé corriger le premier, appelait lui-même
   `root.after` depuis le fil réseau — même piège, un cran plus bas.

Correctif : une file d'attente vidée par le fil Tk toutes les 30 ms.
**Règle à retenir : aucun appel Tk depuis le fil réseau, `root.after`
compris.**

### Affichage purgé à la déconnexion

Joueurs, canaux, profils, chefs, annuaire, annonces, missions, bannis,
liste de journaux et statistiques de débit : tout repasse à
« (non connecté) ». Restaient affichées des informations périmées qu'on
pouvait prendre pour l'état courant. Le **JOURNAL est conservé** — c'est
souvent lui qu'on relit après une déconnexion.

### Confirmation de suppression d'une fiche

La suppression d'une ligne d'annuaire demande confirmation, en nommant le
pseudo **et** le numéro, et rappelle que le numéro pourra être réattribué.

## Fichiers

| Fichier | MD5 | Où |
|---|---|---|
| `circusvoip_client.py` | `bbafc0eb577101bbe15e7a50f49a733d` | client |
| `circusvoip_phone_contacts.py` | `64a9c5bd0097d299b770e204c3e77fb5` | client **et serveur** (déployé le 29/09, cf. ci-dessous) |
| `circusvoip_server.py` | `c739ff735e17590c17177a40213b670a` | serveur |
| `circusvoip_accounts.py` | `69ec15758467baddb8282349adf9d621` | serveur |
| `circusvoip_accounts_ws.py` | `a910ef45d0f4c698df6ef21d1544d951` | serveur |
| `circusvoip_admin.py` | `623fc3e164862a9a0432807746d5d04a` | admin (hors updater) |

`circusvoip_phone_contacts.py` **devient un fichier partagé** (cas C) :
`phone_travail`, `phone_urgence` et `phone_annonces` l'importent pour valider
un numéro, et il n'était que côté client. Absent du VPS, leur `try/except`
retombait sur un repli « 6 chiffres » qui **ne vérifie pas le préfixe 42** :
le serveur appliquait une règle plus permissive que le client, en silence,
depuis le b73. Pas exploitable en l'état — le numéro validé est celui que le
serveur lit dans sa propre fiche — mais le premier champ numéro saisi par un
joueur aurait laissé passer n'importe quelle plage. Déployé le 29/09 ;
à inscrire aux tableaux de répartition (cf. `claude/MAJ_DOCS_BUILD80.md`),
sans quoi le prochain déploiement l'oubliera.

Inchangés depuis le build 79 : `circusvoip_phone_annonces.py`
(`1757aa944bea4f6344445ab3d180c800`), `circusvoip_phone_annonces_app.py`
(`2b471016015e2a5f35ee79936846a60b`), `circusvoip_annonces_store.py`
(`e17d574f737d3256ea3b33e5946e5ee8`), `build_update.py`
(`596570d8d62fc8ec6350cc27f6bbfb73`).

## Bascules de release (§5.7)

À vérifier **avant** de compiler le Setup :

| # | Quoi | État attendu |
|---|---|---|
| 1 | `.iss` `#define AppChannel` | `"stable"` |
| 1 bis | `circusvoip_version.json` `channel` | `"stable"` |
| 2 | `.iss` `#define AppVersion` | `0.4.0` |
| 3 | `BuildInfo_Client.ini` `[Info] Build=` | cf. §5.8 — compteur distinct de celui du version.json |
| 4 | Bouton « Vérifier les MAJ » | commenté — **fait** |
| 7 | Atténuation verticale | case retirée, toujours active — **fait** |
| 8 | `circusvoip_server_config.json` `allow_service_accounts` | `false` |
| 9 | `circusvoip_server.py` `REQUIRE_ACCOUNT` | `True` |

## À tester en groupe

Jamais passé en groupe, et rien de tout ça n'est couvert par un test
solo :

- **Annonces** (build 79) : publication vue par un autre joueur app
  ouverte, case décochée = « sans contact » chez l'autre, badge chef,
  saisie en cours pendant qu'un autre publie, plafond 3, intervalle 30 s.
- **Bannissement** d'un joueur connecté : déconnexion immédiate, message
  avec motif, retour impossible, levée du ban.
- **Modération d'une mission prise** : ce que voit l'exécutant.
- **Téléchargement multiple** de journaux, dont un fichier déjà présent.
- **Notification de prise de mission**, « Autre système », bouton de
  redémarrage.
- **Touche radio de hugo** (incident du 21/09, toujours ouvert) :
  hypothèse d'un PTT radio sans canal assigné, silencieusement ignoré,
  la voix partant en proximité. Demande son journal ou le
  `positions_20260921_15…`.
- **VOIP de Star Citizen à couper** : c'est elle qui produisait la voix
  en double avec ~5 s de décalage. À ajouter à la doc d'installation des
  testeurs.
