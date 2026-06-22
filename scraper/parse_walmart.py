"""Extract structured fields + full visible text from a Walmart product page.

Mirrors the field schema of ``crawler/parse.py`` (the Amazon parser) so the
downstream embedding / deal-detection layers see a uniform record shape across
retailers. The dedup key on Walmart is the numeric ``item_id`` (their usItemId),
analogous to Amazon's ``asin``.

Parsing strategy (most robust first):
  1. PRIMARY  — ``<script id="__NEXT_DATA__">`` JSON blob. Walmart server-side
     renders a giant Next.js payload at
     ``props.pageProps.initialData.data`` with sub-objects ``product``,
     ``idml`` (specs / long description / highlights / warnings) and
     ``reviews``. Nearly every field lives here.
  2. SECONDARY — JSON-LD ``<script type="application/ld+json">`` with
     ``@type":"Product"``. (Observed Walmart pages currently only emit a
     ``WebPage`` LD node, but the Product branch is kept as a defensive
     fallback in case that changes / for partial pages.)
  3. TERTIARY — CSS selectors on the rendered DOM (``<title>``, og:* meta,
     breadcrumb list) for the handful of fields that survive in markup.

Every field extractor is wrapped in ``_safe`` so one selector miss / shifted
JSON path can never abort the whole row. Empty values are dropped via ``_put``.
"""
from __future__ import annotations

import html as _htmllib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)


# --- Module interface (consumed by the generic crawler) ----------------------

DOMAINS = ["walmart.com"]
ID_FIELD = "item_id"
BOT_VENDOR = "PerimeterX"  # Walmart fronts product pages with PerimeterX / HUMAN.

# Strings that appear ONLY on Walmart's block / "Robot or human?" challenge page,
# never on a real rendered product page. (A real product page does mention
# "px-captcha" deep inside bundled JS, so we deliberately anchor on the visible
# interstitial copy + the captcha asset host rather than that bundle token.)
CAPTCHA_MARKERS = [
    "Robot or human?",
    "Verify you are a human",
    "px-captcha",
    "captcha.px-cdn.net",
    "/_px/",
    "block-bot",
    "Activate and hold the button to confirm",
]

# Walmart product URLs: https://www.walmart.com/ip/<slug>/<numeric-item-id>
# Some have no slug: /ip/16005460596. Trailing query / fragment allowed.
_ID_RE = re.compile(r"/ip/(?:[^/]+/)?(\d{4,})(?:[/?#]|$)")
_ID_BARE = re.compile(r"^\d{4,}$")

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_id(url: str) -> Optional[str]:
    """Return the numeric item id from a Walmart product URL, or None."""
    if not url:
        return None
    m = _ID_RE.search(url)
    if m:
        return m.group(1)
    return None


