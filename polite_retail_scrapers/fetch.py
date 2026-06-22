"""Polite HTTP fetcher: jittered delays, UA rotation, CAPTCHA detection."""
from __future__ import annotations

import concurrent.futures
import logging
import random
import time
from dataclasses import dataclass
from typing import Optional

import httpx

from . import captcha, config

log = logging.getLogger(__name__)


class CaptchaBlocked(Exception):
    """Raised when a retailer serves a robot-check / CAPTCHA / 503 page."""

    def __init__(self, status: int, marker: str = "") -> None:
        self.status = status
        self.marker = marker
        super().__init__(f"blocked status={status} marker={marker!r}")


class ProductNotFound(Exception):
    """Raised on HTTP 404 — product no longer exists, skip without backoff."""


class BrowserFetchRequired(Exception):
    """Raised for a domain whose pages need a JS-capable headless browser
    (e.g. PerimeterX "Press & Hold" or Akamai JS proof-of-work) that this
    fetcher doesn't implement. The caller should defer the URL, not retry it."""


@dataclass
class FetchResult:
    url: str
    status: int
    html: str
    final_url: str


# Forward-declare so type hints work without a circular import.
_BrowserFetcher = None  # filled lazily on first browser fetch


def _looks_like_captcha(text: str, url: str) -> Optional[str]:
    """Return the first CAPTCHA marker found for the URL's domain, or None."""
    snippet = text[:8000] if len(text) > 8000 else text
    for marker in captcha.markers_for(url):
        if marker in snippet:
            return marker
    return None


