# KiCad Parts Hub - a modified version of Import-LIB-KiCad-Plugin by Steffen-W
# (https://github.com/Steffen-W/Import-LIB-KiCad-Plugin), GPL-3.0.
# Modified on 2026-10-17; see NOTICE.md for the list of changes.
"""
KiCad Import Plugin for library files from various sources.
Supports Octopart, Samacsys, Ultralibrarian, Snapeda and EasyEDA.
"""

from __future__ import annotations

import atexit
import hashlib
import logging
import os
import socket
import sys
from pathlib import Path
from threading import Thread
from time import sleep
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .snapeda_search import CombinedSearchDialog, SnapedaPart

# Setup paths for local imports
script_dir = Path(__file__).resolve().parent
if str(script_dir) not in sys.path:
    sys.path.insert(0, str(script_dir))

_PLUGIN_PORT = 49152 + (int(hashlib.md5(str(script_dir).encode()).hexdigest(), 16) % 16383)


def quick_instance_check() -> bool:
    """Quick pre-logging check: is another instance already listening?

    Only used to decide the logging mode before full IPC verification.
    The real check (bind attempt) happens in instance_manager.is_already_running().
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(0.5)
        s.connect(("127.0.0.1", _PLUGIN_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


if __name__ == "__main__":
    is_new_instance = not quick_instance_check()

    if is_new_instance:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s [%(name)s:%(filename)s:%(lineno)d]: %(message)s",
            filename=script_dir / "plugin.log",
            filemode="w",  # Overwrite for new instance
        )
        logging.info("New instance started - log file reset")
    else:
        logging.basicConfig(
            level=logging.WARNING,
            format="%(message)s",
            filename=script_dir / "plugin.log",
            filemode="a",  # Append for existing instance
        )
        logging.warning("Another instance detected - exiting or continuing with limited logging")

    logging.debug("Application starting...")

# Import dependencies
try:
    import wx

    logging.info("Successfully imported wx module")
except Exception:
    logging.exception("Failed to import wx module")
    raise

try:
    # Try relative imports first (when run as module)
    from .ConfigHandler import ConfigHandler
    from .FileHandler import FileHandler
    from .impart_gui import impartGUI
    from .KiCad_Settings import KiCad_Settings
    from .KiCadImport import LibImporter
    from .KiCadSettingsPaths import KiCadApp
    from .single_instance_manager import SingleInstanceManager

    logging.info("Successfully imported all local modules using relative imports")

except ImportError as e1:
    try:
        # Fallback to absolute imports (when run as script)
        from ConfigHandler import ConfigHandler  # type: ignore[import-not-found,no-redef]
        from FileHandler import FileHandler  # type: ignore[import-not-found,no-redef]
        from impart_gui import impartGUI  # type: ignore[import-not-found,no-redef]
        from KiCad_Settings import KiCad_Settings  # type: ignore[import-not-found,no-redef]
        from KiCadImport import LibImporter  # type: ignore[import-not-found,no-redef]
        from KiCadSettingsPaths import KiCadApp  # type: ignore[import-not-found,no-redef]
        from single_instance_manager import (  # type: ignore[import-not-found,no-redef]
            SingleInstanceManager,
        )

        logging.info("Successfully imported all local modules using absolute imports")

    except ImportError as e2:
        logging.exception("Failed to import local modules with both relative and absolute imports")
        print(f"Relative import error: {e1}")
        print(f"Absolute import error: {e2}")
        print(f"Python path: {sys.path}")
        print(f"Current working directory: {os.getcwd()}")
        print(f"Script directory: {script_dir}")
        raise e2

# Event handling
EVT_UPDATE_ID = wx.NewIdRef()


def EVT_UPDATE(win: wx.Window, func: Any) -> None:
    """Bind update event to window."""
    win.Connect(-1, -1, EVT_UPDATE_ID, func)


class ResultEvent(wx.PyEvent):
    """Custom event for thread communication."""

    def __init__(self, data: str) -> None:
        wx.PyEvent.__init__(self)
        self.SetEventType(EVT_UPDATE_ID)
        self.data = data


class FileDropTarget(wx.FileDropTarget):
    """Drop target for ZIP files on the text control."""

    def __init__(self, window: wx.Window, callback: Callable[[list[str]], None]) -> None:
        wx.FileDropTarget.__init__(self)
        self.window = window
        self.callback = callback

    def OnDropFiles(self, _x: int, _y: int, filenames: list[str]) -> bool:
        """Called when files are dropped on the text control."""
        zip_files = [f for f in filenames if f.lower().endswith(".zip")]

        if zip_files:
            self.callback(zip_files)
            return True
        else:
            wx.MessageBox(
                "Only .zip files are supported!",
                "Invalid file type",
                wx.OK | wx.ICON_WARNING,
            )
            return False


class PluginThread(Thread):
    """Background thread for monitoring import status."""

    def __init__(self, wx_object: wx.Window, backend: ImpartBackend) -> None:
        Thread.__init__(self)
        self.daemon = True
        self.wx_object = wx_object
        self.backend = backend
        self.stop_thread = False
        self.start()

    def run(self) -> None:
        """Main thread loop."""
        len_str = 0
        while not self.stop_thread:
            current_len = len(self.backend.print_buffer)
            if len_str != current_len:
                self.report(self.backend.print_buffer)
                len_str = current_len
            sleep(0.5)

    def report(self, status: str) -> None:
        """Send status update to main thread."""
        wx.PostEvent(self.wx_object, ResultEvent(status))


class ImpartBackend:
    """Backend handler for the import plugin."""

    # Library names supported by the plugin
    SUPPORTED_LIBRARIES = [
        "Octopart",
        "Samacsys",
        "UltraLibrarian",
        "Snapeda",
        "EasyEDA",
    ]

    def __init__(self) -> None:
        """Initialize backend components."""
        logging.info("Initializing ImpartBackend")

        self.config_path = os.path.join(os.path.dirname(__file__), "config.ini")

        try:
            self.kicad_app = KiCadApp(prefer_ipc=True, min_version="8.0.4")
            self.config = ConfigHandler(self.config_path)
            self.kicad_settings = KiCad_Settings(str(self.kicad_app.settings_path))

            self.folder_handler = FileHandler(
                ".", min_size=1_000, max_size=50_000_000, file_extension=".zip"
            )

            self.importer = LibImporter()
            # Create a wrapper function that matches the expected signature
            self.importer.print = lambda txt: self.print_to_buffer(txt)  # type: ignore[method-assign]

            logging.info("Successfully initialized all backend components")
            logging.info(f"KiCad settings path: {self.kicad_settings.SettingPath}")

        except Exception:
            logging.exception("Failed to initialize backend components")
            raise

        self.run_thread = False
        self.auto_import = False
        self.overwrite_import = False
        self.compress_models = False
        self.local_lib = False
        self.print_buffer = ""

        try:
            self.kicad_app.check_min_version(output_func=self.print_to_buffer)
        except Exception as e:
            logging.warning(f"Failed to check KiCad version: {e}")

        if not self.config.config_is_set:
            self._print_initial_warnings()

    def _print_initial_warnings(self) -> None:
        """Print initial configuration warnings."""
        warning_msg = (
            "Warning: The path where the libraries should be saved has not been "
            "adjusted yet. Maybe you use the plugin in this version for the first time."
        )

        info_msg = (
            "If this plugin is being used for the first time, settings in KiCad are "
            "required. The settings are checked at the end of the import process. "
            "For easy setup, auto setting can be activated."
        )

        self.print_to_buffer(warning_msg)
        self.print_to_buffer(info_msg)
        self.print_to_buffer("\n" + "=" * 50 + "\n")

    def print_to_buffer(self, *args: Any) -> None:
        """Add text to print buffer."""
        for text in args:
            self.print_buffer += str(text) + "\n"

    def find_and_import_new_files(self) -> None:
        """Monitor directory for new files and import them."""
        src_path = self.config.get_SRC_PATH()

        if not os.path.isdir(src_path):
            self.print_to_buffer(f"Source path does not exist: {src_path}")
            return

        while True:
            new_files = self.folder_handler.get_new_files(src_path)

            for lib_file in new_files:
                self._import_single_file(lib_file)

            if not self.run_thread:
                break
            sleep(1)

    def _import_single_file(self, lib_file: str) -> None:
        """Import a single library file."""
        try:
            # Convert string to Path for import_all function
            lib_path = Path(lib_file)
            self.importer.compress_models = self.compress_models
            self.importer.import_all(lib_path, overwrite_if_exists=self.overwrite_import)

        except AssertionError as e:
            self.print_to_buffer(f"Assertion Error: {e}")
        except Exception as e:
            error_msg = f"Import Error: {e}\nPython version: {sys.version}"
            self.print_to_buffer(error_msg)
            logging.exception("Import failed")
        finally:
            self.print_to_buffer("")


def check_library_import(backend: ImpartBackend, project_dir: str | None) -> str:
    """Check that KiCad's settings include the imported libraries."""
    msg = ""

    if backend.local_lib:
        if not project_dir:
            return "\nLocal library mode enabled but no KiCad project available."

        try:
            kicad_settings = KiCad_Settings(str(project_dir), path_prefix="${KIPRJMOD}")
            dest_path = project_dir
            logging.info("Project-specific library check completed")
        except Exception as e:
            logging.error(f"Failed to read project settings: {e}")
            return "\nCould not read project library settings."
    else:
        kicad_settings = backend.kicad_settings
        dest_path = backend.config.get_DEST_PATH()
        msg = kicad_settings.check_GlobalVar(dest_path)

    libs_to_check = (
        [backend.importer.lib_name]
        if backend.importer.lib_name
        else ImpartBackend.SUPPORTED_LIBRARIES
    )
    for lib_name in libs_to_check:
        msg += _check_single_library(kicad_settings, lib_name, dest_path)

    return msg


