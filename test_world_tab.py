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
        self.assertFalse(snap["example"])
        self.assertEqual(snap["mode"], "LIVE_ONLY")
        self.assertEqual(snap["wallet"]["address"], rws.WALLET)
        self.assertEqual(snap["caps"]["A"]["max_usd"], 10)
        self.assertEqual(snap["caps"]["B"]["max_usd"], 15)
        self.assertGreaterEqual(len(snap["positions"]), 1)
        self.assertGreaterEqual(len(snap["fills"]), 1)
        self.assertIn("cashflow", snap)
        self.assertTrue(snap["autonomy"]["note"])
        self.assertIsNone(snap["pending_geofence"])

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
        self.assertIn("nabu-world-lockup", js)
        self.assertIn("nabu-world-orb", js)
        self.assertIn("Completed", js)

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
        "isOpenableGeofenceUrl",
        "pendingUrl",
        "isPlainDataUrl",
        "isDemoPending",
        "isAlertablePending",
        "livePending",
        "alertablePending",
    ]
    bundle = "\n".join(_extract_js_function(js, n) for n in names)
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


if __name__ == "__main__":
    unittest.main()
