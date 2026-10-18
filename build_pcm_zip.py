#!/usr/bin/env python3
"""Build the Plugin-and-Content-Manager ZIP for this plugin.

The single packaging entry point: pure Python, so it runs the same way on Windows,
macOS and Linux (no bash, jq or zip needed):

    python build_pcm_zip.py            # -> kicad-parts-hub-pcm.zip
    python build_pcm_zip.py --install  # ... and copy the payload straight into
                                       #     Documents/KiCad/<version>/3rdparty/plugins/

Layout required by KiCad's PCM (verified against an installed official package):

    metadata.json          package metadata, at the ZIP root
    plugins/...            the payload; its *contents* end up in the package folder
    resources/...          optional package resources

Install the result with KiCad: Plugin and Content Manager -> Install from File.
"""

from __future__ import annotations

import argparse
import datetime
import json
import shutil
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
PLUGIN_DIR = REPO / "plugins"
DEFAULT_ZIP = REPO / "kicad-parts-hub-pcm.zip"


def package_folder_name() -> str:
    """Folder PCM uses for this package: the identifier with dots as underscores.

    KiCad names installed packages after the identifier (``com.local.kicad-parts-hub``
    -> ``com_local_kicad-parts-hub``). A dev install must land in the same folder,
    otherwise the plugin KiCad loads and the one this script writes are two copies.
    """
    try:
        metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
        identifier = str(metadata.get("identifier") or "").strip()
    except (OSError, json.JSONDecodeError):
        identifier = ""
    return identifier.replace(".", "_") or "kicad-parts-hub"


SKIP_DIRS = {
    "__pycache__",
    ".github",
    "docs",
    "tests",
    "ressources",
    "doc",
    # local secrets / runtime state: a browser profile holds the SnapEDA session
    "snapeda_browser_profile",
    "snapeda_session.json",
}
SKIP_SUFFIXES = {".pyc", ".pyo", ".fbp", ".svg", ".log"}
# Never ship the saved SnapEDA session (secret) or local logs.
SKIP_FILES = {"snapeda_session.json"}
# Never touched by an install: the user's login, settings and logs.
USER_DATA_FILES = {"snapeda_session.json", "config.ini"}
USER_DATA_DIRS = {"snapeda_browser_profile"}
RUNTIME_FILES = {"plugin.log", "plugin_fallback.log"}

# License texts that must travel with every copy (GPL-3.0 section 4/5, AGPL-3.0
# section 4). They land at the package root, next to plugin.json.
LEGAL_FILES = (
    (REPO / "LICENSE.txt", "plugins/LICENSE.txt"),
    (REPO / "NOTICE.md", "plugins/NOTICE.md"),
    (PLUGIN_DIR / "kiutils" / "LICENSE", "plugins/kiutils/LICENSE"),
    (PLUGIN_DIR / "easyeda2kicad" / "LICENSE", "plugins/easyeda2kicad/LICENSE"),
)

# Vendored dependencies keep their package structure so runtime imports work.
SUBMODULE_MAP = {
    "kiutils": (PLUGIN_DIR / "kiutils" / "src" / "kiutils", "plugins/kiutils/src/kiutils"),
    "easyeda2kicad": (
        PLUGIN_DIR / "easyeda2kicad" / "easyeda2kicad",
        "plugins/easyeda2kicad/easyeda2kicad",
    ),
}


def iter_payload(src: Path, arc_prefix: str) -> list[tuple[Path, str]]:
    """(source file, archive path) pairs, skipping caches and tooling files."""
    files: list[tuple[Path, str]] = []
    for item in sorted(src.iterdir()):
        name = item.name
        if item.is_dir():
            if name in SKIP_DIRS or name.startswith("."):
                continue
            files += iter_payload(item, f"{arc_prefix}/{name}")
        elif name not in SKIP_FILES and item.suffix not in SKIP_SUFFIXES:
            files.append((item, f"{arc_prefix}/{name}"))
    return files


