"""The AI half of the agent.

When the deterministic extractors disagree or come up empty, ask Claude to read
the page.  Claude returns not just the price but a *CSS selector* for it, which
we store on the product - so the next check is fast, free and deterministic
again.  That is the self-healing loop: sites redesign, the agent re-learns.

Backends
  claude_cli : shells out to the `claude` CLI -> uses your Claude OAuth login,
               no API key and no per-token API bill.
  api        : the anthropic SDK with ANTHROPIC_API_KEY.
  off        : deterministic extraction only.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Comment

from .extract import attr_str
from .models import Extraction

PROMPT = """You are the extraction step of a price-monitoring agent.

Read the product page below and report the price a shopper would pay RIGHT NOW
for the main product on the page.

Rules:
- Current/sale price, not the struck-through original, not "was" or RRP/MSRP.
- Ignore shipping, tax, monthly-installment and bundle/accessory prices.
- Ignore prices for related or recommended products.
- price must be a number only (no symbols, no thousands separators).
- currency is a 3-letter ISO code (USD, EUR, GBP, ...) or null if truly unclear.
- css_selector must be a selector that resolves to the element holding this
  price on this page, stable enough to reuse on the next visit. Append @attr
  (e.g. 'meta[itemprop="price"]@content') when the value lives in an attribute.
- image_url, if you can tell, is the main product photo (not a logo, badge,
  banner or a picture of a different product).
- confidence is 0.0-1.0, your honest read.
- If there is no purchasable price on the page, set price to null.

URL: {url}

Candidates the deterministic extractors already found (may be wrong or empty):
{candidates}
{visual}
PAGE:
{page}

Reply with ONLY a JSON object, no prose and no markdown fences:
{{"price": number|null, "currency": string|null, "in_stock": true|false|null,
  "title": string|null, "css_selector": string|null, "image_url": string|null,
  "confidence": number, "reasoning": string}}"""

# What the browser measured, handed over as a table. This is the part markup
# cannot express: which price is crossed out, which is screen-reader-only, how
# big each one is drawn and where it sits on the page.
VISUAL_BLOCK = """
HOW THE PAGE ACTUALLY RENDERS (measured in a real browser - trust this over the
markup below, since it accounts for CSS the HTML alone does not show):
{table}

Notes on reading that table:
- "crossed out" is a was/list price. Never report it as the price.
- "screen-reader text" is a shop's own accessible copy of a value. It is often
  the cleanest form of the real price, especially where the visible price is
  split across several elements ($ / 859 / 99).
- Bigger text higher on the page is usually the price being sold right now.
- The rendered product photo the browser ranked first: {image}
{shot}"""

CURRENCY_RE = re.compile(
    r"[$€£¥₹₩]|\b(?:USD|EUR|GBP|JPY|CAD|AUD|INR|SEK|PLN|CHF)\b", re.IGNORECASE
)


class LLMUnavailable(RuntimeError):
    pass


# --------------------------------------------------------------------------
def condense(html: str, max_chars: int) -> str:
    """Shrink a page to the parts that could plausibly hold a price."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "iframe", "path", "footer"]):
        tag.decompose()
    for node in soup.find_all(string=lambda t: isinstance(t, Comment)):
        node.extract()

    head_bits = []
    if soup.title:
        head_bits.append(str(soup.title))
    for meta in soup.select("meta[property], meta[itemprop], meta[name]"):
        prop = (
            attr_str(meta, "property")
            or attr_str(meta, "itemprop")
            or attr_str(meta, "name")
            or ""
        ).lower()
        if any(
            k in prop for k in ("price", "title", "availability", "currency", "product")
        ):
            head_bits.append(str(meta))
    for ld in soup.find_all(
        "script", attrs={"type": re.compile("ld\\+json", re.IGNORECASE)}
    ):
        head_bits.append(str(ld)[:4000])

    body = soup.body or soup
    whole = re.sub(r"\n\s*\n+", "\n", str(body))
    if len(whole) <= max_chars:
        return "\n".join(head_bits + [whole])[:max_chars]

    # Too big: keep only neighbourhoods around currency-looking text.
    snippets: list[str] = []
    seen = set()
    for text_node in body.find_all(string=CURRENCY_RE):
        el = text_node.parent
        if el is None:
            continue
        for _ in range(2):  # climb for context
            parent = el.parent
            if parent is not None and parent.name not in ("body", "html", "[document]"):
                el = parent
        chunk = re.sub(r"\s+", " ", str(el))[:800]
        key = chunk[:120]
        if key not in seen:
            seen.add(key)
            snippets.append(chunk)
        if sum(len(s) for s in snippets) > max_chars:
            break

    h1 = body.find("h1")
    if h1:
        snippets.insert(0, str(h1)[:400])
    return "\n".join(head_bits + ["<!-- price-bearing fragments -->"] + snippets)[
        :max_chars
    ]


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


