/* National early-vote dashboard — aggregates data/status.json over a US map. */
(function () {
  "use strict";
  var geo = null, mf = null, proj = null, sort = { key: "cast", dir: -1 }, filter = "";
  function $(s) { return document.querySelector(s); }
  function getJSON(u) { return fetch(u + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u); return r.json(); }); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function marginText(m) { return m == null ? "—" : m > 0 ? "R+" + m.toFixed(1) : m < 0 ? "D+" + (-m).toFixed(1) : "Even"; }

  var STOPS = [[-40, [33, 102, 172]], [-20, [103, 169, 207]], [0, [235, 237, 240]], [20, [239, 138, 98]], [40, [178, 24, 43]]];
  function colorM(m) { if (m == null) return "#c9ced6"; if (m < -40) m = -40; if (m > 40) m = 40;
    for (var i = 0; i < 4; i++) { var a = STOPS[i], b = STOPS[i + 1]; if (m >= a[0] && m <= b[0]) { var t = (m - a[0]) / (b[0] - a[0]); var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }); return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } } return "#c9ced6"; }
  function colorT(p) { if (p == null || p <= 0) return "#eef2f4"; var stops = [[0, [237, 242, 244]], [10, [116, 196, 118]], [30, [49, 163, 84]], [60, [0, 90, 40]]]; if (p > 60) p = 60;
    for (var i = 0; i < 3; i++) { var a = stops[i], b = stops[i + 1]; if (p >= a[0] && p <= b[0]) { var t = (p - a[0]) / (b[0] - a[0]); var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }); return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } } return "#eef2f4"; }

  function byCode() { var m = {}; (mf.states || []).forEach(function (s) { m[s.code] = s; }); return m; }
  function stateColor(s) {
    if (!s || !s.has_data) return "#c9ced6";
    if (s.partisan) return colorM(s.margin);
    return s.turnout_pct != null ? colorT(s.turnout_pct) : colorT(3);
  }

  function eachRing(g, cb) { if (!g) return; if (g.type === "Polygon") g.coordinates.forEach(cb); else if (g.type === "MultiPolygon") g.coordinates.forEach(function (p) { p.forEach(cb); }); }
  function buildProjection(feats) {
    var mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    feats.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) { if (p[0] < mnx) mnx = p[0]; if (p[0] > mxx) mxx = p[0]; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), W = 960, s = W / ((mxx - mnx) * k), H = (mxy - mny) * s;
    return { project: function (p) { return [(p[0] - mnx) * k * s, (mxy - p[1]) * s]; }, W: Math.round(W), H: Math.round(H) };
  }
  function pathFor(g) { var d = ""; eachRing(g, function (ring) { for (var i = 0; i < ring.length; i++) { var p = proj.project(ring[i]); d += (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); } d += "Z"; }); return d; }

  function renderMap() {
    var svg = $("#nmap"), idx = byCode();
    var lower = geo.features.filter(function (f) { return f.properties.usps !== "AK" && f.properties.usps !== "HI"; });
    proj = buildProjection(lower);
    var H = proj.H + 70; svg.setAttribute("viewBox", "0 0 " + proj.W + " " + H);
    var parts = lower.map(function (ft) { var u = ft.properties.usps; return "<path d='" + pathFor(ft.geometry) + "' fill='" + stateColor(idx[u.toLowerCase()]) + "' stroke='#fff' stroke-width='0.7' data-u='" + u + "'></path>"; });
    var iy = proj.H + 8;
    [["AK", 10], ["HI", 150]].forEach(function (pr) { var u = pr[0], x = pr[1]; parts.push("<rect x='" + x + "' y='" + iy + "' width='120' height='52' rx='6' fill='" + stateColor(idx[u.toLowerCase()]) + "' stroke='#fff' data-u='" + u + "'></rect><text class='inset' x='" + (x + 60) + "' y='" + (iy + 30) + "' text-anchor='middle'>" + u + "</text>"); });
    svg.innerHTML = parts.join("");
    var tip = $("#tooltip");
    svg.querySelectorAll("[data-u]").forEach(function (n) {
      n.style.cursor = "pointer";
      n.addEventListener("mousemove", function (e) { var s = idx[n.getAttribute("data-u").toLowerCase()]; if (!s) { tip.hidden = true; return; }
        var r = $("#map-holder").getBoundingClientRect();
        tip.innerHTML = "<b>" + s.name + "</b><br>" + (s.has_data ? (fmt(s.cast) + " ballots" + (s.partisan ? " · " + marginText(s.margin) : (s.turnout_pct != null ? " · " + s.turnout_pct + "% turnout" : ""))) : "no ballots yet");
        tip.hidden = false; tip.style.left = Math.min(e.clientX - r.left + 12, r.width - 170) + "px"; tip.style.top = (e.clientY - r.top + 12) + "px"; });
      n.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
    renderLegend();
  }
  function renderLegend() { $("#legend").innerHTML = "<div style='display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)'><span>D+40</span><span style='width:90px;height:10px;border-radius:3px;background:linear-gradient(90deg," + colorM(-40) + "," + colorM(0) + "," + colorM(40) + ")'></span><span>R+40</span> &nbsp;·&nbsp; <span style='width:60px;height:10px;border-radius:3px;background:linear-gradient(90deg," + colorT(2) + "," + colorT(60) + ")'></span> turnout</div>"; }

  function renderCards() {
    var s = mf.states || [], c = mf.counts || {};
    var totCast = 0, reg = 0, R = 0, D = 0, pReport = 0, tReport = 0;
    s.forEach(function (x) { totCast += x.cast || 0; if (x.registered) reg += x.registered; if (x.partisan && x.has_data) { R += x.rep || 0; D += x.dem || 0; pReport++; } if (x.has_data) tReport++; });
    var totP = R + D, natMargin = totP ? Math.round((100 * R / totP - 100 * D / totP) * 10) / 10 : null;
    var to = reg ? (100 * totCast / reg).toFixed(1) + "%" : "—";
    var cards = [
      ["Ballots cast (national)", fmt(totCast)],
      ["States reporting", (c.live || tReport) + " of " + (c.total || s.length)],
      ["Early-vote lean (party-reg states)", marginText(natMargin)],
      ["Registered (reporting states)", fmt(reg)],
      ["Turnout (reporting states)", to]
    ];
    $("#cards").innerHTML = cards.map(function (k) {
      var col = (k[0].indexOf("lean") >= 0 && natMargin != null) ? (natMargin > 0 ? "var(--rep)" : natMargin < 0 ? "var(--dem)" : "var(--ink)") : "var(--ink)";
      return "<div class='ncard'><div class='k'>" + k[0] + "</div><div class='v' style='color:" + col + "'>" + k[1] + "</div></div>";
    }).join("");
    var any = tReport > 0, pn = $("#pre-note");
    if (!any) { pn.hidden = false; pn.innerHTML = "🗳️ <b>Early voting hasn't started nationally yet.</b> States light up here as their 2026 feeds go live through October; the map & totals fill in automatically."; }
    else pn.hidden = true;
  }

  function renderTable() {
    var rows = (mf.states || []).filter(function (r) { return !filter || r.name.toLowerCase().indexOf(filter) >= 0; });
    rows.sort(function (a, b) { var A = a[sort.key], B = b[sort.key]; if (sort.key === "name") return (A < B ? -1 : 1) * sort.dir; A = A || 0; B = B || 0; return (A - B) * sort.dir; });
    var cols = [["name", "State"], ["cast", "Ballots"], ["turnout_pct", "Turnout"], ["margin", "Lean"]];
    $("#lead thead").innerHTML = "<tr>" + cols.map(function (c) { return "<th data-k='" + c[0] + "'>" + c[1] + (sort.key === c[0] ? (sort.dir > 0 ? " ▲" : " ▼") : "") + "</th>"; }).join("") + "</tr>";
    $("#lead thead").querySelectorAll("th").forEach(function (th) { th.onclick = function () { var k = th.getAttribute("data-k"); if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = k === "name" ? 1 : -1; } renderTable(); }; });
    $("#lead tbody").innerHTML = rows.map(function (r) {
      var lean = r.partisan ? ("<span class='lean' style='background:" + (r.has_data ? colorM(r.margin) : "#c9ced6") + "'></span>" + (r.has_data ? marginText(r.margin) : "—")) : "<span class='dim'>turnout-only</span>";
      return "<tr><td><b>" + r.name + "</b></td><td>" + fmt(r.cast) + "</td><td>" + (r.turnout_pct == null ? "—" : r.turnout_pct + "%") + "</td><td>" + lean + "</td></tr>";
    }).join("");
  }

  function render() { renderCards(); renderMap(); renderTable(); $("#updated").innerHTML = "Updated: <b>" + (mf.generated_at ? new Date(mf.generated_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—") + "</b>"; }
  function renderBrief() {
    getJSON("data/summary.json").then(function (s) {
      var host = $("#brief"); if (!host || !s.headline) return;
      host.hidden = false;
      host.innerHTML = "<div class='card-head'><h2 style='margin:0'>Daily brief</h2></div>" +
        "<p style='font-weight:600;margin:6px 0'>" + s.headline + "</p>" +
        "<ul style='margin:4px 0 0;padding-left:18px'>" + (s.bullets || []).map(function (b) { return "<li style='font-size:13.5px;margin:2px 0'>" + b + "</li>"; }).join("") + "</ul>";
    }).catch(function () {});
  }
  function boot() {
    Promise.all([getJSON("assets/us-states.geojson"), getJSON("data/status.json")]).then(function (res) {
      geo = res[0]; mf = res[1];
      $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderTable(); });
      render();
      renderBrief();
      setInterval(function () { getJSON("data/status.json").then(function (m) { mf = m; render(); }).catch(function () {}); renderBrief(); }, 10 * 60 * 1000);
    }).catch(function (e) { $("#cards").innerHTML = "<div class='ncard'>Could not load: " + e.message + "</div>"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
