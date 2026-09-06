# Storage backends, tenancy, and schema

[Back to the README](../README.md) · [API reference](api-reference.md)

The runtime depends on the `Store` protocol, not on a particular database. Choose
the backend for the deployment shape, then bind the ontology once and hand out
consumer views from that runtime. Storage does not replace the guarded client
surface: direct store access is trusted application code, while reads through an
`OntologyClient` or MCP server remain scope-, sensitivity-, and min-N-aware.

## Choose a backend

| Backend | Best fit | Import |
| --- | --- | --- |
| `InMemoryStore` | Fast tests and dependency-free doubles | `ontary` |
| `ObjectStore` | A local file, script, or single-process application | `ontary` |
| `PostgresStore` | A deployment with multiple application processes | `ontary` |

All three implement the same store contract. The action, ingest, and guarded-read
layers therefore do not need backend-specific branches. A typical local runtime is:

```python
from ontary import ObjectStore

store = ObjectStore(ontology.registry, path="ontary.sqlite")
runtime = ontology.bind(store)
```

For a test double, import the backend from the front door:

```python
from ontary import InMemoryStore

store = InMemoryStore(ontology.registry)
```

PostgreSQL is selected by the optional `postgres` extra and keeps the same runtime
construction:

```python
from ontary import PostgresStore

store = PostgresStore(
    ontology.registry,
    "postgresql://localhost/ontary",
    tenant="acme",
)
runtime = ontology.bind(store)
```

The PostgreSQL implementation has a few intentional differences from SQLite:

- SQLite supports its historical migration path because local store files can
  predate the current engine. PostgreSQL creates the current schema and refuses a
  database whose stamp does not belong to this engine rather than silently adopting
  an unknown layout.
