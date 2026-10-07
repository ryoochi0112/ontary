# `ontary` — API reference: Reading

**English** · [日本語](api-reading.ja.md) · [← API reference](api-reference.md)

*Reference* — Reading contracts; see the [API reference](api-reference.md) and [Getting started](getting-started.md) for related contracts and examples.

## Reading

### `StoredObject`

| Field | Type |
| --- | --- |
| `payload` | `dict[str, Any]` |
| `lineage` | `Lineage` |

A frozen payload/lineage split — never a bare dict with smuggled `_object_type`-style
keys.

### `Lineage`

`object_type`, `object_id`, `valid_from`, `valid_to`, `source_system`, `source_id`,
`extracted_at`.

### Filters, ordering, and bounded reads

`where` accepts a bare scalar for equality or a mapping whose keys are drawn from
`gt`, `gte`, `lt`, `lte`, `in`, `ne`, and `contains`. A mapping with several keys
is their conjunction on that one field, so `{"gte": a, "lt": b}` is a half-open
range and `{"contains": "x", "ne": "x"}` is a substring match with one value
excluded; an empty mapping is refused. Comparisons apply to declared `int`,
`float`, `date`, or `datetime` properties. `date` and `datetime` operands are the
same ISO-8601 strings the property stores, or `date` / offset-aware `datetime`
values, which are converted to that spelling first; a `datetime` must
carry a time component, so a date-only string is `OPERATOR_TYPE_MISMATCH` rather
than a naive midnight that never matches an offset-aware column. The four comparison
operators treat a `datetime` as an instant, so an operand in another UTC offset
matches by moment, not by spelling, and a stored value whose offset-awareness
differs from the operand's (naive against aware) does not match; `eq`, `ne`, and
`in` on a `datetime` still match the stored spelling exactly. `in` takes a list of declared
values, `ne` applies to every declared type, and `contains` is a substring test
for `str`. Unknown operators raise `UNKNOWN_OPERATOR`; an operator or operand
that does not match the declared property type raises
`OPERATOR_TYPE_MISMATCH` — including the bare scalar, which is the only equality
spelling. `None` is the exception: it is the null test, selecting rows where the
property is absent. An `int` operand on a declared `float` is widening, not a
mismatch. Mapping-valued `where` is the same grammar on typed,
string, aggregate, Function, and MCP read surfaces.

A `where` condition on a struct property raises `OPERATOR_TYPE_MISMATCH` with a
message such as `where is not supported on struct property 'amount'`. Inner paths
such as `amount.currency` cannot be queried; they are not top-level properties.

Because a mapping value is operator syntax, dict equality on a declared `json`
property is not spelled `where={"data": {"kind": "a"}}` — that mapping is parsed
as operators, not a value to match. Wrap it as an `in` list of one instead:
`where={"data": {"in": [{"kind": "a"}]}}` is the equality escape.

For a hidden property declared as a `DirectProperty` scope-routing key, the
scope-key exemption applies only to a bare `eq` value and a lone `in` over an
explicit list. `gt`, `gte`, `lt`, `lte`, `ne`, and `contains`, plus `in` over a
non-list, any malformed shape, and any multi-key mapping (even one that contains
an `in`), are refused with `VISIBILITY_DENIED` because they let the caller learn
a value. The operand must also actually supply a value — a `str`,
`int`, `float`, `bool`, or `date` — so `None`, a list containing `None`, and an
empty `in` list are refused too: a bare null needs no prior knowledge and would
be a per-row null probe. Null filtering on a readable field is unchanged. Each
`where` mapping, and each condition inside it, is read exactly once at the public
boundary; every gate and the row matcher consume that one snapshot, so a mapping
that answered differently on a second read cannot be classified as one predicate
and executed as another.

`where` must be a mapping or omitted. Anything else — a string, a list, a number
— raises `INVALID_PARAMS` on every read surface, including a *falsy* one: `""`,
`0`, `[]`, and `False` are refused rather than treated as "no filter". An empty
**mapping** `{}` still means no filter, as does `None`.

`order_by` accepts a declared payload field, ascending by default, or a
`(field, "asc"|"desc")` pair. Unknown fields raise `UNKNOWN_FIELD`; hidden fields
are refused with `VISIBILITY_DENIED`, including hidden scope-routing keys, because
the returned rank discloses the value. `group_by` has no scope-key exemption
either: its returned dictionary key discloses the group value. Lineage fields are
not payload fields.

