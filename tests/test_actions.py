"""Unit tests for `ontary.actions.ActionExecutor` on a toy ontology.

Toy "library" domain (Library/Shelf/Book/Loan) -- deliberately not DSO --
exercising the generic action pipeline: registration, role permission,
declared-parameter scope enforcement (`target` and `scope` semantics),
parameter validation, precondition/handler-exception rollback, and audit
completeness (denied/error/ok). Reuses the ScopePolicy shape from
`test_scope.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from conftest import raises_code
from pydantic import BaseModel, Field

from ontary.actions import ActionContext, ActionError, ActionExecutor
from ontary.errors import ConflictError, InternalError, PermissionDenied, ValidationFailed
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.scope import ScopePolicy, SelfScope, ViaLink
from ontary.security import Consumer
from ontary.store import (
    ObjectStore,
    Source,
    WriteRecord,
)

LEVELS = ["shelf", "library"]

SRC = Source(source_system="test")


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
StoreFactory = Callable[..., ObjectStore]

ACTION_OBJECT_TYPES = [
    ObjectTypeDef(
        api_name="Library",
        display_name="Library",
        description="A library building",
        layer="L0",
        properties=[PropertyDef(name="id", type="str")],
        primary_key="id",
    ),
    ObjectTypeDef(
        api_name="Shelf",
        display_name="Shelf",
        description="A shelf inside a library",
        layer="L0",
        properties=[PropertyDef(name="id", type="str")],
        primary_key="id",
    ),
    ObjectTypeDef(
        api_name="Book",
        display_name="Book",
        description="A book, shelved somewhere",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="status", type="str", required=False),
        ],
        primary_key="id",
        # Declared ontology-owned (declared-contracts §3 AC1) so the
        # RegisterBook/RegisterBookUnwrapped handlers below, which
        # create a Book from inside `execute()`'s write-capture
        # context, aren't refused as a source-backed create.
        owned=True,
    ),
    ObjectTypeDef(
        api_name="Loan",
        display_name="Loan",
        description="A book on loan",
        layer="L0",
        properties=[
            PropertyDef(name="id", type="str"),
            PropertyDef(name="borrower", type="str", required=False),
        ],
        primary_key="id",
        # Declared ontology-owned: CheckoutBook's handler creates a
        # Loan from inside execute()'s write-capture context.
        owned=True,
    ),
]

ACTION_LINK_TYPES = [
    LinkTypeDef(
        api_name="inLibrary",
        from_type="Shelf",
        to_type="Library",
        cardinality=Cardinality.MANY_TO_ONE,
        description="Shelf -> its library",
    ),
    LinkTypeDef(
        api_name="onShelf",
        from_type="Book",
        to_type="Shelf",
        # MANY_TO_MANY (rather than the more realistic MANY_TO_ONE) so the
        # RelocateBook handler in these tests can freely add a new
        # onShelf link without first tearing down the old one -- the
        # store has no link-removal API, and that plumbing is out of
        # scope for this test's purpose (exercising the scope pipeline).
        cardinality=Cardinality.MANY_TO_MANY,
        description="Book -> its shelf",
        # RelocateBook's handler creates this link from inside
        # execute()'s write-capture context (declared-contracts §3 AC1).
        owned=True,
    ),
    LinkTypeDef(
        api_name="loanedBook",
        from_type="Loan",
        to_type="Book",
        cardinality=Cardinality.MANY_TO_ONE,
        description="Loan -> the book on loan",
        # CheckoutBook's handler creates this link from inside
        # execute()'s write-capture context.
        owned=True,
    ),
]

ACTION_TYPES = [
    ActionTypeDef(
        api_name="CheckoutBook",
        display_name="Checkout Book",
        target_type="Loan",
        executable_by_roles=["Librarian"],
        description="Check a book out on loan",
        parameters=[
            ActionParameterDef(
                name="book_id",
                type="str",
                refers_to="Book",
                scope_semantics="target",
            ),
            ActionParameterDef(name="borrower", type="str"),
        ],
    ),
    ActionTypeDef(
        api_name="RelocateBook",
        display_name="Relocate Book",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description="Move a book to a different shelf",
        parameters=[
            ActionParameterDef(
                name="book_id",
                type="str",
                refers_to="Book",
                scope_semantics="target",
            ),
            ActionParameterDef(
                name="shelf_id",
                type="str",
                refers_to="Shelf",
                scope_semantics="scope",
            ),
        ],
    ),
    ActionTypeDef(
        api_name="RegisterBook",
        display_name="Register Book",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description="Register a new book (used for exception-rollback tests)",
        parameters=[ActionParameterDef(name="fail", type="bool", required=False)],
    ),
    ActionTypeDef(
        api_name="RegisterBookUnwrapped",
        display_name="Register Book (handler does not wrap its own txn)",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description=(
            "Register a new book via a handler that does NOT wrap its "
            "own writes in `store.transaction()` -- used to prove the "
            "engine (not the handler) owns transactionality (finding 4)."
        ),
        parameters=[ActionParameterDef(name="fail", type="bool", required=False)],
    ),
    ActionTypeDef(
        api_name="AcknowledgeGap",
        display_name="Acknowledge Gap",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description=(
            "Toy action whose handler raises ActionError with an "
            "author-supplied code (AC7 round-trip test)."
        ),
        parameters=[
            ActionParameterDef(name="acknowledged", type="bool", required=False)
        ],
    ),
    ActionTypeDef(
        api_name="CheckoutBookTyped",
        display_name="Checkout Book (typed)",
        target_type="Loan",
        executable_by_roles=["Librarian"],
        description="Typed-invocation twin of CheckoutBook (ActionContext test)",
        parameters=[
            ActionParameterDef(
                name="book_id",
                type="str",
                refers_to="Book",
                scope_semantics="target",
            ),
            ActionParameterDef(name="borrower", type="str"),
        ],
    ),
    ActionTypeDef(
        api_name="CheckoutBookTypedRefused",
        display_name="Checkout Book (typed, then authority-refused write)",
        target_type="Loan",
        executable_by_roles=["Librarian"],
        description=(
            "Typed handler that writes via ActionContext, then attempts "
            "a source-backed create the engine must refuse -- proves "
            "the ctx's earlier writes roll back with it."
        ),
        parameters=[
            ActionParameterDef(
                name="book_id",
                type="str",
                refers_to="Book",
                scope_semantics="target",
            ),
            ActionParameterDef(name="borrower", type="str"),
        ],
    ),
    ActionTypeDef(
        api_name="RegisterBookTyped",
        display_name="Register Book (typed)",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description="Typed-invocation twin of RegisterBook",
        parameters=[ActionParameterDef(name="fail", type="bool", required=False)],
    ),
    ActionTypeDef(
        api_name="AnnotateBook",
        display_name="Annotate Book",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description=(
            "Toy action with a `json`-typed parameter, used to exercise "
            "the params JSON round-trip refusal (AC12) with a value "
            "that passes declared-type validation but cannot survive "
            "(or doesn't survive unchanged) a JSON round-trip."
        ),
        parameters=[ActionParameterDef(name="note", type="json", required=False)],
    ),
    ActionTypeDef(
        api_name="ReturnNested",
        display_name="Return Nested",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description="Return a nested JSON-safe result.",
        parameters=[],
    ),
    ActionTypeDef(
        api_name="ReturnUnserializable",
        display_name="Return Unserializable",
        target_type="Book",
        executable_by_roles=["Librarian"],
        description="Return a value that cannot cross the JSON boundary.",
        parameters=[],
    ),
]

ACTION_POLICY_RULES = {
    "Library": [SelfScope(level="library")],
    "Shelf": [
        SelfScope(level="shelf"),
        ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
    ],
    "Book": [ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf")],
    "Loan": [ViaLink(link_api_name="loanedBook", direction="from", parent_type="Book")],
}


def _librarian(scope_id: str = "shelf-1") -> Consumer:
    return Consumer(
        actor_id="lib-1", role="Librarian", scope_level="shelf", scope_id=scope_id, kind="human"
    )


def _member() -> Consumer:
    return Consumer(
        actor_id="member-1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )


def _checkout_handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
    if params.get("book_id") is None:
        raise ActionError("book_id required", code="PRECONDITION_FAILED")
    return {"loan_id": "unused"}


def _make_checkout_handler(store: ObjectStore) -> Any:
    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        book = store.read_current("Book", params["book_id"])
        if book is None:
            raise ActionError(f"book {params['book_id']!r} does not exist", code="PRECONDITION_FAILED")
        with store.transaction():
            loan_id = store.insert(
                "Loan", {"borrower": params["borrower"]}, Source(source_system="action:CheckoutBook")
            )
            store.create_link("loanedBook", loan_id, params["book_id"])
        return {"loan_id": loan_id}

    return _handler


def _make_relocate_handler(store: ObjectStore) -> Any:
    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        book = store.read_current("Book", params["book_id"])
        if book is None:
            raise ActionError(f"book {params['book_id']!r} does not exist", code="PRECONDITION_FAILED")
        with store.transaction():
            store.create_link("onShelf", params["book_id"], params["shelf_id"])
        return {"book_id": params["book_id"]}

    return _handler


def _make_register_handler(store: ObjectStore) -> Any:
    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        with store.transaction():
            book_id = store.insert("Book", {"status": "new"}, Source(source_system="action:RegisterBook"))
            if params.get("fail"):
                raise ActionError("forced failure after side effect", code="PRECONDITION_FAILED")
        return {"book_id": book_id}

    return _handler


def _make_register_handler_unwrapped(store: ObjectStore) -> Any:
    """Deliberately does NOT wrap its writes in `store.transaction()` --
    proves the engine (`ActionExecutor.execute`), not the handler, owns
    transactionality (finding 4)."""

    def _handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        book_id = store.insert("Book", {"status": "new"}, Source(source_system="action:RegisterBook"))
        if params.get("fail"):
            raise ActionError("forced failure after side effect", code="PRECONDITION_FAILED")
        return {"book_id": book_id}

    return _handler


def _acknowledge_gap_handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
    """AC7 toy: a handler-side precondition failure with an author-supplied
    code, rather than the engine's default `PRECONDITION_FAILED`."""
    if not params.get("acknowledged"):
        raise ActionError("gap not acknowledged", code="GAP_NOT_ACKNOWLEDGED")
    return {"book_id": "book-1"}


