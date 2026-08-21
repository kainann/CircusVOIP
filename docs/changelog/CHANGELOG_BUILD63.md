# CircusVOIP — Changelog build 63

> **Version** : 0.4.0 **alpha** build 63
> **Statut** : build de mesure — *non destiné aux joueurs*
> **Type** : outillage interne uniquement, aucun changement fonctionnel
> **Particularité** : build de **référence « avant optimisation »**, conservé
> pour comparer les débits avec les versions suivantes.

---

## Pourquoi ce build existe

Le chantier suivant (passage du son en Opus) devait être mesuré avant/après.
Il fallait donc un client identique à la v0.3.0 sur le plan fonctionnel, mais
identifiable sans ambiguïté sur une capture d'écran, et capable de vérifier
les mises à jour depuis un serveur de développement.

Aucune fonctionnalité joueur n'a été modifiée.

---

## Changements

### Titre de fenêtre

- Le titre affichait un libellé explicite **« AVANT OPTI - audio float32 »**
  à la place du numéro de build, pour qu'un artefact de mesure ne puisse pas
  être confondu avec une version postérieure à Opus.
- Purement cosmétique : le numéro de build réel reste celui de
  `circusvoip_version.json`, et c'est toujours lui que l'updater compare.

### Bouton « Vérifier les MAJ » réaffiché

- Le bouton avait été masqué au build 62 (deux `addWidget` commentés) pour la
  release stable. Il est réactivé ici afin de pouvoir tester la distribution
  depuis le serveur de développement.
- ⚠ **À recommenter avant toute release stable destinée aux joueurs.**

---

## Notes internes

- Ce build a été compilé en `.exe` pour servir de témoin. Il ne doit pas
  être publié sur le serveur de mise à jour de production.
- Le compteur de build est partagé entre `build_update.py` et le `.iss` :
  toute release publique ultérieure doit porter un numéro **supérieur** à
  celui-ci, sinon les clients ne la recevront jamais.
