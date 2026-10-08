"""Count object rows returned by an instance's storage steps, for tests only."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps

import pytest

from ontary.store._core import StoreCore


@contextmanager
def count_fetched(store: StoreCore) -> Iterator[Counter[str]]:
    """Count returned rows by object type; restore instance attributes on exit."""
    counter: Counter[str] = Counter()

    def counted(original, single):
        @wraps(original)
        def fetch(obj_type, *args, **kwargs):
            result = original(obj_type, *args, **kwargs)
            counter[obj_type] += int(result is not None) if single else len(result)
            return result

        return fetch

    with pytest.MonkeyPatch.context() as patch:
        for name in (
            "_all_rows", "_filtered_all_rows", "_page_rows", "_filtered_page_rows",
            "_current_row", "_last_row",
        ):
            patch.setattr(store, name, counted(
                getattr(store, name), name in {"_current_row", "_last_row"},
            ))
        yield counter