On the consumer surfaces (`OntologyClient` and `GuardedQuery`), omitting `limit`
applies `DEFAULT_READ_LIMIT=1000` and returns a `Page` (or `TypedPage` for a typed
call). Pass `limit=None` explicitly for the unbounded list form. Inside a declared
Function, `BoundQuery.list` is unbounded when `limit` is omitted and returns a bare
list; pass a positive `limit` for a `Page`. Pagination composes with `order_by` and
`after` on the bounded surfaces.

### Pagination — `Page` / `TypedPage[T]`

Both carry `items` and an opaque `next_cursor: str | None`. On the consumer surfaces,
omitting `limit` returns a page using the default bound above, while `limit=None` is
the explicit unbounded-list opt-in. Inside a declared Function, `BoundQuery.list`
returns a bare list when `limit` is omitted or `None`; passing a positive `limit`
returns a page.

The contract:

- **Exact-size pages.** A page holds exactly `limit` items whenever that many visible
  rows remain — filtering downstream of the store read can never shorten a page. A
  short page always means "no more rows", never "some were hidden from you".
- **The cursor is opaque.** A random per-row token. Not a row id, not a count, not an
  order. Never parse it; store it and pass it back to `after=`.
- **`next_cursor is None` means exhausted-before-full.** A page that fills exactly at
  the last row still carries a cursor; the follow-up call returns one final empty
  page. So `while next_cursor is not None` always terminates correctly rather than
  stopping a page early.
- **During an unordered walk, a mid-walk update may repeat a row but never skip
  one.** The store is close-old-insert-new. During an ordered walk, a row whose
  sort value moves ahead of the cursor is silently dropped, while a row whose
  sort value moves behind it is duplicated. Only a quiescent ordered walk is
  gap-free and repeat-free.
- `after` without `limit` → `AFTER_WITHOUT_LIMIT`. `limit < 1` → `INVALID_LIMIT`.
  A malformed or unknown cursor → `INVALID_CURSOR`. An ordered walk cannot resume
  after any write to its own cursor row — including a retirement or an `update`
  that supersedes it — and raises `STALE_CURSOR`; restart from the first page.

**Scale caveat for ordered pages.** Each ordered page currently materializes and
sorts the whole object type, regardless of `limit` or `where` selectivity. In the
measured 20,000-row case, `limit=10` read all 20,000 rows with `order_by` versus
500 without it: O(N log N) per ordered page, and O(N² log N) for a full ordered
walk.

### `traverse`

`client.traverse(link_cls, from_obj_or_id)` is the typed form;
`client.traverse("Comment", "commentOnTicket", comment_id)` names the source type,
link API name, and source id in that order. `BoundQuery` accepts the same
handle-first typed form inside Functions.

Returned targets are subject to the same visibility checks as any other read.
Traversal through an identity-revealing link raises `VisibilityError` with
`VISIBILITY_DENIED` for a human consumer before the target is returned; AI
consumers still receive normal scope and sensitivity enforcement on the target
rows. `reverse=True` traverses from the link's target side, and the
identity-revealing denial is symmetric in both directions.

### Visible-row counting

`count(obj_type, where=None)` returns the number of matching rows after the
consumer's scope and row-visibility checks; `exists(...)` reports whether that same
post-visibility selection is non-empty. Both accept the same `where` operator
grammar as `list`. A consumer scoped away from every matching row receives `0`
from `count` and `False` from `exists`, not a privacy refusal.

These two operations are deliberately not min-N-gated. They reveal only the size
of a row set after post-visibility filtering, and the consumer
can already enumerate the same rows with `list(..., limit=None)`, so a min-N
refusal would add no disclosure protection.
`count_contributors` remains the sole privacy-counting primitive: unlike
visible-row counting, it resolves the distinct contributor population behind an
aggregate and therefore keeps the aggregate's min-N release discipline.

### Scope limits and redaction on reads

Every Python read applies scope, row visibility, and `Sensitivity` before it returns.
No Python read tells you, per call, whether rows were hidden.
The two ways a hidden field shows up are the same on each method that returns rows.
A typed read returns the redacted field as `None` and names it in `redacted_fields`.
A string-form read leaves the key out of `payload` and has no `redacted_fields` companion.

- **`get`** returns `None` for a missing or retired object.
  It raises `VisibilityError` with `VISIBILITY_DENIED` for an object outside the consumer's scope or row visibility.
  A redacted field follows the typed and string rules above.
