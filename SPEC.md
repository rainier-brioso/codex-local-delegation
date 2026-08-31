# Codex Local Delegation

## Purpose

Provide a reusable, installable personal Codex plugin that keeps Codex Desktop as
the analyst and reviewer while delegating implementation work to a local model
exposed by an already-running, Responses-compatible local inference
server. Version 1 recognises Ollama and `llama.cpp` and also accepts an explicit
compatible endpoint.

The plugin must work from any Codex Desktop session and any opted-in repository.
It must not change the model used by the Desktop analyst session.

## Product name

- Plugin package: `codex-local-delegation`
- User-facing skill: `$local-delegate`
- Local Codex CLI profile: `local-developer`

## Roles

| Role | Runtime | Responsibility |
| --- | --- | --- |
| Analyst / reviewer | Codex Desktop (OpenAI model) | Understand the request, inspect the repository, create a bounded task brief, review the resulting diff and test evidence. |
| Developer | Codex CLI using a local model | Read the brief, edit files, run the requested tests, and report the outcome. |
| Local inference server | User-managed Ollama, `llama.cpp`, or compatible service | Serve the selected model through a Responses-compatible loopback endpoint. |

## Intended workflow

1. The user opens a repository in Codex Desktop and requests work.
2. Codex Desktop analyses the task and writes a bounded handoff file.
3. The `$local-delegate` skill invokes the bundled PowerShell runner.
4. The runner starts `codex exec` with the `local-developer` profile in the target
   repository.
5. The local model developer changes only files within the stated scope, runs
   the requested checks, and leaves its summary in the handoff directory.
6. Codex Desktop inspects the diff and evidence. It either reports completion
   or creates a narrowly scoped repair handoff.

```text
Codex Desktop (analyst / reviewer)
  -> $local-delegate skill
    -> run-local-developer.ps1
      -> codex exec --profile local-developer
        -> existing local Responses endpoint
          -> Ollama, llama.cpp, or compatible service
            -> user-selected local model
```

## Non-goals for version 1

- No background job queue, multi-task dashboard, or status service.
- No automatic commits, pushes, releases, or other external actions.
- The plugin never downloads, installs, configures, starts, stops, upgrades, or
  removes an inference runtime or model. Local model operations remain entirely
  user-managed and outside the project.
- No replacement of Codex Desktop's configured OpenAI model.
- No MCP server. A synchronous runner is sufficient for a single developer
  model; an MCP/job system can be added later if asynchronous delegation is
  needed.

## Plugin layout

```text
codex-local-delegation/
  .codex-plugin/
    plugin.json
  skills/
    local-delegate/
      SKILL.md
  scripts/
    configure-provider.ps1
    run-local-developer.ps1
    doctor.ps1
    lib/
      LocalDelegation.Common.ps1
  templates/
    workspace-AGENTS.md
    local-delegate.toml
    local-developer.config.toml.example
  schemas/
    developer-result.schema.json
  tests/
    mock-provider.ps1
    run-tests.ps1
  SPEC.md
  README.md
  CONTRIBUTING.md
  SECURITY.md
  LICENSE
```

### `$local-delegate` skill

The skill provides the operating procedure; it is not the local model runtime.
It will:

1. Confirm the task is bounded and has acceptance criteria.
2. Create `.codex/local-handoffs/<task-id>/request.md` in the target repository.
3. Record the repository baseline and whether pre-existing changes overlap the
   allowed scope. Overlap requires explicit analyst/user approval before the run.
4. Call `run-local-developer.ps1` with the repository and handoff path.
5. Review the baseline-relative diff, structured result, and test evidence.
6. Escalate unclear, unsafe, out-of-scope, or failing work rather
   than silently widening the task.

### `run-local-developer.ps1`

The runner is the developer harness. It will:

- require an existing repository path and handoff file;
- require a Git worktree and record its starting `HEAD`, status, changed paths,
  and snapshots needed to distinguish pre-existing work from developer changes;
- locate `codex` from `LOCAL_DELEGATE_CODEX_BIN` or `PATH`;
- use the state-owned, isolated `local-developer` profile and `CODEX_HOME` rather
  than inheriting the Desktop user's hooks, MCP servers, or provider settings;
- invoke `codex exec` with an explicit repository working directory,
  automatic approval review (which supplies workspace-write sandboxing), network
  disabled, ephemeral session storage, JSONL output, a configurable hard
  wall-clock timeout (60 minutes by default), and a configurable inactivity
  timeout (15 minutes by default, with zero disabling inactivity detection);
