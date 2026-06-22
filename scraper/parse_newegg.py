"""Extract structured fields + full visible text from a Newegg product page.

Self-contained module mirroring the public interface of ``crawler.parse`` /
``crawler.parse_bestbuy`` but targeting newegg.com. Newegg server-renders its
product data as standard schema.org JSON-LD plus a clean, fully-rendered DOM
(no client-side hydration needed for the core fields). We parse in this order
of robustness:

  1. JSON-LD ``@type":"Product"`` — name, brand, sku, mpn/Model, gtin12 (UPC),
     description, image, offers (price/currency/availability), aggregateRating.
     A separate JSON-LD ``BreadcrumbList`` gives the category path and an
     ``ImageObject`` list gives the full-resolution gallery URLs.
  2. Rendered DOM — the authoritative *deal* price block
     (``div.price-current`` current price + ``span.price-was-data`` struck-through
     was-price), the ``div.product-bullets ul li`` Overview key features, the
     ``table.table-horizontal`` Specifications tables, the gallery
     ``img.product-view-img-original`` slides, the ``div.product-seller-sold-by``
     marketplace seller, and the buy-box button state for availability.
  3. Inline JSON / regex fallbacks (UPC code, JSON-LD offers price).

Every individual extractor is wrapped in ``_safe`` so a miss can't abort the
whole row. Missing fields are omitted rather than written as ``None``.

NOTE ON FETCHING: Newegg sits behind Akamai/PerimeterX edge protection that
TLS/JA3-fingerprints clients. Plain ``httpx``/``curl`` are throttled, but a
TLS-impersonating client (``curl_cffi`` with ``impersonate="safari17_0"``)
retrieves the fully server-rendered HTML with HTTP 200 and no JS challenge.
The string "captcha" appears on normal pages only inside a ``rechaptchaConfig``
blob for the review-submission form — it is NOT a block marker. See report.md.
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

DOMAINS = ["newegg.com"]
ID_FIELD = "item_number"
# Akamai + PerimeterX edge protection; cleared by curl_cffi TLS impersonation.
# No JS proof-of-work challenge was served to safari17_0 during development.
BOT_VENDOR = "Akamai/PerimeterX"

# Strings that appear on Newegg's *block / challenge* interstitials. Kept
# intentionally specific so they do NOT match the benign ``rechaptchaConfig``
# (Google reCAPTCHA for the review form) that loads on every normal page.
CAPTCHA_MARKERS = (
    "Are you a human",                 # PerimeterX press-and-hold headline
    "Press & Hold",                    # PerimeterX challenge button
    "Please verify you are a human",
    "px-captcha",                      # PerimeterX captcha element id/class
    "/_px/",                           # PerimeterX challenge path
    "perimeterx",
    "Access Denied",                   # Akamai edge denial
    "You don't have permission to access",
    "Reference&#32;#",                 # Akamai "Reference #18.xxxxx" error id
    "Reference #",
    "errors.edgesuite.net",            # Akamai edge error host
    "Pardon Our Interruption",         # generic bot-block headline
)


# Newegg item number: "N82E16" + 9 digits (legacy SKU), or sometimes other
# letter prefixes. We accept the canonical N-prefixed form and a bare numeric id.
_ITEM_RE = re.compile(r"(N82E\d{11}|[0-9A-Z]{2}E16\d{9})")


def extract_id(url: str) -> Optional[str]:
    """Pull the Newegg item number out of a product URL.

    Handles the canonical ``.../<slug>/p/<item_number>`` and bare
    ``.../p/<item_number>`` path forms, the ``?Item=<item_number>`` query
    param, and the ``/products/<item_number>`` form.
    """
    if not url:
        return None
    # Query param form: ?Item=N82E16...
    m = re.search(r"[?&]Item=([0-9A-Za-z]+)", url)
    if m:
        v = m.group(1).upper()
        mm = _ITEM_RE.search(v)
        if mm:
            return mm.group(1)
        if v:
            return v
    # Path forms: /p/<item> or /products/<item>
    m = re.search(r"/(?:p|products)/([0-9A-Za-z]+)", url)
    if m:
        v = m.group(1).upper()
        mm = _ITEM_RE.search(v)
        if mm:
            return mm.group(1)
        if v and v not in ("PL",):  # "/p/pl" is a listing page, not a product
            return v
    # Last resort: any item-number-shaped token in the URL.
    m = _ITEM_RE.search(url.upper())
    if m:
        return m.group(1)
    return None


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page."""
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    out: dict[str, Any] = {}

    item = extract_id(source_url) or _safe(
        lambda: str(ld.get("sku")) if ld.get("sku") else None
    )
    if item:
        out["item_number"] = item
        out["url"] = f"https://www.newegg.com/p/{item}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, ld, tree))
    _put(out, "brand", _safe(_brand, ld, tree))
    _put(out, "model_number", _safe(_model, ld))

    price = _safe(_price, tree, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld))
    _put(out, "availability", _safe(_availability, tree, ld))
    _put(out, "bullets", _safe(_bullets, tree))
    _put(out, "description", _safe(_description, ld, tree))
    _put(out, "images", _safe(_images, html, tree))
    _put(out, "categories", _safe(_breadcrumb, html, tree))
    _put(out, "specs", _safe(_specs, tree))
    _put(out, "variations", _safe(_variations, tree))
    _put(out, "seller", _safe(_seller, tree))
    # Tech-retail extras Amazon's schema lacks (omitted when absent).
    _put(out, "upc", _safe(_upc, html, ld))
    _put(out, "whats_included", _safe(_whats_included, tree))
    _put(out, "warranty", _safe(_warranty, tree))
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


