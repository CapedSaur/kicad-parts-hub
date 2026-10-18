"""Read SnapEDA cookies from locally installed browsers.

SnapEDA serves its part pages and downloads behind Cloudflare. A real browser
that is logged into SnapEDA holds two cookies we need:

``cf_clearance``  proves to Cloudflare that a browser solved the challenge
``sessionid``     the SnapEDA (Django) login session

Extraction strategy, in order:

1. **Firefox** - cookies live unencrypted in ``cookies.sqlite`` (stdlib only).
2. **Chromium family** (Chrome, Edge, Brave, Chromium, Vivaldi) - cookies are
   encrypted. ``v10``/``v11`` values are AES-256-GCM with a key wrapped by
   Windows DPAPI; decrypting them needs ``cryptography`` (or ``pycryptodome``).
   Chrome 127+ stores cookies as ``v20`` (App-Bound Encryption) which cannot be
   decrypted from outside the browser - the module reports that explicitly
   instead of failing silently.
3. **Manual paste** - the dialog accepts a ``Cookie:`` header copied from the
   browser's DevTools; the session is then cached in ``snapeda_session.json``.

Nothing is uploaded anywhere: the cookies are only sent to snapeda.com.
"""

from __future__ import annotations

import base64
import ctypes
import json
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPEDA_DOMAIN = "snapeda.com"
SESSION_FILE_NAME = "snapeda_session.json"
_NEEDED_COOKIES = ("cf_clearance", "sessionid")

# Chromium encryption prefixes
_V10 = b"v10"
_V11 = b"v11"
_V20 = b"v20"


class BrowserCookieError(RuntimeError):
    """Raised when a cookie store cannot be read."""


class AppBoundEncryptionError(BrowserCookieError):
    """Chrome/Edge store the cookies with App-Bound Encryption (v20).

    Only the browser process itself can decrypt those values, so an external
    tool cannot read them. Use Firefox, paste the cookie header manually, or
    download the model in the browser and drop the ZIP into the plugin.
    """


@dataclass
class CookieStore:
    """A discovered browser cookie database."""

    browser: str
    profile: str
    path: Path
    kind: str  # "chromium" | "firefox"

    def describe(self) -> str:
        return f"{self.browser} ({self.profile})"


@dataclass
class CookieImportResult:
    cookies: dict[str, str]
    source: str
    warnings: list[str]


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------


def _home() -> Path:
    return Path.home()


def _chromium_roots() -> list[tuple[str, Path]]:
    """Known Chromium-family user-data directories for the current OS."""
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", _home() / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming"))
        return [
            ("Chrome", local / "Google" / "Chrome" / "User Data"),
            ("Edge", local / "Microsoft" / "Edge" / "User Data"),
            ("Brave", local / "BraveSoftware" / "Brave-Browser" / "User Data"),
            ("Chromium", local / "Chromium" / "User Data"),
            ("Vivaldi", local / "Vivaldi" / "User Data"),
            ("Opera", roaming / "Opera Software" / "Opera Stable"),
        ]
    if sys.platform == "darwin":
        support = _home() / "Library" / "Application Support"
        return [
            ("Chrome", support / "Google" / "Chrome"),
            ("Edge", support / "Microsoft Edge"),
            ("Brave", support / "BraveSoftware" / "Brave-Browser"),
            ("Chromium", support / "Chromium"),
            ("Vivaldi", support / "Vivaldi"),
            ("Opera", support / "com.operasoftware.Opera"),
        ]
    config = Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config"))
    return [
        ("Chrome", config / "google-chrome"),
        ("Chromium", config / "chromium"),
        ("Edge", config / "microsoft-edge"),
        ("Brave", config / "BraveSoftware" / "Brave-Browser"),
        ("Vivaldi", config / "vivaldi"),
        ("Opera", config / "opera"),
    ]


def find_chromium_stores() -> list[CookieStore]:
    """Chromium cookie databases that exist on this machine."""
    stores: list[CookieStore] = []
    for browser, root in _chromium_roots():
        if not root.is_dir():
            continue
        candidates = [root / "Network" / "Cookies", root / "Cookies"]
        for network_root in ("Default", "Profile 1", "Profile 2", "Profile 3", "Profile 4"):
            candidates += [
                root / network_root / "Network" / "Cookies",
                root / network_root / "Cookies",
            ]
        for path in candidates:
            if path.is_file():
                profile = (
                    path.parent.parent.name if path.parent.name == "Network" else path.parent.name
                )
                stores.append(CookieStore(browser, profile, path, "chromium"))
    # de-duplicate while keeping order
    seen: set[Path] = set()
    unique: list[CookieStore] = []
    for store in stores:
        if store.path in seen:
            continue
        seen.add(store.path)
        unique.append(store)
    return unique


