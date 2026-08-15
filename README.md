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

Substrat cyanotype. Cobalt = neutre, or = attention, oxblood = franchi/mort. Pas de vert.
