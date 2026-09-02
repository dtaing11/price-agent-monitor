/* Price Monitor — desktop UI.
   No framework and no CDN: the app has to start on a machine with nothing
   installed but Python. Charts are hand-drawn SVG so they stay crisp and
   themeable without a charting library. */

const state = {
  products: [], alerts: [], job: {}, llm: "", sites: [],
  selected: null, filter: "all", sort: "name", query: "", tab: "detail",
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, kids = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) node.append(k);
  return node;
};

/* ---------------- formatting ---------------- */
const SYMBOLS = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", INR: "₹", CAD: "$", AUD: "$" };
function money(amount, currency) {
  if (amount === null || amount === undefined) return "—";
  const sym = SYMBOLS[currency];
  const n = amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return sym ? `${sym}${n}` : currency ? `${n} ${currency}` : n;
}
function when(iso) {
  if (!iso) return "never";
  const then = new Date(iso), mins = (Date.now() - then) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h ago`;
  return then.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const pct = (v) => (v === null || v === undefined ? "" : `${v > 0 ? "+" : ""}${v.toFixed(1)}%`);

/* ---------------- charts ---------------- */
function series(product) {
  return product.history.filter((p) => p.price !== null);
}

function sparkline(product, w = 300, h = 46) {
  const pts = series(product);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "spark");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  if (pts.length < 2) return svg;

  const prices = pts.map((p) => p.price);
  let lo = Math.min(...prices), hi = Math.max(...prices);
  if (product.target_price != null) { lo = Math.min(lo, product.target_price); hi = Math.max(hi, product.target_price); }
  // Breathing room, so a flat series sits mid-height instead of filling the box.
  const margin = (hi - lo) * 0.25 || Math.max(hi * 0.04, 0.5);
  lo -= margin; hi += margin;
  const span = hi - lo || 1, pad = 5;
  const x = (i) => (i / (pts.length - 1)) * w;
  const y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const trend = prices.at(-1) < prices[0] ? "down" : prices.at(-1) > prices[0] ? "up" : "flat";
  const stroke = trend === "down" ? "var(--down)" : trend === "up" ? "var(--up)" : "var(--faint)";

  const line = prices.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const area = `M0,${h} ` + prices.map((v, i) => `L${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ") + ` L${w},${h} Z`;
  svg.innerHTML =
    `<path d="${area}" fill="${stroke}" opacity=".09"/>` +
    (product.target_price != null
      ? `<line x1="0" y1="${y(product.target_price).toFixed(1)}" x2="${w}" y2="${y(product.target_price).toFixed(1)}"
           stroke="var(--down)" stroke-width="1" stroke-dasharray="3 4" opacity=".65"/>` : "") +
    `<path d="${line}" fill="none" stroke="${stroke}" stroke-width="2"
       stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>` +
    `<circle cx="${x(prices.length - 1).toFixed(1)}" cy="${y(prices.at(-1)).toFixed(1)}" r="2.6" fill="${stroke}"/>`;
  return svg;
}

/* Full interactive chart with axes, target line and a hover readout. */
function bigChart(product) {
  const pts = series(product);
  const wrap = el("div", { className: "chart-wrap" });
  if (pts.length < 2) {
    wrap.append(el("p", { className: "hint", textContent:
      pts.length === 1 ? "One data point so far — the line appears after the next check." : "No price history yet." }));
    return wrap;
  }

  const W = 340, H = 170, padL = 42, padR = 8, padT = 12, padB = 22;
  const prices = pts.map((p) => p.price);
  let lo = Math.min(...prices), hi = Math.max(...prices);
  if (product.target_price != null) { lo = Math.min(lo, product.target_price); hi = Math.max(hi, product.target_price); }
  const margin = (hi - lo) * 0.12 || Math.max(hi * 0.05, 1);
  lo -= margin; hi += margin;
  const span = hi - lo || 1;
  const times = pts.map((p) => new Date(p.ts).getTime());
  const t0 = times[0], t1 = times.at(-1), tspan = t1 - t0 || 1;
  const x = (t) => padL + ((t - t0) / tspan) * (W - padL - padR);
  const y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);

  const trend = prices.at(-1) < prices[0] ? "down" : prices.at(-1) > prices[0] ? "up" : "flat";
  const stroke = trend === "down" ? "var(--down)" : trend === "up" ? "var(--up)" : "var(--faint)";
  const line = pts.map((p, i) => `${i ? "L" : "M"}${x(times[i]).toFixed(1)},${y(p.price).toFixed(1)}`).join(" ");
  const area = `M${padL},${H - padB} ` + pts.map((p, i) => `L${x(times[i]).toFixed(1)},${y(p.price).toFixed(1)}`).join(" ") + ` L${(W - padR)},${H - padB} Z`;

  const ticks = [lo + span * 0.08, lo + span / 2, hi - span * 0.08];
  const gridLines = ticks.map((v) =>
    `<line x1="${padL}" x2="${W - padR}" y1="${y(v).toFixed(1)}" y2="${y(v).toFixed(1)}" stroke="var(--line)" stroke-width="1"/>
     <text x="${padL - 6}" y="${(y(v) + 3.5).toFixed(1)}" text-anchor="end" font-size="9" fill="var(--faint)">${money(v, product.currency)}</text>`).join("");

  const targetLine = product.target_price != null
    ? `<line x1="${padL}" x2="${W - padR}" y1="${y(product.target_price).toFixed(1)}" y2="${y(product.target_price).toFixed(1)}"
         stroke="var(--down)" stroke-width="1.2" stroke-dasharray="4 4"/>
       <text x="${W - padR}" y="${(y(product.target_price) - 4).toFixed(1)}" text-anchor="end" font-size="9" fill="var(--down)">target</text>` : "";

  const minI = prices.indexOf(Math.min(...prices));
  const markers =
    `<circle cx="${x(times[minI]).toFixed(1)}" cy="${y(prices[minI]).toFixed(1)}" r="3" fill="var(--down)"/>` +
    `<circle cx="${x(times.at(-1)).toFixed(1)}" cy="${y(prices.at(-1)).toFixed(1)}" r="3.2" fill="${stroke}"/>`;

  const dateLabel = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  const svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="price history">
    ${gridLines}
    <path d="${area}" fill="${stroke}" opacity=".10"/>
    ${targetLine}
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.8" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    ${markers}
    <text x="${padL}" y="${H - 6}" font-size="9" fill="var(--faint)">${dateLabel(t0)}</text>
    <text x="${W - padR}" y="${H - 6}" text-anchor="end" font-size="9" fill="var(--faint)">${dateLabel(t1)}</text>
    <line id="cross" x1="0" x2="0" y1="${padT}" y2="${H - padB}" stroke="var(--line-strong)" stroke-width="1" opacity="0"/>
    <circle id="cursor" r="3.5" fill="var(--panel)" stroke="${stroke}" stroke-width="2" opacity="0"/>
    <rect x="${padL}" y="${padT}" width="${W - padL - padR}" height="${H - padT - padB}" fill="transparent" id="hit"/>
  </svg>`;
  wrap.innerHTML = svg;
  const tip = el("div", { className: "tip" });
  wrap.append(tip);

  const svgNode = wrap.querySelector("svg");
  const cross = wrap.querySelector("#cross"), cursor = wrap.querySelector("#cursor");
  svgNode.addEventListener("pointermove", (ev) => {
    const box = svgNode.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * W;
    let best = 0, bestD = Infinity;
    times.forEach((t, i) => { const d = Math.abs(x(t) - vx); if (d < bestD) { bestD = d; best = i; } });
    const p = pts[best], px = x(times[best]), py = y(p.price);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("opacity", ".9");
    cursor.setAttribute("cx", px); cursor.setAttribute("cy", py); cursor.setAttribute("opacity", "1");
    tip.textContent = `${money(p.price, product.currency)} · ${new Date(p.ts).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" })}`;
    tip.style.opacity = "1";
    tip.style.left = `${Math.min(Math.max((px / W) * box.width - 60, 0), box.width - 130)}px`;
    tip.style.top = `${(py / H) * box.height - 34}px`;
  });
  svgNode.addEventListener("pointerleave", () => {
    tip.style.opacity = "0"; cross.setAttribute("opacity", "0"); cursor.setAttribute("opacity", "0");
  });
  return wrap;
}

