# Changelog

Notable changes to `ontary`. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/); versioning follows
[docs/compatibility.md](docs/compatibility.md) — pre-1.0, so **a minor bump may break
you**.

`ontary` continues `ontos`, authored at Atrae, Inc. and released to the author on
2026-09-06 for open-source development under MIT. Entries below `[0.10.0]` describe
`ontos` releases; their tags exist here as renamed snapshots of the same source.

## [Unreleased]

Pre-v1 hygiene: closes the remaining repository and CI gaps before the v1.0.0
crossing without changing the package version or the runtime contract.

OSS v0 cut: reduces `ontary` to its core. Every removed public name is listed
under `### Removed`; the next release (0.11.0) is the first to ship without them.

### Removed

<!-- one bullet per removed public name; each task appends here -->

### Changed

- Decision B is recorded in `docs/v1-gate.md`: exact-scope-id match is the v1
  contract; parent-covers-child coverage is opt-in and post-1.0. The `covers_scope`
  and `ScopePolicy` docstrings cite the decision instead of calling it deferred, and
  a contract test pins that a parent-scoped consumer never covers a child-owned row.
- Every GitHub Actions `uses:` entry in `verify.yml` and `release.yml` moved off the
  Node 20 runtime: `checkout` v7, `setup-uv` v10.0.1, `upload-artifact` v7,
  `download-artifact` v8.
- `release.yml` serializes runs per tag (`concurrency`, `cancel-in-progress: false`)
  so a re-push cannot cancel a publish that is already uploading.
- The README links the release runbook. The carried release backlog is re-triaged
  for the PyPI path in `rstaff/var/specs/ontary/post-v1-backlog.md`.

## [0.10.0] — 2026-09-04

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"
```

### Changed

- No change to the published storage envelope this crossing: a governed `aggregate`
  or narrow-scope read still stays under one second below ~32,000 rows on `ObjectStore`
  (SQLite) and ~1,600 rows on `PostgresStore` (PostgreSQL) — the same figures `[0.9.0]`
  published below.
- An action handler's `ctx.insert(obj_type, payload)` now fills a missing primary
  key from the runtime's configured `id_factory` before calling `store.insert`,
  instead of falling through to the store's own `uuid.uuid4()` — the same seam
  invocation and effect ids already come from. A direct `store.insert` call
  (bypassing an `ActionContext`) is unaffected and keeps minting `uuid.uuid4()`.
  See [docs/compatibility.md § Auto-minted ids come from `id_factory` (0.9 →
  0.10)](docs/compatibility.md#auto-minted-ids-come-from-id_factory-09--010).
- `docs/storage.md#storage-envelope` now states its publication rule outright and
  adds an observed-run log, closing a silent SELECTION the doc never disclosed: a CI
  run of the Postgres storage-envelope curve step earns a `#### Run <id>` table only
  when it sets or lowers the published floor, and every run actually read — not only
  the ones that earn a table — is listed by id and its own worst per-row rate
  regardless. Two runs (`33704034596`, `33705109711`) had been read and silently
  dropped from the doc, with nothing anywhere in the tree recording that they ever
  ran; both are now visible in the observed log, and neither moves
  `STORAGE_ENVELOPE["PostgresStore"]` — 1,600 rows at 0.000609s/row, unchanged.
  `OBSERVED_POSTGRES_RUN_IDS` (`tests/test_docs.py`) pins this log the same
  structural way the existing `PUBLISHED_POSTGRES_RUN_IDS` already pins the four run
  tables, and is required to be a superset of it.
- The memo-speedup table under `docs/storage.md#storage-envelope` — `2.45x`/`2.58x`
  on `ObjectStore`, `3.02x`/`3.10x` on `PostgresStore` — was an unattributed pair of
  percentages; it now names its source. The `PostgresStore` pair is run 1's
  (`33623316425`) own n=25,000 measurement, already published in full above it in
  the same section, not a separate benchmark run; the `ObjectStore` pair is labelled
  for what it always was, a single local laptop sample, never CI-produced. Neither
  number changed — this is a provenance correction, not a re-measurement. (The
  `## [0.9.0]` entry below quoting "2.45x-3.10x faster ... on both backends" is
  unaffected: those are the same two figures, and they check out against this now
  fully-attributed source.)
- The `## [0.9.0]` entry below quotes "cutting the store calls a governed
  `aggregate` or narrow-scope read issues from 7 to ~2 per row" as one combined
  figure, with no measurement basis stated for either read class next to it.
  `docs/storage.md#storage-envelope`'s own supporting detail only shows the
  `aggregate` side (profiling `aggregate()` against `PostgresStore` at 8,000 rows:
  16,006 calls, ~2 per row, down from 7) and never states a narrow-scope figure at
  all, so the `[0.9.0]` sentence silently generalized from one measured class to
  both without saying so. Both classes were in fact counted directly, on
  `ObjectStore` (SQLite) at 2,000 rows: `aggregate` 7.00 → 2.00 store calls per
  row, narrow-scope list 7.00 → 2.00 store calls per row — a single local count,
  never CI-produced, the same evidentiary class as this project's other
  `ObjectStore`/SQLite figures. The `[0.9.0]` claim itself is unchanged and was not
  wrong, only unattributed for narrow-scope; this is a provenance correction, not a
  re-measurement.

## [0.9.0] — 2026-09-03

