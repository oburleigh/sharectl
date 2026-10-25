# Security

## Reporting a vulnerability

Report it privately through
[GitHub security advisories](https://github.com/oburleigh/sharectl/security/advisories/new).
Do not open a public issue.

## Supported versions

Only the `main` branch receives fixes.

## What the project does to stay safe

- No secrets are needed or stored; there is no `.env`.
- gitleaks runs on every commit; Trivy and pip-audit run in CI.
