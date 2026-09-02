/* Price Monitor — desktop UI.
   No framework, no CDN: this has to start on a machine with nothing installed
   but Python. Charts and gauges are hand-drawn SVG/CSS. */

const state = {
  products: [], alerts: [], job: {}, llm: "", sites: [],
  selected: null, filter: "all", sort: "change", query: "", tab: "detail",
};

const $ = (sel) => document.querySelector(sel);
const el = (tag, props = {}, kids = []) => {
  const node = Object.assign(document.createElement(tag), props);
  for (const k of [].concat(kids)) if (k != null) node.append(k);
  return node;
};

/* ---------------- formatting ---------------- */
const SYMBOLS = { USD: "$", EUR: "€", GBP: "£", JPY: "¥", INR: "₹", CAD: "$", AUD: "$" };
function money(amount, currency, decimals = 2) {
  if (amount === null || amount === undefined) return "—";
  const n = amount.toLocaleString(undefined, { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
  const sym = SYMBOLS[currency];
  return sym ? `${sym}${n}` : currency ? `${n} ${currency}` : n;
}
function when(iso) {
  if (!iso) return "never";
  const mins = (Date.now() - new Date(iso)) / 60000;
  if (mins < 1) return "just now";
  if (mins < 60) return `${Math.round(mins)}m ago`;
  if (mins < 48 * 60) return `${Math.round(mins / 60)}h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}
const series = (p) => p.history.filter((h) => h.price !== null);

/* ---------------- the gauge: how close is this to being worth buying ---------------- */
function gauge(p) {
  const box = el("div", { className: "gauge" + (p.target_hit ? " hit" : "") });
  if (p.last_price == null || p.low == null || p.high == null) {
    box.append(el("div", { className: "none", textContent: "no data" }));
    return box;
  }
  let lo = p.low, hi = p.high;
  if (p.target_price != null) { lo = Math.min(lo, p.target_price); hi = Math.max(hi, p.target_price); }
  const span = hi - lo;

  if (span <= 0) {
    // One price, seen once: a full-width bar would imply a range that does not
    // exist yet. Say so instead.
    box.append(el("div", { className: "track" }),
               el("div", { className: "dot", style: "left:50%" }),
               el("div", { className: "flat", textContent: "no range yet" }));
    return box;
  }

  const at = (v) => ((v - lo) / span) * 100;
  const pos = at(p.last_price);
  box.title = p.target_price != null
    ? `Now ${money(p.last_price, p.currency)} · target ${money(p.target_price, p.currency)} · seen between ${money(lo, p.currency)} and ${money(hi, p.currency)}`
    : `Now ${money(p.last_price, p.currency)} · seen between ${money(lo, p.currency)} and ${money(hi, p.currency)}`;

  box.append(el("div", { className: "track" }));
  box.append(el("div", { className: "fill", style: `width:${pos.toFixed(1)}%` }));
  if (p.target_price != null) {
    box.append(el("div", { className: "notch", style: `left:${at(p.target_price).toFixed(1)}%`, title: `target ${money(p.target_price, p.currency)}` }));
  }
  box.append(el("div", { className: "dot", style: `left:${pos.toFixed(1)}%` }));
  const ends = el("div", { className: "ends" });
  ends.append(el("span", { textContent: money(lo, p.currency, 0) }), el("span", { textContent: money(hi, p.currency, 0) }));
  box.append(ends);
  return box;
}

/* ---------------- charts ---------------- */
function sparkline(p, w = 96, h = 30) {
  const pts = series(p);
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("class", "cell-spark");
  svg.setAttribute("aria-hidden", "true");
  if (pts.length < 2) return svg;

  const prices = pts.map((x) => x.price);
  let lo = Math.min(...prices), hi = Math.max(...prices);
  const margin = (hi - lo) * 0.28 || Math.max(hi * 0.03, 0.5);
  lo -= margin; hi += margin;
  const span = hi - lo || 1, pad = 4;
  const X = (i) => (i / (prices.length - 1)) * w;
  const Y = (v) => h - pad - ((v - lo) / span) * (h - 2 * pad);
  const trend = prices.at(-1) < prices[0] ? "drop" : prices.at(-1) > prices[0] ? "rise" : "ink-3";
  const stroke = `var(--${trend})`;
  svg.innerHTML =
    `<path d="${prices.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join(" ")}"
       fill="none" stroke="${stroke}" stroke-width="1.5" stroke-linejoin="round"
       vector-effect="non-scaling-stroke"/>` +
    `<circle cx="${X(prices.length - 1).toFixed(1)}" cy="${Y(prices.at(-1)).toFixed(1)}" r="1.9" fill="${stroke}"/>`;
  return svg;
}

function bigChart(p) {
  const pts = series(p);
  const wrap = el("div", { className: "chart-wrap" });
  if (pts.length < 2) {
    wrap.append(el("p", { className: "hint", textContent:
      pts.length === 1 ? "One reading so far. The line starts after the next check." : "No price readings yet." }));
    return wrap;
  }
  const W = 336, H = 150, padL = 44, padR = 10, padT = 12, padB = 20;
  const prices = pts.map((x) => x.price);
  let lo = Math.min(...prices), hi = Math.max(...prices);
  if (p.target_price != null) { lo = Math.min(lo, p.target_price); hi = Math.max(hi, p.target_price); }
  const margin = (hi - lo) * 0.14 || Math.max(hi * 0.04, 1);
  lo -= margin; hi += margin;
  const span = hi - lo || 1;
  const times = pts.map((x) => new Date(x.ts).getTime());
  const t0 = times[0], tspan = (times.at(-1) - t0) || 1;
  const X = (t) => padL + ((t - t0) / tspan) * (W - padL - padR);
  const Y = (v) => padT + (1 - (v - lo) / span) * (H - padT - padB);
  const trend = prices.at(-1) < prices[0] ? "drop" : prices.at(-1) > prices[0] ? "rise" : "ink-3";
  const stroke = `var(--${trend})`;

  const grid = [lo + span * 0.1, lo + span * 0.5, hi - span * 0.1].map((v) =>
    `<line x1="${padL}" x2="${W - padR}" y1="${Y(v).toFixed(1)}" y2="${Y(v).toFixed(1)}" stroke="var(--rule)"/>
     <text x="${padL - 7}" y="${(Y(v) + 3).toFixed(1)}" text-anchor="end" font-size="8.5"
       font-family="var(--mono)" fill="var(--ink-3)">${money(v, p.currency, 0)}</text>`).join("");

  const target = p.target_price == null ? "" :
    `<line x1="${padL}" x2="${W - padR}" y1="${Y(p.target_price).toFixed(1)}" y2="${Y(p.target_price).toFixed(1)}"
       stroke="var(--drop)" stroke-dasharray="3 3"/>
     <text x="${W - padR}" y="${(Y(p.target_price) - 4).toFixed(1)}" text-anchor="end" font-size="8.5"
       font-family="var(--mono)" fill="var(--drop)">TARGET</text>`;

  const line = pts.map((x, i) => `${i ? "L" : "M"}${X(times[i]).toFixed(1)},${Y(x.price).toFixed(1)}`).join(" ");
  const day = (t) => new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });

  wrap.innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Price history">
    ${grid}${target}
    <path d="${line}" fill="none" stroke="${stroke}" stroke-width="1.6"
      stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    <circle cx="${X(times.at(-1)).toFixed(1)}" cy="${Y(prices.at(-1)).toFixed(1)}" r="2.6" fill="${stroke}"/>
    <text x="${padL}" y="${H - 5}" font-size="8.5" font-family="var(--mono)" fill="var(--ink-3)">${day(t0)}</text>
    <text x="${W - padR}" y="${H - 5}" text-anchor="end" font-size="8.5" font-family="var(--mono)"
      fill="var(--ink-3)">${day(times.at(-1))}</text>
    <line id="cross" y1="${padT}" y2="${H - padB}" stroke="var(--rule-2)" opacity="0"/>
    <circle id="cursor" r="3" fill="var(--surface)" stroke="${stroke}" stroke-width="1.6" opacity="0"/>
  </svg>`;
  const tip = el("div", { className: "tip" });
  wrap.append(tip);

  const svg = wrap.querySelector("svg"), cross = wrap.querySelector("#cross"), cursor = wrap.querySelector("#cursor");
  svg.addEventListener("pointermove", (ev) => {
    const box = svg.getBoundingClientRect();
    const vx = ((ev.clientX - box.left) / box.width) * W;
    let best = 0, bestD = Infinity;
    times.forEach((t, i) => { const d = Math.abs(X(t) - vx); if (d < bestD) { bestD = d; best = i; } });
    const px = X(times[best]), py = Y(pts[best].price);
    cross.setAttribute("x1", px); cross.setAttribute("x2", px); cross.setAttribute("opacity", "1");
    cursor.setAttribute("cx", px); cursor.setAttribute("cy", py); cursor.setAttribute("opacity", "1");
    tip.textContent = `${money(pts[best].price, p.currency)}  ${new Date(pts[best].ts).toLocaleDateString(undefined, { month: "short", day: "numeric" })}`;
    tip.style.opacity = "1";
    tip.style.left = `${Math.min(Math.max((px / W) * box.width - 52, 0), box.width - 108)}px`;
    tip.style.top = `${(py / H) * box.height - 30}px`;
  });
  svg.addEventListener("pointerleave", () => {
    tip.style.opacity = "0"; cross.setAttribute("opacity", "0"); cursor.setAttribute("opacity", "0");
  });
  return wrap;
}

/* ---------------- ledger ---------------- */
function visible() {
  const q = state.query.trim().toLowerCase();
  const list = state.products.filter((p) => {
    if (q && !`${p.name} ${p.title || ""} ${p.retailer} ${p.url}`.toLowerCase().includes(q)) return false;
    if (state.filter === "hit") return p.target_hit;
    if (state.filter === "drops") return (p.change_pct ?? 0) < -0.01;
    if (state.filter === "stock") return p.last_in_stock === false;
    if (state.filter === "issues") return p.fail_count > 0 || p.last_price === null;
    return true;
  });
  const by = {
    name: (a, b) => (a.title || a.name).localeCompare(b.title || b.name),
    change: (a, b) => (a.change_pct ?? 0) - (b.change_pct ?? 0),
    price: (a, b) => (a.last_price ?? 1e15) - (b.last_price ?? 1e15),
    checked: (a, b) => (b.last_checked || "").localeCompare(a.last_checked || ""),
  }[state.sort];
  return list.sort(by);
}

function thumb(p) {
  if (p.image) {
    const img = el("img", { className: "thumb", src: p.image, alt: "", loading: "lazy" });
    img.onerror = () => img.replaceWith(monogram(p));
    return img;
  }
  return monogram(p);
}
const monogram = (p) => el("div", { className: "thumb-fallback", textContent: (p.title || p.name).trim()[0].toUpperCase() });

function row(p) {
  const node = el("div", {
    className: "row" + (state.selected === p.name ? " sel" : "") + (p.target_hit ? " hit" : "") + (p.active ? "" : " paused"),
    tabIndex: 0, role: "button",
  });
  const open = () => { state.selected = p.name; state.tab = "detail"; render(); };
  node.onclick = open;
  node.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };

  const who = el("div", { className: "who" });
  who.append(el("div", { className: "name", textContent: p.title || p.name, title: p.title || p.name }));
  const sub = el("div", { className: "sub" });
  sub.append(el("span", { textContent: p.retailer }), el("span", { textContent: when(p.last_checked) }));
  if (p.last_in_stock === false) sub.append(el("span", { className: "tag oos", textContent: "out of stock" }));
  if (p.target_hit) sub.append(el("span", { className: "tag hit", textContent: "at target" }));
  if (p.fail_count > 0) sub.append(el("span", { className: "tag", textContent: `${p.fail_count} failed` }));
  if (!p.active) sub.append(el("span", { className: "tag", textContent: "paused" }));
  who.append(sub);

  const price = el("div", { className: "cell-price" });
  price.append(el("div", { className: "now", textContent: money(p.last_price, p.currency) }));
  const d = p.change_pct;
  const dcls = d == null ? "" : d < -0.01 ? "down" : d > 0.01 ? "up" : "";
  const dtxt = d == null ? "no history" : d < -0.01 ? `▼ ${Math.abs(d).toFixed(1)}%` : d > 0.01 ? `▲ ${d.toFixed(1)}%` : "unchanged";
  price.append(el("div", { className: `chg ${dcls}`, textContent: dtxt }));

  node.append(thumb(p), who, price, el("div", { className: "cell-gauge" }, [gauge(p)]), el("div", { className: "cell-spark" }, [sparkline(p)]));
  return node;
}

function renderLedger() {
  const box = $("#rows"), list = visible();
  box.innerHTML = "";
  $("#blank").hidden = state.products.length !== 0;
  $("#ledger-box").hidden = state.products.length === 0;
  list.forEach((p) => box.append(row(p)));
  if (state.products.length && !list.length) {
    box.append(el("div", { style: "padding:26px; text-align:center; color:var(--ink-3); font-size:13px",
                           textContent: "Nothing matches that filter." }));
  }
}

/* One sentence, not a wall of tiles. */
function renderHeadline() {
  const box = $("#headline"), ps = state.products, job = state.job || {};
  box.innerHTML = "";
  if (job.busy) {
    box.append(el("span", { className: "working" }),
               el("span", { textContent: ` Checking ${job.done}/${job.total}${job.current ? " · " + job.current : ""}` }));
    $("#btn-check").disabled = true;
    return;
  }
  $("#btn-check").disabled = false;
  if (!ps.length) { box.append(el("span", { textContent: state.llm ? `AI: ${state.llm}` : "" })); return; }

  const hits = ps.filter((p) => p.target_hit).length;
  const fell = ps.filter((p) => (p.change_pct ?? 0) < -0.01).length;
  const bad = ps.filter((p) => p.fail_count > 0).length;
  box.append(el("b", { textContent: `${ps.length} tracked` }));
  if (hits) box.append(el("span", { textContent: " · " }), el("span", { className: "lede", textContent: `${hits} at or below target` }));
  else if (fell) box.append(el("span", { textContent: ` · ${fell} cheaper than when you started` }));
  else box.append(el("span", { textContent: " · nothing below target yet" }));
  if (bad) box.append(el("span", { textContent: ` · ${bad} need${bad > 1 ? "" : "s"} a look` }));
}

/* ---------------- detail drawer ---------------- */
function drawer() {
  const side = $("#side");
  side.innerHTML = "";
  side.classList.remove("empty-state");
  const p = state.products.find((x) => x.name === state.selected);

  const tabs = el("div", { className: "tabs" });
  [["detail", "Product"], ["alerts", "Activity"], ["sites", "Shops"]].forEach(([key, label]) => {
    const b = el("button", { textContent: label, className: state.tab === key ? "on" : "" });
    b.onclick = () => { state.tab = key; drawer(); };
    tabs.append(b);
  });
  side.append(tabs);

  if (state.tab === "alerts") return renderFeed(side);
  if (state.tab === "sites") return renderShops(side);
  if (!p) {
    side.append(el("p", { className: "hint", textContent: "Pick a row to see its history, and to change what it alerts on." }));
    if (state.job.log?.length) {
      side.append(el("div", { className: "block" }, [
        el("h4", { textContent: "Last run" }), el("div", { className: "log", textContent: state.job.log.join("\n") }),
      ]));
    }
    return;
  }

  side.append(el("h2", { textContent: p.title || p.name }));
  side.append(el("a", { className: "url", href: p.url, target: "_blank", rel: "noreferrer", textContent: p.url }));
  side.append(bigChart(p));

  const kv = (k, v) => el("div", { className: "kv" }, [
    el("span", { className: "k", textContent: k }), el("span", { className: "v", textContent: v })]);
  side.append(el("div", { className: "block" }, [
    el("h4", { textContent: "Numbers" }),
    kv("Now", money(p.last_price, p.currency)),
    kv("Target", p.target_price == null ? "—" : money(p.target_price, p.currency)),
    kv("Lowest seen", money(p.low, p.currency)),
    kv("Highest seen", money(p.high, p.currency)),
    kv("Average", money(p.avg, p.currency)),
    kv("Since first check", p.change_pct == null ? "—" : `${p.change_pct > 0 ? "+" : ""}${p.change_pct.toFixed(1)}%`),
    kv("Readings", String(p.checks)),
    kv("Stock", p.last_in_stock === null ? "unknown" : p.last_in_stock ? "in stock" : "out of stock"),
    kv("Checked", when(p.last_checked)),
    kv("Price read from", p.selector || p.learned_selector || "auto-detection"),
  ]));

  const settings = el("div", { className: "block" }, [el("h4", { textContent: "Settings" })]);
  const target = el("input", { type: "number", step: "0.01", min: "0", value: p.target_price ?? "" });
  const selector = el("input", { type: "text", value: p.selector || "", placeholder: p.learned_selector || "auto-detect" });
  settings.append(el("label", { textContent: "Tell me at or below" }), target,
                  el("label", { textContent: "CSS selector" }), selector);

  const save = el("button", { className: "btn primary sm", textContent: "Save" });
  save.onclick = async () => {
    save.disabled = true; save.textContent = "Saving";
    await api(`/api/products/${encodeURIComponent(p.name)}`, "PATCH",
              { target_price: target.value === "" ? null : target.value, selector: selector.value.trim() });
    await refresh();
  };
  const now = el("button", { className: "btn sm", textContent: "Check now" });
  now.onclick = async () => { await api("/api/check", "POST", { names: [p.name] }); state.job.busy = true; renderHeadline(); poll(); };
  const pause = el("button", { className: "btn sm", textContent: p.active ? "Pause" : "Resume" });
  pause.onclick = async () => { await api(`/api/products/${encodeURIComponent(p.name)}`, "PATCH", { active: !p.active }); await refresh(); };
  const drop = el("button", { className: "btn danger sm", textContent: "Stop tracking" });
  drop.onclick = async () => {
    if (!confirm(`Stop tracking "${p.title || p.name}"? Its price history goes too.`)) return;
    await api(`/api/products/${encodeURIComponent(p.name)}`, "DELETE");
    state.selected = null; await refresh();
  };
  settings.append(el("div", { className: "actions" }, [save, now, pause, drop]));
  side.append(settings);

  const mine = state.alerts.filter((a) => a.product === p.name).slice(0, 6);
  if (mine.length) {
    side.append(el("div", { className: "block" }, [
      el("h4", { textContent: "Recent activity" }),
      el("div", { className: "feed" }, mine.map((a) => feedItem(a, false))),
    ]));
  }
}

const MARKS = { target_hit: "◆", price_drop: "▼", price_rise: "▲", back_in_stock: "◇", out_of_stock: "○", error: "!" };
function feedItem(a, showProduct = true) {
  const node = el("div", { className: "feed-item" });
  node.append(el("span", { className: "num", style: `color:var(--${a.kind === "price_rise" || a.kind === "error" ? "rise" : "drop"})`,
                           textContent: MARKS[a.kind] || "·" }));
  node.append(el("div", {}, [
    el("div", { textContent: showProduct ? a.message : a.message.replace(`${a.product} `, "") }),
    el("div", { className: "when", textContent: when(a.ts) }),
  ]));
  return node;
}

function renderFeed(side) {
  if (!state.alerts.length) {
    side.append(el("p", { className: "hint", textContent:
      "Nothing yet. Activity appears when a price crosses your target, falls sharply, or something comes back in stock." }));
    return;
  }
  side.append(el("div", { className: "feed" }, state.alerts.map((a) => feedItem(a))));
}

function renderShops(side) {
  side.append(el("p", { className: "hint", textContent:
    "These shops have built-in rules. Anywhere else still works through schema.org data, embedded JSON, page heuristics and Claude." }));
  const feed = el("div", { className: "feed" });
  state.sites.forEach((s) => {
    feed.append(el("div", { className: "feed-item" }, [
      el("span", { className: "num", style: "color:var(--ink-3)", textContent: s.protection === "high" ? "◐" : "●" }),
      el("div", {}, [
        el("div", { textContent: s.name }),
        el("div", { className: "when", textContent: s.domain + (s.protection === "high" ? " · needs browser mode" : "") }),
      ]),
    ]));
  });
  side.append(feed);
}

function render() { renderHeadline(); renderLedger(); drawer(); }

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
  Object.assign(state, await api("/api/state"));
  if (state.selected && !state.products.some((p) => p.name === state.selected)) state.selected = null;
  render();
}

