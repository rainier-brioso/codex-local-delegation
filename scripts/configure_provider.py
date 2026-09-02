#!/usr/bin/env python3
"""Configure a local model provider for Codex local delegation."""

import argparse
import json
import os
import sys
import math
from datetime import datetime, timezone

from local_delegation.common import (
    get_ld_state_root,
    initialize_ld_state_root,
    get_ld_provider_probe,
    write_ld_utf8_file,
    get_ld_provider_config_path,
    convert_to_ld_toml_string,
)


def build_profile(
    responses_base_url: str,
    selected_model: str,
    catalog_path: str,
    auto_compact_token_limit: int,
) -> str:
    return (
        f'model_provider = "local-developer"\n'
        f'model = {convert_to_ld_toml_string(selected_model)}\n'
        f'model_catalog_json = {convert_to_ld_toml_string(catalog_path)}\n'
        f'model_reasoning_summary = "none"\n'
        f'model_auto_compact_token_limit = {auto_compact_token_limit}\n'
        f'model_auto_compact_token_limit_scope = "body_after_prefix"\n'
        f'web_search = "disabled"\n'
        f'\n'
        f'[features]\n'
        f'apps = false\n'
        f'remote_plugin = false\n'
        f'multi_agent = false\n'
        f'plugins = false\n'
        f'skill_search = false\n'
        f'\n'
        f'[model_providers.local-developer]\n'
        f'name = "Local model provider"\n'
        f'base_url = {convert_to_ld_toml_string(responses_base_url)}\n'
        f'wire_api = "responses"\n'
        f'requires_openai_auth = false\n'
        f'\n'
        f'[sandbox_workspace_write]\n'
        f'network_access = false\n'
    )


def build_catalog(selected_model: str, context_window: int) -> str:
    catalog = {
        "models": [
            {
                "slug": selected_model,
                "display_name": selected_model,
                "context_window": context_window,
                "truncation_policy": {
                    "mode": "tokens",
                    "limit": int(math.floor(context_window * 0.9)),
                },
                "shell_type": "shell_command",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "base_instructions": (
                    "You are a local coding implementation agent. "
                    "Follow the supplied repository instructions and task handoff. "
                    "Use the shell_command function for repository inspection, "
                    "edits, and verification. The shell is host-native; on Windows "
                    "use PowerShell syntax. Never use cat, bash, sh, heredocs, or "
                    "multiline source embedded in python -c or encoded command "
                    "strings. Prefer focused inspection and short, incremental "
                    "edit commands. Do not delegate work."
                ),
                "supports_parallel_tool_calls": False,
                "experimental_supported_tools": [],
                "supports_reasoning_summaries": False,
                "support_verbosity": False,
                "supported_reasoning_levels": [],
            }
        ]
    }
    return json.dumps(catalog, indent=2) + "\n"


