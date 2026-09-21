# SPDX-License-Identifier: MPL-2.0
"""RPC assertions for payloads emitted by the production Rust request builder.

This is invoked by the ignored Rust integration test, through
scripts/check_native_engine_contract.py. It deliberately never reconstructs or
normalizes the list params: the exact native-generated JSON reaches the engine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"


def main() -> None:
    cases = json.load(sys.stdin)
    expected_titles = {
        "missing": {"Python Engineer", "Technical Writer"},
        "blank": {"Python Engineer", "Technical Writer"},
        "whitespace": {"Python Engineer", "Technical Writer"},
        "search": {"Python Engineer"},
        "no_match": set(),
        "filtered": {"Python Engineer"},
        "next_page": {"Python Engineer"},
    }
    assert {case["id"] for case in cases} == set(expected_titles)
    binary = os.environ.get("CVGNOME_CONTRACT_ENGINE_BINARY")
    command = [binary] if binary else [sys.executable, "-m", "cvgnome_engine"]
    env = {**os.environ, "PYTHONPATH": str(ENGINE_SRC)}

    with tempfile.TemporaryDirectory(prefix="cvgnome-native-contract-") as directory:
        def rpc(request_id: str, method: str, params: dict) -> dict:
            request = {
                "protocol_version": 1,
                "id": request_id,
                "method": method,
                "params": params,
            }
            completed = subprocess.run(
                [*command, "request", "--data-dir", directory],
                input=json.dumps(request) + "\n",
                capture_output=True,
                text=True,
                env=env,
                check=False,
                timeout=30,
            )
            assert completed.returncode == 0, (
                f"{request_id}: RPC exited {completed.returncode}; "
                f"stdout={completed.stdout!r}; stderr={completed.stderr!r}"
            )
            response = json.loads(completed.stdout)
            assert response["protocol_version"] == 1
            assert response["id"] == request_id
            assert response["ok"], f"{request_id}: {response}"
            return response["result"]

        # The default Roles view must work before any role has been saved.
        for case in cases:
            result = rpc(f"empty-{case['id']}", "opportunities.list", case["params"])
            assert result["total"] == 0 and result["items"] == [], result

        rpc("profile", "profile.start.manual", {
            "request_id": str(uuid.uuid4()),
            "expected_parent_profile_version_id": None,
            "patch": {"name": "Contract Test", "summary": "Builds Python services and clear documentation."},
        })
        saved = {}
        for title, description in (
            ("Python Engineer", "Build Python services."),
            ("Technical Writer", "Write clear documentation."),
        ):
            result = rpc(title, "opportunities.save_match", {
                "title": title,
                "description": description,
                "company": "Example",
                "location": "Remote",
                "source_url": None,
                "apply_url": None,
            })
            saved[title] = result["opportunity"]
        engineer = saved["Python Engineer"]
        rpc("apply-stage", "opportunities.update", {
            "opportunity_id": engineer["id"],
            "expected_tracker_updated_at_ms": engineer["tracker_updated_at_ms"],
            "tracker_stage": "applied",
        })

        for case in cases:
            result = rpc(case["id"], "opportunities.list", case["params"])
            titles = {item["title"] for item in result["items"]}
            assert titles == expected_titles[case["id"]], (case["id"], result)
            expected_total = 2 if case["id"] == "next_page" else len(titles)
            assert result["total"] == expected_total, (case["id"], result)
    print(f"Verified {len(cases)} native-generated list requests against {'frozen' if binary else 'source'} engine RPC.")


if __name__ == "__main__":
    main()
