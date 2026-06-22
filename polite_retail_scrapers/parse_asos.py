"""Extract structured fields + full visible text from an ASOS product page.

Mirrors the public interface of ``crawler/parse.py`` (the Amazon parser) so the
generic fetcher / frontier / store modules can drive it unchanged. Self-contained:
the small helpers it needs (``_safe``, ``_put``, ``_text``, ``_parse_money``,
``_page_text``) are copied here rather than imported, per the build spec.

Parsing strategy, most-robust first:

  1. JSON-LD ``<script type="application/ld+json">`` with ``"@type":"Product"`` —
     gives name, brand, image, description, sku, offers (price / priceCurrency /
     availability, or an AggregateOffer / offers[] with low+high -> price_range),
     and aggregateRating when present.
  2. ASOS's embedded product config JSON in an inline ``<script>`` (the
     ``window.asos.pdp.config.product = {...}`` blob, or any inline object that
     carries ``media`` / ``facetGroups`` / ``variants`` / ``price``). Richest
     source for images, variations, sizes, brand description.
  3. ASOS public catalogue API JSON (``/api/product/catalogue/v3/products/{id}``)
     — only used as a *supplementary* source via ``parse_product_api_json`` when a
     caller has already fetched it. ``parse_product(html, source_url)`` itself
     never makes network calls; it works on the saved page HTML.
  4. CSS on the rendered DOM as a final fallback (test-id attributes ASOS uses).

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

DOMAINS = ["asos.com"]
ID_FIELD = "product_id"

# ASOS fronts everything (page HTML *and* its public API) with Akamai. Plain
# httpx / curl get one of two responses, both confirmed against live URLs:
#   * HTTP 403 "Access Denied" page served by `Server: AkamaiGHost`, body carries
#     an `errors.edgesuite.net` reference id.
#   * HTTP/2 stream reset (error_code 2 / INTERNAL_ERROR) on the TLS-fingerprint
#     check, which surfaces as a RemoteProtocolError / ReadTimeout.
BOT_VENDOR = "Akamai"

# Strings that appear on ASOS's Akamai block/challenge page and effectively never
# on a real product page. "Access Denied" + the edgesuite error host are the
# reliable pair. Keep the list tight to avoid false positives on real pages.
CAPTCHA_MARKERS = [
    "AkamaiGHost",
    "errors.edgesuite.net",
    "Reference&#32;&#35;",            # HTML-entity-encoded "Reference #" on the block page
    "<TITLE>Access Denied</TITLE>",
    "<H1>Access Denied</H1>",
    "/_sec/cp_challenge/",            # Akamai PoW challenge interstitial path
    "ak_bmsc",                        # Akamai Bot Manager session cookie name
]

# ASOS product URLs look like:
#   https://www.asos.com/us/<brand-slug>/<product-slug>/prd/<product_id>[?...]
# product_id is the trailing numeric id after /prd/.
_PRD_RE = re.compile(r"/prd/(\d+)")


def extract_id(url: str) -> Optional[str]:
    """Return the ASOS numeric product id from a URL, or None."""
    if not url:
        return None
    m = _PRD_RE.search(url)
    return m.group(1) if m else None


def canonical_product_url(product_id: str) -> str:
    return f"https://www.asos.com/prd/{product_id}"


# --- Public entry points ------------------------------------------------------


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict of every field extractable from an ASOS product page.

    Works purely on the supplied page HTML (no network). Missing fields are
    omitted. Each extractor is wrapped so a single miss can't abort the row.
    """
    tree = HTMLParser(html)
    ld = _safe(_jsonld_product, html) or {}
    cfg = _safe(_config_product, html) or {}
    # ASOS splits the page state across several `window.asos.pdp.config.<var>`
    # assignments (confirmed on live HTML, see report §3): the rich product blob
    # lives in `config.product`, but price is in `config.stockPriceResponse`
    # (a JSON-string array keyed by productId), rating in `config.ratings`, and
    # the fashion copy (productDescription/brandDescription/sizeAndFit/careInfo/
    # aboutMe) in `config.productDescription`. Pull each by name.
    desc_cfg = _safe(_config_named, html, "productDescription") or {}
    ratings_cfg = _safe(_config_named, html, "ratings") or {}

    out: dict[str, Any] = {}

    pid = extract_id(source_url) or _safe(_id_from_sources, ld, cfg, html)
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.asos.com/prd/{pid}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    stock_price = _safe(_stock_price_for, html, pid or (cfg.get("id") if isinstance(cfg, dict) else None))

    _put(out, "title", _safe(_title, tree, ld, cfg))
    _put(out, "brand", _safe(_brand, tree, ld, cfg))

    price = _safe(_price, tree, ld, cfg, stock_price)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, ld, ratings_cfg))
    _put(out, "availability", _safe(_availability, ld, cfg, stock_price))
    _put(out, "bullets", _safe(_bullets, tree, cfg, desc_cfg))
    _put(out, "description", _safe(_description, tree, ld, cfg, desc_cfg))
    _put(out, "images", _safe(_images, tree, ld, cfg))
    _put(out, "categories", _safe(_breadcrumb, tree, html))
    _put(out, "specs", _safe(_specs, cfg, desc_cfg))
    _put(out, "variations", _safe(_variations, tree, cfg))
    _put(out, "seller", _safe(_seller, ld, cfg))

    # ASOS-specific fashion sections Amazon lacks.
    _put(out, "size_and_fit", _safe(_size_and_fit, tree, desc_cfg))
    _put(out, "care_info", _safe(_care_info, tree, desc_cfg))
    _put(out, "material", _safe(_material, tree, cfg, desc_cfg))
    _put(out, "brand_about", _safe(_brand_about, cfg, desc_cfg))
    _put(out, "colour", _safe(_colour, tree, ld, cfg))

    _put(out, "page_text", _safe(_page_text, html))
    return out


