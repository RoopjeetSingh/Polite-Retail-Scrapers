"""Extract structured fields + full visible text from a DICK'S Sporting Goods product page.

Self-contained module mirroring the public interface of ``crawler.parse`` but
targeting dickssportinggoods.com. Parsing order (most robust first):

  1. JSON-LD ``@type":"Product"`` — name, brand, sku/mpn, image, description,
     offers (price/priceCurrency/availability/seller), aggregateRating. A
     separate ``@type":"BreadcrumbList"`` JSON-LD gives the category path.
  2. Inline product-state JSON (Dick's renders a Redux-style preloaded state /
     ``digitalData`` blob containing salePrice / originalPrice / wasPrice,
     swatch (color) + size variations, the "Features" highlight list, and a
     "Specs"/"Details" specification map). We pull individual fields with
     anchored regexes so field-order / null values are handled.
  3. CSS / regex fallbacks on the rendered DOM (title h1, breadcrumb nav,
     struck-through price spans).

Every individual extractor is wrapped in ``_safe`` so a miss can't abort the
whole row. Missing fields are omitted rather than written as ``None``.

NOTE ON FETCHING (see data/parser_tests/dicks/report.md): dickssportinggoods.com
is behind **Akamai Bot Manager**. Every plain HTTP request — including TLS-
impersonating clients (``curl_cffi`` ``safari17_0`` / ``chrome120``) and even
the homepage — is answered with a ~2.3 KB HTTP-200 *behavioral challenge shell*
(``sec-if-cpt-container`` / "Press & Hold", "Powered and protected by Akamai")
that runs an Akamai sensor proof-of-work and sets the ``_abck`` / ``bm_sz`` /
``ak_bmsc`` cookies before the real page is served. curl_cffi cannot execute
that JS, so the page is **browser-only** (needs a headless browser, e.g.
Playwright, that solves the Akamai sensor challenge). This parser therefore
targets the documented rendered-page structure; it is validated against a
faithful fixture (data/parser_tests/dicks/fixture_*.html) rather than live HTML.
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

# --- Module interface ---------------------------------------------------------

DOMAINS = ["dickssportinggoods.com"]
ID_FIELD = "product_id"
BOT_VENDOR = "Akamai"  # Akamai Bot Manager behavioral challenge (confirmed)

# Strings that appear on Dick's Akamai behavioral-challenge interstitial. These
# are specific to the *challenge* shell so they don't match a normally rendered
# product page (which carries Akamai sensor script tags but not these markers).
CAPTCHA_MARKERS = (
    "sec-if-cpt-container",                 # challenge container id
    "behavioral-content",                   # "Press & Hold" behavioral wrapper
    "sec-bc-tile-container",                 # behavioral tile container
    "Powered and protected by",              # Akamai challenge footer text
    "scf-akamai-protected-by",               # Akamai challenge css class
    "akamai.com/site/ko/images/logo",        # Akamai logo on challenge page
    "Pardon Our Interruption",               # generic Akamai/PX block headline
    "Access Denied",                         # Akamai edge denial page
    "Reference #",                           # Akamai edge error id
    "errors.edgesuite.net",                  # Akamai edge error host
)


def extract_id(url: str) -> Optional[str]:
    """Pull the Dick's product/style id out of a product URL.

    Canonical product URL form:
        https://www.dickssportinggoods.com/p/<slug>-<id>/<id>
    The id is the final path segment (also suffixed on the slug). It is an
    alphanumeric style code, e.g. ``25nikakd18pltnmxxmnk``. We prefer the final
    path segment and fall back to the id embedded in the slug.
    """
    if not url:
        return None
    # Strip query/fragment.
    path = url.split("?", 1)[0].split("#", 1)[0]
    # Canonical: .../p/<slug>-<id>/<id>
    m = re.search(r"/p/[^/]*?-([A-Za-z0-9]{8,})/([A-Za-z0-9]{8,})/?$", path)
    if m:
        return m.group(2)
    # Just .../p/<slug>/<id> (no trailing repeat in slug)
    m = re.search(r"/p/[^/]+/([A-Za-z0-9]{8,})/?$", path)
    if m:
        return m.group(1)
    # Last resort: trailing alphanumeric segment after /p/.
    m = re.search(r"/p/.*?([A-Za-z0-9]{8,})/?$", path)
    if m:
        return m.group(1)
    return None


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page."""
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    out: dict[str, Any] = {}

    pid = extract_id(source_url) or _safe(lambda: _ld_sku(ld))
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.dickssportinggoods.com/p/-/{pid}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, ld, tree))
    _put(out, "brand", _safe(_brand, ld, html))

    price = _safe(_price, html, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld, html))
    _put(out, "availability", _safe(_availability, ld, html))
    _put(out, "bullets", _safe(_bullets, html))
    _put(out, "description", _safe(_description, ld, html, tree))
    _put(out, "images", _safe(_images, ld, html))
    _put(out, "categories", _safe(_breadcrumb, html, tree))
    _put(out, "specs", _safe(_specs, html))
    _put(out, "variations", _safe(_variations, html))
    _put(out, "seller", _safe(_seller, ld))

    # Sporting-goods extras (omitted when absent).
    specs = out.get("specs") or {}
    _put(out, "features", _safe(_features, html))
    _put(out, "model_number", _safe(_model_number, ld, specs))
    _put(out, "gender", _safe(_gender, specs, out.get("categories")))
    _put(out, "sport", _safe(_sport, specs, html))
    _put(out, "material", _safe(_material, specs))

    _put(out, "page_text", _safe(_page_text, html))

    return out


