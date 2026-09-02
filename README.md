# pricemon — an AI price-monitoring agent

Watches product pages, records price history, and tells you when something drops
or hits your target. It scrapes deterministically first (schema.org, microdata,
meta tags, CSS selectors) and only asks Claude when the page defeats those —
then **remembers the selector Claude found**, so the next run is instant and free.

```
$ pricemon add https://www.ikea.com/us/en/p/billy-bookcase-white-00263850/ --target 35
Fetching https://www.ikea.com/us/en/p/billy-bookcase-white-00263850/ ...
  found $39.00 via jsonld (confidence 0.90)
  title: BILLY, Bookcase, oak effect
watching billy-bookcase, target $35.00

$ pricemon check
→ billy-bookcase
  $29.00 via jsonld (conf 0.90) · in stock
🎯 billy-bookcase hit your target: $29.00 (target $35.00) — https://www.ikea.com/...
```

---

## Install (Linux)

```bash
cd ~/price-monitor-agent           # wherever you put this
python3 -m pip install -r requirements.txt

# optional: put it on your PATH
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/pricemon" ~/.local/bin/pricemon

pricemon config                    # shows where data lives and how the AI is wired
```

`libnotify-bin` gives you desktop pop-ups (`sudo apt install libnotify-bin`).
Everything else is pure Python.

State lives in `~/.price-monitor/` (`config.yaml`, `prices.db`, `cron.log`).
Override with `PRICEMON_HOME=/some/where`.

---

## Run it automatically, twice a day

```bash
pricemon install-cron                      # 08:00 and 20:00
pricemon install-cron --times 07:30,19:00  # your hours
pricemon install-cron --dry-run            # just show me the crontab lines
pricemon uninstall-cron
```

This writes a marked block into your crontab that you can inspect with
`crontab -l`. Cron runs with almost no environment, so the command embeds its
own `PATH`, `HOME`, and (on Linux) the `DISPLAY` / `DBUS_SESSION_BUS_ADDRESS`
that `notify-send` needs to reach your desktop session.

Scheduled runs use `check --quiet`: **silence unless something happened.**
Alerts land in `~/.price-monitor/cron.log` plus whatever channels you enabled.

Prefer systemd? A timer calling
`ExecStart=/home/you/price-monitor-agent/bin/pricemon check --quiet` works
identically — cron is just the default because it needs no root.

---

## Everyday commands

| Command | What it does |
|---|---|
| `pricemon add <url> [--target N] [--name x] [--selector 'css']` | start watching a page |
| `pricemon list` | everything watched, with current price, target, stock, all-time low |
| `pricemon check [name...]` | check now and fire alerts |
| `pricemon check --quiet` | cron mode: print only when something changed |
| `pricemon history <name>` | price history with an ASCII sparkline |
| `pricemon report` | self-contained HTML dashboard with charts |
| `pricemon set <name> --target 25` | change a target (`--target -1` clears it) |
| `pricemon set <name> --pause` / `--resume` | stop / resume checking one product |
| `pricemon alerts` | recent alert log |
| `pricemon export prices.csv` | every observation as CSV |
| `pricemon watch --interval 3600` | foreground loop, for when you're hovering |
| `pricemon test-notify` | prove your notification channels work |

---

## How the extraction works

Each page runs through a cascade, best evidence first:

1. **Your pinned `--selector`** — always wins. Supports `sel@attribute`, e.g.
   `meta[itemprop="price"]@content`.
2. **A previously learned selector** — whatever worked last time.
3. **schema.org JSON-LD** — the `Product`/`Offer` block. Most large retailers
   publish this; it carries price, currency *and* stock status.
4. **Microdata / RDFa** — `[itemprop="price"]`.
5. **Meta tags** — `product:price:amount`, `og:price:amount`.
6. **Class/id heuristics** — elements that look like prices, scored by where
   they sit on the page. Prices inside the main product region score up; ones
   inside `aside`, carousels, "related products" and listing cards score down,
   as do struck-through `<del>` originals.
7. **Claude** — only if everything above lands below a confidence floor of 0.75.

