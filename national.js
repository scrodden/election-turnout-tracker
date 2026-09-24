/* National early-vote dashboard — aggregates data/status.json over a US map. */
(function () {
  "use strict";
  var geo = null, mf = null, reg = null, proj = null, sort = { key: "cast", dir: -1 }, filter = "", maxCast = 0;
  var ptSort = { key: "total", dir: -1 }, prSort = { key: "total", dir: -1 };
  function $(s) { return document.querySelector(s); }
  function getJSON(u) { return fetch(u + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u); return r.json(); }); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function marginText(m) { return m == null ? "—" : m > 0 ? "R+" + m.toFixed(1) : m < 0 ? "D+" + (-m).toFixed(1) : "Even"; }

  var STOPS = [[-40, [33, 102, 172]], [-20, [103, 169, 207]], [0, [235, 237, 240]], [20, [239, 138, 98]], [40, [178, 24, 43]]];
  function colorM(m) { if (m == null) return "#c9ced6"; if (m < -40) m = -40; if (m > 40) m = 40;
    for (var i = 0; i < 4; i++) { var a = STOPS[i], b = STOPS[i + 1]; if (m >= a[0] && m <= b[0]) { var t = (m - a[0]) / (b[0] - a[0]); var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }); return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } } return "#c9ced6"; }
  function colorT(p) { if (p == null || p <= 0) return "#eef2f4"; var stops = [[0, [237, 242, 244]], [10, [116, 196, 118]], [30, [49, 163, 84]], [60, [0, 90, 40]]]; if (p > 60) p = 60;
    for (var i = 0; i < 3; i++) { var a = stops[i], b = stops[i + 1]; if (p >= a[0] && p <= b[0]) { var t = (p - a[0]) / (b[0] - a[0]); var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }); return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } } return "#eef2f4"; }

  function colorG(f) { if (f == null || f <= 0) return "#eef2f4"; if (f > 1) f = 1;
    var stops = [[0, [226, 240, 226]], [0.15, [161, 217, 155]], [0.5, [49, 163, 84]], [1, [0, 68, 27]]];
    for (var i = 0; i < 3; i++) { var a = stops[i], b = stops[i + 1]; if (f >= a[0] && f <= b[0]) { var t = (f - a[0]) / (b[0] - a[0]); var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }); return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")"; } } return "#eef2f4"; }
  function byCode() { var m = {}; (mf.states || []).forEach(function (s) { m[s.code] = s; }); return m; }
  function stateColor(s) {           // national map: green by ballots cast relative to the busiest state
    if (!s || !s.has_data || !s.cast) return "#c9ced6";
    return colorG(maxCast ? s.cast / maxCast : 0);
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
    maxCast = (mf.states || []).reduce(function (m, s) { return s.has_data && s.cast > m ? s.cast : m; }, 0);
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
  function renderLegend() { $("#legend").innerHTML = "<div style='display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)'><span>fewer</span><span style='width:110px;height:10px;border-radius:3px;background:linear-gradient(90deg," + colorG(0.03) + "," + colorG(0.4) + "," + colorG(1) + ")'></span><span>more ballots cast</span></div>"; }

  function renderCards() {
    var s = mf.states || [], c = mf.counts || {};
    var totCast = 0, reg = 0, R = 0, D = 0, N = 0, O = 0, pReport = 0, tReport = 0;
    s.forEach(function (x) { totCast += x.cast || 0; if (x.registered) reg += x.registered; if (x.partisan && x.has_data) { R += x.rep || 0; D += x.dem || 0; N += x.npa || 0; O += x.oth || 0; pReport++; } if (x.has_data) tReport++; });
    var totP = R + D + N + O, natMargin = totP ? Math.round((100 * R / totP - 100 * D / totP) * 10) / 10 : null;   // total-based, matching per-state margins
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

  function pctText(p) { return p == null ? "—" : p.toFixed(1) + "%"; }
  function pctOf(n, d) { return d ? 100 * n / d : null; }
  function nameOf(code) { var s = (mf.states || []).filter(function (x) { return x.code === code; })[0]; return s ? s.name : code.toUpperCase(); }

  // shared: render a sortable partisan table (rows + pinned National total row).
  function sortableTable(sel, cols, sortState, rows, total, renderCell) {
    var k = sortState.key, dir = sortState.dir;
    rows.sort(function (a, b) {
      if (k === "name") return a.name.localeCompare(b.name) * dir;
      var A = a[k], B = b[k]; A = (A == null ? -Infinity : A); B = (B == null ? -Infinity : B);
      return (A < B ? -1 : A > B ? 1 : 0) * dir;
    });
    var thead = sel.querySelector("thead"), tbody = sel.querySelector("tbody");
    thead.innerHTML = "<tr>" + cols.map(function (c) {
      var arrow = sortState.key === c.key ? (dir > 0 ? " ▲" : " ▼") : "";
      return "<th data-k='" + c.key + "'>" + c.label + arrow + "</th>";
    }).join("") + "</tr>";
    thead.querySelectorAll("th").forEach(function (th) {
      th.onclick = function () {
        var key = th.getAttribute("data-k");
        if (sortState.key === key) sortState.dir *= -1; else { sortState.key = key; sortState.dir = key === "name" ? 1 : -1; }
        render();
      };
    });
    tbody.innerHTML = rows.map(function (r) { return renderCell(r, false); }).join("") + renderCell(total, true);
  }

  // National partisan TURNOUT: party registration of ballots already cast.
  function renderPartisanTurnout() {
    var states = (mf.states || []).filter(function (s) { return s.partisan && s.has_data && (s.rep + s.dem + s.npa + s.oth) > 0; });
    $("#pt-note").textContent = states.length + " state" + (states.length === 1 ? "" : "s");
    var cols = [{ key: "name", label: "State" }, { key: "total", label: "Ballots" }, { key: "rep", label: "Rep" },
                { key: "dem", label: "Dem" }, { key: "npaoth", label: "NPA / Oth" }, { key: "margin", label: "Lean" }];
    if (!states.length) { $("#ptbl thead").innerHTML = "<tr><th>State</th></tr>"; $("#ptbl tbody").innerHTML = "<tr><td class='dim'>No party-registration state is reporting cast ballots yet.</td></tr>"; return; }
    var tot = { name: "National (these states)", rep: 0, dem: 0, npa: 0, oth: 0 };
    var rows = states.map(function (s) {
      tot.rep += s.rep; tot.dem += s.dem; tot.npa += s.npa; tot.oth += s.oth;
      return mkTurnoutRow(s.name, s.rep, s.dem, s.npa, s.oth);
    });
    var totRow = mkTurnoutRow(tot.name, tot.rep, tot.dem, tot.npa, tot.oth);
    sortableTable($("#ptbl"), cols, ptSort, rows, totRow, function (r, bold) {
      var b0 = bold ? "<b>" : "", b1 = bold ? "</b>" : "";
      return "<tr" + (bold ? " style='border-top:2px solid var(--line)'" : "") + "><td>" + b0 + r.name + b1 + "</td>" +
        "<td>" + fmt(r.total) + "</td>" +
        "<td>" + fmt(r.rep) + " <span class='dim'>" + pctText(r.reppct) + "</span></td>" +
        "<td>" + fmt(r.dem) + " <span class='dim'>" + pctText(r.dempct) + "</span></td>" +
        "<td>" + fmt(r.npaoth) + " <span class='dim'>" + pctText(r.nopct) + "</span></td>" +
        "<td><span class='lean' style='background:" + colorM(r.margin) + "'></span>" + marginText(r.margin) + "</td></tr>";
    });
  }
  function mkTurnoutRow(name, rep, dem, npa, oth) {
    var t = rep + dem + npa + oth;
    return { name: name, total: t, rep: rep, dem: dem, npaoth: npa + oth,
      reppct: pctOf(rep, t), dempct: pctOf(dem, t), nopct: pctOf(npa + oth, t),
      margin: t ? Math.round((100 * rep / t - 100 * dem / t) * 10) / 10 : null };
  }

  // National partisan REGISTRATION from data/registration.json.
  function renderPartisanReg() {
    var host = $("#prtbl");
    var codes = reg && reg.states ? Object.keys(reg.states) : [];
    var hasDC = codes.indexOf("dc") >= 0, nSt = codes.length - (hasDC ? 1 : 0);
    $("#pr-note").textContent = nSt + " state" + (nSt === 1 ? "" : "s") + (hasDC ? " + DC" : "");
    var cols = [{ key: "name", label: "State" }, { key: "total", label: "Registered" }, { key: "rep", label: "Rep" },
                { key: "dem", label: "Dem" }, { key: "npa", label: "NPA" }, { key: "oth", label: "Oth" }, { key: "asof", label: "As of" }];
    if (!codes.length) { host.querySelector("thead").innerHTML = "<tr><th>State</th></tr>"; host.querySelector("tbody").innerHTML = "<tr><td class='dim'>Registration data coming online.</td></tr>"; return; }
    var tot = { name: "National (these states)", rep: 0, dem: 0, npa: 0, oth: 0, asof: "" };
    var rows = codes.map(function (code) {
      var v = reg.states[code]; tot.rep += v.rep || 0; tot.dem += v.dem || 0; tot.npa += v.npa || 0; tot.oth += v.oth || 0;
      return mkRegRow(v.name || nameOf(code), v.rep || 0, v.dem || 0, v.npa || 0, v.oth || 0,
                      (v.as_of || "") + (v.manual ? " *" : ""));
    });
    var totRow = mkRegRow(tot.name, tot.rep, tot.dem, tot.npa, tot.oth, "");
    sortableTable(host, cols, prSort, rows, totRow, function (r, bold) {
      var b0 = bold ? "<b>" : "", b1 = bold ? "</b>" : "";
      return "<tr" + (bold ? " style='border-top:2px solid var(--line)'" : "") + "><td>" + b0 + r.name + b1 + "</td>" +
        "<td>" + fmt(r.total) + "</td>" +
        "<td>" + fmt(r.rep) + " <span class='dim'>" + pctText(r.reppct) + "</span></td>" +
        "<td>" + fmt(r.dem) + " <span class='dim'>" + pctText(r.dempct) + "</span></td>" +
        "<td>" + fmt(r.npa) + " <span class='dim'>" + pctText(r.npapct) + "</span></td>" +
        "<td>" + fmt(r.oth) + " <span class='dim'>" + pctText(r.othpct) + "</span></td>" +
        "<td class='dim'>" + (r.asof || "—") + "</td></tr>";
    });
  }
  function mkRegRow(name, rep, dem, npa, oth, asof) {
    var t = rep + dem + npa + oth;
    return { name: name, total: t, rep: rep, dem: dem, npa: npa, oth: oth, asof: asof,
      reppct: pctOf(rep, t), dempct: pctOf(dem, t), npapct: pctOf(npa, t), othpct: pctOf(oth, t) };
  }

  function render() { renderCards(); renderMap(); renderTable(); renderPartisanTurnout(); renderPartisanReg(); $("#updated").innerHTML = "Updated: <b>" + (mf.generated_at ? new Date(mf.generated_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—") + "</b>"; }
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
    Promise.all([
      getJSON("assets/us-states.geojson"),
      getJSON("data/status.json"),
      getJSON("data/registration.json").catch(function () { return { states: {} }; })
    ]).then(function (res) {
      geo = res[0]; mf = res[1]; reg = res[2];
      $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderTable(); });
      render();
      renderBrief();
      setInterval(function () {
        Promise.all([getJSON("data/status.json"), getJSON("data/registration.json").catch(function () { return reg; })])
          .then(function (r) { mf = r[0]; reg = r[1] || reg; render(); }).catch(function () {});
        renderBrief();
      }, 10 * 60 * 1000);
    }).catch(function (e) { $("#cards").innerHTML = "<div class='ncard'>Could not load: " + e.message + "</div>"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