def _annotate_book_handler(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
    """Never actually reached by the JSON round-trip tests below (the
    refusal happens before the handler runs); exists so the action has a
    registered handler at all."""
    return {"book_id": "book-1"}


def _return_nested_handler(
    consumer: Consumer, params: dict[str, Any]
) -> dict[str, Any]:
    return {"nested": {"a": [1, "b", None]}}


def _return_unserializable_handler(
    consumer: Consumer, params: dict[str, Any]
) -> dict[str, Any]:
    return {"nested": {"bad": {"not", "json"}}}


def _setup(
    registry: OntologyRegistry,
    store: ObjectStore,
    make_policy: PolicyFactory,
) -> ActionExecutor:
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )
    executor._register("CheckoutBook", _make_checkout_handler(store))
    executor._register("RelocateBook", _make_relocate_handler(store))
    executor._register("RegisterBook", _make_register_handler(store))
    executor._register(
        "RegisterBookUnwrapped", _make_register_handler_unwrapped(store)
    )
    executor._register("AcknowledgeGap", _acknowledge_gap_handler)
    executor._register("AnnotateBook", _annotate_book_handler)
    executor._register("ReturnNested", _return_nested_handler)
    executor._register("ReturnUnserializable", _return_unserializable_handler)
    return executor


# -- typed-invocation path (ActionContext) -----------------------------------


