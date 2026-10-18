"""Geometry behind the previews of the Library tab (no wx, testable).

Three readers, one output shape:

* :func:`symbol_drawing` - the drawing items of one symbol in a ``.kicad_sym``
  (rectangles, polylines, circles, arcs, pins), in millimetres, y pointing up
  like KiCad stores symbols;
* :func:`footprint_drawing` - pads and graphics of a ``.kicad_mod``, in
  millimetres, y pointing down and already offset by the footprint's own
  ``(at x y)``, so the file can be drawn as-is;
* :func:`mesh_from_file` - a triangle soup out of a VRML ``.wrl`` (what EasyEDA
  exports) or the vertex cloud of a STEP ``.step``/``.stp`` (what SnapEDA and
  Samacsys export, where reconstructing the surface would need a full B-rep
  kernel).

Everything is expressed as lists of points, so the panel only has to map
millimetres to pixels. Arcs and circles are approximated with segments here (a
handful of lines of math) instead of going through the renderer's arc angle
conventions.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ARC_SEGMENTS = 24
CIRCLE_SEGMENTS = 48
MAX_VRML_FACES = 6000
MAX_STEP_POINTS = 6000

# Palette keys understood by the renderer.
SILK = "silk"
COPPER = "copper"
FAB = "fab"
EDGE = "edge"
MODEL = "model"


# ---------------------------------------------------------------------------
# S-expression parsing
# ---------------------------------------------------------------------------


def parse_sexpr(text: str) -> list | None:
    """Parse the first S-expression of ``text`` into nested lists of atoms.

    Quoted strings become plain atoms (quotes and backslash escapes removed), so
    ``(property "Footprint" "Lib:Name")`` yields ``["property", "Footprint",
    "Lib:Name"]``. Returns ``None`` when the text has no expression.
    """
    index = _skip_space(text, 0)
    if index >= len(text) or text[index] != "(":
        return None
    node, _end = _parse_node(text, index)
    return node


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _parse_node(text: str, index: int) -> tuple[list, int]:
    assert text[index] == "("
    items: list = []
    index += 1
    while index < len(text):
        char = text[index]
        if char in " \t\r\n":
            index += 1
            continue
        if char == ")":
            return items, index + 1
        if char == "(":
            child, index = _parse_node(text, index)
            items.append(child)
            continue
        if char == '"':
            atom, index = _parse_string(text, index + 1)
            items.append(atom)
            continue
        start = index
        while index < len(text) and text[index] not in " \t\r\n()":
            index += 1
        items.append(text[start:index])
    raise ValueError("Unbalanced S-expression")


def _parse_string(text: str, index: int) -> tuple[str, int]:
    out: list[str] = []
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(text[index + 1])
            index += 2
            continue
        if char == '"':
            return "".join(out), index + 1
        out.append(char)
        index += 1
    raise ValueError("Unterminated string in S-expression")


def _head(node: object) -> str:
    if isinstance(node, list) and node and isinstance(node[0], str):
        return node[0]
    return ""


def _sub_nodes(node: list, head: str) -> list[list]:
    return [child for child in node if isinstance(child, list) and _head(child) == head]


def _numbers(node: object) -> list[float]:
    """All numbers of a flat node such as ``(start 0 1)`` or ``(size 1.5 0.9)``."""
    if not isinstance(node, list):
        return []
    values: list[float] = []
    for atom in node[1:]:
        if isinstance(atom, str):
            try:
                values.append(float(atom))
            except ValueError:
                continue
    return values


def _point(node: object) -> tuple[float, float] | None:
    values = _numbers(node)
    if len(values) < 2:
        return None
    return values[0], values[1]


def _child(node: list, head: str) -> list | None:
    for candidate in node:
        if isinstance(candidate, list) and _head(candidate) == head:
            return candidate
    return None


def _points_of(node: list, head: str) -> list[tuple[float, float]]:
    block = _child(node, head)
    if block is None:
        return []
    return [p for p in (_point(entry) for entry in _sub_nodes(block, "xy")) if p is not None]


def _stroke_width(node: list, default: float = 0.15) -> float:
    """Line width of an item: ``(stroke (width w) ...)`` in symbols, ``(width w)`` in footprints."""
    stroke = _child(node, "stroke")
    if stroke is not None:
        inner = _child(stroke, "width")
        values = _numbers(inner if inner is not None else stroke)
        if values:
            return abs(values[0])
    width = _child(node, "width")
    if width is not None:
        values = _numbers(width)
        if values:
            return abs(values[0])
    return default


# ---------------------------------------------------------------------------
# drawings
# ---------------------------------------------------------------------------


@dataclass
class Shape:
    """One drawable item: a polyline (possibly closed / filled) in millimetres."""

    points: list[tuple[float, float]]
    width: float = 0.15
    closed: bool = False
    filled: bool = False
    color: str = SILK


@dataclass
class Drawing:
    """Shapes plus their bounds, ready to be scaled into a view."""

    shapes: list[Shape] = field(default_factory=list)
    label: str = ""
    meta: dict[str, int] = field(default_factory=dict)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` over every point, unit box when empty."""
        xs = [x for shape in self.shapes for x, _ in shape.points]
        ys = [y for shape in self.shapes for _, y in shape.points]
        if not xs or not ys:
            return 0.0, 0.0, 1.0, 1.0
        return min(xs), min(ys), max(xs), max(ys)

    def size(self) -> tuple[float, float]:
        min_x, min_y, max_x, max_y = self.bounds
        return max(max_x - min_x, 1e-6), max(max_y - min_y, 1e-6)


