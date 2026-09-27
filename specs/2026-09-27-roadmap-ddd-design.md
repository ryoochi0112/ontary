# Roadmap reset — model an operation, get DDD for free

**Status:** decided in conversation 2026-09-27 (5 decisions + 1 framing clarification, Ryo Ochi). Supersedes §5 of
`2026-09-06-oss-v0-roadmap-design.md` ("the roadmap is driven by Ryo's own use").
**Public roadmap:** `docs/roadmap.md`. **Backlog:** GitHub Issues grouped by milestone.

## 1. Why

The mission changes from "a small SDK one person can maintain and demonstrate" to "a highly
useful SDK for managing an operational ontology". Outside developers are now the audience.
The premise: one business operation is one domain, so Domain-Driven Design is the right
structure. It is the SDK's structure, not the user's prerequisite (see §2a). An audit on
2026-09-27 found the governance core sound but the DDD authoring surface weak:

- a primary key is not unique in the stores (a duplicate insert creates a second live row);
- an action cannot target an `unscoped` type (always `SCOPE_DENIED`);
- `ActionContext` is stringly typed, so `mypy --strict` misses misspelled types and fields;
- no Enum / `Literal` / value-object properties, no declared invariants or transitions;
- link endpoints are unchecked; `datetime` is refused on write but returned on read;
- the clock does not reach handlers or stored history.

## 2. Decisions

| # | Question | Decision |
|---|---|---|
| Q1 | Where the roadmap and backlog live | **Public:** `docs/roadmap.md` + GitHub Issues grouped by milestone |
| Q2 | Milestone order | **M0 trust the core → M1 model your operation → M2 examples and docs → M3 agent surface → M4 refactor** |
| Q3 | The string-based `ActionContext` API once the typed one lands | **Deprecate, then remove one minor later** (`DeprecationWarning` in between) |
| Q4 | Actions on `unscoped` targets | **Allowed; `roles=` is the only gate; the audit entry records `scope: unscoped`** |
| Q5 | Domain events | **Minimal in M1:** `emits=[...]`, stored in the action's transaction, readable from the client and audit. No subscribers, webhooks, or outbox — those stay under "Later" |

## 2a. Framing (clarified 2026-09-27)

DDD is the key, but ontary is not a DDD-specific tool. Success = a developer with no DDD
knowledge or experience models their operation and ends up with a sound DDD model.
Consequences:

- Public API and docs use operations language (things, relationships, actions, rules,
  events = "what happened"). DDD vocabulary is confined to one explanation page.
- Good design is the default path; `validate()`/`diagnose()` messages name the fix.
- Acceptance for M1/M2 includes a newcomer stranger run (no DDD background, docs only),
  reviewed against `docs/ontology-design.md`.
- The M1 milestone is named "Model your operation", not "DDD authoring".

## 3. Rules carried forward

- A feature cut in 0.11.0 returns only when an example or a user needs it, as a small module
  with a docs page — never the old code restored wholesale. Q5 is the first such return.
- Examples and docs (M2) are written once, against the M1 API.
- Every milestone item becomes one issue; each issue states a testable "done when".
