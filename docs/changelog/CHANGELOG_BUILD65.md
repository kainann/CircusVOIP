# CircusVOIP — Changelog build 65

> **Version** : 0.4.0 **alpha** build 65
> **Statut** : préparé — *à marquer « publié le JJ/MM » une fois déployé*
> **Type** : mise à jour client uniquement — aucun changement serveur
> **Particularité** : introduit une option de test désactivée par défaut
> (atténuation verticale dans les vaisseaux).

---

## Nouveautés joueur

### 🚀 Le client démarre cinq fois plus vite

- La fenêtre apparaissait après **4,4 secondes**, parfois davantage, sans
  raison apparente. Elle s'affiche désormais en **0,8 seconde**.
- La cause : le module de suppression de bruit était chargé au démarrage et
  bloquait tout pendant 2,5 secondes. Il est maintenant chargé en arrière-plan
  et s'active dès qu'il est prêt.
- Effet de bord corrigé : la case « Suppression de bruit » pouvait apparaître
  **grisée à tort** selon le moment où elle était affichée, et un simple
  redémarrage semblait résoudre le problème.

### 🗂 Historique des serveurs

- Le champ **Serveur** devient une liste déroulante. Un bouton ▾ ouvre les
  serveurs auxquels vous vous êtes déjà connecté.
- Sélectionner une entrée remplit **aussi le mot de passe** correspondant :
  plus besoin de le retaper en changeant de serveur.
- Cinq serveurs sont mémorisés, le plus récent en tête.
- Un serveur n'est enregistré **qu'après une connexion réussie** : un mot de
  passe erroné n'est jamais conservé.

> Le mot de passe est stocké en clair dans la configuration du client, comme
> l'était déjà le champ précédent.

### 📞 Les joueurs proches entendent votre téléphone sonner

- Quand vous recevez un appel CircusPhone, votre sonnerie est désormais
  **diffusée en proximité**, à **70 %** de son volume local : les joueurs
  autour de vous perçoivent discrètement que votre téléphone sonne, comme
  s'il était rangé dans l'inventaire.
- L'atténuation de la distance s'ajoute par-dessus : à 10 m un joueur entend
  la sonnerie à environ 45 %, à 20 m à environ 11 %, plus rien au-delà de
  30 m.
- La sonnerie part **même si votre micro est coupé** : un téléphone qui sonne
  ne dépend pas du micro. Aucune voix ne fuit pour autant — seule la sonnerie
  est émise.
- Elle n'est diffusée **qu'en proximité** : jamais sur la radio, ni sur la
  radio de profil, ni dans un appel. Si vous appuyez sur une touche de parole
  pendant que ça sonne, la diffusion se met en pause le temps de l'appui ;
  la sonnerie continue de sonner normalement chez vous.
- Vous entendez toujours votre propre sonnerie à plein volume.

### 📻 La radio ne s'active plus sans canal

- Appuyer sur la touche radio **sans canal assigné** émettait du son que
  personne ne pouvait recevoir : la trame était relayée à une liste de
  destinataires vide.
- L'appui est désormais ignoré, comme il l'est déjà pendant un appel
  téléphonique. Rien n'est envoyé, pas de bip, pas d'ouverture du micro.
- Même règle pour la radio de profil : sans profil assigné par un
  administrateur, l'appui est sans effet.

### 🎤 Un micro en panne se voit enfin

- Nouvel indicateur rouge **MICRO INDISPONIBLE** dans la barre du haut, visible
  dès que le périphérique de capture n'a pas pu être ouvert.
- Jusqu'ici, ce cas ne se signalait que par une ligne discrète dans le
  journal : vous parliez sans que personne ne vous entende, et rien à l'écran
  ne distinguait « mon micro est hors service » de « personne ne me répond ».
- L'indicateur reste **masqué quand tout va bien** : la barre du haut est déjà
  chargée. Son infobulle rappelle la conduite à tenir (choisir un autre micro
  dans les Paramètres).
- Rappel : dans cette situation, vous entendez les autres et vous apparaissez
  bien sur la carte. Seule l'émission est perdue.

### ⌨ Raccourci pour le masque DisplayInfo

- Le masque qui cache la zone DisplayInfo peut maintenant être basculé par une
  **touche**, configurable dans les Paramètres à côté des autres raccourcis
  (ligne *Masque DisplayInfo*).
- Utile pour lire ponctuellement une information cachée par le masque sans
  ouvrir les Paramètres.
- La case des Paramètres reste le reflet fidèle de l'état : elle se coche et se
  décoche avec le raccourci.

### 🖥 Overlays — la croix est visible, les symboles sont dans le bon sens

- Le symbole en haut de chaque overlay indique désormais l'**état courant** et
  non l'action à venir : **✓ vert = overlay actif**, **× rouge = overlay
  inactif**. Un clic bascule dans les deux cas.
  L'ancienne convention obligeait à lire chaque icône à l'envers, ce qui était
  vite illisible avec plusieurs overlays affichés en même temps.
- **La croix était purement invisible** : le caractère utilisé n'est présent
  que dans peu de polices Windows, et Qt n'affichait alors rien du tout — alors
  que la coche, mieux supportée, s'affichait normalement. Un caractère présent
  dans toutes les polices latines l'a remplacé.

### ▾ Les listes déroulantes ont retrouvé leur flèche

- **Aucun** menu déroulant de l'application n'affichait de flèche : micro,
  sortie, canal, profil, historique des serveurs ressemblaient à de simples
  champs de texte, sans rien indiquer qu'on pouvait les dérouler.
- Corrigé dans **tout le thème**, et non seulement sur le nouveau champ
  Serveur.