/* ---------------- rendering ---------------- */
function visibleProducts() {
  const q = state.query.trim().toLowerCase();
  let list = state.products.filter((p) => {
    if (q && !(`${p.name} ${p.title || ""} ${p.retailer} ${p.url}`.toLowerCase().includes(q))) return false;
    switch (state.filter) {
      case "hit": return p.target_hit;
      case "drops": return (p.change_pct ?? 0) < -0.01;
      case "stock": return p.last_in_stock === false;
      case "issues": return p.fail_count > 0 || p.last_price === null;
      default: return true;
    }
  });
  const by = {
    name: (a, b) => a.name.localeCompare(b.name),
    change: (a, b) => (a.change_pct ?? 0) - (b.change_pct ?? 0),
    price: (a, b) => (a.last_price ?? 1e15) - (b.last_price ?? 1e15),
    checked: (a, b) => (b.last_checked || "").localeCompare(a.last_checked || ""),
  }[state.sort];
  return list.sort(by);
}

function renderSummary() {
  const ps = state.products;
  const tracked = ps.length;
  const hits = ps.filter((p) => p.target_hit).length;
  const dropped = ps.filter((p) => (p.change_pct ?? 0) < -0.01).length;
  const issues = ps.filter((p) => p.fail_count > 0).length;
  const saved = ps.reduce((sum, p) => {
    const pts = series(p);
    return pts.length >= 2 ? sum + Math.max(0, pts[0].price - pts.at(-1).price) : sum;
  }, 0);
  const cur = ps.find((p) => p.currency)?.currency;

  const box = $("#summary");
  box.innerHTML = "";
  const stat = (k, v, cls = "") => {
    const n = el("div", { className: "stat" });
    n.append(el("div", { className: "k", textContent: k }), el("div", { className: `v ${cls}`, textContent: v }));
    return n;
  };
  box.append(
    stat("Tracked", String(tracked)),
    stat("At or below target", String(hits), hits ? "good" : ""),
    stat("Price dropped", String(dropped), dropped ? "good" : ""),
    stat("Total drop", money(saved, cur), saved > 0 ? "good" : ""),
    stat("Needs attention", String(issues), issues ? "bad" : ""),
  );
}

