# OSS v0 roadmap — cut to a core, shrink the surface, publish the docs

**Status:** design approved in conversation 2026-09-06 (8 decisions, Ryo Ochi). Supersedes
`rstaff/var/specs/ontos/v1-0-0.md` and the three planned v1 ledgers.
**Repo:** `github.com/ryoochi0112/ontary` (local `~/ontary`). Base: `main` at 0.10.0 + pre-v1
hygiene, `make verify` green. `ontary 0.10.0` is already on PyPI.
**Maintainer model:** one contributor (Ryo). Nothing below assumes outside users, reviewers,
or issue traffic. The roadmap after 0.11.0 is driven by Ryo's own use of the SDK.

## 1. Why the roadmap changes

The v1 plan was written for an org-private SDK with a consumer that had to cross minors
without reading a migration guide. That premise is gone: the SDK is now a personal public
project. A production-ready `1.0.0` with a non-breaking-minor contract, an upgrade-fixture
ladder, and a release gate is weight without a beneficiary. The replacement goal is a small
SDK that one person can read, maintain, and demonstrate: **OSS v0 = minimal viable features,
a public surface a stranger can learn in one sitting, and a docs site.**

## 2. Decisions (recorded for the spec, all human)

| # | Question | Decision |
|---|---|---|
| Q1 | What happens to the existing code | **Cut to a core AND shrink the public surface and docs** (B + C) |
| Q2 | Which subsystems are core | **Tier 1 + audit + Postgres; CLI kept** |
| Q3 | CLI shape | **`serve`, `validate`, `version`**; `diagnose` stays as a core module |
| Q4 | Version and 0.x machinery | **`0.11.0`; fixture ladder, honesty job, v1 gate deleted; compatibility becomes one paragraph** |
| Q5 | Docs target | **README + design guide + API reference + storage, EN and JA kept for the two big docs** |
| Q6 | GitHub Pages | **MkDocs Material + `mkdocs-static-i18n`, deployed by a workflow** |
| Q7 | Finish line | **Stranger test + trimmed `examples/tickets` e2e + live Pages site** |
| Q8 | Sequencing | **Two PRs: one subtractive, one additive** |

## 3. PR 1 — the cut (`chore/oss-v0-cut`, lands under `[Unreleased]`)

### 3.1 Kept modules (the core)

`authoring`, `declarations`, `model`, `typesys`, `ontology`, `actions`, `functions`,
`_typed_api`, `scope`, `security`, `query`, `client`, `errors`, `audit`, `diagnose`,
`ingest` (bulk load; decided 2026-09-06, keeps `client.ingest`/`ingest_links`),
`mcp_server`, `cli`, `testing`, and `store/` with `inmemory`, `sqlite`, `postgres`, `_sql`,
`_shared`, `protocol`, `schema`, `values`.

`meta` is kept (core registry). `store/migration` is load-bearing (it issues the SQLite DDL) and is
collapsed to a create-or-refuse path like Postgres: no in-place migration of pre-0.11 files. The
storage envelope (measured constants, `STORAGE_ENVELOPE_EXCEEDED`, `diagnose(store=)`) is removed
with the rest of the operator-scale tooling; the design-guide lints in `diagnose` stay.
Implementation plan: `specs/plans/2026-09-06-oss-v0-cut.md`.

### 3.2 Deleted

- Modules: `connect/*`, `effects`, `outbox`, `outbox_drain`, `erase`, `migrate`,
  `upcast`, `explain`, `fingerprint`, and the conditional modules above when unused.
- CLI subcommands `explain` and `erase`.
- Tests of every deleted module, plus `test_upgrade_fixtures.py`, `test_v1_gate_coverage.py`,
  `test_release_workflow.py`, and `tests/fixtures/upgrade/*` (writers and fixtures).
- CI: the `upgrade-fixture-honesty` job. The `verify`, `postgres`, and `package` jobs stay.
- Extras: `dlt` and `bq`. `mcp`, `postgres`, and `dev` stay.
- Docs: `v1-gate.md`, `connectors.md`, `effects.md`, `releasing.md`, `authority.md`.
  `queries.md` is folded into `api-reference.md`; `cookbook.md` is folded into
  `examples/tickets/README.md`.
- The `Compatibility` project URL in `pyproject.toml`.

Git tags `v0.7.0`–`v0.10.0` stay on the remote as history. The `ontos-fixture-writer` uuid5
seed spelling no longer matters once the writers are gone.

