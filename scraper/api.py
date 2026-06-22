"""High-level scraping API: fetch one product URL or discover Amazon ASINs."""
from __future__ import annotations

import json
import logging
from typing import Optional

from . import parse_target
from .discover import canonical_product_url, parse_listing
from .fetch import BrowserFetchRequired, CaptchaBlocked, FetchResult, PoliteFetcher, ProductNotFound
from .parse_dispatch import parse_for, retailer_for

log = logging.getLogger(__name__)


def _build_record(fetcher: PoliteFetcher, url: str, html: str) -> dict:
    """Dispatch to the right retailer parser.

    Target is special: its price is XHR-only (the redsky pricing API), so we
    make a lightweight secondary GET and merge it. All other retailers parse
    straight from the page HTML.
    """
    r = retailer_for(url)
    if r is not None and r.name == "target":
        tcin = r.extract_id(url)
        price_data = None
        if tcin:
            try:
                status, body = fetcher.get_text(parse_target.price_api_url(tcin))
                if status == 200 and body:
                    price_data = json.loads(body)
                else:
                    log.warning("target price api returned status %s for tcin %s", status, tcin)
            except Exception as e:
                log.warning("target price fetch failed for tcin %s: %r", tcin, e)
        return parse_target.parse_product(html, url, price_data=price_data)
    return parse_for(url, html)


def scrape_product(url: str, fetcher: Optional[PoliteFetcher] = None) -> dict:
    """Fetch one product URL and return a parsed dict.

    Automatically routes the URL to the correct retailer parser. Creates and
    closes a PoliteFetcher internally when none is provided. Pass a shared
    fetcher when scraping multiple products to reuse the session (cookies,
    UA state, browser context).

    Raises:
        CaptchaBlocked  — retailer served a bot challenge / block page
        ProductNotFound — HTTP 404, product no longer exists
        BrowserFetchRequired — domain needs playwright but it isn't installed
    """
    _owned = fetcher is None
    if _owned:
        fetcher = PoliteFetcher()
    try:
        result: FetchResult = fetcher.fetch(url, kind="product")
        return _build_record(fetcher, url, result.html)
    finally:
        if _owned:
            fetcher.close()


def discover_amazon(
    start_url: str,
    fetcher: Optional[PoliteFetcher] = None,
    max_products: Optional[int] = None,
) -> list[str]:
    """Walk Amazon listing/search/category pages starting from start_url.

    Returns a deduplicated list of canonical product URLs
    (https://www.amazon.com/dp/<ASIN>). Follows pagination until no next-page
    link is found or max_products is reached.

    Pass a shared PoliteFetcher to reuse an existing session. Creates and closes
    one internally when none is provided.
    """
    _owned = fetcher is None
    if _owned:
        fetcher = PoliteFetcher()
    seen: set[str] = set()
    results: list[str] = []
    pending = [start_url]
    try:
        while pending:
            if max_products is not None and len(results) >= max_products:
                break
            page_url = pending.pop(0)
            try:
                result = fetcher.fetch(page_url, kind="listing")
            except (CaptchaBlocked, ProductNotFound, BrowserFetchRequired) as e:
                log.warning("discovery stopped at %s: %r", page_url, e)
                break
            asins, next_url = parse_listing(result.html, result.final_url)
            for asin in asins:
                if asin not in seen:
                    seen.add(asin)
                    results.append(canonical_product_url(asin))
                    if max_products is not None and len(results) >= max_products:
                        break
            if next_url and next_url not in seen:
                seen.add(next_url)
                pending.append(next_url)
        return results
    finally:
        if _owned:
            fetcher.close()


# --- CLI ----------------------------------------------------------------------

def _cli() -> None:
    import argparse
    import sys

    p = argparse.ArgumentParser(
        prog="python -m scraper",
        description="Polite multi-retailer product scraper",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    p_url = sub.add_parser("url", help="Scrape a single product URL and print JSON")
    p_url.add_argument("url", help="Product page URL (any supported retailer)")
    p_url.add_argument("--indent", type=int, default=2, help="JSON indent (default: 2)")

    p_disc = sub.add_parser("discover", help="Discover product URLs from an Amazon listing page")
    p_disc.add_argument("url", help="Amazon search / category / listing URL")
    p_disc.add_argument("--max", type=int, default=None, dest="max_products",
                        metavar="N", help="Stop after N product URLs")

    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

    if args.cmd == "url":
        try:
            result = scrape_product(args.url)
            print(json.dumps(result, indent=args.indent, ensure_ascii=False))
        except CaptchaBlocked as e:
            print(f"ERROR: blocked by anti-bot ({e})", file=sys.stderr)
            sys.exit(1)
        except ProductNotFound:
            print("ERROR: product not found (HTTP 404)", file=sys.stderr)
            sys.exit(1)

    elif args.cmd == "discover":
        urls = discover_amazon(args.url, max_products=args.max_products)
        for u in urls:
            print(u)
        log.info("discovered %d product URLs", len(urls))