def parse_product_api_json(payload: Any, source_url: str = "") -> dict[str, Any]:
    """Supplementary: build a record from ASOS catalogue API JSON.

    Only call this when a caller has *already* fetched
    ``/api/product/catalogue/v3/products/{id}``. ``parse_product`` is the canonical
    entry point and never depends on this. The API shape is the same family as the
    embedded ``config.product`` blob, so we route it through the same helpers.
    """
    cfg = payload if isinstance(payload, dict) else {}
    out: dict[str, Any] = {}
    pid = extract_id(source_url) or _safe(_id_from_sources, {}, cfg, "")
    if pid:
        out["product_id"] = pid
        out["url"] = f"https://www.asos.com/prd/{pid}"
    if source_url:
        out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _put(out, "title", _cfg_get(cfg, "name"))
    _put(out, "brand", _cfg_brand(cfg))
    price = _cfg_price(cfg)
    if price:
        pr = price.pop("price_range", None)
        _put(out, "price", price)
        if pr:
            out["price_range"] = pr
    _put(out, "availability", _cfg_availability(cfg))
    _put(out, "images", _cfg_images(cfg))
    _put(out, "variations", _cfg_variations(cfg))
    _put(out, "specs", _cfg_specs(cfg))
    _put(out, "brand_about", _brand_about(cfg))
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
        raw = node.text() or ""
        raw = raw.strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            # Some pages double-encode or include stray trailing chars; try a
            # lenient trailing-comma cleanup.
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


# ASOS embeds a product config object inline. We don't know its exact variable
# name across all page versions, so locate it structurally: find a `{...}` JSON
# object literal in an inline <script> that carries the product-shaped keys.
_CONFIG_HINT_KEYS = ("media", "facetGroups", "variants", "productCode", "totalNumberOfVariants")


def _config_product(html: str) -> Optional[dict[str, Any]]:
    tree = HTMLParser(html)
    candidates: list[dict[str, Any]] = []
    for node in tree.css("script"):
        if (node.attributes.get("type") or "").startswith("application/ld"):
            continue
        js = node.text() or ""
        if not js or "media" not in js and "variants" not in js and "productCode" not in js:
            continue
        for obj in _scan_json_objects(js):
            # ASOS wraps the product object (e.g. {"product": {...}}), so search
            # the parsed object *and* its nested dict/list values for the
            # product-shaped node, not just the top level.
            for cand in _find_product_shaped(obj):
                candidates.append(cand)
    if not candidates:
        return None
    # Prefer the richest object (most product-shaped keys present).
    candidates.sort(key=lambda o: sum(1 for k in _CONFIG_HINT_KEYS if k in o), reverse=True)
    return candidates[0]


def _find_product_shaped(obj: Any, depth: int = 0):
    """Yield every dict (obj or nested) that carries product-config hint keys."""
    if depth > 6:
        return
    if isinstance(obj, dict):
        if any(k in obj for k in _CONFIG_HINT_KEYS):
            yield obj
        for v in obj.values():
            if isinstance(v, (dict, list)):
                yield from _find_product_shaped(v, depth + 1)
    elif isinstance(obj, list):
        for v in obj:
            if isinstance(v, (dict, list)):
                yield from _find_product_shaped(v, depth + 1)