def _is_product(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, list):
        return "Product" in t
    return t == "Product"


def _jsonld_product(html: str) -> Optional[dict[str, Any]]:
    """Return the first schema.org Product JSON-LD object."""
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and _is_product(obj):
                return obj
    return None


# --- Title / brand / model ----------------------------------------------------


def _title(ld: dict, tree: HTMLParser) -> Optional[str]:
    name = ld.get("name")
    if name:
        return name.strip()
    el = tree.css_first("h1.product-title, h1.product-wrap h1, h1")
    return _text(el) or None


def _brand(ld: dict, tree: HTMLParser) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        n = b.get("name")
        if n:
            return n.strip()
    elif isinstance(b, str) and b.strip():
        return b.strip()
    # DOM fallback: the brand logo / link in the product header.
    el = tree.css_first("div.product-view-brand a, .product-brand a")
    if el is not None:
        v = el.attributes.get("title") or _text(el)
        if v:
            return v.strip()
    return None


def _model(ld: dict) -> Optional[str]:
    v = ld.get("Model") or ld.get("model") or ld.get("mpn")
    if v and str(v).strip():
        return str(v).strip()
    return None


# --- Price (the deal signal lives in the rendered price block) ----------------


def _price(tree: HTMLParser, ld: dict) -> Optional[dict[str, Any]]:
    out: Optional[dict[str, Any]] = None

    # 1) Rendered current-price block. The price is split across a leading "$",
    #    a <strong> integer part, and a <sup> fractional part:
    #      <div class="price-current">$<strong>357</strong><sup>.87</sup></div>
    cur_node = tree.css_first("div.price-current, li.price-current")
    if cur_node is not None:
        strong = cur_node.css_first("strong")
        sup = cur_node.css_first("sup")
        if strong is not None:
            whole = _text(strong)
            frac = _text(sup)  # e.g. ".87" or "87"
            num = whole.replace(",", "")
            if frac:
                frac = frac.lstrip(".")
                num = f"{num}.{frac}"
            try:
                out = {"amount": float(num), "currency": "USD"}
            except ValueError:
                out = None
        # Range marker: <span class="price-current-range"> ... </span> with a
        # second strong. When a real upper bound is present, record the range.
        if out is not None:
            strongs = cur_node.css("strong")
            sups = cur_node.css("sup")
            if len(strongs) >= 2:
                hi_whole = _text(strongs[1]).replace(",", "")
                hi_frac = _text(sups[1]).lstrip(".") if len(sups) >= 2 else ""
                hi_num = f"{hi_whole}.{hi_frac}" if hi_frac else hi_whole
                try:
                    hi = float(hi_num)
                    lo = out["amount"]
                    if hi > lo:
                        out["price_range"] = {"min": lo, "max": hi}
                except ValueError:
                    pass

    # 2) Fallback to JSON-LD offers price.
    if out is None:
        amount = _ld_offer_price(ld)
        if amount is not None:
            out = {"amount": amount, "currency": "USD"}

    if out is None:
        return None

    # List price — the struck-through "was" price near the current price.
    was_node = tree.css_first("span.price-was-data, li.price-was span")
    if was_node is not None:
        was = _parse_money(_text(was_node))
        if was and "amount" in was and was["amount"] > out["amount"]:
            out["list_price"] = was["amount"]

    return out


def _ld_offer_price(ld: dict) -> Optional[float]:
    offers = ld.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None
    for o in offers:
        if not isinstance(o, dict):
            continue
        p = o.get("price") or (o.get("priceSpecification") or {}).get("price")
        try:
            if p is not None:
                return float(p)
        except (TypeError, ValueError):
            continue
    return None


