"""Read SnapEDA cookies through a browser the plugin controls (CDP).

Why this exists: since Chrome/Edge 127 those browsers encrypt their cookie
database with *App-Bound Encryption* (``v20``). No outside process can decrypt
it, so :mod:`snapeda_auth` cannot read the cookies of a normal Chrome profile -
and Chrome 155 is exactly that case.

The way around it is to let the browser hand the cookies over itself. The plugin
starts its **own** browser window (separate ``--user-data-dir``, so the normal
profile is untouched - and a dedicated data dir is what allows remote debugging
at all since Chrome 136) and talks to it over the DevTools protocol:

    GET  /json/version                 is it up?
    GET  /json/list                    find a page target
    WS   Network.getAllCookies         cookies, decrypted by the browser itself

The login is a one-time step: the plugin profile keeps the SnapEDA session, so
later downloads just re-read the cookies. Everything is local; cookies only ever
go to snapeda.com.

Only the standard library is used (a minimal WebSocket client is included).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

DEFAULT_PORT = 9333
SNAPEDA_URL = "https://www.snapeda.com/"
PLUGIN_PROFILE_DIRNAME = "snapeda_browser_profile"

_CHROMIUM_NAMES = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    ],
}


class BrowserCdpError(RuntimeError):
    """The plugin-controlled browser could not be reached or queried."""


@dataclass
class BrowserSession:
    """A browser window started by the plugin."""

    process: subprocess.Popen[bytes]
    port: int
    profile_dir: Path
    executable: Path


# ---------------------------------------------------------------------------
# launching
# ---------------------------------------------------------------------------


def find_browser_executable() -> Path | None:
    """Locate Chrome/Edge/Brave on this machine."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        candidates += [Path(path) for path in _CHROMIUM_NAMES["win32"]]
        candidates += [
            local / "Google" / "Chrome" / "Application" / "chrome.exe",
            local / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            local / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe",
        ]
    elif sys.platform == "darwin":
        candidates += [Path(path) for path in _CHROMIUM_NAMES["darwin"]]
    else:
        for name in (
            "google-chrome",
            "chromium",
            "chromium-browser",
            "microsoft-edge",
            "brave-browser",
        ):
            from shutil import which

            found = which(name)
            if found:
                candidates.append(Path(found))

    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def legacy_profile_dir(base_dir: Path | None = None) -> Path:
    """Old location inside the plugin folder (moved away by ``default_profile_dir``)."""
    return Path(base_dir or Path(__file__).resolve().parent) / PLUGIN_PROFILE_DIRNAME


def default_profile_dir(base_dir: Path | None = None) -> Path:
    """Profile of the plugin browser, kept outside the plugin folder.

    Inside ``plugins/`` it was removed by every plugin update, taking the SnapEDA
    login with it; the old folder is moved over once so the session survives.
    """
    legacy = legacy_profile_dir(base_dir)
    try:
        from .user_paths import migrate_dir, user_data_dir
    except ImportError:  # standalone / sys.path import
        from user_paths import migrate_dir, user_data_dir  # type: ignore[import-not-found,no-redef]

    return migrate_dir(legacy, user_data_dir() / PLUGIN_PROFILE_DIRNAME)


def _seed_download_prefs(profile: Path, download_dir: Path) -> None:
    """Point the profile's download folder at ``download_dir`` before Chrome starts.

    ``Browser.setDownloadBehavior`` is accepted by current Chrome but ignored for
    the download path, so the preference file is the reliable knob. It only works
    while the browser is stopped - which is the case here, we are about to launch it.
    """
    try:
        prefs_path = profile / "Default" / "Preferences"
        data: dict[str, object] = {}
        if prefs_path.is_file():
            try:
                data = json.loads(prefs_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        download = data.get("download")
        if not isinstance(download, dict):
            download = {}
        download["default_directory"] = str(download_dir)
        download["prompt_for_download"] = False
        data["download"] = download
        savefile = data.get("savefile")
        if not isinstance(savefile, dict):
            savefile = {}
        savefile["default_directory"] = str(download_dir)
        data["savefile"] = savefile
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = prefs_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data), encoding="utf-8")
        temp_path.replace(prefs_path)
        logger.info("Browser profile downloads pre-set to %s", download_dir)
    except OSError as exc:
        logger.warning("Could not seed the browser download folder: %s", exc)


