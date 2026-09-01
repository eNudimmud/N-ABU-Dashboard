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
import subprocess
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
P_HIST = LIVE / "data" / "equity_history.jsonl"   # écrit UNIQUEMENT par ce script (append)
P_MILESTONE = LIVE / "bin" / "nabu_milestone.py"   # système de paliers

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


def compute_milestone_state() -> dict:
    """Calcule l'état du palier courant via nabu_milestone.py."""
    try:
        p = subprocess.run(
            [sys.executable, str(P_MILESTONE), "json"],
            capture_output=True, text=True, timeout=30
        )
        if p.returncode == 0:
            return json.loads(p.stdout)
    except Exception:
        pass
    return {"current": 0, "crossed": False, "n_closes": 0, "target_trades": TARGET_TRADES}


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
        # expectancy glissante sur les 10 dernières clôtures — la dérive avant la moyenne
        if len(rs) >= 4:
            k = min(10, len(rs))
            out["expectancy_r_recent"] = statistics.fmean(rs[-k:])
            out["recent_window"] = k
    return out


# ---------------------------------------------------------------------------
# Paliers de revue — chaque palier évalué sur SES trades, jamais sur le cumul
# ---------------------------------------------------------------------------

def _slice_stats(chunk: list[dict]) -> dict:
    """Mêmes maths que compute_edge, appliquées à une tranche de clôtures."""
    out = {"n_trades": len(chunk), "expectancy_r": None, "ci95": None,
           "win_rate_pct": None, "best_r": None, "worst_r": None,
           "stop_share_pct": None, "median_hold_h": None,
           "cost_ratio_pct": None, "net_usd": 0.0,
           "avg_win_r": None, "avg_loss_r": None}
    if not chunk:
        return out
    rs = [float(r["r_multiple"]) for r in chunk if r.get("r_multiple") is not None]
    fees = sum(abs(float(r.get("fees_usd") or 0)) for r in chunk)
    fund = sum(float(r.get("funding_usd") or 0) for r in chunk)
    gross = sum(abs(float(r.get("gross_pnl_usd") or 0)) for r in chunk)
    holds = [float(r["hold_hours"]) for r in chunk if r.get("hold_hours") is not None]
    out["net_usd"] = sum(float(r.get("realized_pnl_usd") or 0) for r in chunk)
    out["cost_ratio_pct"] = (fees + abs(fund)) / gross * 100.0 if gross > 0 else None
    out["stop_share_pct"] = len([r for r in chunk if r.get("reason") == "stop"]) / len(chunk) * 100.0
    out["median_hold_h"] = statistics.median(holds) if holds else None
    if rs:
        wins = [r for r in rs if r > 0]
        losses = [r for r in rs if r <= 0]
        out["expectancy_r"] = statistics.fmean(rs)
        out["win_rate_pct"] = len(wins) / len(rs) * 100.0
        out["best_r"], out["worst_r"] = max(rs), min(rs)
        out["avg_win_r"] = statistics.fmean(wins) if wins else None
        out["avg_loss_r"] = statistics.fmean(losses) if losses else None
        if len(rs) >= 2:
            half = 1.96 * statistics.stdev(rs) / math.sqrt(len(rs))
            out["ci95"] = [out["expectancy_r"] - half, out["expectancy_r"] + half]
    return out


def _palier_verdict(st: dict, target: int) -> tuple[str, str]:
    """(verdict, lecture) d'un palier — sur ses trades seulement."""
    n, exp, ci = st["n_trades"], st["expectancy_r"], st["ci95"]
    if not n:
        return "À VENIR", f"Palier non entamé — {target} clôtures à produire."
    if exp is None:
        return "SANS R", "Aucun R journalisé sur ce palier : rien de mesurable."
    if n < target:
        return "EN COURS", (f"{n} / {target} clôtures. Espérance partielle {exp:+.2f} R, "
                            f"non significative : aucune conclusion avant la fin du palier.")
    if ci and ci[0] > 0:
        return "EDGE PROUVÉ", (f"Espérance {exp:+.2f} R et borne basse de l'IC95 positive "
                               f"({ci[0]:+.2f} R). L'edge survit au bruit sur ce palier.")
    if ci and ci[1] < 0:
        return "PERTE PROUVÉE", (f"Espérance {exp:+.2f} R et borne haute de l'IC95 négative "
                                 f"({ci[1]:+.2f} R). Ce palier perd de façon mesurable.")
    if exp > 0:
        return "POSITIF NON PROUVÉ", (f"Espérance {exp:+.2f} R mais l'IC95 traverse zéro : "
                                      f"rentable sans que ce soit démontrable.")
    return "SANS EDGE", (f"Espérance {exp:+.2f} R, IC95 à cheval sur zéro : ce palier ne "
                         f"distingue pas la stratégie du hasard.")