def main(args: list = None) -> int:
    parser = argparse.ArgumentParser(description="Configure a local model provider.")
    parser.add_argument(
        "--provider",
        choices=["Auto", "Ollama", "LlamaCpp", "Custom"],
        default="Auto",
        help="Provider type (default: Auto).",
    )
    parser.add_argument("--base-url", default=None, help="Explicit provider base URL.")
    parser.add_argument("--model", default=None, help="Explicit model name.")
    parser.add_argument(
        "--context-window",
        type=int,
        default=32768,
        help="Context window size (default: 32768).",
    )
    parser.add_argument(
        "--auto-compact-token-limit",
        type=int,
        default=None,
        help=(
            "Compact after this many body tokens; defaults to 75%% of the "
            "configured context window."
        ),
    )
    parser.add_argument("--state-root", default=None, help="State root directory.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=5,
        help="HTTP timeout for discovery probes (default: 5).",
    )
    parsed = parser.parse_args(args)
    if not 4096 <= parsed.context_window <= 1048576:
        parser.error("--context-window must be between 4096 and 1048576")
    if parsed.auto_compact_token_limit is None:
        parsed.auto_compact_token_limit = int(math.floor(parsed.context_window * 0.75))
    if not 1024 <= parsed.auto_compact_token_limit < parsed.context_window:
        parser.error(
            "--auto-compact-token-limit must be at least 1024 and smaller than "
            "--context-window"
        )
    if parsed.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")

    try:
        resolved_state_root = get_ld_state_root(parsed.state_root)
        initialize_ld_state_root(resolved_state_root)

        explicit = parsed.base_url is not None or parsed.provider != "Auto"
        probes = []

        if parsed.base_url is not None:
            if parsed.provider == "Auto":
                print(
                    "Error: -Provider is required when -BaseUrl is supplied. "
                    "Use Ollama, LlamaCpp, or Custom.",
                    file=sys.stderr,
                )
                return 10
            probes.append(
                get_ld_provider_probe(parsed.provider, parsed.base_url, parsed.timeout_seconds)
            )
        elif parsed.provider != "Auto":
            default_url = {
                "Ollama": "http://127.0.0.1:11434",
                "LlamaCpp": "http://127.0.0.1:8080",
            }.get(parsed.provider)
            if default_url is None:
                print("Error: -BaseUrl is required for the Custom provider.", file=sys.stderr)
                return 10
            probes.append(
                get_ld_provider_probe(parsed.provider, default_url, parsed.timeout_seconds)
            )
        else:
            # Discovery mode
            candidates = [
                {"provider": "Ollama", "base_url": "http://127.0.0.1:11434"},
                {"provider": "LlamaCpp", "base_url": "http://127.0.0.1:8080"},
            ]
            for c in candidates:
                try:
                    probes.append(
                        get_ld_provider_probe(c["provider"], c["base_url"], parsed.timeout_seconds)
                    )
                except Exception as exc:
                    print(
                        f"Verbose: {c['provider']} was not compatible "
                        f"at {c['base_url']}: {exc}",
                        file=sys.stderr,
                    )

        if len(probes) == 0:
            print(
                "Error: No compatible provider found. "
                "Start Ollama on 127.0.0.1:11434, llama.cpp on 127.0.0.1:8080, "
                "or supply -Provider and -BaseUrl.",
                file=sys.stderr,
            )
            return 10

        if len(probes) > 1:
            choices = ", ".join(f"{p['provider']} at {p['origin']}" for p in probes)
            print(
                f"Error: Multiple compatible providers found: {choices}. "
                "Select one explicitly with -Provider.",
                file=sys.stderr,
            )
            return 10

        probe = probes[0]
        selected_model = parsed.model

        if selected_model is not None:
            if selected_model not in probe["models"]:
                print(
                    f"Error: Model '{selected_model}' is not reported by "
                    f"{probe['provider']}. Available models: {', '.join(probe['models'])}",
                    file=sys.stderr,
                )
                return 10
        else:
            if len(probe["models"]) != 1:
                print(
                    "Error: Model selection is ambiguous. Supply --model. "
                    f"Reported models: {', '.join(probe['models'])}",
                    file=sys.stderr,
                )
                return 10
            selected_model = probe["models"][0]

        # Write provider config
        config = {
            "schemaVersion": 1,
            "provider": probe["provider"],
            "origin": probe["origin"],
            "responsesBaseUrl": probe["responses_base_url"],
            "model": selected_model,
            "contextWindow": parsed.context_window,
            "autoCompactTokenLimit": parsed.auto_compact_token_limit,
            "selection": "explicit" if explicit else "discovered",
            "configuredAt": datetime.now(timezone.utc).isoformat(),
            "lastDoctor": None,
        }
        config_path = get_ld_provider_config_path(resolved_state_root)
        write_ld_utf8_file(config_path, json.dumps(config, indent=2) + "\n")

        # Write model catalog
        catalog_path = os.path.join(resolved_state_root, "codex-home", "model-catalog.json")
        catalog_content = build_catalog(selected_model, parsed.context_window)
        write_ld_utf8_file(catalog_path, catalog_content)

        # Write profile
        profile_path = os.path.join(resolved_state_root, "codex-home", "local-developer.config.toml")
        profile = build_profile(
            probe["responses_base_url"],
            selected_model,
            catalog_path,
            parsed.auto_compact_token_limit,
        )
        write_ld_utf8_file(profile_path, profile)

        print(f"Configured {probe['identity']}")
        print(f"Model: {selected_model}")
        print(f"Context window: {parsed.context_window}")
        print(f"Auto-compaction limit: {parsed.auto_compact_token_limit}")
        print(f"Responses base URL: {probe['responses_base_url']}")
        print(f"State: {resolved_state_root}")
        print("Run doctor.py before delegation.")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 10


if __name__ == "__main__":
    sys.exit(main())
