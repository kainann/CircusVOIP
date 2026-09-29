# CircusVOIP — 0.4.0 Alpha Build 76

**Statut** : **publié le 31/08/2026** — serveur et client.
**Type** : mise à jour **client + serveur**. Le serveur a été déployé en trois
temps les 29 et 31/08, **avant** le client.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `d1b5555ecf9c9146911a405dfd1f3ed0` |
| `circusvoip_urgence_store.py` | VPS | `12e1d4ae77014c19b7b616025a6a05a4` |
| `circusvoip_accounts.py` | VPS | `cfee14dd0a2b9fc8b5ebcc154d6f8a47` |
| `circusvoip_client.py` | build | `6af551142dff371e47d55c25b8ff213d` |
| `circusvoip_sc_ocr.py` | build | `6deef614d8af673ded8da152f4d921c4` |
| `circusvoip_phone_urgence_app.py` | build | `43853fd4c12ab8fefef4fe1464bd82dc` |
| `circusvoip_phone_travail_app.py` | build | `1324335c11c9b8b53eb4cdb47ae9d106` |
| `circusvoip_phone_travail.py` | build | `077c90962ee56797141177f0af2c2f2a` |

> **Les trois fichiers serveur vont ensemble.** `set_chef` rend désormais une
> clé `_anciens`, `quitter_service` rend une liste au lieu d'un booléen, et le
> serveur exploite les deux. En déposer un sans les autres laisse le serveur
> lire des clés absentes — et `a_prevenir += <booléen>` lève une `TypeError`
> attrapée par la garde générique, qui la transforme en « Action impossible
> pour le moment ». Le 29/08, deux fichiers sur trois n'ont pas été copiés dans
> `fichiers serveurs\` : le `scp` a renvoyé les anciens et l'incohérence est
> passée inaperçue jusqu'au contrôle des empreintes.

---

## OCR en Pyro — un quart des positions étaient fausses

**Symptôme** : aucun. C'est le problème. Les lectures erronées ressortaient en
`COORDS OK`, indistinguables des bonnes.

**Mesure sur les journaux réels du 28/08** :

| Joueur | Lectures en Pyro | Aberrantes |
|---|---|---|
| Kainan | 1052 | 474 (45 %) |
| Firesstones (2ᵉ session) | 257 | 75 (29 %) |
| Firesstones (1ʳᵉ) | 48 | 13 (27 %) |
| Skywat | 71 | 18 (25 %) |

**Cause principale — le séparateur décimal lu comme un chiffre.** En Pyro,
l'affichage passe en km avec quatre décimales. La virgule du HUD est un trait
fin qu'EasyOCR rend en chiffre : `1` dans 1002 cas sur 1097, puis `3`, `0`, `7`.

```
"-236 3 4621km"  ->  -236 puis 3.4621     au lieu de  -236.4621
```

La règle de recollage suivante fabriquait `3.4621km` et laissait `-236` seul :
le triplet se décalait d'un cran et la troisième coordonnée était jetée.

Confirmé par **363 correspondances** avec une lecture voisine propre portant les
mêmes quatre décimales — `-17 1 7997` face à `-17.7997`, `-38 1 0660` face à
`-38.0660`.

**Cause secondaire — une lettre dans les décimales.** 314 nombres touchés. La
lettre coupe le nombre en deux : le morceau de gauche perd son suffixe `km` et
repart en mètres.

```
"194,65g0km"  ->  z = 195 m        au lieu de  194 650 m
"-97 450gkm"  ->  y = -97, z = 450   (la 4e valeur est jetée)
```

**Correctif** : substitution seulement quand elle est vérifiée, troncature
sinon.

> **`g` ne vaut `9` que dans 24 % des cas.** Sur 208 correspondances il se
> répartit sur les dix chiffres : ce n'est pas une confusion de forme, c'est le
> marqueur d'un glyphe illisible. La correction « évidente » aurait été fausse
> trois fois sur quatre.
>
> Les substitutions retenues sont celles qui tiennent : `#` → `4` (100 %,
> 15 correspondances), `S` → `5` (89 %, 19), `o` → `0` (88 %, 66).

