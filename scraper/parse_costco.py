"""Extract structured fields + full visible text from a Costco product page.

Mirrors the field schema of ``crawler/parse.py`` (Amazon) and
``crawler/parse_walmart.py`` (Walmart) so the downstream embedding /
deal-detection layers see a uniform record shape across retailers. The dedup
key on Costco is the numeric ``item_id`` (their product ``pid`` as it appears in
the URL — ``.product.<pid>.html``), analogous to Amazon's ``asin``.

────────────────────────────────────────────────────────────────────────────
IMPORTANT — Costco is BROWSER-ONLY behind Akamai Bot Manager.
────────────────────────────────────────────────────────────────────────────
Product (and category) pages are gated by Akamai's Server-Defined Challenge
(the behavioral "Press & Hold" / proof-of-work shell). A plain HTTP client
(curl_cffi ``safari17_0`` / ``chrome120``, plain ``curl``, ``httpx``) receives
only a ~2.5 KB challenge document — never the real product HTML — because the
``_abck`` cookie stays in its unvalidated ``~-1~`` state until Akamai's
obfuscated sensor JS runs in a real browser and POSTs behavioral telemetry to
flip it to a valid (``~0~``) state. The Costco *homepage* is allowlisted and
loads fully, but that does not clear the challenge for product pages.

Therefore this parser was developed and validated against:
  • a real, fully-rendered archived Costco product page (Wayback Machine),
    which preserves the exact DOM template + ``window.digitalData`` JS object,
  • plus the populated ``window.digitalData`` block captured from a second
    real product snapshot.
To run it in production, the fetch layer must be swapped to a headless browser
(Playwright / Selenium with stealth) or a residential-proxy unblocker that
solves the Akamai challenge. The parsing logic below is unchanged by that.

────────────────────────────────────────────────────────────────────────────
Parsing strategy (most robust first):
  1. PRIMARY  — ``window.digitalData`` inline JS object. Costco server-side
     embeds product core data here:
        digitalData.product.{pid, sku, name, inventoryStatus,
                              priceMin, priceMax, membershipReq}
        digitalData.pageCrumbs  -> breadcrumb category path
     This is the single most reliable source and survives DOM redesigns.
  2. SECONDARY — DOM selectors (Costco uses stable ``automation-id`` hooks and
     ``itemprop`` microdata):
        h1[automation-id="productName"]                 -> title
        span[automation-id="itemNumber"]                -> internal item/SKU
        .product-info-specs .row (.spec-name + value)   -> specs, brand
        div[automation-id="productDetailsOutput"]       -> description
        .product-info-description[automation-id="productDescriptions"] -> bullets
        .online-price .op-value / [data-opvalue]        -> online (list) price
        .your-price .value[automation-id="productPriceOutput"] -> "Your Price"
        ol[itemtype*="BreadcrumbList"] span[itemprop="name"]   -> breadcrumb
        div[data-bv-show="rating_summary"][data-bv-product-id] -> review id
  3. TERTIARY — og:* / product:* meta tags (title, image, availability,
     price:amount — though price:amount is typically EMPTY due to gating).

MEMBERSHIP / SIGN-IN GATING (documented honestly):
  • Many Costco prices render only after sign-in. On a gated page the DOM price
    nodes are literal ``--``/empty and ``<meta product:price:amount>`` is blank.
    When that happens we emit no ``price`` (and set ``member_only=True`` /
    record ``membershipReq``) rather than a fake 0.0. When ``digitalData`` does
    expose ``priceMin``/``priceMax`` (common for openly-priced items) we use it.
  • Ratings come from BazaarVoice, injected via XHR after page load — they are
    NOT in the static/server HTML. We capture the BazaarVoice product id when
    present but cannot read stars/count without executing that XHR.

Every field extractor is wrapped in ``_safe`` so one selector / JSON-path miss
can never abort the whole row. Empty values are dropped via ``_put``.
"""
from __future__ import annotations

import html as _htmllib
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import unquote

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)


# --- Module interface (consumed by the generic crawler) ----------------------

DOMAINS = ["costco.com"]
ID_FIELD = "item_id"
BOT_VENDOR = "Akamai Bot Manager"  # Akamai SDC / behavioral "Press & Hold".

