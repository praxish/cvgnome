# SPDX-License-Identifier: MPL-2.0
"""Collect exact installed dependency notices; never fetch licenses at build time.

The inventory deliberately includes the resolved Rust build graph and installed
Python tooling. It is an attribution inventory, not a claim that every listed
package is linked into the application or a complete binary SBOM.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "src-tauri" / "resources"
PREFIXES = ("license", "licence", "copying", "copyright", "notice")


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_notice(path: Path, label: str, source: str | None = None) -> dict:
    data = path.read_bytes()
    if not data.strip():
        raise RuntimeError(f"Empty license notice: {path}")
    return {"path": label, "sha256": digest(data), "source": source,
            "text": data.decode("utf-8", errors="replace")}


def license_files(directory: Path) -> list[Path]:
    # Include notices for vendored native code as well as the crate/package itself.
    return sorted(p for p in directory.rglob("*")
                  if p.is_file() and p.name.lower().startswith(PREFIXES)
                  and "node_modules" not in p.relative_to(directory).parts)


def python_notices() -> list[dict]:
    records = []
    for distribution in sorted(metadata.distributions(), key=lambda d: d.metadata["Name"].lower()):
        name = distribution.metadata["Name"]
        if name == "cvgnome-engine":
            continue
        files = [p for p in distribution.files or []
                 if ".dist-info" in str(p) and Path(str(p)).name.lower().startswith(PREFIXES)]
        if not files:
            raise RuntimeError(f"Missing Python license text: {name} {distribution.version}")
        records.append({"ecosystem": "python", "name": name, "version": distribution.version,
                        "scope": "installed engine and build environment",
                        "license": distribution.metadata.get("License-Expression") or distribution.metadata.get("License"),
                        "notices": [read_notice(Path(distribution.locate_file(p)), str(p)) for p in files]})
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    records.append({"ecosystem": "runtime", "name": "CPython", "version": sys.version.split()[0],
                    "scope": "bundled interpreter", "license": "PSF-2.0 and included third-party notices",
                    "notices": [read_notice(python_license, "CPython/LICENSE.txt")]})
    import reportlab
    font_notice = Path(reportlab.__file__).parent / "fonts" / "bitstream-vera-license.txt"
    records.append({"ecosystem": "font", "name": "Bitstream Vera", "version": "ReportLab bundled",
                    "scope": "Vera.ttf, VeraBd.ttf, VeraIt.ttf", "license": "Bitstream-Vera",
                    "notices": [read_notice(font_notice, "reportlab/fonts/bitstream-vera-license.txt")]})
    return records


def npm_notices() -> list[dict]:
    lock = json.loads((ROOT / "package-lock.json").read_text())
    records = []
    for relative, package in sorted(lock["packages"].items()):
        if not relative or package.get("dev") or package.get("devOptional"):
            continue
        directory = ROOT / relative
        if not directory.is_dir():
            raise RuntimeError(f"Missing installed npm package: {relative}; run npm ci")
        installed = json.loads((directory / "package.json").read_text())
        if installed["version"] != package["version"]:
            raise RuntimeError(f"npm package differs from lock: {relative}; run npm ci")
        files = license_files(directory)
        if not files:
            raise RuntimeError(f"Missing npm license text: {relative}")
        records.append({"ecosystem": "npm", "name": installed["name"], "version": package["version"],
                        "scope": "production dependency graph (may be tree-shaken)",
                        "license": package.get("license") or installed.get("license"),
                        "notices": [read_notice(p, str(p.relative_to(directory))) for p in files]})
    return records


def rust_notices(target: str) -> list[dict]:
    cargo = shutil.which("cargo")
    if cargo is None and Path("/opt/homebrew/opt/rustup/bin/cargo").is_file():
        cargo = "/opt/homebrew/opt/rustup/bin/cargo"
    if cargo is None:
        raise RuntimeError("cargo is required to inventory Rust dependency notices")
    env = os.environ.copy()
    if not shutil.which("rustc") and Path("/opt/homebrew/opt/rustup/bin/rustc").is_file():
        env["RUSTC"] = "/opt/homebrew/opt/rustup/bin/rustc"
    result = subprocess.run([cargo, "metadata", "--locked", "--format-version", "1", "--filter-platform", target,
                             "--manifest-path", str(ROOT / "src-tauri/Cargo.toml")],
                            check=True, capture_output=True, text=True, env=env)
    graph = json.loads(result.stdout)
    resolved = {node["id"] for node in graph["resolve"]["nodes"]}
    overrides = json.loads((ROOT / "scripts/license-overrides.json").read_text())
    records = []
    for package in sorted(graph["packages"], key=lambda p: (p["name"], p["version"])):
        if package["id"] not in resolved or not package["source"]:
            continue
        directory = Path(package["manifest_path"]).parent
        files = license_files(directory)
        if package.get("license_file"):
            explicit = directory / package["license_file"]
            if explicit not in files:
                files.append(explicit)
        notices = [read_notice(p, str(p.relative_to(directory))) for p in files]
        override = overrides.get(f'{package["name"]}@{package["version"]}')
        if override:
            if override["license"] != package["license"]:
                raise RuntimeError(f'License declaration changed for {package["name"]}')
            for item in override["files"]:
                notice = read_notice(ROOT / "scripts" / item["path"], item["path"], item["source"])
                if notice["sha256"] != item["sha256"]:
                    raise RuntimeError(f'License override checksum changed: {item["path"]}')
                notices.append(notice)
        if not notices:
            raise RuntimeError(f'Missing Rust license text: {package["name"]} {package["version"]}')
        records.append({"ecosystem": "cargo", "name": package["name"], "version": package["version"],
                        "scope": "target-resolved dependency graph, including build and dev dependencies",
                        "license": package["license"], "repository": package.get("repository"),
                        "authors": package.get("authors", []), "notices": notices})
    return records


def generate(target: str) -> None:
    records = python_notices() + npm_notices() + rust_notices(target)
    records.sort(key=lambda item: (item["ecosystem"], item["name"], item["version"]))
    heading = ("CVGnome third-party notices\n\n"
               "This file preserves notices from installed locked dependencies. The inventory includes\n"
               "build tooling and dependency graphs; it is not a binary software bill of materials.\n"
               "Upstream license declarations and authors are reproduced without changing their terms.\n"
               "Generic license templates remain verbatim where upstream links to a standard license.\n\n")
    sections = [heading]
    inventory = []
    for record in records:
        sections.append("=" * 78 + f'\n{record["ecosystem"]}: {record["name"]} {record["version"]}\n'
                        f'License declaration: {record["license"]}\nScope: {record["scope"]}\n')
        if record.get("repository"):
            sections.append(f'Upstream: {record["repository"]}\n')
        if record.get("authors"):
            sections.append(f'Authors declared upstream: {", ".join(record["authors"])}\n')
        clean = {key: value for key, value in record.items() if key != "notices"}
        clean["notices"] = []
        for notice in record["notices"]:
            sections.append(f'\n--- {notice["path"]} ---\n')
            if notice["source"]:
                sections.append(f'Source: {notice["source"]}\n')
            sections.append(notice["text"] + "\n")
            clean["notices"].append({k: v for k, v in notice.items() if k != "text"})
        inventory.append(clean)
    DESTINATION.mkdir(parents=True, exist_ok=True)
    notices = "".join(sections).encode()
    (DESTINATION / "THIRD_PARTY_NOTICES.txt").write_bytes(notices)
    (DESTINATION / "third-party-inventory.json").write_text(json.dumps({
        "schema_version": 1, "target": target, "notice_sha256": digest(notices),
        "lockfiles": {p: digest((ROOT / p).read_bytes()) for p in
                      ("package-lock.json", "engine/uv.lock", "src-tauri/Cargo.lock")},
        "packages": inventory}, indent=2, sort_keys=True) + "\n")
    print(f"Collected {len(records)} dependency/font/runtime notice records for {target}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    arguments = parser.parse_args()
    generate(arguments.target)