The storage-envelope release: decision C (how large may one object type get?) is
resolved with a measured, published number and an advisory runtime signal, never an
enforced ceiling. No path that succeeded at 0.8.0 fails here, and every existing read
answers exactly what it answered before — most of them faster.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.9.0"
```

### Added

- [`docs/storage.md#storage-envelope`](docs/storage.md#storage-envelope) publishes a
  measured, reproducible envelope for both shipped backends: a governed `aggregate` or
  narrow-scope read stays under one second below ~32,000 rows on `ObjectStore` (SQLite)
  and ~1,600 rows on `PostgresStore` (PostgreSQL — a conservative floor measured across
  four CI `postgres:16` runs: a GitHub-hosted runner is a shared VM, not fixed hardware,
  so the doc publishes all four runs' dispersion rather than a single point estimate). A
  bounded, unordered page read is flat at either size and carries no
  measured ceiling. The Postgres curve is loopback-measured; the doc publishes the
  correction arithmetic (roughly `+ 2 x RTT x rows` post-memo, down from `+ 7 x RTT x
  rows` pre-memo) an operator applies for their own network's round-trip time.
- `ontology.diagnose(store=...)` gains one advisory rule: a `Finding` (code
  `STORAGE_ENVELOPE_EXCEEDED`, `severity="warn"`) fires per object type whose stored
  row count, counted per tenant and never as a cross-tenant total, crosses its
  backend's published envelope. Passing no `store`, or a store under the envelope,
  returns no such finding. Nothing refuses, and this adds no error code.
- `OntologyRuntime.explain_scan(consumer, obj_type, where=None)` returns a `ScanReport`
  (`rows_scanned`, `rows_returned`, `rows_hidden_by_scope`) for one governed read. It
  follows `DecisionTrace`: reachable only from `OntologyRuntime`, absent from
  consumer-bound clients, `OntologyClient`, and MCP.
- A call-scoped memo (`ontary.scope._ScopeReadCache`) now reads a shared parent row at
  most once per guarded read instead of once per scanned row, cutting the store calls a
  governed `aggregate` or narrow-scope read issues from 7 to ~2 per row. Measured
  2.45x-3.10x faster at 25,000 rows across both linear read classes on both backends —
  comfortably past the >= 2x this decision required. It is proven result-identical,
  including where a `ScopePolicy.row_visibility` predicate or a `CustomResolver`
  participates in the visibility decision, and its read-once-per-guarded-read behavior
  is documented as an implementation detail of the current engine, not an API guarantee
  ([docs/storage.md#read-consistency](docs/storage.md#read-consistency-an-implementation-detail-not-a-guarantee)).
- `exists()` now stops at the first visible row instead of walking the full selection —
  measurably cheaper than `count()` when an early match exists, with an identical
  answer on every existing case (empty selection, no match, min-N gated, scope-hidden).

### Changed

- **`Store`'s protocol docstring no longer invites a third-party backend.** It used to
  say any other backend "can too [satisfy the protocol] by simply matching these method
  signatures"; a 2026-09-01 human ruling recorded this seam as **engine-internal** —
  only the three shipped backends (`ObjectStore`, `InMemoryStore`, `PostgresStore`) are
  supported — and the docstring is softened to say so.

  This is **Changed, not Breaking**, checked against every clause
  [docs/compatibility.md](docs/compatibility.md#what-counts-as-a-breaking-change) lists:
  `Store` is neither removed nor renamed in `ontary.__all__` (see the residue below) and
  no method signature changed, so the "obvious" clause is not met; no `Declarations`
  string changed (1); no error code changed meaning or disappeared (2); no
  `EffectMeta`/`AuditEntry`/`EffectRecord`/`OutboxRecord` field was removed or re-meant
  (3); no refusal became permissive or a permissive path started refusing — every one
  of the three backends behaves exactly as it did at 0.8.0 (4); `SCHEMA_VERSION` is
  unchanged at 9 (5); and no descriptor-IR/fingerprint-affecting change was made (6). No
  clause is met.

  The residue, stated plainly because this sentence was the only place the extension
  point was ever promised: `Store` REMAINS in `ontary.__all__` — removing it would
  itself be breaking, and the type is needed in annotations. A reader still meets the
  name at the front door; only the promise that a class of their own could satisfy it
  as a supported backend is withdrawn.

### Migration notes

- None. No path that worked at 0.8.0 stops working and no call site needs to change;
  [`examples/tickets/README.md`](examples/tickets/README.md)'s own migration diary
  records that the storage-envelope work forced no change to the reference app.

## [0.8.0] — 2026-08-26

The removal-and-queries release: remodeling and removal become governed business
operations, and the query surface becomes shape-complete. This is a pre-1.0
minor bump; read the migration notes before upgrading.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.8.0"
```

### Added

- `ActionContext.retire(obj_type, obj_id)` and `.unlink(link_api_name, from_id, to_id)` are
  the only handler-facing SDK removal spellings. They run only inside a handler transaction;
  retirement cascade-closes every live link that references the object on the side its link type
  declares for that object type, in that same transaction, and the object and link closures are
  captured in `AuditEntry.writes`.
- `Store.read_last(obj_type, obj_id)` returns an object's newest row whether or
  not it is still live, and `Store.links_from_asof`/`links_to_asof(link_type,
  id, asof)` return the links that were live at a named instant — links closed
  at or after it included. All three on all three shipped backends.
  `read_current` answers `None` both for an id that never existed and for a
  retired one, and `links_from`/`links_to` answer `[]` for an object whose
  links a retirement cascade closed; the action target scope gate needs both
  distinctions. Additive — a custom backend must implement all three.
- All three `Store` backends now provide `retire_object`, `close_link`, and
  `erase_object_content`. Retirement closes the current object row, link closure
  closes one live relationship, and erasure purges content while leaving structural
  tombstone rows intact.
- Source-removal authority is enforced for governed actions: an object type must
  declare whole-type `owned=True` and a link type must declare `owned=True`. An
  undeclared source-backed removal raises `UNDECLARED_SOURCE_REMOVAL`; a cascade
  that reaches such a link rolls back the whole action transaction.
- `where=` now supports the typed operator set `gt`/`gte`/`lt`/`lte`/`in`/`ne`/
  `contains` on every read surface, with catalogued unknown-operator and
  operator-type refusals.
- Consumer object reads accept `order_by` on declared payload fields and are bounded by
  default with `DEFAULT_READ_LIMIT=1000`; pass `limit=None` explicitly for an
  unbounded list. Inside a declared Function, `BoundQuery.list` remains unbounded by
  default and returns a bare list; pass `limit=` when a `Page` is wanted.
- The design guide's CRUD lint now rejects `Remove*` action names alongside
  `Delete*`, keeping removal as a domain business verb.
- `ontary.erase.erase_object()` gives store-holding operators an audited,
  idempotent one-object erasure API. It remains absent from the authoring root,
  `OntologyClient`, `ActionContext`, and MCP surfaces.
- `ontary erase` provides the store-file CLI path for that operator erasure API;
  `--operator` records the supplied identity (or the OS user when omitted),
  successful erasures and coded no-op reports exit zero, and coded refusals exit
  non-zero.
- `GuardedQuery`, `OntologyClient`, and `BoundQuery` now expose visible-row
  `count()` and `exists()` reads, and MCP exposes the same guarded count through
  `count_objects`.
- `aggregate()` and `aggregate_by()` now accept `func="mean"|"count"|"sum"|
  "min"|"max"` on guarded, client, Function, and MCP read surfaces. Every
  reduction retains the existing per-selection/per-group min-N release gate, and
  MCP gains an `aggregate_objects` tool (twelve tools total).
- `traverse(..., reverse=True)` now walks a link from its target side across
  `GuardedQuery`, `OntologyClient`, `BoundQuery`, typed `LinkHandle` calls, and
  MCP. The chosen spelling keeps one traversal method and makes direction an
  explicit call-site choice; human denial for `identity_revealing` links is
  symmetric and occurs before either direction resolves rows.

### Changed

- **Action target scope gate after retirement:** the `scope_semantics="target"`
  gate is now skipped only for a target that was NEVER stored. It used to be
  skipped for anything the store had no live row for, which after this
  release's retirement verb includes a RETIRED object — so a consumer outside
  that object's scope reached the handler with no scope check, and
  `ctx.create_link` (which validates no endpoint) let its writes through.

  <!-- scope-asof-invariant:start -->
  A retired target resolves the scope it owned **as of the instant its own row
  closed**. The links on the chain are read at that instant on both bounds, so
  an edge the object had already left cannot answer for it.

  Where a `ViaLink` hop finds more than one parent at that instant, the first
  parent whose chain resolves wins, over an order the store declares rather
  than each backend's own row order: earliest link `valid_from` first, then
  lowest parent id, compared byte-wise. It is not creation order — `links`
  declares no monotonic key, so two links made in one clock tick are separated
  by id, not by which was written first. Every backend answers in that order
  because it decides which scope owns the object, and therefore which operator
  this gate admits.

  An ancestor object is admitted when it had not already been retired by then —
  but it is read at its newest row, so its payload, and any `DirectProperty`
  key taken off that payload, is the one it carries now: an ancestor updated
  after the instant answers with the scope it is in today, not the one it was
  in then. Making the object side point-in-time too needs an as-of read of an
  object's history, which `Store` does not have. A live target has no closing
  instant and resolves in the present tense, exactly as a consumer read does,
  so this gate loosens nothing for an object that still exists.
  <!-- scope-asof-invariant:end -->

  Outside the resolved scope, `SCOPE_DENIED`; inside it, the handler's own
  precondition refusal as before, including `OBJECT_ALREADY_RETIRED`. It is a
  security fix, not a regression.

  Point-in-time, rather than simply "retired rows allowed", because the two
  obvious readings fail in opposite directions and both are authorization
  defects. Resolving ancestors from the newest row unconditionally lets an
  ancestor retired long ago beat a live one and authorize an operator who no
  longer owns the row. Resolving them live-only refuses an ancestor that was
  alive when the target closed and is the only route to a scope that is alive
  now — denying the operator who does own it, permanently, since a disbanded
  team cannot be un-retired. One instant answers both: at the target's
  `valid_to` the ancestor retired earlier was already closed, and the one that
  outlived the target was not.

  <!-- scope-denied-consequences:start -->
  Where the chain resolves and the consumer covers it, the gate reaches the
  handler's own refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not
  mean one thing: it is raised at two places. One is a defense-in-depth refusal
  of a scope-bearing parameter that is not a `str` — parameter validation
  rejects that first, so it is a floor under type confusion rather than a path
  in normal use. The other fires wherever coverage cannot be shown, and that is
  two situations rather than one: the chain resolved and this consumer is
  outside it, which is the ordinary denial this gate does not change; or the
  chain did not resolve at that instant, and deny-by-default denies. Only the
  second belongs to this frame. Four rule kinds are declared, and each meets
  retirement and erasure at its OWN hop:

  - `SelfScope` answers with the object's own id, which neither retirement nor
    erasure takes away.
  - `DirectProperty` reads the scope key off the payload, and
    `erase_object_content` blanks by design what that rule reads, which no
    history-aware read can recover. Retirement leaves the payload alone,
    because this gate reads the newest row rather than the live one, so erasure
    is the only lifecycle event this hop's OWN READ loses to. That is the
    target itself when the target is `DirectProperty`-scoped; it is equally an
    ANCESTOR whose own hop is `DirectProperty`, which denies a `ViaLink`-scoped
    target whose link erasure preserved. Erasure destroying the scope key is
    the point of erasure, not a gap in the gate.
  - `ViaLink` climbs the `links` table, which erasure preserves, so erasure
    costs this hop nothing. It resolves to nothing when none of the parents it
    reaches resolves in turn — among them a parent already retired BEFORE the
    target closed: its edge may still be readable at that instant, but its own
    row is not admitted, so it cannot answer at the one instant this gate asks
    about. One retired after the target — including in the same cascade tick —
    still answers for it.
  - `CustomResolver` is author code handed the raw `Store`, and the engine does
    not reach inside it, so what a retired or erased object resolves to is the
    resolver's own business rather than this frame's. The natural body reads
    `read_current`, which is `None` for a retired object, so a resolver written
    that way denies. A type that needs the precondition refusal after
    retirement declares a second rule — a `DirectProperty` on a scope-key
    column, which survives retirement.

  Each bullet is about one hop, never about one target, and the four are not
  the whole chain. `ScopePolicy.rules` maps each type to an ORDERED list, so a
  target declares as many of these hops as that list holds and is answered by
  the first that resolves; and a level no rule of its own can answer climbs to
  the canonical instance of a narrower scope, where a type declares one — which
  is none of the four. What the engine's own hops share is the frame: any
  object the engine has to read that had already been retired before the target
  closed is refused there, whichever of those hops reached it — so the hop that
  answers a target's level can fail on an object the target's other hops never
  touch. A `CustomResolver` is outside that frame only for the reads its own
  callable makes: the engine does not thread the instant into author code, so
  an ancestor the callable reaches for itself is read however it reads it,
  retired or not. Its ANSWER re-enters the engine, and every object the engine
  reads from there is refused on the frame's own terms — the canonical instance
  a narrower answer names, and any object whose rules the engine goes on to
  ask, whose row is checked before its own resolver runs.

  Every denial in this list fails closed — the object's own owner is denied,
  nothing is disclosed.
  <!-- scope-denied-consequences:end -->

  This history-aware resolution is scoped to that one gate. `resolve_owning_scope`
  takes `include_retired=False` by default and every consumer read keeps it:
  resolving a retired — or erased — row's scope would put its children back in
  a reader's visible set, carrying a population past `min_n` and releasing an
  aggregate computed over the erased subject's own rows.
- **Erasure follows a link endpoint no declared object can own:** a recorded
  link write whose endpoint id matches the object being erased is now treated
  as a reference unless an object of the endpoint's DECLARED type really
  carries that id. `create_link` validates neither endpoint existence nor
  endpoint type, so the declaration alone was letting erasure walk past audit
  and outbox rows that named the erased object. An endpoint an existing
  same-id object of the declared type can own is still left alone.
  It is a security fix, not a regression.
- `Page` and `TypedPage` now refuse `len(page)`, `page[i]`, and `page[i:j]`
  with `PAGE_NOT_ITERABLE`, the refusal the code already documented for them
  and only implemented for iteration. Both previously raised a bare
  `TypeError` naming neither the page nor `.items`. Truthiness is unchanged.
- `Ontology.diagnose()` now reports the empty `contributor_rules` rule list
  that `ScopePolicy.validate` refuses at startup, so the sweep no longer
  returns clean for a declaration that cannot be built.

- **Hidden-field aggregate disclosure guard:** on a consumer surface,
  `aggregate(func="mean"|"count", value_field=<hidden field>)` now raises
  `VISIBILITY_DENIED`; the hidden-field exemption requires an author-declared
  Function over a type declaring `contributor_rules`. This closes the confirmed
  mean/count plus complementary-selection oracle that recovered an individual
  hidden value. It is a security fix, not a regression.
- **Scope-key disclosure guard:** on a hidden `DirectProperty` scope-routing key,
  only bare equality and `in` over an explicit list remain exempt in `where`.
  `gt`/`gte`/`lt`/`lte`/`ne`/`contains`, `in` over a non-list, `order_by`, and
  `group_by` now refuse with `VISIBILITY_DENIED`. This closes the confirmed
  `contains` alphabet-walk oracle (and prevents rank/group-key disclosure). It is
  a security fix, not a regression.
- **min-N contributor counting:** the floor is now measured over the rows that
  carry `value_field`, and rows whose contributor does not resolve count as one
  unknown identity between them rather than one apiece. A type mapped to an
  empty `contributor_rules` list no longer opens the hidden-field exemption, and
  `ScopePolicy.validate` refuses that declaration. This closes the confirmed
  disclosure where retiring one contributor — or closing its links — made
  several rows by that one person look like several people and released their
  mean, and the one where a group of `min_n` people in which only one answered
  released that person's answer as the "mean". It is a security fix, not a
  regression.
- **`where` snapshot and supplied-value rule:** each `where` mapping and each
  condition inside it is now read exactly once, at the public boundary, and every
  gate and the row matcher consume that one snapshot — closing the confirmed
  split where a mapping answering differently on a second read was classified as
  supply-shaped equality and executed as a `contains` alphabet walk. The
  supply-shaped exemption now also requires an operand that actually supplies a
  value (`str`/`int`/`float`/`bool`/`date`), so `where={<hidden key>: None}`,
  `{"in": [None]}`, and `{"in": []}` raise `VISIBILITY_DENIED` instead of acting
  as a free per-row null probe. It is a security fix, not a regression.

### Migration notes

- **Mapping-valued `where` operands:** `where` mappings are now operator syntax,
  so dict equality on a declared `json` property must use the `in` escape, for
  example `where={"data": {"in": [{"kind": "a"}]}}`.
- **Object reads are bounded by default on consumer surfaces:** omitting `limit` from
  `GuardedQuery.get_objects` or `OntologyClient.list` now returns a `Page` bounded by
  `DEFAULT_READ_LIMIT=1000`. Inside a declared Function, `BoundQuery.list` is deliberately
  unbounded by default and returns a bare list; pass `limit=` to opt into a `Page`. Pass
  `limit=None` explicitly for the unbounded list form on consumer surfaces; update callers
  that previously relied on bare consumer reads returning a list.
- **0.8.0 grouped-empty aggregate refusal:** `aggregate_by()` over an empty
  visible selection now raises `MIN_N_VIOLATION`, matching `aggregate()`, instead
  of returning `{}`. Callers that treated `{}` as an empty-result sentinel must
  catch the catalogued visibility refusal instead.
- **`BoundQuery.traverse` legacy `via=` spelling removed in 0.8.0:**
  `BoundQuery.traverse(from_id, via=link_handle)` is removed; use
  `BoundQuery.traverse(link_handle, from_id)`. Passing `via=` now raises a
  natural Python `TypeError` signature error, not a catalogued refusal. Function
  bodies passing `via=` must be updated; mypy will flag typed call sites, but
  untyped/`Any` ones only fail at runtime.

- **`IngestError` now subclasses `OntaryError`:** a handler ordered
  `except OntaryError:` before `except IngestError:` now shadows the ingest
  branch. Code that relied on `IngestError` being outside the hierarchy must
  reorder its handlers. Conversely, `except OntaryError` now catches client-level
  ingest failures that previously escaped.

- **Aggregates near the min-N floor may now refuse:** three counting changes
  that only ever refuse more. An aggregate over an optional `value_field` counts
  only the rows carrying it; unresolved contributors count as one unknown
  identity collectively; and `ScopePolicy.validate` rejects a type mapped to an
  empty `contributor_rules` list — remove the entry to opt out of contributor
  de-duplication. Selections that sat at the edge of `min_n` will start raising
  `MIN_N_VIOLATION`. That number was never backed by `min_n` distinct people.

- **Null `where` operands on a hidden field now refuse:** a bare `None`, an `in`
  over a list containing `None`, and an empty `in` list no longer receive the
  scope-key exemption and raise `VISIBILITY_DENIED`. Null filtering on any
  readable field is unchanged.

- **Function audit scope:** `Declarations.audit_scope` now records that actions are
  always audited, Functions are audited when they declare capabilities unless overridden
  per Function, and a call that releases a hidden field through the contributor exemption
  is audited even when it declares `audit=False`. The latter appends one `kind="function"`
  entry for each call that actually releases.

- **New error codes:** lifecycle and query refusals add
  `OBJECT_ERASURE_NOT_FOUND`, `OBJECT_RETIRE_NOT_FOUND`, `OBJECT_ALREADY_ERASED`,
  `OBJECT_ALREADY_RETIRED`, `LINK_NOT_FOUND`, `STALE_CURSOR`,
  `UNDECLARED_SOURCE_REMOVAL`, `UNKNOWN_OPERATOR`, `OPERATOR_TYPE_MISMATCH`,
  `PAGE_NOT_ITERABLE`, and `GROUP_KEY_COLLISION`. Branch on these stable codes where a
  refusal needs different handling.

## [0.7.0] — 2026-08-25

The DX-suite release: a stricter authoring front door, fail-loud ingest and
query validation, bounded MCP reads, operator diagnostics, and runnable
authoring/serving helpers. This is a pre-1.0 minor bump; read the migration
notes before upgrading.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.7.0"
```

### Migration notes from 0.6.0

- **Front door exports (additive):** `ontary.__all__` now includes
  `OntologyClient`, `InMemoryStore`, `EffectMeta`, `EffectDispatcher`,
  `ScopePolicy`, `CustomResolver`, `ref`, `scope_ref`, `RetryPolicy`,
  `DrainReport`, `OutboxRecord`, and `Declarations`. Prefer the replacement
  root imports, for example `from ontary import OntologyClient`; canonical
  submodule imports such as `from ontary.client import OntologyClient` remain
  supported.

- **Ingest now raises by default:** `client.ingest()` and
  `client.ingest_links()` now default to `on_error="raise"`. Catch
  `ontary.ingest.IngestError` and inspect its `report`;
  `len(error.report.inserted_ids)` is the committed-record count. Pass
  `on_error="report"` to retain the old report-returning behavior. The
  committed prefix is still written before a later record fails.

- **`IngestError` is an exception, not a Pydantic model:** it now subclasses
  `Exception` and carries `report` plus the committed-record information;
  replace `isinstance(error, BaseModel)` with `isinstance(error, IngestError)`
  or `except IngestError`. Callers using `model_fields` should inspect the
  explicit exception fields (`index`, `reason`, `code`, and `report`) instead.
  `model_dump()` remains only a limited compatibility serializer for the old
  error-row fields; use `error.report.model_dump()` for the complete batch
  report.

- **Execute signature and result shape:** use `client.execute(params)` or
  `client.execute(params=params)` for a typed action, and
  `client.execute("ActionName", {"field": value})` (or the equivalent
  `action=...`, `params=...` keywords) for the string form. Replace callers
  that depended on the old positional-only parameter names. Action handlers
  may now return nested JSON-safe `dict[str, Any]`; callers that assumed every
  result value was a string should consume the JSON value tree instead.

- **Malformed `execute`/`traverse` calls now refuse as `INVALID_PARAMS`:**
  calling `execute` or `traverse` with a shape no overload accepts (for
  example the string form without params or `from_id`) now raises
  `ValidationFailed` with `code="INVALID_PARAMS"` instead of an
  `InternalError` with `code="INTERNAL_ERROR"`; `INTERNAL_ERROR` is reserved
  for genuine engine invariants.

- **String traversal positional order:** the string form changed from
  `client.traverse(obj_type, obj_id, link)` to
  `client.traverse(obj_type, link, from_id)`. All three arguments are `str`,
  so mypy cannot detect this migration; update positional call sites manually.
  The replacement typed form is `client.traverse(link_handle, from_id)`.

- **`BoundQuery.traverse` legacy `via=` spelling removed in 0.8.0:**
  `BoundQuery.traverse(from_id,
  via=link_handle)` deliberately remains supported, and string
  `BoundQuery.traverse(link, from_id)` remains unchanged. New code may use the
  handle-first `BoundQuery.traverse(link_handle, from_id)` form; treat the
  `via=` spelling as legacy if planning a future breaking cleanup. This legacy
  spelling was removed in 0.8.0; see the 0.8.0 migration note above.

- **Unknown `where` keys now refuse:** typed, string, aggregate, and MCP read
  surfaces validate `where` against declared payload fields. A typo now raises
  `ValidationFailed` with `code="UNKNOWN_FIELD"` instead of returning `[]`.
  Replace typo-tolerant empty-result handling with a declared field name and
  catch/branch on `UNKNOWN_FIELD`; lineage fields remain non-filterable.

- **Constructor and enforcement hardening:** `ScopePolicy` rejects
  `min_n < 1`, empty `levels`, and duplicate levels at construction, so an
  ontology declaration with `scope_levels=[]` is rejected when its policy is
  materialized instead of producing a usable definition. Use a non-empty,
  unique level list and `min_n >= 1`. Registry getters now raise
  `ValidationFailed` instead of bare `KeyError`: handle
  `UNKNOWN_OBJECT_TYPE`, `UNKNOWN_LINK_TYPE`, `UNKNOWN_ACTION`, or
  `UNKNOWN_NAME` as appropriate. Enforcement-path assertion failures are now
  real coded validation/errors, so replace `AssertionError` catches with the
  documented `ValidationFailed`/kind classes. Unknown-link traversal on the
  client and MCP surfaces intentionally remains `UNKNOWN_NAME`.

- **MCP consumer contract:** `query_objects` now defaults to a 100-row page,
  caps pages at 1000, and returns `next_cursor`; pass `limit` and the returned
  cursor as `after` to paginate. The `where` grammar is equality-only on
  payload fields. Tool parameters are validated inside the structured error
  envelope, and all successful results are JSON-normalized; return
  JSON-safe values and handle envelope errors by `code`/`kind`, not raw
  framework text. Grouped-aggregate `MIN_N_VIOLATION` messages now say
  `count withheld`; codes are unchanged, and message text is not a wire
  contract. Read tools carry `readOnlyHint`, while `execute_action` carries
  `destructiveHint` through `ToolAnnotations`. The MCP extra is now pinned to
  `mcp>=1.27.2,<2`.

- **New connector declaration requirement:** concrete `BaseConnector` and
  `SourceConnector` subclasses must define a class-level `name`; missing it
  now fails at class definition. Add `name = "your-source-system"` to the
  subclass (abstract connector bases may still omit it).

- **Fingerprint behavior for new property types:** `date` and `choices` are
  fingerprint-neutral for ontologies that do not use them, so upgrading the
  SDK alone leaves those digests byte-identical. An ontology that declares
  `choices=[...]` moves its digest; declaring a `date` property likewise
  changes that declaration's shape. Follow the [fingerprint acceptance
  procedure](docs/compatibility.md#fingerprints-and-07-property-types),
  including row migration before accepting a new choices constraint.

### Additions

- `Ontology.diagnose()` and `Finding`, including design-guide lint findings.
- Runtime-only explain traces via `explain_read()` and `explain_list()`.
- Deterministic test helpers in `ontary.testing`.
- CLI `validate`, `explain`, `version`, and localhost-only `serve --dev`.
- Five runnable recipes in the [cookbook](docs/cookbook.md).
- Runnable MCP and effects-drain examples under
  [`examples/tickets/`](examples/tickets/).

## [0.6.0] — 2026-08-22

The release that completes the SDK-simplification work. This is a pre-1.0 minor
bump, so the public Python surface and the MCP `error.type` field have breaking
changes even though the store schema does not.

```bash
uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.6.0"
```

### Migration from 0.5.0

**0.5.0 was never tagged.** Its `Lineage` → `SourceLineage` migration is part of
the pre-0.6.0 history archived in `github.com/ryoochi0112/ontic`. A consumer coming
from the last published tag, `v0.4.0`, must apply that migration before the 0.6.0
changes. This note does not create or move a git tag.

- **BoundQuery reads:** replace `BoundQuery.get_object(obj_type, obj_id)` with
  `BoundQuery.get(obj_type, obj_id)`, and replace
  `BoundQuery.get_objects(obj_type, where, ...)` with
  `BoundQuery.list(obj_type, where, ...)`. There are no compatibility shims.
  The aggregate methods also gained typed overloads; that is additive and
  non-breaking.

- **`code` is now required when constructing a kind class.** The class-level
  default is gone: `ValidationFailed("msg")` raises `TypeError`, and
  `ValidationFailed("msg", code="INVALID_PARAMS")` is the replacement. Every
  code and its catalogued `kind` are unchanged — this changes only how an
  exception is *constructed*, never what travels on the wire. Passing an empty
  `code` raises `ValueError`.

  If you construct these exceptions yourself, name the code you mean at each
  site; the previous class defaults were `INVALID_PARAMS` (`ValidationFailed`),
  `PRECONDITION_FAILED` (`PreconditionFailed`, `ActionError`),
  `PERMISSION_DENIED`, `VISIBILITY_DENIED`, `AUTHORITY_ERROR`,
  `CARDINALITY_VIOLATION` (`ConflictError`) and `INTERNAL_ERROR`
  (`InternalError`), so passing those preserves today's behaviour exactly. If
  you only *catch* these exceptions, nothing changes.

  Why: a default meant that any construction reached through a rebound name —
  an alias, an assignment, a function parameter, a `for` target, a `getattr` —
  silently shipped the default code instead of the intended one, and the
  consumer branching on `.code` saw the wrong stable value. Five review rounds
  showed that no source-level guard closes this in a language where any name
  can be rebound: each round modelled one more binding shape and the next found
  another. Requiring the argument makes the wrong construction unwriteable
  rather than undetectable, and retired two guards that existed only to chase it.

- **Exception collapse:** the legacy exception names and the `CodedError` alias
  are deleted, not kept as aliases. Catch the kind class shown below and branch
  on its unchanged `code` when the specific refusal matters. `ActionError`
  remains the handler-facing precondition vocabulary.

  - `VisibilityError`: `MinNViolation` → (`VisibilityError`,
    `MIN_N_VIOLATION`); `VisibilityDenied` → (`VisibilityError`,
    `VISIBILITY_DENIED`).
  - `PermissionDenied`: `ActionPermissionError` → (`PermissionDenied`,
    `PERMISSION_DENIED`; also `SCOPE_DENIED`); `Unauthenticated` →
    (`PermissionDenied`, `UNAUTHENTICATED`); `ConsumerUnresolved` →
    (`PermissionDenied`, `CONSUMER_UNRESOLVED`).
  - `PreconditionFailed`: `CapabilityNotProvided` → (`PreconditionFailed`,
    `CAPABILITY_NOT_PROVIDED`); `EffectNotDispatchable` →
    (`PreconditionFailed`, `EFFECT_NOT_DISPATCHABLE`); `FunctionError` →
    (`PreconditionFailed`, `FUNCTION_ERROR`).
  - `ValidationFailed`: `EntityKeyMismatchError` → (`ValidationFailed`,
    `ENTITY_KEY_MISMATCH`); `UndeclaredCapability` → (`ValidationFailed`,
    `UNDECLARED_CAPABILITY`); `UndeclaredEffect` → (`ValidationFailed`,
    `UNDECLARED_EFFECT`); `EffectNotSerializable` → (`ValidationFailed`,
    `EFFECT_NOT_SERIALIZABLE`); `UnknownName` → (`ValidationFailed`,
    `UNKNOWN_NAME`); `UnknownField` → (`ValidationFailed`, `UNKNOWN_FIELD`);
    `OntologyValidationError` → (`ValidationFailed`, `ONTOLOGY_INVALID`);
    `HydrationError` → (`ValidationFailed`, `INVALID_RECORD`);
    `InvalidLimit` → (`ValidationFailed`, `INVALID_LIMIT`);
    `AfterWithoutLimit` → (`ValidationFailed`, `AFTER_WITHOUT_LIMIT`);
    `InvalidGroupBy` → (`ValidationFailed`, `INVALID_GROUP_BY`);
    `NonNumericAggregate` → (`ValidationFailed`, `NON_NUMERIC_AGGREGATE`);
    `ScopePolicyError` → (`ValidationFailed`, `SCOPE_POLICY_ERROR`);
    `UnknownObjectType` → (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`);
    `UnknownLinkType` → (`ValidationFailed`, `UNKNOWN_LINK_TYPE`);
    `ObjectNotFound` → (`ValidationFailed`, `OBJECT_NOT_FOUND`);
    `InvalidBatch` → (`ValidationFailed`, `INVALID_BATCH`);
    `InvalidCursor` → (`ValidationFailed`, `INVALID_CURSOR`); and
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`).
  - `AuthorityError`: the removed store-specific `ontary.store.AuthorityError`
    → (`AuthorityError`, `AUTHORITY_ERROR`); `SourceCreateRefused` →
    (`AuthorityError`, `SOURCE_CREATE_REFUSED`); `UndeclaredSourceWrite` →
    (`AuthorityError`, `UNDECLARED_SOURCE_WRITE`). The `AuthorityError` kind
    class itself is the replacement; it was not removed.
  - `ConflictError`: `StoreBusy` → (`ConflictError`, `STORE_BUSY`);
    `CardinalityViolation` → (`ConflictError`, `CARDINALITY_VIOLATION`);
    `StoreVersionUnsupported` → (`ConflictError`,
    `STORE_VERSION_UNSUPPORTED`); `OntologyDrift` → (`ConflictError`,
    `ONTOLOGY_DRIFT`); `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`); and `UpcastFailed` →
    (`ConflictError`, `UPCAST_FAILED`).
  - **The old root export `ontary.StoreError` was a base class, not an
    internal-error replacement.** Its fallback code `STORE_ERROR` remains in
    the catalog, but there is no one-to-one kind-class replacement: a store
    catch-all must become `except OntaryError:` (from `ontary.errors`, or the
    root `ontary.OntaryError`; alternatively use an explicit tuple of the kind
    classes below).
    `except InternalError:` is **NOT** equivalent and will not catch the
    concrete store failures. The 13 direct subclasses in the old
    `StoreError` hierarchy now land as follows: `StoreBusy` →
    (`ConflictError`, `STORE_BUSY`); `UnknownObjectType` →
    (`ValidationFailed`, `UNKNOWN_OBJECT_TYPE`); `UnknownLinkType` →
    (`ValidationFailed`, `UNKNOWN_LINK_TYPE`); `ObjectNotFound` →
    (`ValidationFailed`, `OBJECT_NOT_FOUND`); `CardinalityViolation` →
    (`ConflictError`, `CARDINALITY_VIOLATION`); `InvalidBatch` →
    (`ValidationFailed`, `INVALID_BATCH`); `InvalidCursor` →
    (`ValidationFailed`, `INVALID_CURSOR`); `StoreVersionUnsupported` →
    (`ConflictError`, `STORE_VERSION_UNSUPPORTED`);
    `DeclaredShapeViolation` → (`ValidationFailed`, `INVALID_RECORD`);
    `OntologyDrift` → (`ConflictError`, `ONTOLOGY_DRIFT`);
    `StoreSchemaIncompatible` → (`ConflictError`,
    `STORE_SCHEMA_INCOMPATIBLE`); the old store-specific `AuthorityError` →
    (`AuthorityError`, `AUTHORITY_ERROR`); and `CallerTransactionRefused` →
    (`ConflictError`, `CALLER_TRANSACTION_REFUSED`). The inherited
    `SourceCreateRefused` and `UndeclaredSourceWrite` are both
    `AuthorityError` failures, with their specific codes shown above.
  - `CodedError` was an alias, not a distinct code: replace it with the
    `OntaryError` base and inspect the concrete error's `code` and `kind` (the
    base default remains `INTERNAL_ERROR`).

  The authoritative migration rule is that every error `code` and `kind` value
  is **UNCHANGED**; those values are the wire/compatibility surface. The names
  to catch are now `OntaryError` plus `VisibilityError`, `PermissionDenied`,
  `PreconditionFailed`, `ValidationFailed`, `AuthorityError`, `ConflictError`,
  `InternalError`, and `ActionError`.

  The following old dual-inheritance relationships are also gone. Catch the
  kind class and code shown above instead: `UnknownName` was a
  (`ValidationFailed`, `KeyError`) → (`ValidationFailed`, `UNKNOWN_NAME`),
  `UnknownField` was a (`ValidationFailed`, `KeyError`) →
  (`ValidationFailed`, `UNKNOWN_FIELD`), `HydrationError` was a
  (`ValidationFailed`, `ValueError`) → (`ValidationFailed`, `INVALID_RECORD`),
  and `ActionPermissionError` was a (`PermissionDenied`, `ActionError`,
  `PermissionError`) → (`PermissionDenied`, `PERMISSION_DENIED` or
  `SCOPE_DENIED`). At 0.6.0, an existing `except KeyError:` around a typed
  read, `except ValueError:` around hydration, or `except ActionError:` (or
  `except PermissionError:`) around `client.execute(...)` silently stops
  catching these failures: there is no import or type error to expose the
  break.

- **MCP wire change:** in an MCP error payload, `error.type` now carries the
  kind-class name, such as `VisibilityError`, `PermissionDenied`, or
  `ValidationFailed`, instead of the legacy exception-class name.
  `error.code` and `error.kind` remain byte-stable. This is a **BREAKING**
  change for consumers that branch on `error.type`; branch on `code` or `kind`
  for the stable contract.

- **Deleted drift and identity features:** replace `assess_drift` by comparing
  `OntologyFingerprint` values directly, and replace `describe_drift` by
  comparing the fingerprints' `.types` mappings. `DriftAssessment` has no
  replacement object: use `OntologyFingerprint` for the comparison and handle
  the `ConflictError` with code `ONTOLOGY_DRIFT` (the former `OntologyDrift`)
  for the store's refusal. The fingerprint check-and-refuse behavior is
  **UNCHANGED**; only the descriptive-assessment API was removed. The deleted
  `resolve_identities`, `IdentityRule`, and `IdentityMatch` APIs have no SDK
  replacement: matching is caller-owned, and the `ontary.connect.identity`
  module is gone.

- **Front-door demotions:** `ontary.__all__` is exactly 45 names. T10 deleted
  nothing from the engine; names outside that curated front door remain
  importable from their canonical submodule. The complete inventory below is
  copied from `tests/test_docs.py`'s `DEMOTED_NAMES_BY_MODULE`, which is the
  source of truth for both the docs and the guard. Import `MappingValidationError`
  from `ontary.connect`.

  | Destination module | Names still importable there |
  | --- | --- |
  | `ontary.actions` | `ActionExecutor` |
  | `ontary.audit` | `CapabilityAccessRecord`, `EffectRecord` |
  | `ontary.authoring` | `ref`, `scope_ref` |
  | `ontary.client` | `OntologyClient`, `OntologyRuntime` |
  | `ontary.connect` | `LinkSkip`, `MappingValidationError`, `RunReport`, `SourceConnector`, `SourceLineage`, `map_batch`, `run_dlt_extract`, `to_date`, `to_datetime`, `to_optional_date`, `to_optional_datetime` |
  | `ontary.declarations` | `Declarations` |
  | `ontary.effects` | `EffectDispatcher`, `EffectMeta` |
  | `ontary.errors` | `ERROR_CODES`, `ErrorCodeInfo`, `Kind` |
  | `ontary.fingerprint` | `OntologyFingerprint`, `fingerprint_ontology` |
  | `ontary.functions` | `FunctionHandler`, `FunctionRegistry` |
  | `ontary.ingest` | `IngestError`, `IngestReport`, `bulk_link`, `bulk_upsert` |
  | `ontary.mcp_server` | `ConsumerResolver`, `build_multi_consumer_mcp_server` |
  | `ontary.meta` | `ActionParameterDef`, `ActionTypeDef`, `FunctionDef`, `LinkTypeDef`, `ObjectTypeDef`, `OntologyRegistry`, `PropertyDef`, `PropertyType`, `ScopeLevel`, `Upcaster` |
  | `ontary.migrate` | `MigrationFailure`, `MigrationReport`, `migrate_object_type`, `upcast_object_type` |
  | `ontary.ontology` | `OntologyDef` |
  | `ontary.outbox` | `DEFAULT_RETRY_POLICY`, `DrainReport`, `OutboxRecord`, `OutboxState`, `RetryPolicy` |
  | `ontary.query` | `GuardedQuery` |
  | `ontary.scope` | `CustomResolver`, `Direction`, `RowVisibilityFn`, `ScopePolicy`, `ScopeRule`, `resolve_contributor`, `resolve_owning_scope` |
  | `ontary.security` | `ConsumerKind`, `covers_scope` |
  | `ontary.store` | `AuditEntry`, `DEFAULT_BATCH`, `DEFAULT_TENANT`, `InMemoryStore`, `Lineage`, `SCHEMA_VERSION`, `StoredObject`, `WriteRecord`, `accept_ontology_fingerprint`, `check_ontology_fingerprint` |
  | `ontary.upcast` | `upcast_payload` |

- **Docs:** the README narrative is now split into Diátaxis-shaped English
  pages: [`docs/storage.md`](docs/storage.md), [`docs/connectors.md`](docs/connectors.md),
  [`docs/mcp-serving.md`](docs/mcp-serving.md), [`docs/effects.md`](docs/effects.md),
  [`docs/queries.md`](docs/queries.md), and [`docs/authority.md`](docs/authority.md).
  The README is a compact front door; the existing Japanese API-reference and
  ontology-design documents remain the maintained JA surfaces.

### Changed

- **Store compatibility:** the store schema was **NOT bumped**. `SCHEMA_VERSION`
  remains **9**, and existing SQLite store files open with no migration. SQLite
  and Postgres now share one SQL/DDL source; that is an internal consolidation
  with no behavior change, and it is why the Postgres `audit_log` column order
  changed on **FRESH-CREATE schemas only**.

- **The Postgres RLS session GUC is renamed `ontarydk.tenant` → `ontary.tenant`,
  and RLS policies are persisted database state.** A database created by ≤ 0.3.0
  has `tenant_isolation` policies compiled against the old GUC; `_init_schema`
  sees the schema stamp already current (still v9) and will not recreate them.
  Because RLS here is fail-closed, 0.4.0 against such a database reads zero rows.
  Run this once per existing RLS-enabled database before upgrading:

  ```sql
  ALTER POLICY tenant_isolation ON objects
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON links
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON audit_log
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ALTER POLICY tenant_isolation ON effect_outbox
      USING (tenant = current_setting('ontary.tenant', true))
      WITH CHECK (tenant = current_setting('ontary.tenant', true));
  ```

Pre-0.6.0 history and tags v0.1.0–v0.4.0 live in github.com/ryoochi0112/ontic.
