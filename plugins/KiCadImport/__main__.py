#!/usr/bin/env python3
# KiCad Parts Hub - a modified version of Import-LIB-KiCad-Plugin by Steffen-W
# (https://github.com/Steffen-W/Import-LIB-KiCad-Plugin), GPL-3.0.
# Modified on 2026-10-17; see NOTICE.md for the list of changes.

# Assembles local KiCad component libraries from downloaded Octopart,
# Samacsys, Ultralibrarian and Snapeda zipfiles using kiutils.
# Supports KiCad 7.0 and newer.

import logging
from pathlib import Path

from . import main

logger = logging.getLogger(__name__)

if __name__ == "__main__":
    import argparse

    logging.basicConfig(level=logging.ERROR)

    # Example: python plugins/KiCadImport.py --lib-folder import_test --download-folder Demo/libs

    parser = argparse.ArgumentParser(
        description="Import vendor ZIPs (or one EasyEDA part) into a KiCad library folder."
    )

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--download-folder",
        help="Import every ZIP found in this folder.",
    )
    group.add_argument("--download-file", help="Import this ZIP file.")

    group.add_argument("--easyeda", help="Import an EasyEDA part by number, for example C2040.")

    parser.add_argument(
        "--lib-folder",
        required=True,
        help="Folder the imported KiCad files are written to.",
    )

    parser.add_argument(
        "--overwrite-if-exists",
        action="store_true",
        help="Replace files that are already in the library.",
    )

    parser.add_argument(
        "--path-variable",
        help="Name written into the library tables (default ${KICAD_3RD_PARTY}).",
    )

    parser.add_argument(
        "--prefer-step",
        action="store_true",
        help="EasyEDA: link the STEP model instead of the WRL one.",
    )

    parser.add_argument(
        "--lib-name",
        help="Put every import into one library with this name.",
    )

    parser.add_argument(
        "--compress-models",
        action="store_true",
        help="Write the STEP models gzipped (.step.gz).",
    )

    args = parser.parse_args()

    lib_folder = Path(args.lib_folder)

    if args.path_variable:
        path_variable = str(args.path_variable).strip()
    else:
        path_variable = "${KICAD_3RD_PARTY}"

    if args.download_file:
        main(
            lib_file=args.download_file,
            lib_folder=args.lib_folder,
            overwrite=args.overwrite_if_exists,
            KICAD_3RD_PARTY_LINK=path_variable,
            lib_name=args.lib_name,
            compress_models=args.compress_models,
        )
    elif args.download_folder:
        download_folder = Path(args.download_folder)
        logger.info(f"Processing folder: {download_folder}")
        if not download_folder.is_dir():
            logger.error(f"Source folder {download_folder} does not exist")
            print(f"Error Source folder {download_folder} does not exist!")
        elif not lib_folder.is_dir():
            logger.error(f"Destination folder {lib_folder} does not exist")
            print(f"Error destination folder {lib_folder} does not exist!")
        else:
            zip_files = list(download_folder.glob("*.zip"))
            logger.info(f"Found {len(zip_files)} zip files to process")
            for zip_file in zip_files:
                if zip_file.is_file() and zip_file.stat().st_size >= 1024:
                    main(
                        lib_file=zip_file,
                        lib_folder=args.lib_folder,
                        overwrite=args.overwrite_if_exists,
                        KICAD_3RD_PARTY_LINK=path_variable,
                        lib_name=args.lib_name,
                        compress_models=args.compress_models,
                    )
                else:
                    logger.warning(f"Skipping {zip_file}: too small or not a file")
    elif args.easyeda:
        logger.info(f"Processing EasyEDA component: {args.easyeda}")
        if not lib_folder.is_dir():
            logger.error(f"Destination folder {lib_folder} does not exist")
            print(f"Error destination folder {lib_folder} does not exist!")
        else:
            component_id = str(args.easyeda).strip()
            print("Try to import EasyEDA / LCSC Part# : ", component_id)
            from impart_easyeda import EasyEDAImporter, ImportConfig

            try:
                config = ImportConfig(
                    base_folder=lib_folder,
                    lib_name=args.lib_name or "EasyEDA",
                    overwrite=args.overwrite_if_exists,
                    lib_var=path_variable,
                    prefer_step=args.prefer_step,
                    compress_models=args.compress_models,
                )

                logger.debug(f"EasyEDA config: {config}")
                paths = EasyEDAImporter(config).import_component(component_id)
                logger.info(f"EasyEDA import completed for {component_id}")

                if paths.symbol_lib:
                    print(f"Library path : {paths.symbol_lib}")
                if paths.footprint_file:
                    print(f"Footprint path: {paths.footprint_file}")
                if paths.model_wrl:
                    print(f"3D model path (wrl): {paths.model_wrl}")
                if paths.model_step:
                    print(f"3D model path (step): {paths.model_step}")

            except Exception as e:
                logger.error(f"EasyEDA import failed for {component_id}: {e}")
                print(f"Error importing component: {e}")
