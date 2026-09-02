#!/usr/bin/env python3
"""Runner: invoke codex exec with a local model for bounded implementation work."""

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from local_delegation.common import (
    get_ld_state_root,
    initialize_ld_state_root,
    read_ld_provider_config,
    get_ld_provider_probe,
    get_ld_sha256_text,
    write_ld_utf8_file,
    read_ld_repository_timeout_config,
    wait_ld_process_with_activity_timeout,
    _kill_process_tree,
    acquire_ld_file_lock,
    release_ld_file_lock,
    inspect_ld_codex_cli,
    CODEX_COMPATIBILITY_CONTRACT_VERSION,
)


def convert_to_relative_path(path: str) -> str:
    """Convert *path* to a repository-relative path. Reject absolute/traversal paths."""
    if os.path.isabs(path):
        raise ValueError(f"Path must be repository-relative: {path}")
    normal = path.replace("\\", "/").strip("/")
    if not normal or normal == ".":
        return "."
    segments = normal.split("/")
    if ".." in segments or "." in segments:
        raise ValueError(f"Path contains a traversal segment: {path}")
    return normal


def canonicalize_path(path: str) -> str:
    """Return a normalized physical path, expanding aliases such as NTFS 8.3 names."""
    return os.path.normpath(os.path.realpath(os.path.abspath(path)))


def is_path_beneath(root: str, candidate: str) -> bool:
    """Return whether an existing candidate is strictly beneath root."""
    canonical_root = canonicalize_path(root)
    canonical_candidate = canonicalize_path(candidate)
    try:
        common = os.path.commonpath([canonical_root, canonical_candidate])
    except ValueError:
        return False
    return (
        os.path.normcase(common) == os.path.normcase(canonical_root)
        and os.path.normcase(canonical_candidate) != os.path.normcase(canonical_root)
    )


def assert_path_inside_repository(
    repository_root: str,
    relative_path: str,
) -> str:
    """Resolve a relative path and ensure it stays within the repository.

    Also rejects symlinks/junctions/reparse-points in the resolved path.
    """
    repository_root = os.path.normpath(repository_root)
    root_with_sep = repository_root.rstrip(os.sep) + os.sep
    candidate = (
        repository_root
        if relative_path == "."
        else os.path.normpath(os.path.join(repository_root, relative_path))
    )
    try:
        contained = os.path.normcase(os.path.commonpath([repository_root, candidate])) == os.path.normcase(repository_root)
    except ValueError:
        contained = False
    if not contained:
        raise ValueError(f"Resolved path escapes the repository: {relative_path}")

    cursor = candidate
    while cursor.startswith(root_with_sep):
        if os.path.lexists(cursor):
            is_reparse = False
            if sys.platform == "win32":
                import ctypes
                attrs = ctypes.windll.kernel32.GetFileAttributesW(cursor)
                if attrs != -1 and (attrs & 0x0400) != 0:  # FILE_ATTRIBUTE_REPARSE_POINT
                    is_reparse = True
            else:
                is_reparse = os.path.islink(cursor)
            if is_reparse:
                raise ValueError(f"Allowed or protected path traverses a symlink or junction: {relative_path}")
        parent = os.path.dirname(cursor)
        if parent == cursor or len(parent) < len(repository_root):
            break
        cursor = parent
    return os.path.normpath(candidate)


def test_path_prefix(path: str, prefix: str) -> bool:
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


def validate_handoff_constraints(handoff_text: str) -> None:
    """Require every v1 side-effect constraint to be present and false."""
    labels = (
        "Network access",
        "Dependency installation",
        "Database or state migrations",
        "Commits or Git ref changes",
        "External actions",
    )
    for label in labels:
        match = re.search(
            rf"(?im)^\s*-\s*{re.escape(label)}\s*:\s*(true|false)\s*$",
            handoff_text,
        )
        if match is None:
            raise ValueError(f"Handoff must declare '{label}: false'.")
        if match.group(1).lower() != "false":
            raise ValueError(f"Handoff requests forbidden capability: {label}.")


