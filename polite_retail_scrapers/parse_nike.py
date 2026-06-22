"""Extract structured fields + full visible text from a Nike (nike.com) product page.

Mirrors the public interface of ``crawler/parse.py`` (Amazon) and
``crawler/parse_asos.py`` (apparel sibling) so the generic fetcher / frontier /
store modules drive it unchanged. Self-contained: the small helpers it needs
(``_safe``, ``_put``, ``_text``, ``_parse_money``, ``_page_text``) are copied
here rather than imported, per the build spec.

Parsing strategy, most-robust-first (confirmed against live HTML, see
``data/parser_tests/nike/report.md``):

  1. ``__NEXT_DATA__`` — Nike is a Next.js SSR app. The full product state lives
     in ``<script id="__NEXT_DATA__" type="application/json">`` under
     ``props.pageProps.selectedProduct``. This is the *primary* source: it carries
     ``prices`` (currentPrice / initialPrice / discountPercentage — the deal
     signal), ``productInfo`` (title / subtitle / fullTitle / productDescription /
     featuresAndBenefits / productDetails / sizeFitSections), ``sizes`` (with
     per-size ACTIVE/OOS status -> availability), ``contentImages``,
     ``taxonomyLabels`` (categories), ``genders`` / ``sportTags`` / ``brands``,
     ``styleColor`` / ``styleCode`` (the dedup ids).
  2. JSON-LD ``<script type="application/ld+json">`` — Nike emits a
     ``@type":"ProductGroup"`` with ``hasVariant`` ``Product`` entries
     (name, brand, color, ``mpn`` = style-color, image, size, gtin,
     offers.price/priceCurrency), plus a ``BreadcrumbList``. Used as a fallback
     for title/brand/price/images/categories and to recover the id.
  3. CSS DOM — last-ditch (Nike's DOM is mostly JS-hydrated, so this is thin).

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

DOMAINS = ["nike.com"]
ID_FIELD = "product_id"

# Nike fronts nike.com (page HTML and its product/GraphQL APIs) with Akamai
# (Bot Manager + edge). Against live URLs, curl_cffi impersonate="safari17_0"
# clears the TLS/JA3 fingerprint check and returns the real SSR HTML (HTTP 200,
# full __NEXT_DATA__). Plain httpx/requests draw an Akamai 403 "Access Denied"
# (Server: AkamaiGHost) or an HTTP/2 stream reset. No PerimeterX "Press & Hold"
# JS-proof-of-work challenge was observed on product pages with the safari
# fingerprint, so this parser works on curl_cffi-fetched HTML without a browser.
BOT_VENDOR = "Akamai"

# Strings that appear on Nike's Akamai block/challenge responses and effectively
# never on a real product page. Kept tight to avoid false positives.
CAPTCHA_MARKERS = [
    "AkamaiGHost",
    "Access Denied",                  # Akamai edge 403 title/body
    "errors.edgesuite.net",           # Akamai edge error reference host
    "Reference&#32;&#35;",            # HTML-entity-encoded "Reference #" on block page
    "/_sec/cp_challenge/",            # Akamai PoW challenge interstitial path
    "ak_bmsc",                        # Akamai Bot Manager session cookie name
    "_abck",                          # Akamai Bot Manager sensor cookie
    "px-captcha",                     # PerimeterX challenge (defensive; not seen on PDPs)
    "Press &amp; Hold",               # PerimeterX human-verification widget
]

# Nike PDP URLs:
#   https://www.nike.com/t/<slug>/<STYLE-COLOR>        (US, e.g. .../CW2288-111)
#   https://www.nike.com/<cc>/t/<slug>/<STYLE-COLOR>   (regional)
#   https://www.nike.com/t/<slug>                      (slug-only -> default colourway)
# The dedup id is the trailing style-color (6-8 alnum chars, a hyphen, 3 alnum).
_STYLE_COLOR_RE = re.compile(r"/([A-Z0-9]{4,8}-[A-Z0-9]{3})(?:[/?#]|$)")


def extract_id(url: str) -> Optional[str]:
    """Return the Nike style-color id from a URL, or None.

    Slug-only URLs (no trailing style-color) return None here; in that case
    ``parse_product`` recovers the id from the page's __NEXT_DATA__/JSON-LD.
    """
    if not url:
        return None
    m = _STYLE_COLOR_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(product_id: str) -> str:
    # Nike needs the slug for a clean canonical URL; without it we fall back to
    # the search-by-style endpoint, which redirects to the right PDP.
    return f"https://www.nike.com/u/{product_id}"


# --- Public entry point -------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from a Nike product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    nd = _safe(_next_data, html) or {}
    sp = _safe(_selected_product, nd) or {}
    pg = _safe(_jsonld_productgroup, html) or {}

    out: dict[str, Any] = {}

    pid = extract_id(source_url) or _safe(_id_from_sources, sp, pg, html)
    if pid:
        out["product_id"] = pid
        out["url"] = _safe(_canonical_url, sp, pg, pid) or canonical_product_url(pid)
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, sp, pg, tree))
    _put(out, "brand", _safe(_brand, sp, pg))

    price = _safe(_price, sp, pg)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, pg))
    _put(out, "availability", _safe(_availability, sp, pg))
    _put(out, "bullets", _safe(_bullets, sp))
    _put(out, "description", _safe(_description, sp, pg))
    _put(out, "images", _safe(_images, sp, pg, tree))
    _put(out, "categories", _safe(_categories, sp, html))
    _put(out, "specs", _safe(_specs, sp))
    _put(out, "variations", _safe(_variations, sp, pg))
    _put(out, "seller", _safe(_seller, sp))

    # Apparel/footwear extras Amazon lacks (omitted when absent).
    _put(out, "colorway", _safe(_colorway, sp, pg))
    _put(out, "style_code", _safe(_style_code, sp))
    _put(out, "material", _safe(_material, sp))
    _put(out, "gender", _safe(_gender, sp, pg))
    _put(out, "sport", _safe(_sport, sp))

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


def _clean_html_text(s: str) -> str:
    """Strip tags from a fragment of HTML and collapse whitespace."""
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


# --- JSON source extraction ---------------------------------------------------


def _next_data(html: str) -> Optional[dict[str, Any]]:
    """Parse the ``__NEXT_DATA__`` JSON blob (Nike's Next.js SSR state)."""
    tree = HTMLParser(html)
    node = tree.css_first('script#__NEXT_DATA__')
    raw = node.text() if node is not None else None
    if not raw:
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S
        )
        raw = m.group(1) if m else None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