_CONFIG_ASSIGN_TMPL = r"window\.asos\.pdp\.config\.%s\s*=\s*"


def _config_named(html: str, var: str) -> Optional[dict[str, Any]]:
    """Return the object assigned to ``window.asos.pdp.config.<var> = {...}``.

    ASOS assigns several distinct config objects (``product``, ``ratings``,
    ``productDescription``, ...). Locate the assignment by name and balance-match
    the following ``{...}`` literal. Returns None if absent or empty.
    """
    for m in re.finditer(_CONFIG_ASSIGN_TMPL % re.escape(var), html):
        p = m.end()
        # skip whitespace to the opening brace; bail if it isn't an object literal
        while p < len(html) and html[p] in " \t\r\n":
            p += 1
        if p >= len(html) or html[p] != "{":
            continue
        blob = _balanced_object(html, p)
        if blob is None:
            continue
        obj = _try_json(blob)
        if isinstance(obj, dict) and obj:
            return obj
    return None


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


def _stock_price_for(html: str, product_id) -> Optional[dict[str, Any]]:
    """Return the ``productPrice`` block for ``product_id`` from
    ``window.asos.pdp.config.stockPriceResponse``.

    On live ASOS pages price/stock are not in the page DOM or JSON-LD; they live
    in this single-quoted JS string holding a JSON array of
    ``{"productId", "productPrice": {...}}`` entries — one for the main product
    plus any "you might also like"/looks items, so we must match by productId.
    """
    m = re.search(r"window\.asos\.pdp\.config\.stockPriceResponse\s*=\s*'", html)
    if not m:
        return None
    start = m.end()
    i = start
    buf: list[str] = []
    while i < len(html):
        ch = html[i]
        if ch == "\\":
            buf.append(html[i:i + 2])
            i += 2
            continue
        if ch == "'":
            break
        buf.append(ch)
        i += 1
    raw = "".join(buf)
    try:
        raw = raw.encode("utf-8", "replace").decode("unicode_escape")
    except Exception:
        pass
    arr = _try_json(raw)
    if not isinstance(arr, list):
        return None
    chosen = None
    for entry in arr:
        if not isinstance(entry, dict):
            continue
        if product_id is not None and str(entry.get("productId")) == str(product_id):
            chosen = entry.get("productPrice")
            break
    if chosen is None and product_id is None and arr:
        first = arr[0]
        chosen = first.get("productPrice") if isinstance(first, dict) else None
    return chosen if isinstance(chosen, dict) else None


def _scan_json_objects(js: str):
    """Yield top-level-ish JSON objects parsed out of an inline script string.

    Walks the string finding balanced ``{...}`` spans and attempting json.loads on
    each. Cheap and tolerant — good enough to recover ASOS's config blob without
    knowing its assignment syntax.
    """
    n = len(js)
    i = 0
    while i < n:
        if js[i] == "{":
            depth = 0
            in_str = False
            esc = False
            quote = ""
            j = i
            while j < n:
                ch = js[j]
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
                        blob = js[i:j + 1]
                        if len(blob) > 40:  # skip tiny objects
                            obj = _try_json(blob)
                            if obj is not None:
                                yield obj
                        i = j
                        break
                j += 1
            else:
                break
        i += 1


def _try_json(blob: str) -> Any:
    s = blob.strip()
    try:
        return json.loads(s)
    except Exception:
        pass
    # JS-literal-ish fallback: single quotes -> double, strip trailing commas.
    try:
        s2 = re.sub(r",\s*([}\]])", r"\1", s.replace("'", '"'))
        return json.loads(s2)
    except Exception:
        return None


def _cfg_get(cfg: dict, *keys) -> Optional[str]:
    for k in keys:
        v = cfg.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


def _id_from_sources(ld: dict, cfg: dict, html: str) -> Optional[str]:
    # The canonical /prd/<id> product id is JSON-LD `productID` / config `id`.
    # `sku` / `productCode` are the *internal style code* (a different number,
    # e.g. 148299035 vs prd 208829618) — never use them as the product id, or
    # downstream lookups keyed by /prd/ id (stockPriceResponse) silently miss.
    for v in (ld.get("productID"), cfg.get("id")):
        if v and re.fullmatch(r"\d{4,}", str(v)):
            return str(v)
    m = _PRD_RE.search(html or "")
    if m:
        return m.group(1)
    for v in (ld.get("sku"), cfg.get("productCode")):
        if v and re.fullmatch(r"\d{4,}", str(v)):
            return str(v)
    return None