def _circle_points(cx: float, cy: float, radius: float, segments: int = CIRCLE_SEGMENTS):
    return [
        (
            cx + radius * math.cos(2 * math.pi * i / segments),
            cy + radius * math.sin(2 * math.pi * i / segments),
        )
        for i in range(segments + 1)
    ]


def _arc_points(
    start: tuple[float, float],
    mid: tuple[float, float],
    end: tuple[float, float],
    segments: int = ARC_SEGMENTS,
) -> list[tuple[float, float]]:
    """Three-point arc -> segment approximation (degenerates to a line)."""
    ax, ay = start
    bx, by = mid
    cx, cy = end
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-12:
        return [start, end]
    a2, b2, c2 = ax * ax + ay * ay, bx * bx + by * by, cx * cx + cy * cy
    ux = (a2 * (by - cy) + b2 * (cy - ay) + c2 * (ay - by)) / determinant
    uy = (a2 * (cx - bx) + b2 * (ax - cx) + c2 * (bx - ax)) / determinant
    radius = math.hypot(ax - ux, ay - uy)
    start_angle = math.atan2(ay - uy, ax - ux)
    mid_angle = math.atan2(by - uy, bx - ux)
    end_angle = math.atan2(cy - uy, cx - ux)
    sweep = (end_angle - start_angle) % (2 * math.pi)
    if (mid_angle - start_angle) % (2 * math.pi) > sweep:
        sweep -= 2 * math.pi
    return [
        (
            ux + radius * math.cos(start_angle + sweep * i / segments),
            uy + radius * math.sin(start_angle + sweep * i / segments),
        )
        for i in range(segments + 1)
    ]


def _rotate_around(point: tuple[float, float], angle_deg: float) -> tuple[float, float]:
    radians = math.radians(angle_deg)
    cos_a, sin_a = math.cos(radians), math.sin(radians)
    x, y = point
    return x * cos_a - y * sin_a, x * sin_a + y * cos_a


# -- symbols ---------------------------------------------------------------


def find_symbol(text: str, name: str) -> list | None:
    """The top-level ``(symbol "name" ...)`` node of a ``.kicad_sym``."""
    root = parse_sexpr(text)
    if root is None:
        return None
    for child in _sub_nodes(root, "symbol"):
        if len(child) > 1 and child[1] == name:
            return child
    return None


def symbol_footprint_references(text: str) -> dict[str, str]:
    """Every symbol's ``Footprint`` property, e.g. ``2N7002P -> Samacsys:SOT95P230X110-3N``.

    This is the real link between a symbol and its footprint: the names do not
    have to match (``2N7002P`` is drawn with ``SOT95P230X110-3N``).
    """
    root = parse_sexpr(text)
    if root is None:
        return {}
    references: dict[str, str] = {}
    for child in _sub_nodes(root, "symbol"):
        if len(child) < 2:
            continue
        name = str(child[1])
        value = ""
        for prop in _sub_nodes(child, "property"):
            if len(prop) > 2 and prop[1] == "Footprint":
                value = str(prop[2]).strip()
                break
        references[name] = value
    return references


