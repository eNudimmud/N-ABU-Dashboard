#!/opt/data/.nabu/venv/bin/python
"""
nabu_dashboard.py — PLANCHE DE LECTURE N*ABU.

Lecture seule. Ce script n'ouvre AUCUN fichier en écriture hors de son propre
`--out`. Il ne touche ni risk.yaml, ni book.json, ni journal.jsonl, ni le KILL.
Il n'appelle aucun venue, ne charge aucun secret, ne fait aucune requête réseau.
Conçu pour tourner en cron no-agent : zéro LLM, zéro token.

    nabu_dashboard.py build --out ~/.nabu/dashboard.html
    nabu_dashboard.py json                       # contrat seul, sur stdout
    nabu_dashboard.py demo --out /tmp/demo.html  # données synthétiques (DA)


POURQUOI CE DASHBOARD N'OUVRE PAS SUR L'EQUITY
----------------------------------------------
L'equity est le chiffre le moins urgent de la page. Le chiffre urgent est :
« est-ce que cet état est encore vrai ? ». Invariant #2 de SOUL.md — pas
d'attestation, pas d'affirmation. La page ouvre donc sur l'attestation et sur
les DEUX horloges, parce qu'il y en a deux et qu'une seule est journalisée.

    sync_age  = now - book.json:synced_at
                book-sync tourne toutes les 5 min. Toujours frais.

    mark_age  = now - max(paper/account.json:positions[].last_mark_ts)
                wrap_mtm.py tourne toutes les heures ET ne marque que des
                bougies ENTIÈREMENT closes. Le uPnL d'une position peut donc
                dater de 1 à 2 h pendant que `synced_at` affiche 30 secondes.

En mode paper, `nabu_book.py:read_venues()` appelle `PaperAccount.snapshot()`
sans dictionnaire de marks : les positions sont valorisées sur `last_mark_px`.
Un dashboard qui n'affiche que `synced_at` reproduit donc exactement l'illusion
du bug de 2026-08-12 — une valeur fausse est bruyante, une valeur PÉRIMÉE qui
a l'air fraîche ne l'est pas. Les deux horloges sont côte à côte, la plus
mauvaise des deux qualifie la page.

Corollaire assumé : flat, `mark_age` est sans objet — l'equity vaut le cash,
au centime près. La page le dit au lieu d'afficher un tiret.


CONTRAT DE DONNÉES (dict `state`, aussi inliné dans le HTML)
------------------------------------------------------------
{
  "built_ts", "built_iso", "mode", "kill": {...},
  "freshness": {"sync_age_s", "mark_age_s", "status", ...},
  "attestation": "BOOK · sync … · equity … · DD … · positions …",
  "capital":   {equity_usd, peak_usd, day_open_usd, week_open_usd,
                dd_pct, day_pnl_pct, week_pnl_pct, paper:{...}},
  "gates":     [{key, label, value_txt, limit_txt, util_pct, status, note}],
  "positions": [{symbol, side, size, entry_px, stop_px, notional_usd,
                 unrealized_pnl_usd, funding_paid_usd, mark_age_s, ...}],
  "edge":      {n_opens, n_closes, expectancy_r, ci95, win_rate_pct,
                cost_ratio_pct, stop_share_pct, median_hold_h,
                plan_written_pct, r_histogram, verified},
  "market":    {"context": {...}, "signals": [...]},
  "provenance":[{source, path, state, note, age_s}]
}

`provenance` porte le Verify nommé pour chaque source : VERIFIED (lue, datée),
UNVERIFIED (absente) ou FAILED (illisible). Une source absente n'est jamais
rendue par un zéro : un zéro se lit comme une mesure.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Chemins — surchargeables par env, jamais devinés en dur ailleurs
# ---------------------------------------------------------------------------

LIVE = Path(os.environ.get("NABU_LIVE_ROOT", "/opt/data/.nabu"))
P_RISK = Path(os.environ.get("NABU_RISK_CONFIG", str(LIVE / "risk.yaml")))
P_BOOK = LIVE / "book.json"
P_ACCOUNT = LIVE / "paper" / "account.json"
P_JOURNAL = LIVE / "journal.jsonl"
P_KILL = LIVE / "KILL"
P_CTX = LIVE / "live_context.json"
P_SCAN = LIVE / "data" / "scan_latest.json"

TARGET_TRADES = 30          # gate G0 du README — en dessous, les stats = bruit
SYNC_WATCH_S, SYNC_HOT_S = 15 * 60, 60 * 60
MARK_WATCH_S, MARK_HOT_S = 90 * 60, 150 * 60


# ---------------------------------------------------------------------------
# Lecture — aucune de ces fonctions ne lève, aucune n'écrit
# ---------------------------------------------------------------------------

class Source:
    """Une source lue, avec son Verify nommé."""

    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        self.state, self.note, self.mtime = "UNVERIFIED", "fichier absent", None
        self.data = None

    def as_dict(self) -> dict:
        return {
            "source": self.name,
            "path": str(self.path),
            "state": self.state,
            "note": self.note,
            "age_s": (time.time() - self.mtime) if self.mtime else None,
        }


def read_json(name: str, path: Path) -> Source:
    s = Source(name, path)
    if not path.exists():
        return s
    try:
        s.mtime = path.stat().st_mtime
        s.data = json.loads(path.read_text())
        s.state, s.note = "VERIFIED", "lu"
    except Exception as e:                                        # noqa: BLE001
        s.state, s.note = "FAILED", f"{type(e).__name__}: {e}"
    return s


def read_jsonl(name: str, path: Path) -> Source:
    s = Source(name, path)
    if not path.exists():
        return s
    try:
        s.mtime = path.stat().st_mtime
        rows, bad = [], 0
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
        s.data = rows
        s.state = "VERIFIED"
        s.note = f"{len(rows)} enregistrements" + (f" · {bad} lignes illisibles" if bad else "")
    except Exception as e:                                        # noqa: BLE001
        s.state, s.note = "FAILED", f"{type(e).__name__}: {e}"
    return s


def read_risk(path: Path) -> Source:
    s = Source("risk.yaml", path)
    if not path.exists():
        return s
    try:
        import yaml
        s.mtime = path.stat().st_mtime
        s.data = yaml.safe_load(path.read_text())
        s.state, s.note = "VERIFIED", "limites chargées"
    except Exception as e:                                        # noqa: BLE001
        s.state, s.note = "FAILED", f"{type(e).__name__}: {e}"
    return s


def read_kill(path: Path) -> dict:
    if not path.exists():
        return {"active": False, "reason": None, "since_iso": None}
    try:
        txt = path.read_text().strip()
    except Exception:                                             # noqa: BLE001
        txt = "(illisible)"
    ts = path.stat().st_mtime
    return {
        "active": True,
        "reason": txt or "(sans motif)",
        "since_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
        "since_ts": ts,
    }


# ---------------------------------------------------------------------------
# Calculs
# ---------------------------------------------------------------------------

def _is_artifact(rec: dict) -> bool:
    return bool(rec.get("exclude_from_edge") or rec.get("phantom") or rec.get("test_trade"))


def compute_capital(book: dict) -> dict:
    eq = float(book.get("equity_usd", 0.0) or 0.0)
    peak = float(book.get("equity_peak_usd", eq) or eq)
    day = float(book.get("equity_day_open_usd", eq) or eq)
    week = float(book.get("equity_week_open_usd", eq) or eq)
    return {
        "equity_usd": eq,
        "peak_usd": peak,
        "day_open_usd": day,
        "week_open_usd": week,
        "dd_pct": 0.0 if peak <= 0 else (peak - eq) / peak * 100.0,
        "day_pnl_pct": 0.0 if day <= 0 else (eq - day) / day * 100.0,
        "week_pnl_pct": 0.0 if week <= 0 else (eq - week) / week * 100.0,
    }


def compute_freshness(book: dict, account: dict | None, n_positions: int) -> dict:
    now = time.time()
    synced_at = float(book.get("synced_at", 0) or 0)
    sync_age = (now - synced_at) if synced_at else None

    mark_age, mark_note = None, None
    if n_positions == 0:
        mark_note = "sans objet — flat, l'equity vaut le cash au centime près"
    elif account:
        marks = [float(p.get("last_mark_ts", 0) or 0) for p in account.get("positions", [])]
        marks = [m for m in marks if m > 0]
        if marks:
            mark_age = now - min(marks)   # la position la PLUS mal marquée qualifie la page
            mark_note = "position la plus mal marquée"
        else:
            mark_note = "aucune position jamais marquée — wrap_mtm.py n'a pas tourné"
    else:
        mark_note = "paper/account.json illisible — fraîcheur du mark inconnue"

    def rank(age, watch, hot):
        if age is None:
            return "unknown"
        if age >= hot:
            return "hot"
        if age >= watch:
            return "watch"
        return "ok"

    s_status = rank(sync_age, SYNC_WATCH_S, SYNC_HOT_S)
    m_status = rank(mark_age, MARK_WATCH_S, MARK_HOT_S) if n_positions else "ok"
    worst = "hot" if "hot" in (s_status, m_status) else \
            "watch" if "watch" in (s_status, m_status) else \
            "unknown" if "unknown" in (s_status, m_status) else "ok"

    return {
        "sync_age_s": sync_age, "sync_status": s_status,
        "mark_age_s": mark_age, "mark_status": m_status, "mark_note": mark_note,
        "status": worst,
    }


def _gate(key, label, value_txt, limit_txt, util_pct, note=""):
    u = max(0.0, float(util_pct))
    status = "breach" if u >= 100 else "hot" if u >= 85 else "watch" if u >= 60 else "ok"
    return {"key": key, "label": label, "value_txt": value_txt,
            "limit_txt": limit_txt, "util_pct": round(u, 1),
            "status": status, "note": note}


def compute_gates(cfg: dict, cap: dict, positions: list, journal: list) -> list[dict]:
    """Utilisation de chaque limite, en % de la limite. 100 % = franchie."""
    if not cfg:
        return []
    br = cfg.get("breakers", {}) or {}
    pf = cfg.get("portfolio", {}) or {}
    capc = cfg.get("capital", {}) or {}
    now = time.time()
    eq = cap["equity_usd"]

    gross = sum(abs(float(p.get("notional_usd", 0) or 0)) for p in positions)
    net = sum(float(p.get("notional_usd", 0) or 0) * (1 if p.get("side") == "long" else -1)
              for p in positions)
    gross_pct = 0.0 if eq <= 0 else gross / eq * 100.0
    net_pct = 0.0 if eq <= 0 else abs(net) / eq * 100.0

    orders = [r for r in journal if r.get("event") == "order"]
    o1h = len([r for r in orders if float(r.get("ts", 0) or 0) > now - 3600])
    o24 = len([r for r in orders if float(r.get("ts", 0) or 0) > now - 86400])

    fills = [r for r in journal if r.get("event") == "fill" and not _is_artifact(r)]
    streak = 0
    for r in reversed(fills):
        pnl = r.get("realized_pnl_usd")
        if pnl is None:
            continue
        if float(pnl) < 0:
            streak += 1
        else:
            break
    last_loss = max((float(r.get("ts", 0) or 0) for r in fills
                     if float(r.get("realized_pnl_usd") or 0) < 0), default=0.0)

    def lim(d, k, default):
        try:
            return float(d.get(k, default))
        except (TypeError, ValueError):
            return float(default)

    max_dd = lim(br, "max_drawdown_pct", 20)
    day_lim = lim(br, "daily_loss_limit_pct", 4)
    week_lim = lim(br, "weekly_loss_limit_pct", 8)
    floor = lim(capc, "min_equity_usd", 0)
    max_pos = lim(pf, "max_concurrent_positions", 1)
    max_gross = lim(pf, "max_gross_exposure_pct", 100)
    max_net = lim(pf, "max_net_exposure_pct", 100)
    max_oh = lim(br, "max_orders_per_hour", 8)
    max_od = lim(br, "max_orders_per_day", 30)
    streak_lim = lim(br, "loss_streak_count", 3)
    cd_h = lim(br, "loss_streak_cooldown_hours", 6)

    gates = [
        _gate("dd", "Drawdown", f"{cap['dd_pct']:.2f}{NB}%", f"{max_dd:.0f}{NB}%",
              cap["dd_pct"] / max_dd * 100 if max_dd else 0,
              "depuis le pic d'equity · au-delà : KILL global"),
        _gate("day", "Perte jour", f"{cap['day_pnl_pct']:+.2f}{NB}%", f"−{day_lim:.0f}{NB}%",
              max(0.0, -cap["day_pnl_pct"]) / day_lim * 100 if day_lim else 0,
              "journée UTC"),
        _gate("week", "Perte semaine", f"{cap['week_pnl_pct']:+.2f}{NB}%", f"−{week_lim:.0f}{NB}%",
              max(0.0, -cap["week_pnl_pct"]) / week_lim * 100 if week_lim else 0,
              "semaine glissante"),
        _gate("floor", "Plancher equity", f"{eq:.0f}{NB}$", f"{floor:.0f}{NB}$",
              (floor / eq * 100) if eq > 0 else 100,
              "sous le plancher : sorties seulement"),
        _gate("pos", "Positions", f"{len(positions)}", f"{max_pos:.0f}",
              len(positions) / max_pos * 100 if max_pos else 0,
              "concurrentes, tous venues"),
        _gate("gross", "Expo brute", f"{gross_pct:.0f}{NB}%", f"{max_gross:.0f}{NB}%",
              gross_pct / max_gross * 100 if max_gross else 0,
              "somme des notionnels / equity"),
        _gate("net", "Expo nette", f"{net_pct:.0f}{NB}%", f"{max_net:.0f}{NB}%",
              net_pct / max_net * 100 if max_net else 0,
              "|long − short| / equity"),
        _gate("oh", "Ordres / h", f"{o1h}", f"{max_oh:.0f}",
              o1h / max_oh * 100 if max_oh else 0, "anti-boucle folle"),
        _gate("od", "Ordres / j", f"{o24}", f"{max_od:.0f}",
              o24 / max_od * 100 if max_od else 0, "anti-overtrading"),
        _gate("streak", "Série pertes", f"{streak}", f"{streak_lim:.0f}",
              streak / streak_lim * 100 if streak_lim else 0,
              "au-delà : cooldown imposé"),
    ]

    if streak >= streak_lim and last_loss:
        left_h = max(0.0, cd_h - (now - last_loss) / 3600)
        gates.append(_gate("cooldown", "Cooldown", f"{left_h:.1f}{NB}h restantes",
                           f"{cd_h:.0f}{NB}h", 100 if left_h > 0 else 0,
                           "ouvertures bloquées"))
    else:
        gates.append(_gate("cooldown", "Cooldown", "inactif", f"{cd_h:.0f}{NB}h", 0,
                           "s'arme après la série"))
    return gates


def compute_edge(journal: list) -> dict:
    opens = [r for r in journal if r.get("event") == "fill"
             and r.get("kind") == "open" and not _is_artifact(r)]
    closes = [r for r in journal if r.get("event") == "fill"
              and r.get("kind") == "close" and not _is_artifact(r)]

    out = {
        "n_opens": len(opens), "n_closes": len(closes),
        "target_trades": TARGET_TRADES,
        "verified": len(closes) >= TARGET_TRADES,
        "expectancy_r": None, "ci95": None, "win_rate_pct": None,
        "cost_ratio_pct": None, "stop_share_pct": None,
        "median_hold_h": None, "plan_written_pct": None,
        "best_r": None, "worst_r": None,
        "fees_usd": 0.0, "funding_usd": 0.0, "gross_usd": 0.0, "net_usd": 0.0,
        "r_histogram": [],
    }
    if opens:
        planned = [r for r in opens
                   if str(r.get("thesis") or "").strip() and str(r.get("invalidation") or "").strip()]
        out["plan_written_pct"] = len(planned) / len(opens) * 100.0
    if not closes:
        return out

    rs = [float(r["r_multiple"]) for r in closes if r.get("r_multiple") is not None]
    fees = sum(abs(float(r.get("fees_usd") or 0)) for r in closes)
    fund = sum(float(r.get("funding_usd") or 0) for r in closes)
    gross = sum(abs(float(r.get("gross_pnl_usd") or 0)) for r in closes)
    net = sum(float(r.get("realized_pnl_usd") or 0) for r in closes)
    holds = [float(r["hold_hours"]) for r in closes if r.get("hold_hours") is not None]
    stops = [r for r in closes if r.get("reason") == "stop"]

    out["fees_usd"], out["funding_usd"] = fees, fund
    out["gross_usd"], out["net_usd"] = gross, net
    out["cost_ratio_pct"] = (fees + abs(fund)) / gross * 100.0 if gross > 0 else None
    out["stop_share_pct"] = len(stops) / len(closes) * 100.0
    out["median_hold_h"] = statistics.median(holds) if holds else None

    if rs:
        out["expectancy_r"] = statistics.fmean(rs)
        out["win_rate_pct"] = len([r for r in rs if r > 0]) / len(rs) * 100.0
        out["best_r"], out["worst_r"] = max(rs), min(rs)
        if len(rs) >= 2:
            sd = statistics.stdev(rs)
            half = 1.96 * sd / math.sqrt(len(rs))
            out["ci95"] = [out["expectancy_r"] - half, out["expectancy_r"] + half]
        edges = [(-9e9, -1), (-1, -0.5), (-0.5, 0), (0, 0.5), (0.5, 1),
                 (1, 2), (2, 3), (3, 9e9)]
        labels = ["<−1R", "−1..−.5", "−.5..0", "0..+.5", "+.5..1R", "1..2R", "2..3R", ">3R"]
        out["r_histogram"] = [
            {"label": lb, "n": len([r for r in rs if lo <= r < hi])}
            for (lo, hi), lb in zip(edges, labels)
        ]
    return out


def compute_positions(book: dict, account: dict | None) -> list[dict]:
    now = time.time()
    acc_by_sym = {}
    if account:
        for p in account.get("positions", []):
            acc_by_sym[str(p.get("symbol", "")).upper()] = p

    out = []
    for p in book.get("positions", []):
        sym = str(p.get("symbol", "")).upper()
        a = acc_by_sym.get(sym, {})
        lm = float(a.get("last_mark_ts", 0) or 0)
        entry = float(p.get("entry_px") or a.get("entry_px") or 0)
        stop = float(p.get("stop_px") or a.get("stop_px") or 0)
        stop_dist = abs(stop - entry) / entry * 100 if entry and stop else None
        out.append({
            "venue": p.get("venue", "?"), "symbol": sym, "side": p.get("side", "?"),
            "size": float(p.get("size") or 0),
            "entry_px": entry, "stop_px": stop, "stop_dist_pct": stop_dist,
            "notional_usd": float(p.get("notional_usd") or 0),
            "unrealized_pnl_usd": float(p.get("unrealized_pnl_usd") or 0),
            "funding_paid_usd": float(p.get("funding_paid_usd") or 0),
            "last_mark_px": float(a.get("last_mark_px") or 0),
            "mark_age_s": (now - lm) if lm else None,
            "opened_ts": float(a.get("opened_ts") or 0),
            "hold_h": ((now - float(a.get("opened_ts") or 0)) / 3600) if a.get("opened_ts") else None,
            "thesis": a.get("thesis") or "",
            "invalidation": a.get("invalidation") or "",
        })
    return out


def compute_self_eval(freshness: dict, edge: dict, gates: list[dict], kill: dict) -> dict:
    """Deterministic introspection loop. It evaluates; it never changes risk limits."""
    n = int(edge.get("n_closes") or 0)
    target = int(edge.get("target_trades") or TARGET_TRADES)
    exp = edge.get("expectancy_r")
    ci = edge.get("ci95")
    plan = edge.get("plan_written_pct")
    costs = edge.get("cost_ratio_pct")
    max_gate = max(gates, key=lambda g: g.get("util_pct", 0), default=None)
    max_util = float(max_gate.get("util_pct", 0)) if max_gate else 0.0

    data_score = {"ok": 20, "watch": 12, "unknown": 2, "hot": 0}.get(
        freshness.get("status"), 0)
    risk_score = 25 if max_util < 60 else 15 if max_util < 85 else 5 if max_util < 100 else 0
    discipline_score = 10 if plan is None else 20 if plan >= 95 else 12 if plan >= 80 else 4
    if costs is not None and costs > 30:
        discipline_score = max(0, discipline_score - 5)

    if n < target:
        edge_score = round(min(10, n / max(target, 1) * 10))
    elif exp is None:
        edge_score = 5
    elif ci and ci[0] > 0:
        edge_score = 35
    elif exp > 0:
        edge_score = 22
    elif exp > -0.10:
        edge_score = 12
    else:
        edge_score = 2
    score = int(max(0, min(100, data_score + risk_score + discipline_score + edge_score)))

    blockers, actions, evidence = [], [], []
    if kill.get("active"):
        blockers.append("KILL actif")
        actions.append("Stopper toute nouvelle prise de risque et demander une revue humaine.")
    if freshness.get("status") in ("hot", "unknown"):
        blockers.append("données périmées ou incomplètes")
        actions.append("Rétablir et vérifier book-sync / marks avant toute autre analyse.")
    if max_gate and max_gate.get("status") in ("hot", "breach"):
        blockers.append(f"limite {max_gate['label']} consommée à {max_util:.0f} %")
        actions.append(f"Réduire le risque lié à « {max_gate['label']} » ; ne modifier aucune limite.")
    if n < target:
        actions.append(f"Collecter {target - n} clôtures supplémentaires sans retuner la stratégie.")
        evidence.append(f"échantillon {n}/{target} trades")
    elif exp is None:
        actions.append("Réparer la journalisation des multiples R avant d'évaluer l'edge.")
    elif exp <= 0:
        actions.append("Segmenter les pertes par setup, actif et régime ; tester une seule hypothèse en paper.")
        evidence.append(f"espérance {exp:+.2f} R")
    elif ci and ci[0] <= 0:
        actions.append("Conserver les paramètres et élargir l'échantillon : l'edge positif reste incertain.")
        evidence.append(f"IC95 {ci[0]:+.2f}…{ci[1]:+.2f} R")
    else:
        actions.append("Maintenir la stratégie ; surveiller la dérive sans optimisation opportuniste.")
        evidence.append(f"espérance {exp:+.2f} R")
    if plan is not None and plan < 90:
        actions.append("Rendre thèse et invalidation obligatoires avant chaque ouverture.")
        evidence.append(f"plans complets {plan:.0f} %")
    if costs is not None and costs > 30:
        actions.append("Réduire frais, slippage ou rotation avant de chercher davantage de rendement brut.")
        evidence.append(f"coûts / brut {costs:.1f} %")
    if max_gate:
        evidence.append(f"risque max {max_gate['label']} {max_util:.0f} %")

    if kill.get("active"):
        verdict = "HALTED"
    elif freshness.get("status") in ("hot", "unknown"):
        verdict = "BLOCKED"
    elif max_util >= 100 or (exp is not None and n >= target and exp <= 0):
        verdict = "DEGRADING"
    elif n < target:
        verdict = "LEARNING"
    elif exp is not None and exp > 0 and ci and ci[0] > 0:
        verdict = "PERFORMING"
    else:
        verdict = "IMPROVING"

    confidence = "low" if n < target else "high" if ci and ci[0] > 0 else "medium"
    return {
        "schema_version": 1,
        "verdict": verdict,
        "score": score,
        "improvement_needed": verdict in ("DEGRADING", "IMPROVING", "BLOCKED"),
        "confidence": confidence,
        "scores": {"data": data_score, "risk": risk_score,
                   "discipline": discipline_score, "edge": edge_score},
        "evidence": evidence,
        "blockers": blockers,
        "next_action": actions[0] if actions else "Observer sans modifier.",
        "actions": actions[:4],
        "review": {"closed_trades_now": n, "closed_trades_target": target,
                   "next_review_after_closes": max(n + 1, target) if n < target else n + 10},
        "mutation_policy": {
            "risk_limits_mutable": False,
            "live_autopromotion": False,
            "one_hypothesis_per_cycle": True,
            "paper_validation_required": True,
            "human_approval_for_live": True,
        },
    }


def build_state(demo: bool = False) -> tuple[dict, list[Source]]:
    if demo:
        return _demo_state(), _demo_sources()

    s_risk = read_risk(P_RISK)
    s_book = read_json("book.json", P_BOOK)
    s_acc = read_json("paper/account.json", P_ACCOUNT)
    s_jrn = read_jsonl("journal.jsonl", P_JOURNAL)
    s_ctx = read_json("live_context.json", P_CTX)
    s_scan = read_json("scan_latest.json", P_SCAN)
    sources = [s_risk, s_book, s_acc, s_jrn, s_ctx, s_scan]

    cfg = s_risk.data or {}
    book = s_book.data or {}
    account = s_acc.data
    journal = s_jrn.data or []

    cap = compute_capital(book)
    positions = compute_positions(book, account)
    fresh = compute_freshness(book, account, len(positions))
    gates = compute_gates(cfg, cap, book.get("positions", []), journal)
    edge = compute_edge(journal)
    kill = read_kill(P_KILL)

    if account:
        cap["paper"] = {
            "cash_usd": float(account.get("cash_usd") or 0),
            "realized_pnl_usd": float(account.get("realized_pnl_usd") or 0),
            "fees_paid_usd": float(account.get("fees_paid_usd") or 0),
            "funding_paid_usd": float(account.get("funding_paid_usd") or 0),
            "closed_trades": int(account.get("closed_trades") or 0),
            "start_equity_usd": float(account.get("start_equity_usd") or 0),
        }

    now = time.time()
    att = (f"BOOK · sync {book.get('synced_iso', '—')} · "
           f"equity {cap['equity_usd']:.2f}$ · DD {cap['dd_pct']:.2f}% · "
           f"positions {len(positions)}")

    state = {
        "built_ts": now,
        "built_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mode": (cfg.get("meta", {}) or {}).get("mode", book.get("mode", "?")),
        "attestation": att,
        "kill": kill,
        "freshness": fresh,
        "capital": cap,
        "gates": gates,
        "positions": positions,
        "edge": edge,
        "market": {
            "context": (s_ctx.data or {}),
            "signals": ((s_scan.data or {}).get("signals") or []),
            "scan_ts": (s_scan.data or {}).get("ts"),
        },
        "warnings": book.get("warnings", []),
        "provenance": [s.as_dict() for s in sources],
        "demo": False,
    }
    state["self_eval"] = compute_self_eval(fresh, edge, gates, kill)
    return state, sources


# ---------------------------------------------------------------------------
# Données de démonstration — pour valider la DA sans exposer d'état réel.
# Les prix de scan proviennent de la note Vault scan-2026-08-15_0003 UTC.
# ---------------------------------------------------------------------------

def _demo_sources() -> list[Source]:
    out = []
    for name, path, st, note, age in [
        ("risk.yaml", P_RISK, "VERIFIED", "limites chargées", 210000),
        ("book.json", P_BOOK, "VERIFIED", "lu", 118),
        ("paper/account.json", P_ACCOUNT, "VERIFIED", "lu", 5760),
        ("journal.jsonl", P_JOURNAL, "VERIFIED", "412 enregistrements", 5760),
        ("live_context.json", P_CTX, "VERIFIED", "lu", 940),
        ("scan_latest.json", P_SCAN, "UNVERIFIED", "fichier absent", None),
    ]:
        s = Source(name, path)
        s.state, s.note = st, note
        s.mtime = (time.time() - age) if age else None
        out.append(s)
    return out


def _demo_state() -> dict:
    now = time.time()
    eq, peak, day_open, week_open = 918.44, 1000.0, 942.90, 963.10
    cap = {
        "equity_usd": eq, "peak_usd": peak,
        "day_open_usd": day_open, "week_open_usd": week_open,
        "dd_pct": (peak - eq) / peak * 100,
        "day_pnl_pct": (eq - day_open) / day_open * 100,
        "week_pnl_pct": (eq - week_open) / week_open * 100,
        "paper": {"cash_usd": 926.11, "realized_pnl_usd": -73.89,
                  "fees_paid_usd": 11.42, "funding_paid_usd": -2.63,
                  "closed_trades": 11, "start_equity_usd": 1000.0},
    }
    positions = [{
        "venue": "hl", "symbol": "ETH", "side": "short", "size": 0.1961,
        "entry_px": 1920.40, "stop_px": 1978.10, "stop_dist_pct": 3.00,
        "notional_usd": 368.92, "unrealized_pnl_usd": -7.67,
        "funding_paid_usd": 0.41, "last_mark_px": 1881.25,
        "mark_age_s": 5760, "opened_ts": now - 3600 * 19,
        "hold_h": 19.2,
        "thesis": "Downtrend daily confirmé, prix sous MA200 (2019), rejet du haut de range 50h.",
        "invalidation": "Clôture horaire au-dessus de 1978 ou funding > +8 bps/8h.",
    }]
    gates = [
        _gate("dd", "Drawdown", "8.16 %", "20 %", 40.8, "depuis le pic d'equity · au-delà : KILL global"),
        _gate("day", "Perte jour", "−2.59 %", "−4 %", 64.8, "journée UTC"),
        _gate("week", "Perte semaine", "−4.64 %", "−8 %", 58.0, "semaine glissante"),
        _gate("floor", "Plancher equity", "918 $", "300 $", 32.7, "sous le plancher : sorties seulement"),
        _gate("pos", "Positions", "1", "4", 25.0, "concurrentes, tous venues"),
        _gate("gross", "Expo brute", "40 %", "150 %", 26.8, "somme des notionnels / equity"),
        _gate("net", "Expo nette", "40 %", "100 %", 40.2, "|long − short| / equity"),
        _gate("oh", "Ordres / h", "1", "8", 12.5, "anti-boucle folle"),
        _gate("od", "Ordres / j", "6", "30", 20.0, "anti-overtrading"),
        _gate("streak", "Série pertes", "2", "3", 66.7, "au-delà : cooldown imposé"),
        _gate("cooldown", "Cooldown", "inactif", "6 h", 0.0, "s'arme après la série"),
    ]
    edge = {
        "n_opens": 12, "n_closes": 11, "target_trades": TARGET_TRADES, "verified": False,
        "expectancy_r": -0.34, "ci95": [-0.92, 0.24], "win_rate_pct": 36.4,
        "cost_ratio_pct": 18.6, "stop_share_pct": 54.5, "median_hold_h": 14.5,
        "plan_written_pct": 100.0, "best_r": 2.4, "worst_r": -1.05,
        "fees_usd": 11.42, "funding_usd": -2.63, "gross_usd": 75.6, "net_usd": -73.89,
        "r_histogram": [
            {"label": "<−1R", "n": 1}, {"label": "−1..−.5", "n": 4},
            {"label": "−.5..0", "n": 2}, {"label": "0..+.5", "n": 1},
            {"label": "+.5..1R", "n": 1}, {"label": "1..2R", "n": 1},
            {"label": "2..3R", "n": 1}, {"label": ">3R", "n": 0},
        ],
    }
    ctx = {"ts": int(now - 940), "coins": {
        "BTC": {"mid": 63028.50, "ma20": 63910.20, "dev_pct": -1.38, "range_lo_50h": 62110.0,
                "range_hi_50h": 64980.0, "funding_bps_8h": 0.125,
                "daily_lecture": "daily downtrend (MA200 69382, pos20j 24%, pos100j 18%)"},
        "ETH": {"mid": 1881.25, "ma20": 1918.60, "dev_pct": -1.95, "range_lo_50h": 1858.0,
                "range_hi_50h": 1974.0, "funding_bps_8h": 0.060,
                "daily_lecture": "daily downtrend (MA200 2019, pos20j 21%, pos100j 15%)"},
        "SOL": {"mid": 75.37, "ma20": 77.02, "dev_pct": -2.14, "range_lo_50h": 74.10,
                "range_hi_50h": 79.60, "funding_bps_8h": 0.070,
                "daily_lecture": "daily downtrend (MA200 96, pos20j 19%, pos100j 12%)"},
        "HYPE": {"mid": 56.50, "ma20": 57.88, "dev_pct": -2.38, "range_lo_50h": 55.20,
                 "range_hi_50h": 60.10, "funding_bps_8h": 0.125,
                 "daily_lecture": "daily downtrend (MA200 71, pos20j 22%, pos100j 20%)"},
        "ZEC": {"mid": 493.27, "ma20": 486.10, "dev_pct": 1.48, "range_lo_50h": 470.0,
                "range_hi_50h": 512.0, "funding_bps_8h": 0.125,
                "daily_lecture": "daily uptrend (MA200 388, pos20j 71%, pos100j 88%)"},
    }}
    signals = [
        {"asset": "BTC", "class": "perp", "side": "short", "conviction": "élevée",
         "price": 63028.50, "funding_bps": 0.125, "oi_usd": 2718e6,
         "thesis": "sous MA200 daily, rejet 64.9k", "invalidation": "reprise > 65k en clôture 1h"},
        {"asset": "HYPE", "class": "perp", "side": "short", "conviction": "élevée",
         "price": 56.50, "funding_bps": 0.125, "oi_usd": 1244e6,
         "thesis": "OI en hausse sur prix plat", "invalidation": "> 60.1"},
        {"asset": "ZEC", "class": "perp", "side": "short", "conviction": "élevée",
         "price": 493.27, "funding_bps": 0.125, "oi_usd": 199e6,
         "thesis": "extension au-dessus MA200", "invalidation": "> 512"},
        {"asset": "ETH", "class": "perp", "side": "short", "conviction": "moyenne",
         "price": 1881.25, "funding_bps": 0.060, "oi_usd": 1665e6,
         "thesis": "sous MA200 2019", "invalidation": "> 1978"},
        {"asset": "SOL", "class": "perp", "side": "short", "conviction": "moyenne",
         "price": 75.37, "funding_bps": 0.070, "oi_usd": 382e6,
         "thesis": "sous MA200 96", "invalidation": "> 79.6"},
        {"asset": "SPCXx", "class": "xstock", "side": "alert", "conviction": "moyenne",
         "price": 140.48, "thesis": "hors séance — prix oracle", "invalidation": "—"},
        {"asset": "NVDAx", "class": "xstock", "side": "alert", "conviction": "moyenne",
         "price": 224.90, "thesis": "hors séance — prix oracle", "invalidation": "—"},
        {"asset": "GBOY", "class": "memecoin_sol", "side": "alert", "conviction": "faible",
         "price": 0.000816, "thesis": "registre mint OK", "invalidation": "—"},
        {"asset": "AWR", "class": "memecoin_sol", "side": "alert", "conviction": "faible",
         "price": 0.000511, "thesis": "registre mint OK", "invalidation": "—"},
        {"asset": "SPX6900", "class": "memecoin_cg", "side": "alert", "conviction": "faible",
         "price": 0.317395, "thesis": "cluster memes saturé", "invalidation": "—"},
    ]
    state = {
        "built_ts": now,
        "built_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "mode": "paper",
        "attestation": (f"BOOK · sync {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - 118))} · "
                        f"equity 918.44$ · DD 8.16% · positions 1"),
        "kill": {"active": False, "reason": None, "since_iso": None},
        "freshness": {"sync_age_s": 118, "sync_status": "ok",
                      "mark_age_s": 5760, "mark_status": "watch",
                      "mark_note": "position la plus mal marquée", "status": "watch"},
        "capital": cap, "gates": gates, "positions": positions, "edge": edge,
        "market": {"context": ctx, "signals": signals, "scan_ts": now - 940},
        "warnings": ["mode paper — compte simulé réel · cash 926.11$ · frais cumulés 11.42$ "
                     "· funding cumulé -2.63$"],
        "provenance": [s.as_dict() for s in _demo_sources()],
        "demo": True,
    }
    state["self_eval"] = compute_self_eval(
        state["freshness"], edge, gates, state["kill"])
    return state


# ---------------------------------------------------------------------------
# Rendu
# ---------------------------------------------------------------------------

def e(x) -> str:
    return html.escape(str(x), quote=True)


def dur(s) -> str:
    if s is None:
        return "—"
    s = float(s)
    if s < 90:
        return f"{s:.0f}\u00a0s"
    if s < 5400:
        return f"{s / 60:.0f}\u00a0min"
    if s < 172800:
        return f"{s / 3600:.1f}\u00a0h"
    return f"{s / 86400:.1f}\u00a0j"


NB = "\u00a0"        # espace insécable — un montant ne se coupe pas de son unité


def money(v, dec=2) -> str:
    return f"{v:,.{dec}f}".replace(",", NB) + NB + "$"


def asset_uri(name: str) -> str:
    """Embed a bundled visual so the generated dashboard stays offline-first."""
    path = Path(__file__).resolve().parent / "assets" / name
    if not path.exists():
        return ""
    mime = "image/webp" if path.suffix.lower() == ".webp" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


CSS = """
:root{
  --paper:#E8E3D9; --paper-2:#DED7C8; --paper-3:#F2EEE6;
  --ink:#0B1533; --ink-soft:#3A4468;
  --cobalt:#1F45C8; --prussian:#0F2E63; --indigo:#141C52; --navy:#070C1E;
  --gold:#B0801F; --gold-lite:#D7A83A;
  --oxblood:#7C1D21; --oxblood-lite:#B14A44;
  --hair:rgba(11,21,51,.22);
  --iii-symbol:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 120 108'%3E%3Ccircle cx='20' cy='25' r='16' fill='black'/%3E%3Ccircle cx='60' cy='19' r='18' fill='black'/%3E%3Ccircle cx='100' cy='25' r='16' fill='black'/%3E%3Cpath d='M7 50L35 45L41 103L3 106Z' fill='black'/%3E%3Cpath d='M46 43H74L78 104H42Z' fill='black'/%3E%3Cpath d='M85 45L113 50L117 106L79 103Z' fill='black'/%3E%3C/svg%3E");
  --mono:ui-monospace,"SF Mono","Cascadia Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --sans:ui-sans-serif,"Helvetica Neue",Helvetica,Arial,sans-serif;
  --serif:"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;
}
*{box-sizing:border-box}
html,body{margin:0;padding:0}
body{
  background:var(--paper); color:var(--ink);
  font-family:var(--mono); font-size:14px; line-height:1.5;
  -webkit-font-smoothing:antialiased;
  background-image:
    radial-gradient(120% 60% at 88% -8%, rgba(31,69,200,.13), transparent 62%),
    radial-gradient(80% 45% at -10% 108%, rgba(124,29,33,.06), transparent 60%);
  background-attachment:fixed;
}
.grain{position:fixed;inset:0;pointer-events:none;z-index:60;opacity:.30;
  mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='180' height='180'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='4' stitchTiles='stitch'/><feColorMatrix type='saturate' values='0'/></filter><rect width='180' height='180' filter='url(%23n)' opacity='.55'/></svg>");
  background-size:180px 180px}
