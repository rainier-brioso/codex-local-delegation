# Contributing

Contributions are welcome through focused issues and pull requests.

Before submitting a change:

1. Keep inference runtimes and model weights outside this repository.
2. Preserve the loopback-only and no-runtime-management boundaries in `SPEC.md`.
3. Run `./tests/run-tests.ps1` with PowerShell 7 or later.
4. Describe the provider and Codex CLI versions used for any end-to-end test.
5. Do not include credentials, private filesystem paths, model weights, or local
   run artifacts.

Changes to provider compatibility should include a reproducible diagnostic case.
Changes to the runner should include a regression test for the affected safety
invariant.
