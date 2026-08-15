# N*ABU · Dashboard

Planche de lecture autonome produite par `nabu_dashboard.py`. Aucune dépendance, aucun réseau, aucun secret. Lit six artefacts dans `/opt/data/.nabu` et n'écrit qu'un fichier HTML.

## Fichiers

| Fichier | Rôle |
|---|---|
| `nabu_dashboard.py` | Générateur — lecture seule, compatible `nabu_guard.sh` |
| `NABU-DASHBOARD-CONTRACT.md` | Contrat de données `state` + mise en service |

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
