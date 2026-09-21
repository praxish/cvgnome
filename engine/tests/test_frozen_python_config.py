# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

import ast
import importlib
from pathlib import Path
import runpy
import sys
import sysconfig
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from frozen_python_config import (  # noqa: E402
    code_has_private_build_path,
    generate,
    sanitized_variables,
)


class FrozenPythonConfigTests(unittest.TestCase):
    def test_only_home_and_temporary_prefixes_change(self) -> None:
        variables = {
            "BINDIR": "/Users/Example Builder/python/bin",
            "CFLAGS": "-I/home/builder/python/include -O2 -I/usr/include",
            "CONFIG_ARGS": "--srcdir=/private/var/folders/ab/synthetic/T/build/Python",
            "SOABI": "cpython-314-darwin",
            "SIZEOF_VOID_P": 8,
            "FLAG": False,
            "UNUSED": None,
            "SUFFIX": ".cpython-314-darwin.so",
        }
        before = variables.copy()
        with patch("frozen_python_config._path_prefixes", return_value=[("/Users/Example Builder", "/build-user")]):
            actual = sanitized_variables(variables)
        self.assertEqual(variables, before)
        expected = before | {
            "BINDIR": "/build-user/python/bin",
            "CFLAGS": "-I/build-user/python/include -O2 -I/usr/include",
            "CONFIG_ARGS": "--srcdir=/build-temp/build/Python",
        }
        self.assertEqual(actual, expected)
        self.assertEqual({key: type(value) for key, value in actual.items()},
                         {key: type(value) for key, value in before.items()})

    def test_archive_gate_sees_compressed_module_constants_and_nested_code(self) -> None:
        for source in (
            "build_time_vars = {'BINDIR': '/Users/Example/python/bin'}",
            "def metadata(): return '/private/var/folders/aa/synthetic/T/python'",
            "build_time_vars = {'PATHS': ('/home/example/python/bin', 'public')}",
        ):
            self.assertTrue(code_has_private_build_path(compile(source, "config.py", "exec")))
        self.assertFalse(code_has_private_build_path(compile(
            "build_time_vars = {'BINDIR': '/build-user/python/bin', 'SIZEOF_VOID_P': 8}",
            "config.py", "exec")))

    @unittest.skipIf(sys.platform == "win32", "Windows CPython has no sysconfig-data module")
    def test_generated_hook_selects_copy_without_changing_installed_python(self) -> None:
        name = sysconfig._get_sysconfigdata_name()
        module = importlib.import_module(name)
        source_path = Path(module.__file__)
        before_bytes = source_path.read_bytes()
        before_variables = module.build_time_vars.copy()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            generate(output)
            hook_path = output / "hooks" / "pre_find_module_path" / f"hook-{name}.py"
            hook = runpy.run_path(str(hook_path))["pre_find_module_path"]
            api = SimpleNamespace(search_dirs=[str(source_path.parent)])
            hook(api)
            self.assertEqual(api.search_dirs, [str((output / "modules").resolve())])
            generated = (Path(api.search_dirs[0]) / f"{name}.py").read_text()
            data = ast.literal_eval(ast.parse(generated).body[0].value)
            self.assertEqual(data, sanitized_variables(before_variables))
            self.assertEqual(data.keys(), before_variables.keys())
            self.assertFalse(code_has_private_build_path(compile(generated, f"{name}.py", "exec")))
        self.assertEqual(module.build_time_vars, before_variables)
        self.assertEqual(source_path.read_bytes(), before_bytes)


if __name__ == "__main__":
    unittest.main()