def find_firefox_stores() -> list[CookieStore]:
    """Firefox cookie databases that exist on this machine."""
    if sys.platform == "win32":
        root = (
            Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming")) / "Mozilla" / "Firefox"
        )
    elif sys.platform == "darwin":
        root = _home() / "Library" / "Application Support" / "Firefox"
    else:
        root = _home() / ".mozilla" / "firefox"
    profiles_dir = root / "Profiles"
    if not profiles_dir.is_dir():
        return []
    stores: list[CookieStore] = []
    for profile in sorted(profiles_dir.iterdir()):
        db = profile / "cookies.sqlite"
        if db.is_file():
            stores.append(CookieStore("Firefox", profile.name, db, "firefox"))
    return stores


def find_cookie_stores() -> list[CookieStore]:
    """All readable cookie stores, Firefox first (no extra dependency)."""
    return find_firefox_stores() + find_chromium_stores()


# ---------------------------------------------------------------------------
# decryption helpers
# ---------------------------------------------------------------------------


def _aes_gcm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes) -> bytes:
    """AES-256-GCM decryption using ``cryptography`` or ``pycryptodome``."""
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # type: ignore[import-not-found]

        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except ImportError:
        pass
    try:
        from Crypto.Cipher import AES  # type: ignore[import-not-found]

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext[:-16], ciphertext[-16:])
    except ImportError as exc:
        raise BrowserCookieError(
            "Reading Chrome/Edge cookies needs the 'cryptography' package "
            "(pip install cryptography), or use Firefox / paste the cookie manually."
        ) from exc


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_unprotect(data: bytes) -> bytes:
    """Windows DPAPI CryptUnprotectData (current user scope)."""
    if sys.platform != "win32":
        raise BrowserCookieError("DPAPI is only available on Windows")

    buffer_in = _DataBlob(
        len(data), ctypes.cast(ctypes.create_string_buffer(data), ctypes.POINTER(ctypes.c_char))
    )
    buffer_out = _DataBlob()
    if not ctypes.windll.crypt32.CryptUnprotectData(  # type: ignore[attr-defined]
        ctypes.byref(buffer_in), None, None, None, None, 0, ctypes.byref(buffer_out)
    ):
        raise BrowserCookieError("DPAPI could not decrypt the browser key")
    try:
        return ctypes.string_at(buffer_out.pbData, buffer_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(buffer_out.pbData)  # type: ignore[attr-defined]


def _linux_key() -> bytes:
    """Best-effort Chrome key on Linux ('peanuts' when no keyring is used)."""
    try:  # pragma: no cover - platform specific
        import secretstorage  # type: ignore[import-not-found]

        connection = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(connection)
        for item in collection.get_all_items():
            if item.get_label() in {"Chrome Safe Storage", "Chromium Safe Storage"}:
                return item.get_secret().ljust(32, b"\x00")[:32]
    except Exception as exc:  # pragma: no cover - platform specific
        logger.debug("Secret storage lookup failed: %s", exc)
    return b"peanuts".ljust(32, b"\x00")


def _chromium_key(store: CookieStore) -> bytes:
    """Recover the AES key used for ``v10``/``v11`` cookie values."""
    if store.path.parent.name == "Network":
        user_data = store.path.parent.parent
    else:
        user_data = store.path.parent
    if store.browser == "Opera":  # Opera keeps Local State one level higher
        user_data = user_data.parent
    local_state_path = user_data / "Local State"
    if not local_state_path.is_file():
        raise BrowserCookieError(f"Local State not found next to {store.path}")

    data = json.loads(local_state_path.read_text(encoding="utf-8", errors="replace"))
    encrypted_key = data.get("os_crypt", {}).get("encrypted_key")
    if not encrypted_key:
        raise BrowserCookieError(f"No os_crypt.encrypted_key in {local_state_path}")

    raw = base64.b64decode(encrypted_key)
    if raw[:5] == b"DPAPI":
        raw = raw[5:]
        if sys.platform == "win32":
            return _dpapi_unprotect(raw)
        # Key is not DPAPI-wrapped (Linux/macOS) - not usable without keyring support.
        raise BrowserCookieError(
            "The Chrome cookie key is not DPAPI-wrapped; automatic import is not supported on this system"
        )
    if sys.platform == "darwin":  # pragma: no cover - platform specific
        import subprocess

        password = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if not password:
            raise BrowserCookieError("Keychain lookup for 'Chrome Safe Storage' failed")
        from hashlib import pbkdf2_hmac

        return pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, 16)
    if sys.platform.startswith("linux"):  # pragma: no cover - platform specific
        return _linux_key()
    raise BrowserCookieError("Unsupported platform for Chrome cookie decryption")


