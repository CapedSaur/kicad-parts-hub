"""Library tab: see what the plugin imported and delete what is not wanted.

The plugin writes into one folder (the SnapEDA / Samacsys / EasyEDA library
folder). Until now the only way to see what was in it - and to remove a part that
was imported by mistake - was the file explorer. This tab lists every part with
its symbol, footprint and 3D model, and removes the selected ones.

It also carries the "install in KiCad" button: the same setup the plugin runs
after an import (``KICAD_3RD_PARTY`` + ``sym-lib-table`` / ``fp-lib-table``), so
a fresh install can be wired into KiCad without importing a part first.

File work lives in :mod:`library_scan` (no wx, tested without KiCad).
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from pathlib import Path

import wx

try:
    from . import library_preview as preview
    from .library_scan import (
        PartEntry,
        delete_part,
        inventory_summary,
        scan_parts,
    )
except ImportError:  # standalone / sys.path import (tests, CLI)
    import library_preview as preview  # type: ignore[import-not-found,no-redef]
    from library_scan import (  # type: ignore[import-not-found,no-redef]
        PartEntry,
        delete_part,
        inventory_summary,
        scan_parts,
    )

logger = logging.getLogger(__name__)

CANVAS_SIZE = (320, 250)
CANVAS_BACKGROUND = (30, 32, 36)
PALETTE = {
    preview.SILK: (232, 224, 192),
    preview.COPPER: (205, 96, 76),
    preview.FAB: (124, 152, 196),
    preview.EDGE: (150, 150, 150),
}
PREVIEW_CACHE_LIMIT = 24

COLUMNS: list[tuple[str, int]] = [
    ("Part", 240),
    ("Library", 110),
    ("Symbol", 70),
    ("Footprint", 80),
    ("3D model", 80),
]

YES = "\u2713"


class LibraryPanel(wx.Panel):  # type: ignore[misc]
    """Browse the plugin's library folder, delete parts, install the paths in KiCad."""

    def __init__(
        self,
        parent: wx.Window,
        *,
        root_dir: Path | None = None,
        on_install: Callable[[], str] | None = None,
        on_changed: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self.root_dir = Path(root_dir) if root_dir else None
        self.on_install = on_install
        self.on_changed = on_changed
        self._parts: list[PartEntry] = []
        self._visible: list[PartEntry] = []
        self._preview_key: tuple[str, str, str] | None = None
        self._preview_cache: dict[tuple[str, str, str], object] = {}
        self._build_ui()
        self.refresh()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = wx.BoxSizer(wx.VERTICAL)

        top = wx.BoxSizer(wx.HORIZONTAL)
        caption = wx.StaticText(self, label="Library folder:")
        _bold(caption)
        top.Add(caption, 0, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)
        self.path_label = wx.StaticText(self, label="-")
        top.Add(self.path_label, 1, wx.ALIGN_CENTER_VERTICAL | wx.RIGHT, 6)

        self.btn_refresh = wx.Button(self, label="Refresh")
        self.btn_install = wx.Button(self, label="Install library paths in KiCad")
        self.btn_install.SetToolTip(
            "Write KICAD_3RD_PARTY and the sym-lib-table / fp-lib-table entries so the "
            "imported libraries show up in the schematic and board editors"
        )
        _bold(self.btn_install)
        self.btn_folder = wx.Button(self, label="Open folder")
        for button in (self.btn_refresh, self.btn_install, self.btn_folder):
            top.Add(button, 0, wx.LEFT, 4)
        root.Add(top, 0, wx.EXPAND | wx.ALL, 8)

        filter_row = wx.BoxSizer(wx.HORIZONTAL)
        self.filter_ctrl = wx.TextCtrl(self, style=wx.TE_PROCESS_ENTER)
        self.filter_ctrl.SetHint("Filter parts (name or library) ...")
        filter_row.Add(self.filter_ctrl, 1, wx.EXPAND | wx.RIGHT, 6)
        self.btn_delete = wx.Button(self, label="Delete selected")
        self.btn_delete.SetToolTip("Remove the symbol block, footprint and 3D files of the parts")
        _bold(self.btn_delete)
        filter_row.Add(self.btn_delete, 0)
        root.Add(filter_row, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.list_ctrl = wx.ListCtrl(self, style=wx.LC_REPORT | wx.LC_SINGLE_SEL | wx.BORDER_SUNKEN)
        for index, (label, width) in enumerate(COLUMNS):
            self.list_ctrl.InsertColumn(index, label, width=width)
        try:
            self.list_ctrl.EnableAlternateRowColours(True)
        except Exception:  # noqa: BLE001 - theme dependent, cosmetic only
            logger.debug("Alternate row colours not available")
        root.Add(self.list_ctrl, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        bottom = wx.BoxSizer(wx.HORIZONTAL)
        self.detail = wx.TextCtrl(self, style=wx.TE_MULTILINE | wx.TE_READONLY)
        self.detail.SetMinSize(wx.Size(-1, 96))
        self.detail.SetHint("Select a part to see its files")
        bottom.Add(self.detail, 1, wx.EXPAND | wx.RIGHT, 8)
        bottom.Add(self._build_preview_pane(), 0, wx.EXPAND)
        root.Add(bottom, 0, wx.EXPAND | wx.ALL, 8)

        self.status = wx.StaticText(self, label="")
        _bold(self.status)
        root.Add(self.status, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        self.SetSizer(root)

        self.btn_refresh.Bind(wx.EVT_BUTTON, self._on_refresh)
        self.btn_install.Bind(wx.EVT_BUTTON, self._on_install)
        self.btn_folder.Bind(wx.EVT_BUTTON, self._on_open_folder)
        self.btn_delete.Bind(wx.EVT_BUTTON, self._on_delete)
        self.filter_ctrl.Bind(wx.EVT_TEXT, self._on_filter)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selected)
        self.list_ctrl.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_selected)

    def _build_preview_pane(self) -> wx.Sizer:
        """The quick look at the selected part: symbol, footprint or 3D model."""
        box = wx.StaticBox(self, label=" Preview ")
        sizer = wx.StaticBoxSizer(box, wx.VERTICAL)

        self.preview_canvas = wx.StaticBitmap(box)
        self.preview_canvas.SetMinSize(wx.Size(*CANVAS_SIZE))
        sizer.Add(self.preview_canvas, 1, wx.EXPAND | wx.ALL, 4)

        self.preview_info = wx.StaticText(box, label="Select a part")
        sizer.Add(self.preview_info, 0, wx.LEFT | wx.RIGHT | wx.BOTTOM, 4)

        buttons = wx.BoxSizer(wx.HORIZONTAL)
        self.preview_buttons: dict[str, wx.ToggleButton] = {}
        for kind, label in (("symbol", "Symbol"), ("footprint", "Footprint"), ("model", "3D")):
            button = wx.ToggleButton(box, label=label)
            button.Bind(wx.EVT_TOGGLEBUTTON, lambda event, k=kind: self._on_preview_choice(k))
            buttons.Add(button, 1, wx.RIGHT, 4)
            self.preview_buttons[kind] = button
        sizer.Add(buttons, 0, wx.EXPAND | wx.ALL, 4)

        self._paint_placeholder("Select a part")
        return sizer

    # ------------------------------------------------------------------
    # data
    # ------------------------------------------------------------------

    def set_root(self, root_dir: Path | None) -> None:
        """Point the panel at a (new) library folder and reload."""
        self.root_dir = Path(root_dir) if root_dir else None
        self.refresh()

    def refresh(self) -> None:
        """Re-read the library folder from disk."""
        root = self.root_dir
        if root is None or not root.is_dir():
            self._parts = []
            self.path_label.SetLabel("no library folder selected")
            self._populate()
            self._set_status("No library folder yet - import a part or pick the destination first.")
            return
        try:
            self._parts = scan_parts(root)
        except Exception as exc:  # noqa: BLE001 - a broken library must not break the tab
            logger.exception("Could not scan %s", root)
            self._parts = []
            self._set_status(f"Could not read {root}: {exc}")
        self.path_label.SetLabel(str(root))
        self._populate()
        if self._parts:
            self._set_status(inventory_summary(self._parts))
        else:
            self._set_status("The library folder is empty.")
        self.Layout()

    def _populate(self) -> None:
        self.list_ctrl.DeleteAllItems()
        needle = self.filter_ctrl.GetValue().strip().lower()
        self._visible = [
            part
            for part in self._parts
            if not needle or needle in part.name.lower() or needle in part.library.lower()
        ]
        for row, part in enumerate(self._visible):
            self.list_ctrl.InsertItem(row, part.name)
            self.list_ctrl.SetItem(row, 1, part.library)
            self.list_ctrl.SetItem(row, 2, YES if part.has_symbol else "")
            self.list_ctrl.SetItem(row, 3, YES if part.has_footprint else "")
            self.list_ctrl.SetItem(row, 4, YES if part.has_3d else "")
        self.btn_delete.Enable(bool(self._visible))
        self.detail.SetValue("")
        self._preview_key = None
        self.preview_info.SetLabel("Select a part")
        for button in self.preview_buttons.values():
            button.SetValue(False)
            button.Enable(False)
        self._paint_placeholder("Select a part")

    def _on_refresh(self, _: wx.CommandEvent) -> None:
        self.refresh()

    def _on_filter(self, _: wx.CommandEvent) -> None:
        self._populate()
        self._set_status(
            f"{len(self._visible)} of {len(self._parts)} parts shown" if self._parts else ""
        )

    def _selected_part(self) -> PartEntry | None:
        index = self.list_ctrl.GetFirstSelected()
        if index < 0 or index >= len(self._visible):
            return None
        return self._visible[index]

    def _on_selected(self, _: wx.ListEvent) -> None:
        part = self._selected_part()
        if part is None:
            return
        lines = [part.describe()]
        if not part.is_complete:
            lines.append(f"  missing:   {', '.join(part.missing)}")
        self.detail.SetValue("\n".join(lines))
        self.show_preview_for(part)

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def _paint_placeholder(self, message: str) -> None:
        """A plain canvas with a message (no part, no model, error)."""
        width, height = CANVAS_SIZE
        bitmap = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bitmap)
        try:
            dc.SetBackground(wx.Brush(CANVAS_BACKGROUND))
            dc.Clear()
            dc.SetTextForeground((150, 150, 155))
            dc.DrawText(message[:60], 8, 8)
        finally:
            dc.SelectObject(wx.NullBitmap)
        self.preview_canvas.SetBitmap(bitmap)
        self.preview_canvas.Refresh()

    def _availability(self, part: PartEntry | None) -> dict[str, bool]:
        return {
            "symbol": bool(part and part.has_symbol),
            "footprint": bool(part and part.has_footprint),
            "model": bool(part and part.has_3d),
        }

    def show_preview_for(self, part: PartEntry | None) -> None:
        """Show the first available view of ``part``, keeping the current choice."""
        available = self._availability(part)
        for kind, button in self.preview_buttons.items():
            button.Enable(available[kind])
        chosen = next(
            (
                kind
                for kind, button in self.preview_buttons.items()
                if button.GetValue() and available[kind]
            ),
            None,
        )
        if chosen is None:
            chosen = next(
                (kind for kind in ("symbol", "footprint", "model") if available[kind]), None
            )
        if chosen is None:
            self._preview_key = None
            for button in self.preview_buttons.values():
                button.SetValue(False)
            self._paint_placeholder("No symbol, footprint or 3D model")
            self.preview_info.SetLabel("Nothing to preview for this part")
            return
        self._select_kind(chosen, part)

    def _select_kind(self, kind: str, part: PartEntry | None = None) -> None:
        for name, button in self.preview_buttons.items():
            button.SetValue(name == kind)
        self._start_preview(kind, part)

    def _on_preview_choice(self, kind: str) -> None:
        self._select_kind(kind)

    def _start_preview(self, kind: str, part: PartEntry | None = None) -> None:
        part = part or self._selected_part()
        if part is None:
            return
        key = (part.library, part.name, kind)
        self._preview_key = key
        cached = self._preview_cache.get(key)
        if cached is not None:
            self._apply_preview(key, cached)
            return
        self.preview_info.SetLabel(f"Reading the {kind} ...")
        threading.Thread(target=self._preview_worker, args=(part, kind, key), daemon=True).start()

    def _preview_worker(self, part: PartEntry, kind: str, key: tuple[str, str, str]) -> None:
        try:
            payload = self._build_preview_payload(part, kind)
        except Exception as exc:  # noqa: BLE001 - a broken file must not kill the tab
            logger.exception("Preview of %s failed", part.name)
            payload = ("error", f"Preview failed: {exc}")
        wx.CallAfter(self._apply_preview, key, payload)

    def _build_preview_payload(self, part: PartEntry, kind: str) -> tuple:
        """Parse what the worker needs (runs off the GUI thread)."""
        if kind == "symbol":
            if part.symbol_file is None:
                return ("error", "No symbol for this part")
            text = part.symbol_file.read_text(encoding="utf-8", errors="replace")
            drawing = preview.symbol_drawing(text, part.name)
            if drawing is None:
                return ("error", f"Symbol {part.name} not found in {part.symbol_file.name}")
            info = f"Symbol {part.name}"
            if drawing.meta.get("pins"):
                info += f" - {drawing.meta['pins']} pins"
            return ("drawing", drawing, True, info)

        if kind == "footprint":
            if part.footprint_file is None:
                return ("error", "No footprint for this part")
            text = part.footprint_file.read_text(encoding="utf-8", errors="replace")
            drawing = preview.footprint_drawing(text)
            if drawing is None:
                return ("error", f"{part.footprint_file.name} is not a footprint")
            info = f"Footprint {drawing.label or part.footprint_file.stem}"
            if drawing.meta.get("pads"):
                info += f" - {drawing.meta['pads']} pads"
            return ("drawing", drawing, False, info)

        candidates = self._model_candidates(part)
        if not candidates:
            logger.info("No 3D model for %s: %s", part.name, self._no_model_message(part))
            return ("error", self._no_model_message(part))
        broken: list[str] = []
        for model in candidates:
            try:
                mesh = preview.mesh_from_file(model)
            except Exception as exc:  # noqa: BLE001 - try the next file
                logger.warning("Could not read %s: %s", model, exc)
                broken.append(f"{model.name}: {exc}")
                continue
            if mesh.faces and mesh.triangles:
                info = f"{model.name} - {len(mesh.triangles)} faces"
            elif mesh.points:
                info = f"{model.name} - STEP vertices ({len(mesh.points)})"
            else:
                broken.append(f"{model.name}: no readable geometry")
                continue
            if mesh.truncated:
                info += " (trimmed for the preview)"
            logger.info("3D preview of %s: %s", part.name, info)
            return ("mesh", mesh, False, info)
        return ("error", "; ".join(broken) or self._no_model_message(part))

    @staticmethod
    def _model_candidates(part: PartEntry) -> list[Path]:
        """The part's model files, VRML (real triangles) first, then the rest."""
        files = [path for path in part.model_files if path.is_file()]
        faces = [path for path in files if path.suffix.lower() in {".wrl", ".vrml"}]
        return [*faces, *(path for path in files if path not in faces)]

    def _preview_model(self, part: PartEntry) -> Path | None:
        """The file the preview would try first (kept for callers and tests)."""
        candidates = self._model_candidates(part)
        return candidates[0] if candidates else None

    @staticmethod
    def _no_model_message(part: PartEntry) -> str:
        """Say what was looked for, so a missing model is not a mystery."""
        wanted = Path(part.footprint_ref.split(":")[-1]).stem if part.footprint_ref else part.name
        folders = (
            ", ".join(sorted({path.parent.name for path in part.model_files}))
            or "the 3dshapes folders"
        )
        return f"No 3D model for this part (looked for {wanted}.* in {folders})"

    def _apply_preview(self, key: tuple[str, str, str], payload: tuple) -> None:
        if key != self._preview_key:
            return  # the selection moved on while parsing
        self._preview_cache[key] = payload
        while len(self._preview_cache) > PREVIEW_CACHE_LIMIT:
            self._preview_cache.pop(next(iter(self._preview_cache)))
        if payload[0] == "error":
            self._paint_placeholder(str(payload[1]))
            self.preview_info.SetLabel(str(payload[1]))
            self.Layout()
            return
        tag, data, flip_y, info = payload
        bitmap = (
            self._render_drawing(data, bool(flip_y))
            if tag == "drawing"
            else self._render_mesh(data)
        )
        self.preview_canvas.SetBitmap(bitmap)
        self.preview_canvas.Refresh()
        self.preview_info.SetLabel(info)
        self.Layout()

    def _render_drawing(self, drawing: object, flip_y: bool) -> wx.Bitmap:
        width, height = CANVAS_SIZE
        bitmap = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bitmap)
        try:
            dc.SetBackground(wx.Brush(CANVAS_BACKGROUND))
            dc.Clear()
            shapes = drawing.shapes  # type: ignore[attr-defined]
            points = [point for shape in shapes for point in shape.points]
            pixels, scale = preview.fit_points(points, width, height, margin=10, flip_y=flip_y)
            index = 0
            for shape in shapes:
                count = len(shape.points)
                mapped = [wx.Point(x, y) for x, y in pixels[index : index + count]]
                index += count
                if len(mapped) < 2:
                    continue
                colour = wx.Colour(*PALETTE.get(shape.color, PALETTE[preview.SILK]))
                if shape.filled:
                    dc.SetPen(wx.TRANSPARENT_PEN)
                    dc.SetBrush(wx.Brush(colour))
                    dc.DrawPolygon(mapped)
                else:
                    dc.SetPen(wx.Pen(colour, max(1, min(4, int(round(shape.width * scale))))))
                    dc.SetBrush(wx.TRANSPARENT_BRUSH)
                    dc.DrawLines(mapped)
        finally:
            dc.SelectObject(wx.NullBitmap)
        return bitmap

    def _render_mesh(self, mesh: object) -> wx.Bitmap:
        width, height = CANVAS_SIZE
        bitmap = wx.Bitmap(width, height)
        dc = wx.MemoryDC(bitmap)
        try:
            dc.SetBackground(wx.Brush(CANVAS_BACKGROUND))
            dc.Clear()
            if mesh.faces and mesh.triangles:  # type: ignore[attr-defined]
                faces = preview.render_faces(mesh)  # type: ignore[arg-type]
                points = [point for polygon, _brightness in faces for point in polygon]
                pixels, _scale = preview.fit_points(points, width, height, margin=10, flip_y=True)
                index = 0
                dc.SetPen(wx.TRANSPARENT_PEN)
                for _polygon, brightness in faces:
                    count = len(_polygon)
                    mapped = [wx.Point(x, y) for x, y in pixels[index : index + count]]
                    index += count
                    if len(mapped) < 3:
                        continue
                    shade = int(70 + 165 * max(0.0, min(1.0, brightness)))
                    dc.SetBrush(wx.Brush(wx.Colour(shade // 2, int(shade * 0.85), shade)))
                    dc.DrawPolygon(mapped)
            else:
                cloud = preview.render_cloud(mesh)  # type: ignore[arg-type]
                pixels, _scale = preview.fit_points(
                    [(x, y) for x, y, _depth in cloud], width, height, margin=10, flip_y=True
                )
                depths = [depth for _x, _y, depth in cloud]
                span = (max(depths) - min(depths)) if depths else 1.0
                dc.SetPen(wx.TRANSPARENT_PEN)
                # A vendor STEP of a small package only has a few hundred vertices;
                # bigger dots keep the silhouette readable there.
                base = 4 if len(pixels) < 1500 else 2
                for (x, y), depth in zip(pixels, depths, strict=False):
                    near = (depth - min(depths)) / (span or 1.0)
                    shade = int(80 + 150 * near)
                    dc.SetBrush(wx.Brush(wx.Colour(shade // 2, int(shade * 0.9), shade)))
                    size = base + 1 if near > 0.6 else base
                    dc.DrawRectangle(x, y, size, size)
        finally:
            dc.SelectObject(wx.NullBitmap)
        return bitmap

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def _on_install(self, _: wx.CommandEvent) -> None:
        if self.on_install is None:
            self._set_status("Library setup is not available here.")
            return
        try:
            message = self.on_install()
        except Exception as exc:  # noqa: BLE001 - report instead of crashing the tab
            logger.exception("KiCad setup failed")
            self._set_status(f"KiCad setup failed: {exc}")
            return
        # Refresh first: it rewrites the status line with the inventory counts.
        self.refresh()
        self._set_status(message.replace("\n", "  "))

    def _on_open_folder(self, _: wx.CommandEvent) -> None:
        root = self.root_dir
        if root is None:
            self._set_status("No library folder yet.")
            return
        root.mkdir(parents=True, exist_ok=True)
        wx.LaunchDefaultApplication(str(root))

    def _on_delete(self, _: wx.CommandEvent) -> None:
        part = self._selected_part()
        if part is None:
            self._set_status("Select the part to delete first.")
            return
        what = [f"symbol {part.name}" if part.has_symbol else ""]
        if part.has_footprint:
            what.append("footprint")
        if part.has_3d:
            what.append(f"{len(part.model_files)} 3D model file(s)")
        detail = ", ".join(item for item in what if item)
        dialog = wx.MessageDialog(
            self,
            f"Delete {part.library}: {part.name}?\n\nThe following is removed: {detail}.\n"
            "This cannot be undone.",
            "Delete part",
            wx.YES_NO | wx.NO_DEFAULT | wx.ICON_WARNING,
        )
        if dialog.ShowModal() != wx.ID_YES:
            dialog.Destroy()
            return
        dialog.Destroy()

        try:
            removed = delete_part(part)
        except Exception as exc:  # noqa: BLE001 - report instead of crashing
            logger.exception("Could not delete %s", part.name)
            self._set_status(f"Delete failed: {exc}")
            return
        if not removed:
            self._set_status(f"Nothing found for {part.name} (already gone?).")
        else:
            self._preview_cache.clear()  # the files behind it are gone
            self._set_status(f"Deleted {part.name}: {'; '.join(removed)}")
            if self.on_changed is not None:
                try:
                    self.on_changed()
                except Exception:  # noqa: BLE001 - callback is a convenience
                    logger.debug("Library change callback failed", exc_info=True)
        self.refresh()

    def _set_status(self, message: str) -> None:
        self.status.SetLabel(message)
        self.Layout()


def _bold(window: wx.Window) -> None:
    """Make ``window`` bold (kept small so the panel has no import side effects)."""
    try:
        font = window.GetFont()
        font.MakeBold()
        window.SetFont(font)
    except Exception:  # noqa: BLE001 - cosmetic only
        logger.debug("Could not set a bold font", exc_info=True)


if __name__ == "__main__":  # pragma: no cover - manual UI smoke test
    import sys

    logging.basicConfig(level=logging.INFO)
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    app = wx.App()
    frame = wx.Frame(None, title="Library manager smoke test", size=wx.Size(1000, 650))
    panel = LibraryPanel(frame, root_dir=root)
    frame.Show()
    app.MainLoop()
