"""Guarded query layer: the only public read path over the object store.

`GuardedQuery` enforces scope visibility, sensitivity redaction
(`ai_usable` / `human_visible`), and the min-N aggregation guard for any
ontology declared via `ontary.meta` + `ontary.scope.ScopePolicy`. The store's
raw reads stay engine-internal; every human or AI consumer reads through
`GuardedQuery`.

Submodules, from low to high in the import order:

- `_where`: `where` normalize, evaluate, compile, and key/operator validation.
- `_params`: read-parameter checks (fields, `order_by`, `group_by`, property type).
- `_disclosure`: scope and row visibility, hidden fields, redaction, disclosure records.
- `_host`: the `_QueryHost` Protocol the moved bodies call back through.
- `_paging`: `Page`, `TypedPage`, limits, ordering, row-id pages.
- `_aggregate`: min-N aggregation, release, and contributor counts.
- `_guarded`: `GuardedQuery`, the public reads and short delegates.
"""

from ontary.query._aggregate import AggregateFunc as AggregateFunc
from ontary.query._aggregate import AggregateValue as AggregateValue
from ontary.query._guarded import GuardedQuery as GuardedQuery
from ontary.query._paging import DEFAULT_READ_LIMIT as DEFAULT_READ_LIMIT
from ontary.query._paging import OrderBy as OrderBy
from ontary.query._paging import Page as Page
from ontary.query._paging import TypedPage as TypedPage

__all__ = [
    "DEFAULT_READ_LIMIT",
    "AggregateFunc",
    "AggregateValue",
    "GuardedQuery",
    "OrderBy",
    "Page",
    "TypedPage",
]
