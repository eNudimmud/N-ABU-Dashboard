#!/usr/bin/env python3
"""World tab stays additive: snapshot format, refresh script, generator hook."""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))

import refresh_world_snapshot as rws  # noqa: E402


class SnapshotExample(unittest.TestCase):
    def test_checked_in_live_snapshot_has_no_fake_pending(self):
        snap = json.loads((ROOT / "assets" / "world-live.json").read_text())
        self.assertFalse(snap.get("example"))
        self.assertEqual(snap.get("mode") or "LIVE_ONLY", "LIVE_ONLY")
        if snap.get("wallet"):
            self.assertEqual(snap["wallet"]["address"], rws.WALLET)
        self.assertIsNone(snap.get("pending_geofence"))
        self.assertFalse(snap.get("awaiting_region_check"))

    def test_refresh_example_is_demo_plain_text_url(self):
        pend = rws.EXAMPLE["pending_geofence"]
        self.assertTrue(rws.EXAMPLE["example"])
        self.assertTrue(pend["request_id"].startswith("ch-check-"))
        self.assertTrue(pend["ticker"].startswith("ETH-2700-WK"))
        self.assertTrue(pend["geofence_url"].startswith("data:text/plain"))


class RefreshFromLedgers(unittest.TestCase):
    def test_builds_snapshot_from_ledger_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "fills.jsonl").write_text(
                json.dumps({
                    "timestamp": "2026-09-12T10:00:00Z",
                    "type": "open",
                    "question": "Demo market",
                    "book": "A",
                    "outcome": "YES",
                    "ticket": 5,
                    "signature": "SigExample111",
                }) + "\n" + json.dumps({
                    "ts": "2026-09-12T11:00:00Z",
                    "action": "close",
                    "market": "Demo market",
                    "track": "A",
                    "side": "YES",
                    "size_usd": 5,
                    "pnl_usd": 0.8,
                    "tx": "SigExample222",
                }) + "\n" + json.dumps({
                    "ts": "2026-09-12T12:00:00Z",
                    "action": "close",
                    "market": "Cashed market",
                    "track": "A",
                    "size_usd": 5,
                    "realized_pnl_est": 2.85,
                    "cashout_tx": "CashoutSig333",
                    "note": "settled_cashed",
                }) + "\n",
                encoding="utf-8",
            )
            (root / "positions.json").write_text(json.dumps({
                "positions": [{
                    "title": "Still open",
                    "track": "B",
                    "side": "NO",
                    "size_usd": 5,
                    "mark": 0.4,
                    "entry": 0.5,
                    "mint": "Mint1",
                    "ticker": "STILL",
                }]
            }), encoding="utf-8")
            (root / "autonomy_cycle.json").write_text(json.dumps({
                "id": "c-9",
                "ts": "2026-09-12T12:00:00Z",
                "summary": "Stand down. B has room.",
                "mode": "LIVE_ONLY",
            }), encoding="utf-8")
            snap = rws.build_snapshot(root)
        self.assertFalse(snap["example"])
        self.assertEqual(snap["mode"], "LIVE_ONLY")
        self.assertEqual(len(snap["positions"]), 1)
        self.assertEqual(snap["positions"][0]["track"], "B")
        self.assertEqual(snap["caps"]["B"]["open_usd"], 5.0)
        self.assertEqual(snap["caps"]["A"]["max_usd"], 10.0)
        self.assertTrue(snap["caps"]["A"]["label_only"])
        self.assertEqual(snap["capacity"]["max_open_usd"], 40.0)
        self.assertEqual(snap["capacity"]["max_open"], 8)
        self.assertEqual(snap["fills"][0]["action"], "close")
        cashed = next(f for f in snap["fills"] if f.get("market") == "Cashed market")
        self.assertEqual(cashed["pnl_usd"], 2.85)
        self.assertEqual(cashed["tx"], "CashoutSig333")
        self.assertEqual(snap["autonomy"]["note"], "Stand down. B has room.")
        self.assertEqual(snap["cashflow"]["tickets_opened"], 1)
        self.assertEqual(snap["cashflow"]["tickets_closed"], 2)
        self.assertIsNone(snap.get("pending_geofence"))

    def test_pending_geofence_from_ledger_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending_geofence.json").write_text(json.dumps({
                "state": "awaiting_region_check",
                "question": "Ready market",
                "book": "B",
                "ticket": 5,
                "id": "req-99",
                "expiry": "2026-09-13T12:00:00Z",
                "data_url": "data:text/plain,geofence-demo",
            }), encoding="utf-8")
            snap = rws.build_snapshot(root)
        self.assertEqual(snap["pending_geofence"]["request_id"], "req-99")
        self.assertEqual(snap["pending_geofence"]["track"], "B")
        self.assertEqual(snap["pending_geofence"]["geofence_url"], "data:text/plain,geofence-demo")
        self.assertIsNone(snap["pending_geofence"].get("check_region_url"))

    def test_pending_geofence_keeps_check_region_url(self):
        pages = "https://enudimmud.github.io/N-ABU-Dashboard/assets/geofence-latest.html"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pending_geofence.json").write_text(json.dumps({
                "state": "awaiting_region_check",
                "question": "Ready market",
                "book": "A",
                "ticket": 5,
                "id": "req-pages",
                "expiry": "2026-09-13T12:00:00Z",
                "data_url": "data:text/html,<h1>phone</h1>",
                "check_region_url": pages,
            }), encoding="utf-8")
            snap = rws.build_snapshot(root)
        self.assertEqual(snap["pending_geofence"]["check_region_url"], pages)
        self.assertEqual(snap["pending_geofence"]["geofence_url"], "data:text/html,<h1>phone</h1>")

    def test_missing_ledgers_do_not_invent_zeros_for_pnl(self):
        with tempfile.TemporaryDirectory() as tmp:
            snap = rws.build_snapshot(Path(tmp))
        self.assertEqual(snap["positions"], [])
        self.assertEqual(snap["fills"], [])
        self.assertIsNone(snap["cashflow"]["realized_pnl_usd"])
        self.assertIsNone(snap["cashflow"]["net_usd"])


