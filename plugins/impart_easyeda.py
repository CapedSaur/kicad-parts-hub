# KiCad Parts Hub - a modified version of Import-LIB-KiCad-Plugin by Steffen-W
# (https://github.com/Steffen-W/Import-LIB-KiCad-Plugin), GPL-3.0.
# Modified on 2026-10-17; see NOTICE.md for the list of changes.
# Simplified EasyEDA to KiCad importer
# Based on: https://github.com/uPesy/easyeda2kicad.py/blob/master/easyeda2kicad/__main__.py
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, NamedTuple

# Configure logging
logger = logging.getLogger(__name__)

current_dir = Path(__file__).resolve().parent
easyeda_submodule = current_dir / "easyeda2kicad"
kiutils_src = current_dir / "kiutils" / "src"
if kiutils_src.exists() and str(kiutils_src) not in sys.path:
    sys.path.insert(0, str(kiutils_src))

if easyeda_submodule.exists():
    # Remove any conflicting easyeda2kicad modules from cache first
    modules_to_remove = [k for k in sys.modules.keys() if k.startswith("easyeda2kicad")]
    for mod in modules_to_remove:
        del sys.modules[mod]

    # Add git submodule path at the very beginning
    easyeda_str = str(easyeda_submodule)
    if easyeda_str not in sys.path:
        sys.path.insert(0, easyeda_str)

    logger.info(f"Using easyeda2kicad from: {easyeda_submodule}")

from easyeda2kicad.easyeda.easyeda_api import EasyedaApi  # noqa: E402
from easyeda2kicad.easyeda.easyeda_importer import (  # noqa: E402
    Easyeda3dModelImporter,
    EasyedaFootprintImporter,
    EasyedaSymbolImporter,
)
from easyeda2kicad.easyeda.parameters_easyeda import EeFootprint  # noqa: E402
from easyeda2kicad.kicad.export_kicad_3d_model import Exporter3dModelKicad  # noqa: E402
from easyeda2kicad.kicad.export_kicad_footprint import (  # noqa: E402
    ExporterFootprintKicad,
)
from easyeda2kicad.kicad.export_kicad_symbol import ExporterSymbolKicad  # noqa: E402

logger.info("Successfully imported easyeda2kicad modules")


class ImportPaths(NamedTuple):
    """Container for all generated file paths"""

    symbol_lib: Path | None = None
    footprint_file: Path | None = None
    model_wrl: Path | None = None
    model_step: Path | None = None


@dataclass
class ImportConfig:
    """Configuration for import operations"""

    base_folder: Path
    lib_name: str = "EasyEDA"
    overwrite: bool = False
    lib_var: str = "${EASYEDA2KICAD}"
    prefer_step: bool = False
    compress_models: bool = False