Pour le reste, on tronque au premier caractère illisible. Une décimale de km
vaut 100 m, la deuxième 10 m, la troisième 1 m — et **82 % des atteintes
tombent en 3ᵉ ou 4ᵉ position**, où la troncature coûte au plus 10 m. En dessous
de deux décimales sûres, le nombre est effacé et la lecture entière rejetée :
un rejet coûte un demi-tour de boucle, une position fausse contamine la
proximité audio et les balises.

**Résultat, sur 7601 lectures Pyro réelles** :

| | Avant | Après |
|---|---|---|
| Lectures justes | 6311 | **7271** |
| Aberrantes acceptées | 1020 | **124** |
| Témoin Stanton, justes | 8455 | 8473 |
| Témoin Stanton, aberrantes | 2 | **0** |

### Le déclencheur reste inconnu

Cinq sessions du même joueur, même machine, même zone `pyro4` :

| Session | Lectures | Séparateur lu comme un chiffre |
|---|---|---|
| Après-midi (jour) | 3135 | **28,0 %** |
| Nuit 21h12 | 550 | 0,0 % |
| Lever d'étoile | 806 | 0,0 % |
| Jour 22h31 | 260 | 0,0 % |
| Soir 23h17 | 325 | 0,6 % |

Deux explications ont été avancées puis **écartées par les données** : la
luminosité du fond, puis le mouvement du joueur — la session du lever avait
80 % de changements de valeur pour zéro lecture fautive, contre 36 % pour
l'après-midi et 28 % de fautes.

Le correctif améliore les cinq sessions et n'en dégrade aucune. Il se validera
statistiquement, sur les journaux remontés, le jour où le défaut réapparaîtra.

---

## Balises d'urgence — SolarSystem n'est plus obligatoire

**Symptôme** : le 28/08, un joueur a enchaîné **onze échecs consécutifs** de
balise, `position non lue`, sur huit minutes. Sa boucle OCR principale
annonçait au même moment `parses_ok=49 taux=100%`.

**Cause** : la capture de hiérarchie exigeait les coordonnées SolarSystem, sous
la justification « sans référentiel commun, pas de distance ». Or sa ligne
`pyro4` était lue **correctement à chaque essai** — seule la ligne SolarSystem
échouait.

Sur son écran, le chiffre `1` des grands nombres sort en tiret :

```
"-3704-01.3527"   au lieu de   "-3704101.3527"
```