def _check_single_library(
    kicad_settings: KiCad_Settings,
    lib_name: str,
    dest_path: str,
) -> str:
    """Check a single library for import."""
    msg = ""

    # Check for symbol libraries
    symbol_variants = [
        f"{lib_name}.kicad_sym",
        f"{lib_name}_kicad_sym.kicad_sym",
        f"{lib_name}_old_lib.kicad_sym",
    ]

    for variant in symbol_variants:
        if os.path.isfile(os.path.join(dest_path, variant)):
            msg += kicad_settings.check_symbollib(variant)
            break

    # Check for footprint libraries
    if os.path.isdir(os.path.join(dest_path, f"{lib_name}.pretty")):
        msg += kicad_settings.check_footprintlib(lib_name)

    return msg


APP_TITLE = "KiCad Parts Hub"
APP_SUBTITLE = "SnapEDA / SnapMagic + EasyEDA imports into KiCad"


def _window_icon_path() -> Path | None:
    """Best available artwork for the window/taskbar icon."""
    base = Path(__file__).resolve().parent
    for candidate in (
        base / "icon.png",  # 64 px, part of the installed payload
        base.parent / "resources" / "icon_256.png",
        base.parent / "resources" / "icon.png",
    ):
        if candidate.is_file():
            return candidate
    return None


# Widgets of the generated dialog that the search-driven UI no longer needs.
# Collapsing the sizer item (not only Hide()) is what removes the empty gap.
HIDDEN_WIDGETS = (
    # "EasyEDA / LCSC Part#" row: the search window imports both sources.
    "m_staticTextLCSC",
    "m_textCtrl2",
    "m_buttonImportManual",
    # Start + the source folder it watched. Imports come from Component Search
    # or from a dropped ZIP, so the folder watcher and its button are gone.
    "m_button",
    "m_staticText_sourcepath",
    "m_dirPicker_sourcepath",
    "m_staticline12",
    # Options that are now fixed defaults set in code (see *_enabled properties).
    "m_autoImport",
    "m_overwrite",
    "m_checkBoxCompressModels",
    "m_checkBoxSingleLib",
    "m_textCtrl_libname",
    # Credited another project; the app stands on its own now.
    "m_staticText5",
)