# --- Generic helpers ----------------------------------------------------------


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


def _json_unescape(s: str) -> str:
    if s is None:
        return ""
    try:
        return json.loads(f'"{s}"')
    except Exception:
        return (
            s.replace('\\"', '"')
            .replace("\\u0026", "&")
            .replace("\\/", "/")
            .replace("\\n", " ")
            .replace("\\t", " ")
        )


# --- JSON-LD ------------------------------------------------------------------


def _iter_jsonld(html: str):
    for m in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    ):
        blob = m.group(1).strip()
        try:
            yield json.loads(blob)
        except Exception:
            continue


def _flatten_ld(d: Any) -> list[dict]:
    out: list[dict] = []
    if isinstance(d, list):
        for x in d:
            out.extend(_flatten_ld(x))
    elif isinstance(d, dict):
        out.append(d)
        if "@graph" in d:
            out.extend(_flatten_ld(d["@graph"]))
    return out


def _is_type(obj: dict, t: str) -> bool:
    v = obj.get("@type")
    if isinstance(v, list):
        return t in v
    return v == t


def _jsonld_product(html: str) -> Optional[dict[str, Any]]:
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and _is_type(obj, "Product"):
                return obj
    return None


def _ld_sku(ld: dict) -> Optional[str]:
    for k in ("sku", "mpn", "productID", "gtin13"):
        v = ld.get(k)
        if v:
            return str(v).strip()
    return None


# --- Title / brand ------------------------------------------------------------


def _title(ld: dict, tree: HTMLParser) -> Optional[str]:
    name = ld.get("name")
    if name:
        return str(name).strip()
    el = tree.css_first(
        "h1.product-title, h1[data-testid='product-title'], "
        "h1.dsg-product-title, h1.css-1xovrc6, h1"
    )
    return _text(el) or None


def _brand(ld: dict, html: str) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        n = b.get("name")
        if n:
            return str(n).strip()
    elif isinstance(b, str) and b:
        return b.strip()
    # State blob fallback: "brandName":"Nike" / "brand":"Nike"
    m = re.search(r'"brand(?:Name)?":"((?:[^"\\]|\\.)+)"', html)
    if m:
        v = _json_unescape(m.group(1)).strip()
        if v:
            return v
    return None


# --- Price --------------------------------------------------------------------

