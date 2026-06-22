# Retail-scraper

A polite, production-quality product data extractor for 16 major US retailers. Auto-routes any supported product URL to the right parser. Returns plain Python dicts. I would try to keep all the scrapers updated and will try to check in on this every 3 months since websites change very often. I will soon create a script that can run the scrapers every few days and check if they still work, and in case they don't I will update them.

---

## What can you do with this?

**You have saved HTML → parse it instantly, no network needed.**
Every `parse_<retailer>.py` is self-contained. Drop in an HTML file, call `parse_product(html, url)`, get a clean dict back. Just `selectolax` and `lxml` needed.

**You have a list of product URLs → scrape them politely.**
Pass any mix of URLs from any of the 16 supported retailers into `scrape_product()`. It auto-routes each URL to the right parser and handles all the hard parts: jittered delays, user-agent rotation, CAPTCHA detection, automatic 30–90 min backoff, browser fingerprint impersonation, and headless Playwright for JS-challenge sites. Make sure to read the **limitations** section.

**You want to find Amazon products from a search or category page → discover them.**
`discover_amazon(url)` walks listing pages, follows pagination, and returns a plain list of product URLs. Feed that list straight into `scrape_product()` for a full discover-and-extract pipeline.

**You want to use just one retailer → copy one file.**
The parsers are intentionally self-contained. If you only need Nike, copy `parse_nike.py` into your project and you're done.

---

## Features

- **16 retailers**: Amazon, Walmart, Best Buy, Target, ASOS, Sephora, Ulta, Newegg, Urban Outfitters, Nike, Wayfair, Nordstrom Rack, H&M, Overstock, Dick's Sporting Goods, Costco
- **3 fetch strategies** : plain `httpx` for basic sites; `curl_cffi` with Safari TLS fingerprint for Akamai-fronted retailers; headless Playwright Chromium for PerimeterX / Akamai JS proof-of-work sites
- **CAPTCHA detection + backoff** : per-domain marker registry detects block pages and bot challenges; automatic 30–90 min sleep + cookie reset + UA rotation after a block event
- **Polite delays with jitter** : per-request random delay (20–45s product, 18–35s listing) plus extra noise, preventing fixed-interval detection
- **Random long pauses** : 1-in-25 chance of a 90–240s "human stepped away" pause
- **User-agent rotation** : rotates UA string every 20–40 requests across a pool of real browser strings
- **Persistent browser contexts** : one Playwright `BrowserContext` per domain, reusing clearance cookies (`_abck`, `_px*`) across requests
- **PerimeterX "Press & Hold" solver** : finds the PX captcha button and executes a human-like mouse movement: ease-in-out easing, micro-tremor drift, natural hold duration
- **playwright-stealth applied** : masks `navigator.webdriver`, fake plugins, and other automation fingerprints
- **Amazon listing discovery** : walks search / category pages following pagination, returns deduplicated product URLs
- **Standalone retailer parsers** : every `parse_<retailer>.py` is fully self-contained; use one parser directly with just `selectolax` + `lxml` installed, no fetcher required
- **All politeness features individually toggleable** : env vars control delays, UA rotation, long pauses, and CAPTCHA backoff independently
- **Uniform output schema** : all 16 parsers return the same field names: `title`, `brand`, `price`, `images`, `bullets`, `description`, `specs`, `variations`, `categories`, `page_text`, plus retailer-specific extras

---

## Installation

Clone the repo, then install with the extras you need:

```bash
# Amazon + Walmart only (httpx, no curl_cffi or playwright)
pip install -e "."

# + Akamai-fronted retailers: Best Buy, ASOS, Target, Sephora, Ulta, Newegg, Urban Outfitters, Nike
pip install -e ".[curl]"

# + JS-challenge retailers: Wayfair, Nordstrom Rack, H&M, Overstock, Dick's, Costco
pip install -e ".[all]"
playwright install chromium  # one-time: downloads the Chromium browser binary
```

