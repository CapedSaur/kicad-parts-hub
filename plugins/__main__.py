# KiCad Parts Hub - a modified version of Import-LIB-KiCad-Plugin by Steffen-W
# (https://github.com/Steffen-W/Import-LIB-KiCad-Plugin), GPL-3.0.
# Modified on 2026-10-17; see NOTICE.md for the list of changes.
import wx

from .impart_action import ImpartFrontend

if __name__ == "__main__":
    app = wx.App()
    frame = wx.Frame(None, title="KiCad Plugin")
    frontend = ImpartFrontend()
    frontend.ShowModal()
    frontend.Destroy()
