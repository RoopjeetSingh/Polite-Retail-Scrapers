"""Extract structured fields + full visible text from a Nordstrom Rack product page.

Self-contained module — copies the small helpers it needs from crawler/parse.py
rather than importing, so it can be moved/reused independently.

Module interface (mirrors what the generic crawler expects per-retailer):
    DOMAINS           list of hostnames this parser handles
    ID_FIELD          name of the dedup-key field in the output record
    BOT_VENDOR        anti-bot vendor in front of the site
    CAPTCHA_MARKERS   strings that appear ONLY on the block/challenge page
    extract_id(url)   -> product id (the dedup key) or None
    parse_product(html, source_url) -> dict

Parsing strategy, most robust first:
  1. JSON-LD  <script type="application/ld+json"> with @type=="Product"
     (name, brand, image, description, sku, offers.price/priceCurrency/
      availability, aggregateRating). Used by Nordstrom/Nordstrom Rack for SEO.
  2. Embedded app-state JSON in inline <script> (__INITIAL_STATE__ /
     __INITIAL_CONFIG__ / __PRELOADED_STATE__ / a product "styleModel" blob with
     price, originalPrice, media, skus, color/size filters).
  3. CSS selectors on the rendered DOM (last resort — class names change often,
     so we match on stable data-* attributes and itemprop where possible).

Every field extractor is wrapped in _safe() so one selector miss can't abort the
whole record. Missing fields are omitted (never written as null).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)

# --- Module metadata ---------------------------------------------------------

DOMAINS = ["nordstromrack.com"]
ID_FIELD = "product_id"

# Nordstrom Rack sits behind Akamai Bot Manager. A plain httpx/curl request gets
# HTTP 200 with a ~250 KB obfuscated JS proof-of-work shell (empty <title>, no
# product data) instead of the real page. The PoW script sets the `_abck` /
# `bm_sz` cookies only after executing in a real JS engine, so headless fetching
# without a browser/JS runtime cannot get past it.
BOT_VENDOR = "Akamai"  # Akamai Bot Manager (confirmed from live fetch attempts)

# Strings that appear ONLY on the Akamai challenge/interstitial shell, never on a
# real rendered product page. Used by the fetch layer to detect a block.
CAPTCHA_MARKERS = [
    "istlWasHere",                 # window['istlWasHere']=true at top of PoW shell
    "X-Akamai-Edgescape",          # Akamai edge headers echoed inside the shell JS
    "X-Akamai-Device-Characteristics",
    "_abck",                       # Akamai bot cookie referenced by the challenge
    "bm-verify=",                  # Akamai verification token
    "/_sec/verify",                # Akamai verification endpoint
    "siteclosed.nordstromrack.com",  # bot-rate-limit redirect to invitation page
    "We've noticed some unusual activity",  # invitation page bot-detection message
]

# Nordstrom Rack product URLs: .../s/<slug>/<product_id>[?...]
# The id is the trailing numeric (occasionally alnum) path segment after the slug.
_ID_RE = re.compile(r"/s/[^/]+/([A-Za-z0-9]+)(?:[/?#]|$)")
# Fallback: any trailing numeric segment of the path.
_ID_TAIL_RE = re.compile(r"/(\d{5,})(?:[/?#]|$)")


def extract_id(url: str) -> Optional[str]:
    """Extract the Nordstrom Rack product id from a product URL."""
    if not url:
        return None
    path = urlparse(url).path
    m = _ID_RE.search(path)
    if m:
        return m.group(1)
    m = _ID_TAIL_RE.search(path)
    if m:
        return m.group(1)
    return None


# --- Public entrypoint -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page.

    Missing fields are omitted rather than included as None.
    """
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    state = _safe(_app_state_product, html) or {}

    out: dict[str, Any] = {}

    pid = (
        extract_id(source_url)
        or _safe(_id_from_ld_state, ld, state)
        or _safe(_id_from_canonical, tree)
    )
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.nordstromrack.com/s/-/{pid}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, ld, state))
    _put(out, "brand", _safe(_brand, tree, ld, state))

    price = _safe(_price, tree, ld, state)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range
        # Discount percent is a strong deal signal for a discount-first retailer.
        if price.get("list_price") and price.get("amount"):
            try:
                pct = round((1 - price["amount"] / price["list_price"]) * 100)
                if pct > 0:
                    out["discount_percent"] = pct
            except (TypeError, ZeroDivisionError):
                pass

    _put(out, "rating", _safe(_rating, tree, ld, state))
    _put(out, "availability", _safe(_availability, tree, ld, state))
    _put(out, "bullets", _safe(_bullets, tree, state))
    _put(out, "description", _safe(_description, tree, ld, state))
    _put(out, "images", _safe(_images, tree, ld, state))
    _put(out, "categories", _safe(_breadcrumb, tree, state))
    _put(out, "specs", _safe(_specs, tree, state))
    _put(out, "variations", _safe(_variations, tree, state))
    _put(out, "seller", _safe(_seller, ld, state))

    # Fashion/apparel extras Amazon lacks.
    _put(out, "size_and_fit", _safe(_size_and_fit, tree, state))
    _put(out, "material", _safe(_material, tree, state))
    _put(out, "care", _safe(_care, tree, state))

    _put(out, "page_text", _safe(_page_text, html))

    return out


