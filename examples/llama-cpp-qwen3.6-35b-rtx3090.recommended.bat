@echo off
setlocal EnableExtensions

rem Required environment variables:
rem   LLAMA_SERVER_EXE  Full path to llama-server.exe
rem   LLAMA_MODEL_PATH  Full path to the Qwen3.6-35B-A3B Q4_K_S GGUF
rem
rem Example from PowerShell:
rem   $env:LLAMA_SERVER_EXE = "C:\path\to\llama-server.exe"
rem   $env:LLAMA_MODEL_PATH = "C:\path\to\Qwen3.6-35B-A3B-UD-Q4_K_S.gguf"
rem   .\llama-cpp-qwen3.6-35b-rtx3090.recommended.bat
rem
rem This launcher is user-managed. Codex Local Delegation never reads these
rem variables, starts llama.cpp, or downloads a model.

if not defined LLAMA_SERVER_EXE (
  echo Error: LLAMA_SERVER_EXE is not set.
  exit /b 2
)

if not defined LLAMA_MODEL_PATH (
  echo Error: LLAMA_MODEL_PATH is not set.
  exit /b 2
)

if not exist "%LLAMA_SERVER_EXE%" (
  echo Error: llama-server.exe was not found: "%LLAMA_SERVER_EXE%"
  exit /b 2
)

if not exist "%LLAMA_MODEL_PATH%" (
  echo Error: model GGUF was not found: "%LLAMA_MODEL_PATH%"
  exit /b 2
)

"%LLAMA_SERVER_EXE%" ^
  --model "%LLAMA_MODEL_PATH%" ^
  --host 127.0.0.1 ^
  --port 8080 ^
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
