"""Extract structured fields + full visible text from an H&M (hm.com) product page.

Mirrors the public interface of ``crawler/parse.py`` (Amazon) and
``crawler/parse_asos.py`` (fashion sibling) so the generic fetcher / frontier /
store modules drive it unchanged. Self-contained: the small helpers it needs
(``_safe``, ``_put``, ``_text``, ``_parse_money``, ``_page_text``) are copied here
rather than imported, per the build spec.

Parsing strategy, most-robust first:

  1. JSON-LD ``<script type="application/ld+json">`` with ``"@type":"Product"`` —
     gives name, brand, image, description, sku, offers (price / priceCurrency /
     availability). H&M's product pages emit a schema.org Product block; this is
     the primary, most stable source.
  2. H&M's embedded product state. Two shapes seen in the wild:
       a) Next.js hydration blob ``<script id="__NEXT_DATA__">`` whose product
          object lives at
          ``props.pageProps.productPageProps.aemData.productArticleDetails``
          (a dict keyed by article id -> {name(=colour), description, sizes,
          compositions, images, whitePriceValue, redPriceValue, ...}).
       b) Older inline assignment ``var productArticleDetails = {...}`` carrying
          the same per-article objects.
     Richest source for colour variants, sizes, composition/material, care, fit,
     concept, and the deal signal (redPriceValue < whitePriceValue).
  3. CSS on the rendered DOM as a final fallback.

Every individual extractor is wrapped in ``_safe`` so one selector miss can't
abort the whole record. Missing fields are omitted, not stored as ``None``.

NOTE (bot protection): live H&M (www2.hm.com) is fronted by Akamai Bot Manager
serving a JS proof-of-work + "Press & Hold" behavioral challenge. curl_cffi
(safari17_0 / chrome120 / chrome131) receives only the challenge shell and cannot
clear it; a real headless browser is required to obtain the product HTML. The
markers below identify that shell so the fetcher can treat it as a block. The
extractors are written against H&M's documented JSON-LD / __NEXT_DATA__ structure.
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

DOMAINS = ["hm.com"]  # covers www2.hm.com and www.hm.com
ID_FIELD = "article_id"

# H&M fronts the entire site with Akamai Bot Manager. Plain httpx / curl /
# curl_cffi (all impersonation profiles tried) get one of:
#   * a 200 with a ~2.5 KB challenge shell (id="sec-if-cpt-container",
#     "Press & Hold" behavioral button, "Powered and protected by" + Akamai logo),
#   * a 403 "Access Denied" served by `Server: AkamaiGHost`.
# The `_abck` cookie comes back unvalidated (`~-1~`) and only flips after the
# in-browser proof-of-work runs.
BOT_VENDOR = "Akamai"

# Strings present on H&M's Akamai challenge / block pages and effectively never
# on a real product page. The behavioral-challenge container id + the Akamai
# "protected by" copy are the reliable pair for the JS PoW shell; AkamaiGHost +
# Access Denied cover the hard 403 block.
CAPTCHA_MARKERS = [
    'id="sec-if-cpt-container"',          # Akamai behavioral-challenge container
    "behavioral-content",                  # Press & Hold challenge body class
    "Powered and protected by",            # Akamai challenge footer copy
    "/akam/",                              # Akamai sensor / pixel path on the shell
    "AkamaiGHost",                         # Server header string on the 403 block
    "<TITLE>Access Denied</TITLE>",
    "<H1>Access Denied</H1>",
    "Reference&#32;&#35;",                 # entity-encoded "Reference #" on block
]

# H&M product URLs look like:
#   https://www2.hm.com/en_us/productpage.0685816002.html
#   https://www2.hm.com/en_us/<slug>/productpage.0685816002.html
# article id is the numeric run inside productpage.<id>.html
_PRODUCTPAGE_RE = re.compile(r"productpage\.(\d+)\.html", re.IGNORECASE)


def extract_id(url: str) -> Optional[str]:
    """Return the H&M numeric article id from a URL, or None."""
    if not url:
        return None
    m = _PRODUCTPAGE_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(article_id: str) -> str:
    return f"https://www2.hm.com/en_us/productpage.{article_id}.html"


# --- Public entry point -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from an H&M product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}

    aid = extract_id(source_url) or _safe(_id_from_sources, ld, html)

    # Locate the per-article product object (colour variant for this id).
    art = _safe(_article_details, html, aid) or {}

    out: dict[str, Any] = {}
    if aid:
        out["article_id"] = aid
        out["url"] = f"https://www2.hm.com/en_us/productpage.{aid}.html"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree, ld, art))
    _put(out, "brand", _safe(_brand, tree, ld, art))

    price = _safe(_price, tree, ld, art)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld))
    _put(out, "availability", _safe(_availability, ld, art))
    _put(out, "bullets", _safe(_bullets, tree, art))
    _put(out, "description", _safe(_description, tree, ld, art))
    _put(out, "images", _safe(_images, tree, ld, art, html))
    _put(out, "categories", _safe(_breadcrumb, tree, html))
    _put(out, "specs", _safe(_specs, art))
    _put(out, "variations", _safe(_variations, tree, art, html, aid))
    _put(out, "seller", _safe(_seller, ld, art))

    # H&M-specific fashion sections Amazon lacks (omitted when absent).
    _put(out, "material", _safe(_material, art))
    _put(out, "care", _safe(_care, art))
    _put(out, "fit", _safe(_fit, art))
    _put(out, "concept", _safe(_concept, art))
    _put(out, "colour", _safe(_colour, ld, art))

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
    """Full visible text with scripts/styles/nav stripped. Site-agnostic."""
    doc = lxml_html.fromstring(html)
    for el in doc.xpath("//script | //style | //noscript | //nav | //header | //footer"):
        el.getparent().remove(el)
    text = doc.text_content()
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip() or None


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


def _next_data(html: str) -> Optional[dict[str, Any]]:
    """Return the parsed __NEXT_DATA__ Next.js hydration blob, if present."""
    tree = HTMLParser(html)
    node = tree.css_first('script#__NEXT_DATA__, script[id="__NEXT_DATA__"]')
    if node is None:
        return None
    raw = (node.text() or "").strip()
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _deep_get(obj: Any, *path) -> Any:
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        else:
            return None
    return cur


def _article_map(html: str) -> dict[str, Any]:
    """Return the productArticleDetails map (article_id -> article object).

    Sources, in order:
      1. __NEXT_DATA__ at props.pageProps.productPageProps.aemData.productArticleDetails
      2. Inline ``var productArticleDetails = {...}`` (older PDP shape).
    Both map article ids (the per-colour numeric codes) to product objects.
    """
    nd = _next_data(html)
    if isinstance(nd, dict):
        m = _deep_get(
            nd, "props", "pageProps", "productPageProps", "aemData", "productArticleDetails"
        )
        if isinstance(m, dict) and m:
            return m
        # Some pages nest it one level differently; scan for it.
        found = _find_article_map(nd)
        if found:
            return found

    # Inline assignment fallback.
    m2 = re.search(r"productArticleDetails\s*[:=]\s*(\{)", html)
    if m2:
        blob = _balanced_object(html, m2.end() - 1)
        if blob:
            obj = _try_json(blob)
            if isinstance(obj, dict) and obj:
                return obj
    return {}


def _find_article_map(obj: Any, depth: int = 0) -> Optional[dict[str, Any]]:
    if depth > 8 or not isinstance(obj, (dict, list)):
        return None
    if isinstance(obj, dict):
        v = obj.get("productArticleDetails")
        if isinstance(v, dict) and v:
            return v
        for val in obj.values():
            r = _find_article_map(val, depth + 1)
            if r:
                return r
    else:
        for val in obj:
            r = _find_article_map(val, depth + 1)
            if r:
                return r
    return None


def _article_details(html: str, aid: Optional[str]) -> dict[str, Any]:
    """Return the article object matching ``aid`` (else the first article)."""
    amap = _article_map(html)
    if not amap:
        return {}
    if aid and aid in amap and isinstance(amap[aid], dict):
        return amap[aid]
    # Match ignoring leading zeros / trailing color suffix differences.
    if aid:
        for k, v in amap.items():
            if str(k).lstrip("0") == str(aid).lstrip("0") and isinstance(v, dict):
                return v
    # Fall back to the first dict article (single-colour PDP).
    for v in amap.values():
        if isinstance(v, dict):
            return v
    return {}


def _balanced_object(s: str, start: int) -> Optional[str]:
    """Return the ``{...}`` substring beginning at ``start`` (brace-balanced)."""
    depth = 0
    in_str = False
    esc = False
    quote = ""
    i = start
    while i < len(s):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        elif ch in "\"'":
            in_str = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return s[start:i + 1]
        i += 1
    return None


def _try_json(blob: str) -> Any:
    s = blob.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    try:
        s2 = re.sub(r",\s*([}\]])", r"\1", s.replace("'", '"'))
        return json.loads(s2)
    except Exception:
        return None


def _id_from_sources(ld: dict, html: str) -> Optional[str]:
    for v in (ld.get("sku"), ld.get("productID"), ld.get("mpn")):
        if v and re.fullmatch(r"\d{6,14}", str(v)):
            return str(v)
    m = _PRODUCTPAGE_RE.search(html or "")
    if m:
        return m.group(1)
    return None


# --- Field extractors ---------------------------------------------------------


def _ld_brand_name(ld: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        n = b.get("name")
        return str(n).strip() if n else None
    if isinstance(b, str) and b.strip():
        return b.strip()
    return None


def _title(tree: HTMLParser, ld: dict, art: dict) -> Optional[str]:
    if ld.get("name"):
        return str(ld["name"]).strip()
    for key in ("productName", "title"):
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    for sel in ("h1.product-item-headline", "h1[class*=ProductName]", "h1"):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


def _brand(tree: HTMLParser, ld: dict, art: dict) -> Optional[str]:
    b = _ld_brand_name(ld)
    if b:
        return b
    for key in ("brandName", "brand"):
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict) and v.get("name"):
            return str(v["name"]).strip()
    return "H&M"


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "")
    m = re.search(r"\d+(?:\.\d+)?", s)
    return float(m.group(0)) if m else None


def _offers(ld: dict):
    o = ld.get("offers")
    if o is None:
        return []
    return o if isinstance(o, list) else [o]


def _price(tree: HTMLParser, ld: dict, art: dict) -> Optional[dict[str, Any]]:
    # 1) Embedded article prices — whitePriceValue (original/list) vs
    #    redPriceValue (current/sale). The deal signal mirrors Amazon's
    #    list_price > price check.
    white = _num(art.get("whitePriceValue"))
    red = _num(art.get("redPriceValue"))
    currency = _art_currency(art) or _ld_currency(ld) or ""
    if red is not None or white is not None:
        # On sale, red is the price paid and white the struck-through original.
        # Off sale, only white is set.
        if red is not None and white is not None and red < white:
            cur: dict[str, Any] = {"amount": red}
            if currency:
                cur["currency"] = currency
            cur["list_price"] = white
            return cur
        amount = red if red is not None else white
        cur = {"amount": amount}
        if currency:
            cur["currency"] = currency
        if red is not None and white is not None and white > red:
            cur["list_price"] = white
        return cur

    # 2) JSON-LD offers.
    amounts: list[float] = []
    ld_ccy = ""
    for off in _offers(ld):
        if not isinstance(off, dict):
            continue
        ld_ccy = ld_ccy or off.get("priceCurrency") or ""
        for k in ("price", "lowPrice", "highPrice"):
            v = _num(off.get(k))
            if v is not None:
                amounts.append(v)
    if amounts:
        lo, hi = min(amounts), max(amounts)
        cur = {"amount": lo}
        if ld_ccy:
            cur["currency"] = ld_ccy
        if hi > lo:
            cur["price_range"] = {"min": lo, "max": hi}
        return cur

    # 3) DOM fallback.
    el = tree.css_first('[class*=price] span.price-value, span[class*=price__value], .product-item-price')
    cur_money = _parse_money(_text(el)) if el is not None else None
    return cur_money or None


def _ld_currency(ld: dict) -> Optional[str]:
    for off in _offers(ld):
        if isinstance(off, dict) and off.get("priceCurrency"):
            return str(off["priceCurrency"])
    return None


def _art_currency(art: dict) -> Optional[str]:
    for k in ("currency", "priceCurrency", "currencyCode"):
        v = art.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _rating(ld: dict) -> Optional[dict[str, Any]]:
    ar = ld.get("aggregateRating")
    if not isinstance(ar, dict):
        return None
    out: dict[str, Any] = {}
    val = _num(ar.get("ratingValue"))
    cnt = ar.get("reviewCount") or ar.get("ratingCount")
    if val is not None:
        out["stars"] = val
    cnt_n = _num(cnt)
    if cnt_n is not None:
        out["count"] = int(cnt_n)
    return out or None


def _availability(ld: dict, art: dict) -> Optional[str]:
    for off in _offers(ld):
        if isinstance(off, dict) and off.get("availability"):
            return str(off["availability"]).rsplit("/", 1)[-1]  # schema.org/InStock -> InStock
    # Derive from per-size availability flags in the article object.
    sizes = art.get("sizes")
    if isinstance(sizes, list) and sizes:
        in_stock = any(
            (s.get("availability") or s.get("inStock") or s.get("available"))
            for s in sizes
            if isinstance(s, dict)
        )
        return "InStock" if in_stock else "OutOfStock"
    av = art.get("availability") or art.get("stockState")
    if isinstance(av, str) and av.strip():
        return av.strip()
    return None


# Composition/care labels that should not be surfaced as feature bullets.
_BULLET_NOISE = re.compile(r"^(art\.\s*no|article\s*number|imported|composition)", re.I)


def _bullets(tree: HTMLParser, art: dict) -> Optional[list[str]]:
    """H&M 'Description & fit' detail bullet list."""
    out: list[str] = []
    # Embedded: details is usually a list of {key,value} or plain strings.
    details = art.get("details")
    if isinstance(details, list):
        for d in details:
            if isinstance(d, str):
                t = _clean(d)
            elif isinstance(d, dict):
                k = d.get("key") or d.get("title") or d.get("name") or ""
                v = d.get("value") or d.get("text") or d.get("description") or ""
                t = _clean(f"{k}: {v}".strip(": ").strip())
            else:
                t = ""
            if t and not _BULLET_NOISE.match(t) and t not in out:
                out.append(t)
    # Some articles carry bullet copy as an HTML string under description blocks.
    if not out:
        for key in ("descriptionFit", "bulletPoints", "characteristics"):
            v = art.get(key)
            if isinstance(v, list):
                for item in v:
                    t = _clean(item if isinstance(item, str) else str(item))
                    if t and t not in out:
                        out.append(t)
            elif isinstance(v, str) and "<li" in v:
                out.extend(_html_list_items(v))
    # DOM fallback: H&M renders a details accordion as a <ul> of bullets.
    if not out:
        for sel in (
            '[class*=ProductDescription] ul li',
            '.product-detail-description-text ul li',
            'ul[class*=details] li',
        ):
            for li in tree.css(sel):
                t = _text(li)
                if t and not _BULLET_NOISE.match(t) and t not in out:
                    out.append(t)
            if out:
                break
    return out or None


def _description(tree: HTMLParser, ld: dict, art: dict) -> Optional[str]:
    for key in ("description", "preamble", "shortDescription"):
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            return _clean(v)
    if ld.get("description"):
        return _clean(str(ld["description"]))
    for sel in (
        '[class*=ProductDescription] p',
        '.product-detail-description-text',
        '[class*=pdp-description]',
    ):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


# --- Images -------------------------------------------------------------------


def _images(tree: HTMLParser, ld: dict, art: dict, html: str) -> Optional[dict[str, list]]:
    main: list[str] = []

    # 1) Embedded article images: list of {url|image|baseUrl} or strings.
    for img in art.get("images") or []:
        u = _img_url(img)
        if u and u not in main:
            main.append(u)

    # 2) JSON-LD image (string or list).
    if not main:
        im = ld.get("image")
        cand = [im] if isinstance(im, str) else (im if isinstance(im, list) else [])
        for c in cand:
            u = _img_url(c)
            if u and u not in main:
                main.append(u)

    # 3) DOM gallery fallback.
    if not main:
        for img in tree.css('img[src*="lp2.hm.com"], img[src*="image.hm.com"], [class*=product-detail] img'):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            u = _img_url(src) if src else None
            if u and u not in main:
                main.append(u)

    variants = _variant_images(html, art)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if variants:
        result["variants"] = variants
    return result or None


def _img_url(img) -> Optional[str]:
    u = None
    if isinstance(img, str):
        u = img
    elif isinstance(img, dict):
        u = img.get("url") or img.get("image") or img.get("baseUrl") or img.get("src")
    if not u or not isinstance(u, str):
        return None
    return _https(u.strip())


def _https(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    return u


def _variant_images(html: str, current: dict) -> dict[str, list[str]]:
    """Per-colour image sets, keyed by colour name, across all articles."""
    out: dict[str, list[str]] = {}
    amap = _article_map(html)
    for art in amap.values():
        if not isinstance(art, dict):
            continue
        name = art.get("name") or art.get("colorName") or art.get("colourName")
        urls: list[str] = []
        for img in art.get("images") or []:
            u = _img_url(img)
            if u and u not in urls:
                urls.append(u)
        if name and urls:
            out.setdefault(str(name), [])
            for u in urls:
                if u not in out[str(name)]:
                    out[str(name)].append(u)
    return out


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(tree: HTMLParser, html: str) -> Optional[list[str]]:
    # 1) BreadcrumbList JSON-LD.
    for obj in _iter_jsonld(html):
        if obj.get("@type") == "BreadcrumbList":
            crumbs = []
            for it in obj.get("itemListElement") or []:
                if not isinstance(it, dict):
                    continue
                name = it.get("name") or _deep_get(it, "item", "name")
                if name:
                    crumbs.append(str(name).strip())
            if crumbs:
                return crumbs
    # 2) DOM breadcrumb.
    crumbs = [
        _text(a)
        for a in tree.css('nav[aria-label="Breadcrumb"] a, ol[class*=breadcrumb] a, .breadcrumbs-placement a')
    ]
    crumbs = [c for c in crumbs if c]
    return crumbs or None


def _specs(art: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    code = art.get("articleCode") or art.get("code") or art.get("id")
    if code:
        out["Article Number"] = str(code)
    mapping = (
        ("Composition", _compositions_text(art)),
        ("Care", _care(art)),
        ("Fit", _fit(art)),
        ("Concept", _concept(art)),
        ("Country of Production", _str(art.get("countryOfProduction") or art.get("countryOfOrigin"))),
    )
    for label, value in mapping:
        if value:
            out.setdefault(label, value)
    return out or None


def _variations(tree: HTMLParser, art: dict, html: str, aid: Optional[str]) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}

    # Sizes from the current article object.
    sizes: list[str] = []
    for s in art.get("sizes") or []:
        if isinstance(s, dict):
            nm = s.get("name") or s.get("sizeName") or s.get("size")
        else:
            nm = s
        if nm and str(nm) not in sizes:
            sizes.append(str(nm))
    if sizes:
        out["size"] = sizes

    # Colours across all articles in the map (each colour = separate article id).
    colours: list[str] = []
    for a in _article_map(html).values():
        if not isinstance(a, dict):
            continue
        nm = a.get("name") or a.get("colorName") or a.get("colourName")
        if nm and str(nm) not in colours:
            colours.append(str(nm))
    if colours:
        out["color"] = colours

    # DOM fallback for sizes.
    if "size" not in out:
        dom_sizes = [
            _text(o)
            for o in tree.css('select[class*=size] option, [class*=sizeSelector] li, [data-testid="size"] li')
            if _text(o) and "select" not in _text(o).lower()
        ]
        if dom_sizes:
            out["size"] = dom_sizes

    return out or None


def _seller(ld: dict, art: dict) -> Optional[str]:
    for off in _offers(ld):
        if isinstance(off, dict):
            s = off.get("seller")
            if isinstance(s, dict) and s.get("name"):
                return str(s["name"]).strip()
    return "H&M"


# --- H&M-specific extras ------------------------------------------------------


def _str(v) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return _clean(v)
    return None


def _compositions_text(art: dict) -> Optional[str]:
    comps = art.get("compositions") or art.get("composition") or art.get("materials")
    if isinstance(comps, list) and comps:
        parts = []
        for c in comps:
            if isinstance(c, str):
                parts.append(_clean(c))
            elif isinstance(c, dict):
                # e.g. {"name": "Shell", "value": "100% Cotton"} or {"compositionType","materials"}
                k = c.get("name") or c.get("compositionType") or c.get("part") or ""
                v = c.get("value") or c.get("materials") or c.get("composition") or ""
                if isinstance(v, list):
                    v = ", ".join(str(x) for x in v)
                t = _clean(f"{k}: {v}".strip(": ").strip()) if (k or v) else ""
                if t:
                    parts.append(t)
        parts = [p for p in parts if p]
        if parts:
            return "; ".join(parts)
    if isinstance(comps, str) and comps.strip():
        return _clean(comps)
    return None


def _material(art: dict) -> Optional[str]:
    return _compositions_text(art)


def _care(art: dict) -> Optional[str]:
    care = art.get("careInstructions") or art.get("care") or art.get("careInfo")
    if isinstance(care, list) and care:
        parts = [_clean(c if isinstance(c, str) else str(c.get("value") or c.get("text") or "")) for c in care]
        parts = [p for p in parts if p]
        if parts:
            return "; ".join(parts)
    if isinstance(care, str) and care.strip():
        return _clean(care)
    return None


def _fit(art: dict) -> Optional[str]:
    for key in ("fit", "fitName", "descriptionFit"):
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            return _clean(v)
    return None


def _concept(art: dict) -> Optional[str]:
    for key in ("concepts", "concept", "sustainability"):
        v = art.get(key)
        if isinstance(v, list) and v:
            parts = [str(x.get("name")) if isinstance(x, dict) else str(x) for x in v]
            parts = [p for p in parts if p and p != "None"]
            if parts:
                return ", ".join(parts)
        if isinstance(v, str) and v.strip():
            return _clean(v)
    return None


def _colour(ld: dict, art: dict) -> Optional[str]:
    if isinstance(ld.get("color"), str) and ld["color"].strip():
        return ld["color"].strip()
    for key in ("name", "colorName", "colourName", "color", "colour"):
        v = art.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# --- HTML text utilities ------------------------------------------------------


def _clean(s) -> str:
    s = str(s)
    if "<" in s:
        try:
            s = lxml_html.fromstring(s).text_content()
        except Exception:
            s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _html_list_items(s: str) -> list[str]:
    items: list[str] = []
    try:
        frag = lxml_html.fromstring(s)
        for li in frag.xpath("//li"):
            t = re.sub(r"\s+", " ", li.text_content()).strip()
            if t:
                items.append(t)
    except Exception:
        pass
    return items
