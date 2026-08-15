# N*ABU · Dashboard

Planche de lecture autonome produite par `nabu_dashboard.py`. Aucune dépendance, aucun réseau, aucun secret. Lit six artefacts dans `/opt/data/.nabu` et n'écrit qu'un fichier HTML.

## Fichiers

| Fichier | Rôle |
|---|---|
| `nabu_dashboard.py` | Générateur — lecture seule, compatible `nabu_guard.sh` |
| `NABU-DASHBOARD-CONTRACT.md` | Contrat de données `state` + mise en service |
| `assets/nabu-command.webp` | Visuel principal N*ABU, intégré en base64 au HTML final |
| `assets/nabu-portrait.webp` | Avatar de navigation, intégré en base64 au HTML final |
| `assets/iii-symbol.svg` | Géométrie vectorielle canonique du symbole `iii` |

## Tirage

```bash
./nabu_dashboard.py build --out ~/.nabu/dashboard.html
./nabu_dashboard.py demo --out /tmp/demo.html
./nabu_dashboard.py json | jq .freshness
```

## Livraison

Le fichier HTML est autonome et s'ouvre hors ligne. Voies possibles :

1. Lecture locale si accès fichier au conteneur
2. Serveur local + tunnel
3. Copie dans le Vault Obsidian (téléchargement manuel)

## Direction

Système éditorial **N*ABU × NOUS Research** : cyanotype cobalt et ivoire, composition brutaliste,
trame risograph, grain argentique, fibres papier et micro-artefacts de scan. Le dashboard reste
strictement lisible et autonome : aucune police, image ou texture distante.

- cobalt / bleu de Prusse : information neutre et structure ;
- or N*ABU : identité, attention et symbole `iii` ;
- oxblood : limite franchie, refus ou KILL ;
- jamais de vert, de néon cyberpunk, de verre brillant ni de gradient logiciel propre.

La couverture installe l'agent ; le reste de la page redevient un instrument. Les textures sont
intentionnellement tactiles, mais ne passent jamais devant les chiffres ni les états de risque.

## Hiérarchie UX

La vue initiale répond à cinq questions seulement : equity actuelle, performance du jour, uPnL
ouvert, PnL réalisé et risque le plus consommé. Elle montre ensuite les quatre limites prioritaires
et les positions ouvertes. Fraîcheur technique, grand livre, limites complètes, statistiques,
contexte, signaux et provenance restent disponibles dans des volets repliables.

Sur desktop, une navigation latérale fixe donne accès aux quatre niveaux utiles : portefeuille,
risques, positions et analyse. Sur mobile, elle devient une barre basse. Les visuels de N*ABU sont
embarqués dans le HTML produit : aucun chargement distant et aucune image cassée hors ligne.

## Symbole `iii` — canon de forme

Le symbole n'est jamais composé avec les caractères typographiques `iii`. C'est un pictogramme
vectoriel à six formes pleines et distinctes : trois disques au-dessus de trois piliers. Le disque
central est légèrement plus grand et plus haut. Les deux piliers extérieurs sont inclinés vers
l'extérieur ; le pilier central reste axial. Les six éléments conservent un espace négatif net et
ne se touchent jamais. La couleur, la texture et l'opacité peuvent varier ; cette silhouette,
ses proportions et son rythme ne varient pas.

**Prompt canon :** “minimal geometric `iii` emblem, exactly three separate solid circular heads,
the center circle slightly larger and raised, exactly three separate tall tapered pillars below,
center pillar upright and symmetrical, outer pillars subtly splayed outward, clear negative space
between all six elements, compact monumental silhouette, flat single-color vector mark, no letters,
no typography, no merged shapes, no extra elements.”

## Boucle d'auto-évaluation

Le dashboard expose `self_eval` dans le JSON embarqué et dans la commande `json`. N*ABU reçoit un
verdict (`HALTED`, `BLOCKED`, `LEARNING`, `DEGRADING`, `IMPROVING`, `PERFORMING`), un score sur 100,
les preuves utilisées et une prochaine action unique. Le score combine données, risque, discipline
et edge ; il ne confond pas une série gagnante avec une stratégie démontrée.

```bash
./nabu_dashboard.py json | jq .self_eval
```

Les limites de risque sont immuables depuis cette boucle. Une amélioration teste une seule hypothèse
en paper, attend la prochaine taille d'échantillon, puis exige une validation humaine avant toute
promotion live.
