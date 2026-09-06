"""M8b: tenant isolation, on every backend that can leak.

Two questions, kept apart on purpose:

1. **Does the engine scope every read and write to its tenant?** Asked of the two
   backends where two stores can share a substrate (one SQLite file, one Postgres
   database). An `InMemoryStore` cannot leak -- two instances share no state -- so
   including it here would be a test that passes for the wrong reason.
2. **Does the DATABASE also enforce it?** Postgres only, and only as a
   non-superuser, because superusers bypass RLS by design. This is the
   defense-in-depth claim, and the only way to test it honestly is to issue raw
   SQL with NO tenant predicate and check what comes back.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import pytest
from conftest import raises_code

from ontary import (
    Cardinality,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    prop,
)
from ontary.errors import ValidationFailed
from ontary.store import DEFAULT_TENANT, AuditEntry, InMemoryStore, Store

SRC = Source(source_system="seed")
DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")


OntologyFactory = Callable[..., Ontology]
StoreFactory = Callable[..., Store]


def _tenancy_ontology(make_ontology: OntologyFactory) -> Ontology:
    ontology = make_ontology(name="tenancy", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0", scope=[SelfScope(level="org")], owned=True, api_name="W"
    )
    class W(OntologyObject):
        id: str = prop(primary_key=True)
        label: str | None = None

    ontology.link("relates", W, W, Cardinality.MANY_TO_MANY, owned=True)
    ontology.validate()
    return ontology


# -- the pair of stores under test -------------------------------------------


@contextmanager
def _sqlite_pair(
    tmp_path: Path,
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> Iterator[tuple[Any, Any]]:
    """Two stores, ONE file, two tenants."""
    ontology = _tenancy_ontology(make_ontology)
    path = str(tmp_path / "shared.db")
    yield (
        make_store(ontology.registry, path, tenant="acme"),
        make_store(ontology.registry, path, tenant="globex"),
    )


@contextmanager
def _postgres_pair(
    _tmp_path: Path,
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> Iterator[tuple[Any, Any]]:
    """Two stores, ONE database schema, two tenants."""
    import psycopg

    assert DSN is not None
    ontology = _tenancy_ontology(make_ontology)
    schema = f"tenancy_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in DSN else "?"
    dsn = f"{DSN}{separator}options=-csearch_path%3D{schema}"
    first = make_store(ontology.registry, dsn, backend="postgres", tenant="acme")
    second = make_store(ontology.registry, dsn, backend="postgres", tenant="globex")
    try:
        yield first, second
    finally:
        first.close()
        second.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


_PAIRS = [pytest.param(_sqlite_pair, id="sqlite")]
if DSN:
    _PAIRS.append(pytest.param(_postgres_pair, id="postgres"))


@pytest.fixture(params=_PAIRS)
def tenants(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> Iterator[tuple[Any, Any]]:
    with request.param(tmp_path, make_ontology, make_store) as pair:
        yield pair


# -- objects ------------------------------------------------------------------


def test_one_tenant_cannot_read_anothers_objects(tenants: tuple[Any, Any]) -> None:
    acme, globex = tenants
    acme.insert("W", {"id": "w-1", "label": "acme's"}, SRC)

    assert acme.read_current("W", "w-1") is not None
    assert globex.read_current("W", "w-1") is None
    assert globex.read_all("W") == []
    assert globex.read_page("W") == []


def test_the_same_primary_key_can_exist_in_two_tenants(
    tenants: tuple[Any, Any],
) -> None:
    """Ids are unique per tenant, not globally -- otherwise onboarding a tenant
    whose ids collide with an existing one would be impossible."""
    acme, globex = tenants
    acme.insert("W", {"id": "w-1", "label": "acme's"}, SRC)
    globex.insert("W", {"id": "w-1", "label": "globex's"}, SRC)

    assert acme.read_current("W", "w-1").payload["label"] == "acme's"  # type: ignore[union-attr]
    assert globex.read_current("W", "w-1").payload["label"] == "globex's"  # type: ignore[union-attr]


def test_one_tenant_cannot_update_anothers_object(tenants: tuple[Any, Any]) -> None:
    acme, globex = tenants
    acme.insert("W", {"id": "w-1", "label": "acme's"}, SRC)

    # Not "updated nothing" -- refused, because from globex's view the row does
    # not exist, and silently succeeding on zero rows is how a cross-tenant write
    # gets mistaken for a no-op.
    with raises_code(ValidationFailed, "OBJECT_NOT_FOUND"):
        globex.update("W", "w-1", {"label": "stolen"}, SRC)

    assert acme.read_current("W", "w-1").payload["label"] == "acme's"  # type: ignore[union-attr]


def test_read_last_is_tenant_scoped(tenants: tuple[Any, Any]) -> None:
    """`read_last` sees RETIRED rows, so it needs its own tenancy row.

    Every other object read filters `valid_to IS NULL`, which means a
    tenant-blind one is caught by the pins above the moment the row is live.
    `read_last` answers for a retired row too -- the state those pins never
    leave a row in -- and it now feeds the action target gate and
    `_resolve_level`, so a tenant-blind implementation would let one tenant
    resolve another's object scope with nothing here to catch it.
    """
    acme, globex = tenants
    acme.insert("W", {"id": "w-1", "label": "acme's"}, SRC)
    globex.insert("W", {"id": "w-2", "label": "globex's"}, SRC)
    acme.retire_object("W", "w-1")

    # Retired in its own tenant: still readable there, by id, as a tombstone.
    assert acme.read_last("W", "w-1") is not None
    assert acme.read_current("W", "w-1") is None

    # ...and not readable at all from the other tenant, live or retired.
    assert globex.read_last("W", "w-1") is None
    assert acme.read_last("W", "w-2") is None


def test_asof_link_reads_are_tenant_scoped(tenants: tuple[Any, Any]) -> None:
    """`links_from_asof`/`links_to_asof` see CLOSED links, so they need
    their own tenancy row for the same reason `read_last` does: the live-only
    pin above cannot leave a link in the state these two reads exist to
    answer for."""
    acme, globex = tenants
    for store in (acme, globex):
        store.insert("W", {"id": "a"}, SRC)
        store.insert("W", {"id": "b"}, SRC)
    acme.create_link("relates", "a", "b")
    closed_at = datetime.now(timezone.utc).isoformat()
    acme.close_link("relates", "a", "b")

    assert acme.links_from_asof("relates", "a", closed_at) == ["b"]
    assert acme.links_to_asof("relates", "b", closed_at) == ["a"]
    assert globex.links_from_asof("relates", "a", closed_at) == []
    assert globex.links_to_asof("relates", "b", closed_at) == []


def test_a_page_token_from_another_tenant_is_refused(
    tenants: tuple[Any, Any],
) -> None:
    """The same fail-open the Postgres backend had for object types, in its
    tenant form: a globally unique token must not be a resume point into another
    tenant's rows."""
    acme, globex = tenants
    acme.insert("W", {"id": "w-1"}, SRC)
    token = acme.read_page("W")[0].key

    with raises_code(ValidationFailed, "INVALID_CURSOR"):
        globex.read_page("W", after_key=token)


