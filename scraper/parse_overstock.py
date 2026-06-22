"""Extract structured fields + full visible text from an Overstock product page.

Mirrors the canonical field schema of ``crawler/parse.py`` (Amazon) and
``crawler/parse_walmart.py`` so the downstream embedding / deal-detection layers
see a uniform record shape across retailers. The dedup key on Overstock is the
numeric product id embedded in the product-page URL (analogous to Amazon's
``asin`` / Walmart's ``item_id``).

Overstock.com relaunched in 2024 on parent company Beyond, Inc.'s commerce
platform. The relaunched site is a **Next.js App Router** application: product
data is server-rendered into streaming React-Server-Component payload chunks
(``self.__next_f.push([1, "<chunk>"]) ``) rather than the classic
``__NEXT_DATA__`` blob, and the pages additionally emit a Schema.org
``Product`` JSON-LD block. Product pages are fronted by **Akamai Bot Manager**
(the "Press & Hold" behavioral / proof-of-work interstitial), so a plain
HTTP fetch (httpx / curl_cffi impersonation) returns only the ~2.6 KB Akamai
challenge shell — see ``CAPTCHA_MARKERS`` / ``BOT_VENDOR`` below. A real
headless browser that clears the challenge is required to obtain product HTML.
This parser is therefore written against the documented JSON-LD / RSC structure
and validated on a faithful fixture; once a browser-backed fetch layer feeds it
real HTML it will populate every extractable field.

Parsing strategy (most robust first):
  1. PRIMARY   — Schema.org ``<script type="application/ld+json">`` with
     ``@type":"Product"`` (name, brand, image, description, sku/mpn,
     offers.price / priceCurrency / availability, aggregateRating).
  2. SECONDARY — the Next.js RSC product blob streamed via
     ``self.__next_f.push([1,"..."])``. The chunks concatenate into an escaped
     JS string; we unescape and scan for the product model object (price,
     listPrice/msrp/wasPrice, images, options/variants, specifications,
     breadcrumbs). This carries the deal signal (list price) and per-variant
     data that JSON-LD usually omits.
  3. TERTIARY  — CSS / og:* meta on the rendered DOM (``<title>``, og:title,
     og:image, breadcrumb list) for the handful of fields that survive markup.

Every extractor is wrapped in ``_safe`` so one selector / JSON-path miss can
never abort the whole row. Empty values are dropped via ``_put``.
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

DOMAINS = ["overstock.com"]
ID_FIELD = "product_id"

# Overstock product pages are gated by Akamai Bot Manager's behavioral
# ("Press & Hold") proof-of-work interstitial. A plain HTTP client gets the
# challenge shell, not the product.
BOT_VENDOR = "Akamai Bot Manager"

# Strings that appear ONLY on Akamai's challenge / block interstitial, never on
# a real rendered product page. Anchored on the Akamai behavioral-challenge DOM
# ids + the visible "Powered and protected by ... Akamai" copy and the
# /akam/ sensor pixel path.
CAPTCHA_MARKERS = [
    "sec-if-cpt-container",          # behavioral challenge container id
    "sec-bc-tile-container",         # "press & hold" tile grid
    "progress-btn-disabled",         # the hold-to-verify button
    "Powered and protected by",      # Akamai logo caption
    "scf-akamai-logo",               # Akamai logo css class
    "/akam/",                        # Akamai sensor-data pixel / script path
    "Pardon Our Interruption",       # generic Akamai block copy (fallback)
    "Access Denied",                 # edge deny (fallback)
]

# Product-page URL forms observed:
#   1. Legacy / current canonical:
#      https://www.overstock.com/<Dept>/<slug>/<product-id>/product.html
#      e.g. /Home-Garden/3-Seater-Sofa-Dark-Gray-82.7-Fabric/37211085/product.html
#      (an optional ?option=<id> selects a variant)
#   2. Relaunch collections form:
#      https://www.overstock.com/products/<slug>-<product-id>
#      e.g. /products/bedding-comforters-sets-39202547
# The product id is the trailing all-digit token in either form.
_ID_PRODUCT_HTML = re.compile(r"/(\d{3,})/product\.html(?:[?#]|$)")
_ID_PRODUCTS_SLUG = re.compile(r"/products/(?:[^/?#]*-)?(\d{3,})(?:[/?#]|$)")
_ID_TRAILING = re.compile(r"/(\d{3,})(?:[/?#]|$)")

_LD_JSON_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)
# Next.js App Router RSC stream chunks: self.__next_f.push([1,"<escaped chunk>"])
_NEXT_F_RE = re.compile(
    r'self\.__next_f\.push\(\[\s*\d+\s*,\s*"((?:[^"\\]|\\.)*)"\s*\]\)',
    re.DOTALL,
)


def extract_id(url: str) -> Optional[str]:
    """Return the numeric Overstock product id from a product URL, or None."""
    if not url:
        return None
    for rx in (_ID_PRODUCT_HTML, _ID_PRODUCTS_SLUG):
        m = rx.search(url)
        if m:
            return m.group(1)
    # Bare numeric id
    if url.isdigit() and len(url) >= 3:
        return url
    m = _ID_TRAILING.search(url)
    return m.group(1) if m else None


# --- Top-level entrypoint -----------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page.

    Missing fields are omitted rather than included as None. Every individual
    extractor is wrapped so a selector / JSON-path miss can't abort the row.
    """
    tree = HTMLParser(html)
    ld = _safe(_ld_product, html) or {}
    blob = _safe(_next_product_blob, html) or {}

    out: dict[str, Any] = {}

    product_id = (
        extract_id(source_url)
        or (_nz(ld.get("sku")) or _nz(ld.get("mpn")) or _nz(ld.get("productID")))
        or _nz(blob.get("productId") or blob.get("id"))
    )
    if product_id:
        out["product_id"] = product_id
        canon = _canonical_url(tree)
        out["url"] = canon or source_url
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, ld, blob, tree))
    _put(out, "brand", _safe(_brand, ld, blob))

    price = _safe(_price, ld, blob)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld, blob))
    _put(out, "availability", _safe(_availability, ld, blob))
    _put(out, "bullets", _safe(_bullets, blob))
    _put(out, "description", _safe(_description, ld, blob))
    _put(out, "images", _safe(_images, ld, blob, tree))
    _put(out, "categories", _safe(_categories, blob, tree))
    specs = _safe(_specs, blob) or {}
    _put(out, "specs", specs)
    _put(out, "variations", _safe(_variations, blob))
    _put(out, "seller", _safe(_seller, blob))
    _put(out, "page_text", _safe(_page_text, html))

    # --- Home-goods extras (omit when absent) -------------------------------
    _put(out, "dimensions", _safe(_dimensions, blob, specs))
    _put(out, "weight", _safe(_weight, blob, specs))
    _put(out, "materials", _safe(_materials, blob, specs))
    assembly = _safe(_assembly_required, blob, specs)
    if assembly is not None:
        out["assembly_required"] = assembly

    return out