def _decrypt_chromium_value(key: bytes, value: bytes) -> str:
    if value[:3] == _V20:
        raise AppBoundEncryptionError(
            "This browser stores cookies with App-Bound Encryption (v20, Chrome/Edge 127+). "
            "Automatic import is not possible; use Firefox, paste the cookie header manually, "
            "or download the model in the browser and drop the ZIP into the plugin."
        )
    if value[:3] in (_V10, _V11):
        nonce, ciphertext = value[3:15], value[15:]
        try:
            return _aes_gcm_decrypt(key, nonce, ciphertext).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - any crypto failure means "unreadable"
            raise BrowserCookieError(f"Could not decrypt cookie value: {exc}") from exc
    if sys.platform == "win32":
        return _dpapi_unprotect(value).decode("utf-8", "replace")
    return value.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# reading stores
# ---------------------------------------------------------------------------


def _sqlite_query(db_path: Path, query: str, params: tuple) -> list[tuple]:
    """Run a read-only query, working around the browser's file lock.

    Order: direct read-only (works while the browser runs and sees WAL data),
    ``immutable=1`` (ignores locks but may miss uncheckpointed data), then a
    copy to a temp folder. Windows browsers hold an exclusive lock on the
    cookie database, so with the browser open all three can fail.
    """
    attempts: list[tuple[str, object]] = [
        ("direct", f"file:{db_path}?mode=ro"),
        ("immutable", f"file:{db_path}?immutable=1"),
    ]
    errors: list[str] = []

    for label, uri in attempts:
        try:
            connection = sqlite3.connect(str(uri), uri=True, timeout=2.0)
            try:
                return connection.execute(query, params).fetchall()
            finally:
                connection.close()
        except sqlite3.Error as exc:
            errors.append(f"{label}: {exc}")

    locked = False
    try:
        with tempfile.TemporaryDirectory(prefix="snapeda_cookies_") as tmp:
            copy = Path(tmp) / db_path.name
            shutil.copyfile(db_path, copy)
            for suffix in ("-wal", "-shm"):
                sidecar = db_path.with_name(db_path.name + suffix)
                if sidecar.exists():
                    shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
            connection = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
            try:
                return connection.execute(query, params).fetchall()
            finally:
                connection.close()
    except OSError as exc:
        locked = getattr(exc, "winerror", None) == 32 or exc.errno == 13
        errors.append(f"copy: {exc}")
    except sqlite3.Error as exc:
        errors.append(f"copy: {exc}")

    if locked:
        raise BrowserCookieError(
            f"{db_path} is locked by the running browser. "
            "Close Chrome/Edge/Brave completely, then import the session again."
        )
    raise BrowserCookieError(
        f"Could not read {db_path} (" + "; ".join(errors) + "). "
        "Close the browser completely and try again."
    )


def read_firefox_cookies(store: CookieStore, domain: str = SNAPEDA_DOMAIN) -> dict[str, str]:
    rows = _sqlite_query(
        store.path,
        "SELECT name, value FROM moz_cookies WHERE host LIKE ?",
        (f"%{domain}%",),
    )
    return {str(name): str(value) for name, value in rows if value}


def read_chromium_cookies(store: CookieStore, domain: str = SNAPEDA_DOMAIN) -> dict[str, str]:
    rows = _sqlite_query(
        store.path,
        "SELECT name, encrypted_value, value FROM cookies WHERE host_key LIKE ?",
        (f"%{domain}%",),
    )
    if not rows:
        return {}

    # Report App-Bound Encryption (v20) before asking for a crypto library:
    # no library can decrypt those values, so 'pip install cryptography' would
    # not help and the user should paste the cookie header instead.
    encrypted_rows = [(str(n), bytes(e)) for n, e, plain in rows if not plain and e]
    if encrypted_rows and all(value[:3] == _V20 for _, value in encrypted_rows):
        raise AppBoundEncryptionError(
            f"This browser stores the {domain} cookies with App-Bound Encryption (v20, "
            "Chrome/Edge 127+): " + ", ".join(name for name, _ in encrypted_rows) + ". "
            "Automatic import is not possible; use Firefox, paste the cookie header "
            "manually, or download the model in the browser and drop the ZIP here."
        )

    key: bytes | None = None
    cookies: dict[str, str] = {}
    for name, encrypted, plain in rows:
        if plain:
            cookies[str(name)] = str(plain)
            continue
        if not encrypted:
            continue
        if key is None:
            key = _chromium_key(store)
        cookies[str(name)] = _decrypt_chromium_value(key, bytes(encrypted))
    return cookies


def read_store(store: CookieStore, domain: str = SNAPEDA_DOMAIN) -> dict[str, str]:
    """Read all cookies of ``domain`` from a store, transparently decrypting them."""
    if store.kind == "firefox":
        return read_firefox_cookies(store, domain)
    return read_chromium_cookies(store, domain)


# ---------------------------------------------------------------------------
# high level helpers
# ---------------------------------------------------------------------------


