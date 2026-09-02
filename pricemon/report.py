"""A self-contained HTML dashboard of everything being watched."""

from __future__ import annotations

import html
from datetime import datetime, timezone
from pathlib import Path

from .money import format_price
from .storage import Store

CSS = """
:root{--bg:#fbfaf8;--card:#fff;--ink:#1a1a19;--dim:#6b6a66;--line:#e6e3dd;
--up:#c0442e;--down:#2f7a4f;--accent:#3a5ba0}
@media (prefers-color-scheme:dark){:root{--bg:#16161a;--card:#1e1e23;--ink:#eceae6;
--dim:#9a978f;--line:#2e2e35;--up:#e2705c;--down:#5fbd8a;--accent:#8fa9e8}}
*{box-sizing:border-box}
body{margin:0;padding:32px 20px 64px;background:var(--bg);color:var(--ink);
font:15px/1.55 ui-sans-serif,-apple-system,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:940px;margin:0 auto}
h1{font-size:26px;letter-spacing:-.02em;margin:0 0 4px}
.sub{color:var(--dim);font-size:13px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:18px 20px;margin-bottom:14px}
.head{display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap}
.name{font-weight:650;font-size:16px;letter-spacing:-.01em}
.name a{color:inherit;text-decoration:none;border-bottom:1px solid var(--line)}
.price{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.meta{color:var(--dim);font-size:12.5px;margin-top:6px;display:flex;gap:14px;flex-wrap:wrap}
.tag{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11.5px;
border:1px solid var(--line)}
.hit{color:var(--down);border-color:var(--down)}
.oos{color:var(--up);border-color:var(--up)}
svg{display:block;width:100%;height:64px;margin-top:12px;overflow:visible}
.empty{color:var(--dim);padding:40px 0;text-align:center}
"""


def _sparkline(points: list[float], target: float | None) -> str:
    if len(points) < 2:
        return ""
    w, h, pad = 900, 60, 4
    lo, hi = min(points), max(points)
    if target is not None:
        lo, hi = min(lo, target), max(hi, target)
    span = (hi - lo) or 1
    step = w / (len(points) - 1)
    coords = [
        (i * step, h - pad - (p - lo) / span * (h - 2 * pad))
        for i, p in enumerate(points)
    ]
    path = " ".join(
        f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(coords)
    )
    area = (
        f"M0,{h} " + " ".join(f"L{x:.1f},{y:.1f}" for x, y in coords) + f" L{w},{h} Z"
    )
    trend = "down" if points[-1] < points[0] else "up"
    tline = ""
    if target is not None:
        ty = h - pad - (target - lo) / span * (h - 2 * pad)
        tline = (
            f'<line x1="0" y1="{ty:.1f}" x2="{w}" y2="{ty:.1f}" stroke="var(--down)" '
            f'stroke-width="1" stroke-dasharray="4 4" opacity=".7"/>'
        )
    cx, cy = coords[-1]
    return (
        f'<svg viewBox="0 0 {w} {h}" preserveAspectRatio="none">'
        f'<path d="{area}" fill="var(--{trend})" opacity=".08"/>'
        f'{tline}<path d="{path}" fill="none" stroke="var(--{trend})" stroke-width="2" '
        f'stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'
        f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="3" fill="var(--{trend})"/></svg>'
    )


def build_report(store: Store, out_path: Path) -> Path:
    products = store.list_products()
    cards = []
    for p in products:
        rows = store.history(p, limit=200)
        points = [r["price"] for r in rows if r["price"] is not None]
        stats = store.price_stats(p)
        hit = (
            p.target_price is not None
            and p.last_price is not None
            and p.last_price <= p.target_price
        )

        tags = []
        if hit:
            tags.append('<span class="tag hit">target hit</span>')
        if p.last_in_stock is False:
            tags.append('<span class="tag oos">out of stock</span>')
        if not p.active:
            tags.append('<span class="tag">paused</span>')

        change = ""
        if len(points) >= 2 and points[0]:
            delta = (points[-1] - points[0]) / points[0] * 100
            arrow = "▼" if delta < 0 else ("▲" if delta > 0 else "→")
            change = f"{arrow} {abs(delta):.1f}% since first check"

        meta = [
            m
            for m in [
                change,
                f"low {format_price(stats['lo'], p.currency)}"
                if stats and stats["n"]
                else "",
                f"high {format_price(stats['hi'], p.currency)}"
                if stats and stats["n"]
                else "",
                f"target {format_price(p.target_price, p.currency)}"
                if p.target_price
                else "",
                f"{stats['n'] if stats else 0} checks",
                f"last {(p.last_checked or 'never')[:16].replace('T', ' ')}",
            ]
            if m
        ]

        cards.append(f"""<div class="card">
  <div class="head">
    <div class="name"><a href="{html.escape(p.url)}">{html.escape(p.name)}</a> {" ".join(tags)}</div>
    <div class="price">{format_price(p.last_price, p.currency)}</div>
  </div>
  <div class="meta">{" · ".join(html.escape(m) for m in meta)}</div>
  {_sparkline(points, p.target_price)}
</div>""")

    body = "\n".join(cards) or '<div class="empty">Nothing watched yet.</div>'
    doc = f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Price Monitor</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Price Monitor</h1>
<div class="sub">{len(products)} products · generated {datetime.now(timezone.utc).astimezone():%Y-%m-%d %H:%M}</div>
{body}</div></body></html>"""

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
