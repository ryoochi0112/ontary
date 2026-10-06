# `ontary` — API reference: Stores & ingest

**English** · [日本語](api-stores.ja.md) · [← API reference](api-reference.md)

*Reference* — Store contracts and bulk ingest; see the [API reference](api-reference.md) and [Storage, tenancy, and schema](storage.md) for related contracts and examples.

## Stores

### The `Store` protocol

`OntologyClient` / `GuardedQuery` / `ActionExecutor` are typed against this protocol,
not a concrete backend.

```python
insert(obj_type, payload, source) -> str
update(obj_type, obj_id, payload_changes, source) -> None
read_current(obj_type, obj_id) -> StoredObject | None
read_last(obj_type, obj_id) -> StoredObject | None
read_all(obj_type) -> list[StoredObject]
read_page(obj_type, after_key=None, batch=500) -> list[PagedRow]
retire_object(object_type, obj_id) -> StoredObject
create_link(link_type, from_id, to_id) -> None
close_link(link_type, from_id, to_id) -> bool
links_from(link_type, from_id) -> list[str]
links_to(link_type, to_id) -> list[str]
links_from_asof(link_type, from_id, asof) -> list[str]
links_to_asof(link_type, to_id, asof) -> list[str]
append_audit(entry) -> None
audit_entries() -> list[AuditEntry]
transaction() -> ContextManager
capture_action_writes() -> ContextManager[list[WriteRecord]]
```

`insert` refuses a primary key that already has a live row of the same object type
with `ConflictError` and code `OBJECT_ALREADY_EXISTS`; the store is left unchanged.
An object has at most one live row, and the SQL backends back this with a partial
unique index. A retired object's id may be inserted again, which starts a new live row.