def _selected_product(nd: dict) -> Optional[dict[str, Any]]:
    """Return ``props.pageProps.selectedProduct`` — the canonical product blob.

    Falls back to ``selectedGroup``/the first entry of ``productGroups`` if the
    selected product is absent (slug-only pages that didn't resolve a colorway).
    """
    pp = (((nd.get("props") or {}).get("pageProps")) or {}) if isinstance(nd, dict) else {}
    sp = pp.get("selectedProduct")
    if isinstance(sp, dict) and sp:
        return sp
    # Fallback: pick the styleColor-keyed product from the first group.
    groups = pp.get("productGroups")
    if isinstance(groups, list):
        for g in groups:
            prods = g.get("products") if isinstance(g, dict) else None
            if isinstance(prods, dict) and prods:
                # prefer the page's styleColor if present
                want = pp.get("styleColor")
                if want and want in prods and isinstance(prods[want], dict):
                    return prods[want]
                for v in prods.values():
                    if isinstance(v, dict):
                        return v
    return None


def _iter_jsonld(html: str):
    """Yield each parsed JSON-LD object found in the page (handles @graph/list)."""
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


def _type_set(obj: dict) -> set:
    t = obj.get("@type")
    if isinstance(t, list):
        return set(t)
    return {t} if t else set()


def _jsonld_productgroup(html: str) -> Optional[dict[str, Any]]:
    """Return the JSON-LD ProductGroup (preferred) or a single Product object."""
    product = None
    for obj in _iter_jsonld(html):
        types = _type_set(obj)
        if "ProductGroup" in types:
            return obj
        if "Product" in types and product is None:
            product = obj
    return product


