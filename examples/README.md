# Runtime examples

These launchers are optional, user-managed starting points. Codex Local
Delegation does not execute them, inspect their paths, configure an inference
runtime, or download a model.

## RTX 3090 and Qwen3.6-35B-A3B on llama.cpp

[`llama-cpp-qwen3.6-35b-rtx3090.recommended.bat`](llama-cpp-qwen3.6-35b-rtx3090.recommended.bat)
is based on a configuration exercised on a 24 GB RTX 3090. It deliberately:

- listens only on loopback;
- uses one inference slot so a single delegated task owns the available context;
- starts with a 65,536-token context instead of reserving 128,000 tokens;
- keeps the K cache at `q8_0` and the V cache at `q4_0`; and
- uses a smaller micro-batch to reduce peak pressure; and
- limits thinking to 4,096 tokens by default.

Treat this as a measured starting point, not a universal optimum. llama.cpp
builds, CUDA versions, prompts, and offload behavior can change the best values.
Set `LLAMA_SERVER_EXE` and `LLAMA_MODEL_PATH` in the shell that launches it;
no machine-specific path is stored in the launcher. Start the server yourself,
then configure the matching provider metadata:

`LLAMA_REASONING_BUDGET` is optional and defaults to `4096`. Set it to `2048`
for mechanical edits or `8192` for difficult debugging.

```powershell
python scripts/configure_provider.py `
  --provider LlamaCpp `
  --base-url http://127.0.0.1:8080 `
  --model Qwen3.6-35B-A3B-UD-Q4_K_S.gguf `
  --context-window 65536 `
  --auto-compact-token-limit 49152

python scripts/doctor.py
```

The model identifier must exactly match the value reported by `/v1/models`.
The context values describe the already-running server to Codex; they do not
change llama.cpp.
