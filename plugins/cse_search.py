"""Component Search Engine (Samacsys) search panel.

The same flow as the SnapEDA tab, for componentsearchengine.com:

* search through :mod:`cse_client` (needs the Cloudflare cookie ``__cf_bm``),
* session handling through :mod:`cse_auth` (the plugin browser, browser cookie
  files, or a pasted ``Cookie:`` header - exactly like SnapEDA),
* "Download in browser": the part page opens in the plugin browser, whose
  downloads land in the folder this panel watches, so the ZIP is imported and
  KiCad configured automatically,
* import of the downloaded ZIP is delegated to the caller (``on_import_zip``),
  which uses the existing :class:`LibImporter` (``Samacsys`` library).

Can be run standalone for a quick UI check:
``python plugins/cse_search.py`` (needs KiCad's Python, which ships wx).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

import wx

try:  # imported as part of the plugin package
    from .cse_auth import clear_session as clear_cse_session
    from .cse_auth import import_cookies_from_browsers as import_cse_cookies
    from .cse_auth import load_session as load_cse_session
    from .cse_auth import save_session as save_cse_session
    from .cse_client import (
        CseBlocked,
        CseClient,
        CseError,
        CsePart,
        CseSearchResult,
        parts_from_search_html,
    )
    from .snapeda_browser import DEFAULT_PORT as CDP_PORT
    from .snapeda_browser import (
        BrowserCdpError,
        browser_user_agent,
        default_profile_dir,
        delete_cookies_in_browser,
        fetch_page_html,
        launch_session_browser,
        open_url,
        read_cookies_with_retry,
        session_state,
        set_cookies_in_browser,
        set_download_directory,
        wait_for_cdp,
    )
    from .snapeda_download import DownloadWatcher, user_downloads_dir
    from .snapeda_search import SessionDialog, _bold, _load_icon, _scaled
except ImportError:  # standalone / sys.path import (tests, CLI)
    from cse_auth import (  # type: ignore[import-not-found,no-redef]
        clear_session as clear_cse_session,
    )
    from cse_auth import (
        import_cookies_from_browsers as import_cse_cookies,
    )
    from cse_auth import (
        load_session as load_cse_session,
    )
    from cse_auth import (
        save_session as save_cse_session,
    )
    from cse_client import (  # type: ignore[import-not-found,no-redef]
        CseBlocked,
        CseClient,
        CseError,
        CsePart,
        CseSearchResult,
        parts_from_search_html,
    )
    from snapeda_browser import DEFAULT_PORT as CDP_PORT  # type: ignore[no-redef]
    from snapeda_browser import (  # type: ignore[import-not-found,no-redef]
        BrowserCdpError,
        browser_user_agent,
        default_profile_dir,
        delete_cookies_in_browser,
        fetch_page_html,
        launch_session_browser,
        open_url,
        read_cookies_with_retry,
        session_state,
        set_cookies_in_browser,
        set_download_directory,
        wait_for_cdp,
    )
    from snapeda_download import (  # type: ignore[import-not-found,no-redef]
        DownloadWatcher,
        user_downloads_dir,
    )
    from snapeda_search import (  # type: ignore[import-not-found,no-redef]
        SessionDialog,
        _bold,
        _load_icon,
        _scaled,
    )

logger = logging.getLogger(__name__)

COLUMNS: list[tuple[str, int]] = [
    ("Part number", 200),
    ("Manufacturer", 160),
    ("Package", 110),
    ("Model", 130),
    ("Datasheet", 70),
    ("Description", 300),
]

_IMAGE_MAX = 170
PAGE_SIZE = 25  # fixed by the site
BASE_SEARCH_URL = "https://componentsearchengine.com/search"


class CsePanel(wx.Panel):  # type: ignore[misc]
    """Search Component Search Engine and hand a downloaded ZIP to the caller."""

    # Site profile used by the shared SessionDialog.
    site_name = "Component Search Engine"
    cookie_domain = "componentsearchengine.com"
    preferred_cookie = "__cf_bm"
    session_headline = "Downloading from Component Search Engine needs a free account"
    session_intro = (
        "Searching works without login as long as Cloudflare trusts the session, so the\n"
        "plugin needs the __cf_bm cookie of a real browser. Downloading a model needs a\n"
        "free CSE account. Chrome/Edge 127 and newer encrypt their cookie files\n"
        "(App-Bound), so use route 1 - the plugin's own browser window."
    )
    browser_button_label = "Open CSE browser window"
    browser_help = (
        "Opens its own Chrome/Edge window with a separate profile: open\n"
        "componentsearchengine.com there and sign in (registration is free and only\n"
        "needed to download). Then press 'Read session'; the login is remembered."
    )
    files_help = (
        "Firefox needs nothing else. For Chrome/Edge the browser must be fully\n"
        "closed (Windows locks the file) and its cookies must not use App-Bound\n"
        "encryption - otherwise use route 1 or 3."
    )
    paste_help = (
        "In your normal browser: F12 -> Network -> reload componentsearchengine.com ->\n"
        "click a request -> Request Headers -> copy the whole 'cookie:' line."
    )
    paste_hint = "__cf_bm=...; connect.sid=...; _csrf=..."

    def __init__(
        self,
        parent: wx.Window,
        *,
        plugin_dir: Path | None = None,
        download_dir: Path | None = None,
        on_import_zip: Callable[[Path, CsePart], None] | None = None,
        on_imported: Callable[[CsePart, str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.plugin_dir = Path(plugin_dir) if plugin_dir else Path(__file__).resolve().parent
        self.download_dir = (
            Path(download_dir)
            if download_dir
            else Path(tempfile.gettempdir()) / "kicad-parts-hub" / "cse"
        )
        self.on_import_zip = on_import_zip
        self.on_imported = on_imported

        cookies, source = load_cse_session()
        self.client = CseClient(cookies)
        self.session_source = source
        self.watcher = DownloadWatcher(self.download_dir)
        # Fallback for browsers that ignore the forced download folder: the user's
        # own Downloads folder is watched while a download is expected.
        self._downloads_watcher: DownloadWatcher | None = None
        self._await_download_until = 0.0

        self._results: list[CsePart] = []
        self._search_id = 0
        self._image_id = 0
        self._image_cache: dict[str, bytes] = {}
        self._session_syncing = False

        self._build_ui()
        self._refresh_session_status()
        threading.Thread(target=self._refresh_user_agent, daemon=True).start()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    @property
    def browser_profile_dir(self) -> Path:
        """Profile of the browser window the plugin opens (shared with SnapEDA)."""
        return default_profile_dir()

    # -- session hooks shared with the SessionDialog --------------------

    def import_site_cookies(self) -> object:
        return import_cse_cookies()

    def read_browser_cookies(self) -> dict[str, str]:
        return read_cookies_with_retry(CDP_PORT, domain_filter=self.cookie_domain)

    def save_site_session(self, cookies: dict[str, str], source: str = "") -> Path:
        return save_cse_session(cookies, source)

    def clear_site_session(self) -> None:
        """Forget the session, in the file **and** in the plugin browser.

        The automatic capture reads the browser's cookies, so one left behind would
        be saved again 15 seconds later.
        """
        cookies = dict(self.client.cookies())
        try:
            if session_state(CDP_PORT, domain_filter=self.cookie_domain).get("running"):
                cookies.update(read_cookies_with_retry(CDP_PORT, domain_filter=self.cookie_domain))
                delete_cookies_in_browser(CDP_PORT, cookies, domain=self.cookie_domain)
        except (BrowserCdpError, OSError) as exc:
            logger.debug("Could not clear the CSE cookies in the browser: %s", exc)
        clear_cse_session()

    def plugin_browser_state(self) -> dict[str, object]:
        return session_state(CDP_PORT, domain_filter=self.cookie_domain)

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        search_row = wx.BoxSizer(wx.HORIZONTAL)
        self.search_ctrl = wx.ComboBox(self, style=wx.CB_DROPDOWN | wx.TE_PROCESS_ENTER, choices=[])
        self.search_ctrl.SetHint(
            "Search a part number on Component Search Engine (e.g. STM32F103C8T6)"
        )
        _scaled(self.search_ctrl, 1.1)
        self.btn_search = wx.Button(self, label="Search")
        self.btn_session = wx.Button(self, label="Session / Login")
        self.btn_page = wx.Button(self, label="Download in browser")
        self.btn_page.SetToolTip(
            "Open the selected part on componentsearchengine.com in the plugin browser; "
            "the ZIP it downloads is imported automatically"
        )
        self.btn_page.Disable()
        self.btn_import = wx.Button(self, label="Import ZIP...")
        self.btn_import.SetToolTip("Import a KiCad/Samacsys ZIP you downloaded yourself")
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
                "Component Search Engine (Samacsys): search here, then 'Download in browser' "
                "-> press Download on the part page and pick the KiCad format. The ZIP is "
                "picked up and imported automatically."
            ),
        )
        root.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.session_label = wx.StaticText(self, label="")
        root.Add(self.session_label, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

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
        self.btn_session.Bind(wx.EVT_BUTTON, self._on_session)
        self.btn_page.Bind(wx.EVT_BUTTON, self._on_browse_part)
        self.btn_import.Bind(wx.EVT_BUTTON, self._on_pick_zip)
        self.btn_folder.Bind(wx.EVT_BUTTON, self._on_open_folder)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selected)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_browse_part)

        # Flow: watch the folder the plugin browser downloads into.
        self._watch_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_watch, self._watch_timer)
        self._watch_timer.Start(2000)

        # Keep the saved session in step with the plugin browser: a login done
        # once in that window is captured without pressing 'Read session'.
        self._session_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_session_sync, self._session_timer)
        self._session_timer.Start(15000)

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
        return panel

    # ------------------------------------------------------------------
    # session
    # ------------------------------------------------------------------

    def session_status_text(self) -> str:
        state = self.client.session_state()
        source = self.session_source or "no saved session"
        if not state["cookies"]:
            return f"{self.site_name} session - {source} (search only)"
        clearance = "yes" if state["clearance"] else "no"
        return (
            f"{self.site_name} session - {source} ({state['cookies']} cookies, "
            f"{self.preferred_cookie}: {clearance})"
        )

    def set_client_cookies(self, cookies: dict[str, str], source: str = "") -> None:
        self.client.set_cookies(cookies)
        self.session_source = source
        self._refresh_session_status()

    def _refresh_session_status(self) -> None:
        try:
            self.client.session_state()
            self.session_label.SetLabel(self.session_status_text())
            self.Layout()
        except Exception:  # noqa: BLE001 - status line must never break the panel
            logger.debug("Could not read the CSE session state", exc_info=True)

    def _refresh_user_agent(self) -> None:
        """Send the browser's own user agent: __cf_bm is bound to it."""
        try:
            state = session_state(CDP_PORT, domain_filter=self.cookie_domain)
            if not state.get("running"):
                return
            agent = browser_user_agent(CDP_PORT)
        except (BrowserCdpError, OSError) as exc:
            logger.debug("Could not read the browser user agent: %s", exc)
            return
        if agent:
            self.client.set_user_agent(agent)

    def _on_session(self, _: wx.CommandEvent) -> None:
        dialog = SessionDialog(self, self)
        try:
            dialog.ShowModal()
        finally:
            dialog.Destroy()
        # The dialog may have started the plugin browser and the user may have
        # logged in there: remember that login without asking for another click.
        threading.Thread(target=self._sync_session_after_dialog, daemon=True).start()

    def _sync_session_after_dialog(self) -> None:
        try:
            if session_state(CDP_PORT, domain_filter=self.cookie_domain).get("running"):
                self.capture_browser_session()
        except (BrowserCdpError, OSError) as exc:
            logger.debug("Could not refresh the CSE session after the dialog: %s", exc)
        wx.CallAfter(self._refresh_session_status)

    # -- keeping the login ---------------------------------------------

    def restore_session_in_browser(self) -> int:
        """Replay the saved session into the plugin browser before opening a part.

        The browser profile normally keeps the login, but one profile reset
        (KiCad update, "clear browsing data", moved folder) forced a new sign-in.
        Replaying the saved cookies puts the window back into the signed-in state
        it had when the session was captured.
        """
        cookies = self.client.cookies()
        if not cookies:
            return 0
        try:
            restored = set_cookies_in_browser(CDP_PORT, cookies, domain=self.cookie_domain)
        except (BrowserCdpError, OSError) as exc:
            logger.debug("Could not restore the CSE session in the browser: %s", exc)
            return 0
        if restored:
            logger.info("Restored %s CSE cookies in the plugin browser", restored)
        return restored

    def capture_browser_session(self) -> dict[str, str]:
        """Read the cookies of the plugin browser and remember them (login included)."""
        cookies = read_cookies_with_retry(CDP_PORT, domain_filter=self.cookie_domain)
        if not cookies:
            return {}
        merged = {**self.client.cookies(), **cookies}
        if merged == self.client.cookies():
            return cookies
        save_cse_session(merged, "plugin browser (CDP)")
        self.client.set_cookies(merged)
        self.session_source = "plugin browser (CDP)"
        logger.info("Saved %s CSE cookies from the plugin browser", len(merged))
        return cookies

    def _on_session_sync(self, _: wx.TimerEvent) -> None:
        if self._session_syncing:
            return
        try:
            if not session_state(CDP_PORT, domain_filter=self.cookie_domain).get("running"):
                return
        except Exception:  # noqa: BLE001 - the timer must never break the panel
            return
        self._session_syncing = True
        threading.Thread(target=self._session_sync_worker, daemon=True).start()

    def _session_sync_worker(self) -> None:
        try:
            self.capture_browser_session()
        except (BrowserCdpError, OSError) as exc:
            logger.debug("CSE session sync failed: %s", exc)
        finally:
            self._session_syncing = False
            wx.CallAfter(self._refresh_session_status)

    # ------------------------------------------------------------------
    # search
    # ------------------------------------------------------------------

    def _on_search(self, _: wx.Event) -> None:
        keyword = self.search_ctrl.GetValue().strip()
        if len(keyword) < 3:
            self._set_status("Type at least 3 characters.")
            return
        self._search_id += 1
        request_id = self._search_id
        self._set_status(f"Searching Component Search Engine for '{keyword}' ...")
        threading.Thread(
            target=self._search_worker, args=(keyword, request_id), daemon=True
        ).start()

    def _search_worker(self, keyword: str, request_id: int) -> None:
        try:
            result = self.client.search(keyword, page=1)
        except CseBlocked:
            # Cloudflare decides per request: when it answers the plugin's own
            # request with the challenge, the browser gets the same page instead.
            wx.CallAfter(
                self._set_status,
                "Cloudflare blocks the direct search - asking the plugin browser ...",
            )
            try:
                result = self._search_through_browser(keyword)
            except (CseBlocked, CseError, BrowserCdpError, OSError) as exc:
                wx.CallAfter(self._search_failed, request_id, str(exc))
                return
        except CseError as exc:
            wx.CallAfter(self._search_failed, request_id, f"Search failed: {exc}")
            return
        wx.CallAfter(self._search_done, request_id, result.parts, result.total)

    def _search_through_browser(self, keyword: str) -> CseSearchResult:
        """Search by navigating the plugin browser and parsing what it renders."""
        url = f"{BASE_SEARCH_URL}?{urllib.parse.urlencode({'term': keyword})}"
        state = session_state(CDP_PORT, domain_filter=self.cookie_domain)
        if not state.get("running"):
            launch_session_browser(
                port=CDP_PORT,
                profile_dir=self.browser_profile_dir,
                url="about:blank",
                download_dir=self.download_dir,
            )
            if not wait_for_cdp(CDP_PORT, timeout=45):
                raise CseError("the plugin browser did not start in time")
        self.restore_session_in_browser()
        html = fetch_page_html(CDP_PORT, url)
        if not html or "Just a moment" in html:
            raise CseBlocked(
                "Cloudflare kept the challenge. Open the plugin browser window, pass the "
                "check there and search again."
            )
        parts, total = parts_from_search_html(html)
        return CseSearchResult(parts=parts, total=total, page=1)

    def _search_failed(self, request_id: int, message: str) -> None:
        if request_id != self._search_id:
            return
        self._set_status(message)

    def _search_done(self, request_id: int, parts: list[CsePart], hits: int) -> None:
        if request_id != self._search_id:
            return
        self._results = parts
        self.list_ctrl.DeleteAllItems()
        for row, part in enumerate(parts):
            self.list_ctrl.InsertItem(row, part.part_number)
            self.list_ctrl.SetItem(row, 1, part.manufacturer)
            self.list_ctrl.SetItem(row, 2, part.package_type)
            self.list_ctrl.SetItem(row, 3, part.availability)
            self.list_ctrl.SetItem(row, 4, str(part.datasheet_count or ""))
            self.list_ctrl.SetItem(row, 5, part.description)
        self.btn_page.Enable(bool(parts))
        if parts:
            self._set_status(f"Showing {len(parts)} of {hits} results")
        else:
            self._set_status(
                "No results. If the part exists on the site, import the session "
                "(Session / Login) and search again."
            )

    # ------------------------------------------------------------------
    # selection + detail
    # ------------------------------------------------------------------

    def _selected_part(self) -> CsePart | None:
        index = self.list_ctrl.GetFirstSelected()
        if index < 0 or index >= len(self._results):
            return None
        return self._results[index]

    def _on_selected(self, _: wx.ListEvent) -> None:
        part = self._selected_part()
        if part is not None:
            self._show_detail(part)

    def _show_detail(self, part: CsePart) -> None:
        for child in list(self._detail_grid.GetChildren()):
            child.GetWindow().Destroy()
        rows = [
            ("Part number", part.part_number),
            ("Manufacturer", part.manufacturer),
            ("Package", part.package_type),
            ("Can download", part.availability),
            ("Models", str(part.model_count or "")),
            ("Datasheets", str(part.datasheet_count or "")),
            ("Page", part.part_url),
        ]
        for label, value in rows:
            if not value:
                continue
            caption = wx.StaticText(self._detail_panel, label=f"{label}:")
            _bold(caption)
            self._detail_grid.Add(caption, 0, wx.ALIGN_CENTER_VERTICAL)
            self._detail_grid.Add(wx.StaticText(self._detail_panel, label=value), 1, wx.EXPAND)
        self._detail_panel.Layout()
        self._detail_panel.FitInside()

        if part.image:
            self._image_id += 1
            request_id = self._image_id
            threading.Thread(
                target=self._image_worker, args=(part.image, request_id), daemon=True
            ).start()
        else:
            self._image_bitmap.Hide()
            self._detail_panel.Layout()

    def _image_worker(self, url: str, request_id: int) -> None:
        data = self._image_cache.get(url)
        if data is None:
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": self.client.user_agent()}
                )
                with urllib.request.urlopen(request, timeout=20) as response:
                    data = response.read()
                self._image_cache[url] = data
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                logger.debug("Could not fetch %s: %s", url, exc)
                return
        wx.CallAfter(self._show_image, request_id, data)

    def _show_image(self, request_id: int, data: bytes) -> None:
        if request_id != self._image_id:
            return
        stream = wx.MemoryInputStream(data, len(data))
        try:
            image = wx.Image(stream, wx.BITMAP_TYPE_ANY)
        except Exception:  # noqa: BLE001 - a broken preview must not break the panel
            return
        if not image.IsOk():
            return
        width, height = image.GetSize()
        if width > _IMAGE_MAX or height > _IMAGE_MAX:
            scale = min(_IMAGE_MAX / max(width, 1), _IMAGE_MAX / max(height, 1))
            image = image.Scale(max(1, int(width * scale)), max(1, int(height * scale)))
        self._image_bitmap.SetBitmap(wx.Bitmap(image))
        self._image_bitmap.Show()
        self._detail_panel.Layout()

    def focus_search(self) -> None:
        """Give the search field the focus (call after the window is shown)."""
        try:
            self.search_ctrl.SetFocus()
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not focus the search field", exc_info=True)

    # ------------------------------------------------------------------
    # download + import
    # ------------------------------------------------------------------

    def _on_browse_part(self, _: wx.Event) -> None:
        """Open the selected part in the plugin browser; its download gets imported."""
        part = self._selected_part()
        if part is None:
            return
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.expect_download()
        self._set_status(f"Opening {part.part_number} in the plugin browser ...")
        threading.Thread(target=self._browse_worker, args=(part,), daemon=True).start()

    def expect_download(self, seconds: float = 1800.0) -> None:
        """Arm the fallback watcher for the next ``seconds`` (30 min)."""
        self._await_download_until = time.monotonic() + seconds
        if self._downloads_watcher is None:
            self._downloads_watcher = DownloadWatcher(
                user_downloads_dir(), ignore_before=time.time(), require_kicad=True
            )

    def _browse_worker(self, part: CsePart) -> None:
        url = part.part_url
        try:
            state = session_state(CDP_PORT, domain_filter=self.cookie_domain)
            if not state.get("running"):
                launch_session_browser(
                    port=CDP_PORT,
                    profile_dir=self.browser_profile_dir,
                    # The part page is opened after the saved cookies are back.
                    url="about:blank",
                    download_dir=self.download_dir,
                )
                if not wait_for_cdp(CDP_PORT, timeout=45):
                    wx.CallAfter(self._browse_done, part, False, "the browser did not start")
                    return
            # The saved session (login included) goes back into the window first,
            # so the page never loads as an anonymous visitor.
            self.restore_session_in_browser()
            set_download_directory(CDP_PORT, self.download_dir)
            open_url(CDP_PORT, url)
            self.capture_browser_session()
            try:
                agent = browser_user_agent(CDP_PORT)
                if agent:
                    self.client.set_user_agent(agent)
            except (BrowserCdpError, OSError) as exc:
                logger.debug("No browser user agent: %s", exc)
            wx.CallAfter(self._browse_done, part, True, "")
        except (BrowserCdpError, OSError) as exc:
            wx.CallAfter(self._browse_done, part, False, str(exc))

    def _browse_done(self, part: CsePart, ok: bool, detail: str) -> None:
        if ok:
            self._set_status(
                f"{part.part_number} open in the plugin browser - press Download there "
                f"(KiCad format); the ZIP is imported automatically into "
                f"{self.download_dir.name}/"
            )
        else:
            self._set_status(f"Could not open the browser ({detail}); opening your default one.")
            try:
                webbrowser_open(part.part_url)
            except Exception:  # noqa: BLE001 - last resort
                logger.warning("Could not open %s in the default browser", part.part_url)

    def _on_watch(self, _: wx.TimerEvent) -> None:
        if self.on_import_zip is None:
            return
        for zip_path in self.watcher.scan():
            self._import_downloaded(zip_path)
        if time.monotonic() >= self._await_download_until or self._downloads_watcher is None:
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
            self.on_imported(part, f"Samacsys ZIP -> {archived}")

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

    def _part_for_zip(self, zip_path: Path) -> CsePart:
        """Match a downloaded ZIP to a search result so the log shows the part."""
        stem = zip_path.stem.lower()
        for part in self._results:
            name = part.part_number.lower()
            if name and (name == stem or stem.startswith(name) or name.startswith(stem)):
                return part
        return CsePart(part_number=zip_path.stem, manufacturer="")

    def _on_pick_zip(self, _: wx.CommandEvent) -> None:
        """Manual route: import a ZIP downloaded with any browser."""
        if self.on_import_zip is None:
            return
        with wx.FileDialog(
            self,
            "Select the KiCad ZIP downloaded from Component Search Engine",
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


def webbrowser_open(url: str) -> bool:
    """Open a URL in the user's default browser (fallback when CDP fails)."""
    import webbrowser

    return webbrowser.open(url)


if __name__ == "__main__":  # pragma: no cover - manual UI smoke test
    logging.basicConfig(level=logging.INFO)
    app = wx.App()
    frame = wx.Frame(None, title="CSE panel smoke test", size=wx.Size(1100, 700))
    panel = CsePanel(frame)
    frame.Show()
    icon = _load_icon(panel.plugin_dir)
    if icon is not None:
        frame.SetIcon(icon)
    app.MainLoop()
