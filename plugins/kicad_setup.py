"""Repair the KiCad settings the plugin needs (path variable + library tables).

The recurring "KICAD_3RD_PARTY is not defined" / "Snapeda is not in the Symbol
Libraries" warnings mean two edits are missing in KiCad's own configuration:

* ``kicad_common.json`` -> ``environment.vars.KICAD_3RD_PARTY``
* the global ``sym-lib-table`` / ``fp-lib-table`` entries pointing at
  ``${KICAD_3RD_PARTY}/<Lib>.kicad_sym`` and ``.../<Lib>.pretty``

Doing that by hand is what the plugin asked the user to do until now. These
functions perform the edits (with a ``.bak`` copy, atomically) and also repair
3D model references that point at files which do not exist, which is what makes
``${KICAD_3RD_PARTY}/...`` show up as a broken model link in KiCad.

Everything here is plain filesystem work: no wx, no pcbnew, so it is testable.
All functions return the list of changes they made (empty = nothing to do).
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

current_dir = Path(__file__).resolve().parent
kiutils_src = current_dir / "kiutils" / "src"
if str(kiutils_src) not in sys.path:
    sys.path.insert(0, str(kiutils_src))

from kiutils.libraries import Library, LibTable  # noqa: E402

logger = logging.getLogger(__name__)

PATH_VAR = "KICAD_3RD_PARTY"
SYMBOL_TABLE = "sym-lib-table"
FOOTPRINT_TABLE = "fp-lib-table"
_COMMON_JSON = "kicad_common.json"
_MODEL_REF = re.compile(r'\(model\s+"([^"]+)"')
_MODEL_SUFFIXES = (".step", ".stp", ".wrl", ".wrz", ".iges", ".igs", ".stl")


@dataclass
class SetupResult:
    """What a setup run changed and what it could not fix."""

    changed: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def summary(self) -> str:
        if not self.changed and not self.problems:
            return "KiCad is already configured correctly."
        lines = [f"- {item}" for item in self.changed]
        lines += [f"! {item}" for item in self.problems]
        return "\n".join(lines)


def default_settings_dir() -> Path | None:
    """KiCad's user settings folder (where the library tables live)."""
    try:
        from KiCadSettingsPaths import KiCadSettingsPaths
    except ImportError:  # pragma: no cover - package layout differs
        try:
            from .KiCadSettingsPaths import KiCadSettingsPaths  # type: ignore[no-redef]
        except ImportError:
            return None
    try:
        path = Path(KiCadSettingsPaths.find_actual_settings_path())
    except Exception:  # noqa: BLE001 - detection is best effort
        logger.debug("Could not detect the KiCad settings folder", exc_info=True)
        return None
    return path if path.is_dir() else None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _backup(path: Path) -> None:
    if path.is_file():
        try:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
        except OSError as exc:  # pragma: no cover - permissions
            logger.warning("Could not back up %s: %s", path, exc)


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    temp.replace(path)


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"{path.name} is not readable JSON: {exc}") from exc
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# path variable
# ---------------------------------------------------------------------------


def ensure_env_var(settings: Path, target: Path, name: str = PATH_VAR) -> list[str]:
    """Make ``name`` point at ``target`` in KiCad's ``kicad_common.json``."""
    path = settings / _COMMON_JSON
    try:
        data = _read_json(path)
    except ValueError:
        return []
    environment = data.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    variables = environment.get("vars")
    if not isinstance(variables, dict):
        variables = {}
    wanted = str(target)
    if variables.get(name) == wanted:
        return []
    variables[name] = wanted
    environment["vars"] = variables
    data["environment"] = environment
    _backup(path)
    _write_atomic(path, json.dumps(data, indent=2) + "\n")
    logger.info("Set %s = %s in %s", name, wanted, _COMMON_JSON)
    return [f"{name} = {wanted} (Configure Paths)"]


# ---------------------------------------------------------------------------
# library tables
# ---------------------------------------------------------------------------


