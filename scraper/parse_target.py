"""Extract structured fields + full visible text from a Target product page.

Mirrors the field schema of ``crawler/parse.py`` (the Amazon parser) and the
public interface of its siblings ``parse_walmart.py`` / ``parse_asos.py`` so the
generic fetcher / frontier / store modules drive it unchanged. The dedup key on
Target is the numeric ``tcin`` (Target.com Item Number), analogous to Amazon's
``asin`` / Walmart's ``item_id``.

Parsing strategy (most robust first):

  1. PRIMARY — the Next.js ``<script id="__NEXT_DATA__" type="application/json">``
     blob. Target server-side renders the React-Query cache at
     ``props.dehydratedState.queries[]``; the product lives in the query whose
     ``queryKey[0]`` is ``@web/domain-product/get-pdp-v1`` at
     ``state.data.data.product``. That node carries (under ``item``):
     ``product_description.title`` / ``.downstream_description`` /
     ``.soft_bullets.bullets`` (Highlights) / ``.bullet_descriptions`` (the
     ``<B>Key:</B> Value`` spec lines), ``primary_brand.name``,
     ``enrichment.image_info`` (primary + alternate + variant images),
     plus top-level ``category.breadcrumbs``, ``variation_hierarchy``,
     ``ratings_and_reviews.statistics``, and per-SKU ``children[]``.

  2. PRICE — NOT in the SSR HTML. Target renders a ``price-module-placeholder``
     div and fetches price client-side from the redsky ``pdp_client_v1``
     aggregation API. ``parse_product`` therefore returns no ``price`` from a
     raw page unless price JSON happens to be embedded; callers fetch the price
     separately (see ``price_api_url``) and feed it to ``parse_price_api_json``
     (or pass it into ``parse_product`` via ``price_data=``) to populate
     ``price`` / ``price_range``. The price object uses ``current_retail`` (or
     ``current_retail_min``/``_max`` for variation ranges) and
     ``reg_retail``/``reg_retail_max`` + ``formatted_comparison_price`` as the
     struck-through "reg" / "was" price (the deal signal).

  3. SECONDARY — JSON-LD ``<script type="application/ld+json">`` ``Product`` node
     (kept as a defensive fallback; current Target pages do not emit one).

  4. TERTIARY — CSS on the rendered DOM (``h1[data-test='product-title']``,
     og:* meta) for the handful of fields that survive in markup.

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

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

log = logging.getLogger(__name__)


# --- Module interface (consumed by the generic crawler) ----------------------

DOMAINS = ["target.com"]
ID_FIELD = "tcin"

# Target fronts its pages with Akamai Bot Manager (same family as ASOS). Real
# product pages cleared cleanly with curl_cffi impersonate="safari17_0"
# (HTTP 200, ~340-380KB SSR HTML carrying __NEXT_DATA__). When blocked, Akamai
# serves either an "Access Denied" edge page or a JS proof-of-work challenge
# shell ("/_sec/cp_challenge/", "Pardon Our Interruption").
BOT_VENDOR = "Akamai"

# Strings that appear on Target's Akamai block / challenge page and effectively
# never on a real rendered product page. Kept tight to avoid false positives
# (a real page never contains these; it does contain __NEXT_DATA__).
CAPTCHA_MARKERS = [
    "Access Denied",
    "errors.edgesuite.net",
    "Reference&#32;&#35;",            # HTML-entity "Reference #" on the block page
    "/_sec/cp_challenge/",            # Akamai PoW challenge interstitial path
    "Pardon Our Interruption",        # PerimeterX-style interruption copy
    "Bot Manager",
    "ak_bmsc",                        # Akamai Bot Manager session cookie name
]

# Target product URLs:  https://www.target.com/p/<slug>/-/A-<tcin>[?...]
#   <tcin> is the numeric id after "/-/A-". Some links omit the slug: /p/-/A-<tcin>.
_ID_RE = re.compile(r"/-/A-(\d{4,})(?:[/?#]|$)")
_ID_BARE = re.compile(r"^\d{4,}$")

# redsky price aggregation key observed in the page bundle. Used only to build
# the optional price-API URL; not required for parse_product itself.
REDSKY_PRICE_KEY = "9f36aeafbe60771e321a7cc95a781407"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL
)


def extract_id(url: str) -> Optional[str]:
    """Return the numeric tcin from a Target product URL, or None."""
    if not url:
        return None
    m = _ID_RE.search(url)
    if m:
        return m.group(1)
    if _ID_BARE.match(url.strip()):
        return url.strip()
    return None


def canonical_product_url(tcin: str) -> str:
    return f"https://www.target.com/p/-/A-{tcin}"


def price_api_url(tcin: str, store_id: str = "1546", key: str = REDSKY_PRICE_KEY) -> str:
    """Build the redsky ``pdp_client_v1`` URL that returns the (XHR-only) price.

    Target does not embed price in the SSR HTML; a caller fetches this URL
    (curl_cffi impersonate="safari17_0" clears it) and passes the parsed JSON to
    ``parse_price_api_json`` or to ``parse_product(..., price_data=...)``.
    """
    return (
        "https://redsky.target.com/redsky_aggregations/v1/web/pdp_client_v1"
        f"?key={key}&tcin={tcin}&store_id={store_id}&pricing_store_id={store_id}"
        f"&has_pricing_store_id=true&visitor_id=0&channel=WEB&page=%2Fp%2FA-{tcin}"
    )


# --- Top-level entrypoint -----------------------------------------------------


def parse_product(
    html: str, source_url: str, price_data: Optional[dict] = None
) -> dict[str, Any]:
    """Return a dict of every field extractable from a Target product page.

    Works on the supplied page HTML (no network). ``price_data`` is the optional
    redsky ``pdp_client_v1`` JSON (or just its ``...product.price`` sub-dict) — if
    supplied, ``price`` / ``price_range`` are populated from it, since Target's
    SSR HTML carries no price. Missing fields are omitted, not stored as None.
    """
    tree = HTMLParser(html)
    product = _safe(_pdp_product, html) or {}
    item = product.get("item") if isinstance(product.get("item"), dict) else {}
    pdesc = item.get("product_description") if isinstance(item.get("product_description"), dict) else {}
    ld = _safe(_ld_product, html) or {}

    out: dict[str, Any] = {}

    tcin = (
        extract_id(source_url)
        or (str(product.get("tcin")) if product.get("tcin") else None)
        or (str(ld.get("sku")) if ld.get("sku") else None)
    )
    if tcin:
        out["tcin"] = tcin
        buy = _nz((item.get("enrichment") or {}).get("buy_url")) if isinstance(item.get("enrichment"), dict) else None
        out["url"] = buy or f"https://www.target.com/p/-/A-{tcin}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, pdesc, ld, tree))
    _put(out, "brand", _safe(_brand, item, ld))

    # Price: from supplied redsky price_data (SSR HTML has none), else any
    # embedded price node, else JSON-LD offers.
    price = _safe(_price, price_data, product, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, product, ld))
    _put(out, "availability", _safe(_availability, product, item, ld, price_data))
    _put(out, "bullets", _safe(_bullets, pdesc))
    _put(out, "description", _safe(_description, pdesc, ld))
    _put(out, "images", _safe(_images, item, product, ld))
    _put(out, "categories", _safe(_categories, product, tree))
    _put(out, "specs", _safe(_specs, pdesc))
    _put(out, "variations", _safe(_variations, product))
    _put(out, "seller", _safe(_seller, item, ld))
    _put(out, "page_text", _safe(_page_text, html))

    # --- Target-specific extras (info beyond the Amazon schema) --------------
    _put(out, "highlights", _safe(lambda: _soft_bullets(pdesc)))
    _put(out, "dimensions", _safe(_dimensions, item, pdesc))

    return out


def parse_price_api_json(payload: Any, source_url: str = "") -> dict[str, Any]:
    """Supplementary: build a minimal record from a redsky ``pdp_client_v1``
    payload (or its ``data.product`` / ``...price`` sub-object).

    Use when a caller has already fetched ``price_api_url(tcin)``. The canonical
    entry point remains ``parse_product``; this is for price-only refreshes.
    """
    out: dict[str, Any] = {}
    tcin = extract_id(source_url)
    prod = _price_product(payload)
    if not tcin and isinstance(prod, dict):
        tcin = str(prod.get("tcin")) if prod.get("tcin") else None
    if tcin:
        out["tcin"] = tcin
        out["url"] = f"https://www.target.com/p/-/A-{tcin}"
    if source_url:
        out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    price = _safe(_price, payload, {}, {})
    if price:
        pr = price.pop("price_range", None)
        _put(out, "price", price)
        if pr:
            out["price_range"] = pr
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
    s = _htmllib.unescape(str(v))
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


def _strip_tags(s: str) -> str:
    """Flatten an HTML fragment to plain text (Target descriptions carry <B> etc.)."""
    if not s:
        return ""
    if "<" in s:
        try:
            txt = lxml_html.fromstring(s).text_content()
        except Exception:
            txt = _htmllib.unescape(re.sub(r"<[^>]+>", " ", s))
    else:
        txt = _htmllib.unescape(s)
    txt = re.sub(r"\s+", " ", txt)
    return txt.strip()


_PRICE_RE = re.compile(r"([^\d\s]{0,3}?)\s*([\d,]+(?:\.\d+)?)")
_CCY_SYMBOL = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR"}


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


# --- __NEXT_DATA__ / redsky / JSON-LD locators -------------------------------


def _next_data(html: str) -> dict[str, Any]:
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return {}
    try:
        return json.loads(m.group(1)) or {}
    except Exception:
        return {}


def _pdp_product(html: str) -> dict[str, Any]:
    """Locate the get-pdp-v1 product node in the dehydrated React-Query cache.

    Matches by ``queryKey[0] == '@web/domain-product/get-pdp-v1'`` rather than a
    fixed index (query order is not guaranteed). Falls back to a structural
    scan for any ``...data.data.product`` carrying a ``tcin``.
    """
    nd = _next_data(html)
    queries = (
        nd.get("props", {}).get("dehydratedState", {}).get("queries")
        if isinstance(nd.get("props"), dict)
        else None
    )
    if isinstance(queries, list):
        for q in queries:
            if not isinstance(q, dict):
                continue
            qk = q.get("queryKey")
            key0 = qk[0] if isinstance(qk, list) and qk else qk
            if isinstance(key0, str) and "get-pdp-v1" in key0:
                prod = (
                    q.get("state", {})
                    .get("data", {})
                    .get("data", {})
                    .get("product")
                )
                if isinstance(prod, dict):
                    return prod
        # Fallback: any query whose data.data.product has a tcin.
        for q in queries:
            try:
                prod = q["state"]["data"]["data"]["product"]
            except Exception:
                continue
            if isinstance(prod, dict) and prod.get("tcin"):
                return prod
    return {}


def _price_product(payload: Any) -> dict[str, Any]:
    """Normalize a redsky price payload to the ``product`` dict.

    Accepts the full ``{data:{product:{...}}}`` envelope, a bare ``product``
    dict, or just the ``price`` sub-dict.
    """
    if not isinstance(payload, dict):
        return {}
    if "data" in payload and isinstance(payload["data"], dict):
        p = payload["data"].get("product")
        if isinstance(p, dict):
            return p
    if "item" in payload or "tcin" in payload or "price" in payload:
        return payload
    # Looks like a bare price sub-dict.
    if "current_retail" in payload or "current_retail_min" in payload or "formatted_current_price" in payload:
        return {"price": payload}
    return payload


def _ld_product(html: str) -> dict[str, Any]:
    for blob in _LD_JSON_RE.findall(html):
        try:
            obj = json.loads(blob)
        except Exception:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for c in candidates:
            if isinstance(c, dict) and "@graph" in c and isinstance(c["@graph"], list):
                candidates = candidates + [g for g in c["@graph"] if isinstance(g, dict)]
        for c in candidates:
            if not isinstance(c, dict):
                continue
            t = c.get("@type")
            t = t if isinstance(t, list) else [t]
            if "Product" in t:
                return c
    return {}


# --- Field extractors ---------------------------------------------------------


def _title(pdesc: dict, ld: dict, tree: HTMLParser) -> Optional[str]:
    return (
        _nz(pdesc.get("title"))
        or _nz(ld.get("name"))
        or _nz(_text(tree.css_first("h1[data-test='product-title'], h1#pdp-product-title-id")))
        or _title_from_meta(tree)
    )


def _title_from_meta(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("meta[property='og:title']")
    if el is not None:
        v = (el.attributes.get("content") or "").strip()
        v = re.sub(r"\s*:\s*Target\s*$", "", v)
        if v:
            return _nz(v)
    el = tree.css_first("title")
    if el is not None:
        v = re.sub(r"\s*:\s*Target\s*$", "", _text(el))
        return _nz(v)
    return None


def _brand(item: dict, ld: dict) -> Optional[str]:
    pb = item.get("primary_brand")
    if isinstance(pb, dict) and pb.get("name"):
        return _nz(pb["name"])
    b = ld.get("brand")
    if isinstance(b, dict):
        b = b.get("name")
    return _nz(b)


# --- Price (from redsky pdp_client_v1 price node) ----------------------------


def _price(price_data: Any, product: dict, ld: dict) -> Optional[dict[str, Any]]:
    # 1) redsky price node (the authoritative live source — SSR HTML has none).
    prod = _price_product(price_data) if price_data else {}
    pnode = prod.get("price") if isinstance(prod, dict) else None
    # Also accept a price embedded in the SSR product (future-proofing) and
    # the AMP/JSON-LD offer as last resorts.
    if not isinstance(pnode, dict):
        emb = product.get("price") if isinstance(product.get("price"), dict) else None
        if isinstance(emb, dict):
            pnode = emb

    if isinstance(pnode, dict):
        out = _money_from_price_node(pnode)
        if out:
            return out

    # 2) JSON-LD offers fallback.
    offers = ld.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offers, dict) and offers.get("price") is not None:
        try:
            amount = float(str(offers["price"]).replace(",", ""))
        except (TypeError, ValueError):
            return None
        cur: dict[str, Any] = {"amount": amount, "currency": "USD"}
        c = offers.get("priceCurrency")
        if c:
            cur["currency"] = c
        return cur
    return None


def _money_from_price_node(p: dict) -> Optional[dict[str, Any]]:
    """Target redsky price -> ``{amount, currency, list_price[, price_range]}``.

    Shape (live): ``{current_retail | current_retail_min/_max,
    reg_retail | reg_retail_max, formatted_current_price,
    formatted_current_price_type ("reg"/"sale"/"clearance"),
    formatted_comparison_price (+_type)}``. ``amount`` is the lowest current
    price; a reg/comparison price above it becomes ``list_price`` (deal signal).
    """
    def num(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    cur = num(p.get("current_retail"))
    cur_min = num(p.get("current_retail_min"))
    cur_max = num(p.get("current_retail_max"))
    if cur is None:
        cur = cur_min
    if cur is None:
        # last resort: parse the formatted string (e.g. "$17.00" / "$24 - $30")
        fc = _parse_money(str(p.get("formatted_current_price") or ""))
        if fc:
            cur = fc["amount"]
    if cur is None:
        return None

    out: dict[str, Any] = {"amount": cur, "currency": "USD"}

    # Variation price range.
    lo = cur_min if cur_min is not None else cur
    hi = cur_max
    if hi is None:
        # When only a *_min is given alongside a higher reg_max for a variation,
        # the formatted string carries the high end ("$129.99 - $179.99").
        fc = str(p.get("formatted_current_price") or "")
        m = re.findall(r"[\d,]+(?:\.\d+)?", fc)
        if len(m) >= 2:
            try:
                hi = float(m[-1].replace(",", ""))
            except ValueError:
                hi = None
    if hi is not None and hi > lo:
        out["price_range"] = {"min": lo, "max": hi}

    # list_price (deal signal): struck-through reg / comparison price > current.
    reg = num(p.get("reg_retail"))
    reg_max = num(p.get("reg_retail_max"))
    comp = _parse_money(str(p.get("formatted_comparison_price") or ""))
    comp_amt = comp["amount"] if comp else None
    candidates = [c for c in (reg, reg_max, comp_amt) if c is not None and c > cur]
    if candidates:
        out["list_price"] = max(candidates)
    return out


def _rating(product: dict, ld: dict) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    rr = product.get("ratings_and_reviews")
    if isinstance(rr, dict):
        stat = rr.get("statistics") if isinstance(rr.get("statistics"), dict) else {}
        rating = stat.get("rating") if isinstance(stat.get("rating"), dict) else {}
        if rating.get("average") is not None:
            try:
                stars = float(rating["average"])
            except (TypeError, ValueError):
                stars = None
        if rating.get("count") is not None:
            try:
                count = int(rating["count"])
            except (TypeError, ValueError):
                count = None
        if count is None and stat.get("review_count") is not None:
            try:
                count = int(stat["review_count"])
            except (TypeError, ValueError):
                pass

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


def _availability(product: dict, item: dict, ld: dict, price_data: Any) -> Optional[str]:
    # Target has no clean availability_status in the SSR HTML or the redsky
    # pdp_client_v1 payload (real stock comes from a separate fulfillment API).
    # Derive a coarse purchasable signal from per-SKU eligibility_rules
    # (ship_to_guest / scheduled_delivery active) when the full redsky payload
    # is supplied, else fall back to an explicit status / JSON-LD offer.
    prod = _price_product(price_data) if price_data else {}
    for src in (prod, product):
        if not isinstance(src, dict):
            continue
        sitem = src.get("item") if isinstance(src.get("item"), dict) else {}
        fulfillment = sitem.get("fulfillment") if isinstance(sitem.get("fulfillment"), dict) else None
        if isinstance(fulfillment, dict):
            so = fulfillment.get("shipping_options")
            if isinstance(so, dict) and so.get("availability_status"):
                v = str(so["availability_status"])
                return {"IN_STOCK": "In stock", "OUT_OF_STOCK": "Out of stock", "UNAVAILABLE": "Unavailable"}.get(v, v.replace("_", " ").title())
        # eligibility_rules on the SKUs: ship_to_guest active => purchasable.
        nodes = src.get("children") if isinstance(src.get("children"), list) else [src]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            er = node.get("item", {}).get("eligibility_rules") if isinstance(node.get("item"), dict) else node.get("eligibility_rules")
            if isinstance(er, dict):
                for rule in ("ship_to_guest", "scheduled_delivery", "hold"):
                    r = er.get(rule)
                    if isinstance(r, dict) and r.get("is_active"):
                        return "In stock"
    offers = ld.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offers, dict) and offers.get("availability"):
        v = str(offers["availability"]).rsplit("/", 1)[-1]
        return {"InStock": "In stock", "OutOfStock": "Out of stock"}.get(v, v)
    return None


def _soft_bullets(pdesc: dict) -> Optional[list[str]]:
    """Target 'Highlights' -> list[str] (the marketing soft bullets)."""
    sb = pdesc.get("soft_bullets")
    out: list[str] = []
    if isinstance(sb, dict):
        for b in sb.get("bullets") or []:
            t = _strip_tags(str(b)) if b else ""
            if t and t not in out:
                out.append(t)
    return out or None


def _bullets(pdesc: dict) -> Optional[list[str]]:
    """Bullets = Target Highlights (soft_bullets), matching the Amazon schema.

    Falls back to soft_bullet_description (a single &bull;-joined string) when
    the structured bullets list is absent.
    """
    sb = _soft_bullets(pdesc)
    if sb:
        return sb
    sbd = pdesc.get("soft_bullet_description")
    if isinstance(sbd, str) and sbd.strip():
        parts = [
            _strip_tags(p)
            for p in re.split(r"&bull;|•|<br\s*/?>|\|", sbd)
        ]
        parts = [p for p in parts if p and len(p) > 1]
        if parts:
            return parts
    return None


def _description(pdesc: dict, ld: dict) -> Optional[str]:
    for raw in (
        pdesc.get("downstream_description"),
        pdesc.get("soft_bullet_description"),
        ld.get("description"),
    ):
        t = _strip_tags(raw) if raw else ""
        if t:
            return t
    return None


# --- Images -------------------------------------------------------------------


def _img_clean(url: Optional[str]) -> Optional[str]:
    """Normalize a Target scene7 URL (drop sizing/crop query so we keep full-res)."""
    if not url:
        return None
    u = str(url)
    if u.startswith("//"):
        u = "https:" + u
    # strip the ?cropN=...&sizeN=... params Target appends to swatch/thumb urls
    u = u.split("?", 1)[0]
    return u or None


def _images(item: dict, product: dict, ld: dict) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []
    variants: dict[str, list[str]] = {}

    enrich = item.get("enrichment") if isinstance(item.get("enrichment"), dict) else {}
    info = enrich.get("image_info") if isinstance(enrich.get("image_info"), dict) else {}

    primary = info.get("primary_image") if isinstance(info.get("primary_image"), dict) else None
    if primary:
        u = _img_clean(primary.get("url"))
        if u:
            main.append(u)
    for alt in info.get("alternate_images") or []:
        if isinstance(alt, dict):
            u = _img_clean(alt.get("url"))
            if u and u not in main:
                main.append(u)
    # content_labels carry extra gallery imagery on some products.
    for cl in info.get("content_labels") or []:
        if isinstance(cl, dict):
            u = _img_clean(cl.get("image_url"))
            if u and u not in main:
                main.append(u)

    # Per-variant images from variation_hierarchy (one swatch + primary per value).
    for v in product.get("variation_hierarchy") or []:
        if not isinstance(v, dict):
            continue
        name = _nz(v.get("value"))
        urls: list[str] = []
        for k in ("primary_image_url", "swatch_image_url"):
            u = _img_clean(v.get(k))
            if u and u not in urls:
                urls.append(u)
        sw = _img_clean(v.get("swatch_image_url"))
        if sw and sw not in thumbs:
            thumbs.append(sw)
        if name and urls:
            variants[name] = urls

    # JSON-LD image fallback.
    if not main:
        imgs = ld.get("image")
        if isinstance(imgs, str):
            imgs = [imgs]
        if isinstance(imgs, list):
            for u in imgs:
                cu = _img_clean(u) if isinstance(u, str) else None
                if cu and cu not in main:
                    main.append(cu)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


# --- Categories / specs / variations / seller --------------------------------


def _categories(product: dict, tree: HTMLParser) -> Optional[list[str]]:
    cat = product.get("category") if isinstance(product.get("category"), dict) else {}
    crumbs = []
    for b in cat.get("breadcrumbs") or []:
        if isinstance(b, dict):
            name = _nz(b.get("name"))
            cid = b.get("category_id")
            # skip the synthetic root crumb (name "target", category_id "root")
            if name and cid != "root" and name.lower() != "target":
                crumbs.append(name)
    if crumbs:
        return crumbs

    dom = [
        _text(a)
        for a in tree.css("[data-test='@web/Breadcrumbs'] a, nav[aria-label='Breadcrumb'] a, ol.breadcrumb a")
    ]
    dom = [c for c in dom if c and c.lower() != "target"]
    return dom or None


# bullet_descriptions are "<B>Key:</B> Value" spec lines.
_SPEC_RE = re.compile(r"^\s*<B>\s*(.+?)\s*:?\s*</B>\s*:?\s*(.*)$", re.IGNORECASE | re.DOTALL)


def _specs(pdesc: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    for raw in pdesc.get("bullet_descriptions") or []:
        if not isinstance(raw, str):
            continue
        m = _SPEC_RE.match(raw)
        if m:
            k = _nz(m.group(1))
            v = _strip_tags(m.group(2))
            if k and v and k not in out:
                out[k] = v
        else:
            # fall back: split a plain "Key: Value" line
            flat = _strip_tags(raw)
            if ":" in flat:
                k, _, v = flat.partition(":")
                k, v = _nz(k), _nz(v)
                if k and v and k not in out:
                    out[k] = v
    return out or None


def _variations(product: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    for v in product.get("variation_hierarchy") or []:
        if not isinstance(v, dict):
            continue
        label = _nz(v.get("name"))
        value = _nz(v.get("value"))
        if not label or not value:
            continue
        key = label.lower()
        out.setdefault(key, [])
        if value not in out[key]:
            out[key].append(value)
    return out or None


def _seller(item: dict, ld: dict) -> Optional[str]:
    """Marketplace / Target Plus partner seller, omitted when sold by Target."""
    for key in ("seller", "marketplace_seller", "third_party_seller"):
        node = item.get(key)
        if isinstance(node, dict):
            name = _nz(node.get("seller_name") or node.get("name") or node.get("display_name"))
            if name and name.lower() not in ("target", "target.com"):
                return name
        elif isinstance(node, str):
            name = _nz(node)
            if name and name.lower() not in ("target", "target.com"):
                return name
    offers = ld.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    if isinstance(offers, dict):
        s = offers.get("seller")
        if isinstance(s, dict) and s.get("name"):
            name = _nz(s["name"])
            if name and name.lower() not in ("target", "target.com"):
                return name
    return None


def _dimensions(item: dict, pdesc: dict) -> Optional[str]:
    """Overall product dimensions, pulled from the spec bullets when present.

    Matches "Dimensions (Overall)", "Dimensions", or per-piece "Piece N
    Dimensions" labels. Prefers an overall dimension over a per-piece one.
    """
    bullets = [b for b in (pdesc.get("bullet_descriptions") or []) if isinstance(b, str)]
    piece_dim: Optional[str] = None
    for raw in bullets:
        m = _SPEC_RE.match(raw)
        if not m:
            continue
        key = (_nz(m.group(1)) or "")
        if re.search(r"Dimensions?\s*\(Overall\)|^Dimensions?$", key, re.IGNORECASE):
            v = _strip_tags(m.group(2))
            if v:
                return v
        if "Dimensions" in key and piece_dim is None:
            v = _strip_tags(m.group(2))
            if v:
                piece_dim = v
    if piece_dim:
        return piece_dim
    # package_dimensions from the first child SKU.
    children = item.get("children") if isinstance(item.get("children"), list) else None
    if not children:
        return None
    pd = children[0].get("item", {}).get("package_dimensions") if isinstance(children[0], dict) else None
    if isinstance(pd, dict) and pd.get("height"):
        unit = (pd.get("dimension_unit_of_measure") or "").title()
        return f"{pd.get('height')} x {pd.get('width')} x {pd.get('depth')} {unit}".strip()
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