# -- links and audit ----------------------------------------------------------


def test_links_are_tenant_scoped(tenants: tuple[Any, Any]) -> None:
    acme, globex = tenants
    for store in (acme, globex):
        store.insert("W", {"id": "a"}, SRC)
        store.insert("W", {"id": "b"}, SRC)
    acme.create_link("relates", "a", "b")

    assert acme.links_from("relates", "a") == ["b"]
    assert globex.links_from("relates", "a") == []
    assert globex.links_to("relates", "b") == []


def test_the_audit_log_is_tenant_scoped(tenants: tuple[Any, Any]) -> None:
    """An audit log that showed another tenant's actions would be a governance
    failure, not a convenience bug: `audit_scope` is declared as an unscoped
    ADMINISTRATIVE view, and "administrative" has to stop at the tenant."""
    acme, globex = tenants
    acme.append_audit(
        AuditEntry(
            actor="acme-op",
            role="Op",
            action="DoThing",
            target_type="W",
            outcome="ok",
        )
    )

    assert [e.actor for e in acme.audit_entries()] == ["acme-op"]
    assert globex.audit_entries() == []


# -- the default tenant -------------------------------------------------------


def test_rows_written_without_a_tenant_belong_to_the_default_tenant(
    tmp_path: Path,
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """A store opened without a tenant argument writes and reads under
    `DEFAULT_TENANT`, and naming that tenant explicitly sees the same rows."""
    ontology = _tenancy_ontology(make_ontology)
    path = str(tmp_path / "upgraded.db")
    before = make_store(ontology.registry, path)
    before.insert("W", {"id": "legacy", "label": "written without a tenant"}, SRC)

    after = make_store(ontology.registry, path)
    row = after.read_current("W", "legacy")
    assert row is not None and row.payload["label"] == "written without a tenant"

    named = make_store(ontology.registry, path, tenant=DEFAULT_TENANT)
    assert named.read_current("W", "legacy") is not None


def test_an_empty_tenant_is_refused(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """Not a silent fallback to the default: `tenant=""` is a bug at the call
    site, and defaulting it would turn that bug into a cross-tenant read."""
    ontology = _tenancy_ontology(make_ontology)
    with pytest.raises(ValueError, match="non-empty"):
        make_store(ontology.registry, tenant="")
    with pytest.raises(ValueError, match="non-empty"):
        InMemoryStore(ontology.registry, tenant="")


def test_in_memory_honors_the_tenant_it_was_given(
    make_ontology: OntologyFactory,
) -> None:
    """In-memory isolation is structural (two instances share nothing), so this
    pins the weaker thing that IS testable: the tenant is honored rather than
    ignored, so a caller cannot "verify" tenancy against a store that silently
    dropped the argument."""
    ontology = _tenancy_ontology(make_ontology)
    store = InMemoryStore(ontology.registry, tenant="acme")
    store.insert("W", {"id": "w-1"}, SRC)

    assert store.read_current("W", "w-1") is not None
    assert store._objects[0]["tenant"] == "acme"


# -- defense in depth: does the DATABASE enforce it? -------------------------


pytestmark_rls = pytest.mark.skipif(
    not DSN, reason="set ONTARY_TEST_POSTGRES_DSN to run the RLS tests"
)


@contextmanager
def _rls_fixture(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> Iterator[tuple[str, str, str]]:
    """A schema plus a NON-SUPERUSER role that owns nothing.

    Both halves matter. A superuser bypasses RLS unconditionally -- that is
    Postgres, not a setting -- so a test connecting as one would pass whether or
    not the policies exist, which is worse than no test. And the policies are
    created with `FORCE`, so even the table owner is subject to them; the role
    here is granted privileges without owning the tables, which is what a real
    application role looks like.
    """
    import psycopg

    assert DSN is not None
    schema = f"rls_{uuid.uuid4().hex[:10]}"
    role = f"rls_app_{uuid.uuid4().hex[:8]}"
    password = "rls-test-password"

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
        conn.execute(f"CREATE ROLE {role} LOGIN PASSWORD '{password}' NOSUPERUSER")

    separator = "&" if "?" in DSN else "?"
    owner_dsn = f"{DSN}{separator}options=-csearch_path%3D{schema}"
    # Create the schema (and the RLS policies) as the owner, then hand the app
    # role exactly the privileges an application needs.
    ontology = _tenancy_ontology(make_ontology)
    owner_store = make_store(
        ontology.registry, owner_dsn, backend="postgres", tenant="acme"
    )
    owner_store.insert("W", {"id": "acme-1", "label": "acme's row"}, SRC)
    owner_store.close()
    globex_store = make_store(
        ontology.registry, owner_dsn, backend="postgres", tenant="globex"
    )
    globex_store.insert("W", {"id": "globex-1", "label": "globex's row"}, SRC)
    globex_store.close()

    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'GRANT USAGE ON SCHEMA "{schema}" TO {role}')
        conn.execute(
            f'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA "{schema}" '
            f"TO {role}"
        )
        conn.execute(
            f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA "{schema}" TO {role}'
        )

    import urllib.parse

    parsed = urllib.parse.urlsplit(DSN)
    netloc = f"{role}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc += f":{parsed.port}"
    app_dsn = urllib.parse.urlunsplit(
        (parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment)
    )
    app_dsn += ("&" if "?" in app_dsn else "?") + f"options=-csearch_path%3D{schema}"

    try:
        yield schema, role, app_dsn
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.execute(f"DROP ROLE IF EXISTS {role}")


@pytestmark_rls
def test_raw_sql_without_a_tenant_predicate_still_sees_only_one_tenant(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """THE defense-in-depth test, and the only one that means anything.

    Every statement this backend issues already filters by tenant, so a test
    going through the store would pass with RLS switched off entirely. This one
    bypasses the engine: it opens a store (which sets `ontary.tenant`), then
    issues `SELECT * FROM objects` with **no WHERE clause at all** on that same
    connection. Two tenants' rows exist. If the policy works, one comes back.
    """
    with _rls_fixture(make_ontology, make_store) as (_schema, _role, app_dsn):
        store = make_store(
            _tenancy_ontology(make_ontology).registry,
            app_dsn,
            backend="postgres",
            tenant="acme",
        )
        try:
            with store._conn.cursor() as cur:
                cur.execute("SELECT id, tenant FROM objects")
                rows = cur.fetchall()
        finally:
            store.close()

    assert [(row[0], row[1]) for row in rows] == [("acme-1", "acme")]


@pytestmark_rls
def test_a_write_cannot_forge_another_tenants_row(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """A tenant cannot write a row labeled as somebody else's.

    Corrected after a mutation check: this docstring used to claim it proved
    `WITH CHECK` is needed as well as `USING`, and removing `WITH CHECK` from the
    policies left the test green. Postgres falls back to the `USING` expression
    when a policy has no `WITH CHECK`, so for policies whose two expressions are
    identical -- as these are -- `WITH CHECK` is redundant. It is kept because it
    is explicit and because the two would have to be written separately the day
    they diverge; what this test actually pins is the OUTCOME (a forged tenant
    label is refused), which holds either way.
    """
    import psycopg

    with _rls_fixture(make_ontology, make_store) as (_schema, _role, app_dsn):
        store = make_store(
            _tenancy_ontology(make_ontology).registry,
            app_dsn,
            backend="postgres",
            tenant="acme",
        )
        try:
            with pytest.raises(psycopg.errors.Error):
                with store._conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO objects (object_type, id, payload, valid_from, "
                        "source_system, page_token, tenant) "
                        "VALUES ('W', 'forged', '{}', 'now', 's', 'tok', 'globex')"
                    )
            store._conn.rollback()
        finally:
            store.close()


@pytestmark_rls
def test_an_unset_tenant_setting_sees_nothing(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """Fail-closed. `current_setting('ontary.tenant', true)` is NULL when unset
    and `tenant = NULL` is never true, so a connection that never set it reads
    zero rows rather than all of them -- which is the direction a mistake here
    has to fail in."""
    import psycopg

    with _rls_fixture(make_ontology, make_store) as (_schema, _role, app_dsn):
        with psycopg.connect(app_dsn) as raw:
            rows = raw.execute("SELECT id FROM objects").fetchall()

    assert rows == []


@pytestmark_rls
def test_rls_false_leaves_the_engine_filter_as_the_only_layer(
    make_ontology: OntologyFactory,
    make_store: StoreFactory,
) -> None:
    """`rls=False` is for a deployment whose role cannot own the tables. It must
    disable the SECOND layer only -- the engine's own scoping still holds, which
    is what this asserts, so nobody reads the flag as "tenancy off"."""
    import psycopg

    assert DSN is not None
    schema = f"norls_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in DSN else "?"
    dsn = f"{DSN}{separator}options=-csearch_path%3D{schema}"
    try:
        ontology = _tenancy_ontology(make_ontology)
        acme = make_store(
            ontology.registry,
            dsn,
            backend="postgres",
            tenant="acme",
            rls=False,
        )
        globex = make_store(
            ontology.registry,
            dsn,
            backend="postgres",
            tenant="globex",
            rls=False,
        )
        try:
            acme.insert("W", {"id": "w-1", "label": "acme's"}, SRC)

            assert globex.read_current("W", "w-1") is None
            assert acme.read_current("W", "w-1") is not None
            # And the database is NOT enforcing it, which is the honest difference:
            # raw SQL sees both tenants.
            with globex._conn.cursor() as cur:
                cur.execute("SELECT tenant FROM objects")
                assert [row[0] for row in cur.fetchall()] == ["acme"]
        finally:
            acme.close()
            globex.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