class EasyEDAImporter:
    """EasyEDA to KiCad component importer"""

    def __init__(self, config: ImportConfig, print_func: Callable[[str], None] | None = None):
        self.print_func = print_func or (lambda _: None)
        self.config = config
        self.config.base_folder = Path(self.config.base_folder).expanduser()
        self.api = EasyedaApi()

        self.symbol_lib_path = self.config.base_folder / f"{self.config.lib_name}.kicad_sym"
        self.footprint_dir = self.config.base_folder / f"{self.config.lib_name}.pretty"
        self.model_dir = self.config.base_folder / f"{self.config.lib_name}.3dshapes"

    def _print(self, message: str) -> None:
        self.print_func(message)

    def _ensure_directories(self) -> None:
        self.config.base_folder.mkdir(parents=True, exist_ok=True)
        self.footprint_dir.mkdir(exist_ok=True)
        self.model_dir.mkdir(exist_ok=True)

    @staticmethod
    def component_value(cad_data: dict[str, Any], fallback: str) -> str:
        """The value KiCad should show (``10k\u03a9``, ``100nF``), not the part number.

        EasyEDA stores it in ``head.c_para.Value`` for resistors, capacitors and
        similar parts. ICs have no such field, so their part number stays the value.
        """
        head = (cad_data.get("dataStr") or {}).get("head") or {}
        value = str((head.get("c_para") or {}).get("Value") or "").strip()
        return value or fallback

    def _set_symbol_value(self, symbol_name: str, value: str) -> bool:
        """Write ``value`` into the symbol's Value field (the part number stays in LCSC Part)."""
        if not value or not self.symbol_lib_path.is_file():
            return False
        try:
            from kiutils.symbol import SymbolLib

            lib = SymbolLib.from_file(str(self.symbol_lib_path), encoding="utf-8")
            changed = False
            for symbol in lib.symbols:
                names = {getattr(symbol, "entryName", ""), getattr(symbol, "libId", "")}
                if symbol_name not in names and symbol_name not in {
                    name.split(":")[-1] for name in names
                }:
                    continue
                for prop in symbol.properties:
                    if prop.key == "Value" and prop.value != value:
                        prop.value = value
                        changed = True
            if changed:
                # UTF-8 (values like "10kΩ") and a temp file first: to_file would
                # truncate the library before hitting an encoding error.
                temp_path = self.symbol_lib_path.with_suffix(".tmp")
                lib.to_file(str(temp_path), encoding="utf-8")
                temp_path.replace(self.symbol_lib_path)
                self._print(f"Value of '{symbol_name}' set to '{value}'")
            return changed
        except Exception as exc:  # noqa: BLE001 - the symbol is already imported
            logger.warning("Could not set the Value of %s: %s", symbol_name, exc)
            return False

    def _import_symbol(self, cad_data: dict[str, Any]) -> tuple[bool, str | None]:
        """Import symbol and return success status and component name."""
        try:
            easyeda_symbol = EasyedaSymbolImporter(easyeda_cp_cad_data=cad_data).get_symbol()
            component_name = easyeda_symbol.info.name

            exporter = ExporterSymbolKicad(
                symbol=easyeda_symbol,
                lib_path=str(self.symbol_lib_path),
            )
            if not exporter.save_to_lib(
                lib_path=str(self.symbol_lib_path),
                footprint_lib_name=self.config.lib_name,
                overwrite=self.config.overwrite,
            ):
                self._print(
                    f"Symbol '{component_name}' already exists in "
                    f"{self.symbol_lib_path.name} (overwrite disabled)."
                )
                return False, component_name

            if easyeda_symbol.sub_symbols:
                self._print(
                    f"Imported {len(easyeda_symbol.sub_symbols)} sub-symbols for: {component_name}"
                )
            self._print(f"Saved symbol: {component_name} -> {self.symbol_lib_path.name}")
            return True, component_name

        except Exception as e:
            self._print(f"Failed to import symbol: {e}")
            logger.error(f"Symbol import failed: {e}")
            return False, None

    def _import_footprint(self, ee_footprint: EeFootprint, model_ext: str = "wrl") -> Path | None:
        """Export footprint to .kicad_mod and return the file path."""
        try:
            footprint_file = self.footprint_dir / f"{ee_footprint.info.name}.kicad_mod"

            if footprint_file.exists() and not self.config.overwrite:
                self._print(
                    f"Footprint '{footprint_file.name}' already exists in "
                    f"{self.footprint_dir.name} (overwrite disabled)."
                )
                return None

            model_3d_path = f"{self.config.lib_var}/{self.config.lib_name}.3dshapes"
            ExporterFootprintKicad(footprint=ee_footprint).export(
                footprint_full_path=str(footprint_file),
                model_3d_path=model_3d_path,
                model_3d_extension=model_ext,
            )
            self._print(f"Created footprint: {footprint_file.name} -> {self.footprint_dir.name}")
            return footprint_file

        except Exception as e:
            self._print(f"Failed to import footprint: {e}")
            logger.error(f"Footprint import failed: {e}")
            return None

    def _import_3d_model(self, cad_data: dict[str, Any]) -> tuple[Path | None, Path | None]:
        """Download and export the 3D model. Returns (wrl_path, step_path).

        Files are always written as plain ``.step`` / ``.wrl``: the footprint points at
        the model by name, so a compressed file is one more way for the 3D viewer to
        find nothing, while plain files open in every KiCad version.
        """
        try:
            model_3d = Easyeda3dModelImporter(
                easyeda_cp_cad_data=cad_data,
                download_raw_3d_model=True,
                api=self.api,
            ).output

            if model_3d is None:
                self._print("No 3D model available for this component.")
                return None, None

            exporter = Exporter3dModelKicad(model_3d=model_3d)
            if not exporter.output:
                self._print("No 3D model available for this component.")
                return None, None

            model_name = exporter.output.name
            exists_msg = f"3D model '{model_name}' already exists in {self.model_dir.name} (overwrite disabled)."

            if not exporter.export(output_dir=str(self.model_dir), overwrite=self.config.overwrite):
                self._print(exists_msg)
                return None, None

            wrl_path = self.model_dir / f"{model_name}.wrl"
            step_path = self.model_dir / f"{model_name}.step"

            wrl: Path | None = wrl_path if wrl_path.exists() else None
            step: Path | None = step_path if step_path.exists() else None

            if self.config.compress_models:
                # Settings may still carry the old "zip 3D" flag; it is ignored, the
                # files are kept plain so every KiCad version can open them.
                logger.info("compress_models is set but EasyEDA models are always written plain")

            if wrl:
                self._print(f"Created 3D model (WRL): {wrl.name} -> {self.model_dir.name}")
            if step:
                self._print(f"Created 3D model (STEP): {step.name} -> {self.model_dir.name}")

            return wrl, step

        except Exception as e:
            self._print(f"Failed to import 3D model: {e}")
            logger.error(f"3D model import failed: {e}")
            return None, None

    def import_component(self, component_id: str) -> ImportPaths:
        """Import a component and return the paths of its imported assets."""
        self._print(f"Starting import for EasyEDA/LCSC component: {component_id}")
        self._print(f"Target library: '{self.config.lib_name}' in {self.config.base_folder}")

        if not component_id.startswith("C"):
            error_msg = (
                f"Invalid component ID: '{component_id}' (must start with 'C', e.g., 'C2040')"
            )
            self._print(error_msg)
            raise ValueError(error_msg)

        self._ensure_directories()

        try:
            cad_data = self.api.get_cad_data_of_component(lcsc_id=component_id)
            if not cad_data:
                error_msg = f"Failed to fetch CAD data for component {component_id}"
                self._print(error_msg)
                raise RuntimeError(error_msg)

            # Parse footprint for footprint export
            ee_footprint = EasyedaFootprintImporter(easyeda_cp_cad_data=cad_data).get_footprint()

            symbol_ok, symbol_name = self._import_symbol(cad_data)
            if symbol_ok and symbol_name:
                self._set_symbol_value(symbol_name, self.component_value(cad_data, symbol_name))
            wrl_path, step_path = self._import_3d_model(cad_data)
            use_step = self.config.prefer_step and step_path is not None
            model_ext = "step" if use_step else "wrl"
            footprint_path = self._import_footprint(ee_footprint, model_ext=model_ext)

            result = ImportPaths(
                symbol_lib=self.symbol_lib_path if symbol_ok else None,
                footprint_file=footprint_path,
                model_wrl=wrl_path,
                model_step=step_path,
            )

            created_files = sum(1 for path in result if path)
            if created_files > 0:
                self._print(
                    f"EasyEDA import of {component_id} into '{self.config.lib_name}' "
                    f"completed ({created_files} files created)"
                )
            else:
                self._print(
                    f"EasyEDA import of {component_id} completed, "
                    f"no new files created in '{self.config.lib_name}'"
                )

            return result

        except Exception as e:
            self._print(f"EasyEDA import of {component_id} failed: {e}")
            logger.error(f"Component import failed for {component_id}: {e}")
            raise


def import_easyeda_component(
    component_id: str, config: ImportConfig, print_func: Callable[[str], None]
) -> ImportPaths:
    importer = EasyEDAImporter(config, print_func)
    return importer.import_component(component_id)


# Example usage
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    config = ImportConfig(
        base_folder=Path("~/Documents/Kicad/EasyEDA"),
        lib_name="EasyEDA",
        overwrite=False,
    )

    try:
        paths = import_easyeda_component("C2040", config, print)

        if paths.symbol_lib:
            print(f"Symbol was written to library: {paths.symbol_lib}")
        if paths.footprint_file:
            print(f"Footprint was created at: {paths.footprint_file}")
        if paths.model_wrl or paths.model_step:
            print("3D models were created:")
            if paths.model_wrl:
                print(f"  WRL: {paths.model_wrl}")
            if paths.model_step:
                print(f"  STEP: {paths.model_step}")

    except Exception as e:
        print(f"Import failed: {e}")