def validate_json_schema_subset(value: Any, schema: Dict[str, Any], path: str = "result") -> None:
    """Validate the JSON Schema features used by developer-result.schema.json."""
    expected_type = schema.get("type")
    type_checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected_type in type_checks and not type_checks[expected_type](value):
        raise ValueError(f"{path} must be a JSON {expected_type}.")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of {schema['enum']}.")

    if expected_type == "object":
        properties = schema.get("properties", {})
        for required in schema.get("required", []):
            if required not in value:
                raise ValueError(f"{path} is missing required field '{required}'.")
        if schema.get("additionalProperties") is False:
            extras = sorted(set(value) - set(properties))
            if extras:
                raise ValueError(f"{path} has unsupported field '{extras[0]}'.")
        for key, item in value.items():
            if key in properties:
                validate_json_schema_subset(item, properties[key], f"{path}.{key}")

    if expected_type == "array":
        item_schema = schema.get("items")
        if item_schema:
            for index, item in enumerate(value):
                validate_json_schema_subset(item, item_schema, f"{path}[{index}]")
        if schema.get("uniqueItems"):
            encoded = [json.dumps(item, sort_keys=True) for item in value]
            if len(encoded) != len(set(encoded)):
                raise ValueError(f"{path} must contain unique items.")


def resolve_timeout_settings(
    cli_timeout: Optional[int],
    cli_inactivity_timeout: Optional[int],
    repository_settings: Dict[str, Optional[int]],
) -> Tuple[int, int]:
    """Apply CLI-over-repository-over-default timeout precedence."""
    timeout = (
        cli_timeout
        if cli_timeout is not None
        else repository_settings.get("timeout_minutes") or 60
    )
    repository_inactivity = repository_settings.get("inactivity_timeout_minutes")
    inactivity = (
        cli_inactivity_timeout
        if cli_inactivity_timeout is not None
        else repository_inactivity
        if repository_inactivity is not None
        else 15
    )
    return timeout, inactivity


def get_ld_workspace_inventory(repository_root: str) -> Dict[str, str]:
    """Build a hash-based inventory of all files (tracked + untracked)."""
    inventory: Dict[str, str] = {}
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=repository_root,
            capture_output=True,
            check=True,
        )
        paths = result.stdout.decode("utf-8").split("\x00")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return inventory

    for path_str in paths:
        if not path_str:
            continue
        relative = path_str.replace("\\", "/")
        full = os.path.normpath(os.path.join(repository_root, relative))
        if not os.path.lexists(full):
            inventory[relative] = "missing"
            continue

        is_reparse = False
        if sys.platform == "win32":
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(full)
            if attrs != -1 and (attrs & 0x0400) != 0:
                is_reparse = True
        else:
            is_reparse = os.path.islink(full)

        if is_reparse:
            link_target = ""
            if os.path.islink(full):
                link_target = os.path.realpath(full)
            inventory[relative] = f"reparse:{link_target}"
        elif os.path.isdir(full):
            # Check for submodule
            is_submodule = os.path.exists(os.path.join(full, ".git"))
            if is_submodule:
                inventory[relative] = "submodule"
            else:
                inventory[relative] = "directory"
        else:
            sha = hashlib.sha256()
            with open(full, "rb") as f:
                for chunk in iter(lambda: f.read(65536), b""):
                    sha.update(chunk)
            inventory[relative] = sha.hexdigest()

    return inventory


def compare_inventory(
    before: Dict[str, str],
    after: Dict[str, str],
) -> List[str]:
    """Return list of paths whose hash changed or were added/removed."""
    all_keys = set(before.keys()) | set(after.keys())
    changed = []
    for key in sorted(all_keys):
        before_val = before.get(key, "<absent>")
        after_val = after.get(key, "<absent>")
        if before_val != after_val:
            changed.append(key)
    return changed


