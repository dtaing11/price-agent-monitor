# pricemon — an AI price-monitoring agent

Track as many products as you like, from Amazon, Walmart, IKEA, eBay or any
other shop. It records price history, shows it in a desktop app, and tells you
when something drops or reaches the price you set. Give it a product *name* and
it finds the page itself — no URL needed.

```
$ pricemon add --search "logitech mx master 3s" --target 79 --pick 1
searching for 'logitech mx master 3s' ...
picked:       $79.11  Walmart  Logitech MX Master 3S, Wireless Performance Mouse
  found $79.11 via site:Walmart (confidence 0.92)
watching logitech-mx-master-3s, target $79.00

$ pricemon app        # the desktop tracker
```

Extraction is deterministic first — the retailer's own markup, schema.org data,
embedded JSON, page heuristics — and only asks Claude when a page defeats all
of those. It then **remembers the selector Claude found**, so the next check is
instant and free. When a site redesigns, confidence drops and it learns again.

---

## Install (Linux)

```bash
cd ~/price-monitor-agent           # wherever you put this
python3 -m pip install -r requirements.txt

# optional: put it on your PATH
mkdir -p ~/.local/bin && ln -sf "$PWD/bin/pricemon" ~/.local/bin/pricemon

pricemon config                    # shows where data lives and how the AI is wired
pricemon install-desktop           # adds it to your applications menu
```

Optional extras, each worth having:

```bash
sudo apt install libnotify-bin                    # desktop pop-up alerts
python3 -m pip install playwright                 # lets it read Amazon & Target
python3 -m playwright install chromium
```

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

## The desktop app

```bash
pricemon app          # opens the tracker in its own window
pricemon serve        # the UI only, in your browser
```

`pricemon app` opens a real window: a native one if you have `pywebview`,
otherwise a Chromium `--app` window with no tabs and no address bar, with its
own taskbar entry. `pricemon install-desktop` puts it in your applications
menu so it starts like any other program.

Inside it you get every tracked product in one ledger: price, how far it has
moved, a gauge showing where today sits between its all-time low and high with
a notch at your target, and a history line. Click any row for its full chart,
statistics and settings. You can add products by name from inside the app, set
targets inline, pause a product, and run a check on demand.

It is a local app — it serves on 127.0.0.1, stores everything in
`~/.price-monitor/`, and needs no account.

---

## Everyday commands

| Command | What it does |
|---|---|
| `pricemon add --search "product name" --pick 1 --target N` | find the product by name and watch it |
| `pricemon search "product name"` | show matching product pages with live prices |
| `pricemon add <url> [--target N] [--name x] [--selector 'css']` | watch a page you already have a link to |
| `pricemon app` / `pricemon serve` | the desktop tracker / the UI alone |
| `pricemon list` | everything watched, with current price, target, stock, all-time low |
| `pricemon check [name...]` | check now and fire alerts |
| `pricemon check --quiet` | cron mode: print only when something changed |
| `pricemon history <name>` | price history with an ASCII sparkline |
| `pricemon report` | self-contained HTML dashboard you can share |
| `pricemon install-desktop` | add it to the Linux applications menu |
| `pricemon set <name> --target 25` | change a target (`--target -1` clears it) |
| `pricemon set <name> --pause` / `--resume` | stop / resume checking one product |
| `pricemon alerts` | recent alert log |
| `pricemon export prices.csv` | every observation as CSV |
| `pricemon watch --interval 3600` | foreground loop, for when you're hovering |
| `pricemon test-notify` | prove your notification channels work |

---

## Finding products by name

```bash
pricemon search "sony wh-1000xm5"
pricemon search "air fryer 5l" --retailer walmart --retailer argos
pricemon add --search "sony wh-1000xm5" --pick 2 --target 249
```

Search finds candidate product pages, opens each one, reads its real price with
the normal extraction cascade, and asks Claude which results are actually that
product rather than a case, cable, older model or multi-pack. Results show the
retailer, live price and stock, so you pick with the numbers in front of you.

---

## Shops it knows