def _collapse(dialog: Any, widget: Any) -> None:
    """Hide a widget and remove its cell, so no gap is left behind."""
    sizer = widget.GetContainingSizer()
    if sizer is not None:
        try:
            sizer.Show(widget, False)
        except Exception:  # noqa: BLE001 - cosmetic only
            logging.debug("Could not collapse %r", widget, exc_info=True)
    widget.Hide()


def apply_main_dialog_style(dialog: Any) -> None:
    """Replace the generated layout with the card layout of ``main_window``.

    The import options (auto import, overwrite, zip 3D, single lib name) and the
    Start button belonged to the folder-watching flow. Imports now start from the
    search window or from a dropped ZIP, so those controls are hidden and the
    defaults live in the ``*_enabled`` properties of :class:`ImpartFrontend`.
    The remaining controls are re-parented into the new layout, which keeps every
    event binding intact.
    """
    try:
        dialog.SetTitle(APP_TITLE)
    except Exception:  # noqa: BLE001 - cosmetic only
        logging.debug("Could not set the dialog title", exc_info=True)

    for name in ("m_buttonImportManual", "m_buttonComponentSearch"):
        button = getattr(dialog, name, None)
        if button is None:
            continue
        try:
            font = button.GetFont()
            font.MakeBold()
            button.SetFont(font)
        except Exception:  # noqa: BLE001 - cosmetic only
            logging.debug("Could not bold %s", name, exc_info=True)

    for name in HIDDEN_WIDGETS:
        widget = getattr(dialog, name, None)
        if widget is not None:
            _collapse(dialog, widget)

    try:
        try:
            from .main_window import restyle_main_window
        except ImportError:  # pragma: no cover - flat module layout
            from main_window import restyle_main_window  # type: ignore[import-not-found,no-redef]

        dialog.m_partsHubWidgets = restyle_main_window(
            dialog,
            title=APP_TITLE,
            subtitle=APP_SUBTITLE,
        )
    except Exception:  # noqa: BLE001 - the window must open even if the layout fails
        logging.exception("Could not build the new main window layout")
        return
    dialog.Layout()


instance_manager = SingleInstanceManager()  # Create global instance manager

# Register cleanup handler to ensure IPC server stops on exit
atexit.register(instance_manager.stop_server)