`insert` and `update` take a `date` or `datetime` value as an ISO-8601 string or a
`date` / offset-aware `datetime` object; see
[Date and datetime values](#date-and-datetime-values).

`create_link` needs a live object at both ends. Each id is looked up under the link
type's declared `from_type` / `to_type`; an id that is missing, retired, or live only
as some other type is refused with `ValidationFailed` and code
`LINK_ENDPOINT_NOT_FOUND`, and no link row is written. The endpoints are checked
before cardinality. An object inserted earlier in the same action transaction counts
as live.

`create_link` is idempotent: creating a link identical to a live one, for any
cardinality, is a no-op. Nothing is written, no `WriteRecord` is captured, and the
call returns normally; it used to duplicate a MANY_TO_MANY link and to refuse a
MANY_TO_ONE link with `CARDINALITY_VIOLATION` against itself. The identical-link check
runs before cardinality. Only a live link counts: after `close_link`, the same pair
may be linked again, and the closed row is kept as history. The SQL backends back
this with a partial unique index on live links.

A primary key is immutable. `update` refuses a change that gives the primary key a
different value with `ValidationFailed` and code `PRIMARY_KEY_IMMUTABLE`, and the store
is left unchanged. This keeps the store id and the payload primary key equal. Repeating
the current value is not a change and is accepted. To give an entity a new key, retire
the object and insert a new one.

`read_current` returns the live row only; `read_last` returns the newest row whether
or not it is still live. `links_from`/`links_to` return live links only; the `_asof`
pair also returns links closed at or after the instant you name.

The three history-aware reads exist for ONE caller: the action executor's target gate,
which has to tell "outside your scope" apart from "already retired". A retired object
still owns the scope it was in, and `ActionContext.retire` closes its links, so the gate
resolves that scope from the object's last row plus the links it held when that row
closed. Consumer reads deliberately do not: resolving a retired row's scope would put
its children back in a reader's visible set, carrying a population past `min_n` and
releasing an aggregate over the retired subject's own rows. A backend that
filtered retired rows out of `read_last`, or live-only links out of the `_asof` pair,
would deny a retired object's own owner instead.

<!-- scope-asof-invariant:start -->
A retired target resolves the scope it owned **as of the instant its own row closed**.
The links on the chain are read at that instant on both bounds, so an edge the object
had already left cannot answer for it.

Where a `ViaLink` hop finds more than one parent at that instant, the first parent whose
chain resolves wins, over an order the store declares rather than each backend's own row
order: earliest link `valid_from` first, then lowest parent id, compared byte-wise. It
is not creation order — `links` declares no monotonic key, so two links made in one
clock tick are separated by id, not by which was written first. Every backend answers in
that order because it decides which scope owns the object, and therefore which operator
this gate admits.

An ancestor object is admitted when it had not already been retired by then — but it is
read at its newest row, so its payload, and any `DirectProperty` key taken off that
payload, is the one it carries now: an ancestor updated after the instant answers with
the scope it is in today, not the one it was in then. Making the object side
point-in-time too needs an as-of read of an object's history, which `Store` does not
have. A live target has no closing instant and resolves in the present tense, exactly as
a consumer read does, so this gate loosens nothing for an object that still exists.
<!-- scope-asof-invariant:end -->

Point-in-time rather than "retired rows allowed", because the two obvious readings
fail in opposite directions. Resolving ancestors from the newest row lets one
retired long ago outrank a live one and authorize an operator who no longer owns
the row; resolving them live-only refuses an ancestor that was alive when the
target closed and is the only route to a scope that is alive now, denying the
owner permanently. At the target's `valid_to` the earlier-retired ancestor was
already closed and the surviving one was not, so one instant settles both.

<!-- scope-denied-consequences:start -->
Where the chain resolves and the consumer covers it, the gate reaches the
handler's own refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not
mean one thing: it is raised at two places. One is a defense-in-depth
refusal of a scope-bearing parameter that is not a `str` — parameter
validation rejects that first, so it is a floor under type confusion
rather than a path in normal use. The other fires wherever coverage cannot
be shown, and that is two situations rather than one: the chain resolved
and this consumer is outside it, which is the ordinary denial this gate
does not change; or the chain did not resolve at that instant, and
deny-by-default denies. Only the second belongs to this frame. Four rule
kinds are declared, and each meets retirement at its OWN hop:

- `SelfScope` answers with the object's own id, which retirement does not
  take away.
- `DirectProperty` reads the scope key off the payload, which retirement
  leaves alone: this gate reads the newest row rather than the live one.
- `ViaLink` climbs the `links` table. It resolves to nothing when none of
  the parents it reaches resolves in turn — among them a parent already
  retired BEFORE the target closed: its edge may still be readable at that
  instant, but its own row is not admitted, so it cannot answer at the one
  instant this gate asks about. One retired after the target — including in
  the same cascade tick — still answers for it.
- `CustomResolver` is author code handed the raw `Store`, and the engine
  does not reach inside it, so what a retired object resolves to is the
  resolver's own business rather than this frame's. The natural body reads
  `read_current`, which is `None` for a retired object, so a resolver
  written that way denies. A type that needs the precondition refusal after
  retirement declares a second rule — a `DirectProperty` on a scope-key
  column, which survives retirement.

Each bullet is about one hop, never about one target, and the four are not
the whole chain. `ScopePolicy.rules` maps each type to an ORDERED
list, so a target declares as many of these hops as that
list holds and is answered by the first that resolves; and a level no rule of its own can
answer climbs to the canonical instance of a narrower scope, where a type
declares one — which is none of the four. What the engine's own hops share is the
frame: any object the engine has to read that had already been retired
before the target closed is refused there, whichever of those hops reached
it — so the hop that answers a target's level can fail on an object the
target's other hops never touch. A `CustomResolver` is outside that frame only
for the reads its own callable makes: the engine does not thread the
instant into author code, so an ancestor the callable reaches for itself is
read however it reads it, retired or not. Its ANSWER re-enters the engine,
and every object the engine reads from there is refused on the frame's own
terms — the canonical instance a narrower answer names, and any object
whose rules the engine goes on to ask, whose row is checked before its own
resolver runs.

Every denial in this list fails closed — the object's own owner is denied,
nothing is disclosed.
<!-- scope-denied-consequences:end -->

Three implementations ship, all proven against one shared conformance suite:

- **`ObjectStore`** — SQLite. History (close-old / insert-new), links, audit log.
- **`InMemoryStore`** — pure Python. No file, no SQL; for tests and dogfooding.
- **`PostgresStore`** — PostgreSQL-backed; available with the `postgres` extra.

The outermost `transaction()` serializes a read followed by a write against
concurrent writers on the same store. It is reentrant, and nested calls share the
outer transaction.

`ObjectStore(registry, path, *, busy_timeout=5.0)` configures how many seconds
SQLite waits for a database lock. `ActionExecutor` holds the outer transaction—and
therefore SQLite's write lock—for the whole handler body, including external
`ctx.capability()` calls. For a competing writer, the time it can wait for that
handler is bounded by its store's busy timeout; expiry raises coded `STORE_BUSY`
(`kind="conflict"`). Increase `busy_timeout` when legitimate handlers can run
longer than the default, or keep capability calls short.

`Source` — `source_system`, `source_id`, `extracted_at`.

`retire_object` closes an object's current row without inserting a replacement;
it does not cascade links. `close_link` closes the one live link matching all
three identifiers. All three shipped backends implement these verbs. The
action-context cascade is layered above the store's object-retirement
primitive.

### Date and datetime values

Every write path checks a `date` or `datetime` property value the same way:
`Store.insert` / `update`, `ActionContext.create` / `save`, `bulk_upsert` /
`client.ingest`, and action parameters (including MCP `execute_action`).

| Value written | `date` property | `datetime` property |
| --- | --- | --- |
| `date` object | Stored as `YYYY-MM-DD` | Refused |
| Offset-aware `datetime` object | Refused | Stored as its own `isoformat()`, offset preserved |
| Naive `datetime` object (no `tzinfo`) | Refused | Refused |
| `"YYYY-MM-DD"` string | Stored verbatim | Refused: a time component is required |
| Other ISO-8601 date spelling, such as `"20261005"` | Refused | — |
| ISO-8601 string with a time, with or without an offset (`Z` included) | — | Stored verbatim |
| Any other string | Refused | Refused |

- **Nothing is normalised.** The store keeps the exact spelling: an offset is not
  converted to UTC, and `Z` stays `Z`. `eq` and `in` filters match that spelling;
  the comparison operators compare instants (see
  [Filters, ordering, and bounded reads](api-reading.md#filters-ordering-and-bounded-reads)).
- **Naive strings are accepted, naive objects are not.** A naive ISO *string* is
  stored as written. A naive `datetime` *object* is refused, because adding
  `tzinfo=` is the only way to say which instant it means. Avoid mixing naive and
  offset-aware values in one property: the two kinds have no defined order.
- **Refusal codes.** A refused value raises `ValidationFailed` with
  `INVALID_RECORD` on `Store.insert` / `update`, `ActionContext.create` / `save`,
  and per record in an ingest report. An action parameter is refused with
  `INVALID_PARAMS` before the handler runs. A `where` operand is refused with
  `OPERATOR_TYPE_MISMATCH`.

```python
from datetime import datetime, timedelta, timezone

from ontary import InMemoryStore, Ontology, OntologyObject, Source, prop
from ontary.errors import ValidationFailed

ontology = Ontology("shifts", scope_levels=["org"], min_n=1)


@ontology.object(layer="L0", scope="unscoped")
class Shift(OntologyObject):
    id: str = prop(primary_key=True)
    starts_at: datetime


ontology.validate()
store = InMemoryStore(ontology.registry)
source = Source(source_system="roster")
jst = timezone(timedelta(hours=9))

store.insert("Shift", {"id": "s1", "starts_at": datetime(2026, 10, 5, 9, tzinfo=jst)}, source)
store.insert("Shift", {"id": "s2", "starts_at": "2026-10-05T00:00:00Z"}, source)
assert store.read_current("Shift", "s1").payload["starts_at"] == "2026-10-05T09:00:00+09:00"
assert store.read_current("Shift", "s2").payload["starts_at"] == "2026-10-05T00:00:00Z"

try:
    store.insert("Shift", {"id": "s3", "starts_at": datetime(2026, 10, 5, 9)}, source)
except ValidationFailed as exc:
    assert exc.code == "INVALID_RECORD"  # a naive datetime object
else:
    raise AssertionError("a naive datetime object must be refused")
```

### Schema versioning

Every SQLite file is stamped with the engine's `SCHEMA_VERSION` via `PRAGMA
user_version` when it is created; Postgres records the same number in `schema_meta`.
Neither backend carries a migration ladder, so any other stamp — higher, lower, or an
unstamped store that already has an `objects` table — is refused at construction with
`STORE_VERSION_UNSUPPORTED`, naming both versions. Moving a store across schema
versions is an explicit operator step: open it with the matching ontary version, or
migrate the data into a fresh store. [storage.md](storage.md#moving-across-a-schema-version)
gives the drop-and-recreate procedure.

---

## Bulk ingest

```python
bulk_upsert(store, registry, obj_type, records, source) -> IngestReport
bulk_link(store, registry, link_type, pairs, source) -> IngestReport

client.ingest(
    obj_type, records, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
client.ingest_links(
    link_api_name, pairs, source, *,
    on_error: Literal["raise", "report"] = "raise",
) -> IngestReport
```

`client.ingest` takes the object type's name as a string (`"Ticket"`), and
`client.ingest_links` takes the link's `api_name`. The typed reads (`get`, `list`,
`traverse`) take the class or `LinkHandle` instead. Ingest names the type because
records usually arrive from a source system as plain dicts, often before any
typed model exists. With a class in hand, pass `Ticket.__name__`, or its
declared `api_name` when that differs.

`bulk_upsert` and `bulk_link` are the engine layer and always return an
`IngestReport`. The client methods run the complete batch first, so valid
records remain committed even when another record fails. By default, a failed
client batch raises `IngestError`; its `.report` is the full report and its
message names the committed and failed counts. Pass `on_error="report"` to
return the report without raising, preserving the report-returning behavior.

`IngestReport` — `inserted_ids: list[str]`, `errors: list[IngestError]`. Records are
validated against declared shape: a missing primary key, a missing required property,
an unknown property, or a type mismatch yields `INVALID_RECORD`. Writing an
ontology-owned type raises `OWNED_TYPE_REFUSED`; supplying an ontology-owned property
raises `OWNED_PROPERTY_REFUSED`. A link pair whose endpoint has no live row of the
declared endpoint type is rejected per pair with `LINK_ENDPOINT_NOT_FOUND`, like a
cardinality violation; the other pairs still land, so ingest objects before their
links. Re-running a link load is idempotent: a pair identical to a live link is a
no-op and is still listed in `inserted_ids`, so the second run returns the same
report as the first.

A `date` or `datetime` value is written as an ISO-8601 string or a `date` /
offset-aware `datetime` object; a naive `datetime` object is refused with
`INVALID_RECORD`. See [Date and datetime values](#date-and-datetime-values).

---

