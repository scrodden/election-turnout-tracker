/* Florida Turnout Tracker — dependency-free front end.
   Reads data/fl/latest.json + assets/fl-counties.geojson, draws an inline-SVG
   choropleth and a sortable county table, and refreshes every 10 minutes. */
(function () {
  "use strict";

  var STATES_URL = "assets/states.json";
  var STATES = [], st = null;                 // registry + current state config
  var DATA_URL, GEO_URL, PRECINCT_GEO_URL, PRECINCT_DATA_URL, HISTORY_URL;  // per-state, set in loadState
  var REFRESH_MS = 10 * 60 * 1000;
  // index of each method within a precincts_all.json value array
  var PFIELD = { cast: 1, mail_voted: 2, early_voted: 3, election_day: 4, provisional: 5 };

  var METHODS = [
    { key: "cast", label: "All cast" },
    { key: "mail_voted", label: "Voted by mail" },
    { key: "early_voted", label: "Voted early" },
    { key: "election_day", label: "Election day" },
    { key: "provisional", label: "Provisional" },
    { key: "mail_provided", label: "Mail outstanding" }
  ];
  var HINTS = {
    cast: "Party registration of everyone who has already cast a ballot (mail + early in person + election day).",
    mail_voted: "Party registration of voters whose mail ballots have been returned.",
    early_voted: "Party registration of voters who cast a ballot in person during early voting.",
    election_day: "Party registration of voters who cast a ballot on election day.",
    provisional: "Party registration of voters who cast a provisional ballot.",
    mail_provided: "Party registration of voters SENT a mail ballot who have not yet returned it (outstanding). From the state file; TQV does not report this."
  };

  var geo = null, data = null;
  var method = "cast";
  var sort = { key: "total", dir: -1 };
  var filter = "";
  var selected = null;
  var proj = null;
  var mapMode = "county";
  var precinctGeo = null, precinctData = null, precinctLoading = false;
  var compare = false, baseline = null, BASELINE_URL = null;
  var CMP_METHODS = ["mail_provided", "mail_voted", "early_voted", "election_day", "cast"];
  var partisan = true;   // false = turnout-only state (no party registration, e.g. GA)

  // ---- utils ---------------------------------------------------------------
  function $(sel) { return document.querySelector(sel); }
  function el(tag, cls) { var e = document.createElement(tag); if (cls) e.className = cls; return e; }
  function fmt(n) { return (n == null) ? "—" : n.toLocaleString("en-US"); }
  function pctText(p) { return (p == null) ? "—" : p.toFixed(1) + "%"; }
  function marginText(m) {
    if (m == null) return "—";
    if (m > 0) return "R+" + m.toFixed(1);
    if (m < 0) return "D+" + (-m).toFixed(1);
    return "Even";
  }
  function getJSON(url) {
    return fetch(url + (url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { if (!r.ok) throw new Error(url + " " + r.status); return r.json(); });
  }
  function methodAvailable(key) {
    if (!data) return false;
    if (key === "cast") return true;
    return (data.methods_present || []).indexOf(key) >= 0;
  }
  function block(entity, key) {
    var b = entity && entity[key];
    return b || { rep: 0, dem: 0, oth: 0, npa: 0, total: 0, rep_pct: null, dem_pct: null, npa_pct: null, oth_pct: null, margin: null };
  }
  function compareActive() { return compare && baseline && CMP_METHODS.indexOf(method) >= 0; }
  function marginOf(b) { return (!b || !b.total) ? null : Math.round((100 * b.rep / b.total - 100 * b.dem / b.total) * 10) / 10; }
  function b22(name) {
    if (!baseline) return null;
    var c = baseline.counties[name]; if (!c || !c[method]) return null;
    var b = c[method]; return { rep: b.rep, dem: b.dem, oth: b.oth, npa: b.npa, total: b.total, margin: marginOf(b) };
  }
  function sw22() {
    if (!baseline || !baseline.statewide[method]) return null;
    var b = baseline.statewide[method]; return { total: b.total, margin: marginOf(b) };
  }
  function shiftText(s) { return s == null ? "—" : s > 0 ? "R+" + s.toFixed(1) : s < 0 ? "D+" + (-s).toFixed(1) : "0"; }

  // ---- diverging color scale ----------------------------------------------
  var STOPS = [
    [-40, [33, 102, 172]], [-20, [103, 169, 207]], [0, [235, 237, 240]],
    [20, [239, 138, 98]], [40, [178, 24, 43]]
  ];
  function colorForMargin(m) {
    if (m == null) return "#c9ced6"; // no data
    if (m < STOPS[0][0]) m = STOPS[0][0];
    if (m > STOPS[STOPS.length - 1][0]) m = STOPS[STOPS.length - 1][0];
    for (var i = 0; i < STOPS.length - 1; i++) {
      var a = STOPS[i], b = STOPS[i + 1];
      if (m >= a[0] && m <= b[0]) {
        var t = (m - a[0]) / (b[0] - a[0]);
        var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); });
        return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      }
    }
    return "#c9ced6";
  }

  // sequential turnout scale (greens); domain 0..~50% of eligible
  var TSTOPS = [
    [0, [237, 242, 244]], [3, [199, 233, 192]], [10, [116, 196, 118]],
    [25, [49, 163, 84]], [50, [0, 90, 40]]
  ];
  function colorForTurnout(pct) {
    if (pct == null) return "#e9edf0";
    if (pct <= 0) return TSTOPS[0] && "#eef2f4";
    if (pct > TSTOPS[TSTOPS.length - 1][0]) pct = TSTOPS[TSTOPS.length - 1][0];
    for (var i = 0; i < TSTOPS.length - 1; i++) {
      var a = TSTOPS[i], b = TSTOPS[i + 1];
      if (pct >= a[0] && pct <= b[0]) {
        var t = (pct - a[0]) / (b[0] - a[0]);
        var c = [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); });
        return "rgb(" + c[0] + "," + c[1] + "," + c[2] + ")";
      }
    }
    return "#eef2f4";
  }

  function colorForFrac(f) {   // 0..1 relative intensity -> light..dark green
    if (f == null || f <= 0) return "#eef2f4";
    return colorForTurnout(Math.min(50, f * 50));
  }

  // ---- map projection ------------------------------------------------------
  function buildProjection(g) {
    var minLon = Infinity, maxLon = -Infinity, minLat = Infinity, maxLat = -Infinity;
    g.features.forEach(function (ft) {
      eachRing(ft.geometry, function (ring) {
        ring.forEach(function (pt) {
          if (pt[0] < minLon) minLon = pt[0]; if (pt[0] > maxLon) maxLon = pt[0];
          if (pt[1] < minLat) minLat = pt[1]; if (pt[1] > maxLat) maxLat = pt[1];
        });
      });
    });
    var k = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
    var W = 1000, s = W / ((maxLon - minLon) * k);
    var H = (maxLat - minLat) * s;
    function project(pt) { return [((pt[0] - minLon) * k * s), ((maxLat - pt[1]) * s)]; }
    return { project: project, W: Math.round(W), H: Math.round(H) };
  }
  function eachRing(geom, cb) {
    if (!geom) return;
    if (geom.type === "Polygon") geom.coordinates.forEach(cb);
    else if (geom.type === "MultiPolygon") geom.coordinates.forEach(function (poly) { poly.forEach(cb); });
  }
  function pathFor(geom) {
    var d = "";
    eachRing(geom, function (ring) {
      for (var i = 0; i < ring.length; i++) {
        var p = proj.project(ring[i]);
        d += (i === 0 ? "M" : "L") + p[0].toFixed(1) + " " + p[1].toFixed(1);
      }
      d += "Z";
    });
    return d;
  }

  // ---- render: meta / method picker / summary ------------------------------
  function renderMeta() {
    var e = data.election || {};
    $("#site-title").textContent = (data.state_name || "") + " Turnout Tracker";
    document.title = (data.state_name || "") + " Turnout Tracker — 2026";
    if (st && st.source_note) $("#source-note").innerHTML = st.source_note;
    var dateStr = e.date ? new Date(e.date + "T12:00:00").toLocaleDateString("en-US", { month: "long", day: "numeric", year: "numeric" }) : "";
    $("#election-name").textContent = (e.name || "Election") + (dateStr ? " · Election Day " + dateStr : "");
    var fetched = new Date(data.generated_at).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" });
    var upd = data.source_compiled_iso
      ? new Date(data.source_compiled_iso).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" })
      : (data.source_compiled || "—");
    $("#updated").innerHTML = "Data updated: <b>" + upd + "</b>";
    $("#updated").title = "Newest county TQV timestamp; page fetched " + fetched;
    $("#hash").textContent = "snapshot " + (data.data_hash || "").slice(0, 10) + " · primary: VR Systems TQV · fetched " + fetched;
  }

  function renderMethodPicker() {
    var host = $("#method-picker");
    host.innerHTML = "";
    METHODS.forEach(function (m) {
      var b = el("button");
      b.textContent = m.label;
      b.setAttribute("role", "tab");
      var avail = methodAvailable(m.key);
      b.setAttribute("aria-selected", String(m.key === method));
      if (!avail) { b.disabled = true; b.title = "No data yet"; b.style.opacity = ".45"; b.style.cursor = "not-allowed"; }
      b.addEventListener("click", function () { if (!avail) return; method = m.key; updateHash(); renderAll(); });
      host.appendChild(b);
    });
    $("#method-hint").textContent = HINTS[method] || "";
  }

  function statCard(cls, k, v, d, barParts) {
    var c = el("div", "stat" + (cls ? " " + cls : ""));
    c.innerHTML = "<div class='k'>" + k + "</div><div class='v'>" + v + "</div><div class='d'>" + (d || "") + "</div>";
    if (barParts) {
      var bar = el("div", "bar");
      barParts.forEach(function (p) {
        if (!p.w) return;
        var i = el("i"); i.style.width = p.w + "%"; i.style.background = p.c; i.title = p.t || "";
        bar.appendChild(i);
      });
      c.appendChild(bar);
    }
    return c;
  }

  function renderSummary() {
    var host = $("#summary");
    host.innerHTML = "";
    var b = block(data.statewide, method);
    var total = b.total || 0;

    if (!partisan) {
      var mlabel = METHODS.filter(function (m) { return m.key === method; })[0].label;
      host.appendChild(statCard("", mlabel, fmt(total), "ballots in this category"));
      var reg = data.statewide.registered || 0, tp = data.statewide.turnout_pct;
      if (reg) host.appendChild(statCard("", "Turnout", (tp == null ? "0.00" : tp.toFixed(2)) + "%",
        fmt((data.statewide.cast || {}).total || 0) + " of " + fmt(reg) + " registered"));
      ["mail_voted", "early_voted", "election_day"].forEach(function (mk) {
        if (!methodAvailable(mk)) return;
        var bb = block(data.statewide, mk);
        if (bb.total) host.appendChild(statCard("", (data.method_labels && data.method_labels[mk]) || mk, fmt(bb.total), ""));
      });
      return;
    }
    host.appendChild(statCard("", (method === "mail_provided" ? "Ballots outstanding" : "Ballots (statewide)"),
      fmt(total), METHODS.filter(function (m) { return m.key === method; })[0].label,
      total ? [
        { w: b.rep_pct, c: "var(--rep)", t: "Republican" },
        { w: b.dem_pct, c: "var(--dem)", t: "Democratic" },
        { w: (b.npa_pct || 0) + (b.oth_pct || 0), c: "var(--npa)", t: "NPA / Other" }
      ] : null));
    host.appendChild(statCard("rep", "Republican", fmt(b.rep), pctText(b.rep_pct)));
    host.appendChild(statCard("dem", "Democratic", fmt(b.dem), pctText(b.dem_pct)));
    host.appendChild(statCard("", "No party / Other", fmt(b.npa + b.oth),
      pctText(b.npa_pct == null ? null : Math.round((b.npa_pct + b.oth_pct) * 10) / 10)));
    var mc = statCard("", "Partisan lean", marginText(b.margin), total ? "of ballots in this category" : "no ballots yet");
    mc.querySelector(".v").style.color = b.margin == null ? "var(--muted)" : (b.margin > 0 ? "var(--rep)" : b.margin < 0 ? "var(--dem)" : "var(--ink)");
    host.appendChild(mc);

    var reg = data.statewide.registered || 0;
    var castTotal = (data.statewide.cast || {}).total || 0;
    var tp = data.statewide.turnout_pct;
    if (reg) {
      host.appendChild(statCard("", "Turnout (all cast)",
        (tp == null ? "0.00" : tp.toFixed(2)) + "%",
        fmt(castTotal) + " of " + fmt(reg) + " registered"));
    }

    if (compareActive()) {
      var s22 = sw22();
      var sh = (b.margin != null && s22 && s22.margin != null) ? Math.round((b.margin - s22.margin) * 10) / 10 : null;
      var cc = statCard("", "Shift vs 2022", shiftText(sh),
        s22 ? ("2026 " + marginText(b.margin) + " vs 2022 " + marginText(s22.margin)) : "no 2022 data");
      cc.querySelector(".v").style.color = sh == null ? "var(--muted)" : sh > 0 ? "var(--rep)" : sh < 0 ? "var(--dem)" : "var(--ink)";
      host.appendChild(cc);
    }
  }

  // ---- render: map ---------------------------------------------------------
  function svgPath(d, fill, cls) {
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", d);
    p.setAttribute("fill", fill);
    if (cls) p.setAttribute("class", cls);
    return p;
  }

  // ---- pan / zoom (viewBox based, dependency-free) -------------------------
  var baseW = 0, baseH = 0, cur = null, panMoved = false;
  function pzInit() {
    baseW = proj.W; baseH = proj.H;
    if (!cur) cur = { x: 0, y: 0, w: baseW, h: baseH };
  }
  function pzApply() {
    if (cur) $("#map").setAttribute("viewBox", cur.x + " " + cur.y + " " + cur.w + " " + cur.h);
  }
  function pzClamp() {
    cur.w = Math.min(baseW, Math.max(baseW / 40, cur.w));
    cur.h = cur.w * (baseH / baseW);
    cur.x = Math.min(baseW - cur.w, Math.max(0, cur.x));
    cur.y = Math.min(baseH - cur.h, Math.max(0, cur.y));
  }
  function pzReset() { cur = { x: 0, y: 0, w: baseW, h: baseH }; pzApply(); }
  function pzZoomAt(factor, fx, fy) {
    var bx = cur.x + fx * cur.w, by = cur.y + fy * cur.h;
    cur.w = cur.w / factor;
    cur.h = cur.w * (baseH / baseW);
    cur.x = bx - fx * cur.w; cur.y = by - fy * cur.h;
    pzClamp(); pzApply();
  }
  function svgFrac(cx, cy) {
    var r = $("#map").getBoundingClientRect();
    return { fx: (cx - r.left) / r.width, fy: (cy - r.top) / r.height, r: r };
  }
  function setupPanZoom() {
    var map = $("#map");
    map.addEventListener("wheel", function (e) {
      e.preventDefault();
      var f = svgFrac(e.clientX, e.clientY);
      pzZoomAt(e.deltaY < 0 ? 1.2 : 1 / 1.2, f.fx, f.fy);
    }, { passive: false });
    var pointers = {}, startCur = null, startMid = null, startDist = 0, downXY = null;
    map.addEventListener("pointerdown", function (e) {
      try { map.setPointerCapture(e.pointerId); } catch (err) {}
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      panMoved = false; downXY = { x: e.clientX, y: e.clientY };
      startCur = { x: cur.x, y: cur.y, w: cur.w, h: cur.h };
      var ids = Object.keys(pointers);
      if (ids.length === 2) {
        var a = pointers[ids[0]], b = pointers[ids[1]];
        startDist = Math.hypot(a.x - b.x, a.y - b.y);
        startMid = svgFrac((a.x + b.x) / 2, (a.y + b.y) / 2);
      }
      map.classList.add("grabbing");
    });
    map.addEventListener("pointermove", function (e) {
      if (!pointers[e.pointerId]) return;
      pointers[e.pointerId] = { x: e.clientX, y: e.clientY };
      var ids = Object.keys(pointers);
      if (ids.length === 2 && startDist) {
        var a = pointers[ids[0]], b = pointers[ids[1]];
        var dist = Math.hypot(a.x - b.x, a.y - b.y);
        cur = { x: startCur.x, y: startCur.y, w: startCur.w, h: startCur.h };
        pzZoomAt(dist / startDist, startMid.fx, startMid.fy);
        panMoved = true;
        return;
      }
      if (downXY) {
        var r = map.getBoundingClientRect();
        cur.x = startCur.x - (e.clientX - downXY.x) * (startCur.w / r.width);
        cur.y = startCur.y - (e.clientY - downXY.y) * (startCur.h / r.height);
        if (Math.abs(e.clientX - downXY.x) + Math.abs(e.clientY - downXY.y) > 4) panMoved = true;
        pzClamp(); pzApply();
      }
    });
    function up(e) {
      delete pointers[e.pointerId];
      if (!Object.keys(pointers).length) { map.classList.remove("grabbing"); downXY = null; startDist = 0; }
      setTimeout(function () { panMoved = false; }, 60);
    }
    map.addEventListener("pointerup", up);
    map.addEventListener("pointercancel", up);
    $("#zoom-in").addEventListener("click", function () { pzZoomAt(1.5, 0.5, 0.5); });
    $("#zoom-out").addEventListener("click", function () { pzZoomAt(1 / 1.5, 0.5, 0.5); });
    $("#zoom-reset").addEventListener("click", pzReset);
  }

  function renderMap() {
    if (mapMode === "precinct") return renderPrecinctMap();
    return renderCountyMap();
  }

  function renderCountyMap() {
    var svg = $("#map");
    pzInit(); pzApply();
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var cmp = compareActive();
    var maxTot = 0;
    if (!partisan) {
      geo.features.forEach(function (ft) { var t = block(data.counties[ft.properties.name], method).total || 0; if (t > maxTot) maxTot = t; });
    }
    geo.features.forEach(function (ft) {
      var name = ft.properties.name;
      var b = block(data.counties[name], method);
      var fill;
      if (!partisan) {
        var cty = data.counties[name] || {};
        fill = (cty.turnout_pct != null) ? colorForTurnout(cty.turnout_pct)
             : colorForFrac(maxTot ? (b.total / maxTot) : 0);
      } else if (cmp) {
        var m22 = b22(name);
        var sh = (b.margin != null && m22 && m22.margin != null) ? (b.margin - m22.margin) : null;
        fill = sh == null ? "#c9ced6" : colorForMargin(sh);
      } else { fill = colorForMargin(b.margin); }
      var p = svgPath(pathFor(ft.geometry), fill, name === selected ? "sel" : null);
      p.setAttribute("data-name", name);
      p.addEventListener("mousemove", function (ev) { showTip(ev, name); });
      p.addEventListener("mouseleave", hideTip);
      p.addEventListener("click", function () { if (panMoved) return; selectCounty(name, true); });
      svg.appendChild(p);
    });
    renderLegend();
  }

  function precinctMethodIdx() { return PFIELD[method] || PFIELD.cast; }

  function renderPrecinctMap() {
    var svg = $("#map");
    pzInit(); pzApply();
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    // base: county outlines for statewide context
    geo.features.forEach(function (ft) {
      svg.appendChild(svgPath(pathFor(ft.geometry), "#eef1f4", "base"));
    });
    if (!precinctGeo) { renderLegend(); return; }
    var idx = precinctMethodIdx();
    var counts = (precinctData && precinctData.counties) || {};
    precinctGeo.features.forEach(function (ft) {
      var code = ft.properties.code, pid = ft.properties.precinct;
      var arr = counts[code] && counts[code][pid];
      var turnout = null;
      if (arr && arr[0]) turnout = 100 * (arr[idx] || 0) / arr[0];
      else if (arr) turnout = 0;
      var p = svgPath(pathFor(ft.geometry), colorForTurnout(turnout), "prec");
      p.setAttribute("data-code", code); p.setAttribute("data-prec", pid);
      p.addEventListener("mousemove", function (ev) { showPrecinctTip(ev, ft.properties, arr); });
      p.addEventListener("mouseleave", hideTip);
      svg.appendChild(p);
    });
    renderLegend();
  }

  function showPrecinctTip(ev, props, arr) {
    var tip = $("#tooltip");
    tip.hidden = false;
    var idx = precinctMethodIdx();
    var lines = "<b>" + props.county + " — Precinct " + props.precinct + "</b>";
    if (arr) {
      var val = arr[idx] || 0, elig = arr[0] || 0, cast = arr[1] || 0;
      var to = elig ? (100 * val / elig).toFixed(2) + "%" : "—";
      lines += "<br>" + METHODS.filter(function (m) { return m.key === method; })[0].label +
        ": " + fmt(val) + "<br>Cast: " + fmt(cast) + " of " + fmt(elig) + " eligible" +
        "<br>Turnout: <span class='tt-margin'>" + to + "</span>";
    } else {
      lines += "<br>No ballots cast yet";
    }
    tip.innerHTML = lines;
    var holder = $("#map-holder").getBoundingClientRect();
    tip.style.left = (ev.clientX - holder.left) + "px";
    tip.style.top = (ev.clientY - holder.top) + "px";
  }

  function renderLegend() {
    var lg = $("#legend");
    if (mapMode === "precinct" || !partisan) {
      lg.innerHTML = "<span>less</span><span class='grad grad-turnout'></span><span>more turnout</span>";
    } else if (compareActive()) {
      lg.innerHTML = "<span>more Dem</span><span class='grad'></span><span>more Rep</span><span style='margin-left:5px'>vs&nbsp;2022</span>";
    } else {
      lg.innerHTML = "<span>D+40</span><span class='grad'></span><span>R+40</span>";
    }
  }

  function setMode(mode) {
    if (mode === mapMode) return;
    mapMode = mode;
    var btns = $("#map-mode").querySelectorAll("button");
    for (var i = 0; i < btns.length; i++) {
      btns[i].setAttribute("aria-selected", String(btns[i].getAttribute("data-mode") === mode));
    }
    $("#map-title").textContent = mode === "precinct" ? "Turnout by precinct" : "Partisan lean by county";
    $("#map-note").textContent = mode === "precinct"
      ? "Shaded by turnout (share of eligible voters who have cast a ballot in the selected category). Precinct-level party is not published live, so lean stays on the county map. Counties fill in as their boundaries are added and voting begins."
      : "Red = Republican lean, blue = Democratic lean, by party registration of ballots in the selected category. Gray = no ballots yet.";
    if (mode === "precinct" && !precinctGeo && !precinctLoading) {
      precinctLoading = true;
      Promise.all([getJSON(PRECINCT_GEO_URL), getJSON(PRECINCT_DATA_URL).catch(function () { return { counties: {} }; })])
        .then(function (r) { precinctGeo = r[0]; precinctData = r[1]; precinctLoading = false; renderMap(); })
        .catch(function () { precinctLoading = false; renderMap(); });
    }
    renderMap();
    updateHash();
  }

  function showTip(ev, name) {
    var b = block(data.counties[name], method);
    var tip = $("#tooltip");
    tip.hidden = false;
    if (!partisan) {
      var cty = data.counties[name] || {};
      tip.innerHTML = "<b>" + name + "</b><br>Ballots: " + fmt(b.total) +
        (cty.turnout_pct != null ? "<br>Turnout: <span class='tt-margin'>" + pctText(cty.turnout_pct) + "</span>" : "");
      var hh = $("#map-holder").getBoundingClientRect();
      tip.style.left = (ev.clientX - hh.left) + "px"; tip.style.top = (ev.clientY - hh.top) + "px";
      return;
    }
    tip.innerHTML = "<b>" + name + "</b><br>" +
      "R " + fmt(b.rep) + " · D " + fmt(b.dem) + " · NPA " + fmt(b.npa) + "<br>" +
      "Total " + fmt(b.total) + " — <span class='tt-margin' style='color:" +
      (b.margin == null ? "#bbb" : b.margin > 0 ? "#ff8a8a" : "#9ec5ff") + "'>" + marginText(b.margin) + "</span>";
    if (compareActive()) {
      var m22 = b22(name);
      var sh = (b.margin != null && m22 && m22.margin != null) ? (b.margin - m22.margin) : null;
      tip.innerHTML += "<br>2022: " + marginText(m22 ? m22.margin : null) + " · shift <b>" + shiftText(sh) + "</b>";
    }
    if (compare && baseline && baseline.counties[name] && baseline.counties[name].method_mix) {
      var mm = baseline.counties[name].method_mix;
      tip.innerHTML += "<br><span style='opacity:.75'>2022 mix: mail " + mm.mail + "% · early " + mm.early + "% · e-day " + mm.eday + "%</span>";
    }
    var holder = $("#map-holder").getBoundingClientRect();
    tip.style.left = (ev.clientX - holder.left) + "px";
    tip.style.top = (ev.clientY - holder.top) + "px";
  }
  function hideTip() { $("#tooltip").hidden = true; }

  // ---- render: table -------------------------------------------------------
  var COLS = [
    { key: "county", label: "County", cls: "county" },
    { key: "rep", label: "Rep" }, { key: "dem", label: "Dem" },
    { key: "oth", label: "Other" }, { key: "npa", label: "NPA" },
    { key: "total", label: "Total" }, { key: "turnout", label: "Turnout" },
    { key: "margin", label: "Lean" }
  ];
  function activeCols() {
    if (!partisan) return [{ key: "county", label: "County", cls: "county" },
                           { key: "total", label: "Ballots" }, { key: "turnout", label: "Turnout" }];
    var cols = COLS.slice();
    if (compareActive()) { cols.push({ key: "m22", label: "2022" }); cols.push({ key: "shift", label: "Δ vs '22" }); }
    return cols;
  }
  function renderTableHead() {
    var tr = el("tr");
    activeCols().forEach(function (c) {
      var th = el("th", c.cls === "county" ? "county" : "");
      th.textContent = c.label;
      if (sort.key === c.key) {
        th.setAttribute("aria-sort", sort.dir < 0 ? "descending" : "ascending");
        var a = el("span", "arrow"); a.textContent = sort.dir < 0 ? " ▼" : " ▲"; th.appendChild(a);
      }
      th.addEventListener("click", function () {
        if (sort.key === c.key) sort.dir = -sort.dir;
        else { sort.key = c.key; sort.dir = (c.key === "county") ? 1 : -1; }
        renderTable();
      });
      tr.appendChild(th);
    });
    var thead = $("#table thead"); thead.innerHTML = ""; thead.appendChild(tr);
  }
  function renderTableBody() {
    var rows = geo.features.map(function (ft) {
      var name = ft.properties.name;
      var cty = data.counties[name] || {};
      var b = block(cty, method);
      var m22b = compareActive() ? b22(name) : null;
      var m22 = m22b ? m22b.margin : null;
      var shift = (compareActive() && b.margin != null && m22 != null) ? Math.round((b.margin - m22) * 10) / 10 : null;
      return { name: name, rep: b.rep, dem: b.dem, oth: b.oth, npa: b.npa, total: b.total,
               margin: b.margin, turnout: cty.turnout_pct, tqv: cty.tqv_url, source: cty.source,
               m22: m22, shift: shift };
    });
    if (filter) rows = rows.filter(function (r) { return r.name.toLowerCase().indexOf(filter) >= 0; });
    rows.sort(function (a, b) {
      var k = sort.key;
      if (k === "county") return a.name.localeCompare(b.name) * sort.dir;
      var av = a[k], bv = b[k];
      if (av == null) av = -Infinity; if (bv == null) bv = -Infinity;
      return (av - bv) * sort.dir;
    });
    var tb = $("#table tbody"); tb.innerHTML = "";
    rows.forEach(function (r) {
      var tr = el("tr"); tr.setAttribute("data-name", r.name);
      if (r.name === selected) tr.className = "sel";
      var pill = "<span class='pill' style='background:" + colorForMargin(r.margin) + ";color:" +
        (r.margin == null ? "#333" : "#fff") + "'>" + marginText(r.margin) + "</span>";
      var link = r.tqv ? " <a class='tqv-mini' href='" + r.tqv + "' target='_blank' rel='noopener' title='Live TQV feed for " + r.name + "'>&#8599;</a>" : "";
      if (!partisan) {
        tr.innerHTML = "<td class='county'>" + r.name + link + "</td><td>" + fmt(r.total) + "</td><td>" + pctText(r.turnout) + "</td>";
        tr.addEventListener("click", function (ev) { if (ev.target.closest("a")) return; selectCounty(r.name, false); });
        tb.appendChild(tr);
        return;
      }
      tr.innerHTML = "<td class='county'>" + r.name + link + "</td>" +
        "<td>" + fmt(r.rep) + "</td><td>" + fmt(r.dem) + "</td><td>" + fmt(r.oth) + "</td>" +
        "<td>" + fmt(r.npa) + "</td><td>" + fmt(r.total) + "</td>" +
        "<td>" + pctText(r.turnout) + "</td><td>" + pill + "</td>";
      if (compareActive()) {
        var pill22 = "<span class='pill' style='background:" + colorForMargin(r.m22) + ";color:" +
          (r.m22 == null ? "#333" : "#fff") + "'>" + marginText(r.m22) + "</span>";
        tr.innerHTML += "<td>" + pill22 + "</td><td class='" +
          (r.shift > 0 ? "sh-r" : r.shift < 0 ? "sh-d" : "") + "'>" + shiftText(r.shift) + "</td>";
      }
      tr.addEventListener("click", function (ev) {
        if (ev.target.closest("a")) return; // let the TQV link work without selecting
        selectCounty(r.name, false);
      });
      tb.appendChild(tr);
    });
  }
  function renderTable() { renderTableHead(); renderTableBody(); }

  // ---- selection sync ------------------------------------------------------
  function selectCounty(name, fromMap) {
    selected = (selected === name) ? null : name;
    // update map classes
    var paths = $("#map").querySelectorAll("path");
    for (var i = 0; i < paths.length; i++) {
      paths[i].setAttribute("class", paths[i].getAttribute("data-name") === selected ? "sel" : "");
    }
    renderTableBody();
    if (selected && !fromMap) {
      var row = $("#table tbody tr[data-name='" + cssEscape(selected) + "']");
      if (row) row.scrollIntoView({ block: "nearest" });
    }
    if (selected) openPrecincts(selected); else closePrecincts();
    updateHash();
  }
  function cssEscape(s) { return s.replace(/'/g, "\\'"); }

  // ---- precinct drill-down -------------------------------------------------
  var precinctCache = {};
  var PMETHODS = [
    { key: "mail_voted", label: "Mail" }, { key: "early_voted", label: "Early" },
    { key: "election_day", label: "Elec. Day" }, { key: "provisional", label: "Prov." }
  ];
  function closePrecincts() { $("#precinct-panel").hidden = true; }
  function openPrecincts(name) {
    var cty = data.counties[name] || {};
    var panel = $("#precinct-panel");
    panel.hidden = false;
    $("#precinct-title").textContent = name + " — precinct detail";
    var link = $("#precinct-tqv");
    if (cty.tqv_url) { link.href = cty.tqv_url; link.style.display = ""; } else { link.style.display = "none"; }
    var body = $("#precinct-body");
    var summary = "<div class='p-summary'>" +
      "Registered: <b>" + fmt(cty.registered || 0) + "</b> · Cast: <b>" + fmt((cty.cast || {}).total || 0) +
      "</b> · Turnout: <b>" + pctText(cty.turnout_pct) + "</b>" +
      (cty.source === "dos-fallback" ? " <span class='src-note'>(state file — live precinct feed not yet publishing)</span>" : "") +
      "</div>";
    body.innerHTML = summary + "<div class='loading'>Loading precincts…</div>";
    if (!cty.code || cty.source === "dos-fallback") {
      body.innerHTML = summary + "<p class='fineprint'>Precinct-level data isn't available for this county yet.</p>";
      return;
    }
    var render = function (payload) {
      if (name !== selected) return; // user moved on
      body.innerHTML = summary + precinctTable(payload);
    };
    if (precinctCache[cty.code]) { render(precinctCache[cty.code]); return; }
    getJSON("data/fl/precincts/" + cty.code + ".json").then(function (p) {
      precinctCache[cty.code] = p; render(p);
    }).catch(function () {
      if (name === selected) body.innerHTML = summary +
        "<p class='fineprint'>No precinct-level ballots recorded yet for this county.</p>";
    });
  }
  function precinctTable(p) {
    var rows = (p.precincts || []).slice().sort(function (a, b) { return (b.cast || 0) - (a.cast || 0); });
    if (!rows.length) return "<p class='fineprint'>No precinct-level ballots recorded yet.</p>";
    var head = "<tr><th class='county'>Precinct</th><th>Eligible</th>";
    PMETHODS.forEach(function (m) { head += "<th>" + m.label + "</th>"; });
    head += "<th>Cast</th><th>Turnout</th></tr>";
    var out = "";
    rows.forEach(function (r) {
      out += "<tr><td class='county'>" + r.precinct + "</td><td>" + fmt(r.eligible) + "</td>";
      PMETHODS.forEach(function (m) { out += "<td>" + fmt(r[m.key] || 0) + "</td>"; });
      out += "<td><b>" + fmt(r.cast || 0) + "</b></td><td>" + pctText(r.turnout_pct) + "</td></tr>";
    });
    return "<div class='table-scroll p-scroll'><table class='p-table'><thead>" + head +
      "</thead><tbody>" + out + "</tbody></table></div>" +
      "<p class='fineprint'>Ballots cast by method per precinct, with eligible voters. TQV does not publish party by precinct.</p>";
  }

  // ---- refresh / flash -----------------------------------------------------
  function flashUpdated() {
    var u = $("#updated");
    var f = el("span", "flash"); f.textContent = "  ✓ updated";
    u.appendChild(f);
    setTimeout(function () { if (f.parentNode) f.parentNode.removeChild(f); }, 4000);
  }
  function refresh() {
    getJSON(DATA_URL).then(function (nd) {
      if (!data || nd.data_hash !== data.data_hash) {
        data = nd; ensureMethodValid(); precinctCache = {}; renderAll();
        if (selected) openPrecincts(selected);
        if (precinctGeo) getJSON(PRECINCT_DATA_URL).then(function (pd) {
          precinctData = pd; if (mapMode === "precinct") renderMap();
        }).catch(function () {});
        loadTrends();
        flashUpdated();
      } else { data.generated_at = nd.generated_at; renderMeta(); }
    }).catch(function () { /* transient; try again next tick */ });
  }
  function ensureMethodValid() {
    if (!methodAvailable(method)) method = pickDefaultMethod();
  }
  function pickDefaultMethod() {
    // show whichever available method currently has the most data
    var best = "cast", bestN = -1;
    METHODS.forEach(function (m) {
      if (!methodAvailable(m.key)) return;
      var t = block(data.statewide, m.key).total || 0;
      if (t > bestN) { bestN = t; best = m.key; }
    });
    return bestN > 0 ? best : "cast";
  }

  // ---- mail return-rate panel ---------------------------------------------
  function renderMail() {
    var m = data.statewide.mail, panel = $("#mail-panel");
    if (!m || !m.requested) { panel.hidden = true; return; }
    panel.hidden = false;
    function stat(k, v) { return "<div class='m'><div class='k'>" + k + "</div><div class='v'>" + v + "</div></div>"; }
    $("#mail-stats").innerHTML =
      stat("Ballots sent", fmt(m.requested)) +
      stat("Returned", fmt(m.returned) + " <span style='font-size:14px;color:var(--muted)'>(" + pctText(m.return_rate) + ")</span>") +
      stat("Still outstanding", fmt(m.outstanding));
    var order = [["rep", "Republican", "var(--rep)"], ["dem", "Democratic", "var(--dem)"],
                 ["npa", "No party", "var(--npa)"], ["oth", "Other", "var(--npa)"]];
    $("#mail-parties").innerHTML = order.map(function (o) {
      var p = m.parties[o[0]] || { rate: null, ret: 0, req: 0 };
      var w = p.rate == null ? 0 : Math.min(100, p.rate);
      return "<div class='mail-row' title='" + fmt(p.ret) + " of " + fmt(p.req) + " returned'>" +
        "<span class='lbl'>" + o[1] + "</span>" +
        "<span class='track'><i class='fill' style='width:" + w + "%;background:" + o[2] + "'></i></span>" +
        "<span class='pct'>" + pctText(p.rate) + "</span></div>";
    }).join("");
  }

  // ---- trend charts (inline SVG, dependency-free) --------------------------
  var trendData = null;
  function loadTrends() {
    if (!HISTORY_URL) { trendData = null; $("#trends").hidden = true; return; }
    fetch(HISTORY_URL + "?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.text() : ""; })
      .then(function (txt) {
        trendData = txt.trim().split("\n").filter(Boolean).map(function (l) {
          try { return JSON.parse(l); } catch (e) { return null; }
        }).filter(Boolean);
        renderTrends();
      }).catch(function () {});
  }
  function seriesFrom(getY) {
    return (trendData || []).map(function (r) {
      return { t: new Date(r.generated_at), y: getY(r.statewide || {}) };
    }).filter(function (p) { return !isNaN(p.t.getTime()); });
  }
  function renderTrends() {
    var sec = $("#trends");
    if (!trendData || trendData.length < 2) { sec.hidden = true; return; }
    sec.hidden = false;
    var cast = seriesFrom(function (s) { return (s.cast && s.cast[4]) || 0; });
    var lean = seriesFrom(function (s) {
      var c = s.cast; return (c && c[4]) ? Math.round((c[0] - c[1]) / c[4] * 1000) / 10 : null;
    });
    drawLineChart($("#chart-cast"), cast, { ymin: 0, fmt: fmt, color: "#31a354" });
    drawLineChart($("#chart-lean"), lean, { symmetric: true, zero: true, fmt: marginText, color: "var(--accent)" });
  }
  function drawLineChart(svg, series, opts) {
    opts = opts || {};
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    var W = 320, H = 180, ml = 46, mr = 12, mt = 12, mb = 22;
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    var NS = "http://www.w3.org/2000/svg";
    function add(tag, attrs, text) {
      var e = document.createElementNS(NS, tag);
      for (var k in attrs) e.setAttribute(k, attrs[k]);
      if (text != null) e.textContent = text;
      svg.appendChild(e); return e;
    }
    var pts = series.filter(function (p) { return p.y != null; });
    if (pts.length < 2) { add("text", { x: ml, y: H / 2, class: "lbl" }, "Not enough data yet"); return; }
    var xs = pts.map(function (p) { return p.t.getTime(); });
    var ys = pts.map(function (p) { return p.y; });
    var xmin = Math.min.apply(null, xs), xmax = Math.max.apply(null, xs);
    var ymin = opts.ymin != null ? opts.ymin : Math.min.apply(null, ys);
    var ymax = opts.ymax != null ? opts.ymax : Math.max.apply(null, ys);
    if (opts.symmetric) { var a = Math.max(Math.abs(ymin), Math.abs(ymax), 1); ymin = -a; ymax = a; }
    if (ymin === ymax) ymax = ymin + 1;
    var px = function (t) { return ml + (t - xmin) / (xmax - xmin || 1) * (W - ml - mr); };
    var py = function (v) { return mt + (1 - (v - ymin) / (ymax - ymin || 1)) * (H - mt - mb); };
    add("line", { class: "axis", x1: ml, y1: mt, x2: ml, y2: H - mb });
    add("line", { class: "axis", x1: ml, y1: H - mb, x2: W - mr, y2: H - mb });
    [ymax, (ymax + ymin) / 2, ymin].forEach(function (v) {
      add("line", { class: "gridline", x1: ml, y1: py(v), x2: W - mr, y2: py(v) });
      add("text", { class: "lbl", x: ml - 5, y: py(v) + 3, "text-anchor": "end" }, opts.fmt ? opts.fmt(Math.round(v)) : v);
    });
    if (opts.zero && ymin < 0 && ymax > 0) add("line", { class: "zero", x1: ml, y1: py(0), x2: W - mr, y2: py(0) });
    var d = pts.map(function (p, i) { return (i ? "L" : "M") + px(p.t.getTime()).toFixed(1) + " " + py(p.y).toFixed(1); }).join("");
    add("path", { d: d, fill: "none", stroke: opts.color || "var(--accent)", "stroke-width": 2 });
    var last = pts[pts.length - 1];
    add("circle", { cx: px(last.t.getTime()), cy: py(last.y), r: 3, fill: opts.color || "var(--accent)" });
    function dl(t) { return t.toLocaleDateString("en-US", { month: "short", day: "numeric" }); }
    add("text", { class: "lbl", x: ml, y: H - 7 }, dl(new Date(xmin)));
    add("text", { class: "lbl", x: W - mr, y: H - 7, "text-anchor": "end" }, dl(new Date(xmax)));
  }

  // ---- CSV export ----------------------------------------------------------
  function csvCell(s) { s = String(s); return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; }
  function exportCSV() {
    var methodLabel = METHODS.filter(function (m) { return m.key === method; })[0].label;
    var cmp = baseline && CMP_METHODS.indexOf(method) >= 0;
    var head = ["County", "FIPS", "Category", "Rep", "Dem", "Other", "NPA", "Total",
                "Turnout_pct", "Lean", "Registered", "Mail_return_pct"];
    if (cmp) head.push("Lean_2022", "Shift_vs_2022");
    var lines = [head.join(",")];
    Object.keys(data.counties).sort().forEach(function (name) {
      var c = data.counties[name], b = block(c, method);
      var row = [csvCell(name), c.fips || "", csvCell(methodLabel), b.rep, b.dem, b.oth, b.npa, b.total,
        c.turnout_pct == null ? "" : c.turnout_pct, b.margin == null ? "" : b.margin,
        c.registered || "", (c.mail && c.mail.return_rate != null) ? c.mail.return_rate : ""];
      if (cmp) {
        var m22 = b22(name); var m = m22 ? m22.margin : null;
        row.push(m == null ? "" : m, (b.margin != null && m != null) ? Math.round((b.margin - m) * 10) / 10 : "");
      }
      lines.push(row.join(","));
    });
    var blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    var url = URL.createObjectURL(blob), a = document.createElement("a");
    a.href = url; a.download = "fl-turnout-" + method + ".csv"; document.body.appendChild(a); a.click();
    document.body.removeChild(a); setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  // ---- shareable deep links (URL hash) ------------------------------------
  function updateHash() {
    var parts = [];
    if (st) parts.push("s=" + st.code);
    parts.push("m=" + method);
    if (mapMode !== "county") parts.push("v=" + mapMode);
    if (compare) parts.push("cmp=1");
    if (selected) parts.push("c=" + encodeURIComponent(selected));
    try { history.replaceState(null, "", "#" + parts.join("&")); } catch (e) {}
  }
  function parseHash() {
    var h = {};
    location.hash.slice(1).split("&").forEach(function (kv) {
      var i = kv.indexOf("="); if (i > 0) h[kv.slice(0, i)] = decodeURIComponent(kv.slice(i + 1));
    });
    return h;
  }
  function updateCmpNote() {
    var e = $("#cmp-note");
    if (!compare) { e.textContent = ""; return; }
    if (!baseline) { e.textContent = "loading 2022…"; return; }
    if (CMP_METHODS.indexOf(method) < 0) { e.textContent = "— no 2022 by-party data for this category"; return; }
    e.textContent = "— map & table show the partisan-lean shift vs 2022 (all methods incl. election day; Broward/Monroe/Volusia mail+early only)";
  }
  function showError(msg) {
    var m = document.querySelector("main");
    var ex = document.querySelector(".error"); if (ex) ex.remove();
    m.insertAdjacentHTML("afterbegin", "<div class='error'>Could not load data: " + msg + "</div>");
  }

  // load a state's config + data, wire feature visibility, render
  function loadState(code, applyHash) {
    st = null;
    for (var i = 0; i < STATES.length; i++) if (STATES[i].code === code) st = STATES[i];
    if (!st) st = STATES[0];
    var sel = $("#state-select"); if (sel) sel.value = st.code;
    // reset per-state state
    data = geo = precinctGeo = precinctData = baseline = trendData = null;
    precinctLoading = false; selected = null; filter = ""; cur = null;
    compare = false; mapMode = "county"; method = "cast";
    partisan = (st.partisan !== false);
    $("#filter").value = ""; var cb = $("#cmp-2022"); if (cb) cb.checked = false;
    DATA_URL = st.data; GEO_URL = st.geojson;
    PRECINCT_GEO_URL = st.precincts || null; PRECINCT_DATA_URL = st.precinct_data || null;
    BASELINE_URL = st.baseline || null; HISTORY_URL = st.history || null;
    // feature visibility
    $("#map-mode").style.display = st.precincts ? "" : "none";
    var row = $("#cmp-2022").closest(".controls-row"); if (row) row.style.display = st.baseline ? "" : "none";
    $("#trends").hidden = true; $("#mail-panel").hidden = true; $("#precinct-panel").hidden = true;

    Promise.all([getJSON(GEO_URL), getJSON(DATA_URL)]).then(function (res) {
      geo = res[0]; data = res[1]; proj = buildProjection(geo); method = pickDefaultMethod();
      if (applyHash) {
        var h = parseHash();
        if (h.m && methodAvailable(h.m)) method = h.m;
        if (h.cmp === "1" && BASELINE_URL) { compare = true; if (cb) cb.checked = true; }
      }
      renderAll();
      if (applyHash) {
        var h2 = parseHash();
        if (h2.v === "precinct" && PRECINCT_GEO_URL) setMode("precinct");
        if (h2.c && data.counties[h2.c]) selectCounty(h2.c, true);
      }
      loadTrends();
      if (BASELINE_URL) getJSON(BASELINE_URL).then(function (b) { baseline = b; renderAll(); }).catch(function () {});
      updateHash();
    }).catch(function (err) { showError(err.message); });
  }

  function renderAll() { renderMeta(); renderMethodPicker(); updateCmpNote(); renderSummary(); renderMail(); renderMap(); renderTable(); }

  // ---- boot ----------------------------------------------------------------
  function boot() {
    getJSON(STATES_URL).then(function (reg) {
      STATES = reg.states || [];
      var sel = $("#state-select");
      STATES.forEach(function (s) { var o = el("option"); o.value = s.code; o.textContent = s.name; sel.appendChild(o); });
      sel.addEventListener("change", function () { loadState(this.value, false); });
      setupPanZoom();
      $("#dl-csv").addEventListener("click", exportCSV);
      $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderTableBody(); });
      $("#precinct-close").addEventListener("click", function () { if (selected) selectCounty(selected, true); });
      $("#cmp-2022").addEventListener("change", function (e) { compare = e.target.checked; updateHash(); renderAll(); });
      var modeBtns = $("#map-mode").querySelectorAll("button");
      for (var i = 0; i < modeBtns.length; i++) modeBtns[i].addEventListener("click", function () { setMode(this.getAttribute("data-mode")); });
      setInterval(refresh, REFRESH_MS);
      var h = parseHash();
      var code = (h.s && STATES.some(function (s) { return s.code === h.s; })) ? h.s : (reg.default || (STATES[0] && STATES[0].code));
      loadState(code, true);
    }).catch(function (err) { showError(err.message); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
