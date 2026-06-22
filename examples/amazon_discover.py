"""Discover product URLs from an Amazon search or category page.

discover_amazon() follows pagination and returns a deduplicated list of
canonical product URLs (https://www.amazon.com/dp/<ASIN>).
"""
import json
from polite_retail_scrapers import PoliteFetcher, discover_amazon, scrape_product

listing_url = "https://www.amazon.com/s?k=wireless+headphones"

# Discover up to 40 product URLs across multiple pages.
product_urls = discover_amazon(listing_url, max_products=40)
print(f"Discovered {len(product_urls)} products")
for url in product_urls:
    print(url)

# Optionally scrape the first few using a shared session (more efficient than
# creating a new PoliteFetcher per product).
print("\nScraping first 3 products...")
with PoliteFetcher() as fetcher:
    for url in product_urls[:3]:
        product = scrape_product(url, fetcher=fetcher)
        print(f"  {product.get('title', 'no title')[:70]}")
        print(f"  {json.dumps(product.get('price', {}))}")
