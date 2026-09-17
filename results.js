/* 2026 Election Results — dependency-free front end.
   National map (states, with AK/HI insets) + scorecard for Senate / Governor /
   House, colored by the current margin. Reads assets/us-states.geojson and
   data/results/<office>.json (empty until results are wired near Election Day). */
(function () {
  "use strict";
  var STATES_URL = "assets/us-states.geojson";
  var REFRESH_MS = 10 * 60 * 1000;
  var INSET = { AK: null, HI: null };           // filled after load; drawn as boxes
  var office = "senate";
  var geo = null, cache = {}, proj = null, sort = { key: "state", dir: 1 }, filter = "";

  function $(s) { return document.querySelector(s); }
  function el(t, c) { var e = document.createElement(t); if (c) e.className = c; return e; }
  function fmt(n) { return (n == null) ? "—" : n.toLocaleString("en-US"); }
  function getJSON(u) {
    return fetch(u + (u.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); });
  }

  // diverging R(red)/D(blue) scale on margin (points); + = R lead
  var STOPS = [[-40, [33, 102, 172]], [-20, [103, 169, 207]], [0, [235, 237, 240]], [20, [239, 138, 98]], [40, [178, 24, 43]]];
  function color(m) {
    if (m == null) return "#c9ced6";
    if (m < STOPS[0][0]) m = STOPS[0][0]; if (m > STOPS[4][0]) m = STOPS[4][0];
    for (var i = 0; i < STOPS.length - 1; i++) { var a = STOPS[i], b = STOPS[i + 1];
      if (m >= a[0] && m <= b[0]) { var t = (m - a[0]) / (b[0] - a[0]);
        var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); });
        return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } }
    return "#c9ced6";
  }

  // ---- per-race computation ----
  function pct(c, tot) { return tot ? 100 * c / tot : 0; }
  function computeRace(r) {
    var out = { hasData: false, leader: null, leaderName: "", margin: null, rd: null, reporting: r.reporting_pct, called: r.called || null };
    var cand = r.candidates || [];
    if (!cand.length) { if (r.leader_party) { out.leader = r.leader_party; out.margin = r.margin; out.rd = r.margin; out.hasData = true; } return out; }
    var tot = 0, byp = {}; cand.forEach(function (c) { tot += (c.votes || 0); });
    var sorted = cand.slice().sort(function (a, b) { return (b.votes || 0) - (a.votes || 0); });
    cand.forEach(function (c) { var p = (c.party || "").toUpperCase()[0]; byp[p] = (byp[p] || 0) + (c.votes || 0); });
    out.hasData = tot > 0;
    if (out.hasData) {
      out.leader = (sorted[0].party || "").toUpperCase()[0];
      out.leaderName = sorted[0].name || "";
      out.margin = Math.round((pct(sorted[0].votes || 0, tot) - pct((sorted[1] || {}).votes || 0, tot)) * 10) / 10;
      out.rd = Math.round((pct(byp.R || 0, tot) - pct(byp.D || 0, tot)) * 10) / 10;
    }
    return out;
  }
  function racesByState(data) { var m = {}; (data.races || []).forEach(function (r) { (m[r.state] = m[r.state] || []).push(r); }); return m; }

  function stateColor(usps, byState) {
    var rs = byState[usps]; if (!rs || !rs.length) return "#c9ced6";
    if (office === "house") {
      var R = 0, D = 0, dec = 0;
      rs.forEach(function (r) { var c = computeRace(r); if (c.hasData) { dec++; if (c.leader === "R") R++; else if (c.leader === "D") D++; } });
      if (!dec) return "#c9ced6";
      return color((R - D) / rs.length * 100);
    }
    var cc = computeRace(rs[0]); return cc.hasData ? color(cc.rd) : "#c9ced6";
  }

  // ---- map ----
  function eachRing(g, cb) { if (!g) return; if (g.type === "Polygon") g.coordinates.forEach(cb); else if (g.type === "MultiPolygon") g.coordinates.forEach(function (p) { p.forEach(cb); }); }
  function buildProjection(feats) {
    var mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    feats.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) {
      if (p[0] < mnx) mnx = p[0]; if (p[0] > mxx) mxx = p[0]; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), W = 960, s = W / ((mxx - mnx) * k), H = (mxy - mny) * s;
    return { project: function (p) { return [(p[0] - mnx) * k * s, (mxy - p[1]) * s]; }, W: Math.round(W), H: Math.round(H) };
  }
  function pathFor(g) { var d = ""; eachRing(g, function (ring) { for (var i = 0; i < ring.length; i++) { var p = proj.project(ring[i]); d += (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1); } d += "Z"; }); return d; }

  function renderMap(data) {
    var svg = $("#rmap"), byState = racesByState(data);
    var lower = geo.features.filter(function (f) { return f.properties.usps !== "AK" && f.properties.usps !== "HI"; });
    proj = buildProjection(lower);
    var H = proj.H + 70;                 // room for AK/HI insets
    svg.setAttribute("viewBox", "0 0 " + proj.W + " " + H);
    var parts = [];
    lower.forEach(function (ft) {
      var u = ft.properties.usps;
      parts.push("<path d='" + pathFor(ft.geometry) + "' fill='" + stateColor(u, byState) + "' stroke='#fff' stroke-width='0.7' data-u='" + u + "'></path>");
    });
    // AK / HI insets
    var iy = proj.H + 8;
    [["AK", 10], ["HI", 150]].forEach(function (pair) {
      var u = pair[0], x = pair[1];
      parts.push("<rect x='" + x + "' y='" + iy + "' width='120' height='52' rx='6' fill='" + stateColor(u, byState) + "' stroke='#fff' stroke-width='1' data-u='" + u + "'></rect>");
      parts.push("<text class='inset' x='" + (x + 60) + "' y='" + (iy + 30) + "' text-anchor='middle'>" + u + "</text>");
    });
    svg.innerHTML = parts.join("");
    var tip = $("#tooltip");
    svg.querySelectorAll("[data-u]").forEach(function (node) {
      node.style.cursor = "pointer";
      node.addEventListener("mousemove", function (e) { showTip(e, node.getAttribute("data-u"), byState); });
      node.addEventListener("mouseleave", function () { tip.hidden = true; });
    });
    renderLegend();
  }
  function showTip(e, usps, byState) {
    var tip = $("#tooltip"), rs = byState[usps]; if (!rs) { tip.hidden = true; return; }
    var name = rs[0].state_name, html = "<b>" + name + "</b>";
    if (office === "house") {
      var R = 0, D = 0, u = 0; rs.forEach(function (r) { var c = computeRace(r); if (!c.hasData) u++; else if (c.leader === "R") R++; else if (c.leader === "D") D++; });
      html += "<br>House: " + R + "R / " + D + "D" + (u ? " · " + u + " uncalled" : "") + " of " + rs.length;
    } else {
      var c = computeRace(rs[0]);
      html += "<br>" + rs[0].office + "<br>" + (c.hasData ? (leanLabel(c.leader) + (c.leaderName ? " (" + c.leaderName + ")" : "") + " +" + c.margin + " · " + (c.reporting != null ? c.reporting + "% in" : "—")) : "not yet reported");
    }
    var r = $("#map-holder").getBoundingClientRect();
    tip.innerHTML = html; tip.hidden = false;
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - 180) + "px";
    tip.style.top = (e.clientY - r.top + 12) + "px";
  }
  function leanLabel(p) { return p === "R" ? "Republican" : p === "D" ? "Democratic" : p ? "Other" : "—"; }
  function renderLegend() {
    var host = $("#legend"); host.innerHTML = "";
    var wrap = el("div"); wrap.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)";
    wrap.innerHTML = "<span>D+40</span><span style='width:120px;height:10px;border-radius:3px;display:inline-block;background:linear-gradient(90deg," + color(-40) + "," + color(0) + "," + color(40) + ")'></span><span>R+40</span>";
    host.appendChild(wrap);
  }

  // ---- scorecard ----
  function renderSummary(data) {
    var host = $("#summary"), s = data.summary || {}, races = data.races || [];
    var d = 0, r = 0, o = 0, u = 0;
    races.forEach(function (rc) { var c = computeRace(rc); if (!c.hasData && !rc.called) u++; else { var w = rc.called || c.leader; if (w === "D") d++; else if (w === "R") r++; else u++; } });
    if (!races.some(function (rc) { return computeRace(rc).hasData || rc.called; })) { d = r = o = 0; u = races.length; }
    host.innerHTML = "";
    [["dem", "Dem leading/won", d], ["rep", "Rep leading/won", r], ["", "Total races", races.length], ["unc", "Not yet reported", u]].forEach(function (p) {
      var e = el("span", "pill " + p[0]); e.innerHTML = p[1] + ": <b>" + p[2] + "</b>"; host.appendChild(e);
    });
  }
  function cols() {
    return office === "house"
      ? [["state", "State"], ["district", "Dist"], ["leader", "Leader"], ["margin", "Margin"], ["reporting", "% In"], ["status", "Status"]]
      : [["state", "State"], ["leader", "Leader"], ["margin", "Margin"], ["reporting", "% In"], ["status", "Status"]];
  }
  function renderScore(data) {
    var thead = $("#score thead"), tbody = $("#score tbody");
    thead.innerHTML = "<tr>" + cols().map(function (c) { return "<th data-k='" + c[0] + "'>" + c[1] + (sort.key === c[0] ? (sort.dir > 0 ? " ▲" : " ▼") : "") + "</th>"; }).join("") + "</tr>";
    thead.querySelectorAll("th").forEach(function (th) { th.onclick = function () { var k = th.getAttribute("data-k"); if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = 1; } renderScore(data); }; });
    var rows = (data.races || []).slice().filter(function (r) { return !filter || (r.state_name + " " + r.state).toLowerCase().indexOf(filter) >= 0; });
    rows.sort(function (a, b) {
      var A, B, ca = computeRace(a), cb = computeRace(b);
      if (sort.key === "margin") { A = ca.margin == null ? -1 : ca.margin; B = cb.margin == null ? -1 : cb.margin; }
      else if (sort.key === "reporting") { A = ca.reporting || 0; B = cb.reporting || 0; }
      else if (sort.key === "leader") { A = ca.leader || "Z"; B = cb.leader || "Z"; }
      else if (sort.key === "district") { A = a.state_name + a.district; B = b.state_name + b.district; }
      else if (sort.key === "status") { A = (a.called ? 0 : ca.hasData ? 1 : 2); B = (b.called ? 0 : cb.hasData ? 1 : 2); }
      else { A = a.state_name; B = b.state_name; }
      return A < B ? -sort.dir : A > B ? sort.dir : 0;
    });
    tbody.innerHTML = rows.map(function (r) {
      var c = computeRace(r), lc = color(office === "house" ? null : c.rd);
      var lead = c.hasData ? (leanLabel(c.leader) + (c.leaderName ? " " + c.leaderName : "")) : "—";
      var status = r.called ? "<span class='badge " + (r.called === "D" ? "dem" : r.called === "R" ? "rep" : "unc") + "'>Called " + r.called + "</span>"
        : c.hasData ? "<span class='badge unc'>Leading</span>" : "<span class='badge unc'>—</span>";
      var tds = ["<span class='lean' style='background:" + (c.hasData ? color(c.rd) : "#c9ced6") + "'></span>" + r.state_name + (r.special ? " *" : "")];
      if (office === "house") tds.push(r.district);
      tds.push(lead, c.margin == null ? "—" : (c.leader || "") + "+" + c.margin, c.reporting == null ? "—" : c.reporting + "%", status);
      return "<tr>" + tds.map(function (x) { return "<td>" + x + "</td>"; }).join("") + "</tr>";
    }).join("");
    $("#score-note").textContent = rows.length + " races" + (office === "senate" ? " · * = special election" : "");
  }

  function renderMeta(data) {
    $("#map-title").textContent = ({ senate: "U.S. Senate", governor: "Governor", house: "U.S. House" }[office]) + " — current margin";
    var upd = data.updated ? new Date(data.updated).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" }) : null;
    $("#updated").innerHTML = upd ? "Updated: <b>" + upd + "</b>" : "<b>Not yet reported</b>";
    var any = (data.races || []).some(function (r) { return computeRace(r).hasData || r.called; });
    var pn = $("#pre-note");
    if (!any) { pn.hidden = false; pn.innerHTML = "🗳️ <b>Results will appear here live on Election Night</b> (Nov 3, 2026). Showing the full 2026 race slate; the map and scorecard fill in as official results are reported."; }
    else pn.hidden = true;
  }

  function render() {
    var data = cache[office];
    renderMeta(data); renderSummary(data); renderMap(data); renderScore(data);
  }
  function loadOffice(o) {
    office = o;
    $("#tabs").querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-office") === o)); });
    if (cache[o]) { render(); return; }
    getJSON("data/results/" + o + ".json").then(function (d) { cache[o] = d; render(); }).catch(function (e) { $("#summary").innerHTML = "<span class='pill'>Could not load results: " + e.message + "</span>"; });
  }
  function refresh() { cache = {}; loadOffice(office); }

  function boot() {
    getJSON(STATES_URL).then(function (g) {
      geo = g;
      $("#tabs").querySelectorAll("button").forEach(function (b) { b.addEventListener("click", function () { loadOffice(b.getAttribute("data-office")); }); });
      $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderScore(cache[office]); });
      setInterval(refresh, REFRESH_MS);
      loadOffice("senate");
    }).catch(function (e) { $("#summary").innerHTML = "<span class='pill'>Could not load map: " + e.message + "</span>"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
