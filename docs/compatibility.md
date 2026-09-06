# Compatibility policy

What `ontary` promises across versions, and what it does not. Written for a team
outside this repo that is about to pin a version.

The short version: **while the major version is `0`, a minor bump may break you, and a
patch bump may not.** Everything below is the detail that makes that sentence usable —
in particular *what counts as breaking*, which for a governance SDK is a wider set than
"the function signature changed".

## Versioning

`MAJOR.MINOR.PATCH`, SemVer-shaped, with the standard 0.x caveat: **`0.MINOR` behaves as
the major**. `0.2.0` may break what `0.1.0` promised; `0.1.1` may not.

Pin accordingly. From an index, that is a range:

```toml
# Recommended while the SDK is pre-1.0: allow patches, review minors.
dependencies = ["ontary>=0.7,<0.8"]
```

This SDK is **not on an index** — it ships from a private repository as a pinned git
ref, so a range is not available and the pin is an exact tag:

```toml
dependencies = [
  "ontary @ git+https://github.com/ryoochi0112/ontary@v0.7.0",
]
```

The practical difference: **a git ref gives you no patch updates for free.** Moving to
`v0.7.1` is an edit, which is more friction but also means nothing changes underneath you
between installs. Pinning `@main` gets the friction back to zero and throws away every
guarantee on this page — do not.

`ontary.__version__` reports the installed version at runtime — log it next to any
audit export, because the meaning of what you exported depends on it.

## What counts as a breaking change

Beyond the obvious (removing or renaming a public name in `ontary.__all__`, changing a
signature incompatibly), **all of the following are breaking**, and this is the part
worth reading:

1. **A declared answer changes.** The strings on `Declarations` — `authority`,
   `capabilities`, `effects`, `writeback`, `reingest`, `visibility_default`,
   `transaction_ownership`, `ontology_evolution`, `idempotency`, `audit_scope`,
   `tenancy`, `identity`, `min_n` — are the runtime's contract with an auditor.
   Changing what one *says* means the behavior it describes changed.
   The M7c outbox is the worked example: `effects` went from "at-most-once, no retry" to
   at-least-once, which silently turned every non-idempotent dispatcher into a latent
   duplicate-send bug. That was a minor bump with the obligation stated in the changelog,
   the README, and the declared string itself.
2. **An error code's meaning changes, or a code disappears.** `ERROR_CODES` is public
   API: integrations branch on `code`, and the MCP surface puts it on the wire. Adding a
   code is additive; repurposing one is not.
3. **An `EffectMeta`, `AuditEntry`, `EffectRecord`, or `OutboxRecord` field is removed or
   re-meant.** Dispatchers and audit readers destructure these.
4. **A refusal becomes permissive, or a permissive path starts refusing.** A change that
   makes the engine *allow* something it used to deny is breaking in the direction that
   matters most for governance, even though no caller's code stops compiling.
5. **`SCHEMA_VERSION` increases.** See the next section — this one has an operational
   procedure attached, not just a note.
6. **A change to the descriptor IR that alters the ontology fingerprint.** Since M9a, a
   store refuses to open under a declaration it was not written under, so an SDK change
   that adds a field to `ObjectTypeDef` (or otherwise moves the digest) makes every
   existing store refuse until its fingerprint is accepted. Additive to the *code*,
   operationally breaking for the *data*.

Additive and therefore **not** breaking: a new export, a new optional parameter with a
default that preserves current behavior, a new error code, a new audit field, a new
`Store` protocol method *implemented on both shipped backends*.

## Store files and `SCHEMA_VERSION`

The SQLite store file carries its own stamp in `PRAGMA user_version`, independent of the
package version. Current: **9**.