# --- Field extractors ---------------------------------------------------------


def _title(tree: HTMLParser, ld: dict, cfg: dict) -> Optional[str]:
    if ld.get("name"):
        return str(ld["name"]).strip()
    v = _cfg_get(cfg, "name")
    if v:
        return v
    for sel in ('h1[data-testid="product-title"]', "h1.product-hero", "h1"):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    return None


def _brand(tree: HTMLParser, ld: dict, cfg: dict) -> Optional[str]:
    b = ld.get("brand")
    if isinstance(b, dict):
        name = b.get("name")
        if name:
            return str(name).strip()
    elif isinstance(b, str) and b.strip():
        return b.strip()
    cb = _cfg_brand(cfg)
    if cb:
        return cb
    el = tree.css_first('[data-testid="product-brand"] a, [data-testid="product-brand"]')
    return _text(el) or None


def _cfg_brand(cfg: dict) -> Optional[str]:
    b = cfg.get("brand")
    if isinstance(b, dict):
        return (b.get("name") or "").strip() or None
    if isinstance(b, str):
        return b.strip() or None
    bd = cfg.get("brandDescription")
    if isinstance(bd, dict) and bd.get("brandName"):
        return str(bd["brandName"]).strip()
    return None


def _offers(ld: dict):
    o = ld.get("offers")
    if o is None:
        return []
    return o if isinstance(o, list) else [o]


def _stock_price_money(sp: Optional[dict]) -> Optional[dict[str, Any]]:
    """Build a price dict from a stockPriceResponse ``productPrice`` block.

    Shape (live ASOS): ``{"current": {"value", "text"}, "previous": {...},
    "rrp": {...}, "currency": "USD", "isMarkedDown": bool, "discountPercentage"}``.
    ``previous`` (or ``rrp``) above ``current`` is the struck-through "was" price
    -> stored as ``list_price`` (the deal signal, mirroring the Amazon parser).
    """
    if not isinstance(sp, dict):
        return None

    def val(node):
        if isinstance(node, dict):
            v = node.get("value")
            if v not in (None, ""):
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    cur = val(sp.get("current"))
    if cur is None:
        return None
    out: dict[str, Any] = {"amount": cur}
    ccy = sp.get("currency")
    if ccy:
        out["currency"] = ccy
    prev = val(sp.get("previous"))
    rrp = val(sp.get("rrp"))
    was = next((v for v in (prev, rrp) if v is not None and v > cur), None)
    if was is not None:
        out["list_price"] = was
    return out


def _price(tree: HTMLParser, ld: dict, cfg: dict, stock_price: Optional[dict] = None) -> Optional[dict[str, Any]]:
    # 0) stockPriceResponse — the authoritative live-price source on real ASOS
    #    pages (JSON-LD offers are an empty {} and the DOM price is JS-rendered).
    sp = _stock_price_money(stock_price)
    if sp:
        return sp

    # 1) JSON-LD offers — most reliable. Handle AggregateOffer (low/high) + array.
    amounts: list[float] = []
    currency = ""
    for off in _offers(ld):
        if not isinstance(off, dict):
            continue
        currency = currency or off.get("priceCurrency") or ""
        # AggregateOffer
        for k in ("lowPrice", "highPrice", "price"):
            v = off.get(k)
            if v not in (None, ""):
                try:
                    amounts.append(float(str(v).replace(",", "")))
                except ValueError:
                    pass
    if amounts:
        lo, hi = min(amounts), max(amounts)
        cur: dict[str, Any] = {"amount": lo}
        if currency:
            cur["currency"] = currency
        if hi > lo:
            cur["price_range"] = {"min": lo, "max": hi}
        _apply_list_price(cur, cfg, ld)
        return cur

    # 2) Config blob price object.
    cur = _cfg_price(cfg)
    if cur:
        return cur

    # 3) DOM fallback (test-id attributes ASOS renders).
    el = tree.css_first('[data-testid="current-price"], span[data-testid="current-price"], .product-price [class*=current]')
    cur_money = _parse_money(_text(el)) if el is not None else None
    if not cur_money:
        return None
    was = tree.css_first('[data-testid="previous-price"], span[data-testid="previous-price"], .product-price [class*=previous]')
    was_money = _parse_money(_text(was)) if was is not None else None
    if was_money and was_money.get("amount", 0) > cur_money["amount"]:
        cur_money["list_price"] = was_money["amount"]
    return cur_money


