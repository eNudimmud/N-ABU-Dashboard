# N\*ABU — planche de lecture · contrat & mise en service

`nabu_dashboard.py` produit un fichier HTML autonome (~36 Ko, aucune dépendance,
aucun réseau, aucun JS requis pour lire la page). Il lit six artefacts et
n'écrit que son `--out`.

---

## 1. Ce que le script touche

| Fichier | Accès | Rôle |
|---|---|---|
| `risk.yaml` | **lecture** | limites — la planche ne les recopie pas, elle les cite |
| `book.json` | **lecture** | equity, positions, ancrages jour/semaine, `synced_at` |
| `paper/account.json` | **lecture** | `last_mark_ts`, thèse, invalidation, cash/frais/funding |
| `journal.jsonl` | **lecture** | edge, ordres/h, ordres/j, série de pertes, dernières clôtures |
| `live_context.json` | **lecture** | contexte hourly + lecture daily |
| `data/scan_latest.json` | **lecture** | signaux du dernier scan |
| `data/equity_history.jsonl` | **append** | un point (equity, score, verdict) par build — seul fichier d'état écrit, par ce script uniquement |
| `KILL` | **lecture** | bandeau oxblood |
| `--out` (HTML) | **écriture** | seul fichier écrit, en remplacement atomique |

Aucun secret n'est lu. Aucun venue n'est joint. Aucune méthode de `nabu_exec`
n'est importée. Le script est compatible `nabu_guard.sh` sans exception : il
n'écrit sur aucun chemin protégé (`risk.yaml`, `book.json`, `journal.jsonl`,
`KILL`, `SOUL.md`, `*/config.yaml`, registre scellé).

Racine surchargeable : `NABU_LIVE_ROOT` (défaut `/opt/data/.nabu`),
`NABU_RISK_CONFIG` pour le fichier de limites.

---

## 2. Les deux horloges — la raison d'être de la page

En mode paper, `nabu_book.py:read_venues()` appelle `PaperAccount.snapshot()`
**sans dictionnaire de marks**. Les positions sont donc valorisées sur
`last_mark_px`, qui n'est mis à jour que par `wrap_mtm.py`, toutes les heures,
et uniquement sur des bougies **entièrement closes**.

Conséquence : `book.json:synced_at` peut afficher 30 secondes pendant que le
uPnL date d'une à deux heures. Une valeur fausse est bruyante ; une valeur
périmée qui a l'air fraîche ne l'est pas — c'est la famille de bug du
`read_venues()` figé de 2026-08-12.

La page affiche donc deux âges, et **le pire des deux qualifie la page**.

| Horloge | Source | Cadence réelle | Jaune | Rouge |
|---|---|---|---|---|
| `sync_age` | `book.json:synced_at` | book-sync, 5 min | > 15 min | > 60 min |
| `mark_age` | `min(account.positions[].last_mark_ts)` | wrap_mtm, 1 h | > 90 min | > 150 min |

`mark_age` retient la position **la plus mal marquée**, pas la moyenne.
Flat, il est sans objet et la page le dit : l'equity vaut le cash au centime
près. Seuils modifiables en tête de fichier (`SYNC_WATCH_S`, `MARK_HOT_S`, …).

---

## 3. Le contrat `state`

Même objet en sortie de `nabu_dashboard.py json` et inliné dans le HTML sous
`<script type="application/json" id="nabu-state">`. Une seconde surface peut
donc le consommer sans réimplémenter les calculs.

