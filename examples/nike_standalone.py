"""Use only the Nike parser — no fetcher, no playwright, no curl_cffi.

Every parse_<retailer>.py module is self-contained. If you already have HTML
(saved from a previous fetch, a browser extension, etc.) you can call
parse_product() directly with just selectolax and lxml installed.

This pattern works identically for all 16 retailers:
    from polite_retail_scrapers.parse_walmart import parse_product
    from polite_retail_scrapers.parse_bestbuy import parse_product
    ...
"""
import gzip
import json

from polite_retail_scrapers.parse_nike import parse_product

# Load a locally-saved HTML file (gzipped or plain).
html_path = "my_nike_page.html.gz"
with gzip.open(html_path) as f:
    html = f.read().decode("utf-8", errors="replace")

# Parse without any network call.
product = parse_product(html, source_url="https://www.nike.com/t/air-force-1-07-mens-shoes/CW2288-111")
print(json.dumps(product, indent=2, ensure_ascii=False))