class AdditiveHook(unittest.TestCase):
    def test_dashboard_html_only_adds_script(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8", errors="replace")
        self.assertIn('src="assets/world-tab.js', html)
        # original four nav items remain exactly as shipped
        self.assertIn(
            '<nav class="rail-nav"><a href="#portfolio" aria-label="Portefeuille">PF</a>'
            '<a href="#risk" aria-label="Risques">RK</a>'
            '<a href="#positions" aria-label="Positions">PX</a>'
            '<a href="#analysis" aria-label="Analyse">AN</a></nav>',
            html,
        )
        self.assertNotIn("nabu-world-nav", html)

    def test_generator_keeps_original_nav_and_appends_hook(self):
        src = (ROOT / "nabu_dashboard.py").read_text(encoding="utf-8")
        self.assertIn(
            '"<a href=\\"#portfolio\\" aria-label=\\"Portefeuille\\">PF</a>"',
            src,
        )
        self.assertIn("assets/world-tab.js", src)
        html = subprocess.check_output(
            [sys.executable, str(ROOT / "nabu_dashboard.py"), "demo", "--out",
             str(ROOT / "dashboard.world-test.tmp")],
            text=True,
        )
        out = Path(str(ROOT / "dashboard.world-test.tmp"))
        self.addCleanup(lambda: out.exists() and out.unlink())
        text = out.read_text(encoding="utf-8", errors="replace")
        self.assertIn('src="assets/world-tab.js', text)
        self.assertIn('href="#portfolio"', text)
        self.assertIn('href="#analysis"', text)
        self.assertNotIn("nabu-world-root", text)
        self.assertIn("DASHBOARD", html)

    def test_world_js_has_geofence_alerts(self):
        js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
        self.assertIn("Notification", js)
        self.assertIn("pending_geofence", js)
        self.assertIn("awaiting_region_check", js)
        self.assertIn("nabu-world-banner", js)
        self.assertIn("nabu-world-toast", js)
        self.assertIn("isOpenableGeofenceUrl", js)
        self.assertIn("pickOpenableGeofenceUrl", js)
        self.assertIn("pickNotifyUrl", js)
        self.assertIn("check_region_url", js)
        self.assertIn("Check région · ", js)
        self.assertIn("window.open(url", js)
        self.assertIn("highlightCopier", js)
        self.assertIn("nabu-world-copy", js)
        self.assertIn("isDemoPending", js)
        self.assertIn("isAlertablePending", js)
        self.assertIn("livePending", js)
        self.assertIn("alertablePending", js)
        self.assertIn("data:text\\/html", js)
        self.assertIn("data:text\\/plain", js)
        self.assertIn("ch-check-", js)
        self.assertIn("ETH-2700-WK", js)
        self.assertIn("EXEMPLE", js)
        self.assertIn("DEMO / URL invalide — attendre le vrai Check région du chat", js)
        self.assertIn("nabu-world-pending--example", js)
        self.assertIn("alertNewPending(alertable)", js)
        self.assertIn("Autoriser les alertes", js)
        self.assertIn("maybeAskNotifyOnce", js)
        self.assertIn("requireInteraction: true", js)
        self.assertIn('localStorage.getItem(SOUND_KEY) !== "0"', js)
        self.assertIn("CHIME_REPEAT_MS = 10000", js)
        self.assertIn("CHIME_MAX = 5", js)
        css = (ROOT / "assets" / "world-tab.css").read_text(encoding="utf-8")
        self.assertIn("#nabu-world-root", css)
        self.assertNotIn("body{", css.split("#nabu-world-root", 1)[0])
        root_block = css.split("#nabu-world-root{", 1)[1].split("}", 1)[0]
        self.assertIn("var(--nw-sky)", root_block)
        self.assertIn("#3EE0FF", css)
        self.assertIn(".nabu-world-orb{", css)
        self.assertIn("nabu-world-pill--pending", css)
        self.assertIn("nabu-world-pill--ok", css)
        self.assertIn("nabu-world-pill--exemple", css)
        self.assertIn("nabu-world-url-invalid", css)
        self.assertIn("#nabu-world-toast", css)
        self.assertIn("nabu-world-btn--cta", css)
        self.assertIn("nabu-world-btn.is-highlight", css)
        self.assertIn("nabu-world-cap.is-label", css)
        self.assertIn("nabu-world-lockup", js)
        self.assertIn("nabu-world-orb", js)
        self.assertIn("Completed", js)
        self.assertIn("normalizeSnapshot", js)
        self.assertIn("capacity.A_open", js)
        self.assertIn("max_open_usd", js)
        self.assertIn("label_only", js)
        self.assertIn("function capUtil", js)
        self.assertIn("function trackShareBasis", js)
        self.assertIn("function bankrollUsd", js)
        self.assertIn("function fillPnl", js)
        self.assertIn("realized_pnl_est", js)
        self.assertIn("cashout_tx", js)
        self.assertIn("du pool", js)
        self.assertIn("if (skip[k] || isBlank(v)) continue;", js)
        self.assertIn("renderRunway", js)
        self.assertIn("TARGET_CHF = 1700", js)
        self.assertIn("USDC idle", js)
        self.assertIn("closeWorld", js)
        self.assertIn("stopChimeLoop(false)", js)
        self.assertIn('href !== "#world"', js)
        toast_block = css.split("#nabu-world-toast{", 1)[1].split("}", 1)[0]
        self.assertIn("82px", toast_block)
        self.assertIn("z-index:64", toast_block)
        banner_block = css.split("#nabu-world-banner{", 1)[1].split("}", 1)[0]
        self.assertIn("z-index:60", banner_block)
        self.assertIn("left:82px", banner_block)

    def test_openable_geofence_url_contract(self):
        js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
        # https PayBox / data:text/html phone page only — not data:text/plain.
        self.assertIn("if (/^https:\\/\\//i.test(u)) return true;", js)
        self.assertIn("if (/^data:text\\/html(?:;|,|$)/i.test(u)) return true;", js)
        body = js.split("function isOpenableGeofenceUrl", 1)[1].split("function pickOpenableGeofenceUrl", 1)[0]
        self.assertNotIn("data:text/plain", body)
        self.assertNotIn("https?:", body)


def _extract_js_function(src: str, name: str) -> str:
    needle = f"function {name}"
    i = src.index(needle)
    brace = src.index("{", i)
    depth = 0
    for j, ch in enumerate(src[brace:], brace):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[i : j + 1]
    raise ValueError(f"unclosed function {name}")


def _eval_gate(expr: str):
    js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
    names = [
        "isBlank",
        "pick",
        "money",
        "isOpenableGeofenceUrl",
        "isHttpsUrl",
        "isShortHttpsUrl",
        "pickFirstUrl",
        "pickNotifyUrl",
        "pickOpenableGeofenceUrl",
        "pendingUrl",
        "isPlainDataUrl",
        "isDemoPending",
        "isAlertablePending",
        "livePending",
        "alertablePending",
        "notifyTitle",
        "notifyBody",
        "numish",
        "copyOwn",
        "asRowList",
        "normalizeTrackCap",
        "normalizePositions",
        "noteText",
        "normalizeAutonomy",
        "normalizeCashflow",
        "sumField",
        "labelsNote",
        "normalizeCapacity",
        "normalizeSnapshot",
        "tracksAreLabels",
        "trackShareBasis",
        "poolMax",
        "poolOpen",
        "bankrollUsd",
        "utilBasisIsBankroll",
        "capUtil",
        "pct",
        "capStatus",
        "esc",
        "signedMoney",
        "actionPill",
        "shortTx",
        "solscanTx",
        "fillPnl",
        "fillTx",
        "ticketKey",
        "rowTicker",
        "rowMarket",
        "actionKind",
        "fillTime",
        "ticketIndex",
        "closedAt",
        "isGhostOpenFill",
        "visibleFills",
        "countFillKind",
        "cashedWord",
        "cashedNote",
        "settledRows",
        "redeemableState",
        "approxEq",
        "consistencyIssues",
        "normalizeFills",
        "fillsKicker",
        "renderCaps",
        "renderRunway",
        "renderFills",
        "renderSettled",
    ]
    preamble = (
        "var URL_KEYS=['check_region_url','geofence_url','url','data_url','link','href'];\n"
        "var SHORT_HTTPS_MAX=280;\n"
        "var NBSP='\\u00a0';\n"
        "var TARGET_CHF=1700;\n"
    )
    bundle = preamble + "\n".join(_extract_js_function(js, n) for n in names)
    script = bundle + f";\nconsole.log(JSON.stringify({expr}));"
    out = subprocess.check_output(["node", "-e", script], text=True)
    return json.loads(out)


class AlertGating(unittest.TestCase):
    """Real pending alerts vs EXEMPLE / data:text/plain — evaluates world-tab.js."""

    LIVE = {
        "request_id": "pbx-live-99",
        "market": "Arsenal YES",
        "track": "A",
        "geofence_url": "https://paybox.example/ch/check-region",
    }
    EXEMPLE = rws.EXAMPLE["pending_geofence"]

    def test_https_live_is_alertable(self):
        self.assertTrue(_eval_gate(
            "isAlertablePending(" + json.dumps(self.LIVE) + ", {example:false})"
        ))

    def test_html_data_url_live_is_alertable(self):
        p = dict(self.LIVE, geofence_url="data:text/html,<h1>check</h1>")
        self.assertTrue(_eval_gate(
            "isAlertablePending(" + json.dumps(p) + ", {example:false})"
        ))

    def test_live_without_url_is_alertable(self):
        p = {"request_id": "pbx-live-88", "market": "No url yet"}
        self.assertTrue(_eval_gate(
            "isAlertablePending(" + json.dumps(p) + ", {example:false})"
        ))

    def test_example_snapshot_is_not_alertable(self):
        self.assertFalse(_eval_gate(
            "isAlertablePending(" + json.dumps(self.EXEMPLE) + ", {example:true})"
        ))

    def test_ch_check_request_id_is_exemple(self):
        p = dict(self.LIVE, request_id="ch-check-20260912-1844-a1")
        self.assertTrue(_eval_gate(
            "isDemoPending(" + json.dumps(p) + ", {example:false})"
        ))
        self.assertFalse(_eval_gate(
            "isAlertablePending(" + json.dumps(p) + ", {example:false})"
        ))

    def test_plain_data_url_is_not_alertable(self):
        p = dict(self.LIVE, geofence_url="data:text/plain,geofence-demo")
        self.assertFalse(_eval_gate(
            "isAlertablePending(" + json.dumps(p) + ", {example:false})"
        ))
        self.assertTrue(_eval_gate(
            "isPlainDataUrl(" + json.dumps(p["geofence_url"]) + ")"
        ))

    def test_alertable_pending_filters_mixed_list(self):
        rows = [
            self.EXEMPLE,
            dict(self.LIVE, geofence_url="data:text/plain,nope"),
            self.LIVE,
        ]
        got = _eval_gate(
            "alertablePending(" + json.dumps(rows) + ", {example:false}).map(p => p.request_id)"
        )
        self.assertEqual(got, ["pbx-live-99"])

    def test_live_pending_keeps_plain_url_but_alerts_do_not(self):
        p = dict(self.LIVE, geofence_url="data:text/plain,nope")
        live = _eval_gate(
            "livePending([" + json.dumps(p) + "], {example:false}).length"
        )
        alertable = _eval_gate(
            "alertablePending([" + json.dumps(p) + "], {example:false}).length"
        )
        self.assertEqual(live, 1)
        self.assertEqual(alertable, 0)

    def test_openable_rejects_plain_accepts_https(self):
        self.assertFalse(_eval_gate(
            "isOpenableGeofenceUrl('data:text/plain,geofence-demo')"
        ))
        self.assertTrue(_eval_gate(
            "isOpenableGeofenceUrl('https://paybox.example/ch')"
        ))
        self.assertTrue(_eval_gate(
            "isOpenableGeofenceUrl('data:text/html,<p>ok</p>')"
        ))

    PAGES = "https://enudimmud.github.io/N-ABU-Dashboard/assets/geofence-latest.html"
    HUGE_HTML = "data:text/html," + ("<p>phone</p>" * 40)

    def test_pick_notify_url_prefers_short_https_over_data_html(self):
        p = dict(self.LIVE, geofence_url=self.HUGE_HTML, check_region_url=self.PAGES)
        self.assertEqual(_eval_gate("pickNotifyUrl(" + json.dumps(p) + ")"), self.PAGES)
        self.assertEqual(
            _eval_gate("pickOpenableGeofenceUrl(" + json.dumps(p) + ")"),
            self.PAGES,
        )

    def test_pick_notify_url_uses_geofence_https_when_no_pages_field(self):
        self.assertEqual(
            _eval_gate("pickNotifyUrl(" + json.dumps(self.LIVE) + ")"),
            self.LIVE["geofence_url"],
        )

    def test_pick_notify_url_skips_data_html(self):
        p = dict(self.LIVE, geofence_url="data:text/html,<h1>check</h1>")
        p.pop("check_region_url", None)
        self.assertEqual(_eval_gate("pickNotifyUrl(" + json.dumps(p) + ")"), "")
        self.assertTrue(_eval_gate(
            "pickOpenableGeofenceUrl(" + json.dumps(p) + ").indexOf('data:text/html')===0"
        ))

    def test_https_check_region_url_overrides_plain_geofence(self):
        p = dict(self.LIVE, geofence_url="data:text/plain,nope", check_region_url=self.PAGES)
        self.assertTrue(_eval_gate(
            "isAlertablePending(" + json.dumps(p) + ", {example:false})"
        ))
        self.assertEqual(_eval_gate("pickNotifyUrl(" + json.dumps(p) + ")"), self.PAGES)

    def test_notify_title_and_body_include_url(self):
        p = dict(self.LIVE, check_region_url=self.PAGES)
        self.assertEqual(
            _eval_gate("notifyTitle(" + json.dumps(p) + ")"),
            "Check région · Arsenal YES",
        )
        body = _eval_gate("notifyBody(" + json.dumps(p) + ")")
        self.assertIn(self.PAGES, body)
        self.assertIn("Arsenal YES", body)
        self.assertIn("track A", body)

    def test_desktop_notify_opens_url_or_highlights_copier(self):
        js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
        body = js.split("function desktopNotify", 1)[1].split("function updateBadge", 1)[0]
        self.assertIn("notifyTitle(p)", body)
        self.assertIn("notifyBody(p)", body)
        self.assertIn("window.open(url, \"_blank\", \"noopener\")", body)
        self.assertIn("highlightCopier()", body)
        self.assertIn("openWorld()", body)


class LegacyScoreSchema(unittest.TestCase):
    """world-tab.js maps live-score capacity/open onto caps/positions."""

    SCORE = {
        "updated_at": "2026-09-12T22:17:32.039+02:00",
        "mode": "LIVE_ONLY",
        "usdc": 21.47,
        "total_usd": 47.47,
        "capacity": {
            "A_open": 10.0,
            "A_cap": 10.0,
            "A_remaining": 0.0,
            "B_open": 15.0,
            "B_cap": 15.0,
            "B_remaining": 0.0,
            "ticket": 5.0,
            "bankroll_usd": 47.47,
        },
        "positions": [{
            "market": "Arsenal",
            "track": "A",
            "side": "YES",
            "size_usd": 5.0,
            "mark_usd": 6.3959,
        }],
        "last_eval": {
            "id": "20260912-2217-LIVE",
            "note": "SKIP_no_capacity + Arsenal mark +61.4%",
        },
        "pending_geofence": None,
        "awaiting_region_check": False,
    }

    def test_capacity_maps_to_caps(self):
        got = _eval_gate("normalizeSnapshot(" + json.dumps(self.SCORE) + ")")
        self.assertEqual(got["caps"]["A"]["open_usd"], 10.0)
        self.assertEqual(got["caps"]["A"]["max_usd"], 10.0)
        self.assertEqual(got["caps"]["B"]["open_usd"], 15.0)
        self.assertEqual(got["caps"]["B"]["max_usd"], 15.0)
        self.assertEqual(got["generated_at"], self.SCORE["updated_at"])
        self.assertEqual(got["ticket_usd"], 5.0)
        self.assertEqual(got["positions"][0]["mark"], 6.3959)
        self.assertEqual(got["cashflow"]["usdc"], 21.47)
        self.assertEqual(got["cashflow"]["idle_usd"], 21.47)
        self.assertEqual(got["cashflow"]["deployed_usd"], 5)
        self.assertEqual(got["cashflow"]["positions_mark_usd"], 6.3959)
        self.assertEqual(got["cashflow"]["bankroll_usd"], 47.47)
        self.assertEqual(got["autonomy"]["note"], "SKIP_no_capacity + Arsenal mark +61.4%")

    def test_open_alias_becomes_positions(self):
        raw = {
            "capacity": {"A_open": 5, "A_cap": 10, "B_open": 0, "B_cap": 15},
            "open": [{"market": "Foo", "track": "A", "size_usd": 5, "mark_usd": 1.2}],
        }
        got = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertEqual(len(got["positions"]), 1)
        self.assertEqual(got["positions"][0]["market"], "Foo")
        self.assertEqual(got["positions"][0]["mark"], 1.2)
        self.assertEqual(got["caps"]["A"]["open_usd"], 5)
        self.assertEqual(got["caps"]["A"]["max_usd"], 10)

    def test_canonical_caps_not_overwritten_by_empty_capacity(self):
        raw = {
            "generated_at": "2026-09-12T16:40:00Z",
            "caps": {"A": {"open_usd": 5, "max_usd": 10}, "B": {"open_usd": 10, "max_usd": 15}},
            "positions": [{"market": "Keep", "track": "B", "mark": 0.4}],
        }
        got = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertEqual(got["caps"]["A"]["open_usd"], 5)
        self.assertEqual(got["positions"][0]["mark"], 0.4)
        self.assertEqual(got["generated_at"], "2026-09-12T16:40:00Z")

    def test_garbage_input_never_throws(self):
        for raw in (None, 1, "x", [], {"capacity": "nope", "open": 1, "autonomy": {"note": {"note": "ok"}}}):
            got = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
            self.assertIsInstance(got, dict)
        nested = _eval_gate('normalizeSnapshot({"autonomy":{"note":{"note":"ok"}}}).autonomy.note')
        self.assertEqual(nested, "ok")

    def test_rich_cashflow_fields_are_kept(self):
        raw = {
            "caps": {"A": {"open_usd": 5, "max_usd": 15}, "B": {"open_usd": 10, "max_usd": 25}},
            "cashflow": {
                "realized_pnl_usd": 1.15,
                "unrealized_pnl_usd": 0.4,
                "fees_usd": 0.12,
                "net_usd": 1.43,
                "volume_usd": 20,
                "tickets_opened": 4,
                "tickets_closed": 1,
                "usdc": 8.2,
                "total_usd": 18.2,
            },
            "positions": [{"market": "X", "track": "A", "size_usd": 5, "mark": 0.6}],
        }
        got = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ").cashflow")
        self.assertEqual(got["realized_pnl_usd"], 1.15)
        self.assertEqual(got["net_usd"], 1.43)
        self.assertEqual(got["idle_usd"], 8.2)
        self.assertEqual(got["deployed_usd"], 5)
        self.assertEqual(got["positions_mark_usd"], 0.6)

    def test_dynamic_caps_not_clobbered_by_legacy_capacity(self):
        raw = {
            "caps": {
                "A": {"open_usd": 10, "max_usd": 15},
                "B": {"open_usd": 15, "max_usd": 25},
            },
            "capacity": {
                "A_open": 10, "A_cap": 10, "B_open": 15, "B_cap": 15, "ticket": 5,
            },
            "cashflow": {
                "realized_pnl_usd": 0.0,
                "unrealized_pnl_usd": 1.31,
                "fees_usd": 0.0,
                "net_usd": 1.31,
                "volume_usd": 25.0,
                "usdc": 21.47,
                "positions_mark_usd": 26.31,
                "positions_cost_usd": 25.0,
                "total_usd": 47.78,
                "tickets_opened": 5,
                "tickets_closed": 0,
            },
            "sub_runway": {"target_chf": 1700, "surplus_chf_est": None, "bankroll_usd": 47.78},
        }
        got = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertEqual(got["caps"]["A"]["max_usd"], 15)
        self.assertEqual(got["caps"]["B"]["max_usd"], 25)
        self.assertEqual(got["cashflow"]["realized_pnl_usd"], 0.0)
        self.assertEqual(got["cashflow"]["unrealized_pnl_usd"], 1.31)
        self.assertEqual(got["cashflow"]["fees_usd"], 0.0)
        self.assertEqual(got["cashflow"]["volume_usd"], 25.0)
        self.assertEqual(got["cashflow"]["idle_usd"], 21.47)
        self.assertEqual(got["cashflow"]["deployed_usd"], 25.0)