class ImpartFrontend(impartGUI):
    """
    Frontend GUI supporting both IPC singleton and fallback modes.

    - IPC mode: Singleton behavior with window focus on subsequent launches
    - Fallback mode: Direct execution from PCBNew, always creates new instance
    Each instance gets a fresh backend with clean state.
    """

    def __init__(self, fallback_mode: bool = False) -> None:
        super().__init__(None)
        self.fallback_mode = fallback_mode

        # Log the mode
        if self.fallback_mode:
            logging.info("Running in FALLBACK MODE (called from pcbnew)")
        else:
            logging.info("Running in NORMAL MODE (direct execution)")

        # Register with instance manager only if not in fallback mode
        if not self.fallback_mode:
            if not instance_manager.register_frontend(self):
                # Another instance already exists - this shouldn't happen
                logging.warning("Frontend instance already exists - destroying this one")
                self.Destroy()
                return
        else:
            logging.info("Fallback mode: Skipping IPC server registration")

        # Set window icon (256 px artwork; the 24 px toolbar image is too coarse)
        try:
            icon_path = _window_icon_path()
            if icon_path is not None:
                self.SetIcon(wx.Icon(str(icon_path), wx.BITMAP_TYPE_PNG))
        except Exception as e:
            logging.warning(f"Could not set window icon: {e}")

        self.backend = create_backend_handler()
        self.thread: PluginThread | None = None
        self.search_dialog: CombinedSearchDialog | None = None

        self._setup_gui()
        self._setup_events()
        self._start_monitoring_thread()
        self._print_initial_paths()
        # Fresh install / new KiCad version: put the libraries that are already
        # there into KiCad's tables without waiting for an import.
        wx.CallAfter(self._install_library_paths_on_start)

    def _setup_gui(self) -> None:
        """Initialize GUI components."""
        self.kicad_project = self.backend.kicad_app.get_project_dir()
        self.project_detected_by_kicad = bool(self.kicad_project)

        # Set initial values
        self.m_dirPicker_sourcepath.SetPath(self.backend.config.get_SRC_PATH())

        # Set checkboxes (load from persistent config)
        auto_import = self.backend.config.get_value("auto_import") == "True"
        overwrite_import = self.backend.config.get_value("overwrite_import") == "True"
        local_lib = self.backend.config.get_value("local_lib") == "True"
        self.m_autoImport.SetValue(auto_import)
        self.m_overwrite.SetValue(overwrite_import)
        self.m_checkBoxLocalLib.SetValue(local_lib)
        self.backend.auto_import = auto_import
        self.backend.overwrite_import = overwrite_import
        self.backend.local_lib = local_lib
        self._update_library_picker()

        single_lib = self.backend.config.get_value("single_lib") == "True"
        lib_name = self.backend.config.get_value("lib_name") or ""
        self.m_checkBoxSingleLib.SetValue(single_lib)
        self.m_textCtrl_libname.SetValue(lib_name)
        self.Layout()
        self.backend.importer.lib_name = lib_name if single_lib and lib_name else None

        compress_models_val = self.backend.config.get_value("compress_models")
        compress_models = compress_models_val == "True"  # default False if not set
        self.m_checkBoxCompressModels.SetValue(compress_models)
        self.backend.compress_models = compress_models

        self._update_button_label()
        # Add drag & drop support
        self._setup_drag_drop()
        self._add_drag_drop_hint()
        apply_main_dialog_style(self)

    # ------------------------------------------------------------------
    # Import options
    # ------------------------------------------------------------------
    # The checkboxes that used to hold these live in the generated dialog and are
    # hidden (see HIDDEN_WIDGETS). They stay as the storage for the values loaded
    # from config.ini, so the behaviour is exactly the one configured before; the
    # rest of the code goes through these properties.

    @property
    def overwrite_enabled(self) -> bool:
        """Replace existing library entries when the same part is imported again."""
        return bool(self.m_overwrite.IsChecked())

    @property
    def compress_models_enabled(self) -> bool:
        """Store STEP models gzip-compressed."""
        return bool(self.m_checkBoxCompressModels.IsChecked())

    @property
    def single_lib_enabled(self) -> bool:
        """Merge every source into one library instead of one per source."""
        return bool(self.m_checkBoxSingleLib.IsChecked())

    @property
    def lib_name_override(self) -> str:
        """Library name used when single-lib mode is on."""
        return self.m_textCtrl_libname.GetValue().strip()

    def _setup_events(self) -> None:
        """Setup event handlers."""
        EVT_UPDATE(self, self.update_display)

    def _setup_drag_drop(self) -> None:
        """Configure drag & drop for the text control."""
        drop_target = FileDropTarget(self.m_text, self._on_files_dropped)
        self.m_text.SetDropTarget(drop_target)

        self.m_text.SetToolTip(
            "Drag ZIP files here for direct import\nSupported: Samacsys, UltraLibrarian, Snapeda"
        )

    def _add_drag_drop_hint(self) -> None:
        """Add visual hint for drag & drop functionality."""
        hint_text = "Tip: You can drag ZIP files directly into this window!"
        self.backend.print_to_buffer(hint_text)
        self.backend.print_to_buffer("=" * 50)

    def _on_files_dropped(self, zip_files: list[str]) -> None:
        """Callback when ZIP files are dropped on the text control."""
        self.backend.print_to_buffer(f"\n{len(zip_files)} file(s) received via drag & drop:")

        for zip_file in zip_files:
            self.backend.print_to_buffer(f"  - {os.path.basename(zip_file)}")

        self.backend.print_to_buffer("")
        self._import_dropped_files(zip_files)

    def _import_dropped_files(self, zip_files: list[str]) -> None:
        """Import files received via drag & drop."""
        if not self._update_backend_settings():
            return

        for zip_file in zip_files:
            self.backend._import_single_file(zip_file)

        # Check library settings after import
        self._check_and_show_library_warnings()

    def _start_monitoring_thread(self) -> None:
        """Start the monitoring thread."""
        self.thread = PluginThread(self, self.backend)

    def _print_initial_paths(self) -> None:
        """Print initial source and destination paths."""
        src_path = self.backend.config.get_SRC_PATH()
        lib_mode, dest_path = self._destination_info()

        self.backend.print_to_buffer(f"Library Mode: {lib_mode}")
        if self.kicad_project:
            self.backend.print_to_buffer(f"KiCad Project: {self.kicad_project}")
        self.backend.print_to_buffer(f"Source Directory: {src_path}")
        self.backend.print_to_buffer(f"Destination Directory: {dest_path}")
        self.backend.print_to_buffer("=" * 50)

    def _print_path_change(self, change_type: str, new_value: str = "") -> None:
        """Print path change information."""
        if change_type == "library_mode":
            lib_mode, dest_path = self._destination_info()
            self.backend.print_to_buffer(f"New Library Mode: {lib_mode}")
            self.backend.print_to_buffer(f"New Destination Directory: {dest_path}")
        elif change_type == "source":
            self.backend.print_to_buffer(f"New Source Directory: {new_value}")
        elif change_type == "destination":
            self.backend.print_to_buffer(f"New Destination Directory: {new_value}")

    def _destination_info(self) -> tuple[str, str]:
        """Return the active library mode and destination."""
        if not self.backend.local_lib:
            return "Global Library", self.backend.config.get_DEST_PATH()
        if not self.kicad_project:
            return "Local Project Library", "no KiCad project detected - select the project folder"
        if not self.project_detected_by_kicad:
            return "Local Project Library", f"{self.kicad_project} (selected manually)"
        return "Local Project Library", self.kicad_project

    def _update_library_picker(self) -> None:
        """Update the destination picker's path and editability."""
        picker = self.m_dirPicker_librarypath
        if self.backend.local_lib:
            picker.SetPath(self.kicad_project or "")
            picker.Enable(not self.project_detected_by_kicad)
            picker.SetToolTip("KiCad project folder")
        else:
            picker.SetPath(self.backend.config.get_DEST_PATH())
            picker.Enable(True)
            picker.SetToolTip("Global library folder")

    def _set_project_dir(self, path: str) -> None:
        """Use a manually selected folder as the project folder."""
        self.kicad_project = path
        self.project_detected_by_kicad = False
        self.m_dirPicker_librarypath.SetPath(path)
        self.backend.folder_handler.known_files = set()
        self.backend.print_to_buffer(f"KiCad Project (selected manually): {path}")
        if not any(Path(path).glob("*.kicad_pro")):
            self.backend.print_to_buffer("Warning: No .kicad_pro file found in this folder.")

    def _ask_project_dir(self) -> None:
        """Ask for the project folder when KiCad did not report one."""
        with wx.DirDialog(
            self,
            "No KiCad project detected - select the project folder",
            style=wx.DD_DEFAULT_STYLE | wx.DD_DIR_MUST_EXIST,
        ) as dlg:
            if dlg.ShowModal() == wx.ID_OK:
                self._set_project_dir(dlg.GetPath())

    def _resolve_project_dir(self) -> str | None:
        """Return the project folder for local mode, asking for it if needed."""
        if not self.kicad_project:
            # KiCad may have opened a project after plugin start
            self.backend.kicad_app.refresh_project_info()
            self.kicad_project = self.backend.kicad_app.get_project_dir()
            self.project_detected_by_kicad = bool(self.kicad_project)
            if self.kicad_project:
                self.backend.print_to_buffer(f"KiCad Project: {self.kicad_project}")

        if self.kicad_project and not Path(self.kicad_project).is_dir():
            self.backend.print_to_buffer(
                f"Error: KiCad project directory does not exist: {self.kicad_project}"
            )
            logging.error(f"KiCad project directory invalid: {self.kicad_project}")
            self.kicad_project = None
            self.project_detected_by_kicad = False

        self._update_library_picker()
        if not self.kicad_project:
            self._ask_project_dir()

        if not self.kicad_project:
            self.backend.print_to_buffer(
                "Error: 'Save local' is selected, but no KiCad project folder is set."
            )
            self.backend.print_to_buffer("Please either:")
            self.backend.print_to_buffer("  1. Select the project folder, or")
            self.backend.print_to_buffer("  2. Uncheck 'Save local' to use the global library path")
            logging.warning("Local library mode selected but no KiCad project folder set")
            return None

        return self.kicad_project

    def _update_button_label(self) -> None:
        """Update the main button label based on current state."""
        if self.backend.run_thread:
            self.m_button.Label = "automatic import / press to stop"
        else:
            self.m_button.Label = "Start"

    def update_display(self, status: ResultEvent) -> None:
        """Update the text display with new status."""
        self.m_text.SetValue(status.data)
        self.m_text.SetInsertionPointEnd()

    def m_checkBoxLocalLibOnCheckBox(self, event: wx.CommandEvent) -> None:
        """Handle local library checkbox change."""
        old_local_lib = self.backend.local_lib
        self.backend.local_lib = self.m_checkBoxLocalLib.IsChecked()
        if self.backend.local_lib and not old_local_lib:
            self._resolve_project_dir()
        else:
            self._update_library_picker()

        # Print change information
        if old_local_lib != self.backend.local_lib:
            self._print_path_change("library_mode")

        event.Skip()

    def m_checkBoxSingleLibOnCheckBox(self, event: wx.CommandEvent) -> None:
        """Handle single library name checkbox change."""
        enabled = self.m_checkBoxSingleLib.IsChecked()
        self.m_textCtrl_libname.Show(enabled)
        self.Layout()
        if enabled:
            name = self.m_textCtrl_libname.GetValue().strip()
            self.backend.importer.lib_name = name if name else None
        else:
            self.backend.importer.lib_name = None
        event.Skip()

    def on_close(self, event: wx.CloseEvent) -> None:
        """Handle window close event with robust cleanup."""
        try:
            if not self.backend.run_thread:
                # No automatic import active: Always close everything completely
                self._safe_cleanup(close_ipc=not self.fallback_mode)
                logging.info("No auto import: Closing everything completely")
                event.Skip()

            elif self.fallback_mode:
                # Fallback mode + auto import: Close GUI but keep background thread
                choice = self._confirm_background_process()
                if choice == "cancel":
                    event.Veto()
                    return
                elif choice == "background":
                    self._safe_cleanup(close_ipc=False, stop_backend=False)
                    logging.info("Fallback mode: GUI closed, background thread continues")
                    event.Skip()  # Close GUI completely
                    return
                else:  # choice == "close"
                    self._safe_cleanup(close_ipc=False, stop_backend=True)
                    logging.info("Fallback mode: Everything stopped")
                    event.Skip()

            else:
                # IPC mode + auto import: Minimize window, keep everything running
                choice = self._confirm_background_process()
                if choice == "cancel":
                    event.Veto()
                    return
                elif choice == "background":
                    self._safe_cleanup(close_ipc=False, stop_backend=False)
                    if not self.IsIconized():
                        self.Iconize(True)
                    logging.info(
                        "IPC mode: Frontend minimized, running in background with IPC active"
                    )
                    event.Veto()  # Prevent actual closing
                    return
                else:  # choice == "close"
                    self._safe_cleanup(close_ipc=True, stop_backend=True)
                    logging.info("IPC mode: Everything stopped")
                    event.Skip()

        except Exception as e:
            logging.exception(f"Error during close event: {e}")
            # Force cleanup on any exception
            try:
                self._safe_cleanup(close_ipc=not self.fallback_mode, stop_backend=True)
            except Exception:
                pass
            event.Skip()

    def _safe_cleanup(self, close_ipc: bool = True, stop_backend: bool = True) -> None:
        """Perform safe cleanup with error handling."""
        try:
            self._save_settings()
        except Exception as e:
            logging.warning(f"Failed to save settings during cleanup: {e}")

        if stop_backend:
            try:
                self.backend.run_thread = False
            except Exception as e:
                logging.warning(f"Failed to stop backend thread: {e}")

        try:
            if self.thread:
                self.thread.stop_thread = True
        except Exception as e:
            logging.warning(f"Failed to stop monitoring thread: {e}")

        if close_ipc:
            try:
                instance_manager.stop_server()
            except Exception as e:
                logging.warning(f"Failed to stop IPC server: {e}")

    def _confirm_background_process(self) -> str:
        """Confirm what to do when background process is running."""
        msg = (
            "Import process runs in automatic mode.\n\n"
            "- HIDE: Keep running, hide window\n"
            "- STOP: Stop import and close\n"
            "- CANCEL: Back to window"
        )

        dlg = wx.MessageDialog(
            None,
            msg,
            "Import Running",
            wx.YES_NO | wx.CANCEL | wx.ICON_QUESTION,
        )

        dlg.SetYesNoLabels("&Hide", "&Stop")

        result = dlg.ShowModal()
        dlg.Destroy()

        if result == wx.ID_YES:
            return "background"
        elif result == wx.ID_NO:
            return "close"
        else:
            return "cancel"

    def _save_settings(self) -> None:
        """Save current settings to backend and persistent config."""
        self.backend.auto_import = self.m_autoImport.IsChecked()
        self.backend.overwrite_import = self.m_overwrite.IsChecked()
        self.backend.compress_models = self.m_checkBoxCompressModels.IsChecked()
        self.backend.local_lib = self.m_checkBoxLocalLib.IsChecked()
        self.backend.config.set_value("auto_import", str(self.backend.auto_import))
        self.backend.config.set_value("overwrite_import", str(self.backend.overwrite_import))
        self.backend.config.set_value("local_lib", str(self.backend.local_lib))
        self.backend.config.set_value("single_lib", str(self.m_checkBoxSingleLib.IsChecked()))
        self.backend.config.set_value("lib_name", self.m_textCtrl_libname.GetValue().strip())
        self.backend.config.set_value(
            "compress_models", str(self.m_checkBoxCompressModels.IsChecked())
        )

    def BottonClick(self, event: wx.CommandEvent) -> None:
        """Handle main button click."""
        if self.backend.run_thread:
            self._stop_import()
        elif self._update_backend_settings():
            self._start_import()

        event.Skip()

    def _update_backend_settings(self) -> bool:
        """Sync GUI settings and report whether the destination is usable."""
        # Update single library name
        if self.single_lib_enabled:
            self.backend.importer.lib_name = self.lib_name_override or None
        else:
            self.backend.importer.lib_name = None

        # Handle overwrite setting change
        overwrite_changed = self.overwrite_enabled and not self.backend.overwrite_import
        if overwrite_changed:
            self.backend.folder_handler.known_files = set()

        self._save_settings()

        if self.backend.local_lib:
            project_dir = self._resolve_project_dir()
            if not project_dir:
                return False
            self.backend.importer.set_DEST_PATH(Path(project_dir))
            kicad_link = "${KIPRJMOD}"
        else:
            self.backend.importer.set_DEST_PATH(Path(self.backend.config.get_DEST_PATH()))
            kicad_link = "${KICAD_3RD_PARTY}"

        self.backend.importer.KICAD_3RD_PARTY_LINK = kicad_link
        return True

    def _stop_import(self) -> None:
        """Stop the import process."""
        self.backend.run_thread = False
        self.m_button.Label = "Start"

    def _start_import(self) -> None:
        """Start the import process."""
        self.backend.run_thread = False
        self.backend.find_and_import_new_files()
        self.m_button.Label = "Start"

        if self.backend.auto_import:
            self.backend.run_thread = True
            self.m_button.Label = "automatic import / press to stop"

            import_thread = Thread(target=self.backend.find_and_import_new_files)
            import_thread.start()

        self._check_and_show_library_warnings()

    def _notify_import(self, title: str, message: str, *, kind: str = "success") -> None:
        """Say that an import finished: toast on screen plus a line in the log box."""
        try:
            from .toast import show_toast
        except ImportError:  # pragma: no cover - flat module layout
            try:
                from toast import show_toast  # type: ignore[import-not-found,no-redef]
            except ImportError:
                show_toast = None  # type: ignore[assignment]
        if show_toast is not None:
            show_toast(self, title, message, kind=kind)
        self.backend.print_to_buffer(f"{title}: {message}")

    def _check_and_show_library_warnings(self) -> None:
        """Keep KiCad's settings in sync and only warn when that fails.

        The old flow asked the user to edit Configure Paths and the two library
        tables by hand after every import ("KICAD_3RD_PARTY is not defined").
        The plugin now makes those edits itself and only falls back to the
        instructions when it cannot write the files.
        """
        msg = check_library_import(self.backend, self.kicad_project)
        if not msg:
            return

        result = self._auto_fix_kicad_setup()
        if result is not None and result.changed:
            self._notify_import(
                "KiCad configured",
                "KICAD_3RD_PARTY and the library tables are set up. Restart KiCad to "
                "see the libraries.",
            )
            for change in result.changed:
                logging.info("KiCad setup: %s", change)
                self.backend.print_to_buffer(f"  {change}")
            msg = check_library_import(self.backend, self.kicad_project)
            if not msg:
                return

        self._show_library_warning(msg)

    def _auto_fix_kicad_setup(self) -> Any:
        """Write KICAD_3RD_PARTY and the library entries into KiCad's settings."""
        if self.backend.local_lib:  # project libraries use ${KIPRJMOD}, nothing to fix
            return None
        try:
            try:
                from . import kicad_setup
            except ImportError:  # pragma: no cover - flat module layout
                import kicad_setup  # type: ignore[import-not-found,no-redef]

            dest = Path(self.backend.config.get_DEST_PATH())
            return kicad_setup.apply_setup_for_folder(dest)
        except Exception:
            logging.exception("Automatic KiCad setup failed")
            return None

    def _show_library_warning(self, msg: str) -> None:
        """Show library configuration warning dialog."""
        full_msg = (
            f"{msg}\n\n"
            "More information can be found in the README, section 'Including the "
            "imported libraries in KiCad'.\n"
            "Some configurations require a KiCad restart to be detected correctly."
        )

        dlg = wx.MessageDialog(None, full_msg, "WARNING", wx.OK | wx.ICON_WARNING)

        if dlg.ShowModal() == wx.ID_OK:
            separator = "\n" + "=" * 50 + "\n"
            self.backend.print_to_buffer(separator + full_msg + separator)

    def DirChange(self, event: wx.CommandEvent) -> None:
        """Handle directory path changes."""
        old_src = self.backend.config.get_SRC_PATH()
        new_src = self.m_dirPicker_sourcepath.GetPath()
        self.backend.config.set_SRC_PATH(new_src)
        if old_src != new_src:
            self.backend.folder_handler.known_files = set()
            self._print_path_change("source", new_src)

        new_dest = self.m_dirPicker_librarypath.GetPath()
        if self.backend.local_lib:
            if new_dest and new_dest != self.kicad_project:
                self._set_project_dir(new_dest)
        elif new_dest and new_dest != self.backend.config.get_DEST_PATH():
            self.backend.config.set_DEST_PATH(new_dest)
            self.backend.folder_handler.known_files = set()
            self._print_path_change("destination", new_dest)

        event.Skip()

    # ------------------------------------------------------------------
    # Component search window (SnapEDA + EasyEDA tabs)
    # ------------------------------------------------------------------

    def _open_component_search(self, initial_tab: int = 0) -> None:
        """Show the search window non-modally and bring it to the front.

        Non-modal on purpose: the main window is modal itself and a modal dialog
        stacked on it swallowed the first click on every button.
        """
        try:
            try:
                from .snapeda_search import CombinedSearchDialog
            except ImportError:
                from snapeda_search import (  # type: ignore[import-not-found,no-redef]
                    CombinedSearchDialog,
                )

            if self.search_dialog is not None and not self.search_dialog:
                # The user closed the window; the Python reference went stale.
                self.search_dialog = None
            if self.search_dialog is None:
                self.search_dialog = CombinedSearchDialog(
                    self,
                    plugin_dir=Path(__file__).resolve().parent,
                    on_select_lcsc=self.m_textCtrl2.SetValue,
                    on_import_zip=self._import_snapeda_zip,
                    on_import_cse=self._import_cse_zip,
                    on_import_easyeda=self._import_easyeda_number,
                    library_dir=self._library_dir(),
                    on_install_libraries=self._install_library_paths,
                    initial_tab=initial_tab,
                )
            else:
                self.search_dialog.select_tab(initial_tab)
                self.search_dialog.set_library_dir(self._library_dir())

            # Show after the click handler returns: showing the window inside the
            # button event let the matching mouse-up land on the new window, which
            # closed it again or clicked a button under the cursor (the "needs a
            # double click" symptom).
            wx.CallAfter(self._reveal_search_dialog)
        except Exception as e:
            logging.exception("Component search failed")
            wx.MessageBox(
                f"The component search could not be opened:\n\n{e}",
                "Component search",
                wx.OK | wx.ICON_ERROR,
            )

    def _reveal_search_dialog(self) -> None:
        """Show / raise the (already created) search window."""
        dialog = self.search_dialog
        if dialog is None or not dialog:
            self.search_dialog = None
            return
        try:
            if not dialog.IsShown():
                dialog.Show()
            dialog.Raise()
            dialog.focus_search()
        except Exception:  # noqa: BLE001 - window closing under our feet
            logging.debug("Could not raise the search window", exc_info=True)
            self.search_dialog = None

    # ------------------------------------------------------------------
    # Library folder: inventory tab + installing the paths in KiCad
    # ------------------------------------------------------------------

    def _library_dir(self, *, prompt: bool = False) -> Path:
        """Folder the importer writes to right now (global or project library)."""
        if self.backend.local_lib:
            project = self._resolve_project_dir() if prompt else self.kicad_project
            if project:
                return Path(project)
        return Path(self.backend.config.get_DEST_PATH())

    def _refresh_library_tab(self) -> None:
        """Keep the Library tab in step after an import."""
        dialog = self.search_dialog
        if dialog is not None and dialog:
            dialog.set_library_dir(self._library_dir())

    def _install_library_paths(self, *, notify: bool = True) -> str:
        """Write the plugin's libraries into KiCad's tables; returns a summary.

        This is what the "Install library paths in KiCad" button runs, and the
        same call the plugin makes after an import - so a first installation is
        wired up without importing a part first.
        """
        if not self._update_backend_settings():
            return "No usable library folder: pick the project or destination first."
        dest = self._library_dir()
        try:
            try:
                from . import kicad_setup
            except ImportError:  # pragma: no cover - flat module layout
                import kicad_setup  # type: ignore[import-not-found,no-redef]

            if self.backend.local_lib:
                project = self._resolve_project_dir()
                if not project:
                    return "No project folder found for the local library."
                result = kicad_setup.apply_project_setup(Path(project), dest)
            else:
                result = kicad_setup.apply_setup_for_folder(dest)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing
            logging.exception("KiCad library setup failed")
            return f"Library setup failed: {exc}"

        if not result.changed and not result.problems:
            return f"KiCad already lists the libraries of {dest}."
        self.backend.print_to_buffer(f"KiCad library setup ({dest}):")
        for line in result.summary().splitlines():
            self.backend.print_to_buffer(f"  {line}")
        if notify and result.changed:
            self._notify_import(
                "KiCad configured",
                "Library paths installed. Restart KiCad to see the libraries.",
            )
        if result.problems:
            return result.summary().replace("\n", "  ")
        return f"{len(result.changed)} change(s): " + "; ".join(result.changed[:4])

    def _install_library_paths_on_start(self) -> None:
        """First run / new KiCad version: wire existing libraries without a click."""
        try:
            try:
                from . import kicad_setup
            except ImportError:  # pragma: no cover - flat module layout
                import kicad_setup  # type: ignore[import-not-found,no-redef]

            dest = self._library_dir()
            if not dest.is_dir() or not kicad_setup.library_names(dest):
                return
            if self.backend.local_lib:
                # No project folder yet: never open the picker unasked on start-up.
                project = self.kicad_project
                if not project or not kicad_setup.project_pending_fix(Path(project), dest):
                    return
            elif not kicad_setup.pending_fix(dest):
                return
        except Exception:  # noqa: BLE001 - never block the main window
            logging.debug("First-run library check failed", exc_info=True)
            return
        self._install_library_paths()

    def _import_easyeda_number(self, lcsc: str) -> None:
        """Import a part chosen in the EasyEDA tab."""
        self.m_textCtrl2.SetValue(lcsc)
        self._import_easyeda()

    def _import_snapeda_zip(self, zip_path: Path, part: SnapedaPart) -> None:
        """Import a downloaded SnapEDA ZIP with the existing importer."""
        self._import_part_zip(zip_path, part, source="SnapEDA")

    def _import_cse_zip(self, zip_path: Path, part: Any) -> None:
        """Import a ZIP downloaded from Component Search Engine (Samacsys format)."""
        self._import_part_zip(zip_path, part, source="Samacsys")

    def _import_part_zip(self, zip_path: Path, part: Any, *, source: str) -> None:
        """Import a downloaded part ZIP and report exactly what was written.

        Used by both search tabs: the source only changes the wording, the library
        name comes from the importer (``Snapeda`` / ``Samacsys``), which detects the
        archive format itself. KiCad's environment variable and library tables are
        checked afterwards, so a new source is installed the same way.
        """
        if not self._update_backend_settings():
            self.backend.print_to_buffer(f"{source} import skipped: no usable library folder.")
            return

        self.backend.importer.compress_models = self.compress_models_enabled
        manufacturer_name = str(getattr(part, "manufacturer", "") or "")
        manufacturer = f" ({manufacturer_name})" if manufacturer_name else ""
        self.backend.print_to_buffer(
            f"Importing {source} part {part.part_number}{manufacturer} from {zip_path.name}"
        )
        libs: tuple[str, ...] | None = None
        try:
            libs = self.backend.importer.import_all(
                Path(zip_path), overwrite_if_exists=self.overwrite_enabled
            )
            logging.info("%s import finished for %s", source, part.part_number)
        except Exception as e:
            self.backend.print_to_buffer(f"{source} import error: {e}")
            logging.exception("%s import failed", source)
            self._notify_import(
                f"{source}: {part.part_number}",
                f"Import failed: {e}",
                kind="warning",
            )
            return
        finally:
            self.backend.print_to_buffer("")

        importer = self.backend.importer
        linked = ", ".join(importer.model_linked[:3])
        if libs and "OK" in libs:
            if importer.model_saved is not None and linked:
                message = f"Symbol, footprint and 3D model added; model linked to {linked}"
            elif importer.model_saved is not None:
                message = (
                    "Symbol and footprint added; the 3D model was saved but no footprint "
                    "matched it (import the symbol and footprint first, then the model again)"
                )
            else:
                message = f"Symbol and footprint added - this {source} export has no 3D model"
                if getattr(part, "has_3d", False):
                    message += ". Download the STEP model from the part page and import it again"
            self._notify_import(f"{source}: {part.part_number} imported", message)
        elif libs:
            detail = (
                "the 3D model is saved but not linked to a footprint yet"
                if importer.model_saved is not None and not linked
                else "some files were skipped (see the log)"
            )
            self._notify_import(
                f"{source}: {part.part_number}",
                f"Imported, but {detail}.",
                kind="warning",
            )
        else:
            self._notify_import(
                f"{source}: {part.part_number}",
                "Nothing new was written (the part may already be in the library).",
                kind="warning",
            )
        self._refresh_library_tab()
        self._check_and_show_library_warnings()

    def OnComponentSearch(self, event: wx.CommandEvent) -> None:
        """Open the component search window (SnapEDA + CSE + EasyEDA tabs)."""
        self._open_component_search(initial_tab=0)
        event.Skip()

    def ButtomManualImport(self, event: wx.CommandEvent) -> None:
        """Handle manual EasyEDA import."""
        self._import_easyeda()
        event.Skip()

    def _import_easyeda(self) -> None:
        """Import the component entered in the LCSC field."""
        if not self._update_backend_settings():
            return
        try:
            self._perform_easyeda_import()
        except Exception as e:
            error_msg = f"Error: {e}\nPython version: {sys.version}"
            self.backend.print_to_buffer(error_msg)
            logging.exception("EasyEDA import failed")
        self._refresh_library_tab()
        self._check_and_show_library_warnings()

    def _perform_easyeda_import(self) -> None:
        """Perform EasyEDA component import."""
        try:
            from .impart_easyeda import ImportConfig, import_easyeda_component
        except ImportError:
            try:
                from impart_easyeda import (  # type: ignore[no-redef]
                    ImportConfig,
                    import_easyeda_component,
                )
            except ImportError as e:
                error_msg = f"Failed to import EasyEDA module: {e}\n\nThis usually means easyeda2kicad is not properly installed or has missing dependencies."
                self.backend.print_to_buffer(error_msg)
                logging.error(f"EasyEDA import module not available: {e}")

                wx.MessageBox(
                    f"EasyEDA Import Error!\n\n{error_msg}\n\n"
                    "Solutions:\n"
                    "1. Run 'install_dependencies.py' to reinstall dependencies\n"
                    "2. Check plugin.log for detailed error information\n"
                    "3. Restart KiCad after fixing dependencies",
                    "Import Error",
                    wx.OK | wx.ICON_ERROR,
                )
                return

        config = ImportConfig(
            base_folder=self.backend.importer.DEST_PATH,
            lib_name=self.backend.importer.lib_name or "EasyEDA",
            overwrite=self.overwrite_enabled,
            lib_var=self.backend.importer.KICAD_3RD_PARTY_LINK,
            compress_models=self.compress_models_enabled,
        )

        component_id = self.m_textCtrl2.GetValue().strip()

        try:
            paths = import_easyeda_component(
                component_id=component_id,
                config=config,
                print_func=self.backend.print_to_buffer,
            )
            self.backend.print_to_buffer("")
            logging.info(f"Successfully imported EasyEDA component {component_id}")
            created = sum(1 for path in paths if path)
            label = paths.symbol_lib.stem if paths.symbol_lib else config.lib_name
            self._notify_import(
                f"EasyEDA: {component_id} imported",
                f"{created} files added to {label}",
                kind="success" if created else "warning",
            )

        except ValueError as e:
            error_msg = f"Invalid component ID {component_id}: {e}"
            logging.error(error_msg)
            wx.MessageBox(error_msg, "Invalid Component ID", wx.OK | wx.ICON_WARNING)

        except RuntimeError as e:
            error_msg = f"Runtime error importing {component_id}: {e}"
            logging.error(error_msg)
            wx.MessageBox(error_msg, "Import Error", wx.OK | wx.ICON_ERROR)

        except Exception as e:
            error_msg = f"Unexpected error during import: {e}"
            self.backend.print_to_buffer(error_msg)
            logging.exception(f"Unexpected error importing {component_id}")
            wx.MessageBox(error_msg, "Unexpected Error", wx.OK | wx.ICON_ERROR)


def create_backend_handler() -> ImpartBackend:
    """Create a new backend handler instance."""
    try:
        backend = ImpartBackend()
        logging.info("Created new backend handler")
        return backend
    except Exception:
        logging.exception("Failed to create backend handler")
        raise


if __name__ == "__main__":
    logging.info("Starting application in standalone mode")

    if instance_manager.is_already_running():
        logging.info("Plugin already running - focus command sent")
        sys.exit(0)

    try:
        app = wx.App()
        frontend = ImpartFrontend(fallback_mode=False)

        if not instance_manager.start_server(frontend):
            logging.warning("Failed to start IPC server - continuing anyway")

        wx.CallAfter(frontend.Raise)
        frontend.ShowModal()
        frontend.Destroy()
        logging.info("Application finished successfully")

    except Exception:
        logging.exception("Failed to run standalone application")
        raise
    finally:
        instance_manager.stop_server()
