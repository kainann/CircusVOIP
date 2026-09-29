# CircusVOIP — 0.4.0 Alpha Build 78

**Statut** : **publié le 07/09/2026**. Client seul.
Republié le 07/09 sur le même numéro (`--notes` sans `--bump-build`), personne n'ayant encore téléchargé : `circusvoip_phone_travail_app.py` corrigé après coup, voir « Confirmation avant retrait ».
**Serveur** : **rien à déployer.** `circusvoip_phone_urgence.py` y est déjà en
`c9ebe040…` depuis le 03/09, et il n'a pas changé depuis.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_client.py` | build | `da5dfb34b8ba1a9753d32e749ed83919` |
| `circusvoip_sc_ocr.py` | build | `afc58d86272da411b346c78cb31345b1` |
| `circusvoip_phone_travail_app.py` | build | `7a934c3692811953c9d91d412c06b1ff` |
| `circusvoip_phone_urgence.py` | VPS **et** build | `c9ebe040a40ce930d8a2acd5c1309822` |
| `circusvoip_phone_urgence_app.py` | build | `e2bfe3a810f30582cd67b38afd8839cb` |
| `circusvoip_phone_annuaire.py` | build | `e9f863453d3ddd25d2ca93203dbe67bc` |
| `circusvoip_phone_apps.py` | build | `a1127c9887c8b363bd81eb69335eb5e9` |

Sept empreintes contrôlées au `certutil` avant le `--bump-build`.

> **Cinq correctifs vérifiés en solo le 07/09**, trois attendent un second
> joueur. Voir « À tester en priorité ».

---

## Les journaux ne remontaient plus, et le client affirmait le contraire

**Symptôme** : deux sessions perdues sans le savoir. Recherche menée deux jours
du mauvais côté — script de récupération, puis plafond de taille serveur.

**Cause** : `_remonter_journal_debug` ignorait le booléen rendu par
`_ws_send_safe`. Le message de succès s'écrivait dans tous les cas, avec des
tailles calculées sur le paquet **local** — donc justes même quand rien ne
partait.

Or `_ws_send_safe` sort à sa première ligne si `state.connected` est faux, et
le `finally` du thread réseau met `connected` à `False` **avant** d'écrire
`[NET] Deconnecte`. Deux occurrences relevées :

| Date | Fait | Écrit dans le journal |
|---|---|---|
| 31/08 | déconnexion 21:59, fermeture 23:24 — 1 h 25 sans connexion | `journal remonté (850 Ko -> 115 Ko)` |
| 03/09 | `[NET] Deconnecte` 19:15:34, remontée 19:15:36 | `journal remonté (847 Ko -> 117 Ko)` |

Un échec qui ressemble à un succès. Troisième occurrence du motif après le
`scp` du 29/08 et le `.gz` résiduel du script de récupération. Règle § 5 ter.

**Correctif** — trois sorties au lieu d'une, chacune bouchant le trou des
autres :

- **le retour est respecté.** « journal remonté » ne s'écrit que si l'envoi est
  passé ;
- **déconnexion volontaire** : `_do_disconnect` remonte le journal **avant**
  `request_stop()`, tant que le socket est vivant. C'est la dernière occasion —
  après, `ws.close()` est planifié ;
- **filet sur disque** : quand l'envoi ne part pas, le paquet est écrit dans
  `circusvoip_debug\en_attente\<nom>.b64`. Ne dépend d'aucun réseau, donc ne
  peut pas rater ;
- **renvoi au `join` suivant**, une seconde après le passage en connecté. Là où
  la connexion est fraîche et vérifiée. Évite une reconnexion fantôme à la
  fermeture, qui aurait figé la fenêtre et produit un `join`/`leave` parasite
  chez les autres joueurs.

