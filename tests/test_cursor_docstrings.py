"""Keep pagination and lineage cursor documentation aligned with behavior."""

from ontary import Page, TypedPage
from ontary.store.values import Lineage


def test_page_docstrings_describe_cursor_contract() -> None:
    for docstring in (Page.__doc__, TypedPage.__doc__):
        assert docstring is not None
        assert "never a payload primary key" not in docstring
        assert "after=" in docstring
        assert "never parse" in docstring
        assert "depends on the read" in docstring
        assert "object id" in docstring


def test_lineage_docstring_explains_primary_key_visibility() -> None:
    docstring = Lineage.__doc__
    assert docstring is not None
    assert "object_id" in docstring
    assert "primary key" in docstring
    assert "cannot be declared" in docstring