# --- Rating -------------------------------------------------------------------


def _rating(ld: dict) -> Optional[dict[str, Any]]:
    agg = ld.get("aggregateRating")
    if not isinstance(agg, dict):
        return None
    out: dict[str, Any] = {}
    stars = agg.get("ratingValue")
    count = agg.get("reviewCount") or agg.get("ratingCount")
    try:
        if stars is not None:
            out["stars"] = float(stars)
    except (TypeError, ValueError):
        pass
    try:
        if count is not None:
            out["count"] = int(count)
    except (TypeError, ValueError):
        pass
    return out or None


# --- Availability -------------------------------------------------------------


def _availability(tree: HTMLParser, ld: dict) -> Optional[str]:
    # Buy-box button state is the most reliable rendered signal.
    btn = tree.css_first("div.product-buy-box button.btn-primary, "
                         "div.product-buy button.btn-primary, "
                         "#ProductBuy button.btn-primary")
    if btn is not None:
        label = _text(btn)
        low = label.lower()
        if "add to cart" in low:
            return "In Stock"
        if "pre-order" in low or "pre order" in low:
            return "Pre-Order"
        if "notify" in low or "auto notify" in low:
            return "Out of Stock"
        if "sold out" in low or "out of stock" in low:
            return "Out of Stock"
        if label:
            return label
    # JSON-LD offer availability.
    offers = ld.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if isinstance(offers, list):
        for o in offers:
            a = o.get("availability") if isinstance(o, dict) else ""
            a = a or ""
            if "InStock" in a:
                return "In Stock"
            if "OutOfStock" in a or "SoldOut" in a:
                return "Out of Stock"
            if "PreOrder" in a:
                return "Pre-Order"
    return None


# --- Bullets (Overview key features) & description ----------------------------


def _bullets(tree: HTMLParser) -> Optional[list[str]]:
    out: list[str] = []
    for li in tree.css("div.product-bullets ul li"):
        t = _text(li)
        if t:
            out.append(t)
    # Dedup preserving order.
    seen = set()
    deduped = []
    for b in out:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped or None


def _description(ld: dict, tree: HTMLParser) -> Optional[str]:
    # JSON-LD description, but only when it adds info beyond the title (Newegg
    # frequently sets description == name, which is not a useful description).
    desc = ld.get("description")
    name = ld.get("name")
    if desc and desc.strip() and desc.strip() != (name or "").strip():
        return desc.strip()
    return None


# --- Images -------------------------------------------------------------------


def _upscale(u: str) -> str:
    """Promote a gallery thumbnail URL to its full-resolution equivalent.

    Newegg gallery slides use a sized path segment like ``/productimage/nb640/``;
    dropping it to ``/ProductImage/`` yields the original full-res asset.
    """
    return re.sub(r"/productimage/[^/]+/", "/ProductImage/", u, flags=re.IGNORECASE)


def _images(html: str, tree: HTMLParser) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    # 1) Rendered gallery. The same class covers both the large main slides
    #    (``/productimage/nb640/...``) and the small thumbnail strip
    #    (``/ProductImageCompressAll60/...``). Route compressed thumbs to
    #    ``thumbnails`` and upscale the main slides to full-res ``ProductImage``.
    for img in tree.css("img.product-view-img-original"):
        src = img.attributes.get("src") or img.attributes.get("data-src") or ""
        if not src:
            continue
        if "ProductImageCompressAll" in src:
            if src not in thumbs:
                thumbs.append(src)
            continue
        if src not in thumbs:
            thumbs.append(src)
        full = _upscale(src)
        if full not in main:
            main.append(full)

    # 2) JSON-LD ImageObject list carries full-res ProductImage URLs.
    for d in _iter_jsonld(html):
        objs = d if isinstance(d, list) else [d]
        for o in objs:
            if isinstance(o, dict) and o.get("@type") == "ImageObject":
                u = o.get("thumbnailUrl") or o.get("contentUrl") or o.get("url")
                if u and u not in main:
                    main.append(u)

    # 3) Fallback: the og:image meta / Product JSON-LD single image.
    if not main:
        ld = _jsonld_product(html) or {}
        img = ld.get("image")
        if isinstance(img, str) and img:
            main.append(img)
        elif isinstance(img, list):
            for u in img:
                if isinstance(u, str) and u and u not in main:
                    main.append(u)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    return result or None


# --- Categories / specs / variations / seller ---------------------------------


