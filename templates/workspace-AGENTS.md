<!-- codex-local-delegation:start -->
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
<!-- codex-local-delegation:end -->
