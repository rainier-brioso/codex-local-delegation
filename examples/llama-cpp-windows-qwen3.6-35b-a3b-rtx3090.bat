@echo off
setlocal

rem Example only: replace these two paths with your existing installation and model.
rem Codex Local Delegation never reads these values or starts this process.
set "SERVER_EXE=C:\path\to\llama-server.exe"
set "MODEL_PATH=C:\path\to\Qwen3.6-35B-A3B-UD-Q4_K_S.gguf"

"%SERVER_EXE%" ^
  --model "%MODEL_PATH%" ^
  --host 127.0.0.1 ^
  --port 8089 ^
  --n-gpu-layers 99 ^
  --parallel 1 ^
  --ctx-size 65536 ^
  --cache-type-k q8_0 ^
  --cache-type-v q4_0 ^
  --batch-size 2048 ^
  --ubatch-size 512 ^
  --flash-attn on ^
  --threads 12 ^
  --jinja

endlocal