class CheckoutParams(BaseModel):
    """A locally-built plain params class -- deliberately not authored via
    any ontary authoring sugar (that's T2's job); this test only pins the
    executor's typed-invocation mechanics."""

    book_id: str
    borrower: str = Field(min_length=1)


class RegisterParams(BaseModel):
    fail: bool = False


received_checkout_params: list[CheckoutParams] = []


def _checkout_ctx_handler(ctx: ActionContext, params: CheckoutParams) -> dict[str, str]:
    received_checkout_params.append(params)
    book = ctx.read_current("Book", params.book_id)
    if book is None:
        raise ActionError(f"book {params.book_id!r} does not exist", code="PRECONDITION_FAILED")
    loan_id = ctx.insert("Loan", {"borrower": params.borrower})
    ctx.create_link("loanedBook", loan_id, params.book_id)
    return {"loan_id": loan_id}


def _checkout_ctx_handler_then_refused(
    ctx: ActionContext, params: CheckoutParams
) -> dict[str, str]:
    """Writes via `ctx` (Loan create + link), THEN attempts a source-backed
    create the engine must refuse (`Library` is not declared `owned=True`)
    -- proves the earlier ctx writes roll back with the refusal."""
    loan_id = ctx.insert("Loan", {"borrower": params.borrower})
    ctx.create_link("loanedBook", loan_id, params.book_id)
    ctx.insert("Library", {"id": "should-not-persist"})
    return {"loan_id": loan_id}  # pragma: no cover -- never reached


def _register_ctx_handler(ctx: ActionContext, params: RegisterParams) -> dict[str, str]:
    book_id = ctx.insert("Book", {"status": "new"})
    if params.fail:
        raise ActionError("forced failure after side effect", code="PRECONDITION_FAILED")
    return {"book_id": book_id}


def _setup_typed(
    registry: OntologyRegistry,
    store: ObjectStore,
    make_policy: PolicyFactory,
) -> ActionExecutor:
    executor = _setup(registry, store, make_policy)
    executor._register("CheckoutBookTyped", _checkout_ctx_handler, CheckoutParams)
    executor._register(
        "CheckoutBookTypedRefused", _checkout_ctx_handler_then_refused, CheckoutParams
    )
    executor._register("RegisterBookTyped", _register_ctx_handler, RegisterParams)
    return executor


