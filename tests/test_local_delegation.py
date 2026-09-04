#!/usr/bin/env python3
"""Unit tests for the local-delegation Python runtime."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid
import socket
from datetime import datetime
from pathlib import Path
from unittest import mock

# Ensure scripts/ is on sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

from local_delegation.common import (
    assert_ld_loopback_uri,
    convert_from_ld_json_response,
    convert_to_ld_toml_string,
    get_ld_origin_and_responses_base,
    get_ld_state_root,
    get_ld_sha256_text,
    get_ld_codex_approval_mode,
    find_ld_codex_cli,
    http_get,
    http_post,
    initialize_ld_state_root,
    read_ld_repository_timeout_config,
    read_ld_provider_config,
    wait_ld_process_with_activity_timeout,
    write_ld_utf8_file,
    _kill_process_tree,
    acquire_ld_file_lock,
    release_ld_file_lock,
)
from run_local_developer import (
    is_path_beneath,
    resolve_timeout_settings,
    validate_handoff_constraints,
    validate_json_schema_subset,
)

def start_test_process(code):
    options = {}
    if sys.platform == "win32":
        options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        options["start_new_session"] = True
    return subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        **options,
    )


class TestStateRoot(unittest.TestCase):
    """Tests for state-root resolution and initialization."""

    def test_default_state_root(self):
        """Default state root resolves to user's cache directory."""
        result = get_ld_state_root()
        self.assertIn(".cache", result)
        self.assertIn("codex-local-delegation", result)

    def test_override_state_root(self):
        """Override parameter takes precedence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = get_ld_state_root(tmpdir)
            self.assertEqual(result, os.path.normpath(os.path.abspath(tmpdir)))

    def test_initialize_creates_subdirectories(self):
        """initialize_ld_state_root creates all required subdirs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            initialize_ld_state_root(tmpdir)
            for subdir in ("config", "logs", "codex-home", "run", "locks", "tmp"):
                self.assertTrue(os.path.isdir(os.path.join(tmpdir, subdir)))


class TestCodexCliDiscovery(unittest.TestCase):
    """Tests for deterministic Codex CLI discovery."""

    def test_override_takes_precedence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            executable = Path(tmpdir) / "codex.exe"
            executable.touch()
            self.assertTrue(os.path.samefile(find_ld_codex_cli(str(executable)), executable))

    def test_missing_override_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "LOCAL_DELEGATE_CODEX_BIN does not exist"):
            find_ld_codex_cli("C:/not-a-real-codex.exe")


class TestUrlHelpers(unittest.TestCase):
    """Tests for URL validation and derivation."""

    def test_ollama_origin(self):
        """Ollama base URL normalizes correctly."""
        result = get_ld_origin_and_responses_base("http://127.0.0.1:11434")
        self.assertEqual(result["origin"], "http://127.0.0.1:11434")
        self.assertEqual(result["responses_base_url"], "http://127.0.0.1:11434/v1")

    def test_ollama_with_v1_suffix(self):
        """Ollama with /v1 suffix normalizes correctly."""
        result = get_ld_origin_and_responses_base("http://127.0.0.1:11434/v1")
        self.assertEqual(result["origin"], "http://127.0.0.1:11434")
        self.assertEqual(result["responses_base_url"], "http://127.0.0.1:11434/v1")

    def test_localhost_normalized(self):
        """localhost:8080 with /v1/ normalizes once."""
        result = get_ld_origin_and_responses_base("http://localhost:8080/v1/")
        self.assertEqual(result["origin"], "http://localhost:8080")
        self.assertEqual(result["responses_base_url"], "http://localhost:8080/v1")

    def test_lan_rejected(self):
        """LAN provider URLs are rejected."""
        with self.assertRaises(ValueError):
            assert_ld_loopback_uri("http://192.168.1.20:8080")

    def test_https_rejected(self):
        """HTTPS is rejected by the v1 loopback contract."""
        with self.assertRaises(ValueError):
            assert_ld_loopback_uri("https://127.0.0.1:8080")

    def test_unexpected_path_rejected(self):
        """Unexpected provider URL paths are rejected."""
        with self.assertRaises(ValueError):
            get_ld_origin_and_responses_base("http://127.0.0.1:8080/custom")

    def test_loopback_localhost(self):
        """localhost is accepted as a loopback address."""
        result = assert_ld_loopback_uri("http://localhost:8080")
        self.assertEqual(result.hostname, "localhost")

    def test_loopback_v6(self):
        """::1 is accepted as a loopback address."""
        result = assert_ld_loopback_uri("http://[::1]:8080")
        self.assertEqual(result.hostname, "::1")

    def test_query_rejected(self):
        """Query strings in provider URLs are rejected."""
        with self.assertRaises(ValueError):
            assert_ld_loopback_uri("http://127.0.0.1:8080?foo=bar")

    def test_fragment_rejected(self):
        """Fragments in provider URLs are rejected."""
        with self.assertRaises(ValueError):
            assert_ld_loopback_uri("http://127.0.0.1:8080#section")


