/* Registration vs. Turnout — compares each party's share of ballots cast to its
   share of registered voters, by state and nationally. Reads data/registration.json
   (registration by party) + data/status.json (early-vote party mix). */
(function () {
  "use strict";
  var reg = null, mf = null, rows = [], sort = { key: "cast_total", dir: -1 }, filter = "";
  function $(s) { return document.querySelector(s); }
  function getJSON(u) { return fetch(u + "?t=" + Date.now(), { cache: "no-store" }).then(function (r) { if (!r.ok) throw new Error(u); return r.json(); }); }
  function fmt(n) { return n == null ? "—" : Math.round(n).toLocaleString("en-US"); }
  function pctS(x) { return x == null ? "—" : x.toFixed(1) + "%"; }
  function gap(x) { return x == null ? "—" : (x > 0 ? "+" : "") + x.toFixed(1); }

  function build() {
    var statusByCode = {}; (mf.states || []).forEach(function (s) { statusByCode[s.code] = s; });
    rows = [];
    var regStates = reg.states || {};
    Object.keys(regStates).forEach(function (code) {
      var rg = regStates[code], rt = rg.total || (rg.rep + rg.dem + rg.npa + rg.oth);
      var st = statusByCode[code] || {};
      var evTot = (st.rep || 0) + (st.dem || 0) + (st.npa || 0) + (st.oth || 0);
      var row = {
        code: code, name: (st.name || code.toUpperCase()), as_of: rg.as_of,
        reg_total: rt, cast_total: st.has_data ? evTot : 0,
        regR: rt ? 100 * rg.rep / rt : null, regD: rt ? 100 * rg.dem / rt : null, regN: rt ? 100 * (rg.npa + rg.oth) / rt : null,
        evR: evTot ? 100 * st.rep / evTot : null, evD: evTot ? 100 * st.dem / evTot : null, evN: evTot ? 100 * ((st.npa || 0) + (st.oth || 0)) / evTot : null
      };
      row.gapR = (row.evR != null && row.regR != null) ? Math.round((row.evR - row.regR) * 10) / 10 : null;
      row.gapD = (row.evD != null && row.regD != null) ? Math.round((row.evD - row.regD) * 10) / 10 : null;
      rows.push(row);
    });
  }

  function national() {
    var R = 0, D = 0, N = 0, eR = 0, eD = 0, eN = 0, n = 0;
    rows.forEach(function (r) {
      if (r.cast_total > 0 && r.reg_total > 0) {  // states with BOTH, for apples-to-apples
        n++;
        R += r.reg_total * r.regR / 100; D += r.reg_total * r.regD / 100; N += r.reg_total * r.regN / 100;
        eR += r.cast_total * r.evR / 100; eD += r.cast_total * r.evD / 100; eN += r.cast_total * r.evN / 100;
      }
    });
    var rt = R + D + N, et = eR + eD + eN;
    return rt && et ? {
      n: n,
      regR: 100 * R / rt, regD: 100 * D / rt, regN: 100 * N / rt,
      evR: 100 * eR / et, evD: 100 * eD / et, evN: 100 * eN / et
    } : null;
  }

  function mixbar(r, d, nn) {
    return "<div class='mixbar'><div style='width:" + (r || 0) + "%;background:var(--rep)'></div><div style='width:" + (d || 0) + "%;background:var(--dem)'></div><div style='width:" + (nn || 0) + "%;background:#adb5bd'></div></div>";
  }

  function renderCards() {
    var nat = national(), host = $("#cards");
    if (!nat) {
      host.innerHTML = "";
      var pn = $("#pre-note"); pn.hidden = false;
      pn.innerHTML = Object.keys(reg.states || {}).length
        ? "🗳️ Registration is loaded; the comparison fills in per state as early voting begins reporting."
        : "🗳️ <b>Coming online:</b> state party-registration data is being wired (it's published year-round); once in, this compares each party's ballots-cast share to its registration share, by state and nationally.";
      return;
    }
    $("#pre-note").hidden = true;
    function card(title, mix) {
      return "<div class='ncard'><div class='k'>" + title + "</div>" +
        "<div style='font-size:13px;margin-top:4px'><span class='r'>R " + mix.R.toFixed(1) + "%</span> · <span class='d'>D " + mix.D.toFixed(1) + "%</span> · <span class='n'>Other " + mix.N.toFixed(1) + "%</span></div>" +
        mixbar(mix.R, mix.D, mix.N) + "</div>";
    }
    var gapR = nat.evR - nat.regR, gapD = nat.evD - nat.regD;
    host.innerHTML =
      card("Registered (national, " + nat.n + " states)", { R: nat.regR, D: nat.regD, N: nat.regN }) +
      card("Ballots cast (same states)", { R: nat.evR, D: nat.evD, N: nat.evN }) +
      "<div class='ncard'><div class='k'>Turnout vs registration gap</div>" +
      "<div style='font-size:15px;font-weight:800;margin-top:6px'><span class='" + (gapR >= 0 ? "gpos" : "gneg") + "'>R " + gap(Math.round(gapR * 10) / 10) + "</span> &nbsp; <span class='" + (gapD >= 0 ? "gpos" : "gneg") + "'>D " + gap(Math.round(gapD * 10) / 10) + "</span></div>" +
      "<div class='k' style='margin-top:4px'>points of ballots-cast share vs registration share</div></div>";
  }

  function renderTable() {
    var cols = [["name", "State"], ["regR", "Reg R"], ["regD", "Reg D"], ["evR", "Cast R"], ["evD", "Cast D"], ["gapR", "Gap R"], ["gapD", "Gap D"], ["cast_total", "Ballots"]];
    var f = rows.filter(function (r) { return !filter || r.name.toLowerCase().indexOf(filter) >= 0; });
    f.sort(function (a, b) { var A = a[sort.key], B = b[sort.key]; if (sort.key === "name") return (A < B ? -1 : 1) * sort.dir; A = A == null ? -999 : A; B = B == null ? -999 : B; return (A - B) * sort.dir; });
    $("#tbl thead").innerHTML = "<tr>" + cols.map(function (c) { return "<th data-k='" + c[0] + "'>" + c[1] + (sort.key === c[0] ? (sort.dir > 0 ? " ▲" : " ▼") : "") + "</th>"; }).join("") + "</tr>";
    $("#tbl thead").querySelectorAll("th").forEach(function (th) { th.onclick = function () { var k = th.getAttribute("data-k"); if (sort.key === k) sort.dir *= -1; else { sort.key = k; sort.dir = k === "name" ? 1 : -1; } renderTable(); }; });
    $("#tbl tbody").innerHTML = f.map(function (r) {
      function g(v) { return v == null ? "<td>—</td>" : "<td class='" + (v > 0 ? "gpos" : v < 0 ? "gneg" : "") + "'>" + gap(v) + "</td>"; }
      return "<tr><td><b>" + r.name + "</b></td>" +
        "<td class='r'>" + pctS(r.regR) + "</td><td class='d'>" + pctS(r.regD) + "</td>" +
        "<td class='r'>" + pctS(r.evR) + "</td><td class='d'>" + pctS(r.evD) + "</td>" +
        g(r.gapR) + g(r.gapD) + "<td>" + fmt(r.cast_total) + "</td></tr>";
    }).join("") || "<tr><td colspan='8' class='dim'>No party-registration states wired yet.</td></tr>";
  }

  function render() { build(); renderCards(); renderTable(); $("#updated").innerHTML = "Updated: <b>" + (reg.generated_at ? new Date(reg.generated_at).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }) : "—") + "</b>"; }
  function boot() {
    Promise.all([getJSON("data/registration.json").catch(function () { return { states: {} }; }), getJSON("data/status.json").catch(function () { return { states: [] }; })])
      .then(function (res) { reg = res[0]; mf = res[1]; $("#filter").addEventListener("input", function (e) { filter = e.target.value.trim().toLowerCase(); renderTable(); }); render();
        setInterval(function () { Promise.all([getJSON("data/registration.json"), getJSON("data/status.json")]).then(function (r2) { reg = r2[0]; mf = r2[1]; render(); }).catch(function () {}); }, 10 * 60 * 1000); });
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot); else boot();
})();