# --- Generic helpers (copied to keep this module self-contained) -------------


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
    if isinstance(v, (dict, list)):
        return None
    s = _htmllib.unescape(str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _strip_tags(s: str) -> str:
    """Descriptions are often HTML fragments; flatten to plain text."""
    if not s:
        return ""
    if "<" in s and ">" in s:
        try:
            s = lxml_html.fromstring(s).text_content()
        except Exception:
            s = _htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    else:
        s = _htmllib.unescape(s)
    return re.sub(r"\s+", " ", s).strip()


_CURRENCY_MAP = {"USD": "USD", "$": "USD", "GBP": "GBP", "£": "GBP", "EUR": "EUR", "CAD": "CAD"}


def _to_float(v: Any) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"[-+]?[\d,]*\.?\d+", str(v))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


# --- JSON-LD / RSC blob locators ---------------------------------------------


def _ld_product(html: str) -> dict[str, Any]:
    """Return the first JSON-LD node whose @type is Product, else {}.

    Handles both a bare Product node and a node nested in an @graph array.
    """
    for raw in _LD_JSON_RE.findall(html):
        raw = raw.strip()
        try:
            obj = json.loads(raw)
        except Exception:
            # Some emitters leave stray trailing commas / HTML comments.
            cleaned = re.sub(r",\s*([}\]])", r"\1", raw)
            try:
                obj = json.loads(cleaned)
            except Exception:
                continue
        for node in _iter_ld_nodes(obj):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" in types:
                return node
    return {}


