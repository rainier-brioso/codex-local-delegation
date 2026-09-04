#!/usr/bin/env python3
"""Enable repository-wide automatic preference for local delegation."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Tuple

from local_delegation.common import write_ld_utf8_file


START_MARKER = "<!-- codex-local-delegation:start -->"
END_MARKER = "<!-- codex-local-delegation:end -->"
HANDOFF_EXCLUDE = ".codex/local-handoffs/"
MANAGED_POLICY = """<!-- codex-local-delegation:start -->
## Local developer workflow

Prefer `$local-delegate` for bounded implementation work that changes source
code, tests, configuration, or documentation.

When `LOCAL_DELEGATION_ACTIVE=1`, you are the delegated developer. Implement
the supplied handoff directly. Never invoke `$local-delegate`, another
delegation workflow, or a subagent.

Codex remains the analyst and reviewer: define scope and acceptance criteria,
delegate the bounded implementation task, and review the resulting diff and
test evidence.

Do not commit, push, install dependencies, use the network, run migrations, or
perform external actions unless the user requests a separate authorized workflow.
<!-- codex-local-delegation:end -->"""


def _newline_for(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def update_agents_content(content: str) -> Tuple[str, bool]:
    """Add or replace the managed policy block without changing other text."""
    start_count = content.count(START_MARKER)
    end_count = content.count(END_MARKER)
    if start_count != end_count or start_count > 1:
        raise RuntimeError(
            "AGENTS.md contains incomplete or duplicate local-delegation markers."
        )

    newline = _newline_for(content)
    policy = MANAGED_POLICY.replace("\n", newline)
    if start_count == 1:
        start = content.index(START_MARKER)
        end = content.index(END_MARKER, start) + len(END_MARKER)
        updated = content[:start] + policy + content[end:]
    else:
        updated = content
        if updated:
            if not updated.endswith(("\n", "\r")):
                updated += newline
            if not updated.endswith(newline * 2):
                updated += newline
        updated += policy + newline
    return updated, updated != content


def update_exclude_content(content: str) -> Tuple[str, bool]:
    """Add the handoff directory to Git's local exclude file once."""
    if any(line.strip() == HANDOFF_EXCLUDE for line in content.splitlines()):
        return content, False
    newline = _newline_for(content)
    updated = content
    if updated and not updated.endswith(("\n", "\r")):
        updated += newline
    updated += HANDOFF_EXCLUDE + newline
    return updated, True


def resolve_repository(path: str) -> str:
    """Resolve *path* to the canonical root of its Git worktree."""
    try:
        result = subprocess.run(
            ["git", "-C", path, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"Not a Git repository: {path}") from exc
    return os.path.normpath(os.path.realpath(os.path.abspath(result.stdout.strip())))


def get_exclude_path(repository_root: str) -> str:
    """Return the worktree-correct Git exclude path."""
    result = subprocess.run(
        ["git", "-C", repository_root, "rev-parse", "--git-path", "info/exclude"],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    path = result.stdout.strip()
    if not os.path.isabs(path):
        path = os.path.join(repository_root, path)
    return os.path.normpath(os.path.abspath(path))


def _read_optional(path: str) -> str:
    if not os.path.exists(path):
        return ""
    if not os.path.isfile(path):
        raise RuntimeError(f"Expected a regular file: {path}")
    with open(path, "r", encoding="utf-8") as stream:
        return stream.read()


def configure_repository(repository: str, check_only: bool = False) -> bool:
    """Configure *repository* and return whether it was already configured."""
    repository_root = resolve_repository(repository)
    agents_path = os.path.join(repository_root, "AGENTS.md")
    if os.path.islink(agents_path):
        raise RuntimeError("Refusing to modify a symlinked AGENTS.md.")
    agents_content = _read_optional(agents_path)
    updated_agents, agents_changed = update_agents_content(agents_content)

    exclude_path = get_exclude_path(repository_root)
    exclude_content = _read_optional(exclude_path)
    updated_exclude, exclude_changed = update_exclude_content(exclude_content)
    already_configured = not agents_changed and not exclude_changed

    if check_only:
        print("configured" if already_configured else "not configured")
        return already_configured

    if agents_changed:
        write_ld_utf8_file(agents_path, updated_agents)
        print(f"Updated repository policy: {agents_path}")
    else:
        print(f"Repository policy already configured: {agents_path}")
    if exclude_changed:
        write_ld_utf8_file(exclude_path, updated_exclude)
        print(f"Updated local Git exclusions: {exclude_path}")
    else:
        print(f"Local Git exclusions already configured: {exclude_path}")
    return already_configured


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Enable automatic local-delegation preference in a Git repository."
    )
    parser.add_argument(
        "--repository",
        default=".",
        help="Repository path (default: current directory).",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check configuration without changing files.",
    )
    args = parser.parse_args(argv)
    try:
        configured = configure_repository(args.repository, check_only=args.check)
        if args.check and not configured:
            return 1
        return 0
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