> **What `-e` means:** editable install: Python points directly at this folder, so any edits you make to the source take effect immediately without reinstalling.

---

## Quick Start

```python
from scraper import scrape_product
import json

product = scrape_product("https://www.amazon.com/dp/B0CRMZHDG7")
print(json.dumps(product, indent=2))
```

---

## Supported Retailers

| Retailer | Fetch Strategy | Bot Vendor | Notes |
|---|---|---|---|
| Amazon | `httpx` | Native CAPTCHA | |
| Walmart | `httpx` | PerimeterX | |
| Best Buy | `curl_cffi` | Akamai | |
| ASOS | `curl_cffi` | Akamai | |
| Target | `curl_cffi` | Akamai | Price via secondary XHR (handled automatically) |
| Sephora | `curl_cffi` | Akamai | |
| Ulta | `curl_cffi` | Akamai | |
| Newegg | `curl_cffi` | Akamai | |
| Urban Outfitters | `curl_cffi` | DataDome | |
| Nike | `curl_cffi` | Akamai | |
| Wayfair | `browser` | PerimeterX | Requires residential proxy for reliable fetching |
| Nordstrom Rack | `browser` | Akamai | Requires `playwright install chromium` |
| H&M | `browser` | Akamai | Requires residential proxy |
| Overstock | `browser` | Akamai | Requires residential proxy |
| Dick's Sporting Goods | `browser` | Akamai | Requires residential proxy |
| Costco | `browser` | Akamai | Requires residential proxy; price is membership-gated |

---

## Feature Flags

All politeness features are on by default and can be individually disabled via environment variables.

| Env Var | Default | Effect when set to `0` |
|---|---|---|
| `SCRAPER_ENABLE_DELAYS` | `1` | No sleep between requests (useful for offline/local HTML testing) |
| `SCRAPER_ENABLE_UA_ROTATION` | `1` | Always use the first User-Agent string |
| `SCRAPER_ENABLE_LONG_PAUSES` | `1` | Skips the random 90–240s "human stepped away" pause |
| `SCRAPER_ENABLE_CAPTCHA_BACKOFF` | `1` | Raises `CaptchaBlocked` immediately instead of sleeping 30–90 min |

```bash
# Example: fast offline testing with no delays
SCRAPER_ENABLE_DELAYS=0 SCRAPER_ENABLE_CAPTCHA_BACKOFF=0 python my_script.py
```

See `.env.example` for a copy-paste template.

---

## Usage

### Scrape a single product URL

```python
from scraper import scrape_product

# URL is auto-routed. Works with any supported retailer
product = scrape_product("https://www.bestbuy.com/site/apple-macbook-pro/6525065.p?skuId=6525065")
print(product["title"])
print(product["price"])
```

### Share a fetcher across multiple products (recommended for bulk scraping)

Sharing a `PoliteFetcher` reuses the HTTP session, UA state, and browser contexts which is more efficient and more realistic-looking than creating a new one per product.

```python
from scraper import PoliteFetcher, scrape_product

with PoliteFetcher() as fetcher:
    for url in my_product_urls:
        try:
            product = scrape_product(url, fetcher=fetcher)
            print(product["title"])
        except Exception as e:
            print(f"failed: {e}")
```

### Discover Amazon product URLs, then extract them

`discover_amazon()` returns a plain Python list of product URLs. Pipe it directly into `scrape_product()`. You could change this to using a database so that you could scrape the product urls later.

```python
from scraper import PoliteFetcher, discover_amazon, scrape_product

urls = discover_amazon(
    "https://www.amazon.com/s?k=mechanical+keyboards",
    max_products=100,
)
# urls is just a list[str], iterate it however you like

# Scrape them using a shared session (one PoliteFetcher reuses cookies + UA state)
products = []
with PoliteFetcher() as fetcher:
    for url in urls:
        try:
            products.append(scrape_product(url, fetcher=fetcher))
        except Exception as e:
            print(f"skipped {url}: {e}")

# Do whatever you want with the list of dicts
for p in products:
    print(p["title"], p.get("price", {}))
```

