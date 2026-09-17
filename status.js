/* Feed-status page — renders data/status.json (all 50 state turnout feeds + results). */
(function () {
  "use strict";
  var sort = { key: "name", dir: 1 }, filter = "", rows = [], notes = {};
  function $(s) { return document.querySelector(s); }
  function getJSON(u) { return fetch(u + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u); return r.json(); }); }
  function fmt(n) { return n == null ? "—" : n.toLocaleString("en-US"); }
  function when(s) { return s ? new Date(s).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—"; }
  function stateOf(s) { return s.frozen ? "frozen" : s.has_data ? "live" : "pending"; }

  function render() {
    var f = rows.filter(function (r) { return !filter || r.name.toLowerCase().indexOf(filter) >= 0; });
    f.sort(function (a, b) {
      var A, B;
      if (sort.key === "name") { A = a.name; B = b.name; }
      else if (sort.key === "status") { A = stateOf(a); B = stateOf(b); }
      else if (sort.key === "mode") { A = a.partisan ? "partisan" : "turnout"; B = b.partisan ? "partisan" : "turnout"; }
      else { A = a[sort.key] || 0; B = b[sort.key] || 0; }
      return A < B ? -sort.dir : A > B ? sort.dir : 0;
    });
    var cols = [["name", "State"], ["mode", "Mode"], ["status", "Status"], ["cast", "Ballots cast"], ["turnout_pct", "Turnout"], ["updated", "Last updated"]];
    $("#tbl thead").innerHTML = "<tr>" + cols.map(function (c) { return "<th data-k='" + c[0] + "'>" + c[1] + (sort.key === c[0] ? (sort.dir > 0 ? " ▲" : " ▼") : "") + "</th>"; }).join("") + "</tr>";
    $("#tbl thead").querySelectorAll("th").forEach(function (th) { th.onclick = function () { var k = th.getAttribute("data-k"); if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = 1; } render(); }; });
    $("#tbl tbody").innerHTML = f.map(function (r) {
      var st = stateOf(r);
      var src = notes[r.code] ? (" · " + notes[r.code]) : "";
      return "<tr class='" + st + "'><td><b>" + r.name + "</b></td>" +
        "<td><span class='tag " + (r.partisan ? "p'>partisan" : "t'>turnout-only") + "</span></td>" +
        "<td><span class='dot'></span>" + st.charAt(0).toUpperCase() + st.slice(1) + "</td>" +
        "<td>" + fmt(r.cast) + "</td><td>" + (r.turnout_pct == null ? "—" : r.turnout_pct + "%") + "</td>" +
        "<td>" + (r.has_data ? when(r.updated) : "—") + "</td></tr>";
    }).join("");
  }

  function boot() {
    Promise.all([getJSON("data/status.json"), getJSON("assets/states.json").catch(function () { return { states: [] }; })])
      .then(function (res) {
        var mf = res[0]; rows = mf.states || [];
        (res[1].states || []).forEach(function (s) {
          var m = (s.source_note || "").match(/>([^<]+)<\/a>/);
          notes[s.code] = m ? m[1] : "";
        });
        var c = mf.counts || {};
        $("#pills").innerHTML =
          "<span class='pill'>Total: <b>" + (c.total || rows.length) + "</b></span>" +
          "<span class='pill'>🟢 Live: <b>" + (c.live || 0) + "</b></span>" +
          "<span class='pill'>⚪ Pending: <b>" + (c.pending || 0) + "</b></span>";
        $("#updated").innerHTML = "Updated: <b>" + when(mf.generated_at) + "</b>";
        var rs = mf.results || {};
        $("#results-status").innerHTML = ["senate", "governor", "house"].map(function (o) {
          var r = rs[o] || {}, s = r.summary || {};
          var live = r.updated ? "🟢 live" : "⚪ pending";
          return "<b>" + o.charAt(0).toUpperCase() + o.slice(1) + "</b>: " + live +
            (s.total ? " · " + s.total + " races" + (s.uncalled != null ? " · " + s.uncalled + " uncalled" : "") : "");
        }).join(" &nbsp;·&nbsp; ");
        $("#src").textContent = "Auto-generated from each state's latest feed every cycle. Times are your local time.";
        $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); render(); });
        render();
        setInterval(function () { getJSON("data/status.json").then(function (mf2) { rows = mf2.states || []; render(); }).catch(function () {}); }, 10 * 60 * 1000);
      }).catch(function (e) { $("#pills").innerHTML = "<span class='pill'>Could not load status: " + e.message + "</span>"; });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
