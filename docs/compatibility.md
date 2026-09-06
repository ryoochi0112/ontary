# Compatibility policy

What `ontary` promises across versions, and what it does not. Written for a team
outside this repo that is about to pin a version.

The short version: **while the major version is `0`, a minor bump may break you, and a
patch bump may not.** Everything below is the detail that makes that sentence usable —
in particular *what counts as breaking*, which for a governance SDK is a wider set than
"the function signature changed".

When `v1.0.0` may be tagged is defined in the [v1.0.0 acceptance gate](v1-gate.md).

## Versioning

`MAJOR.MINOR.PATCH`, SemVer-shaped, with the standard 0.x caveat: **`0.MINOR` behaves as
the major**. `0.2.0` may break what `0.1.0` promised; `0.1.1` may not.

Pin accordingly. From an index, that is a range:

```toml
# Recommended while the SDK is pre-1.0: allow patches, review minors.
dependencies = ["ontary>=0.10,<0.11"]
```

This SDK is published on [PyPI](https://pypi.org/project/ontary/), so the range above
resolves compatible patch releases. It is also installable as a pinned git ref for
consumers who do not resolve from an index; that fallback has no range and uses an
exact tag:

```toml
dependencies = [
  "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0",
]
```

The practical difference: **a git ref gives you no patch updates for free.** Moving to
`v0.10.1` is an edit, which is more friction but also means nothing changes underneath you
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
`Store` protocol method *implemented on all three shipped backends* (`Store` is
engine-internal -- see *Storage backends* below).

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

Per-type versions and read-time upcasters (the spec's options B and C) are implemented.
Declare the type's `version` and one `@ontology.upcaster(..., from_version=...)` for
each step; reads apply the complete chain to old rows, and `upcast_object_type` can
rewrite those rows permanently. What is still not covered is automatic background
rewriting or a downgrade path: the chain must remain available while old rows exist,
and a permanent rewrite is an explicit, idempotent maintenance operation. A deployment
that needs a different version-retention or rollout policy must provide that policy
around the SDK.

## Storage backends

Three backends satisfy one `Store` protocol, and the conformance suite is what makes
"satisfy" mean something: 64 behavioral assertions run against each backend (192 across the three). A backend that
diverges is a bug in the backend, not a documented difference — the one time this repo
shipped such a divergence (`InMemoryStore` dropping `invocation_id`) it was a defect and was
fixed as one.

**`Store` is engine-internal, not a public extension point.** A 2026-09-01 human
ruling, recorded in `CHANGELOG.md`'s `[0.9.0]` entry, withdrew the earlier invitation
for a third party to write and use their own `Store` implementation: matching the
protocol's method signatures no longer makes a class a supported backend. `Store`
remains exported from `ontary.__all__` — removing it would itself be breaking, and the
type is still needed in annotations — but only these three, `ObjectStore`,
`InMemoryStore`, and `PostgresStore`, are supported. This is why a new required
`Store` protocol method is classified as additive, not breaking, above once it is
implemented on those three: it needs implementing only where this project itself
ships an implementation, never on a caller's own class.

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

## Reads and pagination (0.6 → 0.8)

`get_objects` and `list` gained a `limit` parameter. It is **not** additive by the rule
above — its default changes both the return type and the number of rows you get back — so
this section is the migration note that break is owed.

On a consumer surface (`OntologyClient`, MCP), omitting `limit` now returns a `Page`
of at most `DEFAULT_READ_LIMIT` (1000) rows instead of every matching row:

```python
rows = client.list("Reading")          # 0.6: list[StoredObject], all rows
                                        # 0.8: Page, at most 1000 rows
rows = client.list("Reading", limit=None)   # every matching row, as before
```

A `Page` is not a sequence. Reading `.items` gives you the rows on that page and
`.next_cursor` gives you the key to ask for the next one; iterating, indexing or
measuring the page itself raises `PAGE_NOT_ITERABLE`. That refusal exists because
`Page` is a pydantic model, so the inherited iteration would otherwise hand you
`("items", [...])` and `("next_cursor", ...)` and fail much later, somewhere that
names neither the page nor the fix.

**Inside a declared Function the default is the opposite: `BoundQuery.list` is
unbounded.** A Function is author code deriving a value, not a consumer read, and a
value derived from a silently partial selection is wrong. The bound belongs on the
serving surface; carrying it into a Function made `count` and `list` disagree on the
same selection. Ask for `limit=` there and you get a `Page` on the same contract as
above.

If you are porting a Function from 0.6, it keeps working unchanged. If you are writing
a new one that pages deliberately, follow `next_cursor` to the end — the engine's own
`count`, `aggregate` and `count_contributors` all read the whole selection, so a
partial reduction will disagree with them and nothing will tell you.

## Function auditing (0.6 → 0.8)

A call that releases a hidden field through the AC10 contributor exemption now appends
an audit entry, whatever the function declared — `@ontology.function(audit=False)`
included. If your ontology declares `contributor_rules` and has a function that reduces
a hidden field, that function wrote no audit row on 0.6 and writes one per releasing
call now.

This is a governance change, not a signature one: nothing in the API moved, and no code
needs editing. What changes is your audit log's volume and, if you were relying on
`audit=False` to keep a hot path quiet, that it is no longer quiet on the calls that
release. Sizing it is straightforward — one row per call that actually releases, not per
call to the function, and not per function in the ontology:

```python
@ontology.function(api_name="teamMean")            # no capabilities declared
def _team_mean(query: BoundQuery, _params: dict) -> float:
    return query.aggregate(Reading, "raw_score")   # hidden from this consumer
# 0.6: returns the mean, audit log unchanged
# 0.8: returns the same mean, and appends one kind="function" entry
```

A call that opens the exemption but is then refused by min-N released nothing and
appends nothing. A function in the same ontology that only reads visible fields is
unaffected. See `docs/api-reference.md`, "The function audit boundary".

## `aggregate_by` group keys (0.6 → 0.8)

Five reads that used to return a number now refuse. Four returned an answer that did not
describe the selection asked for; the fifth could return a correct answer but is now
withheld for disclosure. There is no version of "keep the old behaviour" worth offering —
but these are behaviour changes, not additions, and an upgrading consumer sees them
without editing any code.

| what you wrote | 0.6 | 0.8 |
| --- | --- | --- |
| `group_by` naming a field the type does not declare | the ungrouped value, keyed `"None"` | `UNKNOWN_FIELD` |
| `group_by` naming a `json` property | `TypeError` (`INTERNAL_ERROR` over MCP) | `INVALID_GROUP_BY` |
| two group values that release as the same key | one cell, later population overwriting the earlier | `GROUP_KEY_COLLISION` |
| `aggregate` / `aggregate_by` / `count_contributors` on an unregistered type | `MIN_N_VIOLATION` | `UNKNOWN_OBJECT_TYPE` |
| hidden-field contributor-dedup `mean` or `count` with any non-empty `where`, or with any `group_by` | may release the narrowed or grouped value | `VISIBILITY_DENIED`; the exemption applies only to the complete visible population |

The third and fifth can appear on calls that were previously correct. For the third, that
is possible only for a `group_by` whose values are not all distinct under `str()`. The
fifth covers hidden-field contributor-dedup `mean` or `count` narrowed by any non-empty
`where` or by any `group_by`. In practice the third-row case means an
**optional** property: rows that lack it key `None`, which collides with a row carrying
the literal string `"None"`. If you group by an optional property, either fill it on
every row or group by a required one; a group legitimately keyed `None` with no such
collision still releases exactly as before.

The first two were already the behaviour of the **typed** overload
(`client.aggregate_by(Ticket, ...)`), which has validated `group_by` against the class's
declared properties since typed authoring landed. What changed is that the string form
and `BoundQuery` now agree with it.

## Bare `where` equality is type-checked (0.6 → 0.8)

`where={"field": value}` is the only equality spelling — there is no `eq` operator — and
until now it was the only `where` shape whose operand was never checked against the
property's declared type. Every mapping form (`gt`, `gte`, `lt`, `lte`, `in`, `ne`,
`contains`) already refused a mismatched operand with `OPERATOR_TYPE_MISMATCH`. The bare
form now does too, on all six read surfaces (`get_objects`, `count`, `exists`,
`aggregate`, `aggregate_by`, `count_contributors`) and through the typed, string,
`BoundQuery` and MCP entries alike.

| what you wrote, on a declared `int` property | 0.6 | 0.8 |
| --- | --- | --- |
| `where={"count": "2"}` | `[]` — matched nothing | `OPERATOR_TYPE_MISMATCH` |
| `where={"count": True}` | **the rows where `count == 1`** | `OPERATOR_TYPE_MISMATCH` |
| `where={"count": 1.0}` | the rows where `count == 1` | `OPERATOR_TYPE_MISMATCH` |
| `where={"flag": 1}` on a declared `bool` | **every row where `flag` is true** | `OPERATOR_TYPE_MISMATCH` |

The two bolded rows are why this is worth an upgrade note rather than a footnote: they
did not return nothing, they returned the **wrong rows**. Python's `bool` is a subclass
of `int`, so `True == 1` and the comparison quietly succeeded against a different value
than the one asked for. A count, an average, or an `exists` built on such a filter was
wrong without saying so.

Two shapes deliberately still work:

- **`None` is still the null test.** `where={"field": None}` selects rows where the
  property is absent, wherever the field is readable. It is unchanged.
- **`int` on a declared `float` is still widening, not a mismatch.** `where={"ratio": 2}`
  matches `2.0`, exactly as the mapping form already allowed.

To upgrade: pass the declared type. A filter that was silently matching nothing was
already not doing what it looked like; a filter that was matching on `bool`/`int`
coercion was returning an answer to a different question.

## `value_field` and `where=` are checked before the engine runs (0.6 → 0.8)

Two read parameters used to be handed to Python instead of being interpreted. Both now
refuse with a catalogued code, and both refusals are new — a call that returned a number
or a row set may now raise.

### An undeclared `value_field` is refused

`aggregate` and `aggregate_by` checked their `value_field` against the declared type
only when the type declared it at all; an unrecognized name skipped the check entirely.
`where=`, `order_by` and `group_by` have always refused such a name with
`UNKNOWN_FIELD`. `value_field` now agrees with its three siblings, on the string,
`BoundQuery` and MCP surfaces — the typed surface already refused it.

| what you wrote | 0.6 | 0.8 |
| --- | --- | --- |
| `aggregate("Rec", "scoer")` — a typo | `MIN_N_VIOLATION` naming `Rec.scoer` | `UNKNOWN_FIELD` |
| `aggregate("Rec", "shadow")` where rows carry an **undeclared** `shadow` | **a real number** | `UNKNOWN_FIELD` |
| `aggregate("Rec", "shadow")` where those values are not numeric | `ValueError`/`TypeError` | `UNKNOWN_FIELD` |

The middle row is the one that needs an upgrade decision, and it is reachable on purpose:
undeclared payload keys are **allowed** on write (a `ViaLink`-scoped row may legitimately
carry the source's own foreign key). Reducing over one released a number governed by no
declaration — no declared type, no `Sensitivity`, no scope routing — while every other
read parameter refused that same name.

To upgrade: **declare the property.** If rows carry a value you aggregate, the ontology
should say so; a declared `int`/`float` property aggregates exactly as before. Nothing
else in the read path changed — a declared but non-numeric `value_field` still raises
`NON_NUMERIC_AGGREGATE`, and a declared but hidden one still raises `VISIBILITY_DENIED`
under the same Function-provenance exemption.

### A `where=` that is not a mapping is refused

`where` accepted anything and then called `.items()` on it, so a non-mapping raised a
bare `AttributeError` from inside the engine — on all six read surfaces, not just one.
A *falsy* non-mapping was worse: it was treated as "no filter".

| what you wrote | 0.6 | 0.8 |
| --- | --- | --- |
| `exists("Rec", "r0")` | `AttributeError: 'str' object has no attribute 'items'` | `INVALID_PARAMS` |
| `exists(Rec, "r0")` — typed | `UNKNOWN_FIELD` naming `['0', 'r']` | `INVALID_PARAMS` |
| `exists(Rec, 7)` — typed | `TypeError: 'int' object is not iterable` | `INVALID_PARAMS` |
| `count("Rec", "")` or `count("Rec", 0)` | **every visible row** | `INVALID_PARAMS` |

The last row is the reason this is an upgrade note. A caller who believed they were
narrowing received the entire visible population, with no exception to notice — the same
class of silent wrong answer as the bare-equality section above. The typed spelling was
not a defence: it shredded a string into its characters and reported them as field names,
and `mypy --strict` only flags a literal, not a `where` typed `Any`.

`{}` and `None` both still mean "no filter", unchanged. The MCP boundary already refused
non-object `where` with this same `INVALID_PARAMS`; the in-process surfaces now agree.

To upgrade: pass a mapping, or omit the argument. A likely source of the error is calling
`exists`/`count` with an object id where `get`/`get_object` was meant.

`INVALID_PARAMS`' catalogue description was widened to say it covers a read parameter
whose *shape* is wrong, alongside an action's params. Its meaning did not change — the
`order_by` shape refusal already used it — but integrations that branch on the published
description text should re-read it.

## Scope declarations: unscoped *or* scope-routed (0.6 → 0.8)

An object type listed in `ScopePolicy.unscoped_types` while it **also** declares
`ScopePolicy.rules[...]` is now refused. `ontology.validate()` raises
`SCOPE_POLICY_ERROR` naming every offending type, `ontary explain` reports it as a
finding, and every read of that type refuses at the call.

| what you declared | 0.6 | 0.8 |
| --- | --- | --- |
| a type in `unscoped_types` **and** in `rules` | accepted; the rule silently ignored | `SCOPE_POLICY_ERROR` at `validate()` and on every read of that type |

This is a behaviour change, not an addition, and an upgrading consumer sees it without
editing any code — but only if it made the declaration in the first place, which the
class-authoring sugar cannot do (`scope=` puts a type in exactly one bucket). It is
reachable by building a `ScopePolicy` directly, or by mutating `policy.unscoped_types`
after the fact.

**Why it is not a warning.** The two halves combine into a disclosure the engine
otherwise refuses. `unscoped_types` removes the scope check, so every row of the type
is visible; the surviving `rules` entry keeps each `DirectProperty` name exempt from
the supplied-value `where=` gate, including one marked `human_visible=False`. Alone,
neither does anything: a scope-routed type denies the rows, and an unscoped type denies
the filter. Together, a consumer filters on the hidden scope key of rows outside its own
scope and reads membership off the row count — recovering a value the engine redacts
from every payload it returns.

**Fixing it.** Decide which one you meant. If the type really has no owning scope, drop
its `rules` entry. If it is scope-routed, remove it from `unscoped_types` — and if you
were using `unscoped_types` to widen what a consumer can see, declare a second, broader
`ScopeRule` for that type instead and scope the consumer at that level. That is a legal
declaration with the same reach, and it keeps the scope bound the exemption assumes.

`contributor_rules` and `row_visibility` are unaffected: `scope="unscoped"` beside
`contributor=[...]` or `row_visibility=...` was and remains legal. A contributor rule
resolves an identity rather than an owning scope and never feeds the `where=` exemption,
and a row-visibility predicate is applied on top of the scope check for every type.

## Auto-minted ids come from `id_factory` (0.9 → 0.10)

When an action handler calls `ctx.insert(obj_type, payload)` and `payload` omits the
type's declared primary key, the runtime now fills it from the executor's configured
`id_factory` — the same seam invocation and effect ids are already minted from — before
`store.insert` is ever called.

A direct `store.insert(obj_type, payload, source)` call — bypassing an `ActionContext`
— is unaffected: it still gets the store-owned `uuid.uuid4()` fallback documented on
`prepare_insert`. Only the action path changed.

This matters because a consumer that configures `id_factory` for deterministic ids
(tests, fixture writers, replay) previously got every id the runtime mints *except*
this one, which fell through to the store's own random UUID regardless of what
`id_factory` produced. A caller working around that gap by monkeypatching stdlib
`uuid.uuid4` no longer needs to.

| what you call | 0.9 | 0.10 |
| --- | --- | --- |
| `ctx.insert("Type", {...})` with no primary key, inside a handler | store-owned `uuid.uuid4()` | runtime's `id_factory()` |
| `store.insert("Type", {...}, source)` directly, with no primary key | store-owned `uuid.uuid4()` | unchanged: store-owned `uuid.uuid4()` |

## Deprecations

While pre-1.0: a name being removed is deprecated for **at least one minor version**
before it goes, warned with `DeprecationWarning`, and the changelog entry names the
replacement. The exception is a change that closes a governance hole — if a permissive
path turns out to be a defect, it is fixed in the next release and called out at the top
of the changelog entry, because leaving it open for a version to be polite about it is
the wrong trade.

**Recorded exception — `via=` (human decision D5, revised 2026-08-27).** This is an
explicit, dated departure from the rule above. The legacy `via=` keyword form of
`BoundQuery.traverse` was described in the 0.7.0 `CHANGELOG.md` as “deliberately
remains supported”; that statement is reversed here. `via=` was removed by commit
`d23e3cd`, landing in release 0.8.0, without a deprecation or warning cycle. The
operator revised decision D5 on 2026-08-27 after decision D8 scoped the
`ontology-prototype` consumer out of this project: do not re-add this dead surface.
The reason is a pre-1.0 spelling cleanup with no consumers in scope. This is explicitly
**not** the existing “closes a governance hole” exception; that exception must not be
stretched to cover this spelling cleanup.

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