You can also pass a shared fetcher into `discover_amazon` so discovery and extraction share the same session:

```python
with PoliteFetcher() as fetcher:
    urls = discover_amazon(start_url, fetcher=fetcher, max_products=50)
    for url in urls:
        product = scrape_product(url, fetcher=fetcher)
```

### Use a single retailer parser standalone (no fetcher needed)

Every `parse_<retailer>.py` has zero dependency on `fetch.py`, `browser_fetch.py`, or `playwright`. If you already have HTML (e.g. from your own fetch layer, a browser extension, or a local file), you can call the parser directly with only `selectolax` + `lxml` installed.

```python
import gzip
from scraper.parse_nike import parse_product  # or any other retailer

with gzip.open("saved_nike_page.html.gz") as f:
    html = f.read().decode()

product = parse_product(html, source_url="https://www.nike.com/t/air-force-1-07/CW2288-111")
print(product["title"])
print(product["price"])
```

This works identically for every supported retailer:
```python
from scraper.parse_walmart import parse_product
from scraper.parse_bestbuy import parse_product
from scraper.parse_sephora import parse_product
# ...
```

---

## Output Schema

All parsers return the same field names where the concept exists. Fields are omitted (not `null`) when unavailable.

```python
{
    # Retailer-specific product ID (asin, item_id, sku, tcin, product_id, etc.)
    "asin": "B0CRMZHDG7",

    "url": "https://www.amazon.com/dp/B0CRMZHDG7",
    "source_url": "https://www.amazon.com/dp/B0CRMZHDG7",
    "fetched_at": "2026-06-19T12:00:00+00:00",

    "title": "Product Name",
    "brand": "Brand Name",

    "price": {
        "amount": 29.99,
        "currency": "USD",
        "list_price": 49.99,   # present when there's a struck-through "was" price
    },
    "price_range": {           # present for variant-priced products
        "min": 29.99,
        "max": 59.99,
    },

    "rating": {"stars": 4.5, "count": 1234},
    "availability": "In Stock",

    "bullets": ["Feature one", "Feature two"],
    "description": "Full product description text...",

    "images": {
        "main": ["https://...full-res.jpg"],
        "thumbnails": ["https://...thumb.jpg"],
        "variants": {"Red": ["https://...red.jpg"]},  # per-color when available
    },

    "categories": ["Electronics", "Keyboards"],
    "specs": {"Switch Type": "Mechanical", "Connectivity": "Wireless"},
    "variations": {"color": ["Black", "White"], "size": ["TKL", "Full"]},
    "seller": "Third-party seller name",   # omitted if sold directly by retailer

    "page_text": "Full visible page text with scripts/nav/styles stripped...",

    # Retailer-specific extras (present when available):
    # Beauty (Sephora, Ulta): ingredients, how_to_use, highlights
    # Fashion (ASOS, H&M, Nike): size_and_fit, care_info, material, colour, colorway
    # Home (Wayfair): dimensions, weight, materials, assembly_required
    # Tech (Best Buy, Newegg): model_number, upc, whats_included, warranty
    # Costco: member_only (True when price is membership-gated)
}
```

See `samples/<retailer>/product.json` for a real example from each retailer.

---

## CAPTCHA Handling

When a retailer serves a bot challenge page, the fetcher raises `CaptchaBlocked`. By default it also:

1. Clears cookies (Akamai sets session-flagging cookies that persist across UA rotation)
2. Rotates the User-Agent to a different browser string
3. Clears the browser context for that domain (if using the `browser` strategy)
4. Sleeps 30–90 minutes before the next attempt

```python
from scraper import CaptchaBlocked, PoliteFetcher, scrape_product

with PoliteFetcher() as fetcher:
    try:
        product = scrape_product(url, fetcher=fetcher)
    except CaptchaBlocked as e:
        print(f"Blocked: status={e.status}, marker={e.marker!r}")
        # If ENABLE_CAPTCHA_BACKOFF=1 (default), backoff already happened.
        # If ENABLE_CAPTCHA_BACKOFF=0, handle retry / proxy rotation yourself.
```

