# SPDX-License-Identifier: MPL-2.0
from __future__ import annotations

from io import BytesIO
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZIP_DEFLATED, ZipFile


ENGINE_SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(ENGINE_SRC))

from cvgnome_engine.source_ingest import (  # noqa: E402
    ParserLimits,
    SourceParserError,
    default_parser_child_command,
    parse_structured_source,
    parser_child_main,
    source_file_identity,
)
from cvgnome_engine.source_ingest.parser import (  # noqa: E402
    _communicate_supervised,
    _terminate_process_tree,
)
from cvgnome_engine.source_ingest.parser_child import _lower_resource_limit  # noqa: E402
from cvgnome_engine.source_ingest.parser_core import ParserCoreError  # noqa: E402
from cvgnome_engine.source_ingest.parser_memory import (  # noqa: E402
    MemoryMonitorUnavailable,
    memory_sampler,
)


WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _write_docx(path: Path, paragraphs: list[str], *, xml_prefix: str = "") -> None:
    body = "".join(
        f"<w:p><w:r><w:t>{paragraph}</w:t></w:r></w:p>"
        for paragraph in paragraphs
    )
    document = (
        f'{xml_prefix}<w:document xmlns:w="{WORD_NAMESPACE}">'
        f"<w:body>{body}</w:body></w:document>"
    ).encode("utf-8")
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document)