| Version | Added |
| --- | --- |
| 1 | `objects.page_token` |
| 2 | `audit_log.effects`, `audit_log.capability_accesses` |
| 3 | `audit_log.invocation_id` (nullable — old rows keep `None`, never a backfilled id) |
| 4 | `effect_outbox` table |
| 5 | `audit_log.kind` (backfilled `'action'` — every prior row genuinely was one) |
| 6 | `ontology_fingerprint` table (see *Changing your own ontology* below) |
| 7 | `objects.type_version` + `ontology_fingerprint.versions` (declared type versions) |
| 8 | `tenant` on `objects`, `links`, `audit_log`, `effect_outbox` (backfilled `'default'`) |
| 9 | `audit_log.principal` (nullable — a historical row means "we do not know who authenticated," never a backfilled/invented value; see *Multi-consumer MCP serving* in the README) |

Two rules, both learned the hard way:

- **Upgrades are automatic and forward-only.** Opening a file stamped lower migrates it
  in place, under one transaction, and stamps the new version. Every migration this
  engine owns is additive and idempotent, so an interrupted upgrade re-runs correctly
  and a file that skips versions (3 → 5) lands in a single pass. A file stamped *higher*
  than the running engine is refused outright with `STORE_VERSION_UNSUPPORTED` rather
  than opened and left to fail on the first query that touches an unknown column.
- **A version bump requires a quiesced rollout.** Because the upgrade is forward-only and
  the older binary refuses the newer stamp, **stop every writer before upgrading any of
  them.** A mixed fleet does not degrade gracefully: the first upgraded process stamps
  the file, and every process still on the old version starts refusing it at
  construction. Plan the bump as a brief stop-the-world, not a rolling deploy.

There is deliberately **no downgrade path**. Keep a copy of the file if you need to roll
back the code.

## Changing your own ontology

This policy covers the SDK and the store's physical schema. Your *ontology* — the object,
link, and action types you declare — is versioned differently, and as of M9a it is no
longer undefined.

**A store remembers the ontology its rows were written under.** Every store records a
fingerprint of the declared descriptor IR. Open it with a different declaration and it
refuses at construction with `ONTOLOGY_DRIFT`, naming each type that changed:

```
ontary.errors.ConflictError: the declared ontology does not match the one this store's
rows were written under: object 'Widget': declaration changed. Migrate the affected rows
(ontary.migrate.migrate_object_type, then accept_ontology_fingerprint), or open the
store with accept_ontology_drift=True to proceed and record that you did
```

Documentation-only fields (`description`, `display_name`, and a function's
input/output descriptions) are excluded from the fingerprint: a check that fires on a
docstring edit is a check people switch off.

### Fingerprints and 0.7 property types

The 0.7 type-system extension is fingerprint-neutral for an existing ontology: if an
ontology uses none of the new property features, its `OntologyFingerprint` digest is
byte-identical to the digest produced by 0.6.0. Upgrading the SDK alone therefore does
not require `accept_ontology_drift`.

`PropertyDef.choices` defaults to `None` and that default is omitted from the canonical
fingerprint, so existing string properties remain byte-identical too. Declaring
`prop(choices=["open", "closed"])` does change that ontology's own fingerprint because
the allowed stored values are now part of its declared shape. Declaring a property with
`type="date"` likewise changes that ontology's fingerprint, just as adding or retyping
any declared property does. Date values are persisted as ISO `YYYY-MM-DD` strings and
hydrate as `datetime.date`.

Plan and acknowledge either declaration change through the ordinary version/upcaster
workflow below. For an explicit drift acceptance, either open the store with
`accept_ontology_drift=True` (which restamps and audits the new fingerprint), or call
`accept_ontology_fingerprint(store, registry)` on an already-open store after any
required row migration. An existing out-of-set string must be migrated before accepting
a new `choices` constraint; otherwise reads reject that row with `INVALID_RECORD`.

### Two ways to change a type

**Version it (usually this one).** Bump the type's `version`, declare one upcaster per
step, and old rows keep reading as the current shape:

```python
@ontology.object(layer="L0", version=2, scope=[...])
class Widget(OntologyObject):
    id: str = prop(primary_key=True)
    label_v2: str | None = None

@ontology.upcaster(Widget, from_version=1)
def widget_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    payload["label_v2"] = payload.pop("label", None)
    return payload
```