To disable the automatic sleep (e.g. when you manage your own proxy rotation):

```bash
SCRAPER_ENABLE_CAPTCHA_BACKOFF=0 python my_script.py
```

---

## Discovery

`discover_amazon` is currently the only built-in discovery module. It returns a plain Python list. Per-retailer listing crawlers and a Google Shopping discovery module are planned.

```python
from scraper import discover_amazon

# Works with Amazon search pages, category pages, and other listing URLs.
# Returns a list[str] of canonical product URLs (https://www.amazon.com/dp/<ASIN>).
urls = discover_amazon(
    "https://www.amazon.com/s?k=noise+cancelling+headphones&rh=n:172282",
    max_products=200,
)

# The list is just Python. Filter, slice, save to a file, pass to a queue, anything.
urls_under_50 = [u for u in urls]  # filter by price after scraping, etc.
```

See **Discover product URLs, then extract them** in the Usage section for the full pipeline.

---

## Adding a New Retailer

There are three cases depending on what you need.

---

### Extract only (parse product pages)

This is the common case. You have product page URLs and want structured data from them.

**1. Create `scraper/parse_<retailer>.py`** with this interface:

```python
DOMAINS = ["example.com"]          # domain substrings this parser handles
ID_FIELD = "product_id"            # field name for the product ID in the output dict
BOT_VENDOR = "Akamai"              # informational
CAPTCHA_MARKERS = [                # strings that appear ONLY on the block/challenge page
    "example-challenge-marker",
]

def extract_id(url: str) -> str | None:
    """Parse the product ID from a product URL."""
    import re
    m = re.search(r"/product/(\d+)", url)
    return m.group(1) if m else None

def parse_product(html: str, source_url: str) -> dict:
    """Extract structured fields from the product page HTML."""
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    out = {}
    # ... extract fields, return dict
    return out
```

**2. Register in `scraper/parse_dispatch.py`** : add a `Retailer(...)` entry to the `REGISTRY` tuple.

**3. Register in `scraper/captcha.py`** : import the new module and add its domains to `_DOMAIN_MARKERS`.

**4. Add to `scraper/config.py`'s `FETCH_STRATEGY`** with `"httpx"`, `"curl_cffi"`, or `"browser"`.

After these four steps, `scrape_product("https://example.com/product/123")` auto-routes to your parser.

---

### Discover only (crawl listing pages for product URLs)

Use this when you want to build a URL list from category/search pages, without necessarily scraping product pages.

**1. Create `scraper/discover_<retailer>.py`** with at minimum:

```python
def parse_listing(html: str, base_url: str) -> tuple[list[str], str | None]:
    """Return (list_of_product_urls, next_page_url_or_None)."""
    from selectolax.parser import HTMLParser
    tree = HTMLParser(html)
    urls = []
    # extract product links...
    next_url = None
    # extract pagination next link...
    return urls, next_url
```

**2. Add a `discover_<retailer>()` function to `scraper/api.py`** following the same pattern as `discover_amazon()`:

```python
def discover_walmart(start_url, fetcher=None, max_products=None):
    from .discover_walmart import parse_listing
    # same loop as discover_amazon, calling parse_listing
    ...
```

**3. Export from `scraper/__init__.py`** if you want it in the top-level API.

---

### Discover + Extract (full pipeline for a new retailer)

Do both of the above. Then the full flow works end-to-end:

```python
urls = discover_walmart("https://www.walmart.com/browse/electronics", max_products=100)
with PoliteFetcher() as fetcher:
    for url in urls:
        product = scrape_product(url, fetcher=fetcher)
```

---

## Installation

**Minimum (Amazon + Walmart only):**
```bash
pip install httpx[http2] selectolax lxml
```