Claude gets a condensed version of the page (scripts stripped, price-bearing
fragments kept) and returns the price, currency, stock, *and a CSS selector*.
The agent **verifies that selector against the page it just fetched** before
saving it — if it doesn't resolve to the same number, it's discarded rather
than trusted. Saved selectors mean step 2 handles the site from then on, and a
site redesign simply drops confidence and re-triggers the learning loop.

### The AI backend

`pricemon config` shows which one is active:

- **`claude_cli`** (default when the `claude` CLI is installed) — runs through
  your **Claude OAuth login**. No API key, no separate API bill.
- **`api`** — the `anthropic` SDK, if `ANTHROPIC_API_KEY` is set.
- **`off`** — deterministic extraction only.

Model defaults to `claude-opus-5`. Price extraction is an easy task, so
`haiku` is dramatically faster and cheaper and works fine:

```yaml
llm:
  backend: auto
  model: haiku        # or claude-opus-5, sonnet, ...
```

or per-run: `pricemon check --model haiku`.

---

## Alerts

Configured in `~/.price-monitor/config.yaml`:

```yaml
alerts:
  drop_pct: 5.0            # notify when the price falls this much vs last check
  notify_price_rise: false
  notify_stock_change: true
  fail_streak_alert: 3     # tell me after 3 consecutive scrape failures

notify:
  console: true
  desktop: true            # notify-send on Linux, osascript on macOS
  webhook_url: null        # a Slack or Discord webhook works as-is
  email:                   # optional
    host: smtp.gmail.com
    port: 587
    user: you@gmail.com
    password: app-password
    to: you@gmail.com
```

Alerts fire on **transitions**, not on every run — a product sitting below its
target stays quiet until it drops further or crosses the line again. You will
not be nagged twice a day forever.

---

## Being a good scraper

- One request per page per run, with a real browser User-Agent.
- **`robots.txt` is respected** by default. If a site disallows the path, the
  check fails loudly rather than sneaking around it. Override deliberately with
  `fetch.respect_robots: false`.
- Per-domain throttling (`min_delay_per_domain`, 3s default) with jitter.
- Retries with exponential backoff, honouring `Retry-After` on 429s.

Twice a day is a polite cadence. Don't point this at a site whose terms forbid
it, and don't crank the interval down to seconds.

---

## When a site doesn't work

**`no price found`** — the page probably renders its price in JavaScript.
Check with `curl -s <url> | grep -i price`; if the number isn't in the HTML, no
amount of parsing will find it. Options: look for a JSON API the page calls
(often the cleanest fix — point `--url` at that), or pin a selector for a
server-rendered element that does exist.

**Wrong price picked** — pin it: `pricemon set <name> --selector 'span.a-price .a-offscreen'`.
Open devtools, right-click the price element, Copy → selector. This also clears
any learned selector.

**`HTTP 403` / captcha** — the retailer is blocking datacentre or bot traffic.
Big marketplaces (Amazon, Walmart, Best Buy) actively fight scrapers and their
terms usually forbid it; many have affiliate or product APIs that are the
supported path.

**`robots.txt disallows`** — working as intended. Decide consciously.

**Cron ran but nothing happened** — check `~/.price-monitor/cron.log`, and
confirm the entries exist with `crontab -l`. If desktop pop-ups don't appear
but the log shows alerts, your session bus differs from the one in the crontab
line — re-run `pricemon install-cron` from inside your desktop session.

---

## Layout

```
pricemon/
  cli.py        commands and argument parsing
  agent.py      the loop: fetch → extract → decide → remember → alert
  fetcher.py    polite HTTP: robots, throttling, retries, encoding
  extract.py    the deterministic extraction cascade
  llm.py        Claude fallback + page condensing + selector learning
  money.py      price-string parsing ($1,234.56 / 1.234,56 € / ₹1,49,900)
  storage.py    SQLite: products, observations, alerts
  notify.py     console / desktop / webhook / email
  cron.py       crontab block management
  report.py     HTML dashboard
tests/          unit tests:  python3 -m unittest discover -s tests
```

Run the tests with `python3 -m unittest discover -s tests -v`.