```jsonc
{
  "built_ts": 1786758108.5, "built_iso": "2026-08-15T01:41:48Z",
  "mode": "paper", "demo": false,
  "attestation": "BOOK · sync … · equity …$ · DD …% · positions n",

  "kill": { "active": false, "reason": null, "since_iso": null },

  "freshness": {
    "sync_age_s": 118,  "sync_status": "ok",
    "mark_age_s": 5760, "mark_status": "watch",
    "mark_note": "position la plus mal marquée",
    "status": "watch"                    // ok | watch | hot | unknown
  },

  "capital": {
    "equity_usd", "peak_usd", "day_open_usd", "week_open_usd",
    "dd_pct", "day_pnl_pct", "week_pnl_pct",
    "paper": { "cash_usd","realized_pnl_usd","fees_paid_usd",
               "funding_paid_usd","closed_trades","start_equity_usd" }
  },

  // part de chaque limite déjà consommée, pas la valeur brute
  "gates": [{ "key":"dd", "label":"Drawdown", "value_txt":"8.16 %",
              "limit_txt":"20 %", "util_pct":40.8,
              "status":"ok",      // ok <60 | watch 60-85 | hot 85-100 | breach ≥100
              "note":"…" }],

  "positions": [{ "venue","symbol","side","size","entry_px","stop_px",
                  "stop_dist_pct","notional_usd","unrealized_pnl_usd",
                  "funding_paid_usd","last_mark_px","mark_age_s",
                  "opened_ts","hold_h","thesis","invalidation" }],

  "edge": {
    "n_opens", "n_closes", "target_trades": 30, "verified": false,
    "expectancy_r", "ci95": [lo, hi], "win_rate_pct",
    "cost_ratio_pct",        // (frais + |funding|) / gain brut · > 30 % = trop fréquent
    "stop_share_pct", "median_hold_h",
    "plan_written_pct",      // ouvertures avec thèse ET invalidation · < 90 % = exécution
    "best_r", "worst_r",
    "fees_usd", "funding_usd", "gross_usd", "net_usd",
    "r_histogram": [{ "label":"<−1R", "n":1 }, …]
  },

  "self_eval": {
    "schema_version": 1,
    "verdict": "LEARNING", // HALTED | BLOCKED | LEARNING | DEGRADING | IMPROVING | PERFORMING
    "score": 54,
    "improvement_needed": false,
    "confidence": "low",
    "scores": { "data":12, "risk":15, "discipline":20, "edge":4 },
    "evidence": ["échantillon 11/30 trades", "risque max Série pertes 67 %"],
    "blockers": [],
    "next_action": "Collecter 19 clôtures supplémentaires sans retuner la stratégie.",
    "actions": ["…maximum quatre actions ordonnées…"],
    "review": { "closed_trades_now":11, "closed_trades_target":30,
                "next_review_after_closes":30 },
    "mutation_policy": {
      "risk_limits_mutable": false,
      "live_autopromotion": false,
      "one_hypothesis_per_cycle": true,
      "paper_validation_required": true,
      "human_approval_for_live": true
    }
  },

  "market": { "context": {…live_context…}, "signals": [...], "scan_ts": … },
  "warnings": [ "…book.warnings…" ],

  "provenance": [{ "source":"book.json", "path":"…", "age_s":118,
                   "state":"VERIFIED",   // VERIFIED | UNVERIFIED | FAILED
                   "note":"lu" }]
}
```

**Règle du contrat :** une source absente ne produit jamais un zéro. Elle
produit `UNVERIFIED`, la section concernée affiche une phrase, et la page
refuse de meubler. Un zéro se lit comme une mesure.

Champs ajoutés à la refonte 2026-08-16 :

- `edge.expectancy_r_recent` + `edge.recent_window` — espérance glissante sur
  les 10 dernières clôtures. C'est la fenêtre qui meurt en premier quand
  l'edge décède ; `self_eval` la consomme (fenêtre récente négative sur global
  positif → action prioritaire « suspendre l'ajout de risque »).
- `recent_closes[]` — les 8 dernières clôtures (R, PnL net, portage, raison de
  sortie, thèse) : la matière première du post-mortem, affichée dans le volet
  Edge.
- `history[]` — points `{ts, equity, dd_pct, score, verdict, n_closes, upnl}`
  appendés dans `data/equity_history.jsonl` à chaque `build` (dédoublonnés si
  rien n'a bougé en moins d'une heure). Rend la courbe d'equity et la
  trajectoire du score d'auto-évaluation.

### Couche vivante (JS embarqué, dégradation propre)

La page reste lisible sans JS et hors ligne — le JS n'ajoute que trois choses :

1. **Marks live** — `POST allMids` sur l'API publique Hyperliquid (CORS
   ouvert, lecture seule, zéro secret) toutes les 15 s quand l'onglet est
   visible. Le uPnL des positions est recalculé **localement** à partir de
   `size`/`entry` inlinés ; equity et perf du jour suivent. La chip d'état
   passe à `LIVE · HL`, chaque valeur qui change flashe or. `book.json` reste
   la source — la page l'écrit sous les métriques.
2. **Horloges qui avancent** — les âges sync/mark vieillissent en continu au
   lieu de rester figés à l'instant du tirage.
3. **Rafraîchissement doux** — toutes les 3 min, si un tirage plus récent
   existe au même URL (`built_ts` supérieur), la page se recharge. Jamais de
   reload aveugle ; rien en `file://`.

Échec réseau : après 2 échecs consécutifs la chip passe à
`live perdu · tirage HH:MMZ` (or). Aucune valeur inventée : les chiffres
retombent sur ceux du tirage.

Artefacts exclus des stats d'edge : `exclude_from_edge`, `phantom`,
`test_trade` — même filtre que `nabu_watchdog.py` et `compute_state()`.

---

## 4. Mise en service