def compute_paliers(journal: list, target: int = TARGET_TRADES,
                    milestone: dict | None = None) -> list[dict]:
    """Découpe les clôtures en paliers de `target` trades et évalue chacun sur
    sa propre tranche. Calcul local : la section se rend même si
    nabu_milestone.py ne répond pas. Les améliorations proposées, elles,
    viennent de nabu_milestone.py — on ne les invente pas."""
    closes = [r for r in journal if r.get("event") == "fill"
              and r.get("kind") == "close" and not _is_artifact(r)]
    ext = {}
    for pp in ((milestone or {}).get("paliers") or []):
        if pp.get("n") is not None:
            ext[int(pp["n"])] = pp

    total = len(closes)
    n_show = max(3, total // target + 2)
    out: list[dict] = []
    prev_exp = None
    for i in range(1, n_show + 1):
        chunk = closes[(i - 1) * target: i * target]
        st = _slice_stats(chunk)
        status = "completed" if len(chunk) == target else "building" if chunk else "pending"
        verdict, reading = _palier_verdict(st, target)
        delta = (st["expectancy_r"] - prev_exp) if (st["expectancy_r"] is not None
                                                    and prev_exp is not None) else None
        if st["expectancy_r"] is not None and status == "completed":
            prev_exp = st["expectancy_r"]
        st.update({
            "n": i, "label": f"Palier {i}", "status": status,
            "range_txt": f"trades {(i - 1) * target + 1} – {i * target}",
            "verdict": verdict, "reading": reading, "delta_r": delta,
            "improvements": (ext.get(i) or {}).get("improvements") or [],
            "first_iso": (chunk[0].get("iso") if chunk else None),
            "last_iso": (chunk[-1].get("iso") if chunk else None),
        })
        out.append(st)
    return out


def compute_recent_closes(journal: list, limit: int = 8) -> list[dict]:
    """Les dernières clôtures, telles quelles — matière première du post-mortem."""
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


# ---------------------------------------------------------------------------
# Historique — appendé par ce script à chaque build (jamais réécrit)
# ---------------------------------------------------------------------------

def append_history(state: dict) -> None:
    """Un point par build : equity, score, verdict. Dédoublonné sur le synced_at."""
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
        # inutile d'empiler des points identiques quand rien n'a bougé
        if last and last.get("equity") == rec["equity"] and \
           last.get("score") == rec["score"] and last.get("upnl") == rec["upnl"] and \
           (rec["ts"] - float(last.get("ts") or 0)) < 3600:
            return
        P_HIST.parent.mkdir(parents=True, exist_ok=True)
        with P_HIST.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass                    # l'historique ne casse jamais un tirage


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
            "funding_paid_usd": float(p.get("funding_paid_usd") or 0),
            "last_mark_px": float(a.get("last_mark_px") or 0),
            "mark_age_s": (now - lm) if lm else None,
            "opened_ts": float(a.get("opened_ts") or 0),
            "hold_h": ((now - float(a.get("opened_ts") or 0)) / 3600) if a.get("opened_ts") else None,
            "thesis": a.get("thesis") or "",
            "invalidation": a.get("invalidation") or "",
        })
    # uPnL recalculé depuis last_mark_px (account) pour être cohérent avec le R
    # (les deux utilisent la même source de marks — pas de mismatch book vs account)
    for pt in out:
        mk = pt["last_mark_px"]
        if mk > 0:
            pt["unrealized_pnl_usd"] = (
                (float(pt["entry_px"]) - mk) if pt["side"] == "short"
                else (mk - float(pt["entry_px"]))
            ) * float(pt["size"])
        else:
            pt["unrealized_pnl_usd"] = float(
                next((bp.get("unrealized_pnl_usd", 0)
                      for bp in book.get("positions", [])
                      if str(bp.get("symbol", "")).upper() == pt["symbol"]),
                0) or 0)
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

    # dérive de fenêtre récente — l'edge meurt d'abord dans les 10 derniers trades
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
    state["paliers"] = compute_paliers(journal, milestone=state["milestone"])
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


def _demo_journal() -> list[dict]:
    """Journal synthetique - 68 clotures sur trois paliers. Le mode demo passe
    par compute_edge / compute_paliers comme la production : ce qui casse en
    demo casse en prod, et inversement."""
    import random
    rng = random.Random(1789)
    now = time.time()
    syms = ["ETH", "BTC", "SOL", "HYPE", "ZEC"]
    theses = ["Rejet du haut de range 50h, funding neutre.",
              "Sous MA200 daily, OI en expansion sur prix plat.",
              "Reprise au-dessus du bas de range, funding negatif.",
              "Cassure du bas de range 50h sur volume.",
              "Divergence OI/prix, squeeze probable."]
    # palier 1 : stops qui glissent - palier 2 : apres correction - palier 3 : en cours
    regimes = [(30, 0.36, 1.20), (30, 0.46, 1.02), (8, 0.50, 0.97)]
    total = sum(r[0] for r in regimes)
    out: list[dict] = []
    idx = 0
    for n_tr, p_win, loss_mag in regimes:
        for _ in range(n_tr):
            idx += 1
            win = rng.random() < p_win
            r = (round(abs(rng.gauss(1.30, 0.70)), 2) if win
                 else round(-abs(rng.gauss(loss_mag, 0.20)), 2))
            sym, side = rng.choice(syms), rng.choice(["long", "short"])
            hold = round(abs(rng.gauss(13, 8)) + 1.2, 1)
            risk_usd = round(rng.uniform(8.0, 11.5), 2)
            gross = round(r * risk_usd, 2)
            fees = round(abs(gross) * 0.02 + 0.35, 2)
            fund = round(rng.gauss(-0.05, 0.18), 3)
            ts_c = now - (total - idx + 1) * 9.4 * 3600
            ts_o = ts_c - hold * 3600
            reason = "stop" if r <= -0.85 else "target" if r >= 1.0 else "invalidation"
            out.append({"event": "fill", "kind": "open", "symbol": sym, "side": side,
                        "ts": ts_o,
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_o)),
                        "thesis": rng.choice(theses),
                        "invalidation": "Cloture horaire de l\u2019autre cote du niveau."})
            out.append({"event": "fill", "kind": "close", "symbol": sym, "side": side,
                        "ts": ts_c,
                        "iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts_c)),
                        "r_multiple": r, "realized_pnl_usd": round(gross - fees + fund, 2),
                        "gross_pnl_usd": gross, "fees_usd": fees, "funding_usd": fund,
                        "hold_hours": hold, "reason": reason,
                        "thesis": ("These tenue jusqu\u2019a la sortie." if r > 0
                                   else "These invalidee avant l\u2019objectif.")})
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
    def _pos(sym, side, size, entry, stop, mark, dist, fund, age, hold, th, inv):
        pnl = ((entry - mark) if side == "short" else (mark - entry)) * size
        return {"venue": "hl", "symbol": sym, "side": side, "size": size,
                "entry_px": entry, "stop_px": stop, "stop_dist_pct": dist,
                "notional_usd": round(mark * size, 2),
                "unrealized_pnl_usd": round(pnl, 2), "funding_paid_usd": fund,
                "last_mark_px": mark, "mark_age_s": age,
                "opened_ts": now - 3600 * hold, "hold_h": hold,
                "thesis": th, "invalidation": inv}
    positions = [
        _pos("ETH", "short", 0.1961, 1920.40, 1978.10, 1881.25, 3.00, 0.41, 5760, 19.2,
             "Downtrend daily confirm\u00e9, prix sous MA200 (2019), rejet du haut de range 50h.",
             "Cl\u00f4ture horaire au-dessus de 1978 ou funding > +8 bps/8h."),
        _pos("BTC", "short", 0.0031, 63750.0, 65180.0, 63028.50, 2.24, -0.08, 3420, 6.4,
             "Sous MA200 daily, rejet net du 64.9k, OI plat.",
             "Cl\u00f4ture 1h au-dessus de 65 180."),
        _pos("SOL", "long", 1.42, 74.60, 72.95, 75.37, 2.21, 0.02, 1180, 2.1,
             "Rebond sur le bas de range 50h (74.1), funding neutre.",
             "Perte du 72.95 en cl\u00f4ture horaire."),
    ]
    gates = [
        _gate("dd", "Drawdown", "8.16 %", "20 %", 40.8, "depuis le pic d'equity · au-delà : KILL global"),
        _gate("day", "Perte jour", "−2.59 %", "−4 %", 64.8, "journée UTC"),
        _gate("week", "Perte semaine", "−4.64 %", "−8 %", 58.0, "semaine glissante"),
        _gate("floor", "Plancher equity", "918 $", "300 $", 32.7, "sous le plancher : sorties seulement"),
        _gate("pos", "Positions", "3", "4", 75.0, "concurrentes, tous venues"),
        _gate("gross", "Expo brute", "40 %", "150 %", 26.8, "somme des notionnels / equity"),
        _gate("net", "Expo nette", "40 %", "100 %", 40.2, "|long − short| / equity"),
        _gate("oh", "Ordres / h", "1", "8", 12.5, "anti-boucle folle"),
        _gate("od", "Ordres / j", "6", "30", 20.0, "anti-overtrading"),
        _gate("streak", "Série pertes", "2", "3", 66.7, "au-delà : cooldown imposé"),
        _gate("cooldown", "Cooldown", "inactif", "6 h", 0.0, "s'arme après la série"),
    ]
    journal = _demo_journal()
    edge = compute_edge(journal)
    paliers = compute_paliers(journal)
    cap["paper"]["closed_trades"] = edge["n_closes"]
    cap["paper"]["realized_pnl_usd"] = round(edge["net_usd"], 2)

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
                        f"equity 918.44$ · DD 8.16% · positions {len(positions)}"),
        "kill": {"active": False, "reason": None, "since_iso": None},
        "freshness": {"sync_age_s": 118, "sync_status": "ok",
                      "mark_age_s": 5760, "mark_status": "watch",
                      "mark_note": "position la plus mal marquée", "status": "watch"},
        "capital": cap, "gates": gates, "positions": positions, "edge": edge,
        "paliers": paliers,
        "recent_closes": compute_recent_closes(journal),
        "history": read_history(),
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