def symbol_footprint_reference(text: str, name: str) -> str:
    """The ``Footprint`` property of one symbol (empty when it has none)."""
    return symbol_footprint_references(text).get(name, "")


def _symbol_units(node: list) -> list[list]:
    """The unit sub-symbols to draw.

    KiCad splits a symbol into ``NAME_0_1`` (the body shared by every unit) and
    ``NAME_<unit>_<style>`` (the pins of each unit), so the common body plus the
    first unit is what a preview should show. Older/flat symbols have no
    sub-symbols at all and are drawn whole.
    """
    units = _sub_nodes(node, "symbol")
    if not units:
        return [node]
    chosen = [
        unit
        for unit in units
        if len(unit) > 1 and str(unit[1]).rsplit("_", 2)[-2:] in (["0", "1"], ["1", "1"])
    ]
    return chosen or units


def symbol_drawing(text: str, name: str) -> Drawing | None:
    """Drawing items of one symbol of a ``.kicad_sym``, or None when not found."""
    node = find_symbol(text, name)
    if node is None:
        return None
    drawing = Drawing(label=name)
    for unit in _symbol_units(node):
        for item in unit:
            if not isinstance(item, list):
                continue
            head = _head(item)
            if head == "rectangle":
                start = _point(_child(item, "start"))
                end = _point(_child(item, "end"))
                if start and end:
                    drawing.shapes.append(
                        Shape(
                            points=[
                                start,
                                (end[0], start[1]),
                                end,
                                (start[0], end[1]),
                                start,
                            ],
                            width=_stroke_width(item),
                            color=COPPER,
                        )
                    )
            elif head == "polyline":
                points = _points_of(item, "pts")
                if len(points) >= 2:
                    drawing.shapes.append(
                        Shape(points=points, width=_stroke_width(item), color=COPPER)
                    )
            elif head == "circle":
                center = _point(_child(item, "center"))
                radius_values = _numbers(_child(item, "radius"))
                if center and radius_values:
                    drawing.shapes.append(
                        Shape(
                            points=_circle_points(center[0], center[1], radius_values[0]),
                            width=_stroke_width(item),
                            color=COPPER,
                        )
                    )
            elif head == "arc":
                start = _point(_child(item, "start"))
                mid = _point(_child(item, "mid"))
                end = _point(_child(item, "end"))
                if start and mid and end:
                    drawing.shapes.append(
                        Shape(
                            points=_arc_points(start, mid, end),
                            width=_stroke_width(item),
                            color=COPPER,
                        )
                    )
            elif head == "pin":
                drawing.shapes.extend(_pin_shapes(item))
                drawing.meta["pins"] = drawing.meta.get("pins", 0) + 1
    drawing.meta["shapes"] = len(drawing.shapes)
    return drawing


def _pin_shapes(pin: list) -> list[Shape]:
    """The line of a pin plus a tick at the connection end, pointing into the body."""
    at = _child(pin, "at")
    if at is None:
        return []
    values = _numbers(at)
    if len(values) < 3:
        return []
    x, y, angle = values[0], values[1], values[2]
    length_values = _numbers(_child(pin, "length"))
    length = length_values[0] if length_values else 2.54
    radians = math.radians(angle)
    end = (x + length * math.cos(radians), y + length * math.sin(radians))
    width = _stroke_width(pin, 0.15)
    return [
        Shape(points=[(x, y), end], width=width, color=SILK),
        Shape(points=_circle_points(x, y, max(width, 0.15)), width=0.1, color=SILK),
    ]


# -- footprints ------------------------------------------------------------


_FOOTPRINT_GRAPHICS = {
    "fp_line": ("start", "end"),
    "fp_rect": ("start", "end"),
    "fp_circle": ("center", "end"),
    "fp_poly": ("pts", None),
    "fp_arc": ("start", "mid", "end"),
}


def _layer_color(layers: list[str]) -> str:
    joined = " ".join(layers)
    if ".Cu" in joined:
        return COPPER
    if ".Fab" in joined or ".CrtYd" in joined:
        return FAB
    if ".SilkS" in joined:
        return SILK
    if "Edge" in joined:
        return EDGE
    return SILK


