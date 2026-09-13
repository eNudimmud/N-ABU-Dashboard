/* DOM-level regression guard for the CH Check région alert.
 *
 * The bug JD hit: a pending_geofence landed in world-live.json while he was on
 * another dashboard tab, and the badge / chime / notification never fired
 * because they were gated on the World panel being open. This drives the real
 * assets/world-tab.js inside a jsdom dashboard with the World panel CLOSED.
 *
 * Needs jsdom. Exits 2 (and prints SKIP) when it is not installed, so the
 * python suite can skip instead of failing on a missing optional dependency.
 *
 *     node scripts/world_alert_dom_check.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

let JSDOM;
try {
  ({ JSDOM } = require("jsdom"));
} catch (_) {
  console.log("SKIP jsdom not installed");
  process.exit(2);
}

const ROOT = path.resolve(__dirname, "..");
let JS = fs.readFileSync(path.join(ROOT, "assets", "world-tab.js"), "utf8");

/* Real logic, scaled clock — the suite must not sit through 8 s polls. */
const SPEEDUPS = [
  [/var POLL_MS = 8000;/, "var POLL_MS = 120;"],
  [/var CHIME_REPEAT_MS = 10000;/, "var CHIME_REPEAT_MS = 150;"],
  [/var TITLE_FLASH_MS = 1400;/, "var TITLE_FLASH_MS = 60;"],
  [/var NOTIFY_REPEAT_MS = 45000;/, "var NOTIFY_REPEAT_MS = 150;"],
  [/setInterval\(tickExpiry, 15000\);/, "setInterval(tickExpiry, 150);"],
];
for (const [re, to] of SPEEDUPS) {
  if (!re.test(JS)) {
    console.log("FAIL world-tab.js no longer carries " + re);
    process.exit(1);
  }
  JS = JS.replace(re, to);
}

const PENDING = {
  request_id: "pbx-btc15m-1153",
  market: "BTC15M YES",
  track: "A",
  side: "YES",
  size_usd: 5,
  status: "awaiting_region_check",
  expires_at: "2099-01-01T00:00:00Z",
  geofence_url: "https://enudimmud.github.io/N-ABU-Dashboard/assets/geofence-buy-btc15m.html",
  note: "VPN off · Suisse · BUY",
};

let served = 0;
function snapshot(pending) {
  served += 1;
  return {
    schema_version: 2,
    example: false,
    mode: "LIVE_ONLY",
    generated_at: "2026-09-13T11:5" + (served % 10) + ":00Z",
    pending_geofence: pending || null,
    awaiting_region_check: !!pending,
    notify: !!pending,
    positions: [],
    fills: [],
    cashflow: {},
  };
}

const state = { snap: snapshot(null), notifications: [], tones: 0 };

const dom = new JSDOM(
  '<!doctype html><html><head><title>N-ABU · Dashboard</title></head><body>' +
    '<nav class="rail-nav"><a href="#portfolio">PF</a><a href="#risk">RK</a></nav>' +
    "</body></html>",
  {
    url: "https://example.test/dashboard.html#portfolio",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  }
);
const win = dom.window;

win.fetch = function () {
  return Promise.resolve({
    ok: true,
    status: 200,
    json: function () { return Promise.resolve(state.snap); },
  });
};

function FakeNotification(title, opts) {
  this.title = title;
  this.options = opts || {};
  this.close = function () {};
  state.notifications.push({ title: title, options: opts || {} });
}
FakeNotification.permission = "granted";
FakeNotification.requestPermission = function () { return Promise.resolve("granted"); };
win.Notification = FakeNotification;

win.AudioContext = function () {
  this.state = "running";
  this.currentTime = 0;
  this.destination = {};
  this.resume = function () { this.state = "running"; return Promise.resolve(); };
  this.createOscillator = function () {
    return {
      frequency: { setValueAtTime: function () {} },
      connect: function () {},
      start: function () { state.tones += 1; },
      stop: function () {},
    };
  };
  this.createGain = function () {
    return {
      gain: { setValueAtTime: function () {}, exponentialRampToValueAtTime: function () {} },
      connect: function () {},
    };
  };
};

