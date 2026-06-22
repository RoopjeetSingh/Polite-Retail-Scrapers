"""Extract structured fields + full visible text from a Wayfair product page.

Mirrors the field schema of ``crawler/parse.py`` (Amazon) so the recommender and
store treat every retailer uniformly. The dedup key here is Wayfair's ``sku``
(e.g. ``w004465882``), the analogue of Amazon's ``asin``.

Parsing strategy, most-robust first:
  1. PRIMARY  -- JSON-LD ``<script type="application/ld+json">`` blocks with
     ``@type":"Product"`` (and a separate ``BreadcrumbList`` for categories).
     This is the most stable surface and survives most layout churn.
  2. SECONDARY -- inline application-state JSON Wayfair embeds in <script> tags
     (large objects keyed by sku, ``specifications`` / ``dimensions`` blobs).
     Discovered via regex; coerced best-effort.
  3. TERTIARY -- CSS selectors on the rendered DOM (class names drift, so these
     are last resort).

Every extractor is wrapped in ``_safe`` so one selector/JSON miss can't abort
the whole record. Missing fields are omitted, never written as ``None``.

NOTE on bot protection: Wayfair fronts every page with **PerimeterX / HUMAN
Security** (NOT Imperva/Incapsula). A plain HTTP client (curl / httpx) gets an
HTTP 429 "Access to this page has been denied" interstitial that requires JS
sensor execution + "Press & Hold" challenge to clear. ``looks_blocked()`` and
``CAPTCHA_MARKERS`` below detect that page so the fetch layer can back off.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)


# --- Module interface metadata -----------------------------------------------

DOMAINS = ["wayfair.com"]
ID_FIELD = "sku"

# Wayfair uses PerimeterX (rebranded HUMAN Security), confirmed from live fetch
# attempts: the block page sets window._pxAppId / _pxUuid and renders a
# "Press & Hold to confirm you are a human" PerimeterX captcha.
BOT_VENDOR = "PerimeterX (HUMAN Security)"

# Strings that appear ONLY on Wayfair's PerimeterX block/interstitial page,
# never on a real product page. Used by the fetch layer to detect a block.
CAPTCHA_MARKERS = [
    "Access to this page has been denied",
    "px-captcha",
    "_pxAppId",
    "_pxUuid",
    "PerimeterX",
    "Press & Hold",
    "captcha.px-cloud.net",
    "/sf-ui-perimeterx-block/",
]


# Wayfair product URLs look like:
#   https://www.wayfair.com/<category>/pdp/<slug>-<sku>.html
# where <sku> is an alphanumeric id, almost always "w" + 9 digits
# (e.g. w004465882) but some legacy ids use a different prefix
# (e.g. xqp10667, aece1047, bfff1372, trpt3489). We grab the trailing
# token before ".html".
_SKU_FROM_URL = re.compile(r"/pdp/.*?-([a-z]{2,4}\d{4,})\.html", re.IGNORECASE)
_SKU_GENERIC = re.compile(r"/pdp/.*?-([A-Za-z0-9]{6,})\.html")


def extract_id(url: str) -> Optional[str]:
    """Pull the Wayfair SKU out of a product URL, or return None."""
    if not url:
        return None
    m = _SKU_FROM_URL.search(url)
    if m:
        return m.group(1).lower()
    m = _SKU_GENERIC.search(url)
    if m:
        return m.group(1).lower()
    return None


def looks_blocked(html: str) -> bool:
    """True if the HTML is a PerimeterX block page rather than a real product."""
    if not html:
        return False
    head = html[:4000]
    return any(marker in head for marker in CAPTCHA_MARKERS)


# =============================================================================
# Main entry point
# =============================================================================


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page."""
    tree = HTMLParser(html)
    ld = _safe(_collect_jsonld, html) or {}
    product_ld = ld.get("product") or {}
    breadcrumb_ld = ld.get("breadcrumb") or []
    state = _safe(_collect_state, html) or {}

    out: dict[str, Any] = {}

    sku = (
        extract_id(source_url)
        or _safe(_sku_from_ld, product_ld)
        or _safe(_sku_from_state, state)
        or _safe(_sku_from_html, tree)
    )
    if sku:
        out["sku"] = sku
    canonical = _safe(_canonical_url, tree) or source_url
    if canonical:
        out["url"] = canonical
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, product_ld))
    _put(out, "brand", _safe(_brand, tree, product_ld))

    price = _safe(_price, tree, product_ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, tree, product_ld))
    _put(out, "availability", _safe(_availability, tree, product_ld))
    _put(out, "bullets", _safe(_bullets, tree, state))
    _put(out, "description", _safe(_description, tree, product_ld))
    _put(out, "images", _safe(_images, tree, html, product_ld))
    _put(out, "categories", _safe(_categories, tree, breadcrumb_ld))
    _put(out, "specs", _safe(_specs, tree, state))
    _put(out, "variations", _safe(_variations, tree, state))
    _put(out, "seller", _safe(_seller, product_ld, state))

    # Wayfair-specific home-goods extras (drawn from the specs map when present).
    specs = out.get("specs") or {}
    _put(out, "dimensions", _safe(_dimensions, specs))
    _put(out, "weight", _safe(_pick_spec, specs,
                              ("overall product weight", "weight", "product weight")))
    _put(out, "materials", _safe(_materials, specs))
    _put(out, "assembly_required", _safe(_assembly_required, specs))
    _put(out, "country_of_origin", _safe(_pick_spec, specs,
                                         ("country of origin", "country of manufacture")))
    _put(out, "care_instructions", _safe(_pick_spec, specs,
                                         ("care instructions", "cleaning method",
                                          "care & maintenance")))

    _put(out, "page_text", _safe(_page_text, html))
    return out


