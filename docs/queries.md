# Queries, typed reads, and pagination

[Back to the README](../README.md) · [API reference](api-reference.md)

The public read vocabulary is the same on `OntologyClient` and `BoundQuery`:
`get`, `list`, `count`, `exists`, `traverse`, `aggregate`, `aggregate_by`, and
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
)                                           # TypedPage[Ticket]

dynamic = client.get("Ticket", ticket_id)   # StoredObject | None
assert dynamic is not None
print(dynamic.payload["subject"])
```

Typed class and link-handle arguments are resolved against the ontology bound to
the client or Function. An undecorated class, a class from another ontology, or a
field name absent from the class fails closed with `ValidationFailed` and a stable
code such as `UNKNOWN_NAME` or `UNKNOWN_FIELD`. A declared but hidden field is
generally not queryable: the guarded layer refuses it with `VisibilityError` and
code `VISIBILITY_DENIED`, with the narrow scope-routing `where` and author-declared
Function aggregate exceptions described below.

`where=` accepts a bare scalar for equality or a one-key operator mapping:
`gt`, `gte`, `lt`, `lte`, `in`, `ne`, and `contains`. Comparisons are valid only
for declared `int`, `float`, or `date` properties; `in` requires a list of
declared-type values; `ne` accepts every declared type; and `contains` is a
substring test on `str` properties. Unknown operators raise
`UNKNOWN_OPERATOR`; an operator or operand incompatible with the declared type
raises `OPERATOR_TYPE_MISMATCH`. That operand check applies to the bare scalar too:
it is the only equality spelling, so `where={"count": "2"}` on a declared `int` is a
mismatch, not an empty match. The one exception is `None`, which is the null test and
selects rows where the property is absent. An `int` operand on a declared `float` is
widening rather than a mismatch. Date values are stored as ISO `YYYY-MM-DD`
strings, so date comparisons use their chronological order.
Because mapping values are operator syntax, dict equality on a declared `json`
property changed from `where={"data": {"kind": "a"}}`; use
`where={"data": {"in": [{"kind": "a"}]}}` as the equality escape.

The scope-key exemption is narrow. It applies only when the hidden property is
declared as a `DirectProperty` scope-routing key and the consumer supplies the
value in an equality-shaped `where`: a bare `eq` value or `in` over an explicit
list. `gt`, `gte`, `lt`, `lte`, `ne`, and `contains`, plus `in` over a non-list or
any malformed shape, never receive the exemption; they let the caller learn a
value and are refused on a hidden field. "Supplies the value" is literal: the
operand must be an actual value the caller already holds — a `str`, `int`,
`float`, `bool`, or `date`. `None` supplies nothing, so `where={"key": None}`,
`{"in": [None]}`, and `{"in": []}` are refused on a hidden field; a bare null
would otherwise be a free per-row null probe needing no prior knowledge. Null
filtering is unaffected wherever the field is readable.

Each `where` mapping and each condition inside it is read exactly once, at the
public boundary, and every gate and the row matcher consume that one snapshot. A
caller-supplied mapping that answered differently on a second read could
otherwise be classified as one predicate and executed as another. `order_by` and `group_by` never receive
it either: an ordering rank and a returned group key disclose the value, so both
consult the un-exempted hidden-field gate and are refused on a hidden field.

The exemption is not proof that the property is safe to disclose. A bare `eq` or
explicit-list `in` still lets a consumer confirm a value it can already guess, and
repeated equality probes over a guessable or enumerable id space can be joined with
the rows that come back to recover the association. An author declaring
`DirectProperty` on a hidden, identity-like property must weigh that confirmation
risk against the property's sensitivity; the exemption is a deliberate trade, not a
safety guarantee. The exemption is scoped per object type (`policy.rules[obj_type]`),
not pooled across the ontology, so declaring `DirectProperty` on a field for one type
does not exempt a same-named field on another type.

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

Pass `reverse=True` to traverse from a link's target side and receive the linked
source objects. The identity-revealing denial is symmetric in both directions.

## Pagination

There is a scale caveat for ordered pages: each page currently materializes and
sorts the entire object type, regardless of `limit` or `where` selectivity. In
the measured 20,000-row case, `limit=10` read all 20,000 rows with `order_by`,
versus 500 without it. That is O(N log N) work per ordered page and
O(N² log N) for a full ordered walk.

`OntologyClient.list` accepts `limit=`, `after=`, and `order_by=`. Omitting `limit`
applies `DEFAULT_READ_LIMIT=1000` and returns a bounded `TypedPage[T]` or `Page`.
Inside a declared Function, `BoundQuery.list` is intentionally unbounded when
`limit` is omitted and returns a bare list; pass a positive `limit` there to receive
a page. On the consumer surface, pass `limit=None` explicitly for the unbounded list
form. With a positive limit, the typed form returns `TypedPage[T]` and the dynamic form
returns `Page`; both carry `items` and an opaque `next_cursor`:

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

`order_by` names a declared payload field and sorts ascending by default. Pass a
`(field, "asc"|"desc")` pair for an explicit direction; unknown fields are refused,
and hidden fields are refused even when they are `DirectProperty` scope-routing
keys. Unlike the equality-shaped `where` exemption above, `order_by` never gets
that exemption because its returned rank discloses the value. `group_by` likewise
never gets it because the group value is returned verbatim as the result dict's key.

`group_by` names a declared payload field on every surface, string form included:
an unknown name is refused with `UNKNOWN_FIELD` rather than matching nothing. A
`json`-declared field cannot be a group key at all (`INVALID_GROUP_BY`) — its
values need not be hashable. And because the result dict is keyed by strings, two
group values that release as the same key are refused with `GROUP_KEY_COLLISION`
rather than one silently overwriting the other; grouping on an *optional* property
is where that bites, since rows lacking it key `None` and collide with a row
holding the literal string `"None"`.

The cursor is an opaque resume token, not a domain id. Store it and pass it back;
do not parse or manufacture one. The read layer fills a page after applying the
filter and visibility gates, so a page reaches its requested size whenever enough
visible matching rows remain. A `None` cursor means the store was exhausted before
the page filled. If a row changes during an unordered walk, the storage history may repeat a
row but does not create a gap in an otherwise quiescent walk.

`after` without `limit` raises `ValidationFailed` with code
`AFTER_WITHOUT_LIMIT`; a non-positive limit raises `ValidationFailed` with
`INVALID_LIMIT`; a malformed or unknown cursor raises `ValidationFailed` with
`INVALID_CURSOR`. An ordered walk cannot resume after any write to its own
cursor row, including a retirement or an `update` that supersedes the row and
issues a new page token; the caller must restart the ordered walk from the
first page and receives `STALE_CURSOR`.

That `STALE_CURSOR` case is not the whole ordered-walk story. If a non-cursor
row changes during an ordered walk, a row whose sort value moves ahead of the
cursor is silently dropped, while a row whose sort value moves behind it is
duplicated. An ordered walk is gap-free and repeat-free only over a quiescent
store.

## Aggregation

### Visible-row counting

`count(obj_type, where=None)` returns the number of matching rows after the
consumer's scope and row-visibility checks; `exists(...)` reports whether that
same post-visibility selection is non-empty. Both accept the same typed `where`
operator grammar as `list`. A consumer scoped away from every matching row receives `0`
from `count` and `False` from `exists`, not a privacy refusal.

`where` must be a mapping or omitted, on every read surface. Anything else raises
`ValidationFailed` with `INVALID_PARAMS`, *including* a falsy value: `""`, `0`, `[]`
and `False` are refused rather than read as "no filter", so passing an object id where
a filter belongs — `exists("Ticket", ticket_id)` when `get` was meant — can no longer
answer `True` against the whole table. An empty **mapping** `{}` still means no filter,
as does `None`.

These operations are deliberately not min-N-gated. They reveal only the size of a
row set after post-visibility filtering; the consumer
can already enumerate the same rows with `list(..., limit=None)`, so a min-N refusal
would add no disclosure protection.
`count_contributors` remains the sole privacy-counting primitive: unlike visible-row
counting, it resolves the distinct contributor population behind an aggregate and
therefore keeps the aggregate's min-N release discipline.

### Privacy-preserving aggregation

`aggregate` and `aggregate_by` have deliberately separate return shapes:

Both accept `func="mean"|"count"|"sum"|"min"|"max"`; the default remains
`"mean"`. `count` returns an integer, while `mean`, `sum`, `min`, and `max`
return numeric values as floats. The grouped form returns one value per group.
Every function remains behind the same min-N release discipline. An empty visible
selection raises `MIN_N_VIOLATION` for both ungrouped and grouped calls, including
the grouped-empty `{}` case; it does not return an empty dictionary.

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
where-clause validation, and min-N over distinct contributors. A `value_field` the
type does not declare raises `ValidationFailed` with `UNKNOWN_FIELD`, exactly as
`where=`, `order_by` and `group_by` do for the same name — including a name that some
stored row happens to carry, since an undeclared payload key is allowed on write but is
not addressable by name in a read. A declared but non-numeric value field raises
`ValidationFailed` with `NON_NUMERIC_AGGREGATE`; an empty group field raises
`ValidationFailed` with `INVALID_GROUP_BY` on the dynamic path.

The hidden-`value_field` exemption applies only to an unnarrowed `aggregate` from an
author-declared Function, over a type declaring `contributor_rules`, with
`func="mean"` or `func="count"`. "Unnarrowed" means no non-empty `where` and no
`group_by`; the predicate's operator and the filtered field's visibility do not alter
that rule. From a consumer surface — client, typed, or MCP — and from any narrowed
author Function read, aggregating a hidden field raises `VisibilityError` with
`VISIBILITY_DENIED`.

`min` and `max` would directly release boundary individuals. `sum` is mathematically
the same total as `mean * count`, so refusing it is not an independent protection.
Mean and count may coexist only for the complete visible population. Refusing every
narrowed or grouped hidden-field read removes caller-selected second populations, but
does not close differencing across repeated unnarrowed reads: the population can still
drift over time or differ across consumer scopes. This is the accepted "Declared, not
defended" residual of the exemption.

Provenance is minted by the engine, never asserted by a caller. The grant is attached
only when `FunctionRegistry` dispatches a handler the author registered against a
declared `FunctionDef`, and it lands on a `BoundQuery` the engine builds for that one
call. Constructing a `BoundQuery` yourself grants nothing — it reads at consumer tier,
the same way it carries no declared capabilities. Author provenance is necessary but
not sufficient: passing raw consumer parameters into `where` now makes the read
narrowed and refuses the exemption, even when the predicate names a visible field.
Authors that need caller-selected populations may aggregate visible fields, but cannot
release hidden-field mean or count cells through this exemption.

`count_contributors` exposes the releasable population size without requiring an
identity field to be readable. It uses the same min-N decision as the aggregate it
accompanies and raises `VisibilityError` with `MIN_N_VIOLATION` when that selection
is withheld. The direct guarantee is that a sub-threshold count is not returned;
complementary differencing across separately released selections remains a
disclosure residual that an ontology author must consider when designing Functions.

## `BoundQuery` in a Function

A Function receives a `BoundQuery`, never a store handle. Its `get`, `list`,
`count`, `exists`, `traverse`, `aggregate`, `aggregate_by`, and
`count_contributors` calls are already
bound to the invoking consumer. Typed overloads use the same class and
`LinkHandle` rules as `OntologyClient`; there is no second naming system for
Function reads.

[See the API reference reading section](api-reference.md#reading) for signatures
and return models. [See the authority guide](authority.md) for the boundary between
the guarded client path and trusted code holding a raw store.

[Return to the README](../README.md) · [Return to the API reference](api-reference.md)
