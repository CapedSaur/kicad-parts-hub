"""Download KiCad models for a SnapEDA part and hand the ZIP to the importer.

What is *verified* (2026-10-16):

* The public JSON search endpoint works without a session (:mod:`snapeda_client`).
* The part page and every file download live behind Cloudflare. Anonymous
  clients get a 403 "Just a moment..." interstitial, so downloading only works
  with cookies taken from a real logged-in browser session (:mod:`snapeda_auth`).

What is *not* verified: the exact URL and verb SnapEDA uses for the KiCad
export. Anonymous access to the part page is blocked, so the endpoint is
resolved at runtime from the authenticated page:

1. the discovered links/forms of the part page, preferring KiCad candidates,
2. documented URL patterns as a fallback,
3. and when everything fails the part page is opened in the default browser so
   the user can download the ZIP and drop it into the plugin (the ZIP import
   path of the base plugin is unchanged and always works).

Every attempt is logged, and the part page can be dumped to disk for offline
inspection of new SnapEDA releases.
"""

from __future__ import annotations

import html as html_module
import json
import logging
import re
import urllib.parse
import webbrowser
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

try:  # imported as part of the plugin package
    from .snapeda_client import (
        BASE_URL,
        SnapedaClient,
        SnapedaCloudflareBlocked,
        SnapedaError,
        SnapedaPart,
        is_cloudflare_challenge,
    )
except ImportError:  # standalone / sys.path import (tests, CLI)
    from snapeda_client import (  # type: ignore[import-not-found,no-redef]
        BASE_URL,
        SnapedaClient,
        SnapedaCloudflareBlocked,
        SnapedaError,
        SnapedaPart,
        is_cloudflare_challenge,
    )

logger = logging.getLogger(__name__)

ZIP_MAGIC = b"PK\x03\x04"
DEFAULT_FORMATS = ("kicad",)

# URL fragments that indicate a CAD export on the part page
_DOWNLOAD_HINTS = ("download", "export", "/kicad", "convert")
_URL_PATTERN = re.compile(
    r"""(?:href|action|data-url|data-href|data-download)["']?\s*[=:]\s*["']([^"']+)["']""", re.I
)
_JS_URL_PATTERN = re.compile(r"""["']((?:/|https?://)[^"'<>\s]{4,200})["']""")


@dataclass
class DownloadResult:
    """Outcome of a download attempt."""

    part: SnapedaPart
    zip_path: Path | None = None
    endpoint: str = ""
    messages: list[str] = field(default_factory=list)
    needs_browser: bool = False
    page_dump: Path | None = None

    @property
    def ok(self) -> bool:
        return self.zip_path is not None