def footprint_drawing(text: str) -> Drawing | None:
    """Pads and graphics of a ``.kicad_mod`` (y downwards, origin included)."""
    root = parse_sexpr(text)
    if root is None or _head(root) not in {"footprint", "module"}:
        return None
    label = str(root[1]) if len(root) > 1 else ""
    origin = _numbers(_child(root, "at"))
    origin_x = origin[0] if origin else 0.0
    origin_y = origin[1] if len(origin) > 1 else 0.0
    rotation = origin[2] if len(origin) > 2 else 0.0

    def place(point: tuple[float, float]) -> tuple[float, float]:
        rotated = _rotate_around(point, rotation)
        return rotated[0] + origin_x, rotated[1] + origin_y

    drawing = Drawing(label=label)
    for item in root:
        if not isinstance(item, list):
            continue
        head = _head(item)
        if head == "pad":
            drawing.shapes.extend(_pad_shapes(item, place, rotation))
            drawing.meta["pads"] = drawing.meta.get("pads", 0) + 1
            continue
        keys = _FOOTPRINT_GRAPHICS.get(head)
        if keys is None:
            continue
        layer = _child(item, "layer")
        layers = [str(value) for value in (layer[1:] if layer else [])]
        color = _layer_color(layers)
        width = _stroke_width(item, 0.12)
        if head == "fp_line":
            start = _point(_child(item, "start"))
            end = _point(_child(item, "end"))
            if start and end:
                drawing.shapes.append(Shape([place(start), place(end)], width=width, color=color))
        elif head == "fp_rect":
            start = _point(_child(item, "start"))
            end = _point(_child(item, "end"))
            if start and end:
                corners = [
                    place(start),
                    place((end[0], start[1])),
                    place(end),
                    place((start[0], end[1])),
                    place(start),
                ]
                drawing.shapes.append(Shape(corners, width=width, color=color))
        elif head == "fp_circle":
            center = _point(_child(item, "center"))
            edge = _point(_child(item, "end"))
            if center and edge:
                radius = math.hypot(edge[0] - center[0], edge[1] - center[1])
                drawing.shapes.append(
                    Shape(
                        [place(point) for point in _circle_points(center[0], center[1], radius)],
                        width=width,
                        color=color,
                    )
                )
        elif head == "fp_arc":
            start = _point(_child(item, "start"))
            mid = _point(_child(item, "mid"))
            end = _point(_child(item, "end"))
            if start and mid and end:
                drawing.shapes.append(
                    Shape(
                        [place(point) for point in _arc_points(start, mid, end)],
                        width=width,
                        color=color,
                    )
                )
        elif head == "fp_poly":
            points = _points_of(item, "pts")
            if len(points) >= 2:
                drawing.shapes.append(
                    Shape([place(point) for point in points], width=width, color=color)
                )
    drawing.meta["shapes"] = len(drawing.shapes)
    return drawing


def _pad_shapes(
    pad: list,
    place,
    footprint_rotation: float = 0.0,
) -> list[Shape]:
    """One pad -> a filled outline of its real shape (rect / circle / oval)."""
    if len(pad) < 3:
        return []
    shape = str(pad[2]).lower()
    at = _numbers(_child(pad, "at"))
    size = _numbers(_child(pad, "size"))
    if len(at) < 2 or len(size) < 2:
        return []
    x, y = at[0], at[1]
    rotation = (at[2] if len(at) > 2 else 0.0) + footprint_rotation
    width, height = size[0], size[1]
    layer = _child(pad, "layers")
    layers = [str(value) for value in (layer[1:] if layer else [])]
    color = _layer_color(layers) if layers else COPPER

    if shape in {"circle", "oval"}:
        points = [
            (
                x + (width / 2) * math.cos(2 * math.pi * i / CIRCLE_SEGMENTS),
                y + (height / 2) * math.sin(2 * math.pi * i / CIRCLE_SEGMENTS),
            )
            for i in range(CIRCLE_SEGMENTS + 1)
        ]
    else:
        corners = [
            (x - width / 2, y - height / 2),
            (x + width / 2, y - height / 2),
            (x + width / 2, y + height / 2),
            (x - width / 2, y + height / 2),
        ]
        points = [(_rotate_around(corner, rotation)) for corner in corners]
        points.append(points[0])
    return [Shape([place(point) for point in points], filled=True, width=0.08, color=color)]


