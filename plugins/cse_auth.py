"""Component Search Engine session helper.

Same three routes as SnapEDA (see :mod:`snapeda_auth`), for this site:

1. the plugin's own browser window (CDP) - works with any Chrome/Edge version,
2. the cookie files of Firefox / older Chrome/Edge,
3. a pasted ``Cookie:`` header.

What the site needs from the cookies is ``__cf_bm``: verified on 2026-10-16 that
a request carrying only that cookie gets HTTP 200 while a request with every
other cookie gets the Cloudflare challenge (403). Downloading a model needs a
free CSE account on top, which the plugin browser keeps logged in.
"""

from __future__ import annotations

from pathlib import Path

try:
    from . import snapeda_auth
except ImportError:  # standalone / sys.path import (tests, CLI)
    import snapeda_auth  # type: ignore[import-not-found,no-redef]

CSE_DOMAIN = "componentsearchengine.com"
SESSION_FILE_NAME = "cse_session.json"
NEEDED_COOKIES = ("__cf_bm",)
PREFERRED_COOKIE = "__cf_bm"


def import_cookies_from_browsers() -> snapeda_auth.CookieImportResult:
    """Read CSE cookies out of every installed browser (best set wins)."""
    return snapeda_auth.import_cookies_from_browsers(
        domain=CSE_DOMAIN, needed=NEEDED_COOKIES, preferred=PREFERRED_COOKIE
    )


def session_file_path(base_dir: Path | None = None) -> Path:
    return snapeda_auth.session_file_path(base_dir, SESSION_FILE_NAME)


def save_session(cookies: dict[str, str], source: str = "", base_dir: Path | None = None) -> Path:
    return snapeda_auth.save_session(cookies, source, base_dir, SESSION_FILE_NAME)


def load_session(base_dir: Path | None = None) -> tuple[dict[str, str], str]:
    return snapeda_auth.load_session(base_dir, SESSION_FILE_NAME)


def clear_session(base_dir: Path | None = None) -> None:
    snapeda_auth.clear_session(base_dir, SESSION_FILE_NAME)


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Re-exported so the session dialog needs one import for either site."""
    try:
        from .snapeda_client import parse_cookie_header as parse
    except ImportError:
        from snapeda_client import parse_cookie_header as parse  # type: ignore[import-not-found,no-redef]
    return parse(raw)
