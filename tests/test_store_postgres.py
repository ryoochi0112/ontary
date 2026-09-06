"""M8a: the Postgres backend's own behavior, beyond the shared contract.

`tests/test_store_conformance.py` runs all 64 behavioral assertions against this
backend when `ONTARY_TEST_POSTGRES_DSN` is set -- that suite is the real proof
the seam holds. What is here is what the shared suite CANNOT express, because it
is Postgres-specific: the schema stamp, the refusal of a database this engine did
not create, concurrent claims across two connections, and the missing-extra
message.

Skipped without a DSN; run against a service container in CI on every PR.
"""

from __future__ import annotations

import os
import sys
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
from conftest import raises_code

from ontary import (
    Consumer,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    prop,
)
from ontary.client import OntologyRuntime
from ontary.errors import ConflictError, VisibilityError
from ontary.store import SCHEMA_VERSION, Store

DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN,
    reason="set ONTARY_TEST_POSTGRES_DSN to run the Postgres backend tests",
)

SRC = Source(source_system="seed")


OntologyFactory = Callable[..., Ontology]
StoreFactory = Callable[..., Store]


@pytest.fixture
def postgres_ontology(make_ontology: OntologyFactory) -> Ontology:
    ontology = make_ontology(name="pg", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0", scope=[SelfScope(level="org")], owned=True, api_name="W"
    )
    class W(OntologyObject):
        id: str = prop(primary_key=True)
        label: str | None = None

    ontology.validate()
    return ontology


@contextmanager
def _schema() -> Iterator[str]:
    """A throwaway schema, dropped afterwards.

    Postgres has no equivalent of `:memory:`, so isolation has to be arranged
    rather than assumed -- and a test that leaves tables behind poisons the next
    run, which is a worse failure than the one it was checking for.
    """
    import psycopg

    assert DSN is not None
    name = f"pgtest_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _dsn_for(schema: str) -> str:
    assert DSN is not None
    separator = "&" if "?" in DSN else "?"
    return f"{DSN}{separator}options=-csearch_path%3D{schema}"


# -- the schema stamp ---------------------------------------------------------