def _seed_shelves(store: ObjectStore) -> None:
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-2"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.create_link("inLibrary", "shelf-2", "lib-1")


# -- permission -------------------------------------------------------------


def test_permission_denied_audited_and_raises(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "PERMISSION_DENIED") as exc_info:
        executor.execute(_member(), "CheckoutBook", {"book_id": "book-1", "borrower": "alice"})
    assert "Member" in str(exc_info.value)

    entries = store.audit_entries()
    assert len(entries) == 1
    assert entries[0].outcome == "denied"
    assert entries[0].action == "CheckoutBook"


# -- scope: target semantics --------------------------------------------------


def test_scope_denied_for_existing_target_outside_consumer_scope(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-2")  # outside shelf-1 consumer
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "SCOPE_DENIED") as exc_info:
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": "alice"},
        )
    assert "does not cover" in str(exc_info.value)

    entries = store.audit_entries()
    assert entries[-1].outcome == "denied"


def test_scope_covered_target_succeeds(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "CheckoutBook",
        {"book_id": "book-1", "borrower": "alice"},
    )
    assert "loan_id" in result

    entries = store.audit_entries()
    assert entries[-1].outcome == "ok"
    # target_id comes from the action's declared target parameter
    # (`book_id`, scope_semantics="target") -- not a guess at the handler's
    # return dict, which would have pointed at the new Loan id instead.
    assert entries[-1].target_id == "book-1"
    assert entries[-1].writes == [
        WriteRecord(op="create", object_type="Loan", object_id=result["loan_id"]),
        WriteRecord(op="link", link_type="loanedBook", from_id=result["loan_id"], to_id="book-1"),
    ]


def test_nonexistent_target_falls_through_to_handler_precondition_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="does not exist"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": "no-such-book", "borrower": "alice"},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"  # NOT "denied"


# -- scope: explicit-scope semantics ------------------------------------------


def test_explicit_scope_param_denied_when_named_scope_not_covered(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "SCOPE_DENIED") as exc_info:
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "RelocateBook",
            {"book_id": "book-1", "shelf_id": "shelf-2"},
        )
    assert "does not cover" in str(exc_info.value)

    entries = store.audit_entries()
    assert entries[-1].outcome == "denied"


def test_explicit_scope_param_covered_succeeds(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "RelocateBook",
        {"book_id": "book-1", "shelf_id": "shelf-1"},
    )
    assert result == {"book_id": "book-1"}
    assert store.audit_entries()[-1].outcome == "ok"


# -- parameter validation -----------------------------------------------------


def test_missing_required_parameter_rejected_and_audited_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="missing required parameter"):
        executor.execute(_librarian(), "CheckoutBook", {"book_id": "book-1"})

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"


def test_unknown_parameter_rejected_and_audited_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="unknown parameter"):
        executor.execute(
            _librarian(),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": "alice", "extra": "nope"},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"


def test_scope_param_as_int_rejected_before_handler_runs(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """A scope-bearing param whose value is an int (not the declared `str`
    type) must be rejected by parameter type validation before it ever
    reaches `_enforce_scope` -- previously this silently skipped scope
    enforcement (fail-open, spec §7) and the handler ran."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="expected type"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": 123, "borrower": "alice"},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    # handler never ran: no Loan object was created.
    assert store.read_current("Loan", "unused") is None


def test_scope_param_as_list_rejected_before_handler_runs(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """Same fail-open bug, via a list value instead of an int."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="expected type"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": ["book-1"], "borrower": "alice"},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"


def test_non_scope_param_type_mismatch_rejected_and_audited_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """A plain declared param (no scope_semantics) with the wrong type is
    still rejected: `_validate_params` enforces every declared param's
    type, not only scope-bearing ones."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="expected type"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": 42},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"


def test_valid_declared_types_still_pass(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """Sanity check: correctly typed params for an action with mixed
    required/optional/bool params still succeed end to end."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "RegisterBook",
        {"fail": False},
    )
    assert "book_id" in result
    assert store.audit_entries()[-1].outcome == "ok"


# -- handler exception rollback -----------------------------------------------


def test_handler_exception_rolls_back_side_effects_and_audits_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="forced failure"):
        executor.execute(_librarian(), "RegisterBook", {"fail": True})

    assert store.read_all("Book") == []  # side effect rolled back

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].target_id is None