# --- Generic helpers (copied from crawler/parse.py) --------------------------


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
        log.debug("extractor %s failed: %s", fn.__name__, e)
        return None


def _text(node) -> str:
    if node is None:
        return ""
    return (node.text(strip=True) if hasattr(node, "text") else "").strip()


_PRICE_RE = re.compile(r"([^\d\s]?)\s*([\d,]+(?:\.\d+)?)")


def _parse_money(s: str) -> Optional[dict[str, Any]]:
    s = (s or "").strip().replace("\xa0", " ")
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    symbol = m.group(1)
    amount = float(m.group(2).replace(",", ""))
    currency = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}.get(symbol, "")
    return {"amount": amount, "currency": currency} if currency else {"amount": amount}


def _to_float(v: Any) -> Optional[float]:
    """Coerce a price-ish value (number or string like '$129.95') to float."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        m = re.search(r"[\d,]+(?:\.\d+)?", v)
        if m:
            try:
                return float(m.group(0).replace(",", ""))
            except ValueError:
                return None
    return None


# --- JSON-LD extraction ------------------------------------------------------


def _iter_jsonld(html: str):
    """Yield every parsed JSON-LD object found in the page (handles @graph)."""
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = node.text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some sites emit slightly malformed JSON-LD; try a lenient cleanup.
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            try:
                data = json.loads(cleaned)
            except Exception:
                continue
        # Normalize: list, @graph wrapper, or a single object.
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and isinstance(data.get("@graph"), list):
            items = data["@graph"]
        else:
            items = [data]
        for it in items:
            if isinstance(it, dict):
                yield it


def _jsonld_product(html: str) -> Optional[dict[str, Any]]:
    """Return the first JSON-LD object whose @type is (or includes) 'Product'."""
    for it in _iter_jsonld(html):
        t = it.get("@type")
        types = t if isinstance(t, list) else [t]
        if any(isinstance(x, str) and x.lower() == "product" for x in types):
            return it
    return None


def _first_offer(ld: dict[str, Any]) -> dict[str, Any]:
    offers = ld.get("offers")
    if isinstance(offers, list):
        return offers[0] if offers and isinstance(offers[0], dict) else {}
    if isinstance(offers, dict):
        return offers
    return {}


# --- Embedded app-state extraction -------------------------------------------

_STATE_VAR_RE = [
    re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;</script>", re.DOTALL),
    re.compile(r"window\.__INITIAL_CONFIG__\s*=\s*(\{.*?\})\s*;</script>", re.DOTALL),
    re.compile(r"window\.__PRELOADED_STATE__\s*=\s*(\{.*?\})\s*;</script>", re.DOTALL),
    re.compile(r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;", re.DOTALL),
    re.compile(r"__INITIAL_CONFIG__\s*=\s*(\{.*?\})\s*;", re.DOTALL),
]


def _app_state(html: str) -> Optional[dict[str, Any]]:
    """Pull the embedded app-state JSON blob, if present."""
    for rx in _STATE_VAR_RE:
        m = rx.search(html)
        if not m:
            continue
        blob = m.group(1)
        data = _loads_balanced(html, m.start(1)) or _loads(blob)
        if isinstance(data, dict):
            return data
    return None


def _loads(s: str) -> Any:
    try:
        return json.loads(s)
    except Exception:
        return None


def _loads_balanced(html: str, start: int) -> Any:
    """Parse a JSON object starting at `start` ('{') by brace-matching.

    The greedy/lazy regexes above can clip nested objects; this walks the string
    to find the matching closing brace so we get the full state object.
    """
    if start >= len(html) or html[start] != "{":
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(html)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return _loads(html[start : i + 1])
    return None


def _app_state_product(html: str) -> Optional[dict[str, Any]]:
    """Find the product/style model inside the app state.

    Nordstrom-family state nests the product under keys like
    `productModel` / `styleModel` / `product`. We search recursively for the
    first dict that looks like a product (has a price-ish + name-ish key).
    """
    state = _app_state(html)
    if not isinstance(state, dict):
        return None
    found = _find_product_dict(state, depth=0)
    return found


_PRODUCT_KEYS = ("price", "originalPrice", "currentPrice", "skus", "media", "styleId")
_NAME_KEYS = ("name", "title", "displayName", "productName", "brandName", "brand")


def _find_product_dict(obj: Any, depth: int) -> Optional[dict[str, Any]]:
    if depth > 8 or not isinstance(obj, dict):
        if isinstance(obj, list):
            for v in obj[:50]:
                r = _find_product_dict(v, depth + 1)
                if r:
                    return r
        return None
    keys = set(obj.keys())
    has_price = any(k in keys for k in _PRODUCT_KEYS)
    has_name = any(k in keys for k in _NAME_KEYS)
    if has_price and has_name:
        return obj
    # Prefer obvious container keys first.
    for k in ("product", "productModel", "styleModel", "currentProduct", "pdp"):
        if k in obj:
            r = _find_product_dict(obj[k], depth + 1)
            if r:
                return r
    for v in obj.values():
        r = _find_product_dict(v, depth + 1)
        if r:
            return r
    return None


# --- ID helpers --------------------------------------------------------------


def _id_from_ld_state(ld: dict[str, Any], state: dict[str, Any]) -> Optional[str]:
    for src in (ld, state):
        for k in ("sku", "productId", "styleId", "id", "@id"):
            v = src.get(k)
            if isinstance(v, (str, int)) and str(v).strip():
                s = str(v).strip()
                m = re.search(r"[A-Za-z0-9]+$", s)
                if m:
                    return m.group(0)
    return None


def _id_from_canonical(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first('link[rel="canonical"]')
    if el is not None:
        href = el.attributes.get("href") or ""
        return extract_id(href)
    return None


# --- Field extractors --------------------------------------------------------


def _title(tree: HTMLParser, ld: dict, state: dict) -> Optional[str]:
    if ld.get("name"):
        return str(ld["name"]).strip()
    for k in ("name", "title", "displayName", "productName"):
        v = state.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    el = tree.css_first('h1[itemprop="name"], h1[data-element="product-title"], h1')
    return _text(el) or None


def _brand(tree: HTMLParser, ld: dict, state: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        if b.get("name"):
            return str(b["name"]).strip()
    elif isinstance(b, str) and b.strip():
        return b.strip()
    for k in ("brand", "brandName"):
        v = state.get(k)
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()
    el = tree.css_first(
        'a[data-element="product-brand"], [itemprop="brand"], a[href*="/brands/"]'
    )
    return _text(el) or None


def _price(tree: HTMLParser, ld: dict, state: dict) -> Optional[dict[str, Any]]:
    amount: Optional[float] = None
    list_price: Optional[float] = None
    currency = "USD"
    pmin = pmax = None

    # 1) JSON-LD offers (most reliable when present).
    offer = _first_offer(ld)
    if offer:
        amount = _to_float(offer.get("price") or offer.get("lowPrice"))
        if offer.get("priceCurrency"):
            currency = str(offer["priceCurrency"])
        pmin = _to_float(offer.get("lowPrice"))
        pmax = _to_float(offer.get("highPrice"))
        # JSON-LD sometimes carries the original price too.
        list_price = _to_float(offer.get("listPrice"))

    # 2) App-state: current vs original price (the discount signal).
    if state:
        cur = _state_price(state, ("currentPrice", "salePrice", "price", "priceValue"))
        orig = _state_price(
            state, ("originalPrice", "regularPrice", "compareAtPrice", "wasPrice", "msrp")
        )
        if amount is None and cur is not None:
            amount = cur
        if list_price is None and orig is not None:
            list_price = orig
        smin = _state_price(state, ("minPrice", "lowPrice"))
        smax = _state_price(state, ("maxPrice", "highPrice"))
        if pmin is None:
            pmin = smin
        if pmax is None:
            pmax = smax

    # 3) DOM fallback — sale price + struck-through original.
    if amount is None:
        cur_node = tree.css_first(
            '[data-element="product-price-current"], '
            'span[itemprop="price"], '
            '.qbsf0n, [class*="current-price"], [data-testid="current-price"]'
        )
        m = _parse_money(_text(cur_node))
        if not m and cur_node is not None:
            m = _to_float(cur_node.attributes.get("content"))
            m = {"amount": m} if m is not None else None
        if m and "amount" in m:
            amount = m["amount"]
    if list_price is None:
        list_node = tree.css_first(
            '[data-element="product-price-original"], '
            '[class*="original-price"], [data-testid="regular-price"], '
            's, del, strike'
        )
        m = _parse_money(_text(list_node)) if list_node is not None else None
        if m and "amount" in m:
            list_price = m["amount"]

    if amount is None and pmin is not None:
        amount = pmin

    if amount is None:
        return None

    out: dict[str, Any] = {"amount": amount, "currency": currency}
    if list_price is not None and list_price > amount:
        out["list_price"] = list_price
    if pmin is not None and pmax is not None and pmax > pmin:
        out["price_range"] = {"min": pmin, "max": pmax}
    return out


def _state_price(state: dict, keys: tuple[str, ...]) -> Optional[float]:
    """Look up a price-ish value under any of `keys` in the state dict.

    Handles both flat numbers and nested {value:..} / {amount:..} shapes.
    """
    for k in keys:
        if k not in state:
            continue
        v = state[k]
        if isinstance(v, dict):
            for sub in ("value", "amount", "price", "current"):
                f = _to_float(v.get(sub))
                if f is not None:
                    return f
        f = _to_float(v)
        if f is not None:
            return f
    return None


def _rating(tree: HTMLParser, ld: dict, state: dict) -> Optional[dict[str, Any]]:
    stars = count = None
    agg = ld.get("aggregateRating")
    if isinstance(agg, dict):
        stars = _to_float(agg.get("ratingValue"))
        c = agg.get("reviewCount") or agg.get("ratingCount")
        if c is not None:
            try:
                count = int(re.sub(r"[^\d]", "", str(c)) or 0) or None
            except ValueError:
                count = None
    if stars is None:
        for k in ("averageRating", "rating", "ratingValue"):
            f = _to_float(state.get(k))
            if f is not None:
                stars = f
                break
    if count is None:
        for k in ("reviewCount", "ratingCount", "numberOfReviews"):
            v = state.get(k)
            if v is not None:
                try:
                    count = int(re.sub(r"[^\d]", "", str(v)) or 0) or None
                    break
                except ValueError:
                    pass
    if stars is None and count is None:
        el = tree.css_first('[data-element="reviews-rating"], [itemprop="ratingValue"]')
        stars = _to_float(_text(el)) if el is not None else None
    if stars is None and count is None:
        return None
    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out


def _availability(tree: HTMLParser, ld: dict, state: dict) -> Optional[str]:
    offer = _first_offer(ld)
    av = offer.get("availability")
    if isinstance(av, str) and av:
        # "https://schema.org/InStock" -> "In Stock"
        name = av.rstrip("/").rsplit("/", 1)[-1]
        name = re.sub(r"(?<!^)(?=[A-Z])", " ", name).strip()
        if name:
            return name
    for k in ("availabilityStatus", "availability", "inventoryStatus"):
        v = state.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    if state.get("inStock") is True:
        return "In Stock"
    if state.get("inStock") is False:
        return "Out of Stock"
    el = tree.css_first('[data-element="availability"], [itemprop="availability"]')
    return _text(el) or None


_BULLET_NOISE = re.compile(
    r"^(read more|see more|show more|less|details|product details|shipping|returns)$",
    re.IGNORECASE,
)


def _bullets(tree: HTMLParser, state: dict) -> Optional[list[str]]:
    out: list[str] = []
    # App-state: a list of detail/feature strings.
    for k in ("details", "productDetails", "features", "bullets", "featureBullets"):
        v = state.get(k)
        if isinstance(v, list):
            for item in v:
                if isinstance(item, str) and item.strip():
                    out.append(item.strip())
                elif isinstance(item, dict):
                    t = item.get("text") or item.get("value") or item.get("label")
                    if isinstance(t, str) and t.strip():
                        out.append(t.strip())
            if out:
                break
    # DOM fallback: the "Details" bullet list.
    if not out:
        for li in tree.css(
            '[data-element="product-details"] li, '
            'section[aria-label*="Details" i] li, '
            'div[class*="details" i] ul li'
        ):
            t = _text(li)
            if t and not _BULLET_NOISE.match(t):
                out.append(t)
    # Dedup, preserve order.
    seen, deduped = set(), []
    for b in out:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped or None


def _description(tree: HTMLParser, ld: dict, state: dict) -> Optional[str]:
    if ld.get("description"):
        d = str(ld["description"]).strip()
        if d:
            return d
    for k in ("description", "productDescription", "longDescription", "shortDescription"):
        v = state.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    el = tree.css_first(
        '[data-element="product-description"], '
        '[itemprop="description"], '
        'div[class*="description" i] p'
    )
    return _text(el) or None


# --- Images ------------------------------------------------------------------


def _normalize_img(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    return u


def _images(tree: HTMLParser, ld: dict, state: dict) -> Optional[dict[str, Any]]:
    main: list[str] = []
    thumbs: list[str] = []
    variants: dict[str, list[str]] = {}

    # 1) JSON-LD image (string or list).
    img = ld.get("image")
    if isinstance(img, str):
        main.append(_normalize_img(img))
    elif isinstance(img, list):
        for u in img:
            if isinstance(u, str):
                main.append(_normalize_img(u))
            elif isinstance(u, dict) and u.get("url"):
                main.append(_normalize_img(str(u["url"])))

    # 2) App-state media — typically a list of {url}/{src} objects, optionally
    #    keyed per color variant.
    if state:
        _collect_state_media(state, main, variants)

    # 3) DOM fallback — gallery / og:image.
    if not main:
        for im in tree.css(
            '[data-element="product-media"] img, '
            'picture img, img[itemprop="image"], img[class*="product" i]'
        ):
            src = (
                im.attributes.get("src")
                or im.attributes.get("data-src")
                or im.attributes.get("srcset", "").split(" ")[0]
            )
            if src and src not in thumbs:
                thumbs.append(_normalize_img(src))
        og = tree.css_first('meta[property="og:image"]')
        if og is not None and og.attributes.get("content"):
            main.append(_normalize_img(og.attributes["content"]))

    # Dedup main/thumbs preserving order.
    main = _dedup(main)
    thumbs = _dedup(thumbs)

    result: dict[str, Any] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = {k: _dedup(v) for k, v in variants.items() if v}
    return result or None


def _dedup(lst: list[str]) -> list[str]:
    seen, out = set(), []
    for x in lst:
        if x and x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _collect_state_media(state: dict, main: list[str], variants: dict) -> None:
    media = None
    for k in ("media", "images", "imageGroups", "styleMedia"):
        if k in state:
            media = state[k]
            break
    if media is None:
        return
    if isinstance(media, dict):
        # Possibly keyed by color, or {main:[...], ...}.
        for key, val in media.items():
            urls = _media_urls(val)
            if not urls:
                continue
            if key.lower() in ("main", "default", "primary"):
                main.extend(urls)
            else:
                variants.setdefault(key, []).extend(urls)
    elif isinstance(media, list):
        main.extend(_media_urls(media))


def _media_urls(val: Any) -> list[str]:
    out: list[str] = []
    if isinstance(val, str):
        return [_normalize_img(val)]
    if isinstance(val, dict):
        for k in ("url", "src", "imageUrl", "large", "zoom", "main"):
            if isinstance(val.get(k), str):
                out.append(_normalize_img(val[k]))
        return out
    if isinstance(val, list):
        for item in val:
            out.extend(_media_urls(item))
    return out


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(tree: HTMLParser, state: dict) -> Optional[list[str]]:
    crumbs: list[str] = []
    # App-state breadcrumb list.
    bc = state.get("breadcrumbs") or state.get("breadcrumb")
    if isinstance(bc, list):
        for c in bc:
            if isinstance(c, str):
                crumbs.append(c)
            elif isinstance(c, dict):
                t = c.get("name") or c.get("label") or c.get("text")
                if isinstance(t, str):
                    crumbs.append(t)
    if not crumbs:
        for a in tree.css(
            'nav[aria-label*="readcrumb" i] a, '
            '[data-element="breadcrumbs"] a, '
            'ol[class*="breadcrumb" i] li'
        ):
            t = _text(a)
            if t:
                crumbs.append(t)
    crumbs = [c.strip() for c in crumbs if c and c.strip()]
    return crumbs or None


def _specs(tree: HTMLParser, state: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    # App-state spec/attribute maps.
    for k in ("specifications", "specs", "attributes", "details"):
        v = state.get(k)
        if isinstance(v, dict):
            for kk, vv in v.items():
                if isinstance(vv, (str, int, float)) and str(vv).strip():
                    out.setdefault(str(kk), str(vv))
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, dict):
                    key = item.get("name") or item.get("label") or item.get("key")
                    val = item.get("value") or item.get("text")
                    if key and isinstance(val, (str, int, float)):
                        out.setdefault(str(key), str(val))
    # DOM fallback: definition lists / spec tables.
    for dl in tree.css('dl[class*="spec" i], dl[data-element="product-specs"]'):
        dts = dl.css("dt")
        dds = dl.css("dd")
        for dt, dd in zip(dts, dds):
            k, val = _text(dt), _text(dd)
            if k and val:
                out.setdefault(k.rstrip(":"), val)
    return out or None


def _variations(tree: HTMLParser, state: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    # App-state: explicit color/size option lists, or filters.
    for axis, keys in (
        ("color", ("colors", "colorOptions", "availableColors")),
        ("size", ("sizes", "sizeOptions", "availableSizes")),
    ):
        for k in keys:
            v = state.get(k)
            vals = _option_values(v)
            if vals:
                out[axis] = vals
                break
    # Derive from skus if present.
    if not out:
        skus = state.get("skus")
        if isinstance(skus, list):
            colors, sizes = [], []
            for s in skus:
                if not isinstance(s, dict):
                    continue
                c = s.get("color") or (s.get("colorName"))
                z = s.get("size") or (s.get("sizeName"))
                c = c.get("name") if isinstance(c, dict) else c
                z = z.get("name") if isinstance(z, dict) else z
                if isinstance(c, str) and c not in colors:
                    colors.append(c)
                if isinstance(z, str) and z not in sizes:
                    sizes.append(z)
            if colors:
                out["color"] = colors
            if sizes:
                out["size"] = sizes
    # DOM fallback.
    if not out:
        for c in tree.css('[data-element="color-selector"] [aria-label], [class*="color" i] button[title]'):
            v = c.attributes.get("aria-label") or c.attributes.get("title") or ""
            v = v.strip()
            if v:
                out.setdefault("color", [])
                if v not in out["color"]:
                    out["color"].append(v)
        for s in tree.css('[data-element="size-selector"] button, [class*="size" i] button'):
            v = _text(s)
            if v:
                out.setdefault("size", [])
                if v not in out["size"]:
                    out["size"].append(v)
    return out or None


def _option_values(v: Any) -> list[str]:
    out: list[str] = []
    if isinstance(v, list):
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                t = item.get("name") or item.get("label") or item.get("value") or item.get("displayValue")
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
    return _dedup(out)


def _seller(ld: dict, state: dict) -> Optional[str]:
    offer = _first_offer(ld)
    s = offer.get("seller")
    if isinstance(s, dict) and s.get("name"):
        return str(s["name"]).strip()
    if isinstance(s, str) and s.strip():
        return s.strip()
    for k in ("seller", "sellerName", "soldBy", "merchant"):
        v = state.get(k)
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# --- Fashion extras ----------------------------------------------------------


def _state_str_list(state: dict, keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        v = state.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, list):
            parts = [x.strip() for x in v if isinstance(x, str) and x.strip()]
            if parts:
                return " ".join(parts)
    return None


def _size_and_fit(tree: HTMLParser, state: dict) -> Optional[str]:
    s = _state_str_list(state, ("sizeAndFit", "fit", "fitInfo", "sizeInfo"))
    if s:
        return s
    el = tree.css_first('[data-element="size-and-fit"], section[aria-label*="Size" i]')
    return _text(el) or None


def _material(tree: HTMLParser, state: dict) -> Optional[str]:
    s = _state_str_list(state, ("material", "composition", "fabric", "materialContent"))
    if s:
        return s
    el = tree.css_first('[data-element="material"], [itemprop="material"]')
    return _text(el) or None


def _care(tree: HTMLParser, state: dict) -> Optional[str]:
    s = _state_str_list(state, ("care", "careInstructions", "careGuide"))
    if s:
        return s
    el = tree.css_first('[data-element="care"], section[aria-label*="Care" i]')
    return _text(el) or None


# --- Full visible page text (copied verbatim from crawler/parse.py) ----------


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