### 3.3 Public surface

`ontary.__all__` drops from 58 names to about 35. Removed families: connectors
(`BaseConnector`, `CanonicalBatch`, `CanonicalRecord`, `MappingSpec`, `RawTables`, `Source`,
`run_pipeline`), effects and outbox (`EffectDispatcher`, `EffectHandle`, `EffectMeta`,
`EffectPayload`, `OutboxRecord`, `DrainReport`, `RetryPolicy`), and anything else only
reachable from a deleted module. `test_typing_surface.py` and the doc tests pin the new list.

### 3.4 Reference app

`examples/tickets/` loses `connector.py`, `run_connector.py`, `run_effects.py`. `fixtures.py`
seeds the store directly. `run_mcp.py` and `test_examples_tickets_e2e.py` stay and remain the
example's gate.

### 3.5 CLAUDE.md and compatibility

The design pillars become four: store each fact once and derive with Functions; Actions are
business verbs owning a full transition; security is declared (`ScopePolicy`, min-N,
`Sensitivity`) and enforced by the engine with `AuditEntry` as the audit; one object type per
real-world entity. The connector pillar and the `migrate_object_type`/upcaster clause go.

`docs/compatibility.md` becomes one paragraph: any 0.x minor may break; the CHANGELOG
"Removed" section is the migration guide. The `Development Status :: 3 - Alpha` classifier
stays.

### 3.6 Done when

`make verify` is green, the Postgres CI job is green, no file under `src/`, `tests/`,
`examples/`, or `docs/` references a deleted module or name, and the CHANGELOG
`[Unreleased]` lists every removed public name under "Removed".

## 4. PR 2 — the additive PR (`feat/oss-v0-docs`, bumps to `0.11.0`)

### 4.1 README (under 200 lines)

One-paragraph pitch; install `pip install "ontary[mcp]"`; a quickstart of about 40 lines that
declares two object types and one action, opens a SQLite store, and serves it over MCP; a
link table to the Pages site, the tickets example, and the design guide. `test_docs.py` pins
the quickstart block so it stays executable.

### 4.2 Docs site

- `mkdocs.yml` at the repo root, theme Material, plugin `mkdocs-static-i18n` with `en`
  default and `ja` second. Pages: Home (README), Quickstart, Ontology design (EN, JA), API
  reference (EN, JA), Storage, MCP serving, Changelog.
- A `docs` dependency group in `pyproject.toml`; `make docs` serves a local preview.
- `.github/workflows/docs.yml` builds on push to `main` and deploys with
  `actions/upload-pages-artifact` + `actions/deploy-pages`.
- Handback: Ryo enables Pages with source "GitHub Actions" in the repo settings. Done looks
  like the site URL returning the Home page.

### 4.3 Stranger-test CI job

The `package` job is extended: build the wheel, install it with `[mcp]` into an empty venv,
run the README quickstart as a script, run the tickets example end to end. Green means a
stranger can follow the README.

### 4.4 Release

`pyproject.toml` version `0.11.0`; CHANGELOG `## [0.11.0] — <date>` with the "Removed" list
first; annotated tag `v0.11.0` on the merge commit; the existing `release.yml` publishes to
PyPI through trusted publishing.

### 4.5 Done when

The stranger-test job, the tickets e2e, and the docs build are green on `main`; the Pages
site is live at the URL in the README; `ontary 0.11.0` is on PyPI.

## 5. Roadmap after 0.11.0

There is no v1 date and no crossing ceremony. Work is picked from Ryo's own use of the SDK
in a real ontology. A feature deleted in PR 1 comes back only when that use needs it, and it
comes back as a small module with a docs page, never as the old code restored wholesale.
Records: `ontos-v1-strategy` memory gets a closing pointer to this spec; a new memory
`ontary-oss-v0` records the eight decisions.

## 6. Out of scope

Restoring any deleted module in either PR; a `1.0.0` plan; SQL pushdown; parent-covers-child
scope; the private Artifact Registry index and everything under `Atrae/ontos`; announcing
the project.

## 7. Testing rule

`make verify` is the offline gate for every commit in both PRs. Doc tests are adjusted in
the same commit as each doc change so the tree is never red between commits. Backend-touching
commits in PR 1 run the Postgres conformance tests locally with `ONTOS_TEST_POSTGRES_DSN`
set or flag "Postgres gate = CI-only" in the review.