class TestTomlHelpers(unittest.TestCase):
    """Tests for TOML string escaping."""

    def test_escape_slash_and_quote(self):
        """TOML strings escape backslash and quote characters."""
        self.assertEqual(convert_to_ld_toml_string('a\\b"c'), '"a\\\\b\\"c"')

    def test_simple_string(self):
        """Simple strings are quoted without modification."""
        self.assertEqual(convert_to_ld_toml_string("hello"), '"hello"')


class TestSha256(unittest.TestCase):
    """Tests for SHA-256 text hashing."""

    def test_deterministic(self):
        """SHA-256 text hash is deterministic."""
        a = get_ld_sha256_text("hello")
        b = get_ld_sha256_text("hello")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)

    def test_unique(self):
        """Different inputs produce different hashes."""
        a = get_ld_sha256_text("hello")
        b = get_ld_sha256_text("world")
        self.assertNotEqual(a, b)

    def test_lowercase(self):
        """SHA-256 hash is lowercase hex."""
        h = get_ld_sha256_text("test")
        self.assertEqual(h, h.lower())
        self.assertTrue(all(c in "0123456789abcdef" for c in h))


class TestFileIO(unittest.TestCase):
    """Tests for file I/O utilities."""

    def test_write_utf8_file(self):
        """write_ld_utf8_file creates and writes a file atomically."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "subdir", "test.txt")
            write_ld_utf8_file(fpath, "hello\nworld")
            self.assertTrue(os.path.isfile(fpath))
            with open(fpath, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "hello\nworld")

    def test_write_utf8_file_no_trailing_tmp(self):
        """No .tmp file remains after write."""
        with tempfile.TemporaryDirectory() as tmpdir:
            fpath = os.path.join(tmpdir, "test.txt")
            write_ld_utf8_file(fpath, "data")
            tmps = [f for f in os.listdir(tmpdir) if f.endswith(".tmp")]
            self.assertEqual(len(tmps), 0)

    def test_file_lock_releases_without_deleting_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            lock_path = os.path.join(tmpdir, "repository.lock")
            first = acquire_ld_file_lock(lock_path)
            with self.assertRaises(RuntimeError):
                acquire_ld_file_lock(lock_path)
            release_ld_file_lock(first)
            self.assertTrue(os.path.isfile(lock_path))
            second = acquire_ld_file_lock(lock_path)
            release_ld_file_lock(second)


class TestRepositoryTimeoutConfig(unittest.TestCase):
    """Tests for .codex/local-delegate.toml parsing."""

    def test_missing_file_returns_defaults(self):
        """Missing config returns None for both timeouts."""
        with tempfile.TemporaryDirectory() as tmpdir:
            result = read_ld_repository_timeout_config(tmpdir)
            self.assertIsNone(result["timeout_minutes"])
            self.assertIsNone(result["inactivity_timeout_minutes"])

    def test_parses_valid_config(self):
        """Valid config values are parsed correctly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("timeout_minutes = 90\n")
                f.write("inactivity_timeout_minutes = 12\n")
            result = read_ld_repository_timeout_config(tmpdir)
            self.assertEqual(result["timeout_minutes"], 90)
            self.assertEqual(result["inactivity_timeout_minutes"], 12)

    def test_rejects_excessive_inactivity(self):
        """Excessive inactivity_timeout_minutes is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("inactivity_timeout_minutes = 1441\n")
            with self.assertRaises(RuntimeError):
                read_ld_repository_timeout_config(tmpdir)

    def test_rejects_excessive_timeout(self):
        """Excessive timeout_minutes is rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("timeout_minutes = 1441\n")
            with self.assertRaises(RuntimeError):
                read_ld_repository_timeout_config(tmpdir)

    def test_rejects_zero_timeout(self):
        """timeout_minutes = 0 is rejected (min is 1)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("timeout_minutes = 0\n")
            with self.assertRaises(RuntimeError):
                read_ld_repository_timeout_config(tmpdir)

    def test_rejects_duplicate_key(self):
        """Duplicate timeout keys are rejected."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("timeout_minutes = 60\n")
                f.write("timeout_minutes = 90\n")
            with self.assertRaises(RuntimeError):
                read_ld_repository_timeout_config(tmpdir)

    def test_zero_inactivity_is_valid(self):
        """inactivity_timeout_minutes = 0 is valid (disables inactivity)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_dir = os.path.join(tmpdir, ".codex")
            os.makedirs(codex_dir)
            toml_path = os.path.join(codex_dir, "local-delegate.toml")
            with open(toml_path, "w") as f:
                f.write("inactivity_timeout_minutes = 0\n")
            result = read_ld_repository_timeout_config(tmpdir)
            self.assertEqual(result["inactivity_timeout_minutes"], 0)


class TestProcessTimeout(unittest.TestCase):
    """Tests for process timeout and activity detection."""

    def test_inactivity_timeout(self):
        """Silent process reaches inactivity timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = start_test_process("import time; time.sleep(4)")
            try:
                result = wait_ld_process_with_activity_timeout(
                    proc,
                    os.path.join(tmpdir, "events.jsonl"),
                    os.path.join(tmpdir, "stderr.log"),
                    hard_timeout=15.0,
                    inactivity_timeout=2.0,
                    poll_milliseconds=50,
                )
                self.assertFalse(result["completed"])
                self.assertEqual(result["termination_reason"], "inactivity-timeout")
            finally:
                if proc.poll() is None:
                    _kill_process_tree(proc.pid)
                    proc.wait()

    def test_active_process_completes(self):
        """Periodic output keeps process active."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = start_test_process(
                "import sys,time\n"
                "for i in range(5):\n"
                " print(i, flush=True)\n"
                " print(f'err-{i}', file=sys.stderr, flush=True)\n"
                " time.sleep(0.2)\n"
            )
            events_path = os.path.join(tmpdir, "events.jsonl")
            stderr_path = os.path.join(tmpdir, "stderr.log")
            activity_path = os.path.join(tmpdir, "activity.jsonl")
            try:
                result = wait_ld_process_with_activity_timeout(
                    proc,
                    events_path,
                    stderr_path,
                    hard_timeout=15.0,
                    inactivity_timeout=2.0,
                    poll_milliseconds=50,
                    activity_path=activity_path,
                )
                self.assertTrue(result["completed"])
                self.assertEqual(result["exit_code"], 0)
                self.assertIn("0", Path(events_path).read_text(encoding="utf-8"))
                self.assertIn("err-4", Path(stderr_path).read_text(encoding="utf-8"))
                datetime.fromisoformat(result["last_activity_at"].replace("Z", "+00:00"))
                activity = [
                    json.loads(line)
                    for line in Path(activity_path).read_text(encoding="utf-8").splitlines()
                ]
                self.assertEqual(activity[0]["event"], "process.started")
                self.assertEqual(activity[-1]["event"], "process.finished")
                self.assertEqual(
                    {item["stream"] for item in activity if item["event"] == "stream.activity"},
                    {"stdout", "stderr"},
                )
                for item in activity:
                    datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            finally:
                if proc.poll() is None:
                    _kill_process_tree(proc.pid)
                    proc.wait()

    def test_zero_inactivity_disabled(self):
        """Zero disables the inactivity timeout."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = start_test_process("import time; time.sleep(0.5)")
            try:
                result = wait_ld_process_with_activity_timeout(
                    proc,
                    os.path.join(tmpdir, "events.jsonl"),
                    os.path.join(tmpdir, "stderr.log"),
                    hard_timeout=10.0,
                    inactivity_timeout=0,
                    poll_milliseconds=25,
                )
                self.assertTrue(result["completed"])
                self.assertEqual(result["exit_code"], 0)
            finally:
                if proc.poll() is None:
                    _kill_process_tree(proc.pid)
                    proc.wait()

    def test_hard_timeout(self):
        """Process exceeds hard timeout and is terminated."""
        with tempfile.TemporaryDirectory() as tmpdir:
            proc = start_test_process("import time; time.sleep(30)")
            try:
                result = wait_ld_process_with_activity_timeout(
                    proc,
                    os.path.join(tmpdir, "events.jsonl"),
                    os.path.join(tmpdir, "stderr.log"),
                    hard_timeout=2.0,
                    inactivity_timeout=0,
                    poll_milliseconds=50,
                )
                self.assertFalse(result["completed"])
                self.assertEqual(result["termination_reason"], "hard-timeout")
            finally:
                if proc.poll() is None:
                    _kill_process_tree(proc.pid)
                    proc.wait()


