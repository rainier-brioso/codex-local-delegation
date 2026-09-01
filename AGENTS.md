# Local developer workflow

Use `$local-delegate` for bounded implementation work that changes source code,
tests, configuration, or documentation.

When `LOCAL_DELEGATION_ACTIVE=1`, you are the delegated developer. Implement the
supplied handoff directly. Never invoke `$local-delegate`, another delegation
workflow, or a subagent.

When acting as analyst and reviewer, define scope and acceptance criteria,
delegate the bounded implementation task, and review the diff and test evidence.

Keep the runtime dependency-free beyond Python 3.11 or later and the Codex CLI.
Preserve the documented safety boundaries, stable exit codes, run artifacts,
provider contract, and public command behavior.

Do not commit, push, install dependencies, use the network, run migrations, or
perform external actions unless the user requests a separate authorized workflow.