def copy_baseline_untracked_files(repository_root: str, destination: str) -> None:
    """Copy untracked files from the repository into *destination*."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--others", "--exclude-standard"],
            cwd=repository_root,
            capture_output=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return

    paths = result.stdout.decode("utf-8").split("\x00")
    for relative in paths:
        source = os.path.join(repository_root, relative)
        if not os.path.isfile(source):
            continue

        is_reparse = False
        if sys.platform == "win32":
            import ctypes
            attrs = ctypes.windll.kernel32.GetFileAttributesW(source)
            if attrs != -1 and (attrs & 0x0400) != 0:
                is_reparse = True
        else:
            is_reparse = os.path.islink(source)
        if is_reparse:
            continue

        target = os.path.join(destination, relative)
        target_dir = os.path.dirname(target)
        if target_dir:
            os.makedirs(target_dir, exist_ok=True)
        shutil.copy2(source, target)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run local developer delegation.")
    parser.add_argument("--repository", required=True, help="Repository root path.")
    parser.add_argument("--handoff-path", required=True, help="Path to the handoff file.")
    parser.add_argument(
        "--allowed-path",
        action="append",
        required=True,
        help="Repository-relative allowed paths (repeatable).",
    )
    parser.add_argument(
        "--protected-path",
        action="append",
        default=[],
        help="Repository-relative protected paths (repeatable).",
    )
    parser.add_argument(
        "--test-command",
        action="append",
        default=[],
        help="Test/verification commands to pass to the developer (repeatable).",
    )
    parser.add_argument(
        "--timeout-minutes",
        type=int,
        default=None,
        help="Hard timeout in minutes (1-1440; repository setting or 60 by default).",
    )
    parser.add_argument(
        "--inactivity-timeout-minutes",
        type=int,
        default=None,
        help="Inactivity timeout in minutes (0-1440; repository setting or 15 by default).",
    )
    parser.add_argument("--state-root", default=None, help="State root directory.")
    parser.add_argument("--codex-bin", default=None, help="Path to Codex CLI executable.")
    args = parser.parse_args(argv)
    if args.timeout_minutes is not None and not 1 <= args.timeout_minutes <= 1440:
        parser.error("--timeout-minutes must be between 1 and 1440")
    if args.inactivity_timeout_minutes is not None and not 0 <= args.inactivity_timeout_minutes <= 1440:
        parser.error("--inactivity-timeout-minutes must be between 0 and 1440")

    lock_stream = None
    run_directory: Optional[str] = None
    handoff_full_path: Optional[str] = None
    runner_record: Dict[str, Any] = {
        "schemaVersion": 1,
        "status": "starting",
        "exitCode": 10,
        "startedAt": datetime.now(timezone.utc).isoformat(),
    }

    try:
        # Recursion prevention
        if os.environ.get("LOCAL_DELEGATION_ACTIVE") == "1":
            raise RuntimeError(
                "Recursive delegation is forbidden when LOCAL_DELEGATION_ACTIVE=1."
            )

        resolved_state_root = get_ld_state_root(args.state_root)
        initialize_ld_state_root(resolved_state_root)
        configuration = read_ld_provider_config(resolved_state_root)

        codex_executable = args.codex_bin
        if not codex_executable:
            codex_executable = os.environ.get("LOCAL_DELEGATE_CODEX_BIN")
        if not codex_executable:
            codex_executable = shutil.which("codex")
        if not codex_executable:
            raise RuntimeError("Codex CLI was not found.")
        codex_info = inspect_ld_codex_cli(codex_executable)

        # Check doctor
        last_doctor = configuration.get("lastDoctor")
        if not last_doctor or last_doctor.get("status") != "passed":
            raise RuntimeError(
                "Provider doctor has not passed. Run doctor.py before delegation."
            )
        doctor_contract = (
            last_doctor.get("codexPath"),
            last_doctor.get("codexVersion"),
            last_doctor.get("approvalMode"),
            last_doctor.get("compatibilityContractVersion"),
        )
        current_contract = (
            codex_info["path"],
            codex_info["version"],
            codex_info["approval_mode"],
            CODEX_COMPATIBILITY_CONTRACT_VERSION,
        )
        if doctor_contract != current_contract:
            raise RuntimeError(
                "Codex CLI changed since the last successful doctor check. "
                "Run doctor.py again before delegation."
            )

        profile_path = os.path.join(resolved_state_root, "codex-home", "local-developer.config.toml")
        if not os.path.isfile(profile_path):
            raise RuntimeError(f"Isolated local-developer profile not found: {profile_path}")

        # Provider preflight
        try:
            probe = get_ld_provider_probe(
                configuration["provider"],
                configuration["origin"],
                10,
            )
            if configuration["model"] not in probe["models"]:
                raise RuntimeError(
                    f"Configured model '{configuration['model']}' is not reported by the provider."
                )
        except Exception as exc:
            raise RuntimeError(f"Provider preflight failed: {exc}") from exc

        # Validate repository
        requested_repo = canonicalize_path(args.repository)
        try:
            result = subprocess.run(
                ["git", "-C", requested_repo, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                check=True,
                timeout=10,
            )
            repository_root = canonicalize_path(result.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            raise RuntimeError(f"Invalid repository: {args.repository}") from exc

        # Timeout config
        repo_timeout_config = read_ld_repository_timeout_config(repository_root)
        effective_timeout, effective_inactivity = resolve_timeout_settings(
            args.timeout_minutes,
            args.inactivity_timeout_minutes,
            repo_timeout_config,
        )

        # Validate handoff
        handoff_full_path = canonicalize_path(args.handoff_path)
        if not os.path.isfile(handoff_full_path) or not is_path_beneath(repository_root, handoff_full_path):
            raise RuntimeError("Handoff must be an existing file beneath the repository root.")

        task_id = os.path.basename(os.path.dirname(handoff_full_path))
        if not re.match(r"^[a-z0-9][a-z0-9-]{0,63}$", task_id):
            raise ValueError(f"Invalid task id '{task_id}'.")

        with open(handoff_full_path, "r", encoding="utf-8") as f:
            handoff_text = f.read()
        validate_handoff_constraints(handoff_text)

        # Validate allowed/protected paths
        allowed = sorted(set(convert_to_relative_path(p) for p in args.allowed_path))
        for path_str in allowed:
            assert_path_inside_repository(repository_root, path_str)

        handoff_rel_dir = os.path.relpath(os.path.dirname(handoff_full_path), repository_root).replace("\\", "/")
        protected_set = {".git", handoff_rel_dir} | set(
            convert_to_relative_path(p) for p in args.protected_path
        )
        for path_str in protected_set:
            assert_path_inside_repository(repository_root, path_str)

        # Compute repository ID
        repository_id = get_ld_sha256_text(repository_root)[:24]

        # Lock file
        lock_path = os.path.join(resolved_state_root, "locks", f"{repository_id}.lock")
        try:
            lock_stream = acquire_ld_file_lock(lock_path)
        except RuntimeError as exc:
            raise RuntimeError(
                f"Another delegation is already active for this repository: {lock_path}"
            ) from exc

        # Create run directory
        candidate_run_dir = os.path.join(resolved_state_root, "run", repository_id, task_id)
        if os.path.exists(candidate_run_dir):
            raise RuntimeError(
                f"Run directory already exists; choose a new task id: {candidate_run_dir}"
            )
        run_directory = candidate_run_dir
        os.makedirs(run_directory, exist_ok=True)
        os.makedirs(os.path.join(run_directory, "baseline-files"), exist_ok=True)

        # Copy handoff
        shutil.copy2(handoff_full_path, os.path.join(run_directory, "request.md"))

        # Capture baseline
        try:
            head_before = subprocess.run(
                ["git", "-C", repository_root, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
            refs_before = subprocess.run(
                ["git", "-C", repository_root, "for-each-ref",
                 "--format=%(refname)%00%(objectname)%00"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout
            status_before = subprocess.run(
                ["git", "-C", repository_root, "status", "--porcelain=v2",
                 "-z", "--untracked-files=all"],
                capture_output=True, check=True, timeout=10,
            ).stdout
            baseline_diff = subprocess.run(
                ["git", "-C", repository_root, "diff", "--binary", "HEAD", "--"],
                capture_output=True, check=True, timeout=30,
            ).stdout
            inventory_before = get_ld_workspace_inventory(repository_root)
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Git baseline capture failed: {exc}") from exc

        write_ld_utf8_file(
            os.path.join(run_directory, "baseline.diff"),
            baseline_diff.decode("utf-8", errors="replace"),
        )
        copy_baseline_untracked_files(repository_root, os.path.join(run_directory, "baseline-files"))

        baseline = {
            "repository": repository_root,
            "head": head_before,
            "refs": refs_before,
            "status": status_before.decode("utf-8", errors="replace"),
            "inventory": inventory_before,
            "allowedPaths": allowed,
            "protectedPaths": list(protected_set),
        }
        write_ld_utf8_file(
            os.path.join(run_directory, "baseline.json"),
            json.dumps(baseline, indent=2) + "\n",
        )

        # Schema path
        script_dir = os.path.dirname(os.path.abspath(__file__))
        schema_path = os.path.normpath(os.path.join(script_dir, "..", "schemas", "developer-result.schema.json"))

        result_path = os.path.join(run_directory, "result.json")
        events_path = os.path.join(run_directory, "events.jsonl")
        stderr_path = os.path.join(run_directory, "stderr.log")
        activity_path = os.path.join(run_directory, "activity.jsonl")
        print(f"Local developer run: {run_directory}", flush=True)
        print(f"Live event log: {events_path}", flush=True)
        print(f"Live stderr log: {stderr_path}", flush=True)
        print(f"Activity log: {activity_path}", flush=True)

        tests = "\n".join(args.test_command) if args.test_command else "Use only the verification commands stated in the handoff."
        report_shape = json.dumps(
            {
                "status": "completed|partial|failed",
                "summary": "string",
                "changed_files": ["path"],
                "commands": [{"command": "string", "outcome": "string"}],
                "known_limitations": ["string"],
                "follow_up_needs": ["string"],
            },
            separators=(",", ":"),
        )

        prompt = (
            f"You are the delegated local developer. LOCAL_DELEGATION_ACTIVE=1 is set.\n"
            f"Never invoke local-delegate, another delegation workflow, or a subagent.\n"
            f"Do not commit, push, change Git refs, install dependencies, use the network,\n"
            f"run migrations, or perform external actions. Change only these repository-relative\n"
            f"paths: {', '.join(allowed)}. Do not modify protected paths: {', '.join(protected_set)}.\n"
            f"Inspect only the paths needed for this task; do not recursively enumerate the\n"
            f"repository root or .git. The shell is host-native. On Windows, use PowerShell\n"
            f"syntax. Never use cat, bash, sh, heredocs, multiline source inside python -c,\n"
            f"or encoded command strings. Prefer focused inspection and short, incremental\n"
            f"edit commands. Keep this run to one coherent bounded task.\n"
            f"Read and implement this handoff and run only its prescribed verification.\n"
            f"Your final response MUST be only one JSON object, with no markdown fences or other\n"
            f"text. It must have exactly these fields:\n"
            f"{report_shape}\n\n"
            f"Verification commands supplied by the analyst:\n{tests}\n\n"
            f"Handoff:\n{handoff_text}\n"
        )

        # Update runner record
        runner_record["repository"] = repository_root
        runner_record["repositoryId"] = repository_id
        runner_record["taskId"] = task_id
        runner_record["runDirectory"] = run_directory
        runner_record["timeoutMinutes"] = effective_timeout
        runner_record["inactivityTimeoutMinutes"] = effective_inactivity
        runner_record["status"] = "running"
        runner_record["codexPath"] = codex_info["path"]
        runner_record["codexVersion"] = codex_info["version"]
        runner_record["approvalMode"] = codex_info["approval_mode"]

        # Launch codex exec
        codex_home = os.path.join(resolved_state_root, "codex-home")
        process_options: Dict[str, Any] = {}
        if sys.platform == "win32":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True

        start = subprocess.Popen(
            [
                codex_executable,
                "exec",
                "--profile", "local-developer",
                "--strict-config",
                *codex_info["sandbox_arguments"],
                *codex_info["approval_arguments"],
                "--ephemeral",
                "--json",
                "--output-schema", schema_path,
                "--output-last-message",
                result_path,
                "--cd", repository_root,
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=repository_root,
            env={**os.environ, "CODEX_HOME": codex_home, "LOCAL_DELEGATION_ACTIVE": "1"},
            **process_options,
        )

        try:
            assert start.stdin is not None
            start.stdin.write(prompt.encode("utf-8"))
            start.stdin.flush()
            start.stdin.close()
            process_result = wait_ld_process_with_activity_timeout(
                start,
                events_path,
                stderr_path,
                hard_timeout=effective_timeout * 60,
                inactivity_timeout=effective_inactivity * 60 if effective_inactivity > 0 else 0,
                poll_milliseconds=100,
                activity_path=activity_path,
            )
        except Exception:
            _kill_process_tree(start.pid)
            start.wait()
            for stream in (start.stdin, start.stdout, start.stderr):
                if stream is not None and not stream.closed:
                    stream.close()
            raise

        # Capture post-run state
        inventory_after = get_ld_workspace_inventory(repository_root)
        changed_during_run = compare_inventory(inventory_before, inventory_after)

        try:
            head_after = subprocess.run(
                ["git", "-C", repository_root, "rev-parse", "HEAD"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout.strip()
            refs_after = subprocess.run(
                ["git", "-C", repository_root, "for-each-ref",
                 "--format=%(refname)%00%(objectname)%00"],
                capture_output=True, text=True, check=True, timeout=10,
            ).stdout
        except subprocess.CalledProcessError:
            head_after = ""
            refs_after = ""

        policy_violations: List[str] = []
        for path_str in changed_during_run:
            if path_str in inventory_after and inventory_after[path_str].startswith("reparse:"):
                policy_violations.append(f"Changed path is a symlink or junction: {path_str}")
            elif any(test_path_prefix(path_str, p) for p in protected_set):
                policy_violations.append(f"Protected path changed: {path_str}")
            elif not any(test_path_prefix(path_str, p) for p in allowed):
                policy_violations.append(f"Out-of-scope path changed: {path_str}")

        if head_before != head_after or refs_before != refs_after:
            policy_violations.append("HEAD or Git refs changed during delegation.")

        runner_record["changedDuringRun"] = changed_during_run
        runner_record["policyViolations"] = policy_violations
        runner_record["developerExitCode"] = process_result["exit_code"]
        runner_record["lastDeveloperActivityAt"] = process_result["last_activity_at"]
        runner_record["developerElapsedMs"] = process_result["elapsed_ms"]

        if not process_result["completed"]:
            runner_record["status"] = "timeout"
            runner_record["exitCode"] = 31
            runner_record["timeoutReason"] = process_result["termination_reason"]
        elif policy_violations:
            runner_record["status"] = "policy-failure"
            runner_record["exitCode"] = 50
        elif process_result["exit_code"] != 0:
            runner_record["status"] = "developer-failure"
            runner_record["exitCode"] = 30
        elif not os.path.isfile(result_path):
            runner_record["status"] = "verification-failure"
            runner_record["exitCode"] = 40
            runner_record["verificationError"] = "Codex did not produce result.json."
        else:
            try:
                with open(result_path, "r", encoding="utf-8") as f:
                    result_text = f.read().strip()

                # Strip JSON fences if present
                fence_match = re.match(r"(?s)^```(?:json)?\s*(?P<json>\{.*\})\s*```\s*$", result_text)
                if fence_match:
                    result_text = fence_match.group("json").strip()

                developer_result = json.loads(result_text)

                # Validate schema
                with open(schema_path, "r", encoding="utf-8") as f:
                    schema = json.load(f)
                validate_json_schema_subset(developer_result, schema)

                write_ld_utf8_file(result_path, result_text + "\n")
                runner_record["developerReportedStatus"] = developer_result.get("status", "unknown")
                if developer_result.get("status") == "completed":
                    runner_record["status"] = "completed"
                    runner_record["exitCode"] = 0
                else:
                    runner_record["status"] = "developer-failure"
                    runner_record["exitCode"] = 30
            except Exception as exc:
                runner_record["status"] = "verification-failure"
                runner_record["exitCode"] = 40
                runner_record["verificationError"] = f"result.json is invalid: {exc}"

    except Exception as exc:
        requested_exit_code = 10
        error_msg = str(exc)
        if "doctor" in error_msg.lower() or "profile" in error_msg.lower() or "preflight" in error_msg.lower():
            requested_exit_code = 20
        runner_record["status"] = "endpoint-profile-failure" if requested_exit_code == 20 else "configuration-failure"
        runner_record["exitCode"] = requested_exit_code
        runner_record["error"] = error_msg
    finally:
        runner_record["finishedAt"] = datetime.now(timezone.utc).isoformat()
        if run_directory and os.path.exists(run_directory):
            runner_path = os.path.join(run_directory, "runner.json")
            write_ld_utf8_file(runner_path, json.dumps(runner_record, indent=2) + "\n")
            if handoff_full_path and os.path.isdir(os.path.dirname(handoff_full_path)):
                shutil.copy2(runner_path, os.path.join(os.path.dirname(handoff_full_path), "runner.json"))
                result_source = os.path.join(run_directory, "result.json")
                if os.path.isfile(result_source):
                    shutil.copy2(result_source, os.path.join(os.path.dirname(handoff_full_path), "result.json"))

        if lock_stream:
            try:
                release_ld_file_lock(lock_stream)
            except Exception:
                pass

    if runner_record["exitCode"] != 0:
        print(json.dumps(runner_record, indent=2, default=str), file=sys.stderr)
    return runner_record["exitCode"]


if __name__ == "__main__":
    sys.exit(main())