# --- Top-level entrypoint -----------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page.

    Missing fields are omitted rather than included as None. Every individual
    extractor is wrapped so a selector / JSON-path miss can't abort the row.
    """
    tree = HTMLParser(html)
    nd = _safe(_next_data_product, html) or {}
    idml = _safe(_next_data_idml, html) or {}
    reviews = _safe(_next_data_reviews, html) or {}
    ld = _safe(_ld_product, html) or {}

    out: dict[str, Any] = {}

    item_id = (
        extract_id(source_url)
        or (str(nd.get("usItemId")) if nd.get("usItemId") else None)
        or (str(ld.get("sku")) if ld.get("sku") else None)
    )
    if item_id:
        out["item_id"] = item_id
        # canonicalUrl is a relative path like "/ip/.../<id>" when present.
        canon = nd.get("canonicalUrl")
        if canon and canon.startswith("/"):
            out["url"] = f"https://www.walmart.com{canon}"
        else:
            out["url"] = f"https://www.walmart.com/ip/{item_id}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, nd, ld, tree))
    _put(out, "brand", _safe(_brand, nd, ld))

    price = _safe(_price, nd, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, nd, reviews, ld))
    _put(out, "availability", _safe(_availability, nd, ld))
    _put(out, "bullets", _safe(_bullets, nd, idml))
    _put(out, "description", _safe(_description, nd, idml, ld))
    _put(out, "images", _safe(_images, nd, ld))
    _put(out, "categories", _safe(_categories, nd, tree))
    _put(out, "specs", _safe(_specs, idml))
    _put(out, "variations", _safe(_variations, nd))
    _put(out, "seller", _safe(_seller, nd))
    _put(out, "page_text", _safe(_page_text, html))

    # --- Walmart-specific extras (info Amazon doesn't surface here) ----------
    _put(out, "model", _safe(lambda: _nz(nd.get("model"))))
    _put(out, "upc", _safe(lambda: _nz(nd.get("upc"))))
    _put(out, "warnings", _safe(_warnings, idml))

    return out


# --- Generic helpers (copied from parse.py to keep this module self-contained) -


def _put(d: dict[str, Any], key: str, value: Any) -> None:
    """Insert key only if value is meaningful (non-empty)."""
    if value is None:
        return
    if isinstance(value, (str, list, dict)) and not value:
        return
    d[key] = value


def _safe(fn, *args):
    try:
        return fn(*args)
    except Exception as e:
        log.debug("extractor %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _text(node) -> str:
    if node is None:
        return ""
    return (node.text(strip=True) if hasattr(node, "text") else "").strip()


def _nz(v: Any) -> Optional[str]:
    """Coerce to a clean non-empty string (HTML entities decoded, ws collapsed)."""
    if v is None:
        return None
    s = _htmllib.unescape(str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _strip_tags(s: str) -> str:
    """Walmart long descriptions are HTML fragments; flatten to plain text."""
    if not s:
        return ""
    try:
        txt = lxml_html.fromstring(s).text_content()
    except Exception:
        txt = _htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


_CURRENCY_MAP = {"USD": "USD", "$": "USD", "GBP": "GBP", "£": "GBP", "EUR": "EUR", "CAD": "CAD"}


# --- __NEXT_DATA__ / JSON-LD locators ----------------------------------------


def _next_data_root(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except Exception:
        return {}
    try:
        return data["props"]["pageProps"]["initialData"]["data"] or {}
    except Exception:
        return {}


def _next_data_product(html: str) -> dict[str, Any]:
    return _next_data_root(html).get("product") or {}


def _next_data_idml(html: str) -> dict[str, Any]:
    return _next_data_root(html).get("idml") or {}


def _next_data_reviews(html: str) -> dict[str, Any]:
    return _next_data_root(html).get("reviews") or {}


def _ld_product(html: str) -> dict[str, Any]:
    """Return the first JSON-LD node whose @type is Product, else {}."""
    for blob in _LD_JSON_RE.findall(html):
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            t = c.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Product" in t:
                return c
    return {}


# --- Field extractors ---------------------------------------------------------


def _title(nd: dict, ld: dict, tree: HTMLParser) -> Optional[str]:
    return (
        _nz(nd.get("name"))
        or _nz(ld.get("name"))
        or _nz(_text(tree.css_first("h1[itemprop='name'], h1#main-title")))
        or _title_from_meta(tree)
    )


def _title_from_meta(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("meta[property='og:title']")
    if el is not None:
        v = (el.attributes.get("content") or "").strip()
        # og:title often ends with " - Walmart.com"
        v = re.sub(r"\s*-\s*Walmart\.com\s*$", "", v)
        if v:
            return v
    el = tree.css_first("title")
    if el is not None:
        v = _text(el)
        v = re.sub(r"\s*-\s*Walmart\.com\s*$", "", v)
        return v or None
    return None


def _brand(nd: dict, ld: dict) -> Optional[str]:
    b = nd.get("brand")
    if isinstance(b, dict):  # defensive — sometimes a {name: ...}
        b = b.get("name")
    if _nz(b):
        return _nz(b)
    lb = ld.get("brand")
    if isinstance(lb, dict):
        lb = lb.get("name")
    return _nz(lb)


def _money_from_node(node: Any) -> Optional[dict[str, Any]]:
    """``{price, currencyUnit}`` style node -> ``{amount, currency}``."""
    if not isinstance(node, dict):
        return None
    amt = node.get("price")
    if amt is None:
        return None
    try:
        amount = float(amt)
    except (TypeError, ValueError):
        return None
    cur = _CURRENCY_MAP.get(node.get("currencyUnit") or "", "")
    out: dict[str, Any] = {"amount": amount}
    if cur:
        out["currency"] = cur
    return out


def _price(nd: dict, ld: dict) -> Optional[dict[str, Any]]:
    pi = nd.get("priceInfo") or {}

    cur = _money_from_node(pi.get("currentPrice"))

    # Fallback: JSON-LD offers.price
    if not cur:
        offers = ld.get("offers")
        offers = offers[0] if isinstance(offers, list) and offers else offers
        if isinstance(offers, dict) and offers.get("price") is not None:
            try:
                amount = float(offers["price"])
                cur = {"amount": amount}
                c = _CURRENCY_MAP.get(offers.get("priceCurrency") or "", "")
                if c:
                    cur["currency"] = c
            except (TypeError, ValueError):
                cur = None

    if not cur:
        return None

    cur.setdefault("currency", "USD")

    # Deal signal: struck-through "was" price greater than the current price.
    was = _money_from_node(pi.get("wasPrice"))
    list_p = _money_from_node(pi.get("listPrice")) if isinstance(pi.get("listPrice"), dict) else None
    candidate = None
    if was and was["amount"] > cur["amount"]:
        candidate = was["amount"]
    if list_p and list_p["amount"] > cur["amount"]:
        candidate = max(candidate or 0, list_p["amount"])
    if candidate:
        cur["list_price"] = candidate

    # Variant price range (e.g. clothing with size-priced variants).
    pr = pi.get("priceRange") or {}
    lo, hi = pr.get("minPrice"), pr.get("maxPrice")
    try:
        if lo is not None and hi is not None:
            lo_f, hi_f = float(lo), float(hi)
            if hi_f > lo_f:
                cur["price_range"] = {"min": lo_f, "max": hi_f}
                # keep canonical price as the lowest available
                if lo_f < cur["amount"]:
                    cur["amount"] = lo_f
    except (TypeError, ValueError):
        pass

    return cur


def _rating(nd: dict, reviews: dict, ld: dict) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    if nd.get("averageRating") is not None:
        try:
            stars = float(nd["averageRating"])
        except (TypeError, ValueError):
            stars = None
    if nd.get("numberOfReviews") is not None:
        try:
            count = int(nd["numberOfReviews"])
        except (TypeError, ValueError):
            count = None

    if stars is None and reviews.get("averageOverallRating") is not None:
        try:
            stars = float(reviews["averageOverallRating"])
        except (TypeError, ValueError):
            pass
    if count is None and reviews.get("totalReviewCount") is not None:
        try:
            count = int(reviews["totalReviewCount"])
        except (TypeError, ValueError):
            pass

    # JSON-LD fallback
    agg = ld.get("aggregateRating") or {}
    if isinstance(agg, dict):
        if stars is None and agg.get("ratingValue") is not None:
            try:
                stars = float(agg["ratingValue"])
            except (TypeError, ValueError):
                pass
        if count is None and (agg.get("reviewCount") or agg.get("ratingCount")) is not None:
            try:
                count = int(agg.get("reviewCount") or agg.get("ratingCount"))
            except (TypeError, ValueError):
                pass

    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out or None


_AVAIL_MAP = {
    "IN_STOCK": "In stock",
    "OUT_OF_STOCK": "Out of stock",
    "RETIRED": "Retired",
    "UNAVAILABLE": "Unavailable",
    "PRE_ORDER": "Pre-order",
    "PREORDER": "Pre-order",
}


def _availability(nd: dict, ld: dict) -> Optional[str]:
    raw = nd.get("availabilityStatus") or nd.get("itemPageAvailabilityStatus")
    if raw:
        return _AVAIL_MAP.get(raw, raw.replace("_", " ").title())
    offers = ld.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offers, dict) and offers.get("availability"):
        v = str(offers["availability"]).rsplit("/", 1)[-1]
        return {"InStock": "In stock", "OutOfStock": "Out of stock"}.get(v, v)
    return None


def _bullets(nd: dict, idml: dict) -> Optional[list[str]]:
    """Walmart's "About this item" / highlights -> bullets list."""
    out: list[str] = []

    # productHighlights: list of {name, value, iconURL} feature chips.
    for h in idml.get("productHighlights") or []:
        if not isinstance(h, dict):
            continue
        name = _nz(h.get("name"))
        val = _nz(h.get("value"))
        if name and val:
            out.append(f"{name}: {val}")
        elif val:
            out.append(val)

    # shortDescription is often a bulleted HTML fragment ("About this item").
    sd = nd.get("shortDescription") or idml.get("shortDescription")
    if sd:
        # Split HTML fragment into <li>/<p>/<br> chunks before stripping tags.
        chunks = re.split(r"</li>|</p>|<br\s*/?>", sd, flags=re.IGNORECASE)
        for ch in chunks:
            t = _strip_tags(ch)
            if t and len(t) > 2 and t not in out:
                out.append(t)

    return out or None


