"""Tunables and constants for the scraper."""
from __future__ import annotations

import os

# --- Feature flags -----------------------------------------------------------
# Set env var to "0" to disable the feature; any other value (including unset)
# keeps the default ON. Example: SCRAPER_ENABLE_DELAYS=0 python my_script.py

ENABLE_DELAYS          = os.environ.get("SCRAPER_ENABLE_DELAYS",          "1") != "0"
ENABLE_UA_ROTATION     = os.environ.get("SCRAPER_ENABLE_UA_ROTATION",     "1") != "0"
ENABLE_LONG_PAUSES     = os.environ.get("SCRAPER_ENABLE_LONG_PAUSES",     "1") != "0"
ENABLE_CAPTCHA_BACKOFF = os.environ.get("SCRAPER_ENABLE_CAPTCHA_BACKOFF", "1") != "0"

# --- Politeness ---------------------------------------------------------------

# Delay ranges in seconds. Every request samples uniformly from these — no fixed cadence.
PRODUCT_DELAY = (20.0, 45.0)
LISTING_DELAY = (18.0, 35.0)
EXTRA_NOISE = (0.0, 8.0)
# Short delay before a secondary same-product request (e.g. Target's XHR-only
# price API). Much smaller than PRODUCT_DELAY because it's part of extracting one
# already-fetched product, not a new product fetch.
SECONDARY_DELAY = (1.0, 3.0)

# 1-in-N chance of an extra long "human stepped away" pause.
LONG_PAUSE_EVERY = 25
LONG_PAUSE_RANGE = (90.0, 240.0)

# After a CAPTCHA / block page, sleep this long before retrying.
CAPTCHA_BACKOFF = (1800.0, 5400.0)  # 30–90 min — longer pause helps shake Akamai IP reputation

# Stop retrying a URL after this many consecutive failures.
MAX_ATTEMPTS_PER_URL = 3

REQUEST_TIMEOUT = 30.0        # per-read-chunk timeout (seconds)
TOTAL_REQUEST_TIMEOUT = 60.0  # hard wall-clock limit per HTTP request (seconds)

# Rotate User-Agent every N successful requests (real browsers don't change UA mid-session).
UA_ROTATE_EVERY = (20, 40)

USER_AGENTS = [
    # Recent desktop Chrome / Firefox / Safari strings. Refresh occasionally.
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
]

BASE_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
    "image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
    "Upgrade-Insecure-Requests": "1",
}

# Markers Amazon serves on its anti-bot challenge / block pages.
# Also includes Akamai interstitial markers (HTTP 200, ~2 KB page that contains
# a proof-of-work puzzle; real browsers execute it, plain httpx sees zero results).
CAPTCHA_MARKERS = (
    # Amazon in-house CAPTCHA
    "api-services-support@amazon.com",
    "Robot Check",
    "Enter the characters you see below",
    "automated access to Amazon data",
    "/errors/validateCaptcha",
    # Akamai Bot Manager interstitial
    "triggerInterstitialChallenge",
    "bm-verify=",
    "/_sec/verify",
    "akam-logo",
)

# Statuses that are not the page we wanted.
BLOCK_STATUS_CODES = {403, 429, 503}

# Amazon host we accept. Edit if you want a different locale (.co.uk, .de, etc.).
AMAZON_HOST = "www.amazon.com"
AMAZON_SCHEME = "https"

# --- Per-retailer fetch strategy ---------------------------------------------
# Some retailers block plain httpx at the TLS-fingerprint layer. Route each
# domain to the client that can actually retrieve it:
#   "httpx"     — default polite httpx client (Amazon, Walmart)
#   "curl_cffi" — TLS-impersonation client; needs the curl_cffi package
#   "browser"   — headless Playwright Chromium for JS-challenge sites
#
# Note: H&M, Dick's, Costco, and Wayfair require a residential proxy for
# reliable fetching even with the browser strategy — see README for details.
FETCH_STRATEGY = {
    "amazon.com": "httpx",
    "walmart.com": "httpx",
    "bestbuy.com": "curl_cffi",
    "asos.com": "curl_cffi",
    "target.com": "curl_cffi",
    "sephora.com": "curl_cffi",
    "ulta.com": "curl_cffi",
    "newegg.com": "curl_cffi",
    "urbanoutfitters.com": "curl_cffi",
    "nike.com": "curl_cffi",
    "wayfair.com": "browser",
    "nordstromrack.com": "browser",
    "hm.com": "browser",
    "overstock.com": "browser",
    "dickssportinggoods.com": "browser",
    "costco.com": "browser",
}
DEFAULT_FETCH_STRATEGY = "httpx"
# The curl_cffi impersonation profile that cleared Akamai for Best Buy & ASOS.
CURL_IMPERSONATE = "safari17_0"


def fetch_strategy_for(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).netloc.lower()
    for domain, strat in FETCH_STRATEGY.items():
        if domain in host:
            return strat
    return DEFAULT_FETCH_STRATEGY

# robots.txt compliance — Amazon disallows most product paths. Default off.
RESPECT_ROBOTS_TXT = False
