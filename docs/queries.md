# Queries, typed reads, and pagination

[Back to the README](../README.md) · [API reference](api-reference.md)

The public read vocabulary is the same on `OntologyClient` and `BoundQuery`:
`get`, `list`, `traverse`, `aggregate`, `aggregate_by`, and
`count_contributors`. `OntologyClient` supplies the `Consumer`; `BoundQuery` is the
consumer-fixed view passed to a Function. Both share the same guarded
implementation, so scope resolution, sensitivity, row visibility, and min-N do not
depend on which façade made the call.

## Typed and string forms

Pass an ontology class to receive a hydrated model, or pass its API name to receive
the dynamic `StoredObject` form used by generic tooling and MCP:

```python
ticket = client.get(Ticket, ticket_id)       # Ticket | None
assert ticket is not None
print(ticket.subject)

open_tickets = client.list(
    Ticket,
    where={"queue_id": queue_id},
)                                           # list[Ticket]

dynamic = client.get("Ticket", ticket_id)   # StoredObject | None
assert dynamic is not None
print(dynamic.payload["subject"])
```

Typed class and link-handle arguments are resolved against the ontology bound to
the client or Function. An undecorated class, a class from another ontology, or a
field name absent from the class fails closed with `ValidationFailed` and a stable
code such as `UNKNOWN_NAME` or `UNKNOWN_FIELD`. A declared but hidden field is not
made queryable by the typed check: the guarded layer still refuses a hidden filter
with `VisibilityError` and code `VISIBILITY_DENIED`.

Hydration is the only place values change representation. Pydantic parses typed
properties such as `datetime`, while the dynamic form retains the stored JSON-safe
value. Both forms expose frozen lineage separately from the payload. A redacted
typed property is `None` only when the consumer cannot see it, and its name appears
in `redacted_fields`, distinguishing redaction from an actually absent optional
value.

## `traverse`

The typed client form passes a `LinkHandle` first:

```python
tickets = client.traverse(commentOnTicket, comment_id)  # list[Ticket]
```

The dynamic client form names the source object, link API name, and source
identifier, in that order:

```python
tickets = client.traverse("Comment", "commentOnTicket", comment_id)
```

`BoundQuery` accepts the same handle-first typed form inside Functions.

The same visibility checks apply to returned targets. Traversal through an
identity-revealing link raises `VisibilityError` with code `VISIBILITY_DENIED` for
a human consumer before the target is returned. AI consumers still receive normal
scope and sensitivity enforcement on the target rows.

## Pagination

`list` accepts `limit=` and `after=`. Without `limit`, it returns the unpaginated
list form. With a positive limit, the typed form returns `TypedPage[T]` and the
dynamic form returns `Page`; both carry `items` and an opaque `next_cursor`:

```python
from ontary import Page, TypedPage

page = client.list(Ticket, limit=1, where={"queue_id": queue_id})
assert isinstance(page, TypedPage)

next_page = client.list(
    Ticket,
    limit=1,
    after=page.next_cursor,
    where={"queue_id": queue_id},
)
assert isinstance(next_page, TypedPage)

string_page = client.list("Ticket", limit=1, where={"queue_id": queue_id})
assert isinstance(string_page, Page)
```

The cursor is an opaque resume token, not a domain id. Store it and pass it back;
do not parse or manufacture one. The read layer fills a page after applying the
filter and visibility gates, so a page reaches its requested size whenever enough
visible matching rows remain. A `None` cursor means the store was exhausted before
the page filled. If a row changes during a walk, the storage history may repeat a
row but does not create a gap in an otherwise quiescent walk.

`after` without `limit` raises `ValidationFailed` with code
`AFTER_WITHOUT_LIMIT`; a non-positive limit raises `ValidationFailed` with
`INVALID_LIMIT`; a malformed or unknown cursor raises `ValidationFailed` with
`INVALID_CURSOR`.

## Aggregation

`aggregate` and `aggregate_by` have deliberately separate return shapes:

```python
mean_age = client.aggregate(
    Ticket,
    "age_hours",
    where={"queue_id": queue_id},
)                                               # float

mean_age_by_status = client.aggregate_by(
    Ticket,
    "age_hours",
    "status",
    where={"queue_id": queue_id},
)                                               # dict[str, float]
```

The ungrouped operation returns the mean across visible matching rows. The grouped
operation returns one mean per distinct group value. Both enforce field visibility,
where-clause validation, and min-N over distinct contributors. A non-numeric value
field raises `ValidationFailed` with `NON_NUMERIC_AGGREGATE`; an empty group field
raises `ValidationFailed` with `INVALID_GROUP_BY` on the dynamic path.

`count_contributors` exposes the releasable population size without requiring an
identity field to be readable. It uses the same min-N decision as the aggregate it
accompanies and raises `VisibilityError` with `MIN_N_VIOLATION` when that selection
is withheld. The direct guarantee is that a sub-threshold count is not returned;
complementary differencing across separately released selections remains a
disclosure residual that an ontology author must consider when designing Functions.

## `BoundQuery` in a Function

A Function receives a `BoundQuery`, never a store handle. Its `get`, `list`,
`traverse`, `aggregate`, `aggregate_by`, and `count_contributors` calls are already
bound to the invoking consumer. Typed overloads use the same class and
`LinkHandle` rules as `OntologyClient`; there is no second naming system for
Function reads.

[See the API reference reading section](api-reference.md#reading) for signatures
and return models. [See the authority guide](authority.md) for the boundary between
the guarded client path and trusted code holding a raw store.

[Return to the README](../README.md) · [Return to the API reference](api-reference.md)