class StructuredSourceParserTests(unittest.TestCase):
    def test_unavailable_memory_supervision_never_dispatches_a_document(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "resume.docx"
            _write_docx(path, ["Ada Lovelace"])
            with (
                patch("cvgnome_engine.source_ingest.parser.memory_sampler", side_effect=MemoryMonitorUnavailable),
                patch("cvgnome_engine.source_ingest.parser.subprocess.Popen") as spawn,
                self.assertRaises(SourceParserError) as raised,
            ):
                parse_structured_source(path)
            spawn.assert_not_called()
        self.assertEqual(raised.exception.code, "parser_unavailable")

    def test_unsupported_platform_cannot_silently_skip_memory_controls(self) -> None:
        with patch("cvgnome_engine.source_ingest.parser_memory.sys.platform", "win32"):
            with self.assertRaises(MemoryMonitorUnavailable):
                memory_sampler()

    def test_failed_or_ineffective_rlimits_fail_closed(self) -> None:
        for set_error in (ValueError("unsupported limit"), None):
            with self.subTest(set_error=set_error):
                resource = SimpleNamespace(
                    RLIMIT_AS=1,
                    RLIM_INFINITY=-1,
                    getrlimit=Mock(return_value=(-1, -1)),
                    setrlimit=Mock(side_effect=set_error),
                )
                with self.assertRaises(ParserCoreError) as raised:
                    _lower_resource_limit(resource, "RLIMIT_AS", 512 * 1024 * 1024)
                self.assertEqual(raised.exception.code, "parser_unavailable")

    @unittest.skipUnless(os.name == "posix", "requires a POSIX process group")
    def test_loss_of_memory_supervision_stops_a_running_child(self) -> None:
        with tempfile.TemporaryFile() as output:
            child = subprocess.Popen(
                [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(); time.sleep(30)"],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            with self.assertRaises(SourceParserError) as raised:
                _communicate_supervised(
                    child, b"request", output, timeout_seconds=2,
                    memory_bytes=512 * 1024 * 1024, output_bytes=64 * 1024,
                    sample_memory=Mock(side_effect=[0, MemoryMonitorUnavailable()]),
                )
            self.assertEqual(raised.exception.code, "parser_unavailable")
            self.assertEqual(child.returncode, -signal.SIGKILL)

    def test_memory_telemetry_can_disappear_just_before_confirmed_exit(self) -> None:
        # libproc can lose the resident task before waitpid observes its exit.
        # The child must confirm exit during the bounded grace interval.
        child = Mock(pid=1234)
        child.poll.return_value = None
        child.wait.return_value = 0
        child.communicate.return_value = (None, None)
        with tempfile.TemporaryFile() as output:
            _communicate_supervised(
                child, b"request", output, timeout_seconds=2,
                memory_bytes=512 * 1024 * 1024, output_bytes=64 * 1024,
                sample_memory=Mock(side_effect=MemoryMonitorUnavailable),
            )
        child.wait.assert_called_once()
        self.assertGreater(child.wait.call_args.kwargs["timeout"], 0)
        self.assertLessEqual(child.wait.call_args.kwargs["timeout"], 0.025)
        child.communicate.assert_called_once()

    def test_memory_telemetry_exit_grace_preserves_the_parse_deadline(self) -> None:
        child = Mock(pid=1234)
        child.poll.return_value = None
        child.wait.return_value = 0
        child.communicate.return_value = (None, None)
        with (
            tempfile.TemporaryFile() as output,
            patch("cvgnome_engine.source_ingest.parser.time.monotonic", side_effect=[100, 100.99, 100.995]),
        ):
            _communicate_supervised(
                child, b"request", output, timeout_seconds=1,
                memory_bytes=512 * 1024 * 1024, output_bytes=64 * 1024,
                sample_memory=Mock(side_effect=MemoryMonitorUnavailable),
            )
        child.wait.assert_called_once()
        self.assertAlmostEqual(child.wait.call_args.kwargs["timeout"], 0.01)
        self.assertAlmostEqual(child.communicate.call_args.kwargs["timeout"], 0.005)

    @unittest.skipUnless(sys.platform == "darwin", "requires macOS libproc")
    def test_native_watchdog_counts_and_stops_launcher_worker_group(self) -> None:
        # A launcher plus worker models a frozen one-file sidecar. No large
        # allocation is needed: set the internal probe threshold just above the
        # launcher's baseline, then start an ordinary small Python worker.
        sampler = memory_sampler()
        self.assertIsNotNone(sampler)
        launcher_code = """
from pathlib import Path
import subprocess, sys
ready, worker_ready = map(Path, sys.argv[1:])
ready.write_text('ready')
sys.stdin.buffer.readline()
worker = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
worker_ready.write_text(str(worker.pid))
sys.stdin.buffer.read()
worker.wait()
"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryFile() as output:
            ready = Path(directory) / "ready"
            worker_ready = Path(directory) / "worker"
            child = subprocess.Popen(
                [sys.executable, "-c", launcher_code, str(ready), str(worker_ready)],
                stdin=subprocess.PIPE, stdout=output, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists())
                baseline = sampler(child.pid)
                self.assertGreater(baseline, 0)
                child.stdin.write(b"start\n")
                child.stdin.flush()
                while not worker_ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(worker_ready.exists())
                worker_pid = int(worker_ready.read_text())
                self.assertEqual(os.getpgid(worker_pid), child.pid)
                while sampler(child.pid) <= baseline + 1024 * 1024 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertGreater(sampler(child.pid), baseline + 1024 * 1024)
                with self.assertRaises(SourceParserError) as raised:
                    _communicate_supervised(
                        child, b"", output, timeout_seconds=2,
                        memory_bytes=baseline + 1024 * 1024, output_bytes=64 * 1024,
                        sample_memory=sampler,
                    )
                self.assertEqual(raised.exception.code, "resource_limit")
                self.assertEqual(child.returncode, -signal.SIGKILL)
                # A killed worker can briefly be a zombie until reaped; libproc
                # no longer returns a resident task for either group member.
                with self.assertRaises(MemoryMonitorUnavailable):
                    sampler(child.pid)
            finally:
                _terminate_process_tree(child)

    def test_default_child_command_routes_frozen_sidecars_to_callable_mode(self) -> None:
        with patch.object(sys, "frozen", True, create=True):
            self.assertEqual(
                default_parser_child_command(),
                (sys.executable, "source-parser-child"),
            )
        with patch.object(sys, "frozen", False, create=True):
            self.assertEqual(
                default_parser_child_command(),
                (
                    sys.executable,
                    "-I",
                    "-m",
                    "cvgnome_engine.source_ingest.parser_child",
                ),
            )

    def test_docx_is_verified_and_extracted_in_child(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "resume.docx"
            _write_docx(path, ["Ada Lovelace", "Built an analytical engine."])
            size, sha256 = source_file_identity(path)

            source = parse_structured_source(
                path,
                expected_size=size,
                expected_sha256=sha256,
            )

        self.assertEqual(source["source_id"], f"src_{sha256[:24]}")
        self.assertEqual(source["kind"], "docx")
        self.assertEqual(source["sha256"], sha256)
        self.assertEqual(source["size_bytes"], size)
        self.assertEqual(
            source["text"], "Ada Lovelace\nBuilt an analytical engine."
        )
        self.assertEqual(source["extraction_status"], "extracted")
        self.assertEqual(source["warnings"], [])

    def test_pdf_is_extracted_with_the_same_plain_source_contract(self) -> None:
        from reportlab.pdfgen.canvas import Canvas

        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "resume.pdf"
            canvas = Canvas(str(path))
            canvas.drawString(72, 720, "Grace Hopper")
            canvas.drawString(72, 700, "Email: grace@example.test")
            canvas.save()

            source = parse_structured_source(path)

        self.assertEqual(source["kind"], "pdf")
        self.assertEqual(source["media_type"], "application/pdf")
        self.assertIn("Grace Hopper", source["text"])
        self.assertIn("grace@example.test", source["text"])
        self.assertEqual(
            set(source),
            {
                "source_id",
                "display_name",
                "media_type",
                "kind",
                "sha256",
                "size_bytes",
                "text",
                "text_sha256",
                "extraction_status",
                "warnings",
            },
        )

    def test_structured_output_truncation_is_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "long-resume.docx"
            _write_docx(path, ["Ada Lovelace", "A" * 200])

            source = parse_structured_source(
                path,
                limits=ParserLimits(max_output_chars=32),
            )

        self.assertEqual(len(source["text"]), 32)
        self.assertEqual(source["extraction_status"], "extracted")
        self.assertEqual(source["warnings"], ["partial_parse"])

    def test_manifest_mismatch_uses_only_a_safe_error(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "private-client-name.docx"
            _write_docx(path, ["Sensitive content that must not escape"])
            size, _sha256 = source_file_identity(path)

            with self.assertRaises(SourceParserError) as raised:
                parse_structured_source(
                    path,
                    expected_size=size,
                    expected_sha256="0" * 64,
                )

        self.assertEqual(raised.exception.code, "input_changed")
        self.assertEqual(
            raised.exception.as_dict(),
            {
                "code": "input_changed",
                "message": "The staged source no longer matches its recorded hash and size.",
            },
        )
        self.assertNotIn("Sensitive", str(raised.exception))
        self.assertNotIn("private-client-name", str(raised.exception))

    def test_child_rechecks_bytes_after_parent_preflight(self) -> None:
        child_code = """
import io
import json
from pathlib import Path
import sys
from cvgnome_engine.source_ingest import parser_child_main
payload = json.loads(sys.stdin.buffer.read())
Path(payload["path"]).write_bytes(b"changed after preflight")
raise SystemExit(parser_child_main(
    input_stream=io.BytesIO(json.dumps(payload, separators=(",", ":")).encode()),
    output_stream=sys.stdout.buffer,
))
"""
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "resume.docx"
            _write_docx(path, ["Original"])

            with self.assertRaises(SourceParserError) as raised:
                parse_structured_source(
                    path,
                    child_command=(sys.executable, "-c", child_code),
                )

        self.assertEqual(raised.exception.code, "input_changed")

    def test_timeout_kills_the_isolated_child_and_returns_a_safe_code(self) -> None:
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "resume.docx"
            _write_docx(path, ["Ada Lovelace"])

            with self.assertRaises(SourceParserError) as raised:
                parse_structured_source(
                    path,
                    timeout_seconds=0.05,
                    child_command=(
                        sys.executable,
                        "-c",
                        "import time; time.sleep(30)",
                    ),
                )

        self.assertEqual(raised.exception.code, "parser_timeout")

    def test_entity_bearing_docx_never_expands_external_or_internal_entities(self) -> None:
        entity_prefix = (
            '<?xml version="1.0"?>'
            '<!DOCTYPE foo [<!ENTITY secret "ENTITY_MUST_NOT_APPEAR">]>'
        )
        with tempfile.TemporaryDirectory() as raw_directory:
            path = Path(raw_directory) / "entity.docx"
            _write_docx(path, ["&secret;"], xml_prefix=entity_prefix)

            source = parse_structured_source(path)

        self.assertEqual(source["text"], "")
        self.assertEqual(source["extraction_status"], "needs_review")
        self.assertEqual(source["warnings"], ["no_text_extracted"])
        self.assertNotIn("ENTITY_MUST_NOT_APPEAR", json.dumps(source))

    def test_callable_child_main_rejects_malformed_requests_without_details(self) -> None:
        output = BytesIO()

        exit_code = parser_child_main(
            input_stream=BytesIO(b"not-json"),
            output_stream=output,
        )

        self.assertEqual(exit_code, 2)
        self.assertEqual(
            json.loads(output.getvalue()),
            {
                "version": 1,
                "ok": False,
                "error": {"code": "invalid_request"},
            },
        )


if __name__ == "__main__":
    unittest.main()
