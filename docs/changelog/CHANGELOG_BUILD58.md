# CircusVOIP — Changelog v0.3.0 alpha, build 58

**Date de release : 08/07/2026** · Canal : alpha · Précédent : v0.2.0 stable, build 57 (18/06/2026)

La v0.3 est la plus grosse mise à jour du projet : elle introduit le
**CircusPhone**, un téléphone en surimpression du jeu, avec ses applications,
sa messagerie et ses jeux multijoueurs en proximité. 20 fichiers de code
distribués (contre 5 en v0.2), plus les fonds d'écran et les skins.

---

## 🆕 CircusPhone (nouveau)

Téléphone overlay affiché par-dessus Star Citizen, entièrement pilotable au
clavier (flèches = naviguer, Entrée = valider, Retour arrière = retour).
- Écran d'accueil avec grille d'applications, dans l'ordre : Appels,
  Messagerie, Portefeuille, Blueprints, Jeux, Caméra, Photos, Paramètres.
- Fond d'écran personnalisable (voir Paramètres) ; fond par défaut fourni.
- Bannière d'état (heure, pseudo) et navigation cohérente dans toutes les apps.

## 📞 Appels & Messagerie (nouveau)

- **Appels** entre joueurs connectés : écran refondu avec **onglets Menu /
  Historique** (journal des appels passés), annuaire, sonneries (sons
  dial/ring fournis), notifications.
- **Messagerie** : écran dédié (fini le double titre « Contact »),
  conversations privées persistantes, accusés non-lus (pastille),
  notification sonore.
