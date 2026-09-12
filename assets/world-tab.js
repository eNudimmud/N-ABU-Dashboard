/* Isolated World tab. Additive overlay — does not rewrite original dashboard UI. */
(function () {
  "use strict";

  var SNAPSHOT_URLS = ["assets/world-live.json", "world-live.json"];
  var CSS_URL = "assets/world-tab.css?v=hotfix1";
  var WALLET_FALLBACK = "27bcZ8xT8qWzkmdyjKy7mRXKqRAR9KBphZt3BMyjmac3";
  var POLL_MS = 8000;
  var SEEN_KEY = "nabu-world-seen-geofence";
  var SOUND_KEY = "nabu-world-sound";
  var NOTIFY_ASKED_KEY = "nabu-world-notify-asked";
  var CHIME_STATE_KEY = "nabu-world-chime-state";
  var TOAST_DISMISS_KEY = "nabu-world-toast-dismissed";
  var CHIME_REPEAT_MS = 10000;
  var CHIME_MAX = 5;
  var NBSP = "\u00a0";
  var root, banner, toast, navLink, snapshot = null, pollTimer = null;
  var chimeTimer = null, chimeKey = "";

  function $(sel, el) { return (el || document).querySelector(sel); }

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function isBlank(v) {
    return v == null || v === "" || (typeof v === "number" && !isFinite(v));
  }

  function money(v) {
    if (isBlank(v)) return "—";
    var n = Number(v);
    if (!isFinite(n)) return "—";
    return (n < 0 ? "\u2212" : "") + Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }).replace(/,/g, NBSP) + NBSP + "$";
  }

  function signedMoney(v) {
    if (isBlank(v)) return { txt: "—", neg: false };
    var n = Number(v);
    if (!isFinite(n)) return { txt: "—", neg: false };
    var sign = n > 0 ? "+" : (n < 0 ? "\u2212" : "");
    return {
      txt: sign + Math.abs(n).toLocaleString("en-US", {
        minimumFractionDigits: 2, maximumFractionDigits: 2
      }).replace(/,/g, NBSP) + NBSP + "$",
      neg: n < 0
    };
  }

  function pct(open, max) {
    if (isBlank(open) || isBlank(max) || Number(max) <= 0) return null;
    return (Number(open) / Number(max)) * 100;
  }

  function shortAddr(addr) {
    if (!addr || addr.length < 12) return addr || "—";
    return addr.slice(0, 6) + "\u2026" + addr.slice(-6);
  }

  function shortTx(tx) {
    if (!tx) return "";
    if (tx.length <= 16) return tx;
    return tx.slice(0, 8) + "\u2026" + tx.slice(-6);
  }

  function solscanTx(tx) {
    return "https://solscan.io/tx/" + encodeURIComponent(tx);
  }

  function solscanAddr(addr) {
    return "https://solscan.io/account/" + encodeURIComponent(addr);
  }

  function pick(obj, keys, fallback) {
    if (!obj) return fallback;
    for (var i = 0; i < keys.length; i++) {
      if (!isBlank(obj[keys[i]])) return obj[keys[i]];
    }
    return fallback;
  }

  function numish(v) {
    if (isBlank(v)) return null;
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function copyOwn(src) {
    var out = {};
    if (!src || typeof src !== "object" || Array.isArray(src)) return out;
    for (var k in src) {
      if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
    }
    return out;
  }

  function asRowList(v) {
    if (!v) return [];
    if (Array.isArray(v)) {
      var rows = [];
      for (var i = 0; i < v.length; i++) {
        if (v[i] && typeof v[i] === "object" && !Array.isArray(v[i])) rows.push(v[i]);
      }
      return rows;
    }
    if (typeof v !== "object") return [];
    if (Array.isArray(v.positions)) return asRowList(v.positions);
    if (Array.isArray(v.open)) return asRowList(v.open);
    if (Array.isArray(v.items)) return asRowList(v.items);
    var keys = Object.keys(v);
    var out = [];
    for (var j = 0; j < keys.length; j++) {
      var item = v[keys[j]];
      if (Array.isArray(item)) {
        var nested = asRowList(item);
        for (var n = 0; n < nested.length; n++) {
          var row = copyOwn(nested[n]);
          if (isBlank(row.track) && /^[AB]$/i.test(keys[j])) row.track = keys[j];
          out.push(row);
        }
      } else if (item && typeof item === "object") {
        if (item.market || item.ticker || item.title || item.question || item.size_usd || item.mint) {
          var one = copyOwn(item);
          if (isBlank(one.track) && /^[AB]$/i.test(keys[j])) one.track = keys[j];
          out.push(one);
        }
      }
    }
    return out;
  }

  function normalizeTrackCap(caps, capacity, key) {
    var c = (caps && typeof caps === "object" && !Array.isArray(caps)) ? (caps[key] || {}) : {};
    if (typeof c !== "object" || Array.isArray(c)) c = {};
    var cap = (capacity && typeof capacity === "object" && !Array.isArray(capacity)) ? capacity : {};
    var open = numish(c.open_usd);
    var max = numish(c.max_usd);
    if (open == null) open = numish(cap[key + "_open"]);
    if (max == null) max = numish(pick(cap, [key + "_cap", key + "_max"], null));
    var o = copyOwn(c);
    if (open != null) o.open_usd = open;
    if (max != null) o.max_usd = max;
    return o;
  }

  function normalizePositions(rows) {
    var src = asRowList(rows);
    var out = [];
    for (var i = 0; i < src.length; i++) {
      var o = copyOwn(src[i]);
      if (isBlank(o.mark)) {
        var mk = pick(o, ["mark_usd", "mark_px", "price"], null);
        if (!isBlank(mk)) o.mark = mk;
      }
      if (isBlank(o.entry)) {
        var en = pick(o, ["entry_usd", "entry_px"], null);
        if (!isBlank(en)) o.entry = en;
      }
      if (isBlank(o.size_usd)) {
        var sz = pick(o, ["size", "notional", "ticket"], null);
        if (!isBlank(sz)) o.size_usd = sz;
      }
      out.push(o);
    }
    return out;
  }

  function noteText(note) {
    if (note == null || note === "") return "";
    if (typeof note === "string") return note;
    if (typeof note === "number" && isFinite(note)) return String(note);
    if (typeof note === "object") {
      var t = pick(note, ["note", "summary", "text", "last_decision"], "");
      return t == null ? "" : String(t);
    }
    return String(note);
  }

  function normalizeAutonomy(auto, lastEval) {
    var src = (auto && typeof auto === "object" && !Array.isArray(auto)) ? auto : {};
    if ((!src.note && !src.cycle_id && !src.evaluated_at) && lastEval && typeof lastEval === "object") {
      src = lastEval;
    }
    var o = copyOwn(src);
    if (typeof lastEval === "string" && isBlank(o.note)) o.note = lastEval;
    if (o.note && typeof o.note === "object") {
      var nested = o.note;
      o.note = noteText(nested);
      if (isBlank(o.cycle_id)) o.cycle_id = pick(nested, ["id", "cycle_id", "cycle"], o.cycle_id);
      if (isBlank(o.evaluated_at)) o.evaluated_at = pick(nested, ["evaluated_at", "ts", "iso"], o.evaluated_at);
    } else if (o.note != null && typeof o.note !== "string") {
      o.note = noteText(o.note);
    }
    if (isBlank(o.note) && lastEval && typeof lastEval === "object") {
      o.note = noteText(lastEval);
      if (isBlank(o.cycle_id)) o.cycle_id = pick(lastEval, ["id", "cycle_id"], o.cycle_id);
      if (isBlank(o.evaluated_at)) o.evaluated_at = pick(lastEval, ["evaluated_at", "ts"], o.evaluated_at);
    }
    return o;
  }

  function normalizeCashflow(cash, data) {
    var o = (cash && typeof cash === "object" && !Array.isArray(cash)) ? copyOwn(cash) : {};
    if (isBlank(o.usdc) && !isBlank(data.usdc)) o.usdc = data.usdc;
    if (isBlank(o.total_usd) && !isBlank(data.total_usd)) o.total_usd = data.total_usd;
    if (isBlank(o.bankroll_usd)) {
      var br = pick(data, ["bankroll_usd"], null);
      if (isBlank(br) && data.capacity) br = pick(data.capacity, ["bankroll_usd"], null);
      if (!isBlank(br)) o.bankroll_usd = br;
    }
    return o;
  }

  /* Live score writes capacity.A_open / A_cap / open / updated_at. WD reads caps / positions / generated_at. */
  function normalizeSnapshot(raw) {
    try {
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) return {};
      var out = copyOwn(raw);
      if (isBlank(out.generated_at)) {
        out.generated_at = pick(out, ["updated_at", "generated_at_zh", "updated_zh", "ts"], null);
      }
      if (isBlank(out.ticket_usd) && out.capacity && typeof out.capacity === "object") {
        out.ticket_usd = pick(out.capacity, ["ticket", "ticket_usd"], out.ticket_usd);
      }
      var pos = out.positions;
      if (!Array.isArray(pos) || !pos.length) {
        if (out.open != null) pos = out.open;
      }
      out.positions = normalizePositions(pos);
      out.caps = {
        A: normalizeTrackCap(out.caps, out.capacity, "A"),
        B: normalizeTrackCap(out.caps, out.capacity, "B")
      };
      if (!Array.isArray(out.fills)) out.fills = [];
      out.cashflow = normalizeCashflow(out.cashflow, out);
      out.autonomy = normalizeAutonomy(out.autonomy, out.last_eval);
      return out;
    } catch (_) {
      try { return (raw && typeof raw === "object") ? raw : {}; }
      catch (__) { return {}; }
    }
  }

  function worldOpen() {
    return location.hash === "#world";
  }

  function injectCss() {
    if (document.getElementById("nabu-world-css")) return;
    var link = document.createElement("link");
    link.id = "nabu-world-css";
    link.rel = "stylesheet";
    link.href = CSS_URL;
    document.head.appendChild(link);
  }

  function injectNav() {
    var nav = $(".rail-nav");
    if (!nav || nav.querySelector(".nabu-world-nav")) return;
    navLink = document.createElement("a");
    navLink.href = "#world";
    navLink.className = "nabu-world-nav";
    navLink.setAttribute("aria-label", "World");
    navLink.innerHTML = 'WD<span class="nabu-world-nav-dot" hidden>0</span>';
    nav.appendChild(navLink);
  }

  function injectPanel() {
    if (document.getElementById("nabu-world-root")) {
      root = document.getElementById("nabu-world-root");
      return;
    }
    root = document.createElement("div");
    root.id = "nabu-world-root";
    root.hidden = true;
    root.setAttribute("role", "region");
    root.setAttribute("aria-label", "World.xyz live trading");
    root.innerHTML = '<div class="nabu-world-shell" id="nabu-world-body"></div>';
    document.body.appendChild(root);
  }

  function injectBanner() {
    if (document.getElementById("nabu-world-banner")) {
      banner = document.getElementById("nabu-world-banner");
      return;
    }
    banner = document.createElement("div");
    banner.id = "nabu-world-banner";
    banner.hidden = true;
    banner.setAttribute("role", "status");
    document.body.appendChild(banner);
  }

  function injectToast() {
    if (document.getElementById("nabu-world-toast")) {
      toast = document.getElementById("nabu-world-toast");
      return;
    }
    toast = document.createElement("div");
    toast.id = "nabu-world-toast";
    toast.hidden = true;
    toast.setAttribute("role", "presentation");
    document.body.appendChild(toast);
  }

  function capStatus(util) {
    if (util == null) return "";
    if (util >= 100) return "is-hot";
    if (util >= 70) return "is-watch";
    return "";
  }

  function renderCaps(caps) {
    caps = caps || {};
    var tracks = ["A", "B"];
    var html = '<div class="nabu-world-caps">';
    for (var i = 0; i < tracks.length; i++) {
      var key = tracks[i];
      var c = caps[key] || {};
      var open = c.open_usd;
      var max = c.max_usd;
      var util = pct(open, max);
      var w = util == null ? 0 : Math.max(0, Math.min(100, util));
      var use = (isBlank(open) || isBlank(max))
        ? "open UNVERIFIED"
        : money(open) + " / " + money(max) + " · " + Math.round(w) + NBSP + "%";
      html += '<div class="nabu-world-cap ' + capStatus(util) + '">'
        + '<div class="nabu-world-cap-top">'
        + '<span class="nabu-world-cap-name">Track ' + esc(key) + '</span>'
        + '<span class="nabu-world-cap-use">' + use + '</span></div>'
        + '<div class="nabu-world-bar" aria-hidden="true"><i style="--w:' + w + '%"></i></div>'
        + '</div>';
    }
    html += "</div>";
    return html;
  }

  function renderCells(cash) {
    cash = cash || {};
    var keys = [
      ["realized_pnl_usd", "PnL réalisé"],
      ["unrealized_pnl_usd", "uPnL"],
      ["fees_usd", "Frais"],
      ["net_usd", "Net"],
      ["usdc", "USDC"],
      ["total_usd", "Total"],
      ["tickets_opened", "Tickets ouverts"],
      ["tickets_closed", "Tickets clos"],
      ["volume_usd", "Volume"]
    ];
    var html = '<div class="nabu-world-cells">';
    var any = false;
    for (var i = 0; i < keys.length; i++) {
      var k = keys[i][0], label = keys[i][1], v = cash[k];
      if (!isBlank(v)) any = true;
      var txt, neg = false;
      if (k.indexOf("tickets") === 0) {
        txt = isBlank(v) ? "—" : String(v);
      } else if (k === "fees_usd" || k === "volume_usd") {
        txt = money(v);
        neg = Number(v) < 0;
      } else {
        var s = signedMoney(v);
        txt = s.txt; neg = s.neg;
      }
      var spark = "";
      if (k === "realized_pnl_usd") spark = sparkline(fillSeries(snapshot && snapshot.fills, "pnl_usd"));
      else if (k === "unrealized_pnl_usd") {
        var marks = [];
        var pos = (snapshot && snapshot.positions) || [];
        for (var pi = 0; pi < pos.length; pi++) {
          if (!isBlank(pos[pi].entry)) marks.push(pos[pi].entry);
          if (!isBlank(pos[pi].mark)) marks.push(pos[pi].mark);
        }
        spark = sparkline(marks);
      }
      html += '<div class="nabu-world-cell"><div class="nabu-world-cell-k">' + label + '</div>'
        + '<div class="nabu-world-cell-v' + (neg ? " nabu-world-neg" : "") + '">' + txt + '</div>'
        + spark + "</div>";
    }
    html += "</div>";
    if (!any) {
      return '<p class="nabu-world-empty">Cashflow UNVERIFIED — le snapshot ne porte pas de résumé PnL. Aucun zéro inventé.</p>';
    }
    return html;
  }

  function sparkline(values) {
    var nums = [];
    for (var i = 0; i < (values || []).length; i++) {
      if (isBlank(values[i])) continue;
      var n = Number(values[i]);
      if (isFinite(n)) nums.push(n);
    }
    if (nums.length < 2) return "";
    var min = Math.min.apply(null, nums), max = Math.max.apply(null, nums);
    var span = max - min || 1;
    var w = 120, h = 36, p = 2;
    var d = "";
    for (var j = 0; j < nums.length; j++) {
      var x = p + (w - p * 2) * (j / (nums.length - 1));
      var y = h - p - ((nums[j] - min) / span) * (h - p * 2);
      d += (j ? "L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }
    return '<svg class="nabu-world-spark" viewBox="0 0 ' + w + " " + h
      + '" preserveAspectRatio="none" aria-hidden="true"><path d="' + d
      + '" fill="none" stroke="#3EE0FF" stroke-width="1.6"/></svg>';
  }

  function fillSeries(fills, key) {
    var out = [];
    var rows = fills || [];
    for (var i = rows.length - 1; i >= 0; i--) {
      if (!isBlank(rows[i][key])) out.push(rows[i][key]);
    }
    return out;
  }

  function actionPill(action) {
    var a = String(action || "").toLowerCase();
    if (a === "close" || a === "settle" || a === "soldé" || a === "completed") {
      return '<span class="nabu-world-pill nabu-world-pill--ok">Completed</span>';
    }
    if (a === "cancel" || a === "cancelled" || a === "canceled" || a === "expired") {
      return '<span class="nabu-world-pill nabu-world-pill--dead">Cancelled</span>';
    }
    if (a === "open" || a === "buy") {
      return '<span class="nabu-world-pill nabu-world-pill--open">' + esc(action) + "</span>";
    }
    return esc(action || "—");
  }

  function renderPositions(rows) {
    if (!rows || !rows.length) {
      return '<p class="nabu-world-empty">Aucune position ouverte dans ce snapshot.</p>';
    }
    var html = '<div class="nabu-world-posgrid">';
    for (var i = 0; i < rows.length; i++) {
      var p = rows[i];
      var track = pick(p, ["track", "book"], "—");
      var mark = numish(pick(p, ["mark", "mark_usd", "mark_px"], null));
      var entry = numish(pick(p, ["entry", "entry_usd", "entry_px"], null));
      var spark = (entry != null && mark != null) ? sparkline([entry, mark]) : "";
      html += '<article class="nabu-world-pos">'
        + '<span class="nabu-world-pill nabu-world-pill--open">open</span> '
        + '<span class="nabu-world-track nabu-world-track--' + esc(String(track).toLowerCase()) + '">'
        + esc(track) + "</span>"
        + "<h3>" + esc(pick(p, ["market", "question", "title"], "—")) + "</h3>"
        + '<div class="nabu-world-pos-meta">'
        + "<span>Sens<b>" + esc(pick(p, ["side", "outcome"], "—")) + "</b></span>"
        + "<span>Taille<b>" + money(pick(p, ["size_usd", "size", "notional"], null)) + "</b></span>"
        + "<span>Mark<b>" + (mark == null ? "—" : mark.toFixed(2)) + "</b></span>"
        + "<span>Ticker<b>" + esc(pick(p, ["ticker"], "—")) + "</b></span>"
        + "</div>" + spark + "</article>";
    }
    html += "</div>";
    return html;
  }

  function renderFills(rows) {
    if (!rows || !rows.length) {
      return '<p class="nabu-world-empty">Aucun fill / close dans ce snapshot.</p>';
    }
    var html = '<div class="nabu-world-tbl-wrap wrap"><table class="nabu-world-tbl"><thead><tr>'
      + "<th>Quand</th><th>Action</th><th>Marché</th><th>Track</th>"
      + '<th class="nabu-world-num">Taille</th><th class="nabu-world-num">PnL</th>'
      + "<th>Tx</th></tr></thead><tbody>";
    for (var i = 0; i < rows.length; i++) {
      var f = rows[i];
      var track = pick(f, ["track", "book"], "—");
      var tx = pick(f, ["tx", "tx_hash", "signature", "sig"], "");
      var pnl = signedMoney(f.pnl_usd);
      var txCell = tx
        ? '<a class="nabu-world-tx" href="' + esc(solscanTx(tx)) + '" target="_blank" rel="noopener">'
          + esc(shortTx(tx)) + "</a>"
        : "—";
      html += "<tr>"
        + "<td>" + esc(pick(f, ["ts", "iso", "time"], "—")) + "</td>"
        + "<td>" + actionPill(pick(f, ["action", "type", "event"], "—")) + "</td>"
        + "<td>" + esc(pick(f, ["market", "question", "title", "ticker"], "—")) + "</td>"
        + '<td><span class="nabu-world-track nabu-world-track--' + esc(String(track).toLowerCase()) + '">'
        + esc(track) + "</span></td>"
        + '<td class="nabu-world-num">' + money(pick(f, ["size_usd", "size"], null)) + "</td>"
        + '<td class="nabu-world-num' + (pnl.neg ? " nabu-world-neg" : "") + '">' + pnl.txt + "</td>"
        + "<td>" + txCell + "</td>"
        + "</tr>";
    }
    html += "</tbody></table></div>";
    return html;
  }

  function collectPending(data) {
    var out = [];
    if (!data) return out;
    var keys = ["pending_geofence", "pending_geofences", "awaiting_region_check"];
    for (var i = 0; i < keys.length; i++) {
      var raw = data[keys[i]];
      if (!raw) continue;
      if (Array.isArray(raw)) {
        for (var j = 0; j < raw.length; j++) if (raw[j] && typeof raw[j] === "object") out.push(raw[j]);
      } else if (typeof raw === "object") {
        var nested = raw.pending || raw.tickets || raw.items;
        if (Array.isArray(nested)) {
          for (var k = 0; k < nested.length; k++) if (nested[k] && typeof nested[k] === "object") out.push(nested[k]);
        } else {
          out.push(raw);
        }
      }
    }
    var seen = {};
    return out.filter(function (p) {
      var id = pendingKey(p);
      if (!id || seen[id]) return false;
      seen[id] = true;
      return true;
    });
  }

  function pendingKey(p) {
    return String(pick(p, ["request_id", "id", "token_id"], "")
      || (pick(p, ["market", "title"], "") + "|" + pick(p, ["track"], "") + "|" + pick(p, ["prepared_at", "ts"], "")));
  }

  function isOpenableGeofenceUrl(url) {
    var u = String(url || "").trim();
    if (/^https:\/\//i.test(u)) return true;
    if (/^data:text\/html(?:;|,|$)/i.test(u)) return true;
    return false;
  }

  function pickOpenableGeofenceUrl(p) {
    if (!p) return "";
    var keys = ["geofence_url", "url", "data_url", "link", "href"];
    for (var i = 0; i < keys.length; i++) {
      var v = p[keys[i]];
      if (!isBlank(v) && isOpenableGeofenceUrl(v)) return String(v).trim();
    }
    return "";
  }

  function isDemoPending(p, snap) {
    if (snap && snap.example === true) return true;
    var rid = String(pick(p, ["request_id", "id", "token_id"], ""));
    if (/^ch-check-/i.test(rid) || /^ETH-2700-WK/i.test(rid)) return true;
    return false;
  }

  function livePending(list, snap) {
    return (list || []).filter(function (p) { return !isDemoPending(p, snap); });
  }

  function pendingUrl(p) {
    return String(pick(p, ["geofence_url", "url", "data_url", "link", "href"], "") || "");
  }

  function isPlainDataUrl(url) {
    return /^data:text\/plain(?:;|,|$)/i.test(String(url || "").trim());
  }

  function isAlertablePending(p, snap) {
    if (!p || isDemoPending(p, snap)) return false;
    if (isPlainDataUrl(pendingUrl(p))) return false;
    return true;
  }

  function alertablePending(list, snap) {
    return (list || []).filter(function (p) { return isAlertablePending(p, snap); });
  }

  function parseExpiry(iso) {
    if (!iso) return null;
    var t = Date.parse(iso);
    return isFinite(t) ? t : null;
  }

  function expiryLabel(iso) {
    var t = parseExpiry(iso);
    if (!t) return { txt: iso ? String(iso) : "—", hot: false };
    var ms = t - Date.now();
    if (ms <= 0) return { txt: "expiré · " + iso, hot: true };
    var s = Math.round(ms / 1000);
    var txt = s < 90 ? s + NBSP + "s" : s < 5400 ? Math.round(s / 60) + NBSP + "min"
      : (s / 3600).toFixed(1) + NBSP + "h";
    return { txt: txt + " restantes · " + iso, hot: s < 300 };
  }

  function renderPending(list, snap) {
    if (!list || !list.length) return "";
    var anyLive = list.some(function (p) { return !isDemoPending(p, snap); });
    var html = '<section class="nabu-world-sec" id="nabu-world-pending">'
      + '<div class="nabu-world-kicker">'
      + (anyLive ? "Action en attente · CH Check région" : "Exemple · pas un ticket live")
      + "</div>"
      + '<div class="nabu-world-rule"></div>';
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var demo = isDemoPending(p, snap);
      var status = pick(p, ["status", "state"], "awaiting_region_check");
      var exp = expiryLabel(pick(p, ["expires_at", "token_expiry", "expiry"], ""));
      var href = pickOpenableGeofenceUrl(p);
      var rid = pick(p, ["request_id", "id", "token_id"], "—");
      html += '<article class="nabu-world-pending' + (demo ? " nabu-world-pending--example" : "")
        + '" data-request="' + esc(rid) + '">'
        + '<div class="nabu-world-pending-top">'
        + "<div>"
        + (demo
          ? '<span class="nabu-world-pill nabu-world-pill--exemple">EXEMPLE</span> '
          : '<span class="nabu-world-pill nabu-world-pill--pending">Pending</span> ')
        + '<span class="nabu-world-pill ' + (demo ? "nabu-world-pill--exemple"
          : (exp.hot ? "nabu-world-pill--dead" : "nabu-world-pill--pending")) + '">'
        + esc(demo ? "pas un ticket live" : status) + "</span>"
        + "<h2>" + esc(pick(p, ["market", "question", "title"], "Ticket préparé")) + "</h2></div>"
        + "</div>"
        + '<div class="nabu-world-pending-grid">'
        + '<div class="nabu-world-card"><span class="nabu-world-card-k">Track</span>'
        + '<span class="nabu-world-card-v">' + esc(pick(p, ["track", "book"], "—")) + "</span></div>"
        + '<div class="nabu-world-card"><span class="nabu-world-card-k">Taille</span>'
        + '<span class="nabu-world-card-v">' + money(pick(p, ["size_usd", "size", "ticket"], null)) + "</span></div>"
        + '<div class="nabu-world-card"><span class="nabu-world-card-k">Sens</span>'
        + '<span class="nabu-world-card-v">' + esc(pick(p, ["side", "outcome"], "—")) + "</span></div>"
        + '<div class="nabu-world-card"><span class="nabu-world-card-k">request_id</span>'
        + '<span class="nabu-world-card-v">' + esc(rid) + "</span></div>"
        + '<div class="nabu-world-card"><span class="nabu-world-card-k">Token expiry</span>'
        + '<span class="nabu-world-card-v nabu-world-expiry' + (exp.hot ? " is-hot" : "") + '" data-expiry="'
        + esc(pick(p, ["expires_at", "token_expiry", "expiry"], "")) + '">' + exp.txt + "</span></div>"
        + "</div>"
        + (p.note ? '<p class="nabu-world-note" style="color:inherit;margin:12px 0 0">' + esc(p.note) + "</p>" : "")
        + (href
          ? '<div class="nabu-world-url">'
            + '<textarea readonly id="nabu-world-url-' + i + '">' + esc(href) + "</textarea>"
            + '<button type="button" class="nabu-world-btn" data-copy="nabu-world-url-' + i + '">Copier l\'URL</button>'
            + '<a class="nabu-world-btn nabu-world-btn--ghost" href="' + esc(href)
            + '" target="_blank" rel="noopener">Ouvrir</a></div>'
          : '<p class="nabu-world-url-invalid">DEMO / URL invalide — attendre le vrai Check région du chat</p>'
            + '<div class="nabu-world-url">'
            + '<button type="button" class="nabu-world-btn nabu-world-btn--ghost" disabled aria-disabled="true">Ouvrir</button>'
            + "</div>")
        + (demo || !isAlertablePending(p, snap) ? "" : ('<div class="nabu-world-actions">'
          + notifyCtaHtml()
          + '<button type="button" class="nabu-world-btn nabu-world-btn--ghost" data-sound="1">'
          + (soundOn() ? "Son : on" : "Son : off") + "</button></div>"))
        + "</article>";
    }
    html += "</section>";
    return html;
  }

  function soundOn() {
    try { return localStorage.getItem(SOUND_KEY) !== "0"; } catch (_) { return true; }
  }

  function setSound(on) {
    try { localStorage.setItem(SOUND_KEY, on ? "1" : "0"); } catch (_) {}
  }

  function syncSoundButtons() {
    document.querySelectorAll("[data-sound]").forEach(function (el) {
      el.textContent = soundOn() ? "Son : on" : "Son : off";
    });
  }

  function readJsonStore(key, storage) {
    try {
      var raw = storage.getItem(key);
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }

  function writeJsonStore(key, storage, map) {
    try { storage.setItem(key, JSON.stringify(map)); } catch (_) {}
  }

  function readSeen() { return readJsonStore(SEEN_KEY, sessionStorage); }
  function writeSeen(map) { writeJsonStore(SEEN_KEY, sessionStorage, map); }
  function readChimeState() { return readJsonStore(CHIME_STATE_KEY, sessionStorage); }
  function writeChimeState(map) { writeJsonStore(CHIME_STATE_KEY, sessionStorage, map); }
  function readToastDismissed() { return readJsonStore(TOAST_DISMISS_KEY, sessionStorage); }
  function writeToastDismissed(map) { writeJsonStore(TOAST_DISMISS_KEY, sessionStorage, map); }

  function playChime() {
    if (!soundOn()) return;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = playChime._ctx || (playChime._ctx = new Ctx());
      if (ctx.state === "suspended") ctx.resume();
      function tone(freq, t0, dur, peak) {
        var o = ctx.createOscillator();
        var g = ctx.createGain();
        o.type = "triangle";
        o.frequency.setValueAtTime(freq, t0);
        g.gain.setValueAtTime(0.0001, t0);
        g.gain.exponentialRampToValueAtTime(peak, t0 + 0.012);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);
        o.connect(g); g.connect(ctx.destination);
        o.start(t0); o.stop(t0 + dur + 0.02);
      }
      var t = ctx.currentTime;
      tone(880, t, 0.16, 0.28);
      tone(1175, t + 0.13, 0.22, 0.24);
    } catch (_) {}
  }

  function stopChimeLoop(markDismissed) {
    if (chimeTimer) {
      clearInterval(chimeTimer);
      chimeTimer = null;
    }
    if (markDismissed && chimeKey) {
      var st = readChimeState();
      st.key = chimeKey;
      st.dismissed = true;
      writeChimeState(st);
    }
  }

  function startChimeLoop(key) {
    if (!key || !soundOn()) return;
    var st = readChimeState();
    if (st.key === key && (st.dismissed || (st.count || 0) >= CHIME_MAX)) return;
    if (chimeKey === key && chimeTimer) return;
    if (chimeTimer) {
      clearInterval(chimeTimer);
      chimeTimer = null;
    }
    chimeKey = key;
    if (st.key !== key) st = { key: key, count: 0, dismissed: false };
    function beat() {
      if (!soundOn()) { stopChimeLoop(false); return; }
      if (typeof document !== "undefined" && document.hidden) return;
      if ((st.count || 0) >= CHIME_MAX) { stopChimeLoop(false); return; }
      playChime();
      st.count = (st.count || 0) + 1;
      writeChimeState(st);
      if (st.count >= CHIME_MAX) stopChimeLoop(false);
    }
    beat();
    if ((st.count || 0) < CHIME_MAX) {
      chimeTimer = setInterval(beat, CHIME_REPEAT_MS);
    }
  }

  function notifyCtaNeeded() {
    return ("Notification" in window) && Notification.permission !== "granted";
  }

  function notifyCtaHtml() {
    if (!notifyCtaNeeded()) return "";
    var denied = Notification.permission === "denied";
    return '<button type="button" class="nabu-world-btn nabu-world-btn--cta" data-notify="1">Autoriser les alertes</button>'
      + (denied
        ? '<span class="nabu-world-notify-hint">Bloqué par le navigateur — activer les notifications pour ce site.</span>'
        : "");
  }

  function markNotifyAsked() {
    try { localStorage.setItem(NOTIFY_ASKED_KEY, "1"); } catch (_) {}
  }

  function notifyAlreadyAsked() {
    try { return localStorage.getItem(NOTIFY_ASKED_KEY) === "1"; } catch (_) { return false; }
  }

  function refreshAlertChrome() {
    if (!snapshot) return;
    var list = alertablePending(collectPending(snapshot), snapshot);
    updateBanner(list);
    if (!worldOpen()) {
      hideToast();
      return;
    }
    if (list[0] && toast && !toast.hidden) showToast(list[0]);
  }

  function maybeAskNotifyOnce() {
    if (!("Notification" in window)) return;
    if (notifyAlreadyAsked()) return;
    markNotifyAsked();
    if (Notification.permission !== "default") return;
    Notification.requestPermission().then(function () { refreshAlertChrome(); });
  }

  function requestNotify() {
    if (!("Notification" in window)) return Promise.resolve("denied");
    markNotifyAsked();
    var pending = (Notification.permission === "granted" || Notification.permission === "denied")
      ? Promise.resolve(Notification.permission)
      : Notification.requestPermission();
    return pending.then(function (perm) {
      refreshAlertChrome();
      return perm;
    });
  }

  function desktopNotify(p) {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    try {
      var n = new Notification("World · ticket prêt — CH Check région", {
        body: (pick(p, ["market", "title"], "Ticket") + " · track "
          + pick(p, ["track", "book"], "?") + " · " + money(pick(p, ["size_usd", "size"], 5))),
        tag: "nabu-world-" + pendingKey(p),
        requireInteraction: true
      });
      n.onclick = function () {
        try { window.focus(); } catch (_) {}
        location.hash = "#world";
        n.close();
      };
    } catch (_) {}
  }

  function updateBadge(n) {
    if (!navLink) navLink = $(".nabu-world-nav");
    if (!navLink) return;
    var dot = navLink.querySelector(".nabu-world-nav-dot");
    navLink.classList.toggle("has-pending", n > 0);
    navLink.classList.toggle("is-pulse", n > 0);
    if (dot) {
      dot.hidden = n <= 0;
      dot.textContent = String(n);
    }
    navLink.setAttribute("aria-label", n > 0 ? "World — " + n + " action en attente" : "World");
  }

  function pulseNav() {
    if (!navLink) return;
    navLink.classList.remove("is-flash");
    void navLink.offsetWidth;
    navLink.classList.add("is-flash");
  }

  function updateBanner(list) {
    if (!banner) return;
    if (!list || !list.length) {
      banner.hidden = true;
      banner.classList.remove("is-pulse");
      return;
    }
    var p = list[0];
    var more = list.length > 1 ? " · +" + (list.length - 1) : "";
    banner.innerHTML = '<span class="nabu-world-pill nabu-world-pill--pending">Pending</span>'
      + '<span class="nabu-world-banner-msg">Ticket prêt — CH Check région · '
      + esc(pick(p, ["market", "title"], "marché")) + " · track "
      + esc(pick(p, ["track", "book"], "?")) + more + "</span>"
      + notifyCtaHtml()
      + '<button type="button" class="nabu-world-btn nabu-world-btn--ghost" data-sound="1">'
      + (soundOn() ? "Son : on" : "Son : off") + "</button>"
      + '<a href="#world">Ouvrir World</a>';
    banner.hidden = false;
    banner.classList.add("is-pulse");
  }

  function toastHtml(p) {
    var href = pickOpenableGeofenceUrl(p);
    var market = pick(p, ["market", "question", "title"], "Ticket préparé");
    var rid = pendingKey(p);
    return '<div class="nabu-world-toast-card" role="alertdialog" aria-modal="true" aria-label="CH Check région">'
      + '<div class="nabu-world-kicker">Ticket prêt · CH Check région</div>'
      + "<h2>" + esc(market) + "</h2>"
      + "<p>Track " + esc(pick(p, ["track", "book"], "?")) + " · "
      + money(pick(p, ["size_usd", "size", "ticket"], 5)) + "</p>"
      + notifyCtaHtml()
      + (href
        ? '<div class="nabu-world-url">'
          + '<textarea readonly id="nabu-world-toast-url">' + esc(href) + "</textarea>"
          + '<button type="button" class="nabu-world-btn" data-copy="nabu-world-toast-url">Copier</button>'
          + '<a class="nabu-world-btn nabu-world-btn--ghost" href="' + esc(href)
          + '" target="_blank" rel="noopener">Ouvrir</a></div>'
        : '<p class="nabu-world-url-invalid">URL geofence pas encore ouvrable</p>')
      + '<button type="button" class="nabu-world-btn nabu-world-btn--ghost" data-dismiss-toast="'
      + esc(rid) + '">OK, vu</button></div>';
  }

  function hideToast() {
    if (!toast) return;
    toast.hidden = true;
    toast.innerHTML = "";
  }

  function showToast(p) {
    if (!toast || !p) return;
    var id = pendingKey(p);
    if (!id || readToastDismissed()[id]) {
      hideToast();
      return;
    }
    toast.innerHTML = toastHtml(p);
    toast.hidden = false;
  }

  function dismissToast(id) {
    var map = readToastDismissed();
    if (id) map[id] = 1;
    writeToastDismissed(map);
    hideToast();
    stopChimeLoop(true);
  }

  function alertNewPending(list) {
    if (!list || !list.length) {
      updateBadge(0);
      updateBanner([]);
      hideToast();
      stopChimeLoop(false);
      chimeKey = "";
      return;
    }
    var seen = readSeen();
    var fresh = [];
    for (var i = 0; i < list.length; i++) {
      var id = pendingKey(list[i]);
      if (id && !seen[id]) fresh.push(list[i]);
    }
    updateBadge(list.length);
    updateBanner(list);
    if (worldOpen()) {
      showToast(list[0]);
      startChimeLoop(pendingKey(list[0]));
    } else {
      hideToast();
      stopChimeLoop(false);
    }
    if (!fresh.length) return;
    pulseNav();
    for (var j = 0; j < fresh.length; j++) {
      desktopNotify(fresh[j]);
      seen[pendingKey(fresh[j])] = 1;
    }
    writeSeen(seen);
  }

  function tickExpiry() {
    if (!root) return;
    root.querySelectorAll("[data-expiry]").forEach(function (el) {
      var lab = expiryLabel(el.getAttribute("data-expiry"));
      el.textContent = lab.txt;
      el.classList.toggle("is-hot", lab.hot);
    });
  }

  function render(data) {
    try {
      renderUnsafe(data);
    } catch (_) {
      snapshot = data || snapshot || {};
    }
  }

  function renderUnsafe(data) {
    snapshot = data || {};
    var unverified = !!snapshot.unverified;
    var example = snapshot.example === true;
    var wallet = (snapshot.wallet && snapshot.wallet.address) || WALLET_FALLBACK;
    var label = (snapshot.wallet && snapshot.wallet.label) || "PayBox";
    var mode = snapshot.mode || "LIVE_ONLY";
    var ticket = snapshot.ticket_usd;
    var generated = snapshot.generated_at || snapshot.updated_at || "—";
    var source = snapshot.source || "assets/world-live.json";
    var pending = collectPending(snapshot);
    var alertable = alertablePending(pending, snapshot);
    var badges = '<span class="nabu-world-badge nabu-world-badge--mode">' + esc(mode) + "</span>"
      + '<span class="nabu-world-badge">Read only</span>';
    if (example) badges += '<span class="nabu-world-badge nabu-world-badge--ex">Exemple</span>';
    if (unverified) badges += '<span class="nabu-world-badge nabu-world-badge--fail">UNVERIFIED</span>';
    if (alertable.length) badges += '<span class="nabu-world-badge nabu-world-badge--hot">Check région</span>';

    var autonomy = snapshot.autonomy || {};
    var autoHtml = "";
    if (autonomy.note || autonomy.cycle_id || autonomy.evaluated_at) {
      autoHtml = '<section class="nabu-world-sec"><div class="nabu-world-kicker">Dernière note d\'autonomie</div>'
        + '<div class="nabu-world-rule"></div><div class="nabu-world-eval">'
        + '<div class="nabu-world-eval-meta">'
        + esc(autonomy.cycle_id || "cycle") + " · " + esc(autonomy.evaluated_at || "horodatage absent")
        + "</div>"
        + (autonomy.note ? "<p>" + esc(autonomy.note) + "</p>" : '<p class="nabu-world-empty">Note absente.</p>')
        + "</div></section>";
    }

    var warn = "";
    if (example) {
      warn = '<p class="nabu-world-note">Snapshot d\'exemple — chiffres illustratifs, pas un book live. '
        + "La pipeline autonomie doit réécrire <code>assets/world-live.json</code> dès qu'un ticket est préparé.</p>";
    } else if (unverified) {
      warn = '<p class="nabu-world-note">Snapshot introuvable ou illisible. Servir la page en HTTP '
        + "(pas <code>file://</code>) et vérifier <code>assets/world-live.json</code>. "
        + "Aucune valeur inventée.</p>";
    }

    var body = $("#nabu-world-body", root);
    if (!body) return;
    body.innerHTML =
      '<header class="nabu-world-hero">'
      + '<div class="nabu-world-lockup"><div class="nabu-world-orb" aria-hidden="true"></div>'
      + '<h1 class="nabu-world-title">world</h1></div>'
      + '<p class="nabu-world-sub">World.xyz · PayBox · lecture seule. La planche d\'origine (book / risk / SOUL) reste inchangée. '
      + "Cet onglet ne signe rien. Un ticket préparé attend le CH Check région.</p>"
      + '<div class="nabu-world-badges">' + badges + "</div></header>"
      + warn
      + renderPending(pending, snapshot)
      + '<div class="nabu-world-map" aria-hidden="true"><span class="nabu-world-map-label">World field</span></div>'
      + '<div class="nabu-world-meta">'
      + '<div class="nabu-world-card"><span class="nabu-world-card-k">Portefeuille ' + esc(label) + "</span>"
      + '<span class="nabu-world-card-v"><a href="' + esc(solscanAddr(wallet)) + '" target="_blank" rel="noopener" title="'
      + esc(wallet) + '">' + esc(shortAddr(wallet)) + "</a></span></div>"
      + '<div class="nabu-world-card"><span class="nabu-world-card-k">Ticket</span>'
      + '<span class="nabu-world-card-v">' + (isBlank(ticket) ? "—" : money(ticket)) + "</span></div>"
      + '<div class="nabu-world-card"><span class="nabu-world-card-k">Snapshot</span>'
      + '<span class="nabu-world-card-v">' + esc(generated) + "</span></div>"
      + "</div>"
      + '<section class="nabu-world-sec"><div class="nabu-world-kicker">Caps A / B · open vs max</div>'
      + '<div class="nabu-world-rule"></div>' + renderCaps(snapshot.caps) + "</section>"
      + '<section class="nabu-world-sec"><div class="nabu-world-kicker">Cashflow / PnL</div>'
      + '<div class="nabu-world-rule"></div>' + renderCells(snapshot.cashflow) + "</section>"
      + '<section class="nabu-world-sec"><div class="nabu-world-kicker">Positions ouvertes</div>'
      + '<div class="nabu-world-rule"></div>' + renderPositions(snapshot.positions) + "</section>"
      + '<section class="nabu-world-sec"><div class="nabu-world-kicker">Fills / closes récents</div>'
      + '<div class="nabu-world-rule"></div>' + renderFills(snapshot.fills) + "</section>"
      + autoHtml
      + '<p class="nabu-world-foot"><b>Lecture seule.</b> Source : ' + esc(source)
      + ". En cas de conflit, les ledgers world-paper et le wallet PayBox gagnent — "
      + "ce JSON n'est qu'un tirage. Voir <code>scripts/refresh_world_snapshot.py</code>.</p>";
    alertNewPending(alertable);
  }

  function unverified() {
    return {
      schema_version: 1,
      unverified: true,
      example: false,
      mode: "LIVE_ONLY",
      ticket_usd: 5,
      wallet: { chain: "solana", address: WALLET_FALLBACK, label: "PayBox" },
      caps: { A: { max_usd: 10 }, B: { max_usd: 15 } },
      positions: [],
      fills: [],
      cashflow: {},
      autonomy: {},
      pending_geofence: null
    };
  }

  function loadSnapshot() {
    function one(i) {
      if (i >= SNAPSHOT_URLS.length) return Promise.resolve(unverified());
      return fetch(SNAPSHOT_URLS[i], { cache: "no-store" })
        .then(function (r) {
          if (!r.ok) throw new Error("http " + r.status);
          return r.json();
        })
        .then(function (data) { return normalizeSnapshot(data); })
        .catch(function () { return one(i + 1); });
    }
    return one(0);
  }

  function applySnapshot(data, fromPoll) {
    try {
      data = normalizeSnapshot(data);
      var nextKeys = collectPending(data).map(pendingKey).sort().join("|");
      var prevKeys = collectPending(snapshot).map(pendingKey).sort().join("|");
      var stamp = function (d) { return d && (d.generated_at || d.updated_at || ""); };
      var sameBody = fromPoll && snapshot && stamp(snapshot) === stamp(data) && nextKeys === prevKeys;
      if (sameBody) {
        tickExpiry();
        return;
      }
      render(data);
    } catch (_) {
      try { render(unverified()); } catch (__) {}
    }
    syncHash();
  }

  function setWorldChrome(el, open) {
    if (!el) return;
    el.hidden = !open;
    el.setAttribute("aria-hidden", open ? "false" : "true");
    try { el.inert = !open; } catch (_) {}
    el.style.pointerEvents = open ? "" : "none";
  }

  function openWorld() {
    if (!root) return;
    setWorldChrome(root, true);
    if (navLink) navLink.classList.add("is-active");
    maybeAskNotifyOnce();
    if (snapshot) {
      var list = alertablePending(collectPending(snapshot), snapshot);
      if (list.length) {
        showToast(list[0]);
        startChimeLoop(pendingKey(list[0]));
      }
    }
  }

  function closeWorld() {
    setWorldChrome(root, false);
    if (navLink) navLink.classList.remove("is-active");
    hideToast();
    stopChimeLoop(false);
  }

  function syncHash() {
    if (worldOpen()) openWorld();
    else closeWorld();
  }

  function onClick(ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
    var railA = t.closest(".rail-nav a");
    if (railA) {
      var href = railA.getAttribute("href") || "";
      if (href !== "#world") closeWorld();
    }
    var copy = t.closest("[data-copy]");
    if (copy) {
      var el = document.getElementById(copy.getAttribute("data-copy"));
      if (el) {
        var txt = el.value || el.textContent || "";
        var done = function () { copy.textContent = "Copié"; };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(txt).then(done).catch(function () {
            el.select(); document.execCommand("copy"); done();
          });
        } else {
          el.select(); document.execCommand("copy"); done();
        }
      }
      return;
    }
    if (t.closest("[data-notify]")) {
      requestNotify();
      return;
    }
    var dismiss = t.closest("[data-dismiss-toast]");
    if (dismiss) {
      dismissToast(dismiss.getAttribute("data-dismiss-toast"));
      return;
    }
    if (t.closest("[data-sound]")) {
      setSound(!soundOn());
      syncSoundButtons();
      if (soundOn() && snapshot && worldOpen()) {
        var next = alertablePending(collectPending(snapshot), snapshot);
        if (next[0]) startChimeLoop(pendingKey(next[0]));
      } else {
        stopChimeLoop(false);
      }
    }
  }

  function boot() {
    injectCss();
    injectNav();
    injectPanel();
    injectBanner();
    injectToast();
    document.addEventListener("click", onClick, true);
    syncHash();
    loadSnapshot().then(function (data) { applySnapshot(data, false); });
    window.addEventListener("hashchange", syncHash);
    pollTimer = setInterval(function () {
      loadSnapshot().then(function (data) { applySnapshot(data, true); });
    }, POLL_MS);
    setInterval(tickExpiry, 15000);
  }

  if (typeof window !== "undefined") {
    window.NabuWorldGate = {
      isOpenableGeofenceUrl: isOpenableGeofenceUrl,
      isPlainDataUrl: isPlainDataUrl,
      isDemoPending: isDemoPending,
      isAlertablePending: isAlertablePending,
      livePending: livePending,
      alertablePending: alertablePending,
      pendingUrl: pendingUrl,
      normalizeSnapshot: normalizeSnapshot,
      worldOpen: worldOpen
    };
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
