"""Inventory of the libraries the plugin imports into (no wx, testable).

The plugin writes one library per source into the library folder, always with the
same three pieces:

``<Lib>.kicad_sym``      symbols
``<Lib>.pretty``         footprints (``<Part>.kicad_mod``)
``<Lib>.3dshapes``       3D models (``<Part>.step`` / ``.wrl`` ...)

:mod:`library_manager` shows this inventory as a tab. Everything here is plain
filesystem work so the listing and the deletion can be tested without KiCad.

Deletion is deliberately conservative: only the symbol *block* is removed from
the ``.kicad_sym`` (the rest of the file stays byte-identical, no library
rewrite), and the footprint/3D files of that part are unlinked. Anything shared
with another part (a 3D model of a different name) is left alone.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    from .library_preview import symbol_footprint_references
except ImportError:  # standalone / sys.path import (tests, CLI)
    from library_preview import (  # type: ignore[import-not-found,no-redef]
        symbol_footprint_references,
    )

logger = logging.getLogger(__name__)

MODEL_SUFFIXES = (".step", ".stp", ".wrl", ".iges", ".igs", ".stl")
_MODEL_REF = re.compile(r'\(model\s+"([^"]+)"')


# ---------------------------------------------------------------------------
# S-expression helpers (depth aware, string aware)
# ---------------------------------------------------------------------------


def _scan(text: str):
    """Yield ``(index, depth_before, char)`` for every structural parenthesis."""
    depth = 0
    in_string = escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "(":
            yield index, depth, char
            depth += 1
        elif char == ")":
            depth -= 1
            yield index, depth, char


def top_level_names(text: str, head: str) -> list[str]:
    """Names of the ``(head "Name"`` expressions at the root of ``text``.

    Only depth 1 counts: a symbol's drawing units are also written as
    ``(symbol "Name_1_1" ...)`` inside it and are not parts of their own.
    """
    pattern = re.compile(rf'\(\s*{re.escape(head)}\s+"((?:[^"\\]|\\.)*)"')
    names: list[str] = []
    for index, depth, char in _scan(text):
        if char != "(" or depth != 1:
            continue
        match = pattern.match(text, index)
        if match:
            names.append(match.group(1))
    return names


def remove_top_level_block(text: str, head: str, name: str) -> tuple[str, bool]:
    """Remove one root-level ``(head "name" ...)`` block; returns (new text, removed)."""
    pattern = re.compile(rf'\(\s*{re.escape(head)}\s+"{re.escape(name)}"')
    start: int | None = None
    for index, depth, char in _scan(text):
        if start is None:
            if char == "(" and depth == 1 and pattern.match(text, index):
                start = index
            continue
        # ``_scan`` reports a ``)`` with the depth *after* it closes, so the end
        # of a depth-1 block is the parenthesis that lands back on depth 1.
        if char == ")" and depth == 1:
            end = index + 1
            while end < len(text) and text[end] in " \t\r\n":
                end += 1
            prefix = text[:start]
            if prefix and not prefix.endswith("\n"):
                prefix += "\n"
            return prefix + text[end:], True
    return text, False


def footprint_models(model_refs: list[str]) -> list[str]:
    """Basenames of the 3D files a footprint points at."""
    names: list[str] = []
    for reference in model_refs:
        base = Path(reference.replace("\\", "/")).name
        if base and base not in names:
            names.append(base)
    return names


def read_footprint_model_refs(path: Path) -> list[str]:
    """The 3D model paths a ``.kicad_mod`` file points at, exactly as written."""
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Could not read %s: %s", path, exc)
        return []
    return [match.replace("\\", "/") for match in _MODEL_REF.findall(text)]


def read_footprint_models(path: Path) -> list[str]:
    """3D model basenames referenced by a ``.kicad_mod`` file."""
    return footprint_models(read_footprint_model_refs(path))


def _unpack_gz(directory: Path) -> None:
    """Turn ``x.step.gz`` (written by old plugin versions) into ``x.step``."""
    if not directory.is_dir():
        return
    for compressed in sorted(directory.glob("*.gz")):
        import gzip
        import shutil

        target = compressed.with_suffix("")
        try:
            if not target.exists():
                with gzip.open(compressed, "rb") as source, open(target, "wb") as out:
                    shutil.copyfileobj(source, out)
            compressed.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not unpack %s: %s", compressed, exc)


# ---------------------------------------------------------------------------
# inventory
# ---------------------------------------------------------------------------


@dataclass
class PartEntry:
    """One part as it exists in the library folder.

    ``footprint_ref`` is what the symbol asks for (``Samacsys:SOT95P230X110-3N``),
    which is *not* always a file named like the symbol - a symbol called
    ``2N7002P`` is drawn with that footprint.
    """

    name: str
    library: str
    symbol_file: Path | None = None
    footprint_file: Path | None = None
    model_files: list[Path] = field(default_factory=list)
    footprint_ref: str = ""

    @property
    def has_symbol(self) -> bool:
        return self.symbol_file is not None and self.symbol_file.is_file()

    @property
    def has_footprint(self) -> bool:
        return self.footprint_file is not None and self.footprint_file.is_file()

    @property
    def has_3d(self) -> bool:
        return any(path.is_file() for path in self.model_files)

    @property
    def footprint_missing(self) -> bool:
        """The symbol points at a footprint that is not in the folder."""
        return bool(self.footprint_ref) and not self.has_footprint

    @property
    def missing(self) -> list[str]:
        """Which of symbol / footprint / 3D model are not there."""
        return [
            name
            for name, present in (
                ("symbol", self.has_symbol),
                ("footprint", self.has_footprint),
                ("3D model", self.has_3d),
            )
            if not present
        ]

    @property
    def is_complete(self) -> bool:
        return not self.missing

    def describe(self) -> str:
        """One line per file of this part (used by the detail box)."""
        lines = [f"{self.library}: {self.name}"]
        lines.append(f"  symbol:    {self.symbol_file if self.has_symbol else '-'}")
        suffix = f"   ({self.footprint_ref})" if self.footprint_ref else ""
        if self.has_footprint:
            lines.append(f"  footprint: {self.footprint_file}{suffix}")
        elif self.footprint_missing:
            lines.append(f"  footprint: not in the folder{suffix}")
        else:
            lines.append("  footprint: -")
        models = [str(path) for path in self.model_files if path.is_file()]
        lines.append(f"  3D model:  {', '.join(models) if models else '-'}")
        return "\n".join(lines)


@dataclass
class LibraryInfo:
    """One library folder entry (symbols + footprints + models of one source)."""

    name: str
    root: Path
    symbol_file: Path | None = None
    footprint_dir: Path | None = None
    model_dir: Path | None = None
    symbol_names: list[str] = field(default_factory=list)
    footprint_names: list[str] = field(default_factory=list)
    model_names: list[str] = field(default_factory=list)
    symbol_refs: dict[str, str] = field(default_factory=dict)

    # -- footprint lookup ------------------------------------------------

    def find_footprint(self, reference: str) -> Path | None:
        """Resolve ``Lib:Footprint`` (as a symbol stores it) to its ``.kicad_mod``.

        Falls back to the symbol's own name and to any other ``.pretty`` folder in
        the library root, because the names in the file and on disk can differ
        after a manual rename.
        """
        reference = (reference or "").strip()
        wanted = reference.split(":", 1)[-1].strip()
        if not wanted:
            return None
        library = reference.split(":", 1)[0].strip() if ":" in reference else self.name
        candidates = [self.root / f"{library}.pretty" / f"{wanted}.kicad_mod"]
        if self.footprint_dir is not None:
            candidates.append(self.footprint_dir / f"{wanted}.kicad_mod")
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        for other in sorted(self.root.glob("*.pretty")):
            candidate = other / f"{wanted}.kicad_mod"
            if candidate.is_file():
                return candidate
        return None

    def models_for(self, footprint: Path | None, *names: str) -> list[Path]:
        """3D files of a part: what its footprint points at, then same-name files.

        A footprint reference names its own folder
        (``${KICAD_3RD_PARTY}/Samacsys.3dshapes/2N7002P.stp``), which is what keeps
        a model of one library from being listed under a part of another one.
        """
        found: list[Path] = []
        if footprint is not None:
            for reference in read_footprint_model_refs(footprint):
                resolved = self.resolve_model_reference(reference)
                if resolved is not None and resolved not in found:
                    found.append(resolved)
        for name in names:
            if not name or self.model_dir is None:
                continue
            for candidate in sorted(self.model_dir.glob(f"{name}.*")):
                if candidate.is_file() and candidate.suffix.lower() in MODEL_SUFFIXES:
                    if candidate not in found:
                        found.append(candidate)
        # A model saved under the footprint name is still that part's model, even
        # when the footprint file itself has no (model ...) block yet.
        if footprint is not None:
            for candidate in self.models_by_stem(footprint.stem):
                if candidate not in found:
                    found.append(candidate)
        return found

    def models_by_stem(self, stem: str) -> list[Path]:
        """Model files named like ``stem`` in any ``*.3dshapes`` folder of the root.

        The extension is not part of the match: SnapEDA writes ``.step`` in the
        footprint while the ZIP may carry ``.stp``, and KiCad opens both.
        """
        if not stem:
            return []
        found: list[Path] = []
        for shapes in [self.model_dir, *sorted(self.root.glob("*.3dshapes"))]:
            if shapes is None or not shapes.is_dir():
                continue
            for candidate in sorted(shapes.glob(f"{stem}.*")):
                if (
                    candidate.is_file()
                    and candidate.suffix.lower() in MODEL_SUFFIXES
                    and candidate not in found
                ):
                    found.append(candidate)
        return found

    def resolve_model_reference(self, reference: str) -> Path | None:
        """``${KICAD_3RD_PARTY}/Snapeda.3dshapes/X.step`` -> the file, if it exists."""
        parts = [piece for piece in str(reference).replace("\\", "/").split("/") if piece]
        for index, piece in enumerate(parts):
            if piece.endswith(".3dshapes"):
                candidate = self.root.joinpath(piece, *parts[index + 1 :])
                if candidate.is_file():
                    return candidate
        name = Path(str(reference).replace("\\", "/")).name
        if not name:
            return None
        if self.model_dir is not None and (self.model_dir / name).is_file():
            return self.model_dir / name
        # Same file, other extension (.step <-> .stp) or another folder of the root.
        matches = self.models_by_stem(Path(name).stem)
        return matches[0] if matches else None

    # -- inventory -------------------------------------------------------

    def parts(self) -> list[PartEntry]:
        """One entry per symbol, plus any footprint no symbol claims."""
        entries: list[PartEntry] = []
        claimed: set[Path] = set()

        for name in self.symbol_names:
            reference = self.symbol_refs.get(name, "")
            footprint = self.find_footprint(reference) if reference else None
            if footprint is None and self.footprint_dir is not None:
                same_name = self.footprint_dir / f"{name}.kicad_mod"
                footprint = same_name if same_name.is_file() else None
            if footprint is not None:
                claimed.add(footprint)
            entries.append(
                PartEntry(
                    name=name,
                    library=self.name,
                    symbol_file=self.symbol_file,
                    footprint_file=footprint,
                    model_files=self.models_for(
                        footprint, name, (reference.split(":", 1)[-1] if reference else "")
                    ),
                    footprint_ref=reference,
                )
            )

        # Footprints without a symbol of their own (imported before the symbol,
        # or shared by several symbols).
        if self.footprint_dir is not None:
            for name in self.footprint_names:
                path = self.footprint_dir / f"{name}.kicad_mod"
                if path in claimed:
                    continue
                entries.append(
                    PartEntry(
                        name=name,
                        library=self.name,
                        footprint_file=path if path.is_file() else None,
                        model_files=self.models_for(path, name),
                    )
                )

        return sorted(entries, key=lambda part: (part.name.lower(), part.library))


def scan_library(root: Path, name: str) -> LibraryInfo:
    """Read one library (``<name>`` inside ``root``) from disk."""
    root = Path(root)
    symbol_file = root / f"{name}.kicad_sym"
    footprint_dir = root / f"{name}.pretty"
    model_dir = root / f"{name}.3dshapes"
    info = LibraryInfo(
        name=name,
        root=root,
        symbol_file=symbol_file if symbol_file.is_file() else None,
        footprint_dir=footprint_dir if footprint_dir.is_dir() else None,
        model_dir=model_dir if model_dir.is_dir() else None,
    )
    if info.symbol_file is not None:
        try:
            text = info.symbol_file.read_text(encoding="utf-8", errors="replace")
            info.symbol_names = top_level_names(text, "symbol")
            info.symbol_refs = symbol_footprint_references(text)
        except OSError as exc:
            logger.warning("Could not read %s: %s", info.symbol_file, exc)
    if info.footprint_dir is not None:
        info.footprint_names = sorted(path.stem for path in info.footprint_dir.glob("*.kicad_mod"))
    if info.model_dir is not None:
        _unpack_gz(info.model_dir)
        info.model_names = sorted(
            path.name
            for path in info.model_dir.iterdir()
            if path.is_file() and path.suffix.lower() in MODEL_SUFFIXES
        )
    return info


def library_names(root: Path) -> list[str]:
    """Every library in ``root`` (symbol file and/or footprint folder)."""
    root = Path(root)
    names: set[str] = set()
    if root.is_dir():
        names.update(path.stem for path in root.glob("*.kicad_sym"))
        names.update(path.stem for path in root.glob("*.pretty") if path.is_dir())
    return sorted(names)


def scan_libraries(root: Path) -> list[LibraryInfo]:
    """All libraries in ``root``, read from disk."""
    return [scan_library(root, name) for name in library_names(root)]


def scan_parts(root: Path) -> list[PartEntry]:
    """Every part of every library in ``root``."""
    parts: list[PartEntry] = []
    for info in scan_libraries(root):
        parts.extend(info.parts())
    return parts


# ---------------------------------------------------------------------------
# deletion
# ---------------------------------------------------------------------------


def _write_text(path: Path, content: str) -> None:
    try:
        from .atomic_file import atomic_write_text
    except ImportError:  # standalone / sys.path import
        try:
            from atomic_file import atomic_write_text  # type: ignore[import-not-found,no-redef]
        except ImportError:
            atomic_write_text = None  # type: ignore[assignment]
    if atomic_write_text is not None:
        atomic_write_text(str(path), content)
        return
    path.write_text(content, encoding="utf-8")


def delete_part(part: PartEntry) -> list[str]:
    """Remove the symbol block, footprint and 3D files of ``part``.

    Returns what was removed (empty = nothing matched). Files shared with another
    part (same 3D model referenced by a footprint of another name) are kept.
    """
    removed: list[str] = []

    if part.has_symbol and part.symbol_file is not None:
        try:
            text = part.symbol_file.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", part.symbol_file, exc)
            text = ""
        if text:
            updated, changed = remove_top_level_block(text, "symbol", part.name)
            if changed:
                _write_text(part.symbol_file, updated)
                removed.append(f"symbol {part.name} ({part.symbol_file.name})")

    if part.has_footprint and part.footprint_file is not None:
        try:
            part.footprint_file.unlink()
            removed.append(f"footprint {part.footprint_file.name}")
        except OSError as exc:
            logger.warning("Could not remove %s: %s", part.footprint_file, exc)

    for model in part.model_files:
        if not model.is_file():
            continue
        try:
            model.unlink()
            removed.append(f"3D model {model.name}")
        except OSError as exc:
            logger.warning("Could not remove %s: %s", model, exc)

    return removed


def inventory_summary(parts: list[PartEntry]) -> str:
    """Short count line for the panel's status bar."""
    libraries = {part.library for part in parts}
    symbols = sum(1 for part in parts if part.has_symbol)
    footprints = sum(1 for part in parts if part.has_footprint)
    models = sum(1 for part in parts if part.has_3d)
    return (
        f"{len(libraries)} librar{'y' if len(libraries) == 1 else 'ies'}, "
        f"{len(parts)} parts - {symbols} symbols, {footprints} footprints, {models} with 3D"
    )