# Dick's state blob price fields. salePrice / offerPrice / listPrice are the
# common names; originalPrice / wasPrice / regularPrice / strikeThroughPrice are
# the list (pre-discount) price used for the deal signal.
_SALE_RE = re.compile(
    r'"(?:salePrice|offerPrice|currentPrice|finalPrice|price)":\s*"?\$?([\d,]+\.\d{2})"?'
)
_LIST_RE = re.compile(
    r'"(?:originalPrice|wasPrice|regularPrice|listPrice|strikeThroughPrice|msrp)":\s*"?\$?([\d,]+\.\d{2})"?'
)
_MIN_RE = re.compile(r'"(?:minPrice|lowPrice|priceMin)":\s*"?\$?([\d,]+\.\d{2})"?')
_MAX_RE = re.compile(r'"(?:maxPrice|highPrice|priceMax)":\s*"?\$?([\d,]+\.\d{2})"?')


def _f(s: str) -> float:
    return float(s.replace(",", ""))


def _offer_obj(ld: dict) -> Optional[dict]:
    offers = ld.get("offers")
    if isinstance(offers, dict):
        # AggregateOffer carries lowPrice/highPrice; Offer carries price.
        return offers
    if isinstance(offers, list):
        for o in offers:
            if isinstance(o, dict):
                return o
    return None


def _price(html: str, ld: dict) -> Optional[dict[str, Any]]:
    amount: Optional[float] = None
    list_price: Optional[float] = None
    rmin: Optional[float] = None
    rmax: Optional[float] = None

    # 1) JSON-LD offers.
    offer = _offer_obj(ld)
    if offer:
        if offer.get("price") is not None:
            try:
                amount = float(offer["price"])
            except (TypeError, ValueError):
                pass
        # AggregateOffer range
        for k, dst in (("lowPrice", "lo"), ("highPrice", "hi")):
            if offer.get(k) is not None:
                try:
                    val = float(offer[k])
                except (TypeError, ValueError):
                    continue
                if dst == "lo":
                    rmin = val
                else:
                    rmax = val
        if amount is None and rmin is not None:
            amount = rmin

    # 2) State-blob sale price (authoritative current price).
    if amount is None:
        m = _SALE_RE.search(html)
        if m:
            amount = _f(m.group(1))

    # 3) State-blob range.
    if rmin is None:
        mm = _MIN_RE.search(html)
        if mm:
            rmin = _f(mm.group(1))
    if rmax is None:
        mm = _MAX_RE.search(html)
        if mm:
            rmax = _f(mm.group(1))
    if amount is None and rmin is not None:
        amount = rmin

    if amount is None:
        return None

    # List price (struck-through original) — the deal signal.
    lm = _LIST_RE.search(html)
    if lm:
        list_price = _f(lm.group(1))

    out: dict[str, Any] = {"amount": amount, "currency": "USD"}
    if list_price and list_price > amount:
        out["list_price"] = list_price
    if rmin is not None and rmax is not None and rmax > rmin:
        out["price_range"] = {"min": rmin, "max": rmax}
    return out


# --- Rating -------------------------------------------------------------------

_RATING_VALUE_RE = re.compile(r'"(?:ratingValue|averageRating|avgRating)":\s*"?([\d.]+)"?')
_RATING_COUNT_RE = re.compile(r'"(?:reviewCount|ratingCount|totalReviewCount)":\s*"?(\d+)"?')


def _rating(ld: dict, html: str) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    agg = ld.get("aggregateRating")
    if isinstance(agg, dict):
        try:
            if agg.get("ratingValue") is not None:
                stars = float(agg["ratingValue"])
        except (TypeError, ValueError):
            pass
        c = agg.get("reviewCount") or agg.get("ratingCount")
        try:
            if c is not None:
                count = int(c)
        except (TypeError, ValueError):
            pass

    if stars is None:
        m = _RATING_VALUE_RE.search(html)
        if m:
            stars = float(m.group(1))
    if count is None:
        m = _RATING_COUNT_RE.search(html)
        if m:
            count = int(m.group(1))

    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out or None