def _iter_ld_nodes(obj: Any):
    if isinstance(obj, list):
        for o in obj:
            yield from _iter_ld_nodes(o)
    elif isinstance(obj, dict):
        if "@graph" in obj and isinstance(obj["@graph"], list):
            for o in obj["@graph"]:
                yield from _iter_ld_nodes(o)
        yield obj


# Keys that strongly indicate a dict is the product model in the RSC stream.
_PRODUCT_KEY_HINTS = ("listPrice", "salePrice", "msrp", "wasPrice", "productId",
                      "specifications", "options", "breadcrumbs")


def _next_product_blob(html: str) -> dict[str, Any]:
    """Reconstruct the Next.js RSC stream and dig out the product model dict.

    The ``self.__next_f.push([1,"..."])`` chunks each carry a JS-escaped string;
    concatenated they form the RSC payload. We unescape, then scan all embedded
    JSON objects for the one that looks like the product model (has price-ish +
    product-ish keys). Returns {} when nothing matches.
    """
    chunks = _NEXT_F_RE.findall(html)
    if not chunks:
        return {}
    payload = "".join(_js_unescape(c) for c in chunks)

    best: dict[str, Any] = {}
    best_score = 0
    for top in _iter_json_objects(payload):
        # The product model is often nested inside an RSC wrapper object
        # (e.g. {"children": {...the model...}}), so walk every nested dict.
        for cand in _walk_dicts(top):
            score = _product_score(cand)
            if score > best_score:
                best_score = score
                best = cand
    return best if best_score >= 2 else {}


