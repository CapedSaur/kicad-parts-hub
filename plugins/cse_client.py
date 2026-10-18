"""Component Search Engine (Samacsys) search client.

Verified against the live site on 2026-10-16:

  GET  /search?term=<query>[&page=<n>]   server-rendered results, 25 per page
  GET  /part-view/<MPN>/<manufacturer>   part page, carries the Download button

The whole site sits behind Cloudflare. Anonymous requests are answered with the
"Just a moment..." challenge (HTTP 403); the decisive cookie is ``__cf_bm``
(IP + user agent bound, refreshed by a real browser), so searching works with
the cookies of the plugin browser - the same mechanism SnapEDA uses. Downloading
a model needs a (free) CSE account, which the plugin browser keeps logged in.

Results are plain HTML: every row is ``<tr id='row-N' data-uid="...">`` with the
part link, manufacturer, description, package, datasheet count and the model
tooltips ("3D model available" / "Symbol and footprint available").
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

BASE_URL = "https://componentsearchengine.com"
SEARCH_PAGE_SIZE = 25  # fixed by the site

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36"
)

_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}

# The server sends id='row-N' (single quotes), a browser's DOM serialization uses
# double quotes: both have to parse.
_ROW_RE = re.compile(r"<tr\b[^>]*\bid=[\"']row-\d+[\"'][^>]*>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<td[^>]*>(.*?)(?=<td|</tr>|$)", re.S)
_UID_RE = re.compile(r'data-uid="(\d+)"')
_PART_LINK_RE = re.compile(r'href="/part-view/([^"/]+)/([^"]*)"')
_MANUFACTURER_RE = re.compile(r'<span class="grey-text">([^<]*)</span>')
_PACKAGE_RE = re.compile(r'<span class="td-package-category">([^<]*)</span>')
_TOOLTIP_RE = re.compile(r'<div class="small-tooltip[^"]*">([^<]*)</div>')
_COUNT_RE = re.compile(
    r"Showing\s*<strong>\s*(\d+)\s*</strong>\s*of\s*<strong>\s*([\d,]+)\s*results?"
)
_IMAGES_RE = re.compile(r'data-images="(\[[^"]*\])"')
_MODELS_RE = re.compile(r"<span class=[\"']hide[\"']>(\d+)</span>")
_TAG_RE = re.compile(r"<[^>]+>")


class CseError(RuntimeError):
    """Base error for Component Search Engine operations."""


class CseBlocked(CseError):
    """Cloudflare served its challenge page ("Just a moment...") instead of HTML."""


class CseAuthRequired(CseError):
    """The action needs a logged-in CSE session."""


def is_cloudflare_challenge(text: str | bytes) -> bool:
    """True when the payload is the Cloudflare interstitial rather than the site."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    return "Just a moment" in text or "cf-challenge" in text or "chl_page" in text


@dataclass
class CsePart:
    """One search hit, normalized from the results table."""

    part_number: str
    manufacturer: str
    uid: str = ""
    description: str = ""
    package_type: str = ""
    datasheet_count: int = 0
    has_symbol: bool = False
    has_footprint: bool = False
    has_3d: bool = False
    model_count: int = 0
    image: str = ""
    images: list[str] = field(default_factory=list)

    @property
    def part_url(self) -> str:
        """The part page, where the Download button lives."""
        quoted = urllib.parse.quote(self.part_number, safe="")
        manufacturer = urllib.parse.quote(self.manufacturer, safe="")
        return f"{BASE_URL}/part-view/{quoted}/{manufacturer}"

    @property
    def availability(self) -> str:
        """Short human summary of what can be downloaded for this part."""
        parts = []
        if self.has_symbol or self.has_footprint:
            parts.append("symbol+footprint")
        if self.has_3d:
            parts.append("3D")
        return " + ".join(parts) or "no model announced"


@dataclass
class CseSearchResult:
    """One page of search results."""

    parts: list[CsePart] = field(default_factory=list)
    total: int = 0
    page: int = 1

    @property
    def hits(self) -> int:
        return self.total


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html_module.unescape(_TAG_RE.sub(" ", text))).strip()


def _announces(tooltips: list[str], what: str) -> bool:
    """True when a tooltip says ``what`` is available (and never the 'not available' one)."""
    return any(
        tip.startswith(what) and "available" in tip and "not available" not in tip
        for tip in tooltips
    )


def _row_part(row: str) -> CsePart | None:
    """Build one :class:`CsePart` from a result row, or None when it has no part link."""
    link = _PART_LINK_RE.search(row)
    if not link:
        return None
    cells = _CELL_RE.findall(row)
    manufacturer = _MANUFACTURER_RE.search(row)
    package = _PACKAGE_RE.search(row)
    tips = [tip.lower() for tip in _TOOLTIP_RE.findall(row)]
    ecad = _announces(tips, "symbol and footprint")
    three = _announces(tips, "3d model")

    images: list[str] = []
    images_match = _IMAGES_RE.search(row)
    if images_match:
        try:
            raw = json.loads(html_module.unescape(images_match.group(1)))
        except json.JSONDecodeError:
            raw = []
        images = [str(url) for url in raw if url]

    datasheets = 0
    if len(cells) > 2:
        digits = re.search(r"\d+", _clean(cells[2]))
        datasheets = int(digits.group(0)) if digits else 0
    models_match = _MODELS_RE.search(row)

    return CsePart(
        part_number=html_module.unescape(link.group(1)),
        manufacturer=html_module.unescape(
            (manufacturer.group(1) if manufacturer else link.group(2)) or ""
        ),
        uid=(_UID_RE.search(row).group(1) if _UID_RE.search(row) else ""),
        description=_clean(cells[3])[:400] if len(cells) > 3 else "",
        package_type=html_module.unescape(package.group(1)) if package else "",
        datasheet_count=datasheets,
        has_symbol=ecad,
        has_footprint=ecad,
        has_3d=three,
        model_count=int(models_match.group(1)) if models_match else 0,
        image=images[0] if images else "",
        images=images,
    )


