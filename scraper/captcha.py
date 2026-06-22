"""Per-domain anti-bot / block-page detection.

Amazon's CAPTCHA markers live in ``config.CAPTCHA_MARKERS``. Each retailer uses a
different anti-bot vendor (Walmart=PerimeterX, Wayfair=PerimeterX, Best
Buy/ASOS/Nordstrom Rack=Akamai), so every retailer parser module exposes its own
``CAPTCHA_MARKERS``. This module maps a URL's domain to the right marker set so
the fetcher can detect a block on any retailer, not just Amazon.
"""
from __future__ import annotations

from urllib.parse import urlparse

from . import config
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

# domain substring -> markers that only appear on that retailer's block page
_DOMAIN_MARKERS: dict[str, tuple[str, ...]] = {
    "amazon.com": tuple(config.CAPTCHA_MARKERS),
}
for _m in (
    parse_walmart, parse_wayfair, parse_asos, parse_bestbuy, parse_nordstromrack,
    parse_target, parse_hm, parse_overstock, parse_dicks, parse_sephora,
    parse_ulta, parse_newegg, parse_urbanoutfitters, parse_nike, parse_costco,
):
    for _d in _m.DOMAINS:
        _DOMAIN_MARKERS[_d] = tuple(_m.CAPTCHA_MARKERS)


def markers_for(url: str) -> tuple[str, ...]:
    """Return the block-page markers for the URL's domain (Amazon's by default)."""
    host = urlparse(url).netloc.lower()
    for domain, markers in _DOMAIN_MARKERS.items():
        if domain in host:
            return markers
    return tuple(config.CAPTCHA_MARKERS)