.fibers{position:fixed;inset:0;pointer-events:none;z-index:59;opacity:.16;
  mix-blend-mode:multiply;
  background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='420' height='420'><filter id='f'><feTurbulence type='fractalNoise' baseFrequency='.012 .34' numOctaves='2'/><feColorMatrix type='saturate' values='0'/></filter><rect width='420' height='420' filter='url(%23f)' opacity='.5'/></svg>")}

.page{max-width:900px;margin:0 auto;padding:34px 22px 90px;position:relative}
.page::before,.page::after{content:"";position:absolute;width:16px;height:16px;
  border:1px solid var(--cobalt);opacity:.5}
.page::before{top:12px;left:6px;border-right:0;border-bottom:0}
.page::after{bottom:56px;right:6px;border-left:0;border-top:0}

.eyebrow{font-size:9.5px;letter-spacing:.30em;text-transform:uppercase;
  color:var(--cobalt);font-weight:700;margin:0 0 8px}
.rule{height:1px;background:var(--hair);margin:0 0 16px}
.rule--thick{height:3px;background:var(--ink);opacity:.9}
.sec{margin:44px 0 0}
.note{font-size:11px;color:var(--ink-soft);line-height:1.55}
.serif{font-family:var(--serif);font-style:italic}

/* ---------- masthead ---------- */
.mast{display:flex;justify-content:space-between;align-items:flex-end;gap:18px;flex-wrap:wrap}
.wordmark{font-family:var(--sans);font-weight:800;font-size:clamp(46px,13vw,92px);
  line-height:.82;letter-spacing:-.045em;transform:scaleX(.9);transform-origin:left bottom;
  color:var(--ink);position:relative}
.wordmark::after{content:"N*ABU";position:absolute;left:1.5px;top:1.5px;
  color:var(--gold);mix-blend-mode:multiply;opacity:.55;z-index:-1}
.path{font-size:9px;color:var(--ink-soft);text-align:left;word-break:break-all}
@media(max-width:600px){.mast-right{text-align:left!important}}
.mast-right{text-align:right;font-size:10px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--ink-soft)}
.badge{display:inline-block;border:1px solid var(--ink);padding:3px 8px;
  font-size:9.5px;letter-spacing:.2em;text-transform:uppercase;font-weight:700}
