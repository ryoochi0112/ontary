"""The domain migration preserves the non-IN SQL compiler contract."""

from typing import Any

import pytest
from _filter_sql_golden import GOLDEN_FILTERS

from ontary.store import _sql
from ontary.store._filter import SQLITE_DOMAIN, RowFilter, compile_filter


@pytest.mark.parametrize("dialect,index", [("sqlite", 0), ("postgres", 1)])
@pytest.mark.parametrize("name,row_filter,sqlite,postgres", GOLDEN_FILTERS,
                         ids=[entry[0] for entry in GOLDEN_FILTERS])
def test_non_in_sql_is_byte_identical(
    name: str, row_filter: RowFilter, sqlite: tuple[str, list[Any]],
    postgres: tuple[str, list[Any]], dialect: _sql.Dialect, index: int,
) -> None:
    assert compile_filter(row_filter, dialect, SQLITE_DOMAIN) == (sqlite, postgres)[index]


def test_golden_corpus_has_at_least_sixty_filters() -> None:
    assert len(GOLDEN_FILTERS) >= 60
