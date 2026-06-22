"""Extract structured fields + full visible text from a Sephora product page.

Mirrors the public interface of ``crawler/parse.py`` (the Amazon parser) and
``crawler/parse_asos.py`` so the generic fetcher / frontier / store modules can
drive it unchanged. Self-contained: the small helpers it needs (``_safe``,
``_put``, ``_text``, ``_parse_money``, ``_page_text``) are copied here rather
than imported, per the build spec.

Parsing strategy, most-robust first:

  1. Sephora's embedded page JSON: ``<script id="linkStore" type="text/json">``.
     This is the richest source and the *primary* one used here — it carries
     ``page.product`` with ``productDetails`` (displayName, brand, long/short
     description, suggestedUsage, rating, reviews) and ``currentSku`` /
     ``regularChildSkus`` (listPrice, salePrice, size, highlights, ingredientDesc,
     skuImages, alternateImages, variation info). On live pages this is server-
     rendered and present without JS.
  2. JSON-LD ``<script type="application/ld+json">``. Sephora uses a
     ``@type":"ProductGroup"`` object (name, brand, image, description,
     productGroupID, category, aggregateRating, hasVariant[] of @type Product
     each carrying offers.price/priceCurrency/availability/seller) plus a
     ``BreadcrumbList``. Used as a fallback / cross-check for most fields.
  3. CSS on the rendered DOM as a final fallback.

Sale signal: Sephora stores the original price in ``listPrice`` ("$26.00") and
the discounted price in ``salePrice`` (null when not on sale). When ``salePrice``
exists and is below ``listPrice``, ``price.amount`` is the sale price and
``price.list_price`` is the original (mirrors the Amazon/ASOS deal convention).

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

DOMAINS = ["sephora.com"]
ID_FIELD = "product_id"

# Sephora fronts its pages with Akamai Bot Manager. Plain curl / httpx and even
# curl_cffi's chrome120 profile get a 403 "Access Denied" served by AkamaiGHost
# (confirmed against live URLs during build — see report). curl_cffi's
# ``impersonate="safari17_0"`` TLS fingerprint clears it and returns the full
# ~700KB product HTML. When the fingerprint check fails Akamai serves either the
# 403 Access Denied page or a tiny (~2KB) interstitial shell.
BOT_VENDOR = "Akamai"

# Strings that appear on Sephora's Akamai block / challenge responses and
# effectively never on a real product page. Kept tight to avoid false positives.
CAPTCHA_MARKERS = [
    # Akamai 403 "Access Denied" block page.
    "AkamaiGHost",
    "Access Denied",
    "Reference&#32;&#35;",          # HTML-entity-encoded "Reference #" on the block page
    "errors.edgesuite.net",
    "/_sec/cp_challenge/",          # Akamai PoW challenge interstitial path
    "Pardon Our Interruption",
    # Akamai Bot Manager JS proof-of-work interstitial (the ~2KB shell served
    # intermittently in place of the product page — confirmed live, see report).
    # These are the reliable, page-specific markers; the cookie names above can
    # legitimately appear in Set-Cookie on real pages, so detection relies on
    # these body markers.
    "sec-if-cpt-container",
    "scf-akamai-protected-by",
    "Powered and protected by",
    "behavioral-content",
]

# Sephora product URLs look like:
#   https://www.sephora.com/product/<slug>-P<digits>[?...]
# product_id is the trailing P-number.
_PID_RE = re.compile(r"-(P\d+)\b")
_PID_LOOSE_RE = re.compile(r"\b(P\d{5,})\b")


def extract_id(url: str) -> Optional[str]:
    """Return the Sephora P-number product id from a URL, or None."""
    if not url:
        return None
    m = _PID_RE.search(url)
    if m:
        return m.group(1)
    m = _PID_LOOSE_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(product_id: str) -> str:
    return f"https://www.sephora.com/product/{product_id}"


# --- Public entry point -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from a Sephora product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    store = _safe(_link_store, html) or {}
    product = store.get("product") if isinstance(store, dict) else {}
    product = product if isinstance(product, dict) else {}
    details = product.get("productDetails") if isinstance(product.get("productDetails"), dict) else {}
    cur = product.get("currentSku") if isinstance(product.get("currentSku"), dict) else {}
    children = _child_skus(product)
    ld = _safe(_jsonld_product_group, html) or {}

    out: dict[str, Any] = {}

    pid = (
        extract_id(source_url)
        or _safe(_id_from_sources, product, ld)
    )
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.sephora.com/product/{pid}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, details, ld))
    _put(out, "brand", _safe(_brand, tree, details, product, ld))

    price = _safe(_price, cur, children, ld)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, details, ld))
    _put(out, "availability", _safe(_availability, cur, ld))
    _put(out, "bullets", _safe(_bullets, cur))
    _put(out, "description", _safe(_description, details, ld))
    _put(out, "images", _safe(_images, cur, children, ld))
    _put(out, "categories", _safe(_breadcrumb, product, ld, tree))
    _put(out, "specs", _safe(_specs, cur, product))
    _put(out, "variations", _safe(_variations, cur, children))
    _put(out, "seller", _safe(_seller, ld))

    # Beauty-specific extras Amazon/ASOS lack.
    _put(out, "highlights", _safe(_highlights, cur))
    _put(out, "ingredients", _safe(_ingredients, cur))
    _put(out, "how_to_use", _safe(_how_to_use, details, cur))
    _put(out, "size", _safe(_size, cur))
    _put(out, "details", _safe(_details_extra, product, cur))

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
    """Pull just the numeric amount from a price string like ``$26.00``."""
    m = _parse_money(s) if isinstance(s, str) else None
    return m.get("amount") if m else None


def _page_text(html: str) -> Optional[str]:
    """Full visible text with scripts/styles/nav stripped. Site-agnostic."""
    doc = lxml_html.fromstring(html)
    for el in doc.xpath("//script | //style | //noscript | //nav | //header | //footer"):
        el.getparent().remove(el)
    text = doc.text_content()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip() or None


def _clean_html_text(s: Any) -> Optional[str]:
    """Strip tags from a fragment of HTML, decode entities, collapse whitespace."""
    if not isinstance(s, str) or not s.strip():
        return None
    if "<" in s or "&" in s:
        try:
            s = lxml_html.fromstring(f"<div>{s}</div>").text_content()
        except Exception:
            s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip() or None


# --- JSON source extraction ---------------------------------------------------


def _link_store(html: str) -> Optional[dict[str, Any]]:
    """Return ``page`` object from the ``<script id="linkStore">`` JSON blob.

    Shape (live Sephora): ``{"page": {"product": {...}}, "ssrProps": {...}}``.
    Returns the ``page`` dict so callers reach ``page.product`` directly.
    """
    tree = HTMLParser(html)
    node = tree.css_first('script#linkStore')
    if node is None:
        # Fallback: some templates use a different id but same payload shape.
        for n in tree.css('script[type="text/json"]'):
            raw = (n.text() or "").strip()
            if '"product"' in raw and '"productDetails"' in raw:
                node = n
                break
    if node is None:
        return None
    raw = (node.text() or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    page = data.get("page") if isinstance(data, dict) else None
    return page if isinstance(page, dict) else None


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


def _jsonld_product_group(html: str) -> Optional[dict[str, Any]]:
    """Return the Sephora ProductGroup JSON-LD object (falls back to Product)."""
    fallback = None
    for obj in _iter_jsonld(html):
        t = obj.get("@type")
        types = t if isinstance(t, list) else [t]
        if "ProductGroup" in types:
            return obj
        if "Product" in types and fallback is None:
            fallback = obj
    return fallback


def _ld_first_variant(ld: dict) -> dict:
    """Return the first ``hasVariant`` Product (carries offers) from a ProductGroup."""
    hv = ld.get("hasVariant")
    if isinstance(hv, list):
        for v in hv:
            if isinstance(v, dict):
                return v
    return {}


def _ld_offers(ld: dict):
    """Yield offer dicts from a ProductGroup's variants or a plain Product."""
    seen = False
    hv = ld.get("hasVariant")
    if isinstance(hv, list):
        for v in hv:
            if not isinstance(v, dict):
                continue
            o = v.get("offers")
            for off in (o if isinstance(o, list) else [o]):
                if isinstance(off, dict):
                    seen = True
                    yield off
    if not seen:
        o = ld.get("offers")
        for off in (o if isinstance(o, list) else [o]):
            if isinstance(off, dict):
                yield off