def _cfg_price(cfg: dict) -> Optional[dict[str, Any]]:
    p = cfg.get("price")
    if not isinstance(p, dict):
        return None

    def amt(node):
        if isinstance(node, dict):
            v = node.get("value")
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    return None
        return None

    cur_amt = amt(p.get("current"))
    if cur_amt is None:
        return None
    ccy = p.get("currency") or ""
    out: dict[str, Any] = {"amount": cur_amt}
    if ccy:
        out["currency"] = ccy
    prev_amt = amt(p.get("previous")) or amt(p.get("rrp"))
    if prev_amt and prev_amt > cur_amt:
        out["list_price"] = prev_amt
    return out


def _apply_list_price(cur: dict, cfg: dict, ld: dict) -> None:
    if "list_price" in cur:
        return
    p = cfg.get("price")
    if isinstance(p, dict):
        for key in ("previous", "rrp"):
            node = p.get(key)
            if isinstance(node, dict) and node.get("value"):
                try:
                    v = float(node["value"])
                    if v > cur["amount"]:
                        cur["list_price"] = v
                        return
                except (TypeError, ValueError):
                    pass


def _rating(ld: dict, ratings_cfg: Optional[dict] = None) -> Optional[dict[str, Any]]:
    # 1) JSON-LD aggregateRating (rarely populated on ASOS, but cheap to check).
    ar = ld.get("aggregateRating")
    if isinstance(ar, dict):
        out: dict[str, Any] = {}
        val = ar.get("ratingValue")
        cnt = ar.get("reviewCount") or ar.get("ratingCount")
        if val not in (None, ""):
            try:
                out["stars"] = float(val)
            except ValueError:
                pass
        if cnt not in (None, ""):
            try:
                out["count"] = int(float(cnt))
            except ValueError:
                pass
        if out:
            return out
    # 2) config.ratings — the live source. Empty {} when a product has no reviews
    #    (rating then genuinely absent; it's loaded client-side via ratingsApiPath
    #    only for items with reviews).
    if isinstance(ratings_cfg, dict) and ratings_cfg:
        out = {}
        stars = ratings_cfg.get("averageOverallRating") or ratings_cfg.get("averageOverallStarRating")
        cnt = ratings_cfg.get("totalReviewCount")
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
        pct = ratings_cfg.get("percentageRecommended")
        if pct not in (None, ""):
            try:
                out["percentage_recommended"] = int(float(pct))
            except (TypeError, ValueError):
                pass
        return out or None
    return None


def _availability(ld: dict, cfg: dict, stock_price: Optional[dict] = None) -> Optional[str]:
    for off in _offers(ld):
        if isinstance(off, dict) and off.get("availability"):
            return str(off["availability"]).rsplit("/", 1)[-1]  # schema.org/InStock -> InStock
    if cfg.get("isInStock") is True:
        return "InStock"
    if cfg.get("isInStock") is False:
        return "OutOfStock"
    # Derive from per-variant stock flags in config.product (live ASOS shape:
    # each variant carries isAvailable / isInStock).
    variants = cfg.get("variants") if isinstance(cfg.get("variants"), list) else None
    if variants:
        flags = [
            v.get("isInStock") if v.get("isInStock") is not None else v.get("isAvailable")
            for v in variants
            if isinstance(v, dict)
        ]
        flags = [f for f in flags if f is not None]
        if flags:
            return "InStock" if any(flags) else "OutOfStock"
    return None


def _cfg_availability(cfg: dict) -> Optional[str]:
    if cfg.get("isInStock") is True:
        return "InStock"
    if cfg.get("isInStock") is False:
        return "OutOfStock"
    return None


def _bullets(tree: HTMLParser, cfg: dict, desc_cfg: Optional[dict] = None) -> Optional[list[str]]:
    out: list[str] = []
    # 1) Live ASOS: bullets are the <li>s inside config.productDescription
    #    .productDescription (an HTML string, e.g. "<...>Shirts</...> by <...>
    #    <ul><li>Spread collar</li>...</ul>").
    if isinstance(desc_cfg, dict):
        pd = desc_cfg.get("productDescription")
        if isinstance(pd, str) and "<li" in pd:
            out = _html_list_items(pd)
    # 2) DOM accordion fallback.
    if not out:
        for sel in (
            '[data-testid="productDescriptionDetails"] ul li',
            "#productDescriptionDetails ul li",
            ".product-details-container ul li",
            '[data-testid="product-details"] ul li',
        ):
            for li in tree.css(sel):
                t = _text(li)
                if t and t not in out:
                    out.append(t)
            if out:
                break
    if not out:
        info = cfg.get("info") if isinstance(cfg.get("info"), dict) else {}
        details = info.get("productDetails") if isinstance(info, dict) else None
        if isinstance(details, str):
            out = _html_list_items(details)
    return out or None


