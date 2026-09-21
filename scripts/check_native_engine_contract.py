# SPDX-License-Identifier: MPL-2.0
"""Check actual native request payloads against the source or frozen Python engine.

Run with the engine environment, for example:
    uv run --project engine --no-dev python scripts/check_native_engine_contract.py
Pass --engine-binary PATH to check the same requests against a packaged sidecar.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-binary", type=Path)
    args = parser.parse_args()
    cargo = shutil.which("cargo")
    if cargo is None:
        parser.error("cargo was not found; add the stable Rust toolchain to PATH")
    env = {**os.environ, "CVGNOME_CONTRACT_PYTHON": sys.executable}
    env.pop("CVGNOME_CONTRACT_ENGINE_BINARY", None)
    if args.engine_binary is not None:
        engine_binary = args.engine_binary.resolve(strict=True)
        if not engine_binary.is_file():
            parser.error("--engine-binary must point to a sidecar executable")
        env["CVGNOME_CONTRACT_ENGINE_BINARY"] = str(engine_binary)
    completed = subprocess.run(
        [
            cargo,
            "test",
            "--locked",
            "--manifest-path",
            str(ROOT / "src-tauri" / "Cargo.toml"),
            "--lib",
            "tests::opportunity_list_native_payload_round_trips_engine_rpc",
            "--",
            "--exact",
            "--ignored",
            "--nocapture",
        ],
        cwd=ROOT,
        env=env,
        check=False,
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
