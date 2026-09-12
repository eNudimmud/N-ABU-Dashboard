/* Isolated World tab. Additive overlay — does not rewrite original dashboard UI. */
(function () {
  "use strict";

  var SNAPSHOT_URL = "assets/world-live.json";
  var CSS_URL = "assets/world-tab.css";
  var WALLET_FALLBACK = "27bcZ8xT8qWzkmdyjKy7mRXKqRAR9KBphZt3BMyjmac3";
  var NBSP = "\u00a0";
  var root, navLink, snapshot = null;

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
    var txt = (n < 0 ? "\u2212" : "") + Math.abs(n).toLocaleString("en-US", {
      minimumFractionDigits: 2, maximumFractionDigits: 2
    }).replace(/,/g, NBSP) + NBSP + "$";
    return txt;
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
    navLink.textContent = "WD";
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

  function renderPositions(rows) {
    if (!rows || !rows.length) {
      return '<p class="nabu-world-empty">Aucune position ouverte dans ce snapshot.</p>';
    }
    var html = '<div class="wrap"><table class="nabu-world-tbl"><thead><tr>'
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
    var html = '<div class="wrap"><table class="nabu-world-tbl"><thead><tr>'
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
        + "<td>" + esc(pick(f, ["action", "type", "event"], "—")) + "</td>"
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
    var badges = '<span class="nabu-world-badge nabu-world-badge--mode">' + esc(mode) + "</span>"
      + '<span class="nabu-world-badge">Read only</span>';
    if (example) badges += '<span class="nabu-world-badge nabu-world-badge--ex">Exemple</span>';
    if (unverified) badges += '<span class="nabu-world-badge nabu-world-badge--fail">UNVERIFIED</span>';

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
        + "Régénérer depuis les ledgers world-paper pour remplacer ce fichier.</p>";
    } else if (unverified) {
      warn = '<p class="nabu-world-note">Snapshot introuvable ou illisible. Servir la page en HTTP '
        + "(pas <code>file://</code>) et vérifier <code>assets/world-live.json</code>. "
        + "Aucune valeur inventée.</p>";
    }

    var body = $("#nabu-world-body", root);
    body.innerHTML =
      '<div class="nabu-world-kicker">World.xyz · PayBox · lecture seule</div>'
      + '<div class="nabu-world-head"><div>'
      + '<h1 class="nabu-world-title">World</h1>'
      + '<p class="nabu-world-sub">Activité live World.xyz. La planche d\'origine (book / risk / SOUL) reste la surface paper. '
      + "Cet onglet ne signe rien et n'appelle pas PayBox.</p>"
      + "</div><div class=\"nabu-world-badges\">" + badges + "</div></div>"
      + '<div class="nabu-world-rule"></div>' + warn
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
      autonomy: {}
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

  function boot() {
    injectCss();
    injectNav();
    injectPanel();
    loadSnapshot().then(function (data) {
      render(data);
      syncHash();
    });
    window.addEventListener("hashchange", syncHash);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
