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

## Install (Linux / macOS)

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

## Install (Windows)

```powershell
cd $HOME\price-monitor-agent
python -m pip install -r requirements.txt

python -m pricemon config            # where data lives, how the AI is wired
python -m pricemon install-desktop   # Start Menu shortcut
```

Add the `bin\` folder to your PATH and `pricemon ...` works from any prompt,
same as on Linux. Optional extras:

```powershell
python -m pip install playwright ; python -m playwright install chromium
```

Windows differences, all handled for you:

- **Scheduling uses Task Scheduler, not cron.** `pricemon install-cron` creates
  two daily tasks (`PriceMonitor 0800`, `PriceMonitor 2000`) that run a
  generated `run-check.cmd` in `%USERPROFILE%\.price-monitor\`. Check them with
  `schtasks /Query /TN "PriceMonitor 0800"`, or run that .cmd by hand to test.
- **Alerts are Windows toast notifications** through PowerShell — no extra
  install. Phone alerts (below) work identically everywhere.
- Scheduled runs use `pythonw.exe` when it is available, so no console window
  pops up twice a day.

---

## The desktop app

```bash
pricemon app          # opens the tracker in its own window
pricemon serve        # the UI only, in your browser
```

**To get it in your applications, run `pricemon install-desktop` once.** It
installs to the right place for your OS:

| | Where it lands | How to open it |
|---|---|---|
| macOS | `~/Applications/Price Monitor.app` | Launchpad, or ⌘-space → "Price Monitor" |
| Linux | `~/.local/share/applications/pricemon.desktop` | your applications menu |
| Windows | Start Menu → Price Monitor | Start menu, or pin it |

On macOS it builds a real `.app` bundle with its own icon and registers it with
Launch Services, so Spotlight and Launchpad find it immediately — a `.desktop`
file, which is what Linux uses, is ignored by macOS entirely.

**For a true native window** rather than a browser tab, install pywebview:

```bash
python3 -m pip install pywebview
```

Without it, `pricemon app` looks for Chrome, Edge, Brave or Chromium and opens
a chromeless `--app` window; failing that it falls back to a normal tab in your
default browser. (One cosmetic wrinkle on macOS: the Dock and menu bar may
still read "Python" even though the window is titled Price Monitor — that is
how framework Python builds identify themselves.)

**Settings live in the app** — the ⚙ button, or ⌘/Ctrl-comma. Three tabs:

- **Alerts** — desktop pop-ups, phone (ntfy topic or Telegram bot), email
  (SMTP host, port, credentials, recipient) and a Slack/Discord webhook, with a
  *Send a test alert* button that saves what is on screen and fires it through
  every channel you filled in.
- **Schedule** — a switch and a list of times. Turning it on writes the cron
  entries (or Windows Scheduled Tasks) for you; turning it off removes them.
  It shows what is currently scheduled and where the log is.
- **Rules** — how big a drop is worth telling you about, when to double-check
  an implausible move, whether rises and stock changes count, how many sites to
  check at once, and how gently to treat one shop.

Secrets are masked when the form loads and only overwritten if you type
something new, so opening settings never hands your SMTP password back out.
Everything still lives in `~/.price-monitor/config.yaml` if you prefer editing
it directly.

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
| `pricemon compare "product name" --target N` | track it at every shop that sells it |
| `pricemon group [name]` | compare the shops for one product |
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

### Search that does not get rate-limited

Web search is the fragile part of any shopping agent, so it lives behind its
own layer (`pricemon/searchlayer/`, [documented here](pricemon/searchlayer/README.md))
built on one rule: **a rate-limit error never reaches the caller**. Quota is a
budget the agent tracks and spends deliberately, rather than something it
discovers by getting a 429.

Queries are normalised before caching, so paraphrases of one question share an
entry; identical concurrent questions collapse into a single call; a ledger
counts calls before they go out and refuses rather than overspending; providers
are chosen by remaining budget; and two rate limits open a circuit. When
everything is genuinely spent you get stale results or a clear error carrying
the earliest reset.

Configure any of Brave, Tavily, Exa, Google Programmable Search or a
self-hosted SearXNG with an environment variable:

```bash
export BRAVE_SEARCH_API_KEY=...     # or TAVILY_API_KEY, EXA_API_KEY, ...
pricemon config                     # shows which providers are ready
```

DuckDuckGo works with no key and is used as a **last resort only** — it has no
public API, so there is no quota to reserve and its throttle cannot be retried
away. With no key configured the agent still runs, just on the one source that
will throttle under load, and it says so.

**Vague is fine too.** "thunderbolt dock" names a category, not a product, and
searching it verbatim returns listicles and category pages. So the agent works
out what you actually mean before searching:

```
$ pricemon search "thunderbolt dock"
working out what 'thunderbolt dock' means ...
  reading that as: Thunderbolt docking stations to expand ports and connect peripherals
    → CalDigit TS4 Thunderbolt 4 Dock
    → Belkin Thunderbolt 4 Dock Pro
    → OWC Thunderbolt Dock
    → Kensington Thunderbolt 4 Dock

  #  PRICE        RETAILER         PRODUCT
  1      $269.99  kensington.com   SD5700T Thunderbolt 4 Dual 4K Docking Station · in stock
  2      $299.99  belkin.com       Pro Thunderbolt 4 Dock · OUT OF STOCK
  3      $379.99  us.caldigit.com  TS4 - Thunderbolt Station 4 · in stock
