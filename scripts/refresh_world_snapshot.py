#!/usr/bin/env python3
"""Rebuild assets/world-live.json from world-paper ledgers.

Read-only on ledgers. Writes only the snapshot JSON (or stdout).
Does not call PayBox, World.xyz, or any network venue.

    # from a live ledger tree
    python3 scripts/refresh_world_snapshot.py \
        --ledgers /opt/data/.nabu/world-paper \
        --out assets/world-live.json

    # rewrite the checked-in example
    python3 scripts/refresh_world_snapshot.py --write-example

Expected ledger files (any missing file is UNVERIFIED, never zero-filled):

    $NABU_WORLD_ROOT/fills.jsonl
    $NABU_WORLD_ROOT/autonomy_cycle.json
    $NABU_WORLD_ROOT/positions.json      # or positions.jsonl
    $NABU_WORLD_ROOT/pending_geofence.json   # or awaiting_region_check.json

The live score / autonomy pipeline MUST, on every real prepared buy:

    (a) write assets/geofence-latest.html from the phone Check région page
    (b) set pending_geofence.check_region_url / geofence_url to the short
        Pages https link, e.g.
        https://enudimmud.github.io/N-ABU-Dashboard/assets/geofence-latest.html
        (prefer this over a huge data:text/html blob)
    (c) rewrite / push assets/world-live.json so WD notifies with that URL

Without this refresh, the dashboard cannot put the Check région URL inside
the browser notification.

Field aliases are accepted (ts/timestamp, tx/signature, track/book, …).
Hard cap is a single pool: max 8 tickets × $5 = $40 (`capacity.max_open_usd`).
A/B are labels only (not independent $40 hard caps). Per-track max_usd in
example snapshots is a soft share, never summed into utilisation.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "assets" / "world-live.json"
DEFAULT_LEDGERS = Path(os.environ.get("NABU_WORLD_ROOT", "/opt/data/.nabu/world-paper"))
WALLET = "27bcZ8xT8qWzkmdyjKy7mRXKqRAR9KBphZt3BMyjmac3"
TICKET_USD = 5.0
MAX_OPEN = 8
MAX_OPEN_USD = MAX_OPEN * TICKET_USD
CAP_A = 10.0  # soft example share — not a hard per-track cap
CAP_B = 15.0

EXAMPLE = {
    "schema_version": 1,
    "example": True,
    "generated_at": "2026-09-12T16:40:00Z",
    "source": "example snapshot — replace by regenerating from world-paper ledgers",
    "venue": "world.xyz",
    "wallet": {"chain": "solana", "address": WALLET, "label": "PayBox"},
    "mode": "LIVE_ONLY",
    "ticket_usd": TICKET_USD,
    "capacity": {
        "max_open": MAX_OPEN,
        "max_open_usd": MAX_OPEN_USD,
        "ticket": TICKET_USD,
        "label_only": True,
        "note": "hard cap = max_open_usd; A/B are labels only",
    },
    "caps": {
        "A": {"open_usd": 5.0, "max_usd": CAP_A, "label_only": True},
        "B": {"open_usd": 10.0, "max_usd": CAP_B, "label_only": True},
        "note": "A/B labels — do not sum max_usd",
    },
    "positions": [
        {
            "market": "Will the FOMC hold rates at the September meeting?",
            "track": "A", "side": "YES", "size_usd": 5.0, "mark": 0.64, "entry": 0.58,
            "mint": "So1FomcSeptHoldYes111111111111111111111111",
            "ticker": "FOMC-HOLD-SEP", "opened_at": "2026-09-11T18:12:04Z",
        },
        {
            "market": "BTC above $80k by month end?",
            "track": "B", "side": "NO", "size_usd": 5.0, "mark": 0.47, "entry": 0.51,
            "mint": "So1Btc80kNo222222222222222222222222222222",
            "ticker": "BTC-80K-EOM", "opened_at": "2026-09-10T21:04:41Z",
        },
        {
            "market": "SOL spot ETF decision published this week?",
            "track": "B", "side": "YES", "size_usd": 5.0, "mark": 0.31, "entry": 0.28,
            "mint": "So1SolEtfYes33333333333333333333333333333",
            "ticker": "SOL-ETF-WK", "opened_at": "2026-09-12T09:17:22Z",
        },
    ],
    "fills": [
        {
            "ts": "2026-09-12T09:17:22Z", "action": "open",
            "market": "SOL spot ETF decision published this week?",
            "track": "B", "side": "YES", "size_usd": 5.0, "price": 0.28, "pnl_usd": None,
            "tx": "5vNabuExamp1eTxOpenSo1Etf111111111111111111111111111111111111",
            "ticker": "SOL-ETF-WK", "note": "$5 ticket",
        },
        {
            "ts": "2026-09-11T18:12:04Z", "action": "open",
            "market": "Will the FOMC hold rates at the September meeting?",
            "track": "A", "side": "YES", "size_usd": 5.0, "price": 0.58, "pnl_usd": None,
            "tx": "4kNabuExamp1eTxOpenFomc222222222222222222222222222222222222",
            "ticker": "FOMC-HOLD-SEP", "note": "$5 ticket",
        },
        {
            "ts": "2026-09-11T07:44:18Z", "action": "close",
            "market": "ETH above $2,600 this week?",
            "track": "A", "side": "YES", "size_usd": 5.0, "price": 0.71, "pnl_usd": 1.15,
            "tx": "3mNabuExamp1eTxCloseEth33333333333333333333333333333333333",
            "ticker": "ETH-2600-WK", "note": "settled / closed",
        },
        {
            "ts": "2026-09-10T21:04:41Z", "action": "open",
            "market": "BTC above $80k by month end?",
            "track": "B", "side": "NO", "size_usd": 5.0, "price": 0.51, "pnl_usd": None,
            "tx": "2bNabuExamp1eTxOpenBtc444444444444444444444444444444444444",
            "ticker": "BTC-80K-EOM", "note": "$5 ticket",
        },
    ],
    "cashflow": {
        "realized_pnl_usd": 1.15, "unrealized_pnl_usd": 0.4, "fees_usd": 0.12,
        "net_usd": 1.43, "tickets_opened": 4, "tickets_closed": 1, "volume_usd": 20.0,
    },
    "autonomy": {
        "cycle_id": "cycle-14",
        "evaluated_at": "2026-09-12T16:35:00Z",
        "note": (
            "Ticket ETH-2700-WK prepared on A. Awaiting CH Check région / geofence "
            "before the fill. Do not let the chat URL expire."
        ),
    },
    "pending_geofence": {
        "status": "awaiting_region_check",
        "market": "Will ETH close above $2,700 this week?",
        "track": "A",
        "side": "YES",
        "size_usd": 5.0,
        "ticker": "ETH-2700-WK",
        "request_id": "ch-check-20260912-1844-a1",
        "prepared_at": "2026-09-12T17:44:00Z",
        "expires_at": "2026-09-13T18:00:00Z",
        "geofence_url": (
            "data:text/plain;charset=utf-8,"
            "CH%20Check%20region%0A"
            "request_id=ch-check-20260912-1844-a1%0A"
            "market=ETH-2700-WK%0A"
            "track=A%0A"
            "size_usd=5%0A"
            "open_this_url_to_clear_geofence"
        ),
        "note": "CH Check région — ticket prêt. Ouvrir l'URL geofence du chat avant expiry du token.",
    },
}


def _pick(row: dict, keys: list[str], default: Any = None) -> Any:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return default


def _num(v: Any) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict]:
    rows: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            rows.append(rec)
    return rows


def _norm_position(row: dict) -> dict:
    return {
        "market": _pick(row, ["market", "question", "title"]),
        "track": _pick(row, ["track", "book"]),
        "side": _pick(row, ["side", "outcome"]),
        "size_usd": _num(_pick(row, ["size_usd", "size", "notional", "ticket"])),
        "mark": _num(_pick(row, ["mark", "mark_px", "price"])),
        "entry": _num(_pick(row, ["entry", "entry_px"])),
        "mint": _pick(row, ["mint"]),
        "ticker": _pick(row, ["ticker", "symbol"]),
        "opened_at": _pick(row, ["opened_at", "ts", "iso"]),
    }


def _norm_fill(row: dict) -> dict:
    return {
        "ts": _pick(row, ["ts", "iso", "timestamp", "time"]),
        "action": _pick(row, ["action", "type", "event"]),
        "market": _pick(row, ["market", "question", "title"]),
        "track": _pick(row, ["track", "book"]),
        "side": _pick(row, ["side", "outcome"]),
        "size_usd": _num(_pick(row, ["size_usd", "size", "notional", "ticket"])),
        "price": _num(_pick(row, ["price", "px", "mark"])),
        "pnl_usd": _num(_pick(row, ["pnl_usd", "realized_pnl_est", "realized_pnl", "pnl"])),
        "tx": _pick(row, ["tx", "tx_hash", "signature", "sig", "cashout_tx"]),
        "ticker": _pick(row, ["ticker", "symbol"]),
        "note": _pick(row, ["note", "reason"]),
    }


def _load_positions(root: Path) -> tuple[list[dict], str]:
    for name in ("positions.json", "positions.jsonl"):
        p = root / name
        if not p.exists():
            continue
        if p.suffix == ".jsonl":
            raw = _read_jsonl(p)
        else:
            data = _read_json(p)
            raw = data.get("positions", data) if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raw = []
        return [_norm_position(r) for r in raw if isinstance(r, dict)], str(p)
    return [], "absent"


def _load_fills(root: Path) -> tuple[list[dict], str]:
    p = root / "fills.jsonl"
    if not p.exists():
        return [], "absent"
    fills = [_norm_fill(r) for r in _read_jsonl(p)]
    fills.sort(key=lambda r: str(r.get("ts") or ""), reverse=True)
    return fills[:40], str(p)


def _norm_pending(row: dict) -> dict:
    check_url = _pick(row, ["check_region_url", "pages_url", "region_check_url"])
    geo_url = _pick(row, ["geofence_url", "url", "data_url", "link", "href"])
    return {
        "status": _pick(row, ["status", "state"], "awaiting_region_check"),
        "market": _pick(row, ["market", "question", "title"]),
        "track": _pick(row, ["track", "book"]),
        "side": _pick(row, ["side", "outcome"]),
        "size_usd": _num(_pick(row, ["size_usd", "size", "ticket", "notional"])),
        "ticker": _pick(row, ["ticker", "symbol"]),
        "request_id": _pick(row, ["request_id", "id", "token_id"]),
        "prepared_at": _pick(row, ["prepared_at", "ts", "iso"]),
        "expires_at": _pick(row, ["expires_at", "token_expiry", "expiry", "expires"]),
        "geofence_url": geo_url or check_url,
        "check_region_url": check_url,
        "note": _pick(row, ["note", "hint", "chat_hint"]),
    }


def _as_pending_list(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if isinstance(raw, dict):
        raw = raw.get("pending") or raw.get("tickets") or raw.get("items") or raw
        if isinstance(raw, dict):
            return [_norm_pending(raw)]
    if isinstance(raw, list):
        return [_norm_pending(r) for r in raw if isinstance(r, dict)]
    return []


def _load_pending(root: Path, auto: dict) -> tuple[list[dict], str]:
    for name in ("pending_geofence.json", "awaiting_region_check.json"):
        p = root / name
        if not p.exists():
            continue
        try:
            data = _read_json(p)
        except Exception:  # noqa: BLE001
            return [], str(p)
        rows = _as_pending_list(data)
        return rows, str(p)
    if auto:
        for key in ("pending_geofence", "pending_geofences", "awaiting_region_check"):
            if auto.get(key):
                return _as_pending_list(auto.get(key)), "autonomy_cycle.json"
    return [], "absent"


def _load_autonomy(root: Path) -> tuple[dict, str]:
    p = root / "autonomy_cycle.json"
    if not p.exists():
        return {}, "absent"
    data = _read_json(p)
    if not isinstance(data, dict):
        return {}, str(p)
    note = _pick(data, ["note", "eval", "summary", "next_action"])
    return {
        "cycle_id": _pick(data, ["cycle_id", "id", "cycle"]),
        "evaluated_at": _pick(data, ["evaluated_at", "ts", "iso"]),
        "note": note,
        "mode": _pick(data, ["mode"], "LIVE_ONLY"),
        "caps": data.get("caps") if isinstance(data.get("caps"), dict) else None,
        "cashflow": data.get("cashflow") if isinstance(data.get("cashflow"), dict) else None,
        "pending_geofence": data.get("pending_geofence"),
        "pending_geofences": data.get("pending_geofences"),
        "awaiting_region_check": data.get("awaiting_region_check"),
    }, str(p)


def _open_by_track(positions: list[dict]) -> dict[str, float]:
    out = {"A": 0.0, "B": 0.0}
    for p in positions:
        track = str(p.get("track") or "").upper()
        size = p.get("size_usd")
        if track in out and size is not None:
            out[track] += float(size)
    return out


def _cashflow_from_fills(fills: list[dict], positions: list[dict], explicit: dict | None) -> dict:
    if explicit:
        return explicit
    realized = 0.0
    fees = 0.0
    opened = closed = 0
    volume = 0.0
    have_realized = False
    for f in fills:
        action = str(f.get("action") or "").lower()
        size = f.get("size_usd") or 0.0
        volume += abs(float(size))
        if action in ("open", "buy"):
            opened += 1
        if action in ("close", "sell", "settle"):
            closed += 1
        if f.get("pnl_usd") is not None:
            realized += float(f["pnl_usd"])
            have_realized = True
    unreal = None
    marks = [p for p in positions if p.get("mark") is not None and p.get("entry") is not None and p.get("size_usd")]
    if marks:
        unreal = 0.0
        for p in marks:
            # binary-style: pnl ≈ (mark - entry) * size  (YES long); NO already encoded by side price
            unreal += (float(p["mark"]) - float(p["entry"])) * float(p["size_usd"])
    net = None
    if have_realized:
        net = realized + (unreal or 0.0) - fees
    return {
        "realized_pnl_usd": round(realized, 4) if have_realized else None,
        "unrealized_pnl_usd": round(unreal, 4) if unreal is not None else None,
        "fees_usd": None,
        "net_usd": round(net, 4) if net is not None else None,
        "tickets_opened": opened or None,
        "tickets_closed": closed or None,
        "volume_usd": round(volume, 4) if fills else None,
    }


def build_snapshot(ledgers: Path) -> dict:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    positions, pos_src = _load_positions(ledgers)
    fills, fill_src = _load_fills(ledgers)
    auto, auto_src = _load_autonomy(ledgers)
    pending, pend_src = _load_pending(ledgers, auto)
    open_usd = _open_by_track(positions)
    caps_src = auto.get("caps") if auto else None
    caps = {"A": {"open_usd": open_usd["A"], "max_usd": CAP_A, "label_only": True},
            "B": {"open_usd": open_usd["B"], "max_usd": CAP_B, "label_only": True},
            "note": "A/B labels — do not sum max_usd"}
    if isinstance(caps_src, dict):
        for key in ("A", "B"):
            raw = caps_src.get(key) or {}
            if isinstance(raw, dict) and raw.get("max_usd") is not None:
                caps[key]["max_usd"] = float(raw["max_usd"])
            elif isinstance(raw, (int, float)):
                caps[key]["max_usd"] = float(raw)
            if isinstance(raw, dict) and raw.get("label_only") is False:
                caps[key]["label_only"] = False
    sources = [s for s in (pos_src, fill_src, auto_src, pend_src) if s != "absent"]
    pending_out: Any
    if len(pending) == 1:
        pending_out = pending[0]
    elif len(pending) > 1:
        pending_out = pending
    else:
        pending_out = None
    return {
        "schema_version": 1,
        "example": False,
        "generated_at": now,
        "source": ", ".join(sources) if sources else f"{ledgers} (no ledger files)",
        "venue": "world.xyz",
        "wallet": {"chain": "solana", "address": WALLET, "label": "PayBox"},
        "mode": (auto.get("mode") if auto else None) or "LIVE_ONLY",
        "ticket_usd": TICKET_USD,
        "capacity": {
            "n_open": sum(1 for p in positions if p),
            "max_open": MAX_OPEN,
            "max_open_usd": MAX_OPEN_USD,
            "remaining_tickets": max(0, MAX_OPEN - sum(1 for p in positions if p)),
            "ticket": TICKET_USD,
            "A_open": open_usd["A"],
            "B_open": open_usd["B"],
            "label_only": True,
            "note": "hard cap = max_open_usd (max_open × ticket); A/B are labels only",
        },
        "caps": caps,
        "positions": positions,
        "fills": fills,
        "cashflow": _cashflow_from_fills(fills, positions, auto.get("cashflow") if auto else None),
        "autonomy": {
            "cycle_id": auto.get("cycle_id"),
            "evaluated_at": auto.get("evaluated_at"),
            "note": auto.get("note"),
        } if auto else {},
        "pending_geofence": pending_out,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Refresh World tab snapshot (read-only on ledgers).")
    ap.add_argument("--ledgers", default=str(DEFAULT_LEDGERS),
                    help="world-paper ledger directory (fills.jsonl, autonomy_cycle.json, positions)")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="snapshot JSON path")
    ap.add_argument("--stdout", action="store_true", help="print JSON instead of writing --out")
    ap.add_argument("--write-example", action="store_true",
                    help="write the checked-in example snapshot (ignore ledgers)")
    args = ap.parse_args()

    snap = EXAMPLE if args.write_example else build_snapshot(Path(args.ledgers))
    text = json.dumps(snap, ensure_ascii=False, indent=2) + "\n"
    if args.stdout:
        sys.stdout.write(text)
        return 0
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    print(f"WORLD SNAPSHOT · {out} · mode {snap.get('mode')} · "
          f"positions {len(snap.get('positions') or [])} · "
          f"fills {len(snap.get('fills') or [])} · "
          f"{'EXAMPLE' if snap.get('example') else snap.get('source')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
