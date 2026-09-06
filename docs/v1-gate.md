# The v1.0.0 acceptance gate

[Back to the README](../README.md) · [API reference](api-reference.md)

This is the mechanical bar for tagging `v1.0.0`. It records requirements and
decisions; it does not resolve open decisions A, B, or C for the maintainer.

## May `v1.0.0` be tagged?

The answer is **yes if and only if every box below is checked** on the release
candidate. An unchecked box means the tag must wait.

- [ ] **Two real crossings:** the reference app in `examples/tickets/` has
  crossed at least **2 REAL minor upgrades**. Each crossing has the full gate
  green: the coverage test, the reference-app e2e test, and the committed
  upgrade fixtures all pass. A version label without a real crossing and its
  evidence does not count.
- [ ] **Decision A — audience:** the intended audience is resolved and the
  decision is recorded in a durable project record.
- [ ] **Decision B — parent-covers-child:** the parent-covers-child rule is
  resolved and recorded in a durable project record.
- [x] **Decision C — storage scale:** the storage-scale decision is resolved
  and recorded in a durable project record.
- [ ] **Release-candidate gate:** the coverage test is green, and **ALL
  committed fixtures** under `tests/fixtures/upgrade/` are green on this
  release candidate.

The three decisions are deliberately preconditions, not choices made by this
document. The evidence for the two real crossings and the release candidate
must be reviewable alongside the release.

## Decision C — storage scale

**Resolved.** The runtime shape — human decision 2026-09-01, spec
`storage-envelope.md` §5 — is an **advisory envelope with an observable
signal**: no enforced ceiling, no SQL pushdown. `ontology.diagnose(store=...)`
reports a `STORAGE_ENVELOPE_EXCEEDED` `Finding` when a type's row count crosses
the measured envelope for its store's backend, and
`OntologyRuntime.explain_scan` reports rows scanned, returned, and hidden by
scope for one governed read. Neither refuses anything: no new error code, no
ceiling enforced at write or read time, no path that worked at `v0.8.0` fails
at `v0.9.0`. SQL pushdown of `where`/`order_by`/`limit`/aggregates stays
deferred past 1.0 (spec `storage-envelope.md` §4): the measured cost is
dominated by Python-side scope resolution, not SQL-side scanning, so pushdown
would only ever address the smaller share.

The decision is justified by the measured envelope recorded in
`src/ontary/diagnose.py`'s `STORAGE_ENVELOPE` constant and published in full,
with its environment and caveats, at [the storage
envelope](storage.md#storage-envelope): `ObjectStore` (SQLite) holds **32,000
rows** at **0.000031s/row**, a single-sample, single-laptop measurement;
`PostgresStore` (PostgreSQL) holds **1,600 rows** at **0.000609s/row**, the
conservative floor derived across **four** CI runs of the project's own
`postgres:16` service container, 2026-09-02. Neither figure is exactly
reader-reproducible: spec `storage-envelope.md` §3's "fixed hardware" premise
does not hold on a shared CI runner, and `docs/storage.md`'s run-to-run
dispersion note — not a single job id — is now the reproducibility evidence
for the Postgres figure; `ObjectStore`/SQLite remains the less-evidenced of
the two, being local and single-sample.

Decisions A (audience) and B (parent-covers-child) stay unresolved here. B is
deliberately **deferred to 0.10** (spec `storage-envelope.md` §4): it is meant
to be decided from the dogfood evidence 0.9's own crossing produces, not
resolved ahead of it.

## Post-1.0 contract

Tagging `v1.0.0` requires rewriting `docs/compatibility.md`: after that rewrite,
minor releases are non-breaking under the compatibility policy. The
`examples/tickets/` reference app remains the **PERMANENT compatibility gate**
for every post-1.0 release.

**HUMAN RULING (2026-08-31; recorded in the feature ledger):** at major `>= 1`,
the required-fixture list keeps **EVERY 0.x fixture required forever** and
additionally requires a fixture for each 1.x minor starting at `v1.0.0`.
Therefore, **CUTTING `v1.0.0` REQUIRES COMMITTING A `v1.0.0` FIXTURE** as part
of that release. The required-fixture list is never empty.

## Release checklist

For every release:

- Append the new tag to the release table below and commit its fixture under
  `tests/fixtures/upgrade/<tag>/` together with its frozen writer **before the
  next tag**.
- When appending a tag, also extend the `upgrade-fixture-honesty` matrix in
  `.github/workflows/verify.yml`; `make verify` now enforces that they match.
- Restate the **L30 CI-only blind spot** in the release review: the Postgres
  e2e job and the fixture-honesty job run **only in GitHub Actions**;
  `make verify` never runs them, so PR CI is the only pre-merge proof for
  those two jobs.
- Treat the `upgrade-fixture-honesty` job in `.github/workflows/verify.yml` as
  the **enforcement mechanism for the writer-freeze policy**. The job is live:
  it regenerates each committed fixture from its own tag and fails on any
  semantic divergence, which is what enforces that a frozen writer still
  produces its committed fixture.

## Release table

This is the authoritative record of completed majors and their last completed
minors. It is one half of the two-artifact ceiling anchor: the docs test parses
this table and compares it with the independent
`tests/test_upgrade_fixtures.py` `_COMPLETED_MAJOR_LAST_MINOR` literal. Change
both artifacts together when the completed-major ceiling changes.

| Major | Last completed minor |
| --- | --- |
| 0 | v0.9.0 |

## AC4 fixture honesty

The committed fixtures exercise the **UPCASTER CHAIN**, the **FINGERPRINT
CLASSIFICATION**, and **reads/aggregates/audit-vs-expected**. They do **not**
exercise the **SCHEMA-LADDER** leg: `SCHEMA_VERSION` has been `9` unchanged
since `v0.6.0`, and no pinned tag stamps below it. A reader must not infer a
schema-ladder crossing that the gate never made.

**v0.7.0 and v0.8.0** exercise the upcaster chain and the fingerprint
classification both: each is written by an OLDER `examples.tickets` registry
(`Ticket` at version 1, no `Escalation`), so opening it under the current SDK
upcasts every `Ticket` row and appends one `AcceptVersionedOntologyChange`
audit entry. **v0.9.0 exercises neither of those two legs.** Its writer runs
under the SAME registry the fixture is read back by (this crossing's own
`examples.tickets`, `Ticket` already at version 2) -- there is no older shape
left to upcast and no fingerprint drift left to accept, so its `as_written`
and `expected_after_upgrade` sections are identical by construction. It still
exercises reads/aggregates/audit-vs-expected in full, and it becomes the
OLDER fixture the *next* crossing upcasts, the same way v0.7.0/v0.8.0 do for
this one today.
