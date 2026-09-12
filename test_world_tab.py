#!/usr/bin/env python3
"""World tab stays additive: snapshot format, refresh script, generator hook."""

from __future__ import annotations

import json
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
        self.assertEqual(snap["fills"][0]["action"], "close")
        self.assertEqual(snap["autonomy"]["note"], "Stand down. B has room.")
        self.assertEqual(snap["cashflow"]["tickets_opened"], 1)
        self.assertEqual(snap["cashflow"]["tickets_closed"], 1)
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
        self.assertIn("nabu-world-lockup", js)
        self.assertIn("nabu-world-orb", js)
        self.assertIn("Completed", js)
        self.assertIn("normalizeSnapshot", js)
        self.assertIn("capacity.A_open", js)
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
        "normalizeSnapshot",
    ]
    preamble = (
        "var URL_KEYS=['check_region_url','geofence_url','url','data_url','link','href'];\n"
        "var SHORT_HTTPS_MAX=280;\n"
        "var NBSP='\\u00a0';\n"
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
        self.assertIn("check_region_url", slot)
        self.assertIn("enudimmud.github.io/N-ABU-Dashboard/assets/geofence-latest.html", slot)


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


if __name__ == "__main__":
    unittest.main()