def _walk_dicts(obj: Any, _depth: int = 0):
    """Yield every dict nested anywhere inside obj (depth-bounded)."""
    if _depth > 12:
        return
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk_dicts(v, _depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_dicts(v, _depth + 1)


def _product_score(cand: dict) -> int:
    score = sum(1 for k in _PRODUCT_KEY_HINTS if k in cand)
    # A product model has a name/title plus at least one price-ish key.
    has_name = any(k in cand for k in ("name", "title", "productTitle"))
    has_price = any(
        k in cand for k in ("price", "salePrice", "listPrice", "msrp", "priceInfo")
    )
    if has_name and has_price:
        score += 2
    return score


def _js_unescape(s: str) -> str:
    """Decode the JS string-literal escaping used inside __next_f chunks."""
    try:
        # The chunk is the *contents* of a double-quoted JS string; wrap + load.
        return json.loads('"' + s + '"')
    except Exception:
        return (
            s.replace('\\"', '"')
            .replace("\\\\", "\\")
            .replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\/", "/")
        )


def _iter_json_objects(text: str):
    """Yield every top-level-ish JSON object found by brace-matching scan.

    The RSC payload is not a single JSON doc (it's interleaved with RSC row
    markers), so we locate each ``{`` and attempt to decode a JSON object
    starting there with json.JSONDecoder.raw_decode.
    """
    decoder = json.JSONDecoder()
    i = 0
    n = len(text)
    while i < n:
        c = text[i]
        if c == "{":
            try:
                obj, end = decoder.raw_decode(text, i)
                yield obj
                i = end
                continue
            except Exception:
                pass
        i += 1


# --- Field extractors ---------------------------------------------------------


def _canonical_url(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("link[rel='canonical']")
    if el is not None:
        v = _nz(el.attributes.get("href"))
        if v:
            return v
    el = tree.css_first("meta[property='og:url']")
    if el is not None:
        return _nz(el.attributes.get("content"))
    return None


def _title(ld: dict, blob: dict, tree: HTMLParser) -> Optional[str]:
    t = (
        _nz(ld.get("name"))
        or _nz(blob.get("name") or blob.get("title") or blob.get("productTitle"))
    )
    if t:
        return t
    el = tree.css_first("meta[property='og:title']")
    if el is not None:
        v = _nz(el.attributes.get("content"))
        if v:
            return re.sub(r"\s*[-|]\s*Overstock(?:\.com)?\s*$", "", v) or v
    el = tree.css_first("h1")
    t = _text(el)
    if t:
        return t
    el = tree.css_first("title")
    if el is not None:
        v = _text(el)
        v = re.sub(r"\s*[-|]\s*Overstock(?:\.com)?\s*$", "", v)
        return v or None
    return None


def _brand(ld: dict, blob: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        b = b.get("name")
    if _nz(b):
        return _nz(b)
    bb = blob.get("brand") or blob.get("manufacturer") or blob.get("brandName")
    if isinstance(bb, dict):
        bb = bb.get("name")
    return _nz(bb)


def _offers_node(ld: dict) -> dict:
    offers = ld.get("offers")
    if isinstance(offers, list):
        return offers[0] if offers and isinstance(offers[0], dict) else {}
    return offers if isinstance(offers, dict) else {}


def _price(ld: dict, blob: dict) -> Optional[dict[str, Any]]:
    amount: Optional[float] = None
    currency = ""
    list_price: Optional[float] = None

    # --- RSC blob (carries the deal signal most reliably) -------------------
    amount = _to_float(
        blob.get("salePrice") or blob.get("price") or blob.get("currentPrice")
    )
    # Sometimes nested under a price object.
    if amount is None and isinstance(blob.get("priceInfo"), dict):
        pi = blob["priceInfo"]
        amount = _to_float(pi.get("salePrice") or pi.get("currentPrice") or pi.get("price"))
        list_price = _to_float(pi.get("listPrice") or pi.get("msrp") or pi.get("wasPrice"))
        currency = _CURRENCY_MAP.get(pi.get("currency") or pi.get("currencyCode") or "", "")
    if list_price is None:
        list_price = _to_float(
            blob.get("listPrice") or blob.get("msrp") or blob.get("wasPrice")
            or blob.get("originalPrice")
        )
    if not currency:
        currency = _CURRENCY_MAP.get(blob.get("currency") or blob.get("currencyCode") or "", "")

    # --- JSON-LD offers fallback / supplement -------------------------------
    offers = _offers_node(ld)
    if amount is None:
        amount = _to_float(offers.get("price") or offers.get("lowPrice"))
    if not currency:
        currency = _CURRENCY_MAP.get(offers.get("priceCurrency") or "", "")
    # Some emitters carry MSRP in offers under "highPrice" or a priceSpecification.
    if list_price is None:
        list_price = _to_float(offers.get("highPrice"))
        if list_price is None:
            ps = offers.get("priceSpecification")
            if isinstance(ps, list):
                for p in ps:
                    if isinstance(p, dict) and "list" in str(p.get("@type", "")).lower():
                        list_price = _to_float(p.get("price"))
                        break

    if amount is None:
        return None

    out: dict[str, Any] = {"amount": amount, "currency": currency or "USD"}
    if list_price is not None and list_price > amount:
        out["list_price"] = list_price

    # Variant price range.
    lo = _to_float(blob.get("minPrice") or (blob.get("priceRange") or {}).get("min")
                   if isinstance(blob.get("priceRange"), dict) else blob.get("minPrice"))
    hi = _to_float(blob.get("maxPrice") or (blob.get("priceRange") or {}).get("max")
                   if isinstance(blob.get("priceRange"), dict) else blob.get("maxPrice"))
    if lo is not None and hi is not None and hi > lo:
        out["price_range"] = {"min": lo, "max": hi}
        if lo < out["amount"]:
            out["amount"] = lo
    return out


def _rating(ld: dict, blob: dict) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    agg = ld.get("aggregateRating")
    if isinstance(agg, dict):
        stars = _to_float(agg.get("ratingValue"))
        c = agg.get("reviewCount") or agg.get("ratingCount")
        if c is not None:
            count = int(_to_float(c) or 0) or None

    if stars is None:
        stars = _to_float(blob.get("averageRating") or blob.get("rating")
                          or blob.get("ratingValue"))
    if count is None:
        c = _to_float(blob.get("reviewCount") or blob.get("numberOfReviews")
                      or blob.get("totalReviewCount"))
        if c is not None:
            count = int(c) or None

    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out or None


_AVAIL_MAP = {
    "InStock": "In stock",
    "OutOfStock": "Out of stock",
    "PreOrder": "Pre-order",
    "BackOrder": "Backordered",
    "Discontinued": "Discontinued",
    "IN_STOCK": "In stock",
    "OUT_OF_STOCK": "Out of stock",
}


def _availability(ld: dict, blob: dict) -> Optional[str]:
    offers = _offers_node(ld)
    av = offers.get("availability")
    if av:
        v = str(av).rsplit("/", 1)[-1]
        return _AVAIL_MAP.get(v, v)
    raw = blob.get("availabilityStatus") or blob.get("availability") or blob.get("stockStatus")
    if isinstance(raw, bool):
        return "In stock" if raw else "Out of stock"
    if raw:
        return _AVAIL_MAP.get(str(raw), str(raw).replace("_", " ").strip().capitalize())
    if blob.get("inStock") is not None:
        return "In stock" if blob.get("inStock") else "Out of stock"
    return None


def _bullets(blob: dict) -> Optional[list[str]]:
    """Overstock 'highlights' / feature bullets."""
    out: list[str] = []
    for key in ("highlights", "features", "bullets", "featureBullets",
                "keyFeatures", "productHighlights"):
        val = blob.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, str):
                    t = _strip_tags(item)
                elif isinstance(item, dict):
                    name = _nz(item.get("name") or item.get("label"))
                    v = _nz(item.get("value") or item.get("text"))
                    t = f"{name}: {v}" if name and v else (v or name or "")
                else:
                    t = ""
                if t and t not in out:
                    out.append(t)
        elif isinstance(val, str):
            # HTML fragment with <li>/<br>
            for ch in re.split(r"</li>|</p>|<br\s*/?>", val, flags=re.IGNORECASE):
                t = _strip_tags(ch)
                if t and len(t) > 2 and t not in out:
                    out.append(t)
        if out:
            break
    return out or None


def _description(ld: dict, blob: dict) -> Optional[str]:
    for raw in (
        blob.get("longDescription"),
        blob.get("description"),
        blob.get("overview"),
        ld.get("description"),
    ):
        if raw:
            t = _strip_tags(raw) if isinstance(raw, str) else None
            if t:
                return t
    return None


def _images(ld: dict, blob: dict, tree: HTMLParser) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []
    variants: dict[str, list[str]] = {}

    def _push(lst, u):
        u = _nz(u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else None))
        if u and u not in lst:
            lst.append(u)

    # RSC blob image arrays
    for key in ("images", "imageUrls", "gallery", "allImages", "media"):
        val = blob.get(key)
        if isinstance(val, list):
            for it in val:
                if isinstance(it, dict):
                    _push(main, it.get("url") or it.get("large") or it.get("src")
                          or it.get("hiRes"))
                    _push(thumbs, it.get("thumb") or it.get("thumbnail")
                          or it.get("thumbnailUrl"))
                else:
                    _push(main, it)
    _push(thumbs, blob.get("thumbnailUrl") or blob.get("thumbnail"))

    # Per-variant images from options/variants
    for key in ("options", "variants", "variantOptions"):
        for crit in blob.get(key) or []:
            if not isinstance(crit, dict):
                continue
            name = _nz(crit.get("name") or crit.get("label") or crit.get("value"))
            urls: list[str] = []
            _push(urls, crit.get("swatchImageUrl") or crit.get("swatch") or crit.get("image"))
            for vi in crit.get("images") or []:
                _push(urls, vi)
            if name and urls:
                variants.setdefault(name, [])
                for u in urls:
                    if u not in variants[name]:
                        variants[name].append(u)

    # JSON-LD image
    if not main:
        imgs = ld.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list):
            for u in imgs:
                _push(main, u)

    # og:image DOM fallback
    if not main:
        for el in tree.css("meta[property='og:image']"):
            _push(main, el.attributes.get("content"))

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