class TestRunnerValidation(unittest.TestCase):
    def test_current_codex_approval_mode_is_preferred(self):
        result = get_ld_codex_approval_mode(
            "--ask-for-approval VALUE\n--approve-for-me"
        )
        self.assertEqual(result["mode"], "approve-for-me")
        self.assertEqual(result["arguments"], ["--approve-for-me"])
        self.assertEqual(result["sandbox_arguments"], [])

    def test_legacy_codex_approval_mode_is_supported(self):
        result = get_ld_codex_approval_mode("--ask-for-approval VALUE")
        self.assertEqual(result["mode"], "ask-for-approval-never")
        self.assertEqual(result["arguments"], ["--ask-for-approval", "never"])
        self.assertEqual(result["sandbox_arguments"], ["--sandbox", "workspace-write"])

    def test_path_containment_rejects_root_and_sibling(self):
        """Containment accepts descendants only, not the root or prefix-matching siblings."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repository = Path(tmpdir, "repository")
            descendant = repository / "handoff.md"
            sibling = Path(tmpdir, "repository-sibling", "handoff.md")
            descendant.parent.mkdir(parents=True)
            sibling.parent.mkdir(parents=True)
            descendant.write_text("test", encoding="utf-8")
            sibling.write_text("test", encoding="utf-8")

            self.assertTrue(is_path_beneath(str(repository), str(descendant)))
            self.assertFalse(is_path_beneath(str(repository), str(repository)))
            self.assertFalse(is_path_beneath(str(repository), str(sibling)))

    @unittest.skipUnless(sys.platform == "win32", "NTFS short paths are Windows-specific")
    def test_path_containment_accepts_ntfs_short_alias(self):
        """An 8.3 alias and its long path identify the same repository tree."""
        import ctypes

        with tempfile.TemporaryDirectory() as tmpdir:
            repository = Path(tmpdir, "repository-long-name")
            handoff = repository / ".codex" / "local-handoffs" / "runner-test" / "request.md"
            handoff.parent.mkdir(parents=True)
            handoff.write_text("test", encoding="utf-8")

            buffer = ctypes.create_unicode_buffer(32768)
            length = ctypes.windll.kernel32.GetShortPathNameW(str(handoff), buffer, len(buffer))
            if length == 0 or os.path.normcase(buffer.value) == os.path.normcase(str(handoff)):
                self.skipTest("NTFS 8.3 aliases are unavailable on this volume")

            self.assertTrue(is_path_beneath(str(repository), buffer.value))

    def test_timeout_precedence(self):
        self.assertEqual(resolve_timeout_settings(None, None, {}), (60, 15))
        self.assertEqual(
            resolve_timeout_settings(
                None,
                None,
                {"timeout_minutes": 90, "inactivity_timeout_minutes": 0},
            ),
            (90, 0),
        )
        self.assertEqual(
            resolve_timeout_settings(
                12,
                3,
                {"timeout_minutes": 90, "inactivity_timeout_minutes": 0},
            ),
            (12, 3),
        )

    def test_handoff_requires_false_constraints(self):
        valid = """\
