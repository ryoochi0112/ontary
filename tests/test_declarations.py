"""Tests for `ontary.declarations` (spec `declared-contracts` §3 AC10):
every field of the `Declarations` value is pinned by asserting the declared
behavior IS the actual runtime behavior -- not just a string this module
happens to return.

A small self-contained "widget" ontology is built once per test: `Org`
(self-scoped) and `Widget` (source-backed except its owned `counter`
property, scoped by `org_id`), plus one action (`BumpCounter`) that writes
the owned `counter` property inside the executor's capture context.
`min_n=2` (deliberately not 1 or 3, so a test asserting `declarations(...)
.min_n == policy.min_n` cannot pass by accident against some other
hardcoded default).
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, Protocol

import pytest
from conftest import raises_code
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from ontary.actions import ActionContext, ActionError
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop, target
from ontary.client import OntologyClient
from ontary.declarations import Declarations, declarations
from ontary.errors import (
    AuthorityError,
    ConflictError,
    PreconditionFailed,
    ValidationFailed,
    VisibilityError,
)
from ontary.functions import BoundQuery
from ontary.ingest import bulk_upsert
from ontary.mcp_server import build_mcp_server, build_multi_consumer_mcp_server
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    FunctionDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.ontology import OntologyDef
from ontary.scope import DirectProperty, ScopePolicy, SelfScope
from ontary.security import Consumer
from ontary.store import (
    ObjectStore,
    Source,
)

SRC = Source(source_system="test")
MIN_N = 2


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
CallFactory = Callable[..., dict[str, Any]]


def _declarations_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Org",
                display_name="Org",
                description="An organization",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            ),
            ObjectTypeDef(
                api_name="Widget",
                display_name="Widget",
                description="A source-backed widget with one ontology-owned property",
                layer="L0",
                properties=[
                    PropertyDef(name="id", type="str"),
                    PropertyDef(name="org_id", type="str", required=False),
                    PropertyDef(name="counter", type="int", required=False),
                ],
                primary_key="id",
                owned={"counter": 0},
            ),
        ],
        action_types=[
            ActionTypeDef(
                api_name="BumpCounter",
                display_name="Bump Counter",
                target_type="Widget",
                executable_by_roles=["Admin"],
                description="Increment a widget's owned counter",
                parameters=[
                    ActionParameterDef(
                        name="widget_id",
                        type="str",
                        refers_to="Widget",
                        scope_semantics="target",
                    ),
                ],
            ),
        ],
    )


def _declarations_policy(
    make_policy: PolicyFactory, registry: OntologyRegistry
) -> ScopePolicy:
    policy = make_policy(
        levels=["org"],
        rules={
            "Org": [SelfScope(level="org")],
            "Widget": [DirectProperty(level="org", property_name="org_id")],
        },
        min_n=MIN_N,
    )
    policy.validate(registry)
    return policy


def _bump_handler(store: ObjectStore) -> Any:
    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        widget = store.read_current("Widget", params["widget_id"])
        if widget is None:
            raise ActionError(f"widget {params['widget_id']!r} does not exist", code="PRECONDITION_FAILED")
        store.update(
            "Widget", params["widget_id"], {"counter": widget.payload["counter"] + 1}, SRC
        )
        return {"widget_id": params["widget_id"]}

    return _handler


def _admin(scope_id: str = "org-1") -> Consumer:
    return Consumer(
        actor_id="admin-1", role="Admin", scope_level="org", scope_id=scope_id, kind="human"
    )


def _build(
    make_registry: RegistryFactory, make_policy: PolicyFactory
) -> tuple[OntologyClient, ObjectStore, OntologyDef]:
    registry = _declarations_registry(make_registry)
    ontology = OntologyDef(
        name="widgets",
        registry=registry,
        policy=_declarations_policy(make_policy, registry),
    )
    ontology.validate()
    store = ObjectStore(registry)
    store.insert("Org", {"id": "org-1"}, SRC)
    store.insert("Widget", {"id": "w-1", "org_id": "org-1", "counter": 0}, SRC)
    client = OntologyClient(ontology, store, _admin())
    client.actions._register("BumpCounter", _bump_handler(store))
    return client, store, ontology


# -- the value itself --------------------------------------------------------


def test_declarations_is_frozen(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    decls = declarations(_build(make_registry, make_policy)[2])
    with pytest.raises(Exception):
        decls.authority = "mutated"  # type: ignore[misc]


def test_declarations_min_n_matches_policy_config(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    _client, _store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.min_n == MIN_N == ontology.policy.min_n


# -- capabilities ------------------------------------------------------------


def test_capabilities_declared_matches_per_call_injection_and_fail_closed() -> None:
    class Reader(Protocol):
        def read(self) -> str: ...

    class ReaderProvider:
        def __init__(self, value: str) -> None:
            self.value = value

        def read(self) -> str:
            return self.value

    ontology = Ontology(name="capability-declaration", scope_levels=["org"], min_n=MIN_N)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")])
    class Item(OntologyObject):
        id: str = prop(primary_key=True)

    reader = ontology.capability(Reader)

    class ReadItemParams(ActionParams):
        item_id: str = target(Item)

    @ontology.action(
        ReadItemParams,
        target=Item,
        roles=["Admin"],
        capabilities=[reader],
        api_name="ReadItem",
    )
    def read_item(ctx: ActionContext, _params: ReadItemParams) -> dict[str, str]:
        return {"value": ctx.capability(reader).read()}

    @ontology.function(api_name="readValue", capabilities=[reader])
    def read_value(query: BoundQuery, _params: dict[str, Any]) -> str:
        return query.capability(reader).read()

    @ontology.function(api_name="undeclaredRead")
    def undeclared_read(query: BoundQuery, _params: dict[str, Any]) -> str:
        return query.capability(reader).read()

    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Item", {"id": "item-1"}, SRC)

    first = OntologyClient(
        ontology,
        store,
        _admin(scope_id="item-1"),
        capabilities={reader: ReaderProvider("first")},
    )
    second = OntologyClient(
        ontology,
        store,
        _admin(scope_id="item-1"),
        capabilities={reader: ReaderProvider("second")},
    )
    decls = first.declarations
    assert decls.capabilities == (
        "declared per action/function; provider bound per client, resolved "
        "per invocation, never process-global; fail-closed when unprovided "
        "or undeclared; the provider itself is trusted author code the "
        "runtime does not sandbox"
    )
    assert ontology.registry.get_action_type("ReadItem").capabilities == ["Reader"]
    assert ontology.registry.get_function("readValue").capabilities == ["Reader"]
    assert first.execute("ReadItem", {"item_id": "item-1"}) == {"value": "first"}
    assert first.call_function("readValue", {}) == "first"
    assert second.call_function("readValue", {}) == "second"

    unprovided = OntologyClient(ontology, store, _admin(scope_id="item-1"))
    with raises_code(PreconditionFailed, "CAPABILITY_NOT_PROVIDED"):
        unprovided.call_function("readValue", {})
    with raises_code(ValidationFailed, "UNDECLARED_CAPABILITY"):
        first.call_function("undeclaredRead", {})


# -- transaction_ownership ----------------------------------------------------


def test_transaction_ownership_declared_matches_actual_refusal(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert "runtime-owned" in decls.transaction_ownership

    with store.transaction():
        with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
            client.execute("BumpCounter", {"widget_id": "w-1"})


# -- idempotency --------------------------------------------------------------


def test_idempotency_declared_matches_two_distinct_audit_entries(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert "distinct" in decls.idempotency

    before = len(store.audit_entries())
    client.execute("BumpCounter", {"widget_id": "w-1"})
    client.execute("BumpCounter", {"widget_id": "w-1"})  # identical retry
    after = store.audit_entries()

    assert len(after) - before == 2
    assert all(e.outcome == "ok" for e in after[-2:])
    # not deduplicated: the widget's counter reflects two separate applies.
    obj = client.get("Widget", "w-1")
    assert obj is not None
    assert obj.payload["counter"] == 2


# -- visibility_default -------------------------------------------------------


def test_visibility_default_declared_matches_deny_by_default(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert "deny-by-default" in decls.visibility_default

    # No org_id, no other rule resolving "org": the scope chain is broken.
    store.insert("Widget", {"id": "w-orphan"}, SRC)
    with raises_code(VisibilityError, "VISIBILITY_DENIED"):
        client.get("Widget", "w-orphan")


# -- min_n ---------------------------------------------------------------


def test_min_n_declared_matches_actual_aggregate_gate(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.min_n == MIN_N

    # Only one visible Widget contributes -- below the configured min_n=2.
    with raises_code(VisibilityError, "MIN_N_VIOLATION") as exc_info:
        client.aggregate("Widget", "counter")
    assert f"min_n={MIN_N}" in str(exc_info.value)


# -- reingest ------------------------------------------------------------


def test_reingest_declared_matches_upsert_merge_survival(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Compact re-assertion of the AC4/AC5 merge-survival behavior
    (`tests/test_ingest.py::test_ingest_owned_property_survives_action_edit_and_reingest`)
    against THIS module's declared `reingest` string."""
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.reingest == (
        "upsert-merge; sources supply only source-backed state; owned "
        "values survive by merge; no deletion"
    )

    registry = ontology.registry
    report1 = bulk_upsert(
        store, registry, "Widget", [{"id": "w-2", "org_id": "org-1"}], SRC
    )
    assert report1.ok
    assert store.read_current("Widget", "w-2").payload["counter"] == 0  # owned default injected

    store.update("Widget", "w-2", {"counter": 9}, SRC)  # action-style edit

    report2 = bulk_upsert(
        store, registry, "Widget", [{"id": "w-2", "org_id": "org-2"}], SRC
    )
    assert report2.ok
    final = store.read_current("Widget", "w-2")
    assert final is not None
    assert final.payload["org_id"] == "org-2"  # source-backed property refreshed
    assert final.payload["counter"] == 9  # owned property survives the re-ingest