- Object payloads are stored as `TEXT`, matching the other backends. The engine does
  not query inside payload JSON, so JSONB normalization would buy no runtime
  behavior while making byte-level parity harder -- reaffirmed by the [storage
  envelope](#storage-envelope) measurement below, which found the dominant linear
  cost is Python-side scope resolution, not SQL scanning.
- Outbox claims use PostgreSQL row locking with `SKIP LOCKED`, preserving the same
  lease semantics when drainers run in separate processes.
- Row-level security can provide a database-side tenancy check in addition to the
  engine predicates. It is defense in depth, not a replacement for the store's
  tenant binding.

The PostgreSQL integration suite is optional locally. `make verify` remains an
offline gate; an absent PostgreSQL service is an expected skip, not a reason to
change the test configuration.

## Storage envelope

This is a measured **envelope, not an SLA**: how much data one object type can hold
before a governed read that must see the whole visible population stops being
usable. It is advisory. Nothing in the SDK refuses a write or a read because a type
has crossed it; `ontology.diagnose(store=...)` reports a `STORAGE_ENVELOPE_EXCEEDED`
`Finding` instead (severity `warn`), and only when a store is actually passed in.
The numbers below are per **tenant**, not per table: every read path they measure
(`Store.read_page`, e.g. `src/ontary/store/sqlite.py:657`'s `AND tenant = ?`) is
tenant-filtered, so both the lint and this section's own curve count one tenant's
rows for a type, never a cross-tenant total.

Every number below is reproducible from the committed `scripts/scan_curve.py`
(`--uncached` reproduces the memo ratio; `--dsn` switches to PostgresStore), measured
against **shipped 0.9** (the scope-resolution memo and the `exists()` early exit both
landed first, per this decision's own "measure after the memo" rule). The two backends
are measured in **different environments, deliberately**: `ObjectStore` locally,
`PostgresStore` in CI -- Postgres is the backend most likely in production, and "a
laptop number is not reproducible by a reader" (spec `storage-envelope.md` §5) was
said specifically about it. "Reproducible" below means the same script against the
same container image, not an exact number: see the run-to-run dispersion note under
the Postgres tables, which corrects §3's "fixed hardware" premise.

- **`ObjectStore` (SQLite)** -- Python 3.12.13's bundled `sqlite3` 3.53.4, file-backed,
  single process, warm page cache, measured 2026-09-02 on macOS 26.5.1 (Darwin
  25.5.0), arm64, Python 3.12.13: `uv run python scripts/scan_curve.py 1000 5000
  10000 25000 50000 100000`. **One sample, one laptop, never CI-produced** -- stated
  plainly here because the Postgres figure below now carries a measured dispersion
  caveat, and an uncaveated number sitting beside a caveated one would otherwise read
  as the better-evidenced of the two. It is not: it is the least-evidenced number this
  section publishes.
- **`PostgresStore` (PostgreSQL)** -- the `postgres:16` service container (PostgreSQL
  16.15, Debian 16.15-1.pgdg13+2) in the `postgres` job of the project's own CI:
  `uv run python scripts/scan_curve.py 1000 5000 25000 --uncached --dsn
  postgresql://postgres:postgres@localhost:5432/postgres` (the exact invocation in
  `.github/workflows/verify.yml`'s "Storage envelope curve" step). **Publication
  rule**, stated outright because it was not before: a run earns its own `#### Run
  <id>` section and cost table below only when it SETS or LOWERS the floor this
  section publishes. Every run this step has actually executed is recorded by id
  and its own worst per-row rate in the observed log below regardless of whether it
  earns a table -- so a future faster run costs one appended line there, never a
  table-and-ordinal rewrite. As of this writing the step has run **six** times;
  **four** earned a table by setting or lowering the floor in turn: GitHub Actions
  run **33623316425** (job `postgres`, **100225149283**, 2026-09-02T11:12Z); 77
  minutes later on the *same* workflow and the *same* `ubuntu-latest` runner label,
  run **33630090428** (job `postgres`, **100247114881**, 2026-09-02T12:29Z); 3
  hours 27 minutes after that (4 hours 44 minutes after the first), run
  **33651633373** (job `postgres`, **100319828397**, 2026-09-02T15:56Z); and
  roughly 8 hours after the third, run **33697469943** (job `postgres`,
  **100469367996**, 2026-09-02T23:59Z) -- see the run-to-run dispersion note below
  for the measured span across these four, not a single flat ratio. Re-read each
  job's raw evidence directly rather than trusting this doc's or
  `src/ontary/diagnose.py`'s copy of the numbers: `gh run view --job 100225149283
  --log` for the first run, `gh run view --job 100247114881 --log` for the second,
  `gh run view --job 100319828397 --log` for the third, `gh run view --job
  100469367996 --log` for the fourth. All four runs are published in full below,
  with the dispersion stated rather than hidden behind a single number. The other
  two: run **33704034596** (job `postgres`, **100489214750**, 2026-09-03T01:32Z,
  roughly 1 hour 32 minutes after the fourth) and run **33705109711** (job
  `postgres`, **100493794533**, 2026-09-03T01:48Z, 16 minutes after that) -- same
  `postgres:16` image and `ubuntu-latest` label as the four above -- were both
  read and neither lowered the floor, so neither earns a table; see the observed
  log below for their own worst rates rather than trusting this sentence. An
  earlier draft of this section quoted a **local Homebrew PostgreSQL 17.11** run
  instead (no Docker daemon was available to run `postgres:16` locally at the
  time); the first CI run alone already measured 2.96x slower than that laptop
  (287.4 vs 97.0us/row at n=25,000). See `src/ontary/diagnose.py`'s
  `STORAGE_ENVELOPE["PostgresStore"]` comment for all four superseded figures
  (the laptop number, and the three earlier CI-derived numbers that followed it),
  kept there as labelled provenance.

### Cost by operation and row count

Bounded, unordered pages are **flat** -- they cost the same at 100,000 rows as at
1,000. Everything that must inspect the whole visible population (`count`, `exists`,
`aggregate`, and any narrow-scope read whose limit outruns what the consumer can see)
is **linear in TOTAL rows, not visible rows** -- the counter-intuitive case is the
narrow-scope column below: a consumer who can see 1% of a type's rows still pays for
100% of them, because the engine must inspect every row to know which 1% is theirs.

`ObjectStore` (SQLite):

| Rows | Bounded page (`limit=1000`) | `aggregate` (~99% visible) | narrow-scope page (~1% visible) |
| ---: | ---: | ---: | ---: |
| 1,000 | 30.8ms | 20.6ms | 20.9ms |
| 5,000 | 35.6ms | 108.2ms | 110.2ms |
| 10,000 | 42.4ms | 222.7ms | 224.4ms |
| 25,000 | 36.9ms | 621.8ms | 613.0ms |
| 50,000 | 37.4ms | 1,465.0ms | 1,373.3ms |
| 100,000 | 37.8ms | 3,089.4ms | 3,002.9ms |

`PostgresStore` (PostgreSQL, CI `postgres:16` service -- see the environment note
above; only the three row counts the CI step runs, unlike `ObjectStore`'s six).
Published as **four runs** -- every run so far that has set or lowered the floor,
per the publication rule above -- not one, because a single CI sample cannot support
a ceiling quoted to two significant figures (see the dispersion note below). Two
further runs were read and did neither; they are recorded in the observed log below
instead of getting a table here:

#### Run 33623316425 -- 2026-09-02T11:12Z, job `postgres` (100225149283)

| Rows | Bounded page (`limit=1000`) | `aggregate` (~99% visible) | narrow-scope page (~1% visible) |
| ---: | ---: | ---: | ---: |
| 1,000 | 311.0ms | 254.9ms | 344.0ms |
| 5,000 | 354.1ms | 1,494.6ms | 1,554.0ms |
| 25,000 | 326.0ms | 7,186.0ms | 7,792.1ms |

#### Run 33630090428 -- 2026-09-02T12:29Z, job `postgres` (100247114881)

| Rows | Bounded page (`limit=1000`) | `aggregate` (~99% visible) | narrow-scope page (~1% visible) |
| ---: | ---: | ---: | ---: |
| 1,000 | 582.8ms | 539.3ms | 542.3ms |
| 5,000 | 616.6ms | 2,740.6ms | 2,712.7ms |
| 25,000 | 605.0ms | 13,845.2ms | 13,754.1ms |

#### Run 33651633373 -- 2026-09-02T15:56Z, job `postgres` (100319828397)

| Rows | Bounded page (`limit=1000`) | `aggregate` (~99% visible) | narrow-scope page (~1% visible) |
| ---: | ---: | ---: | ---: |
| 1,000 | 589.6ms | 541.6ms | 545.0ms |
| 5,000 | 650.5ms | 2,818.6ms | 2,763.3ms |
| 25,000 | 609.4ms | 13,934.1ms | 13,951.4ms |

#### Run 33697469943 -- 2026-09-02T23:59Z, job `postgres` (100469367996)

| Rows | Bounded page (`limit=1000`) | `aggregate` (~99% visible) | narrow-scope page (~1% visible) |
| ---: | ---: | ---: | ---: |
| 1,000 | 670.1ms | 593.6ms | 608.6ms |
| 5,000 | 690.5ms | 2,982.7ms | 2,973.4ms |
| 25,000 | 643.9ms | 14,693.7ms | 14,652.9ms |

**Run-to-run dispersion.** These four runs span 2026-09-02T11:12Z to
2026-09-02T23:59Z (run 1 -> run 2 is 77 minutes; run 2 -> run 3 is 3 hours 27
minutes; run 3 -> run 4 is roughly 8 hours; run 1 -> run 4 is roughly 12 hours 47
minutes), on the same `verify` workflow, the same `postgres:16` service container
(PostgreSQL 16.15, Debian 16.15-1.pgdg13+2, identical in all four), and the same
`ubuntu-latest` runner label. On the two linear classes (`aggregate`, narrow-scope
page) the twenty-four published rates -- four runs x three row counts x two
classes -- **span 254.9-608.6us/row**: state the span, not a single headline
ratio, because the four runs are not cleanly ordered against one another. Run 1 is
uniformly the fastest of the four: its slowest linear-class sample (344.0us/row,
narrow @ n=1,000) is still faster than every linear-class sample in runs 2-4. Run
4 is uniformly the slowest of the four: its fastest linear-class sample
(586.1us/row, narrow @ n=25,000) still exceeds every linear-class sample in runs
1-3. Run 2 and run 3, however, are NOT cleanly ordered against each other: run 2's
fastest sample (539.3us/row) is marginally faster than run 3's fastest
(541.6us/row), while run 3's slowest sample (563.7us/row) exceeds run 2's slowest
(553.8us/row). "Each new run is slower than the last" is therefore not a rule this
data supports uniformly -- only "the worst sample seen across every run published
so far" is a stable quantity, which is exactly what the floor below tracks. At the
top of the tested range (n=25,000) -- the retired rule's own vantage point -- which
class is slower has now flipped three times across the four runs: run 1's
narrow-scope class exceeds aggregate by 8%; run 2's aggregate exceeds narrow-scope
by 0.7% (flip 1); run 3's narrow-scope again exceeds aggregate, 558.1 vs
557.4us/row (flip 2); run 4's aggregate again exceeds narrow-scope, 587.7 vs
586.1us/row (flip 3) -- so "whichever class is slower at n=25,000" is not a stable
rule either, which is exactly why the published floor is the max across every
class and every n, not a rule that commits to one class up front. The flat
bounded-page column does not follow even the run-1-vs-run-2 pattern: run 2's
bounded-page minimum (24.2us/row, at n=25,000) is faster than run 1's bounded-page
maximum (311.0us/row, at n=1,000). Spec `storage-envelope.md` §3 moved this
measurement onto CI specifically because CI was "re-verifiable on fixed hardware";
**that premise is false**. A GitHub-hosted `ubuntu-latest` runner is a shared VM
with variable co-tenancy, not fixed hardware, so repeated runs of the identical
step can legitimately differ by more than a factor of two. A reader who re-runs
`scripts/scan_curve.py` against CI should therefore expect a number that may land
outside every table above, not an exact reproduction of any of them.

The published ceiling below is accordingly not "the" CI number: it is the
**conservative floor** -- the round number just under the one-second crossing
implied by the SLOWEST per-row rate in any row of any of the four tables above,
across both the `aggregate` and narrow-scope classes (worst: run 4's
narrow-scope page @ n=1,000, 608.6ms / 1,000 rows = 608.6us/row -- **not** the top
of the tested range: run 4's own narrow-scope rate FALLS from 608.6us/row at
n=1,000 to 594.7us/row at n=5,000 and 586.1us/row at n=25,000, so the curve is
non-monotone yet again, this time descending rather than peaking in the middle;
the retired "top of the tested range" rule selects whichever class is slower in
that run -- at n=25,000 that is `aggregate` (587.7us/row, 14,693.7ms / 25,000
rows), not the narrow-scope figure above -- and would still have missed the true
worst point, by 3.4%). A future, slower CI sample only ever
**lowers** this ceiling -- deliberate, because the envelope is advisory: a lower
ceiling warns an operator earlier, which is the safe direction to be wrong in. A
future, FASTER CI sample never raises it: the rule takes the worst rate across
every published sample, not the most recent one. Two more executions of this same
step were read after run 4 and are not among the four tables above -- not because
they were never run, but because neither undercut this floor: the observed log
immediately below names both and states the arithmetic that confirms it.

#### Observed log: every CI run read, published or not

Every execution of the `postgres` job's storage-envelope curve step is listed here
by id, whether or not it earned a table above. This is what the publication rule
above means in practice: a sample that does not set or lower the floor is still
visible -- as a row here, by id and its own worst per-row rate across every row and
both linear classes -- rather than silently absent, so a reader cannot mistake "not
published" for "not measured."

| CI run id | `postgres` job id | Worst rate (any row count, either linear class) | Published as |
| ---: | ---: | ---: | :--- |
| 33623316425 | 100225149283 | 344.0us/row | Run 1 |
| 33630090428 | 100247114881 | 553.8us/row | Run 2 |
| 33651633373 | 100319828397 | 563.7us/row | Run 3 |
| 33697469943 | 100469367996 | 608.6us/row | Run 4 |
| 33704034596 | 100489214750 | 555.3us/row | not published -- below the floor |
| 33705109711 | 100493794533 | 351.5us/row | not published -- below the floor |

The last two rows are runs 5 and 6 in this step's own history, not this doc's: run
33704034596 (2026-09-03T01:32Z) worst rate is 555.292us/row (`aggregate_mean` @
n=25,000: 13,882.3ms / 25,000 rows), and run 33705109711 (2026-09-03T01:48Z) worst
rate is 351.480us/row (`narrow_page_1000` @ n=5,000: 1,757.4ms / 5,000 rows). Both
sit below run 4's 608.6us/row (by 8.8% and 42.2% respectively), so the published
floor is unmoved by either -- re-derive both directly rather than trusting this
table: `gh run view --job 100489214750 --log` and `gh run view --job 100493794533
--log`. `OBSERVED_POSTGRES_RUN_IDS` in `tests/test_docs.py` pins the exact SET of
ids this table is allowed to contain -- an equality against this table's own `CI run
id` column, and a superset of `PUBLISHED_POSTGRES_RUN_IDS` -- so a row silently
removed from this table (hiding a sample that was read), or a row silently added to
it without becoming a pinned id (an unvouched-for extra), both fail `make verify`.
This closes silent SELECTION -- a run that was read and discarded from the published
tables now stays visible here -- but not silent OMISSION: a run nobody bothered to
add a row for at all is invisible to this table and to the test that parses it,
because the offline suite cannot reach GitHub to discover it on its own.

The under-one-second ceiling below is a governed `aggregate` or narrow-scope read; a
bounded page has no measured ceiling on either backend:

| Backend | Label | Under-one-second ceiling |
| --- | --- | --- |
| `ObjectStore` | SQLite | ~32,000 rows |
| `PostgresStore` | PostgreSQL | ~1,600 rows |

`ontology.diagnose(store=...)` fires `STORAGE_ENVELOPE_EXCEEDED` for a type whose row
count (for the *current tenant*) exceeds its backend's ceiling above, and stays silent
under it, with no store, or for a backend with no published ceiling (never a guessed
threshold).

### The loopback caveat, and the memo's payoff under it

Both curves above were measured over **loopback TCP** (this machine talking to
itself; CI's `postgres:16` service is loopback too, ~0.05ms per round-trip). A real
deployment's round-trip time is typically 0.5-2ms. Before this decision, resolving one
row's scope cost **7 store calls** (5 `read_current` + 2 link reads, all network round
trips on Postgres) -- at a real RTT that gap dominates the envelope: `7 x 1ms x 50,000
rows` is ~350s on its own. So a Postgres number is never quoted bare: an operator
adjusts the measured curve above by

    + 7 x RTT x rows

using their own network's measured round-trip time, to estimate cost at their real
latency instead of loopback's.

That `7` is what the 0.9 scope-resolution memo (`ontary.scope._ScopeReadCache`, a
per-guarded-read cache with no store-lifetime state) targets, and **its value grows
with network latency**: profiling this section's own `aggregate()` call against
`PostgresStore` (8,000 rows) shows exactly 16,006 store-level calls -- **~2 per row**,
down from 7 -- because only the reads keyed by each row's own unique id (which object
does this row belong to?) cannot be shared across rows; the reads for a *shared*
parent (the same Queue, the same Org) are issued once per guarded read and reused for
every other row that resolves to them. So for the memo'd 0.9 paths the same correction
is closer to

    + 2 x RTT x rows

Measured at 25,000 rows, with the memo forced off via a script-local stand-in
(`scripts/scan_curve.py --uncached` -- see its own docstring; no product code gained a
public toggle). Each row below is a SINGLE sample, not a dedicated benchmark suite,
and the two are not equally evidenced -- named explicitly rather than left as an
anonymous pair of percentages:

| Backend | `aggregate` memo speedup | narrow-scope memo speedup | Source |
| --- | --- | --- | --- |
| `ObjectStore` (SQLite) | 2.45x | 2.58x | one local laptop sample -- same evidentiary class as the `ObjectStore` ceiling above: never CI-produced, not independently re-derivable by a reader |
| `PostgresStore` (PostgreSQL) | 3.02x | 3.10x | run 1's (`33623316425`) own n=25,000 measurement, published in full above -- the `aggregate_mean_memo_speedup`/`narrow_page_1000_memo_speedup` fields in `gh run view --job 100225149283 --log`'s raw JSON output, not a separate benchmark run |

Both clear the >= 2x this decision requires on both backends, and Postgres's is the
larger of the two -- consistent with the memo removing network round trips, which cost
more than the equivalent local SQLite call. Postgres's own memo speedup varies run to
run just like its per-row rate does; 3.02x/3.10x is run 1's own pair, not a claimed
best or worst case -- re-derive any other published run's own pair the same way from
its raw log rather than assuming this one generalizes.

### Read consistency: an implementation detail, not a guarantee

Within one guarded read, the scope memo reads each parent row (and each link query)
**at most once** and reuses that answer for every other row in the same read that
resolves to the same parent. Before 0.9, each row re-resolved its own scope
independently, so a write landing on a shared parent mid-read could be visible to some
rows and not others within a single governed read; the memo makes that one read
internally consistent where it previously was not.

This is an **implementation detail of the current scope-resolution engine, not a
durable API guarantee**: the memo is threaded only through the engine's own
`resolve_owning_scope` reads. A `CustomResolver` or a `ScopePolicy.row_visibility`
predicate still receives the raw, uncached store (or `RowVisibilityStore`) on every
call, exactly as before 0.9, and a later release may resolve scope a different way
without this note changing. Do not depend on read-once-per-guarded-read as a promise
the way the tenant boundary or a documented error code are promises.

### Does this change the JSONB answer?

No -- **reaffirmed, not corrected**. The line above ("The engine does not query
inside payload JSON, so JSONB normalization would buy no runtime behavior") predates
this decision and stays true because of what this decision found, not despite it: the
dominant linear cost this envelope publishes is Python-side scope resolution, not SQL
scanning. The plain per-row scan (`store.read_all`, no scope resolution at all) is
unchanged by the 0.9 memo -- both before and after it, ~6.5-6.7us/row (100,000-row
`ObjectStore`: `read_all_raw` 665.3ms, 6.65us/row, matching the pre-memo measurement
almost exactly, because the memo touches scope resolution, not row scanning). Before
the memo that was ~10% of the total cost; the memo shrank the OTHER 90% (scope
resolution) without touching the scan, so today it is a larger slice of a smaller
total -- ~22% of the measured `ObjectStore` `aggregate` cost above (6.65 / 30.9us/row)
-- and scope resolution is still the majority. SQL pushdown of
`where`/`order_by`/aggregates stays deferred past 1.0 for exactly this reason: it
would only ever address the scan slice, whatever share that is. A payload column type
change only ever helps that same slice, and only paired with real pushdown. Nothing
evaluates payload fields inside the database today, so `TEXT` vs `JSONB` buys nothing
to reverse.

## Tenancy is bound to the store

A store instance represents one tenant. Pass that tenant when constructing the
store; there is no request-scoped tenant switch:

```python
acme = PostgresStore(ontology.registry, dsn, tenant="acme")
globex = PostgresStore(ontology.registry, dsn, tenant="globex")

acme.insert("Widget", {"id": "w-1"}, source)
assert globex.read_current("Widget", "w-1") is None
```

The boundary covers objects, links, audit entries, and durable effect-outbox rows.
Primary keys are unique within a tenant rather than across the whole deployment,
so two tenants may use the same domain identifier without colliding. The tenant is
fixed at construction precisely because a per-call argument is easy to omit and a
missed argument would have a cross-tenant failure direction.

On PostgreSQL, `rls=True` creates `FORCE` row-level-security policies keyed by the
current session tenant. An unset session setting matches nothing. A superuser can
bypass PostgreSQL RLS by database design, so application connections must use an
ordinary role. `rls=False` disables only this second database layer; the engine's
tenant predicates still run.

Database- or schema-per-tenant is also valid when stronger physical isolation is
worth the operational cost. The shared-schema option is for deployments where a
single database is the better fit.

## Schema version compatibility

SQLite stamps each file with `SCHEMA_VERSION` through `PRAGMA user_version`. A file
with a higher stamp is refused with `ConflictError` and code
`STORE_VERSION_UNSUPPORTED`, naming the engine and file versions. A version-zero
file is inspected rather than trusted, because `CREATE TABLE IF NOT EXISTS` would
otherwise leave a narrower pre-existing table in place.

The known legacy pagination-column change is migrated in place, including its
backfill and index, before the file is stamped. If any other required column is
missing, opening the file raises `ConflictError` with code
`STORE_SCHEMA_INCOMPATIBLE` and leaves the stamp untouched. A schema stamp is never
written for a file the engine cannot actually read.

The SDK simplification does not bump the schema version. Existing store files stay
on the compatible schema path; check [the compatibility policy](compatibility.md)
before changing an ontology definition or moving a store between SDK versions.

## Operator erasure runbook

For a data-subject erasure request, run `ontary erase pkg.module:ontology
--store ontary.sqlite --type Person --id person-123 --operator privacy-ops`.
The command purges content from the object's current and historical rows, matching
audit parameters, and effect-outbox payloads while structure and lineage survive
in audited tombstone rows; this matching is structural, not a completeness
guarantee. Detection reaches a bare `id`, the generic `object_id`/`object_ids`/
`target_id`/`target_ids` pairs, and `*_id`/`*_ids` keys whose trailing
underscore-separated stem segments, concatenated case-insensitively, name the
erased object type; a mismatched paired `*_type` suppresses the match. It does
not reach `owner_id`, `actor_id`, or `assignee_id` on an unrelated stem, prose
mentions of the subject in `params`, free text in `EffectRecord.error`, or free
text in outbox `last_error`. Commit `5445af8` added clearing of effect
payloads/errors and outbox `last_error` for records matched by another
reference, and R1 added structured detection through `audit_log.writes`; the
listed residuals still do not trigger detection by themselves, so operators
must review and handle them manually.
`--operator` defaults to the OS user when omitted. Repeating an already
completed request with no newly matched content prints the coded
`OBJECT_ALREADY_ERASED` no-op report and exits successfully; a repeat with late
content runs the purge again and reports a real erasure. A never-stored id
prints `OBJECT_ERASURE_NOT_FOUND` and exits non-zero.

[Return to the README](../README.md) · [See the full storage API](api-reference.md#stores)