def import_cookies_from_browsers(
    domain: str = SNAPEDA_DOMAIN,
    needed: tuple[str, ...] = _NEEDED_COOKIES,
    preferred: str = "cf_clearance",
) -> CookieImportResult:
    """Try every installed browser and return the best cookie set for ``domain``.

    The winner is the first store that provides ``preferred`` (the cookie that gets
    past Cloudflare: ``cf_clearance`` on SnapEDA, ``__cf_bm`` on Component Search
    Engine). Stores without cookies of that site are skipped.
    """
    stores = find_cookie_stores()
    warnings: list[str] = []
    best: tuple[dict[str, str], str] | None = None

    for store in stores:
        try:
            cookies = read_store(store, domain)
        except AppBoundEncryptionError as exc:
            logger.info("Skipping %s: %s", store.describe(), exc)
            warnings.append(f"{store.describe()}: {exc}")
            continue
        except BrowserCookieError as exc:
            logger.debug("Skipping %s: %s", store.describe(), exc)
            warnings.append(f"{store.describe()}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 - keep scanning other browsers
            logger.debug("Unexpected error on %s: %s", store.describe(), exc)
            warnings.append(f"{store.describe()}: {exc}")
            continue

        if not cookies:
            continue
        matched = [name for name in needed if name in cookies]
        logger.info(
            "Found %s cookies in %s: %s", domain, store.describe(), ", ".join(sorted(cookies))
        )
        if preferred in cookies:
            return CookieImportResult(cookies, store.describe(), warnings)
        if best is None and matched:
            best = (cookies, store.describe())

    if best is not None:
        cookies, source = best
        warnings.append(
            f"{source} has {domain} cookies but no '{preferred}': "
            f"open {domain} in that browser first, then import again."
        )
        return CookieImportResult(cookies, source, warnings)

    if not stores:
        warnings.append("No Firefox/Chrome/Edge/Brave cookie database found on this machine.")
    return CookieImportResult({}, "", warnings)


def session_file_path(base_dir: Path | None = None, file_name: str = SESSION_FILE_NAME) -> Path:
    """Where the imported session of one site is cached between runs.

    By default it lives in the user's data folder, **not** in the plugin folder:
    reinstalling the plugin replaces that folder and used to delete the login.
    ``base_dir`` is still honoured for tests and portable setups, and ``file_name``
    keeps one session file per site (SnapEDA, Component Search Engine).
    """
    plugin_dir = Path(__file__).resolve().parent
    if base_dir is not None:
        return Path(base_dir) / file_name
    try:
        from .user_paths import migrate_file, user_data_dir
    except ImportError:  # standalone / sys.path import
        from user_paths import migrate_file, user_data_dir  # type: ignore[import-not-found,no-redef]

    target = user_data_dir() / file_name
    return migrate_file(plugin_dir / file_name, target)


def save_session(
    cookies: dict[str, str],
    source: str = "",
    base_dir: Path | None = None,
    file_name: str = SESSION_FILE_NAME,
    *,
    merge: bool = True,
) -> Path:
    """Persist the cookies so the login survives a plugin restart.

    ``merge`` keeps cookies the existing file has but the new set does not: sites
    like Component Search Engine hand out their login cookie at a different time
    than the Cloudflare one, and a later partial read must not drop it.
    """
    path = session_file_path(base_dir, file_name)
    stored: dict[str, str] = {}
    if merge and cookies:
        stored, _source = load_session(base_dir, file_name)
    payload = {"source": source, "cookies": {**stored, **cookies}}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    try:  # keep the secret readable only by the user
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - Windows/ACL dependent
        pass
    logger.info("Saved session to %s", path)
    return path


def load_session(
    base_dir: Path | None = None, file_name: str = SESSION_FILE_NAME
) -> tuple[dict[str, str], str]:
    """Load a previously cached session; returns ``({}, "")`` when absent."""
    path = session_file_path(base_dir, file_name)
    if not path.is_file():
        return {}, ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return {}, ""
    cookies = payload.get("cookies") or {}
    if not isinstance(cookies, dict):
        return {}, ""
    return {str(k): str(v) for k, v in cookies.items()}, str(payload.get("source") or path.name)


def clear_session(base_dir: Path | None = None, file_name: str = SESSION_FILE_NAME) -> None:
    session_file_path(base_dir, file_name).unlink(missing_ok=True)


if __name__ == "__main__":  # pragma: no cover - manual smoke test
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print("Cookie stores found:")
    for store in find_cookie_stores():
        print(f"  - {store.browser:<9} {store.profile:<12} {store.path}")
    result = import_cookies_from_browsers()
    if result.cookies:
        print(f"\nImported from {result.source}: {sorted(result.cookies)}")
    else:
        print("\nNo SnapEDA cookies imported.")
    for warning in result.warnings:
        print(f"  ! {warning}")