# -- 3D models -------------------------------------------------------------


@dataclass
class Mesh:
    """A triangle soup (``faces=True``) or a vertex cloud (``faces=False``)."""

    points: list[tuple[float, float, float]] = field(default_factory=list)
    triangles: list[tuple[int, int, int]] = field(default_factory=list)
    faces: bool = True
    label: str = ""
    truncated: bool = False

    @property
    def bounds(self) -> tuple[float, float, float, float, float, float]:
        if not self.points:
            return 0.0, 0.0, 0.0, 1.0, 1.0, 1.0
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        zs = [p[2] for p in self.points]
        return min(xs), min(ys), min(zs), max(xs), max(ys), max(zs)


# ``0.`` (a trailing dot, no decimals) is legal in STEP and common in CAD exports,
# so the integer and fraction parts are both optional.
_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_STEP_POINT = re.compile(
    rf"CARTESIAN_POINT\s*\(\s*'[^']*'\s*,\s*\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})"
)
_WRL_COORD = re.compile(r"point\s*\[(.*?)\]", re.S)


# -- STEP topology ---------------------------------------------------------

_STEP_ID = re.compile(r"#(\d+)")
_STEP_ENTITY = re.compile(r"^\s*#(\d+)\s*=\s*([A-Z_0-9]+)\s*\((.*)\)\s*$", re.S)
_STEP_TRIPLE = re.compile(rf"\(\s*({_NUMBER})\s*,\s*({_NUMBER})\s*,\s*({_NUMBER})\s*\)")
_STEP_FORWARD = ".T."


def _step_references(args: str) -> list[int]:
    return [int(match) for match in _STEP_ID.findall(args)]


def step_faces(text: str, max_faces: int = MAX_VRML_FACES) -> Mesh:
    """Triangulate a STEP file through its own topology.

    STEP stores the surface as a B-rep, but the topology is enough to rebuild the
    visible shape: every ``ADVANCED_FACE`` names a bound, the bound an
    ``EDGE_LOOP``, and the loop its ``ORIENTED_EDGE``s, each of which resolves to
    two vertices. Walking a loop therefore yields the face outline, which is
    triangulated as a fan (the vendor exports are planar-faceted). A curved edge
    contributes its chord, so cylinders show up faceted instead of missing.
    """
    points: dict[int, tuple[float, float, float]] = {}
    vertices: dict[int, int] = {}
    edges: dict[int, tuple[int, int]] = {}
    oriented: dict[int, tuple[int, bool]] = {}
    loops: dict[int, list[int]] = {}
    bounds: dict[int, int] = {}
    face_bounds: list[list[int]] = []

    for statement in text.split(";"):
        if "#" not in statement or "=" not in statement:
            continue
        match = _STEP_ENTITY.match(statement)
        if match is None:
            continue
        entity_id, kind, args = int(match.group(1)), match.group(2), match.group(3)
        if kind == "CARTESIAN_POINT":
            triple = _STEP_TRIPLE.search(args)
            if triple:
                points[entity_id] = (
                    float(triple.group(1)),
                    float(triple.group(2)),
                    float(triple.group(3)),
                )
        elif kind == "VERTEX_POINT":
            references = _step_references(args)
            if references:
                vertices[entity_id] = references[0]
        elif kind == "EDGE_CURVE":
            references = _step_references(args)
            if len(references) >= 2:
                edges[entity_id] = (references[0], references[1])
        elif kind == "ORIENTED_EDGE":
            references = _step_references(args)
            if references:
                oriented[entity_id] = (references[-1], _STEP_FORWARD in args)
        elif kind == "EDGE_LOOP":
            loops[entity_id] = _step_references(args)
        elif kind in {"FACE_OUTER_BOUND", "FACE_BOUND"}:
            references = _step_references(args)
            if references:
                bounds[entity_id] = references[0]
        elif kind == "ADVANCED_FACE":
            face_bounds.append(_step_references(args))

    mesh = Mesh(faces=True)
    for bound_ids in face_bounds:
        if len(mesh.triangles) >= max_faces:
            mesh.truncated = True
            break
        polygon = _step_polygon(bound_ids, bounds, loops, oriented, edges, vertices, points)
        for index in range(1, len(polygon) - 1):
            mesh.points.append(polygon[0])
            mesh.points.append(polygon[index])
            mesh.points.append(polygon[index + 1])
            mesh.triangles.append(
                (len(mesh.points) - 3, len(mesh.points) - 2, len(mesh.points) - 1)
            )
    if not mesh.triangles:
        return Mesh(faces=False)
    return mesh


