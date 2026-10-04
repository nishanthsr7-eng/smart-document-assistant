# Security Policy

## Reporting a vulnerability

Please do not open a public issue. Report it privately through GitHub's
[private vulnerability reporting](https://github.com/nishanthsr7-eng/smart-document-assistant/security/advisories/new).
Include the affected endpoint or component, steps to reproduce, and the impact you expect.

You should get an acknowledgement within 7 days. Fixes are released on the `main` branch.

## Supported versions

Only the latest release on `main` receives security fixes.

## Scope

Tenant isolation, authentication, prompt injection through uploaded documents or questions, and
anything that exposes another tenant's data are all in scope. The hardening already in place is
described in [docs/operations.md](docs/operations.md#security-hardening).
