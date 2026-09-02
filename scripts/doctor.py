#!/usr/bin/env python3
"""Diagnostics script: verify Codex CLI, profile, and provider compatibility."""

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

from local_delegation.common import (
    get_ld_state_root,
    initialize_ld_state_root,
    get_ld_provider_probe,
    read_ld_provider_config,
    write_ld_utf8_file,
    get_ld_provider_config_path,
    http_get,
    http_post,
    convert_from_ld_json_response,
    inspect_ld_codex_cli,
    CODEX_COMPATIBILITY_CONTRACT_VERSION,
)


def test_streaming_responses(responses_url: str, model: str, timeout_seconds: int) -> None:
    """Test that the provider handles streamed Responses output."""
    import urllib.request

    payload = {
        "model": model,
        "input": "Reply with the single word ready.",
        "stream": True,
        "max_output_tokens": 16,
    }
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        responses_url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
        if resp.status != 200:
            body = resp.read().decode("utf-8")
            raise RuntimeError(
                f"Streaming Responses probe returned HTTP {resp.status}: {body}"
            )
        content_type = resp.headers.get("Content-Type", "").split(";", 1)[0].strip()
        if content_type != "text/event-stream":
            raise RuntimeError(
                f"Streaming Responses probe returned '{content_type}' instead of text/event-stream."
            )
        saw_data = False
        for line in resp:
            decoded = line.decode("utf-8").strip()
            if decoded.startswith("data:") and len(decoded) > 5 and decoded[5:].strip():
                saw_data = True
    if not saw_data:
        raise RuntimeError("Streaming Responses probe returned no SSE data events.")


def test_tool_round_trip(responses_url: str, model: str, timeout_seconds: int) -> None:
    """Test stateless tool-call/tool-output continuation."""
    first_payload = {
        "model": model,
        "input": "Call local_delegate_doctor_echo exactly once with value set to ping. Do not answer normally.",
        "stream": False,
        "tool_choice": {"type": "function", "name": "local_delegate_doctor_echo"},
        "tools": [
            {
                "type": "function",
                "name": "local_delegate_doctor_echo",
                "description": "A harmless compatibility probe.",
                "strict": True,
                "parameters": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["value"],
                    "properties": {
                        "value": {"type": "string"},
                    },
                },
            }
        ],
    }
    first = convert_from_ld_json_response(
        http_post(responses_url, first_payload, timeout_seconds)
    )
    calls = [
        o for o in first.get("output", [])
        if isinstance(o, dict) and o.get("type") == "function_call"
        and o.get("name") == "local_delegate_doctor_echo"
        and o.get("call_id")
    ]
    if not calls:
        raise RuntimeError(
            "Provider did not return the required Responses function_call with a call_id."
        )
    call = calls[0]

    second_payload = {
        "model": model,
        "input": [
            {
                "type": "function_call",
                "name": call["name"],
                "call_id": call["call_id"],
                "arguments": call["arguments"],
            },
            {
                "type": "function_call_output",
                "call_id": call["call_id"],
                "output": json.dumps({"value": "pong"}),
            },
        ],
        "stream": False,
    }
    second = convert_from_ld_json_response(
        http_post(responses_url, second_payload, timeout_seconds)
    )
    if not second.get("id") or not second.get("output"):
        raise RuntimeError("Provider did not complete the tool-output continuation.")


def find_codex_on_path() -> str:
    """Find the Codex executable using the runner-compatible override or PATH."""
    import shutil
    override = os.environ.get("LOCAL_DELEGATE_CODEX_BIN")
    codex = override or shutil.which("codex")
    if codex and not os.path.isfile(codex) and override:
        raise RuntimeError(f"LOCAL_DELEGATE_CODEX_BIN does not exist: {codex}")
    if codex is None:
        raise RuntimeError("Codex CLI was not found on PATH.")
    return codex


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Doctor: verify Codex and provider compatibility.")
    parser.add_argument("--state-root", default=None, help="State root directory.")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout for compatibility probes (default: 120).",
    )
    args = parser.parse_args(argv)
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be at least 1")

    try:
        resolved_state_root = get_ld_state_root(args.state_root)
        initialize_ld_state_root(resolved_state_root)

        # Check Codex CLI
        codex_path = find_codex_on_path()

        # Check profile
        profile_path = os.path.join(resolved_state_root, "codex-home", "local-developer.config.toml")
        if not os.path.isfile(profile_path):
            print(
                f"Error: Isolated local-developer profile not found: {profile_path}",
                file=sys.stderr,
            )
            return 20

        # Read provider config
        configuration = read_ld_provider_config(resolved_state_root)
        probe = get_ld_provider_probe(
            configuration["provider"],
            configuration["origin"],
            min(args.timeout_seconds, 15),
        )
        if configuration["model"] not in probe["models"]:
            print(
                f"Error: Configured model '{configuration['model']}' is no longer "
                f"reported by the provider.",
                file=sys.stderr,
            )
            return 20

        codex_info = inspect_ld_codex_cli(codex_path)

        # Probe streaming Responses
        responses_url = configuration["responsesBaseUrl"].rstrip("/") + "/responses"
        print(f"Provider: {probe['identity']}")
        print(f"Model: {configuration['model']}")
        print("Checking streamed Responses output...")
        test_streaming_responses(responses_url, configuration["model"], args.timeout_seconds)
        print("Checking tool-call/tool-output continuation...")
        test_tool_round_trip(responses_url, configuration["model"], args.timeout_seconds)

        # Record success
        configuration["lastDoctor"] = {
            "status": "passed",
            "checkedAt": datetime.now(timezone.utc).isoformat(),
            "codexPath": codex_info["path"],
            "codexVersion": codex_info["version"],
            "approvalMode": codex_info["approval_mode"],
            "compatibilityContractVersion": CODEX_COMPATIBILITY_CONTRACT_VERSION,
            "providerIdentity": probe["identity"],
        }
        config_path = get_ld_provider_config_path(resolved_state_root)
        write_ld_utf8_file(config_path, json.dumps(configuration, indent=2) + "\n")
        print("Doctor passed. Local provider is ready for delegation.")
        return 0

    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 20


if __name__ == "__main__":
    sys.exit(main())