def _description(tree: HTMLParser, ld: dict, cfg: dict, desc_cfg: Optional[dict] = None) -> Optional[str]:
    # 1) config.productDescription.aboutMe — richest live "About me" copy
    #    (fabric/composition blurb), preferred over the terse JSON-LD description.
    if isinstance(desc_cfg, dict):
        am = desc_cfg.get("aboutMe")
        if isinstance(am, str) and am.strip():
            return _clean_html_text(am)
    # 2) DOM "About me" block.
    for sel in (
        '[data-testid="productDescriptionAboutMe"]',
        "#productDescriptionAboutMe",
        '[data-testid="about-me"]',
    ):
        el = tree.css_first(sel)
        if el and _text(el):
            return _text(el)
    if ld.get("description"):
        return _clean_html_text(str(ld["description"]))
    info = cfg.get("info") if isinstance(cfg.get("info"), dict) else {}
    am = info.get("aboutMe") if isinstance(info, dict) else None
    if isinstance(am, str) and am.strip():
        return _clean_html_text(am)
    return None


def _named_section(tree: HTMLParser, keywords: tuple[str, ...]) -> Optional[str]:
    """Find an accordion/section whose heading matches any keyword; return its text."""
    for hdr in tree.css("h2, h3, h4, button, summary, [role=heading]"):
        label = _text(hdr).lower()
        if not label:
            continue
        if any(kw in label for kw in keywords):
            # the content typically follows in the parent's next block
            parent = hdr.parent
            if parent is not None:
                txt = parent.text(separator=" ", strip=True)
                # strip the heading text itself from the front
                cleaned = txt
                for kw in keywords:
                    pass
                if len(cleaned) > len(_text(hdr)) + 3:
                    return cleaned
    return None


def _desc_field(desc_cfg: Optional[dict], key: str) -> Optional[str]:
    """Clean-text a string field from config.productDescription (e.g. sizeAndFit,
    careInfo, aboutMe, brandDescription, productDescription)."""
    if isinstance(desc_cfg, dict):
        v = desc_cfg.get(key)
        if isinstance(v, str) and v.strip():
            return _clean_html_text(v)
    return None


def _size_and_fit(tree: HTMLParser, desc_cfg: Optional[dict] = None) -> Optional[str]:
    v = _desc_field(desc_cfg, "sizeAndFit")
    if v:
        return v
    return _named_section(tree, ("size", "fit"))


def _care_info(tree: HTMLParser, desc_cfg: Optional[dict] = None) -> Optional[str]:
    v = _desc_field(desc_cfg, "careInfo")
    if v:
        return v
    return _named_section(tree, ("care", "look after"))


# ASOS embeds fabric composition inside aboutMe, e.g. apparel
# "Thick, brushed fabric<br><br>Main: 81% Polyester, ..." and footwear
# "Faux-suede upper<br><br>Lining sock: 50% ..., Sole: 100% ..., Upper: 100% ...".
# A composition label is a word like Main/Lining/Sole/Upper/Body/Shell/Fabric/
# Material/Composition optionally followed by another word, then ": <pct>%".
_COMPOSITION_RE = re.compile(
    r"((?:Main|Lining|Sole|Upper|Body|Shell|Outer|Fabric|Material|Composition)"
    r"(?:\s+\w+)?\s*:\s*\d.*)",
    re.I,
)


def _material(tree: HTMLParser, cfg: dict, desc_cfg: Optional[dict] = None) -> Optional[str]:
    # Pull the composition line out of config.productDescription.aboutMe.
    am = None
    if isinstance(desc_cfg, dict) and isinstance(desc_cfg.get("aboutMe"), str):
        am = _clean_html_text(desc_cfg["aboutMe"])
    if am:
        m = _COMPOSITION_RE.search(am)
        if m:
            return m.group(1).strip()
    sec = _named_section(tree, ("material", "composition", "fabric", "look after"))
    if sec:
        return sec
    info = cfg.get("info") if isinstance(cfg.get("info"), dict) else {}
    for k in ("careInfo", "materials", "composition"):
        v = info.get(k) if isinstance(info, dict) else None
        if isinstance(v, str) and v.strip():
            return _clean_html_text(v)
    return None