let timer = null;
function poll() {
  clearInterval(timer);
  timer = setInterval(async () => {
    const job = await api("/api/job");
    const was = state.job.busy;
    state.job = job;
    renderHeadline();
    if (was && !job.busy) { await refresh(); clearInterval(timer); }
    if (!was && !job.busy) clearInterval(timer);
  }, 900);
}

/* ---------------- add dialog ---------------- */
let addMode = "name", picked = null, results = [];

function setMode(mode) {
  addMode = mode;
  $("#mode-name").hidden = mode !== "name";
  $("#mode-url").hidden = mode !== "url";
  [...$("#add-mode").children].forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
}

function renderResults() {
  const box = $("#results");
  box.hidden = !results.length;
  box.innerHTML = "";
  results.forEach((r) => {
    const node = el("div", { className: "result" + (picked && picked.url === r.url ? " on" : "") });
    node.onclick = () => { picked = r; renderResults(); };
    node.append(el("div", { className: "p", textContent: r.price == null ? "—" : money(r.price, r.currency) }));
    node.append(el("div", { className: "t", style: "flex:1" }, [
      el("div", { className: "t", textContent: r.title.slice(0, 76) }),
      el("div", { className: "r", textContent: r.retailer + (r.note ? ` · ${r.note.slice(0, 40)}` : r.in_stock === false ? " · out of stock" : "") }),
    ]));
    box.append(node);
  });
}

