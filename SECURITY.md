# Security policy

[← README](README.md)

## Supported versions

`ontary` is pre-1.0. Only the latest `0.x` minor receives security fixes; a fix
ships as a new patch or minor release, never as a backport. See
[docs/compatibility.md](docs/compatibility.md) for the versioning contract.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

https://github.com/ryoochi0112/ontary/security/advisories/new

Do not open a public issue for a security report. You will get an
acknowledgement within 7 days. Once a fix is released, the advisory is
published and the fix is listed in [CHANGELOG.md](CHANGELOG.md).

## What is in scope

The engine enforces `ScopePolicy`, min-N, and `Sensitivity` on behalf of every
adopter, and records every state change as an `AuditEntry`. The following are
security vulnerabilities in `ontary` itself:

- Reading or acting on an object, link, or property past a declared
  `ScopePolicy`, min-N threshold, or `Sensitivity` without the declared grant.
- A mutation (an Action, an ingest, or a store write reachable through the
  public API) that completes without a corresponding `AuditEntry`.
- A tenant boundary crossing in any store backend (in-memory, SQLite,
  Postgres).
- Unsafe deserialization or code execution from an ontology definition, a
  query, or MCP tool input.

Out of scope: an adopter's own ontology that declares a policy too loosely, and
vulnerabilities in dependencies that have their own advisory process (report
those upstream; a bump here follows).