def _brand_about(cfg: dict, desc_cfg: Optional[dict] = None) -> Optional[str]:
    # Live ASOS: config.productDescription.brandDescription is an HTML string.
    v = _desc_field(desc_cfg, "brandDescription")
    if v:
        return v
    bd = cfg.get("brandDescription")
    if isinstance(bd, dict):
        v = bd.get("description") or bd.get("content")
        if isinstance(v, str) and v.strip():
            return _clean_html_text(v)
    return None


def _colour(tree: HTMLParser, ld: dict, cfg: dict) -> Optional[str]:
    if isinstance(ld.get("color"), str) and ld["color"].strip():
        return ld["color"].strip()
    v = cfg.get("colour") or cfg.get("color")
    if isinstance(v, str) and v.strip():
        return v.strip()
    el = tree.css_first('[data-testid="productColour"], [data-testid="product-colour"]')
    return _text(el) or None


# --- Images -------------------------------------------------------------------


def _images(tree: HTMLParser, ld: dict, cfg: dict) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    # 1) config.product.images — live ASOS's canonical gallery: a top-level list
    #    of {url, colour, imageType, isPrimary, isVisible} (NOT under media, which
    #    on live pages only holds {"catwalkUrl": ...}).
    main = _cfg_image_list(cfg)

    # 1b) config.media.images — older/alternate shape, kept as a fallback.
    if not main:
        media = cfg.get("media") if isinstance(cfg.get("media"), dict) else None
        if media:
            for img in media.get("images", []) or []:
                url = _img_url(img)
                if url and url not in main:
                    main.append(url)

    # 2) JSON-LD image (string or list).
    if not main:
        im = ld.get("image")
        cand = [im] if isinstance(im, str) else (im if isinstance(im, list) else [])
        main = [u for u in (_img_url(c) for c in cand if isinstance(c, str)) if u]

    # 3) DOM gallery fallback.
    if not main:
        for img in tree.css('[data-testid="product-gallery"] img, .gallery-image img, img[src*="images.asos-media.com"]'):
            src = img.attributes.get("src") or img.attributes.get("data-src") or ""
            url = _img_url(src) if src else None
            if url and url not in main:
                main.append(url)

    # variants: per-colour image sets from config.
    variants = _cfg_variant_images(cfg)

    result: dict[str, list] = {}
    if main:
        result["main"] = main
    if thumbs:
        result["thumbnails"] = thumbs
    if variants:
        result["variants"] = variants
    return result or None


# ASOS serves a placeholder (".../products/missing/missing-1-none.svg") for
# products whose imagery isn't available; never store it as a real image.
_PLACEHOLDER_IMG_RE = re.compile(r"/products/missing/missing", re.I)


def _img_url(img) -> Optional[str]:
    u = None
    if isinstance(img, str):
        u = img
    elif isinstance(img, dict):
        u = img.get("url") or img.get("src")
    if not u or _PLACEHOLDER_IMG_RE.search(u):
        return None
    return _https(u)


def _https(u: str) -> str:
    if u.startswith("//"):
        return "https:" + u
    return u


def _cfg_image_list(cfg: dict) -> list[str]:
    """Ordered, deduped URL list from config.product.images (visible images)."""
    out: list[str] = []
    imgs = cfg.get("images")
    if not isinstance(imgs, list):
        return out
    for img in imgs:
        if isinstance(img, dict) and img.get("isVisible") is False:
            continue
        u = _img_url(img)
        if u and u not in out:
            out.append(u)
    return out


def _cfg_variant_images(cfg: dict) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    # Live ASOS: group the top-level images list by their non-empty `colour`.
    imgs = cfg.get("images")
    if isinstance(imgs, list):
        for img in imgs:
            if not isinstance(img, dict) or img.get("isVisible") is False:
                continue
            name = img.get("colour")
            u = _img_url(img)
            if name and u:
                out.setdefault(str(name), [])
                if u not in out[str(name)]:
                    out[str(name)].append(u)
    if out:
        return out
    media = cfg.get("media") if isinstance(cfg.get("media"), dict) else None
    if not media:
        return out
    for grp in media.get("colourImages", []) or media.get("variants", []) or []:
        if not isinstance(grp, dict):
            continue
        name = grp.get("colour") or grp.get("name")
        urls = [_img_url(i) for i in (grp.get("images") or [])]
        urls = [u for u in urls if u]
        if name and urls:
            out[name] = urls
    return out


