# CircusVOIP — Changelog build 54 → build 56

**Cible :** fork `firesstones/circus-voip` tag `v0.2.3-build54` → build 56 upstream Kainan
**Date upstream :** 29/05/2026
**Statut :** testé en jeu, OCR validé (parses_ok 90-100% en condition réelle, zéro régression)

Ce document liste tous les changements **upstream** entre le build 54 (base de ton fork) et le
build 56 actuel. Les numéros de ligne sont ceux de la **version upstream build 56** sauf
indication contraire.

Les modifs propres à ton fork (intégration `circusvoip_circus_ocr_client`, désactivation
de l'updater, support Linux, hot-reload hotkeys, etc.) sont **à conserver côté fork** : aucun
changement upstream ne les écrase ni n'entre en conflit avec elles.

---

## 1. `circusvoip_sc_ocr.py` — gros changements à reporter

Le fichier passe de **4442 lignes** (`_legacy.py` build 54 dans ton circus-ocr) à **4788 lignes**
(build 56 upstream). 346 lignes nettes ajoutées.

### 1.1 Doc d'en-tête module (lignes 64-110, +47 lignes)

Nouvelle section "API publique étendue" dans le docstring du module. Énumère les
fonctions/alias publics destinés aux consommateurs externes (notamment ton service
`circus_ocr` et ton `circusvoip_circus_ocr_client.py`). Purement documentaire.

### 1.2 Whitelist de zones connues — ajouts de containers SC

Plusieurs ajouts dans la liste blanche des `_KNOWN_CONTAINERS` (zones canoniques utilisées
pour le fuzzy match) :

| Lignes (build 56) | Zone(s) ajoutée(s) | Origine |
|---|---|---|
| 699-713 | `hangar_{xltop,xlfront,largetop,largefront,mediumtop,mediumfront,smalltop,smallfront}_grimhex` (8 hangars GrimHex) | Observation 23/05/2026 |
| 732 | `transitcarriage_a18_shuttle_{a,b}` (shuttles A18) | Observation |
| 738-748 | `transportcarriage_stanton_grimhex_elevator_{default,mainconcourse}` | Observation 23/05/2026 |
| 825 | `objectcontainer-lorville_sp_int` (**fix** : avant c'était `5p_int`, hypothèse erronée — le HUD affiche `sp` pour Spaceport, non `5p`) | Fix 26/05/2026 |
| 836-842 | `objectcontainer-gate1_int` + `objectcontainer-gate_0{1..6}_int` (gates Lorville) | Observation 26/05/2026 |
| 856-865 | 4 zones Orison : `hangar_mediumfront_orison`, `oc_arcade_int_001`, `oc_orison_hospital_int_001`, `spaceport_interior`, `spaceport_transit` | Observation 23/05/2026 |
| 866-869 | `transitcarriage_orison_{hospital,elev_ht_circular,util_a,shuttle_a}` | Observation |
| 875 | `objectcontainermodifier-003` (nouveau type d'asset SC) | Observation 23/05/2026 |
| 941-942 | `objectcontainer-000` (GrimHex) et `objectcontainer-028` (Orison) | Observation 23/05/2026 |
| 974 | `p2l4_contestedzone` (Checkmate Station Pyro) | Observation 23/05/2026 |
| 979 | `rs_cz_rewards_001` (loot terminal Checkmate) | Observation 23/05/2026 |

**À noter** : les commentaires inline expliquent en détail pourquoi chaque entrée a été
ajoutée et **interdisent** l'extrapolation (ex : ne PAS ajouter `objectcontainer-001` sans
observation, sinon fausses proximités via fuzzy match).

### 1.3 `_OCR_NAME_FIXES` — bloc massif de corrections OCR (lignes 1378-1492, +115 lignes)

Nouvelles paires (lecture OCR pourrie → forme canonique) suite à observations
GrimHex/Orison/Pyro :

- **"De fault" → "Default"** (et 3 variantes) — EasyOCR split visuellement "Default" sur
  les TransportCarriage GrimHex
- **"Starton" → "Stanton"** (et 2 variantes) — confusion `n` → `r` sur certains angles
- **"bjectContainer" → "ObjectContainer"** (avec prefixe espace pour éviter collision avec
  `ObjectContainerModifier`)
- **"ObjectContaine" → "ObjectContainer"** (suffixe r mangé)
- **"ObjedtContainer" → "ObjectContainer"** (c → d après O)
- **"Crhs sader" → "Crusader"** — variante 2 Crusader Stanton
- **"arcadle" / "arcarle" / "argade" → "arcade"** — variantes Orison

Tous prefixés/suffixés d'espaces pour éviter les matches au milieu d'un mot.

### 1.4 NOUVELLE FONCTION publique `ocr_texts_from_region(region)` (lignes 4427-4533, +107 lignes)

```python
def ocr_texts_from_region(region):
    """Capture une region et retourne le texte OCR brut.
    
    Pipeline : capture mss → preprocessing (gamma, denoise, resize x4,
    restauration tirets visuels) → EasyOCR + fallback Tesseract.
    Retourne {text, texts, pipeline, minus_was_restored} sans parsing
    métier de coordonnées.
    """
```

**Important pour ton fork** : cette fonction est **calquée verbatim** sur la version que
tu avais ajoutée toi-même dans ton `circus-ocr/_legacy.py` (lignes 3789-3894). Tu peux
maintenant :
- soit **supprimer** ta version du `_legacy.py` (elle est devenue redondante)
- soit la **conserver** en parallèle (pas de conflit, mais code mort)

Si tu rebases ton `_legacy.py` sur le sc_ocr.py upstream build 56, tu auras directement
cette fonction au bon endroit.

### 1.5 SECTION API PUBLIQUE — 11 alias + setter + getter (lignes 4709-4788, +80 lignes)

Nouvelle section en fin de fichier qui expose proprement les fonctions internes
consommées par ton fork. **Aucun changement de logique** : ce sont des références aux
mêmes objets, juste sous un nom sans prefixe `_`.

| Nom public | Pointe vers | Consommateur actuel chez toi |
|---|---|---|
| `ensure_imaging` | `_ensure_imaging` | `engine.py` |
| `get_easy_ocr` | `_get_easy_ocr` | `engine.py` |
| `easy_ocr_image` | `_easy_ocr_image` | `engine.py` |
| `apply_sign_memory` | `_apply_sign_memory` | `circusvoip_core.py` |
| `is_sign_flip` | `_is_sign_flip` | `circusvoip_core.py` |
| `are_containers_similar` | `_are_containers_similar` | `circusvoip_core.py` |
| `is_cave_container` | `_is_cave_container` | `circusvoip_core.py` |
| `capture_with_backoff` | `_capture_with_backoff` | `circusvoip_core.py` |
| `parse_coords` | `_parse_coords` | `circusvoip_circus_ocr_client.py` ligne 419 |
| `normalize_numbers` | `_normalize_numbers` | `circusvoip_circus_ocr_client.py` ligne 418 |
| `pretty_container_name` | `_pretty_container_name` | `circusvoip_client.py` (UI affichage zone) |
| `set_force_cpu(flag)` | setter pour `_ocr_force_cpu_flag` | utile pour init programmatique |
| `get_minus_was_restored()` | getter pour `_minus_was_restored` | `engine.py` + ton `_read_coords_prefer_circus_ocr` (via `setattr` actuellement) |

**Migration recommandée mais non bloquante** : tu peux remplacer les accès `getattr(mod, "_xxx")`
de ton `circusvoip_circus_ocr_client.py` (lignes 418-421) par les noms publics. Bénéfice :
tes prochains rebases ne seront plus exposés à un éventuel refactor interne de ces fonctions
privées. Les anciens noms privés continuent de fonctionner.

---

## 2. `circusvoip_client.py` — changements UI à reporter

Le fichier passe de **15760 lignes** (ton fork build 54) à **16698 lignes** (build 56 upstream).
+938 lignes nettes, mais l'essentiel se concentre sur la nav clavier CircusPhone.

### 2.1 Nav clavier D-pad CircusPhone (build 55)

Système complet de navigation au clavier dans l'overlay CircusPhone, ajouté car la souris
est captée par Star Citizen quand l'overlay s'ouvre. ZQSD libère les flèches pour cet usage.

**Composants :**

| Élément | Lignes (build 56) | Détail |
|---|---|---|
| `_PhoneNavKeyListener` (nouvelle classe pynput) | 8717-8790 | Listener dédié, capte up/down/left/right/enter/esc. Démarre dans `show_animated`, stoppe dans `hide_animated`. Calqué sur `DisplayInfoMaskKeyListener`. |
| `_PhoneIconLabel.set_nav_selected()` | dans la classe `_PhoneIconLabel` | Dessine un carré arrondi semi-transparent bleu accent (opacité 0.22) sur l'icône sélectionnée. |
| `_PhoneContactRow.set_nav_highlight(selected, action)` | dans `_PhoneContactRow` | Expose `_ic_phone`, `_ic_letter`, méthodes `pseudo()`, `is_online()`. |
| État de nav dans l'overlay : `_nav_index`, `_nav_action`, `_nav_rows`, `_convo_nav_index`, `_convo_in_field` | 8898-8910 | Initialisation des compteurs de nav. |
| `refresh_contacts` reconstruction de `_nav_rows` | 9926-9938 | Reconstruit la liste navigable après chaque refresh. |
| `_apply_nav_highlight()` | 9940-9952 | Re-applique la surbrillance après refresh. |
| `_on_nav_key()` (routeur) | 9953-9968 | Slot main-thread, route vers `_nav_contacts` ou `_nav_convo` selon écran. |
| `_nav_contacts()` | 9970-10010 | ↑↓ change ligne, ←→ change action (Appeler/Message), Entrée déclenche. |
| `_nav_convo()` | 10020-10070 | Navigation 3 cibles : Retour / Champ / Envoyer. |
| `_PhoneMessageInput.set_nav_selected(on)` | dans `_PhoneMessageInput` | Bordure bleue accent via QSS `_QSS_NAV_SEL`, `setFocusPolicy(Qt.StrongFocus)`. |
| `_ensure_nav_visible()` (scroll vers ligne) | 10157-10166 | Scroll auto pour garder la sélection visible. |

**Comportement résumé :**
- Écran contacts : ↑↓ ligne, ←→ action (Appeler ou Message), Entrée → émet `sig_call` / `sig_message`
- Écran conversation : ↑↓ cycle entre Retour / Champ / Envoyer, Entrée sur Retour → revient aux contacts, Entrée sur Envoyer → soumet, Entrée sur Champ → entre en mode frappe
- Échap dans le champ → ressort du champ (rend la nav). Échap hors champ → revient aux contacts.

### 2.2 Fix focus Windows pour frappe dans le champ (build 55)

Méthode `_win32_force_foreground()` à la ligne **10075**, dans `_PhoneOverlay`. Problème
résolu : `setFocus()` Qt seul ne suffit pas (fenêtre `Qt.Tool` ne devient pas active
Windows quand SC est au premier plan).

Solution : `ctypes.windll.user32` avec contournement standard `AttachThreadInput` (attache
notre thread à celui de la fenêtre fg, puis `SetForegroundWindow + SetActiveWindow + SetFocus`,
puis détache). Pas de dépendance ajoutée (ctypes déjà utilisé ligne 6354 dans le projet).

SC perd le focus le temps que l'utilisateur tape (acceptable car immobile pendant la frappe).

Appelée ligne **10055** avant `setFocus()` du champ.

### 2.3 Fix sliders volume (build 55)

Ligne **14026** : `lbl.setFixedWidth(95)` dans le helper `_add_volume_slider`. Avant :
`setMinimumWidth(50)` qui laissait les labels longs ("Bip radio", "Soundboard",
"Sonnerie tel.") déborder et désaligner les 3 sliders. Maintenant tous alignés.

---

## 3. `circusvoip_core.py` — RAS

**Aucun changement upstream** entre build 54 et build 56. Le diff observé contre ton fork
ne montre que **tes propres ajouts** (import `circusvoip_circus_ocr_client`, fonction
`_read_coords_prefer_circus_ocr`, swap de `read_coords` ligne 3171 du fork).

→ **Aucune action requise côté core.** Ton intégration Circus OCR est intacte.

---

## 4. `circusvoip_audio_io.py` — RAS

Le fix compteur underrun (date 25/05/2026) est **antérieur** à ton fork build 54, donc déjà
présent chez toi. Aucun changement post-25/05.

→ **Aucune action requise.**

---

## 5. `circusvoip_security.py` — RAS

Aucun changement.

→ **Aucune action requise.**

---

## 6. Côté serveur (impacte ton fork qui consomme l'infra Kainan)

Les fichiers serveur ne sont pas dans ton fork (tu consommes l'infra VPS de Kainan) mais
ils ont changé en parallèle :

- **`circusvoip_audio_server.py`** : rate limit doublé `60→120 frames/s`, burst `120→240`.
  Effet pour toi : si tes utilisateurs émettent à plus de 60 trames/s (proximity + PTT
  concurrents), ils ne sont plus rate-limited.
- **`circusvoip_server.py`** (positions) : ajout de logs debug `/var/log/circusvoip-positions/`
  côté serveur. Aucun impact côté client.

Les serveurs Kainan sont déjà déployés en build 56.

---

## 7. Résumé migration recommandée

Par ordre de priorité :

1. **`circusvoip_sc_ocr.py`** : rebase complet sur la version upstream build 56 (4788 lignes).
   Tout est compatible avec ton fork, et les nouveaux alias publics te permettent de
   simplifier ton `circusvoip_circus_ocr_client.py`.

2. **`circusvoip_client.py`** : merger la nav clavier CircusPhone (sections 2.1-2.2-2.3
   ci-dessus). Si tu as modifié l'overlay `_PhoneOverlay` pour ton fork, vérifier qu'il
   n'y a pas de conflit avec les nouveaux attributs (`_nav_index`, `_nav_action`,
   `_nav_rows`, `_nav_listener`, `_convo_nav_index`, `_convo_in_field`).

3. **`circusvoip_core.py` / `audio_io.py` / `security.py`** : rien.

4. **Optionnel** : migrer les accès `getattr(_voip_ocr, "_xxx")` de ton
   `circusvoip_circus_ocr_client.py` vers les noms publics. Tes hooks restent fonctionnels
   sans cette migration.

---

## 8. Validation en jeu (29/05/2026)

Test session Kainan en jeu sur RuinStation P6 Leo (zone Pyro) :
- OCR INIT EasyOCR sur RTX 5080 (CUDA + quantize) en ~1.3s
- Taux de parse OCR 90% → 100% sur 4 fenêtres STATS 30s consécutives
- Zéro erreur, zéro warning, zéro exception
- Zéro drop audio TX (`AUDIO STATS = RAS`)
- Mécanisme `MINUS LOW RATIO RECOVERY` actif et fonctionnel (5 déclenchements, concentration 82-100%)
- `[COORDS OK] x=-25m y=-49m z=-6m zone=rs_int_p6leo_ruinstation` → parsing nominal

Les changements OCR n'introduisent aucune régression.
