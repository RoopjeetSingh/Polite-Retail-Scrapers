"""Extract structured fields + full visible text from a Best Buy product page.

Self-contained module mirroring the public interface of ``crawler.parse`` but
targeting bestbuy.com. Best Buy server-renders its product data as a large
Apollo/GraphQL JSON state embedded in the HTML plus standard schema.org
JSON-LD. We parse in this order of robustness:

  1. JSON-LD ``@type":"Product"`` — name, brand, model, color, sku, offers
     (price/currency/availability/seller), aggregateRating.
  2. Inline GraphQL JSON — the authoritative price-with-savings block
     (``customerPrice`` + ``totalSavings``), ``description.long``, the
     ``features`` highlight list, ``specificationGroups``, the ProductImage
     gallery, ``primaryImage``, ``includedItems``, ``variations``, ``upc``,
     ``customStreetDate``.
  3. CSS / regex fallbacks on the rendered DOM (breadcrumb, button states).

Every individual extractor is wrapped in ``_safe`` so a miss can't abort the
whole row. Missing fields are omitted rather than written as ``None``.

NOTE ON FETCHING: Best Buy is behind Akamai Bot Manager which resets HTTP/2
streams and silently drops HTTP/1.1 for plain httpx/curl (TLS/JA3 fingerprint
filtering). A TLS-impersonating client (``curl_cffi`` with ``impersonate=
"safari17_0"``) is required to retrieve the page. See report.md.
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

DOMAINS = ["bestbuy.com"]
ID_FIELD = "sku"
BOT_VENDOR = "Akamai"  # Akamai Bot Manager (confirmed via TLS stream resets)

# Strings that appear on Best Buy's Akamai block / challenge interstitial.
# These are intentionally specific to the *challenge* page so they do NOT match
# the Akamai SDK script tags / sensor data that load on every normal page.
CAPTCHA_MARKERS = (
    "Pardon Our Interruption",          # Akamai/PerimeterX style block headline
    "As you were browsing, something about your browser made us think",
    "Access Denied",                    # Akamai edge denial page
    "You don't have permission to access",
    "Reference&#32;#",                  # Akamai "Reference #18.xxxxx" error id
    "Reference #",
    "errors.edgesuite.net",             # Akamai edge error host
    "_Incapsula_Resource",              # (defensive) alt WAF marker
    "/_sec/cp_challenge/",              # Akamai challenge path
)


def extract_id(url: str) -> Optional[str]:
    """Pull the numeric Best Buy SKU out of a product URL.

    Handles the canonical ``.../site/<slug>/<sku>.p?skuId=<sku>`` form as well
    as the newer ``.../product/<slug>/<bsin>/sku/<sku>`` form, plus any URL that
    simply carries a ``skuId=`` query param.
    """
    if not url:
        return None
    # Query param first — most reliable, present on nearly every product link.
    m = re.search(r"[?&]skuId=(\d{5,})", url)
    if m:
        return m.group(1)
    # Path form: ".../<sku>.p"
    m = re.search(r"/(\d{5,})\.p\b", url)
    if m:
        return m.group(1)
    # Newer canonical form: ".../sku/<sku>"
    m = re.search(r"/sku/(\d{5,})\b", url)
    if m:
        return m.group(1)
    return None


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page."""
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    out: dict[str, Any] = {}

    sku = extract_id(source_url) or _safe(lambda: str(ld.get("sku")) if ld.get("sku") else None)
    if sku:
        out["sku"] = sku
        out["url"] = f"https://www.bestbuy.com/site/-/{sku}.p?skuId={sku}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, ld, tree))
    _put(out, "brand", _safe(_brand, ld))
    _put(out, "model_number", _safe(lambda: ld.get("model") or None))

    price = _safe(_price, html, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld))
    _put(out, "availability", _safe(_availability, html, ld))
    _put(out, "bullets", _safe(_bullets, html))
    _put(out, "description", _safe(_description, html))
    _put(out, "images", _safe(_images, html))
    _put(out, "categories", _safe(_breadcrumb, html, tree))
    _put(out, "specs", _safe(_specs, html))
    _put(out, "variations", _safe(_variations, html))
    _put(out, "seller", _safe(_seller, ld))
    # Tech-retail extras Amazon's schema lacks.
    _put(out, "whats_included", _safe(_whats_included, html))
    _put(out, "release_date", _safe(_release_date, html))
    _put(out, "upc", _safe(_upc, html))
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