- set `LOCAL_DELEGATION_ACTIVE=1` for the child process;
- pass an explicit developer prompt that requires reading the handoff,
  implementing only its scope, running the prescribed checks, and reporting
  the changed files and results;
- preserve the authoritative baseline and all Codex CLI output in the
  state-owned run directory, then mirror review copies into the handoff directory;
- compare every resulting changed path with the handoff allowlist and protected
  paths, including untracked files, renames, submodules, symlinks, and junctions;
- verify that `HEAD` and Git refs were not changed;
- permit only one active delegation per repository by using a lock file; and
- return a documented non-zero exit code for configuration, invocation,
  timeout, verification, or scope-policy failure.

Version 1 never authorises the worker to commit, push, rewrite Git history,
perform destructive resets, install dependencies, use the network, or take
external actions. Such work must occur as a separate user-authorised workflow,
outside this runner.

The version 1 runner edits the requested working tree directly. It does not
automatically roll back a failed or policy-violating run because rollback could
destroy pre-existing user work. Instead, it preserves and reports the complete
baseline-relative partial diff for analyst review. Isolated worktree execution
may be added later.

### Run artifacts and exit contract

Each run uses an authoritative directory outside the delegated workspace:

```text
<LOCAL_DELEGATE_HOME>\run\<repository-id>\<task-id>\
  request.md
  baseline.json
  baseline-files\
  events.jsonl
  result.json
  stderr.log
  runner.json
```

`repository-id` is a stable hash of the canonical repository path. Before
launching the worker, the runner copies the handoff and the contents of any
pre-existing changed files needed for comparison into this directory. The child
must not receive write access to it. After the run, the runner mirrors
`result.json` and `runner.json` into the repository handoff directory for easy
review; the state-owned records remain authoritative.

Task identifiers must match `[a-z0-9][a-z0-9-]{0,63}`. `result.json` must
conform to `schemas/developer-result.schema.json` and contain changed files,
implementation summary, commands run, outcomes, known limitations, and
follow-up needs. The runner must treat the model's claimed test result as
evidence, not proof; the analyst decides whether tests must be rerun independently.

Exit codes are stable: `0` success, `10` invalid input/configuration, `20`
endpoint/profile failure, `30` developer process failure, `31` timeout, `40`
verification failure, and `50` scope or Git-policy violation.

### `doctor.ps1`

The diagnostics script does not modify an opted-in repository. It validates:

- the `codex` executable is available;
- the `local-developer` profile file exists and is syntactically present;
- the configured local endpoint is reachable and identifies as the expected
  provider when a provider-specific identity endpoint is available;
- the configured model name is reported by the endpoint;
- the endpoint completes a streamed Responses request and a
  stateless tool-call/tool-output round trip required by `codex exec`.

It must give actionable errors without printing credentials or secrets.
Any capability probe that needs filesystem writes uses a disposable directory
under the managed tool state root and removes only that exact directory afterward.

## Local provider integration

`codex exec` is the developer agent harness. Its custom provider protocol is the
Responses API. The plugin therefore standardises a capability contract, not an
inference runtime, model format, launcher, or adapter implementation.

### Discovery and explicit configuration

`configure-provider.ps1` records an already-running local provider. It accepts
an explicit provider, base URL, and model identifier. Explicit configuration
always takes precedence and is never replaced by discovery.

When no endpoint is configured, discovery checks only these conventional
loopback locations:

| Provider candidate | Default origin | Identity and model checks |
| --- | --- | --- |
| Ollama | `http://127.0.0.1:11434` | `/api/version`, `/api/tags` |
| llama.cpp | `http://127.0.0.1:8080` | `/health`, `/v1/models` |

Discovery is an HTTP capability probe, not a general port scan. An accepting TCP
port alone is insufficient. The tool must identify the service and validate the
selected model. It must not probe non-loopback hosts, scan additional ports, or
inspect running processes, command lines, installation directories, or model
files.

If exactly one compatible candidate is found, configuration may select it. If
multiple candidates are compatible, configuration must report them and require
an explicit selection. If none is compatible, configuration exits with an
actionable message showing the supported defaults and the explicit endpoint
option.

