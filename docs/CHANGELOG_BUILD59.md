# CircusVOIP — Changelog v0.3.0 alpha, build 59

**Statut : préparé, non publié** · Correctifs sur le build 58 (08/07/2026)

Build de correctifs centré sur Sol VS Terra et les raccourcis clavier,
suite aux premiers retours du build 58.

---

## 🚢 Sol VS Terra

- **Le skin « vaisseaux » s'affiche enfin par défaut.** Le jeu démarrait sur
  le skin « classic » (sans images) à cause d'un nom de skin obsolète dans le
  code de sélection ; il démarre maintenant sur le skin illustré dès que ses
  images sont présentes.
- **Le sprite d'un navire coulé apparaît sur la grille ennemie (réseau).**
  Jusqu'ici, en multijoueur, couler un navire n'affichait que des croix : le
  client du tireur ne connaissait que le NOM du navire coulé, jamais ses
  cases. Le défenseur transmet désormais les cellules du navire coulé, et le
  vaisseau apparaît sous les impacts avec son voile rouge « coulé ».
  (Rétro-compatible : face à un client build 58, pas de sprite mais aucun
  dysfonctionnement.)
- **Parties limitées à 2 joueurs** (1v1 strict). Le lobby acceptait jusqu'à
  8 joueurs alors que le jeu n'a que deux flottes ; un 3ᵉ joueur reçoit
  désormais « Partie pleine ». (Changement côté serveur.)
- **Changement de skin : touche V** (au lieu de C) — voir raccourcis.

## ⌨ Raccourcis : la touche C rendue à Star Citizen

C sert à **s'allonger** chez certains joueurs : comme Espace (saut) au
build 58, elle ne doit jamais être confisquée par les jeux du téléphone.
- **Sol VS Terra** : changer de skin = **V** (au lieu de C).
- **Valakkar** : changer de skin = **V** (au lieu de C).
- **Poker** : checker / suivre = **V** ou Entrée (au lieu de C ou Entrée) ;
  F = se coucher, inchangé. Aide et libellés mis à jour.
- **Client** : la touche C n'est plus interceptée ni bloquée — s'allonger
  fonctionne même en pleine partie. V est routée vers les jeux à la place.

## 🖼 Fond d'écran par défaut

- Le fond par défaut est maintenant cherché **dans le dossier des fonds**
  (`circusvoip_phone_wallpapers/hugolisoir.png`), puis à la racine du client
  en repli. Au build 58, il n'était cherché qu'à la racine : les joueurs
  voyaient le dégradé sombre au lieu de l'image par défaut.

## 🛠 Serveur (déploiement séparé, hors paquet client)

- `circusvoip_mp_server.py` : capacité Sol VS Terra passée de (2, 8) à
  **(2, 2)** — à pousser sur le VPS par scp + restart du service.

---

### Fichiers modifiés (client, distribués par la mise à jour)
`circusvoip_client.py` · `circusvoip_phone_solvsterra.py` ·
`circusvoip_phone_poker.py` · `circusvoip_phone_valakkar.py`

### Fichier serveur (VPS)
`circusvoip_mp_server.py`

### Tests recommandés avant publication
1. Sol VS Terra s'ouvre directement avec le skin vaisseaux ; V le change,
   **C t'allonge** sans effet dans le jeu.
2. Partie réseau : couler un navire adverse → son sprite apparaît sous les
   croix ; un 3ᵉ joueur est refusé à l'entrée du lobby.
3. Poker : V (ou Entrée) suit/checke, F se couche, C t'allonge.
4. Premier lancement sans fond choisi → l'image par défaut s'affiche
   (plus le dégradé sombre).