def _child_skus(product: dict) -> list[dict]:
    """Merge all sibling-variant sku lists from the linkStore product blob.

    Sephora stores in-stock variants in ``regularChildSkus`` but moves them to
    ``onSaleChildSkus`` when the product is on sale (confirmed live: the 50-shade
    Fenty foundation on sale had an empty ``regularChildSkus`` and 51 entries in
    ``onSaleChildSkus``). ``ancillarySkus`` are samples/add-ons and are skipped.
    Deduplicated by skuId, order preserved.
    """
    out: list[dict] = []
    seen: set = set()
    for key in ("regularChildSkus", "onSaleChildSkus"):
        lst = product.get(key)
        if not isinstance(lst, list):
            continue
        for c in lst:
            if not isinstance(c, dict):
                continue
            sid = c.get("skuId")
            if sid is not None and sid in seen:
                continue
            if sid is not None:
                seen.add(sid)
            out.append(c)
    return out


def _id_from_sources(product: dict, ld: dict) -> Optional[str]:
    for v in (product.get("productId"), ld.get("productGroupID")):
        if isinstance(v, str) and _PID_LOOSE_RE.fullmatch(v):
            return v
        if v and re.fullmatch(r"P\d+", str(v)):
            return str(v)
    return None


# --- Field extractors ---------------------------------------------------------


