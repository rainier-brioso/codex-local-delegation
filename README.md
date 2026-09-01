# Codex Local Delegation

An experimental Codex plugin that keeps Codex Desktop in the analyst/reviewer
role while delegating a bounded implementation task to a user-managed local
model endpoint.

The plugin does not install or configure Ollama, llama.cpp, adapters, or model
weights. It discovers conventional loopback endpoints or uses an explicitly
configured endpoint, then verifies Responses API and tool-calling compatibility.
Its isolated Codex profile disables native web search and uses Codex's
function-based `shell_command` tool so Responses-compatible local servers such
as llama.cpp do not need to accept Codex-specific custom tools.

## Requirements

- Python 3.11 or later.
- Git and Codex CLI on `PATH`.
- An already-running model exposed through a compatible local endpoint.
- One of:
  - Ollama on `http://127.0.0.1:11434`;
  - llama.cpp on `http://127.0.0.1:8080`; or
  - an explicit loopback Responses-compatible base URL.

## Install

Add this repository as a Codex marketplace and install the plugin:

```bash
codex plugin marketplace add rainier-brioso/codex-local-delegation
codex plugin add codex-local-delegation@rainier-local-tools
```

Start a new Codex task after installation so the `local-delegate` skill is
loaded. The plugin contains no model weights or inference runtime and does not
download either one.

## Configure the provider

Automatic discovery checks only the two conventional endpoints:

```bash
python scripts/configure_provider.py
```

Explicit configuration always wins:

```bash
python scripts/configure_provider.py \
  --provider LlamaCpp \
  --base-url http://127.0.0.1:8080 \
  --model my-local-model \
  --context-window 128000
```

`--context-window` describes the selected model to Codex; it does not configure
the inference server. It defaults conservatively to 32768 tokens.

Configuration writes only beneath `LOCAL_DELEGATE_HOME`, which defaults to
`$HOME/.cache/codex-local-delegation`.

## Verify compatibility

```bash
python scripts/doctor.py
```

The doctor checks the Codex CLI, provider identity, model availability,
streaming Responses behavior, and a stateless tool-call/tool-output round trip.

## Delegate

Install the plugin during development, opt a repository in with the supplied
`templates/workspace-AGENTS.md`, and invoke `$local-delegate` with a bounded task.
The skill creates a handoff and calls the runner. The runner edits the current
worktree, preserves logs outside it, and rejects out-of-scope changes.

The runner has a 60-minute hard timeout and a 15-minute inactivity timeout by
default. Any stdout or stderr from the local `codex exec` resets the inactivity
clock. Configure repository defaults in `.codex/local-delegate.toml`:

```toml
timeout_minutes = 60
inactivity_timeout_minutes = 15
```

Set `inactivity_timeout_minutes = 0` to disable inactivity detection while
retaining the hard timeout. For one task, `--timeout-minutes` and
`--inactivity-timeout-minutes` override repository configuration.

See [SPEC.md](SPEC.md) for the complete security and behavior contract.

## Development

Run the self-contained checks:

```bash
python -m unittest discover -s tests -v
```

This project is licensed under the MIT License. It does not redistribute or
wrap model weights or inference runtimes; those remain subject to their own
licenses and user-managed installation processes.
