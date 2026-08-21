# Bibliothèques tierces

CircusVOIP est distribué sous licence MIT (voir `LICENSE`). L'installeur
embarque les bibliothèques ci-dessous, qui restent soumises à **leur propre**
licence.

> ⚠️ Ce tableau est un **point de départ à vérifier**, pas un avis juridique.
> Les licences peuvent changer d'une version à l'autre : contrôlez-les pour les
> versions réellement embarquées, avec `pip-licenses` par exemple.

## À vérifier en priorité : les deux LGPL

| Bibliothèque | Licence | Ce que ça implique |
|---|---|---|
| **PySide6** | LGPL v3 (ou commerciale) | voir ci-dessous |
| **pynput** | LGPL v3 | voir ci-dessous |

La LGPL **n'oblige pas** à ouvrir le code de CircusVOIP. Elle impose deux
choses quand on distribue un binaire :

1. **Fournir le texte de la licence** et indiquer où trouver les sources de la
   bibliothèque.
2. **Permettre à l'utilisateur de remplacer** la bibliothèque par une autre
   version.

Le point 2 est celui qui se rate. Un PyInstaller `--onefile` fusionne tout dans
un exécutable unique et rend le remplacement impossible ; `--onedir` laisse les
`.pyd` séparés et satisfait l'exigence sans effort particulier.

**À vérifier avant la 0.4.0 stable** : quel mode utilise le `.iss` actuel.

## Licences permissives

Elles demandent seulement de conserver les mentions de copyright — ce que
l'inclusion de ce fichier dans l'installeur suffit à assurer.

| Bibliothèque | Licence (à confirmer) |
|---|---|
| numpy | BSD-3-Clause |
| torch (PyTorch) | BSD-3-Clause |
| easyocr | Apache-2.0 |
| opencv-python (cv2) | Apache-2.0 |
| cryptography | Apache-2.0 / BSD-3-Clause |
| pytesseract | Apache-2.0 |
| Pillow (PIL) | MIT-CMU (HPND) |
| websockets | BSD-3-Clause |
| sounddevice | MIT |
| mss | MIT |
| bettercam | MIT |
| psutil | BSD-3-Clause |
| pydub | MIT |
| opuslib / opuslib_next | BSD-3-Clause |
| pynvml | BSD-3-Clause |
| pyrnnoise | BSD-3-Clause |

## Composants natifs

Ils ne sont pas installés par `pip` et s'oublient facilement dans un
récapitulatif de licences.

| Composant | Licence | Remarque |
|---|---|---|
| **Tesseract OCR** | Apache-2.0 | binaire externe, appelé par `pytesseract` |
| **libopus** | BSD-3-Clause | encodage audio |
| **RNNoise** | BSD-3-Clause | réduction de bruit |

## Régénérer ce tableau

```cmd
pip install pip-licenses
pip-licenses --format=markdown --with-urls > licences_reelles.md
```

À faire sur l'environnement qui sert à construire l'installeur, pas sur un
autre : ce sont les versions embarquées qui comptent.

## Hors licences : Star Citizen

CircusVOIP lit l'écran du jeu par OCR. C'est une question distincte de la
licence logicielle — elle relève des conditions d'utilisation de RSI, pas du
droit d'auteur sur ce code.