# --------------------------------------------------------------------------
class LLM:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.backend = self._resolve(cfg.get("backend", "auto"))
        self.model = cfg.get("model") or "claude-opus-5"

    @staticmethod
    def _resolve(backend: str) -> str:
        if backend != "auto":
            return backend
        if shutil.which("claude"):
            return "claude_cli"
        if os.environ.get("ANTHROPIC_API_KEY"):
            return "api"
        return "off"

    @property
    def available(self) -> bool:
        return self.backend in ("claude_cli", "api")

    def describe(self) -> str:
        if self.backend == "claude_cli":
            return f"claude CLI (OAuth login), model={self.model}"
        if self.backend == "api":
            return f"anthropic API, model={self.model}"
        return "disabled"

    # -- backends ---------------------------------------------------------
    def _ask_cli_with_tools(self, prompt: str, tools: str) -> str:
        """Run the CLI with a specific tool allowed, for jobs that need one."""
        return self._ask_cli(prompt, allowed_tools=tools)

    def _ask_cli(
        self,
        prompt: str,
        screenshot: str | None = None,
        allowed_tools: str | None = None,
    ) -> str:
        # Tools stay off by default - this is a read-and-answer task. The one
        # exception is a screenshot: Read is what lets the CLI open the image,
        # so it is allowed only when there is an image to look at.
        tools = (
            allowed_tools
            if allowed_tools is not None
            else ("Read" if screenshot else "")
        )
        cmd = [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            self.model,
            "--allowed-tools",
            tools,
        ]
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.cfg.get("timeout", 180),
            check=False,
        )
        if proc.returncode != 0:
            raise LLMUnavailable(
                f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:300]}"
            )
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return proc.stdout
        if payload.get("is_error"):
            raise LLMUnavailable(
                f"claude CLI error: {str(payload.get('result'))[:300]}"
            )
        return payload.get("result", "")

    def ask_with_web_search(self, prompt: str) -> str:
        """Answer a question that needs the live web.

        Extraction reads a page we already fetched, so it runs with tools off.
        Finding *which* page to fetch is the opposite problem, and web search is
        the tool for it.
        """
        if not self.available:
            raise LLMUnavailable("no LLM backend available")
        if self.backend == "claude_cli":
            return self._ask_cli_with_tools(prompt, tools="WebSearch")
        return self._ask_api(prompt)

    def _ask_api(self, prompt: str, screenshot: str | None = None) -> str:
        try:
            import anthropic
        except ImportError as exc:
            raise LLMUnavailable("anthropic SDK not installed") from exc

        content: list[Any] = []
        if screenshot and Path(screenshot).is_file():
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": "image/png",
                        "data": base64.standard_b64encode(
                            Path(screenshot).read_bytes()
                        ).decode(),
                    },
                }
            )
        content.append({"type": "text", "text": prompt})

        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=self.model,
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(
            str(getattr(b, "text", ""))
            for b in resp.content
            if getattr(b, "type", "") == "text"
        )

    # -- public -----------------------------------------------------------
    def extract(
        self,
        html: str,
        url: str,
        candidates: list[Extraction],
        rendered=None,
    ) -> Extraction:
        """Ask Claude for the price, given everything we know about the page.

        `rendered` is an optional RenderedPage from the headless browser. When
        present the model is told how the page actually draws - which prices are
        crossed out or screen-reader-only, how large each is, where it sits -
        and is shown a screenshot. Markup alone cannot express any of that, and
        it is exactly what separates the price from the "was" price.
        """
        if not self.available:
            raise LLMUnavailable("no LLM backend available")

        cand_text = (
            "\n".join(
                f"- {c.method}: {c.price} {c.currency or ''} (confidence {c.confidence:.2f}, "
                f"selector {c.selector or '-'}) {c.note}"
                for c in candidates[:6]
            )
            or "- none"
        )

        visual = ""
        screenshot = None
        if rendered is not None and rendered.prices:
            screenshot = rendered.screenshot
            visual = VISUAL_BLOCK.format(
                table=rendered.summarise(),
                image=rendered.product_image() or "none found",
                shot=(
                    f"- A screenshot of the page is at {screenshot} — open it and "
                    "look at the price before answering."
                    if screenshot
                    else ""
                ),
            )

        prompt = PROMPT.format(
            url=url,
            candidates=cand_text,
            visual=visual,
            page=condense(html, int(self.cfg.get("max_html_chars", 40000))),
        )
        raw = (
            self._ask_cli(prompt, screenshot=screenshot)
            if self.backend == "claude_cli"
            else self._ask_api(prompt, screenshot=screenshot)
        )
        data = _extract_json(raw)
        if not data:
            raise LLMUnavailable(f"could not parse model reply: {raw[:200]!r}")

        price = data.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        return Extraction(
            price=price,
            currency=(data.get("currency") or None),
            in_stock=data.get("in_stock"),
            title=data.get("title"),
            image=(data.get("image_url") or None),
            method="llm",
            confidence=float(data.get("confidence") or 0.7),
            selector=data.get("css_selector") or None,
            note=(data.get("reasoning") or "")[:300],
        )