function card(p) {
  const node = el("div", { className: "card" + (state.selected === p.name ? " sel" : "") + (p.active ? "" : " paused") });
  node.onclick = () => { state.selected = p.name; state.tab = "detail"; render(); };

  const delta = p.change_pct;
  const dcls = delta == null ? "flat" : delta < -0.01 ? "down" : delta > 0.01 ? "up" : "flat";
  const arrow = delta == null ? "" : delta < -0.01 ? "▼ " : delta > 0.01 ? "▲ " : "→ ";

  node.append(
    el("div", { className: "retailer", textContent: p.retailer }),
    el("h3", { textContent: p.title || p.name }),
  );
  const row = el("div", { className: "row" });
  row.append(
    el("div", { className: "price", textContent: money(p.last_price, p.currency) }),
    el("div", { className: `delta ${dcls}`, textContent: delta == null ? "" : `${arrow}${Math.abs(delta).toFixed(1)}%` }),
  );
  node.append(row);

  const meta = el("div", { className: "meta" });
  if (p.target_hit) meta.append(el("span", { className: "pill hit", textContent: `🎯 target ${money(p.target_price, p.currency)}` }));
  else if (p.target_price != null) meta.append(el("span", { className: "pill", textContent: `target ${money(p.target_price, p.currency)}` }));
  if (p.last_in_stock === false) meta.append(el("span", { className: "pill oos", textContent: "out of stock" }));
  if (p.fail_count > 0) meta.append(el("span", { className: "pill err", textContent: `${p.fail_count} failed check${p.fail_count > 1 ? "s" : ""}` }));
  if (!p.active) meta.append(el("span", { className: "pill", textContent: "paused" }));
  meta.append(el("span", { className: "pill", textContent: when(p.last_checked) }));
  node.append(meta, sparkline(p));
  return node;
}

function renderGrid() {
  const grid = $("#grid"), list = visibleProducts();
  grid.innerHTML = "";
  $("#empty").hidden = state.products.length !== 0;
  list.forEach((p) => grid.append(card(p)));
  if (state.products.length && !list.length) {
    grid.append(el("p", { className: "hint", textContent: "Nothing matches that filter." }));
  }
}

