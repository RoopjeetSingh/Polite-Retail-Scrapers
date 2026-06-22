"""Route a product URL to the correct retailer parser by domain.

Frontier URLs may point at any supported retailer. `parse_for(url, html)` picks
the matching parser; `retailer_for(url)` exposes the retailer descriptor (id
field, id extractor) used for raw-HTML keying and dedup. Each retailer parser
module exposes a uniform interface (`DOMAINS`, `ID_FIELD`, `extract_id`,
`parse_product`); the Amazon parser predates that convention so it's adapted
here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from . import parse as _amazon
from . import (
    parse_asos,
    parse_bestbuy,
    parse_costco,
    parse_dicks,
    parse_hm,
    parse_newegg,
    parse_nike,
    parse_nordstromrack,
    parse_overstock,
    parse_sephora,
    parse_target,
    parse_ulta,
    parse_urbanoutfitters,
    parse_walmart,
    parse_wayfair,
)
from .discover import extract_asin


@dataclass(frozen=True)
class Retailer:
    name: str
    domains: tuple[str, ...]
    id_field: str
    extract_id: Callable[[str], Optional[str]]
    parse_product: Callable[[str, str], dict]


# Amazon predates the uniform module interface — adapt it explicitly.
AMAZON = Retailer("amazon", ("amazon.com",), "asin", extract_asin, _amazon.parse_product)


def _wrap(name: str, module) -> Retailer:
    return Retailer(
        name=name,
        domains=tuple(module.DOMAINS),
        id_field=module.ID_FIELD,
        extract_id=module.extract_id,
        parse_product=module.parse_product,
    )


REGISTRY: tuple[Retailer, ...] = (
    AMAZON,
    _wrap("walmart", parse_walmart),
    _wrap("wayfair", parse_wayfair),
    _wrap("asos", parse_asos),
    _wrap("bestbuy", parse_bestbuy),
    _wrap("nordstromrack", parse_nordstromrack),
    _wrap("target", parse_target),
    _wrap("hm", parse_hm),
    _wrap("overstock", parse_overstock),
    _wrap("dicks", parse_dicks),
    _wrap("sephora", parse_sephora),
    _wrap("ulta", parse_ulta),
    _wrap("newegg", parse_newegg),
    _wrap("urbanoutfitters", parse_urbanoutfitters),
    _wrap("nike", parse_nike),
    _wrap("costco", parse_costco),
)


def retailer_for(url: str) -> Optional[Retailer]:
    """Return the Retailer whose domain matches the URL's host, or None."""
    host = urlparse(url).netloc.lower()
    for r in REGISTRY:
        if any(d in host for d in r.domains):
            return r
    return None


def parse_for(url: str, html: str) -> dict:
    """Dispatch to the retailer parser matching the URL's domain.

    Unknown domains fall back to the Amazon parser — historically every frontier
    URL is Amazon — so legacy single-retailer behavior is preserved.
    """
    r = retailer_for(url) or AMAZON
    return r.parse_product(html, url)


def product_key(url: str, fallback_id: Optional[str] = None) -> str:
    """Return a collision-free raw-HTML key: ``"<retailer>-<id>"``.

    Different retailers can share the same numeric id (a Walmart item id and a
    Best Buy sku could collide), so the retailer name is prefixed. Falls back to
    the frontier-provided id (e.g. the ASIN already stored on the row) when the
    URL's own id can't be parsed.
    """
    r = retailer_for(url)
    if r is None:
        return fallback_id or "unknown"
    pid = r.extract_id(url) or fallback_id or "unknown"
    return f"{r.name}-{pid}"