**For Akamai-fronted retailers** (Best Buy, ASOS, Target, Sephora, Ulta, Newegg, Urban Outfitters, Nike):
```bash
pip install -e ".[curl]"
# or: pip install httpx[http2] selectolax lxml curl-cffi
```

**For JS-challenge retailers** (Wayfair, Nordstrom Rack, H&M, Overstock, Dick's, Costco):
```bash
pip install -e ".[all]"
playwright install chromium
```

**All at once:**
```bash
pip install -r requirements.txt
playwright install chromium
```

---

## Limitations

- **H&M, Dick's Sporting Goods, Costco** : parsers are fully implemented and tested. However, Akamai's `_abck` cookie validation hard-blocks headless Chromium on these three sites, even with playwright-stealth. **A residential proxy is required** for live fetching. Fixtures for these are provided in `samples/` for offline use.

- **Wayfair** : PerimeterX "Press & Hold" challenge is partially solved (the hold interaction fires), but PX's behavioral sensor currently rejects headless Chrome. **A residential proxy significantly improves success rate.**

- **Nordstrom Rack** : Akamai JS PoW clears automatically with the browser strategy. However, the site aggressively rate-limits repeated fetches from the same IP and redirects to `siteclosed.nordstromrack.com`. Production delays (20–45s between requests) are required.

- **Costco** : Price is membership-gated. The parser returns all available fields with `member_only: true`; `price` is absent until a signed-in session is wired in.

- **Overstock** : Requires a residential proxy (Akamai blocks headless Chrome). The parser handles both URL formats — `/Home-Garden/<slug>/<id>/product.html` and `/products/<slug>-<id>`. No listing page discovery module yet.

- **Target** : Product price is not in the page HTML; it's fetched from a secondary XHR endpoint (the `redsky` pricing API). `scrape_product()` handles this automatically. Calling `parse_target.parse_product(html, url)` directly will return no price unless you also fetch `parse_target.price_api_url(tcin)` and pass the result as `price_data=`.

- **No JavaScript rendering for `httpx` / `curl_cffi` strategies** : dynamically loaded content (prices, variant options, reviews) that only appears after JS execution won't be captured. Use the `browser` strategy if the target site requires JS.

- **`curl_cffi` impersonates Safari 17** : if a site updates its bot detection to flag this specific fingerprint, switching to a different `CURL_IMPERSONATE` value in `config.py` may help.

- **No proxy rotation built in** : if a site blocks your IP, manage proxy infrastructure externally (e.g. pass a proxy to `httpx.Client` or `curl_cffi`). The fetcher does not rotate proxies automatically.

- **Discovery modules** : only Amazon is currently supported. Per-retailer listing discovery and Google Shopping discovery are planned but not yet implemented.

- **Rate limits are site-specific and change over time** : Datacenter IPs typically require longer delays or proxy rotation.

---

## Legal Notice

**This repository is provided for educational and research purposes only.**

### Terms of Service
Every retailer covered by this tool prohibits automated scraping in their Terms of Service. Violating a ToS is a breach of contract between you and the site. You are solely responsible for reviewing and complying with the ToS of any site you scrape before using this tool against it.
Additionally, scraping behind a login wall is a materially different legal situation and is not what this tool is designed for.

### Copyright
Product titles, descriptions, images, and other content displayed on retail websites may be protected by copyright held by the retailer or the brand. This tool collects that data; what you do with it is your responsibility. Reproducing copyrighted content at scale for commercial purposes, for example, mirroring a retailer's catalog or reselling product data are not the intended purpose of this repo.

This repo is **not** intended for bulk commercial data reselling, operating services that republish retailer content without a license, or any use that causes measurable harm or disruption to a retailer's infrastructure.

### Disclaimer
The maintainers of this repository are not lawyers. Nothing here is legal advice. If you are building a commercial product that depends on scraped data at scale, consult a lawyer before proceeding. The maintainers accept no liability for how this tool is used.

---

## License

MIT : see [LICENSE](LICENSE).
