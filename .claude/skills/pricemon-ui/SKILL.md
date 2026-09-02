---
name: pricemon-ui
description: The design system for the Price Monitor desktop app UI (pricemon/static). Use when editing app.css, app.js or index.html, or adding any screen, chart or control to the tracker, so new work matches the existing ledger aesthetic.
---

# Price Monitor UI

Project-scoped: this skill describes only this repository's app and lives in
`.claude/skills/`, not in the global `~/.claude` directory.

## The idea

The app is a **ledger, not a dashboard**. Someone tracking a dozen products
wants one question answered at a glance: *did anything move, and is anything
cheap enough to buy yet?* Rows scan better than cards, so rows it is.

## Non-negotiables

- **No CDN, no framework, no build step.** The app must start on a machine with
  nothing installed but Python, and work offline. Charts and gauges are
  hand-written SVG/CSS. Never add a `<script src="https://…">`.
- **System fonts only**, for the same reason. Personality comes from the
  mono/sans split, not from a webfont.
- Everything is served from `pricemon/static/` by `pricemon/webapp.py`.

## Tokens

Defined once at the top of `app.css`; never hardcode a colour anywhere else.
Both themes are defined - `:root` for light, `:root[data-theme="dark"]` for
dark - and every new colour needs both.

| Role | Token |
|---|---|
| Page / surface / raised | `--paper` `--surface` `--surface-2` |
| Text: primary, secondary, tertiary | `--ink` `--ink-2` `--ink-3` |
| Hairlines | `--rule` `--rule-2` |
| Price fell | `--drop` `--drop-bg` |
| Price rose | `--rise` `--rise-bg` |

**Hue is reserved for price direction.** Green means a price fell or a target
was met; clay means it rose or something needs attention. Everything else -
buttons, chips, selection, focus - is ink on paper. Do not introduce a brand
blue, a gradient, or a second accent.

## Type

- `--sans` for prose and product names.
- `--mono` for **every number**, plus micro-labels, metadata and column
  headers. Prices use `font-variant-numeric: tabular-nums` so digits align
  down the column. This is the app's voice: an instrument reading, not a
  marketing page.
- Column headers and section labels: mono, 10px, uppercase, `.09em` tracking,
  `--ink-3`.

## Layout

- 54px title bar, hairline bottom. The status is **one sentence**
  ("5 tracked · 1 at or below target"), never a row of stat tiles.
- The ledger is one bordered block; rows are separated by hairlines, not by
  gaps or shadows. Rows have no radius; the container has `--radius`.
- Row state shows in a 2px inset left edge: ink for selected, `--drop` for at
  target. Never colour a whole row except the selected+at-target case.
- The detail drawer is 372px on the right, hairline on its left edge.

## The signature: the target gauge

Each row carries a gauge showing where today's price sits between the
all-time low and high, with a notch at the target. It answers "is it cheap
yet" without reading a single number. Keep it:

- track (all-time range) → fill (up to today) → dot (today) → notch (target);
- low/high labels sit **below** the track, never on it;
- when low equals high, say "no range yet" rather than drawing a full bar that
  implies a range that does not exist.

## Writing

Sentence case, plain verbs, no exclamation marks. Name things the way a shopper
would: "Tell me at or below", not "Alert threshold". An empty state is an
invitation ("Nothing tracked yet" + what to do next), an error says what
happened and what to try. Buttons say what they do and keep that word
afterwards ("Start tracking" → the row appears).

## Quality floor

Keyboard: rows are focusable and respond to Enter/Space; `:focus-visible`
outlines are never removed. `prefers-reduced-motion` disables animation.
Below 940px the drawer overlays and the gauge/history columns drop out.
Product images always have a monogram fallback - a shop's CDN will fail.

## Checking your work

```bash
python3 -m pricemon serve --port 8794 --no-open   # then screenshot both themes
```
Confirm no console errors, and check light *and* dark before calling it done.
