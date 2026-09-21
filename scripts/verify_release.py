# SPDX-License-Identifier: MPL-2.0
"""Fail when a frozen engine loses notices or gains unreviewed font payloads."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from PyInstaller.archive.readers import CArchiveReader
from cvgnome_engine.legacy_compatibility import LEGACY_ENGINE_PACKAGE

ROOT = Path(__file__).resolve().parents[1]


def verify(binary: Path) -> None:
    archive = CArchiveReader(str(binary))
    modules = archive.open_embedded_archive("PYZ.pyz").toc
    required_modules = {"cvgnome_engine", "cvgnome_engine.workspace_transfer",
                        "cvgnome_engine.source_ingest.parser_memory"}
    if not required_modules <= set(modules) or any(
            name == LEGACY_ENGINE_PACKAGE or name.startswith(f"{LEGACY_ENGINE_PACKAGE}.") for name in modules):
        raise SystemExit("Frozen engine has stale or missing CVGnome modules; rebuild before release")
    resources = ROOT / "src-tauri/resources"
    for name in ("THIRD_PARTY_NOTICES.txt", "third-party-inventory.json", "LICENSE", "NOTICE"):
        expected = ((ROOT if name in {"LICENSE", "NOTICE"} else resources) / name).read_bytes()
        key = f"licenses/{name}"
        if key not in archive.toc:
            raise SystemExit(f"Frozen engine is missing {name}; rebuild before release")
        if not expected.strip() or archive.extract(key) != expected:
            raise SystemExit(f"Frozen engine has missing or stale {name}")
    inventory = json.loads((resources / "third-party-inventory.json").read_text())
    notices = (resources / "THIRD_PARTY_NOTICES.txt").read_bytes()
    if hashlib.sha256(notices).hexdigest() != inventory["notice_sha256"]:
        raise SystemExit("Notice text does not match its inventory")
    for path, expected in inventory["lockfiles"].items():
        if hashlib.sha256((ROOT / path).read_bytes()).hexdigest() != expected:
            raise SystemExit(f"Notice inventory is stale for {path}; rebuild the engine")
    expected_fonts = {f"reportlab/fonts/{name}" for name in (
        "Vera.ttf", "VeraBd.ttf", "VeraIt.ttf", "bitstream-vera-license.txt")}
    actual_fonts = {name for name in archive.toc if name.startswith("reportlab/fonts/")}
    if actual_fonts != expected_fonts:
        raise SystemExit(f"Unreviewed/missing font payload: {sorted(actual_fonts ^ expected_fonts)}")
    required = {("python", "python-docx"), ("python", "reportlab"), ("runtime", "CPython"),
                ("font", "Bitstream Vera"), ("npm", "react"), ("cargo", "tauri")}
    present = {(item["ecosystem"], item["name"]) for item in inventory["packages"] if item["notices"]}
    if not required <= present:
        raise SystemExit(f"Required notice records are absent: {sorted(required - present)}")
    print(f"Verified frozen notices and four required font assets: {binary.name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-binary", required=True, type=Path)
    verify(parser.parse_args().engine_binary)