$("#add-mode").onclick = (e) => { const b = e.target.closest("button"); if (b) setMode(b.dataset.mode); };
$("#btn-search").onclick = async () => {
  const q = $("#f-query").value.trim();
  if (q.length < 2) return;
  const btn = $("#btn-search"), err = $("#add-err");
  err.hidden = true; btn.disabled = true; btn.textContent = "Searching";
  $("#results").hidden = false;
  $("#results").innerHTML = '<div style="padding:14px;color:var(--ink-3);font-size:12.5px">Reading prices from each candidate…</div>';
  try {
    const data = await api(`/api/search?q=${encodeURIComponent(q)}`);
    results = data.results;
    picked = results.find((r) => r.price != null) || results[0] || null;
    renderResults();
    if (!results.length) $("#results").innerHTML = '<div style="padding:14px;color:var(--ink-3);font-size:12.5px">Nothing found. Try the brand and model.</div>';
  } catch (e) {
    $("#results").hidden = true;
    err.textContent = e.message; err.hidden = false;
  } finally { btn.disabled = false; btn.textContent = "Search"; }
};

const dlg = $("#dlg-add");
const openAdd = () => {
  $("#form-add").reset(); $("#add-err").hidden = true;
  picked = null; results = []; renderResults();
  setMode("name"); dlg.showModal(); $("#f-query").focus();
};
$("#btn-add").onclick = openAdd;
$("#btn-add-blank").onclick = openAdd;