.badge--mode{background:var(--ink);color:var(--paper)}
.badge--demo{border-color:var(--gold);color:var(--gold)}
.tagline{font-size:9.5px;letter-spacing:.34em;text-transform:uppercase;color:var(--cobalt);
  margin-top:10px}

/* ---------- kill ---------- */
.kill{margin-top:26px;background:var(--oxblood);color:var(--paper-3);padding:18px 20px;
  border:2px solid var(--navy)}
.kill h2{margin:0 0 6px;font-family:var(--sans);font-size:26px;letter-spacing:.06em}
.kill p{margin:0;font-size:12px;opacity:.92}

/* ---------- attestation (héros) ---------- */
.attest{margin-top:30px}
.attest-line{font-family:var(--sans);font-weight:700;
  font-size:clamp(15px,3.4vw,23px);line-height:1.34;letter-spacing:-.012em;
  margin:0 0 20px;word-break:break-word}
.clocks{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media(max-width:520px){.clocks{grid-template-columns:1fr}}
.clock{border:1px solid var(--hair);border-left:5px solid var(--cobalt);
  padding:12px 14px;background:rgba(255,255,255,.28)}
.clock--watch{border-left-color:var(--gold);background:rgba(176,128,31,.09)}
.clock--hot{border-left-color:var(--oxblood);background:rgba(124,29,33,.09)}
.clock--unknown{border-left-color:var(--ink-soft)}
.clock-k{display:block;font-size:9.5px;letter-spacing:.26em;color:var(--ink-soft)}
.clock-v{display:block;font-family:var(--sans);font-weight:800;font-size:30px;
  line-height:1.1;margin:2px 0 2px;font-variant-numeric:tabular-nums}
.clock--watch .clock-v{color:var(--gold)}
.clock--hot .clock-v{color:var(--oxblood)}
.clock-n{display:block;font-size:10.5px;color:var(--ink-soft)}
.verdict{margin:18px 0 0;font-family:var(--serif);font-style:italic;
  font-size:15px;line-height:1.6;color:var(--ink);border-left:2px solid var(--cobalt);
  padding-left:14px}

/* ---------- plaque (fond encré) ---------- */
.plate{background:var(--prussian);color:var(--paper-3);padding:26px 24px;position:relative;
  overflow:hidden;box-shadow:0 1px 0 rgba(11,21,51,.5)}
.plate::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.30;
  background-image:radial-gradient(rgba(232,227,217,.5) .7px, transparent .8px);
  background-size:4px 4px}