Built-in rules for 24 retailers — Amazon, Walmart, Target, Best Buy, eBay,
Etsy, Newegg, Home Depot, Lowe's, Costco, Wayfair, Chewy, B&H, GameStop, Steam,
IKEA, Argos, Currys, John Lewis, Zalando, AliExpress, Apple, Nike, Decathlon —
covering their price markup, stock indicators and currency. Amazon links are
canonicalised to `/dp/<ASIN>` and tracking parameters are stripped, so the same
product can never be tracked twice under two URLs.

**Anywhere else still works.** The rules are a shortcut, not a requirement, and
they are never the last word: if a retailer redesigns, extraction falls through
to the generic cascade and then to Claude, which learns the new selector.

---

## How the extraction works

Each page runs through a cascade, best evidence first:

1. **Your pinned `--selector`** — always wins. Supports `sel@attribute`, e.g.
   `meta[itemprop="price"]@content`.
2. **A previously learned selector** — whatever worked last time.
3. **The retailer's own markup** — from the rules above.
4. **schema.org JSON-LD** — the `Product`/`Offer` block. Most large retailers
   publish this; it carries price, currency *and* stock status.
5. **Microdata / RDFa** — `[itemprop="price"]`.
6. **Meta tags** — `product:price:amount`, `og:price:amount`.
7. **Embedded JSON** — `__NEXT_DATA__`, `__NUXT__`, Redux state dumps. Modern
   storefronts ship the product as JSON and render it client-side; the number
   is in the HTML, just not in a tag you can select. Candidates are scored by
   key name and JSON path, so shipping, warranty and "customers also bought"
   prices lose to the real one.
8. **Class/id heuristics** — elements that look like prices, scored by where
   they sit on the page. Prices inside the main product region score up; ones
   inside `aside`, carousels, "related products" and listing cards score down,
   as do struck-through `<del>` originals.
9. **Claude** — only if everything above lands below a confidence floor of 0.75.

### Sites that hide prices from plain HTTP

Amazon and Target send an empty price skeleton to anything that is not a
browser and fill it in with JavaScript. No parser recovers a number that was
never sent, so those get rendered in a real headless browser:

```
plain HTTP  →  headless browser  →  Claude
```

Each step costs more than the last, so each only runs when the cheaper one came
up short. Install Playwright (see above) and it happens automatically for
high-protection sites, when the raw HTML has no price, or when a bot wall is
detected. Without Playwright the agent still works — it just cannot read those
particular retailers. Force it on or off with `fetch.browser: always | auto |
never`.

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

## Load and etiquette

- One request per page per run, with a real browser User-Agent.
- Per-domain throttling (`min_delay_per_domain`, 3s default) with jitter, so
  several products on the same shop never fire at once.
- Retries with exponential backoff, honouring `Retry-After` on 429s.
- **`robots.txt` is not consulted by default.** This is a personal watchlist
  checked a couple of times a day, not a crawler. Set
  `fetch.respect_robots: true` to honour it, and checks on disallowed paths
  will then fail with a clear message instead of fetching.

The throttle is the part that matters: it is what keeps you under the radar and
keeps your price history intact. Retailers' terms of use still apply to you
regardless of what the tool does, and twice a day is a cadence that stays well
inside normal browsing volume — don't crank the interval down to seconds.

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

**`robots.txt disallows`** — only appears if you set `fetch.respect_robots: true`. Set it back to `false` to fetch anyway.

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
  fetcher.py    polite HTTP + headless-browser rendering
  extract.py    the deterministic extraction cascade
  sites.py      per-retailer rules, URL canonicalisation, bot-wall detection
  search.py     product name → candidate pages, priced and AI-ranked
  llm.py        Claude fallback + page condensing + selector learning
  money.py      price-string parsing ($1,234.56 / 1.234,56 € / ₹1,49,900)
  storage.py    SQLite: products, observations, alerts
  notify.py     console / desktop / webhook / email
  cron.py       crontab block management
  webapp.py     local JSON API behind the desktop app
  desktop.py    window launcher and .desktop entry
  static/       the UI (no framework, no CDN, works offline)
  report.py     standalone HTML dashboard
tests/          unit tests:  python3 -m unittest discover -s tests
```

`.claude/skills/pricemon-ui/` holds this project's UI design system, scoped to
this repository rather than installed globally.

Run the tests with `python3 -m unittest discover -s tests -v`.