def _jsonld_breadcrumb(html: str) -> Optional[list[str]]:
    for obj in _iter_jsonld(html):
        if "BreadcrumbList" in _type_set(obj):
            crumbs = []
            for it in obj.get("itemListElement") or []:
                if not isinstance(it, dict):
                    continue
                name = it.get("name") or (it.get("item") or {}).get("name")
                if name:
                    crumbs.append(str(name).strip())
            if crumbs:
                return crumbs
    return None


def _pg_variants(pg: dict):
    hv = pg.get("hasVariant")
    if isinstance(hv, list):
        return [v for v in hv if isinstance(v, dict)]
    return []


# --- Id / url -----------------------------------------------------------------


def _id_from_sources(sp: dict, pg: dict, html: str) -> Optional[str]:
    # 1) selectedProduct.styleColor (canonical Nike style-color id).
    for v in (sp.get("styleColor"), sp.get("displayStyle")):
        if isinstance(v, str) and _STYLE_COLOR_RE.search("/" + v + "/"):
            return v
    # 2) JSON-LD productGroupID / variant mpn.
    pgid = pg.get("productGroupID")
    if isinstance(pgid, str) and _STYLE_COLOR_RE.search("/" + pgid + "/"):
        return pgid
    for v in _pg_variants(pg):
        mpn = v.get("mpn")
        if isinstance(mpn, str) and _STYLE_COLOR_RE.search("/" + mpn + "/"):
            return mpn
    # 3) Anywhere in raw HTML.
    m = re.search(r'"styleColor"\s*:\s*"([A-Z0-9]{4,8}-[A-Z0-9]{3})"', html or "")
    return m.group(1) if m else None


def _canonical_url(sp: dict, pg: dict, pid: str) -> Optional[str]:
    pi = sp.get("productInfo") if isinstance(sp.get("productInfo"), dict) else {}
    for v in (pi.get("url"), _pdp_url(sp.get("pdpUrl")), pg.get("url")):
        if isinstance(v, str) and v.startswith("http"):
            return v
    return None


def _pdp_url(v) -> Optional[str]:
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        u = v.get("url")
        if isinstance(u, str):
            return u
    return None


# --- Field extractors ---------------------------------------------------------


def _pi(sp: dict) -> dict:
    pi = sp.get("productInfo")
    return pi if isinstance(pi, dict) else {}


def _title(sp: dict, pg: dict, tree: HTMLParser) -> Optional[str]:
    pi = _pi(sp)
    for v in (pi.get("fullTitle"), pi.get("title"), pg.get("name")):
        if isinstance(v, str) and v.strip():
            return v.strip()
    el = tree.css_first('h1#pdp_product_title, h1[data-testid="product_title"], h1')
    return _text(el) or None