def _title(tree: HTMLParser, details: dict, ld: dict) -> Optional[str]:
    v = details.get("displayName")
    if isinstance(v, str) and v.strip():
        return v.strip()
    if isinstance(ld.get("name"), str) and ld["name"].strip():
        return ld["name"].strip()
    for sel in ('h1[data-comp*="ProductName"]', "h1.css-1g2jq23", "h1"):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


def _brand(tree: HTMLParser, details: dict, product: dict, ld: dict) -> Optional[str]:
    b = details.get("brand")
    if isinstance(b, dict):
        for k in ("displayName", "name"):
            if isinstance(b.get(k), str) and b[k].strip():
                return b[k].strip()
    elif isinstance(b, str) and b.strip():
        return b.strip()
    cur = product.get("currentSku") if isinstance(product.get("currentSku"), dict) else {}
    if isinstance(cur.get("brandName"), str) and cur["brandName"].strip():
        return cur["brandName"].strip()
    lb = ld.get("brand")
    if isinstance(lb, dict) and isinstance(lb.get("name"), str):
        return lb["name"].strip() or None
    if isinstance(lb, str) and lb.strip():
        return lb.strip()
    el = tree.css_first('[data-comp*="BrandName"] a, [data-comp*="BrandName"]')
    return _text(el) or None


def _price(cur: dict, children: list, ld: dict) -> Optional[dict[str, Any]]:
    """Build the price dict.

    Primary source is the linkStore sku ``listPrice`` / ``salePrice`` strings.
    salePrice (when present and below listPrice) is the deal: price.amount is the
    sale price, list_price is the struck-through original. For multi-size /
    multi-shade products the full child-sku range is reported as price_range,
    with price.amount = the current sku's price (or the minimum).
    """
    cur = cur if isinstance(cur, dict) else {}
    list_amt = _money_amount(cur.get("listPrice"))
    sale_amt = _money_amount(cur.get("salePrice"))

    amount: Optional[float] = None
    list_price: Optional[float] = None
    if sale_amt is not None and (list_amt is None or sale_amt < list_amt):
        amount = sale_amt
        if list_amt is not None and list_amt > sale_amt:
            list_price = list_amt
    elif list_amt is not None:
        amount = list_amt

    currency = ""
    # Per-child price range (different sizes/shades).
    child_amounts: list[float] = []
    for c in (children or []):
        if not isinstance(c, dict):
            continue
        a = _money_amount(c.get("salePrice")) or _money_amount(c.get("listPrice"))
        if a is not None:
            child_amounts.append(a)

    if amount is None:
        # Fall back to JSON-LD offers.
        amts: list[float] = []
        for off in _ld_offers(ld):
            currency = currency or off.get("priceCurrency") or ""
            for k in ("price", "lowPrice", "highPrice"):
                v = off.get(k)
                if v not in (None, ""):
                    try:
                        amts.append(float(str(v).replace(",", "")))
                    except ValueError:
                        pass
        if amts:
            amount = min(amts)
            child_amounts = amts + child_amounts

    if amount is None:
        return None

    if not currency:
        for off in _ld_offers(ld):
            if off.get("priceCurrency"):
                currency = off["priceCurrency"]
                break
    out: dict[str, Any] = {"amount": amount}
    out["currency"] = currency or "USD"
    if list_price is not None:
        out["list_price"] = list_price

    if child_amounts:
        lo, hi = min(child_amounts + [amount]), max(child_amounts + [amount])
        if hi > lo:
            out["price_range"] = {"min": lo, "max": hi}
    return out


