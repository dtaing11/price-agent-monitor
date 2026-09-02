"""A search layer that treats upstream quota as a budget it spends on purpose.

The rule the whole package exists to keep: a rate-limit error never reaches the
caller. Calls are counted before they go out, providers are chosen by what
budget they have left, and when everything is spent the caller gets stale
results or a clear `AllProvidersExhausted` carrying the earliest reset - never
someone else's 429.

    from pricemon.searchlayer import build_router
    router = build_router()
    results = await router.search("thunderbolt dock", count=8)
"""

from __future__ import annotations

import logging
import os

from .cache import SearchCache
from .errors import (
    AllProvidersExhausted,
    MalformedResponse,
    ProviderTimeout,
    QueryBudgetExceeded,
    RateLimited,
    SearchLayerError,
    Upstream5xx,
)
from .fusion import canonical_url, reciprocal_rank_fusion
from .ledger import QuotaLedger
from .models import ProviderConfig, ProviderHealth, SearchProvider, SearchResult
from .normalize import cache_key, normalize
from .router import SearchRouter

logger = logging.getLogger(__name__)

__all__ = [
    "AllProvidersExhausted",
    "MalformedResponse",
    "ProviderConfig",
    "ProviderHealth",
    "ProviderTimeout",
    "QueryBudgetExceeded",
    "QuotaLedger",
    "RateLimited",
    "SearchCache",
    "SearchLayerError",
    "SearchProvider",
    "SearchResult",
    "SearchRouter",
    "Upstream5xx",
    "build_router",
    "cache_key",
    "canonical_url",
    "normalize",
    "provider_status",
    "reciprocal_rank_fusion",
]

# Documented free-tier limits, as of writing. These are what the ledger holds
# itself to (at 80%), so if a provider changes its terms, change them here -
# guessing high is how you get a 429.
DEFAULTS: dict[str, dict] = {
    "brave": {"tier": 0, "per_second": 1, "per_minute": 60, "per_month": 2000},
    "tavily": {"tier": 0, "per_minute": 60, "per_month": 1000},
    "exa": {"tier": 1, "per_minute": 60, "per_month": 1000},
    "google_pse": {"tier": 0, "per_minute": 60, "per_day": 100},
    "searxng": {"tier": 0, "per_second": 5, "per_minute": 300},
    # No key, no quota to reserve, and a throttle that cannot be retried away:
    # allowed only as a last resort, never first and never alone.
    "ddg": {"tier": 2, "per_minute": 6, "per_day": 200, "last_resort": True},
}

ENV_KEYS = {
    "brave": ("BRAVE_SEARCH_API_KEY", "BRAVE_API_KEY"),
    "tavily": ("TAVILY_API_KEY",),
    "exa": ("EXA_API_KEY",),
    "google_pse": ("GOOGLE_PSE_API_KEY", "GOOGLE_API_KEY"),
}


def _keys_for(name: str) -> list[str]:
    """Several keys per provider, comma separated, each with its own ledger."""
    for env_name in ENV_KEYS.get(name, ()):
        raw = os.environ.get(env_name)
        if raw:
            return [k.strip() for k in raw.split(",") if k.strip()]
    return []


def build_router(
    settings: dict | None = None,
    cache: SearchCache | None = None,
    ledger: QuotaLedger | None = None,
    redis=None,
    **router_kwargs,
) -> SearchRouter:
    """Assemble whichever providers this machine is actually configured for.

    Adding a provider is one adapter file plus one entry in DEFAULTS.
    """
    from .providers.brave import BraveProvider
    from .providers.ddg import DDGProvider
    from .providers.exa import ExaProvider
    from .providers.google_pse import GooglePSEProvider
    from .providers.searxng import SearxngProvider
    from .providers.tavily import TavilyProvider

    settings = settings or {}
    classes = {
        "brave": BraveProvider,
        "tavily": TavilyProvider,
        "exa": ExaProvider,
        "google_pse": GooglePSEProvider,
        "searxng": SearxngProvider,
        "ddg": DDGProvider,
    }

    providers, configs = [], {}
    for name, cls in classes.items():
        options = {**DEFAULTS.get(name, {}), **(settings.get(name) or {})}
        if options.pop("enabled", True) is False:
            continue
        config = ProviderConfig(
            name=name,
            api_keys=options.pop("api_keys", None) or _keys_for(name),
            endpoint=options.pop("endpoint", None)
            or os.environ.get(f"{name.upper()}_ENDPOINT"),
            **options,
        )
        provider = cls(config)
        if not getattr(provider, "configured", False):
            continue  # no credentials, so nothing to spend
        providers.append(provider)
        configs[name] = config

    if list(configs) == ["ddg"]:
        # Working, but on the one source that cannot reserve quota. Say so
        # rather than letting it look healthy until it starts throttling.
        logger.warning(
            "DuckDuckGo is the only search provider configured. It has no API "
            "and no quota to reserve, so it will throttle under load. Set "
            "BRAVE_SEARCH_API_KEY or TAVILY_API_KEY for a provider with a "
            "budget the agent can actually spend."
        )

    return SearchRouter(
        providers,
        configs=configs,
        cache=cache or SearchCache(redis=redis),
        ledger=ledger or QuotaLedger(redis=redis),
        **router_kwargs,
    )


def provider_status(settings: dict | None = None) -> list[tuple[str, bool, str]]:
    """(name, configured, why) - for `pricemon config` and the settings pane."""
    router = build_router(settings)
    active = set(router.providers)
    out = []
    for name, defaults in DEFAULTS.items():
        if name in active:
            note = "last resort only" if defaults.get("last_resort") else "ready"
            out.append((name, True, note))
        else:
            env = " or ".join(ENV_KEYS.get(name, ("endpoint",)))
            out.append((name, False, f"set {env}"))
    return out
