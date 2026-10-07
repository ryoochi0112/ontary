"""The guarded read path (package; implementation in `_guarded`)."""

from ontary.query._guarded import DEFAULT_READ_LIMIT as DEFAULT_READ_LIMIT
from ontary.query._guarded import AggregateFunc as AggregateFunc
from ontary.query._guarded import AggregateValue as AggregateValue
from ontary.query._guarded import GuardedQuery as GuardedQuery
from ontary.query._guarded import OrderBy as OrderBy
from ontary.query._guarded import Page as Page
from ontary.query._guarded import TypedPage as TypedPage

# transitional re-exports, removed once the split lands
# isort: split
from ontary.query._guarded import _AUTHOR_DISPATCH as _AUTHOR_DISPATCH
from ontary.query._guarded import _UNSET_LIMIT as _UNSET_LIMIT
from ontary.query._guarded import _AuthorDispatch as _AuthorDispatch
from ontary.query._where import _normalize_where as _normalize_where
from ontary.query._where import _require_where_mapping as _require_where_mapping

__all__ = [
    "DEFAULT_READ_LIMIT",
    "AggregateFunc",
    "AggregateValue",
    "GuardedQuery",
    "OrderBy",
    "Page",
    "TypedPage",
]
