"""Scrape a single product URL from any supported retailer."""
import json
from polite_retail_scrapers import scrape_product

# Works with any of the 16 supported retailers — the URL is auto-routed.
url = "https://www.amazon.com/dp/B0CRMZHDG7"

product = scrape_product(url)
print(json.dumps(product, indent=2, ensure_ascii=False))
