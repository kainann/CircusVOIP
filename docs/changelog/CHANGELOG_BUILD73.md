# CircusVOIP — 0.4.0 Alpha Build 73

**Statut** : **publié le 18/08/2026** — serveur et client.
**Type** : mise à jour **client + serveur**. Le serveur doit être redéployé, et
**avant** le client.

> ⚠️ **Ce build a été publié DEUX FOIS, sous le même numéro.** Le premier
> upload ne contenait ni `circusvoip_phone_urgence.py` ni
> `circusvoip_phone_urgence_app.py` : ils n'avaient pas été ajoutés à
> `RELEASE_FILES` de `build_update.py`. Le manifest a été **republié le
> 18/08 sans incrément de build**, avec les 37 fichiers complets.
>
> **Conséquence à connaître** : l'updater compare `remote.build > local.build`
> strictement (`circusvoip_client.py` l.1364). Un client ayant pris le
> **premier** 73 se croit à jour et ne récupérera **jamais** les deux modules —
> il faut les lui copier à la main, ou attendre le build 74. Les clients restés
> en 72 ou antérieur reçoivent, eux, un 73 complet.
>
> **Symptôme du 73 incomplet** : aucune erreur, aucun plantage. Le registre
> importe chaque app dans son propre `try/except` et ne l'ajoute au home que si
> l'import réussit — l'icône Urgence est simplement absente, avec une ligne
> `[PHONE REGISTRY] app ignoree` sur stderr. Un porteur de rôle reçoit malgré
> tout le son et le badge de notification, sans rien à ouvrir.

| Fichier | Destination | Empreinte |
|---|---|---|
| `circusvoip_server.py` | VPS | `59f740d405f6e868e90f651ef64cb40b` |
| `circusvoip_accounts.py` | VPS | `d24b0f7655bf155bf40f65a4abee693d` |
| `circusvoip_urgence_store.py` | VPS | `bfbd482bc516098bb19808691a7d8360` |
| `circusvoip_phone_urgence.py` | **VPS + build** | `6ab6787a39fa853a4bdaa7b9301cf191` |
| `circusvoip_sc_ocr.py` | build | `0acc6b968401351236fc669a85d9199c` |
| `circusvoip_core.py` | build | `174a7279d989e3313efa3a9e6bdea135` |
| `circusvoip_audio_io.py` | build | `0f12af4a750be128eb3ecc1df708d3d5` |
| `circusvoip_client.py` | build | `eecdef6b5bc87d0518ebe00b34879dd7` |
| `circusvoip_phone_urgence_app.py` | build | `cfdfda21a6bbfd8197f8373af55761ab` |
| `circusvoip_phone_registry.py` | build | `bea3105f7ebf110f0314c9d278c0f7bd` |
| `circusvoip_admin.py` | outil | `71c003d291b7038755d0b8d0e27bea61` |

> **`circusvoip_phone_urgence.py` va des DEUX côtés**, comme
> `circusvoip_phone_travail.py`. Ne le pousser que d'un côté fait diverger le
> client et le serveur — sans erreur visible : ils appliqueraient simplement
> des règles différentes, et l'un accepterait ce que l'autre refuse.

> **`sc_ocr`, `core` et `audio_io` sont couplés.** Le core importe
> `_is_big_hangar` du module OCR et appelle `set_echo_profile()` d'audio_io.
> Pousser l'un sans les autres donne un `ImportError` au démarrage.

> Aucune purge, aucune perte de données. Les jetons déjà émis restent valides.