def _ensure_table_entry(table_path: Path, name: str, uri: str, table_type: str) -> list[str]:
    if table_path.is_file():
        try:
            table = LibTable.from_file(str(table_path), encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - keep the user's file untouched
            logger.warning("Could not read %s: %s", table_path.name, exc)
            return []
    else:
        table = LibTable.create_new(table_type)
    changed: list[str] = []
    entry = next((lib for lib in table.libs if lib.name == name), None)
    if entry is None:
        table.libs.append(Library(name=name, type="KiCad", uri=uri, options="", description=""))
        changed.append(f"{name} -> {uri} ({table_path.name})")
    elif entry.uri != uri:
        old = entry.uri
        entry.uri = uri
        changed.append(f"{name}: {old} -> {uri} ({table_path.name})")
    if not changed:
        return []
    _backup(table_path)
    table.to_file(str(table_path), encoding="utf-8")
    logger.info("Updated %s: %s", table_path.name, changed)
    return changed


def ensure_symbol_library(settings: Path, name: str, uri: str) -> list[str]:
    return _ensure_table_entry(settings / SYMBOL_TABLE, name, uri, "sym_lib_table")


def ensure_footprint_library(settings: Path, name: str, uri: str) -> list[str]:
    return _ensure_table_entry(settings / FOOTPRINT_TABLE, name, uri, "fp_lib_table")


# ---------------------------------------------------------------------------
# 3D model links
# ---------------------------------------------------------------------------


def _unpack_gz(path: Path) -> Path | None:
    """Turn ``x.step.gz`` into ``x.step``; KiCad cannot open gzip-compressed models."""
    target = path.with_suffix("") if path.suffix.lower() == ".gz" else None
    if target is None:
        return None
    try:
        if not target.exists():
            with gzip.open(path, "rb") as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)
        if not target.is_file():
            return None
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning("Could not unpack %s: %s", path, exc)
        return None
    return target


def _expand(reference: str, variables: dict[str, str]) -> str:
    expanded = reference
    for key, value in variables.items():
        expanded = expanded.replace("${" + key + "}", value).replace("$(" + key + ")", value)
    return expanded


def _model_block(reference: str) -> str:
    return (
        f'\t(model "{reference}"\n\t\t(offset (xyz 0 0 0))\n\t\t(scale (xyz 1 1 1))\n'
        "\t\t(rotate (xyz 0 0 0))\n\t)"
    )


def _link_model_by_name(
    dest: Path, lib_name: str, footprint: Path, content: str, variable: str
) -> str | None:
    """Add a model block to a footprint that has none; returns the model name used.

    Only a file named like the *footprint* counts (any extension, any
    ``*.3dshapes`` folder of the library root), so a model that merely shares the
    symbol's name is never attached: that is how one vendor's part used to show
    another vendor's 3D file.
    """
    folders = [dest / f"{lib_name}.3dshapes", *sorted(dest.glob("*.3dshapes"))]
    chosen: Path | None = None
    for folder in folders:
        if not folder.is_dir():
            continue
        for candidate in sorted(folder.glob(f"{footprint.stem}.*")):
            if candidate.is_file() and candidate.suffix.lower() in _MODEL_SUFFIXES:
                chosen = candidate
                break
        if chosen is not None:
            break
    if chosen is None:
        return None
    folder_name = chosen.parent.name
    reference = f"${{{variable}}}/{folder_name}/{chosen.name}"
    stripped = content.rstrip()
    if not stripped.endswith(")"):
        return None
    updated = f"{stripped[:-1]}\n{_model_block(reference)}\n)\n"
    try:
        _write_atomic(footprint, updated)
    except OSError as exc:
        logger.warning("Could not update %s: %s", footprint, exc)
        return None
    return chosen.name


def fix_3d_links(
    dest: Path,
    lib_name: str,
    *,
    variables: dict[str, str] | None = None,
    variable: str = PATH_VAR,
) -> list[str]:
    """Repoint 3D model references whose file does not exist.

    A footprint that points at ``${KICAD_3RD_PARTY}/Snapeda.3dshapes/X.wrl``
    shows a broken link whenever either the variable or the file is missing.
    The model in ``<Lib>.3dshapes`` with the same stem is used instead.
    """
    dest = Path(dest)
    pretty = dest / f"{lib_name}.pretty"
    shapes = dest / f"{lib_name}.3dshapes"
    if not pretty.is_dir():
        return []
    mapping = {variable: str(dest), **(variables or {})}
    changed: list[str] = []

    for footprint in sorted(pretty.glob("*.kicad_mod")):
        try:
            content = footprint.read_text(encoding="utf-8")
        except OSError as exc:
            logger.warning("Could not read %s: %s", footprint, exc)
            continue
        references = _MODEL_REF.findall(content)
        if not references:
            # A footprint imported without a 3D link still gets one when the model
            # is on disk: without it KiCad shows the part with no 3D at all and the
            # library tab reports "no 3D model" for a file that is right there.
            added = _link_model_by_name(dest, lib_name, footprint, content, variable)
            if added:
                changed.append(f"{footprint.name}: 3D model linked -> {added}")
            continue
        updated = content
        for reference in references:
            resolved = Path(_expand(reference, mapping))
            if resolved.suffix.lower() == ".gz":
                # Written by older plugin versions; KiCad ignored it silently.
                if _unpack_gz(resolved) is not None:
                    replacement = reference[: -len(".gz")]
                    updated = updated.replace(f'"{reference}"', f'"{replacement}"')
                    changed.append(f"{footprint.name}: 3D model unpacked -> {resolved.stem}")
                continue
            if resolved.is_file():
                continue
            stem = Path(reference.replace("\\", "/")).stem
            candidates = sorted(shapes.glob(f"{stem}.*")) if shapes.is_dir() else []
            if not candidates:
                continue
            # Keep the variable reference (not the expanded path): a hard-coded
            # absolute path breaks as soon as the library folder moves.
            replacement = f"${{{variable}}}/{lib_name}.3dshapes/{candidates[0].name}"
            updated = updated.replace(f'"{reference}"', f'"{replacement}"')
            changed.append(f"{footprint.name}: 3D model -> {candidates[0].name}")
        if updated != content:
            try:
                _write_atomic(footprint, updated)
            except OSError as exc:
                logger.warning("Could not update %s: %s", footprint, exc)
    return changed