File bornée à 5 paquets et 14 jours : sans borne, un joueur durablement hors
ligne accumulerait des centaines de Mo sans s'en apercevoir. Le serveur
n'accepte qu'un journal par connexion (`_JOUEUR_LOG_RECU`), donc le plus récent
part en premier — c'est celui qu'on veut lire.

Un garde empêche le doublon : deux points de sortie appellent désormais la
remontée, et fermer après s'être déconnecté en produirait deux.

> **Les journaux du 31/08 et du 03/09 ne seront pas rattrapés** : le filet
> n'existait pas quand ils ont été perdus. Ils sont sur le disque du poste.

---

## Travail

### Retour arrière annulait toute la saisie

**Signalé au test du 77.** Dans le formulaire de création, Retour arrière sur
un champ **vide** fermait le formulaire et perdait tout, au lieu de
désélectionner le champ.

`handle_nav` avait bien une branche « esc » qui sort du champ — mais
**l'overlay route « esc » vers `handle_back()`, jamais vers `handle_nav()`**.
Cette branche attendait une touche qui ne vient pas. `handle_back` ne testait
pas `_dans_champ_` et refermait d'un coup. Les apps Urgence et Messagerie font
ce test depuis toujours ; Travail était la seule à ne pas le faire.

Le correctif du focus souris du build 77 était nécessaire mais pas suffisant :
il armait le drapeau, ce qui rendait ce second défaut atteignable.

### Notification quand une mission est prise

On était prévenu quand quelqu'un **abandonnait** sa mission, pas quand il la
**prenait** — l'inverse de ce qui est utile.

Rien à ajouter à la trame : le serveur pousse déjà `travail_etat` à l'auteur
après un `travail_prendre`, et chaque mission de `miennes` porte son
`executant`. L'information arrivait, rien ne la remarquait.

**Correctif côté client**, dans `_on_travail_etat` et **non** dans l'app :
`appliquer_etat` n'est appelée que si l'app Travail est ouverte, et c'est
justement fermée qu'on a besoin d'être prévenu. Une transition vide → non vide
sur l'`executant` déclenche `sig_travail_notif` : badge, son, entrée au
journal. Mécanisme déjà en place, il n'y avait qu'à l'appeler.

Le premier état reçu ne déclenche rien — sans point de comparaison, toutes les
missions déjà prises paraîtraient l'être à l'instant.

### Confirmation avant retrait

Retirer une mission était immédiat et irréversible.

La carte devient sa propre confirmation, sans page séparée : l'annonce reste
lisible pendant qu'on décide — on voit **ce** qu'on supprime. Bandeau rouge,
puis **Annuler** et **Confirmer le retrait**.

> **Deux allers-retours avant que ce soit utilisable**, tous deux sur la
> navigation D-pad, tous deux invisibles en lecture de code — il a fallu des
> captures d'écran du jeu.
>
> **1. Aucun bouton atteignable.** Seule la *carte* était enregistrée dans
> `_nav`, avec une action unique ; ses boutons internes n'existaient que pour
> la souris. Tant qu'il n'y avait qu'un bouton (« Retirer »), l'action de la
> carte suffisait. Avec deux actions opposées, « Confirmer le retrait » était
> inatteignable au clavier. `_Carte` expose désormais ses boutons, et en
> confirmation ce sont eux les cibles.
>
> **2. Bloqué sur Annuler.** Les deux boutons sont côte à côte, or le D-pad ne
> se déplace latéralement que dans une « grille » — `handle_nav` le dit :
> *hors grille, gauche/droite ne font rien*. Ils forment maintenant une grille
> d'une rangée à deux colonnes, `confirm:<id>`, même mécanisme que les boutons
> de métier.
>
> Effet de bord accepté : **bas** depuis Annuler mène aussi à Confirmer —
> descendre d'une rangée sort de la grille et atterrit sur son dernier
> élément. Sans danger, une Entrée délibérée reste nécessaire.