def spark_svg(points: list[dict], key: str = "equity", w: int = 640, h: int = 96,
              baseline: float | None = None) -> str:
    """Courbe inline SVG — lisible sans JS, thème cyanotype. Vide si < 2 points."""
    vals = [(float(p["ts"]), float(p[key])) for p in points
            if p.get(key) is not None and p.get("ts")]
    if len(vals) < 2:
        return ""
    xs, ys = [v[0] for v in vals], [v[1] for v in vals]
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    if baseline is not None:
        y0, y1 = min(y0, baseline), max(y1, baseline)
    pad = max((y1 - y0) * 0.12, 0.01)
    y0, y1 = y0 - pad, y1 + pad
    sx = lambda x: 2 + (x - x0) / max(x1 - x0, 1e-9) * (w - 4)
    sy = lambda y: h - 3 - (y - y0) / max(y1 - y0, 1e-9) * (h - 6)
    pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in vals)
    base_line = ""
    if baseline is not None and y0 <= baseline <= y1:
        by = sy(baseline)
        base_line = (f'<line x1="0" y1="{by:.1f}" x2="{w}" y2="{by:.1f}" '
                     f'stroke="#B0801F" stroke-width="1" stroke-dasharray="3 4" opacity=".8"/>')
    last_col = "#7C1D21" if (baseline is not None and ys[-1] < baseline) else "#1F45C8"
    area = f"M{pts.split()[0].split(',')[0]},{h} L{pts.replace(' ', ' L')} L{sx(xs[-1]):.1f},{h} Z"
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" '
            f'role="img" aria-label="courbe {key}">'
            f'<path d="{area}" fill="rgba(31,69,200,.08)"/>{base_line}'
            f'<polyline points="{pts}" fill="none" stroke="#1F45C8" stroke-width="1.6"/>'
            f'<circle cx="{sx(xs[-1]):.1f}" cy="{sy(ys[-1]):.1f}" r="3" fill="{last_col}"/></svg>')


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


/* ---------- paliers de revue ---------- */
.tier{border:1px solid var(--hair);border-left:4px solid var(--cobalt);
  background:rgba(255,255,255,.30);margin-top:14px}
.tier--building{border-left-color:var(--gold);background:rgba(176,128,31,.06)}
.tier--pending{border-left-color:var(--hair);opacity:.72}
.tier-head{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;
  flex-wrap:wrap;padding:11px 14px;border-bottom:1px solid var(--hair)}
.tier--pending .tier-head{border-bottom:0}
.tier-id{display:flex;flex-direction:column;gap:2px;min-width:0}
.tier-n{font-family:var(--sans);font-weight:800;font-size:17px;letter-spacing:-.01em;
  line-height:1.1}
.tier-range{font-size:9px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink-faint)}
.tier-right{display:flex;align-items:center;gap:10px;margin-left:auto}
.tier-verdict{font-size:8.5px;letter-spacing:.16em;font-weight:700;padding:2px 7px;
  border:1px solid currentColor;text-transform:uppercase;color:var(--ink-soft);white-space:nowrap}
.tier-verdict--completed{color:var(--cobalt)}
.tier-verdict--building{color:var(--gold)}
.tier-verdict--pending{color:var(--ink-faint)}
.tier-exp{font-family:var(--sans);font-weight:800;font-size:22px;
  font-variant-numeric:tabular-nums;line-height:1}
