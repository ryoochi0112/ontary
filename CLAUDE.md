# ontary

The ontology SDK: author an ontology and get a governed runtime.

## Designing an ontology?

**Before designing or reviewing any ontology on this SDK, read
[docs/ontology-design.md](docs/ontology-design.md)** (EN; 日本語:
[docs/ontology-design.ja.md](docs/ontology-design.ja.md)) — patterns and anti-patterns.

- Store each fact once; derive scores and aggregates with Functions, never store them. A declared snapshot type is the one sanctioned exception.
- Actions are business verbs that own a full state transition—never CRUD setters or one-property micro-actions.
- Bind to a canonical staging schema through connectors, never directly to a vendor source.
- Security is the engine's job: declare `ScopePolicy`, min-N, and `Sensitivity`; do not hand-roll checks. Audit is the engine's `AuditEntry`, never a declared type.
- Use one object type per real-world entity. History is row history plus `migrate_object_type` and versions/upcasters—never a `V2` or `*History` type.

`make verify` is the offline definition of done (`ruff` + `mypy --strict` + `pytest`).

## Repository workflow

This repository lives at `github.com/Atrae/ontary`. The `main` branch is protected; all changes land via PRs. Releases are annotated tags (`vX.Y.Z`) consumed by the `ontology-prototype` repository via a pinned git URL.