def _categories(blob: dict, tree: HTMLParser) -> Optional[list[str]]:
    crumbs: list[str] = []
    for key in ("breadcrumbs", "breadcrumb", "categoryPath", "categories"):
        val = blob.get(key)
        if isinstance(val, list):
            for p in val:
                if isinstance(p, dict):
                    n = _nz(p.get("name") or p.get("label") or p.get("title"))
                elif isinstance(p, str):
                    n = _nz(p)
                else:
                    n = None
                if n and n.lower() not in ("home",):
                    crumbs.append(n)
        if crumbs:
            break

    if not crumbs:
        # JSON-LD BreadcrumbList sometimes present separately — skip; use DOM.
        for a in tree.css(
            "nav[aria-label='breadcrumb'] a, ol.breadcrumb a, "
            "nav.breadcrumbs a, [data-testid*='breadcrumb'] a"
        ):
            t = _text(a)
            if t and t.lower() not in ("home",):
                crumbs.append(t)

    # de-dup preserving order
    seen = set()
    out = []
    for c in crumbs:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out or None


def _spec_pairs(blob: dict):
    """Yield (key, value) pairs from the blob's specification structures."""
    for key in ("specifications", "specs", "attributes", "details",
                "productSpecifications", "specificationGroups"):
        val = blob.get(key)
        if isinstance(val, dict):
            for k, v in val.items():
                kk, vv = _nz(k), _nz(v)
                if kk and vv:
                    yield kk, vv
        elif isinstance(val, list):
            for item in val:
                if not isinstance(item, dict):
                    continue
                # grouped specs: {groupName, specifications:[{name,value}]}
                inner = item.get("specifications") or item.get("attributes") or item.get("items")
                if isinstance(inner, list):
                    for s in inner:
                        if isinstance(s, dict):
                            kk = _nz(s.get("name") or s.get("label") or s.get("key"))
                            vv = _nz(s.get("value") or s.get("text"))
                            if kk and vv:
                                yield kk, vv
                    continue
                kk = _nz(item.get("name") or item.get("label") or item.get("key"))
                vv = _nz(item.get("value") or item.get("text"))
                if kk and vv:
                    yield kk, vv


