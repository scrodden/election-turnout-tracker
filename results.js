/* 2026 Election Results — dependency-free front end.
   National map (states, with AK/HI insets) + scorecard for Senate / Governor /
   House, colored by the current margin. Reads assets/us-states.geojson and
   data/results/<office>.json (empty until results are wired near Election Day). */
(function () {
  "use strict";
  var STATES_URL = "assets/us-states.geojson";
  // On Election Day (and the UTC day after, for late western counting) poll every
  // 2 minutes to match the faster server-side updates; otherwise every 10 minutes.
  function refreshMs() {
    var d = new Date().toISOString().slice(0, 10);
    return (d === "2026-11-03" || d === "2026-11-04") ? 2 * 60 * 1000 : 10 * 60 * 1000;
  }
  var INSET = { AK: null, HI: null };           // filled after load; drawn as boxes
  var office = "senate";
  // past results: data/results/history/<office>_<year>.json (same race ids as 2026, for later comparisons)
  var YEARS = { senate: ["2026", "2024", "2022", "2020", "2018"],
    governor: ["2026", "2024", "2022", "2020", "2018", "offcycle_latest", "offcycle_previous"],
    president: ["2024", "2020"], house: ["2026"], measures: ["2026"] };
  var YEAR_LABEL = { "2026": "2026 (live)", offcycle_latest: "Off-year: latest (KY/LA/MS 2023 · NJ/VA 2025)",
    offcycle_previous: "Off-year: previous (KY/LA/MS 2019 · NJ/VA 2021)" };
  var year = "2026";
  function isPast() { return year !== "2026"; }
  var NO_RACE = "#e6e8ec", ON_BALLOT = "#efe0b0";   // 2026 map before results: no race / race on the ballot
  // The last time each 2026 seat was up (2026-specific): Senate class II seats 2020, the
  // FL and OH specials (class III) 2022; governors 2022, NH / VT (two-year terms) 2024.
  var PREV = {
    senate: [["2020", function (r) { return r.special ? null : r.id; }],
             ["2022", function (r) { return r.special ? r.id.replace(/-special$/, "") : null; }]],
    governor: [["2024", function (r) { return r.id; }], ["2022", function (r) { return r.id; }]]
  };
  var prevIdx = {}, prevBusy = {}, histStore = {};
  function ensurePrev(o) {
    if (prevIdx[o] || prevBusy[o] || !PREV[o]) return;
    prevBusy[o] = true;
    Promise.all(PREV[o].map(function (p) {
      var u = "data/results/history/" + o + "_" + p[0] + ".json";
      return (histStore[u] ? Promise.resolve(histStore[u]) : getJSON(u).then(function (d) { histStore[u] = d; return d; })).catch(function () { return null; });
    })).then(function (docs) {
      var idx = {}, slate = (cache[o + "|2026"] || {}).races || [];
      slate.forEach(function (r) {
        for (var i = 0; i < PREV[o].length; i++) {
          var key = PREV[o][i][1](r), d = docs[i];
          var pr = key && d && d.races.filter(function (x) { return x.id === key; })[0];
          if (pr) { idx[r.id] = { race: pr, year: PREV[o][i][0] }; break; }
        }
      });
      prevIdx[o] = idx; prevBusy[o] = false;
      if (office === o && !isPast()) render();
    });
  }
  function pLetter(p) {
    p = p || "";
    if (/independ|petition|non-?partisan|unaffiliated|no party|unenrolled/i.test(p)) return "I";
    if (/^dfl$|democratic-npl/i.test(p)) return "D";
    return p.charAt(0).toUpperCase();
  }
  function lastName(n) {
    var w = String(n || "").replace(/\s+(Jr\.?|Sr\.?|II|III|IV)$/i, "").split(" ");
    return w[w.length - 1];
  }
  function ballotSorted(r) {   // Democrat / Republican first, then the rest, otherwise in the source's order
    var b = r.ballot || [];
    return b.filter(function (x) { return /^[DR]$/.test(pLetter(x.party)); }).concat(b.filter(function (x) { return !/^[DR]$/.test(pLetter(x.party)); }));
  }
  function ballotShort(r, max) {   // 'Collins (R) · Ossoff (D)' for the table / tooltip
    var b = r.ballot || []; if (!b.length) return "";
    max = max || 3;
    var major = b.filter(function (x) { return /^[DR]$/.test(pLetter(x.party)); }), rest = b.filter(function (x) { return major.indexOf(x) < 0; });
    var pick = major.concat(rest).slice(0, max);
    return pick.map(function (x) { return lastName(x.name) + " (" + pLetter(x.party) + ")"; }).join(" · ") + (b.length > max ? " +" + (b.length - max) : "");
  }
  function prevOf(r) { return !isPast() && prevIdx[office] ? prevIdx[office][r.id] || null : null; }
  function prevText(r, long) {
    var p = prevOf(r); if (!p) return "—";
    var c = computeRace(p.race);
    return (c.margin == null ? "—" : (c.leader || "") + "+" + c.margin) + " (" + p.year + (long && p.race.winner ? ", " + p.race.winner : "") + ")";
  }
  function linkify(s) {   // our own source notes: make any URL in them clickable
    return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/(https?:\/\/[^\s)]+[^\s).,;])/g, "<a href='$1' target='_blank' rel='noopener'>$1</a>");
  }
  function officeName(o) { return { senate: "U.S. Senate", governor: "Governor", house: "U.S. House", president: "President", measures: "Ballot measures" }[o]; }
  var geo = null, cache = {}, proj = null, sort = { key: "state", dir: 1 }, filter = "";
  var whatif = false, overrides = {};   // path-to-majority: {race_id: "D"|"R"|""}

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
    var rs = byState[usps]; if (!rs || !rs.length) return NO_RACE;
    if (office === "measures") {
      var pass = 0, dec = 0;
      rs.forEach(function (r) { if (r.called === "Pass" || (r.candidates && r.candidates.length && r.yes_pct != null)) { dec++; if (r.called === "Pass" || (r.yes_pct != null && r.yes_pct >= 50)) pass++; } });
      if (!dec) return "#cfe8cf";                 // has measures, none decided yet
      var f = Math.max(0, Math.min(1, pass / rs.length));
      return "rgb(" + Math.round(199 - 150 * f) + "," + Math.round(233 - 90 * f) + "," + Math.round(192 - 140 * f) + ")"; // greener = more passing
    }
    if (office === "house") {
      var R = 0, D = 0, dec = 0;
      rs.forEach(function (r) { var c = computeRace(r); if (c.hasData) { dec++; if (c.leader === "R") R++; else if (c.leader === "D") D++; } });
      if (!dec) return ON_BALLOT;
      return color((R - D) / rs.length * 100);
    }
    var cc = computeRace(rs[0]);
    if (cc.hasData && cc.leader !== "D" && cc.leader !== "R") return "#9b84c4";   // independent / third-party leader or winner
    return cc.hasData ? color(cc.rd) : ON_BALLOT;
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
      node.addEventListener("click", function () {   // open the state's race (and, for past years, its county map)
        var rs = byState[node.getAttribute("data-u")];
        if (rs && rs.length && office === "measures") { tip.hidden = true; renderMeasures(rs); }
        else if (rs && rs.length && office === "house") { tip.hidden = true; renderHouseState(rs); }
        else if (rs && rs.length) { tip.hidden = true; renderDetail(rs[0], rs); }
      });
    });
    renderLegend();
  }
  function showTip(e, usps, byState) {
    var tip = $("#tooltip"), rs = byState[usps]; if (!rs) { tip.hidden = true; return; }
    var name = rs[0].state_name, html = "<b>" + name + "</b>";
    if (office === "measures") {
      html += "<br>" + rs.length + " statewide measure" + (rs.length === 1 ? "" : "s") + " on the ballot";
      rs.slice(0, 6).forEach(function (x) { html += "<br>&middot; " + x.title + (x.subject ? " — " + x.subject.split(";")[0] : ""); });
      if (rs.length > 6) html += "<br>&middot; +" + (rs.length - 6) + " more";
    } else if (office === "house") {
      var R = 0, D = 0, u = 0; rs.forEach(function (r) { var c = computeRace(r); if (!c.hasData) u++; else if (c.leader === "R") R++; else if (c.leader === "D") D++; });
      html += "<br>" + (u === rs.length ? rs.length + (rs.length === 1 ? " seat (at-large)" : " districts") + " on the ballot"
        : "House: " + R + "R / " + D + "D" + (u ? " · " + u + " uncalled" : "") + " of " + rs.length);
      if (!isPast()) html += "<br><span class='dim'>click for the " + (rs.length === 1 ? "race" : "district map") + "</span>";
    } else {
      var c = computeRace(rs[0]);
      html += "<br>" + rs[0].office + (isPast() && rs[0].year ? " " + rs[0].year : "") + "<br>" + (c.hasData ? (leanLabel(c.leader) + (c.leaderName ? " (" + c.leaderName + ")" : "") + " +" + c.margin + " · " +
        (isPast() ? (rs[0].first_round ? "won runoff" : "final") : (c.reporting != null ? c.reporting + "% in" : "—"))) : (isPast() ? "no result" : "on the ballot, no results yet"));
      rs.slice(1).forEach(function (x) { html += "<br>also: " + x.office + (x.special && !/special/i.test(x.office || "") ? " (special)" : ""); });
      if (!isPast() && !c.hasData && rs[0].ballot) html += "<br>" + ballotShort(rs[0], 4);
      if (!isPast() && prevOf(rs[0])) html += "<br>last time: " + prevText(rs[0], true);
    }
    var r = $("#map-holder").getBoundingClientRect();
    tip.innerHTML = html; tip.hidden = false;
    tip.style.left = Math.min(e.clientX - r.left + 12, r.width - 180) + "px";
    tip.style.top = (e.clientY - r.top + 12) + "px";
  }
  function leanLabel(p) { return p === "R" ? "Republican" : p === "D" ? "Democratic" : p === "I" ? "Independent" : p ? "Other" : "—"; }
  function renderLegend() {
    var host = $("#legend"); host.innerHTML = "";
    var wrap = el("div"); wrap.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted)";
    wrap.innerHTML = "<span>D+40</span><span style='width:120px;height:10px;border-radius:3px;display:inline-block;background:linear-gradient(90deg," + color(-40) + "," + color(0) + "," + color(40) + ")'></span><span>R+40</span>";
    host.appendChild(wrap);
    if (!isPast() && office !== "measures") {
      var k2 = el("div"); k2.style.cssText = "display:flex;align-items:center;gap:6px;font-size:11px;color:var(--muted);margin-top:4px";
      k2.innerHTML = "<span style='width:12px;height:10px;border-radius:2px;display:inline-block;background:" + ON_BALLOT + "'></span><span>on the ballot</span>" +
        "<span style='width:12px;height:10px;border-radius:2px;display:inline-block;margin-left:6px;background:" + NO_RACE + "'></span><span>no race</span>";
      host.appendChild(k2);
    }
  }

  // ---- scorecard ----
  function renderSummary(data) {
    var host = $("#summary"), s = data.summary || {}, races = data.races || [];
    if (office === "measures") {
      host.innerHTML = "";
      [["", "Measures", s.total || races.length], ["dem", "Passing/passed", s.passed || 0], ["rep", "Failing/failed", s.failed || 0], ["unc", "Undecided", s.undecided != null ? s.undecided : races.length]].forEach(function (p) {
        var e = el("span", "pill " + p[0]); e.innerHTML = p[1] + ": <b>" + p[2] + "</b>"; host.appendChild(e);
      });
      return;
    }
    var d = 0, r = 0, o = 0, u = 0;
    races.forEach(function (rc) { var c = computeRace(rc); if (!c.hasData && !rc.called) u++; else { var w = rc.called || c.leader; if (w === "D") d++; else if (w === "R") r++; else u++; } });
    if (!races.some(function (rc) { return computeRace(rc).hasData || rc.called; })) { d = r = o = 0; u = races.length; }
    host.innerHTML = "";
    var pills = isPast() ? [["dem", "Democratic wins", d], ["rep", "Republican wins", r], ["", "Other", u], ["", "Races", races.length]]
      : [["dem", "Dem leading/won", d], ["rep", "Rep leading/won", r], ["", "Total races", races.length], ["unc", "Not yet reported", u]];
    pills.forEach(function (p) {
      var e = el("span", "pill " + p[0]); e.innerHTML = p[1] + ": <b>" + p[2] + "</b>"; host.appendChild(e);
    });
  }
  function cols() {
    if (office === "measures") return [["state", "State"], ["title", "Measure"], ["yes", "Yes"], ["reporting", "% In"], ["status", "Status"]];
    return office === "house"
      ? [["state", "State"], ["district", "Dist"], ["leader", "Leader"], ["margin", "Margin"], ["reporting", "% In"], ["status", "Status"]]
      : isPast() ? [["state", "State"], ["leader", "Winner"], ["margin", "Margin"], ["votes", "Total votes"], ["status", "Result"]]
      : [["state", "State"], ["leader", "Leader"], ["margin", "Margin"], ["reporting", "% In"], ["status", "Status"]]
        .concat(PREV[office] ? [["prev", "Last result"]] : []);
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
      else if (sort.key === "votes") { A = a.total_votes || 0; B = b.total_votes || 0; }
      else if (sort.key === "leader") { A = ca.leader || "Z"; B = cb.leader || "Z"; }
      else if (sort.key === "district") { A = a.state_name + a.district; B = b.state_name + b.district; }
      else if (sort.key === "yes") { A = a.yes_pct == null ? -1 : a.yes_pct; B = b.yes_pct == null ? -1 : b.yes_pct; }
      else if (sort.key === "title") { A = a.title || ""; B = b.title || ""; }
      else if (sort.key === "prev") { var pa = prevOf(a), pb = prevOf(b), qa = pa && computeRace(pa.race), qb = pb && computeRace(pb.race);
        A = qa && qa.rd != null ? qa.rd : 999; B = qb && qb.rd != null ? qb.rd : 999; }
      else if (sort.key === "status") { A = (a.called ? 0 : ca.hasData ? 1 : 2); B = (b.called ? 0 : cb.hasData ? 1 : 2); }
      else { A = a.state_name; B = b.state_name; }
      return A < B ? -sort.dir : A > B ? sort.dir : 0;
    });
    tbody.innerHTML = rows.map(function (r) {
      if (office === "measures") {
        var decided = r.called || (r.yes_pct != null);
        var pass = r.called === "Pass" || (r.yes_pct != null && r.yes_pct >= 50);
        var yesTxt = r.yes_pct == null ? "—" : "Yes " + r.yes_pct + "%";
        var mstat = r.called ? "<span class='badge " + (r.called === "Pass" ? "dem" : "rep") + "'>" + r.called + "</span>"
          : decided ? "<span class='badge unc'>" + (pass ? "Passing" : "Failing") + "</span>" : "<span class='badge unc'>—</span>";
        var mtds = ["<span class='lean' style='background:" + (decided ? (pass ? "#2f9e44" : "#adb5bd") : "#c9ced6") + "'></span>" + r.state_name,
          "<b>" + (r.title || "Measure") + "</b>" + (r.subject ? "<div class='dim' style='font-size:12px'>" + r.subject + "</div>" : ""),
          yesTxt, r.reporting_pct == null ? "—" : r.reporting_pct + "%", mstat];
        return "<tr data-id='" + r.id + "' style='cursor:pointer'>" + mtds.map(function (x) { return "<td>" + x + "</td>"; }).join("") + "</tr>";
      }
      var c = computeRace(r), lc = color(office === "house" ? null : c.rd);
      var lead = c.hasData ? (leanLabel(c.leader) + (c.leaderName ? " " + c.leaderName : "")) : (!isPast() && r.ballot ? "<span class='dim'>" + ballotShort(r) + "</span>" : "—");
      var status = (isPast() && r.called) ? "<span class='badge " + (r.called === "D" ? "dem" : r.called === "R" ? "rep" : "unc") + "'>" +
          (r.year && year.indexOf("offcycle") === 0 ? r.year + " · " : "") + (r.first_round ? "Won runoff" : "Won") + "</span>"
        : r.called ? "<span class='badge " + (r.called === "D" ? "dem" : r.called === "R" ? "rep" : "unc") + "'>Called " + r.called + "</span>"
        : c.hasData ? "<span class='badge unc'>Leading</span>" : "<span class='badge unc'>" + (isPast() ? "—" : "On the ballot") + "</span>";
      var ovBadge = (whatif && (r.id in overrides)) ? " <span class='badge " + (overrides[r.id] === "D" ? "dem" : overrides[r.id] === "R" ? "rep" : "unc") + "'>" + (overrides[r.id] || "UND") + "?</span>" : "";
      var tds = ["<span class='lean' style='background:" + (c.hasData ? color(c.rd) : ON_BALLOT) + "'></span>" + r.state_name + (r.special ? " *" : "") + ovBadge];
      if (office === "house") tds.push(r.district);
      tds.push(lead, c.margin == null ? "—" : (c.leader || "") + "+" + c.margin,
        isPast() ? fmt(r.total_votes) : (c.reporting == null ? "—" : c.reporting + "%"), status);
      if (!isPast() && PREV[office]) tds.push(prevText(r));
      return "<tr data-id='" + r.id + "' style='cursor:pointer'>" + tds.map(function (x) { return "<td>" + x + "</td>"; }).join("") + "</tr>";
    }).join("");
    $("#score tbody").onclick = function (e) {
      var tr = e.target.closest("tr[data-id]"); if (!tr) return;
      var id = tr.getAttribute("data-id");
      if (whatif && !isPast()) {
        var cur = (id in overrides) ? (overrides[id] || "UND") : "actual";
        var nextMap = { "actual": "D", "D": "R", "R": "UND", "UND": "remove" };
        var nx = nextMap[cur];
        if (nx === "remove") delete overrides[id]; else overrides[id] = (nx === "UND" ? "" : nx);
        renderScore(cache[office]); renderWhatif(); return;
      }
      var rr = (cache[office].races || []).filter(function (x) { return x.id === id; })[0];
      if (rr) renderDetail(rr, (cache[office].races || []).filter(function (x) { return x.state === rr.state; }));
    };
    $("#score-note").textContent = rows.length + (office === "measures" ? " measures" : " races") + (rows.length ? " · click a row for detail" : "") + (office === "senate" ? " · * = special" : "");
  }

  // ---- county map for a past race (data/results/history/county/<office>_<year>.json) ----
  var countyCache = {}, shapeCache = {};
  var PARTY_COLOR = { D: "#2b6cb0", R: "#d62f2f", I: "#7e5bb5", L: "#c99a06", G: "#2f9e44" };
  var ALT_COLOR = { D: "#0e9aa7", R: "#e8833a", I: "#b07cc6", L: "#8d6e00", G: "#6aa84f" };
  function partyKey(p) {   // 'By Petition', 'Nonpartisan', 'Unaffiliated' ... read as independent
    p = p || "";
    return /independ|petition|non-?partisan|unaffiliated|no party|unenrolled/i.test(p) ? "I" : (p || "O").toUpperCase()[0];
  }
  function candColors(cands) {
    var seen = {};
    return cands.map(function (c) {
      var p = partyKey(c.party);
      var col = seen[p] ? (ALT_COLOR[p] || "#8a8f98") : (PARTY_COLOR[p] || "#6b7280");
      seen[p] = true; return col;
    });
  }
  function mix(hex, t) {   // white -> hex
    var n = parseInt(hex.slice(1), 16), c = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
    return "rgb(" + c.map(function (v) { return Math.round(255 + (v - 255) * t); }).join(",") + ")";
  }
  function getCached(store, url) {
    return store[url] ? Promise.resolve(store[url]) : getJSON(url).then(function (d) { store[url] = d; return d; });
  }
  function loadCountyMap(r, host, fileYear) {
    if (!host) return;
    if (r.state === "DC") { host.innerHTML = ""; return; }
    host.innerHTML = "<p class='dim'>Loading county map…</p>";
    getCached(countyCache, "data/results/history/county/" + office + "_" + (fileYear || year) + ".json").then(function (cd) {
      var e = (cd.races || {})[r.id];
      if (!e) {
        var why = (cd.missing || {})[r.id];
        host.innerHTML = "<p class='fineprint'>No county map for this race" + (r.state === "AK" ? " (Alaska reports results by state house district, not by borough)" :
          why ? " (" + why + ")" : "") + ".</p>";
        return;
      }
      return getCached(shapeCache, e.geo).then(function (g) { if (host.isConnected) drawCountyMap(host, e, g); });
    }).catch(function () { host.innerHTML = "<p class='fineprint'>County results aren't available for this race yet.</p>"; });
  }
  function drawCountyMap(host, e, g) {
    var feats = g.features, cols = candColors(e.cands);
    var mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    feats.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) {
      var x = p[0] > 0 ? p[0] - 360 : p[0];   // keep Aleutian islands west of the dateline together
      if (x < mnx) mnx = x; if (x > mxx) mxx = x; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), W = 600, s = W / ((mxx - mnx) * k), H = (mxy - mny) * s;
    if (H > 520) { s *= 520 / H; W = (mxx - mnx) * k * s; H = 520; }
    function pt(p) { var x = p[0] > 0 ? p[0] - 360 : p[0]; return ((x - mnx) * k * s).toFixed(1) + " " + ((mxy - p[1]) * s).toFixed(1); }
    var won = {};
    var paths = feats.map(function (ft) {
      var name = ft.properties.name, row = e.units[name], fill = "#e3e6ea";
      if (row) {
        var tot = row.reduce(function (a, b) { return a + b; }, 0), w = 0, sec = 0;
        for (var i = 1; i < e.cands.length; i++) if (row[i] > row[w]) w = i;
        for (var j = 0; j < e.cands.length; j++) if (j !== w && row[j] > sec) sec = row[j];
        if (tot) { won[w] = true; fill = mix(cols[w], 0.25 + 0.75 * Math.min(1, 100 * (row[w] - sec) / tot / 40)); }
      }
      var d = ""; eachRing(ft.geometry, function (ring) { d += "M" + ring.map(pt).join("L") + "Z"; });
      return "<path d='" + d + "' fill='" + fill + "' stroke='#fff' stroke-width='0.6' data-n=\"" + name.replace(/"/g, "&quot;") + "\"></path>";
    }).join("");
    var legend = e.cands.map(function (c, i) {
      return won[i] || i < 2 ? "<span class='cm-key'><i style='background:" + cols[i] + "'></i>" + c.name + "</span>" : "";
    }).join("");
    host.innerHTML = "<div class='cm-head'>" + (e.round === "first" ? "County results · first round (November)" : e.round === "runoff" ? "County results · runoff" : "County results") + "</div>" +
      "<div class='cm-wrap'><svg viewBox='0 0 " + Math.ceil(W) + " " + Math.ceil(H) + "' role='img' aria-label='County results map'>" + paths + "</svg>" +
      "<div class='cm-tip' hidden></div></div>" +
      "<div class='cm-legend'>" + legend + "<span class='dim'>darker = wider margin</span></div>" +
      (e.note ? "<p class='fineprint'>" + e.note + "</p>" : "") +
      "<p class='fineprint'>County figures: " + linkify(e.source) + "</p>";
    var tip = host.querySelector(".cm-tip"), wrap = host.querySelector(".cm-wrap");
    function show(ev) {
      var n = ev.target.getAttribute && ev.target.getAttribute("data-n"); if (!n) { tip.hidden = true; return; }
      var row = e.units[n], html = "<b>" + n + "</b>";
      if (!row) html += "<br>no results";
      else {
        var tot = row.reduce(function (a, b) { return a + b; }, 0);
        e.cands.forEach(function (c, i) { html += "<br><i class='dot' style='background:" + cols[i] + "'></i>" + c.name + ": " + fmt(row[i]) + (tot ? " (" + (100 * row[i] / tot).toFixed(1) + "%)" : ""); });
        if (row[e.cands.length]) html += "<br>Others: " + fmt(row[e.cands.length]);
      }
      tip.innerHTML = html; tip.hidden = false;
      var b = wrap.getBoundingClientRect();
      tip.style.left = Math.min(ev.clientX - b.left + 10, b.width - 190) + "px";
      tip.style.top = (ev.clientY - b.top + 10) + "px";
    }
    wrap.addEventListener("mousemove", show);
    wrap.addEventListener("click", show);
    wrap.addEventListener("mouseleave", function () { tip.hidden = true; });
  }

  function data_src() {
    var d = cache[office + "|" + year], b = d && d.ballot_source;
    return b && b.url ? "<a href='" + b.url + "' target='_blank' rel='noopener'>" + b.label + "</a>" : "";
  }
  function measureName(r) {   // 'California Proposition 1, Veteran and Housing ... Measure' -> 'Veteran and Housing ... Measure'
    var n = r.name || "", pre = (r.state_name + " " + r.title).toLowerCase();
    if (n.toLowerCase().indexOf(pre) === 0) n = n.slice(pre.length).replace(/^[,:\s-]+/, "");
    return n && n !== r.title ? n : "";
  }
  function renderMeasures(list) {   // a state's ballot measures (or one) in the detail panel
    var d = $("#detail"), body = $("#detail-body"), r0 = list[0];
    $("#detail-title").textContent = r0.state_name + " · " + (list.length > 1 ? list.length + " ballot measures" : r0.title);
    var src = (cache["measures|" + year] || {}).list_source;
    body.innerHTML = list.map(function (r) {
      var res = r.yes_pct != null ? "<b>Yes " + r.yes_pct + "%</b>" + (r.called ? " &middot; " + r.called : "") : "<span class='dim'>no results yet</span>";
      return "<div class='measure-item'><div><b>" + r.title + "</b> <span class='dim'>" + (r.type || "") + "</span></div>" +
        (measureName(r) ? "<div style='font-size:13px'>" + measureName(r) + "</div>" : "") +
        (r.description ? "<div class='dim' style='font-size:13px;margin-top:2px'>" + r.description + "</div>" : "") +
        "<div style='font-size:12.5px;margin-top:4px'>" + res + (r.url ? " &middot; <a href='" + r.url + "' target='_blank' rel='noopener'>details</a>" : "") + "</div></div>";
    }).join("") + (src ? "<p class='fineprint'>List: <a href='" + src.url + "' target='_blank' rel='noopener'>" + src.label + "</a>. Results come from official state sources on Election Night.</p>" : "");
    d.hidden = false; d.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }
  function renderDetail(r, siblings) {
    var d = $("#detail"), body = $("#detail-body"), c = computeRace(r);
    $("#detail-title").textContent = r.state_name + (r.district ? (" — " + districtLabel(r)) : "") + " · " + r.office + (r.title ? (" — " + r.title) : "") + (r.special && !/special/i.test(r.office || "") ? " (special)" : "");
    if (office === "measures") { renderMeasures([r]); return; }
    if (!r.candidates || !r.candidates.length) {
      var pv = prevOf(r);
      var onBallot = (!isPast() && r.ballot && r.ballot.length) ? "<h4 style='margin:10px 0 4px'>On the ballot</h4><ul class='ballot-list'>" +
        ballotSorted(r).map(function (x) { var pc = pLetter(x.party);
          return "<li><i style='background:" + (pc === "R" ? "#d62f2f" : pc === "D" ? "#2b6cb0" : pc === "I" ? "#7e5bb5" : "#6b7280") + "'></i>" + x.name +
            " <span class='dim'>(" + x.party + ")</span></li>"; }).join("") + "</ul>" +
        (data_src() ? "<p class='fineprint'>Candidates: " + data_src() + ". Official names and votes replace this list as results are reported.</p>" : "") : "";
      body.innerHTML = "<p class='dim'>" + (isPast() ? "No results for this race." : "On the ballot November 3, 2026 &mdash; no results yet.") + "</p>" + onBallot +
        (pv ? "<div class='detail-grid'><div><h4 style='margin:10px 0 2px'>Last time this seat was up (" + pv.year + ")</h4>" + bars(pv.race.candidates) +
          "<p class='fineprint'>" + (pv.race.first_round ? "Decided in a runoff. " : "") + "Source: " + (/^https?:\/\/\S+$/.test(pv.race.source || "") ?
            "<a href='" + pv.race.source + "' target='_blank' rel='noopener'>official results</a>" : linkify(pv.race.source || "")) + "</p></div>" +
          "<div id='county-map' class='county-map'></div></div>" : "");
      d.hidden = false; d.scrollIntoView({ behavior: "smooth", block: "nearest" });
      if (pv) loadCountyMap(pv.race, $("#county-map"), pv.year);
      houseExtras(r);
      return;
    }
    var tot = r.candidates.reduce(function (s, x) { return s + (x.votes || 0); }, 0);
    var sorted = r.candidates.slice().sort(function (a, b) { return (b.votes || 0) - (a.votes || 0); });
    function bars(list) {
      var t = list.reduce(function (s, x) { return s + (x.votes || 0); }, 0);
      return list.slice().sort(function (a, b) { return (b.votes || 0) - (a.votes || 0); }).map(function (x) {
        var p = t ? 100 * (x.votes || 0) / t : 0, pc = (x.party || "").toUpperCase()[0];
        var col = pc === "R" ? "#d62f2f" : pc === "D" ? "#2b6cb0" : "#6b7280";
        return "<div style='margin:8px 0'><div style='display:flex;justify-content:space-between;font-size:13px'><span><b>" + (x.name || "—") + "</b> (" + (x.party || "?") + ")</span><span>" + fmt(x.votes || 0) + " · " + p.toFixed(1) + "%</span></div>" +
          "<div style='height:9px;border-radius:5px;background:var(--line);margin-top:3px'><div style='width:" + p.toFixed(1) + "%;height:100%;border-radius:5px;background:" + col + "'></div></div></div>";
      }).join("");
    }
    if (isPast()) {
      var others = (siblings || []).filter(function (x) { return x.id !== r.id; });
      body.innerHTML = "<p class='dim'>" + (r.year || year) + " general · final results" + (r.runoff_note ? " · " + r.runoff_note : "") + "</p>" +
        (others.length ? "<p class='fineprint'>Also in " + r.state_name + ": " + others.map(function (x) {
          return "<a href='#' data-race='" + x.id + "'>" + (x.special ? "special election" : "regular election") + "</a>"; }).join(" · ") + "</p>" : "") +
        "<div class='detail-grid'><div>" +
        (r.first_round ? "<h4 style='margin:10px 0 2px'>Runoff</h4>" : "") + bars(r.candidates) +
        (r.first_round ? "<h4 style='margin:12px 0 2px'>First round</h4>" + bars(r.first_round.candidates) : "") +
        "</div><div id='county-map' class='county-map'></div></div>" +
        (r.note ? "<p class='fineprint'>" + linkify(r.note) + "</p>" : "") +
        (r.source ? "<p class='fineprint'>Source: " + (/^https?:\/\/\S+$/.test(r.source) ? "<a href='" + r.source + "' target='_blank' rel='noopener'>official results</a>" : linkify(r.source)) + "</p>" : "");
      body.querySelectorAll("[data-race]").forEach(function (a) {
        a.addEventListener("click", function (e) {
          e.preventDefault();
          var nx = (siblings || []).filter(function (x) { return x.id === a.getAttribute("data-race"); })[0];
          if (nx) renderDetail(nx, siblings);
        });
      });
      d.hidden = false; d.scrollIntoView({ behavior: "smooth", block: "nearest" });
      loadCountyMap(r, $("#county-map"));
      return;
    }
    body.innerHTML = "<p class='dim'>" + (c.reporting != null ? c.reporting + "% reporting" : "reporting —") + (r.called ? (" · Called " + r.called) : "") + "</p>" +
      sorted.map(function (x) {
        var p = tot ? 100 * (x.votes || 0) / tot : 0, pc = (x.party || "").toUpperCase()[0];
        var col = pc === "R" ? "#d62f2f" : pc === "D" ? "#2b6cb0" : pc === "Y" ? "#2f9e44" : pc === "N" ? "#adb5bd" : "#6b7280";
        return "<div style='margin:8px 0'><div style='display:flex;justify-content:space-between;font-size:13px'><span><b>" + (x.name || "—") + "</b> (" + (x.party || "?") + ")</span><span>" + fmt(x.votes || 0) + " · " + p.toFixed(1) + "%</span></div>" +
          "<div style='height:9px;border-radius:5px;background:var(--line);margin-top:3px'><div style='width:" + p.toFixed(1) + "%;height:100%;border-radius:5px;background:" + col + "'></div></div></div>";
      }).join("");
    houseExtras(r);
    d.hidden = false; d.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  // ---- House: a state's districts (assets/<st>-cd.geojson — the lines in use for November 2026) ----
  var districtGeo = {};
  function districtNo(r) { return r.district === "AL" ? 0 : parseInt(r.district, 10); }
  function districtLabel(r) { return r.district === "AL" ? "At-large" : "District " + parseInt(r.district, 10); }
  function houseRaces(usps) { return ((cache[office] || {}).races || []).filter(function (x) { return x.state === usps; }); }
  function districtFill(r) {
    if (!r) return NO_RACE;
    var c = computeRace(r);
    if (!c.hasData) return ON_BALLOT;
    return c.leader !== "D" && c.leader !== "R" ? "#9b84c4" : color(c.rd);
  }
  function renderHouseState(rs) {
    if (rs.length === 1) { renderDetail(rs[0], rs); return; }
    rs = rs.slice().sort(function (a, b) { return districtNo(a) - districtNo(b); });
    var d = $("#detail"), body = $("#detail-body"), r0 = rs[0], R = 0, D = 0, u = 0;
    rs.forEach(function (r) { var c = computeRace(r); if (!c.hasData) u++; else if (c.leader === "R") R++; else if (c.leader === "D") D++; });
    $("#detail-title").textContent = r0.state_name + " · U.S. House — " + rs.length + " districts";
    var rows = rs.map(function (r) {
      var c = computeRace(r);
      var who = c.hasData ? leanLabel(c.leader) + (c.leaderName ? " " + c.leaderName : "") + " +" + c.margin : (r.ballot ? ballotShort(r, 3) : "—");
      return "<tr data-id='" + r.id + "'><td><span class='lean' style='background:" + districtFill(r) + "'></span>" + (r.district === "AL" ? "AL" : districtNo(r)) +
        "</td><td>" + who + "</td><td>" + (c.reporting != null ? c.reporting + "%" : "—") + "</td></tr>";
    }).join("");
    body.innerHTML = "<p class='dim'>" + (u === rs.length ? "On the ballot November 3, 2026 &mdash; no results yet. " : R + " R &middot; " + D + " D leading &middot; " + u + " not yet reported. ") +
      "Hover a district for its candidates; click one for detail.</p>" +
      "<div class='house-grid'><div id='district-map' class='county-map'></div>" +
      "<div class='district-list'><table><thead><tr><th>District</th><th>" + (u === rs.length ? "Candidates" : "Leader") + "</th><th>% In</th></tr></thead><tbody>" + rows + "</tbody></table></div></div>" +
      (data_src() ? "<p class='fineprint'>Candidates: " + data_src() + ". Official names and votes replace them as results are reported.</p>" : "");
    body.querySelector(".district-list tbody").addEventListener("click", function (e) {
      var tr = e.target.closest("tr[data-id]"); if (!tr) return;
      var r = rs.filter(function (x) { return x.id === tr.getAttribute("data-id"); })[0];
      if (r) renderDetail(r, rs);
    });
    d.hidden = false; d.scrollIntoView({ behavior: "smooth", block: "nearest" });
    loadDistricts(r0.state, rs, $("#district-map"), null);
  }
  function houseExtras(r) {   // a House race's detail: back to the state's districts + where this one is
    if (office !== "house" || isPast()) return;
    var body = $("#detail-body"), all = houseRaces(r.state);
    if (all.length > 1) {
      var back = el("p", "fineprint"); back.style.marginTop = "0";
      back.innerHTML = "<a href='#'>&larr; All " + all.length + " " + r.state_name + " districts</a>";
      back.querySelector("a").addEventListener("click", function (e) { e.preventDefault(); renderHouseState(all); });
      body.insertBefore(back, body.firstChild);
    }
    var host = el("div", "county-map district-locator"); body.appendChild(host);
    loadDistricts(r.state, all, host, r.id);
  }
  function loadDistricts(usps, races, host, focusId) {
    if (!host) return;
    host.innerHTML = "<p class='dim'>Loading district map…</p>";
    getCached(districtGeo, "assets/" + usps.toLowerCase() + "-cd.geojson").then(function (g) { if (host.isConnected) drawDistricts(host, g, races, focusId); })
      .catch(function () { host.innerHTML = "<p class='fineprint'>The district map isn't available for this state.</p>"; });
  }
  function drawDistricts(host, g, races, focusId) {
    var byNum = {}; races.forEach(function (r) { byNum[districtNo(r)] = r; });
    var feats = g.features, mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    feats.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) {
      var x = p[0] > 0 ? p[0] - 360 : p[0];   // Aleutians west of the dateline
      if (x < mnx) mnx = x; if (x > mxx) mxx = x; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var maxH = focusId ? 300 : 520;
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), W = 600, s = W / ((mxx - mnx) * k), H = (mxy - mny) * s;
    if (H > maxH) { s *= maxH / H; W = (mxx - mnx) * k * s; H = maxH; }
    function xy(p) { var x = p[0] > 0 ? p[0] - 360 : p[0]; return [(x - mnx) * k * s, (mxy - p[1]) * s]; }
    var labels = [];
    var paths = feats.map(function (ft) {
      var n = ft.properties.district_number, r = byNum[n], fill = districtFill(r);
      if (focusId) fill = r && r.id === focusId ? (computeRace(r).hasData ? fill : "#c9a227") : "#e6e8ec";
      var d = "", best = null, bestA = 0;
      eachRing(ft.geometry, function (ring) {
        var pts = ring.map(xy), a = 0, cx = 0, cy = 0;
        d += "M" + pts.map(function (q) { return q[0].toFixed(1) + " " + q[1].toFixed(1); }).join("L") + "Z";
        for (var i = 0, j = pts.length - 1; i < pts.length; j = i++) {
          var f = pts[j][0] * pts[i][1] - pts[i][0] * pts[j][1]; a += f; cx += (pts[j][0] + pts[i][0]) * f; cy += (pts[j][1] + pts[i][1]) * f;
        }
        if (Math.abs(a) > bestA) { bestA = Math.abs(a); best = a ? [cx / (3 * a), cy / (3 * a)] : null; }
      });
      if (best && n) labels.push("<text x='" + best[0].toFixed(1) + "' y='" + best[1].toFixed(1) + "' data-a='" + Math.round(bestA / 2) + "' text-anchor='middle' dominant-baseline='central'>" + n + "</text>");
      return "<path d='" + d + "' fill='" + fill + "' stroke='#fff' vector-effect='non-scaling-stroke' stroke-width='" + (focusId && r && r.id === focusId ? 1.6 : 0.9) + "'" + (r ? " data-id='" + r.id + "'" : "") + "></path>";
    }).join("");
    host.innerHTML = (focusId ? "<div class='cm-head'>Where the district is</div>" : "") +
      "<div class='cm-wrap'><svg viewBox='0 0 " + Math.ceil(W) + " " + Math.ceil(H) + "' role='img' aria-label='Congressional district map'>" + paths +
      (focusId ? "" : "<g class='cd-labels'>" + labels.join("") + "</g>") + "</svg><div class='cm-tip' hidden></div>" +
      (focusId || feats.length < 4 ? "" : "<div class='zoom-btns'><button type='button' data-z='in' title='Zoom in'>+</button><button type='button' data-z='out' title='Zoom out'>&minus;</button>" +
        "<button type='button' data-z='reset' title='Reset'>&#8634;</button></div>") + "</div>" +
      (focusId || feats.length < 4 ? "" : "<p class='fineprint' style='margin-top:2px'>Zoom with + / &minus; or double-click; drag to pan.</p>") +
      "<p class='fineprint'>District lines: " + (g.source || "U.S. Census Bureau") + ".</p>";
    var tip = host.querySelector(".cm-tip"), wrap = host.querySelector(".cm-wrap");
    function raceAt(ev) { var id = ev.target.getAttribute && ev.target.getAttribute("data-id"); return id && races.filter(function (x) { return x.id === id; })[0]; }
    wrap.addEventListener("mousemove", function (ev) {
      var r = raceAt(ev); if (!r) { tip.hidden = true; return; }
      var c = computeRace(r), html = "<b>" + r.state_name + " " + districtLabel(r) + "</b>";
      if (c.hasData) html += "<br>" + leanLabel(c.leader) + (c.leaderName ? " (" + c.leaderName + ")" : "") + " +" + c.margin + (c.reporting != null ? " · " + c.reporting + "% in" : "");
      else if (r.ballot && r.ballot.length) ballotSorted(r).slice(0, 6).forEach(function (x) {
        var pc = pLetter(x.party);
        html += "<br><i class='dot' style='background:" + (pc === "R" ? "#d62f2f" : pc === "D" ? "#2b6cb0" : pc === "I" ? "#7e5bb5" : "#6b7280") + "'></i>" + x.name + " (" + x.party + ")";
      });
      else html += "<br>on the ballot, no results yet";
      tip.innerHTML = html; tip.hidden = false;
      var b = wrap.getBoundingClientRect();
      tip.style.left = Math.min(ev.clientX - b.left + 10, b.width - 190) + "px";
      tip.style.top = (ev.clientY - b.top + 10) + "px";
    });
    wrap.addEventListener("mouseleave", function () { tip.hidden = true; });
    // zoom / pan (state view): the viewBox moves; strokes stay thin and labels keep their size
    var svg = wrap.querySelector("svg"), vb = [0, 0, W, H], drag = null, moved = false;
    function setVB() {
      svg.setAttribute("viewBox", vb.map(function (v) { return v.toFixed(1); }).join(" "));
      var z = W / vb[2];
      svg.querySelectorAll(".cd-labels text").forEach(function (t) {
        t.setAttribute("font-size", (11 / z).toFixed(2));
        t.style.display = +t.getAttribute("data-a") * z * z > 450 ? "" : "none";
      });
    }
    function zoomAt(f, cx, cy) {   // f > 1 zooms in, around (cx, cy) in map units
      var w = Math.min(W, Math.max(W / 24, vb[2] / f)), h = w * H / W;
      vb = [Math.min(Math.max(0, cx - (cx - vb[0]) * w / vb[2]), W - w), Math.min(Math.max(0, cy - (cy - vb[1]) * h / vb[3]), H - h), w, h];
      setVB();
    }
    function fit() {   // screen px per map unit, and the letterbox offset (the svg can be capped by max-height)
      var b = svg.getBoundingClientRect(), k = Math.min(b.width / vb[2], b.height / vb[3]);
      return { k: k, x: b.left + (b.width - vb[2] * k) / 2, y: b.top + (b.height - vb[3] * k) / 2 };
    }
    function toMap(ev) { var f = fit(); return [vb[0] + (ev.clientX - f.x) / f.k, vb[1] + (ev.clientY - f.y) / f.k]; }
    setVB();
    if (!focusId && feats.length >= 4) {
      wrap.querySelectorAll("[data-z]").forEach(function (b) {
        b.addEventListener("click", function (ev) {
          ev.stopPropagation(); var z = b.getAttribute("data-z");
          if (z === "reset") { vb = [0, 0, W, H]; setVB(); } else zoomAt(z === "in" ? 2 : 0.5, vb[0] + vb[2] / 2, vb[1] + vb[3] / 2);
        });
      });
      svg.addEventListener("dblclick", function (ev) { ev.preventDefault(); var p = toMap(ev); zoomAt(2, p[0], p[1]); });
      svg.addEventListener("pointerdown", function (ev) { drag = { x: ev.clientX, y: ev.clientY, vb: vb.slice() }; moved = false; });
      svg.addEventListener("pointermove", function (ev) {
        if (!drag || vb[2] >= W) return;
        var f = fit(), dx = (ev.clientX - drag.x) / f.k, dy = (ev.clientY - drag.y) / f.k;
        if (!moved && Math.abs(ev.clientX - drag.x) + Math.abs(ev.clientY - drag.y) > 4) { moved = true; try { svg.setPointerCapture(ev.pointerId); } catch (e) {} }
        if (moved) { vb[0] = Math.min(Math.max(0, drag.vb[0] - dx), W - vb[2]); vb[1] = Math.min(Math.max(0, drag.vb[1] - dy), H - vb[3]); setVB(); tip.hidden = true; }
      });
      svg.addEventListener("pointerup", function () { drag = null; });
      svg.addEventListener("pointercancel", function () { drag = null; });
    }
    wrap.addEventListener("click", function (ev) {
      if (moved) { moved = false; return; }
      var r = raceAt(ev); if (r && r.id !== focusId) { tip.hidden = true; renderDetail(r, races); }
    });
    wrap.style.cursor = "pointer";
  }

  function renderMeta(data) {
    $("#map-title").textContent = office === "measures" ? "Ballot measures by state"
      : officeName(office) + (isPast() ? " — " + (YEAR_LABEL[year] || year) + " results" : " — current margin");
    var mn = $("#map-note");
    if (mn) mn.textContent = isPast()
      ? "Red = Republican win, blue = Democratic win, shaded by the final margin; purple = independent. Gray = no race that year." +
        (data.missing_note ? " " + data.missing_note + "." : "") + " Sources: MIT Election Data + Science Lab (president, Senate, most governors), official state results; open a row for its source."
      : "Red = Republican lead, blue = Democratic lead, by current margin. Tan = on the ballot, no results yet; gray = no " + officeName(office) + " race in 2026. " + (office === "house" ? "Click a state for its district map." : "Hover or click a state for the seat's last result.");
    if (isPast()) { $("#pre-note").hidden = true; $("#updated").innerHTML = "Final results"; return; }
    var upd = data.updated ? new Date(data.updated).toLocaleString("en-US", { hour: "numeric", minute: "2-digit", month: "short", day: "numeric" }) : null;
    $("#updated").innerHTML = upd ? "Updated: <b>" + upd + "</b>" : "<b>Not yet reported</b>";
    var any = (data.races || []).some(function (r) { return computeRace(r).hasData || r.called; });
    var pn = $("#pre-note");
    if (!any) { pn.hidden = false; pn.innerHTML = "🗳️ <b>Results will appear here live on Election Night</b> (Nov 3, 2026). Showing the full 2026 race slate; the map and scorecard fill in as official results are reported."; }
    else pn.hidden = true;
  }

  function renderBalance(data) {
    var host = $("#balance"), b = data.balance;
    if (!b) { host.hidden = true; return; }
    host.hidden = false;
    var T = b.total, cx = b.control;
    $("#balance-title").textContent = (office === "senate" ? "U.S. Senate" : "U.S. House") + " — balance of power";
    var segs = [
      ["dc", b.dem_called, "#2b6cb0"], ["dl", b.dem_lead, "#7aa9d6"],
      ["un", b.undecided, "#c9ced6"],
      ["rl", b.rep_lead, "#e08f7a"], ["rc", b.rep_called, "#d62f2f"]
    ];
    var bar = segs.map(function (s) {
      if (!s[1]) return "";
      return "<div title='" + s[1] + "' style='width:" + (100 * s[1] / T) + "%;background:" + s[2] + "'></div>";
    }).join("");
    // control marker (magic number) from the left
    bar += "<div style='position:absolute;top:-3px;bottom:-3px;left:" + (100 * cx / T) + "%;width:2px;background:var(--ink)'></div>";
    $("#balance-bar").innerHTML = bar;
    var v = $("#balance-verdict");
    if (b.dem_total >= cx) { v.textContent = "Democrats control"; v.style.color = "var(--dem)"; }
    else if (b.rep_total >= cx) { v.textContent = "Republicans control"; v.style.color = "var(--rep)"; }
    else { v.textContent = "Undecided"; v.style.color = "var(--muted)"; }
    $("#balance-note").innerHTML = "<span class='sh-d'>Dem " + b.dem_total + "</span> · " +
      "<span class='sh-r'>Rep " + b.rep_total + "</span> · " + b.undecided + " undecided · " +
      cx + " for control · " + T + " seats" +
      (b.not_up && (b.not_up.D || b.not_up.R) ? " (incl. " + b.not_up.D + "D/" + b.not_up.R + "R not up in 2026)" : "") +
      (b.dem_lead + b.rep_lead ? " · lighter shades = currently leading, not yet called" : "");
  }

  function whatifBalance() {
    var data = cache[office], b = data.balance; if (!b) return null;
    var nu = b.not_up || { D: 0, R: 0, other: 0 };
    var D = nu.D || 0, R = nu.R || 0, O = nu.other || 0, und = 0;
    (data.races || []).forEach(function (r) {
      var win = (r.id in overrides) ? overrides[r.id] : (r.called || computeRace(r).leader);
      if (win === "D") D++; else if (win === "R") R++; else if (win) O++; else und++;
    });
    return { D: D, R: R, O: O, und: und, control: b.control, total: b.total };
  }
  function renderWhatif() {
    var wb = $("#whatif-bar"), note = $("#whatif-note");
    if (!whatif || !cache[office] || !cache[office].balance) { wb.hidden = true; if (!whatif) note.textContent = ""; return; }
    var b = whatifBalance(), T = b.total, cx = b.control;
    function seg(w, c) { return w ? "<div style='width:" + (100 * w / T) + "%;background:" + c + "'></div>" : ""; }
    wb.hidden = false;
    wb.innerHTML = seg(b.D, "#2b6cb0") + seg(b.und, "#c9ced6") + seg(b.O, "#7a7f87") + seg(b.R, "#d62f2f") +
      "<div style='position:absolute;top:-3px;bottom:-3px;left:" + (100 * cx / T) + "%;width:2px;background:var(--ink)'></div>";
    var verdict = b.D >= cx ? "Democrats reach " + cx : b.R >= cx ? "Republicans reach " + cx : (b.und + " still undecided");
    var n = Object.keys(overrides).length;
    note.innerHTML = "<b>Projected:</b> <span class='sh-d'>D " + b.D + "</span> · <span class='sh-r'>R " + b.R + "</span> · " + b.und + " undecided — " + verdict +
      (n ? " · " + n + " set" : "") + ". Click uncalled races to cycle D → R → undecided.";
  }

  function render() {
    var data = cache[office + "|" + year];
    if (!isPast()) ensurePrev(office);
    if (isPast()) { $("#balance").hidden = true; }
    renderMeta(data); renderBalance(data); renderSummary(data); renderMap(data); renderScore(data); renderWhatif();
  }
  function fillYears() {
    var sel = $("#year"); if (!sel) return;
    var ys = YEARS[office] || ["2026"];
    if (ys.indexOf(year) < 0) year = ys[0];
    sel.innerHTML = ys.map(function (y) { return "<option value='" + y + "'" + (y === year ? " selected" : "") + ">" + (YEAR_LABEL[y] || y) + "</option>"; }).join("");
    sel.parentNode.hidden = ys.length < 2;
    // the President tab only for presidential years (2020, 2024)
    var pt = document.querySelector("#tabs [data-office='president']");
    if (pt) pt.hidden = (YEARS.president || []).indexOf(year) < 0;
  }
  function loadOffice(o) {
    office = o; fillYears();
    var dt = $("#detail"); if (dt) dt.hidden = true;
    $("#tabs").querySelectorAll("button").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-office") === o)); });
    try { history.replaceState(null, "", "#" + o + (year !== "2026" ? "&y=" + year : "")); } catch (e) {}
    var key = o + "|" + year;
    if (cache[key]) { cache[o] = cache[key]; render(); return; }
    var url = year === "2026" ? "data/results/" + o + ".json" : "data/results/history/" + o + "_" + year + ".json";
    getJSON(url).then(function (d) { cache[key] = d; cache[o] = d; render(); }).catch(function (e) { $("#summary").innerHTML = "<span class='pill'>Could not load results: " + e.message + "</span>"; });
  }
  function refresh() { cache = {}; loadOffice(office); }

  function boot() {
    getJSON(STATES_URL).then(function (g) {
      geo = g;
      $("#tabs").querySelectorAll("button").forEach(function (b) { b.addEventListener("click", function () { loadOffice(b.getAttribute("data-office")); }); });
      $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderScore(cache[office]); });
      $("#detail-close").addEventListener("click", function () { $("#detail").hidden = true; });
      $("#whatif-toggle").addEventListener("click", function () {
        whatif = !whatif;
        this.textContent = whatif ? "Path to majority ▾" : "Path to majority ▸";
        $("#whatif-reset").hidden = !whatif;
        renderScore(cache[office]); renderWhatif();
      });
      $("#whatif-reset").addEventListener("click", function () { overrides = {}; renderScore(cache[office]); renderWhatif(); });
      (function schedule() { setTimeout(function () { if (!isPast()) refresh(); schedule(); }, refreshMs()); })();
      var ys = $("#year");
      if (ys) ys.addEventListener("change", function (e) { year = e.target.value; loadOffice(office); });
      function fromHash() {   // #<office>[&y=<year>]
        var hm = location.hash.slice(1).split("&"), ho = hm[0], hy = (hm.filter(function (x) { return x.indexOf("y=") === 0; })[0] || "").slice(2);
        year = hy || "2026";
        loadOffice(YEARS[ho] ? ho : "senate");
      }
      window.addEventListener("hashchange", fromHash);
      fromHash();
    }).catch(function (e) { $("#summary").innerHTML = "<span class='pill'>Could not load map: " + e.message + "</span>"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