def _brand(sp: dict, pg: dict) -> Optional[str]:
    brands = sp.get("brands")
    if isinstance(brands, list) and brands:
        # First brand is the umbrella ("Nike"/"Jordan"); keep it simple.
        first = brands[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    b = pg.get("brand")
    if isinstance(b, dict) and b.get("name"):
        return str(b["name"]).strip()
    if isinstance(b, str) and b.strip():
        return b.strip()
    return None


def _prices(sp: dict) -> dict:
    p = sp.get("prices")
    return p if isinstance(p, dict) else {}


def _num(v) -> Optional[float]:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _price(sp: dict, pg: dict) -> Optional[dict[str, Any]]:
    # 1) __NEXT_DATA__ prices — authoritative live price + deal signal.
    pr = _prices(sp)
    cur = _num(pr.get("currentPrice"))
    if cur is not None:
        out: dict[str, Any] = {"amount": cur}
        ccy = pr.get("currency")
        if ccy:
            out["currency"] = ccy
        # initialPrice above currentPrice == struck-through "was" price (deal).
        init = _num(pr.get("initialPrice"))
        if init is not None and init > cur:
            out["list_price"] = init
        return out

    # 2) JSON-LD offers across variants — collect amounts; min = canonical,
    #    spread = price_range.
    amounts: list[float] = []
    currency = ""
    for v in _pg_variants(pg):
        off = v.get("offers")
        offs = off if isinstance(off, list) else ([off] if isinstance(off, dict) else [])
        for o in offs:
            if not isinstance(o, dict):
                continue
            currency = currency or (o.get("priceCurrency") or "")
            a = _num(o.get("price")) or _num(o.get("lowPrice"))
            if a is not None:
                amounts.append(a)
            hi = _num(o.get("highPrice"))
            if hi is not None:
                amounts.append(hi)
    if amounts:
        lo, hi = min(amounts), max(amounts)
        cur2: dict[str, Any] = {"amount": lo}
        if currency:
            cur2["currency"] = currency
        if hi > lo:
            cur2["price_range"] = {"min": lo, "max": hi}
        return cur2
    return None


def _rating(pg: dict) -> Optional[dict[str, Any]]:
    # Nike loads reviews client-side (TurnTo); JSON-LD aggregateRating is the
    # only static source and is usually absent. Emit only when present.
    ar = pg.get("aggregateRating")
    if not isinstance(ar, dict):
        return None
    out: dict[str, Any] = {}
    stars = _num(ar.get("ratingValue"))
    if stars is not None:
        out["stars"] = stars
    cnt = ar.get("reviewCount") or ar.get("ratingCount")
    cnt_n = _num(cnt)
    if cnt_n is not None:
        out["count"] = int(cnt_n)
    return out or None


def _availability(sp: dict, pg: dict) -> Optional[str]:
    # 1) Per-size status from __NEXT_DATA__ sizes (ACTIVE == purchasable).
    sizes = sp.get("sizes")
    if isinstance(sizes, list) and sizes:
        statuses = [
            (s.get("status") or "").upper()
            for s in sizes
            if isinstance(s, dict)
        ]
        if statuses:
            return "InStock" if any(st == "ACTIVE" for st in statuses) else "OutOfStock"
    # 2) Product-level published flags.
    for key in ("publishType", "status"):
        v = sp.get(key)
        if isinstance(v, str) and v:
            # not a clean availability string; skip unless boolean below
            break
    # 3) JSON-LD offers availability.
    for v in _pg_variants(pg):
        off = v.get("offers")
        offs = off if isinstance(off, list) else ([off] if isinstance(off, dict) else [])
        for o in offs:
            if isinstance(o, dict) and o.get("availability"):
                return str(o["availability"]).rsplit("/", 1)[-1]
    return None


def _bullets(sp: dict) -> Optional[list[str]]:
    """Nike Benefits / Product Details map to bullets."""
    out: list[str] = []
    pi = _pi(sp)
    for key in ("featuresAndBenefits", "enhancedBenefits", "productDetails"):
        sections = pi.get(key)
        if not isinstance(sections, list):
            continue
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            body = sec.get("body")
            if isinstance(body, list):
                for item in body:
                    if isinstance(item, str):
                        t = _clean_html_text(item)
                        if t and t not in out:
                            out.append(t)
            elif isinstance(body, str) and "<li" in body:
                for t in _html_list_items(body):
                    if t not in out:
                        out.append(t)
    # sizeFitSections is fit guidance, useful as a detail bullet.
    sf = pi.get("sizeFitSections")
    if isinstance(sf, list):
        for item in sf:
            if isinstance(item, str):
                for t in (_html_list_items(item) if "<li" in item else [_clean_html_text(item)]):
                    if t and t not in out:
                        out.append(t)
    return out or None


def _description(sp: dict, pg: dict) -> Optional[str]:
    pi = _pi(sp)
    v = pi.get("productDescription")
    if isinstance(v, str) and v.strip():
        return _clean_html_text(v)
    md = pi.get("moreDescriptions")
    if isinstance(md, list):
        parts = [_clean_html_text(x) for x in md if isinstance(x, str) and x.strip()]
        if parts:
            return " ".join(parts)
    if isinstance(pg.get("description"), str) and pg["description"].strip():
        return _clean_html_text(pg["description"])
    # variant-level description as last resort
    for variant in _pg_variants(pg):
        d = variant.get("description")
        if isinstance(d, str) and d.strip():
            return _clean_html_text(d)
    return None


# --- Images -------------------------------------------------------------------


def _image_from_content(card: dict) -> Optional[str]:
    """Pull the best URL from a contentImages card's properties."""
    props = card.get("properties") if isinstance(card, dict) else None
    if not isinstance(props, dict):
        return None
    for key in ("squarish", "portrait", "landscape"):
        node = props.get(key)
        if isinstance(node, dict) and isinstance(node.get("url"), str):
            return node["url"]
    u = props.get("url") or props.get("imageUrl")
    return u if isinstance(u, str) else None


def _images(sp: dict, pg: dict, tree: HTMLParser) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []
    variants: dict[str, list[str]] = {}

    # 1) __NEXT_DATA__ selectedProduct.contentImages — primary gallery for the
    #    selected colorway.
    ci = sp.get("contentImages")
    if isinstance(ci, list):
        for card in ci:
            if isinstance(card, dict) and (card.get("cardType") == "image" or "properties" in card):
                u = _image_from_content(card)
                if u and u not in main:
                    main.append(u)

    # 2) JSON-LD variant images, grouped per color into variants.
    for v in _pg_variants(pg):
        img = v.get("image")
        urls = [img] if isinstance(img, str) else (img if isinstance(img, list) else [])
        urls = [u for u in urls if isinstance(u, str)]
        if not urls:
            continue
        color = v.get("color")
        if color:
            variants.setdefault(str(color), [])
            for u in urls:
                if u not in variants[str(color)]:
                    variants[str(color)].append(u)
        if not main:
            for u in urls:
                if u not in main:
                    main.append(u)

    # 3) DOM fallback.
    if not main:
        for img in tree.css('img[src*="static.nike.com"], picture img'):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            if src and src.startswith("http") and src not in main:
                main.append(src)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


# --- Categories / specs / variations / seller --------------------------------


def _categories(sp: dict, html: str) -> Optional[list[str]]:
    # 1) taxonomyLabels — clean category facets (Gender / Sports / Product Type).
    tax = sp.get("taxonomyLabels")
    if isinstance(tax, dict) and tax:
        crumbs: list[str] = []
        # Order: Gender, Product Type, Collections, Sports — a sensible path.
        for key in ("Gender", "Product Type", "Collections", "Sports"):
            vals = tax.get(key)
            if isinstance(vals, list):
                for v in vals:
                    if isinstance(v, str) and v and v not in crumbs:
                        crumbs.append(v)
        if crumbs:
            return crumbs
    # 2) JSON-LD BreadcrumbList.
    return _jsonld_breadcrumb(html)


def _specs(sp: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    if sp.get("styleColor"):
        out["Style"] = str(sp["styleColor"])
    if sp.get("styleCode"):
        out.setdefault("Style Code", str(sp["styleCode"]))
    cd = sp.get("colorDescription")
    if isinstance(cd, str) and cd.strip():
        out["Colour Shown"] = cd.strip()
    pt = sp.get("productType")
    if isinstance(pt, str) and pt.strip():
        out["Product Type"] = pt.strip().title()
    coo = sp.get("manufacturingCountriesOfOrigin")
    if isinstance(coo, list) and coo:
        names = [str(c) for c in coo if c]
        if names:
            out["Country of Origin"] = ", ".join(names)
    # "Product Details" section -> flatten any "Key: value" lines as specs too.
    pi = _pi(sp)
    for sec in (pi.get("productDetails") or []):
        if not isinstance(sec, dict):
            continue
        for line in (sec.get("body") or []):
            if isinstance(line, str) and ":" in line and len(line) < 80:
                k, _, val = line.partition(":")
                k, val = k.strip(), val.strip()
                if k and val and k not in out:
                    out[k] = val
    return out or None


def _variations(sp: dict, pg: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    sizes: list[str] = []
    colours: list[str] = []

    # Sizes from __NEXT_DATA__.
    for s in (sp.get("sizes") or []):
        if not isinstance(s, dict):
            continue
        label = s.get("label") or s.get("localizedLabel")
        if label and str(label) not in sizes:
            sizes.append(str(label))

    # Colours: the styleColor's colorDescription plus any JSON-LD variant colours.
    cd = sp.get("colorDescription")
    if isinstance(cd, str) and cd.strip():
        colours.append(cd.strip())
    for v in _pg_variants(pg):
        c = v.get("color")
        if isinstance(c, str) and c and c not in colours:
            colours.append(c)
        # JSON-LD variant sizes too (fallback when sizes[] absent).
        if not sizes:
            sz = v.get("size")
            if isinstance(sz, str) and sz and sz not in sizes:
                sizes.append(sz)

    if sizes:
        out["size"] = sizes
    if colours:
        out["color"] = colours
    return out or None


def _seller(sp: dict) -> Optional[str]:
    # Nike sells direct; brands[0] distinguishes Nike vs Jordan vs Converse.
    brands = sp.get("brands")
    if isinstance(brands, list) and brands and isinstance(brands[0], str):
        b = brands[0].strip()
        if b in ("Jordan", "Converse"):
            return b
    return "Nike"


# --- Apparel/footwear extras --------------------------------------------------


def _colorway(sp: dict, pg: dict) -> Optional[str]:
    cd = sp.get("colorDescription")
    if isinstance(cd, str) and cd.strip():
        return cd.strip()
    for v in _pg_variants(pg):
        c = v.get("color")
        if isinstance(c, str) and c.strip():
            return c.strip()
    return None


def _style_code(sp: dict) -> Optional[str]:
    sc = sp.get("styleCode")
    if isinstance(sc, str) and sc.strip():
        return sc.strip()
    return None


# Nike puts fabric/composition in a "Product Details" bullet or the description.
_COMPOSITION_RE = re.compile(
    r"((?:Body|Shell|Lining|Pocket|Rib|Upper|Sole|Main|Fabric|Material|Composition)"
    r"(?:\s+\w+)?\s*:\s*\d.*?%|(?:\d{1,3}%\s+[A-Za-z][A-Za-z ]+))",
    re.I,
)


def _material(sp: dict) -> Optional[str]:
    pi = _pi(sp)
    # Scan Product Details / Benefits bodies for a composition line.
    for key in ("productDetails", "featuresAndBenefits"):
        for sec in (pi.get(key) or []):
            if not isinstance(sec, dict):
                continue
            for line in (sec.get("body") or []):
                if not isinstance(line, str):
                    continue
                t = _clean_html_text(line)
                m = _COMPOSITION_RE.search(t)
                if m:
                    return t.strip()
    return None


_GENDER_MAP = {"MEN": "Men", "WOMEN": "Women", "BOYS": "Boys", "GIRLS": "Girls",
               "UNISEX": "Unisex", "KIDS": "Kids"}


def _gender(sp: dict, pg: dict) -> Optional[str]:
    genders = sp.get("genders")
    if isinstance(genders, list) and genders:
        mapped = [_GENDER_MAP.get(str(g).upper(), str(g).title()) for g in genders if g]
        mapped = list(dict.fromkeys(mapped))
        if mapped:
            return "/".join(mapped)
    # JSON-LD audience.suggestedGender (schema.org/Male).
    aud = pg.get("audience")
    if isinstance(aud, dict):
        g = aud.get("suggestedGender")
        if isinstance(g, str) and g:
            tail = g.rsplit("/", 1)[-1]
            return {"Male": "Men", "Female": "Women"}.get(tail, tail)
    return None


def _sport(sp: dict) -> Optional[str]:
    tags = sp.get("sportTags")
    if isinstance(tags, list):
        vals = [str(t).strip() for t in tags if t]
        vals = list(dict.fromkeys(vals))
        if vals:
            return ", ".join(vals)
    return None
