"""Small, non-blocking notification window used after an import.

The plugin imports parts from a search window that is not the main window, so a
line in the log is easy to miss. This toast pops up bottom-right of the window
that started the import, says what was imported and closes itself.

Never let a notification break an import: every call is wrapped in try/except.
"""

from __future__ import annotations

import logging

import wx

logger = logging.getLogger(__name__)

SUCCESS = "success"
WARNING = "warning"

_COLOURS = {
    SUCCESS: wx.Colour(0x1B, 0x7F, 0x4B),  # green
    WARNING: wx.Colour(0xB4, 0x5F, 0x06),  # amber
}
_GLYPHS = {SUCCESS: "\u2714", WARNING: "!"}
_MARGIN = 24


class Toast(wx.Frame):  # type: ignore[misc]
    """Borderless, always-on-top notice that destroys itself after a few seconds."""

    def __init__(
        self,
        parent: wx.Window | None,
        title: str,
        message: str,
        *,
        kind: str = SUCCESS,
        seconds: float = 6.0,
        width: int = 380,
    ) -> None:
        super().__init__(
            parent,
            style=wx.FRAME_NO_TASKBAR | wx.STAY_ON_TOP | wx.BORDER_NONE,
        )
        colour = _COLOURS.get(kind, _COLOURS[SUCCESS])

        panel = wx.Panel(self)
        panel.SetBackgroundColour(colour)
        sizer = wx.BoxSizer(wx.HORIZONTAL)

        glyph = wx.StaticText(panel, label=_GLYPHS.get(kind, _GLYPHS[SUCCESS]))
        glyph.SetForegroundColour(wx.WHITE)
        glyph_font = glyph.GetFont()
        glyph_font.MakeBold()
        try:
            glyph_font = glyph_font.Scaled(1.8)
        except Exception:  # noqa: BLE001 - older wx builds
            logger.debug("Font scaling not available")
        glyph.SetFont(glyph_font)
        sizer.Add(glyph, 0, wx.ALIGN_CENTRE_VERTICAL | wx.LEFT | wx.RIGHT, 14)

        text = wx.BoxSizer(wx.VERTICAL)
        headline = wx.StaticText(panel, label=title)
        headline.SetForegroundColour(wx.WHITE)
        headline_font = headline.GetFont()
        headline_font.MakeBold()
        headline.SetFont(headline_font)
        detail = wx.StaticText(panel, label=message)
        detail.SetForegroundColour(wx.WHITE)
        detail.Wrap(width - 90)
        text.Add(headline, 0, wx.TOP, 12)
        text.Add(detail, 0, wx.TOP, 4)
        sizer.Add(text, 1, wx.ALIGN_CENTRE_VERTICAL | wx.RIGHT | wx.BOTTOM, 14)

        panel.SetSizer(sizer)
        outer = wx.BoxSizer(wx.VERTICAL)
        outer.Add(panel, 1, wx.EXPAND)
        self.SetSizer(outer)
        self.SetSize(wx.Size(width, sizer.CalcMin().height + 8))
        self._place(parent)

        self._timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_timeout, self._timer)
        self._timer.Start(int(seconds * 1000), oneShot=True)
        self.Bind(wx.EVT_LEFT_UP, self._on_timeout)

    def _place(self, parent: wx.Window | None) -> None:
        index = wx.Display.GetFromWindow(parent) if parent is not None else wx.NOT_FOUND
        if index == wx.NOT_FOUND:
            index = 0
        area = wx.Display(index).GetClientArea()
        width, height = self.GetSize()
        self.SetPosition(
            wx.Point(area.GetRight() - width - _MARGIN, area.GetBottom() - height - _MARGIN)
        )

    def _on_timeout(self, _: wx.Event | None = None) -> None:
        try:
            self._timer.Stop()
        except Exception:  # noqa: BLE001 - window may be gone already
            pass
        if self:
            self.Destroy()


def show_toast(
    parent: wx.Window | None,
    title: str,
    message: str,
    *,
    kind: str = SUCCESS,
    seconds: float = 6.0,
) -> Toast | None:
    """Show a toast; returns None when the platform refuses (never raises)."""
    try:
        toast = Toast(parent, title, message, kind=kind, seconds=seconds)
        toast.ShowWithoutActivating()
        return toast
    except Exception:  # noqa: BLE001 - a notification must never break the import
        logger.debug("Could not show the import notification", exc_info=True)
        return None