> **Où ranger les nouveaux fichiers** (cf. `CIRCUSVOIP_INFRA.md` §7) :
> `circusvoip_urgence_store.py` va dans `fichiers serveurs\` — cas B, serveur
> seul, **jamais** dans `RELEASE_FILES` ; `circusvoip_phone_urgence.py` et
> `circusvoip_phone_urgence_app.py` vont à la racine — cas C pour le premier
> (partagé, poussé aussi par scp), cas A pour le second. Les deux sont à
> ajouter à `RELEASE_FILES`, et `circusvoip_urgence_store.py` au tableau du §6
> de l'INFRA.

---

## L'essentiel

Ce build apporte l'**app Urgence** — un joueur en difficulté déclenche un
signal médical ou sécurité, et des secouristes le retrouvent grâce à sa
position lue à l'écran.

Il corrige aussi deux bugs préexistants trouvés en chemin : les grottes de
roche ne déclenchaient jamais l'écho audio, et l'appariement de containers
pouvait produire une fausse proximité entre deux stations de Stanton.

---

## 1. App Urgence

**Le besoin.** Un joueur blessé, échoué ou attaqué n'a aucun moyen d'appeler
quelqu'un qui ne connaît pas déjà son numéro. L'app Travail fait se rencontrer
les joueurs par métier, mais sur des jours ; une urgence se compte en minutes.

**Un signal, pas un appel.** C'est le point de départ de toute la conception.
Un signal ne porte pas de numéro, donc il ne distribue pas d'annuaire, donc il
ne contourne pas la règle RP qui veut que les numéros s'échangent en jeu. C'est
ce qui distingue cette app du bouton d'appel de l'overlay et des groupes, tous
deux bloqués sur cette question depuis des mois.

**Deux corps cloisonnés.** Médical et sécurité. Un médecin ne voit rien de la
sécurité — ni les signaux, ni les collègues, ni le chef d'en face. Une
agression produit souvent les deux besoins : c'est à la victime de déclencher
deux signaux si elle le juge utile, pas au code de le décider.

**Les rôles ne sont pas des métiers.** Les huit métiers de l'app Travail sont
auto-déclarés : le joueur coche, le serveur valide contre une liste fermée. La
validation porte sur *quoi*, jamais sur *qui a le droit*. `medecin` et
`securite` sont **attribués** par un chef, et vivent dans un champ séparé de la
fiche, écrit par un autre chemin. S'ils partageaient la même liste, un client
bricolé enverrait `travail_metiers` avec `["medecin"]` et se décernerait le
rôle.

Effet de bord voulu : le plafond de deux métiers ne s'applique pas aux rôles.
Un mineur-pilote peut être médecin.

**Chefs.** Un par corps, désigné depuis la console d'admin. Il distribue son
rôle par numéro, immédiatement, sans acceptation du destinataire. Ça ouvre un
oracle d'existence — un chef peut énumérer les numéros et savoir lesquels sont
attribués — mais il n'est ouvert qu'aux deux comptes désignés à la main par un
administrateur. C'est assumé, et écrit dans le code pour que la question ne
ressorte pas dans six mois comme une faille qu'on croirait avoir manquée.

Un chef ne peut pas faire de chef : sinon le contrôle se perdrait en deux
sauts.

**Prise de service.** Un détenteur de rôle connecté n'est pas de garde. Il
pointe quand il accepte de répondre, et il est hors service à chaque connexion.
Sans ce mécanisme, « aucun secouriste disponible » serait faux dès qu'un
médecin joue à autre chose, et l'écran d'équipe du chef afficherait qui est
connecté — une information subie plutôt que déclarée.

Le chef est en service de fait dès sa connexion : il est le filet de sécurité
du dispositif. Sa seule échappatoire est de se déconnecter.

**Prise multiple.** Plusieurs secouristes peuvent prendre la même demande — une
équipe intervient à plusieurs. Le nombre de preneurs n'est affiché nulle part :
il n'aide pas à décider et il révèle qui fait quoi. Un contour vert dit
seulement que quelqu'un s'en occupe déjà, ce qui n'interdit rien.

**Durée de vie.** En RAM, jamais sur disque. Un joueur déconnecté est un signal
abandonné. Expiration à une heure. Un signal restauré au redémarrage décrirait
une victime qui n'est plus connectée, à une position qui n'a plus cours.

---

## 2. Lecture de la position — `capture_hierarchy()`

**Le problème.** La boucle temps réel ne lit qu'une ligne du DisplayInfo : le
container immédiat du joueur. Une position locale ne dit rien à quelqu'un qui
n'est pas dans le même container — et c'est précisément le cas d'une urgence.

**Le principe.** Une capture haute, une seule passe OCR, découpe du texte sur
les occurrences de `Zone:`, arrêt à `SolarSystem`. Le découpage se fait sur le
texte et non sur la géométrie : les lignes du HUD ne font pas exactement la
hauteur calibrée, le décalage s'accumule, et un niveau entier passait entre
deux bandes — `levski_all-001` a disparu ainsi lors des premiers essais, sans
que la chaîne cesse d'être plausible.

**Aucune interprétation des niveaux.** On ne cherche pas à savoir lequel est la
planète, l'avant-poste ou le vaisseau. La profondeur varie selon les lieux —
Levski a un seul niveau intermédiaire là où un site minier en a deux — un
vaisseau peut être à l'intérieur d'un autre, et rien dans le HUD ne déclare la
nature d'un niveau. Toute règle de position se serait trompée sur au moins un
des cas relevés.

**Double lecture pour la victime, simple pour le secouriste.** La double
lecture protège un one-shot : une victime au sol ne peut pas relancer sa
capture. Le secouriste, lui, remesure régulièrement et **se déplace** — en vol,
ses coordonnées planétaires bougent de 233 m entre deux lectures espacées de
trois secondes, là où la tolérance est de 5 m. Élargir la tolérance aurait été
le mauvais correctif : à plusieurs centaines de mètres près, elle n'aurait plus
rien attrapé.

**Repli si la victime bouge.** Un blessé fuit ou rampe. Quand la double lecture
échoue, on accepte une lecture unique et l'écran le dit — « position relevée en
déplacement ». Refuser serait pire : sans signal, personne ne vient.

**Noms bruts, sauf trois catégories.** Les corps planétaires et les points de
Lagrange prennent leur nom de starmap — `ooc_stanton_2b_daymar` devient
« Daymar », `ooc_stanton1_l1` devient « HUR-L1 ». Les grottes et les bunkers
deviennent « Grotte » et « Bunker », parce qu'ils n'ont pas de nom : le seul
renseignement utilisable est leur nature, et c'en est un vrai. Tout le reste
garde son nom technique, affiché tel quel — un avant-poste maquillé serait
indétectable, alors que la liste des corps planétaires est fixe et courte.

Le nom brut reste affiché sous la phrase : « Grotte » dit quoi chercher, le nom
technique dit laquelle.

---

## 3. Distance — trois règles, pas une

**Le piège.** Les astres orbitent. Un joueur immobile sur Hurston garde des
coordonnées locales identiques au centimètre pendant que ses coordonnées
SolarSystem dérivent de 2,2 km par lecture — environ 600 m/s. Une position
figée y vieillit de 2 100 km par heure.

**Container commun** — la distance se calcule dans les coordonnées locales du
niveau commun le plus profond, qui ne dérivent pas. Vérifié en session : 3,4 m
dans un bâtiment de Bloom, stable.

**Pas de container commun, mais un astre dans une des chaînes** — pas de
distance du tout. L'écran affiche la destination, et c'est elle qui sert : on
ne navigue pas vers un vecteur, on sélectionne un astre dans son ordinateur de
vol.

**Ni container commun ni astre** — vaisseau en vol, champ d'astéroïdes : la
distance système est fiable, puisque rien n'orbite. C'est aussi le seul cas où
il n'existe aucune destination sélectionnable, donc où les coordonnées sont la
seule information disponible.

**Exception pour la liste.** Choisir une demande n'exige pas une distance
juste, seulement de savoir si c'est loin. La liste autorise donc le repli
système malgré un astre, mais n'affiche que des tranches : sur place, proche,
loin, autre système. Une erreur de 2 000 km sur un écart de 12 millions ne
change aucune décision.

**Containers non appariables.** `ObjectContainer_Commercial` est partagé par
toutes les stations de Stanton, avec des coordonnées locales à chacune : deux
joueurs dans deux stations différentes seraient affichés à trente mètres l'un
de l'autre. Les hangars sont tous personnels, donc deux joueurs ne peuvent
jamais y être ensemble. Dans les deux cas on remonte d'un niveau.

**Cadence.** Une mesure ponctuelle à l'ouverture de la liste — une seule suffit
pour classer toutes les demandes, c'est la position du secouriste qui coûte six
secondes, pas la comparaison. Puis un rafraîchissement continu de 10 à 30 s
selon la distance, en échelle logarithmique, uniquement après la prise en
charge. La boucle de proximité n'a pas de cadence fixe et consomme tout ce que
la machine donne : chaque mesure lui est prise.

---

## 4. Reverb dans les grands hangars

Demande joueur. La reverb des grottes ne sonnait pas juste dans un hangar : une
caverne est un volume irrégulier et absorbant, un hangar une grande boîte
métallique ouverte.

Deux profils au lieu d'un réglage. Trois différences, mesurées sur une
impulsion :

| | 1ʳᵉ réflexion | Queue à −60 dB | Brillance | Wet |
|---|---|---|---|---|
| grotte | 30 ms | 0,57 s | 4 280 Hz | 0,60 |
| hangar | 90 ms | 1,60 s | 10 260 Hz | 0,40 |

Le **pré-délai de 30 ms** est ce qui manquait : dans un grand volume, la
première réflexion arrive nettement après la voix directe, et c'est ce silence
qui fait entendre l'espace. Sans lui, même avec une longue queue, l'oreille
perçoit une petite pièce réverbérante.

Les buffers des deux profils sont alloués au démarrage — environ 90 ko. Allouer
au changement de lieu voudrait dire allouer pendant que le callback audio lit.

Large et XL seulement. `set_cave_echo()` existe toujours pour les appelants
historiques et le mannequin.

---

## 5. Corrections

**Les grottes de roche ne déclenchaient jamais l'écho.** `_is_cave_container()`
normalise les sept premiers caractères en remplaçant `o` par `0` — et
`rock01_` contient un `o`, donc devenait `r0ck01_`, qui ne correspondait plus
au préfixe cherché. Seules les grottes de sable fonctionnaient, `sand01_` ne
contenant ni `o` ni `l`. Le bug était invisible : rien n'échouait, l'écho
manquait simplement. Trouvé en branchant l'affichage « Grotte » sur cette même
fonction.

**Trois entrées de whitelist corrompues.** `rockol_occu_091_*` était écrit avec
les lettres `o` et `l`. Comme le normaliseur convertit `rockol` en `rock01`
avant comparaison, ces entrées ne pouvaient jamais correspondre exactement à
quoi que ce soit : elles ne servaient qu'à attirer d'autres grottes par fuzzy.
Une grotte réellement lue s'est fait rabattre sur l'une d'elles en session.

**Le suffixe `_L1` des points de Lagrange était mangé.** L'OCR rend
`OOC_Stanton1_L1` en `OOc Stantonl Ll`, et le nettoyage des parasites retirait
le `Ll` final comme deux caractères isolés. On obtenait `ooc_stanton1`, sans
moyen de distinguer L1 de L5 — ce qui affectait aussi l'audio de proximité,
deux joueurs à HUR-L1 et HUR-L4 partageant le même identifiant de container.

**Séparateurs décimaux doubles.** Un motif `,.` collé à un chiffre ne faisait
pas échouer le parsing, il le faisait mentir : sur `12849644,.3530km`, le
nombre est coupé en deux, les trois coordonnées glissent d'un cran et la
dernière est perdue. Le résultat restait parfaitement plausible. Le segment est
maintenant refusé.

**Verrou sur EasyOCR.** La boucle temps réel et `capture_hierarchy()` partagent
le même objet reader — `readtext()` n'est pas réentrant, et deux appels
simultanés depuis deux threads corrompent son état interne, ce qui se
manifesterait par des positions fausses sans la moindre erreur.

**Threads Qt parentés.** Sans parent, l'objet C++ d'un `QThread` peut être
détruit alors que le thread tourne : Qt abandonne alors le processus avec
`QThread: Destroyed while thread is still running`, **sans exception Python**.
Le client se ferme et rien n'apparaît dans le journal.

**Commandes admin envoyées depuis la boucle asyncio.** Les deux requêtes
automatiques à la connexion échouaient systématiquement avec « WS pas prêt ».
`_ws_send_safe()` planifie une coroutine puis attend son résultat une seconde —
appelée depuis la boucle elle-même, elle attendait un travail que la boucle ne
pouvait pas exécuter, puisqu'elle était bloquée à attendre. Les deux repassent
par le thread Tk, avec cinq reprises espacées d'une seconde : une commande
déclenchée par un clic part toujours, une commande automatique n'a pas ce luxe,
et son échec laissait le bandeau CHEFS vide en silence — ce qui ressemble à
« aucun chef désigné » alors que la question n'avait pas été posée.

**Méthodes de saisie perdues dans une réécriture.** `_entrer_dans_champ`,
`_champ_vide` et `_sortir_du_champ` avaient disparu en réécrivant la navigation
pour les trois onglets, alors qu'elles étaient encore appelées : valider sur un
champ levait une `AttributeError`. Un contrôle systématique — méthodes appelées
mais absentes — a été ajouté à la vérification.

**Champ numéro sans traitement clavier.** Le champ de recrutement était un
`QLineEdit` brut : le retour arrière sortait de la saisie au lieu d'effacer,
donc une faute de frappe ne se corrigeait pas. Il a désormais sa classe, comme
la description.

**Croix de retrait inatteignables au D-pad.** Elles n'étaient pas dans la liste
des cibles de navigation : un chef pouvait recruter mais jamais retirer, son
équipe ne sachant que grandir. Elles sont recollectées à chaque reconstruction
de la liste, comme les boutons « Prendre ».

**Confirmation de retrait invisible.** L'écran de confirmation vit dans la pile
de l'onglet Urgence ; déclenché depuis l'onglet Service, il basculait une pile
que personne ne regardait, et la croix semblait morte.

**Zones ajoutées à la whitelist**, toutes réellement observées :
`rock01_unoc_001_size02_001_int`, `int_s1_dc_sasu_magnolia`,
`ab_mine_stanton1_sml_003`, les vingt points de Lagrange de Stanton, et les
huit hangars « Collector ».

**Normalisation `stanton` + lettre → chiffre.** SC écrit toujours un numéro de
système après `stanton` ; l'OCR y rend régulièrement un `l`. La correction est
ciblée sur ce mot, donc elle ne peut pas casser `levski` ou `calliope`.

---

## Ordre de déploiement

**1. Serveur d'abord.** Le client émet des trames `urgence_*` qu'un serveur non
mis à jour ignorerait en silence : l'app afficherait un écran vide, sans
erreur.

> Les commandes ci-dessous sont à passer depuis `cmd` sous Windows : **une
> seule ligne chacune**. Le `\` de continuation est une syntaxe de shell Unix,
> il ne fonctionne pas ici.

```cmd
scp circusvoip_server.py circusvoip_accounts.py circusvoip_urgence_store.py circusvoip_phone_urgence.py root@178.104.207.46:/home/circusvoip/app/
```

```cmd
ssh root@178.104.207.46 "chown circusvoip:circusvoip /home/circusvoip/app/*.py && systemctl restart circusvoip-server.service"
```

Vérifier l'absence de bandeau `[URGENCE] *** APP URGENCE DESACTIVEE ***` :

```cmd
ssh root@178.104.207.46 "journalctl -u circusvoip-server.service -n 40 --no-pager | grep -i 'URGENCE\|TRAVAIL\|COMPTES\|Traceback'"
```

**2. Puis le client.** ⚠ **Avant de builder**, vérifier que les deux modules
Urgence sont bien dans `RELEASE_FILES` — c'est l'oubli qui a coûté un second
upload :

```cmd
findstr /n "urgence" build_update.py
```

Trois lignes attendues : les deux `.py`, plus `circusvoip_urgence_store.py`
dans le bloc « NE JAMAIS ajouter ici ».

```cmd
py -3 build_update.py --bump-build --notes "Build 73 — app Urgence, reverb hangars"
```

**Puis contrôler ce qui est réellement parti.** `build_update.py` n'a aucun
moyen de savoir qu'une liste est incomplète : il copie ce qu'on lui donne, sans
se plaindre. Ce contrôle est le seul qui rattrape l'oubli :

```cmd
ssh root@178.104.207.46 "ls /home/circusvoip/updates/files/ | grep urgence"
```

```cmd
ssh root@178.104.207.46 "grep -c urgence /home/circusvoip/updates/manifest.json"
```

Deux fichiers, et `2`. Un fichier présent dans `files/` mais absent du manifest
ne serait jamais téléchargé — l'updater ne lit que le manifest.

**3. Enfin, nommer les premiers chefs** depuis la console d'admin, bandeau
CHEFS. Sans eux, personne ne peut distribuer de rôle, donc personne n'est en
service, donc toute demande est refusée.

---

## Ce que ce build ne corrige pas

- **Les clients ayant pris le premier 73 n'auront jamais l'app Urgence** par
  l'updater (cf. encadré en tête). À traiter au build 74, ou par copie manuelle
  des deux fichiers.
- **L'app n'a jamais tourné à deux clients réels.** Toute la validation
  serveur est faite en tests isolés ; le parcours complet victime → secouriste
  n'a jamais été joué à deux. Ce qui A été vérifié en conditions réelles le
  18/08 : déploiement serveur, désignation d'un chef depuis l'admin,
  affichage du rôle côté client, recrutement d'un second numéro.
- **Le bouton « Situer les demandes »** reste en l'état : il occupe le bas de
  l'écran même quand la liste est vide, et son libellé ne dit pas qu'il lit
  l'écran pendant six secondes. À trancher — le masquer, le rendre
  automatique, ou le supprimer au profit du seul ordre chronologique.
- **Le crash à l'ouverture du téléphone** observé le 15/08 : une cause
  plausible a été trouvée et corrigée (thread Qt sans parent), mais elle n'a
  pas été reproduite dans le client. Si ça revient, il faut la sortie console.
- **`ab_mine_*` et les stations isolées** ne sont dans aucune table d'astres :
  elles orbitent probablement, mais passent pour non-orbitales et autorisent
  donc le repli système. Deux déclenchements au même endroit à une minute
  d'intervalle les classeraient.
- **Le bandeau CHEFS de l'admin est temporaire** dans sa forme : posé à côté
  du bandeau de débit plutôt qu'intégré, en attendant la refonte de
  l'interface.
- **La cadence de rafraîchissement (10–30 s)** n'a été éprouvée que par un
  seul joueur. À deux, chacun consomme de l'OCR pendant que la boucle de
  proximité tourne des deux côtés.
- **Le chef ne peut pas se mettre hors service** autrement qu'en se
  déconnectant.
- **Les descriptions ne sont pas modifiables** après création d'une demande ;
  seule la position s'actualise.