def _step_polygon(
    bound_ids: list[int],
    bounds: dict[int, int],
    loops: dict[int, list[int]],
    oriented: dict[int, tuple[int, bool]],
    edges: dict[int, tuple[int, int]],
    vertices: dict[int, int],
    points: dict[int, tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Outline of one STEP face, from its outer bound."""
    loop: list[int] = []
    for bound_id in bound_ids:
        loop_id = bounds.get(bound_id)
        if loop_id is not None and loops.get(loop_id):
            loop = loops[loop_id]
            break
    outline: list[tuple[float, float, float]] = []
    for oriented_id in loop:
        entry = oriented.get(oriented_id)
        if entry is None:
            continue
        edge_id, forward = entry
        ends = edges.get(edge_id)
        if ends is None:
            continue
        start, stop = (ends[0], ends[1]) if forward else (ends[1], ends[0])
        for vertex_id in (start, stop):
            coordinate = points.get(vertices.get(vertex_id, -1))
            if coordinate is None:
                continue
            if not outline or outline[-1] != coordinate:
                outline.append(coordinate)
    if len(outline) > 1 and outline[0] == outline[-1]:
        outline.pop()
    return outline


def step_points(text: str, max_points: int = MAX_STEP_POINTS) -> Mesh:
    """Every ``CARTESIAN_POINT`` of a STEP file as a vertex cloud.

    STEP from these vendors has no cheap triangle mesh (that needs a B-rep
    kernel), but the vertices are enough for a recognisable preview. Points are
    subsampled evenly once the model is bigger than ``max_points``.
    """
    raw = _STEP_POINT.findall(text)
    mesh = Mesh(faces=False)
    step = max(1, len(raw) // max_points) if len(raw) > max_points else 1
    taken = raw[::step]
    mesh.points = [(float(x), float(y), float(z)) for x, y, z in taken]
    mesh.truncated = len(taken) < len(raw)
    return mesh


def vrml_mesh(text: str, max_faces: int = MAX_VRML_FACES) -> Mesh:
    """Triangles of a VRML 2 (``.wrl``) model, the format EasyEDA exports."""
    mesh = Mesh(faces=True)
    offset = 0
    for block in _WRL_COORD.findall(text):
        numbers = [float(value) for value in re.findall(_NUMBER, block)]
        for index in range(0, len(numbers) - 2, 3):
            mesh.points.append((numbers[index], numbers[index + 1], numbers[index + 2]))
        offset += len(numbers) // 3
    for block in re.findall(r"coordIndex\s*\[(.*?)\]", text, re.S):
        face: list[int] = []
        for token in re.split(r"[,\s]+", block.strip()):
            if not token:
                continue
            if token == "-1":
                for index in range(1, len(face) - 1):  # fan triangulation
                    mesh.triangles.append((face[0], face[index], face[index + 1]))
                face = []
                continue
            try:
                face.append(int(token))
            except ValueError:
                continue
        if len(mesh.triangles) > max_faces:
            mesh.triangles = mesh.triangles[:max_faces]
            mesh.truncated = True
            break
    if not mesh.points:
        return Mesh(faces=False)
    return mesh


def mesh_from_file(path: Path, max_points: int = MAX_STEP_POINTS) -> Mesh:
    """Read an ``.wrl`` (triangles) or ``.step``/``.stp`` (vertex cloud) model."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return Mesh(faces=False, label=path.name)
    if path.suffix.lower() in {".wrl", ".vrml"} or "#VRML" in text[:200]:
        mesh = vrml_mesh(text)
    else:
        # Real surface when the STEP topology is usable, vertex cloud otherwise.
        mesh = step_faces(text)
        if not mesh.triangles:
            mesh = step_points(text, max_points=max_points)
    mesh.label = path.name
    return mesh


# ---------------------------------------------------------------------------
# projection (pure math so the renderer stays dumb)
# ---------------------------------------------------------------------------

AZIMUTH_DEG = 35.0
ELEVATION_DEG = 22.0
LIGHT = (0.4, -0.6, 0.7)


def _view_matrix(azimuth: float = AZIMUTH_DEG, elevation: float = ELEVATION_DEG):
    az, el = math.radians(azimuth), math.radians(elevation)
    ca, sa = math.cos(az), math.sin(az)
    ce, se = math.cos(el), math.sin(el)

    def project(point: tuple[float, float, float]) -> tuple[float, float, float]:
        x, y, z = point
        x1 = x * ca - y * sa
        y1 = x * sa + y * ca
        return x1, z * ce - y1 * se, y1 * ce + z * se

    return project


def render_faces(mesh: Mesh, limit: int = 4000) -> list[tuple[list[tuple[float, float]], float]]:
    """Project + shade the triangles: ``(polygon, brightness)``, far to near."""
    project = _view_matrix()
    centre = _mesh_centre(mesh)
    face_step = max(1, len(mesh.triangles) // limit) if mesh.triangles else 1
    shaded: list[tuple[float, list[tuple[float, float]], float]] = []
    for index, triangle in enumerate(mesh.triangles[::face_step]):
        try:
            a, b, c = (mesh.points[triangle[0]], mesh.points[triangle[1]], mesh.points[triangle[2]])
        except IndexError:
            continue
        edges = _cross(_sub(b, a), _sub(c, a))
        normal = _normalize(edges)
        brightness = 0.25 + 0.75 * abs(_dot(normal, _normalize(LIGHT)))
        projected = [project(_sub(point, centre))[:2] for point in (a, b, c)]
        depth = sum(project(_sub(point, centre))[2] for point in (a, b, c)) / 3
        shaded.append((depth, [(px, py) for px, py in projected], brightness))
    shaded.sort(key=lambda item: item[0])
    return [(polygon, brightness) for _depth, polygon, brightness in shaded]


def render_cloud(mesh: Mesh, limit: int = 6000) -> list[tuple[float, float, float]]:
    """Project a vertex cloud: ``(x, y, depth)``, far to near."""
    project = _view_matrix()
    centre = _mesh_centre(mesh)
    step = max(1, len(mesh.points) // limit) if mesh.points else 1
    projected = [project(_sub(point, centre)) for point in mesh.points[::step]]
    projected.sort(key=lambda item: item[2])
    return projected


def _mesh_centre(mesh: Mesh) -> tuple[float, float, float]:
    min_x, min_y, min_z, max_x, max_y, max_z = mesh.bounds
    return (min_x + max_x) / 2, (min_y + max_y) / 2, (min_z + max_z) / 2


def _sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _cross(a, b):
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _dot(a, b) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _normalize(vector) -> tuple[float, float, float]:
    length = math.sqrt(_dot(vector, vector))
    if length < 1e-12:
        return 0.0, 0.0, 0.0
    return vector[0] / length, vector[1] / length, vector[2] / length


# ---------------------------------------------------------------------------
# fitting drawings into a view
# ---------------------------------------------------------------------------


def fit_points(
    points: list[tuple[float, float]],
    width: int,
    height: int,
    *,
    margin: int = 8,
    flip_y: bool = False,
) -> tuple[list[tuple[int, int]], float]:
    """Scale millimetre points into a pixel box; returns (pixel points, scale)."""
    if not points:
        return [], 0.0
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(max_x - min_x, 1e-6)
    span_y = max(max_y - min_y, 1e-6)
    usable_w = max(width - 2 * margin, 1)
    usable_h = max(height - 2 * margin, 1)
    scale = min(usable_w / span_x, usable_h / span_y)
    offset_x = margin + (usable_w - span_x * scale) / 2
    offset_y = margin + (usable_h - span_y * scale) / 2
    pixels: list[tuple[int, int]] = []
    for x, y in points:
        px = offset_x + (x - min_x) * scale
        py = offset_y + ((max_y - y) if flip_y else (y - min_y)) * scale
        pixels.append((int(round(px)), int(round(py))))
    return pixels, scale