$("#form-add").addEventListener("submit", async (ev) => {
  if (ev.submitter && ev.submitter.value === "cancel") return;
  ev.preventDefault();
  const btn = $("#btn-add-go"), err = $("#add-err");
  const url = addMode === "name" ? picked && picked.url : $("#f-url").value.trim();
  if (!url) {
    err.textContent = addMode === "name" ? "Search first, then choose one of the results." : "Paste a product link.";
    err.hidden = false; return;
  }
  err.hidden = true; btn.disabled = true; btn.textContent = "Reading the page";
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
  } finally { btn.disabled = false; btn.textContent = "Start tracking"; }
});

/* ---------------- chrome ---------------- */
$("#btn-check").onclick = async () => { await api("/api/check", "POST", {}); state.job.busy = true; renderHeadline(); poll(); };
$("#search").oninput = (e) => { state.query = e.target.value; renderLedger(); };
$("#sort").onchange = (e) => { state.sort = e.target.value; renderLedger(); };
$("#filters").onclick = (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.filter = b.dataset.filter;
  [...$("#filters").children].forEach((c) => c.classList.toggle("on", c === b));
  renderLedger();
};

const THEME = "pricemon.theme";
const setTheme = (mode) => {
  document.documentElement.dataset.theme = mode;
  try { localStorage.setItem(THEME, mode); } catch { /* storage may be blocked */ }
};
$("#btn-theme").onclick = () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
try { setTheme(localStorage.getItem(THEME) || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light")); }
catch { setTheme("light"); }

document.addEventListener("keydown", (e) => {
  if (dlg.open) return;
  if (e.key === "/" && document.activeElement !== $("#search")) { e.preventDefault(); $("#search").focus(); }
  if (e.key === "Escape") { state.selected = null; render(); }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "n") { e.preventDefault(); openAdd(); }
});

refresh().then(() => { if (state.job.busy) poll(); });
setInterval(() => { if (!state.job.busy && !dlg.open) refresh(); }, 20000);