function detailPanel() {
  const side = $("#side");
  side.innerHTML = "";
  const p = state.products.find((x) => x.name === state.selected);

  const tabs = el("div", { className: "tabs" });
  [["detail", "Product"], ["alerts", "Alerts"], ["sites", "Supported sites"]].forEach(([key, label]) => {
    const b = el("button", { textContent: label, className: state.tab === key ? "on" : "" });
    b.onclick = () => { state.tab = key; detailPanel(); };
    tabs.append(b);
  });
  side.append(tabs);

  if (state.tab === "alerts") return renderAlerts(side);
  if (state.tab === "sites") return renderSites(side);
  if (!p) {
    side.append(el("p", { className: "hint", textContent: "Select a product to see its price history and settings." }));
    if (state.job.log?.length) {
      side.append(el("div", { className: "section" }, [
        el("h4", { textContent: "Last run" }),
        el("div", { className: "log", textContent: state.job.log.join("\n") }),
      ]));
    }
    return;
  }

  side.append(el("h2", { textContent: p.title || p.name }));
  if (p.title && p.title !== p.name) {
    side.append(el("div", { className: "retailer", style: "margin:-2px 0 6px", textContent: `${p.retailer} · ${p.name}` }));
  }
  const link = el("a", { href: p.url, target: "_blank", rel: "noreferrer", textContent: p.url });
  side.append(el("div", { className: "url" }, [link]));
  side.append(bigChart(p));

  const facts = el("div", { className: "section" }, [el("h4", { textContent: "Numbers" })]);
  const kv = (k, v) => el("div", { className: "kv" }, [
    el("span", { className: "k", textContent: k }), el("span", { className: "v", textContent: v })]);
  facts.append(
    kv("Current", money(p.last_price, p.currency)),
    kv("Target", p.target_price == null ? "—" : money(p.target_price, p.currency)),
    kv("All-time low", money(p.low, p.currency)),
    kv("All-time high", money(p.high, p.currency)),
    kv("Average", money(p.avg, p.currency)),
    kv("Change since first check", p.change_pct == null ? "—" : pct(p.change_pct)),
    kv("Checks recorded", String(p.checks)),
    kv("Stock", p.last_in_stock === null ? "unknown" : p.last_in_stock ? "in stock" : "out of stock"),
    kv("Last checked", when(p.last_checked)),
    kv("Reads price via", p.selector || p.learned_selector || "auto-detection"),
  );
  side.append(facts);

  const settings = el("div", { className: "section" }, [el("h4", { textContent: "Settings" })]);
  const target = el("input", { type: "number", step: "0.01", min: "0", value: p.target_price ?? "" });
  const selector = el("input", { type: "text", value: p.selector || "", placeholder: p.learned_selector || "auto-detect" });
  settings.append(el("label", { textContent: "Alert at or below" }), target,
                  el("label", { textContent: "CSS selector (leave blank to auto-detect)" }), selector);

  const actions = el("div", { className: "actions" });
  const save = el("button", { className: "btn primary sm", textContent: "Save" });
  save.onclick = async () => {
    save.disabled = true; save.textContent = "Saving…";
    await api(`/api/products/${encodeURIComponent(p.name)}`, "PATCH", {
      target_price: target.value === "" ? null : target.value,
      selector: selector.value.trim(),
    });
    await refresh();
  };
  const checkOne = el("button", { className: "btn sm", textContent: "Check now" });
  checkOne.onclick = async () => { await api("/api/check", "POST", { names: [p.name] }); poll(); };
  const pause = el("button", { className: "btn sm", textContent: p.active ? "Pause" : "Resume" });
  pause.onclick = async () => {
    await api(`/api/products/${encodeURIComponent(p.name)}`, "PATCH", { active: !p.active });
    await refresh();
  };
  const del = el("button", { className: "btn danger sm", textContent: "Remove" });
  del.onclick = async () => {
    if (!confirm(`Stop tracking "${p.name}"? Its price history is deleted too.`)) return;
    await api(`/api/products/${encodeURIComponent(p.name)}`, "DELETE");
    state.selected = null; await refresh();
  };
  actions.append(save, checkOne, pause, del);
  settings.append(actions);
  side.append(settings);

  const mine = state.alerts.filter((a) => a.product === p.name).slice(0, 6);
  if (mine.length) {
    const box = el("div", { className: "section" }, [el("h4", { textContent: "Recent alerts" })]);
    const list = el("div", { className: "alertlist" });
    mine.forEach((a) => list.append(alertRow(a, false)));
    box.append(list); side.append(box);
  }
}