class PagesRoot(unittest.TestCase):
    def test_index_redirects_to_dashboard(self):
        html = (ROOT / "index.html").read_text(encoding="utf-8")
        self.assertIn("dashboard.html", html)
        self.assertIn('http-equiv="refresh"', html)
        self.assertIn("location.replace", html)
        self.assertTrue((ROOT / ".nojekyll").exists())
        slot = (ROOT / "assets" / "geofence-latest.html").read_text(encoding="utf-8")
        self.assertIn("Check région", slot)
        # Empty placeholder when no ticket is pending; live pages still name this slot.
        if "Aucun ticket" not in slot:
            self.assertIn("check_region_url", slot)
            self.assertIn("enudimmud.github.io/N-ABU-Dashboard/assets/geofence-latest.html", slot)


def _plain(s: str) -> str:
    return str(s).replace("\u00a0", " ").replace("\xa0", " ")


class RunwayUtilPool(unittest.TestCase):
    """Utilisation = deployed / live bankroll — never 8×ticket ($40) or A.max+B.max."""

    DOUBLE_TRACK = {
        "ticket_usd": 5,
        "bankroll_usd": 48.57,
        "caps": {
            "A": {"open_usd": 5.0, "max_usd": 40.0},
            "B": {"open_usd": 30.0, "max_usd": 40.0},
        },
        "capacity": {
            "n_open": 7,
            "max_open": 8,
            "max_open_usd": 40.0,
            "ticket": 5.0,
            "bankroll_usd": 48.57,
            "A_open": 5.0,
            "B_open": 30.0,
            "note": "paper-aligned: max 8 · $5–10 · A/B labels",
        },
        "cashflow": {
            "usdc": 14.29,
            "idle_usdc": 14.29,
            "positions_cost_usd": 35.0,
            "deployed_cost_usd": 35.0,
            "total_usd": 48.57,
            "bankroll_usd": 48.57,
        },
    }

    # Phone WD screenshot: bankroll 50.66 · idle 15.15 · deployed 35 · fake $40 cap.
    JD_PHONE = {
        "ticket_usd": 5,
        "bankroll_usd": 50.66,
        "caps": {
            "A": {"open_usd": 5.0, "max_usd": 40.0, "label_only": True},
            "B": {"open_usd": 30.0, "max_usd": 40.0, "label_only": True},
            "pool_usd": 40.0,
        },
        "capacity": {
            "n_open": 7,
            "max_open": 8,
            "remaining_tickets": 1,
            "max_open_usd": 40.0,
            "ticket": 5.0,
            "bankroll_usd": 50.66,
        },
        "cashflow": {
            "usdc": 15.15,
            "idle_usd": 15.15,
            "idle_usdc": 15.15,
            "deployed_usd": 35.0,
            "deployed_cost_usd": 35.0,
            "total_usd": 50.66,
            "bankroll_usd": 50.66,
        },
    }

    def test_utilisation_uses_bankroll_not_ticket_capacity(self):
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(self.JD_PHONE) + ")")
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertEqual(got["open"], 35)
        self.assertAlmostEqual(got["max"], 50.66)
        self.assertNotEqual(got["max"], 40)
        html = _plain(_eval_gate("renderRunway(" + json.dumps(snap) + ")"))
        self.assertIn("35.00", html)
        self.assertIn("50.66", html)
        self.assertIn("15.15", html)
        self.assertIn("69 %", html)  # 35 / 50.66
        self.assertNotIn("/ 40.00", html)
        self.assertNotIn("40.00", html)

    def test_utilization_basis_bankroll_prefers_published_max(self):
        raw = {
            "capacity": {"max_open_usd": 40.0, "bankroll_usd": 48.0},
            "cashflow": {"deployed_usd": 35, "bankroll_usd": 48.0},
            "utilization": {
                "open_usd": 35,
                "max_usd": 50.66,
                "basis": "deployed_cost / bankroll",
            },
        }
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertTrue(_eval_gate("utilBasisIsBankroll(" + json.dumps(snap) + ")"))
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertAlmostEqual(got["max"], 48.0)
        self.assertNotEqual(got["max"], 40)

    def test_double_track_max_renders_util_vs_bankroll_not_40_or_80(self):
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(self.DOUBLE_TRACK) + ")")
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertEqual(got["open"], 35)
        self.assertAlmostEqual(got["max"], 48.57)
        self.assertNotEqual(got["max"], 40)
        self.assertNotEqual(got["max"], 80)
        html = _plain(_eval_gate("renderRunway(" + json.dumps(snap) + ")"))
        self.assertIn("35.00", html)
        self.assertIn("48.57", html)
        self.assertNotIn("/ 40.00", html)
        self.assertNotIn("80.00", html)
        self.assertIn("14.29", html)

    def test_max_open_times_ticket_is_not_utilisation_denom(self):
        raw = {
            "ticket_usd": 5,
            "caps": {"A": {"open_usd": 5, "max_usd": 40}, "B": {"open_usd": 30, "max_usd": 40}},
            "capacity": {"max_open": 8, "ticket": 5},
            "cashflow": {"deployed_usd": 35, "total_usd": 48.57},
        }
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertEqual(got["open"], 35)
        self.assertAlmostEqual(got["max"], 48.57)
        basis = _eval_gate("trackShareBasis(" + json.dumps(snap) + ")")
        self.assertEqual(basis["max"], 40)
        self.assertEqual(basis["basis"], "max_open_usd")

    def test_label_only_caps_show_open_not_hard_per_track_max(self):
        raw = {
            "ticket_usd": 5,
            "caps": {
                "A": {"open_usd": 5, "max_usd": 40, "label_only": True},
                "B": {"open_usd": 30, "max_usd": 40, "label_only": True},
            },
            "capacity": {"max_open_usd": 40, "label_only": True},
            "cashflow": {"deployed_usd": 35, "total_usd": 48.57},
        }
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertTrue(_eval_gate("tracksAreLabels(" + json.dumps(snap) + ")"))
        basis = _eval_gate("trackShareBasis(" + json.dumps(snap) + ")")
        self.assertEqual(basis["basis"], "max_open_usd")
        self.assertEqual(basis["max"], 40)
        html = _plain(_eval_gate(
            "renderCaps(" + json.dumps(snap["caps"]) + ", " + json.dumps(snap) + ")"
        ))
        self.assertIn("5.00", html)
        self.assertIn("30.00", html)
        self.assertIn("du pool", html)
        self.assertIn("13 %", html)  # 5/40 ticket slots
        self.assertIn("75 %", html)  # 30/40
        self.assertIn("is-label", html)
        self.assertNotIn("/ 40.00", html)
        self.assertNotIn("/ 80.00", html)
        util = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertAlmostEqual(util["max"], 48.57)

    def test_label_note_without_flag_still_skips_track_max_sum(self):
        raw = {
            "caps": {"A": {"open_usd": 5, "max_usd": 40}, "B": {"open_usd": 30, "max_usd": 40}},
            "capacity": {
                "max_open_usd": 40,
                "note": "paper-aligned: max 8 · $5–10 · A/B labels",
            },
            "cashflow": {"positions_cost_usd": 35, "total_usd": 48.57},
        }
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        self.assertTrue(snap["caps"]["A"].get("label_only"))
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertAlmostEqual(got["max"], 48.57)
        html = _plain(_eval_gate(
            "renderCaps(" + json.dumps(snap["caps"]) + ", " + json.dumps(snap) + ")"
        ))
        self.assertNotIn("/ 40.00", html)
        self.assertIn("du pool", html)

    def test_shared_pool_tracks_even_without_label_flag(self):
        raw = {
            "ticket_usd": 5,
            "caps": {"A": {"open_usd": 5, "max_usd": 40}, "B": {"open_usd": 30, "max_usd": 40}},
            "capacity": {"max_open_usd": 40},
            "cashflow": {"deployed_usd": 35, "total_usd": 48.57, "bankroll_usd": 48.57},
        }
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        html = _plain(_eval_gate(
            "renderCaps(" + json.dumps(snap["caps"]) + ", " + json.dumps(snap) + ")"
        ))
        self.assertIn("du pool", html)
        self.assertIn("is-label", html)
        self.assertNotIn("/ 40.00", html)
        self.assertNotIn("/ 80.00", html)
        util = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertAlmostEqual(util["max"], 48.57)

    def test_prefer_deployed_cost_over_track_open_sum(self):
        raw = {
            "caps": {"A": {"open_usd": 5}, "B": {"open_usd": 10}},
            "capacity": {"max_open_usd": 40, "bankroll_usd": 50.66},
            "cashflow": {
                "deployed_cost_usd": 35,
                "positions_cost_usd": 35,
                "bankroll_usd": 50.66,
            },
        }
        got = _eval_gate("capUtil(normalizeSnapshot(" + json.dumps(raw) + "))")
        self.assertEqual(got["open"], 35)
        self.assertAlmostEqual(got["max"], 50.66)

    def test_no_bankroll_hides_utilisation_instead_of_fake_40(self):
        raw = {
            "caps": {"A": {"open_usd": 5}, "B": {"open_usd": 10}},
            "capacity": {"max_open_usd": 40},
            "cashflow": {"deployed_cost_usd": 35, "positions_cost_usd": 35},
        }
        got = _eval_gate("capUtil(normalizeSnapshot(" + json.dumps(raw) + "))")
        self.assertIsNone(got)

    def test_checked_in_live_snapshot_util_uses_bankroll(self):
        raw = json.loads((ROOT / "assets" / "world-live.json").read_text())
        snap = _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")
        got = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        bank = snap["cashflow"]["bankroll_usd"]
        dep = snap["cashflow"].get("deployed_usd") or snap["cashflow"].get("deployed_cost_usd")
        self.assertAlmostEqual(got["open"], dep)
        self.assertAlmostEqual(got["max"], bank)
        self.assertNotEqual(got["max"], 40)
        self.assertNotEqual(got["max"], 80)
        html = _plain(_eval_gate("renderRunway(" + json.dumps(snap) + ")"))
        self.assertIn(f"{dep:.2f}", html)
        self.assertIn(f"{bank:.2f}", html)
        self.assertNotIn("/ 40.00", html)
        self.assertNotIn("80.00", html)
        tracks = _plain(_eval_gate(
            "renderCaps(" + json.dumps(snap["caps"]) + ", " + json.dumps(snap) + ")"
        ))
        self.assertIn("du pool", tracks)
        self.assertIn("5.00", tracks)
        self.assertIn("30.00", tracks)
        self.assertNotIn("/ 40.00", tracks)
        self.assertNotIn("/ 80.00", tracks)