def _breadcrumb(html: str, tree: HTMLParser) -> Optional[list[str]]:
    # Primary: BreadcrumbList JSON-LD.
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and obj.get("@type") == "BreadcrumbList":
                crumbs = []
                for it in obj.get("itemListElement", []):
                    item = it.get("item") if isinstance(it, dict) else None
                    if isinstance(item, dict):
                        name = item.get("name")
                    else:
                        name = it.get("name") if isinstance(it, dict) else None
                    if name and name.strip().lower() != "home":
                        crumbs.append(name.strip())
                if crumbs:
                    return crumbs
    # Fallback: rendered breadcrumb nav.
    crumbs = [
        _text(a) for a in tree.css("div.breadcrumbs ol li a, ol.breadcrumb li a")
    ]
    crumbs = [c for c in crumbs if c and c.strip().lower() != "home"]
    return crumbs or None


def _specs(tree: HTMLParser) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for table in tree.css("table.table-horizontal"):
        cap = _text(table.css_first("caption"))
        for row in table.css("tr"):
            th = row.css_first("th")
            td = row.css_first("td")
            if th is None or td is None:
                continue
            k = _text(th).rstrip(":").strip()
            v = _text(td)
            # Skip Amazon-style BSR equivalents and empty rows.
            if not k or not v:
                continue
            if "Best Seller Ranking" in k or "Best Sellers Rank" in k:
                continue
            if k in out:
                continue
            out[k] = v
    return out or None


def _variations(tree: HTMLParser) -> Optional[dict[str, list[str]]]:
    """Selectable product variations (color/size/configuration), when present.

    Most Newegg single-SKU tech pages have no variations; this captures the
    combo/variation selector grids on pages that do.
    """
    out: dict[str, list[str]] = {}
    for grp in tree.css("div.product-options div.product-option, "
                        "div.product-variation div.product-option"):
        label_el = grp.css_first(".product-option-title, .product-option-label, label")
        label = _text(label_el).rstrip(":").strip().lower()
        if not label:
            continue
        values: list[str] = []
        for opt in grp.css("a[title], button[title], li[title]"):
            v = (opt.attributes.get("title") or _text(opt)).strip()
            if v and v not in values:
                values.append(v)
        if label and len(values) > 1:
            out[label] = values
    return out or None


def _seller(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("div.product-seller-sold-by")
    if el is None:
        return None
    # The block reads "Sold by <Newegg | seller name>". Prefer the linked /
    # bolded seller name; treat Newegg-direct as no marketplace seller.
    name_el = el.css_first("a strong span, a strong, strong span, strong, a")
    name = _text(name_el)
    if not name:
        txt = _text(el)
        m = re.search(r"Sold\s+by\s+(.+)", txt, re.IGNORECASE)
        name = m.group(1).strip() if m else ""
    name = name.strip()
    if not name or name.lower() == "newegg":
        return None
    return name


# --- Tech-retail extras -------------------------------------------------------

_UPC_JSON_RE = re.compile(r'"UPCCode"\s*:\s*"(\d{8,14})"')


def _upc(html: str, ld: dict) -> Optional[str]:
    for key in ("gtin12", "gtin13", "gtin", "gtin14"):
        v = ld.get(key)
        if v and str(v).strip().isdigit():
            return str(v).strip()
    m = _UPC_JSON_RE.search(html)
    if m:
        return m.group(1)
    return None


def _whats_included(tree: HTMLParser) -> Optional[list[str]]:
    """Package-contents list when Newegg renders a 'Package Contents' section.

    The contents live in a single ``<td>`` with the entries separated by
    ``<br>`` tags, so we split on the line breaks rather than reading the
    collapsed cell text.
    """
    for table in tree.css("table.table-horizontal"):
        cap = _text(table.css_first("caption")).lower()
        if not ("package" in cap or "what's in" in cap or "in the box" in cap):
            continue
        items: list[str] = []
        for row in table.css("tr"):
            td = row.css_first("td")
            if td is None:
                continue
            # Split on <br> by replacing them with a sentinel before text().
            raw = td.html or ""
            raw = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
            frag = HTMLParser(raw).text(separator="\n")
            for line in frag.split("\n"):
                line = line.strip()
                if line:
                    items.append(line)
        if items:
            return items
    return None


def _warranty(tree: HTMLParser) -> Optional[str]:
    for table in tree.css("table.table-horizontal"):
        for row in table.css("tr"):
            th = row.css_first("th")
            td = row.css_first("td")
            if th is None or td is None:
                continue
            k = _text(th).lower()
            if "warranty" in k:
                v = _text(td)
                if v:
                    return v
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
