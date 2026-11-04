/* Florida's implied NPA vote by county (fl-npa.html).
   Each county's 2026 Senate / Governor result (data/fl/results_county.json, Florida Election Watch) minus its
   Republican and Democratic ballots cast (data/fl/latest.json, the turnout tracker's county feeds): what's left of the
   margin, over NPA + minor-party ballots, is how those voters implicitly voted. */
(function () {
  "use strict";
  var TURNOUT_URL = "data/fl/latest.json", RESULTS_URL = "data/fl/results_county.json", GEO_URL = "assets/fl-counties.geojson";
  var RACES = {
    sen: { label: "U.S. Senate (special)", short: "Senate", file: "data/results/senate.json", id: "sen-FL-special" },
    gov: { label: "Governor", short: "Governor", file: "data/results/governor.json", id: "gov-FL" }
  };
  var REFRESH_MS = 10 * 60 * 1000;
  var race = /gov/.test(location.hash) ? "gov" : "sen";
  var turnout = null, results = null, geo = null, cands = {}, rows = [], sw = null;
  var sort = { key: "m", dir: 1 };

  function $(s) { return document.querySelector(s); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function pct(n, d) { return n == null ? "—" : n.toFixed(d == null ? 1 : d) + "%"; }
  function mText(m, d) { if (m == null || !isFinite(m)) return "—"; var v = Math.abs(m).toFixed(d == null ? 1 : d); return +v === 0 ? "Even" : (m > 0 ? "R+" : "D+") + v; }
  function getJSON(u) { return fetch(u + (u.indexOf("?") < 0 ? "?" : "&") + "t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u + " " + r.status); return r.json(); }); }
  function when(iso) { return iso ? new Date(iso).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—"; }

  // the site's diverging margin scale (blue - neutral gray - red), stretched to +/-60: implied NPA margins run wider
  // than county results
  var STOPS = [[-60, [33, 102, 172]], [-30, [103, 169, 207]], [0, [235, 237, 240]], [30, [239, 138, 98]], [60, [178, 24, 43]]];
  function color(m) {
    if (m == null || !isFinite(m)) return "#c9ced6";
    m = Math.max(STOPS[0][0], Math.min(STOPS[STOPS.length - 1][0], m));
    for (var i = 0; i < STOPS.length - 1; i++) {
      var a = STOPS[i], b = STOPS[i + 1];
      if (m <= b[0]) {
        var t = (m - a[0]) / (b[0] - a[0]);
        return "rgb(" + [0, 1, 2].map(function (j) { return Math.round(a[1][j] + t * (b[1][j] - a[1][j])); }).join(",") + ")";
      }
    }
    return "#c9ced6";
  }
  function pill(m) {
    var dark = m == null || Math.abs(m) < 14;
    return "<span class='pill' style='background:" + color(m) + ";color:" + (dark ? "#1a1f26" : "#fff") + "'>" + mText(m) + "</span>";
  }

  // ---- the arithmetic -------------------------------------------------------
  function figure(name, t, r) {   // t: ballots cast by party; r: the race's votes
    var pool = (t.npa || 0) + (t.oth || 0), votes = (r.rep || 0) + (r.dem || 0) + (r.other || 0);
    var impR = r.rep - t.rep, impD = r.dem - t.dem, ratio = t.total ? votes / t.total : null;
    return {
      name: name, rep: r.rep, dem: r.dem, other: r.other || 0, votes: votes, reporting: r.reporting,
      tR: t.rep, tD: t.dem, pool: pool, cast: t.total,
      impR: impR, impD: impD,
      m: pool ? 100 * (impR - impD) / pool : null,
      rShare: pool ? 100 * impR / pool : null, dShare: pool ? 100 * impD / pool : null,
      res: votes ? 100 * (r.rep - r.dem) / votes : null,
      tm: t.total ? 100 * (t.rep - t.dem) / t.total : null,
      share: t.total ? 100 * pool / t.total : null,
      ratio: ratio,
      cross: impR < 0 || impD < 0,
      flag: ratio != null && (ratio > 1.005 || ratio < 0.95)
    };
  }
  function compute() {
    rows = [];
    var tot = { rep: 0, dem: 0, other: 0 }, tt = { rep: 0, dem: 0, npa: 0, oth: 0, total: 0 };
    Object.keys(turnout.counties).sort().forEach(function (n) {
      var t = turnout.counties[n].cast, r = results.counties[n] && results.counties[n][race];
      if (!t || !r || !t.total) { rows.push({ name: n, missing: true }); return; }
      rows.push(figure(n, t, r));
      ["rep", "dem", "other"].forEach(function (k) { tot[k] += r[k] || 0; });
      ["rep", "dem", "npa", "oth", "total"].forEach(function (k) { tt[k] += t[k] || 0; });
    });
    sw = tt.total ? figure("Florida", tt, tot) : null;
  }

  // ---- render ----------------------------------------------------------------
  function candName(p) { var c = cands[race] && cands[race][p]; return c || (p === "rep" ? "the Republican" : "the Democrat"); }
  function lastName(n) { return /^the /.test(n) ? n : n.replace(/\s+(Jr\.?|Sr\.?|II|III|IV)$/i, "").split(/\s+/).pop(); }
  function stat(cls, k, v, d) { return "<div class='stat " + cls + "'><div class='k'>" + k + "</div><div class='v'>" + v + "</div><div class='d'>" + d + "</div></div>"; }

  function renderSummary() {
    var R = lastName(candName("rep")), D = lastName(candName("dem"));
    if (!sw) { $("#summary").innerHTML = ""; $("#story").textContent = "No county results for this race yet."; return; }
    var leanD = rows.filter(function (r) { return !r.missing && r.m < 0; }).length, n = rows.filter(function (r) { return !r.missing; }).length;
    var other = 100 - sw.rShare - sw.dShare;
    $("#summary").innerHTML =
      stat(sw.m > 0 ? "rep" : "dem", "NPAs &amp; minor parties (implied)", mText(sw.m), D + " " + pct(sw.dShare) + " · " + R + " " + pct(sw.rShare) + " · other/blank " + pct(other)) +
      stat(sw.res > 0 ? "rep" : "dem", "Result", mText(sw.res), R + " " + fmt(sw.rep) + " · " + D + " " + fmt(sw.dem)) +
      stat(sw.tm > 0 ? "rep" : "dem", "Ballots cast, by party", mText(sw.tm), "Republicans " + fmt(sw.tR) + " · Democrats " + fmt(sw.tD)) +
      stat("", "NPA &amp; minor-party ballots", fmt(sw.pool), pct(sw.share) + " of " + fmt(sw.cast) + " ballots") +
      stat("", "Counties where NPAs leaned " + (leanD * 2 >= n ? "Democratic" : "Republican"), (leanD * 2 >= n ? leanD : n - leanD) + " of " + n,
        rows.filter(function (r) { return r.cross; }).length + " hatched: party voters crossed over");
    var gap = sw.tR - sw.tD, won = sw.rep - sw.dem, net = sw.impR - sw.impD;
    $("#story").innerHTML = "Statewide, " + (gap >= 0 ? "Republicans out-voted Democrats by <b>" + fmt(gap) : "Democrats out-voted Republicans by <b>" + fmt(-gap)) +
      "</b> ballots, and " + (won >= 0 ? R : D) + " won by <b>" + fmt(Math.abs(won)) + "</b> votes. If every Republican voted for " + R +
      " and every Democrat for " + D + ", NPAs and minor-party voters made up the difference: <b>" + (net >= 0 ? R : D) + " by " + fmt(Math.abs(net)) +
      " votes</b>, " + mText(sw.m) + " among their " + fmt(sw.pool) + " ballots.";
  }

  function eachRing(g, cb) { if (!g) return; if (g.type === "Polygon") g.coordinates.forEach(cb); else if (g.type === "MultiPolygon") g.coordinates.forEach(function (p) { p.forEach(cb); }); }
  var byName = {};
  function renderMap() {
    var feats = geo.features, mnx = Infinity, mxx = -Infinity, mny = Infinity, mxy = -Infinity;
    feats.forEach(function (ft) { eachRing(ft.geometry, function (ring) { ring.forEach(function (p) {
      if (p[0] < mnx) mnx = p[0]; if (p[0] > mxx) mxx = p[0]; if (p[1] < mny) mny = p[1]; if (p[1] > mxy) mxy = p[1]; }); }); });
    var k = Math.cos((mny + mxy) / 2 * Math.PI / 180), W = 640, s = W / ((mxx - mnx) * k), H = (mxy - mny) * s;
    function pt(p) { return ((p[0] - mnx) * k * s).toFixed(1) + " " + ((mxy - p[1]) * s).toFixed(1); }
    byName = {}; rows.forEach(function (r) { byName[r.name] = r; });
    var base = "", over = "";
    feats.forEach(function (ft) {
      var n = ft.properties.name, r = byName[n], d = "";
      eachRing(ft.geometry, function (ring) { d += "M" + ring.map(pt).join("L") + "Z"; });
      var q = n.replace(/"/g, "&quot;");
      base += "<path class='c' d='" + d + "' fill='" + (r && !r.missing ? color(r.m) : "#c9ced6") + "' data-n=\"" + q + "\"></path>";
      if (r && r.cross) over += "<path class='hatch' d='" + d + "' fill='url(#" + (Math.abs(r.m) >= 30 ? "hatch-l" : "hatch-d") + ")'></path>";
      if (r && r.flag) over += "<path class='flag' d='" + d + "'></path>";
    });
    var svg = $("#map");
    svg.setAttribute("viewBox", "0 0 " + Math.ceil(W) + " " + Math.ceil(H));
    function hatch(id, ink) {   // light stripes on saturated fills, dark ones on pale fills
      return "<pattern id='" + id + "' patternUnits='userSpaceOnUse' width='6' height='6' patternTransform='rotate(45)'>" +
        "<line x1='3' y1='0' x2='3' y2='6' stroke='" + ink + "' stroke-width='1.8'/></pattern>";
    }
    svg.innerHTML = "<defs>" + hatch("hatch-l", "rgba(255,255,255,.8)") + hatch("hatch-d", "rgba(26,31,38,.6)") + "</defs>" + base + over;
    $("#map-title").textContent = "Implied NPA margin by county · " + RACES[race].short;
    var grad = "linear-gradient(90deg," + STOPS.map(function (x) { return "rgb(" + x[1].join(",") + ")"; }).join(",") + ")";
    $("#legend").innerHTML = "<div><div class='grad' style='background:" + grad + "'></div><div class='ends'><span>D+60</span><span>Even</span><span>R+60</span></div></div>" +
      "<span><i class='sw hatch'></i>party voters crossed over (the assumption fails)</span>" +
      "<span><i class='sw dash'></i>party turnout and votes counted disagree</span>";
  }

  function tipHTML(r) {
    if (!r || r.missing) return "<b>" + (r ? r.name : "") + "</b><br>no results yet";
    var R = lastName(candName("rep")), D = lastName(candName("dem"));
    var h = "<b>" + r.name + "</b><br><span class='big'>NPAs " + mText(r.m) + "</span>" +
      (r.pool ? " <span style='color:var(--muted)'>(" + D + " " + pct(r.dShare, 0) + " · " + R + " " + pct(r.rShare, 0) + ")</span>" : "") +
      "<br>Result: " + mText(r.res) + " · " + R + " " + fmt(r.rep) + ", " + D + " " + fmt(r.dem) +
      "<br>Ballots: Rep " + fmt(r.tR) + " · Dem " + fmt(r.tD) + " · NPA/minor " + fmt(r.pool);
    if (r.impD < 0) h += "<div class='warn'>" + D + " got " + fmt(-r.impD) + " fewer votes than Democrats cast ballots: at least that many Democrats voted for someone else or skipped the race.</div>";
    if (r.impR < 0) h += "<div class='warn'>" + R + " got " + fmt(-r.impR) + " fewer votes than Republicans cast ballots: at least that many Republicans voted for someone else or skipped the race.</div>";
    if (r.flag) h += "<div class='warn'>Votes in the race are " + pct(100 * r.ratio) + " of ballots cast with a party on file" +
      (r.ratio > 1 ? ": party turnout is incomplete here." : ": results may be incomplete.") + "</div>";
    return h;
  }
  function wireMap() {
    var wrap = $("#map-wrap"), tip = $("#tip");
    function show(ev) {
      var n = ev.target.getAttribute && ev.target.getAttribute("data-n");
      if (!n) { tip.hidden = true; return; }
      tip.innerHTML = tipHTML(byName[n]); tip.hidden = false;
      var b = wrap.getBoundingClientRect(), w = tip.offsetWidth;
      var x = ev.clientX - b.left + 12; if (x + w > b.width) x = Math.max(0, ev.clientX - b.left - w - 12);
      tip.style.left = x + "px"; tip.style.top = (ev.clientY - b.top + 12) + "px";
    }
    wrap.addEventListener("mousemove", show);
    wrap.addEventListener("click", function (ev) {
      show(ev);
      var n = ev.target.getAttribute && ev.target.getAttribute("data-n"), tr = n && document.querySelector("#tbl tr[data-n=\"" + n.replace(/"/g, '\\"') + "\"]");
      highlight(n);
      if (tr) tr.scrollIntoView({ block: "nearest", behavior: "smooth" });
    });
    wrap.addEventListener("mouseleave", function () { tip.hidden = true; });
  }
  function highlight(n) {
    document.querySelectorAll(".hi").forEach(function (x) { x.classList.remove("hi"); });
    if (!n) return;
    var q = "[data-n=\"" + n.replace(/"/g, '\\"') + "\"]";
    document.querySelectorAll("#map path" + q + ", #tbl tr" + q).forEach(function (x) { x.classList.add("hi"); });
  }

  var COLS = [["name", "County"], ["m", "Implied NPA"], ["res", "Result"], ["tm", "Ballots R−D"], ["share", "NPA share"], ["ratio", "Votes/ballots"]];
  function renderTable() {
    var thead = $("#tbl thead"), tbody = $("#tbl tbody");
    thead.innerHTML = "<tr>" + COLS.map(function (c) { return "<th data-k='" + c[0] + "'>" + c[1] + (sort.key === c[0] ? (sort.dir > 0 ? " ▲" : " ▼") : "") + "</th>"; }).join("") + "</tr>";
    var list = rows.slice().sort(function (a, b) {
      if (a.missing !== b.missing) return a.missing ? 1 : -1;
      var x = a[sort.key], y = b[sort.key];
      if (typeof x === "string") return sort.dir * x.localeCompare(y);
      return sort.dir * ((x == null ? Infinity : x) - (y == null ? Infinity : y));
    });
    tbody.innerHTML = list.map(function (r) {
      var q = r.name.replace(/"/g, "&quot;");
      if (r.missing) return "<tr data-n=\"" + q + "\"><td>" + r.name + "</td><td colspan='5' style='text-align:left' class='dim'>no results yet</td></tr>";
      return "<tr data-n=\"" + q + "\"><td>" + r.name + (r.cross ? "<span class='flagmark' title='Party voters crossed over'>▨</span>" : "") +
        (r.flag ? "<span class='flagmark' title='Party turnout and votes counted disagree'>⚠</span>" : "") + "</td>" +
        "<td>" + pill(r.m) + "</td><td>" + mText(r.res) + "</td><td>" + mText(r.tm) + "</td><td>" + pct(r.share) + "</td>" +
        "<td>" + (r.ratio == null ? "—" : r.ratio.toFixed(3)) + "</td></tr>";
    }).join("");
    thead.querySelectorAll("th").forEach(function (th) {
      th.onclick = function () { var k = th.getAttribute("data-k"); if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = k === "name" ? 1 : -1; } renderTable(); };
    });
    tbody.querySelectorAll("tr").forEach(function (tr) {
      tr.onmouseenter = function () { highlight(tr.getAttribute("data-n")); };
      tr.onclick = function () { highlight(tr.getAttribute("data-n")); };
    });
    $("#tbl-note").innerHTML = "Implied NPA: the county's leftover margin over NPA + minor-party ballots. Result: the race's margin among all its votes. " +
      "Ballots R−D: Republicans' lead over Democrats among ballots cast. NPA share: NPA + minor-party ballots as a share of all ballots. Votes/ballots: votes in the race over ballots cast with a party on file (below 1 is normal: some voters skip a race). " +
      "▨ crossover county · ⚠ turnout and votes counted disagree. Click a column to sort.";
  }

  function renderSources() {
    var t = turnout.source_compiled_iso || turnout.generated_at;
    $("#updated").innerHTML = "Results: <b>" + when(results.updated) + "</b> · Turnout: <b>" + when(t) + "</b>";
    $("#sources").innerHTML = "Results by county: <a href='https://floridaelectionwatch.gov/' target='_blank' rel='noopener'>Florida Election Watch</a> " +
      "(Florida Division of Elections, unofficial until certified), read every few minutes into <a href='data/fl/results_county.json'>data/fl/results_county.json</a>. " +
      "Ballots cast by party: county <a href='https://tqv.vrswebapps.com/?state=FL' target='_blank' rel='noopener'>Turnout Quick View</a> feeds (VR Systems), Broward's " +
      "<a href='https://my.browardvotes.gov/TEDElectionLink/TurnOutWidget/dashboard/view/turnout-summary' target='_blank' rel='noopener'>Supervisor of Elections dashboard</a>, and " +
      "Lake's <a href='https://www.lakevotes.gov/180/Current-Elections' target='_blank' rel='noopener'>Current Elections page</a> for Election Day: vote-by-mail returned + early votes + " +
      "Election Day check-ins, in <a href='data/fl/latest.json'>data/fl/latest.json</a>.";
  }

  function renderAll() {
    compute();
    document.querySelectorAll("#race-tabs button").forEach(function (b) { b.setAttribute("aria-selected", String(b.getAttribute("data-race") === race)); });
    renderSummary(); renderMap(); renderTable(); renderSources();
  }

  function csv() {
    var R = candName("rep"), D = candName("dem");
    var head = ["County", "Race", "Implied_NPA_margin", "Implied_NPA_R_pct", "Implied_NPA_D_pct", R + "_votes", D + "_votes", "Other_votes",
                "Rep_ballots", "Dem_ballots", "NPA_minor_ballots", "Ballots_total", "Result_margin", "Votes_over_ballots", "Crossover", "Data_flag"];
    var out = [head.join(",")];
    rows.concat(sw ? [sw] : []).forEach(function (r) {
      if (r.missing) return;
      var f1 = function (v) { return v == null ? "" : (Math.round(v * 10) / 10); };
      out.push(["\"" + r.name + "\"", RACES[race].short, f1(r.m), f1(r.rShare), f1(r.dShare), r.rep, r.dem, r.other, r.tR, r.tD, r.pool, r.cast,
        f1(r.res), r.ratio == null ? "" : r.ratio.toFixed(4), r.cross ? "yes" : "", r.flag ? "yes" : ""].join(","));
    });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([out.join("\n")], { type: "text/csv" }));
    a.download = "fl_implied_npa_" + race + ".csv"; a.click();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 1000);
  }

  function loadCands() {
    return Promise.all(Object.keys(RACES).map(function (k) {
      return getJSON(RACES[k].file).then(function (d) {
        var r = (d.races || []).filter(function (x) { return x.id === RACES[k].id; })[0], m = {};
        ((r && r.candidates) || []).forEach(function (c) { var p = (c.party || "").toUpperCase(); if (p === "REP" && !m.rep) m.rep = c.name; if (p === "DEM" && !m.dem) m.dem = c.name; });
        cands[k] = m;
      }).catch(function () { cands[k] = {}; });
    }));
  }
  function load() {
    return Promise.all([getJSON(TURNOUT_URL), getJSON(RESULTS_URL), loadCands()]).then(function (v) { turnout = v[0]; results = v[1]; renderAll(); });
  }

  function boot() {
    document.querySelectorAll("#race-tabs button").forEach(function (b) {
      b.addEventListener("click", function () { race = b.getAttribute("data-race"); history.replaceState(null, "", "#" + race); renderAll(); });
    });
    $("#csv").addEventListener("click", csv);
    getJSON(GEO_URL).then(function (g) { geo = g; wireMap(); return load(); }).catch(function (e) {
      $("#story").textContent = "Couldn't load the data (" + e.message + ").";
    });
    setInterval(function () { if (geo) load().catch(function () {}); }, REFRESH_MS);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
