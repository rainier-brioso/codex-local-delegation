# Security policy

## Reporting a vulnerability

Do not publish credentials, private source code, or exploit details in a public
issue. Contact the repository maintainers through the private security-reporting
channel configured by the hosting project.

## Security boundary

The plugin runs a local model as a coding agent inside Codex's workspace-write
sandbox. Version 1 additionally checks changed paths and Git refs after the run,
but these checks are defense in depth rather than a substitute for reviewing the
diff.

The plugin accepts only loopback HTTP provider endpoints. It never installs,
starts, configures, or modifies inference runtimes or model weights. A provider,
adapter, model, or repository may still be untrusted; users should review all
generated changes and test evidence before accepting them.