def test_handler_success_writes_and_audits_ok(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    result = executor.execute(_librarian(), "RegisterBook", {"fail": False})
    assert store.read_current("Book", result["book_id"]) is not None

    entries = store.audit_entries()
    assert entries[-1].outcome == "ok"
    # RegisterBook declares no target/scope parameter referring to Book (or
    # matching its own target_type) -- target_id falls back to None rather
    # than guessing from the handler's return dict (AC11).
    assert entries[-1].target_id is None
    assert entries[-1].writes == [
        WriteRecord(op="create", object_type="Book", object_id=result["book_id"])
    ]


def test_nested_json_handler_return_round_trips_to_caller(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = _setup(registry, store, make_policy)
    expected = {"nested": {"a": [1, "b", None]}}

    result = executor.execute(_librarian(), "ReturnNested", {})

    assert result == expected


def test_non_json_handler_return_is_internal_error_and_audited(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(InternalError) as exc_info:
        executor.execute(_librarian(), "ReturnUnserializable", {})

    assert exc_info.value.code == "INTERNAL_ERROR"
    entry = store.audit_entries()[-1]
    assert entry.outcome == "error"


@pytest.mark.parametrize(
    "non_finite",
    [
        pytest.param(float("inf"), id="positive-infinity"),
        pytest.param(float("-inf"), id="negative-infinity"),
        pytest.param(float("nan"), id="nan"),
    ],
)
def test_non_finite_handler_result_is_refused_as_internal_error(
    non_finite: float,
) -> None:
    with pytest.raises(InternalError) as exc_info:
        ActionExecutor._validate_result_json_safe(
            "ReturnNonFinite", {"value": non_finite}
        )

    assert exc_info.value.code == "INTERNAL_ERROR"
    assert exc_info.value.kind == "internal"
    assert str(exc_info.value) == (
        "'ReturnNonFinite': handler result is not JSON-serializable"
    )


def test_engine_owns_transactionality_for_handlers_that_dont_wrap_themselves(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """Finding 4 (AC2/§7): the engine, not the handler, owns
    transactionality. `RegisterBookUnwrapped`'s handler inserts a Book and
    THEN raises, without ever calling `store.transaction()` itself --
    `ActionExecutor.execute` must still roll back that insert (by wrapping
    the whole handler call in its own transaction) and the error audit
    entry must still survive the rollback.
    """
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="forced failure"):
        executor.execute(_librarian(), "RegisterBookUnwrapped", {"fail": True})

    # The Book insert happened before the raise, with no transaction of the
    # handler's own -- if the engine didn't wrap the call, this row would
    # survive. It must not.
    assert store.read_all("Book") == []

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].target_id is None


def test_engine_owned_transaction_nests_with_handlers_that_wrap_themselves(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """`ObjectStore.transaction()` is reentrant: a handler that DOES wrap
    its own writes (e.g. `RegisterBook`) still works unchanged now that the
    engine also wraps the call -- nesting doesn't double-commit or
    early-commit the inner transaction."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    result = executor.execute(_librarian(), "RegisterBook", {"fail": False})
    assert store.read_current("Book", result["book_id"]) is not None

    with pytest.raises(ActionError, match="forced failure"):
        executor.execute(_librarian(), "RegisterBook", {"fail": True})

    # Only the successful call's Book row should exist; the failed call's
    # insert (wrapped by both the handler's own transaction() and the
    # engine's) must be fully rolled back.
    books = store.read_all("Book")
    assert [b.payload["id"] for b in books] == [result["book_id"]]


# -- registration errors ------------------------------------------------------


def test_register_handler_for_unregistered_action_raises(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError, match="unregistered"):
        executor._register("NoSuchAction", _checkout_handler)


def test_double_register_handler_raises(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )
    executor._register("CheckoutBook", _checkout_handler)

    with pytest.raises(ActionError, match="already registered"):
        executor._register("CheckoutBook", _checkout_handler)


def test_handler_decorator_registers(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC7 removed the `.handler(...)` decorator sugar -- `_register` is
    the only entry point now, but the pipeline exercise this test pins
    (legacy `(consumer, dict)` handler through the FULL `execute()`
    pipeline) is otherwise unchanged."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    def _handle(consumer: Consumer, params: dict[str, Any]) -> dict[str, str]:
        return {"loan_id": "x"}

    executor._register("CheckoutBook", _handle)

    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "CheckoutBook",
        {"book_id": "book-1", "borrower": "alice"},
    )
    assert result == {"loan_id": "x"}


def test_execute_unregistered_action_raises(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError, match="unregistered action"):
        executor.execute(_librarian(), "NoSuchAction", {})


def test_execute_action_without_handler_raises(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError, match="no handler registered"):
        executor.execute(_librarian(), "CheckoutBook", {"book_id": "x", "borrower": "y"})


# -- registry.validate() rejections for bad ActionParameterDef ---------------


def test_registry_validate_rejects_dangling_refers_to(make_registry: RegistryFactory) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    registry.register_action_type(
        ActionTypeDef(
            api_name="BadAction",
            display_name="Bad Action",
            target_type="Book",
            executable_by_roles=["Librarian"],
            description="has a dangling refers_to",
            parameters=[
                ActionParameterDef(
                    name="x", type="str", refers_to="NoSuchType", scope_semantics="target"
                )
            ],
        )
    )
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "NoSuchType" in str(exc_info.value)


def test_registry_validate_rejects_scope_semantics_without_refers_to(
    make_registry: RegistryFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    registry.register_action_type(
        ActionTypeDef(
            api_name="BadAction2",
            display_name="Bad Action 2",
            target_type="Book",
            executable_by_roles=["Librarian"],
            description="scope_semantics without refers_to",
            parameters=[
                ActionParameterDef(name="x", type="str", scope_semantics="target")
            ],
        )
    )
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.validate()
    assert "scope_semantics" in str(exc_info.value)


def test_registry_validate_passes_for_well_formed_actions(make_registry: RegistryFactory) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    registry.validate()  # no raise


# -- error codes (AC6/AC7) ----------------------------------------------------


def test_permission_denied_carries_permission_denied_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC6: a role denial is distinguishable from a scope denial without
    reading the message text."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "PERMISSION_DENIED") as exc_info:
        executor.execute(_member(), "CheckoutBook", {"book_id": "book-1", "borrower": "alice"})
    assert exc_info.value.kind == "permission"


def test_scope_denied_carries_scope_denied_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-2")  # outside shelf-1 consumer
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "SCOPE_DENIED") as exc_info:
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": "alice"},
        )
    assert exc_info.value.kind == "permission"


def test_invalid_params_carries_invalid_params_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "CheckoutBook", {"book_id": "book-1"})
    assert exc_info.value.code == "INVALID_PARAMS"
    assert exc_info.value.kind == "validation"


def test_unregistered_action_carries_unknown_action_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "NoSuchAction", {})
    assert exc_info.value.code == "UNKNOWN_ACTION"
    assert exc_info.value.kind == "validation"


def test_action_without_handler_carries_unknown_action_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "CheckoutBook", {"book_id": "x", "borrower": "y"})
    assert exc_info.value.code == "UNKNOWN_ACTION"
    assert exc_info.value.kind == "validation"


def test_default_precondition_failure_carries_precondition_failed_code(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """A handler-raised `ActionError` with no explicit `code=` gets the
    engine default `PRECONDITION_FAILED` (kind precondition) -- AC7's
    "absent one" branch."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "RegisterBook", {"fail": True})
    assert exc_info.value.code == "PRECONDITION_FAILED"
    assert exc_info.value.kind == "precondition"


def test_author_supplied_precondition_code_round_trips_through_executor(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC7: an author-supplied code on an `ActionError` raised inside a
    handler survives `ActionExecutor.execute`'s pipeline unchanged --
    round-trips all the way back to the caller."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "AcknowledgeGap", {"acknowledged": False})
    assert exc_info.value.code == "GAP_NOT_ACKNOWLEDGED"
    assert exc_info.value.kind == "precondition"

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"

    # Absent an author-supplied code, the same action falls back to the
    # engine default.
    result = executor.execute(_librarian(), "AcknowledgeGap", {"acknowledged": True})
    assert result == {"book_id": "book-1"}


# -- transaction ownership (AC9) ----------------------------------------------


def test_execute_inside_caller_transaction_refused_and_not_audited(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC9: wrapping `execute()` in the caller's own `store.transaction()`
    is refused with `CALLER_TRANSACTION_REFUSED` -- and, per spec §5, this
    refusal happens BEFORE any audit write (an audit row inside the
    caller's transaction could itself be rolled back with it), so no audit
    entry is written at all."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED") as exc_info:
        with store.transaction():
            executor.execute(_librarian(), "RegisterBook", {"fail": False})
    assert exc_info.value.kind == "conflict"

    assert store.audit_entries() == []


def test_execute_outside_any_transaction_still_works(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """Sanity check: the refusal is specific to a caller-opened
    transaction, not to `store.in_transaction` never being true (the engine
    opens its own internally)."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    result = executor.execute(_librarian(), "RegisterBook", {"fail": False})
    assert "book_id" in result
    assert store.audit_entries()[-1].outcome == "ok"


# -- audit integrity: writes + never-fail audit (AC11/AC12) -------------------


def test_denied_attempt_audit_entry_has_empty_writes(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "PERMISSION_DENIED"):
        executor.execute(_member(), "CheckoutBook", {"book_id": "book-1", "borrower": "alice"})

    assert store.audit_entries()[-1].writes == []


def test_error_attempt_audit_entry_has_empty_writes(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError, match="forced failure"):
        executor.execute(_librarian(), "RegisterBook", {"fail": True})

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].writes == []


def test_params_not_json_serializable_refused_invalid_params_and_audited(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC12: a param value that passes declared-type validation (a `json`
    param accepts a `dict`) but cannot survive a JSON round trip (a nested
    `set` fails `json.dumps` outright) is refused up front as
    `INVALID_PARAMS`, and the refusal itself is still audited -- with a
    placeholder for the unencodable value, never a crash."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    executor = _setup(registry, store, make_policy)

    bad_params: dict[str, Any] = {"note": {"tags": {"a", "b"}}}
    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "AnnotateBook", bad_params)
    assert exc_info.value.code == "INVALID_PARAMS"
    assert exc_info.value.kind == "validation"

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].writes == []
    # The unserializable value is a placeholder, not lost/raised.
    assert "$unserializable" in entries[-1].params["note"]


def test_params_json_round_trip_mismatch_refused_invalid_params(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC12: a `dict` param with a non-string key survives `_validate_params`
    (it's still a `dict`) but doesn't round-trip through JSON unchanged
    (JSON coerces the key to a string) -- refused as `INVALID_PARAMS`."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    executor = _setup(registry, store, make_policy)

    with pytest.raises(ActionError) as exc_info:
        executor.execute(_librarian(), "AnnotateBook", {"note": {1: "a"}})
    assert exc_info.value.code == "INVALID_PARAMS"

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"


def test_applied_action_audit_entry_lists_real_write_records(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC11: the applied-audit entry's `writes` are the real (op, type, id)
    set the handler made, not a guess."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup(registry, store, make_policy)

    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "CheckoutBook",
        {"book_id": "book-1", "borrower": "alice"},
    )

    entries = store.audit_entries()
    assert entries[-1].outcome == "ok"
    assert entries[-1].target_id == "book-1"
    assert entries[-1].writes == [
        WriteRecord(op="create", object_type="Loan", object_id=result["loan_id"]),
        WriteRecord(
            op="link",
            link_type="loanedBook",
            from_id=result["loan_id"],
            to_id="book-1",
        ),
    ]


# -- typed-invocation path (ActionContext) -----------------------------------


def test_action_context_writes_are_source_stamped(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """ctx.insert/create_link auto-stamp the action's own `Source`
    (`action:<api_name>`) -- the handler never supplies one."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)

    result = executor.execute(
        _librarian(scope_id="shelf-1"),
        "CheckoutBookTyped",
        {"book_id": "book-1", "borrower": "alice"},
    )

    loan = store.read_current("Loan", result["loan_id"])
    assert loan is not None
    assert loan.lineage.source_system == "action:CheckoutBookTyped"


def test_action_context_refusal_mid_handler_rolls_back_and_audits_error(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """AC (edge case §8): an authority refusal partway through a ctx-based
    handler's writes rolls back everything the handler already wrote via
    `ctx` -- same rollback guarantee as the legacy store-closure handlers,
    and an "error" audit row is still written."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)

    with pytest.raises(Exception):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBookTypedRefused",
            {"book_id": "book-1", "borrower": "alice"},
        )

    assert store.read_all("Loan") == []  # ctx writes rolled back

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].action == "CheckoutBookTypedRefused"


def test_typed_handler_receives_params_instance_via_dict_execute(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """String/dict `execute` builds the params instance via
    `params_cls.model_validate` and hands it to the handler unchanged."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)
    received_checkout_params.clear()

    executor.execute(
        _librarian(scope_id="shelf-1"),
        "CheckoutBookTyped",
        {"book_id": "book-1", "borrower": "alice"},
    )

    assert len(received_checkout_params) == 1
    assert isinstance(received_checkout_params[0], CheckoutParams)
    assert received_checkout_params[0].book_id == "book-1"
    assert received_checkout_params[0].borrower == "alice"


def test_typed_handler_receives_same_instance_via_typed_execute(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """An already-constructed params instance reaches the same handler as
    the string/dict form, and is used directly (not rebuilt)."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)
    received_checkout_params.clear()

    params = CheckoutParams(book_id="book-1", borrower="alice")
    executor.execute(_librarian(scope_id="shelf-1"), "CheckoutBookTyped", params)

    assert len(received_checkout_params) == 1
    assert received_checkout_params[0] is params


def test_typed_execution_refuses_missing_coerced_params(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)
    monkeypatch.setattr(executor, "_coerce_typed_params", lambda *args: None)

    with raises_code(InternalError, "INTERNAL_ERROR"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBookTyped",
            CheckoutParams(book_id="book-1", borrower="alice"),
        )


def test_scope_enforcement_refuses_missing_refers_to(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = _setup(registry, store, make_policy)
    action_def = registry.get_action_type("RelocateBook")
    malformed_scope_param = action_def.parameters[1].model_copy(
        update={"refers_to": None}
    )
    malformed_action = action_def.model_copy(
        update={"parameters": [action_def.parameters[0], malformed_scope_param]}
    )

    with raises_code(InternalError, "INTERNAL_ERROR"):
        executor._enforce_scope(
            _librarian(),
            "RelocateBook",
            malformed_action,
            {"shelf_id": "shelf-1"},
            None,
        )


def test_model_validate_failure_is_invalid_params_audited_like_shape_failure(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """A value that passes declared-shape validation (`borrower` is a
    `str`) but fails the params class's own Pydantic validation (`min_
    length=1`) is refused as `INVALID_PARAMS`, audited "error" -- identical
    outcome/ordering to today's declared-shape validation failures, and the
    handler never runs (`received_checkout_params` stays empty)."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    executor = _setup_typed(registry, store, make_policy)
    received_checkout_params.clear()

    with pytest.raises(ActionError) as exc_info:
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBookTyped",
            {"book_id": "book-1", "borrower": ""},
        )
    assert exc_info.value.code == "INVALID_PARAMS"

    entries = store.audit_entries()
    assert entries[-1].outcome == "error"
    assert entries[-1].action == "CheckoutBookTyped"
    assert received_checkout_params == []


def test_action_context_exposes_no_store_handle(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    """Handlers cannot reach a store handle (public or private-by-
    convention) through `ActionContext` -- only the declared write/read
    surface exists.

    `capability` and `emit` are on this list as of M5 (spec `governed-effects`
    AC6/AC9). Neither weakens the guard. `capability` hands back an
    author-supplied provider object, never the store and never the guarded
    query layer; `emit` only appends a frozen payload snapshot to a per-call
    list and performs no I/O at all. What this test still forbids is the ENGINE
    exposing a store handle -- an author who binds a provider that happens to
    hold a store is trusted code at the same tier as the handler itself (spec
    §8 R1), a documented limitation no attribute-set assertion can police.

    Keep this assertion EXACT, and add to it deliberately. It is the only thing
    standing between a future refactor and a handler that can reach the raw
    store. In particular, do not make it pass by overriding `__dir__` to hide a
    new method: that blinds the guard to EVERY later addition, including the
    one it exists to catch. (T7's delegated implementation did exactly that,
    because its file allowlist forbade editing this test; the override was
    removed and this list widened instead.)
    """
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    ctx = ActionContext(store, Source(source_system="action:Test"), _librarian())

    public_attrs = {name for name in dir(ctx) if not name.startswith("_")}
    assert public_attrs == {
        "capability",
        "consumer",
        "create_link",
        "emit",
        "insert",
        "links_from",
        "links_to",
        "read_all",
        "read_current",
        "update",
    }
    assert not hasattr(ctx, "store")

    # And the guard's real target, stated directly: no attribute on the context
    # -- public or private-by-convention -- may BE the store, other than the one
    # private slot the engine itself uses.
    store_valued = {
        name
        for name in dir(ctx)
        if not name.startswith("__") and getattr(ctx, name, None) is store
    }
    assert store_valued == {"_store"}


def test_action_context_read_all_delegates_to_the_trusted_raw_store_read(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    """Handlers can enumerate raw current rows without reaching ``ctx._store``.

    This is deliberately the same trusted tier as ``read_current``: action
    handlers are ontology author code, and an id allocator must see every row
    rather than a scope- or sensitivity-filtered subset that could reuse an id.
    """
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    store.insert("Book", {"id": "book-1", "title": "First"}, SRC)
    store.insert("Book", {"id": "book-2", "title": "Second"}, SRC)
    ctx = ActionContext(store, Source(source_system="action:Test"), _librarian())

    assert ctx.read_all("Book") == store.read_all("Book")


def test_register_private_duplicate_and_unregistered_checks_unchanged(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """`_register` (the sole registration entry point, AC7) keeps the same
    declared-action and duplicate-registration checks for both the legacy
    dict-shaped path and the typed path."""
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    executor = ActionExecutor(
        store,
        registry,
        make_policy(
            levels=LEVELS,
            unscoped_types=set(),
            rules=ACTION_POLICY_RULES,
            contributor_rules={},
            row_visibility={},
            min_n=3,
        ),
    )

    with pytest.raises(ActionError) as exc_info:
        executor._register("NoSuchAction", _checkout_ctx_handler, CheckoutParams)
    assert exc_info.value.code == "UNKNOWN_ACTION"

    executor._register("CheckoutBookTyped", _checkout_ctx_handler, CheckoutParams)
    with pytest.raises(ActionError, match="already registered"):
        executor._register("CheckoutBookTyped", _checkout_ctx_handler, CheckoutParams)