def _description(nd: dict, idml: dict, ld: dict) -> Optional[str]:
    for raw in (
        idml.get("longDescription"),
        nd.get("longDescription"),
        nd.get("shortDescription"),
        idml.get("shortDescription"),
        ld.get("description"),
    ):
        t = _strip_tags(raw) if raw else ""
        if t:
            return t
    return None


def _images(nd: dict, ld: dict) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []
    variants: dict[str, list[str]] = {}

    ii = nd.get("imageInfo") or {}
    for img in ii.get("allImages") or []:
        if not isinstance(img, dict):
            continue
        u = _nz(img.get("url"))
        if u and u not in main:
            main.append(u)
    thumb = _nz(ii.get("thumbnailUrl"))
    if thumb:
        thumbs.append(thumb)

    # Per-variant swatch images from variantCriteria.
    for crit in nd.get("variantCriteria") or []:
        if not isinstance(crit, dict):
            continue
        for v in crit.get("variantList") or []:
            if not isinstance(v, dict):
                continue
            name = _nz(v.get("name"))
            urls: list[str] = []
            sw = _nz(v.get("swatchImageUrl"))
            if sw:
                urls.append(sw)
            for vi in v.get("images") or []:
                if isinstance(vi, dict):
                    u = _nz(vi.get("url"))
                    if u and u not in urls:
                        urls.append(u)
                elif isinstance(vi, str) and vi not in urls:
                    urls.append(vi)
            if name and urls:
                variants[name] = urls

    # JSON-LD image fallback
    if not main:
        imgs = ld.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list):
            for u in imgs:
                if isinstance(u, str) and u not in main:
                    main.append(u)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