# --- Availability -------------------------------------------------------------


def _availability(ld: dict, html: str) -> Optional[str]:
    offer = _offer_obj(ld)
    if offer:
        a = str(offer.get("availability") or "")
        if "InStock" in a:
            return "In Stock"
        if "OutOfStock" in a or "SoldOut" in a or "Discontinued" in a:
            return "Out of Stock"
        if "PreOrder" in a:
            return "Pre-Order"
    # State-blob fallback.
    m = re.search(r'"(?:availabilityStatus|inventoryStatus|availability)":"([^"]+)"', html)
    if m:
        v = m.group(1)
        if re.search(r"in[\s_]?stock", v, re.I):
            return "In Stock"
        if re.search(r"out[\s_]?of[\s_]?stock|sold[\s_]?out", v, re.I):
            return "Out of Stock"
        return v
    if re.search(r'"(?:isInStock|inStock)":\s*true', html):
        return "In Stock"
    if re.search(r'"(?:isInStock|inStock)":\s*false', html):
        return "Out of Stock"
    return None


# --- Bullets / Features / Description ------------------------------------------

# Dick's renders the highlight list under a "Features" heading as <li> items
# inside a description/features container, and also carries them in the state
# blob as a "features" / "bulletPoints" array of strings.
_FEATURES_ARR_RE = re.compile(
    r'"(?:features|featureBullets|bulletPoints|highlights)":\s*(\[[^\]]*\])'
)


def _features(html: str) -> Optional[list[str]]:
    """Sporting-goods 'Features' highlight list (also exposed as `bullets`)."""
    m = _FEATURES_ARR_RE.search(html)
    if m:
        try:
            arr = json.loads(m.group(1))
        except Exception:
            arr = None
        if isinstance(arr, list):
            items = []
            for x in arr:
                if isinstance(x, str):
                    t = _json_unescape(x).strip()
                elif isinstance(x, dict):
                    t = _json_unescape(str(x.get("description") or x.get("text") or x.get("value") or "")).strip()
                else:
                    t = ""
                if t and t not in items:
                    items.append(t)
            if items:
                return items
    # DOM fallback: a "Features" section's <li> items.
    tree = HTMLParser(html)
    for hdr in tree.css("h2, h3, h4"):
        if _text(hdr).strip().lower() in ("features", "product features"):
            ul = hdr.next
            # Walk forward to the first following <ul>.
            sib = hdr.next
            items: list[str] = []
            steps = 0
            while sib is not None and steps < 6:
                if getattr(sib, "tag", None) == "ul":
                    for li in sib.css("li"):
                        t = _text(li)
                        if t and t not in items:
                            items.append(t)
                    break
                sib = sib.next
                steps += 1
            if items:
                return items
    # Generic container fallback.
    items = []
    for li in tree.css(
        "div.product-features li, [data-testid='product-features'] li, "
        "ul.product-features-list li, div.features li"
    ):
        t = _text(li)
        if t and t not in items:
            items.append(t)
    return items or None


def _bullets(html: str) -> Optional[list[str]]:
    # On Dick's the "Features" highlights are the bullet equivalent.
    return _features(html)


_DESC_STATE_RE = re.compile(
    r'"(?:longDescription|productDescription|description|descriptionHtml)":"((?:[^"\\]|\\.)*)"'
)


def _description(ld: dict, html: str, tree: HTMLParser) -> Optional[str]:
    # 1) JSON-LD description.
    d = ld.get("description")
    if isinstance(d, str) and d.strip():
        return _strip_html(d.strip())
    # 2) State-blob description.
    m = _DESC_STATE_RE.search(html)
    if m:
        t = _strip_html(_json_unescape(m.group(1)).strip())
        if t:
            return t
    # 3) DOM fallback.
    el = tree.css_first(
        "div.product-description, [data-testid='product-description'], "
        "div.description-content, #product-overview, div.pdp-description"
    )
    if el is not None:
        t = _text(el)
        if t:
            return t
    return None