Provider discovery does not establish Codex compatibility. Before delegation,
`doctor.ps1` must complete a streamed `/v1/responses` request and a harmless,
stateless tool-call/tool-output round trip using the selected model. The second
request repeats the function call alongside its output instead of relying on
`previous_response_id`, which is not required by Codex's HTTP Responses flow.
A provider that fails this contract is rejected even if its health and
model-list endpoints succeed.
An adapter or newer provider version may be used, but installing and operating it
is outside this project's scope.

### Managed tool state

The plugin repository remains source-only. Tool configuration and run artifacts
are stored outside the repository. The state root is read from
`LOCAL_DELEGATE_HOME`; when unset it defaults to
`$HOME\.cache\codex-local-delegation`:

```text
<LOCAL_DELEGATE_HOME>\
  config\
    provider.json
  logs\
  codex-home\
    local-developer.config.toml
    model-catalog.json
  run\
```

`configure-provider.ps1` writes only `provider.json` and the isolated Codex
configuration beneath this state root. `provider.json` records the provider
kind, canonical base URL, model identifier, whether the choice was explicit or
discovered, and the last successful doctor result. It contains no runtime path,
model path, launcher command, or runtime performance flags.

Example profile concept:

```toml
# <LOCAL_DELEGATE_HOME>\codex-home\local-developer.config.toml
model_provider = "local-developer"
model = "<selected-model-id>"
model_catalog_json = "<LOCAL_DELEGATE_HOME>\\codex-home\\model-catalog.json"
model_reasoning_summary = "none"
web_search = "disabled"

[features]
apps = false
remote_plugin = false
multi_agent = false
plugins = false
skill_search = false

[model_providers.local-developer]
name = "Local model provider"
base_url = "<selected-responses-base-url>"
wire_api = "responses"
requires_openai_auth = false
```

The exact model identifier is explicitly configured or selected from the
provider's reported models. Automatic selection is allowed only when exactly one
model is available; otherwise configuration requires a model choice. The
optional context-window value describes the selected model to Codex and defaults
to 32768; it does not change the inference server's runtime configuration. The
generated model catalog selects Codex's function-based `shell_command` tool and
turns off unsupported reasoning and parallel-tool metadata. The final developer
response is prompted as JSON and validated locally against
`schemas/developer-result.schema.json`; the runner does not depend on a
provider-specific grammar implementation. The
runner sets the child process's `CODEX_HOME` to the state-owned `codex-home`
directory. The Desktop-wide `~/.codex/config.toml` remains unchanged. Installing an optional
copy of the profile into the normal Codex home is a separate, confirmed action
for manual troubleshooting and is not required by the plugin.

## Per-workspace opt-in

An opted-in repository contains only policy and optional settings, not a copy
of the plugin scripts:

```text
repository/
  AGENTS.md
  .codex/
    local-delegate.toml       # optional overrides
    local-handoffs/           # generated, normally gitignored
```

Suggested `AGENTS.md` policy:

```md
# Local developer workflow

Use `$local-delegate` for implementation work that changes source code, tests,
configuration, or documentation.

When the environment variable `LOCAL_DELEGATION_ACTIVE` is `1`, you are the
delegated developer. Implement the supplied handoff directly and never invoke
`$local-delegate` or another delegation workflow.

Act as analyst and reviewer: define scope and acceptance criteria, delegate the
bounded implementation task, then review the diff and test evidence.

Do not commit, push, or perform external actions unless the user explicitly
requests them.
```

Repository-specific `AGENTS.md` files may add canonical test commands,
architectural constraints, and protected paths. The optional TOML file may set
the hard and inactivity timeouts, default test command, and paths that require
analyst review. Explicit runner timeout flags override repository values. The
state-owned worker profile is not replaceable by repository configuration.

## Handoff format

Every generated handoff must contain:

- Task identifier and timestamp.
- Repository root and allowed file scope.
- Problem statement and desired outcome.
- Explicit acceptance criteria.
- Required test/verification commands.
- Constraints, non-goals, and protected files.
- Network access, dependency installation, migrations, commits, and external
  actions, all explicitly recorded as false. The version 1 runner rejects a
  handoff that marks any of them true.
- A completion-report template: changed files, implementation summary, tests
  run, test outcome, known limitations, and follow-up needs.

## Safety and quality rules

- Delegation is a request for implementation, not permission to widen scope.
- The analyst reviews all diffs before presenting work as complete.
- Generated handoffs and output logs are ignored by Git unless a repository
  explicitly chooses to retain them.