```

Those model names are only a plan: each is then searched and priced for real, so
a suggestion that is discontinued or invented simply finds nothing and drops
out. The reading is printed so you can see what it went looking for.

**How exact does the name have to be?** Not very — brand and model is plenty.
You can paste a whole product title straight off a shop page and it will be
trimmed for you:

| You paste | It searches for |
|---|---|
| `Logitech MX Master 3S - Wireless Performance Mouse with Ultra-fast Scrolling, Ergo, 8K DPI…` | `Logitech MX Master 3S` |
| `Sony WH-1000XM5 Wireless Industry Leading Noise Cancelling Headphones with…` | `Sony WH-1000XM5` |
| `BILLY Bookcase, white, 31 1/2x11x79 1/2"` | `BILLY Bookcase` |

Searching a full title verbatim is what fails: engines answer a marketing
sentence with the brand's home page rather than the product. The trimmed query
is printed so you can see what was actually looked up, and if it guessed wrong,
type the brand and model yourself — or paste the product link and skip search
entirely.

---

## One product, every shop that sells it

```bash
pricemon compare "logitech mx master 3s" --target 75
pricemon group logitech-mx-master-3s
```

`compare` searches, prices each shop, and tracks them all as one group — big
retailers and niche ones alike (in testing: Walmart, Office Depot and
Logitech's own store). You then watch a single line in the app that shows the
cheapest price, and the drawer draws **one line per shop** so you can see who
is actually cheapest over time, not just today.

```
MX Master 3S Bluetooth Edition            4 shops
  → $80.99  Walmart          · low $80.99 · target $75.00
    $84.99  logitech.com
    $99.99  officedepot.com
   $109.99  logitech.com
cheapest is Walmart — $29.00 less than the dearest
```

If several shops cross your target at once you get **one** notification, naming
the cheapest and how many it beat — not one per shop.

In the app, tick several results and the button becomes **Track N together** —
one group, one row, one chart. That works for the same product at several shops
*and* for several competing products; the app tells them apart by whether the
members share a title, and says "5 shops" or "5 products compared" accordingly.

Each line gets its own colour from a fixed palette, checked for colour-blind
separation and contrast against both themes rather than eyeballed. Green stays
reserved for the target line and the "cheapest" tag, and every line is named
with its price in the legend, so nothing depends on colour alone.

**Missed a shop?** Add it to the group by URL:

```bash
pricemon add "https://us.govee.com/products/…" --group govee-permanent-outdoor-lights
pricemon set <name> --group ""      # take one back out
```

**Watch out for variants.** Shops list a 50ft kit and a 200ft kit under one
product name, and a group has no way to know they differ. When one member's
price is a fraction of the rest, both the CLI and the app say so rather than
presenting it as a bargain — open it and pin the right variant's URL if the
warning is right.

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

### Reading the page the way you see it

Markup cannot say which price is crossed out, which is only announced to screen
readers, or which picture is the product rather than the shop's logo — that
lives in the CSS and in where things land on screen. When a page is rendered in
a browser, the agent asks it directly: for every price-like string it records
the font size, weight, whether it is struck through, whether it is visible at
all, and its position; for every image, its real size and place on the page.

That yields a short, high-signal table instead of a megabyte of markup, and it
is shared by both readers:

- **The deterministic scorer** uses it to rank candidates, so a struck-through
  "was" price and a hidden element can never win.
- **Claude** gets the same table *plus a screenshot of the page*, so it is
  looking at the rendered page rather than guessing from tags.

Two rules earn their keep here. A shop's screen-reader price
(`<span class="a-offscreen">$859.99</span>`) is often the cleanest form of the
real price, especially when the visible one is split across elements — so it is
trusted rather than discarded as "hidden". And when the only things on screen
are finance offers, bundle totals and protection plans, the visual reader
declines instead of reporting one as the price.

Screenshots are kept in `~/.price-monitor/shots/` so you can see what the agent
saw.

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
  desktop: true            # notify-send · osascript · Windows toast
  ntfy:                    # push to your phone — free, no account
    topic: pick-something-unguessable
    server: https://ntfy.sh
  telegram:                # or Telegram, if you prefer
    bot_token: null
    chat_id: null
  webhook_url: null        # a Slack or Discord webhook works as-is
  email:                   # optional
    host: smtp.gmail.com
    port: 587
    user: you@gmail.com
    password: app-password
    to: you@gmail.com
```

### Getting alerts on your phone

The scheduled check runs at 08:00 and 20:00 whether or not you are at the
machine, so a desktop toast is easy to miss — and a good price can be gone by
the evening. Two ways to reach your pocket:

**ntfy** (fastest to set up, free, no account): install the ntfy app, subscribe
to a topic name, and put that same name in `notify.ntfy.topic`. Anyone who
knows a topic can post to it, so pick something unguessable. Target hits arrive
at priority 5, which cuts through Do Not Disturb; routine moves stay quiet.

**Telegram**: create a bot with `@BotFather`, message it once, then read your
chat id from `https://api.telegram.org/bot<token>/getUpdates`, and fill in
`notify.telegram`.

Either way the notification carries the product link, so an alert is one tap
from the page. Test both with `pricemon test-notify`.

Alerts fire on **transitions**, not on every run — a product sitting below its
target stays quiet until it drops further or crosses the line again. You will
not be nagged twice a day forever.

---

## Load and etiquette

- **Several sites are checked at once** (`fetch.workers`, 6 by default), so a
  watchlist finishes in about the time of its slowest single page rather than
  the sum of all of them. Measured on six shops: 12.4s of work done in 9.3s,
  where 9.3s *was* the one page needing a browser render.
- Per-domain throttling (`min_delay_per_domain`, 3s default) with jitter, so
  several products **on the same shop** still go one at a time, however many
  workers are running. Concurrency spreads across shops, never within one.
- Browser renders and LLM calls are capped separately (`fetch.heavy_workers`,
  2 by default) — starting six copies of Chromium at once helps nobody.
- One request per page per run, with a real browser User-Agent.
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
  cron.py       crontab block management (Linux/macOS)
  schedule.py   one scheduling command for cron and Windows Task Scheduler
  webapp.py     local JSON API behind the desktop app
  desktop.py    window launcher and .desktop entry
  static/       the UI (no framework, no CDN, works offline)
  report.py     standalone HTML dashboard
tests/          unit tests:  python3 -m unittest discover -s tests
```

`.claude/skills/pricemon-ui/` holds this project's UI design system, scoped to
this repository rather than installed globally.

Run the tests with `python3 -m unittest discover -s tests -v`.
