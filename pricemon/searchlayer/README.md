# The search layer

Upstream quota is a budget this package tracks and spends on purpose. **A
rate-limit error never reaches the caller**: calls are counted before they go
out, and a provider is only asked while its ledger says there is budget left.
When everything really is spent, the caller gets stale results or
`AllProvidersExhausted` carrying the earliest reset — never someone else's 429.

```python
from pricemon.searchlayer import build_router

router = build_router()  # reads keys from the environment
results = await router.search("thunderbolt dock", count=8)
print(router.report())  # hit rate, calls, skips, errors
```

## Why it is shaped this way

**Send fewer requests.** Queries are normalised before they are hashed, so
"Sony WH-1000XM5 price", "sony wh-1000xm5  PRICE??" and "what is the price of
Sony WH-1000XM5" share one cache entry. Two tiers back that up — in-process
over Redis, so a restart does not replay the trace cold — plus a semantic tier
for paraphrases the exact key misses, negative entries so a dead question is
not re-asked, and single-flight so fifty concurrent copies of one question
become one call. On a trace with paraphrased repeats this alone takes the hit
rate past 75%, and the first two steps usually stop throttling on their own.

**Never exceed a limit.** A Redis ledger holds rolling per-second, per-minute,
per-day and per-month counts, shared across workers so one budget is not spent
twice. Each provider is fronted by a token bucket at 80% of its documented
quota — the documented number is a cliff, not a target — and an AIMD controller
that widens after sustained success and halves on any 429 or timeout. Every
response reconciles the ledger from `X-RateLimit-Remaining`, `-Reset` and
`Retry-After`, because the provider's own count beats our arithmetic. Two
consecutive rate limits open a circuit, with the cooldown scaled to the reset
the provider gave.

**Spread across owned quota.** Providers are chosen by remaining budget rather
than fixed priority, which keeps every budget alive instead of draining one and
falling over. Free tiers are tried before paid ones. Several keys per provider
each get their own ledger entry.

## Providers

Official APIs only. Adding one is a single adapter file plus an entry in
`DEFAULTS`.

| Provider | Configure with |
|---|---|
| Brave Search | `BRAVE_SEARCH_API_KEY` |
| Tavily | `TAVILY_API_KEY` |
| Exa | `EXA_API_KEY` |
| Google Programmable Search | `GOOGLE_PSE_API_KEY` + engine id |
| SearXNG (self-hosted) | `SEARXNG_ENDPOINT` |
| DuckDuckGo | nothing — and that is the problem |

**DuckDuckGo is demoted on purpose.** It has no public API, so any client for
it scrapes an HTML endpoint: no key, no quota to reserve, and a throttle that
cannot be retried away from a datacentre IP. It is last resort only — never
chosen first, never used alone — and every way it says "slow down", including a
202 with an anomaly page, is translated into `RateLimited` so the router treats
it like any other spent budget. If it is the only provider configured, the
layer logs a warning saying so.

## Contracts

`SearchResult` carries `url, title, snippet, published_at, rank, provider`.
A provider is `name`, `async search(query, count, lang)` and `async health()`.
Adapters raise only `RateLimited`, `Upstream5xx`, `ProviderTimeout` and
`MalformedResponse` — no library exception escapes an adapter, so the router
never has to know one provider's vocabulary from another's.

## Modules

`normalize` · `cache` · `singleflight` · `ledger` · `limiter` · `breaker` ·
`fusion` (RRF merge plus URL canonicalisation) · `router` · `observability` ·
`providers/`

## Verifying it

```bash
python3 -m pytest tests/searchlayer -q
```

The tests are the acceptance criteria: a 1,000-query run raises nothing and
breaches no window; hit rate above 60% on paraphrased repeats; fifty concurrent
identical queries make one call; a provider forced to constant 429 is dropped
within two attempts and the router falls through silently; killing Redis
mid-run degrades to in-process limits and keeps serving.
