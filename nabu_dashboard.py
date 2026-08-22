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
"""
from __future__ import annotations

import argparse
import base64
import html
import json
import math
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path

LIVE = Path(os.environ.get("NABU_LIVE_ROOT", "/opt/data/.nabu"))
P_RISK = Path(os.environ.get("NABU_RISK_CONFIG", str(LIVE / "risk.yaml")))
P_BOOK = LIVE / "book.json"
P_ACCOUNT = LIVE / "paper" / "account.json"
P_JOURNAL = LIVE / "journal.jsonl"
P_KILL = LIVE / "KILL"
P_CTX = LIVE / "live_context.json"
P_SCAN = LIVE / "data" / "scan_latest.json"
P_HIST = LIVE / "data" / "equity_history.jsonl"
P_MILESTONE = LIVE / "bin" / "nabu_milestone.py"

TARGET_TRADES = 30
SYNC_WATCH_S, SYNC_HOT_S = 15 * 60, 60 * 60
MARK_WATCH_S, MARK_HOT_S = 90 * 60, 150 * 60


class Source:
    def __init__(self, name: str, path: Path):
        self.name, self.path = name, path
        self.state, self.note, self.mtime = "UNVERIFIED", "fichier absent", None
        self.data = None

    def as_dict(self) -> dict:
        return {
            "source": self.name, "path": str(self.path),
            "state": self.state, "note": self.note,
            "age_s": (time.time() - self.mtime) if self.mtime else None,
        }


def read_json(name: str, path: Path) -> Source:
    s = Source(name, path)
    try:
        if not path.exists():
            return s
        s.data = json.loads(path.read_text(encoding="utf-8"))
        s.state, s.mtime = "VERIFIED", path.stat().st_mtime
    except Exception as e:
        s.state, s.note = "FAILED", str(e)[:120]
    return s


def read_jsonl(name: str, path: Path) -> Source:
    s = Source(name, path)
    try:
        if not path.exists():
            return s
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        s.data = []
        for line in lines:
            line = line.strip()
            if line:
                try:
                    s.data.append(json.loads(line))
                except Exception:
                    continue
        s.state, s.mtime = "VERIFIED", path.stat().st_mtime
        s.note = f"{len(s.data)} enregistrements"
    except Exception as e:
        s.state, s.note = "FAILED", str(e)[:120]
    return s


def read_risk(path: Path) -> Source:
    s = Source("risk.yaml", path)
    try:
        if not path.exists():
            return s
        import yaml
        with path.open("r", encoding="utf-8") as f:
            s.data = yaml.safe_load(f)
        s.state, s.mtime = "VERIFIED", path.stat().st_mtime
        s.note = "limites chargées"
    except Exception as e:
        s.state, s.note = "FAILED", str(e)[:120]
    return s


def read_kill(path: Path) -> dict:
    try:
        if not path.exists():
            return {"active": False, "reason": None, "since_iso": None}
        data = json.loads(path.read_text(encoding="utf-8"))
        return {
            "active": bool(data.get("active")),
            "reason": data.get("reason"),
            "since_iso": data.get("since"),
        }
    except Exception:
        return {"active": False, "reason": None, "since_iso": None}


def _is_artifact(rec: dict) -> bool:
    return bool(rec.get("exclude_from_edge") or rec.get("phantom") or rec.get("test_trade"))


def compute_capital(book: dict) -> dict:
    eq = float(book.get("equity_usd") or 0)
    peak = float(book.get("equity_peak_usd") or eq)
    day_open = float(book.get("equity_day_open_usd") or eq)
    week_open = float(book.get("equity_week_open_usd") or eq)
    return {
        "equity_usd": eq,
        "peak_usd": peak,
        "day_open_usd": day_open,
        "week_open_usd": week_open,
        "dd_pct": (peak - eq) / peak * 100 if peak > 0 else 0,
        "day_pnl_pct": (eq - day_open) / day_open * 100 if day_open > 0 else 0,
        "week_pnl_pct": (eq - week_open) / week_open * 100 if week_open > 0 else 0,
    }


def compute_freshness(book: dict, account: dict | None, n_positions: int) -> dict:
    now = time.time()
    sync_ts = float(book.get("synced_at") or 0)
    sync_age = now - sync_ts if sync_ts > 0 else None
    sync_note = "book.json · book-sync toutes les 5 min"

    marks = []
    if account:
        for p in account.get("positions", []):
            lm = float(p.get("last_mark_ts", 0) or 0)
            if lm > 0:
                marks.append(lm)

    if marks:
        mark_age = now - min(marks)
        mark_note = "position la plus mal marquée"
    elif n_positions > 0:
        mark_age = None
        mark_note = "aucune position jamais marquée — wrap_mtm.py n'a pas tourné"
    else:
        mark_age = None
        mark_note = "pas de position"

    def rank(age, watch, hot):
        if age is None:
            return "unknown"
        if age >= hot:
            return "hot"
        if age >= watch:
            return "watch"
        return "ok"

    sync_status = rank(sync_age, SYNC_WATCH_S, SYNC_HOT_S)
    mark_status = rank(mark_age, MARK_WATCH_S, MARK_HOT_S)

    status = "ok"
    if "hot" in (sync_status, mark_status):
        status = "hot"
    elif "watch" in (sync_status, mark_status):
        status = "watch"
    elif "unknown" in (sync_status, mark_status):
        status = "unknown"

    return {
        "sync_age_s": sync_age,
        "sync_status": sync_status,
        "sync_note": sync_note,
        "mark_age_s": mark_age,
        "mark_status": mark_status,
        "mark_note": mark_note,
        "status": status,
    }


def _gate(key, label, value_txt, limit_txt, util_pct, note=""):
    return {
        "key": key, "label": label,
        "value_txt": value_txt, "limit_txt": limit_txt,
        "util_pct": util_pct, "note": note,
        "status": "breach" if util_pct >= 100 else "hot" if util_pct >= 85 else "watch" if util_pct >= 60 else "ok",
    }


def compute_gates(cfg: dict, cap: dict, positions: list, journal: list) -> list[dict]:
    max_dd = float((cfg.get("risk", {}) or {}).get("max_drawdown_pct", 20))
    day_lim = float((cfg.get("risk", {}) or {}).get("max_day_loss_pct", 4))
    week_lim = float((cfg.get("risk", {}) or {}).get("max_week_loss_pct", 8))
    floor = float((cfg.get("risk", {}) or {}).get("equity_floor_usd", 300))
    max_pos = int((cfg.get("risk", {}) or {}).get("max_positions", 4))
    max_gross = float((cfg.get("risk", {}) or {}).get("max_gross_exposure_pct", 150))
    max_net = float((cfg.get("risk", {}) or {}).get("max_net_exposure_pct", 100))
    max_oh = float((cfg.get("risk", {}) or {}).get("max_orders_per_hour", 8))
    max_od = float((cfg.get("risk", {}) or {}).get("max_orders_per_day", 30))
    streak_lim = int((cfg.get("risk", {}) or {}).get("max_loss_streak", 3))
    cd_h = float((cfg.get("risk", {}) or {}).get("cooldown_hours", 6))

    long_usd = sum(float(p.get("notional_usd") or 0) for p in positions if p.get("side") == "long")
    short_usd = sum(float(p.get("notional_usd") or 0) for p in positions if p.get("side") == "short")
    gross_usd = long_usd + short_usd
    net_usd = abs(long_usd - short_usd)
    eq = cap["equity_usd"]
    gross_pct = gross_usd / eq * 100 if eq > 0 else 0
    net_pct = net_usd / eq * 100 if eq > 0 else 0

    now = time.time()
    o1h = 0
    o24 = 0
    for r in journal:
        ts_raw = r.get("ts", 0)
        try:
            ts = float(ts_raw)
        except (ValueError, TypeError):
            try:
                from datetime import datetime, timezone
                dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                ts = dt.timestamp()
            except Exception:
                ts = 0
        if ts > now - 3600:
            o1h += 1
        if ts > now - 86400:
            o24 += 1

    streak = 0
    last_loss = 0.0
    fills = [r for r in journal if r.get("event") == "fill" and r.get("kind") == "close"]
    for r in fills:
        pnl = float(r.get("realized_pnl_usd") or 0)
        ts = float(r.get("ts") or 0)
        if pnl < 0:
            streak += 1
            last_loss = max(last_loss, ts)
        else:
            break

    gates = [
        _gate("dd", "Drawdown", f"{cap['dd_pct']:.2f} %", f"{max_dd:.0f} %",
              cap['dd_pct'] / max_dd * 100 if max_dd else 0,
              "depuis le pic d'equity · au-delà : KILL global"),
        _gate("day", "Perte jour", f"{cap['day_pnl_pct']:+.2f} %", f"−{day_lim:.0f} %",
              0 if cap['day_pnl_pct'] >= 0 else abs(cap['day_pnl_pct']) / day_lim * 100,
              "journée UTC"),
        _gate("week", "Perte semaine", f"{cap['week_pnl_pct']:+.2f} %", f"−{week_lim:.0f} %",
              0 if cap['week_pnl_pct'] >= 0 else abs(cap['week_pnl_pct']) / week_lim * 100,
              "semaine glissante"),
        _gate("floor", "Plancher equity", f"{eq:.0f} $", f"{floor:.0f} $",
              0 if eq >= floor else 100,
              "sous le plancher : sorties seulement"),
        _gate("pos", "Positions", f"{len(positions)}", f"{max_pos:.0f}",
              len(positions) / max_pos * 100 if max_pos else 0,
              "concurrentes, tous venues"),
        _gate("gross", "Expo brute", f"{gross_pct:.0f} %", f"{max_gross:.0f} %",
              gross_pct / max_gross * 100 if max_gross else 0,
              "somme des notionnels / equity"),
        _gate("net", "Expo nette", f"{net_pct:.0f} %", f"{max_net:.0f} %",
              net_pct / max_net * 100 if max_net else 0,
              "|long − short| / equity"),
        _gate("oh", "Ordres / h", f"{o1h}", f"{max_oh:.0f}",
              o1h / max_oh * 100 if max_oh else 0,
              "anti-boucle folle"),
        _gate("od", "Ordres / j", f"{o24}", f"{max_od:.0f}",
              o24 / max_od * 100 if max_od else 0,
              "anti-overtrading"),
        _gate("streak", "Série pertes", f"{streak}", f"{streak_lim:.0f}",
              streak / streak_lim * 100 if streak_lim else 0,
              "au-delà : cooldown imposé"),
    ]

    if streak >= streak_lim and last_loss:
        left_h = max(0.0, cd_h - (now - last_loss) / 3600)
        gates.append(_gate("cooldown", "Cooldown", f"{left_h:.1f} h restantes",
                           f"{cd_h:.0f} h", 100 if left_h > 0 else 0,
                           "ouvertures bloquées"))
    else:
        gates.append(_gate("cooldown", "Cooldown", "inactif", f"{cd_h:.0f} h", 0,
                           "s'arme après la série"))

    return gates


def compute_milestone_state() -> dict:
    try:
        p = subprocess.run(
            [sys.executable, str(P_MILESTONE), "json"],
            capture_output=True, text=True, timeout=30
        )
        if p.returncode == 0:
            return json.loads(p.stdout)
    except Exception:
        pass
    return {"current": 0, "crossed": False, "n_closes": 0, "target": TARGET_TRADES}


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
        if len(rs) >= 4:
            k = min(10, len(rs))
            out["expectancy_r_recent"] = statistics.fmean(rs[-k:])
            out["recent_window"] = k
    return out


def compute_recent_closes(journal: list, limit: int = 8) -> list[dict]:
    closes = [r for r in journal if r.get("event") == "fill"
              and r.get("kind") == "close" and not _is_artifact(r)]
    out = []
    for r in closes[-limit:][::-1]:
        out.append({
            "ts": r.get("ts"), "iso": r.get("iso"),
            "symbol": r.get("symbol"), "side": r.get("side"),
            "r_multiple": r.get("r_multiple"),
            "realized_pnl_usd": r.get("realized_pnl_usd"),
            "fees_usd": r.get("fees_usd"), "hold_hours": r.get("hold_hours"),
            "reason": r.get("reason"), "thesis": r.get("thesis") or "",
        })
    return out


def append_history(state: dict) -> None:
    try:
        cap, se = state.get("capital") or {}, state.get("self_eval") or {}
        rec = {
            "ts": round(state.get("built_ts") or time.time(), 1),
            "sync_ts": None, "equity": round(float(cap.get("equity_usd") or 0), 2),
            "dd_pct": round(float(cap.get("dd_pct") or 0), 3),
            "score": se.get("score"), "verdict": se.get("verdict"),
            "n_closes": (state.get("edge") or {}).get("n_closes"),
            "upnl": round(sum(float(p.get("unrealized_pnl_usd") or 0)
                              for p in state.get("positions") or []), 2),
        }
        last = None
        if P_HIST.exists():
            tail = P_HIST.read_text(encoding="utf-8").strip().splitlines()
            if tail:
                last = json.loads(tail[-1])
        if last and last.get("equity") == rec["equity"] and \
           last.get("score") == rec["score"] and last.get("upnl") == rec["upnl"] and \
           (rec["ts"] - float(last.get("ts") or 0)) < 3600:
            return
        P_HIST.parent.mkdir(parents=True, exist_ok=True)
        with P_HIST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def read_history(max_points: int = 400) -> list[dict]:
    try:
        if not P_HIST.exists():
            return []
        lines = P_HIST.read_text(encoding="utf-8").strip().splitlines()
        pts = []
        for l in lines[-max_points:]:
            try:
                pts.append(json.loads(l))
            except Exception:
                continue
        return pts
    except Exception:
        return []


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
            "last_mark_px": float(a.get("last_mark_px") or p.get("last_mark_px") or 0),
            "mark_age_s": now - lm if lm > 0 else None,
            "opened_ts": float(p.get("opened_ts") or 0),
            "hold_h": (now - float(p.get("opened_ts") or now)) / 3600,
            "thesis": p.get("thesis") or "",
            "invalidation": p.get("invalidation") or "",
        })
    return out


def compute_self_eval(freshness: dict, edge: dict, gates: list[dict], kill: dict) -> dict:
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

    rec = edge.get("expectancy_r_recent")
    if rec is not None and exp is not None and n >= 8:
        drift = rec - exp
        evidence.append(f"espérance récente {rec:+.2f} R (dérive {drift:+.2f})")
        if rec < 0 and exp > 0:
            actions.insert(0, "Fenêtre récente négative alors que le global est positif : "
                              "suspendre l'ajout de risque et segmenter les 10 derniers trades.")

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
        "recent_closes": compute_recent_closes(journal),
        "history": read_history(),
        "market": {
            "context": (s_ctx.data or {}),
            "signals": ((s_scan.data or {}).get("signals") or []),
            "scan_ts": (s_scan.data or {}).get("ts"),
        },
        "warnings": book.get("warnings", []),
        "provenance": [s.as_dict() for s in sources],
        "demo": False,
    }
    state["milestone"] = compute_milestone_state()
    state["self_eval"] = compute_self_eval(fresh, edge, gates, kill)
    return state, sources


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
    return {
        "built_ts": time.time(),
        "built_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "mode": "paper",
        "attestation": "BOOK · sync 2026-08-15T00:00:00Z · equity 1000.00$ · DD 0.00% · positions 0",
        "kill": {"active": False, "reason": None, "since_iso": None},
        "freshness": {"status": "ok", "sync_age_s": 60, "sync_status": "ok", "sync_note": "",
                       "mark_age_s": 60, "mark_status": "ok", "mark_note": ""},
        "capital": {"equity_usd": 1000.0, "peak_usd": 1000.0, "day_open_usd": 1000.0,
                    "week_open_usd": 1000.0, "dd_pct": 0, "day_pnl_pct": 0, "week_pnl_pct": 0},
        "gates": [
            {"key": "dd", "label": "Drawdown", "value_txt": "0 %", "limit_txt": "20 %",
             "util_pct": 0, "status": "ok", "note": ""},
            {"key": "pos", "label": "Positions", "value_txt": "0", "limit_txt": "4",
             "util_pct": 0, "status": "ok", "note": ""},
        ],
        "positions": [],
        "edge": {"n_opens": 0, "n_closes": 0, "target_trades": TARGET_TRADES, "verified": False,
                 "expectancy_r": None, "ci95": None, "win_rate_pct": None, "cost_ratio_pct": None,
                 "stop_share_pct": None, "median_hold_h": None, "plan_written_pct": None,
                 "best_r": None, "worst_r": None, "fees_usd": 0, "funding_usd": 0, "gross_usd": 0,
                 "net_usd": 0, "r_histogram": []},
        "recent_closes": [],
        "history": [],
        "market": {"context": {}, "signals": [], "scan_ts": None},
        "warnings": [],
        "provenance": _demo_sources(),
        "demo": True,
    }


def dur(age_s):
    if age_s is None:
        return "—"
    age_s = float(age_s)
    if age_s < 90:
        return f"{age_s:.0f} s"
    if age_s < 5400:
        return f"{age_s / 60:.0f} min"
    if age_s < 172800:
        return f"{age_s / 3600:.1f} h"
    return f"{age_s / 86400:.1f} j"


def money(v, dec=2):
    return f"{float(v):,.{dec}f} $".replace(",", " ")


def spark_svg(pts: list[dict], key: str, baseline: float = None) -> str | None:
    if not pts:
        return None
    vals = [float(p.get(key) or 0) for p in pts]
    if not vals:
        return None
    min_v, max_v = min(vals), max(vals)
    if baseline is not None:
        min_v = min(min_v, baseline)
        max_v = max(max_v, baseline)
    rng = max_v - min_v or 1
    w, h = 640, 96
    pad = 4
    xs = [pad + i * (w - 2 * pad) / max(len(vals) - 1, 1) for i in range(len(vals))]
    ys = [h - pad - (v - min_v) / rng * (h - 2 * pad) for v in vals]
    path = f"M{xs[0]:.1f},{ys[0]:.1f}" + "".join(f" L{x:.1f},{y:.1f}" for x, y in zip(xs[1:], ys[1:]))
    parts = [f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" role="img">']
    if baseline is not None:
        y_base = h - pad - (baseline - min_v) / rng * (h - 2 * pad)
        parts.append(f'<line x1="0" y1="{y_base:.1f}" x2="{w}" y2="{y_base:.1f}" '
                     f'stroke="#B0801F" stroke-width="1" stroke-dasharray="3,3" opacity="0.5"/>')
    parts.append(f'<path d="{path}" fill="none" stroke="#1F45C8" stroke-width="2"/>')
    return "".join(parts) + "</svg>"


def asset_uri(name: str) -> str | None:
    p = Path(__file__).parent / "assets" / name
    if not p.exists():
        return None
    ext = p.suffix.lower()
    mime = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".svg": "image/svg+xml"}.get(ext)
    if not mime:
        return None
    b64 = base64.b64encode(p.read_bytes()).decode()
    return f"data:{mime};base64,{b64}"


# ---------------------------------------------------------------------------
# CSS — inclus en inline
# ---------------------------------------------------------------------------

def _css() -> str:
    return '''
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

.kill{margin-top:26px;background:var(--oxblood);color:var(--paper-3);padding:18px 20px;
  border:2px solid var(--navy)}
.kill h2{margin:0 0 6px;font-family:var(--sans);font-size:26px;letter-spacing:.06em}
.kill p{margin:0;font-size:12px;opacity:.92}

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

.plate{background:var(--prussian);color:var(--paper-3);padding:26px 24px;position:relative;
  overflow:hidden;box-shadow:0 1px 0 rgba(11,21,51,.5)}
.plate::before{content:"";position:absolute;inset:0;pointer-events:none;opacity:.30;
  background-image:linear-gradient(135deg,transparent 60%,rgba(242,238,230,.15))}
.cap-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:18px}
.cap-k{font-size:9px;letter-spacing:.2em;color:rgba(242,238,230,.55)}
.cap-eq{font-family:var(--sans);font-weight:800;font-size:34px;line-height:1}
.cap-v{font-family:var(--sans);font-weight:800;font-size:22px;line-height:1.1}
.cap-strip{display:flex;flex-wrap:wrap;gap:14px;margin-top:18px;
  font-size:10px;color:rgba(242,238,230,.7)}
.cap-strip b{color:var(--paper-3)}

.overview{margin-top:30px}
.overview-head{display:flex;justify-content:space-between;align-items:center;
  flex-wrap:wrap;gap:8px}
.overview-title{font-weight:700;font-size:13px;letter-spacing:.14em;text-transform:uppercase}
.overview-grid{display:grid;grid-template-columns:repeat(5,1fr);gap:14px;margin-top:14px}
@media(max-width:700px){.overview-grid{grid-template-columns:repeat(3,1fr)}}
.metric{border:1px solid var(--hair);padding:14px 16px;background:rgba(255,255,255,.30)}
.metric-k{font-size:9.5px;letter-spacing:.18em;color:var(--ink-soft);text-transform:uppercase}
.metric-v{font-family:var(--sans);font-weight:800;font-size:24px;line-height:1.1;
  margin:4px 0 2px;font-variant-numeric:tabular-nums}
.metric-v.neg{color:var(--oxblood)}
.metric-n{font-size:10px;color:var(--ink-soft)}
.decision{margin-top:16px;border:1px solid var(--hair);padding:14px 16px;
  display:flex;gap:12px;align-items:baseline;background:rgba(255,255,255,.30)}
.decision b{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--cobalt)}
.live-chip{font-size:9px;letter-spacing:.1em;padding:2px 7px;border:1px solid var(--ink-soft);
  color:var(--ink-soft);background:rgba(255,255,255,.30)}
.health{font-size:9px;letter-spacing:.1em;padding:2px 7px;border:1px solid var(--ink-soft)}
.health--ok{color:var(--gold);border-color:var(--gold)}
.health--watch{color:var(--cobalt);border-color:var(--cobalt)}
.health--hot{color:var(--oxblood);border-color:var(--oxblood)}
.health--unknown{color:var(--ink-soft);border-color:var(--ink-soft)}

.curve{margin-top:30px;border:1px solid var(--hair);padding:18px;background:rgba(255,255,255,.30)}
.curve-head{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:12px}
.curve-title{font-weight:700;font-size:13px;letter-spacing:.14em;text-transform:uppercase}
.curve-meta{font-size:10px;color:var(--ink-soft)}
.spark{width:100%;height:96px;display:block}

.self-eval{margin-top:12px;display:grid;grid-template-columns:190px minmax(0,1fr) 1.1fr;
  border:1px solid var(--ink);background:var(--paper-3)}
.eval-verdict{padding:20px;background:var(--navy);color:var(--paper-3);
  display:flex;flex-direction:column;justify-content:space-between}
.eval-k{font-size:8px;letter-spacing:.2em;text-transform:uppercase;color:rgba(242,238,230,.56)}
.eval-status{font:900 18px/.9 var(--sans);margin-top:8px}
.eval-status--PERFORMING{color:var(--gold)}
.eval-status--IMPROVING{color:var(--paper-3)}
.eval-status--DEGRADING{color:var(--oxblood)}
.eval-status--HALTED,.eval-status--BLOCKED{color:var(--oxblood-lite)}
.eval-score{font:900 58px/.9 var(--sans);margin-top:22px}
.eval-score small{font:10px var(--mono);color:rgba(242,238,230,.55)}
.eval-axes{padding:20px;border-right:1px solid var(--hair)}
.axis{display:grid;grid-template-columns:68px 1fr 28px;align-items:center;gap:8px;
  font-size:10px;margin-bottom:6px}
.axis-track{height:6px;background:rgba(11,21,51,.12);position:relative}
.axis-track i{position:absolute;inset:0 auto 0 0;background:var(--cobalt);width:var(--w)}
.axis b{font-variant-numeric:tabular-nums;text-align:right}
.eval-action{padding:20px;display:flex;flex-direction:column;justify-content:space-between}
.eval-action strong{font:800 9px var(--mono);letter-spacing:.16em;text-transform:uppercase;color:var(--cobalt)}
.eval-action p{font:italic 17px/1.35 var(--serif);margin:10px 0;color:var(--ink)}
.eval-meta{font-size:9px;color:var(--ink-soft)}
@media(max-width:850px){.self-eval{grid-template-columns:150px 1fr}.eval-action{grid-column:1/-1;border-top:1px solid var(--hair)}.eval-axes{border-right:0}}
@media(max-width:520px){.self-eval{grid-template-columns:1fr}.eval-verdict{min-height:150px}.eval-axes,.eval-action{grid-column:auto;border-top:1px solid var(--hair)}.eval-score{font-size:46px}}

.risk-strip{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px}
.risk-card{border:1px solid var(--hair);padding:13px;background:rgba(242,238,230,.65)}
.risk-card--ok{border-left:3px solid var(--gold)}
.risk-card--watch{border-left:3px solid var(--cobalt)}
.risk-card--hot{border-left:3px solid var(--oxblood)}
.risk-card--breach{border-left:3px solid var(--oxblood);background:var(--oxblood);color:var(--paper-3)}
.risk-card--breach .risk-card-head{color:var(--paper-3)}
.risk-card-head{display:flex;justify-content:space-between;font-size:10px;
  letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft)}
.risk-card-head b{font-family:var(--sans);font-size:16px}
.risk-meter{height:4px;background:rgba(11,21,51,.12);margin:8px 0 4px}
.risk-meter span{display:block;height:100%;background:var(--cobalt)}
.risk-card--hot .risk-meter span{background:var(--oxblood)}
.risk-card--breach .risk-meter span{background:var(--paper-3)}
.risk-value{font-size:10px;color:var(--ink-soft)}

.wrap{overflow-x:auto}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th,.tbl td{padding:6px 8px;text-align:left;border-bottom:1px solid var(--hair)}
.tbl th{font-size:9px;letter-spacing:.12em;text-transform:uppercase;color:var(--ink-soft)}
.tbl .num{text-align:right;font-variant-numeric:tabular-nums}
.tbl .num.neg{color:var(--oxblood)}
.side{font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:2px 6px;border:1px solid var(--hair)}
.side--long{color:var(--gold);border-color:var(--gold)}
.side--short{color:var(--oxblood);border-color:var(--oxblood)}

.edge-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:1px;
  margin-top:14px}
.cell{border:1px solid var(--hair);padding:12px 14px;background:rgba(255,255,255,.30)}
.cell-k{font-size:9px;letter-spacing:.18em;text-transform:uppercase;color:var(--ink-soft)}
.cell-v{font-family:var(--sans);font-weight:800;font-size:22px;line-height:1.1;
  margin:4px 0 2px;font-variant-numeric:tabular-nums}
.cell-v.neg{color:var(--oxblood)}
.cell-n{font-size:10px;color:var(--ink-soft)}

.hist{display:flex;align-items:flex-end;gap:1px;height:80px;margin-top:18px}
.hbar{background:var(--cobalt);min-height:2px;flex:1}
.hbar--neg{background:var(--oxblood)}
.hlab{display:grid;grid-template-columns:repeat(8,1fr);gap:1px;font-size:8px;
  text-align:center;color:var(--ink-soft);margin-top:4px}

.progress{height:8px;background:rgba(31,69,200,.12);margin-top:12px;position:relative}
.progress span{position:absolute;inset:0 auto 0 0;background:var(--cobalt);width:var(--w)}

.stamp{display:inline-block;padding:3px 8px;font-size:9px;letter-spacing:.12em;
  text-transform:uppercase;font-weight:700;margin-top:10px}
.stamp--ok{border:1px solid var(--gold);color:var(--gold)}
.stamp--watch{border:1px solid var(--cobalt);color:var(--cobalt)}
.stamp--hot{border:1px solid var(--oxblood);color:var(--oxblood)}

.closes-tbl .num{font-variant-numeric:tabular-nums;text-align:right}
.closes-tbl .num.r-pos{color:var(--gold)}
.closes-tbl .num.r-neg{color:var(--oxblood)}
.reason-tag{font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:2px 6px;
  border:1px solid var(--hair)}
.reason-tag--stop_ratchet{color:var(--gold);border-color:var(--gold)}
.reason-tag--stop{color:var(--oxblood);border-color:var(--oxblood)}
.reason-tag--manual{color:var(--ink-soft)}
.reason-tag--thesis{color:var(--cobalt);border-color:var(--cobalt)}

.empty{padding:18px;background:rgba(255,255,255,.30);border:1px dashed var(--hair);
  font-size:11px;color:var(--ink-soft);font-style:italic}

.rail{position:fixed;left:0;top:0;bottom:0;width:58px;background:var(--navy);
  color:var(--paper-3);display:flex;flex-direction:column;align-items:center;
  padding:18px 0;z-index:50}
.rail-logo{font:900 18px/.9 var(--sans);letter-spacing:-.04em;margin-bottom:18px}
.rail-avatar{width:38px;height:38px;border-radius:50%;object-fit:cover;margin-bottom:18px;
  border:2px solid var(--gold)}
.rail-nav{display:flex;flex-direction:column;gap:4px;flex:1}
.rail-nav a{color:var(--paper-3);text-decoration:none;font-size:9px;letter-spacing:.08em;
  padding:8px 0;text-align:center;border-left:2px solid transparent;transition:.18s}
.rail-nav a:hover{border-left-color:var(--gold)}
.rail-foot{font-size:7px;letter-spacing:.2em;writing-mode:vertical-rl;color:rgba(242,238,230,.45);
  margin-bottom:14px}
@media(max-width:700px){.rail{display:none}.page{margin:0;padding:10px 12px 82px}}

.drawer{margin-top:18px;border:1px solid var(--hair);background:rgba(255,255,255,.30)}
.drawer summary{padding:14px 16px;cursor:pointer;font-weight:700;font-size:13px;
  letter-spacing:.14em;text-transform:uppercase}
.drawer-body{padding:0 16px 18px}

footer{margin-top:44px;padding:18px 0;border-top:1px solid var(--hair);
  font-size:10px;color:var(--ink-soft);text-align:center}

.prov .path{font-size:9px;word-break:break-all}
.vs{font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:2px 6px}
.vs--VERIFIED{color:var(--gold);border:1px solid var(--gold)}
.vs--UNVERIFIED{color:var(--ink-soft);border:1px solid var(--ink-soft)}
.vs--FAILED{color:var(--oxblood);border:1px solid var(--oxblood)}

.conv{font-size:9px;letter-spacing:.1em;text-transform:uppercase;padding:2px 6px;
  border:1px solid var(--hair)}
.conv--moyenne{color:var(--ink-soft)}
.conv--élevée{color:var(--gold);border-color:var(--gold)}
.conv--faible{color:var(--oxblood);border-color:var(--oxblood)}

.topline{display:flex;justify-content:space-between;align-items:center;
  font-size:9px;letter-spacing:.14em;text-transform:uppercase;color:var(--ink-soft);
  padding:8px 0;border-bottom:1px solid var(--hair)}
.topline .issue{font-family:var(--sans);font-weight:800;font-size:18px;
  letter-spacing:-.04em;color:var(--ink)}
.iii-symbol{display:inline-block;width:12px;height:12px;background:var(--iii-symbol);
  background-size:cover;vertical-align:middle}

@media(max-width:700px){
  .rail{display:none}
  .page{margin:0;padding:10px 12px 82px}
  .mast{height:430px;grid-template-columns:1fr;grid-template-rows:190px 240px}
  .mast-copy{padding:22px}
  .mast-copy::after{width:58px;height:52px;right:14px;top:14px}
  .mast-visual img{object-position:50% 30%}
  .wordmark{font-size:72px}
  .motto{font-size:16px}
  .topline{height:30px}
}
'''


# ---------------------------------------------------------------------------
# JS — inclus en inline
# ---------------------------------------------------------------------------

def _js() -> str:
    return '''
(function(){
"use strict";
var S; try{ S = JSON.parse(document.getElementById("nabu-state").textContent); }catch(_){ return; }
var chip = document.getElementById("live-chip");
var note = document.getElementById("live-note");
var NBSP = " ";
function fmt(v, dec){ return v.toLocaleString("en-US",{minimumFractionDigits:dec===undefined?2:dec,maximumFractionDigits:dec===undefined?2:dec}).replace(/,/g,NBSP)+NBSP+"$"; }
function setChip(cls, txt){ if(!chip) return; chip.className = "live-chip"+(cls?" "+cls:""); chip.textContent = txt; }

var builtMs = (S.built_ts||0)*1000;
function tickAges(){
  var extra = (Date.now()-builtMs)/1000;
  document.querySelectorAll(".clock").forEach(function(c){
    var v = c.querySelector(".clock-v"); if(!v) return;
    if(v.dataset.base===undefined){
      var k = c.querySelector(".clock-k")||{textContent:""};
      v.dataset.base = /sync/i.test(k.textContent) ? (S.freshness.sync_age_s||"") : (S.freshness.mark_age_s||"");
    }
    var b = parseFloat(v.dataset.base); if(isNaN(b)) return;
    var s = b + extra;
    v.textContent = s<90 ? Math.round(s)+NBSP+"s" : s<5400 ? Math.round(s/60)+NBSP+"min" : s<172800 ? (s/3600).toFixed(1)+NBSP+"h" : (s/86400).toFixed(1)+NBSP+"j";
  });
}
setInterval(tickAges, 30000);

var rows = Array.prototype.slice.call(document.querySelectorAll(".pos-row"));
var isPaper = String(S.mode||"").toLowerCase()==="paper";
var staticUpnl = (S.positions||[]).reduce(function(a,p){return a+(p.unrealized_pnl_usd||0);},0);
var lastOk = 0, failures = 0;

function applyMids(mids){
  var liveUpnl = 0, n = 0;
  rows.forEach(function(r){
    var sym = r.dataset.sym, mid = parseFloat(mids[sym]);
    if(!mid || !isFinite(mid)) return;
    var size = parseFloat(r.dataset.size), entry = parseFloat(r.dataset.entry);
    var pnl = (r.dataset.side==="short" ? (entry-mid) : (mid-entry)) * size;
    liveUpnl += pnl; n++;
    var cell = r.querySelector(".pos-upnl");
    if(cell){
      var old = cell.textContent;
      cell.textContent = (pnl>=0?"+":"")+pnl.toFixed(2)+NBSP+"$";
      cell.classList.toggle("neg", pnl<0);
      if(old!==cell.textContent){ cell.classList.remove("flash"); void cell.offsetWidth; cell.classList.add("flash"); }
    }
    var mk = r.querySelector(".pos-mark");
    if(mk){ mk.textContent = "live"; mk.classList.add("delta-up"); }
  });
  if(!n) return;
  var mU = document.getElementById("m-upnl");
  if(mU){ mU.textContent = fmt(liveUpnl); mU.classList.toggle("neg", liveUpnl<0); }
  var eq = (S.capital.equity_usd||0) - staticUpnl + liveUpnl;
  var mE = document.getElementById("m-equity");
  if(mE){ mE.textContent = fmt(eq); }
  var dOpen = S.capital.day_open_usd||0;
  if(dOpen>0){
    var dp = (eq-dOpen)/dOpen*100;
    var mD = document.getElementById("m-day");
    if(mD){ mD.textContent = (dp>=0?"+":"")+dp.toFixed(2)+NBSP+"%"; mD.classList.toggle("neg", dp<0); }
  }
  lastOk = Date.now();
  setChip("live-chip--on", "LIVE · HL");
  if(note) note.textContent = "marks live Hyperliquid · uPnL recalculé localement · " +
    new Date().toISOString().slice(11,19) + "Z · book.json reste la source (" + n + "/" + rows.length + " positions)";
}

function poll(){
  if(document.hidden) return;
  if(!rows.length){ setChip("live-chip--on","LIVE · flat"); return; }
  var ctl = new AbortController(); var t = setTimeout(function(){ctl.abort();}, 8000);
  fetch("https://api.hyperliquid.xyz/info", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:'{"type":"allMids"}', signal:ctl.signal, cache:"no-store"
  }).then(function(r){ clearTimeout(t); if(!r.ok) throw 0; return r.json(); })
    .then(function(mids){ failures=0; applyMids(mids); })
    .catch(function(){ clearTimeout(t); failures++;
      if(failures>=2){ setChip("live-chip--stale", lastOk ? "live perdu · tirage "+S.built_iso.slice(11,16)+"Z" : "page statique"); } });
}

function checkNewer(){
  if(document.hidden || location.protocol==="file:") return;
  fetch(location.href, {cache:"no-store"}).then(function(r){ return r.text(); })
    .then(function(txt){
      var m = txt.match(/"built_ts":\\s*([0-9.]+)/);
      if(m && parseFloat(m[1]) > (S.built_ts||0)+1){ location.reload(); }
    }).catch(function(){});
}

if(rows.length || isPaper){ poll(); setInterval(poll, 15000); }
setInterval(checkNewer, 180000);
document.addEventListener("visibilitychange", function(){ if(!document.hidden){ poll(); tickAges(); } });
tickAges();
})();
'''


# ---------------------------------------------------------------------------
# Rendu HTML
# ---------------------------------------------------------------------------

def render(state: dict) -> str:
    S = state
    fr = S["freshness"]
    cap = S["capital"]
    edge = S["edge"]
    hero = asset_uri("nabu-command.webp")
    avatar = asset_uri("nabu-portrait.webp")
    o: list[str] = []
    a = o.append
    e = html.escape
    NB = " "

    a("<!DOCTYPE html><html lang=\"fr\"><head><meta charset=\"utf-8\">")
    a("<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">")
    a(f"<title>N*ABU · planche de lecture · {e(S['built_iso'])}</title>")
    a("<meta name=\"color-scheme\" content=\"light\">")
    a("<style>" + _css() + "</style></head><body>")
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

    positions = S["positions"]
    upnl = sum(float(p.get("unrealized_pnl_usd") or 0) for p in positions)
    max_gate = max(S["gates"], key=lambda x: x["util_pct"], default=None)
    risk_txt = f"{max_gate['util_pct']:.0f} %" if max_gate else "—"
    risk_note = max_gate["label"] if max_gate else "limites indisponibles"
    health_label = {"ok": "À jour", "watch": "À surveiller", "hot": "Périmé",
                    "unknown": "Inconnu"}.get(fr["status"], fr["status"])
    p = cap.get("paper") or {}
    realized = float(p.get("realized_pnl_usd") or 0)
    a("<section class=\"overview\" id=\"portfolio\" aria-label=\"Synthèse du portefeuille\">")
    a(f"<div class=\"overview-head\"><div class=\"overview-title\">Portefeuille · maintenant</div>"
      f"<div style=\"display:flex;gap:8px;align-items:center\">"
      f"<span class=\"live-chip\" id=\"live-chip\" title=\"prix Hyperliquid en direct — recalcul local du uPnL\">page statique</span>"
      f"<span class=\"health health--{e(fr['status'])}\">{e(health_label)}</span></div></div>")
    a("<div class=\"overview-grid\">")
    metrics = [
        ("Equity", money(cap["equity_usd"]), f"pic {money(cap['peak_usd'], 0)}", False, "m-equity"),
        ("Aujourd'hui", f"{cap['day_pnl_pct']:+.2f} %", "performance UTC", cap["day_pnl_pct"] < 0, "m-day"),
        ("uPnL ouvert", money(upnl), f"{len(positions)} position{'s' if len(positions) != 1 else ''}", upnl < 0, "m-upnl"),
        ("PnL réalisé", money(realized), "net des clôtures", realized < 0, "m-realized"),
        ("Risque max", risk_txt, risk_note, bool(max_gate and max_gate["status"] in ("hot", "breach")), "m-risk"),
    ]
    for kk, vv, nn, bad, mid in metrics:
        a(f"<div class=\"metric\"><div class=\"metric-k\">{e(kk)}</div>"
          f"<div class=\"metric-v upd{' neg' if bad else ''}\" id=\"{mid}\">{e(vv)}</div>"
          f"<div class=\"metric-n\">{e(nn)}</div></div>")
    a(f"</div><div class=\"decision\"><b>Lecture</b><span>{e(v)}</span></div>"
      f"<div id=\"live-note\"></div></section>")

    hist = S.get("history") or []
    curve = spark_svg(hist, "equity", baseline=float((cap.get("paper") or {}).get("start_equity_usd") or 0) or None)
    if curve:
        first, lastp = hist[0], hist[-1]
        span_h = (float(lastp["ts"]) - float(first["ts"])) / 3600
        span_txt = f"{span_h / 24:.1f} j" if span_h > 48 else f"{span_h:.0f} h"
        a(f"<div class=\"curve\"><div class=\"curve-head\"><span class=\"curve-title\">Courbe d'equity</span>"
          f"<span class=\"curve-meta\">400 points · {span_txt} · trait or = capital initial · dernier point {money(lastp.get('equity'))}</span></div>"
          f"{curve}</div>")

    se = S.get("self_eval") or {}
    a("<section class=\"self-eval\" id=\"self-eval\" aria-label=\"Auto-évaluation de N*ABU\">")
    a(f"<div class=\"eval-verdict\"><div><div class=\"eval-k\">Self-evaluation / cycle</div>"
      f"<div class=\"eval-status eval-status--{e(se.get('verdict', 'unknown'))}\">{e(se.get('verdict', '?'))}</div></div>"
      f"<div class=\"eval-score\">{int(se.get('score') or 0)}<small>/100</small></div></div>")
    a("<div class=\"eval-axes\"><div class=\"eval-k\" style=\"color:var(--ink-soft)\">Qualité du système</div>")
    axes = [("Données", "data"), ("Risque", "risk"), ("Discipline", "discipline"), ("Edge", "edge")]
    for label, key in axes:
        score = int((se.get("scores") or {}).get(key, 0))
        max_score = 35 if key == "edge" else 25 if key == "risk" else 20
        pct = min(100, score / max_score * 100) if max_score else 0
        a(f"<div class=\"axis\"><span>{e(label)}</span><span class=\"axis-track\"><i style=\"--w:{pct:.1f}%\"></i></span><b>{score}</b></div>")
    a("</div>")
    a(f"<div class=\"eval-action\"><div><strong>Prochaine amélioration</strong>"
      f"<p>{e(se.get('next_action', 'Observer.'))}</p></div>"
      f"<div class=\"eval-meta\">Confiance {e(se.get('confidence', '?'))} · prochaine revue après {se.get('review', {}).get('next_review_after_closes', '?')} clôtures · limites de risque immuables</div></div></section>")

    a("<details class=\"drawer\"><summary>Fraîcheur et attestation des données</summary><div class=\"drawer-body\">"
      "<section class=\"attest\"><div class=\"eyebrow\">Attestation — invariant #2</div>")
    a("<div class=\"rule rule--thick\"></div>")
    a(f"<p class=\"attest-line\">{e(S['attestation'])}</p>")
    a("<div class=\"clocks\">")
    a(f"<div class=\"clock clock--{e(fr['sync_status'])}\"><span class=\"clock-k\">Âge du sync</span>"
      f"<span class=\"clock-v\">{dur(fr['sync_age_s'])}</span>"
      f"<span class=\"clock-n\">{e(fr['sync_note'])}</span></div>")
    a(f"<div class=\"clock clock--{e(fr['mark_status'])}\"><span class=\"clock-k\">Âge du mark</span>"
      f"<span class=\"clock-v\">{dur(fr['mark_age_s'])}</span>"
      f"<span class=\"clock-n\">{e(fr['mark_note'])}</span></div>")
    a("</div>")
    a(f"<p class=\"verdict\">{e(v)}</p></section></div></details>")

    a("<details class=\"drawer\"><summary>Détail du capital et du compte</summary><div class=\"drawer-body\">"
      "<section class=\"sec\"><div class=\"eyebrow\">Capital</div><div class=\"rule\"></div>")
    a("<div class=\"plate\"><div class=\"cap-grid\">")
    a(f"<div><div class=\"cap-k\">Equity</div><div class=\"cap-eq\">{money(cap['equity_usd'])}</div></div>")
    a(f"<div><div class=\"cap-k\">Drawdown / pic</div><div class=\"cap-v neg\">{cap['dd_pct']:.2f} %</div>"
      f"<div class=\"note\">pic {money(cap['peak_usd'], 0)}</div></div>")
    a(f"<div><div class=\"cap-k\">Jour / semaine</div><div class=\"cap-v\">{cap['day_pnl_pct']:+.2f} %</div>"
      f"<div class=\"cap-v neg\" style=\"font-size:16px;opacity:.85\">{cap['week_pnl_pct']:+.2f} % · 7 j</div></div>")
    a("</div>")
    a("<div class=\"cap-strip\">")
    a(f"<span>Cash <b>{money(p.get('cash_usd'))}</b></span>")
    a(f"<span>PnL réalisé <b>{money(p.get('realized_pnl_usd'))}</b></span>")
    a(f"<span>Frais cumulés <b>{money(p.get('fees_paid_usd'))}</b></span>")
    a(f"<span>Funding cumulé <b>{money(p.get('funding_paid_usd'), 4)}</b></span>")
    a(f"<span>Trades clos <b>{p.get('closed_trades', 0)}</b></span>")
    a(f"<span>Capital initial <b>{money(p.get('start_equity_usd'))}</b></span>")
    a("</div></div></section></div></details>")

    a("<section class=\"sec primary-section\" id=\"risk\"><div class=\"eyebrow\">Risques à surveiller</div><div class=\"rule\"></div>")
    a("<div class=\"risk-strip\">")
    for g in S["gates"]:
        a(f"<div class=\"risk-card risk-card--{e(g['status'])}\"><div class=\"risk-card-head\"><span>{e(g['label'])}</span><b>{e(g['value_txt'])}</b></div>"
          f"<div class=\"risk-meter\"><span style=\"--w:{min(g['util_pct'], 100):.1f}%\"></span></div>"
          f"<div class=\"risk-value\">{e(g['value_txt'])} / {e(g['limit_txt'])}</div></div>")
    a("</div></section>")

    if positions:
        a("<details class=\"drawer\" id=\"positions\"><summary>Positions ouvertes</summary><div class=\"drawer-body\">"
          "<section class=\"sec\"><div class=\"eyebrow\">Détail des positions</div><div class=\"rule\"></div>")
        a("<div class=\"wrap\"><table class=\"tbl\"><thead><tr><th>Venue</th><th>Sym</th><th>Sens</th>"
          "<th class=\"num\">Entrée</th><th class=\"num\">Stop</th><th class=\"num\">Dist</th>"
          "<th class=\"num\">Notional</th><th class=\"num\">uPnL</th><th class=\"num\">Portage</th></tr></thead><tbody>")
        for pos in positions:
            a(f"<tr><td>{e(pos.get('venue'))}</td><td><b>{e(pos.get('symbol'))}</b></td>"
              f"<td><span class=\"side side--{e(pos.get('side'))}\">{e(pos.get('side'))}</span></td>"
              f"<td class=\"num\">{pos.get('entry_px', 0):.4f}</td>"
              f"<td class=\"num\">{pos.get('stop_px', 0):.4f}</td>"
              f"<td class=\"num\">{pos.get('stop_dist_pct', 0):.2f} %</td>"
              f"<td class=\"num\">{money(pos.get('notional_usd'))}</td>"
              f"<td class=\"num{' neg' if pos.get('unrealized_pnl_usd', 0) < 0 else ''}\">{money(pos.get('unrealized_pnl_usd'))}</td>"
              f"<td class=\"num\">{pos.get('hold_h', 0):.1f} h</td></tr>")
        a("</tbody></table></div></section></div></details>")

    # ================================================================
    # EDGE MESURÉ + MILESTONE REVIEW
    # Structure:
    #   1. Progression (barre)
    #   2. Stats actuelles (tous les trades)
    #   3. Palier 1 (complété)
    #   4. Palier 2 (en cours)
    #   5. Palier 3-7 (à venir)
    # ================================================================
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
      "En dessous, ces chiffres décrivent un échantillon, pas un edge — l'intervalle le dit mieux que la moyenne.</p>")

    # --- Stats actuelles (tous les trades) ---
    if n:
        a("<div class=\"rule\"></div>")
        a("<div class=\"eyebrow\">Stats actuelles</div>")
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
              "Une espérance portée par une seule barre à droite n'est pas un edge, c'est un trade.</p>")

        rec = edge.get("expectancy_r_recent")
        if rec is not None:
            drift = ""
            if edge.get("expectancy_r") is not None:
                d = rec - edge["expectancy_r"]
                drift = f" · dérive vs global {d:+.2f} R"
            a(f"<p class=\"note\" style=\"margin-top:8px\"><b>Espérance récente ({edge.get('recent_window')} derniers) : "
              f"{rec:+.2f} R</b>{e(drift)} — c'est la fenêtre qui meurt en premier quand l'edge décède.</p>")

        closes_r = S.get("recent_closes") or []
        if closes_r:
            a("<div class=\"eyebrow\" style=\"margin-top:26px\">Dernières clôtures — matière du post-mortem</div>"
              "<div class=\"rule\"></div>"
              "<div class=\"wrap\"><table class=\"tbl closes-tbl\"><thead><tr>"
              "<th>Quand</th><th>Sym</th><th>Sens</th><th class=\"num\">R</th>"
              "<th class=\"num\">PnL net</th><th class=\"num\">Portage</th><th>Sortie</th><th>Thèse</th>"
              "</tr></thead><tbody>")
            for c in closes_r:
                r_val = c.get("r_multiple")
                r_txt = f"{float(r_val):+.2f}" if r_val is not None else "—"
                r_cls = "" if r_val is None else (" r-pos" if float(r_val) > 0 else " r-neg")
                pnl = float(c.get("realized_pnl_usd") or 0)
                when = c.get("iso") or (time.strftime("%m-%d %H:%M", time.gmtime(float(c["ts"]))) if c.get("ts") else "—")
                if isinstance(when, str) and "T" in when:
                    when = when[5:16].replace("T", " ")
                reason = str(c.get("reason") or "—")
                thesis_short = (c.get("thesis") or "—")
                if len(thesis_short) > 70:
                    thesis_short = thesis_short[:67] + "…"
                a(f"<tr><td class=\"num\" style=\"text-align:left\">{e(when)}</td>"
                  f"<td><b>{e(c.get('symbol'))}</b></td>"
                  f"<td><span class=\"side side--{e(c.get('side'))}\">{e(c.get('side'))}</span></td>"
                  f"<td class=\"num{r_cls}\">{e(r_txt)}</td>"
                  f"<td class=\"num{' neg' if pnl < 0 else ''}\">{pnl:+.2f}{NB}$</td>"
                  f"<td class=\"num\">{float(c.get('hold_hours') or 0):.1f}{NB}h</td>"
                  f"<td><span class=\"reason-tag reason-tag--{e(reason)}\">{e(reason)}</span></td>"
                  f"<td class=\"note\">{e(thesis_short)}</td></tr>")
            a("</tbody></table></div>")
    else:
        a("<p class=\"empty\">Aucun trade clos non-artefact dans le journal. Rien à mesurer, "
          "rien à inventer.</p>")

    # --- Milestone Review ---
    ms = S.get("milestone") or {}
    ms_n = ms.get("n_closes", 0)
    ms_target = ms.get("target", tgt)
    ms_progress = ms.get("progress", {})
    ms_paliers = ms.get("paliers", [])

    if ms_paliers:
        # Progression dans le palier actuel
        a("<div class=\"rule\"></div>")
        a("<div class=\"eyebrow\">Progression</div>")
        pct_prog = ms_progress.get("pct", 0)
        done_prog = ms_progress.get("done", 0)
        total_prog = ms_progress.get("total", tgt)
        a(f"<div class=\"progress\"><span style=\"--w:{pct_prog:.1f}%\"></span></div>")
        a(f"<p class=\"note\" style=\"margin-top:8px\">{done_prog} / {total_prog} trades vers l'évaluation suivante.</p>")

        # Chaque palier
        for p in ms_paliers:
            p_n = p.get("n", 0)
            p_status = p.get("status", "pending")
            p_label = p.get("label", f"Palier {p_n}")
            p_n_trades = p.get("n_trades", 0)

            a("<div class=\"rule\"></div>")

            if p_status == "completed":
                p_exp = p.get("expectancy_r")
                p_ci = p.get("ci95")
                p_ci_txt = f"IC95 {p_ci[0]:+.2f} … {p_ci[1]:+.2f}" if p_ci else "IC95 indisponible"
                p_exp_s = f"{p_exp:+.2f}R" if p_exp is not None else "?"
                a(f"<div class=\"eyebrow\">{p_label} — {p_n_trades} trades · terminé</div>")
                cells = [
                    ("Espérance", p_exp_s, p_ci_txt, p_exp is not None and p_exp < 0),
                    ("Taux de gain", f"{p.get('win_rate_pct', 0):.0f}%", "métrique de vanité", False),
                    ("Meilleur R", f"{p.get('best_r', 0):+.2f}R", "best trade", False),
                    ("Pire R", f"{p.get('worst_r', 0):+.2f}R", "worst trade", True),
                ]
                a("<div class=\"edge-grid\">")
                for kk, vv, nn, bad in cells:
                    a(f"<div class=\"cell\"><div class=\"cell-k\">{e(kk)}</div>"
                      f"<div class=\"cell-v{' neg' if bad else ''}\">{e(vv)}</div>"
                      f"<div class=\"cell-n\">{e(nn)}</div></div>")
                a("</div>")
                p_imps = p.get("improvements", [])
                if p_imps:
                    a("<div class=\"rule\"></div>")
                    a("<div class=\"eyebrow\">Améliorations proposées</div>")
                    a("<ul class=\"note\" style=\"margin:0;padding-left:18px\">")
                    for imp in p_imps[:5]:
                        a(f"<li>{e(imp)}</li>")
                    a("</ul>")
            elif p_status == "building":
                p_exp = p.get("expectancy_r")
                p_exp_s = f"{p_exp:+.2f}R" if p_exp is not None else "?"
                a(f"<div class=\"eyebrow\">{p_label} — {p_n_trades} trades · en cours</div>")
                cells = [
                    ("Espérance partielle", p_exp_s, "non significatif", p_exp is not None and p_exp < 0),
                    ("Taux de gain", f"{p.get('win_rate_pct', 0):.0f}%", "métrique de vanité", False),
                    ("Meilleur R", f"{p.get('best_r', 0):+.2f}R", "best trade", False),
                    ("Pire R", f"{p.get('worst_r', 0):+.2f}R", "worst trade", True),
                ]
                a("<div class=\"edge-grid\">")
                for kk, vv, nn, bad in cells:
                    a(f"<div class=\"cell\"><div class=\"cell-k\">{e(kk)}</div>"
                      f"<div class=\"cell-v{' neg' if bad else ''}\">{e(vv)}</div>"
                      f"<div class=\"cell-n\">{e(nn)}</div></div>")
                a("</div>")
            else:
                a(f"<div class=\"eyebrow\">{p_label} — à venir</div>")

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
              f"<td class=\"num{' neg' if dev < 0 else ''}\">{dev:+.2f} %</td>"
              f"<td class=\"num\">{float(d.get('range_lo_50h') or 0):,.2f} – "
              f"{float(d.get('range_hi_50h') or 0):,.2f}</td>"
              f"<td class=\"num\">{f8:+.3f} bps</td>"
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
                  f"<td class=\"num\">{float(x.get('funding_bps') or 0):+.3f} bps</td>"
                  f"<td class=\"num\">{float(x.get('oi_usd') or 0) / 1e6:,.0f} M$</td>"
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
    a("</script>")
    a("<script>" + _js() + "</script>")
    a("</body></html>")

    return "".join(o)


# ---------------------------------------------------------------------------
# Main
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
    tmp.replace(out)
    if a.cmd == "build":
        append_history(state)

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