.tier-exp.neg{color:var(--oxblood)}
.tier-body{padding:12px 14px 14px}
.tier-fill{display:flex;align-items:center;gap:9px;margin-bottom:12px}
.tier-fill span{flex:1;height:5px;background:rgba(31,69,200,.13);position:relative;display:block}
.tier-fill span::after{content:"";position:absolute;inset:0 auto 0 0;width:var(--w);
  background:var(--cobalt)}
.tier--building .tier-fill span::after{background:var(--gold)}
.tier-fill b{font-size:9.5px;color:var(--ink-soft);font-variant-numeric:tabular-nums}
.tier-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:1px;background:var(--hair);
  border:1px solid var(--hair)}
@media(min-width:560px){.tier-grid{grid-template-columns:repeat(4,1fr)}}
.tier-cell{background:var(--paper-3);padding:8px 10px;min-width:0}
.tier-cell span{display:block;font-size:8px;letter-spacing:.16em;text-transform:uppercase;
  color:var(--ink-faint)}
.tier-cell b{display:block;font-size:13px;font-weight:700;margin-top:2px;
  font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tier-cell b.neg{color:var(--oxblood)}
.tier-read{margin:12px 0 0;font-family:var(--serif);font-style:italic;font-size:13px;
  line-height:1.55;border-left:2px solid var(--cobalt);padding-left:12px}
.tier--building .tier-read{border-left-color:var(--gold)}
.tier-delta{margin-top:9px}
.tier-imp{margin-top:12px;border-top:1px solid var(--hair);padding-top:10px}
.tier-imp ul{margin:6px 0 0;padding-left:18px;font-size:11px;color:var(--ink-soft);line-height:1.6}
.tier-imp-empty{margin-top:12px;border-top:1px solid var(--hair);padding-top:10px}

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

/* ---------- vie : live chip, pouls, sparkline, clôtures ---------- */
.live-chip{display:inline-flex;align-items:center;gap:7px;border:1px solid var(--hair);
  padding:4px 9px;font-size:8.5px;letter-spacing:.16em;text-transform:uppercase;font-weight:700;
  color:var(--ink-soft);background:rgba(242,238,230,.7);font-variant-numeric:tabular-nums}
.live-chip::before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ink-soft)}
.live-chip--on{color:var(--cobalt)}
.live-chip--on::before{background:var(--cobalt);animation:pulse 2.2s ease-out infinite}
.live-chip--stale{color:var(--gold)}.live-chip--stale::before{background:var(--gold)}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(31,69,200,.45)}70%{box-shadow:0 0 0 7px rgba(31,69,200,0)}100%{box-shadow:0 0 0 0 rgba(31,69,200,0)}}
@media(prefers-reduced-motion:reduce){.live-chip--on::before{animation:none}}
.flash{animation:flash .9s ease-out}
@keyframes flash{0%{background:rgba(215,168,58,.35)}100%{background:transparent}}
.delta-up{color:var(--cobalt)}.delta-dn{color:var(--oxblood)}
.curve{margin-top:12px;border:1px solid var(--hair);background:var(--paper-3);padding:16px 18px 12px}
.curve-head{display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.curve-title{font:800 10px var(--sans);letter-spacing:.18em;text-transform:uppercase;color:var(--cobalt)}
.curve-meta{font-size:9px;color:var(--ink-soft);letter-spacing:.08em}
.spark{width:100%;height:96px;display:block}
.closes-tbl .r-pos{color:var(--cobalt);font-weight:700}.closes-tbl .r-neg{color:var(--oxblood);font-weight:700}
.reason-tag{font-size:8px;letter-spacing:.14em;text-transform:uppercase;border:1px solid currentColor;padding:1px 5px;color:var(--ink-soft)}
.reason-tag--stop{color:var(--oxblood)}.reason-tag--thesis{color:var(--cobalt)}
.upd{transition:color .3s ease}
#live-note{font-size:9px;color:var(--ink-soft);margin-top:6px;font-variant-numeric:tabular-nums}
"""

# ---------------------------------------------------------------------------
# Couche vivante — JS embarqué, dégradation propre.
#
# Trois responsabilités, rien d'autre :
#   1. marks live  : POST allMids sur l'API publique Hyperliquid (CORS ouvert,
#                    lecture seule, aucun secret, aucune clé) toutes les 15 s
#                    quand l'onglet est visible → recalcul LOCAL du uPnL des
#                    positions inlinées dans la page. Ce calcul ne touche
#                    aucun fichier : book.json reste la source, la page le dit.
#   2. horloges    : les âges (sync/mark) avancent en continu au lieu d'être
#                    figés à l'instant du tirage.
#   3. rechargement: toutes les 3 min, si un tirage plus récent existe sur le
#                    même URL, la page se remplace — jamais de reload aveugle.
# Hors ligne (fichier local, réseau coupé), la chip dit « page statique » et
# tout le reste fonctionne comme avant : zéro dépendance dure au réseau.
# ---------------------------------------------------------------------------

LIVE_JS = r"""<script>
(function(){
"use strict";
var S; try{ S = JSON.parse(document.getElementById("nabu-state").textContent); }catch(_){ return; }
var chip = document.getElementById("live-chip");
var note = document.getElementById("live-note");
var NBSP = "\u00a0";
function fmt(v, dec){ dec = (dec===undefined?2:dec);
  return v.toLocaleString("en-US",{minimumFractionDigits:dec,maximumFractionDigits:dec}).replace(/,/g,NBSP)+NBSP+"$"; }
function setChip(cls, txt){ if(!chip) return; chip.className = "live-chip"+(cls?" "+cls:""); chip.textContent = txt; }
function put(id, txt, neg){
  var n = document.getElementById(id); if(!n) return;
  if(n.textContent !== txt){ n.textContent = txt;
    n.classList.remove("flash"); void n.offsetWidth; n.classList.add("flash"); }
  if(neg !== undefined) n.classList.toggle("neg", !!neg);
}
function human(s){ s = Math.max(0, s); return s<90 ? Math.round(s)+NBSP+"s"
  : s<5400 ? Math.round(s/60)+NBSP+"min"
  : s<172800 ? (s/3600).toFixed(1)+NBSP+"h" : (s/86400).toFixed(1)+NBSP+"j"; }

/* ---- 1. horloges qui avancent, y compris le silence ---------------- */
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
    v.textContent = human(b + extra);
  });
  var sil = document.getElementById("m-silence");
  if(sil && sil.dataset.since){ sil.textContent = human(Date.now()/1000 - parseFloat(sil.dataset.since)); }
}
setInterval(tickAges, 15000);

