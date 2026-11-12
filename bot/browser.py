"""Shared headless Camoufox browser session for keyless scraping and anti-bot fallbacks."""
import atexit
import json
import platform
import time
from contextlib import contextmanager

from .util import breathe, log

_OS_MAP = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}
_BLOCKED_RESOURCES = {"image", "media", "font"}


def _host_os():
    return _OS_MAP.get(platform.system(), "linux")


class _BrowserManager:
    def __init__(self):
        self._cm = None
        self._browser = None
        self._page = None
        self._depth = 0
        self._last_nav = 0.0

    def page(self):
        if self._page is None:
            from camoufox.sync_api import Camoufox

            self._cm = Camoufox(headless=True, os=_host_os(), canvas_noise=False)
            self._browser = self._cm.__enter__()
            self._page = self._browser.new_page()
            self._page.route(
                "**/*",
                lambda r: r.abort() if r.request.resource_type in _BLOCKED_RESOURCES else r.continue_(),
            )
            log("browser: Camoufox session started")
        return self._page

    def goto(self, url, timeout=25000):
        """Navigate with polite pacing and resource cleanup."""
        gap = 1.0 - (time.time() - self._last_nav)
        if gap > 0:
            time.sleep(gap)
        self._last_nav = time.time()
        p = self.page()
        resp = p.goto(url, wait_until="domcontentloaded", timeout=timeout)
        breathe()
        return p, resp

    def fetch_json(self, url, timeout=25000):
        p, resp = self.goto(url, timeout=timeout)
        if resp and resp.status >= 400:
            raise RuntimeError(f"HTTP {resp.status} for {url}")
        text = p.inner_text("body").strip()
        if not text and resp:
            return resp.json()
        return json.loads(text)

    def close(self):
        if self._cm is not None:
            try:
                self._cm.__exit__(None, None, None)
            except Exception:
                pass
            self._cm = None
            self._browser = None
            self._page = None


_mgr = _BrowserManager()
atexit.register(_mgr.close)


@contextmanager
def browser_session():
    """Ensure Camoufox (if lazily started during the block) is closed on exit."""
    _mgr._depth += 1
    try:
        yield _mgr
    finally:
        _mgr._depth -= 1
        if _mgr._depth <= 0:
            _mgr.close()


def browser_goto(url, timeout=25000):
    return _mgr.goto(url, timeout=timeout)


def browser_fetch_json(url, timeout=25000):
    return _mgr.fetch_json(url, timeout=timeout)