class FillPnlAliases(unittest.TestCase):
    """Fills table picks realized PnL / cashout_tx when pnl_usd / tx are omitted."""

    def test_realized_pnl_est_renders_signed_money(self):
        rows = [{
            "ts": "2026-09-12T23:19:38+02:00",
            "action": "close",
            "market": "Arsenal YES",
            "track": "A",
            "size_usd": 5,
            "pnl_usd": None,
            "realized_pnl_est": 2.85,
            "note": "settled_cashed",
        }]
        self.assertEqual(_eval_gate("fillPnl(" + json.dumps(rows[0]) + ")"), 2.85)
        html = _plain(_eval_gate("renderFills(" + json.dumps(rows) + ")"))
        self.assertIn("2.85", html)
        self.assertIn("Arsenal YES", html)
        self.assertNotIn("— $", html)

    def test_fill_tx_falls_back_to_cashout_tx(self):
        row = {
            "action": "close",
            "market": "Milwaukee YES",
            "pnl_usd": None,
            "realized_pnl": -0.4,
            "tx": None,
            "tx_hash": None,
            "cashout_tx": "CashoutSigABCDEF1234567890",
        }
        self.assertEqual(
            _eval_gate("fillTx(" + json.dumps(row) + ")"),
            "CashoutSigABCDEF1234567890",
        )
        html = _eval_gate("renderFills(" + json.dumps([row]) + ")")
        self.assertIn("solscan.io/tx/CashoutSigABCDEF1234567890", html)
        self.assertIn("0.40", _plain(html))

    def test_missing_pnl_is_em_dash_only(self):
        rows = [{"action": "open", "market": "CIN Bengals YES", "size_usd": 5, "pnl_usd": None}]
        self.assertIsNone(_eval_gate("fillPnl(" + json.dumps(rows[0]) + ")"))
        html = _eval_gate("renderFills(" + json.dumps(rows) + ")")
        self.assertIn("\u2014", html)
        self.assertNotIn("0.00", html)


