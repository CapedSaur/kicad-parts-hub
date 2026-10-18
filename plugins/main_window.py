"""Layout of the main window: header band, two cards, log and footer.

The generated dialog (``impart_gui.py``) is a flat stack of controls from the
original impart project. This module rebuilds it as one screen with a clear
reading order, which is also what makes the two real jobs of the window obvious:

1. where the libraries are installed (global or project folder) and
2. how a part gets in (the component search, the library manager, or a dropped ZIP).

The controls themselves are **re-parented, not recreated**, so every event
binding of :class:`impart_action.ImpartFrontend` keeps working unchanged.

wx requires every window in a sizer to be a child of the window that owns that
sizer, so the layout is built out of plain panels (one per card, each a child of
its host panel) instead of ``wx.StaticBox``. The function is defensive on
purpose: if anything here fails, the plugin keeps the old layout instead of not
opening.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import wx

logger = logging.getLogger(__name__)

PAD = 8
GAP = 8


def _bold(widget: wx.Window, factor: float = 1.0) -> None:
    try:
        font = widget.GetFont()
        font.MakeBold()
        if factor != 1.0:
            font = font.Scaled(factor)
        widget.SetFont(font)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not set a bold font", exc_info=True)


def _muted(widget: wx.Window, factor: float = 0.92) -> None:
    try:
        font = widget.GetFont()
        widget.SetFont(font.Scaled(factor))
        widget.SetForegroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_GRAYTEXT))
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not mute a label", exc_info=True)


def _icon_bitmap(size: int) -> wx.Bitmap | None:
    base = Path(__file__).resolve().parent
    for candidate in (
        base / "icon.png",
        base.parent / "resources" / "icon_256.png",
        base.parent / "resources" / "icon.png",
    ):
        if not candidate.is_file():
            continue
        try:
            image = wx.Image(str(candidate), wx.BITMAP_TYPE_PNG)
            if not image.IsOk():
                continue
            if image.GetWidth() != size:
                image = image.Scale(size, size, wx.IMAGE_QUALITY_HIGH)
            return wx.Bitmap(image)
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not load %s", candidate, exc_info=True)
    return None


def update_status(dialog: Any) -> None:
    """Refresh the footer line with the current library mode and folder."""
    label = getattr(dialog, "m_partsHubStatus", None)
    if label is None:
        return
    try:
        mode, destination = dialog._destination_info()
        label.SetLabel(f"{mode}   ·   {destination}")
        dialog.Layout()
    except Exception:  # noqa: BLE001 - the footer is a convenience
        logger.debug("Could not update the status line", exc_info=True)


def _card(host: wx.Window, label: str) -> tuple[wx.Panel, wx.BoxSizer]:
    """A titled block: a panel with a heading, a separator and a content sizer."""
    panel = wx.Panel(host)
    try:
        panel.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE))
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not tint a card", exc_info=True)
    sizer = wx.BoxSizer(wx.VERTICAL)
    heading = wx.StaticText(panel, label=label)
    _bold(heading)
    sizer.Add(heading, 0, wx.ALL, PAD)
    sizer.Add(wx.StaticLine(panel), 0, wx.EXPAND | wx.LEFT | wx.RIGHT, PAD)
    panel.SetSizer(sizer)
    return panel, sizer


def restyle_main_window(
    dialog: Any,
    *,
    title: str,
    subtitle: str,
    version: str = "",
    icon_size: int = 48,
) -> dict[str, wx.Window]:
    """Rebuild ``dialog`` as the card layout; returns the new widgets."""
    shell = wx.BoxSizer(wx.VERTICAL)
    made: dict[str, wx.Window] = {}

    _add_header(
        dialog, shell, title=title, subtitle=subtitle, version=version, size=icon_size, made=made
    )
    _add_body(dialog, shell, made)
    _add_log(dialog, shell, made)
    _add_footer(dialog, shell, made)

    dialog.SetSizer(shell)
    _hide_leftovers(dialog, made)
    try:
        dialog.SetMinSize(wx.Size(720, 600))
        if dialog.GetSize().GetWidth() < 780:
            dialog.SetSize(wx.Size(840, 720))
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not size the main window", exc_info=True)
    dialog.Layout()
    return made


def _hide_leftovers(dialog: Any, made: dict[str, wx.Window]) -> None:
    """Hide generated controls the new layout does not use, so none floats at (0, 0)."""
    kept = set(made.values())
    for child in dialog.GetChildren():
        try:
            if child in kept or child.GetContainingSizer() is not None:
                continue
            child.Hide()
        except Exception:  # noqa: BLE001 - layout is cosmetic
            logger.debug("Could not sweep a leftover control", exc_info=True)


def _reparent(dialog: Any, name: str, parent: wx.Window) -> wx.Window | None:
    """Move one generated control into the new layout, keeping its bindings."""
    widget = getattr(dialog, name, None)
    if widget is None:
        return None
    try:
        sizer = widget.GetContainingSizer()
        if sizer is not None:
            sizer.Detach(widget)
        widget.Reparent(parent)
    except Exception:  # noqa: BLE001 - keep the control where it is
        logger.warning("Could not re-parent %s", name, exc_info=True)
        return None
    return widget


def _add_header(
    dialog: Any,
    shell: wx.BoxSizer,
    *,
    title: str,
    subtitle: str,
    version: str,
    size: int,
    made: dict[str, wx.Window],
) -> None:
    band = wx.Panel(dialog)
    try:
        band.SetBackgroundColour(wx.SystemSettings.GetColour(wx.SYS_COLOUR_BTNFACE))
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not tint the header", exc_info=True)
    row = wx.BoxSizer(wx.HORIZONTAL)

    bitmap = _icon_bitmap(size)
    if bitmap is not None:
        row.Add(wx.StaticBitmap(band, bitmap=bitmap), 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 12)

    names = wx.BoxSizer(wx.VERTICAL)
    heading = wx.StaticText(band, label=title)
    _bold(heading, 1.5)
    tagline = wx.StaticText(band, label=subtitle)
    _muted(tagline)
    names.Add(heading, 0)
    names.Add(tagline, 0, wx.TOP, 2)
    row.Add(names, 1, wx.ALIGN_CENTER_VERTICAL)

    if version:
        chip = wx.StaticText(band, label=version)
        _muted(chip, 0.88)
        row.Add(chip, 0, wx.ALIGN_TOP | wx.LEFT, 8)
        made["version"] = chip

    band.SetSizer(row)
    band.SetMinSize(wx.Size(-1, size + 24))
    shell.Add(band, 0, wx.EXPAND)
    made["header"] = band


def _add_body(dialog: Any, shell: wx.BoxSizer, made: dict[str, wx.Window]) -> None:
    body = wx.Panel(dialog)
    columns = wx.BoxSizer(wx.HORIZONTAL)

    # -- card 1: where the libraries live ---------------------------------
    where, content = _card(body, "1 · Where the libraries are installed")
    picker = _reparent(dialog, "m_dirPicker_librarypath", where)
    if picker is not None:
        content.Add(picker, 0, wx.EXPAND | wx.LEFT | wx.RIGHT, PAD)
    local = _reparent(dialog, "m_checkBoxLocalLib", where)
    if local is not None:
        try:
            local.SetLabel("Install inside the project folder (${KIPRJMOD})")
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not relabel the local-library checkbox", exc_info=True)
        content.Add(local, 0, wx.ALL, PAD)

    actions = wx.BoxSizer(wx.HORIZONTAL)
    open_button = wx.Button(where, label="Open folder")
    install_button = wx.Button(where, label="Install in KiCad")
    _bold(install_button)
    install_button.SetToolTip(
        "Write KICAD_3RD_PARTY and the symbol/footprint tables so the imported "
        "libraries appear in the schematic and board editors"
    )
    open_button.Bind(wx.EVT_BUTTON, lambda _event: _open_destination(dialog))
    install_button.Bind(wx.EVT_BUTTON, lambda _event: _install_libraries(dialog))
    actions.Add(open_button, 0, wx.RIGHT, 6)
    actions.Add(install_button, 0)
    content.Add(actions, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD)

    hint = wx.StaticText(
        where, label="The plugin writes the libraries here and can point KiCad at them."
    )
    _muted(hint)
    content.Add(hint, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD)

    made["open_folder"] = open_button
    made["install"] = install_button
    columns.Add(where, 3, wx.EXPAND | wx.RIGHT, GAP)

    # -- card 2: how a part gets in ---------------------------------------
    finding, content2 = _card(body, "2 · Add a part")
    search = _reparent(dialog, "m_buttonComponentSearch", finding)
    if search is not None:
        try:
            search.SetLabel("Search components…")
            search.SetMinSize(wx.Size(-1, 44))
            _bold(search, 1.15)
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not restyle the search button", exc_info=True)
        content2.Add(search, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, PAD)

    library_button = wx.Button(finding, label="Library manager…")
    library_button.SetToolTip("What is installed, with a preview of each part")
    library_button.Bind(wx.EVT_BUTTON, lambda _event: _open_library(dialog))
    content2.Add(library_button, 0, wx.EXPAND | wx.ALL, PAD)
    made["library"] = library_button

    drop_hint = wx.StaticText(
        finding, label="…or drop a ZIP (Snapeda, Samacsys, UltraLibrarian) in the log below."
    )
    _muted(drop_hint)
    try:
        drop_hint.Wrap(220)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not wrap the drop hint", exc_info=True)
    content2.Add(drop_hint, 1, wx.LEFT | wx.RIGHT | wx.BOTTOM, PAD)
    columns.Add(finding, 2, wx.EXPAND)

    body.SetSizer(columns)
    shell.Add(body, 0, wx.EXPAND | wx.ALL, GAP)
    made["body"] = body


def _add_log(dialog: Any, shell: wx.BoxSizer, made: dict[str, wx.Window]) -> None:
    card, content = _card(dialog, "Log")
    log = _reparent(dialog, "m_text", card)
    if log is not None:
        try:
            font = log.GetFont()
            font.SetFaceName("Consolas" if wx.Platform == "__WXMSW__" else "monospace")
            log.SetFont(font)
        except Exception:  # noqa: BLE001 - cosmetic only
            logger.debug("Could not set the log font", exc_info=True)
        content.Add(log, 1, wx.EXPAND | wx.ALL, PAD)
    shell.Add(card, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, GAP)
    made["log_card"] = card


def _add_footer(dialog: Any, shell: wx.BoxSizer, made: dict[str, wx.Window]) -> None:
    line = wx.BoxSizer(wx.HORIZONTAL)
    status = wx.StaticText(dialog, label="")
    _muted(status)
    line.Add(status, 1, wx.ALIGN_CENTER_VERTICAL | wx.LEFT, GAP)
    made["status"] = status
    dialog.m_partsHubStatus = status

    close_button = wx.Button(dialog, label="Close")
    close_button.Bind(wx.EVT_BUTTON, lambda _event: dialog.Close())
    line.Add(close_button, 0)
    made["close"] = close_button

    shell.Add(line, 0, wx.EXPAND | wx.ALL, GAP)
    update_status(dialog)


def _open_destination(dialog: Any) -> None:
    try:
        folder = dialog.backend.config.get_DEST_PATH()
        Path(folder).mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultApplication(folder)
    except Exception as exc:  # noqa: BLE001 - report, never crash the window
        logger.warning("Could not open the library folder: %s", exc)


def _install_libraries(dialog: Any) -> None:
    """Run the same setup the button of the library tab runs."""
    installer = getattr(dialog, "_install_library_paths", None)
    if installer is None:
        return
    try:
        message = installer()
    except Exception as exc:  # noqa: BLE001 - report, never crash the window
        logger.exception("KiCad library setup failed")
        message = f"Library setup failed: {exc}"
    try:
        dialog.backend.print_to_buffer(message)
    except Exception:  # noqa: BLE001 - the log is a convenience
        logger.debug("Could not write to the log", exc_info=True)
    update_status(dialog)


def _open_library(dialog: Any) -> None:
    opener = getattr(dialog, "_open_component_search", None)
    if opener is None:
        return
    try:
        opener(initial_tab=3)
    except Exception:  # noqa: BLE001 - report, never crash the window
        logger.exception("Could not open the library manager")