def _strip_html(s: str) -> str:
    if "<" not in s:
        return s
    try:
        frag = lxml_html.fromstring(s)
        t = frag.text_content()
        return re.sub(r"\s+", " ", t).strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", s).strip()


# --- Images -------------------------------------------------------------------

_SCENE7_RE = re.compile(r'https?://[^"\\\s]*?dks(?:scontent)?[^"\\\s]*?\.(?:jpg|jpeg|png|webp)', re.I)
# Dick's images are served from scene7 (dks*.scene7.com) and dickssportinggoods
# CDN. Match common image URL patterns in the state blob.
_IMG_URL_RE = re.compile(
    r'https?://(?:[^"\\\s]+\.)?(?:scene7\.com|dickssportinggoods\.com|dimg\.dgl\.com)/[^"\\\s]+?\.(?:jpg|jpeg|png|webp)(?:\?[^"\\\s]*)?',
    re.I,
)


def _images(ld: dict, html: str) -> Optional[dict[str, list]]:
    main: list[str] = []

    # 1) JSON-LD image (str or list).
    img = ld.get("image")
    if isinstance(img, str):
        main.append(img)
    elif isinstance(img, list):
        for u in img:
            if isinstance(u, str) and u and u not in main:
                main.append(u)
            elif isinstance(u, dict):
                uu = u.get("url") or u.get("contentUrl")
                if uu and uu not in main:
                    main.append(uu)

    # 2) State-blob image URLs (CDN / scene7).
    if not main:
        for u in _IMG_URL_RE.findall(html):
            u = _json_unescape(u)
            if u not in main:
                main.append(u)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    return result or None


# --- Categories ---------------------------------------------------------------


def _breadcrumb(html: str, tree: HTMLParser) -> Optional[list[str]]:
    # 1) BreadcrumbList JSON-LD.
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and _is_type(obj, "BreadcrumbList"):
                crumbs = []
                for it in obj.get("itemListElement", []) or []:
                    if not isinstance(it, dict):
                        continue
                    item = it.get("item")
                    name = None
                    if isinstance(item, dict):
                        name = item.get("name")
                    name = name or it.get("name")
                    if name and str(name).strip().lower() not in ("home", "dick's sporting goods"):
                        crumbs.append(str(name).strip())
                if crumbs:
                    return crumbs
    # 2) DOM breadcrumb nav.
    crumbs = [
        _text(a)
        for a in tree.css(
            "nav.breadcrumb a, ol.breadcrumb a, [data-testid='breadcrumb'] a, "
            "nav[aria-label='breadcrumb'] a, .breadcrumbs a"
        )
    ]
    crumbs = [c for c in crumbs if c and c.lower() not in ("home", "dick's sporting goods")]
    return crumbs or None


# --- Specs --------------------------------------------------------------------

# State-blob spec/detail entries: {"name":"Color","value":"Black"} style, with
# a few common key naming variants seen on Dick's spec maps.
_SPEC_PAIR_RE = re.compile(
    r'\{[^{}]*?"(?:name|label|displayName|key)":"((?:[^"\\]|\\.)+)"[^{}]*?'
    r'"(?:value|val|displayValue)":"((?:[^"\\]|\\.)*)"[^{}]*?\}'
)


def _specs(html: str) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for name, value in _SPEC_PAIR_RE.findall(html):
        k = _json_unescape(name).strip()
        v = _json_unescape(value).strip()
        if k and v and k not in out and len(k) < 60:
            out[k] = v

    # DOM fallback: a "Specs"/"Details" table or definition list.
    if not out:
        tree = HTMLParser(html)
        for row in tree.css(
            "table.product-specs tr, table.specs tr, [data-testid='specifications'] tr, "
            "div.product-details-table tr, dl.specs > div"
        ):
            cells = row.css("th, td, dt, dd")
            if len(cells) >= 2:
                k = _text(cells[0]).rstrip(":").strip()
                v = _text(cells[1]).strip()
                if k and v and k not in out:
                    out[k] = v
    return out or None