class OverlayClose(unittest.TestCase):
    def test_close_world_hides_toast_and_stops_chimes(self):
        js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
        body = js.split("function closeWorld", 1)[1].split("function syncHash", 1)[0]
        self.assertIn("hideToast()", body)
        self.assertIn("stopChimeLoop(false)", body)
        self.assertIn("setWorldChrome(root, false)", body)
        alert = js.split("function alertNewPending", 1)[1].split("function tickExpiry", 1)[0]
        self.assertIn("worldOpen()", alert)
        self.assertIn("hideToast()", alert)


def _pos(ticker: str, market: str, track: str = "B", upnl: float = -0.05) -> dict:
    return {
        "market": market, "ticker": ticker, "track": track, "side": "YES",
        "size_usd": 5.0, "mark_usd": round(5.0 + upnl, 4), "upnl_usd": upnl,
        "status": "open", "position_result": "pending",
    }


def _fill(action: str, market: str, ticker, ts: str, **extra) -> dict:
    row = {
        "ts": ts, "action": action, "market": market, "ticker": ticker,
        "track": "B", "side": "YES" if action == "open" else "sell",
        "size_usd": 5.0, "pnl_usd": None, "tx": "Sig" + (ticker or market).replace(" ", "")[:24],
        "note": "filled" if action == "open" else "exited_sold",
    }
    row.update(extra)
    return row