- **`list`** returns only visible rows, and redaction follows the same two rules.
  A short page always means "no more rows".
- **`count`** counts only visible rows.
  A consumer scoped away from every row gets `0`.
- **`exists`** checks only visible rows.
  A consumer scoped away from every row gets `False`.
- **`traverse`** returns only visible target rows, and redaction follows the same two rules.
- **`events`** returns only visible events.
  A redacted payload field is `None` and is named in the record's `redacted_fields`.

The MCP surface differs: its read results carry a `scope_limited` flag and a
per-row `redacted_fields` list.
The Python surface is unchanged.
See [MCP read results](api-mcp.md#read-results).

### Aggregates

`aggregate(...)` and `aggregate_by(..., group_by=...)` accept
`func="mean"|"count"|"sum"|"min"|"max"`; the default remains `"mean"`.
Ungrouped aggregation returns a plain numeric value: `count` is an `int` and
the other functions return `float`. Grouped aggregation returns the corresponding
value per group in a `dict`.

`func="count"` accepts any declared field type — it counts the rows carrying
that field and never coerces the values to `float`. Its `value_field` may also
be omitted (or passed as `None`) to count every visible row in the selection,
min-N gated over those rows' contributors (per group, when grouped); omitting
`value_field` for any other func raises `INVALID_PARAMS`, naming the func and
saying `value_field` is required.

Both enforce hidden-field checks on `where`/`group_by`, min-N over **distinct
contributors**, `UNKNOWN_FIELD` for a `value_field` the type does not declare, and
`NON_NUMERIC_AGGREGATE` for a declared but non-numeric one under `mean`/`sum`/`min`/`max`
(`count` is exempt; both checks run before any row is read). A hidden `value_field` may be aggregated only from an
author-declared Function, over a type declaring `contributor_rules`, and only with
`func="mean"` or `func="count"`. From a consumer surface — client, typed, or MCP —
that operation raises `VisibilityError` with `VISIBILITY_DENIED`; `sum`, `min`, and
`max` remain refused. Every function uses the same min-N release discipline. An
empty visible selection raises
`MIN_N_VIOLATION`; `aggregate_by` also refuses the grouped-empty `{}` case
instead of returning an empty dictionary.

`group_by` is validated like every other field name on this surface, on **all**
forms rather than only the typed one: a name the type does not declare raises
`UNKNOWN_FIELD`, exactly as `where` keys and `order_by` do, instead of matching
nothing and returning the ungrouped value under the key `"None"`. A falsy
`group_by` is never a silent collapse to the ungrouped path either — the code
still differs there by surface: `INVALID_GROUP_BY` on the string form and
`BoundQuery`, `UNKNOWN_FIELD` on the typed form (the class-property check runs
first).

A `json`-declared `group_by` raises `INVALID_GROUP_BY`: its values may be a
`dict` or `list` and so need not be hashable. The **declared type** is what is
checked, not the stored values, so a `json` property that happens to hold only
scalars refuses too rather than working until the first `dict` arrives.
Struct properties also raise `INVALID_GROUP_BY` when used as `group_by`; the
declared struct value is not a supported group key.

Two distinct group values that release as the same dictionary key raise
`GROUP_KEY_COLLISION` — most often an optional property, which keys `None` on
the rows that lack it and collides with a row carrying the literal string
`"None"`. One released cell can only describe one population. The check runs per
group as each is released, after that group's min-N test, so a selection that
min-N would withhold still raises `MIN_N_VIOLATION`.

Every string-form read refuses an unregistered object type with
`UNKNOWN_OBJECT_TYPE`. This covers `list`, `count`, `get`, `exists`, `traverse`,
`aggregate`, `aggregate_by`, `count_contributors`, the `GuardedQuery` methods, and
`BoundQuery` string reads in Function bodies. The type check runs before any other
check and before any row is read, so the refusal is the same for every caller,
whatever their scope. For `traverse`, an unregistered anchor type is refused this
way; an unknown link stays `UNKNOWN_NAME`.

### `GuardedQuery(store, registry, policy)`

The engine-level read path, taking an explicit `consumer` per call:
`.get_object`, `.get_objects`, `.traverse`, `.aggregate`, `.aggregate_by`,
`.count`, `.exists`, and `.count_contributors`. Most
callers use `OntologyClient` instead.

---