# --- Variations ---------------------------------------------------------------

# Dick's variations live in the state blob: a color/swatch list and a size list.
_COLOR_LIST_RE = re.compile(r'"(?:colors|colorOptions|swatches)":\s*(\[[^\]]*\])')
_SIZE_LIST_RE = re.compile(r'"(?:sizes|sizeOptions)":\s*(\[[^\]]*\])')
_NAME_IN_OBJ_RE = re.compile(r'"(?:name|value|label|displayName|color|size)":"((?:[^"\\]|\\.)+)"')


def _variation_values(arr_json: str) -> list[str]:
    vals: list[str] = []
    try:
        arr = json.loads(arr_json)
    except Exception:
        arr = None
    if isinstance(arr, list):
        for x in arr:
            if isinstance(x, str):
                v = _json_unescape(x).strip()
            elif isinstance(x, dict):
                v = ""
                for key in ("name", "value", "label", "displayName", "color", "size", "description"):
                    if x.get(key):
                        v = _json_unescape(str(x[key])).strip()
                        break
            else:
                v = ""
            if v and v not in vals:
                vals.append(v)
    else:
        # Fallback: pull names directly from the raw array text.
        for m in _NAME_IN_OBJ_RE.finditer(arr_json):
            v = _json_unescape(m.group(1)).strip()
            if v and v not in vals:
                vals.append(v)
    return vals


def _variations(html: str) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    cm = _COLOR_LIST_RE.search(html)
    if cm:
        colors = _variation_values(cm.group(1))
        if len(colors) > 1 or (colors and colors[0]):
            if colors:
                out["color"] = colors
    sm = _SIZE_LIST_RE.search(html)
    if sm:
        sizes = _variation_values(sm.group(1))
        if sizes:
            out["size"] = sizes
    # Keep only meaningful (non-empty) lists.
    out = {k: v for k, v in out.items() if v}
    return out or None


# --- Seller -------------------------------------------------------------------


def _seller(ld: dict) -> Optional[str]:
    offer = _offer_obj(ld)
    if not offer:
        return None
    seller = offer.get("seller")
    name = seller.get("name") if isinstance(seller, dict) else seller
    if name and str(name).strip() and str(name).strip().lower() not in (
        "dick's sporting goods", "dicks sporting goods", "dick's"
    ):
        return str(name).strip()
    return None


# --- Sporting-goods extras ----------------------------------------------------


def _model_number(ld: dict, specs: dict) -> Optional[str]:
    v = ld.get("mpn") or ld.get("model")
    if v:
        return str(v).strip()
    for k in ("Model", "Model Number", "Style", "Style Number", "Manufacturer Style"):
        if specs.get(k):
            return specs[k]
    return None


def _gender(specs: dict, categories: Optional[list]) -> Optional[str]:
    for k in ("Gender", "Age Group"):
        if specs.get(k):
            return specs[k]
    text = " ".join(categories or []).lower()
    for g, label in (("women", "Women's"), ("men", "Men's"),
                     ("girls", "Girls'"), ("boys", "Boys'"),
                     ("kids", "Kids'"), ("unisex", "Unisex")):
        if g in text:
            return label
    return None


def _sport(specs: dict, html: str) -> Optional[str]:
    for k in ("Sport", "Sport/Activity", "Activity"):
        if specs.get(k):
            return specs[k]
    m = re.search(r'"sport":"((?:[^"\\]|\\.)+)"', html)
    if m:
        v = _json_unescape(m.group(1)).strip()
        if v:
            return v
    return None


def _material(specs: dict) -> Optional[str]:
    for k in ("Material", "Materials", "Fabric", "Upper Material", "Shell"):
        if specs.get(k):
            return specs[k]
    return None


# --- Full visible page text (copied verbatim from crawler/parse.py) -----------


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