/* ---- 2. marks live → uPnL, R par position, risque au stop ---------- */
var rows = Array.prototype.slice.call(document.querySelectorAll(".pos-row"));
var isPaper = String(S.mode||"").toLowerCase()==="paper";
var staticUpnl = (S.positions||[]).reduce(function(a,p){return a+(p.unrealized_pnl_usd||0);},0);
var lastOk = 0, failures = 0;

function applyMids(mids){
  var liveUpnl = 0, rOpen = 0, atRiskUsd = 0, atRiskR = 0, n = 0;
  rows.forEach(function(r){
    var sym = r.dataset.sym, mid = parseFloat(mids[sym]);
    if(!mid || !isFinite(mid)) return;
    var size = parseFloat(r.dataset.size), entry = parseFloat(r.dataset.entry),
        stop = parseFloat(r.dataset.stop), short = r.dataset.side==="short",
        risk = Math.abs(entry-stop);
    var pnl = (short ? (entry-mid) : (mid-entry)) * size;
    var rr  = risk>0 ? (short ? (entry-mid) : (mid-entry))/risk : 0;
    var loss = ((short ? (stop-mid) : (mid-stop))) * size;
    var unit = risk*size;
    liveUpnl += pnl; rOpen += rr; n++;
    atRiskUsd += Math.max(0, loss);
    atRiskR   += unit ? Math.max(0, loss)/unit : 0;

    var cell = r.querySelector(".pos-upnl");
    if(cell){
      var old = cell.textContent;
      cell.textContent = (pnl>=0?"+":"")+pnl.toFixed(2)+NBSP+"$";
      cell.classList.toggle("neg", pnl<0);
      if(old!==cell.textContent){ cell.classList.remove("flash"); void cell.offsetWidth; cell.classList.add("flash"); }
    }
    var rc = r.querySelector(".pos-r");
    if(rc){
      var t = "<b>"+(rr>=0?"+":"")+rr.toFixed(2)+NBSP+"R</b>";
      if(rc.innerHTML!==t){ rc.innerHTML = t;
        rc.classList.remove("flash"); void rc.offsetWidth; rc.classList.add("flash"); }
      rc.classList.toggle("neg", rr<0);
    }
    var mk = r.querySelector(".pos-mark");
    if(mk){ mk.textContent = "live"; mk.classList.add("delta-up"); }
  });
  // Ne pas écraser le header avec des valeurs PARTIELLES :
  // si une position n'est pas résolue dans allMids, on ne met pas à jour le header.
  // Les positions non résolues gardent leur valeur statique du build.
  if(n !== rows.length) return;
  if(!n) return;
  lastOk = Date.now();

  put("m-upnl", fmt(liveUpnl), liveUpnl<0);
  put("m-ropen", (rOpen>=0?"+":"")+rOpen.toFixed(2)+NBSP+"R", rOpen<0);
  put("m-atrisk", "\u2212"+fmt(atRiskUsd), true);
  var an = document.getElementById("m-atrisk-note");
  if(an) an.textContent = atRiskR.toFixed(2)+" R rendus si tous les stops tombent";
  /* equity live = equity du book corrigée du delta de marks — approximation affichée comme telle */
  var eq = (S.capital.equity_usd||0) - staticUpnl + liveUpnl;
  put("m-equity", fmt(eq));
  var dOpen = S.capital.day_open_usd||0;
  if(dOpen>0){
    var dp = (eq-dOpen)/dOpen*100;
    put("m-day", (dp>=0?"+":"")+dp.toFixed(2)+NBSP+"%", dp<0);
  }
  if(note) note.textContent = "marks live Hyperliquid · uPnL et R recalculés localement · " +
    new Date().toISOString().slice(11,19) + "Z · book.json reste la source (" + n + "/" + rows.length + " positions)";
}

/* ---- 3. transport : WebSocket d'abord, repli HTTP ------------------ */
var ws = null, pollTimer = null, retry = 0;