# -- authority / writeback ----------------------------------------------


def test_authority_declared_matches_runtime_enforced_refusal(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """authority == "model-declared-runtime-checked": a captured write
    crossing the source-backed/ontology-owned line is refused at runtime,
    not merely documented."""
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.authority == "model-declared-runtime-checked"

    with store.capture_action_writes():
        with raises_code(AuthorityError, "SOURCE_CREATE_REFUSED"):
            store.insert("Org", {"id": "org-sneaky"}, SRC)  # Org is not owned=True


def test_writeback_declared_matches_owned_writes_and_source_refusal(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Ontology-owned writes commit, while captured source-backed writes
    have no legal write path and are refused."""
    client, store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.writeback == (
        "ontology writes are all ontology-owned; the runtime has no outward "
        "write path of its own — but a capability provider is unsandboxed "
        "author code that can write outward inline, so this is a declared "
        "convention, not an enforced boundary"
    )

    client.execute("BumpCounter", {"widget_id": "w-1"})
    widget = store.read_current("Widget", "w-1")
    assert widget is not None and widget.payload["counter"] == 1

    with store.capture_action_writes():
        with raises_code(AuthorityError, "UNDECLARED_SOURCE_WRITE"):
            store.update("Widget", "w-1", {"org_id": "org-2"}, SRC)  # source-backed


def test_writeback_declaration_admits_the_unenforced_capability_path() -> None:
    """The `writeback` string's "declared convention, not an enforced boundary"
    clause must be TRUE, i.e. the loophole it admits must actually exist.

    Whole-branch review finding (2026-07-26): an earlier string claimed the
    runtime enforced the boundary. That is false. A capability provider is
    arbitrary author code returned UNWRAPPED, so a provider can write outward
    inline -- from a Function, which AC4 otherwise bars from any outward write,
    and from an Action before it raises and rolls its ontology writes back.
    The runtime cannot observe it.

    This test demonstrates the loophole deliberately, so the honest wording is
    pinned to real behavior the same way every other declaration is. If a later
    milestone ever DOES enforce the boundary (a provider proxy, an outward-write
    capability kind), this test should fail and the string should be restored to
    the stronger claim -- that is the point of pinning it.
    """
    ontology = Ontology("writeback-loophole", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Note(OntologyObject):
        id: str = prop(primary_key=True)

    sent: list[str] = []

    class Mailer:
        """A "capability" that in fact performs an outward WRITE."""

        def send(self, body: str) -> None:
            sent.append(body)

    MAILER = ontology.capability(Mailer)

    @ontology.function(api_name="leak", capabilities=[MAILER])
    def leak(query: BoundQuery, _params: dict[str, object]) -> str:
        query.capability(MAILER).send("sent from a Function")
        return "done"

    ontology.validate()
    store = ObjectStore(ontology.registry)
    client = OntologyClient(
        ontology, store, _admin(scope_id="org-1"), capabilities={MAILER: Mailer()}
    )

    assert client.call_function("leak", {}) == "done"
    # A Function performed an outward write. The runtime never saw it.
    assert sent == ["sent from a Function"]

    # Functions ARE audited now (M7), so the log is no longer empty -- but the
    # loophole this test exists to pin is untouched, and that distinction is
    # the whole point. What got recorded is that the capability was RETRIEVED,
    # once. What did not, and structurally cannot, get recorded is the outward
    # write the provider then performed: the provider is handed back unwrapped,
    # so the runtime never sees `.send(...)` happen at all.
    entries = store.audit_entries()
    assert [(e.kind, e.action, e.outcome) for e in entries] == [
        ("function", "leak", "ok")
    ]
    assert [(a.api_name, a.count) for a in entries[0].capability_accesses] == [
        ("Mailer", 1)
    ]
    assert entries[0].writes == []


# -- audit_scope -----------------------------------------------------------


def test_audit_scope_declared_matches_unscoped_administrative_view(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """`audit_scope` names two things, and both must stay true.

    "tenant-scoped administrative view" (M8b sharpened this from "unscoped"):
    `audit_entries()` still takes no consumer/scope argument -- there is no
    CONSUMER scoping to apply, structurally -- but the store is bound to a tenant
    and the log it returns is that tenant's. Both halves matter: administrative
    within a tenant, never across one.

    And the coverage clause added in M7: actions are always audited, functions
    only when `FunctionDef.audited` says so. A `Declarations` value exists so a
    consumer need not read the source to know what the runtime does, so it has
    to state the conditional rather than imply blanket coverage.

    The third clause is the one a declaration cannot express: a call that
    RELEASES a hidden field through the AC10 contributor exemption is audited
    whatever the function declared, `audit=False` included. It belongs here
    because a consumer reading `audit_scope` to answer "could a hidden value
    have left this system unrecorded?" would otherwise read the conditional
    and conclude yes. Pinned against behaviour in
    `tests/test_function_audit_boundary.py`.
    """
    _client, _store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    assert decls.audit_scope == (
        "tenant-scoped administrative view; actions always audited, functions "
        "audited iff they declare capabilities unless overridden per function, "
        "and always when a call releases a hidden field through the "
        "contributor exemption"
    )

    params = list(inspect.signature(ObjectStore.audit_entries).parameters)
    assert params == ["self"]

    # The coverage clause, pinned against real behavior rather than prose.
    capability_free = FunctionDef(
        api_name="pure", description="", input_description="", output_description=""
    )
    with_capability = FunctionDef(
        api_name="reaches",
        description="",
        input_description="",
        output_description="",
        capabilities=["Clock"],
    )
    assert capability_free.audited is False
    assert with_capability.audited is True
    assert FunctionDef(
        api_name="forced",
        description="",
        input_description="",
        output_description="",
        audit=True,
    ).audited is True


# -- identity ----------------------------------------------------------------


def test_identity_declared_matches_transport_proof_resolver_trust_and_dual_audit(
    make_call: CallFactory,
) -> None:
    """`identity`'s clauses, each demonstrated against real behavior rather
    than left as prose (spec `multi-consumer-mcp` AC11):

    1. Scoped to the multi-consumer server: proven by the transport, not
       the runtime -- with no `AccessToken` on the request (no configured
       verifier), that server refuses every call -- never a default
       identity.
    2. The honest counterpart on the OTHER surface: a single-consumer
       server (`build_mcp_server`) has no verifier at all and refuses
       NOTHING -- its Consumer is operator-asserted at construction, not
       proven. (A prior draft of this string claimed refusal here too;
       that claim falsified itself the moment it was served by exactly
       this server.)
    3. Mapped by trusted, unsandboxed author code: a resolver that maps
       every principal onto ONE privileged actor is not stopped -- the
       runtime does not inspect or constrain what the resolver returns.
    4. Both the principal and the resolved actor are audited, which is what
       makes that resolver's choice VISIBLE in the log rather than hidden.
    """
    ontology = Ontology(name="identity-declaration", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0",
        owned={"touched": False},
        scope=[SelfScope(level="org")],
    )
    class Item(OntologyObject):
        id: str = prop(primary_key=True)
        touched: bool = False

    class TouchParams(ActionParams):
        item_id: str = target(Item)

    @ontology.action(TouchParams, target=Item, roles=["Admin"], api_name="Touch")
    def touch(ctx: ActionContext, params: TouchParams) -> dict[str, str]:
        ctx.update("Item", params.item_id, {"touched": True})
        return {"item_id": params.item_id}

    ontology.validate()
    store = ObjectStore(ontology.registry)
    store.insert("Item", {"id": "item-1"}, SRC)

    decls = declarations(ontology.definition)
    assert decls.identity == (
        "on a multi-consumer MCP server, proven by the transport, not by "
        "this runtime -- no token is verified or issued here, so such a "
        "deployment with no configured verifier refuses every call rather "
        "than assuming a default identity; on a single-consumer server or "
        "in direct Python use, the Consumer is asserted by the operator at "
        "construction and nothing proves it; the verified principal (multi-"
        "consumer only) is mapped to a Consumer by a resolver, which is "
        "trusted author code the runtime does not sandbox, on the same "
        "footing as a capability provider; both that principal and the "
        "actor it resolved to are audited, so a resolver mapping every "
        "principal onto one privileged actor is visible in the log rather "
        "than hidden by it; not every audited row has one"
    )

    # (2) Falsify the OLD claim, prove the NEW one: a single-consumer server
    # has no verifier configured and no token on the call, yet refuses
    # NOTHING -- its Consumer was asserted at construction.
    single_consumer_store = ObjectStore(ontology.registry)
    single_consumer_store.insert("Item", {"id": "item-1"}, SRC)
    single_consumer_server = build_mcp_server(
        ontology,
        single_consumer_store,
        Consumer(
            actor_id="op", role="Admin", scope_level="org", scope_id="item-1", kind="human"
        ),
    )
    unrefused = make_call(single_consumer_server, "get_declarations", {})
    assert "result" in unrefused
    assert "error" not in unrefused

    def _one_privileged_actor(_token: AccessToken) -> Consumer:
        # Deliberately collapses EVERY authenticated principal onto the same
        # actor -- the loophole clause (2) admits the runtime cannot close.
        return Consumer(
            actor_id="root", role="Admin", scope_level="org", scope_id="item-1", kind="human"
        )

    server = build_multi_consumer_mcp_server(
        ontology, store, resolve_consumer=_one_privileged_actor
    )

    # (1) No proof from the transport -> refused, not a default identity.
    unauthenticated = make_call(server, "get_declarations", {})
    assert unauthenticated["error"]["code"] == "UNAUTHENTICATED"
    assert "result" not in unauthenticated

    # (2) + (3): whatever principal authenticates, the resolver maps it to
    # "root" -- and that collapse is visible in the audit log, not hidden.
    access_token = AccessToken(token="t", client_id="agent-alice", scopes=[], subject="alice")
    reset = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        result = make_call(
            server, "execute_action", {"api_name": "Touch", "params": {"item_id": "item-1"}}
        )
    finally:
        auth_context_var.reset(reset)
    assert result == {"result": {"item_id": "item-1"}}

    entry = store.audit_entries()[-1]
    assert entry.actor == "root"  # the resolver's one privileged actor
    assert entry.principal == "alice"  # the transport-proved principal
    assert entry.actor != entry.principal  # the collapse IS visible

    # `identity` itself is exposed unchanged over the wire, same as every
    # other `Declarations` field -- `get_declarations` returns
    # `client.declarations.model_dump()` verbatim.
    access_token_2 = AccessToken(token="t2", client_id="agent-bob", scopes=[], subject="bob")
    reset_2 = auth_context_var.set(AuthenticatedUser(access_token_2))
    try:
        declared = make_call(server, "get_declarations", {})
    finally:
        auth_context_var.reset(reset_2)
    assert declared["result"]["identity"] == decls.identity


def test_declarations_class_round_trips_every_field_through_model_dump(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    """Guard the CLASS (lesson L-c), not one field: `model_dump()` -- what
    `get_declarations` sends over MCP verbatim -- must carry EVERY field of
    `Declarations` unchanged. A future field silently excluded from the wire
    (e.g. a custom `model_dump`/`model_config` override) fails this without
    needing its own dedicated MCP round-trip test."""
    _client, _store, ontology = _build(make_registry, make_policy)
    decls = declarations(ontology)
    dumped = decls.model_dump()

    assert set(dumped) == set(Declarations.model_fields)
    for field_name in Declarations.model_fields:
        assert dumped[field_name] == getattr(decls, field_name), (
            f"{field_name!r} did not round-trip through model_dump() unchanged"
        )