def _cfg_images(cfg: dict) -> Optional[dict[str, list]]:
    main = _cfg_image_list(cfg)
    if not main:
        media = cfg.get("media") if isinstance(cfg.get("media"), dict) else None
        if media:
            for img in media.get("images", []) or []:
                u = _img_url(img)
                if u and u not in main:
                    main.append(u)
    out: dict[str, list] = {}
    if main:
        out["main"] = main
    var = _cfg_variant_images(cfg)
    if var:
        out["variants"] = var
    return out or None


# --- Categories / specs / variations / seller --------------------------------


def _breadcrumb(tree: HTMLParser, html: str) -> Optional[list[str]]:
    # 1) BreadcrumbList JSON-LD.
    for obj in _iter_jsonld(html):
        if obj.get("@type") == "BreadcrumbList":
            items = obj.get("itemListElement") or []
            crumbs = []
            for it in items:
                name = (it.get("name") or (it.get("item") or {}).get("name")) if isinstance(it, dict) else None
                if name:
                    crumbs.append(str(name).strip())
            if crumbs:
                return crumbs
    # 2) DOM breadcrumb.
    crumbs = [_text(a) for a in tree.css('[data-testid="breadcrumb"] a, nav.breadcrumb a, ol.breadcrumb a')]
    crumbs = [c for c in crumbs if c]
    return crumbs or None


def _specs(cfg: dict, desc_cfg: Optional[dict] = None) -> Optional[dict[str, str]]:
    out = _cfg_specs(cfg) or {}
    # Live ASOS: Size & Fit / Care live in config.productDescription, not in cfg.info.
    if isinstance(desc_cfg, dict):
        for label, key in (("Size & Fit", "sizeAndFit"), ("Care", "careInfo")):
            if label in out:
                continue
            v = desc_cfg.get(key)
            if isinstance(v, str) and v.strip():
                out[label] = _clean_html_text(v)
    if isinstance(cfg.get("countryOfManufacture"), str) and cfg["countryOfManufacture"].strip():
        out.setdefault("Country of Manufacture", cfg["countryOfManufacture"].strip())
    return out or None


def _cfg_specs(cfg: dict) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}
    pc = cfg.get("productCode") or cfg.get("id")
    if pc:
        out["Product Code"] = str(pc)
    info = cfg.get("info") if isinstance(cfg.get("info"), dict) else {}
    if isinstance(info, dict):
        for label, key in (("Size & Fit", "sizeAndFit"), ("Care", "careInfo")):
            v = info.get(key)
            if isinstance(v, str) and v.strip():
                out[label] = _clean_html_text(v)
    return out or None


def _variations(tree: HTMLParser, cfg: dict) -> Optional[dict[str, list[str]]]:
    return _cfg_variations(cfg) or _dom_variations(tree)


def _cfg_variations(cfg: dict) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    sizes: list[str] = []
    colours: list[str] = []
    for v in cfg.get("variants", []) or []:
        if not isinstance(v, dict):
            continue
        sz = v.get("size") or v.get("sizeName") or v.get("brandSize")
        if sz and str(sz) not in sizes:
            sizes.append(str(sz))
        col = v.get("colour") or v.get("color")
        if col and str(col) not in colours:
            colours.append(str(col))
    # facetGroups (ASOS sometimes exposes colour as a facet across products).
    for fg in cfg.get("facetGroups", []) or []:
        if not isinstance(fg, dict):
            continue
        name = (fg.get("name") or "").lower()
        vals = [str(f.get("name")) for f in (fg.get("facetValues") or []) if isinstance(f, dict) and f.get("name")]
        if "colour" in name or "color" in name:
            for c in vals:
                if c not in colours:
                    colours.append(c)
    if sizes:
        out["size"] = sizes
    if colours:
        out["colour"] = colours
    return out or None


def _dom_variations(tree: HTMLParser) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    sizes = [
        _text(o)
        for o in tree.css('select#variantSelector option, [data-testid="sizeSelector"] option, select[id*=size] option')
        if _text(o) and "select" not in _text(o).lower()
    ]
    if sizes:
        out["size"] = sizes
    return out or None


def _seller(ld: dict, cfg: dict) -> Optional[str]:
    for off in _offers(ld):
        if isinstance(off, dict):
            s = off.get("seller")
            if isinstance(s, dict) and s.get("name"):
                return str(s["name"]).strip()
    if cfg.get("isDtc") is False or cfg.get("sellerId"):
        # partner/marketplace item; name may not be in blob — default below
        pass
    return "ASOS"


# --- HTML text utilities ------------------------------------------------------


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