def collect_payload() -> list[tuple[Path, str]]:
    """Everything that must land in the package folder of the installed plugin."""
    files: list[tuple[Path, str]] = []
    for item in sorted(PLUGIN_DIR.iterdir()):
        name = item.name
        # Checked here too: the top-level folders are passed to iter_payload()
        # directly, so its own skip list never saw them (the browser profile with
        # the saved SnapEDA session used to leak into the package that way).
        if name in SKIP_DIRS or name.startswith("."):
            continue
        if name in SUBMODULE_MAP:
            source, prefix = SUBMODULE_MAP[name]
            if not source.is_dir():
                raise SystemExit(
                    f"{source} is missing - run: git submodule update --init --recursive"
                )
            files += iter_payload(source, prefix)
        elif item.is_dir():
            files += iter_payload(item, f"plugins/{name}")
        elif name not in SKIP_FILES and item.suffix not in SKIP_SUFFIXES:
            files.append((item, f"plugins/{name}"))

    # Distribution without the licenses is not allowed, so a missing one is an
    # error, not a warning.
    for source, arc_name in LEGAL_FILES:
        if not source.is_file():
            raise SystemExit(f"{source} is missing - the package cannot ship without its licenses")
        files.append((source, arc_name))
    return files


def build_zip(target: Path, version: str) -> Path:
    metadata = json.loads((REPO / "metadata.json").read_text(encoding="utf-8"))
    metadata.setdefault("versions", [{}])
    metadata["versions"][0]["version"] = version

    payload = collect_payload()
    if target.exists():
        target.unlink()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("metadata.json", json.dumps(metadata, indent=2))
        for resource in sorted((REPO / "resources").glob("*")):
            if resource.is_file() and resource.suffix not in SKIP_SUFFIXES:
                archive.write(resource, f"resources/{resource.name}")
        for source, arc_name in payload:
            archive.write(source, arc_name)

    print(
        f"ZIP: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB, {len(payload)} payload files)"
    )
    print(f"     identifier: {metadata['identifier']}  version: {version}")
    return target


def install_payload(package: str | None = None) -> Path:
    """Copy the payload into KiCad's third-party plugin folder (dev install).

    This mirrors what PCM does: the *contents* of ``plugins/`` become the package
    folder, so ``plugin.json`` ends up at its root - the place KiCad looks at.
    """
    package = package or package_folder_name()
    thirdparty_candidates = sorted((Path.home() / "Documents" / "KiCad").glob("*/3rdparty"))
    if not thirdparty_candidates:
        raise SystemExit(
            "No KiCad third-party folder found under ~/Documents/KiCad/<version>/3rdparty"
        )
    target = thirdparty_candidates[-1] / "plugins" / package
    target.mkdir(parents=True, exist_ok=True)

    # Copy over the existing install instead of deleting it first: replacing the
    # folder wiped the user's session/config, and a single locked file (plugin.log
    # held by a running KiCad, or an indexer) aborted the rmtree halfway and left
    # a broken plugin behind.
    payload = collect_payload()
    written: set[str] = set()
    for source, arc_name in payload:
        relative = arc_name.removeprefix("plugins/")
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if relative in USER_DATA_FILES and destination.exists():
            # config.ini holds the user's paths; a stock file must not overwrite it.
            written.add(Path(relative).as_posix())
            continue
        shutil.copy2(source, destination)
        written.add(Path(relative).as_posix())

    kept = sorted(name for name in (USER_DATA_FILES | USER_DATA_DIRS) if (target / name).exists())
    removed = 0
    for path in sorted(target.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        relative = path.relative_to(target)
        if relative.parts[0] in USER_DATA_FILES or relative.parts[0] in USER_DATA_DIRS:
            continue
        if path.is_file():
            if (
                relative.as_posix() in written
                or path.suffix in {".pyc", ".pyo"}
                or path.name in RUNTIME_FILES
            ):
                continue
            path.unlink()
            removed += 1
        else:
            try:
                path.rmdir()  # only succeeds when empty
            except OSError:
                pass
    if kept:
        print(f"  kept user data: {', '.join(kept)}")
    print(f"  removed {removed} stale files")
    ok = (target / "plugin.json").is_file() and (target / "impart_action.py").is_file()
    print(f"Installed to: {target}")
    print(f"  plugin.json at package root: {ok}")
    if not ok:
        raise SystemExit("Install layout looks wrong - plugin.json is not at the package root")
    print("  Restart KiCad to pick it up: Tools -> External Plugins in Schematic and PCB editor.")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--zip", type=Path, default=DEFAULT_ZIP, help="output ZIP path")
    parser.add_argument(
        "--version",
        default=datetime.date.today().strftime("%Y.%m.%d"),
        help="package version written into metadata.json (default: today)",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="also copy the payload into Documents/KiCad/<version>/3rdparty/plugins/",
    )
    parser.add_argument(
        "--package-name", default=package_folder_name(), help="folder name in 3rdparty/plugins"
    )
    args = parser.parse_args(argv)

    build_zip(args.zip, args.version)
    if args.install:
        install_payload(args.package_name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
