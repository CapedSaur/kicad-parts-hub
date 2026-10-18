"""SnapEDA / SnapMagic Search client (search + part metadata).

Endpoints verified on 2026-10-16 against the live site (see README "SnapEDA API"):

  GET  /search/?q=<query>&page=<n>          HTML search page, sets ``csrftoken`` cookie
  POST /api/v1/search_local_internal        JSON results, 20 parts per page
       body   : q, page, search-type=parts, csrfmiddlewaretoken
       headers: X-Requested-With, Referer
  GET  /api/v1/search_autocomplete?q=<q>    JSON array of part numbers
  GET  /api/v1/search_featured?q=<q>        JSON featured/ads payload

The part page (``/parts/<mpn>/<manufacturer>/view-part/``) and the file
downloads are protected by Cloudflare and require cookies from a real logged-in
browser session; see :mod:`snapeda_auth` and :mod:`snapeda_download`.

Only the Python standard library is used so the module also works inside
KiCad's bundled interpreter.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

BASE_URL = "https://www.snapeda.com"
SEARCH_PAGE_SIZE = 20  # fixed by the endpoint

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": DEFAULT_USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
}


class SnapedaError(RuntimeError):
    """Base error for SnapEDA operations."""


class SnapedaCloudflareBlocked(SnapedaError):
    """Cloudflare served its challenge page ("Just a moment...")."""


class SnapedaAuthRequired(SnapedaError):
    """The action needs a logged-in SnapEDA session."""


@dataclass
class SnapedaPart:
    """One search hit, normalized from the JSON payload."""

    part_number: str
    manufacturer: str
    package_type: str = ""
    description: str = ""
    unipart_id: str = ""
    has_symbol: bool = False
    has_footprint: bool = False
    has_3d: bool = False
    has_datasheet: bool = False
    has_sim: bool = False
    datasheet_url: str = ""
    image: str = ""
    symbol_image: str = ""
    footprint_image: str = ""
    model_image: str = ""
    manufacturer_logo: str = ""
    distributors: list[str] = field(default_factory=list)
    cached_formats: list[str] = field(default_factory=list)
    part_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def needs_login(self) -> bool:
        """True when a KiCad download cannot be done anonymously."""
        return bool(self.part_number and self.manufacturer)

    @property
    def cad_summary(self) -> str:
        bits = []
        if self.has_symbol:
            bits.append("Symbol")
        if self.has_footprint:
            bits.append("Footprint")
        if self.has_3d:
            bits.append("3D")
        return " + ".join(bits) if bits else "No CAD"


@dataclass
class SnapedaSearchResult:
    """Normalized search response."""

    query: str
    parts: list[SnapedaPart]
    total_hits: int = 0
    page: int = 1
    query_type: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def page_count(self) -> int:
        return max(1, (self.total_hits + SEARCH_PAGE_SIZE - 1) // SEARCH_PAGE_SIZE)

    @property
    def has_more(self) -> bool:
        return self.page * SEARCH_PAGE_SIZE < self.total_hits


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def part_from_json(item: dict[str, Any], *, query: str = "") -> SnapedaPart:
    """Build a :class:`SnapedaPart` from one ``results`` entry."""
    part_number = _text(item.get("part_number") or item.get("name"))
    manufacturer = _text(item.get("manufacturer"))
    te = item.get("te_param") if isinstance(item.get("te_param"), dict) else {}
    images = te.get("part_images") if isinstance(te.get("part_images"), dict) else {}

    def _img(key: str, fallback: str) -> str:
        return _text(images.get(key)) or _text(item.get(fallback))

    part = SnapedaPart(
        part_number=part_number,
        manufacturer=manufacturer,
        package_type=_text(item.get("package_type")),
        description=_text(item.get("short_description") or item.get("description")),
        unipart_id=_text(item.get("unipart_id")),
        has_symbol=_as_bool(item.get("has_symbol")),
        has_footprint=_as_bool(item.get("has_footprint")),
        has_3d=_as_bool(item.get("has_3d")),
        has_datasheet=_as_bool(item.get("has_datasheet_bool") or item.get("has_datasheet")),
        has_sim=_as_bool(item.get("has_sim")),
        datasheet_url=_text(item.get("datasheeturl")),
        image=_text(item.get("image") or item.get("coverart")),
        symbol_image=_img("symbol", "symbol_image"),
        footprint_image=_img("footprint", "footprint_image"),
        model_image=_img("3dmodel", "3dmodel_image"),
        manufacturer_logo=_text(item.get("organization_image_100_20")),
        distributors=[_text(d) for d in (item.get("distributors") or []) if _text(d)],
        cached_formats=[_text(f) for f in (item.get("cached_formats") or []) if _text(f)],
        raw=item,
    )
    part.part_url = build_part_url(part.part_number, part.manufacturer, query=query)
    return part


def build_part_url(part_number: str, manufacturer: str, *, query: str = "") -> str:
    """Part page URL, matching the pattern used by the website itself."""
    if not part_number or not manufacturer:
        return ""
    mpn = urllib.parse.quote(part_number, safe="")
    mfr = urllib.parse.quote(manufacturer, safe="")
    params = {"ref": "search", "t": query or part_number}
    return f"{BASE_URL}/parts/{mpn}/{mfr}/view-part/?{urllib.parse.urlencode(params)}"


def is_cloudflare_challenge(body: bytes | str) -> bool:
    """True when the response is Cloudflare's interstitial instead of content."""
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return "Just a moment..." in text or "cf-challenge" in text