def _categories(nd: dict, tree: HTMLParser) -> Optional[list[str]]:
    cat = nd.get("category") or {}
    crumbs = [
        _nz(p.get("name"))
        for p in (cat.get("path") or [])
        if isinstance(p, dict)
    ]
    crumbs = [c for c in crumbs if c]
    if crumbs:
        return crumbs

    # DOM fallback: breadcrumb list.
    dom = [
        _text(a)
        for a in tree.css("nav[aria-label='breadcrumb'] a, ol.breadcrumb a, li.breadcrumb a")
    ]
    dom = [c for c in dom if c]
    return dom or None


def _specs(idml: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for s in idml.get("specifications") or []:
        if not isinstance(s, dict):
            continue
        k = _nz(s.get("name"))
        v = _nz(s.get("value"))
        if k and v and k not in out:
            out[k] = v
    return out or None


def _variations(nd: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    for crit in nd.get("variantCriteria") or []:
        if not isinstance(crit, dict):
            continue
        label = _nz(crit.get("name")) or _nz(crit.get("id"))
        if not label:
            continue
        values: list[str] = []
        for v in crit.get("variantList") or []:
            if not isinstance(v, dict):
                continue
            name = _nz(v.get("name"))
            if name and name not in values:
                values.append(name)
        if values:
            out[label.lower()] = values
    return out or None


def _seller(nd: dict) -> Optional[str]:
    """Marketplace seller name, omitted when sold by Walmart itself."""
    name = _nz(nd.get("sellerDisplayName")) or _nz(nd.get("sellerName"))
    if not name:
        return None
    if name.lower() in ("walmart.com", "walmart"):
        return None
    return name


def _warnings(idml: dict) -> Optional[str]:
    """Safety warnings (choking / Prop 65 / etc.). Useful for content filtering.

    Walmart's ``idml.warnings`` is a list of ``{name, value}`` dicts; we join
    them into one readable string. (Defensively also handles an HTML-string
    shape in case the payload changes.)
    """
    w = idml.get("warnings")
    if not w:
        return None
    if isinstance(w, list):
        parts = []
        for item in w:
            if not isinstance(item, dict):
                t = _strip_tags(str(item))
                if t:
                    parts.append(t)
                continue
            name = _nz(item.get("name"))
            val = _strip_tags(item.get("value") or "")
            if name and val:
                parts.append(f"{name}: {val}")
            elif val:
                parts.append(val)
            elif name:
                parts.append(name)
        return " | ".join(parts) or None
    return _strip_tags(w) or None


# --- Full visible page text (verbatim from parse.py — site-agnostic) ---------


def _page_text(html: str) -> Optional[str]:
    """Return the full visible text of the page with scripts/styles/nav stripped."""
    doc = lxml_html.fromstring(html)
    for el in doc.xpath("//script | //style | //noscript | //nav | //header | //footer"):
        el.getparent().remove(el)
    text = doc.text_content()
    # Collapse whitespace; the raw text has tons of indentation noise.
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip() or None
