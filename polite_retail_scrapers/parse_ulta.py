"""Extract structured fields + full visible text from an Ulta Beauty product page.

Mirrors the public interface of ``crawler/parse.py`` (Amazon) and
``crawler/parse_sephora.py`` (the beauty sibling) so the generic fetcher /
frontier / store modules drive it unchanged. Self-contained: the small helpers
it needs (``_safe``, ``_put``, ``_text``, ``_parse_money``, ``_page_text``) are
copied here rather than imported, per the build spec.

Parsing strategy, most-robust first:

  1. JSON-LD ``<script type="application/ld+json">``. Ulta server-renders a
     ``@type":"Product"`` object (name, brand, description, productID, sku,
     image, size, offers{price,priceCurrency,availability,url}, aggregateRating)
     plus a ``BreadcrumbList``. This is the most stable cross-page source and
     supplies title, brand, price, rating, availability, categories, one image.

  2. The embedded Apollo/GraphQL state blob assigned to
     ``window.__APOLLO_STATE__`` inside ``<script id='apollo_state'>``. It is a
     JSON object whose page modules carry the rich fields JSON-LD lacks:
       * ``ProductPricing``  -> listPrice / salePrice (deal signal), brandName,
                                 productName, productCategory*, productDiscount,
                                 promotionTags, image, skuId, productId.
       * ``ProductDetail``   -> description (markdown), usage (how_to_use),
                                 ingredients, rating, reviewCount.
       * ``MediaGallery``    -> full ordered product image list (items[].imageUrl).
       * ``ProductVariant``  -> variants[] (size / shade names) + variantType.
     The blob is followed by more ``window.__X__ = ...`` assignments in the same
     <script>, so it is extracted with ``json.JSONDecoder().raw_decode`` (stops
     at the end of the first JSON object) rather than slicing to ``</script>``.

  3. CSS on the rendered DOM as a final fallback (h1 title, og:image, etc.).

Sale signal: Ulta stores the regular price in ``listPrice`` ("$181.00") and the
discounted price in ``salePrice`` (null when not on sale). When ``salePrice``
exists and is below ``listPrice``, ``price.amount`` is the sale price and
``price.list_price`` is the regular original (mirrors the Amazon/Sephora deal
convention). JSON-LD ``offers.price`` is the current/effective price and is used
when the Apollo blob is unavailable.

Every individual extractor is wrapped in ``_safe`` so one selector miss can't
abort the whole record. Missing fields are omitted, not stored as ``None``.
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

# --- Module interface contract (consumed by the generic crawler) -------------

DOMAINS = ["ulta.com"]
ID_FIELD = "product_id"

# Ulta fronts its pages with Akamai Bot Manager (the same vendor as Sephora).
# Real product pages embed Akamai's sensor/telemetry JS (the ``ak.v`` / ``ak.cp``
# / ``*.akamaihd.net`` markers appear inline on every legitimate page, so those
# are NOT useful as block markers). When the TLS fingerprint / behavioral check
# fails, Akamai serves either a 403 "Access Denied" page (AkamaiGHost) or a PoW
# challenge interstitial ("Pardon Our Interruption"). The 5 saved pages were
# fetched with curl_cffi ``impersonate="safari17_0"``, which clears Akamai and
# returns the full ~1.3MB product HTML.
BOT_VENDOR = "Akamai"

# Strings that appear on Ulta's Akamai block / challenge responses and
# effectively never on a real product page. Kept tight to avoid false positives;
# the inline Akamai sensor markers (akamaihd.net, ak.v) are deliberately excluded
# because they appear on every legitimate product page.
CAPTCHA_MARKERS = [
    "AkamaiGHost",                  # 403 "Access Denied" block page server banner
    "Access Denied",
    "errors.edgesuite.net",         # Akamai edge error host on the block page
    "Reference&#32;&#35;",          # entity-encoded "Reference #" on block page
    "Pardon Our Interruption",      # bot interstitial headline
    "/_sec/cp_challenge/",          # Akamai proof-of-work challenge path
    "Why did this happen?",         # interstitial copy
    "Request unsuccessful. Incapsula",  # belt-and-braces if WAF ever changes
]

# Ulta product URLs look like:
#   https://www.ulta.com/p/<slug>-pimprod<digits>?sku=<skuId>
# product_id is the trailing pimprod token.
_PID_RE = re.compile(r"\b(pimprod\d+)\b", re.IGNORECASE)


def extract_id(url: str) -> Optional[str]:
    """Return the Ulta ``pimprod...`` product id from a URL, or None."""
    if not url:
        return None
    m = _PID_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(product_id: str) -> str:
    return f"https://www.ulta.com/p/{product_id}"


# --- Public entry point -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from an Ulta product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    crumbs_ld = _safe(_jsonld_breadcrumb, html) or []

    state = _safe(_apollo_state, html) or {}
    pricing = _safe(_find_module, state, "ProductPricing", lambda d: "productName" in d and "listPrice" in d) or {}
    detail = _safe(_find_module, state, "ProductDetail", lambda d: "ingredients" in d or "usage" in d) or {}
    gallery = _safe(_find_module, state, "MediaGallery", lambda d: "items" in d) or {}
    pvariant = _safe(_find_module, state, "ProductVariant", lambda d: "variants" in d) or {}

    out: dict[str, Any] = {}

    pid = (
        extract_id(source_url)
        or (pricing.get("productId") if isinstance(pricing.get("productId"), str) else None)
        or (ld.get("productID") if isinstance(ld.get("productID"), str) else None)
    )
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.ulta.com/p/{pid}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, pricing, ld))
    _put(out, "brand", _safe(_brand, tree, pricing, ld))

    price = _safe(_price, pricing, pvariant, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, detail, ld))
    _put(out, "availability", _safe(_availability, pricing, ld))
    _put(out, "bullets", _safe(_bullets, detail))
    _put(out, "description", _safe(_description, detail, ld))
    _put(out, "images", _safe(_images, gallery, pvariant, pricing, ld))
    _put(out, "categories", _safe(_breadcrumb, pricing, crumbs_ld, tree))
    _put(out, "specs", _safe(_specs, pricing, ld, pvariant))
    _put(out, "variations", _safe(_variations, pvariant))
    _put(out, "seller", _safe(_seller, ld))

    # Beauty-specific extras Amazon/ASOS lack.
    _put(out, "highlights", _safe(_bullets, detail))
    _put(out, "ingredients", _safe(_ingredients, detail))
    _put(out, "how_to_use", _safe(_how_to_use, detail))
    _put(out, "size", _safe(_size, pricing, ld))
    _put(out, "details", _safe(_details_extra, detail))

    _put(out, "page_text", _safe(_page_text, html))
    return out


# --- Generic helpers (copied to keep the module self-contained) ---------------


def _put(d: dict[str, Any], key: str, value: Any) -> None:
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


_PRICE_RE = re.compile(r"([^\d\s]{0,3}?)\s*([\d,]+(?:\.\d+)?)")
_CCY_SYMBOL = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "C$": "CAD"}


def _parse_money(s: str) -> Optional[dict[str, Any]]:
    s = (s or "").strip().replace("\xa0", " ")
    if not s:
        return None
    m = _PRICE_RE.search(s)
    if not m:
        return None
    symbol = (m.group(1) or "").strip()
    amount = float(m.group(2).replace(",", ""))
    currency = _CCY_SYMBOL.get(symbol, "")
    return {"amount": amount, "currency": currency} if currency else {"amount": amount}


def _money_amount(s: Any) -> Optional[float]:
    """Pull just the numeric amount from a price string like ``$181.00``."""
    if isinstance(s, (int, float)):
        return float(s)
    m = _parse_money(s) if isinstance(s, str) else None
    return m.get("amount") if m else None


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


def _clean_md_text(s: Any) -> Optional[str]:
    """Light markdown -> plain text: strip ``#`` headings, ``-`` bullet markers,
    ``[label](url)`` link syntax, ``**bold**``; collapse blank runs."""
    if not isinstance(s, str) or not s.strip():
        return None
    s = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", s)   # links -> label
    s = re.sub(r"^\s{0,3}#{1,6}\s*", "", s, flags=re.MULTILINE)  # headings
    s = re.sub(r"^\s*[-*]\s+", "", s, flags=re.MULTILINE)        # bullet markers
    s = re.sub(r"^\s*Item\s+\d+\s*$", "", s, flags=re.MULTILINE)  # trailing "Item NNNN" line
    s = s.replace("**", "").replace("__", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip() or None


# --- JSON source extraction ---------------------------------------------------


def _iter_jsonld(html: str):
    """Yield each parsed JSON-LD object found in the page (handles @graph arrays)."""
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        raw = (node.text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw))
            except Exception:
                continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict) and isinstance(item.get("@graph"), list):
                yield from (g for g in item["@graph"] if isinstance(g, dict))
            elif isinstance(item, dict):
                yield item


def _jsonld_product(html: str) -> Optional[dict[str, Any]]:
    for obj in _iter_jsonld(html):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            return obj
    return None


def _jsonld_breadcrumb(html: str) -> Optional[list[str]]:
    for obj in _iter_jsonld(html):
        if obj.get("@type") == "BreadcrumbList":
            names: list[str] = []
            for it in (obj.get("itemListElement") or []):
                if not isinstance(it, dict):
                    continue
                nm = it.get("name") or (it.get("item") or {}).get("name") if isinstance(it.get("item"), dict) else it.get("name")
                if nm:
                    names.append(str(nm).strip())
            if names:
                return names
    return None


def _apollo_state(html: str) -> Optional[dict[str, Any]]:
    """Parse the ``window.__APOLLO_STATE__ = {...}`` JSON object.

    The assignment lives inside ``<script id='apollo_state'>`` alongside many
    other ``window.__X__ = ...`` lines, so slicing to ``</script>`` over-captures.
    ``raw_decode`` stops cleanly at the end of the first JSON value.
    """
    i = html.find("window.__APOLLO_STATE__")
    if i == -1:
        return None
    start = html.find("{", i)
    if start == -1:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(html[start:])
    except Exception:
        return None
    return obj if isinstance(obj, dict) else None


def _find_module(state: dict, module_name: str, predicate=None) -> Optional[dict]:
    """Depth-first search the Apollo state for the first dict whose
    ``moduleName``/``type`` equals ``module_name`` (and passes ``predicate``)."""
    found: list[dict] = []

    def walk(d):
        if found:
            return
        if isinstance(d, dict):
            if (d.get("moduleName") == module_name or d.get("type") == module_name):
                if predicate is None or predicate(d):
                    found.append(d)
                    return
            for v in d.values():
                walk(v)
        elif isinstance(d, list):
            for v in d:
                walk(v)

    walk(state)
    return found[0] if found else None


# --- Field extractors ---------------------------------------------------------


def _title(tree: HTMLParser, pricing: dict, ld: dict) -> Optional[str]:
    v = pricing.get("productName")
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(ld.get("name"), str) and ld["name"].strip():
        return ld["name"].strip()
    for sel in ('h1[class*="ProductName"]', "h1"):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


def _brand(tree: HTMLParser, pricing: dict, ld: dict) -> Optional[str]:
    v = pricing.get("brandName")
    if isinstance(v, str) and v.strip():
        return v.strip()
    b = ld.get("brand")
    if isinstance(b, dict) and isinstance(b.get("name"), str):
        return b["name"].strip() or None
    if isinstance(b, str) and b.strip():
        return b.strip()
    return None


def _price(pricing: dict, pvariant: dict, ld: dict) -> Optional[dict[str, Any]]:
    """Build the price dict.

    Primary source is the Apollo ``ProductPricing`` ``listPrice`` / ``salePrice``
    strings. salePrice (when present and below listPrice) is the deal: amount is
    the sale price, list_price is the struck-through regular original. The
    per-variant size/shade prices are aggregated into price_range when they span
    a range. JSON-LD ``offers.price`` (the effective price) is the fallback.
    """
    pricing = pricing if isinstance(pricing, dict) else {}
    list_amt = _money_amount(pricing.get("listPrice")) or _money_amount(pricing.get("productListPrice"))
    sale_amt = _money_amount(pricing.get("salePrice")) or _money_amount(pricing.get("productSalePrice"))

    amount: Optional[float] = None
    list_price: Optional[float] = None
    if sale_amt is not None and (list_amt is None or sale_amt < list_amt):
        amount = sale_amt
        if list_amt is not None and list_amt > sale_amt:
            list_price = list_amt
    elif list_amt is not None:
        amount = list_amt

    currency = "USD"

    # Per-variant price range (different sizes/shades may carry their own prices).
    child_amounts: list[float] = []
    for v in (pvariant.get("variants") or []):
        if not isinstance(v, dict):
            continue
        a = _money_amount(v.get("salePrice")) or _money_amount(v.get("listPrice"))
        if a is not None:
            child_amounts.append(a)

    if amount is None:
        # JSON-LD offers fallback.
        offers = ld.get("offers")
        offers = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
        amts: list[float] = []
        for off in offers:
            if not isinstance(off, dict):
                continue
            if off.get("priceCurrency"):
                currency = off["priceCurrency"]
            for k in ("price", "lowPrice", "highPrice"):
                a = _money_amount(off.get(k))
                if a is not None:
                    amts.append(a)
        if amts:
            amount = min(amts)
            child_amounts += amts

    if amount is None:
        return None

    out: dict[str, Any] = {"amount": amount, "currency": currency}
    if list_price is not None:
        out["list_price"] = list_price

    if child_amounts:
        lo, hi = min(child_amounts + [amount]), max(child_amounts + [amount])
        if hi > lo:
            out["price_range"] = {"min": lo, "max": hi}
    return out


def _rating(detail: dict, ld: dict) -> Optional[dict[str, Any]]:
    out: dict[str, Any] = {}
    stars = detail.get("rating") if isinstance(detail, dict) else None
    cnt = detail.get("reviewCount") if isinstance(detail, dict) else None
    if stars in (None, ""):
        ar = ld.get("aggregateRating")
        if isinstance(ar, dict):
            stars = ar.get("ratingValue")
            cnt = ar.get("reviewCount") or ar.get("ratingCount")
    if stars not in (None, ""):
        try:
            out["stars"] = round(float(stars), 2)
        except (TypeError, ValueError):
            pass
    if cnt not in (None, ""):
        try:
            out["count"] = int(float(cnt))
        except (TypeError, ValueError):
            pass
    return out or None


def _availability(pricing: dict, ld: dict) -> Optional[str]:
    pricing = pricing if isinstance(pricing, dict) else {}
    unavailable = pricing.get("unavailable")
    if unavailable is True:
        return "OutOfStock"
    offers = ld.get("offers")
    offers = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
    for off in offers:
        if isinstance(off, dict) and off.get("availability"):
            return str(off["availability"]).rsplit("/", 1)[-1]  # schema.org/InStock -> InStock
    if unavailable is False:
        return "InStock"
    return None


# Markdown bullet line: "- Top - red and dark berries" (skip headings).
_MD_BULLET_RE = re.compile(r"^\s{0,3}[-*]\s+(.+?)\s*$")
_BULLET_NOISE = re.compile(r"^(item\s+\d+|sku\s|see\b)", re.IGNORECASE)


def _bullets(detail: dict) -> Optional[list[str]]:
    """Ulta puts highlights as markdown bullet lines inside the Details
    description (``- Top - ...``). Pull each ``- `` line as a bullet."""
    detail = detail if isinstance(detail, dict) else {}
    desc = detail.get("description")
    if not isinstance(desc, str) or not desc.strip():
        return None
    out: list[str] = []
    for line in desc.splitlines():
        m = _MD_BULLET_RE.match(line)
        if not m:
            continue
        t = m.group(1).strip()
        t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t).replace("**", "")
        if t and not _BULLET_NOISE.match(t) and t not in out:
            out.append(t)
    return out or None


def _description(detail: dict, ld: dict) -> Optional[str]:
    detail = detail if isinstance(detail, dict) else {}
    cleaned = _clean_md_text(detail.get("description"))
    if cleaned:
        return cleaned
    d = ld.get("description")
    if isinstance(d, str) and d.strip():
        return re.sub(r"\s+", " ", d).strip()
    return None


# --- Images -------------------------------------------------------------------


def _img_url(u: Any) -> Optional[str]:
    if isinstance(u, dict):
        u = u.get("imageUrl") or u.get("url")
    if not isinstance(u, str) or not u.strip():
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif u.startswith("/"):
        u = "https://www.ulta.com" + u
    # Drop CDN sizing query (?w=500&h=500) to keep the canonical full-res asset.
    return u.split("?", 1)[0]


def _images(gallery: dict, pvariant: dict, pricing: dict, ld: dict) -> Optional[dict[str, list]]:
    main: list[str] = []
    gallery = gallery if isinstance(gallery, dict) else {}
    for it in (gallery.get("items") or []):
        if not isinstance(it, dict):
            continue
        u = _img_url(it.get("imageUrl"))
        if u and u not in main:
            main.append(u)

    if not main:
        u = _img_url((pricing or {}).get("image"))
        if u:
            main.append(u)
    if not main:
        im = ld.get("image")
        cand = [im] if isinstance(im, str) else (im if isinstance(im, list) else [])
        for c in cand:
            u = _img_url(c)
            if u and u not in main:
                main.append(u)

    # Per-variant swatch images (shade products); keyed by variant name.
    variants: dict[str, list[str]] = {}
    for v in ((pvariant or {}).get("variants") or []):
        if not isinstance(v, dict):
            continue
        key = v.get("name") or v.get("shadeDescription") or v.get("skuId")
        if not key:
            continue
        urls: list[str] = []
        for ik in ("mainImage", "swatchImage", "smooshImage"):
            u = _img_url(v.get(ik))
            if u and u not in urls:
                urls.append(u)
        if urls:
            variants[str(key)] = urls

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if variants and len(variants) > 1:
        result["variants"] = variants
    return result or None


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(pricing: dict, crumbs_ld: list, tree: HTMLParser) -> Optional[list[str]]:
    # 1) JSON-LD BreadcrumbList (top-level first, includes "Home").
    if isinstance(crumbs_ld, list) and crumbs_ld:
        crumbs = [c for c in crumbs_ld if c and c.lower() != "home"]
        if crumbs:
            return crumbs
    # 2) Apollo ProductPricing colon-delimited category string.
    cat = (pricing or {}).get("productCategory")
    if isinstance(cat, str) and cat.strip():
        parts = [p.strip() for p in cat.split(":") if p.strip()]
        if parts:
            return parts
    # 3) DOM breadcrumb fallback.
    crumbs = [_text(a) for a in tree.css('nav[aria-label*="readcrumb"] a, [class*="Breadcrumb"] a')]
    crumbs = [c for c in crumbs if c and c.lower() != "home"]
    return crumbs or None


def _specs(pricing: dict, ld: dict, pvariant: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    pricing = pricing if isinstance(pricing, dict) else {}
    sku = pricing.get("skuId") or ld.get("sku")
    if sku:
        out["SKU"] = str(sku)
    size = pricing.get("variantLabel") or ld.get("size")
    vtype = pricing.get("variantTypeLabel") or (pvariant or {}).get("variantType")
    if isinstance(size, str) and size.strip():
        label = vtype.strip() if isinstance(vtype, str) and vtype.strip() else "Size"
        out[label] = size.strip()
    return out or None


def _variations(pvariant: dict) -> Optional[dict[str, list[str]]]:
    pvariant = pvariant if isinstance(pvariant, dict) else {}
    vals: list[str] = []
    for v in (pvariant.get("variants") or []):
        if not isinstance(v, dict):
            continue
        name = v.get("name") or v.get("shadeDescription")
        if name and str(name).strip() and str(name).strip() not in vals:
            vals.append(str(name).strip())
    if not vals:
        return None
    vtype = pvariant.get("variantType") or pvariant.get("typeLabel") or "variant"
    return {str(vtype).strip().lower(): vals}


def _seller(ld: dict) -> Optional[str]:
    offers = ld.get("offers")
    offers = offers if isinstance(offers, list) else ([offers] if isinstance(offers, dict) else [])
    for off in offers:
        if isinstance(off, dict):
            s = off.get("seller")
            if isinstance(s, dict) and s.get("name"):
                return str(s["name"]).strip()
    return "Ulta Beauty"


# --- Beauty-specific extras ---------------------------------------------------


def _ingredients(detail: dict) -> Optional[str]:
    detail = detail if isinstance(detail, dict) else {}
    v = detail.get("ingredients")
    if isinstance(v, str) and v.strip():
        return re.sub(r"\s+", " ", v).strip()
    return None


def _how_to_use(detail: dict) -> Optional[str]:
    detail = detail if isinstance(detail, dict) else {}
    return _clean_md_text(detail.get("usage"))


def _size(pricing: dict, ld: dict) -> Optional[str]:
    pricing = pricing if isinstance(pricing, dict) else {}
    for v in (pricing.get("variantLabel"), ld.get("size")):
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _details_extra(detail: dict) -> Optional[str]:
    """Prop 65 / restrictions blurbs Ulta shows under Details, when present."""
    detail = detail if isinstance(detail, dict) else {}
    for k in ("prop65WarningDetail", "restrictions"):
        v = detail.get(k)
        cleaned = _clean_md_text(v)
        if cleaned:
            return cleaned
    return None