# Strings that appear ONLY on Costco's Akamai challenge / block interstitial,
# never on a real rendered product page. The challenge shell is a ~2.5 KB
# document whose tell-tale markers are the SDC container id, the Akamai
# "Powered and protected by" copy, and the /akam/ sensor pixel/script path.
CAPTCHA_MARKERS = [
    "sec-if-cpt-container",          # SDC behavioral-challenge container id
    "Powered and protected by",      # Akamai challenge footer copy
    "scf-akamai-logo",               # Akamai logo block on the challenge page
    "behavioral-content",            # SDC "Press & Hold" wrapper
    "/akam/",                        # Akamai sensor script / pixel path
    "Access Denied",                 # hard Akamai 403 block page
    "Reference&#32;&#35;",           # Akamai "Reference #..." denial page
]

# Costco product URLs: https://www.costco.com/<slug>.product.<pid>.html
#   e.g. .../lg-86%22-class---ur8000-series---4k-uhd-led-tv.product.4000153824.html
# The numeric <pid> is the dedup key. Slug + query/fragment are ignored.
_ID_RE = re.compile(r"\.product\.(\d{4,})(?:\.html)?(?:[/?#]|$)", re.IGNORECASE)
_ID_BARE = re.compile(r"^\d{4,}$")


def extract_id(url: str) -> Optional[str]:
    """Return the numeric product id from a Costco product URL, or None."""
    if not url:
        return None
    u = unquote(url)
    m = _ID_RE.search(u)
    if m:
        return m.group(1)
    if _ID_BARE.match(u.strip()):
        return u.strip()
    return None


# --- Locators for inline blobs ------------------------------------------------

# window.digitalData = { ... } — a JS-object literal (single-quoted, trailing
# commas, unquoted keys). We grab the balanced {...} after the assignment.
_DIGITAL_DATA_RE = re.compile(r"window\.digitalData\s*=\s*\{", re.IGNORECASE)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


