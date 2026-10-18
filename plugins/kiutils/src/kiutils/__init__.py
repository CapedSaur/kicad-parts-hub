"""Init script for kiutils

A Python library for working with KiCad files (.kicad_sch, .kicad_pcb, .kicad_mod, etc.)

This library provides a "pythonic" abstraction of KiCad file formats based on the
documentation found at the KiCad Developer Reference. It is designed to work with
SCM systems like Git or SVN without breaking the layout of files when modified.

Authors:
    (C) Marvin Mager - @mvnmgrx - 2022

License identifier:
    GPL-3.0
"""

# Version information
__version__ = "1.4.8"
__author__ = "Marvin Mager"
__email__ = "99667992+mvnmgrx@users.noreply.github.com"
__license__ = "GPL-3.0"

# Main classes for easy import
from .board import Board
from .dru import Constraint, DesignRules, Rule
from .footprint import Attributes, Footprint, Model, Pad
from .items.brditems import GeneralSettings, Segment, Target, Via

# Commonly used items
from .items.common import (
    ColorRGBA,
    Effects,
    Fill,
    Font,
    Net,
    Position,
    Property,
    Stroke,
)
from .items.fpitems import FpArc, FpCircle, FpLine, FpPoly, FpRect, FpText
from .items.gritems import GrArc, GrCircle, GrLine, GrRect, GrText
from .items.schitems import (
    GlobalLabel,
    Junction,
    LocalLabel,
    NoConnect,
    SchematicSymbol,
    Text,
)
from .items.zones import FillSettings, KeepoutSettings, Zone
from .libraries import Library, LibTable
from .schematic import Schematic
from .symbol import Symbol, SymbolLib, SymbolPin

# Utility functions
from .utils.strings import dequote, remove_prefix
from .wks import WorkSheet

# Define what gets imported with "from kiutils import *"
__all__ = [
    # Main classes
    "Board",
    "Schematic",
    "Footprint",
    "Attributes",
    "Model",
    "Pad",
    "Symbol",
    "SymbolLib",
    "SymbolPin",
    "Library",
    "LibTable",
    "DesignRules",
    "Rule",
    "Constraint",
    "WorkSheet",
    # Common items used across multiple file types
    "Position",
    "Effects",
    "Property",
    "Net",
    "Fill",
    "Stroke",
    "Font",
    "ColorRGBA",
    # Board items
    "Segment",
    "Via",
    "Target",
    "GeneralSettings",
    # Footprint items
    "FpText",
    "FpLine",
    "FpRect",
    "FpCircle",
    "FpArc",
    "FpPoly",
    # Schematic items
    "Junction",
    "Text",
    "LocalLabel",
    "GlobalLabel",
    "SchematicSymbol",
    "NoConnect",
    # Graphics items
    "GrText",
    "GrLine",
    "GrRect",
    "GrCircle",
    "GrArc",
    # Zone items
    "Zone",
    "KeepoutSettings",
    "FillSettings",
    # Utilities
    "dequote",
    "remove_prefix",
]

# Package metadata
__title__ = "kiutils"
__description__ = "Simple and SCM-friendly KiCad file parser for KiCad 6.0 and up"
__url__ = "https://github.com/mvnmgrx/kiutils"
__download_url__ = "https://pypi.org/project/kiutils/"
__docs_url__ = "https://kiutils.readthedocs.io/"