# ---------------------------------------------------------------------------
# one call for everything
# ---------------------------------------------------------------------------


def apply_setup(
    dest: Path,
    lib_names: Sequence[str],
    *,
    settings: Path | None = None,
    create_3dshapes: bool = True,
    variable: str = PATH_VAR,
    configure_env: bool = True,
) -> SetupResult:
    """Configure KiCad for ``lib_names`` living in ``dest`` (best effort).

    ``settings`` is the folder holding the library tables: KiCad's own settings
    folder for global libraries (``variable="KICAD_3RD_PARTY"``), or the project
    folder for project libraries (``variable="KIPRJMOD"``, ``configure_env=False`` -
    ``KIPRJMOD`` always exists and must not be redefined).
    """
    result = SetupResult()
    folder = Path(dest)
    if not folder.is_dir():
        result.problems.append(f"library folder does not exist: {folder}")
        return result
    settings_dir = Path(settings) if settings is not None else default_settings_dir()
    if settings_dir is None or not settings_dir.is_dir():
        result.problems.append("KiCad settings folder not found")
        return result

    if configure_env:
        result.changed += ensure_env_var(settings_dir, folder)
    for lib_name in lib_names:
        symbol_file = folder / f"{lib_name}.kicad_sym"
        if symbol_file.is_file():
            result.changed += ensure_symbol_library(
                settings_dir, lib_name, f"${{{variable}}}/{symbol_file.name}"
            )
        footprint_dir = folder / f"{lib_name}.pretty"
        if footprint_dir.is_dir():
            result.changed += ensure_footprint_library(
                settings_dir, lib_name, f"${{{variable}}}/{footprint_dir.name}"
            )
            if create_3dshapes:
                shapes = folder / f"{lib_name}.3dshapes"
                if not shapes.is_dir() and any(footprint_dir.glob("*.kicad_mod")):
                    try:
                        shapes.mkdir(parents=True, exist_ok=True)
                    except OSError as exc:
                        logger.warning("Could not create %s: %s", shapes, exc)
        try:
            result.changed += fix_3d_links(folder, lib_name, variable=variable)
        except Exception as exc:  # noqa: BLE001 - never abort the whole setup
            logger.warning("3D model check failed for %s: %s", lib_name, exc)
            result.problems.append(f"3D models of {lib_name} could not be checked: {exc}")
    return result


def library_names(dest: Path) -> list[str]:
    """Libraries present in ``dest`` (symbol file and/or footprint folder)."""
    dest = Path(dest)
    names: set[str] = set()
    for symbol in dest.glob("*.kicad_sym"):
        names.add(symbol.stem)
    for pretty in dest.glob("*.pretty"):
        if pretty.is_dir():
            names.add(pretty.stem)
    return sorted(names)


def apply_setup_for_folder(dest: Path, **kwargs: object) -> SetupResult:
    """``apply_setup`` for every library found in ``dest``."""
    kwargs.setdefault("settings", None)
    return apply_setup(dest, library_names(dest), **kwargs)  # type: ignore[arg-type]


def apply_project_setup(
    project_dir: Path,
    dest: Path | None = None,
    **kwargs: object,
) -> SetupResult:
    """Register the libraries in the project's own tables (``${KIPRJMOD}``).

    With "Local Project Library" checked the plugin writes the files next to the
    ``.kicad_pro``. That is all that is missing for KiCad to list them in that
    project: the symbol/footprint tables of the project are used first, the global
    ones are not touched.
    """
    project = Path(project_dir)
    folder = Path(dest) if dest is not None else project
    kwargs.setdefault("variable", "KIPRJMOD")
    kwargs.setdefault("configure_env", False)
    return apply_setup(folder, library_names(folder), settings=project, **kwargs)  # type: ignore[arg-type]