class SnapedaDownloader:
    """Resolve and fetch KiCad exports for SnapEDA parts."""

    def __init__(
        self,
        client: SnapedaClient,
        download_dir: Path,
        *,
        dump_pages: bool = True,
    ) -> None:
        self.client = client
        self.download_dir = Path(download_dir)
        self.dump_pages = dump_pages

    # ------------------------------------------------------------------
    # part page
    # ------------------------------------------------------------------

    def debug_dir(self) -> Path:
        path = self.download_dir / "_debug"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def fetch_part_page(self, part: SnapedaPart) -> str:
        """Return the part page HTML, raising when Cloudflare blocks us."""
        if not part.part_url:
            raise SnapedaError(f"Part {part.part_number!r} has no part page URL")
        status, payload, headers = self.client.fetch(part.part_url, referer=f"{BASE_URL}/search/")
        text = payload.decode("utf-8", "replace")
        if status == 403 or is_cloudflare_challenge(text):
            raise SnapedaCloudflareBlocked(
                "SnapEDA is asking for a Cloudflare challenge. Import the cookies of a "
                "browser that is logged into snapeda.com (button 'Session / Login')."
            )
        if status != 200:
            raise SnapedaError(f"Part page {part.part_url} returned HTTP {status}")
        if self.dump_pages:
            dump = self.debug_dir() / f"{_safe_name(part.part_number)}_part.html"
            dump.write_text(text, encoding="utf-8")
            logger.info("Part page dumped to %s", dump)
        return text

    def candidates(self, html: str, part: SnapedaPart) -> list[str]:
        """Download candidates found in the part page, best first."""
        urls: list[str] = []
        for match in _URL_PATTERN.finditer(html):
            urls.append(html_module.unescape(match.group(1)))
        for match in _JS_URL_PATTERN.finditer(html):
            urls.append(html_module.unescape(match.group(1)))

        scored: list[tuple[int, str]] = []
        slug = _safe_name(part.part_number).lower()
        for raw in urls:
            if raw.startswith(("javascript:", "#", "mailto:")):
                continue
            lowered = raw.lower()
            if not any(hint in lowered for hint in _DOWNLOAD_HINTS):
                continue
            if lowered.endswith((".png", ".jpg", ".jpeg", ".svg", ".css", ".js", ".gif")):
                continue
            absolute = urllib.parse.urljoin(BASE_URL + "/", raw)
            if "snapeda.com" not in urllib.parse.urlparse(absolute).netloc:
                continue

            score = 0
            if "kicad" in lowered:
                score += 4
            if part.unipart_id and part.unipart_id in lowered:
                score += 2
            if slug and slug in lowered:
                score += 1
            if "download" in lowered:
                score += 1
            if lowered.endswith(".zip"):
                score += 3
            if score:
                scored.append((score, absolute))

        seen: set[str] = set()
        ordered: list[str] = []
        for _, url in sorted(scored, key=lambda item: -item[0]):
            if url in seen:
                continue
            seen.add(url)
            ordered.append(url)

        # Documented fallback patterns, tried after whatever the page advertises.
        mpn = urllib.parse.quote(part.part_number, safe="")
        mfr = urllib.parse.quote(part.manufacturer, safe="")
        for fmt in DEFAULT_FORMATS:
            for template in (
                f"{BASE_URL}/parts/{mpn}/{mfr}/download/?format={fmt}",
                f"{BASE_URL}/parts/{mpn}/{mfr}/download/?format={fmt}&ref=part_detail",
            ):
                if template not in seen:
                    ordered.append(template)
                    seen.add(template)
        return ordered

    # ------------------------------------------------------------------
    # download
    # ------------------------------------------------------------------

    def download(
        self,
        part: SnapedaPart,
        *,
        formats: tuple[str, ...] = DEFAULT_FORMATS,
        extra_candidates: list[str] | None = None,
    ) -> DownloadResult:
        """Fetch the KiCad export for ``part`` and return the saved ZIP."""
        result = DownloadResult(part=part)
        if not part.part_number:
            result.messages.append("Part without part number: nothing to download")
            return result

        self.download_dir.mkdir(parents=True, exist_ok=True)
        target = self.download_dir / f"{_safe_name(part.part_number)}.zip"

        try:
            page = self.fetch_part_page(part)
        except SnapedaCloudflareBlocked as exc:
            result.messages.append(str(exc))
            result.needs_browser = True
            return result
        except SnapedaError as exc:
            result.messages.append(f"Could not read the part page: {exc}")
            result.needs_browser = True
            return result

        result.page_dump = self.debug_dir() / f"{_safe_name(part.part_number)}_part.html"
        for candidate in (extra_candidates or []) + self.candidates(page, part):
            saved = self._try_url(
                candidate, target, part, formats=formats, messages=result.messages
            )
            if saved is not None:
                result.zip_path = saved
                result.endpoint = candidate
                result.messages.append(f"Downloaded {saved.name} from {candidate}")
                return result

        result.messages.append(
            "No working download endpoint found. SnapEDA may have changed its export URL; "
            f"the part page HTML was saved to {result.page_dump} for inspection."
        )
        result.needs_browser = True
        return result

    def _try_url(
        self,
        url: str,
        target: Path,
        part: SnapedaPart,
        *,
        formats: tuple[str, ...],
        messages: list[str],
    ) -> Path | None:
        """GET ``url`` (+ KiCad format variants) and keep the first ZIP answer."""
        tried: set[str] = set()
        variants = [url]
        if "format=" not in url:
            separator = "&" if "?" in url else "?"
            variants += [f"{url}{separator}format={fmt}" for fmt in formats]

        for variant in variants:
            if variant in tried:
                continue
            tried.add(variant)
            try:
                status, payload, headers = self.client.fetch(variant, referer=part.part_url)
            except SnapedaError as exc:
                logger.debug("Candidate %s failed: %s", variant, exc)
                continue

            if is_cloudflare_challenge(payload):
                messages.append(f"Cloudflare challenge while fetching {variant}")
                continue
            if payload[:4] == ZIP_MAGIC:
                target.write_bytes(payload)
                logger.info("Saved %s (%s bytes) from %s", target, len(payload), variant)
                return target

            content_type = headers.get("Content-Type", "")
            logger.debug(
                "Candidate %s returned HTTP %s (%s, %s bytes)",
                variant,
                status,
                content_type,
                len(payload),
            )
            if status in (401, 403) and "html" in content_type:
                messages.append(f"{variant} needs a logged-in SnapEDA session (HTTP {status}).")
        return None

    # ------------------------------------------------------------------
    # manual fallback
    # ------------------------------------------------------------------

    @staticmethod
    def open_part_in_browser(part: SnapedaPart) -> bool:
        """Open the part page so the user can download the KiCad ZIP manually."""
        if not part.part_url:
            return False
        return webbrowser.open(part.part_url)

    def write_instructions(self, part: SnapedaPart, result: DownloadResult) -> Path:
        """Write a small helper file next to the download folder."""
        note = self.download_dir / "HOW_TO_GET_THE_ZIP.txt"
        lines = [
            f"Manual download for {part.part_number} ({part.manufacturer})",
            "",
            f"1. Open: {part.part_url}",
            "2. Log in if SnapEDA asks for it.",
            "3. Press 'Download', choose the KiCad format and save the ZIP.",
            f"4. Drag the ZIP into the plugin (or copy it to {self.download_dir}).",
            "",
            "Attempt log:",
            *[f"  - {message}" for message in result.messages],
        ]
        note.write_text("\n".join(lines), encoding="utf-8")
        return note


