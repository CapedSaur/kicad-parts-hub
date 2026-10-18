# Notices and attribution

**KiCad Parts Hub** — <https://github.com/CapedSaur/kicad-parts-hub>

Copyright (C) 2026 CapedSaur and the contributors of this repository.

This file is the "prominent notice" required by section 5(a) of the GNU General
Public License v3.0: it states which files were modified, when, and which
third-party work they come from. Keep it with the source and with every
redistributed copy of the package (it is shipped inside `kicad-parts-hub-pcm.zip`).

## This is a modified version of another project

This repository is a **modified version** of
**[Import-LIB-KiCad-Plugin](https://github.com/Steffen-W/Import-LIB-KiCad-Plugin)**
(also known as *impartGUI*) by **Steffen-W** and its contributors, which is
licensed under the **GNU General Public License v3.0**.

**Modifications: first made on 2026-10-17 and continuing afterwards.** They are
not the work of the original author, and the original author does not endorse
this version.

Files derived from that project carry a short notice at the top. These are the
files that were changed or added on top of the original:

| Area | Files | What changed |
| --- | --- | --- |
| Importer | `plugins/KiCadImport/`, `plugins/impart_easyeda.py`, `plugins/FileHandler/`, `plugins/ConfigHandler/` | gzip/STEP handling, library writing, model linking, EasyEDA conversion fixes |
| GUI | `plugins/impart_action.py`, `plugins/impart_gui.py`, `plugins/__init__.py`, `plugins/__main__.py`, `plugins/main_window.py` | new main window layout, search tabs, notifications, project/global modes |
| KiCad setup | `plugins/KiCad_Settings/`, `plugins/KiCadSettingsPaths/`, `plugins/kicad_setup.py`, `plugins/kicad_cli/` | `KICAD_3RD_PARTY` handling, library tables, `.bak` copies, 3D link repair |
| Process / files | `plugins/single_instance_manager.py`, `plugins/atomic_file/` | single-instance IPC, atomic writes |
| New features (not in the original) | `plugins/snapeda_*.py`, `plugins/cse_*.py`, `plugins/library_scan.py`, `plugins/library_preview.py`, `plugins/library_manager.py`, `plugins/toast.py`, `plugins/user_paths.py` | SnapEDA / Component Search Engine / EasyEDA search, library manager and previews |
| Packaging and docs | `build_pcm_zip.py`, `tools/`, `tests/`, `README.md`, `NOTICE.md`, `metadata.json` | PCM packaging, release tooling, documentation, tests |

## Bundled third-party components

Both are vendored as-is (pinned to the versions in the table, which are not
tracked as git submodules) and keep their own license text next to the code
inside the installed package. When either copy is updated, update the version
here in the same commit.

| Component | Author | License | License text in the package | Source |
| --- | --- | --- | --- | --- |
| `kiutils` 1.4.8 | mvnmgrx and contributors | GPL-3.0 | `plugins/kiutils/LICENSE` | <https://github.com/mvnmgrx/kiutils> |
| `easyeda2kicad` 1.0.1 | uPesy and contributors | **AGPL-3.0** | `plugins/easyeda2kicad/LICENSE` | <https://github.com/uPesy/easyeda2kicad.py> |

The AGPL-3.0 network clause applies to `easyeda2kicad` itself; this project
distributes it unchanged and does not host it as a network service. If you
modify that component and let others use it over a network, section 13 of the
AGPL applies to your version.

The GUI skeleton of the original project was in turn based on
[wexi/impart](https://github.com/wexi/impart) and
[topherbuckley/kicad_remote_import](https://github.com/topherbuckley/kicad_remote_import);
credit for that foundation belongs to their authors.

## License of this repository

**GPL-3.0** — the full text is in [`LICENSE.txt`](LICENSE.txt), and the same text
ships as `plugins/LICENSE.txt` inside the package. Every distributed copy must
keep `LICENSE.txt`, this `NOTICE.md` and the two vendored license texts, and must
make the corresponding source available under the same license.

## Non-commercial project, free to share

This is a **non-commercial community project**: nobody sells it, there is no paid
version or paid support, it carries no advertising and no telemetry, and it is
published free of charge so other people can use and improve it.

That is a statement of intent, not an extra license restriction. The GPL-3.0
still gives everyone the right to redistribute this code, including for a fee,
provided the license terms are respected; nothing here narrows those rights.

## Trademarks and vendor data

KiCad, SnapEDA/SnapMagic, Component Search Engine (Samacsys), EasyEDA, LCSC,
Octopart and Ultra Librarian are names and trademarks of their respective owners.
They are used here only to describe what the plugin talks to. This project is
**not affiliated with, authorised, sponsored or endorsed by** any of them.

Symbols, footprints and 3D models downloaded through the plugin come from those
vendors and remain subject to **their** terms; the plugin only stores them in
your own libraries, on your own machine, for your own designs. Do not publish or
redistribute a vendor's library data as if it were yours.

## No warranty

This software is provided "as is", without warranty of any kind. Use it at your
own risk; see the *Disclaimer and acceptable use* section of the README.