.plate::after{content:"";position:absolute;inset:0;pointer-events:none;opacity:.16;
  background:repeating-linear-gradient(180deg,rgba(255,255,255,.14) 0 1px,transparent 1px 3px)}
.plate .eyebrow{color:var(--gold-lite)}
.plate .note{color:rgba(232,227,217,.68)}
.plate > *{position:relative;z-index:1}

.cap-grid{display:grid;grid-template-columns:1.35fr 1fr 1fr;gap:22px;align-items:end}
@media(max-width:640px){.cap-grid{grid-template-columns:1fr 1fr}}
.cap-eq{white-space:nowrap;font-family:var(--sans);font-weight:800;letter-spacing:-.035em;
  font-size:clamp(40px,10.5vw,68px);line-height:.94;font-variant-numeric:tabular-nums}
.cap-k{font-size:9.5px;letter-spacing:.26em;color:rgba(232,227,217,.6);text-transform:uppercase}
.cap-v{font-family:var(--sans);font-weight:700;font-size:22px;font-variant-numeric:tabular-nums;
  line-height:1.2}
.neg{color:var(--oxblood-lite)}
.cap-strip{display:flex;flex-wrap:wrap;gap:20px;margin-top:22px;padding-top:16px;
  border-top:1px solid rgba(232,227,217,.22);font-size:11.5px}
.cap-strip b{font-weight:700;font-variant-numeric:tabular-nums}

/* ---------- planche des limites (signature) ---------- */
.wedge{display:grid;grid-template-columns:repeat(auto-fit,minmax(72px,1fr));gap:1px;
  background:var(--paper-3);padding:1px}
.step{background:var(--paper-3);box-shadow:0 0 0 1px var(--hair);padding:12px 6px 10px;display:flex;flex-direction:column;
  align-items:center;gap:8px;min-width:0}
.step-pct{font-family:var(--sans);font-weight:800;font-size:13px;
  font-variant-numeric:tabular-nums;color:var(--cobalt)}
.step--watch .step-pct{color:var(--gold)}
.step--hot .step-pct,.step--breach .step-pct{color:var(--oxblood)}
.track{width:100%;height:132px;position:relative;background:rgba(31,69,200,.07);
  background-image:repeating-linear-gradient(0deg,rgba(11,21,51,.16) 0 1px,transparent 1px 26.4px);
  overflow:hidden}
.fill{position:absolute;left:0;right:0;bottom:0;height:var(--h);background:var(--cobalt);
  animation:expose .85s cubic-bezier(.2,.75,.25,1) both}
.step--watch .fill{background:var(--gold)}
.step--hot .fill{background:var(--oxblood)}
.step--breach .fill{background:repeating-linear-gradient(45deg,var(--oxblood) 0 5px,var(--navy) 5px 10px)}
@keyframes expose{from{height:0}to{height:var(--h)}}
@media(prefers-reduced-motion:reduce){.fill{animation:none}}
.step-l{font-size:9px;letter-spacing:.08em;text-transform:uppercase;text-align:center;
  line-height:1.25;color:var(--ink);min-height:22px}
.step-v{font-size:10px;font-variant-numeric:tabular-nums;text-align:center;color:var(--ink)}
.step-lim{font-size:9px;color:var(--ink-soft);text-align:center}
.legend{display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;font-size:10px;
  letter-spacing:.06em;color:var(--ink-soft)}
.dot{display:inline-block;width:8px;height:8px;margin-right:5px;vertical-align:baseline}
.dot--ok{background:var(--cobalt)}.dot--watch{background:var(--gold)}
.dot--hot{background:var(--oxblood)}