```bash
cp nabu_dashboard.py /opt/data/.nabu/bin/ && chmod +x /opt/data/.nabu/bin/nabu_dashboard.py

# tirage manuel
/opt/data/.nabu/bin/nabu_dashboard.py build --out /opt/data/.nabu/dashboard.html

# contrat seul (pour brancher autre chose dessus)
/opt/data/.nabu/bin/nabu_dashboard.py json | jq .freshness
```

Cron no-agent, cadencé sur le watchdog plutôt que sur book-sync — retirer un
tirage toutes les 5 min n'apporte rien tant que le mark est horaire :

```bash
hermes cron create "*/15 * * * *" \
  --script "/opt/data/.nabu/bin/nabu_dashboard.py build" \
  --name "dashboard"
```

Sortie stdout, livrable en l'état à Telegram :

```
DASHBOARD · /opt/data/.nabu/dashboard.html · mode paper · sync 2 min · mark 1.6 h · fraîcheur WATCH
  ! UNVERIFIED · scan_latest.json · fichier absent
```

### `.gitignore` — à ajouter

`/opt/data/.nabu` **est** le dépôt poussé sur `eNudimmud/N-ABU`. La page
contient l'equity, les positions et les stats du journal — exactement ce pour
quoi `book.json` et `journal.jsonl` sont déjà exclus. Sans cette ligne, un
`git push` annule cette décision :

```gitignore
dashboard.html
dashboard.tmp
```

### Livraison — le seul point non résolu

Le fichier est autonome et s'ouvre sur n'importe quel navigateur, hors ligne.
Ce que je ne peux pas trancher sans savoir ce que le conteneur Hermes expose :
**comment il arrive sur ton écran.** Trois voies, par ordre de robustesse :

1. **Lecture locale** — si tu as un accès fichier au conteneur depuis le Steam
   Deck, rien à faire.
2. **Serveur local** — `python3 -m http.server 8080 --directory /opt/data/.nabu
   --bind 127.0.0.1`, puis tunnel. Dépend de ce que la plateforme laisse passer
   en ports sortants ; à vérifier avant d'y investir.
3. **Copie dans le Vault** — `Nabu-Vault` est privé, mais GitHub ne rend pas le
   HTML : tu télécharges le fichier avant de l'ouvrir. Ça marche, c'est juste
   deux gestes de plus sur Android.

---

## 5. Direction artistique — la règle de couleur

Substrat cyanotype : ivoire papier, encre cobalt, plaques prussiennes.
L'or et l'oxblood ne décorent **rien** — ils sont réservés au sémantique :

| Couleur | Sens | Où |
|---|---|---|
| Cobalt | structure, donnée neutre, limite sous 60 % | partout |
| **Or** | attention — limite à 60-85 %, source `UNVERIFIED`, mark périmé | planche, horloges, provenance |
| **Oxblood** | franchi, perdu, mort — KILL, limite > 85 %, PnL négatif, R négatifs | bandeau, planche, histogramme |

Pas de vert. Un gain n'a pas besoin d'être signalé : c'est la survie qui a
besoin de l'être. Le PnL positif reste en encre.

**Élément signature :** la planche des limites, construite comme une charte de
densité photographique — onze colonnes exposées de bas en haut, chacune montrant
la part de sa limite déjà consommée. Ce n'est pas une jauge de performance :
une barre pleine n'annonce pas une perte, elle annonce un **refus du gate**.

---

## 6. Boucle d'auto-évaluation

N*ABU consomme directement `.self_eval`, pas le texte visuel de la page :

```bash
/opt/data/.nabu/bin/nabu_dashboard.py json | jq .self_eval
```

Ordre de décision déterministe : KILL → fraîcheur → limites → taille de
l'échantillon → espérance/IC95 → discipline → coûts. Une seule hypothèse peut
être testée par cycle, en paper. Les revues sont déclenchées par un nombre de
trades clos, pas par le temps : aucun retuning quotidien sur le bruit.

`PERFORMING` exige au moins 30 clôtures, une espérance positive et une borne
basse de l'IC95 strictement positive. `LEARNING` interdit de conclure avant le
seuil. `DEGRADING` demande une segmentation des pertes mais n'autorise jamais
à desserrer une limite. `BLOCKED` et `HALTED` suspendent la boucle.

## 7. Ce que cette page ne fait pas

- Elle n'ouvre, ne ferme et ne modifie aucune position. Lecture seule, sans exception.
- Elle propose une prochaine expérience, mais n'applique aucun changement de stratégie.
- Elle ne remplace pas `book.json`. En cas de conflit, le book gagne — c'est un
  tirage, pas la source.
- Elle ne mesure pas d'edge sous 30 trades clos. Elle affiche les chiffres avec
  un tampon `UNVERIFIED` et un intervalle de confiance qui dit ce que la moyenne
  cache.
- Elle ne dit rien de la **justesse** du mark, seulement de son âge. Un mark
  frais issu d'une bougie fausse reste faux.