const ICONS = { target_hit: "🎯", price_drop: "📉", price_rise: "📈", back_in_stock: "📦", out_of_stock: "🚫", error: "⚠️" };
function alertRow(a, showProduct = true) {
  const node = el("div", { className: "alert" });
  node.append(el("span", { textContent: ICONS[a.kind] || "•" }));
  const body = el("div");
  body.append(el("div", { textContent: showProduct ? a.message : a.message.replace(`${a.product} `, "") }));
  body.append(el("div", { className: "when", textContent: when(a.ts) }));
  node.append(body);
  return node;
}

function renderAlerts(side) {
  if (!state.alerts.length) {
    side.append(el("p", { className: "hint", textContent: "No alerts yet. They appear when a price crosses your target, drops sharply, or an item comes back in stock." }));
    return;
  }
  const list = el("div", { className: "alertlist" });
  state.alerts.forEach((a) => list.append(alertRow(a)));
  side.append(list);
}

function renderSites(side) {
  side.append(el("p", { className: "hint", textContent:
    "These retailers have built-in extraction rules. Any other shop still works through schema.org data, embedded JSON, heuristics and Claude." }));
  const list = el("div", { className: "alertlist" });
  state.sites.forEach((s) => {
    const row = el("div", { className: "alert" });
    row.append(el("span", { textContent: s.protection === "high" ? "🛡️" : s.protection === "medium" ? "◐" : "✓" }));
    const body = el("div");
    body.append(el("div", { textContent: s.name }));
    body.append(el("div", { className: "when", textContent: s.domain + (s.protection === "high" ? " · blocks bots, may need browser mode" : "") }));
    row.append(body); list.append(row);
  });
  side.append(list);
}

function renderStatus() {
  const s = $("#status"), job = state.job || {};
  s.innerHTML = "";
  if (job.busy) {
    s.append(el("span", { className: "spin" }),
             el("span", { textContent: `Checking ${job.done}/${job.total}${job.current ? " — " + job.current : ""}` }));
    $("#btn-check").disabled = true;
  } else {
    $("#btn-check").disabled = false;
    const bits = [];
    if (job.last_summary) bits.push(job.last_summary);
    if (state.llm) bits.push(`AI: ${state.llm}`);
    s.append(el("span", { textContent: bits.join("  ·  ") }));
  }
}

function render() { renderSummary(); renderGrid(); detailPanel(); renderStatus(); }

/* ---------------- data ---------------- */
async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw Object.assign(new Error(data.error || res.statusText), { data });
  return data;
}

async function refresh() {
  const data = await api("/api/state");
  Object.assign(state, data);
  if (state.selected && !state.products.some((p) => p.name === state.selected)) state.selected = null;
  render();
}

let pollTimer = null;
function poll() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    const job = await api("/api/job");
    const wasBusy = state.job.busy;
    state.job = job;
    renderStatus();
    if (wasBusy && !job.busy) { await refresh(); clearInterval(pollTimer); }
    if (!wasBusy && !job.busy) clearInterval(pollTimer);
  }, 900);
}

/* ---------------- wiring ---------------- */
$("#btn-check").onclick = async () => { await api("/api/check", "POST", {}); state.job.busy = true; renderStatus(); poll(); };
$("#search").oninput = (e) => { state.query = e.target.value; renderGrid(); };
$("#sort").onchange = (e) => { state.sort = e.target.value; renderGrid(); };
$("#filters").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.filter = b.dataset.filter;
  [...$("#filters").children].forEach((c) => c.classList.toggle("on", c === b));
  renderGrid();
};

/* ---------------- add dialog: search by name or paste a URL ---------------- */
let addMode = "name";
let chosen = null;   // the search result the user picked

function setAddMode(mode) {
  addMode = mode;
  $("#mode-name").hidden = mode !== "name";
  $("#mode-url").hidden = mode !== "url";
  [...$("#add-mode").children].forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
}

