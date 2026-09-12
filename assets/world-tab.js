/* Isolated World tab. Additive overlay — does not rewrite original dashboard UI. */
(function () {
  "use strict";

  var SNAPSHOT_URL = "assets/world-live.json";
  var CSS_URL = "assets/world-tab.css";
  var WALLET_FALLBACK = "27bcZ8xT8qWzkmdyjKy7mRXKqRAR9KBphZt3BMyjmac3";
  var POLL_MS = 8000;
  var SEEN_KEY = "nabu-world-seen-geofence";
  var SOUND_KEY = "nabu-world-sound";
  var NBSP = "\u00a0";
  var root, banner, navLink, snapshot = null, pollTimer = null;

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
      html += '<div class="nabu-world-cell"><div class="nabu-world-cell-k">' + label + '</div>'
        + '<div class="nabu-world-cell-v' + (neg ? " nabu-world-neg" : "") + '">' + txt + '</div></div>';
    }
    html += "</div>";
    if (!any) {
      return '<p class="nabu-world-empty">Cashflow UNVERIFIED — le snapshot ne porte pas de résumé PnL. Aucun zéro inventé.</p>';
    }
    return html;
  }

  function actionPill(action) {
    var a = String(action || "").toLowerCase();
    if (a === "close" || a === "settle" || a === "soldé") {
      return '<span class="nabu-world-pill nabu-world-pill--ok">soldé</span>';
    }
    if (a === "open" || a === "buy") {
      return '<span class="nabu-world-pill nabu-world-pill--ok">' + esc(action) + "</span>";
    }
    return esc(action || "—");
  }

  function renderPositions(rows) {
    if (!rows || !rows.length) {
      return '<p class="nabu-world-empty">Aucune position ouverte dans ce snapshot.</p>';
    }
    var html = '<div class="nabu-world-tbl-wrap wrap"><table class="nabu-world-tbl"><thead><tr>'
      + "<th>Marché</th><th>Track</th><th>Sens</th>"
      + '<th class="nabu-world-num">Taille</th><th class="nabu-world-num">Mark</th>'
      + "<th>Ticker</th><th>Mint</th></tr></thead><tbody>";
    for (var i = 0; i < rows.length; i++) {
      var p = rows[i];
      var track = pick(p, ["track", "book"], "—");
      var mark = isBlank(p.mark) ? "—" : Number(p.mark).toFixed(2);
      html += "<tr>"
        + "<td><b>" + esc(pick(p, ["market", "question", "title"], "—")) + "</b></td>"
        + '<td><span class="nabu-world-track nabu-world-track--' + esc(String(track).toLowerCase()) + '">'
        + esc(track) + "</span></td>"
        + "<td>" + esc(pick(p, ["side", "outcome"], "—")) + "</td>"
        + '<td class="nabu-world-num">' + money(pick(p, ["size_usd", "size", "notional"], null)) + "</td>"
        + '<td class="nabu-world-num">' + esc(mark) + "</td>"
        + "<td>" + esc(pick(p, ["ticker"], "—")) + "</td>"
        + '<td class="nabu-world-mono">' + esc(pick(p, ["mint"], "—")) + "</td>"
        + "</tr>";
    }
    html += "</tbody></table></div>";
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
        + "<td>" + esc(pick(f, ["market", "question", "title"], "—")) + "</td>"
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

  function safeHref(url) {
    if (!url) return "";
    var u = String(url).trim();
    if (/^(https?:|data:text\/|data:application\/)/i.test(u)) return u;
    return "";
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

  function renderPending(list) {
    if (!list || !list.length) return "";
    var html = '<section class="nabu-world-sec" id="nabu-world-pending">'
      + '<div class="nabu-world-kicker">Action en attente · CH Check région</div>'
      + '<div class="nabu-world-rule"></div>';
    for (var i = 0; i < list.length; i++) {
      var p = list[i];
      var status = pick(p, ["status", "state"], "awaiting_region_check");
      var exp = expiryLabel(pick(p, ["expires_at", "token_expiry", "expiry"], ""));
      var url = pick(p, ["geofence_url", "url", "data_url", "link", "href"], "");
      var href = safeHref(url);
      var rid = pick(p, ["request_id", "id", "token_id"], "—");
      html += '<article class="nabu-world-pending" data-request="' + esc(rid) + '">'
        + '<div class="nabu-world-pending-top">'
        + '<div><span class="nabu-world-pill nabu-world-pill--pending">Pending</span> '
        + '<span class="nabu-world-pill ' + (exp.hot ? "nabu-world-pill--dead" : "nabu-world-pill--pending") + '">'
        + esc(status) + "</span>"
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
        + '<div class="nabu-world-url">'
        + '<textarea readonly id="nabu-world-url-' + i + '">' + esc(url) + "</textarea>"
        + '<button type="button" class="nabu-world-btn" data-copy="nabu-world-url-' + i + '">Copier l\'URL</button>'
        + (href ? '<a class="nabu-world-btn nabu-world-btn--ghost" href="' + esc(href)
          + '" target="_blank" rel="noopener">Ouvrir</a>' : "")
        + "</div>"
        + '<div class="nabu-world-actions">'
        + '<button type="button" class="nabu-world-btn" data-notify="1">Autoriser les alertes navigateur</button>'
        + '<button type="button" class="nabu-world-btn nabu-world-btn--ghost" data-sound="1">'
        + (soundOn() ? "Son : on" : "Son : off") + "</button>"
        + "</div></article>";
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

  function readSeen() {
    try {
      var raw = sessionStorage.getItem(SEEN_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (_) { return {}; }
  }

  function writeSeen(map) {
    try { sessionStorage.setItem(SEEN_KEY, JSON.stringify(map)); } catch (_) {}
  }

  function playChime() {
    if (!soundOn()) return;
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      var ctx = playChime._ctx || (playChime._ctx = new Ctx());
      if (ctx.state === "suspended") ctx.resume();
      var o = ctx.createOscillator();
      var g = ctx.createGain();
      o.type = "sine";
      o.frequency.setValueAtTime(880, ctx.currentTime);
      o.frequency.exponentialRampToValueAtTime(520, ctx.currentTime + 0.22);
      g.gain.setValueAtTime(0.0001, ctx.currentTime);
      g.gain.exponentialRampToValueAtTime(0.08, ctx.currentTime + 0.02);
      g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.35);
      o.connect(g); g.connect(ctx.destination);
      o.start(); o.stop(ctx.currentTime + 0.36);
    } catch (_) {}
  }

  function requestNotify() {
    if (!("Notification" in window)) return Promise.resolve("denied");
    if (Notification.permission === "granted" || Notification.permission === "denied") {
      return Promise.resolve(Notification.permission);
    }
    return Notification.requestPermission();
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

  function updateBanner(list, pulse) {
    if (!banner) return;
    if (!list || !list.length) {
      banner.hidden = true;
      banner.classList.remove("is-pulse");
      return;
    }
    var p = list[0];
    var more = list.length > 1 ? " · +" + (list.length - 1) : "";
    banner.innerHTML = '<span class="nabu-world-pill nabu-world-pill--pending">Pending</span>'
      + "<span>Ticket prêt — CH Check région · "
      + esc(pick(p, ["market", "title"], "marché")) + " · track "
      + esc(pick(p, ["track", "book"], "?")) + more + "</span>"
      + '<a href="#world">Ouvrir World</a>';
    banner.hidden = false;
    banner.classList.toggle("is-pulse", !!pulse);
  }

  function alertNewPending(list) {
    if (!list || !list.length) {
      updateBadge(0);
      updateBanner([], false);
      return;
    }
    var seen = readSeen();
    var fresh = [];
    for (var i = 0; i < list.length; i++) {
      var id = pendingKey(list[i]);
      if (id && !seen[id]) fresh.push(list[i]);
    }
    updateBadge(list.length);
    updateBanner(list, fresh.length > 0);
    if (!fresh.length) return;
    pulseNav();
    playChime();
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
    snapshot = data || {};
    var unverified = !!snapshot.unverified;
    var example = snapshot.example === true;
    var wallet = (snapshot.wallet && snapshot.wallet.address) || WALLET_FALLBACK;
    var label = (snapshot.wallet && snapshot.wallet.label) || "PayBox";
    var mode = snapshot.mode || "LIVE_ONLY";
    var ticket = snapshot.ticket_usd;
    var generated = snapshot.generated_at || "—";
    var source = snapshot.source || "assets/world-live.json";
    var pending = collectPending(snapshot);
    var badges = '<span class="nabu-world-badge nabu-world-badge--mode">' + esc(mode) + "</span>"
      + '<span class="nabu-world-badge">Read only</span>';
    if (example) badges += '<span class="nabu-world-badge nabu-world-badge--ex">Exemple</span>';
    if (unverified) badges += '<span class="nabu-world-badge nabu-world-badge--fail">UNVERIFIED</span>';
    if (pending.length) badges += '<span class="nabu-world-badge nabu-world-badge--hot">Check région</span>';

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

    var ticketTxt = isBlank(ticket) ? "—" : money(ticket);
    var body = $("#nabu-world-body", root);
    body.innerHTML =
      '<div class="nabu-world-topline"><span>N*ABU · World.xyz</span><strong>Read only</strong>'
      + '<span class="nabu-world-issue">' + esc(generated) + "</span></div>"
      + '<div class="nabu-world-mast">'
      + '<div class="nabu-world-mast-copy">'
      + '<div class="nabu-world-kicker">World.xyz · PayBox · lecture seule</div>'
      + '<h1 class="nabu-world-title">world</h1>'
      + '<p class="nabu-world-motto">Activité live World.xyz. La planche d\'origine (book / risk / SOUL) reste la surface paper. '
      + "Cet onglet ne signe rien et n'appelle pas PayBox. Un ticket préparé attend le CH Check région.</p>"
      + '<div class="nabu-world-agent-id">' + esc(mode) + "<i></i>tickets " + ticketTxt
      + "<i></i>caps A ≤ $10 · B ≤ $15</div>"
      + "</div>"
      + '<div class="nabu-world-mast-visual" aria-hidden="true">'
      + '<div class="nabu-world-grid"></div><div class="nabu-world-orb"></div>'
      + '<div class="nabu-world-mast-meta">' + badges + "</div>"
      + '<div class="nabu-world-mast-stamp">WORLD / ORB</div>'
      + "</div></div>"
      + warn
      + renderPending(pending)
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
    alertNewPending(pending);
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
    return fetch(SNAPSHOT_URL, { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("http " + r.status);
        return r.json();
      })
      .catch(function () { return unverified(); });
  }

  function applySnapshot(data, fromPoll) {
    var nextKeys = collectPending(data).map(pendingKey).sort().join("|");
    var prevKeys = collectPending(snapshot).map(pendingKey).sort().join("|");
    var sameBody = fromPoll && snapshot && snapshot.generated_at === data.generated_at && nextKeys === prevKeys;
    if (sameBody) {
      tickExpiry();
      return;
    }
    render(data);
    syncHash();
  }

  function openWorld() {
    if (!root) return;
    root.hidden = false;
    if (navLink) navLink.classList.add("is-active");
  }

  function closeWorld() {
    if (!root) return;
    root.hidden = true;
    if (navLink) navLink.classList.remove("is-active");
  }

  function syncHash() {
    if (location.hash === "#world") openWorld();
    else closeWorld();
  }

  function onClick(ev) {
    var t = ev.target;
    if (!t || !t.closest) return;
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
    if (t.closest("[data-sound]")) {
      setSound(!soundOn());
      t.closest("[data-sound]").textContent = soundOn() ? "Son : on" : "Son : off";
    }
  }

  function boot() {
    injectCss();
    injectNav();
    injectPanel();
    injectBanner();
    document.addEventListener("click", onClick);
    loadSnapshot().then(function (data) { applySnapshot(data, false); });
    window.addEventListener("hashchange", syncHash);
    pollTimer = setInterval(function () {
      loadSnapshot().then(function (data) { applySnapshot(data, true); });
    }, POLL_MS);
    setInterval(tickExpiry, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