Trois détails repris du motif de sortie de groupe de la Messagerie plutôt que
d'en inventer un autre : **Annuler est l'action de navigation** (une Entrée
tapée trop vite annule, elle ne supprime pas) ; **rien n'est retiré
localement** (la mission disparaît quand le serveur a confirmé, sinon elle
disparaîtrait puis réapparaîtrait si l'envoi échouait) ; **Retour arrière
annule**, et la confirmation est désarmée à la réouverture de l'app — la
retrouver sans se souvenir de l'avoir demandée invite à valider par réflexe.

---

## Appels — la trace n'existait pas

`_phone_log` était un **stub vide** depuis la suppression de la page Phone
Debug. Sa docstring affirmait que ces traces se retrouvaient « dans le log
debug global via les exceptions et events serveur ». **C'était faux** :
vérification sur les 7239 lignes du journal du 03/09, aucune trace d'appel.
Tous les `_phone_do_*` écrivaient dans le vide.

Le serveur, lui, journalise la séquence complète — c'est ce qui a permis de
reconstituer le défaut du 03/09 :

```
16:45:19  [PHONE] Appel : Skywat -> Kainan (call_id=f27c6072)
16:45:22  [PHONE] Decroche
16:45:27  [PHONE] Raccroche par Kainan (etat=active)
```

Mais le serveur ne peut pas dire **quel état chaque client s'est donné**, et
c'est exactement ce qui manquait.

**Correctif** : `_phone_log` écrit dans `_dbg_log`, préfixé `[PHONE]`. Et
surtout, `_phone_set_state` trace la **transition elle-même** — elle commande
`state.phone_in_call`, donc à la fois le flag d'émission (`0x03` en appel,
`0x00` en proximité) et le filtre de réception.

> **Le défaut n'est pas corrigé, il est instrumenté.** Symptôme rapporté :
> après un raccroché, Skywat entendait Kainan sans réciproque. Hypothèse — un
> client resté en `in_call` quand l'autre est repassé en `idle`. Le filtre est
> asymétrique par construction : le repos jette les trames téléphone
> (`if not state.phone_in_call: continue`), tandis que l'appel laisse passer la
> proximité. Non vérifié : il faut rejouer le scénario et comparer les deux
> journaux sur `[PHONE]`.

---

## OCR — les noms de mission avec un numéro de segment

Deux règles de `_normalize_numbers` s'appliquaient à **toute** la ligne, nom de
zone compris, sans garde d'unité :

```python
text = re.sub(r"(\d)-(\d{3,})", r"\1.\2", text)
text = re.sub(r"(\d)\s*_\s*(\d)", r"\1.\2", text)
```

`glaciemring_segment_mission_genrl_002-007` devenait `..._002.007` ; le groupe
`<n>` de `_PAT_FIRST_CONTAINER` n'accepte pas le point, le motif n'atteignait
plus `Pos:`, et le container **n'était jamais lu** — ni par la boucle
principale (proximité audio), ni par la capture de hiérarchie (balise).

**C'était une régression** : les découvertes terrain du 16/07 relèvent
`glaciemring_segment_mission_genrl_001-021` lu correctement, avec son cid
`name:` préservant le segment.

**Correctif** : les deux règles exigent une unité `m`/`km` derrière le nombre,
comme les autres règles de reconstitution décimale. Un tiret ou un underscore
entre chiffres n'est un séparateur décimal perdu que dans une **coordonnée**.

Vérifié : `808-4524km` et `97 _ 72m` toujours corrigés, `002-007` et `002_007`
conservés, `area18_central-001` intact (le caractère avant le tiret est une
lettre).

Confirmé sur données réelles le 06/09 : `glaciemring…_002-016` et
`keeger…_001 007` lus avec leurs numéros, et servant de repère de distance.

> La whitelist reste **volontairement sans entrée** pour ces familles.
> Canonicaliser perdrait le numéro de segment et apparierait des joueurs
> éloignés. Décision documentée le 16/07, cohérente avec le garde
> chiffre-contre-chiffre du build 77.