- Network access: false
- Dependency installation: false
- Database or state migrations: false
- Commits or Git ref changes: false
- External actions: false
"""
        validate_handoff_constraints(valid)
        with self.assertRaises(ValueError):
            validate_handoff_constraints(valid.replace("Network access: false", "Network access: true"))
        with self.assertRaises(ValueError):
            validate_handoff_constraints(valid.replace("- External actions: false\n", ""))

    def test_result_schema_subset(self):
        schema_path = Path(__file__).parents[1] / "schemas" / "developer-result.schema.json"
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        valid = {
            "status": "completed",
            "summary": "done",
            "changed_files": ["file.txt"],
            "commands": [{"command": "test", "outcome": "passed"}],
            "known_limitations": [],
            "follow_up_needs": [],
        }
        validate_json_schema_subset(valid, schema)
        with self.assertRaises(ValueError):
            validate_json_schema_subset({**valid, "status": "unknown"}, schema)
        with self.assertRaises(ValueError):
            validate_json_schema_subset({**valid, "extra": True}, schema)
        with self.assertRaises(ValueError):
            validate_json_schema_subset({**valid, "commands": [{"command": "test"}]}, schema)


class TestJsonResponse(unittest.TestCase):
    """Tests for JSON response parsing."""

    def test_success_parses_json(self):
        """Successful response parses JSON."""
        resp = {
            "is_success": True,
            "status_code": 200,
            "content": '{"key": "value"}',
        }
        result = convert_from_ld_json_response(resp)
        self.assertEqual(result["key"], "value")

    def test_failure_raises(self):
        """Failed response raises with HTTP code."""
        resp = {
            "is_success": False,
            "status_code": 500,
            "content": "Internal Server Error - very long message " * 50,
        }
        with self.assertRaises(RuntimeError):
            convert_from_ld_json_response(resp)

    def test_invalid_json_raises(self):
        """Invalid JSON in response raises."""
        resp = {
            "is_success": True,
            "status_code": 200,
            "content": "not valid json {{{",
        }
        with self.assertRaises(RuntimeError):
            convert_from_ld_json_response(resp)


class TestScriptSyntax(unittest.TestCase):
    """Tests for script syntax and schema validity."""

    def test_configure_provider_syntax(self):
        """configure_provider.py parses without syntax errors."""
        import py_compile
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        py_compile.compile(
            os.path.join(scripts_dir, "configure_provider.py"),
            doraise=True,
        )

    def test_doctor_syntax(self):
        """doctor.py parses without syntax errors."""
        import py_compile
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        py_compile.compile(
            os.path.join(scripts_dir, "doctor.py"),
            doraise=True,
        )

    def test_run_local_developer_syntax(self):
        """run_local_developer.py parses without syntax errors."""
        import py_compile
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        py_compile.compile(
            os.path.join(scripts_dir, "run_local_developer.py"),
            doraise=True,
        )

    def test_common_syntax(self):
        """common.py parses without syntax errors."""
        import py_compile
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        py_compile.compile(
            os.path.join(scripts_dir, "local_delegation", "common.py"),
            doraise=True,
        )

    def test_schema_validates_sample(self):
        """Developer result schema accepts a valid result."""
        schemas_dir = os.path.join(os.path.dirname(__file__), "..", "schemas")
        schema_path = os.path.join(schemas_dir, "developer-result.schema.json")
        self.assertTrue(os.path.isfile(schema_path))
        with open(schema_path, "r") as f:
            schema = json.load(f)
        sample = {
            "status": "completed",
            "summary": "Implemented the bounded task.",
            "changed_files": ["src/example.txt"],
            "commands": [{"command": "test", "outcome": "passed"}],
            "known_limitations": [],
            "follow_up_needs": [],
        }
        for key in schema.get("required", []):
            self.assertIn(key, sample)
        self.assertEqual(sample["status"], "completed")
        self.assertIsInstance(sample["changed_files"], list)
        self.assertIsInstance(sample["commands"], list)


class TestNoPowerShellRemnants(unittest.TestCase):
    """Tests that old PowerShell files have been removed."""

    def test_no_ps1_in_scripts(self):
        """No .ps1 files remain under scripts/."""
        scripts_dir = os.path.join(os.path.dirname(__file__), "..", "scripts")
        for root, dirs, files in os.walk(scripts_dir):
            for f in files:
                if f.endswith(".ps1"):
                    self.fail(f"PowerShell file still present: {os.path.join(root, f)}")

    def test_no_ps1_in_tests(self):
        """No .ps1 files remain under tests/."""
        tests_dir = os.path.join(os.path.dirname(__file__))
        for f in os.listdir(tests_dir):
            if f.endswith(".ps1"):
                self.fail(f"PowerShell file still present: {os.path.join(tests_dir, f)}")


class TestDocReferences(unittest.TestCase):
    """Tests that documentation references Python entry points."""

    def test_skill_uses_python(self):
        """Skill references Python entry points."""
        skill_path = os.path.join(os.path.dirname(__file__), "..", "skills", "local-delegate", "SKILL.md")
        with open(skill_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("configure_provider.py", content)
        self.assertIn("doctor.py", content)
        self.assertIn("run_local_developer.py", content)
        self.assertNotIn("setup_repository.py", content)
        self.assertIn("never require or create a repository `AGENTS.md` opt-in", content)

    def test_readme_uses_python(self):
        """README references Python entry points."""
        readme_path = os.path.join(os.path.dirname(__file__), "..", "README.md")
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("configure_provider.py", content)
        self.assertIn("doctor.py", content)
        self.assertNotIn("setup_repository.py", content)
        self.assertIn("without modifying", content)

    def test_contributing_uses_python(self):
        """CONTRIBUTING references Python test command."""
        contrib_path = os.path.join(os.path.dirname(__file__), "..", "CONTRIBUTING.md")
        with open(contrib_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("python -m unittest", content)


class TestCiWorkflow(unittest.TestCase):
    """Tests for CI workflow configuration."""

    def test_ci_uses_python(self):
        """CI workflow runs Python tests on both platforms."""
        ci_path = os.path.join(
            os.path.dirname(__file__),
            "..",
            ".github",
            "workflows",
            "ci.yml",
        )
        with open(ci_path, "r") as f:
            content = f.read()
        self.assertIn("python -m unittest", content)
        self.assertIn("ubuntu-latest", content)
        self.assertIn("windows-latest", content)


class TestMockProviderIntegration(unittest.TestCase):
    """Integration tests using a local mock Responses provider."""

    @classmethod
    def setUpClass(cls):
        """Start a mock provider for all integration tests."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        cls.mock_port = sock.getsockname()[1]
        sock.close()

        cls.mock_ready_file = os.path.join(
            tempfile.gettempdir(), f"mock-ready-{uuid.uuid4().hex}"
        )
        cls.integration_root = os.path.join(
            tempfile.gettempdir(), f"ld-test-{uuid.uuid4().hex}"
        )
        os.makedirs(cls.integration_root, exist_ok=True)

        cls.mock_bin = os.path.join(cls.integration_root, "mock-bin")
        os.makedirs(cls.mock_bin, exist_ok=True)
        cls.fake_codex_script = os.path.join(cls.mock_bin, "fake_codex.py")
        Path(cls.fake_codex_script).write_text(
            """\
import json
import os
import pathlib
import sys

if "--version" in sys.argv:
    print("codex-cli 0.test")
    raise SystemExit(0)

if "--help" in sys.argv:
    print("--profile --strict-config --sandbox --approve-for-me --ephemeral --json --output-schema --output-last-message --cd")
    raise SystemExit(0)

prompt = sys.stdin.read()
pathlib.Path(os.environ["FAKE_CODEX_PROMPT_PATH"]).write_text(prompt, encoding="utf-8")
pathlib.Path(os.environ["FAKE_CODEX_ARGS_PATH"]).write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
pathlib.Path(os.environ["FAKE_CODEX_EDIT_PATH"]).write_text("changed by fake codex\\n", encoding="utf-8")
output_path = pathlib.Path(sys.argv[sys.argv.index("--output-last-message") + 1])
result = {
    "status": "completed",
    "summary": "Implemented the bounded test change.",
    "changed_files": ["work.txt"],
    "commands": [{"command": "test", "outcome": "passed"}],
    "known_limitations": [],
    "follow_up_needs": [],
}
output_path.write_text(json.dumps(result), encoding="utf-8")
print(json.dumps({"type": "turn.completed"}), flush=True)
print("fake codex stderr", file=sys.stderr, flush=True)
""",
            encoding="utf-8",
        )
        if sys.platform == "win32":
            cls.codex_path = os.path.join(cls.mock_bin, "codex.cmd")
            Path(cls.codex_path).write_text(
                f'@echo off\r\n"{sys.executable}" "{cls.fake_codex_script}" %*\r\n',
                encoding="utf-8",
            )
        else:
            cls.codex_path = os.path.join(cls.mock_bin, "codex")
            Path(cls.codex_path).write_text(
                f'#!/bin/sh\nexec "{sys.executable}" "{cls.fake_codex_script}" "$@"\n',
                encoding="utf-8",
            )
            os.chmod(cls.codex_path, 0o755)

        mock_script = os.path.join(os.path.dirname(__file__), "mock_provider.py")
        cls.mock_proc = subprocess.Popen(
            [sys.executable, mock_script,
             "--port", str(cls.mock_port),
             "--ready-file", cls.mock_ready_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.time() + 10
        while not os.path.exists(cls.mock_ready_file) and time.time() < deadline:
            time.sleep(0.1)
        if not os.path.exists(cls.mock_ready_file):
            raise RuntimeError("Mock provider did not become ready.")

    @classmethod
    def tearDownClass(cls):
        """Clean up mock provider and temp directory."""
        if hasattr(cls, "mock_proc"):
            cls.mock_proc.kill()
            cls.mock_proc.wait()
            try:
                os.remove(cls.mock_ready_file)
            except OSError:
                pass
            if hasattr(cls, "integration_root") and os.path.exists(cls.integration_root):
                shutil.rmtree(cls.integration_root, ignore_errors=True)

    def test_configure_succeeds(self):
        """Explicit custom provider configuration succeeds."""
        from configure_provider import main as configure_main
        exit_code = configure_main([
            "--provider", "Custom",
            "--base-url", f"http://127.0.0.1:{self.mock_port}",
            "--model", "local-test-model",
            "--state-root", self.integration_root,
        ])
        self.assertEqual(exit_code, 0)

    def test_profile_generated(self):
        """Worker profile disables native web search."""
        profile_path = os.path.join(
            self.integration_root, "codex-home", "local-developer.config.toml"
        )
        self.assertTrue(os.path.isfile(profile_path))
        with open(profile_path, "r") as f:
            content = f.read()
        self.assertIn('web_search = "disabled"', content)
        self.assertIn("model_auto_compact_token_limit = 24576", content)
        self.assertIn('model_auto_compact_token_limit_scope = "body_after_prefix"', content)

    def test_model_catalog_generated(self):
        """Model catalog selects shell_command and records model."""
        catalog_path = os.path.join(
            self.integration_root, "codex-home", "model-catalog.json"
        )
        self.assertTrue(os.path.isfile(catalog_path))
        with open(catalog_path, "r") as f:
            catalog = json.load(f)
        self.assertEqual(catalog["models"][0]["slug"], "local-test-model")
        self.assertEqual(catalog["models"][0]["shell_type"], "shell_command")
        self.assertEqual(catalog["models"][0]["context_window"], 32768)
        self.assertIn("Never use cat, bash, sh, heredocs", catalog["models"][0]["base_instructions"])

    def test_provider_config_generated(self):
        """Provider config records schema version and model."""
        config_path = os.path.join(self.integration_root, "config", "provider.json")
        self.assertTrue(os.path.isfile(config_path))
        with open(config_path, "r") as f:
            config = json.load(f)
        self.assertEqual(config["schemaVersion"], 1)
        self.assertEqual(config["provider"], "Custom")
        self.assertEqual(config["model"], "local-test-model")
        self.assertEqual(config["autoCompactTokenLimit"], 24576)

    def test_doctor_succeeds(self):
        """The actual doctor entry point probes and records success."""
        from doctor import main as doctor_main

        with mock.patch.dict(
            os.environ,
            {"PATH": self.mock_bin + os.pathsep + os.environ.get("PATH", "")},
        ):
            exit_code = doctor_main([
                "--state-root", self.integration_root,
                "--timeout-seconds", "10",
            ])
        self.assertEqual(exit_code, 0)
        config = read_ld_provider_config(self.integration_root)
        self.assertEqual(config["lastDoctor"]["status"], "passed")
        self.assertEqual(config["lastDoctor"]["codexVersion"], "codex-cli 0.test")
        self.assertEqual(config["lastDoctor"]["approvalMode"], "approve-for-me")
        self.assertEqual(config["lastDoctor"]["compatibilityContractVersion"], 2)

    def test_runner_end_to_end(self):
        """The runner sends its prompt and preserves run artifacts."""
        from run_local_developer import main as runner_main

        repository = os.path.join(self.integration_root, "repository")
        os.makedirs(repository, exist_ok=True)
        subprocess.run(["git", "init", "-q", repository], check=True)
        subprocess.run(["git", "-C", repository, "config", "user.email", "test@example.invalid"], check=True)
        subprocess.run(["git", "-C", repository, "config", "user.name", "Test"], check=True)
        Path(repository, "work.txt").write_text("baseline\n", encoding="utf-8")
        Path(repository, "AGENTS.md").write_text("Local delegation test.\n", encoding="utf-8")
        subprocess.run(["git", "-C", repository, "add", "work.txt", "AGENTS.md"], check=True)
        subprocess.run(["git", "-C", repository, "commit", "-q", "-m", "baseline"], check=True)

        handoff_dir = Path(repository, ".codex", "local-handoffs", "runner-test")
        handoff_dir.mkdir(parents=True)
        handoff = handoff_dir / "request.md"
        handoff.write_text(
            """\
# Test handoff

## Constraints

- Network access: false
- Dependency installation: false
- Database or state migrations: false
- Commits or Git ref changes: false
- External actions: false
""",
            encoding="utf-8",
        )
        prompt_path = os.path.join(self.integration_root, "received-prompt.txt")
        args_path = os.path.join(self.integration_root, "received-args.json")
        environment = {
            "FAKE_CODEX_PROMPT_PATH": prompt_path,
            "FAKE_CODEX_ARGS_PATH": args_path,
            "FAKE_CODEX_EDIT_PATH": os.path.join(repository, "work.txt"),
        }
        with mock.patch.dict(os.environ, environment):
            exit_code = runner_main([
                "--repository", repository,
                "--handoff-path", str(handoff),
                "--allowed-path", "work.txt",
                "--protected-path", ".git/",
                "--state-root", self.integration_root,
                "--codex-bin", self.codex_path,
                "--timeout-minutes", "1",
                "--inactivity-timeout-minutes", "1",
            ])
        self.assertEqual(exit_code, 0)
        received_prompt = Path(prompt_path).read_text(encoding="utf-8")
        self.assertIn("You are the delegated local developer", received_prompt)
        self.assertIn("Never use cat, bash, sh, heredocs", received_prompt)
        received_args = json.loads(Path(args_path).read_text(encoding="utf-8"))
        self.assertIn("--approve-for-me", received_args)
        self.assertNotIn("--ask-for-approval", received_args)
        self.assertNotIn("--sandbox", received_args)
        self.assertTrue(received_args[received_args.index("--output-schema") + 1].endswith("developer-result.schema.json"))
        runner_record = json.loads((handoff_dir / "runner.json").read_text(encoding="utf-8"))
        self.assertEqual(runner_record["status"], "completed")
        run_dir = Path(runner_record["runDirectory"])
        self.assertIn("turn.completed", (run_dir / "events.jsonl").read_text(encoding="utf-8"))
        self.assertIn("fake codex stderr", (run_dir / "stderr.log").read_text(encoding="utf-8"))
        activity = [
            json.loads(line)
            for line in (run_dir / "activity.jsonl").read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(activity[-1]["event"], "process.finished")
        self.assertEqual(runner_record["codexVersion"], "codex-cli 0.test")

    def test_runner_rejects_stale_codex_doctor_result(self):
        """A Codex CLI change requires doctor to be run again."""
        from run_local_developer import main as runner_main

        config_path = os.path.join(self.integration_root, "config", "provider.json")
        config = read_ld_provider_config(self.integration_root)
        original_version = config["lastDoctor"]["codexVersion"]
        config["lastDoctor"]["codexVersion"] = "codex-cli stale"
        Path(config_path).write_text(json.dumps(config), encoding="utf-8")
        try:
            exit_code = runner_main([
                "--repository", self.integration_root,
                "--handoff-path", os.path.join(self.integration_root, "missing.md"),
                "--allowed-path", "work.txt",
                "--state-root", self.integration_root,
                "--codex-bin", self.codex_path,
            ])
            self.assertEqual(exit_code, 20)
        finally:
            config["lastDoctor"]["codexVersion"] = original_version
            Path(config_path).write_text(json.dumps(config), encoding="utf-8")

    def test_multiple_providers_detected(self):
        """If multiple providers respond, configuration requires explicit choice."""
        import socket as _socket
        sock2 = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        sock2.bind(("127.0.0.1", 0))
        port2 = sock2.getsockname()[1]
        sock2.close()

        ready2 = os.path.join(tempfile.gettempdir(), f"mock-ready-2-{uuid.uuid4().hex}")
        mock_script = os.path.join(os.path.dirname(__file__), "mock_provider.py")
        proc2 = subprocess.Popen(
            [sys.executable, mock_script,
             "--port", str(port2),
             "--ready-file", ready2],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.time() + 10
            while not os.path.exists(ready2) and time.time() < deadline:
                time.sleep(0.1)

            from local_delegation.common import get_ld_provider_probe
            probe1 = get_ld_provider_probe(
                "Custom", f"http://127.0.0.1:{self.mock_port}", 5
            )
            probe2 = get_ld_provider_probe(
                "Custom", f"http://127.0.0.1:{port2}", 5
            )
            self.assertIn("local-test-model", probe1["models"])
            self.assertIn("local-test-model", probe2["models"])
        finally:
            proc2.kill()
            proc2.wait()
            try:
                os.remove(ready2)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main()
