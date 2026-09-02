"""Reading a page the way a person sees it.

Parsing markup alone cannot tell a live price from a struck-through one, or a
product photo from a tracking pixel - that information lives in the CSS, and in
where things actually land on the screen. When a page is rendered in a real
browser we can ask it directly: how big is this text, is it crossed out, is it
visible at all, where is it on the page.

The browser does the work, so this is cheap. What comes back is a short, high
signal table that both the deterministic scorer and Claude can read, instead of
a megabyte of markup neither can.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Collected inside the page. Kept deliberately small: text nodes that could be
# a price, plus the visual facts that decide which one a shopper would read.
PRICE_PROBE_JS = r"""
() => {
  const MONEY = /(?:[$£€¥₹₩]|USD|EUR|GBP|CAD|AUD|INR|SEK|PLN|CHF)\s*\d|\d[\d.,]*\s*(?:[$£€¥₹₩]|USD|EUR|GBP)/i;
  const out = [];

  const selectorFor = (el) => {
    if (el.id && /^[A-Za-z][\w-]*$/.test(el.id)) return "#" + el.id;
    for (const a of ["data-testid", "data-test", "data-qa", "itemprop"]) {
      const v = el.getAttribute && el.getAttribute(a);
      if (v) return `${el.tagName.toLowerCase()}[${a}="${v}"]`;
    }
    const cls = (el.className && typeof el.className === "string" ? el.className : "")
      .split(/\s+/).filter((c) => /^[A-Za-z][\w-]*$/.test(c)).slice(0, 3);
    return el.tagName.toLowerCase() + cls.map((c) => "." + c).join("");
  };

  const crossedOut = (el) => {
    let node = el, depth = 0;
    while (node && depth < 6) {
      const tag = node.tagName ? node.tagName.toLowerCase() : "";
      if (tag === "del" || tag === "s" || tag === "strike") return true;
      const dec = getComputedStyle(node).textDecorationLine || "";
      if (dec.includes("line-through")) return true;
      node = node.parentElement; depth++;
    }
    return false;
  };

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  const seen = new Set();
  let node;
  while ((node = walker.nextNode())) {
    const text = (node.textContent || "").trim();
    if (!text || text.length > 40 || !MONEY.test(text)) continue;
    const el = node.parentElement;
    if (!el || seen.has(el)) continue;
    seen.add(el);

    const style = getComputedStyle(el);
    const box = el.getBoundingClientRect();

    // Three states, not two. Text can be invisible on purpose *for sighted
    // users* while being the canonical value for a screen reader - Amazon's
    // .a-offscreen price is exactly this, and it is the most reliable string
    // on the page. Treating it as "hidden" throws away the right answer.
    // An element inside a display:none parent has a perfectly normal computed
    // style of its own and a 0x0 box, so asking the element alone is not
    // enough. checkVisibility walks the ancestors the way the renderer does.
    const rendered = el.checkVisibility
      ? el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true })
      : !!(el.offsetParent || style.position === "fixed");
    const gone =
      !rendered || style.display === "none" || style.visibility === "hidden" ||
      Number(style.opacity) === 0 || el.closest("option, template, [aria-hidden=true]") !== null;
    const clipped =
      (style.clip && style.clip !== "auto") ||
      (style.clipPath && style.clipPath !== "none") ||
      box.width <= 1 || box.height <= 1 ||
      box.x + window.scrollX < -500 || box.y + window.scrollY < -500 ||
      (style.position === "absolute" && (box.width < 4 || box.height < 4));
    const screenReader = !gone && clipped;
    const hidden = gone || (clipped && !screenReader);

    // Text the renderer never draws cannot be the price, and on a page like
    // Amazon's there is enough of it in menus and hidden panels to fill the
    // budget before the walker ever reaches the buy box.
    if (gone) continue;

    out.push({
      text,
      selector: selectorFor(el),
      tag: el.tagName.toLowerCase(),
      font_size: Math.round(parseFloat(style.fontSize) || 0),
      font_weight: String(style.fontWeight || ""),
      color: style.color,
      struck: crossedOut(el),
      hidden,
      screen_reader: screenReader,
      x: Math.round(box.x + window.scrollX),
      y: Math.round(box.y + window.scrollY),
      width: Math.round(box.width),
      height: Math.round(box.height),
      // What a screen reader would announce nearby - "was", "sale", "each".
      context: (el.parentElement
        ? (el.parentElement.innerText || el.parentElement.textContent || "")
        : "").replace(/\s+/g, " ").trim().slice(0, 90),
    });
    if (out.length >= 140) break;
  }
  return out;
}
"""

# The product photo, chosen the way you would: the biggest picture near the top
# of the page that is actually on screen, ignoring logos, icons and pixels.
IMAGE_PROBE_JS = r"""
() => {
  const junk = /sprite|logo|icon|badge|placeholder|transparent|blank|spacer|pixel|1x1|avatar|flag|marketing|banner|promo|prime[_-]?logo/i;
  const shots = [];
  for (const img of Array.from(document.images).slice(0, 250)) {
    const box = img.getBoundingClientRect();
    const src = img.currentSrc || img.src || "";
    if (!src.startsWith("http")) continue;
    const w = img.naturalWidth || box.width, h = img.naturalHeight || box.height;
    if (w < 150 || h < 150) continue;                 // too small to be the product
    // Amazon serves product shots from /images/I/ and chrome from everywhere
    // else on the same CDN, so the host proves nothing - the path does.
    if (junk.test(src)) continue;
    const style = getComputedStyle(img);
    if (style.display === "none" || style.visibility === "hidden") continue;
    shots.push({
      src, width: w, height: h,
      top: Math.round(box.y + window.scrollY),
      area: Math.round(w * h),
      alt: (img.alt || "").slice(0, 80),
    });
  }
  // Prefer big, and prefer high on the page - galleries sit above the fold.
  // Big and high on the page, and an alt text is a good sign it is the
  // product rather than decoration.
  shots.sort((a, b) => {
    const rank = (s) => s.area * (s.alt ? 1.35 : 1) * (s.top < 1400 ? 1.5 : 1);
    return rank(b) - rank(a);
  });
  return shots.slice(0, 6);
}
"""


@dataclass
class SeenPrice:
    """A price-looking string, with how it actually appears on screen."""

    text: str
    selector: str
    tag: str = ""
    font_size: int = 0
    font_weight: str = ""
    color: str = ""
    struck: bool = False
    hidden: bool = False
    screen_reader: bool = False
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    context: str = ""

    @property
    def prominence(self) -> float:
        """How much this reads like *the* price, from looks alone.

        Big, bold, visible, near the top, not crossed out. This is the judgement
        a person makes in a glance and markup cannot express.
        """
        if self.hidden or self.struck:
            return 0.0
        if self.screen_reader:
            # No visual size to judge by, but a shop only writes an accessible
            # price for the price a shopper pays - a strong signal on its own.
            score = 0.62
            if self.y < 1400:
                score += 0.08
            lowered = self.context.lower()
            if any(w in lowered for w in ("list:", "was", "rrp", "msrp", "save")):
                score -= 0.35
            if any(w in lowered for w in ("per ", "/month", "month", "shipping")):
                score -= 0.3
            return max(0.0, min(score, 1.0))
        score = min(self.font_size / 34.0, 1.0) * 0.55
        weight = self.font_weight
        if weight in ("bold", "bolder") or (weight.isdigit() and int(weight) >= 600):
            score += 0.16
        if self.y < 1200:
            score += 0.18 * (1 - min(self.y, 1200) / 1200)
        if self.x < 900:
            score += 0.05
        lowered = self.context.lower()
        if any(
            w in lowered
            for w in ("was", "list price", "rrp", "msrp", "save", "you save")
        ):
            score -= 0.22
        if any(
            w in lowered
            for w in ("/month", "per month", "installment", "shipping", "delivery")
        ):
            score -= 0.3
        return max(0.0, min(score, 1.0))

    def describe(self) -> str:
        marks = []
        if self.struck:
            marks.append("crossed out")
        if self.hidden:
            marks.append("hidden")
        if self.screen_reader:
            marks.append("screen-reader text")
        if self.font_size:
            marks.append(f"{self.font_size}px")
        if self.font_weight in ("bold", "bolder") or (
            self.font_weight.isdigit() and int(self.font_weight) >= 600
        ):
            marks.append("bold")
        marks.append(f"at y={self.y}")
        note = f" — {self.context[:60]}" if self.context else ""
        return f"{self.text} [{', '.join(marks)}] via {self.selector}{note}"


@dataclass
class RenderedPage:
    """What a real browser saw."""

    html: str
    url: str
    prices: list[SeenPrice] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    screenshot: str | None = None  # path to a PNG of the page

    def visible_prices(self) -> list[SeenPrice]:
        """Price-like text a shopper could actually read, best-looking first."""
        usable = [p for p in self.prices if not p.hidden and not p.struck]
        return sorted(usable, key=lambda p: -p.prominence)

    def product_image(self) -> str | None:
        return self.images[0]["src"] if self.images else None

    def summarise(self, limit: int = 14) -> str:
        """A compact table for the model - far denser signal than raw markup."""
        lines = []
        for seen in sorted(self.prices, key=lambda p: -p.prominence)[:limit]:
            lines.append(f"- {seen.describe()}")
        return "\n".join(lines) or "- (no price-like text found on the rendered page)"
