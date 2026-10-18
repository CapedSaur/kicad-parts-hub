"""SnapEDA search panel for the KI-CAD plugin.

Embeds a direct SnapEDA (SnapMagic Search) search into the plugin GUI:

* search + autocomplete through :mod:`snapeda_client` (works without login),
* session handling through :mod:`snapeda_auth` (cookies from a logged-in
  browser, or a pasted ``Cookie:`` header),
* part download through :mod:`snapeda_download` (needs the session), with the
  manual "open the part page and drop the ZIP" fallback,
* import of the downloaded ZIP is delegated to the caller
  (``on_import_zip``), which uses the existing :class:`LibImporter`.

Can be run standalone for a quick UI check:
``python plugins/snapeda_search.py`` (needs KiCad's Python, which ships wx).
"""

from __future__ import annotations

import io
import logging
import shutil
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import wx
import wx.adv

try:  # imported as part of the plugin package
    from .snapeda_auth import (
        clear_session,
        import_cookies_from_browsers,
        load_session,
        save_session,
    )
    from .snapeda_browser import (
        DEFAULT_PORT as CDP_PORT,
    )
    from .snapeda_browser import (
        BrowserCdpError,
        default_profile_dir,
        launch_session_browser,
        open_url,
        read_cookies_with_retry,
        session_state,
        set_download_directory,
        wait_for_cdp,
    )
    from .snapeda_client import (
        DEFAULT_USER_AGENT,
        SnapedaClient,
        SnapedaCloudflareBlocked,
        SnapedaError,
        SnapedaPart,
        parse_cookie_header,
    )
    from .snapeda_download import DownloadWatcher, SnapedaDownloader, user_downloads_dir
except ImportError:  # standalone / sys.path import
    from snapeda_auth import (  # type: ignore[import-not-found,no-redef]
        clear_session,
        import_cookies_from_browsers,
        load_session,
        save_session,
    )
    from snapeda_browser import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_PORT as CDP_PORT,
    )
    from snapeda_browser import (  # type: ignore[import-not-found,no-redef]
        BrowserCdpError,
        default_profile_dir,
        launch_session_browser,
        open_url,
        read_cookies_with_retry,
        session_state,
        set_download_directory,
        wait_for_cdp,
    )
    from snapeda_client import (  # type: ignore[import-not-found,no-redef]
        DEFAULT_USER_AGENT,
        SnapedaClient,
        SnapedaCloudflareBlocked,
        SnapedaError,
        SnapedaPart,
        parse_cookie_header,
    )
    from snapeda_download import (  # type: ignore[import-not-found,no-redef]
        DownloadWatcher,
        SnapedaDownloader,
        user_downloads_dir,
    )

logger = logging.getLogger(__name__)

COLUMNS: list[tuple[str, int]] = [
    ("Part number", 190),
    ("Manufacturer", 150),
    ("Package", 110),
    ("CAD", 150),
    ("Datasheet", 70),
    ("Description", 320),
]

_IMAGE_MAX = 170
_AUTOCOMPLETE_MIN_CHARS = 3
_AUTOCOMPLETE_DELAY_MS = 400
_PAGE_SIZE = 20


APP_NAME = "KiCad Parts Hub"


def _bold(widget: wx.Window) -> None:
    font = widget.GetFont()
    font.MakeBold()
    widget.SetFont(font)


def _scaled(widget: wx.Window, factor: float) -> None:
    try:
        widget.SetFont(widget.GetFont().Scaled(factor))
    except Exception:  # noqa: BLE001 - cosmetic only
        pass


def _load_icon(plugin_dir: Path | None) -> wx.Icon | None:
    """Window icon: the 64 px artwork, not the 24 px toolbar image."""
    base = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
    for candidate in (
        base / "icon.png",  # 64 px, part of the installed payload
        base.parent / "resources" / "icon.png",
    ):
        if not candidate.is_file():
            continue
        try:
            return wx.Icon(str(candidate), wx.BITMAP_TYPE_PNG)
        except Exception:  # noqa: BLE001 - cosmetic only
            continue
    return None


def cse_download_dir(base: Path | None) -> Path:
    """Folder the Component Search Engine tab downloads into.

    It has to be its own folder: both tabs force the download folder of the same
    plugin browser, and each tab watches its own.
    """
    if base is None:
        return Path(tempfile.gettempdir()) / "kicad-parts-hub" / "cse"
    return Path(base) / "cse"


def _part_field(part: SnapedaPart, name: str) -> str:
    if name == "cad":
        return part.cad_summary
    if name == "datasheet":
        return "yes" if part.has_datasheet else ""
    return str(getattr(part, name, "") or "")