# =============================================================================
# Generic helpers (copied to keep this module self-contained)
# =============================================================================


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
    except Exception as e:  # pragma: no cover - defensive
        log.debug("extractor %s failed: %s", getattr(fn, "__name__", fn), e)
        return None


def _text(node) -> str:
    if node is None:
        return ""
    return (node.text(strip=True) if hasattr(node, "text") else "").strip()


_PRICE_RE = re.compile(r"([^\d\s]?)\s*([\d,]+(?:\.\d+)?)")
_CURRENCY_SYM = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "C$": "CAD"}


def _parse_money(s: Any) -> Optional[dict[str, Any]]:
    """Parse '$1,299.99' / '1299.99' / 1299.99 into {amount, currency?}."""
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return {"amount": float(s)}
    s = str(s).strip().replace("\xa0", " ")
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    symbol = m.group(1)
    try:
        amount = float(m.group(2).replace(",", ""))
    except ValueError:
        return None
    currency = _CURRENCY_SYM.get(symbol, "")
    return {"amount": amount, "currency": currency} if currency else {"amount": amount}


# =============================================================================
# JSON-LD collection (PRIMARY source)
# =============================================================================


def _collect_jsonld(html: str) -> dict[str, Any]:
    """Parse all <script type=application/ld+json> blocks.

    Returns {"product": <Product dict>, "breadcrumb": [<crumb str>, ...]}.
    JSON-LD may be a single object, a list, or a @graph wrapper.
    """
    tree = HTMLParser(html)
    product: dict[str, Any] = {}
    breadcrumb: list[str] = []

    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        raw = raw.strip()
        if not raw:
            continue
        data = _loads_lenient(raw)
        if data is None:
            continue
        for obj in _iter_ld_objects(data):
            if not isinstance(obj, dict):
                continue
            t = obj.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types and not product:
                product = obj
            elif "BreadcrumbList" in types and not breadcrumb:
                breadcrumb = _crumbs_from_ld(obj)

    return {"product": product, "breadcrumb": breadcrumb}


def _iter_ld_objects(data: Any):
    """Yield candidate dicts from a JSON-LD payload (object / list / @graph)."""
    if isinstance(data, list):
        for item in data:
            yield from _iter_ld_objects(item)
    elif isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            for item in data["@graph"]:
                yield from _iter_ld_objects(item)
        yield data


