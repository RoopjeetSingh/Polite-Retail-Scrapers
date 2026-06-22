"""Extract structured fields + full visible text from an Amazon product page."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from lxml import html as lxml_html
from selectolax.parser import HTMLParser

from .discover import extract_asin

log = logging.getLogger(__name__)


def parse_product(html: str, source_url: str) -> dict[str, Any]:
    """Return a dict with every field we could extract from a product page.

    Missing fields are omitted rather than included as None. Every individual
    extractor is wrapped so a selector miss can't abort the whole row.
    """
    tree = HTMLParser(html)
    out: dict[str, Any] = {}

    asin = extract_asin(source_url) or _safe(_asin_from_html, tree)
    if asin:
        out["asin"] = asin
        out["url"] = f"https://www.amazon.com/dp/{asin}"
    out["source_url"] = source_url
    out["fetched_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    _put(out, "title", _safe(_title, tree))
    _put(out, "brand", _safe(_brand, tree))
    price = _safe(_price, tree)
    if price:
        price_range = price.pop("price_range", None)
        _put(out, "price", price)
        if price_range:
            out["price_range"] = price_range

    _put(out, "rating", _safe(_rating, tree))
    _put(out, "availability", _safe(_availability, tree))
    _put(out, "bullets", _safe(_bullets, tree))
    _put(out, "description", _safe(_description, tree))
    _put(out, "images", _safe(_images, tree, html))
    _put(out, "categories", _safe(_breadcrumb, tree))
    _put(out, "best_sellers_rank", _safe(_best_sellers_rank, tree))
    _put(out, "specs", _safe(_specs, tree))
    _put(out, "variations", _safe(_variations, tree))
    _put(out, "seller", _safe(_seller, tree))
    _put(out, "frequently_bought_together_asins", _safe(_fbt_asins, tree))
    _put(out, "page_text", _safe(_page_text, html))

    return out


# --- Helpers ------------------------------------------------------------------


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
        log.debug("extractor %s failed: %s", fn.__name__, e)
        return None


def _text(node) -> str:
    if node is None:
        return ""
    return (node.text(strip=True) if hasattr(node, "text") else "").strip()


# --- Field extractors ---------------------------------------------------------


def _asin_from_html(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("input#ASIN, input[name='ASIN']")
    if el is not None:
        v = el.attributes.get("value")
        if v and re.fullmatch(r"[A-Z0-9]{10}", v):
            return v
    return None


def _title(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("#productTitle")
    return _text(el) or None


def _brand(tree: HTMLParser) -> Optional[str]:
    # Byline anchor: e.g. "Visit the Acme Store" / "Brand: Acme"
    el = tree.css_first("#bylineInfo")
    if el is not None:
        t = _text(el)
        t = re.sub(r"^(Visit the |Brand:\s*)", "", t)
        t = re.sub(r"\s+Store$", "", t)
        if t:
            return t
    # Product overview table row labelled "Brand"
    for row in tree.css("#productOverview_feature_div tr"):
        cells = row.css("td, th")
        if len(cells) >= 2 and _text(cells[0]).lower() == "brand":
            return _text(cells[1]) or None
    return None


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


def _price(tree: HTMLParser) -> Optional[dict[str, Any]]:
    cur_node = tree.css_first(
        "#corePrice_feature_div span.a-price > span.a-offscreen, "
        "#corePriceDisplay_desktop_feature_div span.a-price > span.a-offscreen, "
        "span.priceToPay span.a-offscreen, "
        "#priceblock_ourprice, #priceblock_dealprice, #priceblock_saleprice"
    )
    cur = _parse_money(_text(cur_node))

    if not cur:
        # Fallback: clothing/shoes with size-variant pricing — price is spread
        # across multiple span.a-price[data-a-size] nodes (one per comparison item).
        # Collect all, take the minimum as the canonical price, store full range.
        amounts = []
        for node in tree.css("span.a-price[data-a-size] span.a-offscreen"):
            m = _parse_money(_text(node))
            if m and "amount" in m:
                amounts.append(m["amount"])
        if amounts:
            lo, hi = min(amounts), max(amounts)
            cur = {"amount": lo, "currency": "USD"}
            if hi > lo:
                cur["price_range"] = {"min": lo, "max": hi}
        if not cur:
            return None

    # List price — the struck-through "was X" near the main price block.
    list_node = tree.css_first(
        "#corePrice_feature_div span.basisPrice span.a-offscreen, "
        "#corePriceDisplay_desktop_feature_div span.basisPrice span.a-offscreen, "
        "#corePrice_feature_div span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen, "
        "#corePriceDisplay_desktop_feature_div span.a-price.a-text-price[data-a-strike='true'] span.a-offscreen, "
        "#listPrice, #priceblock_listprice"
    )
    list_p = _parse_money(_text(list_node)) if list_node is not None else None
    if list_p and "amount" in list_p and list_p["amount"] > cur["amount"]:
        cur["list_price"] = list_p["amount"]
    return cur


def _rating(tree: HTMLParser) -> Optional[dict[str, Any]]:
    stars: Optional[float] = None
    count: Optional[int] = None

    el = tree.css_first("#acrPopover")
    if el is not None:
        t = el.attributes.get("title") or ""
        m = re.search(r"([0-9]+(?:\.[0-9]+)?)", t)
        if m:
            stars = float(m.group(1))
    if stars is None:
        el = tree.css_first("span.a-icon-alt")
        if el is not None:
            m = re.search(r"([0-9]+(?:\.[0-9]+)?) out of", _text(el))
            if m:
                stars = float(m.group(1))

    el = tree.css_first("#acrCustomerReviewText")
    if el is not None:
        m = re.search(r"([\d,]+)", _text(el))
        if m:
            count = int(m.group(1).replace(",", ""))

    if stars is None and count is None:
        return None
    out: dict[str, Any] = {}
    if stars is not None:
        out["stars"] = stars
    if count is not None:
        out["count"] = count
    return out


def _availability(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("#availability span, #availability")
    return _text(el) or None


_BULLET_NOISE = re.compile(
    r"^(customer reviews|to calculate|showing|report|product description|about this item|"
    r"read more|see more|collapse|expand|click here|^\d+\s*$)",
    re.IGNORECASE,
)


def _bullets(tree: HTMLParser) -> Optional[list[str]]:
    out: list[str] = []

    # Primary: standard electronics/general layout
    for li in tree.css("#feature-bullets ul li"):
        cls = (li.attributes.get("class") or "")
        if "aok-hidden" in cls:
            continue
        t = _text(li)
        if t:
            out.append(t)

    # Fallback: clothing/shoes layout — bullets are in a <ul> inside .a-expander-content
    if not out:
        for li in tree.css(".a-expander-content ul li"):
            t = _text(li)
            if t and len(t) > 30 and not _BULLET_NOISE.match(t):
                out.append(t)

    return out or None


def _description(tree: HTMLParser) -> Optional[str]:
    # Primary: standard description div
    el = tree.css_first("#productDescription")
    if el:
        t = _text(el)
        if t:
            return t

    # Fallback: clothing/shoes use <p> tags directly inside productDescription
    # or a separate product description section
    for sel in ["#productDescription_feature_div p", "#dpx-product-description_feature_div p"]:
        parts = [_text(p) for p in tree.css(sel) if _text(p)]
        if parts:
            return " ".join(parts)

    return None


# --- Images -------------------------------------------------------------------

_COLOR_IMAGES_RE = re.compile(
    r"['\"]colorImages['\"]\s*:\s*\{\s*['\"]initial['\"]\s*:\s*(\[[^\n]*?\])\s*[},]",
    re.DOTALL,
)
_IMG_GALLERY_DATA_RE = re.compile(
    r"['\"]imageGalleryData['\"]\s*:\s*(\[[^\n]*?\])",
    re.DOTALL,
)
_COLOR_TO_ASIN_RE = re.compile(
    r"['\"]colorToAsin['\"]\s*:\s*(\{.*?\})\s*,\s*['\"]",
    re.DOTALL,
)


def _try_json(blob: str) -> Any:
    # Amazon's inline blobs are JS-literal-ish (single quotes, sometimes trailing
    # commas). Coerce to JSON best-effort. If json.loads fails, return None.
    s = blob.strip()
    s = s.replace("'", '"')
    s = re.sub(r",\s*([}\]])", r"\1", s)
    try:
        return json.loads(s)
    except Exception:
        return None


def _images(tree: HTMLParser, html: str) -> Optional[dict[str, list]]:
    main: list[str] = []
    thumbs: list[str] = []

    # 1) Primary source: the colorImages.initial array embedded in inline JS.
    m = _COLOR_IMAGES_RE.search(html)
    if m:
        data = _try_json(m.group(1))
        if isinstance(data, list):
            for entry in data:
                if not isinstance(entry, dict):
                    continue
                hi = entry.get("hiRes") or entry.get("large") or entry.get("mainUrl")
                if hi and hi not in main:
                    main.append(hi)
                th = entry.get("thumb") or entry.get("lowRes")
                if th and th not in thumbs:
                    thumbs.append(th)

    # 2) Secondary: imageGalleryData array (newer pages use this name).
    if not main:
        m2 = _IMG_GALLERY_DATA_RE.search(html)
        if m2:
            data = _try_json(m2.group(1))
            if isinstance(data, list):
                for entry in data:
                    if not isinstance(entry, dict):
                        continue
                    hi = entry.get("mainUrl") or entry.get("large")
                    if hi and hi not in main:
                        main.append(hi)
                    th = entry.get("thumbUrl") or entry.get("thumb")
                    if th and th not in thumbs:
                        thumbs.append(th)

    # 3) Fallback / belt-and-braces: scrape #altImages thumbnails (then upscale
    #    Amazon's image URL convention from ._SX38_ etc. to base size).
    for img in tree.css("#altImages img, li.imageThumbnail img"):
        src = img.attributes.get("src") or img.attributes.get("data-src") or ""
        if not src:
            continue
        if src not in thumbs:
            thumbs.append(src)
        # Strip Amazon's size segment: "...I/AB12._SX38_SY50_CR,0,0,38,50_.jpg"
        # -> "...I/AB12.jpg" gives you the full-size version.
        full = re.sub(r"\._[A-Z0-9,_]+_\.", ".", src)
        if full != src and full not in main:
            main.append(full)

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
    """Pull per-variant image sets from the colorImages map (one key per color)."""
    # The full colorImages object has shape {"initial": [...], "<color>": [...]}.
    # We need a slightly different regex that captures the whole object.
    m = re.search(r"['\"]colorImages['\"]\s*:\s*(\{.*?\})\s*,\s*['\"]", html, re.DOTALL)
    if not m:
        return {}
    obj = _try_json(m.group(1))
    if not isinstance(obj, dict):
        return {}
    out: dict[str, list[str]] = {}
    for variant, entries in obj.items():
        if variant == "initial":
            continue
        if not isinstance(entries, list):
            continue
        urls: list[str] = []
        for e in entries:
            if not isinstance(e, dict):
                continue
            hi = e.get("hiRes") or e.get("large") or e.get("mainUrl")
            if hi and hi not in urls:
                urls.append(hi)
        if urls:
            out[variant] = urls
    return out


# --- Categories / BSR / specs / variations / seller / FBT --------------------


def _breadcrumb(tree: HTMLParser) -> Optional[list[str]]:
    crumbs = [
        _text(a)
        for a in tree.css(
            "#wayfinding-breadcrumbs_feature_div ul li a, "
            "#wayfinding-breadcrumbs_container ul li a"
        )
    ]
    crumbs = [c for c in crumbs if c]
    return crumbs or None


# One BSR entry per <li>: "#543 in Video Games (See Top 100 ...)"
# The "See Top 100..." link is a separate phrase; we anchor on the leading "#NUM in".
_RANK_LI_RE = re.compile(r"#\s*([\d,]+)\s+in\s+([^()]+?)(?:\s*\(|\s*$)", re.IGNORECASE)


def _best_sellers_rank(tree: HTMLParser) -> Optional[list[dict[str, Any]]]:
    out: list[dict[str, Any]] = []

    # Locate the row/section containing the BSR list, then walk its <li> items.
    # Walking individual <li>s avoids the flatten-then-regex pitfall where
    # "See Top 100 in Video Games" leaks into the next entry's category.
    for row_sel in (
        "#productDetails_detailBullets_sections1 tr",
        "#productDetails_db_sections tr",
        "table.prodDetTable tr",
    ):
        for row in tree.css(row_sel):
            th = row.css_first("th")
            if th is None or "Best Sellers Rank" not in _text(th):
                continue
            for li in row.css("td li, td span li"):
                # Use selectolax's text(separator=' ') equivalent: pass a sep so
                # adjacent inline elements aren't glued together without spaces.
                txt = li.text(separator=" ", strip=True)
                m = _RANK_LI_RE.search(txt)
                if not m:
                    continue
                try:
                    rank = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                out.append({"rank": rank, "category": m.group(2).strip()})

    # Fallback layout: detailBullets list with BSR as one of the bullet items.
    if not out:
        for li in tree.css("#detailBulletsWrapper_feature_div li, #detailBullets_feature_div li"):
            txt = li.text(separator=" ", strip=True)
            if "Best Sellers Rank" not in txt:
                continue
            for m in _RANK_LI_RE.finditer(txt):
                try:
                    rank = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                out.append({"rank": rank, "category": m.group(2).strip()})

    # Deduplicate (preserve order).
    seen = set()
    deduped: list[dict[str, Any]] = []
    for r in out:
        key = (r["rank"], r["category"])
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    return deduped or None


def _specs(tree: HTMLParser) -> Optional[dict[str, str]]:
    out: dict[str, str] = {}

    # Product overview key-value table (top of page on many products).
    for row in tree.css("#productOverview_feature_div tr"):
        cells = row.css("td, th")
        if len(cells) >= 2:
            k = _text(cells[0])
            v = _text(cells[1])
            if k and v and k not in out:
                out[k] = v

    # Technical details / additional info tables.
    for table_sel in (
        "#productDetails_techSpec_section_1 tr",
        "#productDetails_techSpec_section_2 tr",
        "#productDetails_detailBullets_sections1 tr",
        "table.prodDetTable tr",
    ):
        for row in tree.css(table_sel):
            th = row.css_first("th")
            td = row.css_first("td")
            if th is None or td is None:
                continue
            k = _text(th)
            v = _text(td)
            if k and v and k not in out and "Best Sellers Rank" not in k:
                out[k] = v

    # detailBullets list format (li.li ... "Key:" then value)
    for li in tree.css("#detailBulletsWrapper_feature_div li, #detailBullets_feature_div li"):
        spans = li.css("span.a-list-item > span")
        if len(spans) >= 2:
            k = _text(spans[0]).rstrip(":").strip()
            v = _text(spans[1])
            if k and v and k not in out and "Best Sellers Rank" not in k:
                out[k] = v

    return out or None


def _variations(tree: HTMLParser) -> Optional[dict[str, list[str]]]:
    out: dict[str, list[str]] = {}
    twister = tree.css_first("#twister_feature_div, #twister")
    if twister is None:
        return None
    for row in twister.css("[id^=variation_]"):
        label_el = row.css_first("label")
        label = _text(label_el).rstrip(":").strip() if label_el is not None else ""
        if not label:
            # id like "variation_color_name" — derive from the id suffix
            rid = row.attributes.get("id") or ""
            label = rid.replace("variation_", "").replace("_name", "")
        values: list[str] = []
        for opt in row.css("li[title], button[title]"):
            v = opt.attributes.get("title") or ""
            v = re.sub(r"^Click to select\s*", "", v).strip()
            if v and v not in values:
                values.append(v)
        if label and values:
            out[label.lower()] = values
    return out or None


def _seller(tree: HTMLParser) -> Optional[str]:
    el = tree.css_first("#sellerProfileTriggerId")
    if el is not None:
        t = _text(el)
        if t:
            return t
    el = tree.css_first("#merchant-info")
    if el is not None:
        t = _text(el)
        m = re.search(r"sold by\s+(.+?)(?:\.|$)", t, re.IGNORECASE)
        if m:
            return m.group(1).strip()
        if t:
            return t
    return None


def _fbt_asins(tree: HTMLParser) -> Optional[list[str]]:
    out: list[str] = []
    for node in tree.css("#sims-fbt [data-asin], #similarities_feature_div [data-asin]"):
        a = node.attributes.get("data-asin") or ""
        if a and re.fullmatch(r"[A-Z0-9]{10}", a) and a not in out:
            out.append(a)
    return out or None


# --- Full visible page text --------------------------------------------------


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
