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
- [x] **Decision A — audience:** the intended audience is resolved and the
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

## Decision A — audience

**Resolved.** The intended audience is **public** — human decision 2026-09-06, at
the fork of `ontos` into `ontary`. `ontary` is distributed from **public PyPI** at
[pypi.org/project/ontary](https://pypi.org/project/ontary/), published by
[`.github/workflows/release.yml`](../.github/workflows/release.yml) through PyPI
trusted publishing on every `vX.Y.Z` tag. This supersedes the pre-fork decision of
2026-09-04 (internal audience, private Artifact Registry index), which applied to
`ontos` and is void for `ontary`.

The git-ref install is **not** replaced by the index: a consumer who does not
resolve from PyPI still installs with
`uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"`. Both paths
are documented side by side at [the README install paths](../README.md#install).

The decision is evidenced by the release gate rather than asserted: a tag reaches
PyPI only after `release.yml`'s `gate` job re-runs `make verify`, builds the
distributions, and installs the wheel into an empty environment. [The release
runbook](releasing.md) is the operational half — publish, confirm, and roll back by
**yanking** the version (PEP 592), never by deleting it.

What this decision does not settle: Decision B stays open, and nothing here changes
runtime behaviour, adds an error code, or narrows what the engine accepts.

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

Decision A (audience) is resolved above. Decision B (parent-covers-child)
stays unresolved here, deliberately **deferred to 0.10** (spec
`storage-envelope.md` §4): it is meant to be decided from the dogfood evidence
0.9's own crossing produces, not resolved ahead of it.

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
- Restate the **L30 CI-only blind spot** in the release review: the
  `postgres` (Postgres e2e), `upgrade-fixture-honesty` (fixture-honesty),
  and `package` (packaging smoke test) jobs run **only in GitHub Actions**;
  `make verify` never runs them, so PR CI is the only pre-merge proof for
  those three jobs.
- Treat the `upgrade-fixture-honesty` job in `.github/workflows/verify.yml` as
  the **enforcement mechanism for the writer-freeze policy**. The job is live:
  it regenerates each committed fixture from its own tag and fails on any
  semantic divergence, which is what enforces that a frozen writer still
  produces its committed fixture.
- The matrix names `v{current}` as soon as `pyproject.toml`'s version bumps,
  which is before that tag can exist. Until the current version's tag is
  pushed, its leg falls back to running against the PR head instead (a
  visible `::warning::` marks the run so the log cannot read as a tagged
  run). Only the current version may fall back this way: a missing tag for
  any earlier version still fails the job outright.

## Release table

This is the authoritative record of completed majors and their last completed
minors. It is one half of the two-artifact ceiling anchor: the docs test parses
this table and compares it with the independent
`tests/test_upgrade_fixtures.py` `_COMPLETED_MAJOR_LAST_MINOR` literal. Change
both artifacts together when the completed-major ceiling changes.

| Major | Last completed minor |
| --- | --- |
| 0 | v0.10.0 |

## AC4 fixture honesty

The fixture SET, across all fixtures together, exercises the **UPCASTER
CHAIN**, the **FINGERPRINT CLASSIFICATION**, and
**reads/aggregates/audit-vs-expected** -- not every fixture individually; a
same-version fixture has no upcast or fingerprint drift of its own to
exercise, see below for which fixture covers which leg. The set does **not**
exercise the **SCHEMA-LADDER** leg at all: `SCHEMA_VERSION` has been `9`
unchanged since `v0.6.0`, and no pinned tag stamps below it. A reader must
not infer a schema-ladder crossing that the gate never made.

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

**The v0.9.0 writer also patches `uuid.uuid4` for the duration of its own
run.** `OpenEscalation` (`examples/tickets/ontology.py`, frozen application
code) calls `ActionContext.insert("Escalation", ...)` with no client-supplied
id -- `OpenEscalationParams` has no id field to set one with -- so the store
mints one via the `uuid.uuid4()` primary-key fallback in
`ontary.store._shared.prepare_insert`, whose own docstring says that fallback
is "deliberately store-owned and is not runtime-seamed; tests that need
deterministic object IDs should pass explicit primary keys." With no primary
key to pass, `tests/fixtures/upgrade/writers/write_v0_9_0.py`'s
`_DeterministicAutoIds` instead monkeypatches the stdlib `uuid.uuid4` name
for the duration of the writer's run, substituting a `uuid5`-derived
deterministic callable so the fixture stays byte-reproducible. This is a
NARROWER seam than the 0.8.0 precedent: `FixedClock`/`SequentialIds` are
public, exported from `ontary.testing` for any caller to use; this
monkeypatch is private to the writer script and, before this paragraph, was
documented only in `_DeterministicAutoIds`'s own docstring. The design fix
this seam works around -- routing store auto-ids through the runtime
`id_factory` the way invocation/effect ids already are -- is deferred, not
attempted here.

**v0.10.0** carries the same seed shape and the same "no upcast, no
fingerprint drift" story as v0.9.0: its writer runs under the SAME registry
the fixture is read back by, so `as_written == expected_after_upgrade` for
both its Tickets and its audit log, exactly as v0.9.0's does. **Unlike
v0.9.0, its writer needs no `uuid.uuid4` patch.** The action-path primary-key
gap the paragraph above describes is closed as of this crossing: an action
handler's `ctx.insert` with no primary key now fills it from the runtime's
configured `id_factory` before the store is ever touched, so
`OpenEscalation`'s Escalation id comes from the same `SequentialIds`
instance every other id in the fixture already does. See
[docs/compatibility.md § Auto-minted ids come from `id_factory` (0.9 →
0.10)](compatibility.md#auto-minted-ids-come-from-id_factory-09--010) for the
seam itself.
