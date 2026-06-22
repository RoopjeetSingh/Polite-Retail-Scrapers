"""Playwright-based headless browser fetcher for JS-challenge sites.

Used for domains where neither httpx nor curl_cffi can clear the anti-bot
challenge (PerimeterX "Press & Hold", Akamai JS proof-of-work). Playwright
runs a real Chromium engine that executes the challenge JS and mints the
clearance cookie (_abck / _px*) that all subsequent requests need.

Architecture
------------
- One persistent BrowserContext per domain: the expensive clearance is paid
  once, not per product. Cookie reuse is the main performance lever here.
- playwright-stealth 2.x (Stealth class) patches are applied to each context
  so automation signals are masked before the anti-bot JS runs.
- PerimeterX "Press & Hold": synthesised via the Playwright Mouse API after
  detecting the challenge overlay.
- Akamai JS PoW: auto-cleared by running the challenge JS inside real Chromium
  — no additional interaction needed in most cases.

Integration
-----------
Called from PoliteFetcher.fetch() when config.fetch_strategy_for(url) == "browser".
Returns (status_code, html, final_url) — the same tuple shape as _get_curl_cffi()
so the rest of fetch() (CAPTCHA-marker check, ProductNotFound, etc.) is unchanged.
"""
from __future__ import annotations

import logging
import random
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Playwright selector candidates for PerimeterX "Press & Hold" button
_PX_SELECTORS = [
    "#px-captcha",
    ".px-captcha-holder",
    "[id*='px-captcha']",
    "[class*='px-captcha']",
    "div[data-px]",
]

# How long to hold the mouse button down in milliseconds (PX needs ≥3s)
_PX_HOLD_MS = 3_500

# Maximum time to wait for full network quiet after navigation
_NAV_TIMEOUT_MS = 30_000

# Time to wait for network idle after solving a PX challenge
_IDLE_AFTER_CHALLENGE_MS = 15_000

# Strings that appear only on a PerimeterX block page, never real product HTML
_PX_MARKERS = [
    "Press & Hold",
    "px-captcha",
    "_pxAppId",
    "captcha.px-cloud.net",
    "PerimeterX",
    "px-cloud.net",
]

# Chrome-like UA (matches what we tell the browser context to use)
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


def domain_key(url: str) -> str:
    """Return a stable per-domain bucket key (e.g. ``wayfair.com``).

    Used both for persistent-context bucketing and for cookie-clear on backoff.
    Strips common ``www.`` / ``www2.`` / ``m.`` prefixes so all subdomains of
    a retailer share the same context and clearance cookie.
    """
    host = urlparse(url).netloc.lower()
    return re.sub(r"^(?:www\d*\.|m\.)", "", host)


