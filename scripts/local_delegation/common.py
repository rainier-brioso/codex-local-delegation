#!/usr/bin/env python3
"""Shared utilities for the local-delegation Python runtime."""

from __future__ import annotations

import json
import os
import re
import signal
import sys
import hashlib
import shutil
import subprocess
import threading
import time
import uuid
import urllib.parse
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

CODEX_COMPATIBILITY_CONTRACT_VERSION = 2

# ---------------------------------------------------------------------------
# State root
# ---------------------------------------------------------------------------

def get_ld_state_root(override: Optional[str] = None) -> str:
    """Return the resolved state-root directory."""
    if override:
        candidate = override
    else:
        candidate = os.environ.get("LOCAL_DELEGATE_HOME")
    if not candidate:
        candidate = os.path.join(
            os.path.expanduser("~"), ".cache", "codex-local-delegation"
        )
    return os.path.normpath(os.path.abspath(candidate))


def initialize_ld_state_root(state_root: str) -> None:
    """Create the standard sub-directories under *state_root*."""
    for subdir in ("config", "logs", "codex-home", "run", "locks", "tmp"):
        os.makedirs(os.path.join(state_root, subdir), exist_ok=True)


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def assert_ld_loopback_uri(url: str) -> urllib.parse.ParseResult:
    """Validate and return a loopback HTTP URL."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "http":
        raise ValueError(f"Version 1 accepts only http:// loopback URLs, got {parsed.scheme}.")
    if parsed.netloc == "":
        raise ValueError(f"Invalid absolute provider URL: {url}")
    host = parsed.hostname or ""
    allowed_hosts = {"127.0.0.1", "localhost", "::1"}
    if host not in allowed_hosts:
        raise ValueError(f"Version 1 rejects non-loopback host '{host}'.")
    if parsed.query or parsed.fragment:
        raise ValueError("Provider URLs must not contain query string or fragment.")
    return parsed


def get_ld_origin_and_responses_base(base_url: str) -> Dict[str, str]:
    """Derive origin and /v1 base URL from *base_url*."""
    parsed = assert_ld_loopback_uri(base_url)
    trimmed = parsed._replace(query="", fragment="").geturl().rstrip("/")
    path = parsed.path.rstrip("/")
    # Accept empty path, "/", or "/v1"
    if path in ("", "/", "/v1"):
        if path in ("/v1", "/v1/"):
            origin = trimmed[:-3] if trimmed.endswith("/v1") else trimmed
        else:
            origin = trimmed
    else:
        raise ValueError(f"Provider URL paths may be empty or /v1 only, got '{path}'.")
    origin = origin.rstrip("/")
    return {
        "origin": origin,
        "responses_base_url": origin + "/v1",
    }


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------

def _http_request(
    method: str,
    url: str,
    body: Any = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """Send an HTTP request and return structured result dict."""
    assert_ld_loopback_uri(url)
    import urllib.request

    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            content = resp.read().decode("utf-8")
            return {
                "is_success": 200 <= resp.status < 300,
                "status_code": resp.status,
                "content_type": resp.headers.get("Content-Type", ""),
                "content": content,
            }
    except urllib.error.HTTPError as exc:
        with exc:
            content = exc.read().decode("utf-8", errors="replace")
            return {
                "is_success": False,
                "status_code": exc.code,
                "content_type": exc.headers.get("Content-Type", ""),
                "content": content,
            }


def http_get(url: str, timeout: int = 10) -> Dict[str, Any]:
    return _http_request("GET", url, timeout=timeout)


def http_post(url: str, body: Any, timeout: int = 10) -> Dict[str, Any]:
    return _http_request("POST", url, body=body, timeout=timeout)


def convert_from_ld_json_response(response: Dict[str, Any]) -> Any:
    """Parse JSON from an HTTP response dict, raising on errors."""
    if not response["is_success"]:
        excerpt = response["content"][:300]
        raise RuntimeError(f"HTTP {response['status_code']}: {excerpt}")
    try:
        return json.loads(response["content"])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Provider returned invalid JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# Provider probe
# ---------------------------------------------------------------------------

def get_ld_provider_probe(
    provider: str,
    base_url: str,
    timeout_seconds: int = 5,
) -> Dict[str, Any]:
    """Probe a local provider and return capability info."""
    urls = get_ld_origin_and_responses_base(base_url)
    models: List[str] = []
    identity = ""

    if provider == "Ollama":
        version_resp = convert_from_ld_json_response(
            http_get(f"{urls['origin']}/api/version", timeout=timeout_seconds)
        )
        version = version_resp.get("version", "")
        if not version:
            raise RuntimeError("Ollama identity response did not contain a version.")
        tags_resp = convert_from_ld_json_response(
            http_get(f"{urls['origin']}/api/tags", timeout=timeout_seconds)
        )
        models = sorted({
            m.get("name", "") for m in tags_resp.get("models", []) if m.get("name")
        })
        identity = f"Ollama {version}"
    elif provider == "LlamaCpp":
        health = http_get(f"{urls['origin']}/health", timeout=timeout_seconds)
        if not health["is_success"]:
            raise RuntimeError(
                f"llama.cpp health check returned HTTP {health['status_code']}."
            )
        identity = "llama.cpp-compatible server"
        listing = convert_from_ld_json_response(
            http_get(f"{urls['responses_base_url']}/models", timeout=timeout_seconds)
        )
        models = sorted({
            m.get("id", "") for m in listing.get("data", []) if m.get("id")
        })
    else:  # Custom
        identity = "custom Responses-compatible server"
        listing = convert_from_ld_json_response(
            http_get(f"{urls['responses_base_url']}/models", timeout=timeout_seconds)
        )
        models = sorted({
            m.get("id", "") for m in listing.get("data", []) if m.get("id")
        })

    return {
        "provider": provider,
        "origin": urls["origin"],
        "responses_base_url": urls["responses_base_url"],
        "identity": identity,
        "models": models,
    }


# ---------------------------------------------------------------------------
# TOML helper (stdlib, minimal)
# ---------------------------------------------------------------------------

def convert_to_ld_toml_string(value: str) -> str:
    """Escape a value for use inside a TOML string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def write_ld_utf8_file(path: str, content: str) -> None:
    """Write *content* atomically to *path* using a temp file."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = path + "." + uuid.uuid4().hex + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def get_ld_provider_config_path(state_root: str) -> str:
    return os.path.join(state_root, "config", "provider.json")


def read_ld_provider_config(state_root: str) -> Dict[str, Any]:
    path = get_ld_provider_config_path(state_root)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Provider configuration not found: {path}. "
            f"Run configure_provider.py first."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def get_ld_sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def acquire_ld_file_lock(path: str) -> Any:
    """Acquire a nonblocking OS lock that is released if the process exits."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    stream = open(path, "a+b")
    try:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except (OSError, IOError):
        stream.close()
        raise RuntimeError(f"Another delegation is already active: {path}")