The store then opens **without any acknowledgement** — the rows are readable exactly as
declared, so there is nothing for a human to accept. It is still recorded: a
`kind="migration"` audit entry names what was covered.

This is the governance property worth stating plainly: **you may evolve a type, but you
must version it.** A changed declaration at the *same* version is still refused as drift,
because nothing distinguishes it from an accident.

Three things to know before you rely on it:

- `ontology.validate()` refuses an **incomplete chain** — declare a step for every version
  a row might be sitting at. One step per registration; a v1 → v3 jump is two functions,
  because a jump is useless to the row that happens to be at v2.
- The chain runs **on every read of an old row, forever**, and cannot be deleted while one
  row still needs it. `upcast_object_type(store, registry, "Widget")` rewrites the rows
  through the same declared chain and stamps them current, after which the read path does
  nothing and the upcaster becomes deletable dead code.
- An upcaster that raises fails the **read** with `UPCAST_FAILED`, naming the row, the step,
  and the author's error. It never falls back to the raw payload: handing a consumer data in
  a shape the declaration says does not exist is the failure this whole area is about.

A **downgrade** — running code that declares an older version than the store was written at
— is refused, for the same reason a newer `SCHEMA_VERSION` file is.

### Rewriting instead, in order

1. **Change the declaration** and run your tests. The store will refuse to open.
2. **Migrate the rows.** `migrate_object_type(store, registry, "Widget", transform)` walks
   every current row, applies your transform, validates each result against the *new*
   declaration, and writes in batches. `dry_run=True` reports exactly what it would do,
   failures included. One row whose transform raises does not abort the run — it lands in
   `report.failures` with its object id.

   Open the store with `accept_ontology_drift=True` for this step: you are deliberately
   working on a store the check would otherwise refuse, and the acceptance is audited.
3. **Accept the new shape** with `accept_ontology_fingerprint(store, registry)` once every
   affected type is migrated. Deliberately separate from step 2 — "the rows are migrated"
   and "this declaration is now the shape of record" are different claims, and a
   migration usually touches one type at a time.

Two properties of the tooling worth knowing before you rely on it: **your transform must
be idempotent** (batches commit as they go, so the fix for a crash mid-run is to run it
again), and a **dropped key is nulled rather than deleted** — `Store.update` merges, so
the old *value* is gone but the key survives as `null`.

### An additive change may need no migration at all

Adding an optional property, or a required one whose rows already satisfy it, changes the
fingerprint without invalidating any row. Acknowledge it and move on:

```python
store = ObjectStore(registry, path, accept_ontology_drift=True)
```

That is a call-site argument on purpose — not an environment variable, not a config key —
and it appends a `kind="migration"` audit entry recording both digests and what changed.
Someone taking the risk names it where the store is opened, and the log says they did.

### Rows are also validated on the way in now

`Store.insert`/`update` — and so `ActionContext.insert`/`update`, the path every action
writes through — validate against the declared shape, the way `bulk_upsert` always has.
Before M9a only ingest checked, so an action could commit a row the same ontology's typed
reader then refused to hydrate. **This is a breaking change** for any code that was
writing rows violating its own declarations: those writes now raise `INVALID_RECORD`.
Undeclared extra keys are still allowed — hydration ignores them, and real rows carry
them.

### What is still not covered