/* ---------- tables ---------- */
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;font-size:9px;letter-spacing:.2em;text-transform:uppercase;
  color:var(--ink-soft);font-weight:700;padding:0 10px 7px 0;border-bottom:1px solid var(--hair);
  white-space:nowrap}
.tbl td{padding:9px 10px 9px 0;border-bottom:1px solid rgba(11,21,51,.10);
  font-variant-numeric:tabular-nums;vertical-align:top}
.tbl tr:last-child td{border-bottom:0}
.tbl .num{text-align:right;padding-right:20px;white-space:nowrap}
.side{font-weight:700;letter-spacing:.1em;font-size:10px;text-transform:uppercase}
.side--short{color:var(--oxblood)}.side--long{color:var(--cobalt)}
.conv{font-size:9px;letter-spacing:.14em;text-transform:uppercase;border:1px solid currentColor;
  padding:1px 5px}
.conv--elevee{color:var(--gold)}.conv--moyenne{color:var(--cobalt)}
.conv--faible{color:var(--ink-soft)}
.wrap{max-width:100%;overflow-x:auto}
.thesis{font-family:var(--serif);font-style:italic;font-size:12.5px;color:var(--ink-soft);
  margin:8px 0 0;line-height:1.55}
.empty{font-family:var(--serif);font-style:italic;font-size:16px;color:var(--ink-soft);
  padding:20px 0}

/* ---------- edge ---------- */
.stamp{display:inline-block;border:2px solid var(--oxblood);color:var(--oxblood);
  padding:4px 10px;font-size:10px;letter-spacing:.22em;font-weight:700;text-transform:uppercase;
  transform:rotate(-1.4deg)}
.stamp--ok{border-color:var(--cobalt);color:var(--cobalt)}
.edge-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:1px;
  background:var(--hair);margin-top:16px}
.cell{background:var(--paper-3);padding:13px 12px}
.cell-k{font-size:9px;letter-spacing:.2em;text-transform:uppercase;color:var(--ink-soft)}
.cell-v{font-family:var(--sans);font-weight:800;font-size:24px;line-height:1.2;margin-top:3px;
  font-variant-numeric:tabular-nums}
.cell-n{font-size:10px;color:var(--ink-soft);margin-top:3px}
.progress{height:8px;background:rgba(31,69,200,.12);margin-top:12px;position:relative}
.progress span{position:absolute;inset:0 auto 0 0;background:var(--cobalt);width:var(--w)}
.hist{display:grid;grid-template-columns:repeat(8,1fr);gap:3px;align-items:end;height:92px;
  margin-top:18px}
.hbar{background:var(--cobalt);min-height:2px;position:relative}
.hbar--neg{background:var(--oxblood)}
.hlab{display:grid;grid-template-columns:repeat(8,1fr);gap:3px;margin-top:6px;
  font-size:8.5px;color:var(--ink-soft);text-align:center;letter-spacing:.02em}

/* ---------- provenance ---------- */
.prov{font-size:11.5px}
.prov td:first-child{font-weight:700}
.vs{font-size:9px;letter-spacing:.18em;font-weight:700;padding:1px 6px;border:1px solid currentColor}
.vs--VERIFIED{color:var(--cobalt)}.vs--UNVERIFIED{color:var(--gold)}
.vs--FAILED{color:var(--oxblood)}

footer{margin-top:56px;padding-top:18px;border-top:3px solid var(--ink);
  font-size:10.5px;color:var(--ink-soft);line-height:1.7}
footer b{color:var(--ink)}

/* ---------- NOUS × N*ABU / editorial system ---------- */
body::before{content:"";position:fixed;inset:0;pointer-events:none;z-index:-1;
  background:linear-gradient(90deg,rgba(15,46,99,.035) 1px,transparent 1px),
    linear-gradient(rgba(15,46,99,.025) 1px,transparent 1px);background-size:42px 42px}
.page{max-width:1180px;padding:22px 34px 100px}
.topline{display:flex;justify-content:space-between;align-items:center;padding:7px 0 9px;
  border-top:5px solid var(--ink);border-bottom:1px solid var(--hair);font-size:8px;
  letter-spacing:.24em;text-transform:uppercase;color:var(--ink-soft)}
.topline strong{color:var(--cobalt)}
.issue{font-variant-numeric:tabular-nums}
.mast{display:grid;grid-template-columns:minmax(0,1.45fr) minmax(250px,.55fr);align-items:stretch;
  gap:0;margin-top:14px;border:1px solid var(--ink)}
.mast-main{min-height:300px;padding:28px 30px 24px;display:flex;flex-direction:column;
  justify-content:space-between;position:relative;overflow:hidden;
  background:var(--prussian);color:var(--paper-3)}
.mast-main::before{content:"";position:absolute;inset:0;opacity:.20;pointer-events:none;
  background:repeating-radial-gradient(ellipse at 80% 38%,transparent 0 6px,rgba(242,238,230,.2) 7px 8px)}
.mast-main::after{content:"03";position:absolute;right:-.05em;bottom:-.25em;
  font-family:var(--sans);font-weight:900;font-size:260px;line-height:1;color:rgba(242,238,230,.045)}
.mast-main>*{position:relative;z-index:1}
.wordmark{color:var(--paper-3);font-size:clamp(66px,12vw,138px);letter-spacing:-.07em;
  text-shadow:2px 0 0 rgba(215,168,58,.65);transform:scaleX(.88)}
.wordmark::after{display:none}
.tagline{color:var(--gold-lite);margin-top:14px}
.motto{max-width:560px;margin:34px 0 0;font-family:var(--serif);font-size:clamp(18px,2vw,27px);
  font-style:italic;line-height:1.25;color:var(--paper-3)}
.mast-side{position:relative;display:flex;flex-direction:column;justify-content:space-between;
  min-height:300px;padding:24px;background:var(--paper-3);overflow:hidden}
.agent-mark{align-self:flex-end;width:138px;height:138px;border-radius:50%;border:1px solid var(--cobalt);
  display:grid;place-items:center;position:relative;color:var(--cobalt);font-family:var(--sans);
  font-size:58px;font-weight:900;letter-spacing:-.16em;filter:contrast(1.1)}
.agent-mark::before{content:"";position:absolute;inset:8px;border:1px dashed rgba(31,69,200,.34);border-radius:50%}
.agent-mark::after{content:"ANALYST / HUNTER";position:absolute;width:210px;text-align:center;
  bottom:-25px;font:7px var(--mono);letter-spacing:.24em;transform:rotate(-8deg)}
.mast-right{text-align:left;margin-top:32px}.mast-right .badge{margin-bottom:10px}
.attest{margin-top:18px;border:1px solid var(--hair);padding:20px;background:rgba(242,238,230,.55)}
.sec{margin-top:58px}.eyebrow{display:flex;align-items:center;gap:10px}.eyebrow::before{
  content:"";width:19px;height:18px;flex:0 0 auto;background:var(--gold);
  -webkit-mask:var(--iii-symbol) center/contain no-repeat;mask:var(--iii-symbol) center/contain no-repeat}
.plate{padding:34px 30px;box-shadow:10px 10px 0 rgba(15,46,99,.11)}
.clock,.step,.cell{transition:transform .18s ease,background .18s ease}
.clock:hover,.cell:hover{transform:translateY(-2px);background:var(--paper-3)}
.step:hover{transform:translateY(-3px);z-index:2}
.wedge{border:1px solid var(--hair);background:var(--ink)}
.track{height:156px;background-color:rgba(31,69,200,.055)}
.tbl tbody tr{transition:background .15s ease}.tbl tbody tr:hover{background:rgba(31,69,200,.07)}
.tbl th{color:var(--cobalt);border-bottom:2px solid var(--cobalt)}
.stamp{box-shadow:3px 3px 0 rgba(124,29,33,.12)}
footer{display:grid;grid-template-columns:1fr auto;gap:24px;align-items:end}
footer::after{content:"";width:54px;height:48px;background:var(--gold);
  -webkit-mask:var(--iii-symbol) center/contain no-repeat;mask:var(--iii-symbol) center/contain no-repeat}