def launch_session_browser(
    *,
    profile_dir: Path | None = None,
    port: int = DEFAULT_PORT,
    url: str = SNAPEDA_URL,
    executable: Path | None = None,
    download_dir: Path | None = None,
) -> BrowserSession:
    """Start a visible browser window with a dedicated profile and CDP enabled.

    A separate ``--user-data-dir`` is required: Chrome ignores
    ``--remote-debugging-port`` when it would use the default profile.
    """
    binary = executable or find_browser_executable()
    if binary is None:
        raise BrowserCdpError(
            "No Chrome/Edge/Brave found. Install one of them or paste the cookie header manually."
        )

    profile = Path(profile_dir) if profile_dir else default_profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    if download_dir is not None:
        Path(download_dir).mkdir(parents=True, exist_ok=True)
        _seed_download_prefs(profile, Path(download_dir))

    command = [
        str(binary),
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        url,
    ]
    logger.info("Starting browser session: %s", " ".join(command))
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(  # noqa: S603 - fixed executable, fixed arguments
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation_flags,
    )
    return BrowserSession(process=process, port=port, profile_dir=profile, executable=binary)


# ---------------------------------------------------------------------------
# minimal WebSocket client (CDP transport)
# ---------------------------------------------------------------------------


class _WebSocket:
    """Just enough WebSocket for one request/response CDP call."""

    def __init__(self, url: str, timeout: float = 20.0) -> None:
        parsed = urlparse(url)
        if parsed.scheme != "ws" or not parsed.hostname:
            raise BrowserCdpError(f"Unsupported CDP websocket URL: {url}")
        self.host = parsed.hostname
        self.port = parsed.port or 80
        self.path = parsed.path or "/"
        if parsed.query:
            self.path += "?" + parsed.query
        self.timeout = timeout
        self._socket: socket.socket | None = None

    def __enter__(self) -> _WebSocket:
        self._socket = socket.create_connection((self.host, self.port), timeout=self.timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        handshake = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {self.host}:{self.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        self._socket.sendall(handshake.encode())
        response = self._read_until(b"\r\n\r\n")
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise BrowserCdpError(f"CDP websocket handshake failed: {response!r}")
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None

    def _read_until(self, marker: bytes) -> bytes:
        assert self._socket is not None
        buffer = b""
        while marker not in buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                break
            buffer += chunk
        return buffer

    def _recv_exact(self, count: int) -> bytes:
        assert self._socket is not None
        data = b""
        while len(data) < count:
            chunk = self._socket.recv(count - len(data))
            if not chunk:
                raise BrowserCdpError("CDP websocket closed unexpectedly")
            data += chunk
        return data

    def _send_text(self, payload: bytes) -> None:
        assert self._socket is not None
        mask = secrets.token_bytes(4)
        header = bytearray([0x81])  # FIN + text frame
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        self._socket.sendall(bytes(header) + mask + masked)

    def _read_frame(self) -> tuple[int, bytes]:
        assert self._socket is not None
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return opcode, payload

    def call(self, method: str, params: dict | None = None, request_id: int = 1) -> dict:
        """Send one CDP command and return its result (ignores unsolicited events)."""
        message = json.dumps({"id": request_id, "method": method, "params": params or {}}).encode()
        self._send_text(message)

        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            opcode, payload = self._read_frame()
            if opcode == 0x9:  # ping -> pong
                self._send_text(payload)
                continue
            if opcode == 0x8:
                raise BrowserCdpError("CDP websocket closed by the browser")
            if opcode not in (0x1, 0x0):  # text or continuation
                continue
            try:
                data = json.loads(payload.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if data.get("id") != request_id:
                continue  # event from the browser
            if "error" in data:
                raise BrowserCdpError(f"CDP error for {method}: {data['error']}")
            result = data.get("result")
            return result if isinstance(result, dict) else {}
        raise BrowserCdpError(f"CDP call {method} timed out")


# ---------------------------------------------------------------------------
# talking to the browser
# ---------------------------------------------------------------------------


def _http_json(url: str, timeout: float = 2.0) -> dict:
    request = urllib.request.Request(url)  # noqa: S310 - localhost only
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8", "replace"))


def wait_for_cdp(port: int = DEFAULT_PORT, timeout: float = 40.0) -> bool:
    """Wait until the browser answers the DevTools HTTP endpoint."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            _http_json(f"http://127.0.0.1:{port}/json/version")
            return True
        except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
            time.sleep(0.5)
    return False


def _page_target(port: int) -> str:
    """WebSocket URL of a page target, preferring one already on snapeda.com."""
    targets = _http_json(f"http://127.0.0.1:{port}/json/list")
    pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
    if not pages:
        raise BrowserCdpError(
            "The browser has no debuggable page. Open a tab (any page) in the plugin window and retry."
        )
    for target in pages:
        if "snapeda.com" in str(target.get("url", "")):
            return str(target["webSocketDebuggerUrl"])
    return str(pages[0]["webSocketDebuggerUrl"])


def read_cookies(
    port: int = DEFAULT_PORT,
    *,
    domain_filter: str = "snapeda.com",
    timeout: float = 20.0,
) -> dict[str, str]:
    """Return the cookies the browser holds, decrypted by the browser itself.

    Works with App-Bound Encryption because the browser performs the decryption:
    ``Network.getAllCookies`` is an ordinary DevTools command.
    """
    websocket_url = _page_target(port)
    cookies: dict[str, str] = {}
    with _WebSocket(websocket_url, timeout=timeout) as client:
        result = client.call("Network.getAllCookies")
        for cookie in result.get("cookies") or []:
            domain = str(cookie.get("domain", "")).lstrip(".")
            if domain_filter and domain_filter not in domain:
                continue
            name, value = cookie.get("name"), cookie.get("value")
            if name and value:
                cookies[str(name)] = str(value)
    return cookies


def read_cookies_with_retry(
    port: int = DEFAULT_PORT,
    *,
    domain_filter: str = "snapeda.com",
    attempts: int = 3,
) -> dict[str, str]:
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return read_cookies(port, domain_filter=domain_filter)
        except (BrowserCdpError, OSError, TimeoutError) as exc:
            last = exc
            logger.debug("CDP cookie read attempt %s failed: %s", attempt, exc)
            time.sleep(1.0 * attempt)
    raise BrowserCdpError(f"Could not read cookies from the plugin browser: {last}")


def cookie_payload(cookies: dict[str, str], domain: str) -> list[dict[str, object]]:
    """CDP ``Network.setCookies`` records for ``cookies`` on ``domain``.

    A leading dot makes the cookie valid for the bare host *and* its
    subdomains, which is what a site that sets its login cookie on
    ``example.com`` while serving ``www.example.com`` needs.
    """
    host = domain.strip().lstrip(".")
    if not host:
        return []
    return [
        {
            "name": str(name),
            "value": str(value),
            "domain": f".{host}",
            "path": "/",
            "secure": True,
        }
        for name, value in cookies.items()
        if name and value
    ]


def set_cookies_in_browser(
    port: int = DEFAULT_PORT,
    cookies: dict[str, str] | None = None,
    *,
    domain: str = "snapeda.com",
    timeout: float = 20.0,
) -> int:
    """Put saved cookies back into the plugin browser.

    The profile normally keeps the login, but it is also the file that a KiCad
    update, a "clear browsing data" click or a moved profile can reset; replaying
    the saved session first means the window opens already signed in instead of
    asking for the password again. Returns how many cookies were restored.
    """
    records = cookie_payload(cookies or {}, domain)
    if not records:
        return 0
    try:
        with _WebSocket(_page_target(port), timeout=timeout) as client:
            client.call("Network.setCookies", {"cookies": records}, request_id=8)
    except (BrowserCdpError, OSError):
        # Narrower CDP surfaces only expose the browser-target command.
        with _WebSocket(_browser_target(port), timeout=timeout) as browser:
            browser.call("Storage.setCookies", {"cookies": records}, request_id=9)
    logger.info("Restored %s saved cookies for %s in the plugin browser", len(records), domain)
    return len(records)


def delete_cookies_in_browser(
    port: int = DEFAULT_PORT,
    cookies: dict[str, str] | None = None,
    *,
    domain: str = "snapeda.com",
    timeout: float = 20.0,
) -> int:
    """Delete the given cookie **names** from the plugin browser for ``domain``.

    "Forget saved session" has to remove them from the browser too: the panel
    captures the browser's session automatically, so a cookie left behind would be
    saved again on the next tick. Returns how many names were sent.
    """
    host = domain.strip().lstrip(".")
    if not host or not cookies:
        return 0
    sent = 0
    with _WebSocket(_page_target(port), timeout=timeout) as client:
        for offset, name in enumerate(cookies):
            for suffix, value in (("." + host, 30 + offset * 2), (host, 31 + offset * 2)):
                try:
                    client.call(
                        "Network.deleteCookies",
                        {"name": str(name), "domain": suffix},
                        request_id=value,
                    )
                except BrowserCdpError as exc:  # pragma: no cover - older CDP surfaces
                    logger.debug("Could not delete %s for %s: %s", name, suffix, exc)
            sent += 1
    logger.info("Deleted %s cookie names for %s from the plugin browser", sent, host)
    return sent


def browser_user_agent(port: int = DEFAULT_PORT, timeout: float = 10.0) -> str:
    """The user agent of the plugin browser.

    Cloudflare's ``__cf_bm`` cookie is bound to it, so HTTP requests made by the
    plugin must introduce themselves exactly like that browser does.
    """
    websocket_url = _page_target(port)
    with _WebSocket(websocket_url, timeout=timeout) as client:
        result = client.call(
            "Runtime.evaluate",
            {"expression": "navigator.userAgent", "returnByValue": True},
            request_id=4,
        )
    value = (result.get("result") or {}).get("value")
    return str(value or "")


def open_url(port: int = DEFAULT_PORT, url: str = SNAPEDA_URL) -> bool:
    """Open ``url`` in a new tab of the plugin browser."""
    websocket_url = _page_target(port)
    with _WebSocket(websocket_url) as client:
        client.call("Target.createTarget", {"url": url}, request_id=3)
    logger.info("Opened %s in the plugin browser", url)
    return True


def fetch_page_html(
    port: int = DEFAULT_PORT, url: str = SNAPEDA_URL, *, timeout: float = 90.0
) -> str:
    """Navigate the plugin browser to ``url`` and return the rendered HTML.

    Cloudflare decides per request whether it trusts a plain HTTP client, so a
    site can answer the plugin's own request with the challenge while the browser
    (which passed it once) is let through. Asking the browser for the page keeps
    that site usable; the caller parses the same HTML it would get over HTTP.
    """
    websocket_url = _page_target(port)
    deadline = time.monotonic() + timeout
    html = ""
    with _WebSocket(websocket_url, timeout=timeout) as client:
        client.call("Page.enable", {}, request_id=5)
        client.call("Page.navigate", {"url": url}, request_id=6)
        while time.monotonic() < deadline:
            result = client.call(
                "Runtime.evaluate",
                {
                    "expression": (
                        "document.readyState + '|' + "
                        "(document.documentElement ? document.documentElement.outerHTML : '')"
                    ),
                    "returnByValue": True,
                },
                request_id=7,
            )
            value = str((result.get("result") or {}).get("value") or "")
            state, _, html = value.partition("|")
            if state == "complete" and html and "Just a moment" not in html:
                return html
            time.sleep(0.4)
    return html


def _browser_target(port: int) -> str:
    """WebSocket URL of the **browser** target (needed for Browser.* commands)."""
    version = _http_json(f"http://127.0.0.1:{port}/json/version")
    url = version.get("webSocketDebuggerUrl")
    if not url:
        raise BrowserCdpError("The browser did not expose a DevTools websocket URL")
    return str(url)


def set_download_directory(port: int = DEFAULT_PORT, path: Path | str | None = None) -> bool:
    """Send downloads of the plugin browser to ``path`` (the folder the plugin watches).

    With this, pressing "Download" on a SnapEDA part page drops the ZIP where the
    plugin picks it up and imports it - no cookie replay, no scraping.

    ``Browser.setDownloadBehavior`` only answers on the *browser* target; sending
    it to a page target (as this used to) returns success and changes nothing, so
    the downloads kept landing in the normal Downloads folder.
    """
    if path is None:
        return False
    target = Path(path)
    target.mkdir(parents=True, exist_ok=True)
    params = {"behavior": "allow", "downloadPath": str(target), "eventsEnabled": False}

    done = False
    try:
        with _WebSocket(_browser_target(port)) as client:
            client.call("Browser.setDownloadBehavior", params, request_id=2)
        done = True
    except (BrowserCdpError, OSError) as exc:
        logger.warning("Browser.setDownloadBehavior failed: %s", exc)

    # Older/narrower CDP surfaces: also set it per page (deprecated, but harmless).
    for target_url in _all_page_targets(port)[:3]:
        try:
            with _WebSocket(target_url) as client:
                client.call("Page.setDownloadBehavior", params, request_id=3)
            done = True
        except (BrowserCdpError, OSError) as exc:
            logger.debug("Page.setDownloadBehavior failed for %s: %s", target_url, exc)

    if done:
        logger.info("Plugin browser downloads go to %s", target)
    return done


def _all_page_targets(port: int = DEFAULT_PORT) -> list[str]:
    """WebSocket URLs of up to three page targets (best effort)."""
    try:
        targets = _http_json(f"http://127.0.0.1:{port}/json/list")
    except (urllib.error.URLError, OSError, json.JSONDecodeError, TimeoutError):
        return []
    urls = [
        str(t["webSocketDebuggerUrl"])
        for t in targets
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl")
    ]
    return urls


def session_state(
    port: int = DEFAULT_PORT, domain_filter: str = "snapeda.com"
) -> dict[str, object]:
    """Is the plugin browser running, and does it hold cookies for the site?"""
    state: dict[str, object] = {"running": False, "cookies": {}, "error": ""}
    try:
        _http_json(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        state["running"] = True
    except Exception:  # noqa: BLE001 - "not running" is the common case
        return state
    try:
        state["cookies"] = read_cookies(port, domain_filter=domain_filter)
    except Exception as exc:  # noqa: BLE001 - report instead of raising
        state["error"] = str(exc)
    return state


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    import argparse

    parser = argparse.ArgumentParser(description="Plugin browser session helper")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--launch", action="store_true", help="start the plugin browser window")
    parser.add_argument("--cookies", action="store_true", help="print the SnapEDA cookie names")
    parser.add_argument("--domain", default="snapeda.com")
    args = parser.parse_args()

    if args.launch:
        session = launch_session_browser(port=args.port)
        print(f"Browser started (pid {session.process.pid}), profile: {session.profile_dir}")
        print("CDP ready:", wait_for_cdp(args.port))
    if args.cookies:
        cookies = read_cookies_with_retry(args.port, domain_filter=args.domain)
        print(f"{len(cookies)} cookies for {args.domain}: {sorted(cookies)}")