class SnapedaClient:
    """Cookie-aware SnapEDA client.

    ``cookies`` accepts a mapping or a raw ``Cookie:`` header string, typically
    obtained from :mod:`snapeda_auth`. Without cookies the client still works for
    search, because the public search endpoint only needs a CSRF token.
    """

    def __init__(
        self,
        cookies: dict[str, str] | str | None = None,
        *,
        timeout: float = 25.0,
        user_agent: str = DEFAULT_USER_AGENT,
        retries: int = 3,
    ) -> None:
        self.timeout = timeout
        self.user_agent = user_agent
        self.retries = max(1, retries)
        self._jar = http.cookiejar.CookieJar()
        self._ssl_context = ssl.create_default_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self._jar),
            urllib.request.HTTPSHandler(context=self._ssl_context),
        )
        self._csrf_token = ""
        if cookies:
            self.set_cookies(cookies)

    # ------------------------------------------------------------------
    # cookies
    # ------------------------------------------------------------------

    def set_cookies(self, cookies: dict[str, str] | str) -> None:
        """Load cookies from a mapping or a raw ``Cookie:`` header string."""
        if isinstance(cookies, str):
            cookies = parse_cookie_header(cookies)
        for name, value in cookies.items():
            if not name or value is None:
                continue
            self._jar.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=str(value),
                    port=None,
                    port_specified=False,
                    domain=".snapeda.com",
                    domain_specified=True,
                    domain_initial_dot=True,
                    path="/",
                    path_specified=True,
                    secure=True,
                    expires=None,
                    discard=False,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )

    def cookies(self) -> dict[str, str]:
        return {c.name: c.value for c in self._jar}

    def has_cookie(self, name: str) -> bool:
        return name in self.cookies()

    # ------------------------------------------------------------------
    # transport
    # ------------------------------------------------------------------

    def _open(
        self,
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        referer: str = "",
    ) -> tuple[int, bytes, dict[str, str]]:
        merged = dict(_HEADERS)
        merged["User-Agent"] = self.user_agent
        if referer:
            merged["Referer"] = referer
        if headers:
            merged.update(headers)

        body = urllib.parse.urlencode(data).encode() if data is not None else None
        last_error: Exception | None = None

        for attempt in range(1, self.retries + 1):
            request = urllib.request.Request(url, data=body, headers=merged)  # noqa: S310
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    payload = response.read()
                    return response.status, payload, dict(response.headers)
            except urllib.error.HTTPError as exc:
                payload = exc.read()
                if exc.code == 403 and is_cloudflare_challenge(payload):
                    last_error = SnapedaCloudflareBlocked(
                        "SnapEDA responded with a Cloudflare challenge "
                        "(no valid cf_clearance cookie for this IP/browser)"
                    )
                    logger.warning("Cloudflare challenge on %s (attempt %s)", url, attempt)
                    if attempt < self.retries:
                        time.sleep(1.5 * attempt)
                    continue
                # 4xx/5xx that carry a usable body (Django error JSON, etc.)
                return exc.code, payload, dict(exc.headers)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = SnapedaError(f"Request to {url} failed: {exc}")
                logger.warning("Request failed on %s (attempt %s): %s", url, attempt, exc)
                if attempt < self.retries:
                    time.sleep(1.0 * attempt)

        raise last_error or SnapedaError(f"Request to {url} failed")

    # ------------------------------------------------------------------
    # session bootstrap
    # ------------------------------------------------------------------

    @property
    def csrf_token(self) -> str:
        return self._csrf_token or self.cookies().get("csrftoken", "")

    def ensure_csrf_token(self, query: str = "kicad", *, force: bool = False) -> str:
        """Make sure a CSRF token is available.

        The token is only handed out together with the search *page*, so this
        fetches ``/search/`` once (also the moment Cloudflare decides whether the
        session carries a valid ``cf_clearance`` cookie).
        """
        if self.csrf_token and not force:
            return self.csrf_token

        url = f"{BASE_URL}/search/?{urllib.parse.urlencode({'q': query})}"
        status, payload, _ = self._open(url)
        token = self.cookies().get("csrftoken", "")
        if not token:
            match = re.search(rb'name="csrfmiddlewaretoken"\s+value="([^"]+)"', payload)
            if match:
                token = match.group(1).decode()
        if not token:
            raise SnapedaError(
                f"Could not obtain a SnapEDA CSRF token (HTTP {status}); "
                "the search page did not return one"
            )
        self._csrf_token = token
        return token

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def autocomplete(self, query: str) -> list[str]:
        """Part-number suggestions (no login required)."""
        query = query.strip()
        if not query:
            return []
        url = f"{BASE_URL}/api/v1/search_autocomplete?{urllib.parse.urlencode({'q': query})}"
        status, payload, _ = self._open(url)
        if status != 200:
            raise SnapedaError(f"Autocomplete failed with HTTP {status}")
        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:  # pragma: no cover - defensive
            raise SnapedaError(f"Autocomplete returned invalid JSON: {exc}") from exc
        if isinstance(data, dict):  # tolerate {"results": [...]}
            data = data.get("results") or data.get("suggestions") or []
        return [_text(item) for item in data if _text(item)]

    def search(self, query: str, page: int = 1) -> SnapedaSearchResult:
        """Search parts. Raises :class:`SnapedaCloudflareBlocked` when blocked."""
        query = query.strip()
        if not query:
            raise ValueError("Search query must not be empty")
        token = self.ensure_csrf_token(query)

        url = f"{BASE_URL}/api/v1/search_local_internal"
        referer = f"{BASE_URL}/search/?{urllib.parse.urlencode({'q': query, 'page': page})}"
        status, payload, headers = self._open(
            url,
            data={
                "q": query,
                "page": str(page),
                "search-type": "parts",
                "csrfmiddlewaretoken": token,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "X-Requested-With": "XMLHttpRequest",
                "Accept": "application/json, text/javascript, */*; q=0.01",
            },
            referer=referer,
        )
        content_type = headers.get("Content-Type", "")
        if status == 403 and "json" not in content_type:
            # Django rejected the CSRF token: refresh once and retry.
            logger.info("SnapEDA search rejected the CSRF token, refreshing it")
            self.ensure_csrf_token(query, force=True)
            status, payload, headers = self._open(
                url,
                data={
                    "q": query,
                    "page": str(page),
                    "search-type": "parts",
                    "csrfmiddlewaretoken": self.csrf_token,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-Requested-With": "XMLHttpRequest",
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                },
                referer=referer,
            )

        if is_cloudflare_challenge(payload):
            raise SnapedaCloudflareBlocked("SnapEDA search was answered by Cloudflare")
        if status != 200:
            raise SnapedaError(f"SnapEDA search failed with HTTP {status}")

        try:
            data = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise SnapedaError(f"SnapEDA search returned invalid JSON: {exc}") from exc

        items = data.get("results") or []
        parts = [part_from_json(item, query=query) for item in items if isinstance(item, dict)]
        return SnapedaSearchResult(
            query=query,
            parts=parts,
            total_hits=int(data.get("hits") or len(parts)),
            page=int(data.get("page") or page),
            query_type=_text(data.get("query_type")),
            raw=data,
        )

    def fetch(self, url: str, *, referer: str = "") -> tuple[int, bytes, dict[str, str]]:
        """Raw GET for part pages / download endpoints (used by snapeda_download)."""
        if url.startswith("/"):
            url = BASE_URL + url
        return self._open(url, referer=referer)

    def session_state(self) -> dict[str, Any]:
        """Report whether the current cookies look like a logged-in session."""
        state: dict[str, Any] = {
            "logged_in": False,
            "has_cf_clearance": self.has_cookie("cf_clearance"),
            "has_sessionid": self.has_cookie("sessionid"),
            "user_id": "",
        }
        try:
            status, payload, _ = self._open(f"{BASE_URL}/search/?q=kicad")
        except SnapedaError as exc:
            state["error"] = str(exc)
            return state

        html = payload.decode("utf-8", "replace")
        state["http_status"] = status
        if is_cloudflare_challenge(html):
            state["error"] = "Cloudflare challenge (cf_clearance cookie missing or expired)"
            return state

        match = re.search(r"'userid'\s*:\s*'(\d+)'", html)
        if match:
            state["user_id"] = match.group(1)
        state["logged_in"] = bool(match) or "/accounts/logout" in html
        return state


def parse_cookie_header(raw: str) -> dict[str, str]:
    """Parse ``a=1; b=2`` (DevTools "Cookie" header or document.cookie)."""
    cookies: dict[str, str] = {}
    for chunk in raw.replace("\n", ";").split(";"):
        if "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            cookies[name] = value.strip()
    return cookies


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO)
    client = SnapedaClient()
    result = client.search("STM32F103")
    print(f"{result.query!r}: {result.total_hits} hits, page {result.page}/{result.page_count}")
    for part in result.parts[:5]:
        print(
            f"  {part.part_number:<22} {part.manufacturer:<18} {part.package_type:<10} {part.cad_summary}"
        )
