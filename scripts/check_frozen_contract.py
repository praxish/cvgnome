# SPDX-License-Identifier: MPL-2.0
"""Run the native/engine contract against this host's packaged engine."""
import subprocess
import sys

from build_engine import ROOT, TAURI_BINARIES, _target_triple

suffix = ".exe" if sys.platform == "win32" else ""
binary = TAURI_BINARIES / f"cvgnome-engine-{_target_triple()}{suffix}"
if not binary.is_file():
    raise SystemExit("Build the sidecar first with npm run engine:build")
subprocess.run([sys.executable, str(ROOT / "scripts/check_native_engine_contract.py"),
                "--engine-binary", str(binary)], check=True)