class SessionDialog(wx.Dialog):  # type: ignore[misc]
    """Get a site session into the plugin: plugin browser, browser files or paste.

    The panel supplies the wording and the site-specific hooks (``site_name``,
    ``preferred_cookie``, ``import_site_cookies()``, ``read_browser_cookies()``,
    ``save_site_session()``, ``clear_site_session()``, ``plugin_browser_state()``),
    so SnapEDA and Component Search Engine share one dialog.
    """

    def __init__(self, parent: wx.Window, panel: wx.Panel) -> None:
        super().__init__(
            parent,
            title=f"{panel.site_name} session",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=wx.Size(760, 620),
        )
        self.panel = panel
        icon = _load_icon(panel.plugin_dir)
        if icon is not None:
            self.SetIcon(icon)

        root = wx.BoxSizer(wx.VERTICAL)

        headline = wx.StaticText(self, label=panel.session_headline)
        _bold(headline)
        _scaled(headline, 1.1)
        root.Add(headline, 0, wx.LEFT | wx.RIGHT | wx.TOP, 10)
        root.Add(
            wx.StaticText(self, label=panel.session_intro),
            0,
            wx.LEFT | wx.RIGHT | wx.TOP | wx.BOTTOM,
            10,
        )

        self.status = wx.StaticText(self, label=panel.session_status_text())
        _bold(self.status)
        root.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- route 1: plugin browser (CDP) ---------------------------------
        box1 = wx.StaticBox(self, label=" 1 - Browser of the plugin (recommended) ")
        sizer1 = wx.StaticBoxSizer(box1, wx.VERTICAL)
        self.btn_open_browser = wx.Button(box1, label=panel.browser_button_label)
        self.btn_read_browser = wx.Button(box1, label="Read session from that browser")
        _bold(self.btn_open_browser)
        _bold(self.btn_read_browser)
        row1 = wx.BoxSizer(wx.HORIZONTAL)
        row1.Add(self.btn_open_browser, 0, wx.RIGHT, 6)
        row1.Add(self.btn_read_browser, 0)
        sizer1.Add(row1, 0, wx.ALL, 6)
        sizer1.Add(
            wx.StaticText(box1, label=panel.browser_help),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            6,
        )
        root.Add(sizer1, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- route 2: browser cookie files ---------------------------------
        box2 = wx.StaticBox(self, label=" 2 - Cookie files of Firefox / older Chrome/Edge ")
        sizer2 = wx.StaticBoxSizer(box2, wx.VERTICAL)
        self.btn_import = wx.Button(box2, label="Read cookies from browsers")
        sizer2.Add(self.btn_import, 0, wx.ALL, 6)
        sizer2.Add(
            wx.StaticText(box2, label=panel.files_help),
            0,
            wx.LEFT | wx.RIGHT | wx.BOTTOM,
            6,
        )
        root.Add(sizer2, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        # --- route 3: paste ------------------------------------------------
        box3 = wx.StaticBox(self, label=" 3 - Paste the Cookie header ")
        sizer3 = wx.StaticBoxSizer(box3, wx.VERTICAL)
        sizer3.Add(
            wx.StaticText(box3, label=panel.paste_help),
            0,
            wx.ALL,
            6,
        )
        self.paste = wx.TextCtrl(box3, style=wx.TE_MULTILINE)
        self.paste.SetMinSize(wx.Size(-1, 56))
        self.paste.SetHint(panel.paste_hint)
        sizer3.Add(self.paste, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 6)
        root.Add(sizer3, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 10)

        self.log = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.log.SetMinSize(wx.Size(-1, 84))
        root.Add(self.log, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, 10)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self.btn_forget = wx.Button(self, label="Forget saved session")
        bottom.Add(self.btn_forget, 0, wx.ALIGN_CENTER_VERTICAL)
        bottom.AddStretchSpacer()
        bottom.Add(self.CreateButtonSizer(wx.CLOSE), 0)
        root.Add(bottom, 0, wx.EXPAND | wx.ALL, 10)

        self.SetSizer(root)

        self.btn_open_browser.Bind(wx.EVT_BUTTON, self._on_open_browser)
        self.btn_read_browser.Bind(wx.EVT_BUTTON, self._on_read_browser)
        self.btn_import.Bind(wx.EVT_BUTTON, self._on_import_cookies)
        self.btn_forget.Bind(wx.EVT_BUTTON, self._on_forget)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CLOSE)
        self.Bind(wx.EVT_BUTTON, self._on_close, id=wx.ID_CANCEL)

        self._refresh_plugin_browser_state()

    # -- helpers -------------------------------------------------------

    def _write(self, message: str) -> None:
        self.log.AppendText(message + "\n")

    def _busy(self, busy: bool) -> None:
        for button in (self.btn_open_browser, self.btn_read_browser, self.btn_import):
            button.Enable(not busy)

    def _refresh_plugin_browser_state(self) -> None:
        state = self.panel.plugin_browser_state()
        if not state.get("running"):
            self.btn_read_browser.Disable()
            return
        self.btn_read_browser.Enable()
        cookies = state.get("cookies") or {}
        preferred = self.panel.preferred_cookie
        self._write(
            f"Plugin browser is running: {len(cookies)} {self.panel.site_name} cookies"
            + (f" ({preferred} present)" if preferred in cookies else f" (no {preferred} yet)")
            + ". If you are not logged in yet, do it in that window and read again."
        )

    # -- route 1 -------------------------------------------------------

    def _on_open_browser(self, _: wx.CommandEvent) -> None:
        self._busy(True)
        self._write(
            f"Opening the plugin browser window (log into {self.panel.cookie_domain} there) ..."
        )
        threading.Thread(target=self._open_browser_worker, daemon=True).start()

    def _open_browser_worker(self) -> None:
        try:
            self.panel.download_dir.mkdir(parents=True, exist_ok=True)
            session = launch_session_browser(
                port=CDP_PORT,
                profile_dir=self.panel.browser_profile_dir,
                # Seeded in the profile so the ZIPs of this window land where the
                # plugin watches them, not in the normal Downloads folder.
                download_dir=self.panel.download_dir,
            )
            ready = wait_for_cdp(CDP_PORT, timeout=45)
            if ready:
                try:
                    set_download_directory(CDP_PORT, self.panel.download_dir)
                except (BrowserCdpError, OSError) as exc:
                    logger.debug("Could not set the download folder over CDP: %s", exc)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing
            wx.CallAfter(self._open_browser_done, False, str(exc))
            return
        wx.CallAfter(self._open_browser_done, ready, f"pid {session.process.pid}")

    def _open_browser_done(self, ready: bool, detail: str) -> None:
        self._busy(False)
        if ready:
            self._write(f"Browser ready ({detail}). Log in, then press 'Read session'.")
            self.btn_read_browser.Enable()
        else:
            self._write(f"! The browser did not answer the DevTools endpoint ({detail}).")

    # -- route 2: CDP read ---------------------------------------------

    def _on_read_browser(self, _: wx.CommandEvent) -> None:
        self._busy(True)
        self._write(f"Reading the {self.panel.site_name} cookies from the plugin browser ...")
        threading.Thread(target=self._read_browser_worker, daemon=True).start()

    def _read_browser_worker(self) -> None:
        try:
            cookies = self.panel.read_browser_cookies()
        except (BrowserCdpError, OSError) as exc:
            wx.CallAfter(self._session_failed, f"Could not read the session: {exc}")
            return
        wx.CallAfter(self._session_ready, cookies, "plugin browser (CDP)")

    # -- route 3: cookie files -----------------------------------------

    def _on_import_cookies(self, _: wx.CommandEvent) -> None:
        self._busy(True)
        self._write("Scanning the cookie files of Firefox/Chrome/Edge ...")
        threading.Thread(target=self._import_worker, daemon=True).start()

    def _import_worker(self) -> None:
        try:
            result = self.panel.import_site_cookies()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the dialog
            wx.CallAfter(self._session_failed, f"Unexpected error: {exc}")
            return
        for warning in result.warnings:
            wx.CallAfter(self._write, f"! {warning}")
        wx.CallAfter(self._session_ready, result.cookies, result.source)

    # -- shared result handling ----------------------------------------

    def _session_failed(self, message: str) -> None:
        self._busy(False)
        self._write(f"! {message}")
        self._write(
            "Tip: route 1 (plugin browser) works with every Chrome/Edge version, "
            "including the ones with App-Bound Encryption."
        )

    def _session_ready(self, cookies: dict[str, str], source: str) -> None:
        self._busy(False)
        panel = self.panel
        if not cookies:
            self._write(
                f"No {panel.site_name} cookies found. Log into {panel.cookie_domain} and try again."
            )
            return
        self._write(f"Got {len(cookies)} cookies from {source}: {', '.join(sorted(cookies))}")
        if panel.preferred_cookie not in cookies:
            self._write(
                f"! No {panel.preferred_cookie} cookie yet: Cloudflare will keep blocking requests."
            )
            self._write(
                f"  Open {panel.cookie_domain} in the plugin window, then read the session again."
            )
        else:
            self._write(f"Session looks complete: {panel.preferred_cookie} present.")
        path = panel.save_site_session(cookies, source)
        self.panel.set_client_cookies(cookies, source)
        self._write(f"Saved to {path}. The Import button can download now.")
        self.status.SetLabel(self.panel.session_status_text())
        self.Layout()

    def _on_forget(self, _: wx.CommandEvent) -> None:
        self.panel.clear_site_session()
        self.panel.set_client_cookies({}, "")
        self.status.SetLabel(self.panel.session_status_text())
        self._write("Saved session removed.")

    def _on_close(self, event: wx.CommandEvent) -> None:
        text = self.paste.GetValue().strip()
        if text:
            cookies = parse_cookie_header(text)
            kept = {k: v for k, v in cookies.items() if v}
            if kept:
                self.panel.save_site_session(kept, "pasted")
                self.panel.set_client_cookies(kept, "pasted")
                self._write(f"Saved {len(kept)} pasted cookies.")
        event.Skip()
        self.EndModal(wx.ID_CLOSE)


class SnapedaPanel(wx.Panel):  # type: ignore[misc]
    """Search SnapEDA and hand a downloaded ZIP to the caller."""

    # Site profile used by the shared SessionDialog.
    site_name = "SnapEDA"
    cookie_domain = "snapeda.com"
    preferred_cookie = "cf_clearance"
    session_headline = "Downloading from SnapEDA needs a logged-in session"
    session_intro = (
        "Searching works without login. The part page and the download are behind\n"
        "Cloudflare, so the plugin needs the cookies cf_clearance + sessionid.\n"
        "Chrome/Edge 127 and newer encrypt their cookie files (App-Bound), so use\n"
        "route 1 - the plugin's own browser window - which works with any version."
    )
    browser_button_label = "Open SnapEDA browser window"
    browser_help = (
        "Opens its own Chrome/Edge window with a separate profile: log into\n"
        "snapeda.com there once, then press 'Read session'. Your normal browser\n"
        "stays untouched and the login is remembered for next time."
    )
    files_help = (
        "Firefox needs nothing else. For Chrome/Edge the browser must be fully\n"
        "closed (Windows locks the file) and its cookies must not use App-Bound\n"
        "encryption - otherwise use route 1 or 3."
    )
    paste_help = (
        "In your normal browser: F12 -> Network -> reload snapeda.com -> click a\n"
        "request -> Request Headers -> copy the whole 'cookie:' line."
    )
    paste_hint = "cf_clearance=...; sessionid=...; csrftoken=..."

    def __init__(
        self,
        parent: wx.Window,
        *,
        plugin_dir: Path | None = None,
        download_dir: Path | None = None,
        on_import_zip: Callable[[Path, SnapedaPart], None] | None = None,
        on_imported: Callable[[SnapedaPart, str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.plugin_dir = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
        self.download_dir = (
            Path(download_dir)
            if download_dir
            else Path(tempfile.gettempdir()) / "kicad-parts-hub" / "snapeda"
        )
        self.on_import_zip = on_import_zip
        self.on_imported = on_imported

        cookies, source = load_session()
        self.client = SnapedaClient(cookies)
        self.downloader = SnapedaDownloader(self.client, self.download_dir)
        self.session_source = source
        self.watcher = DownloadWatcher(self.download_dir)
        # Fallback for browsers that ignore the forced download folder (or an
        # already running browser window): the user's own Downloads folder is
        # watched only while a download is expected from a part page.
        self._downloads_watcher: DownloadWatcher | None = None
        self._await_download_until = 0.0

        self._results: list[SnapedaPart] = []
        self._sort_col = -1
        self._sort_asc = True
        self._search_id = 0
        self._image_id = 0
        self._auto_id = 0
        self._image_cache: dict[str, bytes] = {}
        self._filling_choices = False
        self._browser_launched = False

        self._build_ui()
        self._refresh_session_status()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    @property
    def browser_profile_dir(self) -> Path:
        """Profile of the browser window the plugin opens for the SnapEDA login."""
        return default_profile_dir()

    # -- session hooks shared with the SessionDialog --------------------

    def import_site_cookies(self) -> Any:
        return import_cookies_from_browsers()

    def read_browser_cookies(self) -> dict[str, str]:
        return read_cookies_with_retry(CDP_PORT, domain_filter=self.cookie_domain)

    def save_site_session(self, cookies: dict[str, str], source: str = "") -> Path:
        return save_session(cookies, source)

    def clear_site_session(self) -> None:
        clear_session()

    def plugin_browser_state(self) -> dict[str, object]:
        return session_state(CDP_PORT, domain_filter=self.cookie_domain)

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        search_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_ctrl = wx.ComboBox(self, style=wx.CB_DROPDOWN | wx.TE_PROCESS_ENTER, choices=[])
        self.search_ctrl.SetHint("Search a part number on SnapEDA (e.g. STM32F103C8T6)")
        _scaled(self.search_ctrl, 1.1)
        self.btn_search = wx.Button(self, label="Search")
        self.btn_session = wx.Button(self, label="Session / Login")
        self.btn_page = wx.Button(self, label="Download in browser")
        self.btn_page.SetToolTip(
            "Open the selected part in the SnapEDA browser window; the ZIP it downloads is "
            "imported automatically"
        )
        self.btn_page.Disable()
        self.btn_import = wx.Button(self, label="Import ZIP...")
        self.btn_import.SetToolTip("Import a KiCad ZIP you downloaded yourself (any browser)")
        self.btn_folder = wx.Button(self, label="Downloads folder")
        for button in (self.btn_search, self.btn_session, self.btn_page):
            _bold(button)

        search_row.Add(self.search_ctrl, 1, wx.EXPAND | wx.RIGHT, 4)
        search_row.Add(self.btn_search, 0, wx.RIGHT, 4)
        search_row.Add(self.btn_session, 0, wx.RIGHT, 4)
        search_row.Add(self.btn_page, 0, wx.RIGHT, 4)
        search_row.Add(self.btn_import, 0, wx.RIGHT, 4)
        search_row.Add(self.btn_folder, 0)
        root.Add(search_row, 0, wx.EXPAND | wx.ALL, 8)

        hint = wx.StaticText(
            self,
            label=(
                "SnapEDA: search here, then 'Download in browser' -> press Download on the part "
                "page. The ZIP is picked up and imported automatically."
            ),
        )
        root.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        splitter = wx.SplitterWindow(self, style=wx.SP_LIVE_UPDATE | wx.SP_3DSASH)
        splitter.SetMinimumPaneSize(80)
        self.list_ctrl = wx.ListCtrl(
            splitter, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN
        )
        for index, (label, width) in enumerate(COLUMNS):
            self.list_ctrl.InsertColumn(index, label, width=width)
        try:
            self.list_ctrl.EnableAlternateRowColours(True)
        except Exception:  # noqa: BLE001 - theme dependent, cosmetic only
            logger.debug("Alternate row colours not available")
        self.detail_panel = self._build_detail(splitter)
        splitter.SplitHorizontally(self.list_ctrl, self.detail_panel, sashPosition=-240)
        root.Add(splitter, 1, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.status = wx.StaticText(self, label="")
        _bold(self.status)
        root.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(root)

        self.btn_search.Bind(wx.EVT_BUTTON, self._on_search)
        self.search_ctrl.Bind(wx.EVT_TEXT_ENTER, self._on_search)
        self.search_ctrl.Bind(wx.EVT_TEXT, self._on_text)
        self.btn_session.Bind(wx.EVT_BUTTON, self._on_session)
        self.btn_page.Bind(wx.EVT_BUTTON, self._on_browse_part)
        self.btn_import.Bind(wx.EVT_BUTTON, self._on_pick_zip)
        self.btn_folder.Bind(wx.EVT_BUTTON, self._on_open_folder)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selected)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_browse_part)
        self.list_ctrl.Bind(wx.EVT_LIST_COL_CLICK, self._on_col_click)

        self._autocomplete_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_autocomplete_timer, self._autocomplete_timer)

        # Hybrid flow: watch the folder the plugin browser downloads into.
        self._watch_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_watch, self._watch_timer)
        self._watch_timer.Start(2000)

    def _build_detail(self, parent: wx.Window) -> wx.ScrolledWindow:
        panel = wx.ScrolledWindow(parent, style=wx.VSCROLL)
        panel.SetScrollRate(0, 12)
        self._detail_panel = panel

        self._image_bitmap = wx.StaticBitmap(panel)
        self._image_bitmap.Hide()

        self._detail_grid = wx.FlexGridSizer(cols=2, vgap=4, hgap=8)
        self._detail_grid.AddGrowableCol(1, 1)

        outer = wx.BoxSizer(wx.HORIZONTAL)
        outer.Add(self._detail_grid, 1, wx.EXPAND | wx.ALL, 4)
        outer.Add(self._image_bitmap, 0, wx.ALIGN_TOP | wx.ALL, 4)
        panel.SetSizer(outer)
        self._detail_widgets: list[wx.Window] = []
        return panel

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    def session_status_text(self) -> str:
        cookies = self.client.cookies()
        parts = []
        if self.session_source:
            parts.append(f"source: {self.session_source}")
        parts.append("cf_clearance: " + ("yes" if "cf_clearance" in cookies else "no"))
        parts.append("sessionid: " + ("yes" if "sessionid" in cookies else "no"))
        state = "search only" if "cf_clearance" not in cookies else "search + downloads"
        return f"SnapEDA session - {', '.join(parts)} ({state})"

    def set_client_cookies(self, cookies: dict[str, str], source: str = "") -> None:
        self.client = SnapedaClient(cookies)
        self.downloader = SnapedaDownloader(self.client, self.download_dir)
        self.session_source = source

    def _refresh_session_status(self) -> None:
        self._set_status(self.session_status_text())

    def _on_session(self, _: wx.CommandEvent) -> None:
        dialog = SessionDialog(self, self)
        dialog.ShowModal()
        dialog.Destroy()
        self._refresh_session_status()

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def _on_text(self, _: wx.CommandEvent) -> None:
        if self._filling_choices:  # our own update, not the user typing
            return
        self._autocomplete_timer.Start(_AUTOCOMPLETE_DELAY_MS, oneShot=True)

    def _on_autocomplete_timer(self, _: wx.TimerEvent) -> None:
        keyword = self.search_ctrl.GetValue().strip()
        if len(keyword) < _AUTOCOMPLETE_MIN_CHARS:
            return
        self._auto_id += 1
        request_id = self._auto_id
        threading.Thread(
            target=self._autocomplete_worker, args=(keyword, request_id), daemon=True
        ).start()

    def _autocomplete_worker(self, keyword: str, request_id: int) -> None:
        try:
            suggestions = self.client.autocomplete(keyword)
        except Exception as exc:  # noqa: BLE001 - suggestions are optional
            logger.debug("Autocomplete failed: %s", exc)
            return
        wx.CallAfter(self._apply_suggestions, request_id, suggestions)

    def _apply_suggestions(self, request_id: int, suggestions: list[str]) -> None:
        if request_id != self._auto_id or not suggestions:
            return
        current = self.search_ctrl.GetValue()
        # Only fill the drop-down list; opening it programmatically made the list
        # flicker open/closed while typing, so the user opens it with the arrow.
        self._filling_choices = True
        try:
            self.search_ctrl.Freeze()
            self.search_ctrl.Set(suggestions)
            self.search_ctrl.SetValue(current)
            self.search_ctrl.Thaw()
        finally:
            self._filling_choices = False

    def _on_search(self, _: wx.CommandEvent) -> None:
        keyword = self.search_ctrl.GetValue().strip()
        if not keyword:
            return
        self._search_id += 1
        request_id = self._search_id
        self.btn_search.Disable()
        self.list_ctrl.DeleteAllItems()
        self._results = []
        self._set_status(f"Searching SnapEDA for '{keyword}' ...")
        threading.Thread(
            target=self._search_worker, args=(keyword, request_id), daemon=True
        ).start()

    def _search_worker(self, keyword: str, request_id: int) -> None:
        try:
            result = self.client.search(keyword, page=1)
        except SnapedaCloudflareBlocked as exc:
            wx.CallAfter(self._search_failed, request_id, f"Cloudflare blocked the search: {exc}")
        except (SnapedaError, ValueError) as exc:
            wx.CallAfter(self._search_failed, request_id, f"Search failed: {exc}")
        else:
            wx.CallAfter(self._search_done, request_id, result.parts, result.total_hits)

    def _search_failed(self, request_id: int, message: str) -> None:
        if request_id != self._search_id:
            return
        self.btn_search.Enable()
        self._set_status(message)

    def _search_done(self, request_id: int, parts: list[SnapedaPart], hits: int) -> None:
        if request_id != self._search_id:
            return
        self.btn_search.Enable()
        self._results = parts
        self._populate()
        limit = (
            " (page 1 of the results; refine the search to see others)" if hits > _PAGE_SIZE else ""
        )
        self._set_status(f"{len(parts)} of {hits} parts shown{limit}.")

    def _populate(self) -> None:
        self.list_ctrl.DeleteAllItems()
        self.btn_page.Disable()
        for part in self._results:
            index = self.list_ctrl.InsertItem(self.list_ctrl.GetItemCount(), part.part_number)
            self.list_ctrl.SetItem(index, 1, part.manufacturer)
            self.list_ctrl.SetItem(index, 2, part.package_type)
            self.list_ctrl.SetItem(index, 3, part.cad_summary)
            self.list_ctrl.SetItem(index, 4, "yes" if part.has_datasheet else "")
            self.list_ctrl.SetItem(index, 5, part.description)

    def _on_col_click(self, event: wx.ListEvent) -> None:
        column = event.GetColumn()
        if self._sort_col == column:
            self._sort_asc = not self._sort_asc
        else:
            self._sort_col = column
            self._sort_asc = True
        keys = ["part_number", "manufacturer", "package_type", "cad", "datasheet", "description"]
        key = keys[column] if column < len(keys) else "part_number"
        self._results.sort(
            key=lambda part: _part_field(part, key).lower(), reverse=not self._sort_asc
        )
        self._populate()

    # ------------------------------------------------------------------
    # selection / detail
    # ------------------------------------------------------------------

    def _selected_part(self) -> SnapedaPart | None:
        index = self.list_ctrl.GetFirstSelected()
        if index == wx.NOT_FOUND or index >= len(self._results):
            return None
        return self._results[index]

    def _on_selected(self, _: wx.ListEvent) -> None:
        part = self._selected_part()
        if part is None:
            return
        self.btn_page.Enable(bool(part.part_url))
        self._show_detail(part)
        for url in (part.image, part.symbol_image, part.footprint_image, part.model_image):
            if url:
                self._image_id += 1
                threading.Thread(
                    target=self._image_worker, args=(url, self._image_id), daemon=True
                ).start()
                break

    def _show_detail(self, part: SnapedaPart) -> None:
        panel = self._detail_panel
        self._image_bitmap.Hide()
        for widget in self._detail_widgets:
            widget.Destroy()
        self._detail_widgets.clear()
        self._detail_grid.Clear()
        wrap = max(240, panel.GetClientSize().width - _IMAGE_MAX - 40)

        rows: list[tuple[str, str]] = [
            ("Part number", part.part_number),
            ("Manufacturer", part.manufacturer),
            ("Package", part.package_type),
            ("CAD", part.cad_summary),
            ("Description", part.description),
            ("Distributors", ", ".join(part.distributors[:6])),
            ("Formats cached by SnapEDA", ", ".join(part.cached_formats)),
            ("Part URL", part.part_url),
        ]
        if part.datasheet_url:
            rows.append(("Datasheet", part.datasheet_url))

        for label, value in rows:
            if not value:
                continue
            text = wx.StaticText(panel, label=f"{label}:")
            font = text.GetFont()
            font.MakeBold()
            text.SetFont(font)
            self._detail_grid.Add(text, 0, wx.ALIGN_CENTER_VERTICAL)
            if value.startswith("http"):
                widget: wx.Window = wx.adv.HyperlinkCtrl(panel, label=value[:70], url=value)
            else:
                widget = wx.StaticText(panel, label=value)
                widget.Wrap(wrap)
            self._detail_grid.Add(widget, 1, wx.ALIGN_LEFT | wx.ALIGN_CENTER_VERTICAL)
            self._detail_widgets += [text, widget]

        self._detail_grid.Layout()
        panel.FitInside()
        panel.Layout()

    def _image_worker(self, url: str, request_id: int) -> None:
        data = self._image_cache.get(url)
        if data is None:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": DEFAULT_USER_AGENT})
                with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                    data = response.read()
                self._image_cache[url] = data
            except Exception as exc:  # noqa: BLE001 - previews are best effort
                logger.debug("Preview %s failed: %s", url, exc)
                data = b""
        wx.CallAfter(self._show_image, request_id, data)

    def _show_image(self, request_id: int, data: bytes) -> None:
        if request_id != self._image_id or not data:
            return
        try:
            image = wx.Image(io.BytesIO(data), wx.BITMAP_TYPE_ANY)  # pyright: ignore[reportArgumentType,reportCallIssue]
        except Exception:  # noqa: BLE001
            return
        if not image.IsOk():
            return
        width, height = image.GetWidth(), image.GetHeight()
        if width > _IMAGE_MAX or height > _IMAGE_MAX:
            scale = _IMAGE_MAX / max(width, height)
            image = image.Scale(int(width * scale), int(height * scale), wx.IMAGE_QUALITY_HIGH)
        self._image_bitmap.SetBitmap(wx.Bitmap(image))  # pyright: ignore[reportArgumentType]
        self._image_bitmap.Show()
        self._detail_panel.FitInside()
        self._detail_panel.Layout()

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def focus_search(self) -> None:
        """Give the search field the focus (call after the window is shown)."""
        try:
            self.search_ctrl.SetFocus()
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not focus the search field", exc_info=True)

    def _on_browse_part(self, _: wx.Event) -> None:
        """Open the selected part in the plugin browser; its download gets imported."""
        part = self._selected_part()
        if part is None or not part.part_url:
            return
        self.download_dir.mkdir(parents=True, exist_ok=True)
        # From here on a ZIP is expected, so the user's Downloads folder is watched too.
        self.expect_download()
        self._set_status(f"Opening {part.part_number} in the SnapEDA browser ...")
        threading.Thread(target=self._browse_worker, args=(part,), daemon=True).start()

    def expect_download(self, seconds: float = 1800.0) -> None:
        """Arm the fallback watcher for the next ``seconds`` (30 min)."""
        self._await_download_until = time.monotonic() + seconds
        if self._downloads_watcher is None:
            self._downloads_watcher = DownloadWatcher(
                user_downloads_dir(), ignore_before=time.time(), require_kicad=True
            )

    def _browse_worker(self, part: SnapedaPart) -> None:
        try:
            state = session_state(CDP_PORT)
            if not state.get("running"):
                launch_session_browser(
                    port=CDP_PORT,
                    profile_dir=self.browser_profile_dir,
                    url=part.part_url,
                    download_dir=self.download_dir,
                )
                if not wait_for_cdp(CDP_PORT, timeout=45):
                    wx.CallAfter(
                        self._browse_done, part, False, "the browser did not start in time"
                    )
                    return
            else:
                open_url(CDP_PORT, part.part_url)
            set_download_directory(CDP_PORT, self.download_dir)
            wx.CallAfter(self._browse_done, part, True, "")
        except (BrowserCdpError, OSError) as exc:
            wx.CallAfter(self._browse_done, part, False, str(exc))

    def _browse_done(self, part: SnapedaPart, ok: bool, detail: str) -> None:
        if ok:
            self._set_status(
                f"{part.part_number} open in the SnapEDA browser - press Download there, "
                f"the ZIP is imported automatically into {self.download_dir.name}/"
            )
        else:
            self._set_status(f"Could not open the browser ({detail}); opening your default one.")
            SnapedaDownloader.open_part_in_browser(part)

    # ------------------------------------------------------------------
    # hybrid import: ZIPs that land in the download folder
    # ------------------------------------------------------------------

    def _on_watch(self, _: wx.TimerEvent) -> None:
        if self.on_import_zip is None:
            return
        for zip_path in self.watcher.scan():
            self._import_downloaded(zip_path)
        if time.monotonic() >= self._await_download_until:
            return
        if self._downloads_watcher is None:
            return
        for zip_path in self._downloads_watcher.scan():
            self._import_downloaded(zip_path, watched=self._downloads_watcher)

    def _import_downloaded(self, zip_path: Path, *, watched: DownloadWatcher | None = None) -> None:
        part = self._part_for_zip(zip_path)
        self._set_status(f"Downloaded ZIP detected: {zip_path.name} - importing ...")
        assert self.on_import_zip is not None
        try:
            self.on_import_zip(zip_path, part)
        except Exception as exc:  # noqa: BLE001 - report, keep watching
            logger.exception("Import of %s failed", zip_path)
            self._set_status(f"Import of {zip_path.name} failed: {exc}")
            (watched or self.watcher).forget(zip_path)
            return
        archived = self._archive_imported(zip_path)
        self._set_status(f"Imported {zip_path.name} ({part.part_number})")
        if self.on_imported is not None:
            self.on_imported(part, f"SnapEDA ZIP -> {archived}")

    def _archive_imported(self, zip_path: Path) -> str:
        """Move an imported ZIP out of the watched folder so it is not seen again."""
        destination = self.download_dir / "imported"
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / zip_path.name
            if target.exists():
                target = destination / f"{zip_path.stem}-{int(time.time())}{zip_path.suffix}"
            shutil.move(str(zip_path), str(target))
            return f"{destination.name}/{target.name}"
        except OSError as exc:
            logger.warning("Could not move %s into imported/: %s", zip_path, exc)
            return zip_path.name

    def _part_for_zip(self, zip_path: Path) -> SnapedaPart:
        """Match a downloaded ZIP to a search result so the log shows the part."""
        stem = zip_path.stem.lower()
        for part in self._results:
            name = part.part_number.lower()
            if name and (name == stem or stem.startswith(name) or name.startswith(stem)):
                return part
        return SnapedaPart(part_number=zip_path.stem, manufacturer="")

    def _on_pick_zip(self, _: wx.CommandEvent) -> None:
        """Manual route: import a ZIP downloaded with any browser."""
        if self.on_import_zip is None:
            return
        with wx.FileDialog(
            self,
            "Select the KiCad ZIP downloaded from SnapEDA",
            wildcard="KiCad ZIP (*.zip)|*.zip|All files (*.*)|*.*",
            style=wx.FD_OPEN | wx.FD_FILE_MUST_EXIST,
        ) as dialog:
            if dialog.ShowModal() != wx.ID_OK:
                return
            zip_path = Path(dialog.GetPath())
            self.watcher.forget(zip_path)
            self._import_downloaded(zip_path)

    def _on_open_folder(self, _: wx.CommandEvent) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultApplication(str(self.download_dir))

    def _set_status(self, message: str) -> None:
        self.status.SetLabel(message)
        self.Layout()

    def apply_download_dir(self, path: Path) -> None:
        """Called by the frontend when KiCad's library folder changes."""
        self.download_dir = Path(path)
        self.downloader = SnapedaDownloader(self.client, self.download_dir)
        self.watcher = DownloadWatcher(self.download_dir)
        self._downloads_watcher = None


class SnapedaSearchDialog(wx.Dialog):  # type: ignore[misc]
    """Dialog wrapper so the SnapEDA search can be opened from the main GUI."""

    def __init__(
        self,
        parent: wx.Window,
        *,
        plugin_dir: Path | None = None,
        download_dir: Path | None = None,
        on_import_zip: Callable[[Path, SnapedaPart], None] | None = None,
        on_imported: Callable[[SnapedaPart, str], None] | None = None,
    ) -> None:
        super().__init__(
            parent,
            title=f"{APP_NAME} - SnapEDA search",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=wx.Size(940, 660),
        )
        icon = _load_icon(plugin_dir)
        if icon is not None:
            self.SetIcon(icon)
        self.panel = SnapedaPanel(
            self,
            plugin_dir=plugin_dir,
            download_dir=download_dir,
            on_import_zip=on_import_zip,
            on_imported=on_imported,
        )
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(self.panel, 1, wx.EXPAND)
        self.SetSizer(root)
        self.Centre()


class CombinedSearchDialog(wx.Dialog):  # type: ignore[misc]
    """One search window with a tab per source: SnapEDA and EasyEDA/LCSC.

    Shown non-modally on purpose: a modal dialog stacked on the (modal) main
    window swallowed the first click on every button. Each tab now owns its
    import path - EasyEDA calls ``on_import_easyeda`` with the LCSC number,
    SnapEDA imports the ZIPs it downloads through ``on_import_zip``.
    """

    def __init__(
        self,
        parent: wx.Window,
        *,
        plugin_dir: Path | None = None,
        download_dir: Path | None = None,
        on_select_lcsc: Callable[[str], None] | None = None,
        on_import_zip: Callable[[Path, SnapedaPart], None] | None = None,
        on_import_easyeda: Callable[[str], None] | None = None,
        on_snapeda_imported: Callable[[SnapedaPart, str], None] | None = None,
        on_import_cse: Callable[[Path, Any], None] | None = None,
        on_cse_imported: Callable[[Any, str], None] | None = None,
        library_dir: Path | None = None,
        on_install_libraries: Callable[[], str] | None = None,
        on_library_changed: Callable[[], None] | None = None,
        initial_tab: int = 0,
    ) -> None:
        self.on_import_easyeda = on_import_easyeda
        super().__init__(
            parent,
            title=f"{APP_NAME} - SnapEDA + Component Search Engine + EasyEDA/LCSC",
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
            size=wx.Size(1000, 700),
        )
        icon = _load_icon(plugin_dir)
        if icon is not None:
            self.SetIcon(icon)
        book = wx.Notebook(self)

        self.snapeda_panel = SnapedaPanel(
            book,
            plugin_dir=plugin_dir,
            download_dir=download_dir,
            on_import_zip=on_import_zip,
            on_imported=on_snapeda_imported,
        )
        book.AddPage(self.snapeda_panel, "SnapEDA")

        self.cse_panel: Any = None
        try:
            try:
                from .cse_search import CsePanel
            except ImportError:
                from cse_search import CsePanel  # type: ignore[import-not-found,no-redef]

            self.cse_panel = CsePanel(
                book,
                plugin_dir=plugin_dir,
                download_dir=cse_download_dir(download_dir),
                on_import_zip=on_import_cse,
                on_imported=on_cse_imported,
            )
            book.AddPage(self.cse_panel, "Component Search Engine")
        except Exception as exc:  # noqa: BLE001 - the other tabs must keep working
            logger.warning("Component Search Engine tab unavailable: %s", exc)

        self.easyeda_panel: Any = None
        try:
            try:
                from .component_search import SearchPanel
            except ImportError:
                from component_search import SearchPanel  # type: ignore[import-not-found,no-redef]

            self.easyeda_panel = SearchPanel(
                book,
                on_select=on_select_lcsc,
                on_import=self._import_easyeda,
            )
            book.AddPage(self.easyeda_panel, "EasyEDA / LCSC")
        except Exception as exc:  # noqa: BLE001 - SnapEDA tab must keep working
            logger.warning("EasyEDA search tab unavailable: %s", exc)

        self.library_panel: Any = None
        try:
            try:
                from .library_manager import LibraryPanel
            except ImportError:
                from library_manager import LibraryPanel  # type: ignore[import-not-found,no-redef]

            self.library_panel = LibraryPanel(
                book,
                root_dir=library_dir,
                on_install=on_install_libraries,
                on_changed=on_library_changed,
            )
            book.AddPage(self.library_panel, "Library")
        except Exception as exc:  # noqa: BLE001 - the search tabs must keep working
            logger.warning("Library tab unavailable: %s", exc)

        self.book = book
        book.SetSelection(max(0, min(initial_tab, book.GetPageCount() - 1)))
        root = wx.BoxSizer(wx.VERTICAL)
        root.Add(book, 1, wx.EXPAND)
        self.SetSizer(root)
        self.Centre()

    def select_tab(self, index: int) -> None:
        """Switch tabs on an already open window (0=SnapEDA, 1=CSE, 2=EasyEDA, 3=Library)."""
        try:
            self.book.SetSelection(max(0, min(index, self.book.GetPageCount() - 1)))
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not switch the search tab", exc_info=True)

    def get_lcsc(self) -> str | None:
        if self.easyeda_panel is None:
            return None
        return self.easyeda_panel.get_selected_lcsc()

    def _import_easyeda(self, lcsc: str | None) -> None:
        """EasyEDA tab pressed Import: hand the LCSC number to the main window."""
        number = lcsc or self.get_lcsc()
        if not number or self.on_import_easyeda is None:
            return
        self.on_import_easyeda(number)

    def set_library_dir(self, library_dir: Path | None) -> None:
        """Point the Library tab at the destination the main window currently uses."""
        if self.library_panel is not None:
            try:
                self.library_panel.set_root(library_dir)
            except Exception:  # noqa: BLE001 - the tab is a convenience
                logger.debug("Could not set the library folder", exc_info=True)

    def refresh_library(self) -> None:
        """Re-read the library tab after an import (call from the import callback)."""
        if self.library_panel is not None:
            try:
                self.library_panel.refresh()
            except Exception:  # noqa: BLE001 - the tab is a convenience
                logger.debug("Could not refresh the library tab", exc_info=True)

    def focus_search(self) -> None:
        """Focus the search box of the active tab (call after Show())."""
        try:
            book = next(child for child in self.GetChildren() if isinstance(child, wx.Notebook))
        except StopIteration:  # pragma: no cover - notebook always exists
            return
        page = book.GetCurrentPage()
        if isinstance(page, SnapedaPanel):
            page.focus_search()
        elif self.cse_panel is not None and page is self.cse_panel:
            self.cse_panel.focus_search()
        elif self.easyeda_panel is not None and page is self.easyeda_panel:
            try:
                page.search_ctrl.SetFocus()
            except Exception:  # noqa: BLE001 - EasyEDA panel layout differs
                logger.debug("Could not focus the EasyEDA search field", exc_info=True)


if __name__ == "__main__":  # pragma: no cover - manual UI check
    logging.basicConfig(level=logging.INFO)
    app = wx.App(False)
    frame = wx.Frame(None, title="SnapEDA search (test)")
    frame.panel = SnapedaPanel(frame, on_import_zip=lambda path, part: print("import", path, part))
    sizer = wx.BoxSizer(wx.VERTICAL)
    sizer.Add(frame.panel, 1, wx.EXPAND)
    frame.SetSizer(sizer)
    frame.SetSize(wx.Size(900, 600))
    frame.Show()
    app.MainLoop()