const store = {};
const storage = {
  getItem: (k) => (Object.prototype.hasOwnProperty.call(store, k) ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
  removeItem: (k) => { delete store[k]; },
  clear: () => { for (const k in store) delete store[k]; },
};
Object.defineProperty(win, "localStorage", { value: storage, configurable: true });
Object.defineProperty(win, "sessionStorage", { value: storage, configurable: true });

win.eval(JS);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const POLLS = (n) => sleep(140 * n);

function badge() {
  const dot = win.document.querySelector(".nabu-world-nav-dot");
  return dot ? { hidden: dot.hidden, text: dot.textContent } : null;
}

function bannerHidden() {
  const b = win.document.getElementById("nabu-world-banner");
  return b ? b.hidden : null;
}

/* The title alternates between the alert and the original, so sample a window. */
async function titlesOverAWindow() {
  const seen = [];
  for (let i = 0; i < 12; i++) {
    seen.push(win.document.title);
    await sleep(30);
  }
  return seen;
}

const failures = [];
function check(name, ok, detail) {
  console.log((ok ? "PASS " : "FAIL ") + name + (detail == null ? "" : " — " + detail));
  if (!ok) failures.push(name);
}

(async function run() {
  await POLLS(3);
  check("quiet snapshot leaves the badge hidden", badge() && badge().hidden === true);
  check("quiet snapshot leaves the tab title alone",
    win.document.title === "N-ABU · Dashboard", win.document.title);

  state.snap = snapshot(PENDING);
  await POLLS(4);

  check("World panel stayed closed", win.location.hash === "#portfolio", win.location.hash);
  check("nav badge counts the pending ticket",
    badge() && badge().hidden === false && badge().text === "1", JSON.stringify(badge()));
  check("banner is visible off #world", bannerHidden() === false);
  check("desktop notification fired off #world", state.notifications.length >= 1,
    JSON.stringify(state.notifications[0] || null));
  check("notification is requireInteraction",
    !!(state.notifications[0] && state.notifications[0].options.requireInteraction === true));
  check("chime rang off #world", state.tones >= 3, "tones=" + state.tones);
  const titles = await titlesOverAWindow();
  check("tab title flashes the ticket",
    titles.some((t) => t === "(1) Check région · BTC15M YES"),
    JSON.stringify(Array.from(new Set(titles))));
  check("tab title flash keeps the original title in the rotation",
    titles.some((t) => t === "N-ABU · Dashboard"));

  const tonesBefore = state.tones;
  const notifsBefore = state.notifications.length;
  Object.defineProperty(win.document, "hidden", { value: true, configurable: true });
  await POLLS(4);
  check("hidden tab keeps chiming", state.tones > tonesBefore,
    tonesBefore + " -> " + state.tones);
  check("hidden tab re-fires the notification", state.notifications.length > notifsBefore,
    notifsBefore + " -> " + state.notifications.length);
  check("re-fired notification sets renotify",
    state.notifications.slice(notifsBefore).some((n) => n.options.renotify === true));
  Object.defineProperty(win.document, "hidden", { value: false, configurable: true });

  state.snap = snapshot(Object.assign({}, PENDING, { expires_at: "2020-01-01T00:00:00Z" }));
  await POLLS(4);
  check("expired token clears the badge", badge() && badge().hidden === true,
    JSON.stringify(badge()));
  check("expired token hides the banner", bannerHidden() === true);
  check("expired token restores the tab title",
    win.document.title === "N-ABU · Dashboard", win.document.title);

  const notifsAfterExpiry = state.notifications.length;
  state.snap = snapshot(Object.assign({}, PENDING, { request_id: "pbx-btc15m-1210" }));
  await POLLS(4);
  check("the next ticket alerts again", badge() && badge().hidden === false,
    JSON.stringify(badge()));
  check("the next ticket gets its own notification",
    state.notifications.length > notifsAfterExpiry, "n=" + state.notifications.length);

  console.log(failures.length ? "\nFAILED: " + failures.join(", ") : "\nall checks passed");
  process.exit(failures.length ? 1 : 0);
})();
