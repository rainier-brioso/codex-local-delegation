---
name: local-delegate
description: Delegate a bounded implementation task to a configured local model provider, then review the resulting diff and evidence. Use for source, test, configuration, or documentation changes in an opted-in Git repository. Do not use for analysis-only requests or tasks requiring network access, dependency installation, commits, pushes, migrations, or external actions.
---

# Local Delegate

Keep the current Codex session in the analyst and reviewer role. Use the bundled
runner to give implementation work to the configured local model developer.

Before running any bundled file, resolve this `SKILL.md` to an absolute path and
derive the plugin root as its grandparent directory. Resolve all paths below
against that plugin root. Do not resolve them against the user's repository or
assume the plugin source checkout is the current working directory.

## Preconditions

- Work only in a Git repository that opts in through `AGENTS.md`.
- Do not invoke this workflow when `LOCAL_DELEGATION_ACTIVE=1`.
- Require a bounded outcome, explicit acceptance criteria, allowed paths, and
  verification commands.
- Stop if the task needs network access, dependency installation, commits,
  pushes, migrations, destructive Git actions, or another external side effect.
- Run `<plugin-root>/scripts/doctor.ps1` if provider compatibility has not been verified since
  the provider, selected model, or Codex CLI changed.

## Workflow

1. Inspect the repository and identify pre-existing changes that overlap the
   proposed allowed paths. Obtain explicit approval before continuing through an
   overlap.
2. Create `.codex/local-handoffs/<task-id>/request.md` from
   `<plugin-root>/templates/handoff-request.md`. Use a lowercase task identifier matching
   `[a-z0-9][a-z0-9-]{0,63}`.
3. Call `<plugin-root>/scripts/run-local-developer.ps1` with the repository, handoff path, every
   allowed path, every protected path, and the required verification commands.
4. Review the baseline-relative diff, `result.json`, `runner.json`, and command
   evidence. Treat model-reported checks as evidence, not proof.
5. Report completion only when the diff remains in scope and the acceptance
   criteria are demonstrated. Otherwise explain the failure or create one
   narrowly scoped repair handoff.

The runner deliberately preserves partial changes on failure. Never perform an
automatic reset or cleanup that could remove the user's pre-existing work.