- Setup documents an opt-in `.git/info/exclude` entry for
  `.codex/local-handoffs/`; the runner warns when artifacts are visible to Git
  but does not silently modify ignore rules.
- The runner uses an explicit working directory and never executes against a
  parent workspace by accident.
- Resolved allowed paths must remain beneath the repository root after symlink
  and junction resolution.
- Endpoint URLs default to loopback addresses only. Version 1 rejects a
  non-loopback endpoint unless a future version defines a separate explicit
  trust and authentication policy.
- The plugin never reads, writes, moves, hashes, or deletes model weights and
  never manages inference-server processes.
- Secrets are read from the local environment if ever required; they are not
  stored in repository templates or logged.

## Installation design

The project is a personal Codex plugin:

1. Install it once through the personal plugin marketplace.
2. The `$local-delegate` skill then becomes available in all Codex Desktop and
   Codex CLI sessions on this machine.
3. Add the small opt-in `AGENTS.md` and optional TOML file to repositories that
   should use delegation.
4. Run `doctor.ps1` after configuration and after provider, model, or Codex CLI
   upgrades.

The first-time configuration sequence is:

```powershell
cd <path-to-codex-local-delegation>
.\scripts\configure-provider.ps1
.\scripts\doctor.ps1
```

This writes only tool configuration beneath `LOCAL_DELEGATE_HOME`. It does not
modify the provider, its model storage, or its process. An optional profile copy
into the normal Codex home requires a separate confirmation.

The plugin source remains a normal Git repository so its scripts, templates,
and release notes can be versioned and updated independently of application
repositories.

## Milestones

### M1 — Scaffold and documentation

- Create valid plugin manifest and personal marketplace entry.
- Add this specification, README, and workspace templates.

### M2 — Synchronous delegation

- Implement the skill and PowerShell runner.
- Implement handoff creation and output capture.
- Add baseline capture, recursion prevention, scope validation, locking,
  timeout handling, stable exit codes, and tests for each failure mode.

### M3 — Local-stack diagnostics

- Implement `doctor.ps1`.
- Add provider discovery, the state-owned isolated `local-developer` profile, and the
  Responses/tool-calling capability contract.
- Test an end-to-end change in a disposable repository.

### M4 — Optional enhancements

- Add explicit repair-loop support.
- Add isolated-worktree execution and reviewed patch application.
- Consider a job queue/MCP service only if background execution, cancellation,
or multiple local developers becomes necessary.

## Acceptance criteria for version 1

1. The plugin installs cleanly and exposes `$local-delegate`.
2. A Desktop session can use it without switching away from its OpenAI model.
3. Given a valid, already-running Ollama, llama.cpp, or explicitly configured
   compatible endpoint, the runner starts a local-model-backed `codex exec` task in the
   requested repository.
4. The task receives a constrained handoff, produces a developer report, and
   leaves an auditable log.
5. A failed endpoint, profile, or developer run produces a clear error and
   preserves an auditable baseline-relative partial diff without automatic
   rollback or loss of pre-existing user changes.
6. The analyst can review the resulting diff and verification evidence before
   declaring the task complete.
7. Out-of-scope changes, Git ref changes, recursion attempts, and unapproved
   network or external actions are reported as policy failures.
8. Configuration discovers compatible services only at the Ollama and llama.cpp
   loopback defaults when no endpoint is explicit; it performs no general scan.
9. The plugin performs no inference-runtime or model lifecycle operations.

## Decisions for version 1

1. Inference runtimes and model weights are entirely user-managed. The plugin
   owns only delegation configuration, isolated Codex configuration, and run
   artifacts.
2. Explicit provider configuration wins. Otherwise, discovery checks Ollama at
   `127.0.0.1:11434` and llama.cpp at `127.0.0.1:8080`, identifies the services,
   and requires an explicit choice if more than one is compatible.
3. Every provider must satisfy the streamed Responses and tool-calling
   capability contract. Health or port availability alone is not sufficient.
4. Provider listeners are loopback-only in version 1. LAN and remote endpoints
   are out of scope.
5. M1 creates the personal marketplace entry so installation can be exercised
   during development.
6. The worker edits the current worktree directly, preserves partial changes on
   failure, and never performs automatic rollback. Isolated worktrees are a
   post-v1 enhancement.
7. Delegated runs use an isolated state-owned `CODEX_HOME`, a 60-minute default
   timeout, no network, and one active run per repository.
