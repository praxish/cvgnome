# SPDX-License-Identifier: MPL-2.0
"""Assemble an explicitly scoped, history-free source snapshot for publication."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    "README.md", "CHANGELOG.md", "LICENSE", "NOTICE", "CONTRIBUTING.md",
    "SECURITY.md", ".gitignore", ".gitattributes", ".node-version", ".uv-version",
    "rust-toolchain.toml", "package.json", "package-lock.json", "index.html",
    "tsconfig.json", "tsconfig.node.json", "vite.config.ts", "vitest.config.ts",
}
DIRECTORIES = ("src", "public", "docs", "scripts", ".github")
NATIVE_FILES = {
    "src-tauri/Cargo.toml", "src-tauri/Cargo.lock", "src-tauri/build.rs",
    "src-tauri/tauri.conf.json", "src-tauri/.gitignore",
    "src-tauri/binaries/.gitkeep",
}
ENGINE_FILES = {
    "engine/README.md", "engine/pyproject.toml", "engine/uv.lock",
    "engine/.python-version", "engine/sidecar_entry.py",
}
EXTRA_DIRECTORIES = (
    "src-tauri/src", "src-tauri/icons", "src-tauri/capabilities",
    "engine/src", "engine/tests",
)
EXCLUDED = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git",
    ".venv", "node_modules", "target", "dist", "build", "gen", ".DS_Store",
}
TEXT_SUFFIXES = {".py", ".rs", ".ts", ".tsx", ".css", ".mjs", ".js", ".json", ".toml", ".yml", ".yaml", ".md", ".txt", ".svg", ".lock"}
LEGACY_PATTERN = re.compile("career" + "vault|resu" + "maid", re.IGNORECASE)
LEGACY_ALLOWLIST = {"engine/src/cvgnome_engine/legacy_compatibility.py"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b"),
)
PRIVATE_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9_.-]+/")


def selected_files(root: Path) -> list[Path]:
    selected: set[Path] = set()
    for name in ROOT_FILES | NATIVE_FILES | ENGINE_FILES:
        path = root / name
        if path.exists() or path.is_symlink():
            selected.add(path)
    for name in DIRECTORIES + EXTRA_DIRECTORIES:
        directory = root / name
        if directory.is_symlink():
            raise ValueError(f"Publication directory is a symlink: {name}")
        if not directory.is_dir():
            raise ValueError(f"Publication directory is missing: {name}")
        for path in directory.rglob("*"):
            relative = path.relative_to(root)
            if EXCLUDED.intersection(relative.parts) or any(part.endswith(".egg-info") for part in relative.parts):
                continue
            if path.is_symlink():
                raise ValueError(f"Publication file is a symlink: {relative}")
            if path.is_file():
                selected.add(path)
    return sorted(selected, key=lambda path: path.relative_to(root).as_posix())


def inspect(root: Path) -> tuple[list[dict[str, object]], list[str]]:
    records: list[dict[str, object]] = []
    errors: list[str] = []
    for path in selected_files(root):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink() or not path.is_file():
            errors.append(f"Unsafe publication entry: {relative}")
            continue
        content = path.read_bytes()
        if path.suffix.lower() in {".sqlite3", ".db", ".docx", ".pdf", ".zip", ".p12", ".pfx", ".key"}:
            errors.append(f"Personal-data or generated binary format requires exclusion: {relative}")
        if path.name.startswith(".env"):
            errors.append(f"Environment file must not be published: {relative}")
        if path.suffix in TEXT_SUFFIXES or path.name in ROOT_FILES:
            try:
                text = content.decode("utf-8")
            except UnicodeDecodeError:
                errors.append(f"Expected UTF-8 source: {relative}")
                continue
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    errors.append(f"Credential-pattern candidate needs review: {relative}")
                    break
            if PRIVATE_PATH.search(text):
                errors.append(f"Private absolute path needs removal: {relative}")
            if relative not in LEGACY_ALLOWLIST and LEGACY_PATTERN.search(text):
                errors.append(f"Former product name outside compatibility module: {relative}")
        records.append({"path": relative, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()})
    names = {record["path"] for record in records}
    for required in {"LICENSE", "NOTICE", "README.md", "package-lock.json", "engine/uv.lock", "src-tauri/Cargo.lock"}:
        if required not in names:
            errors.append(f"Required publication file missing: {required}")
    return records, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, help="New directory for the reviewed source snapshot; existing paths are refused")
    args = parser.parse_args()
    records, errors = inspect(ROOT)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    if args.destination:
        destination = args.destination.absolute()
        if destination.is_relative_to(ROOT) or ROOT.is_relative_to(destination):
            raise ValueError("Source snapshot must be separate from the development tree")
        destination.mkdir(mode=0o755, parents=False, exist_ok=False)
        try:
            for record in records:
                relative = str(record["path"])
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
                if hashlib.sha256(target.read_bytes()).hexdigest() != record["sha256"]:
                    raise ValueError(f"Source changed during publication staging: {relative}")
            manifest = {"format": 1, "project": "CVGnome", "files": records}
            (destination / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=2) + "\n")
        except Exception:
            # This invocation exclusively created the destination. No existing
            # repository or user content is ever reused or removed.
            shutil.rmtree(destination)
            raise
    print(f"Publication audit passed: {len(records)} explicitly selected files; no development history or local application data included.")
    print("Pattern checks are bounded; review the manifest and asset contents before publication.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
