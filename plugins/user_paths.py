"""Stable per-user folders for state that must survive plugin updates.

The SnapEDA session (``snapeda_session.json``) and the plugin browser profile used
to live inside the plugin folder. Reinstalling the plugin replaces that folder, so
the login was wiped every time - that is why the session had to be read again.
Both now live in the user's data folder, and the old copies are migrated once.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

APP_DIR_NAME = "kicad-parts-hub"


def user_data_dir() -> Path:
    """Per-user, update-safe folder for the plugin's state.

    ``KICAD_PARTS_HUB_DATA`` overrides it (handy for tests and portable setups).
    """
    override = os.environ.get("KICAD_PARTS_HUB_DATA")
    if override:
        path = Path(override).expanduser()
        path.mkdir(parents=True, exist_ok=True)
        return path

    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    path = base / APP_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def migrate_file(legacy: Path, target: Path) -> Path:
    """Move ``legacy`` to ``target`` once; a newer target always wins."""
    legacy, target = Path(legacy), Path(target)
    if target.exists() or not legacy.is_file():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        logger.info("Moved %s to %s", legacy, target)
    except OSError as exc:
        logger.warning("Could not migrate %s: %s", legacy, exc)
    return target


def migrate_dir(legacy: Path, target: Path) -> Path:
    """Move a whole folder (the browser profile) once, keeping the login inside."""
    legacy, target = Path(legacy), Path(target)
    if target.exists() or not legacy.is_dir():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
        logger.info("Moved %s to %s", legacy, target)
    except OSError as exc:
        logger.warning("Could not migrate %s: %s", legacy, exc)
    return target