def _crumbs_from_ld(obj: dict[str, Any]) -> list[str]:
    out: list[str] = []
    items = obj.get("itemListElement") or []
    if not isinstance(items, list):
        return out
    # Order by position when available.
    def pos(e):
        try:
            return int(e.get("position", 0))
        except (TypeError, ValueError):
            return 0

    for el in sorted(items, key=pos):
        if not isinstance(el, dict):
            continue
        name = el.get("name")
        item = el.get("item")
        if not name and isinstance(item, dict):
            name = item.get("name")
        if isinstance(name, str) and name.strip():
            out.append(name.strip())
    return out


def _loads_lenient(raw: str) -> Any:
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Strip trailing commas, retry.
    cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
    try:
        return json.loads(cleaned)
    except Exception:
        return None


# =============================================================================
# Inline application-state JSON (SECONDARY source)
# =============================================================================

# Wayfair embeds product application state in inline <script> blobs. The exact
# variable names drift, so we capture the most useful sub-objects by key.
_STATE_BLOCK_RES = [
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
    re.compile(r"window\.__APP_DATA__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
    re.compile(r"window\.__PROPS__\s*=\s*(\{.*?\})\s*;\s*</script>", re.DOTALL),
    re.compile(r'application_data\s*[:=]\s*(\{.*?\})\s*[,;]', re.DOTALL),
]

# Specifications usually arrive as a list of {name/specificationName, value}.
_SPEC_LIST_RE = re.compile(
    r'"(?:specifications|productSpecifications|weightsAndDimensions)"\s*:\s*(\[.*?\])',
    re.DOTALL,
)
# At-a-glance / highlights / features bullet arrays.
_HIGHLIGHTS_RE = re.compile(
    r'"(?:atAGlance|at_a_glance|highlights|features|productHighlights)"\s*:\s*(\[[^\[\]]*?\])',
    re.DOTALL,
)
_SKU_RE = re.compile(r'"sku"\s*:\s*"([A-Za-z0-9]{6,})"')
_MFG_RE = re.compile(r'"(?:manufacturer|brandName|brand)"\s*:\s*"([^"]{1,80})"')


def _collect_state(html: str) -> dict[str, Any]:
    """Pull useful sub-structures out of inline app-state JSON, best-effort."""
    state: dict[str, Any] = {}

    # Big state blobs (parse fully if small enough / valid).
    for rx in _STATE_BLOCK_RES:
        m = rx.search(html)
        if m:
            data = _loads_lenient(m.group(1))
            if isinstance(data, dict):
                state.setdefault("_full", data)
                break

    # Specifications list (works even without a fully-parseable blob).
    specs: list[dict[str, str]] = []
    for m in _SPEC_LIST_RE.finditer(html):
        data = _loads_lenient(m.group(1))
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    name = (item.get("name") or item.get("specificationName")
                            or item.get("label") or item.get("key"))
                    val = (item.get("value") or item.get("specificationValue")
                           or item.get("displayValue"))
                    if isinstance(name, str) and val is not None:
                        specs.append({"name": name.strip(), "value": str(val).strip()})
            if specs:
                break
    if specs:
        state["specs"] = specs

    # Highlights / feature bullets.
    bullets: list[str] = []
    for m in _HIGHLIGHTS_RE.finditer(html):
        data = _loads_lenient(m.group(1))
        if isinstance(data, list):
            for item in data:
                if isinstance(item, str) and item.strip():
                    bullets.append(item.strip())
                elif isinstance(item, dict):
                    v = item.get("text") or item.get("value") or item.get("name")
                    if isinstance(v, str) and v.strip():
                        bullets.append(v.strip())
            if bullets:
                break
    if bullets:
        state["bullets"] = bullets

    m = _SKU_RE.search(html)
    if m:
        state["sku"] = m.group(1)
    m = _MFG_RE.search(html)
    if m:
        state["manufacturer"] = m.group(1).strip()

    return state


# =============================================================================
# Field extractors
# =============================================================================


def _canonical_url(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first('link[rel="canonical"]')
    if el is not None:
        href = el.attributes.get("href")
        if href:
            return href.strip()
    el = tree.css_first('meta[property="og:url"]')
    if el is not None:
        c = el.attributes.get("content")
        if c:
            return c.strip()
    return None


def _sku_from_ld(product_ld: dict[str, Any]) -> Optional[str]:
    for key in ("sku", "mpn", "productID"):
        v = product_ld.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    return None


def _sku_from_state(state: dict[str, Any]) -> Optional[str]:
    v = state.get("sku")
    return v.lower() if isinstance(v, str) and v else None


def _sku_from_html(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("[data-sku], [data-enzyme-id='ProductSku']")
    if el is not None:
        v = el.attributes.get("data-sku")
        if v:
            return v.strip().lower()
    return None


def _title(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[str]:
    name = product_ld.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    for sel in ("header > h1", "h1[data-enzyme-id='ProductTitle']", "h1"):
        el = tree.css_first(sel)
        t = _text(el)
        if t:
            return t
    el = tree.css_first('meta[property="og:title"]')
    if el is not None:
        c = (el.attributes.get("content") or "").strip()
        # og:title is often "<name> & Reviews | Wayfair"
        c = re.sub(r"\s*&\s*Reviews\s*\|\s*Wayfair\s*$", "", c)
        c = re.sub(r"\s*\|\s*Wayfair\s*$", "", c)
        if c:
            return c
    return None


def _brand(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[str]:
    b = product_ld.get("brand")
    if isinstance(b, dict):
        n = b.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    if isinstance(b, str) and b.strip():
        return b.strip()
    mfg = product_ld.get("manufacturer")
    if isinstance(mfg, dict) and isinstance(mfg.get("name"), str):
        return mfg["name"].strip()
    if isinstance(mfg, str) and mfg.strip():
        return mfg.strip()
    # DOM fallback: a brand/manufacturer link near the title.
    el = tree.css_first("a[data-enzyme-id='ManufacturerLink'], a.ProductOverviewInformation-manufacturer")
    t = _text(el)
    return t or None


def _price(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[dict[str, Any]]:
    cur: Optional[dict[str, Any]] = None
    currency = ""

    offers = product_ld.get("offers")
    offer_list = offers if isinstance(offers, list) else ([offers] if offers else [])
    prices: list[float] = []
    list_price: Optional[float] = None
    for off in offer_list:
        if not isinstance(off, dict):
            continue
        currency = currency or off.get("priceCurrency") or ""
        # AggregateOffer: lowPrice / highPrice.
        lo = off.get("lowPrice")
        hi = off.get("highPrice")
        p = off.get("price")
        for cand in (p, lo, hi):
            m = _parse_money(cand)
            if m and "amount" in m and m["amount"] > 0:
                prices.append(m["amount"])

    if prices:
        lo, hi = min(prices), max(prices)
        cur = {"amount": lo}
        if currency:
            cur["currency"] = currency
        if hi > lo:
            cur["price_range"] = {"min": lo, "max": hi}

    # DOM fallback for current price.
    if cur is None:
        node = tree.css_first(
            ".SFPrice > div:first-child > span:first-child, "
            ".SFPrice span, "
            "[data-enzyme-id='PriceBlock'] [data-enzyme-id='SalePrice'], "
            "span[data-enzyme-id='PriceDisplay']"
        )
        cur = _parse_money(_text(node))
        if cur and "currency" not in cur:
            cur["currency"] = currency or "USD"

    if cur is None:
        return None

    # List / "was" price -> only a deal if it strictly exceeds current.
    list_node = tree.css_first(
        ".SFPrice del, .SFPrice s, "
        "[data-enzyme-id='ListPrice'], span.ListPrice, "
        ".pl-Price--listPrice, del[data-enzyme-id]"
    )
    lp = _parse_money(_text(list_node)) if list_node is not None else None
    if lp and "amount" in lp and lp["amount"] > cur["amount"]:
        list_price = lp["amount"]
    if list_price is not None:
        cur["list_price"] = list_price

    return cur


def _rating(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    agg = product_ld.get("aggregateRating")
    if isinstance(agg, dict):
        rv = agg.get("ratingValue")
        try:
            stars = float(rv) if rv is not None else None
        except (TypeError, ValueError):
            stars = None
        rc = agg.get("reviewCount") or agg.get("ratingCount")
        try:
            count = int(float(rc)) if rc is not None else None
        except (TypeError, ValueError):
            count = None

    if stars is None:
        el = tree.css_first(".ProductRatingNumberWithCount-rating, [data-enzyme-id='ReviewStars']")
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", _text(el))
        if m:
            stars = float(m.group(1))
    if count is None:
        el = tree.css_first(".ProductRatingNumberWithCount-count, [data-enzyme-id='ReviewCount']")
        m = re.search(r"([\d,]+)", _text(el))
        if m:
            count = int(m.group(1).replace(",", ""))

    if stars is None and count is None:
        return None
    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out


_AVAIL_MAP = {
    "instock": "In Stock",
    "in_stock": "In Stock",
    "outofstock": "Out of Stock",
    "out_of_stock": "Out of Stock",
    "backorder": "Backordered",
    "preorder": "Pre-order",
    "discontinued": "Discontinued",
}


def _availability(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[str]:
    offers = product_ld.get("offers")
    offer_list = offers if isinstance(offers, list) else ([offers] if offers else [])
    for off in offer_list:
        if isinstance(off, dict):
            av = off.get("availability")
            if isinstance(av, str) and av:
                key = av.rsplit("/", 1)[-1].lower()
                return _AVAIL_MAP.get(key, key.replace("_", " ").title() or av)
    el = tree.css_first("[data-enzyme-id='StockStatus'], .ShippingDeliveryAndStock-status")
    t = _text(el)
    return t or None


def _bullets(tree: HTMLParser, state: dict[str, Any]) -> Optional[list[str]]:
    out: list[str] = []
    for b in state.get("bullets") or []:
        if isinstance(b, str) and b.strip() and b.strip() not in out:
            out.append(b.strip())
    if out:
        return out
    # DOM fallback: "Features" / at-a-glance lists.
    for sel in (
        "[data-enzyme-id='AtAGlance'] li",
        ".ProductOverviewInformation-features li",
        ".ProductWeightsDimensions-attribute li",
        "ul[data-enzyme-id='Highlights'] li",
    ):
        for li in tree.css(sel):
            t = _text(li)
            if t and t not in out:
                out.append(t)
        if out:
            break
    return out or None


def _description(tree: HTMLParser, product_ld: dict[str, Any]) -> Optional[str]:
    d = product_ld.get("description")
    if isinstance(d, str) and d.strip():
        return _clean_html_text(d.strip())
    for sel in (
        ".ProductOverviewInformation-description",
        "[data-enzyme-id='ProductDescription']",
        "section[data-enzyme-id='Overview']",
    ):
        el = tree.css_first(sel)
        t = _text(el)
        if t:
            return t
    el = tree.css_first('meta[name="description"]')
    if el is not None:
        c = (el.attributes.get("content") or "").strip()
        if c and "px-captcha" not in c:
            return c
    return None


def _clean_html_text(s: str) -> str:
    """JSON-LD descriptions sometimes carry HTML; strip tags + collapse space."""
    if "<" in s and ">" in s:
        try:
            s = lxml_html.fromstring(s).text_content()
        except Exception:
            s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _images(tree: HTMLParser, html: str, product_ld: dict[str, Any]) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    img = product_ld.get("image")
    if isinstance(img, str):
        main.append(img)
    elif isinstance(img, list):
        for u in img:
            if isinstance(u, str) and u not in main:
                main.append(u)
            elif isinstance(u, dict):
                uu = u.get("url") or u.get("contentUrl")
                if isinstance(uu, str) and uu not in main:
                    main.append(uu)
    elif isinstance(img, dict):
        uu = img.get("url") or img.get("contentUrl")
        if isinstance(uu, str):
            main.append(uu)

    # og:image fallback.
    if not main:
        for el in tree.css('meta[property="og:image"]'):
            c = (el.attributes.get("content") or "").strip()
            if c and c not in main:
                main.append(c)

    # DOM thumbnails / carousel images (wfcdn hosts).
    for el in tree.css(
        "img[data-enzyme-id='ProductThumbnail'], "
        ".ProductDetailImageThumbnail img, "
        ".ImageComponent img, "
        "img[src*='wfcdn.com'], img[data-src*='wfcdn.com']"
    ):
        src = el.attributes.get("src") or el.attributes.get("data-src") or ""
        if src and src not in thumbs:
            thumbs.append(src)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    return result or None


def _categories(tree: HTMLParser, breadcrumb_ld: list[str]) -> Optional[list[str]]:
    crumbs = [c for c in (breadcrumb_ld or []) if isinstance(c, str) and c.strip()]
    # Drop a leading generic "Wayfair"/"Home" root if present, keep the path.
    crumbs = [c for c in crumbs if c.lower() not in ("wayfair", "wayfair.com")]
    if crumbs:
        return crumbs
    # DOM fallback.
    dom = [
        _text(a)
        for a in tree.css(
            "nav[aria-label='Breadcrumb'] a, "
            "ol[data-enzyme-id='Breadcrumbs'] a, "
            ".Breadcrumbs a"
        )
    ]
    dom = [c for c in dom if c and c.lower() not in ("wayfair", "wayfair.com")]
    return dom or None


def _specs(tree: HTMLParser, state: dict[str, Any]) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for item in state.get("specs") or []:
        if isinstance(item, dict):
            k = item.get("name")
            v = item.get("value")
            if isinstance(k, str) and k and v and k not in out:
                out[k] = str(v)
    if out:
        return out
    # DOM fallback: Specifications / Weights & Dimensions tables.
    for row_sel in (
        "[data-enzyme-id='Specifications'] tr",
        ".ProductWeightsDimensions tr",
        ".Specifications-table tr",
        "table.ProductSpecifications tr",
    ):
        for row in tree.css(row_sel):
            cells = row.css("th, td")
            if len(cells) >= 2:
                k = _text(cells[0]).rstrip(":").strip()
                v = _text(cells[1])
                if k and v and k not in out:
                    out[k] = v
    # dt/dd definition-list layout.
    if not out:
        for dl in tree.css("dl[data-enzyme-id='Specifications'], dl.Specifications"):
            dts = dl.css("dt")
            dds = dl.css("dd")
            for dt, dd in zip(dts, dds):
                k = _text(dt).rstrip(":").strip()
                v = _text(dd)
                if k and v and k not in out:
                    out[k] = v
    return out or None


def _variations(tree: HTMLParser, state: dict[str, Any]) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    full = state.get("_full")
    if isinstance(full, dict):
        opts = _deep_find(full, "options") or _deep_find(full, "variations")
        if isinstance(opts, list):
            for opt in opts:
                if not isinstance(opt, dict):
                    continue
                label = (opt.get("name") or opt.get("category")
                         or opt.get("optionCategory"))
                choices = opt.get("options") or opt.get("values") or opt.get("choices")
                vals: list[str] = []
                if isinstance(choices, list):
                    for c in choices:
                        if isinstance(c, str):
                            vals.append(c)
                        elif isinstance(c, dict):
                            n = c.get("name") or c.get("label") or c.get("value")
                            if isinstance(n, str):
                                vals.append(n)
                if isinstance(label, str) and vals:
                    out[label.lower()] = _dedup(vals)
    if out:
        return out
    # DOM fallback: option swatch groups.
    for group in tree.css("[data-enzyme-id='OptionSelect'], .OptionSelect, fieldset.ProductOptions"):
        label_el = group.css_first("legend, label, .OptionSelect-label")
        label = _text(label_el).rstrip(":").strip()
        vals: list[str] = []
        for opt in group.css("button[title], li[title], option[value], [data-option-name]"):
            v = (opt.attributes.get("title") or opt.attributes.get("data-option-name")
                 or _text(opt))
            v = (v or "").strip()
            if v and v not in vals:
                vals.append(v)
        if label and vals:
            out[label.lower()] = vals
    return out or None


def _seller(product_ld: dict[str, Any], state: dict[str, Any]) -> Optional[str]:
    offers = product_ld.get("offers")
    offer_list = offers if isinstance(offers, list) else ([offers] if offers else [])
    for off in offer_list:
        if isinstance(off, dict):
            sb = off.get("seller")
            if isinstance(sb, dict) and isinstance(sb.get("name"), str):
                name = sb["name"].strip()
                if name and name.lower() != "wayfair":
                    return name
    return None


# --- Wayfair-specific home-goods extras --------------------------------------


def _norm_key(k: str) -> str:
    return re.sub(r"\s+", " ", (k or "").strip().lower())


def _pick_spec(specs: dict[str, str], keys: tuple[str, ...]) -> Optional[str]:
    norm = {_norm_key(k): v for k, v in specs.items()}
    for want in keys:
        if want in norm and norm[want]:
            return norm[want]
    # Substring match as a softer fallback.
    for nk, v in norm.items():
        if any(want in nk for want in keys) and v:
            return v
    return None


_DIM_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(?:"|\bin(?:ches)?\b|\bw\b|\bd\b|\bh\b)?', re.IGNORECASE
)


def _dimensions(specs: dict[str, str]) -> Optional[dict[str, str]]:
    norm = {_norm_key(k): v for k, v in specs.items()}
    out: dict[str, str] = {}
    mapping = {
        "width": ("overall width - side to side", "overall width", "width"),
        "depth": ("overall depth - front to back", "overall depth", "depth"),
        "height": ("overall height - top to bottom", "overall height", "height"),
        "length": ("overall length", "length"),
    }
    for dim, keys in mapping.items():
        for want in keys:
            if want in norm and norm[want]:
                out[dim] = norm[want]
                break
    # Single combined "Overall" field e.g. "30''H x 60''W x 35''D".
    if not out:
        for k in ("overall product dimensions", "overall", "dimensions",
                  "overall dimensions"):
            if k in norm and norm[k]:
                out["overall"] = norm[k]
                break
    return out or None


def _materials(specs: dict[str, str]) -> Optional[list[str]]:
    norm = {_norm_key(k): v for k, v in specs.items()}
    vals: list[str] = []
    for nk, v in norm.items():
        if not v:
            continue
        if nk == "material" or nk.endswith(" material") or "primary material" in nk:
            for part in re.split(r"[,/;]| and ", v):
                part = part.strip()
                if part and part not in vals:
                    vals.append(part)
    return vals or None


def _assembly_required(specs: dict[str, str]) -> Optional[bool]:
    v = _pick_spec(specs, ("assembly required", "adult assembly required",
                           "assembly"))
    if v is None:
        return None
    low = v.strip().lower()
    if low in ("yes", "true", "required"):
        return True
    if low in ("no", "false", "none", "no assembly required"):
        return False
    if "yes" in low:
        return True
    if "no" in low:
        return False
    return None


def _deep_find(obj: Any, key: str, _depth: int = 0) -> Any:
    """Breadth-limited search for the first value at any dict key == ``key``."""
    if _depth > 8:
        return None
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            found = _deep_find(v, key, _depth + 1)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = _deep_find(v, key, _depth + 1)
            if found is not None:
                return found
    return None


def _dedup(seq: list[str]) -> list[str]:
    out: list[str] = []
    for s in seq:
        if s and s not in out:
            out.append(s)
    return out


# =============================================================================
# Full visible page text (copied verbatim from crawler/parse.py)
# =============================================================================


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