def _jsonld_product(html: str) -> Optional[dict[str, Any]]:
    """Return the first schema.org Product JSON-LD object (handles @graph/lists)."""
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and _is_product(obj):
                return obj
    return None


def _is_product(obj: dict) -> bool:
    t = obj.get("@type")
    if isinstance(t, list):
        return "Product" in t
    return t == "Product"


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


# --- Title / brand ------------------------------------------------------------


def _title(ld: dict, tree: HTMLParser) -> Optional[str]:
    name = ld.get("name")
    if name:
        return name.strip()
    el = tree.css_first("h1.heading-5, div.sku-title h1, h1")
    return _text(el) or None


def _brand(ld: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        n = b.get("name")
        if n:
            # Best Buy appends a trademark glyph to some house brands (Insignia™).
            return n.replace("™", "").strip() or n.strip()
    elif isinstance(b, str) and b:
        return b.replace("™", "").strip()
    return None


# --- Price --------------------------------------------------------------------

# Authoritative price block in the embedded GraphQL state. The customerPrice is
# the price the customer pays now; totalSavings (when > 0) is the discount off
# the regular price, so list_price = customerPrice + totalSavings. The two values
# can be separated by other keys (connectionType, mobileContracts, ...), so we
# grab the whole ItemPrice object body and pull each field out of it separately.
_PRICE_BLOCK_RE = re.compile(
    r'"price":\{"__typename":"ItemPrice",([^{}]*?"customerPrice"[^{}]*?)\}'
)
_CUSTOMER_PRICE_IN_BLOCK_RE = re.compile(r'"customerPrice":([\d.]+)')
_TOTAL_SAVINGS_IN_BLOCK_RE = re.compile(r'"totalSavings":([\d.]+)')
# Any standalone customerPrice as a last resort (TV/sold-out items still carry it).
_ANY_CUSTOMER_PRICE_RE = re.compile(r'"customerPrice":([\d.]+)')


def _price(html: str, ld: dict) -> Optional[dict[str, Any]]:
    amount: Optional[float] = None
    list_price: Optional[float] = None

    # 1) Authoritative price-with-savings block.
    m = _PRICE_BLOCK_RE.search(html)
    if m:
        body = m.group(1)
        cm = _CUSTOMER_PRICE_IN_BLOCK_RE.search(body)
        if cm:
            amount = float(cm.group(1))
            sm = _TOTAL_SAVINGS_IN_BLOCK_RE.search(body)
            if sm:
                sav = float(sm.group(1))
                if sav > 0:
                    list_price = round(amount + sav, 2)

    # 2) JSON-LD offers — prefer the NewCondition offer (offers may include
    #    Open-Box/Used conditions whose price is lower and must NOT win).
    if amount is None:
        offer = _new_offer(ld)
        if offer is not None:
            try:
                amount = float(offer.get("price"))
            except (TypeError, ValueError):
                amount = None

    # 3) Any customerPrice anywhere in the embedded JSON (covers sold-out items
    #    whose JSON-LD offers list is empty).
    if amount is None:
        m2 = _ANY_CUSTOMER_PRICE_RE.search(html)
        if m2:
            amount = float(m2.group(1))

    if amount is None:
        return None

    out: dict[str, Any] = {"amount": amount, "currency": "USD"}
    if list_price and list_price > amount:
        out["list_price"] = list_price
    return out


def _new_offer(ld: dict) -> Optional[dict]:
    offers = ld.get("offers")
    if isinstance(offers, dict):
        offers = [offers]
    if not isinstance(offers, list):
        return None
    new, fallback = None, None
    for o in offers:
        if not isinstance(o, dict):
            continue
        fallback = fallback or o
        cond = (o.get("itemCondition") or "")
        desc = (o.get("description") or "").lower()
        if "NewCondition" in cond or desc == "new":
            new = o
            break
    return new or fallback


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

_BUTTON_STATE_RE = re.compile(r'"buttonState":"([A-Z_]+)"')
_BUTTON_LABELS = {
    "ADD_TO_CART": "Add to Cart",
    "PRE_ORDER": "Pre-Order",
    "COMING_SOON": "Coming Soon",
    "CHECK_STORES": "Available In Store",
    "FIND_A_STORE": "Available In Store",
    "SOLD_OUT": "Sold Out",
    "NOT_AVAILABLE": "Sold Out",
    "WAITLIST": "Sold Out",
}


def _availability(html: str, ld: dict) -> Optional[str]:
    states = []
    for s in _BUTTON_STATE_RE.findall(html):
        if s not in states:
            states.append(s)
    if states:
        # If a purchasable state exists, that wins; otherwise report the first.
        for pref in ("ADD_TO_CART", "PRE_ORDER", "COMING_SOON", "CHECK_STORES"):
            if pref in states:
                return _BUTTON_LABELS[pref]
        return _BUTTON_LABELS.get(states[0], states[0].replace("_", " ").title())
    # Fallback to JSON-LD offer availability.
    offer = _new_offer(ld)
    if offer:
        a = offer.get("availability") or ""
        if "InStock" in a:
            return "In Stock"
        if "OutOfStock" in a or "SoldOut" in a:
            return "Sold Out"
    return None


# --- Bullets (feature highlights) & description -------------------------------

# The product "features" highlight list: a JSON array of ProductFeature objects
# each with a "description" and a "title" (the title is sometimes null, e.g. on
# Apple listings). Match the whole object body so field order / null titles are
# handled, then pull description + title out of it.
_FEATURE_RE = re.compile(
    r'\{((?:"(?:description|title|__typename)":(?:"(?:[^"\\]|\\.)*"|null),?)+)"__typename":"ProductFeature"\}'
)
_FEAT_DESC_RE = re.compile(r'"description":"((?:[^"\\]|\\.)*)"')
_FEAT_TITLE_RE = re.compile(r'"title":"((?:[^"\\]|\\.)*)"')
_DESC_LONG_RE = re.compile(
    r'"description":\{"long":"((?:[^"\\]|\\.)*)"', re.DOTALL
)


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


def _bullets(html: str) -> Optional[list[str]]:
    out: list[str] = []
    for body in _FEATURE_RE.findall(html):
        dm = _FEAT_DESC_RE.search(body)
        tm = _FEAT_TITLE_RE.search(body)
        t = _json_unescape(tm.group(1)).strip() if tm else ""
        d = _json_unescape(dm.group(1)).strip() if dm else ""
        # Title is the highlight headline; description is the supporting line.
        # Combine into a single readable bullet (matches Amazon bullet feel).
        if t and d:
            out.append(f"{t}: {d}")
        elif d:
            out.append(d)
        elif t:
            out.append(t)
    # Dedup preserving order.
    seen = set()
    deduped = []
    for b in out:
        if b not in seen:
            seen.add(b)
            deduped.append(b)
    return deduped or None


def _description(html: str) -> Optional[str]:
    m = _DESC_LONG_RE.search(html)
    if m:
        t = _json_unescape(m.group(1)).strip()
        if t:
            return t
    return None


# --- Images -------------------------------------------------------------------

_PRODUCT_IMAGE_RE = re.compile(r'"piscesHref":"([^"]+)","__typename":"ProductImage"')
_PRIMARY_IMAGE_RE = re.compile(r'"primaryImage":\{[^}]*?"piscesHref":"([^"]+)"')
_VARIATION_IMG_RE = re.compile(
    r'"href":"([^"]+)"[^}]*?"piscesHref":"[^"]+","__typename":"VariationImage"\},'
    r'"rawName":"([^"]+)","value":"([^"]*)"'
)


def _clean_img(u: str) -> str:
    return _json_unescape(u).strip()


def _is_prescaled(u: str) -> bool:
    return "/prescaled/" in u


def _images(html: str) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    # primaryImage first so it leads the main list.
    pm = _PRIMARY_IMAGE_RE.search(html)
    if pm:
        u = _clean_img(pm.group(1))
        if u and not _is_prescaled(u):
            main.append(u)

    for raw in _PRODUCT_IMAGE_RE.findall(html):
        u = _clean_img(raw)
        if not u:
            continue
        if _is_prescaled(u):
            if u not in thumbs:
                thumbs.append(u)
        else:
            if u not in main:
                main.append(u)

    variants = _variant_images(html)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


def _variant_images(html: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for href, rawname, value in _VARIATION_IMG_RE.findall(html):
        u = _clean_img(href)
        v = _json_unescape(value).strip()
        if not u or not v:
            continue
        out.setdefault(v, [])
        if u not in out[v]:
            out[v].append(u)
    return out


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(html: str, tree: HTMLParser) -> Optional[list[str]]:
    # Primary: BreadcrumbList JSON-LD.
    for d in _iter_jsonld(html):
        for obj in _flatten_ld(d):
            if isinstance(obj, dict) and obj.get("@type") == "BreadcrumbList":
                crumbs = []
                for it in obj.get("itemListElement", []):
                    item = it.get("item") if isinstance(it, dict) else None
                    name = item.get("name") if isinstance(item, dict) else None
                    if name and name != "Best Buy":
                        crumbs.append(name.strip())
                if crumbs:
                    return crumbs
    # Fallback: the analytics blob embeds an (escaped) breadcrumbs array, present
    # even when BreadcrumbList JSON-LD is absent (laptops/TVs). The escaping depth
    # varies (\" vs \\\"), so match leniently and take the longest hit.
    best: Optional[list[str]] = None
    for m in _BREADCRUMBS_BLOB_RE.finditer(html):
        names = re.findall(r'\\*"((?:[^"\\]|\\.)*?)\\*"', m.group(1))
        names = [
            _json_unescape(n.replace("\\&", "&")).strip()
            for n in names
            if n and n.strip().lower() != "best buy"
        ]
        names = [n for n in names if n]
        if names and (best is None or len(names) > len(best)):
            best = names
    if best:
        return best
    # Last resort: rendered breadcrumb nav.
    crumbs = [
        _text(a)
        for a in tree.css("nav.c-breadcrumbs a, ol.breadcrumb-list a, .breadcrumb a")
    ]
    crumbs = [c for c in crumbs if c and c.lower() != "best buy"]
    return crumbs or None


_BREADCRUMBS_BLOB_RE = re.compile(r'breadcrumbs\\*":\[(.*?)\]')


_SPEC_RE = re.compile(
    r'"displayName":"((?:[^"\\]|\\.)*)","value":"((?:[^"\\]|\\.)*)","__typename":"ProductSpecification"'
)


def _specs(html: str) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for name, value in _SPEC_RE.findall(html):
        k = _json_unescape(name).strip()
        v = _json_unescape(value).strip()
        if k and v and k not in out:
            out[k] = v
    return out or None


_VARIATION_RE = re.compile(r'"rawName":"((?:[^"\\]|\\.)*)","value":"((?:[^"\\]|\\.)*)"')


def _variations(html: str) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    for rawname, value in _VARIATION_RE.findall(html):
        rn = _json_unescape(rawname)
        v = _json_unescape(value).strip()
        if not v:
            continue
        # rawName looks like "Major_Kitchen_Appliances:Color" or "...:Capacity".
        label = rn.split(":")[-1].strip().lower() or rn.strip().lower()
        if not label:
            continue
        out.setdefault(label, [])
        if v not in out[label]:
            out[label].append(v)
    # Drop single-option "variations" (not real selectable variants).
    out = {k: vs for k, vs in out.items() if len(vs) > 1}
    return out or None


def _seller(ld: dict) -> Optional[str]:
    offer = _new_offer(ld)
    if not offer:
        return None
    seller = offer.get("seller")
    name = seller.get("name") if isinstance(seller, dict) else seller
    if name and name.strip() and name.strip().lower() != "best buy":
        return name.strip()
    return None


# --- Tech-retail extras -------------------------------------------------------

_INCLUDED_RE = re.compile(r'"includedItems":\[((?:"(?:[^"\\]|\\.)*"\s*,?\s*)+)\]')
_STREET_DATE_RE = re.compile(r'"customStreetDate":"([^"]+)"')
_UPC_RE = re.compile(r'"upc":"(\d{8,14})"')


def _whats_included(html: str) -> Optional[list[str]]:
    m = _INCLUDED_RE.search(html)
    if not m:
        return None
    try:
        items = json.loads("[" + m.group(1) + "]")
    except Exception:
        return None
    items = [str(i).strip() for i in items if str(i).strip()]
    return items or None


def _release_date(html: str) -> Optional[str]:
    m = _STREET_DATE_RE.search(html)
    if m:
        return m.group(1)
    return None


def _upc(html: str) -> Optional[str]:
    m = _UPC_RE.search(html)
    if m:
        return m.group(1)
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