Per-type versions and read-time upcasters (the spec's options B and C) are deliberately
not built. There is no way to say "this row is at version 2 of `Widget`" and have the
engine translate it on read; the model is *rewrite the rows, once, explicitly*. If you
need old and new shapes to coexist indefinitely, that is a milestone that has not
happened — say so rather than working around it, because the workaround is a store whose
rows do not match their declarations, which is exactly what this milestone exists to
prevent.

## Storage backends

Three backends satisfy one `Store` protocol, and the conformance suite is what makes
"satisfy" mean something: 64 behavioral assertions run against each backend (192 across the three). A backend that
diverges is a bug in the backend, not a documented difference — the one time this repo
shipped such a divergence (`InMemoryStore` dropping `invocation_id`) it was a defect and was
fixed as one.

The shared transaction contract includes isolation, not only rollback: within one
outermost `transaction()`, a read followed by a write is serialized against concurrent
writers on the same logical store. Nested `transaction()` calls remain reentrant and
join that outer transaction.

`SCHEMA_VERSION` is shared, but what it means differs:

- **SQLite** (`ObjectStore`) carries the full v1 → v8 migration ladder, because files written
  by every past version exist. Upgrades are automatic, forward-only, and require the quiesced
  rollout described above.
- **Postgres** (`PostgresStore`) ships *at* the current version with **no ladder** — still, even
  now that `SCHEMA_VERSION` has moved (8 → 9, M10). This gap was named here in advance rather
  than discovered at upgrade time, and this is the move that gap warned about: it stays safe
  only because no *released* ontary ever wrote a v8 Postgres store to begin with — `0.1.0`
  shipped `SCHEMA_VERSION` **5**, and every schema bump since (6, 7, 8, and now 9) landed after
  that tag, on `main`, never in a tagged release a Postgres deployment could have pinned. Nothing
  has ever written a Postgres store with an older ontary, so there is nothing to migrate *yet*; a
  database at any other version — or one holding an `objects` table with no ontary stamp — is
  refused with `STORE_VERSION_UNSUPPORTED` rather than adopted. **The gap is still open, and
  narrower than it looks:** the day a Postgres deployment is actually running a tagged release
  below the current `SCHEMA_VERSION`, that deployment has no migration path, and today's answer
  remains dump, recreate, and reload.
- **In-memory** has no persistence and therefore no version to check.

### Tenancy

Every row carries a `tenant`, and a store is bound to one at construction: reads and writes
are scoped to it on all three backends. **On Postgres the database enforces it too**, via
row-level security policies keyed on a session variable, so a query missing its tenant
predicate still returns only the current tenant's rows.

What that does and does not promise:

- A store serves **one** tenant. There is no request-scoped tenant switch; a process holds one
  store per tenant.
- Primary keys are unique **per tenant**, not globally.
- `rls=False` disables the database layer only (for a role that cannot own the tables — policies
  are DDL). The engine's scoping still applies.
- **A superuser bypasses RLS**, by Postgres design. An application role must not be one.
- Schema- or database-per-tenant is still available and still stronger: the isolation is the
  database's rather than this engine's.

Adding the tenant column was `SCHEMA_VERSION` 7 → 8 with a `'default'` backfill — rows written
before tenancy existed belong to the one implicit tenant that wrote them, so the backfill states
a fact. An existing single-tenant deployment keeps working without passing `tenant` at all.

## Deprecations

While pre-1.0: a name being removed is deprecated for **at least one minor version**
before it goes, warned with `DeprecationWarning`, and the changelog entry names the
replacement. The exception is a change that closes a governance hole — if a permissive
path turns out to be a defect, it is fixed in the next release and called out at the top
of the changelog entry, because leaving it open for a version to be polite about it is
the wrong trade.

## Python and dependencies

- **Python**: 3.12+. Dropping a Python version is a minor bump pre-1.0, and is announced
  in the changelog.
- **pydantic**: `>=2.7`, and the core install depends on nothing else. Everything else —
  the MCP server, dlt/duckdb connectors, BigQuery — is an optional extra, so an adopter
  who wants only the governed runtime inherits only pydantic.
- **An extra's floor can rise, and that narrows what you can install.** It is not a
  breaking change to the API, but it can break a *resolution*, so it is announced in the
  changelog like any other. M10 raised the `mcp` extra from `>=1.2` to `>=1.27.2`:
  `AccessToken.subject`, which the multi-consumer server reads to stamp the audited
  principal, does not exist before 1.27.2 — pydantic silently dropped the field, so the
  old floor resolved to installs where that server could not work. If another package in your
  environment pins `mcp` below that, the multi-consumer server is not available to you;
  the single-consumer `build_mcp_server` has no such requirement.