### Mesure

`banc_ocr.py` a été **réécrit** — l'original n'avait jamais été sauvegardé hors
conversation, et n'était capté ni par le projet ni par `sync_projet.bat`.

Sur 4 journaux, 10786 lectures : **aucune différence** entre le 77 et le 78.
Sur les 60 journaux du corpus, module du 78 seul : 117888 lectures, 85,4 % de
parses, 356 containers, 4141 aberrantes (4,1 %). **Premier relevé global du
corpus**, à reprendre comme repère.

La comparaison avant/après sur les 60 journaux **n'a pas été faite**.

---

## État des tests

### Vérifié en solo le 07/09

- **Champs à la souris** — clic dans le titre, retour arrière : le caractère
  s'efface, le formulaire reste. Champ **vide** : on sort du champ sans fermer
  la création. Les deux moitiés du correctif tiennent.
- **Confirmation de retrait** — après les deux correctifs de navigation.
- **Trace `[PHONE]`** — un appel vers un numéro inexistant produit la séquence
  complète : trame envoyée, transition d'état, réponse serveur.

```
[PHONE] → phone_call_request (numéro=425789)
[PHONE] etat idle -> ringing_out (peer=None call_id='6c5ce124')
[PHONE] ← phone_call_ringing
[PHONE] → phone_call_hangup (call_id=6c5ce124)
[PHONE] etat ringing_out -> idle (peer=None call_id=None)
[PHONE] ← phone_call_missed
```

  `peer=None` est normal : seul `sig_phone_accepted` transporte encore des
  pseudos, le reste passe par les numéros. Le `call_id` identifie l'appel.