class BrowserFetcher:
    """Long-lived Playwright Chromium instance with one persistent context per domain.

    Create once per crawl run (owned by PoliteFetcher); call close() to release
    browser and OS resources when done.
    """

    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._contexts: dict[str, object] = {}

    # ---- lifecycle ----------------------------------------------------------

    def _ensure_started(self) -> None:
        if self._pw is not None:
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        # Prefer the system Chrome channel — it has the production TLS/HTTP2
        # fingerprint that Akamai and similar vendors trust. Fall back to the
        # Playwright-bundled Chromium if Chrome isn't installed.
        launch_kwargs = dict(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        try:
            self._browser = self._pw.chromium.launch(channel="chrome", **launch_kwargs)
            log.info("browser fetcher: system Chrome started")
        except Exception:
            self._browser = self._pw.chromium.launch(**launch_kwargs)
            log.info("browser fetcher: bundled Chromium started (system Chrome not found)")

    def _make_context(self, domain: str):
        ctx = self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=_UA,
            locale="en-US",
            timezone_id="America/New_York",
            java_script_enabled=True,
        )
        try:
            from playwright_stealth import Stealth

            Stealth().apply_stealth_sync(ctx)
            log.debug("browser fetcher: stealth applied for %s", domain)
        except Exception as e:
            log.warning("browser fetcher: stealth unavailable for %s: %r", domain, e)
        return ctx

    def _get_context(self, domain: str):
        if domain not in self._contexts:
            self._contexts[domain] = self._make_context(domain)
            log.debug("browser fetcher: new persistent context for %s", domain)
        return self._contexts[domain]

    # ---- per-URL helpers ----------------------------------------------------

    def clear_context(self, domain: str) -> None:
        """Clear cookies/storage for a domain.

        Called by PoliteFetcher.backoff_after_block() so the next attempt starts
        with a clean session (same as clearing httpx cookies on CAPTCHA).
        """
        if domain in self._contexts:
            try:
                self._contexts[domain].clear_cookies()
                log.info("browser fetcher: cleared cookies for %s", domain)
            except Exception as e:
                log.warning("browser fetcher: cookie clear failed for %s: %r", domain, e)

    def _try_solve_perimeter_x(self, page) -> bool:
        """Simulate a PerimeterX 'Press & Hold' challenge with human-like movement.

        Waits for the hold-button to become visible (PX challenge JS needs a few
        seconds to render it), then approaches it with natural mouse movement and
        holds for _PX_HOLD_MS with slight drift during the hold.
        Returns True if the interaction fired; False if no button found.
        """
        for selector in _PX_SELECTORS:
            try:
                loc = page.locator(selector)
                if loc.count() == 0:
                    continue
                # PX renders the hold-button asynchronously — wait for it to be
                # actually visible before attempting to get its position.
                loc.first.wait_for(state="visible", timeout=8_000)
                box = loc.first.bounding_box()
                if not box:
                    continue
                cx = box["x"] + box["width"] / 2
                cy = box["y"] + box["height"] / 2
                log.info(
                    "browser: PX press-and-hold at (%.0f, %.0f) selector=%s",
                    cx, cy, selector,
                )

                # Start from a random natural position (not the exact center)
                start_x = random.uniform(100, 900)
                start_y = random.uniform(100, 500)
                page.mouse.move(start_x, start_y)
                page.wait_for_timeout(random.randint(200, 500))

                # Move toward the button in small steps with natural jitter
                steps = 18
                for i in range(1, steps + 1):
                    t = i / steps
                    # Ease-in-out: slow start, fast middle, slow end
                    ease = t * t * (3 - 2 * t)
                    noise = (1 - t) * 3  # jitter decreases as we approach
                    mx = start_x + (cx - start_x) * ease + random.uniform(-noise, noise)
                    my = start_y + (cy - start_y) * ease + random.uniform(-noise, noise)
                    page.mouse.move(mx, my)
                    page.wait_for_timeout(random.randint(15, 45))

                page.mouse.move(cx, cy)
                page.wait_for_timeout(random.randint(80, 200))

                # Press and hold with slight drift during the hold (natural hand tremor)
                page.mouse.down()
                elapsed = 0
                step_ms = 400
                while elapsed < _PX_HOLD_MS:
                    page.wait_for_timeout(step_ms)
                    elapsed += step_ms
                    drift_x = cx + random.uniform(-1.5, 1.5)
                    drift_y = cy + random.uniform(-1.5, 1.5)
                    page.mouse.move(drift_x, drift_y)
                page.mouse.up()
                return True
            except Exception as e:
                log.debug("browser: PX selector %r: %r", selector, e)
        return False

    # ---- main fetch ---------------------------------------------------------

    def fetch(self, url: str) -> tuple[int, str, str]:
        """Navigate to url with headless Chromium, clear any JS challenge, and
        return ``(status_code, html, final_url)``.

        The returned html is the fully-rendered DOM after challenges have been
        resolved. Raises on unrecoverable navigation errors (timeout, crash).
        """
        self._ensure_started()
        domain = domain_key(url)
        ctx = self._get_context(domain)
        page = ctx.new_page()
        try:
            log.info("browser GET %s", url)

            resp = None
            try:
                # "load" waits for the load event (all resources fetched) which is
                # more reliable than "networkidle" (no XHR activity). Both PX sensor
                # POSTs and Akamai PoW XHRs keep "networkidle" from ever triggering.
                resp = page.goto(url, wait_until="load", timeout=_NAV_TIMEOUT_MS)
            except Exception as e:
                err_type = type(e).__name__
                log.warning("browser: goto error for %s [%s] — waiting for load state", url, err_type)
                # Wait a bit for in-flight redirects or challenge JS to settle.
                try:
                    page.wait_for_load_state("load", timeout=15_000)
                except Exception:
                    pass

            status = resp.status if resp else 200

            # Give anti-bot JS a moment to run after load (PX/Akamai sensor scripts
            # execute after the load event). 2 s is enough for the initial render.
            page.wait_for_timeout(2_000)

            def _get_content() -> str:
                try:
                    return page.content()
                except Exception as e:
                    # page.content() can fail if the page is still mid-navigation
                    # (e.g. a JS redirect launched after load). Wait for it to settle.
                    log.warning("browser: page.content() failed (%r) — retrying after load", e)
                    page.wait_for_load_state("load", timeout=15_000)
                    return page.content()

            html = _get_content()
            final_url = page.url

            # PerimeterX "Press & Hold" challenge
            if any(m in html for m in _PX_MARKERS):
                log.info("browser: PerimeterX detected on %s — attempting press-and-hold", url)
                solved = self._try_solve_perimeter_x(page)
                if solved:
                    try:
                        page.wait_for_load_state("load", timeout=_IDLE_AFTER_CHALLENGE_MS)
                    except Exception:
                        pass
                    page.wait_for_timeout(2_000)
                    html = _get_content()
                    final_url = page.url
                    log.info("browser: PX press-and-hold interaction complete for %s", url)
                else:
                    log.warning(
                        "browser: PX challenge not solved for %s — no hold-button found", url
                    )
                # If still blocked, html will contain PX markers → CaptchaBlocked upstream.

            return status, html, final_url

        finally:
            page.close()

    # ---- teardown -----------------------------------------------------------

    def close(self) -> None:
        for ctx in self._contexts.values():
            try:
                ctx.close()
            except Exception:
                pass
        self._contexts.clear()
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
        self._browser = None
        self._pw = None
        log.info("browser fetcher: Chromium stopped")