class PoliteFetcher:
    """Single-IP HTTP client with jittered delays and UA-per-session rotation."""

    def __init__(self) -> None:
        self.client = httpx.Client(
            http2=True,
            follow_redirects=True,
            timeout=config.REQUEST_TIMEOUT,
        )
        self._requests_since_ua_rotate = 0
        self._ua_session_budget = random.randint(*config.UA_ROTATE_EVERY)
        self._current_ua = random.choice(config.USER_AGENTS)
        self._total_requests = 0
        self._last_url: Optional[str] = None
        self._pre_sleep_callback = None  # called with (backoff_seconds,) before sleeping
        self._browser_fetcher = None    # BrowserFetcher; lazily started on first browser-strategy URL

    def close(self) -> None:
        self.client.close()
        if self._browser_fetcher is not None:
            self._browser_fetcher.close()

    def __enter__(self) -> "PoliteFetcher":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- curl_cffi (TLS impersonation) ----------------------------------

    def _get_curl_cffi(self, url: str) -> tuple[int, str, str]:
        """Fetch via curl_cffi with a browser TLS fingerprint.

        Some retailers (Akamai-fronted Best Buy / ASOS) reject plain httpx at the
        TLS layer but admit a real-browser fingerprint. Returns (status, text,
        final_url). The impersonation profile sets its own UA/headers, so we add
        only a language hint and a referer.
        """
        from curl_cffi import requests as cffi

        headers = {"Accept-Language": "en-US,en;q=0.9"}
        if self._last_url:
            headers["Referer"] = self._last_url
        resp = cffi.get(
            url,
            headers=headers,
            impersonate=config.CURL_IMPERSONATE,
            timeout=config.TOTAL_REQUEST_TIMEOUT,
            allow_redirects=True,
        )
        return resp.status_code, resp.text, str(resp.url)

    # ---- Playwright browser ---------------------------------------------

    def _get_browser(self, url: str) -> tuple[int, str, str]:
        """Fetch via headless Playwright Chromium (JS-challenge sites).

        Lazily starts the browser on first call so the heavy Playwright init
        doesn't happen for runs that only touch httpx/curl_cffi sites.
        Returns (status, html, final_url).
        """
        if self._browser_fetcher is None:
            from .browser_fetch import BrowserFetcher
            self._browser_fetcher = BrowserFetcher()
        return self._browser_fetcher.fetch(url)

    # ---- Delay -----------------------------------------------------------

    def _delay_for(self, kind: str) -> float:
        if kind == "listing":
            base = random.uniform(*config.LISTING_DELAY)
        else:
            base = random.uniform(*config.PRODUCT_DELAY)
        noise = random.uniform(*config.EXTRA_NOISE)
        total = base + noise
        if config.ENABLE_LONG_PAUSES and random.randint(1, config.LONG_PAUSE_EVERY) == 1:
            extra = random.uniform(*config.LONG_PAUSE_RANGE)
            log.info("long pause: %.1fs (humans step away sometimes)", extra)
            total += extra
        return total

    def _sleep(self, kind: str) -> None:
        if not config.ENABLE_DELAYS:
            return
        d = self._delay_for(kind)
        log.debug("sleeping %.2fs before %s fetch", d, kind)
        time.sleep(d)

    # ---- UA rotation -----------------------------------------------------

    def _maybe_rotate_ua(self) -> None:
        if not config.ENABLE_UA_ROTATION:
            return
        if self._requests_since_ua_rotate >= self._ua_session_budget:
            self._current_ua = random.choice(config.USER_AGENTS)
            self._ua_session_budget = random.randint(*config.UA_ROTATE_EVERY)
            self._requests_since_ua_rotate = 0
            log.debug("rotated UA -> %s (next rotate in %d)",
                      self._current_ua[:40], self._ua_session_budget)

    def _headers(self) -> dict[str, str]:
        h = dict(config.BASE_HEADERS)
        h["User-Agent"] = self._current_ua
        if self._last_url:
            # A realistic referer makes the request look like normal navigation.
            h["Referer"] = self._last_url
            h["Sec-Fetch-Site"] = "same-origin"
        return h

    # ---- Fetch -----------------------------------------------------------

    def _get_with_total_timeout(self, url: str, headers: dict) -> httpx.Response:
        """Run client.get() in a thread; abort + recreate client if it exceeds TOTAL_REQUEST_TIMEOUT."""
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
            future = ex.submit(self.client.get, url, headers=headers)
            try:
                return future.result(timeout=config.TOTAL_REQUEST_TIMEOUT)
            except concurrent.futures.TimeoutError:
                # Close client to unblock the stuck thread, then rebuild for future requests.
                self.client.close()
                self.client = httpx.Client(
                    http2=True,
                    follow_redirects=True,
                    timeout=config.REQUEST_TIMEOUT,
                )
                try:
                    future.result(timeout=5.0)
                except Exception:
                    pass
                raise httpx.ReadTimeout(
                    f"total timeout ({config.TOTAL_REQUEST_TIMEOUT}s) exceeded for {url}"
                )

    def get_text(self, url: str) -> tuple[int, str]:
        """Lightweight secondary GET for the current product (e.g. Target's
        XHR-only price API).

        Uses the same per-domain client as `fetch()` but only a short
        SECONDARY_DELAY (not the full PRODUCT_DELAY) — it's part of extracting an
        already-fetched product, not a new product fetch. Returns
        (status_code, body_text) and does NOT raise on block statuses; the caller
        decides what to do with a non-200.
        """
        strategy = config.fetch_strategy_for(url)
        if strategy == "browser":
            raise BrowserFetchRequired(url)
        if config.ENABLE_DELAYS:
            time.sleep(random.uniform(*config.SECONDARY_DELAY))
        if strategy == "curl_cffi":
            status, text, _ = self._get_curl_cffi(url)
        else:
            resp = self._get_with_total_timeout(url, self._headers())
            status, text = resp.status_code, resp.text
        self._total_requests += 1
        return status, text

    def fetch(self, url: str, kind: str) -> FetchResult:
        """Fetch a URL after the appropriate polite delay.

        Routes to the right client per domain:
          - httpx       — plain polite client (Amazon, Walmart)
          - curl_cffi   — TLS-impersonation client (Best Buy, ASOS, Target, …)
          - browser     — headless Playwright Chromium (Wayfair, Nordstrom Rack, …)

        Raises CaptchaBlocked on robot-check / 503 / 429 / 403,
        ProductNotFound on 404. Other transport errors propagate as-is.
        """
        self._sleep(kind)
        strategy = config.fetch_strategy_for(url)

        if strategy == "browser":
            log.info("GET %s [browser]", url)
            status, text, final_url = self._get_browser(url)
        else:
            self._maybe_rotate_ua()
            log.info("GET %s [%s]", url, strategy)
            if strategy == "curl_cffi":
                status, text, final_url = self._get_curl_cffi(url)
            else:
                resp = self._get_with_total_timeout(url, self._headers())
                status, text, final_url = resp.status_code, resp.text, str(resp.url)
            self._requests_since_ua_rotate += 1

        self._total_requests += 1

        if status == 404:
            raise ProductNotFound(url)

        # For browser strategy, skip status-code block detection: the initial
        # HTTP response may be a 403 Akamai challenge shell that the browser
        # already cleared via JS — page.content() holds the final DOM, not the
        # original response body. Use CAPTCHA-marker scanning on the HTML instead.
        if strategy != "browser" and status in config.BLOCK_STATUS_CODES:
            raise CaptchaBlocked(status, f"http-{status}")

        # For browser strategy, also detect anti-bot domain redirects: when the
        # site redirects to a different host (e.g. siteclosed.nordstromrack.com,
        # a captcha subdomain, etc.), treat it as a block.
        if strategy == "browser":
            from urllib.parse import urlparse as _up
            req_host = _up(url).netloc.lower()
            fin_host = _up(final_url).netloc.lower()
            if fin_host and fin_host != req_host:
                raise CaptchaBlocked(status, f"domain-redirect:{fin_host}")

        marker = _looks_like_captcha(text, url)
        if marker is not None:
            raise CaptchaBlocked(status, marker)

        self._last_url = final_url
        return FetchResult(url=url, status=status, html=text, final_url=final_url)

    # ---- CAPTCHA backoff -------------------------------------------------

    def backoff_after_block(self, url: Optional[str] = None) -> float:
        """Reset session state after a block and optionally sleep.

        Pass `url` when the block came from a browser-strategy domain so the
        persistent browser context's cookies are also cleared.

        Returns the backoff duration in seconds (0.0 if ENABLE_CAPTCHA_BACKOFF is off).
        """
        # Break the flagged session: drop cookies (Akamai sets bm-sz/bm-sv etc.
        # when it flags a session — they persist across UA rotation and keep
        # the next request flagged), rotate UA, drop the referer.
        self.client.cookies.clear()
        if url is not None and self._browser_fetcher is not None:
            from .browser_fetch import domain_key
            self._browser_fetcher.clear_context(domain_key(url))
        prev_ua = self._current_ua
        choices = [u for u in config.USER_AGENTS if u != prev_ua] or list(config.USER_AGENTS)
        self._current_ua = random.choice(choices)
        self._ua_session_budget = random.randint(*config.UA_ROTATE_EVERY)
        self._requests_since_ua_rotate = 0
        self._last_url = None
        log.warning("cleared cookies, rotated UA to %s ...", self._current_ua[:40])

        d = random.uniform(*config.CAPTCHA_BACKOFF)
        if config.ENABLE_CAPTCHA_BACKOFF:
            log.warning("blocked — backing off for %.0fs (%.1f min)", d, d / 60)
            if self._pre_sleep_callback:
                self._pre_sleep_callback(d)
            time.sleep(d)
            return d
        else:
            log.warning("blocked — CAPTCHA_BACKOFF disabled, skipping sleep")
            return 0.0