Ce qui déclenche la garde du séparateur décimal double, d'où `ligne SolarSystem
illisible`, puis `lecture incomplete`, trois fois, repli compris. Sa zone OCR
fait **18 px de haut contre 28** chez un autre joueur : les glyphes sont plus
fins et le `1`, simple trait vertical, dégénère.

**Correctif** : la règle devient « au moins un repère exploitable » — un
container commun **ou** les coordonnées système. Les deux situations réelles
sont couvertes : la balise en plein vide qui n'a que SolarSystem, et celle qui
n'a qu'un container. Une lecture sans ni l'un ni l'autre reste refusée.

Deux illisibilités étaient confondues dans le même compteur et sont désormais
distinctes : un niveau **intermédiaire** manquant ampute la chaîne en silence et
reste bloquant ; la ligne SolarSystem ne sert qu'au repli orbital, lui-même
bloqué en orbite.

Sur la double lecture, l'absence de système ne disqualifie plus la concordance,
mais **une seule** des deux lectures avec système la disqualifie — signe d'un
OCR instable sur cette ligne.

> **Réserve.** La chaîne de réparation OCR n'a pas pu être rejouée de bout en
> bout : `ocr_texts_from_region` fait plusieurs passes et sélectionne la
> meilleure. Le basculement a été vérifié sur l'état que le compte rendu
> établit lui-même. C'est le correctif du lot qui demande le plus une
> validation réelle.

---

## Chef d'équipe destitué — il restait dans l'équipe

**Symptôme** : un chef retiré de son poste gardait son rôle, continuait de
recevoir les signaux, et son téléphone gardait ses onglets jusqu'à reconnexion.

**Cause** : `set_chef` retirait le drapeau `chef` et s'arrêtait là. Trois
oublis :

1. **`acc["role"]` intact** — il restait membre de l'équipe.
2. **Le service n'était pas quitté** — le registre en RAM le comptait parmi les
   disponibles, donc `quelqu_un_dispo()` renvoyait vrai grâce à quelqu'un qui
   n'était plus là. Une victime pouvait créer une balise que personne ne
   prendrait.
3. **Aucun état ne lui était poussé** — seul le *nouveau* chef était notifié.

**Décision retenue** : l'ancien chef perd tout. Il n'avait pas demandé son rôle
séparément — c'est la nomination qui le lui a donné. L'équipe, elle, survit : un
membre ordinaire garde le sien.

Trois populations sont désormais prévenues : l'ancien chef, les **victimes**
dont il relâchait le signal, et les **collègues** restés en service, pour que la
demande redevienne disponible chez eux.

### Un bug préexistant découvert en chemin

Le passage hors service d'un secouriste ordinaire ne prévenait **jamais** la
victime. Le code prévu pour ça reconstituait la liste depuis `visibles()` et sa
clé `auteur` — qui n'existe pas : cette vue expose `pris` et `mien`, jamais
l'auteur, précisément pour ne pas révéler qui a déclenché. La liste ne
contenait que des `None`, éliminés par le filtre de la ligne suivante.

`quitter_service` rend maintenant les numéros des victimes concernées : c'est le
seul endroit qui sache lesquels il relâche.

### Repli de page après destitution

L'écran restait sur la liste des demandes, sans barre d'onglets pour en sortir —
donc sans aucun moyen de déclencher une urgence. Le repli s'appuyait sur
`self._barre.isVisible()`, qui rend `False` dès qu'un ancêtre est masqué : un
joueur destitué **téléphone fermé** ne le déclenchait jamais. La décision se
prend désormais sur l'état, capté avant `appliquer()`.

---

## App Travail — un principe appliqué à moitié

L'app annonce « le serveur décide, le client dessine, chaque action renvoie un
état complet ». Mais cet état ne partait **qu'à l'auteur de l'action**.

Quatre conséquences :

- une mission prise ne prévenait pas son auteur ;
- une publication affichait « Nouvelle mission mécanicien » sans faire
  apparaître la mission dans la liste ;
- une mission close ou retirée laissait le preneur avec un bandeau de mission en
  cours qui n'existait plus ;
- **seul l'abandon prévenait l'auteur** — la preuve que le besoin était connu,
  mais traité une fois sur quatre.

`_travail_pousser_etat()` comble les quatre, calquée sur celle des groupes, avec
le même `sauf_ws` pour ne pas redessiner deux fois l'écran de celui qui a agi.
Elle est appelée **hors du `try`** : une poussée qui échoue ne doit pas
transformer une action réussie en erreur affichée.

### Noms au lieu de numéros

L'app affichait partout des numéros à six chiffres. Le répertoire local sait
déjà faire la substitution — `afficher()` s'y décrit comme le point d'entrée
**unique** du couple nom/numéro, par lequel tout ce qui montre un correspondant
est censé passer. Cette app était la seule à ne pas y passer.

Le numéro reste affiché quand le contact est inconnu, et c'est voulu : le
CircusPhone est un téléphone, ce numéro est ce qu'on compose. On ne reconnaît
quelqu'un que si on l'a noté.

S'ajoutent un bouton **Appeler** dans le bandeau, et **Ajouter aux contacts**
proposé seulement si le numéro est inconnu.

### Interface

- Le « Prise par … » est passé **dans** la carte. Il flottait au-dessus d'elle :
  avec deux missions publiées, on ne savait plus laquelle était prise.
- **L'échéance** apparaît sur les annonces ouvertes de l'auteur, et reste muette
  tant qu'il reste plus d'une semaine. Les missions expirent à 30 jours et rien
  ne l'indiquait.
- **Toutes les actions sont journalisées**, refus compris. Sur les quatre
  journaux du 28/08, l'app n'avait produit qu'**une seule ligne**.

### Panneau cassé après un appel

Le bouton « Appeler » a ouvert un chemin qui n'existait pas : quitter l'app
**panneau déployé**. Celui-ci se positionne en géométrie absolue calculée depuis
le bandeau ; au retour, elle se recalcule sur un bandeau qui n'a pas repris sa
taille, et le panneau se pose en travers de la barre d'onglets.

Le repli est fait aux deux bouts. `show_screen_outgoing` fait un simple
`setCurrentWidget` et **n'appelle pas `on_hide()`** — un repli à la sortie seul
n'aurait rien corrigé.

---

## Divers

### Comptes rendus d'urgence remontés avec le journal

Le journal de debug ne contenait que `position non lue`, qui dit **qu'on** a
échoué mais pas **pourquoi**. Le motif — divergence, lecture incomplète, ligne
SolarSystem illisible — n'était écrit que dans `logs_urgence\`, sur la machine
du joueur. Le 28/08, onze échecs sont restés hors de portée.

Ils sont désormais ajoutés **à la fin du journal**, après la troncature. Pas
dans une trame séparée : le serveur n'accepte qu'un `debug_log` par connexion,
une seconde serait jetée en silence. Les `.png` ne partent pas — plusieurs
centaines de Ko pour une trame plafonnée à 1 Mo.

### Voile « Réseau non disponible »

Hors connexion, le CircusPhone s'ouvrait normalement et montrait des écrans
peuplés de données périmées. Toutes les actions partaient dans le vide.

Le voile intercepte les clics par construction, et deux gardes couvrent le
clavier : le D-pad est avalé dans `_on_nav_key`, et `_go_home()` est appelé en
posant le voile — les apps marquées `CAPTURES_KEYBOARD` reçoivent leurs touches
par le focus Qt et auraient continué à tourner derrière un écran noir.

Il est aussi posé **à la création de l'overlay** : celui-ci est construit
paresseusement, et un joueur ouvrant son téléphone déjà déconnecté n'a jamais vu
passer le changement de statut.

### Liste des joueurs vidée à la déconnexion

Les cartes étaient déjà retirées, mais `state.players` et ses quatre
dictionnaires dérivés n'étaient purgés qu'à la réception d'un welcome. Entre une
déconnexion et la connexion suivante, le client gardait en mémoire la liste
complète du serveur qu'il venait de quitter.

### Décompte de distance visible

Le compte à rebours existait, mais partait dans un `setToolTip()` : sur un
overlay en jeu, une infobulle ne s'affiche jamais. Le secouriste voyait une
distance figée sans pouvoir distinguer « la mesure tourne » de « c'est bloqué ».

Les quatre points d'arrêt du décompte sont regroupés dans `_arreter_decompte()` :
répéter trois lignes à quatre endroits garantissait qu'un cinquième oublierait
l'effacement.

### Lisibilité

- « Aucun secouriste n'est actuellement disponible » passe de **11 à 13 px**.
- Les refus d'attribution de l'écran chef passent de **10 à 12 px**.
- Le message d'absence de distance — 110 caractères — s'affichait sur **une
  seule ligne** débordant de l'écran des deux côtés. `setWordWrap` ajouté.

### Whitelist

`drak_ironclad_assault` ajouté comme entrée **propre**, pas comme alias de
`drak_ironclad` : ce sont deux vaisseaux différents. Le fuzzy ne pouvait pas
rattraper — 21 caractères donnent un seuil de 3, l'écart vaut 8 insertions.
Sans cette entrée, les 14 graphies observées repartaient brutes ; 10 se
canonicalisent désormais.

---

## Ordre de déploiement

> Une seule ligne par commande sous `cmd`. Le `\` de continuation est une
> syntaxe de shell Unix.

**1. Le serveur.** *(fait les 29 et 31/08)*

```cmd
scp "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_server.py" "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_urgence_store.py" "D:\Projet CircusVOIP\fichiers serveurs\circusvoip_accounts.py" root@178.104.207.46:/home/circusvoip/app/
```

```cmd
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py && systemctl restart circusvoip-server.service"
```

**Contrôler les trois empreintes**, pas seulement redémarrer :

```cmd
ssh root@178.104.207.46 "md5sum /home/circusvoip/app/circusvoip_server.py /home/circusvoip/app/circusvoip_urgence_store.py /home/circusvoip/app/circusvoip_accounts.py"
```

**2. Le client.** *(fait le 31/08)* Vérifier les empreintes **avant** de
builder.

```cmd
py -3 build_update.py --bump-build --notes "Build 76 - OCR Pyro, urgence sans SolarSystem, app Travail, voile reseau"
```

**3. Pousser la release** :

```cmd
scp -r "D:\Projet CircusVOIP\updates" root@178.104.207.46:/home/circusvoip/
```

**Contrôles :**

```cmd
ssh root@178.104.207.46 "grep -o '\"build\"[^,]*' /home/circusvoip/updates/manifest.json"
```

```cmd
ssh root@178.104.207.46 "md5sum /home/circusvoip/updates/files/circusvoip_client.py /home/circusvoip/updates/files/circusvoip_sc_ocr.py /home/circusvoip/updates/files/circusvoip_phone_urgence_app.py /home/circusvoip/updates/files/circusvoip_phone_travail_app.py /home/circusvoip/updates/files/circusvoip_phone_travail.py"
```

`76`, puis les cinq empreintes du tableau.

---

## Outils — hors release

`circusvoip_mannequin.py` (`84b5853ee0d5679caea2a0c6e2145e90`) ne part **ni**
dans `RELEASE_FILES` **ni** sur le VPS.

- **Travail** : exercer un métier, prendre et abandonner une mission. Le premier
  bouton n'est pas un confort — sans métier correspondant, le serveur filtre la
  liste et le mannequin ne voit **aucune** mission. L'abandon non plus : le
  modèle n'autorise qu'une mission en cours par joueur.
- **Urgence** : déclencher et annuler une balise, à position **simulée**. Elle
  ne passe par aucun OCR, ce qui permet de tester diffusion, prise en charge et
  affichage sans dépendre de la lecture d'écran. Le mannequin envoie
  `system: None` avec un seul container — exactement la configuration qui
  faisait échouer les balises, donc chaque essai éprouve le correctif.

> Le `cid` est normalisé comme celui d'un vrai client. `state.container` porte
> par défaut `name:ooc stanton 4 microtech` avec des **espaces**, quand un
> client réel produit `name:ooc_stanton_4_microtech` — `_cle_container()` ne
> rattrape pas l'écart, et un test aurait conclu à tort à un bug de calcul de
> distance. Un `container_id` **numérique** l'emporte sur tout : c'est un
> identifiant d'instance unique.

`recuperer_logs_joueurs.ps1` : récupération incrémentale des journaux depuis le
VPS. Ne télécharge que ce qui manque, décompresse via .NET, et ne supprime le
`.gz` qu'après succès.

---

## Ce que ce build ne corrige pas

- **Un plantage à l'ouverture du téléphone**, observé le 28/08 à 22:03, sans
  aucune trace d'exception. Non reproduit depuis, jamais expliqué.
- **Le déclencheur du défaut OCR en Pyro** reste inconnu — voir plus haut.
- **La famille des bunkers** est absente de la whitelist. Le 31/08, dans un seul
  bunker : 7 containers distincts pour 26 lectures. Correctif prêt
  (`circusvoip_sc_ocr.py` `de1771d08e9d9eabc43736bcd0785e5f`), **pas dans ce
  build**.
- **Le seuil de coupure vertical reste serré** : à 2,6 m d'écart le volume vaut
  encore 1 %.
- **`dist = 0.0` à la réception d'une position** quand la sienne est inconnue
  (`circusvoip_client.py` l.3056).
- **Rien ne purge `/home/circusvoip/logs_joueurs/`** côté serveur. Le script
  PowerShell le fait depuis le poste, ce qui n'est pas une rétention.
- **La case d'atténuation verticale est toujours désactivée par défaut**, et
  chaque client calcule son propre volume.
- **`pip_packages/nvidia_ml_py-*.whl` introuvable** au build : les statistiques
  GPU manqueront à qui n'a pas déjà `pynvml`. Antérieur à ce build.
- **Le dépôt GitHub est en retard** de deux builds.
- **La zone collante peut confondre deux bunkers réels** : `bunker_027_cave_int_003`
  est à distance 3 de `bunker_013_cave_int_001`, sous le seuil de 6. Le passage
  par `pyro4` entre les deux décolle en pratique.