# Snapshot shipped by the PayBox rebuild on 2026-09-13: 7 live tickets, but the
# fills ledger still carries the 6 buys of tickets that were exited or settled.
# The Real Madrid buy has no ticker at all — it must survive the ghost filter.
WORLD_GHOSTS = {
    "schema_version": 2,
    "example": False,
    "generated_at": "2026-09-13T04:24:33Z",
    "mode": "LIVE_ONLY",
    "ticket_usd": 5.0,
    "bankroll_usd": 50.6,
    "caps": {
        "A": {"open_usd": 5.0, "max_usd": 40.0, "pool_usd": 40.0, "label_only": True},
        "B": {"open_usd": 30.0, "max_usd": 40.0, "pool_usd": 40.0, "label_only": True},
        "pool_usd": 40.0,
    },
    "capacity": {
        "n_open": 7, "max_open": 8, "remaining_tickets": 1, "ticket": 5.0,
        "max_open_usd": 50.6, "ticket_cap_usd": 40.0, "bankroll_usd": 50.6,
        "util_basis": "bankroll_usd",
    },
    "positions": [
        _pos("WXNFL-26SEP13ATLPIT-PSTE", "PIT Steelers YES", upnl=-0.0127),
        _pos("WXLIG-26SEP131615BARCLEVA-BARC", "Barcelona YES", upnl=-0.0624),
        _pos("WXNFL-26SEP13CLEJAC-JJAG", "JAC Jaguars YES", upnl=-0.0719),
        _pos("WXNFL-26SEP13NODET-DLIO", "DET Lions YES", upnl=-0.0772),
        _pos("WXNFL-26SEP13WASPHI-PEAG", "PHI Eagles YES", upnl=-0.0878),
        _pos("WXNFL-26SEP13TBCIN-CBEN", "CIN Bengals YES", upnl=-0.0939),
        _pos("WXLIG-26SEP152130RMADELCH-RMAD", "Real Madrid YES", track="A", upnl=-0.1373),
    ],
    "fills": [
        _fill("close", "MIL Brewers YES", "WXMLB-26SEP121810CINMIL-MBRE",
              "2026-09-13T04:43:05+02:00", pnl_usd=2.6072, note="settled_cashed"),
        _fill("close", "Arsenal YES", "WXEPL-26SEP122000ARSESUND-ARSE",
              "2026-09-12T23:19:38+02:00", pnl_usd=2.8454, track="A", note="settled_cashed"),
        _fill("open", "CIN Bengals YES", "WXNFL-26SEP13TBCIN-CBEN", "2026-09-12T23:08:38Z"),
        _fill("open", "DET Lions YES", "WXNFL-26SEP13NODET-DLIO", "2026-09-12T22:51:05Z"),
        _fill("open", "JAC Jaguars YES", "WXNFL-26SEP13CLEJAC-JJAG", "2026-09-12T22:38:22Z"),
        _fill("open", "PIT Steelers YES", "WXNFL-26SEP13ATLPIT-PSTE", "2026-09-12T22:27:36Z"),
        _fill("close", "TB Buccaneers YES", "WXNFL-26SEP13TBCIN-TBUC",
              "2026-09-12T22:22:40Z", pnl_usd=-0.3266),
        _fill("close", "NO Saints YES", "WXNFL-26SEP13NODET-NSAI",
              "2026-09-12T22:16:57Z", pnl_usd=-0.4466),
        _fill("close", "CLE Browns YES", "WXNFL-26SEP13CLEJAC-CBRO",
              "2026-09-12T22:14:59Z", pnl_usd=-0.537),
        _fill("close", "ATL Falcons YES", "WXNFL-26SEP13ATLPIT-AFAL",
              "2026-09-12T22:11:56Z", pnl_usd=-0.4082),
        # ghosts: buys of tickets that are no longer in the book
        _fill("open", "NO Saints YES", "WXNFL-26SEP13NODET-NSAI", "2026-09-12T21:48:52Z"),
        _fill("open", "TB Buccaneers YES", "WXNFL-26SEP13TBCIN-TBUC", "2026-09-12T21:35:44Z"),
        _fill("open", "CLE Browns YES", "WXNFL-26SEP13CLEJAC-CBRO", "2026-09-12T21:23:43Z"),
        _fill("open", "ATL Falcons YES", "WXNFL-26SEP13ATLPIT-AFAL", "2026-09-12T21:08:41Z"),
        _fill("open", "Barcelona YES", "WXLIG-26SEP131615BARCLEVA-BARC", "2026-09-12T16:16:12Z"),
        _fill("open", "Milwaukee YES", "WXMLB-26SEP121810CINMIL-MBRE", "2026-09-12T16:05:53Z"),
        _fill("open", "Arsenal YES", "WXEPL-26SEP122000ARSESUND-ARSE",
              "2026-09-12T15:00:49Z", track="A"),
        _fill("open", "PHI Eagles YES", "WXNFL-26SEP13WASPHI-PEAG", "2026-09-12T14:47:42Z"),
        _fill("open", "Real Madrid YES", None, "2026-09-12T14:40:21Z", track="A"),
    ],
    "settled": [
        {"market": "Arsenal YES", "ticker": "WXEPL-26SEP122000ARSESUND-ARSE", "result": "won",
         "pnl_usd": 2.8454, "size_usd": 5.0, "track": "A", "redeemable": "cashed",
         "settlement_asset": "CASH"},
        {"market": "MIL Brewers YES", "ticker": "WXMLB-26SEP121810CINMIL-MBRE", "result": "won",
         "pnl_usd": 2.6072, "size_usd": 5.0, "track": "B", "redeemable": "cashed",
         "settlement_asset": "CASH"},
    ],
    # venue view, still stale after the CASH → USDC sweep
    "settled_paybox": [
        {"market_ticker": "WXEPL-26SEP122000ARSESUND-ARSE", "market_status": "finalized",
         "position_result": "won", "redeemable": "open", "settlement_asset": "CASH",
         "held_amount": None, "redemption_value_usd": None},
        {"market_ticker": "WXMLB-26SEP121810CINMIL-MBRE", "market_status": "finalized",
         "position_result": "won", "redeemable": "open", "settlement_asset": "CASH",
         "held_amount": None, "redemption_value_usd": None},
    ],
    "cashflow": {
        "usdc": 15.15, "idle_usd": 15.15, "idle_usdc": 15.15, "sol_dust_usd": 0.9854,
        "deployed_usd": 35.0, "deployed_cost_usd": 35.0, "positions_mark_usd": 34.46,
        "positions_cost_usd": 35.0, "total_usd": 50.6, "bankroll_usd": 50.6,
        "realized_pnl_usd": 3.7342, "unrealized_pnl_usd": -0.5432, "fees_usd": 0.0,
        "net_usd": 3.191,
    },
    "utilization": {
        "open_usd": 35.0, "max_usd": 50.6, "basis": "deployed_cost / bankroll_usd",
        "ticket_cap_usd": 40.0,
    },
    "sub_runway": {"target_chf": 1700, "bankroll_usd": 50.6, "realized_pnl_usd": 3.7342},
}


def _norm(raw: dict):
    return _eval_gate("normalizeSnapshot(" + json.dumps(raw) + ")")


def _fill_kinds(snap: dict):
    return _eval_gate(
        "visibleFills(" + json.dumps(snap) + ").map(function(f){"
        "return [actionKind(f.action), f.ticker || f.market, fillPnl(f)];})"
    )


