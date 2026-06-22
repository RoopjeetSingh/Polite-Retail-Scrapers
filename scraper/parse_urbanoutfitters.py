"""Extract structured fields + full visible text from an Urban Outfitters page.

Mirrors the public interface of ``crawler/parse.py`` (the Amazon parser) and its
fashion sibling ``crawler/parse_asos.py`` so the generic fetcher / frontier /
store modules can drive it unchanged. Self-contained: the small helpers it needs
(``_safe``, ``_put``, ``_text``, ``_parse_money``, ``_page_text``) are copied here
rather than imported, per the build spec.

Parsing strategy, most-robust first:

  1. JSON-LD ``<script type="application/ld+json">`` with ``"@type":"Product"`` —
     UO pages carry three LD blocks (Corporation, BreadcrumbList, Product). The
     Product block gives name, image[], description (markdown), sku/mpn, category,
     color, offers (price / priceCurrency / availability / seller) and
     aggregateRating. The BreadcrumbList block gives the category path.
  2. UO runs on the URBN Vue/Pinia platform. A ``<script id="urbnInitialPiniaState">``
     tag holds the full SSR state as a JSON string-literal that itself contains
     escaped JSON (``json.loads`` twice). Under ``catalog.products.<slug>`` it
     carries the richest data: ``product`` (styleNumber, longDescription, facets,
     sizeAndFit, brand, parentCategory), ``skuInfo`` (salePrice/listPrice low+high,
     markdownState, primarySlice=Color, secondarySlice=Size, per-color images),
     and ``reviews`` (count, averageRating). This is the authoritative source for
     price/markdown (the deal signal), variations and per-colour image sets.
  3. CSS on the rendered DOM as a final fallback.

Every extractor is wrapped in ``_safe`` so one selector miss can't abort the
whole record. Missing fields are omitted, not stored as ``None``.
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

DOMAINS = ["urbanoutfitters.com"]
ID_FIELD = "product_id"

# UO sits behind DataDome (the Pinia state even carries a top-level `datadome`
# key, and responses set the `datadome` cookie). A blocked request returns a
# DataDome interstitial / "blocked" JSON rather than the product HTML.
BOT_VENDOR = "DataDome"

# Strings present on a DataDome block/challenge response and effectively never on
# a real product page. Kept tight to avoid false positives (note: the *legit*
# Pinia state contains a `datadome` key, so we do NOT match the bare word).
CAPTCHA_MARKERS = [
    "geo.captcha-delivery.com",          # DataDome challenge asset host
    "captcha-delivery.com",
    "dd_cookie",                          # DataDome cookie set on block pages
    'window.geetest',                     # geetest puzzle sometimes embedded
    "You have been blocked",
    "interstitial.captcha",
    "DataDome",                           # appears on the block page body/title
]

# UO product URLs are flat: https://www.urbanoutfitters.com/shop/<slug>[?...].
# There is no numeric id in the URL — the dedup id (styleNumber, e.g. 106912801)
# lives only in the page body. extract_id therefore returns the slug from the URL
# as a stable per-URL key; parse_product overrides product_id with the numeric
# styleNumber once the body is parsed (the real cross-URL dedup key).
_SHOP_RE = re.compile(r"/shop/([^/?#]+)")


def extract_id(url: str) -> Optional[str]:
    """Return the UO product slug from a /shop/<slug> URL, or None.

    The slug is the only id available from the URL alone. The numeric styleNumber
    (the true dedup key, like Amazon's ASIN) is only in the page body; once parsed
    ``parse_product`` replaces ``product_id`` with it.
    """
    if not url:
        return None
    m = _SHOP_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(slug: str) -> str:
    return f"https://www.urbanoutfitters.com/shop/{slug}"


# --- Public entry point -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from a UO product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    node = _safe(_pinia_product_node, html) or {}
    prod = node.get("product", {}) if isinstance(node, dict) else {}
    sku = node.get("skuInfo", {}) if isinstance(node, dict) else {}
    reviews = node.get("reviews", {}) if isinstance(node, dict) else {}

    out: dict[str, Any] = {}

    # product_id: prefer numeric styleNumber (cross-URL dedup key), else slug.
    style = prod.get("styleNumber") or ld.get("sku") or ld.get("mpn")
    slug = prod.get("productSlug") or extract_id(source_url)
    if style:
        out["product_id"] = str(style)
    elif slug:
        out["product_id"] = str(slug)
    if slug:
        out["url"] = canonical_product_url(slug)
    elif ld.get("url"):
        out["url"] = str(ld["url"])
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, ld, prod))
    _put(out, "brand", _safe(_brand, ld, prod))

    price = _safe(_price, ld, sku)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld, reviews))
    _put(out, "availability", _safe(_availability, ld, sku))

    desc, bullets, care = _safe(_split_description, ld, prod) or (None, None, None)
    _put(out, "description", desc)
    _put(out, "bullets", bullets)

    _put(out, "images", _safe(_images, ld, sku, prod))
    _put(out, "categories", _safe(_breadcrumb, tree, html, ld, prod))
    _put(out, "specs", _safe(_specs, prod, sku))
    _put(out, "variations", _safe(_variations, sku))
    _put(out, "seller", _safe(_seller, ld, prod))

    # UO/fashion + home extras Amazon lacks. Omitted when absent.
    _put(out, "care", care)
    _put(out, "material", _safe(_material, prod, care))
    _put(out, "measurements", _safe(_measurements, prod))
    _put(out, "colour", _safe(_colour, ld, sku))

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
_CCY_SYMBOL = {"$": "USD", "US$": "USD", "£": "GBP", "€": "EUR", "¥": "JPY", "A$": "AUD"}


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


# --- JSON source extraction ---------------------------------------------------


def _iter_jsonld(html: str):
    """Yield each parsed JSON-LD object found in the page (handles arrays/@graph)."""
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
            if isinstance(item, dict) and "@graph" in item and isinstance(item["@graph"], list):
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


def _pinia_state(html: str) -> Optional[dict[str, Any]]:
    """Return the parsed urbnInitialPiniaState SSR state dict, or None.

    The script body is a JSON *string literal* whose value is itself a JSON
    document, so it must be json.loads'd twice.
    """
    tree = HTMLParser(html)
    node = tree.css_first("script#urbnInitialPiniaState")
    if node is None:
        # fall back to scanning by id attribute in raw text if selector misses
        m = re.search(r'id="urbnInitialPiniaState"[^>]*>(.*?)</script>', html, re.S)
        raw = m.group(1) if m else None
    else:
        raw = node.text()
    if not raw or not raw.strip():
        return None
    try:
        outer = json.loads(raw)
    except Exception:
        return None
    state = json.loads(outer) if isinstance(outer, str) else outer
    return state if isinstance(state, dict) else None


def _pinia_product_node(html: str) -> Optional[dict[str, Any]]:
    """Return the catalog.products.<slug> node (product/skuInfo/reviews)."""
    state = _pinia_state(html)
    if not state:
        return None
    products = (state.get("catalog") or {}).get("products")
    if not isinstance(products, dict) or not products:
        return None
    # Prefer the slug the page says is current; else the only/first entry.
    current = (state.get("product") or {}).get("currentSlug")
    if current and isinstance(products.get(current), dict):
        return products[current]
    for v in products.values():
        if isinstance(v, dict) and ("skuInfo" in v or "product" in v):
            return v
    return None


# --- Field extractors ---------------------------------------------------------


def _title(tree: HTMLParser, ld: dict, prod: dict) -> Optional[str]:
    if ld.get("name"):
        return str(ld["name"]).strip()
    if prod.get("displayName"):
        return str(prod["displayName"]).strip()
    for sel in ("h1.c-pwa-product-meta__heading", 'h1[class*="product"]', "h1"):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


def _brand(ld: dict, prod: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict) and b.get("name"):
        return str(b["name"]).strip()
    if isinstance(b, str) and b.strip():
        return b.strip()
    pb = prod.get("brand")
    if isinstance(pb, str) and pb.strip():
        return pb.strip()
    if isinstance(pb, dict) and pb.get("name"):
        return str(pb["name"]).strip()
    return None


def _money_from(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _price(ld: dict, sku: dict) -> Optional[dict[str, Any]]:
    """Price + deal signal.

    Authoritative source is the Pinia ``skuInfo``: ``salePriceLow/High`` is the
    current price; ``listPriceLow/High`` is the original. ``markdownState`` !=
    NONE (or list>sale) flags a markdown -> store the original as ``list_price``
    (the deal signal, mirroring Amazon's ``list_price``). JSON-LD offers are the
    fallback (current price only, no struck-through original).
    """
    currency = "USD"
    sale_lo = _money_from(sku.get("salePriceLow"))
    sale_hi = _money_from(sku.get("salePriceHigh"))
    list_lo = _money_from(sku.get("listPriceLow"))
    if sale_lo is not None:
        out: dict[str, Any] = {"amount": sale_lo, "currency": currency}
        if sale_hi is not None and sale_hi > sale_lo:
            out["price_range"] = {"min": sale_lo, "max": sale_hi}
        # list_price only when a genuine markdown above current exists.
        has_md = bool(sku.get("hasMarkdown")) or (sku.get("markdownState") not in (None, "NONE"))
        if list_lo is not None and list_lo > sale_lo and has_md:
            out["list_price"] = list_lo
        return out

    # JSON-LD offers fallback.
    offers = ld.get("offers")
    offs = offers if isinstance(offers, list) else ([offers] if offers else [])
    amounts: list[float] = []
    for off in offs:
        if not isinstance(off, dict):
            continue
        currency = off.get("priceCurrency") or currency
        for k in ("price", "lowPrice", "highPrice"):
            a = _money_from(off.get(k))
            if a is not None:
                amounts.append(a)
    if amounts:
        lo, hi = min(amounts), max(amounts)
        out = {"amount": lo, "currency": currency}
        if hi > lo:
            out["price_range"] = {"min": lo, "max": hi}
        return out
    return None


def _rating(ld: dict, reviews: dict) -> Optional[dict[str, Any]]:
    out: dict[str, Any] = {}
    ar = ld.get("aggregateRating")
    if isinstance(ar, dict):
        stars = ar.get("ratingValue")
        cnt = ar.get("ratingCount") or ar.get("reviewCount")
        if stars not in (None, ""):
            try:
                out["stars"] = float(stars)
            except (TypeError, ValueError):
                pass
        if cnt not in (None, ""):
            try:
                out["count"] = int(float(cnt))
            except (TypeError, ValueError):
                pass
    if not out and isinstance(reviews, dict):
        stars = reviews.get("averageRating")
        cnt = reviews.get("count")
        if stars not in (None, ""):
            try:
                out["stars"] = float(stars)
            except (TypeError, ValueError):
                pass
        if cnt not in (None, ""):
            try:
                out["count"] = int(float(cnt))
            except (TypeError, ValueError):
                pass
    return out or None


def _availability(ld: dict, sku: dict) -> Optional[str]:
    offers = ld.get("offers")
    offs = offers if isinstance(offers, list) else ([offers] if offers else [])
    for off in offs:
        if isinstance(off, dict) and off.get("availability"):
            return str(off["availability"]).rsplit("/", 1)[-1]  # schema.org/InStock -> InStock
    if sku.get("hasAvailableSku") is True:
        return "InStock"
    if sku.get("hasAvailableSku") is False:
        return "OutOfStock"
    return None


# UO long-descriptions are markdown: a lead paragraph, then sections delimited by
# `**Heading**`, each a list of `\- item` lines. "Features" -> bullets,
# "Content + Care" / "Care" -> care text, anything else stays in description.
_MD_HEADING_RE = re.compile(r"\*\*\s*(.+?)\s*\*\*")


def _md_clean(s: str) -> str:
    """Strip UO markdown escaping/markers and collapse whitespace to one line."""
    s = s.replace("\\-", "-").replace("\\*", "*")
    s = re.sub(r"\*\*(.+?)\*\*", r"\1", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" -\t").strip()


def _split_description(ld: dict, prod: dict):
    """Return (description, bullets, care) parsed from the markdown long copy."""
    raw = prod.get("longDescription") or ld.get("description") or ""
    if not isinstance(raw, str) or not raw.strip():
        return None, None, None

    # Split into (lead text, sections). A section starts at a **Heading** line.
    lines = raw.split("\n")
    lead: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur_head: Optional[str] = None
    cur_lines: list[str] = []

    def flush():
        if cur_head is not None:
            sections.append((cur_head, cur_lines[:]))

    for ln in lines:
        stripped = ln.strip()
        if not stripped:
            continue
        m = _MD_HEADING_RE.fullmatch(stripped)
        if m:
            flush()
            cur_head = m.group(1).strip()
            cur_lines = []
        elif cur_head is None:
            lead.append(stripped)
        else:
            cur_lines.append(stripped)
    flush()

    description = _md_clean(" ".join(lead)) or None

    bullets: list[str] = []
    care_parts: list[str] = []
    for head, body in sections:
        items = [_md_clean(b) for b in body]
        items = [i for i in items if i]
        hl = head.lower()
        if "care" in hl or "content" in hl or "material" in hl or "fabric" in hl:
            care_parts.extend(items)
        elif "feature" in hl or "detail" in hl or "highlight" in hl:
            bullets.extend(items)
        else:
            # Unknown section: fold into bullets if it looks like a list, else desc.
            bullets.extend(items)

    # If markdown had no headings at all, the whole thing is the description and
    # there's nothing to bullet/care-split.
    if not sections and description is None:
        description = _md_clean(raw) or None

    care = "; ".join(care_parts) if care_parts else None
    return description, (bullets or None), care


def _images(ld: dict, sku: dict, prod: dict) -> Optional[dict[str, list]]:
    """main: JSON-LD image[]; variants: per-colour sets built from skuInfo slices."""
    main: list[str] = []
    im = ld.get("image")
    cand = [im] if isinstance(im, str) else (im if isinstance(im, list) else [])
    for u in cand:
        if isinstance(u, str) and u and u not in main:
            main.append(u)

    variants: dict[str, list[str]] = {}
    primary = sku.get("primarySlice") if isinstance(sku, dict) else None
    if isinstance(primary, dict):
        for item in primary.get("sliceItems") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("displayName")
            base = item.get("id")          # e.g. "106912801_001"
            suffixes = item.get("images")  # e.g. ["b","b2","b3","b4"]
            if not (name and base and isinstance(suffixes, list)):
                continue
            urls = [
                f"https://images.urbndata.com/is/image/UrbanOutfitters/{base}_{suf}"
                f"?$xlarge$&fit=constrain&qlt=80&wid=640"
                for suf in suffixes
                if suf
            ]
            if urls:
                variants[str(name)] = urls

    # Fallback main from the default colour's variant set / defaultImage.
    if not main:
        if variants:
            main = next(iter(variants.values()))
        elif prod.get("defaultImage"):
            main = [
                f"https://images.urbndata.com/is/image/UrbanOutfitters/"
                f"{prod['defaultImage']}?$xlarge$&fit=constrain&qlt=80&wid=640"
            ]

    out: dict[str, list] = {}
    if main:
        out["main"] = main
    if variants:
        out["variants"] = variants
    return out or None


def _breadcrumb(tree: HTMLParser, html: str, ld: dict, prod: dict) -> Optional[list[str]]:
    # 1) BreadcrumbList JSON-LD.
    for obj in _iter_jsonld(html):
        if obj.get("@type") == "BreadcrumbList":
            crumbs = []
            for it in obj.get("itemListElement") or []:
                if not isinstance(it, dict):
                    continue
                name = it.get("name") or (it.get("item") or {}).get("name")
                if name:
                    crumbs.append(str(name).strip())
            if crumbs:
                return crumbs
    # 2) parentCategory display path ("Men's > Tops > Hoodies + Sweatshirts").
    pc = prod.get("parentCategory")
    if isinstance(pc, dict) and isinstance(pc.get("displayName"), str):
        parts = [p.strip() for p in pc["displayName"].split(">") if p.strip()]
        if parts:
            return parts
    # 3) JSON-LD Product.category string.
    if isinstance(ld.get("category"), str):
        parts = [p.strip() for p in ld["category"].split(">") if p.strip()]
        if parts:
            return parts
    # 4) DOM fallback.
    crumbs = [_text(a) for a in tree.css('nav[aria-label="Breadcrumb"] a, ol.c-breadcrumbs a, .breadcrumbs a')]
    crumbs = [c for c in crumbs if c]
    return crumbs or None


def _specs(prod: dict, sku: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    if prod.get("styleNumber"):
        out["Style Number"] = str(prod["styleNumber"])
    facets = prod.get("facets") if isinstance(prod.get("facets"), dict) else {}
    label_map = {
        "genders": "Gender",
        "itemType": "Item Type",
        "silhouettes": "Silhouette",
        "attributionProductType": "Product Type",
        "countryOfOrigin": "Country of Origin",
        "care": "Care",
    }
    for key, label in label_map.items():
        vals = facets.get(key)
        if isinstance(vals, list) and vals:
            joined = ", ".join(str(v).strip() for v in vals if str(v).strip())
            if joined:
                out[label] = joined
    if prod.get("sizeGuide"):
        out.setdefault("Size Guide", str(prod["sizeGuide"]))
    return out or None


def _variations(sku: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    colours: list[str] = []
    primary = sku.get("primarySlice") if isinstance(sku, dict) else None
    if isinstance(primary, dict):
        for item in primary.get("sliceItems") or []:
            if isinstance(item, dict) and item.get("displayName"):
                c = str(item["displayName"]).strip()
                if c and c not in colours:
                    colours.append(c)
    sizes: list[str] = []
    secondary = sku.get("secondarySlice") if isinstance(sku, dict) else None
    if isinstance(secondary, dict):
        for item in secondary.get("sliceItems") or []:
            if not isinstance(item, dict):
                continue
            for sz in item.get("includedSizes") or []:
                if isinstance(sz, dict) and sz.get("displayName"):
                    s = str(sz["displayName"]).strip()
                    if s and s not in sizes:
                        sizes.append(s)
            # Some products' slice items are themselves the size values.
            if not item.get("includedSizes") and item.get("displayName"):
                s = str(item["displayName"]).strip()
                if s and s.upper() not in ("REGULAR",) and s not in sizes:
                    sizes.append(s)
    label = (secondary or {}).get("displayLabel", "size") if isinstance(secondary, dict) else "size"
    if colours:
        out["color"] = colours
    if sizes:
        out[str(label).lower()] = sizes
    return out or None


def _seller(ld: dict, prod: dict) -> Optional[str]:
    offers = ld.get("offers")
    offs = offers if isinstance(offers, list) else ([offers] if offers else [])
    for off in offs:
        if isinstance(off, dict):
            s = off.get("seller")
            if isinstance(s, dict) and s.get("name"):
                return str(s["name"]).strip()
    if prod.get("isMarketPlace") is True:
        # marketplace item; seller name may be the brand
        b = _brand(ld, prod)
        if b:
            return b
    return "Urban Outfitters"


def _colour(ld: dict, sku: dict) -> Optional[str]:
    if isinstance(ld.get("color"), str) and ld["color"].strip():
        return ld["color"].strip()
    primary = sku.get("primarySlice") if isinstance(sku, dict) else None
    if isinstance(primary, dict):
        items = primary.get("sliceItems") or []
        if items and isinstance(items[0], dict) and items[0].get("displayName"):
            return str(items[0]["displayName"]).strip()
    return None


_COMPOSITION_RE = re.compile(r"(\d{1,3}\s*%\s*[A-Za-z][^;,]*)")


def _material(prod: dict, care: Optional[str]) -> Optional[str]:
    """Composition line, mined from the Content + Care text (e.g. '60% Cotton, 40% polyester')."""
    text = care or ""
    if not text:
        return None
    parts = _COMPOSITION_RE.findall(text)
    if parts:
        return ", ".join(p.strip() for p in parts)
    return None


def _measurements(prod: dict) -> Optional[str]:
    """sizeAndFit dimensions text (apparel only)."""
    saf = prod.get("sizeAndFit")
    if not isinstance(saf, list):
        return None
    out: list[str] = []
    for entry in saf:
        if isinstance(entry, dict) and entry.get("dimensions"):
            cleaned = _md_clean(str(entry["dimensions"]).replace("\n", " "))
            if cleaned:
                st = entry.get("sizeType")
                out.append(f"{st}: {cleaned}" if st else cleaned)
    return "; ".join(out) if out else None