def release_ld_file_lock(stream: Any) -> None:
    """Release a lock obtained by acquire_ld_file_lock."""
    try:
        stream.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    finally:
        stream.close()


# ---------------------------------------------------------------------------
# Repository timeout config
# ---------------------------------------------------------------------------

def read_ld_repository_timeout_config(repository_root: str) -> Dict[str, Optional[int]]:
    """Read .codex/local-delegate.toml for timeout overrides."""
    settings: Dict[str, Optional[int]] = {
        "timeout_minutes": None,
        "inactivity_timeout_minutes": None,
    }
    path = os.path.join(repository_root, ".codex", "local-delegate.toml")
    if not os.path.isfile(path):
        return settings

    seen_keys: set = set()
    pattern = re.compile(
        r"^\s*(timeout_minutes|inactivity_timeout_minutes)\s*=\s*(\d+)\s*(?:#.*)?$"
    )
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            m = pattern.match(line)
            if not m:
                continue
            key = m.group(1)
            if key in seen_keys:
                raise RuntimeError(f"Duplicate timeout setting '{key}' in {path}.")
            seen_keys.add(key)
            value = int(m.group(2))
            if key == "timeout_minutes":
                if not (1 <= value <= 1440):
                    raise RuntimeError("timeout_minutes must be between 1 and 1440.")
                settings["timeout_minutes"] = value
            else:
                if not (0 <= value <= 1440):
                    raise RuntimeError(
                        "inactivity_timeout_minutes must be between 0 and 1440."
                    )
                settings["inactivity_timeout_minutes"] = value
    return settings


# ---------------------------------------------------------------------------
# Process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Terminate a process tree cross-platform."""
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True,
        )
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass


def _iso_timestamp() -> str:
    """Return current UTC time as ISO 8601 string (cross-platform)."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def get_ld_codex_approval_mode(help_text: str) -> Dict[str, Any]:
    """Select the supported non-interactive approval interface."""
    if "--approve-for-me" in help_text:
        return {
            "mode": "approve-for-me",
            "arguments": ["--approve-for-me"],
            "sandbox_arguments": [],
        }
    if "--ask-for-approval" in help_text:
        return {
            "mode": "ask-for-approval-never",
            "arguments": ["--ask-for-approval", "never"],
            "sandbox_arguments": ["--sandbox", "workspace-write"],
        }
    raise RuntimeError(
        "Codex CLI exposes neither --approve-for-me nor --ask-for-approval. "
        "Install a compatible Codex CLI."
    )


def inspect_ld_codex_cli(codex_path: str, timeout: int = 10) -> Dict[str, Any]:
    """Validate a Codex CLI and return its version and invocation contract."""
    resolved_path = shutil.which(codex_path) or codex_path
    canonical_path = os.path.normcase(
        os.path.normpath(os.path.realpath(os.path.abspath(resolved_path)))
    )
    try:
        version_result = subprocess.run(
            [resolved_path, "--version"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        help_result = subprocess.run(
            [resolved_path, "exec", "--help"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not inspect Codex CLI: {exc}") from exc

    version_text = (version_result.stdout + version_result.stderr).strip()
    if version_result.returncode != 0 or not version_text:
        raise RuntimeError("Codex CLI did not return a version.")
    help_text = help_result.stdout + help_result.stderr
    if help_result.returncode != 0:
        raise RuntimeError("Codex CLI could not display exec help.")
    required_flags = [
        "--profile",
        "--strict-config",
        "--sandbox",
        "--ephemeral",
        "--json",
        "--output-schema",
        "--output-last-message",
        "--cd",
    ]
    missing = [flag for flag in required_flags if flag not in help_text]
    if missing:
        raise RuntimeError(
            "Codex CLI does not expose required option(s): " + ", ".join(missing)
        )
    approval = get_ld_codex_approval_mode(help_text)
    return {
        "path": canonical_path,
        "version": version_text.splitlines()[0].strip(),
        "approval_mode": approval["mode"],
        "approval_arguments": approval["arguments"],
        "sandbox_arguments": approval["sandbox_arguments"],
    }


def wait_ld_process_with_activity_timeout(
    process: subprocess.Popen,
    standard_output_path: str,
    standard_error_path: str,
    hard_timeout: float,
    inactivity_timeout: float,
    poll_milliseconds: int = 100,
    activity_path: Optional[str] = None,
) -> Dict[str, Any]:
    """Wait for *process* with activity-based inactivity timeout.

    If *inactivity_timeout* is zero, inactivity detection is disabled.
    Returns a dict with completed, exit_code, termination_reason, etc.
    """
    start_time = time.monotonic()
    last_activity_time = start_time
    last_activity_at = _iso_timestamp()
    activity_lock = threading.Lock()
    reader_errors: List[BaseException] = []
    termination_reason: Optional[str] = None
    poll_interval = poll_milliseconds / 1000.0

    artifact_paths = [standard_output_path, standard_error_path]
    if activity_path:
        artifact_paths.append(activity_path)
    for artifact_path in artifact_paths:
        artifact_parent = os.path.dirname(artifact_path)
        if artifact_parent:
            os.makedirs(artifact_parent, exist_ok=True)

    activity_artifact = None

    def _record_activity(event: str, **fields: Any) -> None:
        if activity_artifact is None:
            return
        record = {"timestamp": _iso_timestamp(), "event": event, **fields}
        activity_artifact.write(json.dumps(record, separators=(",", ":")) + "\n")
        activity_artifact.flush()

    def _read_stream(stream_name: str, stream: Any, artifact: Any) -> None:
        nonlocal last_activity_time, last_activity_at
        try:
            while True:
                read = getattr(stream, "read1", stream.read)
                chunk = read(4096)
                if not chunk:
                    break
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8", errors="replace")
                artifact.write(chunk)
                artifact.flush()
                with activity_lock:
                    last_activity_time = time.monotonic()
                    last_activity_at = _iso_timestamp()
                    _record_activity("stream.activity", stream=stream_name, bytes=len(chunk))
        except BaseException as exc:
            reader_errors.append(exc)

    try:
        if activity_path:
            activity_artifact = open(activity_path, "w", encoding="utf-8")
        _record_activity("process.started", pid=process.pid)
        with open(standard_output_path, "wb") as stdout_artifact, open(
            standard_error_path, "wb"
        ) as stderr_artifact:
            stdout_thread = threading.Thread(
                target=_read_stream,
                args=("stdout", process.stdout, stdout_artifact),
                name="local-delegate-stdout",
                daemon=True,
            )
            stderr_thread = threading.Thread(
                target=_read_stream,
                args=("stderr", process.stderr, stderr_artifact),
                name="local-delegate-stderr",
                daemon=True,
            )
            stdout_thread.start()
            stderr_thread.start()

            while process.poll() is None:
                now = time.monotonic()
                with activity_lock:
                    inactive_for = now - last_activity_time
                if now - start_time >= hard_timeout:
                    termination_reason = "hard-timeout"
                    _kill_process_tree(process.pid)
                elif inactivity_timeout > 0 and inactive_for >= inactivity_timeout:
                    termination_reason = "inactivity-timeout"
                    _kill_process_tree(process.pid)
                if termination_reason is not None:
                    break
                time.sleep(poll_interval)

            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _kill_process_tree(process.pid)
                process.wait(timeout=5)

            stdout_thread.join(timeout=5)
            stderr_thread.join(timeout=5)
        _record_activity(
            "process.finished",
            exit_code=process.returncode if termination_reason is None else -1,
            termination_reason=termination_reason,
            elapsed_ms=int((time.monotonic() - start_time) * 1000),
        )
    finally:
        if activity_artifact is not None:
            activity_artifact.close()

    for stream in (process.stdout, process.stderr):
        if stream is not None and not stream.closed:
            stream.close()

    if reader_errors and termination_reason is None:
        raise RuntimeError(f"Failed to capture developer process output: {reader_errors[0]}")

    exit_code = process.returncode if termination_reason is None else -1

    return {
        "completed": termination_reason is None,
        "exit_code": exit_code,
        "termination_reason": termination_reason,
        "last_activity_at": last_activity_at,
        "elapsed_ms": int((time.monotonic() - start_time) * 1000),
    }