function resultRow(r) {
  const row = el("div", { className: "alert", style: "cursor:pointer" });
  if (chosen && chosen.url === r.url) row.style.borderColor = "var(--accent)";
  row.onclick = () => { chosen = r; renderResults(window.__results || []); };
  row.append(el("span", { textContent: chosen && chosen.url === r.url ? "◉" : "○" }));
  const body = el("div", { style: "flex:1" });
  const head = el("div", { style: "display:flex;justify-content:space-between;gap:10px" });
  head.append(
    el("strong", { textContent: r.price == null ? "no price" : money(r.price, r.currency) }),
    el("span", { style: "color:var(--dim)", textContent: r.retailer }),
  );
  body.append(head, el("div", { textContent: r.title.slice(0, 92) }));
  if (r.note) body.append(el("div", { className: "when", textContent: r.note }));
  else if (r.in_stock === false) body.append(el("div", { className: "when", textContent: "out of stock" }));
  row.append(body);
  return row;
}

function renderResults(results) {
  window.__results = results;
  const box = $("#results");
  box.innerHTML = "";
  if (!results.length) {
    box.append(el("div", { className: "hint", textContent: "Nothing found — try the brand and model, e.g. \"logitech mx master 3s\"." }));
    return;
  }
  results.forEach((r) => box.append(resultRow(r)));
}

$("#add-mode").onclick = (e) => { const b = e.target.closest("button"); if (b) setAddMode(b.dataset.mode); };

$("#btn-search").onclick = async () => {
  const q = $("#f-query").value.trim();
  if (q.length < 2) return;
  const btn = $("#btn-search"), err = $("#add-err");
  err.hidden = true; btn.disabled = true; btn.textContent = "Searching…";
  $("#results").innerHTML = '<div class="hint">Searching the web and reading prices — this takes a few seconds…</div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    chosen = data.results.find((r) => r.price != null) || data.results[0] || null;
    renderResults(data.results);
    if (chosen && !$("#f-name").value) $("#f-name").value = "";
  } catch (e) {
    $("#results").innerHTML = "";
    err.textContent = e.message; err.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "Search";
  }
};

const dlg = $("#dlg-add");
const openAdd = () => {
  $("#add-err").hidden = true;
  $("#form-add").reset();
  chosen = null; $("#results").innerHTML = "";
  setAddMode("name");
  dlg.showModal();
  $("#f-query").focus();
};
$("#btn-add").onclick = openAdd;
$("#btn-add-empty").onclick = openAdd;

$("#form-add").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const btn = $("#btn-add-go"), err = $("#add-err");
  const url = addMode === "name" ? (chosen && chosen.url) : $("#f-url").value.trim();
  if (!url) {
    err.textContent = addMode === "name"
      ? "Search for the product first, then pick one of the results."
      : "Paste a product URL.";
    err.hidden = false;
    return;
  }
  err.hidden = true; btn.disabled = true; btn.textContent = "Fetching page…";
  try {
    await api("/api/products", "POST", {
      url,
      name: $("#f-name").value.trim(),
      target_price: $("#f-target").value === "" ? null : $("#f-target").value,
      selector: addMode === "url" ? $("#f-selector").value.trim() : "",
    });
    dlg.close();
    await refresh();
  } catch (e) {
    err.textContent = e.message + (e.data?.hint ? ` — ${e.data.hint}` : "");
    err.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "Track it";
  }
});

const THEME_KEY = "pricemon.theme";
function applyTheme(mode) {
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem(THEME_KEY, mode); } catch { /* private mode */ }
}
$("#btn-theme").onclick = () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
try {
  const saved = localStorage.getItem(THEME_KEY);
  applyTheme(saved || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"));
} catch { applyTheme("light"); }

document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement !== $("#search") && !dlg.open) { e.preventDefault(); $("#search").focus(); }
  if (e.key === "Escape" && !dlg.open) { state.selected = null; render(); }
  if ((e.metaKey || e.ctrlKey) && e.key === "n") { e.preventDefault(); openAdd(); }
});

refresh().then(() => { if (state.job.busy) poll(); });
setInterval(() => { if (!state.job.busy) refresh(); }, 20000);