def pending_fix(dest: Path, settings: Path | None = None) -> bool:
    """True when the plugin would still change something in KiCad's settings."""
    folder = Path(dest)
    names = library_names(folder)
    if not names:
        return False
    settings_dir = Path(settings) if settings is not None else default_settings_dir()
    if settings_dir is None:
        return True
    try:
        variables = (_read_json(settings_dir / _COMMON_JSON).get("environment") or {}).get(
            "vars"
        ) or {}
    except ValueError:
        return True
    if variables.get(PATH_VAR) != str(folder):
        return True
    for table_name, suffix in ((SYMBOL_TABLE, ".kicad_sym"), (FOOTPRINT_TABLE, ".pretty")):
        path = settings_dir / table_name
        if not path.is_file():
            if any(folder.glob(f"*{suffix}")):
                return True
            continue
        try:
            table = LibTable.from_file(str(path), encoding="utf-8")
        except Exception:  # noqa: BLE001 - unreadable table means "cannot verify"
            return True
        entries = {lib.name: lib.uri for lib in table.libs}
        for name in names:
            if suffix == ".kicad_sym" and not (folder / f"{name}.kicad_sym").is_file():
                continue
            if suffix == ".pretty" and not (folder / f"{name}.pretty").is_dir():
                continue
            if entries.get(name) != f"${{{PATH_VAR}}}/{name}{suffix}":
                return True
    return False


def project_pending_fix(project_dir: Path, dest: Path | None = None) -> bool:
    """True when the project's own tables still miss a library of ``dest``.

    The project counterpart of :func:`pending_fix`: with "Local Project Library"
    checked the entries live in ``<project>/sym-lib-table`` and
    ``<project>/fp-lib-table`` and point at ``${KIPRJMOD}``.
    """
    project = Path(project_dir)
    folder = Path(dest) if dest is not None else project
    names = library_names(folder)
    if not names:
        return False
    for table_name, suffix in ((SYMBOL_TABLE, ".kicad_sym"), (FOOTPRINT_TABLE, ".pretty")):
        path = project / table_name
        try:
            table = LibTable.from_file(str(path), encoding="utf-8") if path.is_file() else None
        except Exception:  # noqa: BLE001 - unreadable table means "cannot verify"
            return True
        entries = {lib.name: lib.uri for lib in table.libs} if table is not None else {}
        for name in names:
            if suffix == ".kicad_sym" and not (folder / f"{name}.kicad_sym").is_file():
                continue
            if suffix == ".pretty" and not (folder / f"{name}.pretty").is_dir():
                continue
            if entries.get(name) != f"${{KIPRJMOD}}/{name}{suffix}":
                return True
    return False


def describe_missing(dest: Path, settings: Path | None = None) -> list[str]:
    """Human readable list of what is still missing (used by the warning text)."""
    folder = Path(dest)
    settings_dir = Path(settings) if settings is not None else default_settings_dir()
    problems: list[str] = []
    if settings_dir is None:
        return ["KiCad settings folder not found"]
    try:
        variables = (_read_json(settings_dir / _COMMON_JSON).get("environment") or {}).get(
            "vars"
        ) or {}
    except ValueError as exc:
        return [str(exc)]
    if variables.get(PATH_VAR) != str(folder):
        problems.append(f"{PATH_VAR} is not set to {folder}")
    for lib_name in library_names(folder):
        for table_name, suffix in ((SYMBOL_TABLE, ".kicad_sym"), (FOOTPRINT_TABLE, ".pretty")):
            if suffix == ".kicad_sym" and not (folder / f"{lib_name}.kicad_sym").is_file():
                continue
            if suffix == ".pretty" and not (folder / f"{lib_name}.pretty").is_dir():
                continue
            table_path = settings_dir / table_name
            uris: dict[str, str] = {}
            if table_path.is_file():
                try:
                    uris = {lib.name: lib.uri for lib in LibTable.from_file(str(table_path)).libs}
                except Exception:  # noqa: BLE001
                    uris = {}
            if uris.get(lib_name) != f"${{{PATH_VAR}}}/{lib_name}{suffix}":
                problems.append(f"{lib_name} is not in {table_name}")
    return problems


__all__ = [
    "PATH_VAR",
    "SetupResult",
    "apply_setup",
    "apply_setup_for_folder",
    "default_settings_dir",
    "describe_missing",
    "ensure_env_var",
    "ensure_footprint_library",
    "ensure_symbol_library",
    "fix_3d_links",
    "library_names",
    "pending_fix",
]