class GhostOpenFills(unittest.TestCase):
    """13 buys + 6 closes in the ledger, 7 tickets in the book → 7 open rows."""

    def test_ghost_buys_are_dropped_and_closes_kept(self):
        snap = _norm(WORLD_GHOSTS)
        self.assertEqual(len(snap["fills"]), 19)
        rows = _fill_kinds(snap)
        opens = [r for r in rows if r[0] == "open"]
        closes = [r for r in rows if r[0] == "close"]
        self.assertEqual(len(opens), 7)
        self.assertEqual(len(closes), 6)
        self.assertEqual(len(opens), len(snap["positions"]))
        self.assertEqual(len(opens), snap["capacity"]["n_open"])

    def test_open_rows_are_exactly_the_live_book(self):
        snap = _norm(WORLD_GHOSTS)
        shown = {r[1] for r in _fill_kinds(snap) if r[0] == "open"}
        live = {p["ticker"] for p in snap["positions"]}
        # the Real Madrid buy carries no ticker — matched by market name
        self.assertIn("Real Madrid YES", shown)
        self.assertTrue(shown - {"Real Madrid YES"} <= live)
        for ghost in ("WXNFL-26SEP13NODET-NSAI", "WXNFL-26SEP13TBCIN-TBUC",
                      "WXNFL-26SEP13CLEJAC-CBRO", "WXNFL-26SEP13ATLPIT-AFAL",
                      "WXMLB-26SEP121810CINMIL-MBRE", "WXEPL-26SEP122000ARSESUND-ARSE"):
            self.assertNotIn(ghost, shown)

    def test_every_close_row_carries_signed_pnl_and_opens_show_em_dash(self):
        snap = _norm(WORLD_GHOSTS)
        rows = _fill_kinds(snap)
        for kind, who, pnl in rows:
            if kind == "close":
                self.assertIsNotNone(pnl, who)
            else:
                self.assertIsNone(pnl, who)
        html = _plain(_eval_gate(
            "renderFills(visibleFills(" + json.dumps(snap) + "))"
        ))
        for money_txt in ("+2.61", "+2.85", "\u22120.33", "\u22120.45", "\u22120.54", "\u22120.41"):
            self.assertIn(money_txt, html)
        self.assertEqual(html.count("<tr>"), 14)  # header + 7 opens + 6 closes

    def test_ghost_buy_of_closed_market_is_never_labelled_open(self):
        ghost = _fill("open", "ATL Falcons YES", "WXNFL-26SEP13ATLPIT-AFAL",
                      "2026-09-12T21:08:41Z")
        snap = _norm(WORLD_GHOSTS)
        idx = _eval_gate("ticketIndex(" + json.dumps(snap) + ")")
        self.assertTrue(idx["hasBook"])
        self.assertTrue(_eval_gate(
            "isGhostOpenFill(" + json.dumps(ghost) + ", ticketIndex(" + json.dumps(snap) + "))"
        ))
        live = _fill("open", "PIT Steelers YES", "WXNFL-26SEP13ATLPIT-PSTE",
                     "2026-09-12T22:27:36Z")
        self.assertFalse(_eval_gate(
            "isGhostOpenFill(" + json.dumps(live) + ", ticketIndex(" + json.dumps(snap) + "))"
        ))

    def test_closed_ticket_buys_drop_even_without_positions(self):
        raw = dict(WORLD_GHOSTS)
        raw.pop("positions", None)
        raw.pop("capacity", None)
        snap = _norm(raw)
        self.assertEqual(snap["positions"], [])
        rows = _fill_kinds(snap)
        opens = [r for r in rows if r[0] == "open"]
        self.assertEqual(len(opens), 7)
        self.assertEqual(len([r for r in rows if r[0] == "close"]), 6)

    def test_empty_book_with_n_open_zero_hides_every_buy(self):
        raw = dict(WORLD_GHOSTS, positions=[], capacity={"n_open": 0, "max_open": 8})
        rows = _fill_kinds(_norm(raw))
        self.assertEqual([r for r in rows if r[0] == "open"], [])
        self.assertEqual(len([r for r in rows if r[0] == "close"]), 6)

    def test_unknown_book_keeps_buys_of_untouched_tickets(self):
        raw = {
            "fills": [_fill("open", "Fresh market", "FRESH-1", "2026-09-13T01:00:00Z")],
        }
        rows = _fill_kinds(_norm(raw))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], "open")

    def test_reopen_after_close_is_kept(self):
        raw = {
            "positions": [_pos("RE-1", "Re-entered market")],
            "capacity": {"n_open": 1},
            "fills": [
                _fill("open", "Re-entered market", "RE-1", "2026-09-13T03:00:00Z"),
                _fill("close", "Re-entered market", "RE-1", "2026-09-13T02:00:00Z", pnl_usd=0.4),
                _fill("open", "Re-entered market", "RE-1", "2026-09-13T01:00:00Z"),
            ],
        }
        rows = _fill_kinds(_norm(raw))
        self.assertEqual(len([r for r in rows if r[0] == "open"]), 1)

    def test_fills_kicker_counts_what_is_shown(self):
        kicker = _eval_gate("fillsKicker(" + json.dumps(_norm(WORLD_GHOSTS)) + ")")
        self.assertIn("7 open", kicker)
        self.assertIn("6 close", kicker)
        self.assertIn("6 buy de ticket clos", kicker)

    def test_checked_in_snapshot_shows_no_ghost_open(self):
        raw = json.loads((ROOT / "assets" / "world-live.json").read_text())
        snap = _norm(raw)
        rows = _fill_kinds(snap)
        opens = [r for r in rows if r[0] == "open"]
        closes = [r for r in rows if r[0] == "close"]
        live = {p.get("ticker") for p in snap["positions"]}
        live |= {p.get("market") for p in snap["positions"]}
        for _kind, who, _pnl in opens:
            self.assertIn(who, live)
        self.assertLessEqual(len(opens), len(snap["positions"]))
        for _kind, who, pnl in closes:
            self.assertIsNotNone(pnl, f"close without realized PnL: {who}")
        n_open = (snap.get("capacity") or {}).get("n_open")
        if n_open is not None:
            self.assertEqual(n_open, len(snap["positions"]))


class ClosePnlBackfill(unittest.TestCase):
    """A close must never render empty when any PnL field exists anywhere."""

    def test_exit_close_with_only_realized_pnl_est(self):
        raw = {
            "positions": [],
            "capacity": {"n_open": 0},
            "fills": [{
                "ts": "2026-09-12T22:22:40Z", "action": "close", "market": "TB Buccaneers YES",
                "ticker": "WXNFL-26SEP13TBCIN-TBUC", "track": "B", "size_usd": 5,
                "realized_pnl_est": -0.3266, "tx": "SigTBExit1234567890",
                "note": "exited_sold",
            }],
        }
        snap = _norm(raw)
        self.assertEqual(_eval_gate("fillPnl(" + json.dumps(snap["fills"][0]) + ")"), -0.3266)
        html = _plain(_eval_gate("renderFills(visibleFills(" + json.dumps(snap) + "))"))
        self.assertIn("\u22120.33", html)
        self.assertIn("TB Buccaneers YES", html)
        self.assertNotIn("— $", html)

    def test_close_without_pnl_takes_it_from_settled(self):
        raw = {
            "positions": [],
            "capacity": {"n_open": 0},
            "fills": [{
                "ts": "2026-09-12T23:19:38Z", "action": "close", "market": "Arsenal YES",
                "ticker": "WXEPL-26SEP122000ARSESUND-ARSE", "track": "A", "size_usd": 5,
                "tx": "SigArsenalCashout1234", "note": "settled_cashed",
            }],
            "settled": [{
                "market": "Arsenal YES", "ticker": "WXEPL-26SEP122000ARSESUND-ARSE",
                "result": "won", "pnl_usd": 2.8454, "redeemable": "cashed",
                "settlement_asset": "CASH",
            }],
        }
        snap = _norm(raw)
        self.assertEqual(snap["fills"][0]["pnl_usd"], 2.8454)
        self.assertEqual(snap["fills"][0]["pnl_source"], "settled")
        html = _plain(_eval_gate("renderFills(visibleFills(" + json.dumps(snap) + "))"))
        self.assertIn("+2.85", html)

    def test_close_without_any_pnl_says_unverified_not_blank(self):
        rows = [
            _fill("close", "TB Buccaneers YES", "TBUC", "2026-09-12T22:22:40Z"),
            _fill("open", "CIN Bengals YES", "CBEN", "2026-09-12T23:08:38Z"),
        ]
        html = _eval_gate("renderFills(" + json.dumps(rows) + ")")
        self.assertIn("PnL UNVERIFIED", html)
        self.assertEqual(html.count("PnL UNVERIFIED"), 1)
        self.assertIn("\u2014", html)  # the open row keeps its em dash
        self.assertNotIn("0.00", html)

    def test_open_rows_keep_blank_pnl(self):
        snap = _norm({
            "positions": [_pos("LIVE-1", "Live market")],
            "capacity": {"n_open": 1},
            "fills": [_fill("open", "Live market", "LIVE-1", "2026-09-13T01:00:00Z")],
            "settled": [{"ticker": "LIVE-1", "pnl_usd": 9.99}],
        })
        self.assertIsNone(snap["fills"][0]["pnl_usd"])
        html = _plain(_eval_gate("renderFills(visibleFills(" + json.dumps(snap) + "))"))
        self.assertIn("\u2014", html)
        self.assertNotIn("9.99", html)


