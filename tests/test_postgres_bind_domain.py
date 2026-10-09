"""Postgres encoding domains can be decided without a database connection."""

import pytest

from ontary.store._filter import BindDomain
from ontary.store.postgres import _bind_domain


@pytest.mark.parametrize(
    "client,server,expected",
    [
        ("utf-8", "UTF8", ("utf-8",)),
        ("iso8859-1", "LATIN1", ("iso8859-1",)),
        ("utf-8", "LATIN1", ("utf-8", "iso8859-1")),
        ("iso8859-1", "UTF8", ("iso8859-1", "utf-8")),
        ("utf-8", "SQL_ASCII", ("utf-8",)),
        ("iso8859-1", "SQL_ASCII", ("iso8859-1",)),
        ("utf-8", "MULE_INTERNAL", ("utf-8", "ascii")),
        ("ascii", "MULE_INTERNAL", ("ascii",)),
    ],
)
def test_postgres_bind_domain_mapping(client, server, expected):
    pytest.importorskip("psycopg")
    assert _bind_domain(client, server) == BindDomain(expected)
