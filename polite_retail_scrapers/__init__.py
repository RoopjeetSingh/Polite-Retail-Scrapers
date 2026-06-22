"""scraper — polite multi-retailer product scraper.

Quick start:
    from scraper import scrape_product
    product = scrape_product("https://www.amazon.com/dp/B0CRMZHDG7")

For scraping multiple products efficiently, share a PoliteFetcher:
    from scraper import PoliteFetcher, scrape_product
    with PoliteFetcher() as fetcher:  # or call fetcher.close() manually
        for url in my_urls:
            product = scrape_product(url, fetcher=fetcher)
"""

from .api import discover_amazon, scrape_product
from .fetch import (
    BrowserFetchRequired,
    CaptchaBlocked,
    FetchResult,
    PoliteFetcher,
    ProductNotFound,
)
from .parse_dispatch import REGISTRY, parse_for, retailer_for

__all__ = [
    # High-level API
    "scrape_product",
    "discover_amazon",
    # Fetcher (for multi-product sessions)
    "PoliteFetcher",
    "FetchResult",
    # Exceptions
    "CaptchaBlocked",
    "ProductNotFound",
    "BrowserFetchRequired",
    # Dispatch / registry
    "REGISTRY",
    "retailer_for",
    "parse_for",
]