def test_a_fresh_database_is_created_and_stamped(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    with _schema() as schema:
        store = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        try:
            with store._conn.cursor() as cur:
                cur.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'")
                assert cur.fetchone()[0] == str(SCHEMA_VERSION)
        finally:
            store.close()


def test_reopening_a_stamped_database_does_not_recreate_it(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    with _schema() as schema:
        first = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        first.insert("W", {"id": "org-1", "label": "keep"}, SRC)
        first.close()

        second = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        try:
            row = second.read_current("W", "org-1")
            assert row is not None and row.payload["label"] == "keep"
        finally:
            second.close()


def test_a_wrong_schema_version_is_refused_not_migrated(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    """This backend ships without a migration ladder, on purpose: nothing has
    ever written a Postgres store with an older ontary, so a ladder would be
    untested code pretending to be a safety net. A version it cannot read is
    refused with the same coded error a wrong-version SQLite FILE gets."""
    import psycopg

    with _schema() as schema:
        make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        ).close()
        with psycopg.connect(_dsn_for(schema), autocommit=True) as conn:
            conn.execute(
                "UPDATE schema_meta SET value = %s WHERE key = 'schema_version'",
                (str(SCHEMA_VERSION + 1),),
            )

        with raises_code(ConflictError, "STORE_VERSION_UNSUPPORTED") as exc_info:
            make_store(
                postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
            )

        message = str(exc_info.value)
        assert str(SCHEMA_VERSION + 1) in message and str(SCHEMA_VERSION) in message


def test_an_unstamped_database_with_tables_is_refused_not_adopted(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    """The "lying stamp" the SQLite backend's AC11 exists to rule out, in its
    Postgres form: an `objects` table with no stamp was created by something that
    is not this engine, and stamping it would claim a shape nobody verified."""
    import psycopg

    with _schema() as schema:
        with psycopg.connect(_dsn_for(schema), autocommit=True) as conn:
            conn.execute("CREATE TABLE objects (id TEXT)")

        with raises_code(ConflictError, "STORE_VERSION_UNSUPPORTED") as exc_info:
            make_store(
                postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
            )

        assert "did not create" in str(exc_info.value)


# -- concurrency the other backends cannot express ---------------------------


def test_two_connections_never_claim_the_same_effect_row(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    """`FOR UPDATE SKIP LOCKED` on top of the declared lease.

    The lease alone makes double-claiming impossible only if both claimers see
    each other's write; two connections in flight at once are exactly the case a
    single-process test cannot reach, which is why this lives here rather than in
    the shared suite.
    """
    from ontary.outbox import OutboxRecord

    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    with _schema() as schema:
        writer = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        reader = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        try:
            writer.enqueue_effects(
                [
                    OutboxRecord(
                        effect_id=f"eff-{index}",
                        invocation_id="inv-1",
                        seq=index,
                        api_name="Notice",
                        payload={"n": index},
                        action="Emit",
                        actor_id="a",
                        role="Op",
                        emitted_at=now,
                        next_attempt_at=now,
                        updated_at=now,
                    )
                    for index in range(4)
                ]
            )

            first = writer.claim_due_effects(
                limit=2, now=now, lease=timedelta(seconds=60)
            )
            second = reader.claim_due_effects(
                limit=2, now=now, lease=timedelta(seconds=60)
            )

            assert len(first) == 2 and len(second) == 2
            # Disjoint: no row was handed to both drainers.
            assert {r.effect_id for r in first}.isdisjoint(
                {r.effect_id for r in second}
            )
            # And between them they took all four, so SKIP LOCKED skipped rather
            # than blocked -- a blocked second claimer would have returned none.
            assert {r.effect_id for r in first} | {
                r.effect_id for r in second
            } == {"eff-0", "eff-1", "eff-2", "eff-3"}
        finally:
            writer.close()
            reader.close()


def test_a_rolled_back_transaction_leaves_nothing_behind(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    """Same guarantee the other backends make, asserted here against a real
    server: the engine's atomicity claims rest on the transaction actually being
    one, and a connection-level mistake (autocommit left on, say) would pass every
    single-statement test while failing this."""
    with _schema() as schema:
        store = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        try:
            with pytest.raises(RuntimeError):
                with store.transaction():
                    store.insert("W", {"id": "org-1"}, SRC)
                    raise RuntimeError("caller failed")

            assert store.read_current("W", "org-1") is None
            assert store.read_all("W") == []
        finally:
            store.close()


def test_a_governed_runtime_runs_against_postgres(
    postgres_ontology: Ontology, make_store: StoreFactory
) -> None:
    """The point of the backend, end to end: the same guarded runtime, on
    Postgres, refusing what it refuses on SQLite."""
    with _schema() as schema:
        store = make_store(
            postgres_ontology.registry, dsn=_dsn_for(schema), backend="postgres"
        )
        try:
            store.insert("W", {"id": "org-1", "label": "mine"}, SRC)
            store.insert("W", {"id": "org-2", "label": "theirs"}, SRC)
            runtime = OntologyRuntime(postgres_ontology, store)
            client = runtime.for_consumer(
                Consumer(
                    actor_id="a",
                    role="Reader",
                    scope_level="org",
                    scope_id="org-1",
                    kind="human",
                )
            )

            assert client.get("W", "org-1") is not None
            with raises_code(VisibilityError, "VISIBILITY_DENIED"):
                client.get("W", "org-2")
        finally:
            store.close()


# -- the missing extra --------------------------------------------------------


class _BlockPsycopg:
    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> None:
        if fullname == "psycopg" or fullname.startswith("psycopg"):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


def test_missing_postgres_extra_names_the_install_command() -> None:
    """`pip install ontary` then importing this backend used to be a bare
    `ModuleNotFoundError: No module named 'psycopg'`. Same first-contact failure
    the MCP server had, so it gets the same treatment."""
    from ontary.store.postgres import _psycopg

    cached = {
        name: module
        for name, module in sys.modules.items()
        if name == "psycopg" or name.startswith("psycopg")
    }
    for name in cached:
        del sys.modules[name]
    finder = _BlockPsycopg()
    sys.meta_path.insert(0, finder)
    try:
        with pytest.raises(ImportError) as exc_info:
            _psycopg()
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(cached)

    assert "ontary[postgres]" in str(exc_info.value)
    assert isinstance(exc_info.value.__cause__, ModuleNotFoundError)