### 🔊 Repère visuel de parole plus lisible

- Le contour vert qui pulse dans le sélecteur de micro est plus épais
  (2 à 6 pixels au lieu de 1 à 3).
- Correction associée : le trait était tracé sur le bord exact du widget et se
  trouvait donc **rogné de moitié** par Qt. Il apparaît maintenant en entier.

### 🧭 Champs Serveur et Mot de passe élargis

- Le champ Serveur ne laissait voir qu'une adresse tronquée, en particulier
  depuis l'ajout de la syntaxe `adresse:port`. Les deux champs ont été
  agrandis.

### 🛰 Atténuation verticale dans les vaisseaux — **option de test**

Nouvelle case dans **Paramètres**, **décochée par défaut** :
*« Atténuation verticale dans les vaisseaux (test) »*.

La portée de la proximité est une **sphère** de 30 mètres. Un joueur situé
2 mètres au-dessus, sur le pont supérieur, est donc à 2 mètres — c'est-à-dire
dans la zone de volume maximal, exactement comme s'il se tenait à côté de
vous. La cloison entre les ponts n'existe pas pour le calcul.

Activée, l'option aplatit cette sphère à l'intérieur des vaisseaux :

| Situation | Option désactivée | Option activée |
|---|---|---|
| 2 m en face, même pont | 100 % | 100 % |
| 1 m au-dessus | 100 % | 64 % |
| 2 m au-dessus | 100 % | 16 % |
| 3 m au-dessus | 100 % | 0 % |
| 10 m en face | 64 % | 64 % |

Points importants :

- **Aucun effet à l'horizontale** : les distances au sol sont inchangées.
- **Aucun effet hors des vaisseaux.** Les stations, hangars, planètes,
  grottes et l'espace conservent la bulle actuelle : ailleurs, une différence
  d'altitude n'implique pas de cloison, et quelqu'un sur une passerelle ou une
  caisse doit rester audible.
- Un vaisseau non encore répertorié conserve le comportement actuel. La liste
  s'enrichira au fil des rencontres.
- L'atténuation est **progressive**, pas une coupure : un seuil net ferait
  clignoter la voix d'un joueur situé pile à la limite au moindre défaut de
  lecture.

Le changement prend effet immédiatement, sans reconnexion.

---

## Notes internes

- Facteur de pondération fixé à 10, en dur dans `VERTICAL_WEIGHT_FACTOR`
  (`circusvoip_sc_ocr.py`). À réajuster après essai en jeu.
- **Mannequin de test aligné sur le client** : il applique la même pondération
  verticale (`VERTICAL_WEIGHT_FACTOR` importé depuis `circusvoip_sc_ocr.py`,
  repli à 10.0), sans quoi les deux calculaient des volumes différents pour la
  même scène et un test devenait ininterprétable.
  - Bouton **PROX VERTICALE : ON/OFF** — bascule à chaud, remplace la variable
    d'environnement `CIRCUSVOIP_VERTICAL_PROX` qui imposait un redémarrage pour
    changer d'avis. C'est le **récepteur** qui calcule l'atténuation : pour
    entendre l'effet en parlant au mannequin, c'est ce bouton qui compte, pas
    la case du client.
  - Bouton **POS + DZ** (avec champ de décalage en mètres, 2 par défaut) : copie
    la position du joueur en changeant uniquement le Z, container identique.
    Simule le pont du dessus sans mobiliser deux joueurs réels dans un vaisseau.
- Instrumentation ajoutée au démarrage audio : durée de chaque import,
  inventaire complet des périphériques avec leur interface, durée d'ouverture
  du micro. C'est elle qui a permis d'identifier la cause du démarrage lent
  après deux hypothèses erronées.
- **Flèche des combos** : la règle `QComboBox::drop-down` du style global a été
  **retirée**. Styliser ce sous-contrôle désactive le rendu natif de la flèche ;
  sans fournir en plus une image `::down-arrow`, plus rien n'était dessiné. Sans
  la règle, Qt redessine sa flèche native. On perd le contrôle de la largeur de
  la zone (18 px) et de sa bordure — sans conséquence visuelle notable, et bien
  préférable à une flèche absente.
- **Sonnerie en proximité** : `PHONE_RING_TX_FACTOR = 0.70` dans
  `circusvoip_audio_io.py`, pointeur de lecture TX distinct du pointeur local
  (`pop_ring_tx_frame` / `is_ring_tx_active`), injection dans le flux TX côté
  `circusvoip_core.py` avec écrêtage `np.clip` sur la somme voix + sonnerie.
  Atténuation pure, sans filtre passe-bas : choix assumé.
  Restriction au canal proximité **imposée par le codec** : le récepteur n'a
  qu'**un décodeur Opus par émetteur**, tous flags confondus. Émettre un contenu
  différent en `0x00` et en `0x01` au même instant ferait passer deux flux dans
  le même décodeur → artefacts. Un décodeur par couple émetteur/flag serait la
  solution propre, c'est un chantier à part.
- **Raccourci masque** : `mask_toggle_key` en config, `_do_mask_toggle()` passe
  par `cb_displayinfo_mask.setChecked()` plutôt que d'agir directement, pour que
  la case reste la source de vérité et que la sauvegarde reste dans son handler.
- **Indicateur micro** : `lbl_mic_status`, piloté par `not ok_in` au retour du
  démarrage audio (cas observé : micro Corsair exposé sous plusieurs API
  Windows, la variante WDM-KS refusant l'ouverture en `-9996`).
  Les threads OCR / casque / heartbeat sont lancés **dans tous les cas**, y
  compris capture KO : un joueur sans micro doit entendre et apparaître sur la
  carte.