- **Partage d'images** : envoi de screenshots dans une conversation
  (bouton trombone → sélecteur de captures), affichage en vignette dans la
  bulle — vignettes équilibrées quelle que soit l'orientation (les photos
  verticales ne dévorent plus l'écran) ; images transmises en meilleure
  qualité (jusqu'à 1600 px) pour rester nettes en plein écran ; cache local.
- Navigation clavier complète dans une conversation (parcourir les
  messages, ouvrir une image en grand sur le moniteur, répondre).

## 💰 App Portefeuille (nouveau)

Suivi automatique de tes mouvements d'argent, lu depuis le Game.log de SC :
- **Ventes et achats** en boutique (nom de l'objet, quantité, magasin).
- **Achats/ventes de marchandises** (commodités, cargo).
- **Transferts d'argent entre joueurs** : détection des envois ET des
  réceptions, **nom du joueur affiché** (« de Skywatt » / « à Hugo »),
  **taxe de 0,5 %** calculée sur les envois (« dont taxe X »), les simples
  demandes de transfert sont ignorées (pas de double comptage).
- **Locations de véhicules/vaisseaux** : prix, durée (1 j / 3 j / 7 j) et
  nom lisible avec marque développée (Drake Cutter, Greycat STV…), validées
  seulement si le paiement a réussi.
- **Filtre de période** : 24 h / 7 j / 30 j / Tout (par défaut Tout).
- **Résumé** en tête : REÇU (vert) · DÉPENSÉ (rouge) · SOLDE (net, signé),
  séparateur visuel avant le solde, montants abrégés (142k, 1,08M).
- Liste STRICTEMENT antichronologique (tri par horodatage, même pour les
  événements multi-lignes du log), montants complets avec séparateur de
  milliers lisible, taxe détaillée par ligne ; menu épuré (l'outil de test
  « Simuler un mouvement » a été retiré).
- Navigation clavier : gauche/droite change le filtre, haut/bas fait défiler.
- Historique persistant entre sessions (`circusphone_wallet.json`),
  menu ⋯ (choisir le Game.log en mode autonome, vider l'historique).

## 📐 App Blueprints (nouveau)

- Liste les **plans de fabrication (blueprints) reçus** (récompenses de
  mission), lus depuis le Game.log : nom + date/heure.
- Même présentation que le Portefeuille : filtre 24 h / 7 j / 30 j / Tout,
  compteur sur la période, navigation clavier, historique persistant.

## 📷 App Caméra & 🖼 App Photos (nouveau)

- **Caméra** : fenêtre viseur par-dessus le jeu (ne vole pas le focus),
  R = pivoter (portrait/paysage), effet **flash** à la prise de vue,
  Entrée = photo, Retour = fermer. Les clichés sont enregistrés dans
  `screenshots/`.
- **Photos** : galerie des clichés (vignettes, récents en premier),
  icône « pile de polaroids » dédiée.
- **Visionneuse plein écran sur le MONITEUR** : sélectionner une photo
  (galerie ou image d'une conversation) l'affiche en grand sur l'écran de
  jeu — 80 % de la hauteur, sans bordure ni fond, orientation d'origine
  conservée. Retour arrière ferme.

## 🎮 App Jeux (nouveau)

Dossier « Jeux » regroupant quatre jeux ; les jeux multijoueurs se jouent en
**proximité réelle** (les adversaires doivent être au même endroit en jeu,
vérifié côté serveur).

- **Valakkar** (solo) : le serpent des sables, skins, **meilleurs scores
  partagés** entre tous les joueurs du serveur (top affiché en jeu,
  synchronisation des records faits hors ligne).
- **Billard** (2 joueurs) : blackball (jaunes/rouges/noire), physique
  déterministe en lockstep, **tables réelles** relevées dans le verse
  (stations Stanton & Pyro, Carrack…) — l'icône n'apparaît que près d'une
  vraie table.
- **Sol VS Terra** (bataille navale) : solo contre l'IA ou 1v1 en réseau,
  flottes secrètes, skins.
- **Poker** (2 à 8 joueurs) : Texas Hold'em, l'hôte fait autorité,
  chrono anti-AFK (fold automatique des absents), écran d'options de la
  partie (blindes, tapis), relances au clavier, calcul et transfert des
  gains en fin de main, écran de fin et classement.
- **Robustesse** : bannière « Adversaire inactif — Retour pour quitter »
  au Billard (60 s) et à Sol VS Terra (45 s, y compris pendant la pose de
  flotte) pour ne jamais rester bloqué face à un joueur AFK.

## ⚙ App Paramètres (nouveau)

- **Fonds d'écran** : grille de fonds fournis + dégradés générés + import
  de tes propres images ; le choix est conservé.
- **Journal RX audio** (diagnostic) : case à cocher pour tracer l'audio reçu.

## ⌨ Raccourcis & confort

- **Espace n'est JAMAIS interceptée** : c'est la touche de saut de SC, elle
  file toujours au jeu — **Entrée** est la touche de validation partout
  (jeux, caméra, apps).
- **Retour = Retour arrière** (l'ancienne touche Échap est libérée pour SC),
  et les touches **ZQSD ne sont plus utilisées** par le téléphone (flèches
  uniquement) : aucune touche de déplacement SC n'est confisquée.
- Toutes les apps non-jeu se pilotent au D-pad ; les jeux capturent le
  clavier seulement quand ils sont à l'écran.

## 🛰 OCR & zones (améliorations)

- Nouvelles zones reconnues au fil des relevés terrain : New Babbage
  (spaceport/transit), rest stops CRU-L5 (hall commercial, habs, ascenseur
  des habs)…
- Nouvelle table de billard relevée : **CRU L5** (en plus des stations
  Stanton/Pyro et de la Carrack).
- Canonisation renforcée : tirets/underscores équivalents, confusions
  OCR l↔1 rattrapées (ex. « rs entry cru leol » → `rs_entry_cru-leo1`).

## 🔒 Serveur & réseau (déployé côté VPS)

- **Lobbies multijoueur** avec vérification de proximité durcie (les deux
  joueurs doivent prouver le même lieu).
- **Meilleurs scores partagés** par jeu (top 20, un record par joueur).
- **Relais des images** de la messagerie entre joueurs.

## 📦 Distribution

- Le paquet passe de 5 à **20 fichiers de code** + sons ; l'updater crée
  automatiquement les nouveaux fichiers chez les joueurs (migration 0.2 → 0.3
  sans manipulation).
- **Fonds d'écran** (dont le fond par défaut) et **skins des jeux**
  (Sol VS Terra, Valakkar) inclus dans le paquet.
- Affichage de version : « 0.3.0 alpha 058 » (canal alpha : le numéro de
  build est visible pour faciliter les remontées de bugs).