- **Remontée du journal** — deux sessions, deux fichiers reçus sur le VPS à
  08:44:39 et 08:46:21 UTC, à la seconde où le message s'écrivait.
  `circusvoip_debug\en_attente\` créé et **vide** : le filet est en place et
  n'a rien eu à retenir. Premier journal de ce poste arrivé sur le VPS depuis
  le 31/08 au matin.
- **Noms de mission OCR** — validé le 06/09 :
  `name:glaciemring_segment_mission_genrl_002_016` et
  `name:keeger_segment_mission_genrl_001_007`, numéros de segment conservés,
  servant de repère de distance.

### Reste à tester — demande un second joueur

1. **Se faire prendre une mission**, app Travail **fermée** : badge et son.
2. **Une balise entre deux systèmes différents** — jamais éprouvé : les quatre
   balises du 06/09 portaient toutes `795179149930`.
3. **Un appel raccroché**, puis comparer les deux journaux sur `[PHONE]`. La
   trace est prête des deux côtés.

---

## Ce que ce build ne corrige pas

- **La voix asymétrique après un raccroché** : instrumentée, pas corrigée.
- **L'identifiant système n'est pas stable dans le temps.** Même vaisseau,
  même endroit : `793143283050` le 03/09, `795179149930` le 06/09. Le vaisseau
  garde le sien (`793441392364`), le système non. `meme_systeme` reste correct
  entre deux clients lisant au même moment, mais une balise capturée avant un
  redémarrage de shard porterait un identifiant périmé → « Autre système » à
  tort. Le garde-fou envisagé — ne conclure que si les coordonnées confirment
  — **n'est pas dans le code**.
- **Désaccord entre les deux écrans d'urgence.** Le suivi affiche le nombre à
  toute échelle (`30 000 000 km`) ; la liste classe et bascule sur « Autre
  système » au-delà de 1e11 m. À 120 millions de km, les deux se
  contredisent. Le seuil de 1e11 n'a **jamais été mesuré**.
- **« Position inconnue »** mélange « rien reçu » et « reçu mais
  incomparable ». Dans le second cas, le container brut devrait être montré.
- **Le rafraîchissement du formulaire de mission** : `appliquer_etat()` appelle
  `_rafraichir()` sans condition et reconstruit les champs vides. Un autre
  joueur qui publie pendant la saisie l'efface. **Non tranché** : ne pas
  rafraîchir formulaire ouvert, ou préserver la saisie.
- **Le chef seul et sa propre balise.** `creer()` refuse s'il n'y a aucun
  secouriste disponible, mais `quelqu_un_dispo()` compte **le demandeur**. Un
  chef seul en service crée donc une balise que `prendre()` lui interdira de
  prendre. **Non tranché** : refuser, ou accepter en l'affichant.
- **Balise refusée après la capture OCR.** Vérifié le 06/09 : type `securite`
  sans agent en service, refus correct — mais 5,1 s d'OCR dépensées avant. La
  disponibilité est connue du serveur ; le type pourrait être grisé.
- **Le corollaire de la règle § 5 ter** — trace au premier passage dans
  `_app_dans_champ()`, classe de base `_AppAvecChamps` — est **écrit dans la
  doc et absent du code**. Même faute que la docstring de `_phone_log`.
- **`SC ferme` / `SC actif` inexpliqués** : 10 fois en 1 h 45 le 03/09, 4 fois
  en 25 min le 06/09, dont une de 3 s. Précédés d'une chute du taux de lecture
  (55 %, puis 9 %). **Non caractérisé.** Le journal ne garde que le texte lu,
  jamais l'image : proposition d'enregistrer la zone quand le taux s'effondre,
  comme le fait déjà `logs_urgence\`.
- **La déconnexion à 19:15:34**, deux secondes avant la fermeture. Le filet
  garantit désormais que le journal survit, pas que la déconnexion soit
  normale.
- **Auto-update au démarrage** (point 31). Un joueur peut encore faire une
  session entière sur un build périmé. Et le contrôle de version peut échouer
  **en silence** : `[UPDATE] Echec check : Permission denied
  '\\.\aswMonFltProxy\...'` — un antivirus, relevé le 18/05.
- **Réglage de taille du téléphone** (demande joueur, écran 2K). Formule
  dupliquée dans 7 fichiers, plafond à 760 px logiques,
  `phone_settings` hors grille (300×620). Chantier 0.5.0, en deux entrées
  distinctes. Résolution et échelle Windows du demandeur toujours inconnues.
- **`recuperer_logs_joueurs.ps1`** corrigé (`1dad9d39…`) mais toujours **non
  documenté** dans `CIRCUSVOIP_INFRA.md`, dont la section propose encore un
  `scp *.gz` qui retélécharge tout.
- **`banc_ocr.py` et `sync_projet.bat`** ne sont captés par aucun motif de
  synchronisation. Ils peuvent se reperdre comme la première version du banc.

---

## Déploiement

> Une seule ligne par commande sous `cmd`.

**Serveur** : rien.

**Client** — les sept empreintes vérifiées au `certutil`, puis :

```cmd
py -3 build_update.py --bump-build --notes "Build 78 - journaux en attente, notif prise de mission, confirmation retrait, retour arriere formulaire, trace des appels, noms de mission OCR"
```

```cmd
scp -r "D:\Projet CircusVOIP\updates" root@178.104.207.46:/home/circusvoip/
```

**Contrôles** :

```cmd
ssh root@178.104.207.46 "grep -o '\"build\"[^,]*' /home/circusvoip/updates/manifest.json"
```

```cmd
ssh root@178.104.207.46 "cd /home/circusvoip/updates/files && md5sum circusvoip_client.py circusvoip_sc_ocr.py circusvoip_phone_travail_app.py circusvoip_phone_urgence.py circusvoip_phone_urgence_app.py circusvoip_phone_annuaire.py circusvoip_phone_apps.py"
```

`78`, puis les sept empreintes du tableau.

**Avant le test de groupe** : vérifier que chacun est bien en 78. La première
ligne `[UPDATE]` de son journal le dira après coup, mais c'est **avant** qu'il
faut le savoir.