def parts_from_search_html(html: str) -> tuple[list[CsePart], int]:
    """Return (parts, total results) parsed from a CSE search page."""
    parts: list[CsePart] = []
    for row in _ROW_RE.findall(html):
        part = _row_part(row)
        if part is not None:
            parts.append(part)
    total = 0
    count = _COUNT_RE.search(html)
    if count:
        total = int(count.group(2).replace(",", ""))
    elif parts:
        total = len(parts)
    return parts, total


def is_logged_in(html: str) -> bool:
    """True when a CSE page was rendered for a signed-in user."""
    return "/signout" in html or "Sign Out" in html


class CseClient:
    """Cookie-carrying HTTP client for componentsearchengine.com."""

    def __init__(self, cookies: dict[str, str] | None = None, user_agent: str = "") -> None:
        self._cookies: dict[str, str] = dict(cookies or {})
        # Cloudflare binds __cf_bm to the user agent, so the plugin sends the one
        # of the browser the cookies came from.
        self._user_agent = user_agent.strip() or DEFAULT_USER_AGENT

    # -- session -------------------------------------------------------

    def cookies(self) -> dict[str, str]:
        return dict(self._cookies)

    def set_cookies(self, cookies: dict[str, str] | None) -> None:
        self._cookies = dict(cookies or {})

    def user_agent(self) -> str:
        return self._user_agent

    def set_user_agent(self, user_agent: str) -> None:
        self._user_agent = (user_agent or "").strip() or DEFAULT_USER_AGENT

    def session_state(self) -> dict[str, object]:
        """What the client can tell about the session it holds."""
        return {
            "cookies": len(self._cookies),
            "clearance": "__cf_bm" in self._cookies,
            "user_agent": self._user_agent,
        }

    # -- transport -----------------------------------------------------

    def fetch(
        self, url: str, referer: str | None = None, *, timeout: float = 30.0
    ) -> tuple[int, bytes, dict[str, str]]:
        """GET ``url`` with the session cookies; raises :class:`CseError` on network trouble."""
        target = url if url.startswith("http") else f"{BASE_URL}{url}"
        headers = dict(_HEADERS)
        headers["User-Agent"] = self._user_agent
        if self._cookies:
            headers["Cookie"] = "; ".join(
                f"{name}={value}" for name, value in self._cookies.items()
            )
        if referer:
            headers["Referer"] = referer
        request = urllib.request.Request(target, headers=headers, method="GET")
        context = ssl.create_default_context()
        try:
            with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
                return response.status, response.read(), dict(response.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise CseError(f"Request to {target} failed: {exc}") from exc

    # -- search --------------------------------------------------------

    def search(self, term: str, page: int = 1) -> CseSearchResult:
        """Search by part number or keyword; raises :class:`CseBlocked` on Cloudflare."""
        parameters = {"term": term}
        if page > 1:
            parameters["page"] = str(page)
        query = urllib.parse.urlencode(parameters)
        status, payload, _headers = self.fetch(f"/search?{query}", referer=f"{BASE_URL}/")
        text = payload.decode("utf-8", "replace")
        if is_cloudflare_challenge(text):
            raise CseBlocked(
                "Component Search Engine is asking for a Cloudflare challenge. Import the "
                "cookies of the plugin browser (button 'Session / Login')."
            )
        if status != 200:
            raise CseError(f"Search for {term!r} returned HTTP {status}")
        parts, total = parts_from_search_html(text)
        return CseSearchResult(parts=parts, total=total, page=page)

    # -- part page -----------------------------------------------------

    def part_page(self, part: CsePart) -> str:
        """HTML of a part page, raising :class:`CseBlocked` when Cloudflare interferes."""
        status, payload, _headers = self.fetch(part.part_url, referer=f"{BASE_URL}/search")
        text = payload.decode("utf-8", "replace")
        if is_cloudflare_challenge(text):
            raise CseBlocked(f"Cloudflare blocked the page of {part.part_number}")
        if status != 200:
            raise CseError(f"Part page of {part.part_number} returned HTTP {status}")
        return text

    def account_state(self) -> str:
        """``signed in`` / ``anonymous`` / ``unknown`` for the current cookies."""
        try:
            status, payload, _headers = self.fetch("/", referer=f"{BASE_URL}/")
        except CseError:
            return "unknown"
        text = payload.decode("utf-8", "replace")
        if status != 200 or is_cloudflare_challenge(text):
            return "unknown"
        return "signed in" if is_logged_in(text) else "anonymous"


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        from .snapeda_client import parse_cookie_header
    except ImportError:
        from snapeda_client import parse_cookie_header  # type: ignore[no-redef]

    try:
        from .cse_auth import import_cookies_from_browsers, load_session
    except ImportError:
        from cse_auth import import_cookies_from_browsers, load_session  # type: ignore[no-redef]

    cookies, source = load_session()
    if not cookies:
        imported = import_cookies_from_browsers()
        cookies, source = imported.cookies, imported.source
    if len(sys.argv) > 2 and sys.argv[1] == "--cookie":
        cookies = parse_cookie_header(sys.argv[2])
    client = CseClient(cookies)
    print("Session source:", source or "anonymous")
    print("State:", client.session_state(), client.account_state())
    query = sys.argv[-1] if len(sys.argv) > 1 else "STM32F103"
    found = client.search(query)
    print(f"{found.total} results, showing {len(found.parts)}")
    for part in found.parts[:5]:
        print(f"  {part.part_number:<24} {part.manufacturer:<20} {part.availability}")
