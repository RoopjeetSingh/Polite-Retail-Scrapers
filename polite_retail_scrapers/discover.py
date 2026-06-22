"""Parse Amazon listing (search / category) pages: extract ASINs + next page."""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from selectolax.parser import HTMLParser

from . import config

# Amazon ASINs are 10 chars of A-Z0-9 (typically B followed by 9 alnum).
_ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)")
_ASIN_BARE = re.compile(r"^[A-Z0-9]{10}$")


def canonical_product_url(asin: str) -> str:
    return f"{config.AMAZON_SCHEME}://{config.AMAZON_HOST}/dp/{asin}"


def absolute_url(href: str, base: str) -> str:
    return urljoin(base, href)


def extract_asin(href: str) -> Optional[str]:
    m = _ASIN_RE.search(href)
    if m:
        return m.group(1)
    return None


def parse_listing(html: str, base_url: str) -> tuple[list[str], Optional[str]]:
    """Return (list of ASINs found, next-page URL or None)."""
    tree = HTMLParser(html)
    asins: list[str] = []
    seen: set[str] = set()

    # 1) Search result cards expose `data-asin` on the wrapper div.
    for node in tree.css("[data-asin]"):
        asin = node.attributes.get("data-asin") or ""
        if asin and _ASIN_BARE.match(asin) and asin not in seen:
            seen.add(asin)
            asins.append(asin)

    # 2) As a fallback (category/browse pages), pull ASINs from any /dp/ or
    #    /gp/product/ link present.
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        asin = extract_asin(href)
        if asin and asin not in seen:
            seen.add(asin)
            asins.append(asin)

    # 3) Next page URL: search uses `a.s-pagination-next`; older browse pages
    #    use `#pagnNextLink`. Either may be absent on the final page.
    next_url: Optional[str] = None
    for selector in ("a.s-pagination-next", "li.a-last a", "#pagnNextLink"):
        el = tree.css_first(selector)
        if el is not None:
            href = el.attributes.get("href")
            if href:
                next_url = absolute_url(href, base_url)
                break

    # Drop tracking-only query params if present so duplicate "next" URLs collapse.
    if next_url:
        next_url = _strip_tracking(next_url)

    return asins, next_url


_TRACKING_PARAMS = ("ref", "ref_", "linkCode", "tag", "qid", "sr")


def _strip_tracking(url: str) -> str:
    p = urlparse(url)
    if not p.query:
        return url
    keep = []
    for pair in p.query.split("&"):
        k, _, _ = pair.partition("=")
        if k not in _TRACKING_PARAMS:
            keep.append(pair)
    return urlunparse(p._replace(query="&".join(keep)))
