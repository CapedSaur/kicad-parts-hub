"""Items package for kiutils

This package contains all item classes for different KiCad file types including
boards, schematics, footprints, symbols, and worksheets. Each module contains
dataclass-based representations of KiCad file elements.

The items are organized by file type:
- common: Items used across multiple file types
- brditems: Board-specific items (.kicad_pcb files)
- fpitems: Footprint-specific items (.kicad_mod files)
- schitems: Schematic-specific items (.kicad_sch files)
- syitems: Symbol-specific items (.kicad_sym files)
- gritems: Graphics items used in various contexts
- zones: Zone and fill-related items
- dimensions: Dimension and measurement items
"""

# Board-specific items (.kicad_pcb files)
from .brditems import (
    GeneralSettings,
    LayerToken,
    PlotSettings,
    Segment,
    SetupData,
    Stackup,
    StackupLayer,
    StackupSubLayer,
    Target,
    Via,
)

# Common items - used across multiple file types
from .common import (
    ColorRGBA,
    Coordinate,
    Effects,
    Fill,
    Font,
    Group,
    Image,
    Justify,
    Net,
    PageSettings,
    Position,
    ProjectInstance,
    Property,
    RenderCache,
    RenderCachePolygon,
    Stroke,
    TitleBlock,
)

# Dimension and measurement items
from .dimensions import Dimension, DimensionFormat, DimensionStyle

# Footprint-specific items (.kicad_mod files)
from .fpitems import FpArc, FpCircle, FpCurve, FpLine, FpPoly, FpRect, FpText, FpTextBox

# Graphics items (used in multiple contexts)
from .gritems import GrArc, GrCircle, GrCurve, GrLine, GrPoly, GrRect, GrText, GrTextBox

# Schematic-specific items (.kicad_sch files)
from .schitems import (
    Arc,
    BusAlias,
    BusEntry,
    Circle,
    Connection,
    GlobalLabel,
    HierarchicalLabel,
    HierarchicalPin,
    HierarchicalSheet,
    HierarchicalSheetInstance,
    Junction,
    LocalLabel,
    NetclassFlag,
    NoConnect,
    PolyLine,
    Rectangle,
    SchematicSymbol,
    SymbolInstance,
    Text,
    TextBox,
)

# Symbol-specific items (.kicad_sym files)
from .syitems import SyArc, SyCircle, SyCurve, SyPolyLine, SyRect, SyText, SyTextBox

# Zone and fill items
from .zones import (
    FilledPolygon,
    FillSegments,
    FillSettings,
    Hatch,
    KeepoutSettings,
    Zone,
    ZonePolygon,
)

# Export list for controlled imports
__all__ = [
    # Common items
    "Position",
    "Coordinate",
    "ColorRGBA",
    "Stroke",
    "Font",
    "Justify",
    "Effects",
    "Net",
    "Group",
    "PageSettings",
    "TitleBlock",
    "Property",
    "RenderCache",
    "RenderCachePolygon",
    "Fill",
    "Image",
    "ProjectInstance",
    # Board items
    "GeneralSettings",
    "LayerToken",
    "StackupLayer",
    "StackupSubLayer",
    "Stackup",
    "PlotSettings",
    "SetupData",
    "Segment",
    "Via",
    "Arc",
    "Target",
    # Footprint items
    "FpText",
    "FpLine",
    "FpRect",
    "FpTextBox",
    "FpCircle",
    "FpArc",
    "FpPoly",
    "FpCurve",
    # Schematic items
    "Junction",
    "NoConnect",
    "BusEntry",
    "BusAlias",
    "Connection",
    "PolyLine",
    "Text",
    "TextBox",
    "LocalLabel",
    "GlobalLabel",
    "HierarchicalLabel",
    "SchematicSymbol",
    "HierarchicalSheet",
    "HierarchicalPin",
    "HierarchicalSheetInstance",
    "SymbolInstance",
    "Rectangle",
    "Circle",
    "NetclassFlag",
    # Symbol items
    "SyArc",
    "SyCircle",
    "SyCurve",
    "SyPolyLine",
    "SyRect",
    "SyText",
    "SyTextBox",
    # Graphics items
    "GrText",
    "GrTextBox",
    "GrLine",
    "GrRect",
    "GrCircle",
    "GrArc",
    "GrPoly",
    "GrCurve",
    # Zone items
    "Zone",
    "KeepoutSettings",
    "FillSettings",
    "ZonePolygon",
    "FilledPolygon",
    "FillSegments",
    "Hatch",
    # Dimension items
    "Dimension",
    "DimensionFormat",
    "DimensionStyle",
]