function startPoll(){
  if(pollTimer || location.protocol==="file:") return;
  pollOnce(); pollTimer = setInterval(pollOnce, 15000);
}
function stopPoll(){ if(pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

function pollOnce(){
  if(document.hidden) return;
  var ctl = new AbortController(); var t = setTimeout(function(){ctl.abort();}, 8000);
  fetch("https://api.hyperliquid.xyz/info", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body:'{"type":"allMids"}', signal:ctl.signal, cache:"no-store"
  }).then(function(r){ clearTimeout(t); if(!r.ok) throw 0; return r.json(); })
    .then(function(mids){ failures=0; applyMids(mids);
      if(!ws || ws.readyState!==1) setChip("live-chip--on","LIVE · HTTP"); })
    .catch(function(){ clearTimeout(t); failures++;
      if(failures>=2){ setChip("live-chip--stale", lastOk ? "live perdu · tirage "+S.built_iso.slice(11,16)+"Z" : "page statique"); } });
}

function startWS(){
  if(location.protocol==="file:"){ setChip("", "hors ligne · fichier local"); return; }
  var sock;
  try{ sock = new WebSocket("wss://api.hyperliquid.xyz/ws"); }
  catch(_){ startPoll(); return; }
  ws = sock;
  var opened = false;
  var guard = setTimeout(function(){ if(!opened){ try{sock.close();}catch(_){} } }, 7000);
  sock.onopen = function(){
    opened = true; retry = 0; clearTimeout(guard); stopPoll();
    setChip("live-chip--on","LIVE · WS");
    sock.send(JSON.stringify({method:"subscribe",subscription:{type:"allMids"}}));
  };
  sock.onmessage = function(ev){
    var m; try{ m = JSON.parse(ev.data); }catch(_){ return; }
    if(m && m.channel==="allMids" && m.data && m.data.mids){
      applyMids(m.data.mids); setChip("live-chip--on","LIVE · WS");
    }
  };
  sock.onclose = function(){
    clearTimeout(guard);
    setChip("live-chip--stale", lastOk ? "reconnexion…" : "repli HTTP");
    startPoll();                       /* on ne reste jamais aveugle */
    retry = Math.min(retry+1, 5);
    setTimeout(startWS, 3000 * retry); /* backoff, jamais de boucle serrée */
  };
  sock.onerror = function(){ };
}

/* ---- 4. tirage plus récent → remplacement doux --------------------- */
function checkNewer(){
  if(document.hidden || location.protocol==="file:") return;
  fetch(location.href, {cache:"no-store"}).then(function(r){ return r.text(); })
    .then(function(txt){
      var m = txt.match(/"built_ts":\s*([0-9.]+)/);
      if(m && parseFloat(m[1]) > (S.built_ts||0)+1){ location.reload(); }
    }).catch(function(){});
}

if(rows.length){ startWS(); }
else { setChip("live-chip--on","LIVE · flat"); }
setInterval(checkNewer, 180000);
document.addEventListener("visibilitychange", function(){
  if(!document.hidden){ tickAges(); if(!ws || ws.readyState!==1) pollOnce(); } });
tickAges();
})();
</script>"""


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
    a("<link rel=\"icon\" type=\"image/svg+xml\" href=\"assets/favicon.svg\">")
    a("<link rel=\"icon\" type=\"image/png\" sizes=\"32x32\" href=\"assets/favicon-32x32.png\">")
    a("<link rel=\"icon\" type=\"image/png\" sizes=\"16x16\" href=\"assets/favicon-16x16.png\">")
    a("<link rel=\"apple-touch-icon\" sizes=\"180x180\" href=\"assets/apple-touch-icon.png\">")
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

    def _pos_r(q):
        risk = abs(float(q["entry_px"]) - float(q["stop_px"]))
        if not risk:
            return 0.0
        mk = float(q.get("last_mark_px") or q["entry_px"])
        d = (float(q["entry_px"]) - mk) if q["side"] == "short" else (mk - float(q["entry_px"]))
        return d / risk

    r_open = sum(_pos_r(q) for q in positions)
    at_risk_usd, at_risk_r = 0.0, 0.0
    for q in positions:
        mk = float(q.get("last_mark_px") or q["entry_px"])
        loss = ((float(q["stop_px"]) - mk) if q["side"] == "short"
                else (mk - float(q["stop_px"]))) * float(q["size"])
        unit = abs(float(q["entry_px"]) - float(q["stop_px"])) * float(q["size"])
        at_risk_usd += max(0.0, loss)
        at_risk_r += (max(0.0, loss) / unit) if unit else 0.0
    last_close_ts = next((c.get("ts") for c in (S.get("recent_closes") or []) if c.get("ts")), None)
    a("<section class=\"overview\" id=\"portfolio\" aria-label=\"Synthèse du portefeuille\">")
    a(f"<div class=\"overview-head\"><div class=\"overview-title\">Portefeuille · maintenant</div>"
      f"<div style=\"display:flex;gap:8px;align-items:center\">"
      f"<span class=\"live-chip\" id=\"live-chip\" title=\"prix Hyperliquid en direct — recalcul local du uPnL\">page statique</span>"
      f"<span class=\"health health--{e(fr['status'])}\">{e(health_label)}</span></div></div>")
    a("<div class=\"overview-grid\">")
    metrics = [
        ("Equity", money(cap["equity_usd"]), f"pic {money(cap['peak_usd'], 0)}", False, "m-equity"),
        ("Aujourd'hui", f"{cap['day_pnl_pct']:+.2f}{NB}%", "performance UTC", cap["day_pnl_pct"] < 0, "m-day"),
        ("uPnL ouvert", money(upnl), f"{len(positions)} position{'s' if len(positions) != 1 else ''}", upnl < 0, "m-upnl"),
        ("PnL réalisé", money(realized), "net des clôtures", realized < 0, "m-realized"),
        ("R ouvert", f"{r_open:+.2f}{NB}R", "somme des R en cours", r_open < 0, "m-ropen"),
        ("Risque au stop", "−" + money(at_risk_usd),
         f"{at_risk_r:.2f} R rendus si tous les stops tombent", True, "m-atrisk"),
        ("Risque max", risk_txt, risk_note, bool(max_gate and max_gate["status"] in ("hot", "breach")), "m-risk"),
    ]
    for kk, vv, nn, bad, mid in metrics:
        a(f"<div class=\"metric\"><div class=\"metric-k\">{e(kk)}</div>"
          f"<div class=\"metric-v upd{' neg' if bad else ''}\" id=\"{mid}\">{e(vv)}</div>"
          f"<div class=\"metric-n\"{' id=\"m-atrisk-note\"' if mid == 'm-atrisk' else ''}>"
          f"{e(nn)}</div></div>")
    a(f"</div><div class=\"decision\"><b>Lecture</b><span>{e(v)}</span></div>")
    if last_close_ts:
        a(f"<p class=\"note\" style=\"margin-top:9px\">Dernière clôture il y a "
          f"<b id=\"m-silence\" data-since=\"{float(last_close_ts):.0f}\">"
          f"{e(dur(time.time() - float(last_close_ts)))}</b> · le silence n'est pas une panne : "
          f"flat est une position.</p>")
    a("<div id=\"live-note\"></div></section>")

    # -- courbe d'equity — la mémoire de la page
    hist = S.get("history") or []
    curve = spark_svg(hist, "equity", baseline=float((cap.get("paper") or {}).get("start_equity_usd") or 0) or None)
    if curve:
        first, lastp = hist[0], hist[-1]
        span_h = (float(lastp["ts"]) - float(first["ts"])) / 3600
        span_txt = f"{span_h / 24:.1f} j" if span_h > 48 else f"{span_h:.0f} h"
        a(f"<div class=\"curve\"><div class=\"curve-head\"><span class=\"curve-title\">Courbe d'equity</span>"
          f"<span class=\"curve-meta\">{len(hist)} points · {e(span_txt)} · trait or = capital initial · "
          f"dernier point {money(float(lastp.get('equity') or 0))}</span></div>{curve}</div>")

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
          "<th>Venue</th><th>Sym</th><th>Sens</th><th class=\"num\">R</th>"
          "<th class=\"num\">Notionnel</th>"
          "<th class=\"num\">Entrée</th><th class=\"num\">Stop</th><th class=\"num\">uPnL</th>"
          "<th class=\"num\">Funding</th><th class=\"num\">Portage</th><th class=\"num\">Mark</th>"
          "</tr></thead><tbody>")
        for pos in S["positions"]:
            up = pos["unrealized_pnl_usd"]
            sd = f"{pos['stop_dist_pct']:.2f}{NB}%" if pos.get("stop_dist_pct") else "—"
            rp = _pos_r(pos)
            a(f"<tr class=\"pos-row\" data-sym=\"{e(pos['symbol'])}\" data-side=\"{e(pos['side'])}\" "
              f"data-size=\"{pos['size']}\" data-entry=\"{pos['entry_px']}\" data-stop=\"{pos['stop_px']}\">"
              f"<td>{e(pos['venue'])}</td><td><b>{e(pos['symbol'])}</b></td>"
              f"<td><span class=\"side side--{e(pos['side'])}\">{e(pos['side'])}</span></td>"
              f"<td class=\"num pos-r{' neg' if rp < 0 else ''}\"><b>{rp:+.2f}{NB}R</b></td>"
              f"<td class=\"num\">{e(money(pos['notional_usd'], 0))}</td>"
              f"<td class=\"num\">{pos['entry_px']:,.2f}</td>"
              f"<td class=\"num\">{pos['stop_px']:,.2f}<br><span class=\"step-lim\">{e(sd)}</span></td>"
              f"<td class=\"num upd pos-upnl{' neg' if up < 0 else ''}\">{up:+.2f}{NB}$</td>"
              f"<td class=\"num\">{pos['funding_paid_usd']:+.3f}{NB}$</td>"
              f"<td class=\"num\">{(pos['hold_h'] or 0):.1f}{NB}h</td>"
              f"<td class=\"num upd pos-mark\">{e(dur(pos.get('mark_age_s')))}</td></tr>")
        a("</tbody></table></div>")
        for pos in S["positions"]:
            if pos.get("thesis") or pos.get("invalidation"):
                a(f"<p class=\"thesis\"><b>{e(pos['symbol'])}</b> — thèse : {e(pos['thesis'] or '—')}"
                  f"<br>Invalidation : {e(pos['invalidation'] or '—')}</p>")
    else:
        a("<p class=\"empty\">Flat. C'est une position, pas une absence de position.</p>")
    a("</section>")

    # -- edge · deux étages : tout depuis le début, puis palier par palier
    a("<details class=\"drawer\" id=\"analysis\"><summary>Performance statistique de N*ABU</summary>"
      "<div class=\"drawer-body\">")

    # ===== ÉTAGE 1 — depuis le début ========================================
    a("<section class=\"sec\"><div class=\"eyebrow\">Depuis le début — toutes les clôtures</div>")
    a("<div class=\"rule rule--thick\"></div>")
    n, tgt = edge["n_closes"], edge["target_trades"]
    if edge["verified"]:
        a(f"<span class=\"stamp stamp--ok\">Verified · {n} trades clos</span>")
    else:
        a(f"<span class=\"stamp\">Unverified · {n} / {tgt} trades clos</span>")
    pct_g0 = min(100.0, n / tgt * 100 if tgt else 0)
    a(f"<div class=\"progress\"><span style=\"--w:{pct_g0:.1f}%\"></span></div>")
    a(f"<p class=\"note\" style=\"margin-top:8px\">Sortie du gate G0 : {tgt} trades journalisés. "
      "En dessous, ces chiffres décrivent un échantillon, pas un edge — l'intervalle le dit "
      "mieux que la moyenne. Ce bloc agrège <b>tout depuis le premier trade</b> ; les paliers "
      "sont évalués séparément plus bas.</p>")

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
                cls = " hbar--neg" if i < 3 else ""
                a(f"<div class=\"hbar{cls}\" style=\"height:{h['n'] / mx * 100:.1f}%\" "
                  f"title=\"{h['n']} trades\"></div>")
            a("</div><div class=\"hlab\">")
            for h in hist:
                a(f"<div>{e(h['label'])}<br>{h['n']}</div>")
            a("</div>")
            a("<p class=\"note\" style=\"margin-top:10px\">Distribution des R sur l'ensemble. "
              "Une espérance portée par une seule barre à droite n'est pas un edge, "
              "c'est un trade.</p>")

        rec = edge.get("expectancy_r_recent")
        if rec is not None:
            drift = ""
            if edge.get("expectancy_r") is not None:
                drift = f" · dérive vs global {rec - edge['expectancy_r']:+.2f} R"
            a(f"<p class=\"note\" style=\"margin-top:8px\"><b>Espérance récente "
              f"({edge.get('recent_window')} derniers) : {rec:+.2f} R</b>{e(drift)} — "
              "c'est la fenêtre qui meurt en premier quand l'edge décède.</p>")

        closes_r = S.get("recent_closes") or []
        if closes_r:
            a("<div class=\"eyebrow\" style=\"margin-top:26px\">Dernières clôtures — matière du "
              "post-mortem</div><div class=\"rule\"></div>"
              "<div class=\"wrap\"><table class=\"tbl closes-tbl\"><thead><tr>"
              "<th>Quand</th><th>Sym</th><th>Sens</th><th class=\"num\">R</th>"
              "<th class=\"num\">PnL net</th><th class=\"num\">Portage</th><th>Sortie</th>"
              "<th>Thèse</th></tr></thead><tbody>")
            for c in closes_r:
                r_val = c.get("r_multiple")
                r_txt = f"{float(r_val):+.2f}" if r_val is not None else "—"
                r_cls = "" if r_val is None else (" r-pos" if float(r_val) > 0 else " r-neg")
                pnl = float(c.get("realized_pnl_usd") or 0)
                when = c.get("iso") or (time.strftime("%m-%d %H:%M", time.gmtime(float(c["ts"])))
                                        if c.get("ts") else "—")
                if isinstance(when, str) and "T" in when:
                    when = when[5:16].replace("T", " ")
                reason = str(c.get("reason") or "—")
                th = (c.get("thesis") or "—")
                if len(th) > 70:
                    th = th[:67] + "…"
                a(f"<tr><td class=\"num\" style=\"text-align:left\">{e(when)}</td>"
                  f"<td><b>{e(c.get('symbol'))}</b></td>"
                  f"<td><span class=\"side side--{e(c.get('side'))}\">{e(c.get('side'))}</span></td>"
                  f"<td class=\"num{r_cls}\">{e(r_txt)}</td>"
                  f"<td class=\"num{' neg' if pnl < 0 else ''}\">{pnl:+.2f}{NB}$</td>"
                  f"<td class=\"num\">{float(c.get('hold_hours') or 0):.1f}{NB}h</td>"
                  f"<td><span class=\"reason-tag reason-tag--{e(reason)}\">{e(reason)}</span></td>"
                  f"<td class=\"note\">{e(th)}</td></tr>")
            a("</tbody></table></div>")
    else:
        a("<p class=\"empty\">Aucun trade clos non-artefact dans le journal. Rien à mesurer, "
          "rien à inventer.</p>")
    a("</section>")

    # ===== ÉTAGE 2 — paliers de 30 trades ===================================
    paliers = S.get("paliers") or []
    if paliers:
        a(f"<section class=\"sec\"><div class=\"eyebrow\">Évaluation par paliers de {tgt} trades</div>")
        a("<div class=\"rule rule--thick\"></div>")
        a(f"<p class=\"note\">Chaque palier est mesuré <b>sur ses {tgt} clôtures seulement</b>, "
          "jamais sur le cumul : c'est ce qui rend deux paliers comparables. La revue est "
          "déclenchée par un nombre de trades, pas par le calendrier. Une seule hypothèse "
          "testée par cycle, en paper, validation humaine avant tout passage live.</p>")

        for pl in paliers:
            st = pl["status"]
            exp_p = pl.get("expectancy_r")
            a(f"<article class=\"tier tier--{e(st)}\">")
            # -- en-tête du palier
            a("<header class=\"tier-head\"><div class=\"tier-id\">"
              f"<span class=\"tier-n\">{e(pl['label'])}</span>"
              f"<span class=\"tier-range\">{e(pl['range_txt'])}"
              f"{' · ' + e(str(pl['first_iso'])[:10]) if pl.get('first_iso') else ''}"
              f"{' → ' + e(str(pl['last_iso'])[:10]) if pl.get('last_iso') else ''}</span></div>"
              f"<div class=\"tier-right\">"
              f"<span class=\"tier-verdict tier-verdict--{e(st)}\">{e(pl['verdict'])}</span>"
              f"<span class=\"tier-exp{' neg' if (exp_p or 0) < 0 else ''}\">"
              f"{(f'{exp_p:+.2f}' + NB + 'R') if exp_p is not None else '—'}</span>"
              "</div></header>")

            if st == "pending":
                a(f"<div class=\"tier-body\"><p class=\"note\">{e(pl['reading'])}</p></div>")
                a("</article>")
                continue

            a("<div class=\"tier-body\">")
            # -- barre de remplissage du palier
            fill = min(100.0, pl["n_trades"] / tgt * 100)
            a(f"<div class=\"tier-fill\"><span style=\"--w:{fill:.1f}%\"></span>"
              f"<b>{pl['n_trades']} / {tgt}</b></div>")
            # -- métriques compactes, corps volontairement plus petit que l'étage 1
            ci_p = pl.get("ci95")
            tcells = [
                ("IC95", f"{ci_p[0]:+.2f} … {ci_p[1]:+.2f}" if ci_p else "—"),
                ("Taux de gain", f"{pl['win_rate_pct']:.0f}{NB}%" if pl["win_rate_pct"] is not None else "—"),
                ("Gain moyen", f"{pl['avg_win_r']:+.2f}{NB}R" if pl["avg_win_r"] is not None else "—"),
                ("Perte moyenne", f"{pl['avg_loss_r']:+.2f}{NB}R" if pl["avg_loss_r"] is not None else "—"),
                ("Meilleur / pire",
                 f"{pl['best_r']:+.2f} / {pl['worst_r']:+.2f}" if pl["best_r"] is not None else "—"),
                ("Sorties au stop",
                 f"{pl['stop_share_pct']:.0f}{NB}%" if pl["stop_share_pct"] is not None else "—"),
                ("Portage médian",
                 f"{pl['median_hold_h']:.1f}{NB}h" if pl["median_hold_h"] is not None else "—"),
                ("PnL net du palier", money(pl["net_usd"])),
            ]
            a("<div class=\"tier-grid\">")
            for kk, vv in tcells:
                bad = kk == "Perte moyenne" and (pl.get("avg_loss_r") or 0) < -1.02
                a(f"<div class=\"tier-cell\"><span>{e(kk)}</span>"
                  f"<b class=\"{'neg' if bad else ''}\">{e(vv)}</b></div>")
            a("</div>")
            # -- lecture + comparaison au palier précédent
            a(f"<p class=\"tier-read\">{e(pl['reading'])}</p>")
            d = pl.get("delta_r")
            if d is not None:
                word = "au-dessus" if d > 0 else "en dessous"
                a(f"<p class=\"note tier-delta\">Écart au palier précédent : "
                  f"<b class=\"{'neg' if d < 0 else ''}\">{d:+.2f}{NB}R</b> — ce palier est "
                  f"{word}. C'est la seule comparaison qui a du sens : deux échantillons de "
                  f"même taille, mesurés séparément.</p>")
            # -- améliorations, uniquement si nabu_milestone.py en a produit
            if pl.get("improvements"):
                a("<div class=\"tier-imp\"><div class=\"eyebrow\">Améliorations proposées</div>"
                  "<ul>")
                for imp in pl["improvements"][:5]:
                    a(f"<li>{e(imp)}</li>")
                a("</ul></div>")
            elif st == "completed":
                a("<p class=\"note tier-imp-empty\">Aucune amélioration enregistrée pour ce "
                  "palier — <b>nabu_milestone.py</b> n'a rien produit. Une case vide vaut mieux "
                  "qu'une recommandation inventée ici.</p>")
            a("</div></article>")
        a("</section>")
    a("</div></details>")

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
    a("</script>")
    a(LIVE_JS)
    a("</body></html>")
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
    if a.cmd == "build":
        append_history(state)        # trace de la courbe d'equity + score

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