class SettledRedeemable(unittest.TestCase):
    """CASH → USDC done: never imply the money is still stuck redeemable."""

    def test_stale_paybox_open_is_overridden_by_cashed(self):
        snap = _norm(WORLD_GHOSTS)
        rows = _eval_gate("settledRows(" + json.dumps(snap) + ")")
        self.assertEqual(len(rows), 2)
        for row in rows:
            state = _eval_gate(
                "redeemableState(" + json.dumps(row) + ", " + json.dumps(snap) + ")"
            )
            self.assertEqual(state, "cashed")
            self.assertTrue(row["stale_redeemable"])
        html = _plain(_eval_gate("renderSettled(" + json.dumps(snap) + ")"))
        self.assertIn("Encaissé", html)
        self.assertNotIn("À réclamer", html)
        self.assertIn("Rien à réclamer", html)
        self.assertIn("+2.85", html)
        self.assertIn("+2.61", html)

    def test_settled_cashed_fill_note_clears_redeemable_open(self):
        raw = {
            "positions": [],
            "capacity": {"n_open": 0},
            "fills": [_fill("close", "MIL Brewers YES", "MBRE", "2026-09-13T04:43:05Z",
                            pnl_usd=2.6072, note="settled_cashed")],
            "settled_paybox": [{
                "market_ticker": "MBRE", "position_result": "won", "redeemable": "open",
                "settlement_asset": "CASH", "market_status": "finalized",
            }],
        }
        snap = _norm(raw)
        row = _eval_gate("settledRows(" + json.dumps(snap) + ")")[0]
        self.assertEqual(
            _eval_gate("redeemableState(" + json.dumps(row) + ", " + json.dumps(snap) + ")"),
            "cashed",
        )
        html = _plain(_eval_gate("renderSettled(" + json.dumps(snap) + ")"))
        self.assertNotIn("À réclamer", html)
        self.assertIn("CASH → USDC", html)

    def test_genuinely_unclaimed_ticket_still_says_a_reclamer(self):
        raw = {
            "positions": [],
            "capacity": {"n_open": 0},
            "fills": [],
            "settled_paybox": [{
                "market_ticker": "WAITING-1", "position_result": "won", "redeemable": "open",
                "settlement_asset": "CASH", "market_status": "finalized",
            }],
        }
        snap = _norm(raw)
        row = _eval_gate("settledRows(" + json.dumps(snap) + ")")[0]
        self.assertEqual(
            _eval_gate("redeemableState(" + json.dumps(row) + ", " + json.dumps(snap) + ")"),
            "open",
        )
        html = _plain(_eval_gate("renderSettled(" + json.dumps(snap) + ")"))
        self.assertIn("À réclamer", html)
        self.assertNotIn("Rien à réclamer", html)

    def test_no_settled_rows_renders_nothing(self):
        self.assertEqual(_eval_gate("renderSettled(" + json.dumps({"fills": []}) + ")"), "")


class UtilisationRegression(unittest.TestCase):
    """Utilisation denominator stays the live bankroll — never 8 × ticket."""

    def test_live_shaped_snapshot_is_35_over_bankroll(self):
        snap = _norm(WORLD_GHOSTS)
        util = _eval_gate("capUtil(" + json.dumps(snap) + ")")
        self.assertEqual(util["open"], 35.0)
        self.assertAlmostEqual(util["max"], 50.6)
        self.assertNotEqual(util["max"], 40)
        self.assertEqual(round(util["pct"]), 69)
        html = _plain(_eval_gate("renderRunway(" + json.dumps(snap) + ")"))
        self.assertIn("35.00 $ / 50.60 $", html)
        self.assertIn("69 %", html)
        self.assertNotIn("/ 40.00", html)

    def test_ticket_cap_40_never_becomes_the_denominator(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["capacity"]["max_open_usd"] = 40.0  # old pipeline wrote 8 × $5 here
        raw["utilization"] = {"open_usd": 35.0, "max_usd": 40.0, "basis": "deployed / max_open_usd"}
        util = _eval_gate("capUtil(" + json.dumps(_norm(raw)) + ")")
        self.assertAlmostEqual(util["max"], 50.6)
        self.assertNotEqual(util["max"], 40)
        self.assertNotEqual(round(util["pct"]), 88)  # 35 / 40


class ConsistencyChecks(unittest.TestCase):
    """Cards must agree with the snapshot fields behind them."""

    def test_clean_snapshot_raises_no_flag(self):
        self.assertEqual(_eval_gate("consistencyIssues(" + json.dumps(_norm(WORLD_GHOSTS)) + ")"), [])

    def test_bankroll_card_vs_cashflow_mismatch(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["capacity"]["bankroll_usd"] = 40.0
        keys = [i["key"] for i in _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")]
        self.assertIn("bankroll", keys)

    def test_idle_plus_deployed_vs_bankroll_mismatch(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["cashflow"]["idle_usd"] = 5.0
        raw["cashflow"]["idle_usdc"] = 5.0
        raw["cashflow"]["usdc"] = 5.0
        keys = [i["key"] for i in _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")]
        self.assertIn("reconcile", keys)

    def test_open_count_vs_positions_mismatch(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["capacity"]["n_open"] = 13
        issues = _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")
        keys = [i["key"] for i in issues]
        self.assertIn("n_open", keys)
        self.assertIn("13", " ".join(i["msg"] for i in issues))

    def test_realized_pnl_missing_a_close_is_flagged(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["cashflow"]["realized_pnl_usd"] = 1.13  # forgot the two settled wins
        keys = [i["key"] for i in _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")]
        self.assertIn("realized", keys)

    def test_closes_outside_the_fills_window_are_not_flagged(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["cashflow"]["realized_pnl_usd"] = 41.0  # older closes, dropped from the window
        keys = [i["key"] for i in _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")]
        self.assertNotIn("realized", keys)

    def test_duplicate_buy_of_live_ticket_is_flagged(self):
        raw = json.loads(json.dumps(WORLD_GHOSTS))
        raw["fills"].insert(0, _fill("open", "PIT Steelers YES", "WXNFL-26SEP13ATLPIT-PSTE",
                                     "2026-09-13T00:00:00Z"))
        keys = [i["key"] for i in _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")]
        self.assertIn("ghost_open", keys)

    def test_garbage_snapshots_never_throw(self):
        for raw in (None, 1, "x", [], {"fills": "nope", "positions": 3},
                    {"fills": [None, 7, {"action": "close"}], "settled": "no",
                     "settled_paybox": {"a": {"market_ticker": "T", "redeemable": "open"}},
                     "capacity": "nope", "cashflow": []}):
            snap = _norm(raw)
            self.assertIsInstance(snap, dict)
            self.assertIsInstance(_eval_gate("visibleFills(" + json.dumps(snap) + ")"), list)
            self.assertIsInstance(_eval_gate("consistencyIssues(" + json.dumps(snap) + ")"), list)
            self.assertIsInstance(_eval_gate("renderSettled(" + json.dumps(snap) + ")"), str)
            self.assertIsInstance(
                _eval_gate("renderFills(visibleFills(" + json.dumps(snap) + "))"), str
            )

    def test_checked_in_snapshot_is_self_consistent(self):
        raw = json.loads((ROOT / "assets" / "world-live.json").read_text())
        issues = _eval_gate("consistencyIssues(" + json.dumps(_norm(raw)) + ")")
        self.assertEqual(issues, [], f"world-live.json contradicts itself: {issues}")


class CacheBust(unittest.TestCase):
    def test_world_tab_assets_share_one_version_token(self):
        html = (ROOT / "dashboard.html").read_text(encoding="utf-8", errors="replace")
        gen = (ROOT / "nabu_dashboard.py").read_text(encoding="utf-8")
        js = (ROOT / "assets" / "world-tab.js").read_text(encoding="utf-8")
        tag = re.search(r'assets/world-tab\.js\?v=([A-Za-z0-9._-]+)', html)
        self.assertIsNotNone(tag, "dashboard.html must cache-bust world-tab.js")
        version = tag.group(1)
        self.assertNotEqual(version, "bankroll1", "bump the cache-bust token with UI changes")
        self.assertIn(f'assets/world-tab.js?v={version}', gen)
        self.assertIn(f'assets/world-tab.css?v={version}', js)


if __name__ == "__main__":
    unittest.main()