def _rating(details: dict, ld: dict) -> Optional[dict[str, Any]]:
    out: dict[str, Any] = {}
    stars = details.get("rating")
    cnt = details.get("reviews")
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
    if not out:
        ar = ld.get("aggregateRating")
        if isinstance(ar, dict):
            rv, rc = ar.get("ratingValue"), ar.get("reviewCount") or ar.get("ratingCount")
            if rv not in (None, ""):
                try:
                    out["stars"] = round(float(rv), 2)
                except (TypeError, ValueError):
                    pass
            if rc not in (None, ""):
                try:
                    out["count"] = int(float(rc))
                except (TypeError, ValueError):
                    pass
    return out or None


def _availability(cur: dict, ld: dict) -> Optional[str]:
    cur = cur if isinstance(cur, dict) else {}
    oos = cur.get("isOutOfStock")
    if oos is True:
        return "OutOfStock"
    if oos is False:
        return "InStock"
    for off in _ld_offers(ld):
        av = off.get("availability")
        if av:
            return str(av).rsplit("/", 1)[-1]  # schema.org/InStock -> InStock
    return None


def _bullets(cur: dict) -> Optional[list[str]]:
    """Sephora "highlights" map to bullets — short feature/benefit tags."""
    cur = cur if isinstance(cur, dict) else {}
    out: list[str] = []
    for h in (cur.get("highlights") or []):
        if isinstance(h, dict):
            name = h.get("name") or h.get("altText")
            if isinstance(name, str) and name.strip() and name.strip() not in out:
                out.append(name.strip())
    return out or None


def _description(details: dict, ld: dict) -> Optional[str]:
    for k in ("longDescription", "shortDescription"):
        v = details.get(k)
        cleaned = _clean_html_text(v)
        if cleaned:
            return cleaned
    return _clean_html_text(ld.get("description"))


# --- Images -------------------------------------------------------------------

_IMG_KEYS = ("imageUrl", "image1500", "image1000", "image750", "image500", "image450", "image250", "image135", "src")


def _img_url(img: Any) -> Optional[str]:
    if isinstance(img, str):
        u = img
    elif isinstance(img, dict):
        u = None
        for k in _IMG_KEYS:
            if isinstance(img.get(k), str) and img[k].strip():
                u = img[k]
                break
    else:
        return None
    if not u:
        return None
    if u.startswith("/"):
        u = "https://www.sephora.com" + u
    elif u.startswith("//"):
        u = "https:" + u
    # Drop CDN sizing/query suffix to get the canonical asset.
    u = u.split("?", 1)[0]
    return u


def _sku_images(cur: dict) -> list[str]:
    out: list[str] = []
    si = cur.get("skuImages")
    main = _img_url(si)
    if main:
        out.append(main)
    for alt in (cur.get("alternateImages") or []):
        u = _img_url(alt)
        if u and u not in out:
            out.append(u)
    return out


def _images(cur: dict, children: list, ld: dict) -> Optional[dict[str, list]]:
    cur = cur if isinstance(cur, dict) else {}
    main = _sku_images(cur)

    # JSON-LD image fallback.
    if not main:
        im = ld.get("image")
        cand = [im] if isinstance(im, str) else (im if isinstance(im, list) else [])
        main = [u for u in (_img_url(c) for c in cand) if u]

    # Per-variant image sets: one entry per child sku keyed by its variation value.
    variants: dict[str, list[str]] = {}
    for c in (children or []):
        if not isinstance(c, dict):
            continue
        key = c.get("variationValue") or c.get("swatchText") or c.get("skuId")
        if not key:
            continue
        urls = _sku_images(c)
        if urls:
            variants[str(key)] = urls

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if variants and len(variants) > 1:
        result["variants"] = variants
    return result or None


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(product: dict, ld: dict, tree: HTMLParser) -> Optional[list[str]]:
    # 1) Walk the nested parentCategory chain in linkStore.
    crumbs: list[str] = []
    cat = product.get("parentCategory") if isinstance(product, dict) else None
    chain: list[str] = []
    seen = set()
    while isinstance(cat, dict) and len(chain) < 12:
        name = cat.get("displayName")
        cid = cat.get("categoryId")
        if isinstance(name, str) and name.strip() and cid not in seen:
            chain.append(name.strip())
            seen.add(cid)
        cat = cat.get("parentCategory")
    if chain:
        crumbs = list(reversed(chain))  # top-level first
    # 2) JSON-LD BreadcrumbList fallback (from page-level JSON-LD).
    if not crumbs:
        for obj in _iter_jsonld_from_product(product, ld):
            if obj.get("@type") == "BreadcrumbList":
                for it in (obj.get("itemListElement") or []):
                    nm = (it.get("name") or (it.get("item") or {}).get("name")) if isinstance(it, dict) else None
                    if nm:
                        crumbs.append(str(nm).strip())
                if crumbs:
                    break
    # 3) DOM breadcrumb fallback.
    if not crumbs:
        crumbs = [_text(a) for a in tree.css('[data-comp*="Breadcrumbs"] a, nav[aria-label*="readcrumb"] a')]
        crumbs = [c for c in crumbs if c]
    return crumbs or None


