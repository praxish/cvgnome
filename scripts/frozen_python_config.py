# SPDX-License-Identifier: MPL-2.0
"""Remove development-machine paths from frozen Python build metadata only."""
from __future__ import annotations

import argparse
import importlib
from pathlib import Path
import pprint
import re
import sysconfig
import tempfile
from types import CodeType


_HOME_PREFIX = re.compile(r"/(?:Users|home)/[^/\s'\";:]+")
_MAC_TEMP_PREFIX = re.compile(r"/(?:private/)?var/folders/[^/\s]+/[^/\s]+/[TtCc](?=/|$)")


def _path_prefixes() -> list[tuple[str, str]]:
    prefixes = [(str(Path.home()), "/build-user")]
    temporary = tempfile.gettempdir().rstrip("/")
    if temporary not in {"", "/tmp", "/private/tmp", "/var/tmp"}:
        prefixes.append((temporary, "/build-temp"))
    return sorted(prefixes, key=lambda item: len(item[0]), reverse=True)


def sanitized_variables(variables: dict[str, object]) -> dict[str, object]:
    """Keep every build variable; remap only developer home/temp path prefixes.

    These install/build paths do not exist on recipient machines. ABI flags,
    numeric limits, platform tags and all other values remain unchanged. The
    original dictionary and the installed Python files are never modified.
    """
    result = {}
    for key, value in variables.items():
        if isinstance(value, str):
            for prefix, replacement in _path_prefixes():
                if prefix and prefix != "/":
                    value = value.replace(prefix, replacement)
            value = _HOME_PREFIX.sub("/build-user", value)
            value = _MAC_TEMP_PREFIX.sub("/build-temp", value)
        result[key] = value
    return result


def contains_private_build_path(value: str) -> bool:
    return bool(
        _HOME_PREFIX.search(value)
        or _MAC_TEMP_PREFIX.search(value)
        or any(prefix and prefix != "/" and prefix in value for prefix, _ in _path_prefixes())
    )


def code_has_private_build_path(code: CodeType) -> bool:
    def inspect(value: object) -> bool:
        if isinstance(value, CodeType):
            return inspect((value.co_filename, *value.co_consts))
        if isinstance(value, (tuple, frozenset)):
            return any(inspect(item) for item in value)
        return isinstance(value, str) and contains_private_build_path(value)

    return inspect(code)


def generate(output_directory: Path) -> None:
    hooks = output_directory / "hooks" / "pre_find_module_path"
    modules = output_directory / "modules"
    hooks.mkdir(parents=True, exist_ok=True)
    modules.mkdir(parents=True, exist_ok=True)
    try:
        module_name = sysconfig._get_sysconfigdata_name()
    except AttributeError:
        return  # Windows CPython has no separate sysconfig-data module.
    if not re.fullmatch(r"_sysconfigdata_[A-Za-z0-9_]+", module_name):
        raise RuntimeError("Unexpected Python build-configuration module name")
    original = importlib.import_module(module_name).build_time_vars
    sanitized = sanitized_variables(original)
    source = "# Generated build metadata; installed Python remains unchanged.\n"
    source += "build_time_vars = " + pprint.pformat(sanitized, sort_dicts=True) + "\n"
    if code_has_private_build_path(compile(source, f"{module_name}.py", "exec")):
        raise RuntimeError("Python build metadata still contains a private build path")
    (modules / f"{module_name}.py").write_text(source, encoding="utf-8")
    # This documented PyInstaller hook selects our copy for analysis only. It
    # does not change sys.path or sysconfig in the build interpreter.
    (hooks / f"hook-{module_name}.py").write_text(
        "from pathlib import Path\n\n"
        "def pre_find_module_path(api):\n"
        "    api.search_dirs = [str(Path(__file__).resolve().parents[2] / 'modules')]\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    generate(parser.parse_args().output_dir)
