/* Independent research panel: no trading-control calls, no live-state writes. */
(() => {
  "use strict";
  const root = document.getElementById("bt-root");
  if (!root) return;
  const el = id => document.getElementById("bt-" + id);
  const active = s => ["queued", "running", "cancelling"].includes(s);
  const esc = value => { const node = document.createElement("span"); node.textContent = String(value ?? ""); return node.innerHTML; };
  const money = value => value == null ? "—" : Number(value).toLocaleString("en-IN", {style:"currency", currency:"INR", maximumFractionDigits:2});
  const num = (value, digits=2) => value == null ? "—" : Number(value).toFixed(digits);
  const ist = value => value ? new Intl.DateTimeFormat("en-IN", {timeZone:"Asia/Kolkata", day:"2-digit", month:"short", year:"numeric", hour:"2-digit", minute:"2-digit", hour12:false}).format(new Date(value * 1000)) : "—";
  let options = null, runs = [], selected = null, loadedResult = null, timer = null, fetching = false, refreshAgain = false, selectedActive = false;
  const error = message => { el("error").textContent = message || ""; el("error").hidden = !message; };

  async function api(path, body) {
    const init = body === undefined ? {} : {
      method:"POST", headers:{"Content-Type":"application/json", "X-Control-Token":el("token").value}, body:JSON.stringify(body)
    };
    const response = await fetch(path, init);
    let data;
    try { data = await response.json(); } catch (_) { throw new Error("Server did not return JSON (HTTP " + response.status + ")"); }
    if (!response.ok) throw new Error(data.message || data.error || "HTTP " + response.status);
    return data;
  }

  function table(target, headers, rows) {
    if (!rows.length) { target.innerHTML = '<p class="bt-muted">No rows.</p>'; return; }
    target.innerHTML = "<table><thead><tr>" + headers.map(h => "<th>" + esc(h) + "</th>").join("") + "</tr></thead><tbody>" +
      rows.map(row => "<tr>" + row.map(cell => "<td>" + cell + "</td>").join("") + "</tr>").join("") + "</tbody></table>";
  }

  function renderRuns() {
    table(el("runs"), ["Run / view", "Source", "Period", "Status", "Net P&L"], runs.map(r => [
      '<button type="button" data-bt-run="' + esc(r.id) + '">' + esc(r.id.slice(0, 8)) + "</button>",
      r.request.source === "demo" ? "SYNTHETIC" : "Dhan",
      esc(r.request.start + " → " + r.request.end), esc(r.status), money(r.summary?.net_pnl)
    ]));
    el("runs").querySelectorAll("[data-bt-run]").forEach(button => button.onclick = () => {
      selected = button.dataset.btRun; loadedResult = null; el("results").hidden = true;
      refresh().catch(e => error(e.message));
    });
    el("run").disabled = !options || runs.some(r => active(r.status));
  }

  function renderStatus(run) {
    selectedActive = active(run.status);
    el("status").innerHTML = '<div class="bt-status-title">' + esc(run.status.toUpperCase()) + " · " +
      esc(run.request.source === "demo" ? "SYNTHETIC DEMO" : "DHAN HISTORY") + " · " + esc(run.id.slice(0, 8)) +
      '</div><div class="bt-status-detail">' + esc(run.detail) + (run.total ? " (" + num(run.progress, 0) + " / " + num(run.total, 0) + ")" : "") + "</div>";
    el("progress").hidden = !active(run.status);
    if (run.total) el("progress").value = Math.min(100, run.progress / run.total * 100);
    else el("progress").removeAttribute("value");
    el("cancel").hidden = !active(run.status);
    el("cancel").disabled = run.status === "cancelling";
  }

  function renderChart(points) {
    if (!points.length) { el("chart").textContent = "No equity points."; return; }
    // Bound SVG size while preserving each bucket's extrema (drawdowns stay visible).
    const sampled = [];
    const bucket = Math.max(1, Math.ceil(points.length / 600));
    for (let i = 0; i < points.length; i += bucket) {
      const group = points.slice(i, i + bucket).map((p, k) => ({p, i:i+k}));
      const candidates = [group[0], group[group.length - 1], group.reduce((a,b) => a.p.equity < b.p.equity ? a : b), group.reduce((a,b) => a.p.equity > b.p.equity ? a : b)];
      sampled.push(...candidates.sort((a,b) => a.i-b.i));
    }
    let low = Infinity, high = -Infinity;
    for (const p of points) { low = Math.min(low, p.equity); high = Math.max(high, p.equity); }
    const spread = Math.max(1, high - low), left = 15, width = 870;
    const polyline = sampled.map(({p,i}) => (left + i / Math.max(1, points.length - 1) * width).toFixed(2) + "," + (185 - (p.equity - low) / spread * 155).toFixed(2)).join(" ");
    el("chart").innerHTML = '<div class="bt-chart-axis"><span>High ' + money(high) + '</span><span>Low ' + money(low) + '</span></div>' +
      '<svg viewBox="0 0 900 210" role="img" aria-label="Mark to market equity curve"><path d="M15 30H885 M15 108H885 M15 185H885" stroke="#293440" stroke-width="1" fill="none"/><polyline points="' + polyline + '" fill="none" stroke="#58a6ff" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>' +
      '<div class="bt-chart-axis"><span>' + esc(ist(points[0].ts)) + '</span><span>' + esc(ist(points[points.length-1].ts)) + ' IST</span></div>';
  }

  function list(target, rows) {
    target.replaceChildren();
    rows.forEach(text => { const li = document.createElement("li"); li.textContent = text; target.appendChild(li); });
  }

  function renderResult(result) {
    el("results").hidden = false;
    const m = result.metrics;
    el("result-title").textContent = (result.data.source === "SYNTHETIC_DEMO" ? "Synthetic demonstration" : "Research results") + " · " + result.request.start + " → " + result.request.end;
    el("downloads").innerHTML = ["result.json", "trades.csv", "equity.csv", "daily.csv", "signals.csv"].map(name =>
      '<a class="bt-download" href="/api/backtests/' + encodeURIComponent(result.run_id) + '/export/' + name + '">' + name + " ↓</a>"
    ).join("");
    const cards = [
      ["Net P&L · includes open marks", money(m.net_pnl), m.net_pnl >= 0 ? "bt-positive" : "bt-negative"],
      ["Closed / open trades", m.closed_trades + " / " + m.open_trades, ""],
      ["Win rate · closed net trades", num(m.win_rate_pct) + "%", ""],
      ["Maximum MTM drawdown", money(m.max_drawdown), "bt-negative"],
      ["Gross realized P&L", money(m.gross_realized_pnl), ""],
      ["Unrealized P&L · last marks", money(m.unrealized_pnl), ""],
      ["Configured transaction costs", money(m.total_costs), ""],
      ["Profit factor · closed net trades", num(m.profit_factor), ""]
    ];
    el("metrics").innerHTML = cards.map(([label,value,cls]) => '<div class="bt-metric"><div class="bt-metric-label">' + esc(label) + '</div><div class="bt-metric-value ' + cls + '">' + esc(value) + '</div></div>').join("");
    renderChart(result.equity_curve);
    list(el("warnings"), result.warnings.length ? result.warnings : ["No detected coverage warnings. This does not guarantee data completeness or executable fills."]);
    list(el("assumptions"), result.assumptions);
    el("coverage").textContent = JSON.stringify({data:result.data, coverage:result.coverage, settings:result.settings, request:result.request}, null, 2);
    table(el("trades"), ["Contract / expiry", "Status", "Qty", "Entry · IST", "Sell", "Exit · IST", "Buy", "Gross P&L", "Costs", "Net closed P&L", "Exit reason"], result.trades.slice(0,250).map(t => [
      esc(t.underlying + " " + t.strike + " " + t.option_type + " · " + t.expiry), esc(t.status), num(t.quantity,0),
      esc(ist(t.entry_time)), num(t.entry_price), esc(ist(t.exit_time)), num(t.exit_price), money(t.gross_pnl), money(t.costs), money(t.net_pnl), esc(t.exit_reason || "Still open")
    ]));
    table(el("daily"), ["Date · IST", "Daily MTM P&L", "Cumulative P&L", "Equity"], result.daily_pnl.map(d => [esc(d.date), money(d.pnl), money(d.cumulative_pnl), money(d.equity)]));
  }

  async function refresh() {
    if (fetching) { refreshAgain = true; return; }
    fetching = true;
    clearTimeout(timer);
    try {
      runs = (await api("/api/backtests")).runs;
      renderRuns();
      if (selected) {
        const target = selected;
        const run = await api("/api/backtests/" + encodeURIComponent(target));
        if (selected !== target) { refreshAgain = true; return; }
        renderStatus(run);
        if (run.status === "completed" && loadedResult !== run.id) {
          const result = await api("/api/backtests/" + encodeURIComponent(run.id) + "/result");
          if (selected === run.id) { renderResult(result); loadedResult = run.id; }
          else refreshAgain = true;
        }
      }
    } catch (e) { error(e.message); }
    finally {
      fetching = false;
      if (refreshAgain) { refreshAgain = false; timer = setTimeout(refresh, 0); }
      else if (selectedActive || runs.some(r => active(r.status))) timer = setTimeout(refresh, 2000);
    }
  }

  el("form").addEventListener("submit", async event => {
    event.preventDefault(); error("");
    const symbols = Array.from(el("symbols").selectedOptions, option => option.value);
    const payload = {
      source:el("source").value, start:el("start").value, end:el("end").value,
      history_start:el("history").value, symbols, fill_mode:"candle_close",
      initial_capital:Number(el("capital").value), slippage_bps:Number(el("slippage").value),
      fee_per_order:Number(el("fee").value), cost_bps:Number(el("cost").value), close_at_end:el("close").checked
    };
    el("run").disabled = true;
    try {
      const run = await api("/api/backtests", payload);
      selected = run.id; loadedResult = null; el("results").hidden = true;
      renderStatus(run); await refresh();
    } catch (e) { error(e.message); el("run").disabled = false; }
  });
  el("cancel").onclick = async () => {
    error("");
    try { renderStatus(await api("/api/backtests/" + encodeURIComponent(selected) + "/cancel", {})); await refresh(); }
    catch (e) { error(e.message); }
  };
  el("refresh").onclick = () => { error(""); refresh(); };
  el("all").onclick = () => Array.from(el("symbols").options).forEach(o => o.selected = true);
  el("none").onclick = () => Array.from(el("symbols").options).forEach(o => o.selected = false);
  el("indices").onclick = () => Array.from(el("symbols").options).forEach(o => o.selected = ["NIFTY","BANKNIFTY"].includes(o.value));
  el("start").onchange = () => { if (el("start").value) el("history").value = el("start").value.slice(0,8) + "01"; };
  el("run").disabled = true;
  (async () => {
    try {
      options = await api("/api/backtests/options");
      el("symbols").replaceChildren();
      options.symbols.forEach(symbol => { const o = document.createElement("option"); o.value = symbol; o.textContent = symbol; o.selected = symbol === "NIFTY"; el("symbols").appendChild(o); });
      if (!el("symbols").selectedOptions.length && el("symbols").options.length) el("symbols").options[0].selected = true;
      const end = new Date(options.today + "T00:00:00Z"); end.setUTCDate(end.getUTCDate()-1);
      const start = new Date(end); start.setUTCDate(start.getUTCDate()-2);
      el("end").value = end.toISOString().slice(0,10); el("start").value = start.toISOString().slice(0,10);
      el("history").value = el("start").value.slice(0,8) + "01";
      el("start").max = options.today; el("end").max = options.today; el("history").max = options.today;
      el("capital").value = options.initial_capital; el("slippage").value = options.slippage_bps;
      el("connection").textContent = options.dhan_configured ? "Dhan credentials configured on server; data access is checked at run time." : "Dhan credentials are not configured here. Configure them on the server, or explicitly select the synthetic demo.";
      el("token-label").hidden = !options.control_token_required;
      el("settings").textContent = JSON.stringify({strategy:options.strategy, risk:options.risk, weekly_expiries:options.weekly_expiries}, null, 2);
      await refresh();
      if (runs.length) { selected = (runs.find(r => active(r.status)) || runs[0]).id; await refresh(); }
    } catch (e) { error(e.message); }
  })();
})();
