/* Embeddable widgets. Usage (iframe src):
     embed.html?w=balance&chamber=senate   -> Senate/House balance-of-power bar
     embed.html?w=state&state=fl           -> one state's turnout summary
     embed.html?w=national                 -> national early-vote headline
   Transparent background; sized for a small iframe. */
(function () {
  "use strict";
  var host = document.getElementById("embed");
  var SITE = location.href.replace(/embed\.html.*$/, "");
  function q(k, d) { var m = new RegExp("[?&]" + k + "=([^&]+)").exec(location.search); return m ? decodeURIComponent(m[1]) : d; }
  function getJSON(u) { return fetch(u + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u); return r.json(); }); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function marginText(m) { return m == null ? "—" : m > 0 ? "R+" + m.toFixed(1) : m < 0 ? "D+" + (-m).toFixed(1) : "Even"; }
  function credit(href) { return "<a class='credit' href='" + SITE + (href || "") + "' target='_blank' rel='noopener'>2026 Turnout &amp; Results Tracker →</a>"; }

  function balance() {
    var chamber = q("chamber", "senate");
    getJSON("data/results/" + chamber + ".json").then(function (d) {
      var b = d.balance; if (!b) { host.textContent = "No balance data."; return; }
      var T = b.total, cx = b.control;
      var seg = function (w, c) { return w ? "<div style='width:" + (100 * w / T) + "%;background:" + c + "'></div>" : ""; };
      var bar = seg(b.dem_called, "#2b6cb0") + seg(b.dem_lead, "#7aa9d6") + seg(b.undecided, "#c9ced6") + seg(b.rep_lead, "#e08f7a") + seg(b.rep_called, "#d62f2f") +
        "<div style='position:absolute;top:-3px;bottom:-3px;left:" + (100 * cx / T) + "%;width:2px;background:var(--ink)'></div>";
      var verdict = b.dem_total >= cx ? "Democrats control" : b.rep_total >= cx ? "Republicans control" : "Undecided";
      host.innerHTML = "<h3>" + (chamber === "house" ? "U.S. House" : "U.S. Senate") + " — balance of power</h3>" +
        "<div class='bar'>" + bar + "</div>" +
        "<div class='row'><span style='color:var(--dem);font-weight:700'>Dem " + b.dem_total + "</span>" +
        "<span style='font-weight:700'>" + verdict + " · " + cx + " to win</span>" +
        "<span style='color:var(--rep);font-weight:700'>Rep " + b.rep_total + "</span></div>" + credit("results.html");
    }).catch(function (e) { host.textContent = "Could not load: " + e.message; });
  }

  function state() {
    var code = (q("state", "fl") || "fl").toLowerCase();
    getJSON("data/status.json").then(function (mf) {
      var s = (mf.states || []).filter(function (x) { return x.code === code; })[0];
      if (!s) { host.textContent = "Unknown state."; return; }
      var lean = s.partisan ? marginText(s.margin) : (s.turnout_pct != null ? s.turnout_pct + "% turnout" : "turnout-only");
      host.innerHTML = "<h3>" + s.name + " — 2026 early vote</h3>" +
        "<div class='big'>" + fmt(s.cast) + "</div><div class='k'>ballots cast</div>" +
        "<div class='row'><span>" + (s.partisan ? "Registration lean" : "Turnout") + "</span><span style='font-weight:700;color:" +
        (s.partisan && s.margin != null ? (s.margin > 0 ? "var(--rep)" : "var(--dem)") : "var(--ink)") + "'>" + lean + "</span></div>" +
        credit("index.html");
    }).catch(function (e) { host.textContent = "Could not load: " + e.message; });
  }

  function national() {
    getJSON("data/status.json").then(function (mf) {
      var tot = 0, R = 0, D = 0, rep = 0, c = mf.counts || {};
      (mf.states || []).forEach(function (x) { tot += x.cast || 0; if (x.partisan && x.has_data) { R += x.rep || 0; D += x.dem || 0; } });
      var tp = R + D, m = tp ? Math.round((100 * R / tp - 100 * D / tp) * 10) / 10 : null;
      host.innerHTML = "<h3>National early vote — 2026</h3>" +
        "<div class='big'>" + fmt(tot) + "</div><div class='k'>ballots cast · " + (c.live || 0) + " of " + (c.total || 50) + " states reporting</div>" +
        "<div class='row'><span>Early-vote lean (party-reg states)</span><span style='font-weight:700;color:" +
        (m != null ? (m > 0 ? "var(--rep)" : "var(--dem)") : "var(--ink)") + "'>" + marginText(m) + "</span></div>" + credit("national.html");
    }).catch(function (e) { host.textContent = "Could not load: " + e.message; });
  }

  var w = q("w", "national");
  if (w === "balance") balance(); else if (w === "state") state(); else national();
})();