@media(max-width:760px){
  .page{padding:14px 14px 72px}.topline span:nth-child(2){display:none}
  .mast{grid-template-columns:1fr}.mast-main{min-height:260px;padding:24px 20px}
  .mast-side{min-height:170px;padding:18px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
  .agent-mark{width:104px;height:104px;font-size:42px}.mast-right{margin:0;align-self:end}
  .attest{padding:15px}.sec{margin-top:42px}.plate{padding:26px 18px}
  .cap-grid{grid-template-columns:1fr}.cap-eq{font-size:clamp(38px,13vw,58px)}
}
@media print{.grain,.fibers{display:none}.page{max-width:none;padding:0}.clock:hover,.cell:hover,.step:hover{transform:none}}

/* ---------- portfolio cockpit / progressive disclosure ---------- */
.overview{margin-top:18px;background:var(--paper-3);border:1px solid var(--ink);padding:22px}
.overview-head{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}
.overview-title{font:800 12px var(--sans);letter-spacing:.16em;text-transform:uppercase}
.health{display:inline-flex;align-items:center;gap:7px;border:1px solid currentColor;padding:4px 8px;
  color:var(--cobalt);font-size:9px;letter-spacing:.14em;text-transform:uppercase;font-weight:700}
.health::before{content:"";width:7px;height:7px;border-radius:50%;background:currentColor}
.health--watch,.health--unknown{color:var(--gold)}.health--hot{color:var(--oxblood)}
.overview-grid{display:grid;grid-template-columns:1.55fr repeat(4,1fr);gap:1px;background:var(--hair)}
.metric{min-width:0;background:var(--paper-3);padding:15px 14px}
.metric:first-child{background:var(--prussian);color:var(--paper-3)}
.metric-k{font-size:8.5px;letter-spacing:.2em;text-transform:uppercase;color:var(--ink-soft)}
.metric:first-child .metric-k{color:rgba(242,238,230,.62)}
.metric-v{font:800 clamp(20px,3vw,31px)/1.05 var(--sans);margin-top:5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.metric:first-child .metric-v{font-size:clamp(30px,5vw,52px)}
.metric-n{margin-top:6px;font-size:9px;color:var(--ink-soft)}
.metric:first-child .metric-n{color:rgba(242,238,230,.62)}
.decision{display:flex;gap:10px;align-items:flex-start;margin-top:15px;padding-top:14px;border-top:1px solid var(--hair);
  font-family:var(--serif);font-style:italic;color:var(--ink-soft)}
.decision b{font:800 9px var(--mono);font-style:normal;letter-spacing:.16em;text-transform:uppercase;color:var(--cobalt)}
.self-eval{margin-top:12px;display:grid;grid-template-columns:190px minmax(0,1fr) 1.1fr;border:1px solid var(--ink);background:var(--paper-3)}
.eval-verdict{padding:20px;background:var(--navy);color:var(--paper-3);display:flex;flex-direction:column;justify-content:space-between}
.eval-k{font-size:8px;letter-spacing:.2em;text-transform:uppercase;color:rgba(242,238,230,.56)}
.eval-status{font:900 21px/1 var(--sans);letter-spacing:.04em;margin-top:8px;color:var(--gold-lite)}
.eval-status--PERFORMING{color:var(--paper-3)}.eval-status--DEGRADING,.eval-status--HALTED,.eval-status--BLOCKED{color:var(--oxblood-lite)}
.eval-score{font:900 58px/.9 var(--sans);margin-top:22px}.eval-score small{font:10px var(--mono);color:rgba(242,238,230,.55)}
.eval-axes{padding:20px;border-right:1px solid var(--hair)}
.axis{display:grid;grid-template-columns:80px 1fr 28px;gap:8px;align-items:center;margin:10px 0;font-size:8px;letter-spacing:.12em;text-transform:uppercase}
.axis-track{height:6px;background:rgba(31,69,200,.10)}.axis-track i{display:block;width:var(--w);height:100%;background:var(--cobalt)}
.eval-action{padding:20px;display:flex;flex-direction:column;justify-content:space-between}.eval-action strong{font:800 9px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--cobalt)}
.eval-action p{font:italic 17px/1.35 var(--serif);margin:10px 0;color:var(--ink)}
.eval-meta{font-size:9px;color:var(--ink-soft)}
.risk-strip{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.risk-card{border:1px solid var(--hair);padding:13px;background:rgba(242,238,230,.65)}
.risk-card-head{display:flex;justify-content:space-between;gap:8px;font-size:9px;text-transform:uppercase;letter-spacing:.08em}
.risk-card-head b{color:var(--cobalt)}.risk-card--watch .risk-card-head b{color:var(--gold)}
.risk-card--hot .risk-card-head b,.risk-card--breach .risk-card-head b{color:var(--oxblood)}
.risk-meter{height:5px;background:rgba(31,69,200,.10);margin:10px 0 7px}.risk-meter span{display:block;height:100%;width:var(--w);background:var(--cobalt)}
.risk-card--watch .risk-meter span{background:var(--gold)}.risk-card--hot .risk-meter span,.risk-card--breach .risk-meter span{background:var(--oxblood)}
.risk-value{font-size:10px;color:var(--ink-soft)}
.drawer{margin-top:22px;border-top:1px solid var(--hair);border-bottom:1px solid var(--hair);padding:0}
.drawer>summary{cursor:pointer;list-style:none;padding:14px 4px;display:flex;align-items:center;justify-content:space-between;
  gap:16px;font-size:9px;font-weight:700;letter-spacing:.18em;text-transform:uppercase;color:var(--cobalt)}
.drawer>summary::-webkit-details-marker{display:none}.drawer>summary::after{content:"＋";font-size:17px;font-weight:400}
.drawer[open]>summary::after{content:"−"}.drawer[open]>summary{border-bottom:1px solid var(--hair)}
.drawer-body{padding:20px 4px 26px}.drawer .sec{margin-top:0}
.primary-section{margin-top:40px}.primary-section>.eyebrow{font-size:11px}
@media(max-width:850px){.overview-grid{grid-template-columns:repeat(2,1fr)}.metric:first-child{grid-column:1/-1}.risk-strip{grid-template-columns:1fr 1fr}}
@media(max-width:520px){.overview{padding:15px}.overview-head{align-items:flex-start;flex-direction:column}.overview-grid{grid-template-columns:1fr 1fr}
  .metric{padding:12px 10px}.risk-strip{grid-template-columns:1fr}.decision{display:block}.decision b{display:block;margin-bottom:4px}}
@media(max-width:850px){.self-eval{grid-template-columns:150px 1fr}.eval-action{grid-column:1/-1;border-top:1px solid var(--hair)}.eval-axes{border-right:0}}
@media(max-width:520px){.self-eval{grid-template-columns:1fr}.eval-verdict{min-height:150px}.eval-axes,.eval-action{grid-column:auto;border-top:1px solid var(--hair)}.eval-score{font-size:46px}}

/* ---------- modern agent command center ---------- */
body{background:var(--paper-3)}
.rail{position:fixed;inset:0 auto 0 0;width:82px;z-index:70;background:var(--navy);color:var(--paper-3);
  display:flex;flex-direction:column;align-items:center;padding:18px 0 16px;border-right:1px solid rgba(242,238,230,.16)}
.rail-logo{font:900 28px/1 var(--sans);letter-spacing:-.16em;color:var(--gold-lite);writing-mode:vertical-rl;transform:rotate(180deg)}
.rail-avatar{width:48px;height:48px;margin-top:16px;border-radius:50%;object-fit:cover;border:1px solid rgba(215,168,58,.7);
  filter:grayscale(.45) contrast(1.15);box-shadow:0 0 0 4px rgba(31,69,200,.18)}
.rail-nav{margin:auto 0;display:flex;flex-direction:column;gap:9px}.rail-nav a{width:42px;height:42px;display:grid;place-items:center;
  border:1px solid rgba(242,238,230,.18);color:rgba(242,238,230,.68);text-decoration:none;font-size:9px;letter-spacing:.08em;transition:.18s}
.rail-nav a:hover,.rail-nav a:focus{background:var(--cobalt);color:white;border-color:var(--cobalt);transform:translateX(3px)}
.rail-foot{font-size:7px;letter-spacing:.2em;writing-mode:vertical-rl;color:rgba(242,238,230,.45)}
.page{max-width:1320px;margin:0 auto 0 calc(82px + max(0px,(100vw - 1402px)/2));padding:18px 28px 90px}
.topline{border-top:0;height:36px;padding:0 2px}
.mast{height:350px;display:grid;grid-template-columns:1.08fr .92fr;margin-top:10px;border:0;background:var(--prussian);overflow:hidden;position:relative}
.mast::after{content:"";position:absolute;inset:0;pointer-events:none;z-index:4;opacity:.18;
  background:repeating-linear-gradient(0deg,transparent 0 3px,rgba(255,255,255,.16) 4px)}
.mast-copy{padding:34px 36px;display:flex;flex-direction:column;justify-content:space-between;position:relative;z-index:5;color:var(--paper-3)}
.mast-copy::after{content:"";position:absolute;right:22px;top:18px;width:86px;height:78px;background:rgba(215,168,58,.24);
  -webkit-mask:var(--iii-symbol) center/contain no-repeat;mask:var(--iii-symbol) center/contain no-repeat}
.iii-icon{display:inline-block;width:22px;height:20px;background:currentColor;vertical-align:middle;
  -webkit-mask:var(--iii-symbol) center/contain no-repeat;mask:var(--iii-symbol) center/contain no-repeat}
.wordmark{font-size:clamp(68px,9vw,126px);line-height:.76}.tagline{margin-top:20px}.motto{font-size:21px;margin:0;max-width:470px}
.agent-id{display:flex;gap:12px;align-items:center;font-size:8px;letter-spacing:.18em;text-transform:uppercase;color:rgba(242,238,230,.62)}
.agent-id i{width:28px;height:1px;background:var(--gold-lite)}
.mast-visual{position:relative;overflow:hidden;background:var(--navy)}
.mast-visual img{width:100%;height:100%;object-fit:cover;object-position:50% 36%;filter:grayscale(.5) contrast(1.15) saturate(.72)}
.mast-visual::before{content:"";position:absolute;inset:0;z-index:2;background:rgba(31,69,200,.42);mix-blend-mode:color}
.mast-visual::after{content:"AGENT 03 / ACTIVE OBSERVER";position:absolute;z-index:3;right:16px;bottom:14px;padding:6px 8px;
  background:var(--paper-3);color:var(--prussian);font:700 8px var(--mono);letter-spacing:.17em}
.mast-meta{position:absolute;z-index:5;left:18px;top:18px;display:flex;gap:6px;flex-wrap:wrap}.mast-meta .badge{background:rgba(7,12,30,.78);color:var(--paper-3);border-color:rgba(242,238,230,.45)}
.overview{margin-top:12px;border:0;padding:0;background:transparent}.overview-head{margin:0 0 10px;padding:12px 15px;background:var(--ink);color:var(--paper-3)}
.overview-title{font-size:10px}.overview-grid{border:1px solid var(--hair);gap:0;background:transparent}.metric{border-right:1px solid var(--hair)}
.metric:last-child{border-right:0}.metric:first-child{background:var(--cobalt)}
.decision{margin-top:0;padding:12px 15px;border:1px solid var(--hair);border-top:0;background:rgba(31,69,200,.04)}
.primary-section{scroll-margin-top:18px}.drawer{scroll-margin-top:18px}
@media(max-width:760px){
  .rail{inset:auto 0 0 0;width:auto;height:58px;flex-direction:row;padding:7px 12px;border-right:0;border-top:1px solid rgba(242,238,230,.18)}
  .rail-logo,.rail-avatar,.rail-foot{display:none}.rail-nav{margin:0;width:100%;flex-direction:row;justify-content:space-around;gap:6px}.rail-nav a{width:48px;height:42px}
  .page{margin:0;padding:10px 12px 82px}.mast{height:430px;grid-template-columns:1fr;grid-template-rows:190px 240px}.mast-copy{padding:22px}.mast-copy::after{width:58px;height:52px;right:14px;top:14px}
  .mast-visual img{object-position:50% 30%}.wordmark{font-size:72px}.motto{font-size:16px}.topline{height:30px}.metric:first-child{grid-column:1/-1}
}
"""


def render(state: dict) -> str:
    S = state
    fr = S["freshness"]
    cap = S["capital"]
    edge = S["edge"]
    hero = asset_uri("nabu-command.webp")
    avatar = asset_uri("nabu-portrait.webp")
    o: list[str] = []
    a = o.append

    a("<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\">")
    a("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    a(f"<title>N*ABU · planche de lecture · {e(S['built_iso'])}</title>")
    a("<meta name=\"color-scheme\" content=\"light\">")
    a("<style>" + CSS + "</style></head><body>")
    a("<div class=\"grain\"></div><div class=\"fibers\"></div>"
      "<aside class=\"rail\" aria-label=\"Navigation principale\">"
      "<div class=\"rail-logo\">N*ABU</div>")
    if avatar:
        a(f"<img class=\"rail-avatar\" src=\"{avatar}\" alt=\"N*ABU\">")
    a("<nav class=\"rail-nav\">"
      "<a href=\"#portfolio\" aria-label=\"Portefeuille\">PF</a>"
      "<a href=\"#risk\" aria-label=\"Risques\">RK</a>"
      "<a href=\"#positions\" aria-label=\"Positions\">PX</a>"
      "<a href=\"#analysis\" aria-label=\"Analyse\">AN</a>"
      "</nav><div class=\"rail-foot\">READ ONLY / MARK 03</div></aside><main class=\"page\">")
    a("<div class=\"topline\"><span><strong>NOUS / N*ABU</strong> · Agent telemetry</span>"
      "<span>Sacred technology · Evidence before assertion</span>"
      "<span class=\"issue\"><i class=\"iii-icon\" aria-label=\"symbole iii\"></i> / 2026</span></div>")

    # -- masthead incarné
    a("<header class=\"mast\"><div class=\"mast-copy\"><div>"
      "<div class=\"agent-id\"><i></i>Autonomous portfolio intelligence</div>"
      "<div class=\"wordmark\">N*ABU</div><div class=\"tagline\">Trade · Analyse · Execute</div></div>"
      "<p class=\"motto\">Le signal avant le bruit.<br>La preuve avant le récit.</p></div>"
      "<div class=\"mast-visual\">")
    if hero:
        a(f"<img src=\"{hero}\" alt=\"Portrait de N*ABU devant ses écrans de marché\">")
    a("<div class=\"mast-meta\">")
    a(f"<span class=\"badge badge--mode\">{e(str(S['mode']).upper())}</span>")
    if S.get("demo"):
        a("<span class=\"badge badge--demo\">Données de démonstration</span>")
    a("<span class=\"badge\">Read only</span></div></div></header>")

    # -- kill
    k = S["kill"]
    if k.get("active"):
        a("<section class=\"kill\"><h2>KILL ACTIF</h2>")
        a(f"<p>{e(k.get('reason'))}</p>")
        a(f"<p style=\"margin-top:6px\">Armé depuis {e(k.get('since_iso'))} · "
          "nouvelles prises de risque bloquées · sorties autorisées · JD requis.</p></section>")

    if fr["status"] == "ok":
        v = ("Données à jour. Le portefeuille peut être suivi depuis cette page.")
    elif fr["status"] == "watch":
        v = ("Le dernier mark vieillit : les positions restent lisibles, mais leur uPnL doit être "
             "confirmé avant toute décision.")
    elif fr["status"] == "hot":
        v = ("Données périmées. Vérifier book-sync et wrap_mtm.py avant de lire le portefeuille.")
    else:
        v = ("Fraîcheur indéterminée : une source manque. Aucun chiffre ne doit déclencher d'action.")

    # -- cockpit essentiel
    positions = S["positions"]
    upnl = sum(float(p.get("unrealized_pnl_usd") or 0) for p in positions)
    max_gate = max(S["gates"], key=lambda x: x["util_pct"], default=None)
    risk_txt = f"{max_gate['util_pct']:.0f}{NB}%" if max_gate else "—"
    risk_note = max_gate["label"] if max_gate else "limites indisponibles"
    health_label = {"ok": "À jour", "watch": "À surveiller", "hot": "Périmé",
                    "unknown": "Inconnu"}.get(fr["status"], fr["status"])
    p = cap.get("paper") or {}
    realized = float(p.get("realized_pnl_usd") or 0)
    a("<section class=\"overview\" id=\"portfolio\" aria-label=\"Synthèse du portefeuille\">")
    a(f"<div class=\"overview-head\"><div class=\"overview-title\">Portefeuille · maintenant</div>"
      f"<span class=\"health health--{e(fr['status'])}\">{e(health_label)}</span></div>")
    a("<div class=\"overview-grid\">")
    metrics = [
        ("Equity", money(cap["equity_usd"]), f"pic {money(cap['peak_usd'], 0)}", False),
        ("Aujourd'hui", f"{cap['day_pnl_pct']:+.2f}{NB}%", "performance UTC", cap["day_pnl_pct"] < 0),
        ("uPnL ouvert", money(upnl), f"{len(positions)} position{'s' if len(positions) != 1 else ''}", upnl < 0),
        ("PnL réalisé", money(realized), "net des clôtures", realized < 0),
        ("Risque max", risk_txt, risk_note, bool(max_gate and max_gate["status"] in ("hot", "breach"))),
    ]
    for kk, vv, nn, bad in metrics:
        a(f"<div class=\"metric\"><div class=\"metric-k\">{e(kk)}</div>"
          f"<div class=\"metric-v{' neg' if bad else ''}\">{e(vv)}</div>"
          f"<div class=\"metric-n\">{e(nn)}</div></div>")
    a(f"</div><div class=\"decision\"><b>Lecture</b><span>{e(v)}</span></div></section>")

    # -- boucle d'auto-évaluation, lisible par l'humain et reflétée dans #nabu-state
    se = S.get("self_eval") or {}
    if se:
        labels = {"data": "Données", "risk": "Risque", "discipline": "Discipline", "edge": "Edge"}
        maxima = {"data": 20, "risk": 25, "discipline": 20, "edge": 35}
        a("<section class=\"self-eval\" id=\"self-eval\" aria-label=\"Auto-évaluation de N*ABU\">")
        a(f"<div class=\"eval-verdict\"><div><div class=\"eval-k\">Self-evaluation / cycle</div>"
          f"<div class=\"eval-status eval-status--{e(se['verdict'])}\">{e(se['verdict'])}</div></div>"
          f"<div class=\"eval-score\">{int(se['score'])}<small>/100</small></div></div>")
        a("<div class=\"eval-axes\"><div class=\"eval-k\" style=\"color:var(--ink-soft)\">Qualité du système</div>")
        for key in ("data", "risk", "discipline", "edge"):
            val = int(se["scores"].get(key, 0))
            pct_axis = val / maxima[key] * 100
            a(f"<div class=\"axis\"><span>{e(labels[key])}</span>"
              f"<span class=\"axis-track\"><i style=\"--w:{pct_axis:.1f}%\"></i></span>"
              f"<b>{val}</b></div>")
        a("</div>")
        review = se.get("review") or {}
        a(f"<div class=\"eval-action\"><div><strong>Prochaine amélioration</strong>"
          f"<p>{e(se.get('next_action') or 'Observer sans modifier.')}</p></div>"
          f"<div class=\"eval-meta\">Confiance {e(se.get('confidence'))} · prochaine revue après "
          f"{e(review.get('next_review_after_closes', '—'))} clôtures · limites de risque immuables</div></div></section>")

    # -- attestation technique, disponible sans encombrer la lecture
    open_attr = " open" if fr["status"] in ("hot", "unknown") else ""
    a(f"<details class=\"drawer\"{open_attr}><summary>Fraîcheur et attestation des données</summary>"
      "<div class=\"drawer-body\"><section class=\"attest\"><div class=\"eyebrow\">Attestation — invariant #2</div>")
    a("<div class=\"rule rule--thick\"></div>")
    a(f"<p class=\"attest-line\">{e(S['attestation'])}</p>")
    a("<div class=\"clocks\">")
    a(f"<div class=\"clock clock--{e(fr['sync_status'])}\"><span class=\"clock-k\">Âge du sync</span>"
      f"<span class=\"clock-v\">{e(dur(fr['sync_age_s']))}</span>"
      "<span class=\"clock-n\">book.json · book-sync toutes les 5 min</span></div>")
    a(f"<div class=\"clock clock--{e(fr['mark_status'])}\"><span class=\"clock-k\">Âge du mark</span>"
      f"<span class=\"clock-v\">{e(dur(fr['mark_age_s']))}</span>"
      f"<span class=\"clock-n\">paper/account.json · wrap_mtm.py toutes les heures<br>"
      f"{e(fr.get('mark_note') or '')}</span></div>")
    a("</div>")

    a(f"<p class=\"verdict\">{e(v)}</p></section></div></details>")

    # -- grand livre du capital (secondaire)
    a("<details class=\"drawer\"><summary>Détail du capital et du compte</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Capital</div><div class=\"rule\"></div>")
    a("<div class=\"plate\"><div class=\"cap-grid\">")
    a(f"<div><div class=\"cap-k\">Equity</div><div class=\"cap-eq\">{e(money(cap['equity_usd']))}</div></div>")
    dd_cls = " neg" if cap["dd_pct"] > 0 else ""
    a(f"<div><div class=\"cap-k\">Drawdown / pic</div>"
      f"<div class=\"cap-v{dd_cls}\">{cap['dd_pct']:.2f}{NB}%</div>"
      f"<div class=\"note\">pic {e(money(cap['peak_usd'], 0))}</div></div>")
    dcls = " neg" if cap["day_pnl_pct"] < 0 else ""
    wcls = " neg" if cap["week_pnl_pct"] < 0 else ""
    a(f"<div><div class=\"cap-k\">Jour / semaine</div>"
      f"<div class=\"cap-v{dcls}\">{cap['day_pnl_pct']:+.2f}{NB}%</div>"
      f"<div class=\"cap-v{wcls}\" style=\"font-size:16px;opacity:.85\">{cap['week_pnl_pct']:+.2f}{NB}% · 7{NB}j</div></div>")
    a("</div>")
    p = cap.get("paper")
    if p:
        a("<div class=\"cap-strip\">")
        for kk, vv in [("Cash", money(p["cash_usd"])), ("PnL réalisé", money(p["realized_pnl_usd"])),
                       ("Frais cumulés", money(p["fees_paid_usd"])),
                       ("Funding cumulé", money(p["funding_paid_usd"], 4)),
                       ("Trades clos", str(p["closed_trades"])),
                       ("Capital initial", money(p["start_equity_usd"], 0))]:
            a(f"<span>{e(kk)} <b>{e(vv)}</b></span>")
        a("</div>")
    a("</div></section></div></details>")

    # -- risques essentiels puis planche complète repliable
    a("<section class=\"sec primary-section\" id=\"risk\"><div class=\"eyebrow\">Risques à surveiller</div><div class=\"rule\"></div>")
    if S["gates"]:
        essentials = sorted(S["gates"], key=lambda x: x["util_pct"], reverse=True)[:4]
        a("<div class=\"risk-strip\">")
        for g in essentials:
            w = min(100.0, g["util_pct"])
            a(f"<div class=\"risk-card risk-card--{e(g['status'])}\">"
              f"<div class=\"risk-card-head\"><span>{e(g['label'])}</span><b>{g['util_pct']:.0f}%</b></div>"
              f"<div class=\"risk-meter\"><span style=\"--w:{w:.1f}%\"></span></div>"
              f"<div class=\"risk-value\">{e(g['value_txt'])} / {e(g['limit_txt'])}</div></div>")
        a("</div>")
    else:
        a("<p class=\"empty\">Limites indisponibles.</p>")
    a("</section>")
    a("<details class=\"drawer\"><summary>Toutes les limites de risque</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Planche complète des limites</div>")
    a("<div class=\"rule\"></div>")
    if S["gates"]:
        a("<div class=\"wedge\">")
        for i, g in enumerate(S["gates"]):
            h = min(100.0, g["util_pct"])
            a(f"<div class=\"step step--{e(g['status'])}\" title=\"{e(g['note'])}\">")
            a(f"<div class=\"step-pct\">{g['util_pct']:.0f}%</div>")
            a(f"<div class=\"track\"><div class=\"fill\" style=\"--h:{h:.1f}%;"
              f"animation-delay:{i * 55}ms\"></div></div>")
            a(f"<div class=\"step-l\">{e(g['label'])}</div>")
            a(f"<div class=\"step-v\">{e(g['value_txt'])}</div>")
            a(f"<div class=\"step-lim\">/ {e(g['limit_txt'])}</div></div>")
        a("</div>")
        a("<div class=\"legend\">"
          "<span><i class=\"dot dot--ok\"></i>sous 60 %</span>"
          "<span><i class=\"dot dot--watch\"></i>60–85 % · surveiller</span>"
          "<span><i class=\"dot dot--hot\"></i>au-delà de 85 % · le gate va refuser</span>"
          "<span>hachuré · limite franchie</span></div>")
        a("<p class=\"note\" style=\"margin-top:10px\">Chaque barre est la part de la limite "
          "consommée, pas la valeur brute. Une barre pleine ne raconte pas une perte : elle "
          "annonce un refus du gate. Les limites viennent de <b>risk.yaml</b>, jamais d'ici.</p>")
    else:
        a("<p class=\"empty\">risk.yaml illisible — aucune limite à afficher. "
          "Une planche vide vaut mieux qu'une planche inventée.</p>")
    a("</section></div></details>")

    # -- positions
    a("<section class=\"sec primary-section\" id=\"positions\"><div class=\"eyebrow\">Positions ouvertes</div><div class=\"rule\"></div>")
    if S["positions"]:
        a("<div class=\"wrap\"><table class=\"tbl\"><thead><tr>"
          "<th>Venue</th><th>Sym</th><th>Sens</th><th class=\"num\">Notionnel</th>"
          "<th class=\"num\">Entrée</th><th class=\"num\">Stop</th><th class=\"num\">uPnL</th>"
          "<th class=\"num\">Funding</th><th class=\"num\">Portage</th><th class=\"num\">Mark</th>"
          "</tr></thead><tbody>")
        for pos in S["positions"]:
            up = pos["unrealized_pnl_usd"]
            sd = f"{pos['stop_dist_pct']:.2f}{NB}%" if pos.get("stop_dist_pct") else "—"
            a("<tr>"
              f"<td>{e(pos['venue'])}</td><td><b>{e(pos['symbol'])}</b></td>"
              f"<td><span class=\"side side--{e(pos['side'])}\">{e(pos['side'])}</span></td>"
              f"<td class=\"num\">{e(money(pos['notional_usd'], 0))}</td>"
              f"<td class=\"num\">{pos['entry_px']:,.2f}</td>"
              f"<td class=\"num\">{pos['stop_px']:,.2f}<br><span class=\"step-lim\">{e(sd)}</span></td>"
              f"<td class=\"num{' neg' if up < 0 else ''}\">{up:+.2f}{NB}$</td>"
              f"<td class=\"num\">{pos['funding_paid_usd']:+.3f}{NB}$</td>"
              f"<td class=\"num\">{(pos['hold_h'] or 0):.1f}{NB}h</td>"
              f"<td class=\"num\">{e(dur(pos.get('mark_age_s')))}</td></tr>")
        a("</tbody></table></div>")
        for pos in S["positions"]:
            if pos.get("thesis") or pos.get("invalidation"):
                a(f"<p class=\"thesis\"><b>{e(pos['symbol'])}</b> — thèse : {e(pos['thesis'] or '—')}"
                  f"<br>Invalidation : {e(pos['invalidation'] or '—')}</p>")
    else:
        a("<p class=\"empty\">Flat. C'est une position, pas une absence de position.</p>")
    a("</section>")

    # -- edge
    a("<details class=\"drawer\" id=\"analysis\"><summary>Performance statistique de N*ABU</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Edge — mesuré, jamais supposé</div>")
    a("<div class=\"rule\"></div>")
    n, tgt = edge["n_closes"], edge["target_trades"]
    if edge["verified"]:
        a(f"<span class=\"stamp stamp--ok\">Verified · {n} trades clos</span>")
    else:
        a(f"<span class=\"stamp\">Unverified · {n} / {tgt} trades clos</span>")
    pct = min(100.0, n / tgt * 100 if tgt else 0)
    a(f"<div class=\"progress\"><span style=\"--w:{pct:.1f}%\"></span></div>")
    a(f"<p class=\"note\" style=\"margin-top:8px\">Sortie du gate G0 : {tgt} trades journalisés. "
      "En dessous, ces chiffres décrivent un échantillon, pas un edge — l'intervalle le dit "
      "mieux que la moyenne.</p>")

    if n:
        exp = edge["expectancy_r"]
        ci = edge["ci95"]
        ci_txt = f"IC95 {ci[0]:+.2f} … {ci[1]:+.2f}" if ci else "IC95 indisponible"
        cells = [
            ("Espérance", f"{exp:+.2f}{NB}R" if exp is not None else "—", ci_txt,
             exp is not None and exp < 0),
            ("Taux de gain", f"{edge['win_rate_pct']:.0f}{NB}%" if edge["win_rate_pct"] is not None else "—",
             "métrique de vanité — lue après l'espérance", False),
            ("Coûts / gain brut",
             f"{edge['cost_ratio_pct']:.1f}{NB}%" if edge["cost_ratio_pct"] is not None else "—",
             f"frais {money(edge['fees_usd'])} · funding {money(edge['funding_usd'], 4)}",
             (edge["cost_ratio_pct"] or 0) > 30),
            ("Sorties au stop",
             f"{edge['stop_share_pct']:.0f}{NB}%" if edge["stop_share_pct"] is not None else "—",
             "part des clôtures déclenchées par le stop", False),
            ("Portage médian",
             f"{edge['median_hold_h']:.1f}{NB}h" if edge["median_hold_h"] is not None else "—",
             "durée de vie d'une thèse", False),
            ("Plan écrit",
             f"{edge['plan_written_pct']:.0f}{NB}%" if edge["plan_written_pct"] is not None else "—",
             "thèse ET invalidation présentes à l'ouverture",
             (edge["plan_written_pct"] or 100) < 90),
        ]
        a("<div class=\"edge-grid\">")
        for kk, vv, nn, bad in cells:
            a(f"<div class=\"cell\"><div class=\"cell-k\">{e(kk)}</div>"
              f"<div class=\"cell-v{' neg' if bad else ''}\">{e(vv)}</div>"
              f"<div class=\"cell-n\">{e(nn)}</div></div>")
        a("</div>")

        hist = edge.get("r_histogram") or []
        if hist:
            mx = max([h["n"] for h in hist] + [1])
            a("<div class=\"hist\">")
            for i, h in enumerate(hist):
                hh = h["n"] / mx * 100
                cls = " hbar--neg" if i < 3 else ""
                a(f"<div class=\"hbar{cls}\" style=\"height:{hh:.1f}%\" title=\"{h['n']} trades\"></div>")
            a("</div><div class=\"hlab\">")
            for h in hist:
                a(f"<div>{e(h['label'])}<br>{h['n']}</div>")
            a("</div>")
            a("<p class=\"note\" style=\"margin-top:10px\">Distribution des R. "
              "Une espérance portée par une seule barre à droite n'est pas un edge, "
              "c'est un trade.</p>")
    else:
        a("<p class=\"empty\">Aucun trade clos non-artefact dans le journal. Rien à mesurer, "
          "rien à inventer.</p>")
    a("</section></div></details>")

    # -- contexte live
    ctx = (S["market"].get("context") or {}).get("coins") or {}
    a("<details class=\"drawer\"><summary>Contexte de marché</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Contexte live — lu avant toute décision</div>")
    a("<div class=\"rule\"></div>")
    if ctx:
        a("<div class=\"wrap\"><table class=\"tbl\"><thead><tr><th>Actif</th>"
          "<th class=\"num\">Mid</th><th class=\"num\">vs MA20</th><th class=\"num\">Range 50 h</th>"
          "<th class=\"num\">Funding 8 h</th><th>Lecture daily</th></tr></thead><tbody>")
        for coin, d in ctx.items():
            dev = float(d.get("dev_pct") or 0)
            f8 = float(d.get("funding_bps_8h") or 0)
            a(f"<tr><td><b>{e(coin)}</b></td>"
              f"<td class=\"num\">{float(d.get('mid') or 0):,.2f}</td>"
              f"<td class=\"num{' neg' if dev < 0 else ''}\">{dev:+.2f}{NB}%</td>"
              f"<td class=\"num\">{float(d.get('range_lo_50h') or 0):,.2f} – "
              f"{float(d.get('range_hi_50h') or 0):,.2f}</td>"
              f"<td class=\"num\">{f8:+.3f}{NB}bps</td>"
              f"<td>{e(d.get('daily_lecture') or '—')}</td></tr>")
        a("</tbody></table></div>")
    else:
        a("<p class=\"empty\">live_context.json absent. Invariant opérationnel n°1 : "
          "pas de trade sans lecture du contexte live.</p>")
    a("</section></div></details>")

    # -- signaux
    sig = S["market"].get("signals") or []
    a("<details class=\"drawer\"><summary>Opportunités détectées</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Signaux du dernier scan</div>")
    a("<div class=\"rule\"></div>")
    if sig:
        perps = [x for x in sig if x.get("class") == "perp"]
        autres = [x for x in sig if x.get("class") != "perp"]
        if perps:
            a("<div class=\"wrap\"><table class=\"tbl\"><thead><tr><th>Actif</th><th>Sens</th>"
              "<th class=\"num\">Prix</th><th class=\"num\">Funding</th><th class=\"num\">OI</th>"
              "<th>Conviction</th><th>Thèse / invalidation</th></tr></thead><tbody>")
            for x in perps:
                cv = str(x.get("conviction", "")).replace("é", "e")
                a(f"<tr><td><b>{e(x.get('asset'))}</b></td>"
                  f"<td><span class=\"side side--{e(x.get('side'))}\">{e(x.get('side'))}</span></td>"
                  f"<td class=\"num\">{float(x.get('price') or 0):,.2f}</td>"
                  f"<td class=\"num\">{float(x.get('funding_bps') or 0):+.3f}{NB}bps</td>"
                  f"<td class=\"num\">{float(x.get('oi_usd') or 0) / 1e6:,.0f}{NB}M$</td>"
                  f"<td><span class=\"conv conv--{e(cv)}\">{e(x.get('conviction'))}</span></td>"
                  f"<td>{e(x.get('thesis') or '—')}<br>"
                  f"<span class=\"step-lim\">inval. {e(x.get('invalidation') or '—')}</span></td></tr>")
            a("</tbody></table></div>")
        if autres:
            a("<div class=\"wrap\" style=\"margin-top:22px\"><table class=\"tbl\"><thead><tr>"
              "<th>Actif</th><th>Classe</th><th class=\"num\">Prix</th><th>Note</th>"
              "</tr></thead><tbody>")
            for x in autres:
                px = float(x.get("price") or 0)
                pxs = f"{px:,.6f}" if px < 1 else f"{px:,.2f}"
                a(f"<tr><td><b>{e(x.get('asset'))}</b></td><td>{e(x.get('class'))}</td>"
                  f"<td class=\"num\">{e(pxs)}</td><td>{e(x.get('thesis') or '—')}</td></tr>")
            a("</tbody></table></div>")
        a("<p class=\"note\" style=\"margin-top:10px\">Un signal n'est pas un ordre. Le gate reste "
          "seul juge, et refuse encore après cette page.</p>")
    else:
        a("<p class=\"empty\">Aucun signal dans le dernier scan — ou scan_latest.json absent. "
          "Flat par défaut, le silence n'est pas un bug.</p>")
    a("</section></div></details>")

    # -- avertissements
    if S.get("warnings"):
        a("<section class=\"sec\"><div class=\"eyebrow\">Avertissements du book</div>")
        a("<div class=\"rule\"></div><ul class=\"note\" style=\"margin:0;padding-left:18px\">")
        for w in S["warnings"]:
            a(f"<li>{e(w)}</li>")
        a("</ul></section>")

    # -- provenance
    a("<details class=\"drawer\"><summary>Audit et provenance des sources</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Provenance — Verify nommé par source</div>")
    a("<div class=\"rule\"></div><div class=\"wrap\"><table class=\"tbl prov\"><thead><tr>"
      "<th>Source</th><th>État</th><th>Âge fichier</th><th>Note</th><th>Chemin</th>"
      "</tr></thead><tbody>")
    for pr in S["provenance"]:
        a(f"<tr><td>{e(pr['source'])}</td>"
          f"<td><span class=\"vs vs--{e(pr['state'])}\">{e(pr['state'])}</span></td>"
          f"<td>{e(dur(pr.get('age_s')))}</td><td>{e(pr['note'])}</td>"
          f"<td class=\"path\">{e(pr['path'])}</td></tr>")
    a("</tbody></table></div></section></div></details>")

    a("<footer><b>Lecture seule.</b> Cette page n'ouvre, ne ferme et ne modifie rien. "
      "Elle ne charge aucun secret et ne joint aucun venue.<br>"
      "En cas de conflit avec ce qui est affiché ici, <b>book.json gagne</b> — cette planche "
      "n'est qu'un tirage, pas la source.<br>"
      f"Tirée le {e(S['built_iso'])} par <b>nabu_dashboard.py</b> · "
      "limites lues dans risk.yaml, jamais réécrites.</footer>")

    a("</main>")
    a("<script type=\"application/json\" id=\"nabu-state\">")
    a(html.escape(json.dumps(S, ensure_ascii=False, default=str), quote=False))
    a("</script></body></html>")
    return "\n".join(o)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(prog="nabu_dashboard",
                                 description="Planche de lecture N*ABU (lecture seule).")
    ap.add_argument("cmd", choices=["build", "json", "demo"])
    ap.add_argument("--out", default=str(LIVE / "dashboard.html"))
    a = ap.parse_args()

    state, _ = build_state(demo=(a.cmd == "demo"))

    if a.cmd == "json":
        print(json.dumps(state, ensure_ascii=False, indent=2, default=str))
        return 0

    out = Path(os.path.expanduser(a.out))
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(render(state), encoding="utf-8")
    tmp.replace(out)                 # atomique : jamais de page à moitié écrite

    fr = state["freshness"]
    print(f"DASHBOARD · {out} · mode {state['mode']} · "
          f"sync {dur(fr['sync_age_s'])} · mark {dur(fr['mark_age_s'])} · "
          f"fraîcheur {fr['status'].upper()}")
    bad = [p for p in state["provenance"] if p["state"] != "VERIFIED"]
    for p in bad:
        print(f"  ! {p['state']} · {p['source']} · {p['note']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