def _specs(blob: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for k, v in _spec_pairs(blob):
        if k not in out:
            out[k] = v
    return out or None


def _variations(blob: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    for key in ("options", "variants", "variantCriteria", "variantOptions",
                "configurableOptions"):
        for crit in blob.get(key) or []:
            if not isinstance(crit, dict):
                continue
            label = _nz(crit.get("name") or crit.get("label") or crit.get("type")
                        or crit.get("attributeName"))
            if not label:
                continue
            values: list[str] = []
            choices = (crit.get("values") or crit.get("options") or crit.get("variantList")
                       or crit.get("choices") or [])
            for v in choices:
                if isinstance(v, dict):
                    nv = _nz(v.get("name") or v.get("value") or v.get("label"))
                elif isinstance(v, str):
                    nv = _nz(v)
                else:
                    nv = None
                if nv and nv not in values:
                    values.append(nv)
            if values:
                out[label.lower()] = values
        if out:
            break
    return out or None


def _seller(blob: dict) -> Optional[str]:
    name = _nz(blob.get("sellerName") or blob.get("sellerDisplayName")
               or blob.get("merchantName") or blob.get("soldBy"))
    if isinstance(blob.get("seller"), dict):
        name = name or _nz(blob["seller"].get("name"))
    if not name:
        return None
    if name.lower() in ("overstock", "overstock.com", "bed bath & beyond"):
        return None
    return name


# --- Home-goods extras --------------------------------------------------------


def _find_spec(blob: dict, *patterns: str) -> Optional[str]:
    rx = re.compile("|".join(patterns), re.IGNORECASE)
    for k, v in _spec_pairs(blob):
        if rx.search(k):
            return v
    return None


def _dimensions(blob: dict, specs: dict) -> Optional[str]:
    for key in ("dimensions", "productDimensions", "overallDimensions", "size"):
        v = _nz(blob.get(key))
        if v:
            return v
    return _find_spec(blob, r"dimension", r"overall (size|dimension)", r"\bsize\b")


def _weight(blob: dict, specs: dict) -> Optional[str]:
    for key in ("weight", "productWeight", "shippingWeight"):
        v = _nz(blob.get(key))
        if v:
            return v
    return _find_spec(blob, r"weight")


def _materials(blob: dict, specs: dict) -> Optional[str]:
    for key in ("materials", "material", "primaryMaterial"):
        val = blob.get(key)
        if isinstance(val, list):
            mats = [_nz(x) for x in val if _nz(x)]
            if mats:
                return ", ".join(mats)
        v = _nz(val)
        if v:
            return v
    return _find_spec(blob, r"material")


_TRUE_RE = re.compile(r"^(yes|true|required|assembly required)$", re.IGNORECASE)
_FALSE_RE = re.compile(r"^(no|false|none|no assembly)$", re.IGNORECASE)


def _assembly_required(blob: dict, specs: dict) -> Optional[bool]:
    val = blob.get("assemblyRequired")
    if isinstance(val, bool):
        return val
    raw = _nz(val) or _find_spec(blob, r"assembly")
    if not raw:
        return None
    if _TRUE_RE.match(raw):
        return True
    if _FALSE_RE.match(raw):
        return False
    return None


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
