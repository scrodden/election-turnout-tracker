/* Florida's 2026 Senate / Governor results by precinct (fl-precincts.html).
   data/fl/precinct_results.json (scripts/fl_precinct_results.py: each county's VR Systems results site, keyed by the
   precinct map's ids) drawn on assets/fl-precincts.geojson, with county lines from assets/fl-counties.geojson. */
(function () {
  "use strict";
  var PREC_GEO = "assets/fl-precincts.geojson", CTY_GEO = "assets/fl-counties.geojson", DATA = "data/fl/precinct_results.json";
  var COUNTY_RESULTS = "data/fl/results_county.json";
  var RACES = { sen: { short: "Senate", file: "data/results/senate.json", id: "sen-FL-special" },
                gov: { short: "Governor", file: "data/results/governor.json", id: "gov-FL" } };
  var REFRESH_MS = 15 * 60 * 1000;
  var race = /gov/.test(location.hash) ? "gov" : "sen";
  var pgeo = null, cgeo = null, data = null, ctot = null, cands = {}, paths = [], full = null, view = null;

  function $(s) { return document.querySelector(s); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function mText(m) { if (m == null || !isFinite(m)) return "—"; var v = Math.abs(m).toFixed(1); return +v === 0 ? "Even" : (m > 0 ? "R+" : "D+") + v; }
  function getJSON(u, bust) { return fetch(u + (bust ? "?t=" + Date.now() : ""), { cache: bust ? "no-store" : "default" }).then(function (r) { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); }); }
  function when(iso) { return iso ? new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—"; }

  // the results page's diverging margin scale (+/-40)
  var STOPS = [[-40, [33, 102, 172]], [-20, [103, 169, 207]], [0, [235, 237, 240]], [20, [239, 138, 98]], [40, [178, 24, 43]]];
  function color(m) {
    if (m == null || !isFinite(m)) return null;
    m = Math.max(-40, Math.min(40, m));
    for (var i = 0; i < STOPS.length - 1; i++) {
      var a = STOPS[i], b = STOPS[i + 1];
      if (m <= b[0]) { var t = (m - a[0]) / (b[0] - a[0]);
        return "rgb(" + [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }).join(",") + ")"; }
    }
    return null;
  }
  var NO_COUNTY = "#c9ced6", NO_PRECINCT = "#e6e9ed";

  // one precinct's votes in this race: [rep, dem, other], following a shared polling-place result to its owner
  function votes(code, pid) {
    var c = data && data.counties && data.counties[code];
    if (!c) return null;
    var e = c.precincts[pid];
    if (e && e.of) e = c.precincts[e.of];
    return e && e[race] ? e[race] : null;
  }
  function margin(v) { var t = v ? v[0] + v[1] + v[2] : 0; return t ? 100 * (v[0] - v[1]) / t : null; }

  // ---- geometry: one equal-area-ish projection for both layers ----
  function eachRing(g, cb) { if (!g) return; if (g.type === "Polygon") g.coordinates.forEach(cb); else if (g.type === "MultiPolygon") g.coordinates.forEach(function (p) { p.forEach(cb); }); }
  function project() {
    var mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    cgeo.features.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) {
      if (p[0] < mnx) mnx = p[0]; if (p[0] > mxx) mxx = p[0]; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), s = 1000 / ((mxx - mnx) * k);
    full = { x: 0, y: 0, w: 1000, h: (mxy - mny) * s };
    return function (p) { return [(p[0] - mnx) * k * s, (mxy - p[1]) * s]; };
  }
  function pathOf(g, pr, box) {
    var d = "";
    eachRing(g, function (ring) {
      var last = null;
      ring.forEach(function (p, i) {
        var q = pr(p);
        if (box) { if (q[0] < box[0]) box[0] = q[0]; if (q[1] < box[1]) box[1] = q[1]; if (q[0] > box[2]) box[2] = q[0]; if (q[1] > box[3]) box[3] = q[1]; }
        var t = q[0].toFixed(1) + " " + q[1].toFixed(1);
        if (t !== last) { d += (i ? "L" : "M") + t; last = t; }
      });
      d += "Z";
    });
    return d;
  }
  var countyBox = {};
  function build() {
    var pr = project(), svg = $("#pmap"), NS = "http://www.w3.org/2000/svg";
    var gp = document.createElementNS(NS, "g"), gc = document.createElementNS(NS, "g");
    paths = pgeo.features.map(function (ft, i) {
      var el = document.createElementNS(NS, "path"), pp = ft.properties, box = countyBox[pp.code] || (countyBox[pp.code] = [Infinity, Infinity, -Infinity, -Infinity]);
      el.setAttribute("d", pathOf(ft.geometry, pr, box));
      el.setAttribute("class", "p");
      el.setAttribute("data-i", i);
      gp.appendChild(el);
      return { el: el, code: pp.code, county: pp.county, pid: String(pp.precinct) };
    });
    cgeo.features.forEach(function (ft) {
      var el = document.createElementNS(NS, "path");
      el.setAttribute("d", pathOf(ft.geometry, pr)); el.setAttribute("class", "cty"); gc.appendChild(el);
    });
    svg.innerHTML = ""; svg.appendChild(gp); svg.appendChild(gc);
    setView(home());
    var sel = $("#county"), seen = {};
    paths.slice().sort(function (a, b) { return a.county.localeCompare(b.county); }).forEach(function (p) {
      if (seen[p.code]) return; seen[p.code] = true;
      var o = document.createElement("option"); o.value = p.code; o.textContent = p.county; sel.appendChild(o);
    });
  }

  function paint() {
    paths.forEach(function (p) {
      var has = data && data.counties && data.counties[p.code], v = has ? votes(p.code, p.pid) : null;
      var e = has && has.precincts[p.pid], bad = e && (e.chk != null || (e.of && has.precincts[e.of] && has.precincts[e.of].chk != null));
      p.el.setAttribute("fill", !has ? NO_COUNTY : bad ? NO_PRECINCT : (color(margin(v)) || NO_PRECINCT));
    });
  }

  // ---- pan / zoom by viewBox ----
  function setView(v) {
    view = v;
    $("#pmap").setAttribute("viewBox", [v.x, v.y, v.w, v.h].map(function (n) { return n.toFixed(2); }).join(" "));
  }
  function aspect() { var r = $("#pmap").getBoundingClientRect(); return r.width && r.height ? r.width / r.height : full.w / full.h; }
  function fitBox(b) {   // [x0, y0, x1, y1] in map units -> a view of the frame's shape, with a little margin
    var pad = 0.04 * Math.max(b[2] - b[0], b[3] - b[1]), w = b[2] - b[0] + 2 * pad, h = b[3] - b[1] + 2 * pad, a = aspect();
    if (w / h < a) w = h * a; else h = w / a;
    return { x: (b[0] + b[2]) / 2 - w / 2, y: (b[1] + b[3]) / 2 - h / 2, w: w, h: h };
  }
  function home() { return fitBox([full.x, full.y, full.x + full.w, full.y + full.h]); }
  function zoomAt(f, cx, cy) {   // f < 1 zooms in, about the point (cx, cy) in map units
    var w = Math.max(8, Math.min(full.w * 1.5, view.w * f)), h = w / aspect();
    setView({ x: cx - (cx - view.x) * w / view.w, y: cy - (cy - view.y) * h / view.h, w: w, h: h });
  }
  function toMap(ev) {
    var r = $("#pmap").getBoundingClientRect(), sc = Math.max(view.w / r.width, view.h / r.height);
    var ox = (r.width * sc - view.w) / 2, oy = (r.height * sc - view.h) / 2;   // preserveAspectRatio: xMidYMid meet
    return [view.x + (ev.clientX - r.left) * sc - ox, view.y + (ev.clientY - r.top) * sc - oy, sc];
  }
  function fitCounty(code) {
    var b = countyBox[code];
    setView(code && b ? fitBox(b) : home());
  }
  function wire() {
    var svg = $("#pmap"), drag = null, moved = false, pts = {};
    svg.addEventListener("wheel", function (ev) { ev.preventDefault(); var m = toMap(ev); zoomAt(ev.deltaY < 0 ? 0.8 : 1.25, m[0], m[1]); }, { passive: false });
    svg.addEventListener("pointerdown", function (ev) {
      pts[ev.pointerId] = [ev.clientX, ev.clientY];
      drag = { x: ev.clientX, y: ev.clientY, v: view, d: null }; moved = false; svg.setPointerCapture(ev.pointerId);
    });
    svg.addEventListener("pointermove", function (ev) {
      if (pts[ev.pointerId]) pts[ev.pointerId] = [ev.clientX, ev.clientY];
      var ids = Object.keys(pts);
      if (ids.length === 2) {   // pinch
        var a = pts[ids[0]], b = pts[ids[1]], d = Math.hypot(a[0] - b[0], a[1] - b[1]);
        if (drag && drag.d) { var m = toMap({ clientX: (a[0] + b[0]) / 2, clientY: (a[1] + b[1]) / 2 }); zoomAt(drag.d / d, m[0], m[1]); }
        if (drag) drag.d = d; moved = true; return;
      }
      if (drag && (ev.buttons || ev.pointerType === "touch")) {
        var sc = toMap(ev)[2], dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) { moved = true; svg.classList.add("dragging"); }
        if (moved) setView({ x: drag.v.x - dx * sc, y: drag.v.y - dy * sc, w: drag.v.w, h: drag.v.h });
        return;
      }
      tipFor(ev);
    });
    function end(ev) { delete pts[ev.pointerId]; if (!Object.keys(pts).length) { drag = null; svg.classList.remove("dragging"); } }
    svg.addEventListener("pointerup", function (ev) { var wasMoved = moved; end(ev); if (!wasMoved) tipFor(ev); });
    svg.addEventListener("pointercancel", end);
    svg.addEventListener("pointerleave", function () { $("#tip").hidden = true; });
    $("#zin").onclick = function () { zoomAt(0.6, view.x + view.w / 2, view.y + view.h / 2); };
    $("#zout").onclick = function () { zoomAt(1 / 0.6, view.x + view.w / 2, view.y + view.h / 2); };
    $("#zreset").onclick = function () { $("#county").value = ""; setView(home()); };
    window.addEventListener("resize", function () { fitCounty($("#county").value); });
    $("#county").onchange = function () { fitCounty(this.value); };
    document.querySelectorAll("#race-tabs button").forEach(function (b) {
      b.onclick = function () { race = b.getAttribute("data-race"); history.replaceState(null, "", "#" + race); render(); };
    });
  }

  function lastName(n) { return String(n || "").replace(/\s+(Jr\.?|Sr\.?|II|III|IV)$/i, "").split(/\s+/).pop(); }
  function tipFor(ev) {
    var t = ev.target, tip = $("#tip"), i = t && t.getAttribute && t.getAttribute("data-i");
    document.querySelectorAll("#pmap path.hi").forEach(function (x) { x.classList.remove("hi"); });
    if (i == null) { tip.hidden = true; return; }
    var p = paths[+i], c = data && data.counties && data.counties[p.code], h = "<b>" + p.county + " · precinct " + p.pid + "</b>";
    t.classList.add("hi");
    if (!c) h += "<div class='dim2'>" + ((data && data.missing && data.missing[p.county]) || "no precinct results") + "</div>";
    else {
      var v = votes(p.code, p.pid), e = c.precincts[p.pid] || {};
      if (!v) h += "<div class='dim2'>no results matched to this precinct</div>";
      else if (!(v[0] + v[1] + v[2])) h += "<div class='dim2'>no votes cast here</div>";
      else {
        var tot = v[0] + v[1] + v[2], R = lastName((cands[race] || {}).rep) || "Republican", D = lastName((cands[race] || {}).dem) || "Democrat";
        h += "<br><span class='big'>" + mText(margin(v)) + "</span>" +
          "<br>" + R + " (R) " + fmt(v[0]) + " · " + (100 * v[0] / tot).toFixed(1) + "%" +
          "<br>" + D + " (D) " + fmt(v[1]) + " · " + (100 * v[1] / tot).toFixed(1) + "%" +
          (v[2] ? "<br>Others " + fmt(v[2]) : "") + "<br><span class='dim2'>" + fmt(tot) + " votes</span>";
        if (e.chk != null) h += "<div class='dim2'>Not colored: these votes are " + e.chk + "× the precinct's ballots in the county's turnout feed, " +
          "so the county's results precinct doesn't line up with the map's here (renumbered or merged precincts).</div>";
        var grp = e.of ? [e.of].concat((c.precincts[e.of] || {}).with || []) : e.with ? [p.pid].concat(e.with) : null;
        if (grp) h += "<div class='dim2'>Reported together for precincts " + grp.join(", ") + " (one polling place)</div>";
      }
    }
    tip.innerHTML = h; tip.hidden = false;
    var w = $("#pm-wrap").getBoundingClientRect(), tw = tip.offsetWidth, x = ev.clientX - w.left + 14;
    if (x + tw > w.width) x = Math.max(4, ev.clientX - w.left - tw - 14);
    tip.style.left = x + "px"; tip.style.top = Math.min(ev.clientY - w.top + 14, w.height - tip.offsetHeight - 4) + "px";
  }

  function render() {
    document.querySelectorAll("#race-tabs button").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-race") === race)); });
    paint();
    var counties = data.counties || {}, mapped = 0, all = 0, n = 0;
    Object.keys(ctot.counties || {}).forEach(function (name) { var t = ctot.counties[name][race]; if (t) all += t.rep + t.dem + t.other; });
    Object.keys(counties).forEach(function (code) {
      var c = counties[code], t = ctot.counties[c.name] && ctot.counties[c.name][race], un = (c.unmatched || {})[race] || [0, 0, 0];
      if (t && (c.total_ok || {})[race]) { mapped += t.rep + t.dem + t.other - un[0] - un[1] - un[2]; n++; }
    });
    var off = Object.keys(counties).map(function (code) {
      var c = counties[code], t = ctot.counties[c.name] && ctot.counties[c.name][race], un = (c.unmatched || {})[race] || [0, 0, 0];
      var tot = t ? t.rep + t.dem + t.other : 0, miss = un[0] + un[1] + un[2];
      return tot && miss / tot > 0.03 ? c.name + " " + Math.round(100 * miss / tot) + "%" : null;
    }).filter(Boolean);
    $("#coverage").innerHTML = "<b>" + n + " of 67 counties</b> post " + RACES[race].short + " results by precinct; <b>" + (all ? Math.round(100 * mapped / all) : 0) +
      "% of the state's votes</b> are on this map. Gray counties post no precinct results; pale precincts have none matched." +
      (off.length ? " Votes not on the map (new precincts with no shape yet): " + off.join(", ") + "." : "") + " Scroll or pinch to zoom; drag to pan.";
    var grad = "linear-gradient(90deg," + STOPS.map(function (x) { return "rgb(" + x[1].join(",") + ")"; }).join(",") + ")";
    $("#legend").innerHTML = "<div><div class='grad' style='background:" + grad + "'></div><div class='ends'><span>D+40</span><span>Even</span><span>R+40</span></div></div>" +
      "<span><i class='sw' style='background:" + NO_COUNTY + "'></i>county posts no precinct results</span>" +
      "<span><i class='sw' style='background:" + NO_PRECINCT + ";border:1px solid var(--line)'></i>no result matched, or one that doesn't line up with the precinct's turnout</span>";
    $("#updated").innerHTML = "Precinct results read: <b>" + when(data.updated) + "</b>";
    $("#sources").innerHTML = "Margins are the Republican's share minus the Democrat's, of all votes in the race.";
  }

  function loadCands() {
    return Promise.all(Object.keys(RACES).map(function (k) {
      return getJSON(RACES[k].file, true).then(function (d) {
        var r = (d.races || []).filter(function (x) { return x.id === RACES[k].id; })[0], m = {};
        ((r && r.candidates) || []).forEach(function (c) { var p = (c.party || "").toUpperCase(); if (p === "REP" && !m.rep) m.rep = c.name; if (p === "DEM" && !m.dem) m.dem = c.name; });
        cands[k] = m;
      }).catch(function () { cands[k] = {}; });
    }));
  }
  function loadData() {
    return Promise.all([getJSON(DATA, true), getJSON(COUNTY_RESULTS, true), loadCands()]).then(function (v) { data = v[0]; ctot = v[1]; render(); });
  }
  function boot() {
    Promise.all([getJSON(PREC_GEO), getJSON(CTY_GEO)]).then(function (g) { pgeo = g[0]; cgeo = g[1]; build(); wire(); return loadData(); })
      .catch(function (e) { $("#coverage").textContent = "Couldn't load the map (" + e.message + ")."; });
    setInterval(function () { if (pgeo) loadData().catch(function () {}); }, REFRESH_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