def _iter_jsonld_from_product(product: dict, ld: dict):
    """Yield JSON-LD-shaped objects available via the product blob's embedded LD.

    Sephora stores its breadcrumb JSON-LD as a string in
    ``product.breadcrumbsSeoJsonLd`` / ``product.navigationSeoJsonLd``.
    """
    for key in ("breadcrumbsSeoJsonLd", "navigationSeoJsonLd"):
        raw = product.get(key) if isinstance(product, dict) else None
        if isinstance(raw, str) and raw.strip():
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            items = obj if isinstance(obj, list) else [obj]
            for it in items:
                if isinstance(it, dict):
                    yield it


def _specs(cur: dict, product: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    cur = cur if isinstance(cur, dict) else {}
    if cur.get("skuId"):
        out["SKU"] = str(cur["skuId"])
    if isinstance(cur.get("size"), str) and cur["size"].strip():
        out["Size"] = cur["size"].strip()
    if isinstance(cur.get("variationTypeDisplayName"), str) and cur.get("variationValue") not in (None, ""):
        label = cur["variationTypeDisplayName"].strip()
        if label and label.lower() != "size":
            out[label] = str(cur["variationValue"]).strip()
    # skuRefinements: structured attribute tags (finish, coverage, ingredient prefs).
    refs = cur.get("skuRefinements")
    if isinstance(refs, list):
        for r in refs:
            if not isinstance(r, dict):
                continue
            key = r.get("key")
            vals = r.get("values")
            if isinstance(key, str) and isinstance(vals, list) and vals:
                label = _humanize(key)
                joined = ", ".join(str(v) for v in vals if v)
                if joined and label not in out:
                    out[label] = joined
    return out or None


def _humanize(key: str) -> str:
    s = re.sub(r"(?<!^)(?=[A-Z])", " ", key)  # camelCase -> spaced
    return s[:1].upper() + s[1:]


def _variations(cur: dict, children: list) -> Optional[dict[str, list[str]]]:
    cur = cur if isinstance(cur, dict) else {}
    out: dict[str, list[str]] = {}
    if not children:
        return None
    # Group child sku variation values by their variation type label.
    label = None
    if isinstance(cur.get("variationTypeDisplayName"), str):
        label = cur["variationTypeDisplayName"].strip()
    vals: list[str] = []
    for c in children:
        if not isinstance(c, dict):
            continue
        v = c.get("swatchText") or c.get("variationValue")
        if v not in (None, "") and str(v).strip() not in vals:
            vals.append(str(v).strip())
    if vals:
        key = (label or "variant").lower()
        # Normalize "size + concentration + formulation" -> keep as-is lowercased.
        out[key] = vals
    return out or None


def _seller(ld: dict) -> Optional[str]:
    for off in _ld_offers(ld):
        s = off.get("seller")
        if isinstance(s, dict) and s.get("name"):
            return str(s["name"]).strip()
    return "Sephora"


# --- Beauty-specific extras ---------------------------------------------------


def _highlights(cur: dict) -> Optional[list[str]]:
    """Structured highlight tags (same source as bullets, kept as an explicit
    beauty field). Mirrors bullets; omitted when absent."""
    return _bullets(cur)


def _ingredients(cur: dict) -> Optional[str]:
    cur = cur if isinstance(cur, dict) else {}
    return _clean_html_text(cur.get("ingredientDesc"))


def _how_to_use(details: dict, cur: dict) -> Optional[str]:
    v = details.get("suggestedUsage") if isinstance(details, dict) else None
    cleaned = _clean_html_text(v)
    if cleaned:
        return cleaned
    return _clean_html_text(cur.get("suggestedUsage")) if isinstance(cur, dict) else None


def _size(cur: dict) -> Optional[str]:
    cur = cur if isinstance(cur, dict) else {}
    v = cur.get("size")
    return v.strip() if isinstance(v, str) and v.strip() else None


def _details_extra(product: dict, cur: dict) -> Optional[str]:
    """Sephora's "quick look" description / extra detail blurb, when present."""
    v = product.get("quickLookDescription") if isinstance(product, dict) else None
    return _clean_html_text(v)