# --- Top-level entrypoint -----------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page.

    Missing fields are omitted rather than included as None. Every individual
    extractor is wrapped so a selector / JSON-path miss can't abort the row.
    """
    tree = HTMLParser(html)
    dd = _safe(_digital_data_product, html) or {}
    crumbs_dd = _safe(_digital_data_crumbs, html) or []
    meta = _safe(_meta_map, tree) or {}

    out: dict[str, Any] = {}

    item_id = (
        extract_id(source_url)
        or (str(dd.get("pid")).strip() or None if dd.get("pid") else None)
    )
    if item_id:
        out["item_id"] = item_id
        out["url"] = f"https://www.costco.com/.product.{item_id}.html"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, dd, tree, meta))
    _put(out, "brand", _safe(_brand, tree))

    price = _safe(_price, dd, tree, meta)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, tree))
    _put(out, "availability", _safe(_availability, dd, tree, meta))
    _put(out, "bullets", _safe(_bullets, tree))
    _put(out, "description", _safe(_description, tree))
    _put(out, "images", _safe(_images, tree, meta))
    _put(out, "categories", _safe(_categories, crumbs_dd, tree))
    _put(out, "specs", _safe(_specs, tree))
    _put(out, "variations", _safe(_variations, tree))
    _put(out, "page_text", _safe(_page_text, html))

    # --- Costco-specific extras (omitted when absent) ------------------------
    _put(out, "model_number", _safe(_model_number, tree))
    _put(out, "dimensions", _safe(lambda: _spec_lookup(tree, _DIM_KEYS)))
    _put(out, "weight", _safe(lambda: _spec_lookup(tree, _WEIGHT_KEYS)))
    member_only = _safe(_member_only, dd, out)
    if member_only is not None:
        out["member_only"] = member_only

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


_PRICE_RE = re.compile(r"([^\d\s]?)\s*([\d,]+(?:\.\d+)?)")


def _parse_money(s: str) -> Optional[dict[str, Any]]:
    """Parse a money-ish string like ``$1,299.99`` -> {amount, currency}."""
    s = (s or "").strip().replace("\xa0", " ")
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
    currency = {"$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY"}.get(symbol, "")
    return {"amount": amount, "currency": currency} if currency else {"amount": amount}


def _num(s: Any) -> Optional[float]:
    """Best-effort numeric coercion; rejects Costco sentinels ('na', '', '--')."""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").lstrip("$").strip()
    if t in ("", "na", "--", "n/a"):
        return None
    try:
        return float(t)
    except ValueError:
        m = re.search(r"[\d]+(?:\.\d+)?", t)
        return float(m.group(0)) if m else None


# --- window.digitalData (PRIMARY source) -------------------------------------


def _digital_data_obj(html: str) -> dict[str, Any]:
    """Return the parsed ``window.digitalData`` object, or {}.

    The blob is a JS-object literal: unquoted keys, single-quoted strings,
    trailing commas, escaped quotes inside names. We slice the balanced ``{...}``
    after the assignment, then coerce it to JSON best-effort.
    """
    m = _DIGITAL_DATA_RE.search(html)
    if not m:
        return {}
    start = m.end() - 1  # position of the opening brace
    depth = 0
    in_str: Optional[str] = None
    esc = False
    end = -1
    for i in range(start, min(len(html), start + 20000)):
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == in_str:
                in_str = None
            continue
        if ch in ("'", '"'):
            in_str = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end < 0:
        return {}
    blob = html[start:end]
    return _js_obj_to_dict(blob)


def _js_obj_to_dict(blob: str) -> dict[str, Any]:
    """Coerce a JS-object literal to a dict via best-effort JSON normalization."""
    s = blob
    # Strip JS line/block comments.
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.DOTALL)
    s = re.sub(r"//[^\n]*", "", s)
    # Quote unquoted object keys:  key :  ->  "key":
    s = re.sub(r"([{,]\s*)([A-Za-z_$][\w$]*)\s*:", r'\1"\2":', s)
    # Convert single-quoted string values/keys to double-quoted. Handle escaped
    # double-quotes that appear inside single-quoted JS strings (e.g. 86\").
    def _sq(match: re.Match) -> str:
        inner = match.group(1)
        inner = inner.replace('\\"', '"').replace('"', '\\"')
        return '"' + inner + '"'

    s = re.sub(r"'((?:[^'\\]|\\.)*)'", _sq, s)
    # Remove trailing commas before } or ].
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except Exception as e:
        log.debug("digitalData JSON coercion failed: %s", e)
        return {}


def _digital_data_product(html: str) -> dict[str, Any]:
    p = _digital_data_obj(html).get("product")
    return p if isinstance(p, dict) else {}


def _digital_data_crumbs(html: str) -> list[str]:
    c = _digital_data_obj(html).get("pageCrumbs")
    if isinstance(c, list):
        return [_nz(x) for x in c if _nz(x)]
    return []


# --- og:/product: meta --------------------------------------------------------


def _meta_map(tree: HTMLParser) -> dict[str, str]:
    out: dict[str, str] = {}
    for el in tree.css("meta[property], meta[name]"):
        key = el.attributes.get("property") or el.attributes.get("name") or ""
        val = el.attributes.get("content") or ""
        if key and key not in out:
            out[key] = _htmllib.unescape(val)
    return out


# --- Field extractors ---------------------------------------------------------


def _title(dd: dict, tree: HTMLParser, meta: dict) -> Optional[str]:
    if _nz(dd.get("name")):
        return _nz(dd.get("name"))
    el = tree.css_first('h1[automation-id="productName"], h1[itemprop="name"]')
    if _text(el):
        return _nz(_text(el))
    t = _nz(meta.get("og:title"))
    if t:
        return t
    el = tree.css_first("title")
    if el is not None:
        t = re.sub(r"\s*\|\s*Costco\s*$", "", _text(el))
        return _nz(t)
    return None


def _brand(tree: HTMLParser) -> Optional[str]:
    # Brand is exposed via microdata inside the specifications table.
    el = tree.css_first('[itemprop="brand"]')
    if _text(el):
        return _nz(_text(el))
    # Fallback: a "Brand" row in the specs table.
    return (_specs(tree) or {}).get("Brand")


def _price(dd: dict, tree: HTMLParser, meta: dict) -> Optional[dict[str, Any]]:
    """Resolve current price + (optional) list_price / range.

    Source priority:
      1. digitalData.product.priceMin/priceMax (most reliable when not gated)
      2. DOM: "Your Price" + "Online Price" nodes
      3. og/product:price:amount meta (usually empty on gated pages)
    A struck-through "Online Price" greater than the "Your Price" is the deal
    signal -> list_price. When everything is gated/empty we return None (no
    fake 0.0); the caller's member_only flag records the gating.
    """
    pmin = _num(dd.get("priceMin"))
    pmax = _num(dd.get("priceMax"))

    cur: Optional[dict[str, Any]] = None
    if pmin is not None:
        cur = {"amount": pmin, "currency": "USD"}
        if pmax is not None and pmax > pmin:
            cur["price_range"] = {"min": pmin, "max": pmax}

    # DOM "Your Price" (the price the member pays) — sentinel '--' when gated.
    your_node = tree.css_first(
        '.your-price .value[automation-id="productPriceOutput"], '
        'span[automation-id="productPriceOutput"]'
    )
    your_amt = _num(_text(your_node)) if your_node is not None else None

    # DOM "Online Price" — the pre-discount list price (often in a data attr).
    online_node = tree.css_first(".online-price")
    online_amt = None
    if online_node is not None:
        online_amt = _num(online_node.attributes.get("data-opvalue"))
        if online_amt is None:
            v = online_node.css_first('.op-value, [automation-id="onlinePriceOutput"]')
            online_amt = _num(_text(v)) if v is not None else None

    if cur is None and your_amt is not None:
        cur = {"amount": your_amt, "currency": "USD"}

    # meta fallback (typically empty due to gating).
    if cur is None:
        mp = _num(meta.get("product:price:amount"))
        if mp is not None:
            ccy = (meta.get("product:price:currency") or "USD").strip() or "USD"
            cur = {"amount": mp, "currency": ccy}

    if cur is None:
        return None

    # Deal signal: online (list) price strictly greater than current paid price.
    if online_amt is not None and online_amt > cur["amount"]:
        cur["list_price"] = online_amt

    return cur


def _rating(tree: HTMLParser) -> Optional[dict[str, Any]]:
    """Costco ratings are rendered client-side by BazaarVoice (XHR-injected).

    The static server HTML only carries the BazaarVoice widget placeholder
    (``data-bv-product-id``); the actual stars/count are not present unless a
    browser executed the BV script. We surface what static markup may carry:
    microdata aggregateRating if Costco ever inlines it, otherwise None.
    """
    val_el = tree.css_first('[itemprop="ratingValue"]')
    cnt_el = tree.css_first('[itemprop="reviewCount"], [itemprop="ratingCount"]')
    stars = _num(val_el.attributes.get("content") or _text(val_el)) if val_el is not None else None
    count = None
    if cnt_el is not None:
        c = _num(cnt_el.attributes.get("content") or _text(cnt_el))
        count = int(c) if c is not None else None
    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out or None


def _availability(dd: dict, tree: HTMLParser, meta: dict) -> Optional[str]:
    inv = _nz(dd.get("inventoryStatus"))
    if inv and inv.lower() != "na":
        return inv.title() if inv.islower() else inv
    av = _nz(meta.get("og:availability"))
    if av:
        return {"instock": "In stock", "outofstock": "Out of stock"}.get(av.lower(), av)
    el = tree.css_first('[automation-id="outOfStockText"], .out-of-stock, .product-availability')
    return _nz(_text(el))


_BULLET_NOISE = re.compile(r"^(specifications|features|description)$", re.IGNORECASE)


def _bullets(tree: HTMLParser) -> Optional[list[str]]:
    """Costco "Product Details / Features" list -> bullets."""
    out: list[str] = []
    for sel in (
        '.product-info-description[automation-id="productDescriptions"] li',
        "#productDescriptions1 li",
        ".product-info-features li",
    ):
        for li in tree.css(sel):
            t = _nz(_text(li))
            if t and not _BULLET_NOISE.match(t) and t not in out:
                out.append(t)
        if out:
            break
    return out or None


def _description(tree: HTMLParser) -> Optional[str]:
    # Full description microdata node (often hidden via class="hide").
    el = tree.css_first('[itemprop="description"][automation-id="productDetailsOutput"], '
                        '[automation-id="productDetailsOutput"], '
                        '[itemprop="description"]')
    if el is not None:
        t = _nz(el.text(separator=" ", strip=True))
        if t:
            return t
    # Fallback: the visible product-info-description block (sans specs/features).
    el = tree.css_first('.product-info-description[automation-id="productDescriptions"]')
    if el is not None:
        t = _nz(el.text(separator=" ", strip=True))
        if t:
            return t
    return None


# Costco product imagery is served from Bynder asset CDNs; site chrome (badges,
# logos, membership banners) comes from mobilecontent.costco.com resource paths.
_PRODUCT_IMG_HOST = re.compile(r"(bfasset\.costco-static\.com|cdn\.bfldr\.com)", re.I)
_IMG_NOISE = re.compile(r"(badge|logo|/resource/img/|/homepage/|membership|"
                        r"flyout|spotlight|instacart|\.svg(?:$|\?))", re.I)


def _is_product_img(u: str) -> bool:
    return bool(u) and u.startswith("http") and bool(_PRODUCT_IMG_HOST.search(u)) \
        and not _IMG_NOISE.search(u)


def _images(tree: HTMLParser, meta: dict) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    # Primary: og:image (full-resolution hero on bfasset.costco-static.com).
    og = _nz(meta.get("og:image"))
    if og and _is_product_img(og):
        main.append(og)

    # Gallery / thumbnail images carry zoom/full URLs in data-* attributes.
    for img in tree.css(
        '.thumbImage img, [automation-id="thumbnailImage"] img, '
        '.product-image-container img, #initialImageContainer img, img.img-responsive'
    ):
        is_thumb = "thumb" in (img.attributes.get("class") or "").lower()
        for attr in ("data-zoom-image", "data-large-image", "data-src", "src"):
            u = img.attributes.get(attr)
            if not _is_product_img(u or ""):
                continue
            if attr in ("data-zoom-image", "data-large-image"):
                if u not in main:
                    main.append(u)
            elif is_thumb or attr in ("data-src", "src"):
                if u not in thumbs:
                    thumbs.append(u)
            break

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    return result or None


def _categories(crumbs_dd: list[str], tree: HTMLParser) -> Optional[list[str]]:
    if crumbs_dd:
        return crumbs_dd
    # DOM fallback: schema.org BreadcrumbList. Drop the leading "Home" + any
    # trailing "404 - Product Not Found" sentinel.
    dom = [
        _nz(_text(span))
        for span in tree.css(
            'ol[itemtype*="BreadcrumbList"] li [itemprop="name"], '
            'ol.crumbs li [itemprop="name"]'
        )
    ]
    dom = [c for c in dom if c and c.lower() != "home" and "not found" not in c.lower()]
    return dom or None


def _specs(tree: HTMLParser) -> Optional[dict[str, str]]:
    """Costco Specifications table: ``.product-info-specs .row`` rows where the
    first cell (``.spec-name``) is the key and the next sibling div the value.
    """
    out: dict[str, str] = {}
    for row in tree.css(".product-info-specs .row"):
        name_el = row.css_first(".spec-name")
        if name_el is None:
            continue
        key = _nz(_text(name_el))
        if not key:
            continue
        # Value is the row's value div — a sibling that is NOT the row wrapper
        # and NOT the .spec-name cell. Skip both to avoid grabbing the row's
        # concatenated "<name><value>" text.
        val = None
        for div in row.css("div"):
            cls = div.attributes.get("class") or ""
            if "spec-name" in cls or "row" in cls.split():
                continue
            val = _nz(_text(div))
            if val:
                break
        if key and val and key not in out:
            out[key] = val
    return out or None


def _variations(tree: HTMLParser) -> Optional[dict[str, list[str]]]:
    """Variant selectors (size/color/etc.) when present on the PDP."""
    out: dict[str, list[str]] = {}
    for grp in tree.css('.attribute-selector, [automation-id*="variant"], .product-variations .variation'):
        label_el = grp.css_first(".attribute-label, label, .variation-label")
        label = _nz(_text(label_el))
        if not label:
            continue
        label = label.rstrip(":").strip().lower()
        values: list[str] = []
        for opt in grp.css("option, li[data-value], button[data-value], .swatch[title]"):
            v = (
                opt.attributes.get("data-value")
                or opt.attributes.get("title")
                or _text(opt)
            )
            v = _nz(v)
            if v and v.lower() not in ("select", "choose") and v not in values:
                values.append(v)
        if values:
            out[label] = values
    return out or None


# --- Costco-specific extras ---------------------------------------------------

# Match on whole-word spec keys to avoid false hits (e.g. "Container Size"
# must NOT satisfy a "dimensions" lookup).
_DIM_KEYS = ("dimensions", "product dimensions", "assembled dimensions",
             "overall dimensions")
_WEIGHT_KEYS = ("weight", "product weight", "item weight", "shipping weight")
_MODEL_KEYS = ("model", "model number", "model no", "manufacturer part number",
               "mfg part number", "mpn")


def _spec_lookup(tree: HTMLParser, keys: tuple[str, ...]) -> Optional[str]:
    specs = _specs(tree) or {}
    lowered = {k.lower(): v for k, v in specs.items()}
    for k in keys:
        if k in lowered:
            return lowered[k]
    # Whole-word partial match (a key token must appear as a word in the spec).
    for lk, v in lowered.items():
        words = set(re.findall(r"[a-z]+", lk))
        if any(all(w in words for w in k.split()) for k in keys):
            return v
    return None


def _model_number(tree: HTMLParser) -> Optional[str]:
    return _spec_lookup(tree, _MODEL_KEYS)


def _member_only(dd: dict, out: dict) -> Optional[bool]:
    """True when the page is membership/sign-in gated for pricing.

    Signals: digitalData.membershipReq is a real (non-'na') value, OR no price
    could be extracted at all (a common symptom of a sign-in-gated PDP).
    """
    req = _nz(dd.get("membershipReq"))
    if req and req.lower() not in ("na", "n/a"):
        return True
    if "price" not in out:
        # Only assert gating when we at least had a product context.
        if out.get("title") or out.get("item_id"):
            return True
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