def user_downloads_dir() -> Path:
    """The folder a normal browser drops downloads into."""
    return Path.home() / "Downloads"


def looks_like_kicad_archive(path: Path) -> bool:
    """True when the ZIP contains KiCad library files (symbol, footprint or legacy lib)."""
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                lowered = name.lower()
                if lowered.endswith((".kicad_sym", ".kicad_mod", ".lib")):
                    return True
    except (zipfile.BadZipFile, OSError):
        return False
    return False


class DownloadWatcher:
    """Detect finished ZIP files landing in a folder (the browser's downloads).

    This is the hybrid flow: the user searches in the plugin, presses *Download*
    on the SnapEDA page in the plugin browser, and the plugin notices the ZIP and
    imports it. Files are only reported once their size stopped changing and the
    file really is a ZIP, so half-written downloads are ignored.

    ``ignore_before`` skips files older than that timestamp (used when watching
    the user's own Downloads folder, where unrelated ZIPs already live) and
    ``require_kicad`` only accepts archives that carry KiCad library files.
    """

    def __init__(
        self,
        folder: Path,
        *,
        min_bytes: int = 512,
        ignore_before: float | None = None,
        require_kicad: bool = False,
    ) -> None:
        self.folder = Path(folder)
        self.min_bytes = min_bytes
        self.ignore_before = ignore_before
        self.require_kicad = require_kicad
        self._sizes: dict[Path, int] = {}
        self._done: set[Path] = set()

    def scan(self) -> list[Path]:
        """Return the ZIPs that are complete and were not reported yet."""
        ready: list[Path] = []
        if not self.folder.is_dir():
            return ready
        for candidate in sorted(self.folder.glob("*.zip")):
            if candidate in self._done:
                continue
            try:
                stat = candidate.stat()
            except OSError:
                continue
            if self.ignore_before is not None and stat.st_mtime < self.ignore_before:
                continue
            size = stat.st_size
            if size < self.min_bytes:
                continue
            if self._sizes.get(candidate) != size:
                self._sizes[candidate] = size  # first sighting (or still growing)
                continue
            if zipfile.is_zipfile(candidate):
                if self.require_kicad and not looks_like_kicad_archive(candidate):
                    self._done.add(candidate)  # a ZIP, but not a KiCad export
                    continue
                self._done.add(candidate)
                ready.append(candidate)
            else:
                self._done.add(candidate)  # not an importable archive: stop watching
        return ready

    def forget(self, path: Path | None = None) -> None:
        """Allow re-importing a file (or reset the whole watcher when path is None)."""
        if path is None:
            self._sizes.clear()
            self._done.clear()
            return
        self._done.discard(Path(path))
        self._sizes.pop(Path(path), None)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value or "part").strip("._-")
    return cleaned or "part"


def load_part_from_json(path: Path) -> SnapedaPart:
    """Helper for debugging: rebuild a part from a saved search JSON entry."""
    from snapeda_client import part_from_json  # noqa: PLC0415 - debug helper

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, dict) and "results" in data:
        data = data["results"][0]
    return part_from_json(data)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        from .snapeda_auth import import_cookies_from_browsers, load_session
    except ImportError:
        from snapeda_auth import import_cookies_from_browsers, load_session  # type: ignore[no-redef]

    cookies, source = load_session()
    if not cookies:
        imported = import_cookies_from_browsers()
        cookies, source = imported.cookies, imported.source
    print(f"Session source: {source or 'anonymous'}")

    query = sys.argv[1] if len(sys.argv) > 1 else "STM32F103"
    client = SnapedaClient(cookies)
    print("Session state:", client.session_state())
    found = client.search(query)
    if not found.parts:
        raise SystemExit("No results")
    part = found.parts[0]
    print(f"Trying download for {part.part_number} ({part.manufacturer}) ...")
    downloader = SnapedaDownloader(client, Path.home() / "KiCad_snapeda_downloads")
    outcome = downloader.download(part)
    for message in outcome.messages:
        print(" -", message)
    print("ZIP:", outcome.zip_path)
