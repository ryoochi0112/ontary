"""Unit tests for `ontary.actions.ActionExecutor` on a toy ontology.

Toy "library" domain (Library/Shelf/Book/Loan) -- deliberately not DSO --
exercising the generic action pipeline: registration, role permission,
declared-parameter scope enforcement (`target` and `scope` semantics),
parameter validation, precondition/handler-exception rollback, and audit
completeness (denied/error/ok). Reuses the ScopePolicy shape from
`test_scope.py`.
"""

from __future__ import annotations

import ast
import hashlib
import itertools
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, NamedTuple, get_args

import pytest
from conftest import raises_code
from pydantic import BaseModel, Field

import ontary.store.sqlite
from ontary import ActionParams, Ontology, OntologyClient, OntologyObject, prop, target
from ontary.actions import ActionContext, ActionError, ActionExecutor
from ontary.errors import (
    AuthorityError,
    ConflictError,
    InternalError,
    PermissionDenied,
    ValidationFailed,
)
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    ScopePolicy,
    ScopeRule,
    SelfScope,
    ViaLink,
)
from ontary.security import Consumer
from ontary.store import (
    ObjectStore,
    Source,
    Store,
    WriteRecord,
)
from ontary.store.inmemory import InMemoryStore

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


def _lifecycle_client(
    *, badge_holder_owned: bool = True
) -> tuple[OntologyClient, ObjectStore]:
    """A declared business-verb runtime for lifecycle integration tests."""
    ontology = Ontology(
        name="lifecycle-actions", scope_levels=["employee"], min_n=1
    )

    @ontology.object(
        layer="L0", scope=[SelfScope(level="employee")], owned=True
    )
    class Employee(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Department(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Badge(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link(
        "employeeDepartment",
        Employee,
        Department,
        Cardinality.MANY_TO_MANY,
        owned=True,
    )
    ontology.link(
        "badgeHolder",
        Badge,
        Employee,
        Cardinality.MANY_TO_MANY,
        owned=badge_holder_owned,
    )
    ontology.link(
        "employeePeer",
        Employee,
        Employee,
        Cardinality.MANY_TO_MANY,
        owned=True,
    )

    class AssignEmployeeToDepartmentParams(ActionParams):
        employee_id: str = target(Employee)
        department_id: str

    @ontology.action(
        AssignEmployeeToDepartmentParams,
        target=Employee,
        roles=["Operator"],
        api_name="AssignEmployeeToDepartment",
    )
    def assign_employee_to_department(
        ctx: ActionContext, params: AssignEmployeeToDepartmentParams
    ) -> dict[str, str]:
        ctx.create_link(
            "employeeDepartment", params.employee_id, params.department_id
        )
        return {
            "employee_id": params.employee_id,
            "department_id": params.department_id,
        }

    class OffboardEmployeeParams(ActionParams):
        employee_id: str = target(Employee)

    @ontology.action(
        OffboardEmployeeParams,
        target=Employee,
        roles=["Operator"],
        api_name="OffboardEmployee",
    )
    def offboard_employee(
        ctx: ActionContext, params: OffboardEmployeeParams
    ) -> dict[str, str]:
        ctx.retire("Employee", params.employee_id)
        return {"employee_id": params.employee_id}

    class UnassignEmployeeParams(ActionParams):
        employee_id: str = target(Employee)
        department_id: str

    @ontology.action(
        UnassignEmployeeParams,
        target=Employee,
        roles=["Operator"],
        api_name="UnassignEmployee",
    )
    def unassign_employee(
        ctx: ActionContext, params: UnassignEmployeeParams
    ) -> dict[str, str]:
        ctx.unlink(
            "employeeDepartment", params.employee_id, params.department_id
        )
        return {"employee_id": params.employee_id}

    ontology.validate()
    store = ObjectStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="employee",
            scope_id="employee-1",
            kind="human",
        ),
    )
    return client, store


def _vialink_lifecycle_client() -> tuple[OntologyClient, ObjectStore]:
    """The `ViaLink` twin of `_lifecycle_client`, for the retire path.

    `_lifecycle_client`'s `Employee` is `SelfScope`, which reads its scope id
    off the object itself, so `ActionContext.retire`'s link cascade (AC3)
    cannot cost it its scope. `DirectProperty` survives the cascade for the
    same reason -- it reads the object's own row. A `ViaLink`-scoped object
    has only the link to climb, and the cascade closes it -- so this fixture,
    and not that one, can construct "retired through the sanctioned verb, and
    still owns a scope reachable only through a now-closed link".
    """
    ontology = Ontology(name="lifecycle-vialink", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="onTeam", direction="from", parent_type="Team")],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    store = ObjectStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="team",
            scope_id="team-1",
            kind="human",
        ),
    )
    return client, store


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


def test_scope_enforced_for_a_retired_target_outside_consumer_scope(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """Retirement must not turn a scoped object into an unscoped one.

    The target gate skipped every object `read_current` could not return, a
    set that used to mean "never existed". Retirement (this branch) added a
    second member: an object that exists, owns a scope, and carries history
    -- so a consumer outside its scope reached the handler with no scope
    check at all. The sibling above pins the LIVE case and cannot construct
    this one: it never retires the book.
    """
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    _seed_shelves(store)
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-2")  # outside shelf-1 consumer
    store.retire_object("Book", "book-1")
    executor = _setup(registry, store, make_policy)

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        executor.execute(
            _librarian(scope_id="shelf-1"),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": "alice"},
        )

    entries = store.audit_entries()
    assert entries[-1].outcome == "denied"

    # The other half, and the reason this is a GATE and not a blanket
    # refusal: the consumer the retired book still belongs to is not denied,
    # it reaches the handler's own precondition check. Without this, closing
    # the bypass by denying every retired target would pass the assertion
    # above while making `OBJECT_ALREADY_RETIRED` unreachable.
    with pytest.raises(ActionError, match="does not exist"):
        executor.execute(
            _librarian(scope_id="shelf-2"),
            "CheckoutBook",
            {"book_id": "book-1", "borrower": "alice"},
        )
    assert store.audit_entries()[-1].outcome == "error"  # NOT "denied"


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


def test_explicit_scope_param_naming_a_retired_scope_object_is_denied(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
    make_policy: PolicyFactory,
) -> None:
    """`scope` semantics stays on the LIVE read; only `target` may resolve a
    retired object's scope.

    `_enforce_scope` asks `include_retired=param.scope_semantics == "target"`.
    That containment is the whole reason the target gate is safe -- and it
    fails OPEN if it breaks: hard-code the flag to `True` and a consumer may
    relocate a book ONTO A RETIRED SHELF, because the retired shelf still
    resolves the library that covers them. The write commits and audits "ok".
    Nothing else in the suite reds on that mutation, so this is the pin.

    A retired shelf is not a place a book can go. `read_current` answers
    `None` for it, the library never resolves, and deny-by-default denies.
    """
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

    # Retired shelf, still linked to the library the consumer is scoped to --
    # so ONLY the live-read containment stands between them and the write.
    store.retire_object("Shelf", "shelf-2")

    library_consumer = Consumer(
        actor_id="lib-admin",
        role="Librarian",
        scope_level="library",
        scope_id="lib-1",
        kind="human",
    )

    with raises_code(PermissionDenied, "SCOPE_DENIED") as exc_info:
        executor.execute(
            library_consumer,
            "RelocateBook",
            {"book_id": "book-1", "shelf_id": "shelf-2"},
        )
    assert "does not cover" in str(exc_info.value)
    assert store.audit_entries()[-1].outcome == "denied"

    # The same consumer on the LIVE sibling shelf succeeds, so the denial
    # above is the retirement and not the consumer's scope.
    assert executor.execute(
        library_consumer,
        "RelocateBook",
        {"book_id": "book-1", "shelf_id": "shelf-1"},
    ) == {"book_id": "book-1"}


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


def test_retire_business_verb_cascades_both_link_directions_and_audits_writes(
) -> None:
    """Removing any cascade branch or write capture must fail this pin."""
    client, store = _lifecycle_client()
    for employee_id in ("employee-1", "employee-2", "employee-3"):
        store.insert("Employee", {"id": employee_id}, SRC)
    store.insert("Department", {"id": "department-1"}, SRC)
    store.insert("Badge", {"id": "badge-1"}, SRC)
    store.create_link("employeeDepartment", "employee-1", "department-1")
    store.create_link("employeeDepartment", "employee-2", "department-1")
    store.create_link("badgeHolder", "badge-1", "employee-1")
    store.create_link("employeePeer", "employee-1", "employee-2")
    store.create_link("employeePeer", "employee-3", "employee-1")

    result = client.execute(
        "OffboardEmployee", {"employee_id": "employee-1"}
    )

    assert result == {"employee_id": "employee-1"}
    assert store.read_current("Employee", "employee-1") is None
    assert store.links_from("employeeDepartment", "employee-1") == []
    assert store.links_to("badgeHolder", "employee-1") == []
    assert store.links_from("employeePeer", "employee-1") == []
    assert store.links_to("employeePeer", "employee-1") == []
    assert store.links_from("employeeDepartment", "employee-2") == [
        "department-1"
    ]

    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert [write.model_dump() for write in entry.writes] == [
        {
            "op": "retire",
            "object_type": "Employee",
            "link_type": None,
            "object_id": "employee-1",
            "from_id": None,
            "to_id": None,
        },
        {
            "op": "unlink",
            "object_type": None,
            "link_type": "employeeDepartment",
            "object_id": None,
            "from_id": "employee-1",
            "to_id": "department-1",
        },
        {
            "op": "unlink",
            "object_type": None,
            "link_type": "badgeHolder",
            "object_id": None,
            "from_id": "badge-1",
            "to_id": "employee-1",
        },
        {
            "op": "unlink",
            "object_type": None,
            "link_type": "employeePeer",
            "object_id": None,
            "from_id": "employee-1",
            "to_id": "employee-2",
        },
        {
            "op": "unlink",
            "object_type": None,
            "link_type": "employeePeer",
            "object_id": None,
            "from_id": "employee-3",
            "to_id": "employee-1",
        },
    ]


def test_retire_deduplicates_duplicate_links_and_audits_success() -> None:
    """Two ordinary assign calls can create duplicate live links.

    The old fixture could not construct this input through a consumer action;
    the duplicate state is deliberately built through two calls to the
    AssignEmployeeToDepartment business verb instead of direct link insertion.
    """
    client, store = _lifecycle_client()
    store.insert("Employee", {"id": "employee-1"}, SRC)
    store.insert("Department", {"id": "department-1"}, SRC)

    for _ in range(2):
        assert client.execute(
            "AssignEmployeeToDepartment",
            {"employee_id": "employee-1", "department_id": "department-1"},
        ) == {
            "employee_id": "employee-1",
            "department_id": "department-1",
        }
    assert store.links_from("employeeDepartment", "employee-1") == [
        "department-1",
        "department-1",
    ]

    result = client.execute("OffboardEmployee", {"employee_id": "employee-1"})

    assert result == {"employee_id": "employee-1"}
    assert store.read_current("Employee", "employee-1") is None
    assert store.links_from("employeeDepartment", "employee-1") == []

    entry = store.audit_entries()[-1]
    assert entry.action == "OffboardEmployee"
    assert entry.outcome == "ok"
    assert [(write.op, write.link_type, write.object_type, write.object_id, write.from_id, write.to_id) for write in entry.writes] == [
        ("retire", None, "Employee", "employee-1", None, None),
        (
            "unlink",
            "employeeDepartment",
            None,
            None,
            "employee-1",
            "department-1",
        ),
    ]


def test_unlink_business_verb_closes_link_and_audits_write() -> None:
    """Calling the wrong store verb or omitting capture must fail this pin."""
    client, store = _lifecycle_client()
    store.insert("Employee", {"id": "employee-1"}, SRC)
    store.insert("Department", {"id": "department-1"}, SRC)
    store.create_link("employeeDepartment", "employee-1", "department-1")

    client.execute(
        "UnassignEmployee",
        {"employee_id": "employee-1", "department_id": "department-1"},
    )

    assert store.links_from("employeeDepartment", "employee-1") == []
    entry = store.audit_entries()[-1]
    assert entry.outcome == "ok"
    assert [(write.op, write.link_type, write.from_id, write.to_id) for write in entry.writes] == [
        ("unlink", "employeeDepartment", "employee-1", "department-1")
    ]


def test_retire_source_backed_link_refusal_rolls_back_whole_cascade() -> None:
    """A late authority refusal must restore the object and earlier link close."""
    client, store = _lifecycle_client(badge_holder_owned=False)
    store.insert("Employee", {"id": "employee-1"}, SRC)
    store.insert("Department", {"id": "department-1"}, SRC)
    store.insert("Badge", {"id": "badge-1"}, SRC)
    store.create_link("employeeDepartment", "employee-1", "department-1")
    store.create_link("badgeHolder", "badge-1", "employee-1")

    with raises_code(AuthorityError, "UNDECLARED_SOURCE_REMOVAL"):
        client.execute("OffboardEmployee", {"employee_id": "employee-1"})

    assert store.read_current("Employee", "employee-1") is not None
    assert store.links_from("employeeDepartment", "employee-1") == [
        "department-1"
    ]
    assert store.links_to("badgeHolder", "employee-1") == ["badge-1"]
    entry = store.audit_entries()[-1]
    assert entry.action == "OffboardEmployee"
    assert entry.outcome == "denied"
    assert entry.writes == []


@pytest.mark.parametrize(
    ("employee_id", "first_retire", "error_type", "code"),
    [
        ("missing", False, ValidationFailed, "OBJECT_RETIRE_NOT_FOUND"),
        ("employee-1", True, ConflictError, "OBJECT_ALREADY_RETIRED"),
    ],
)
def test_retire_refusals_are_coded_and_audited_as_denied(
    employee_id: str,
    first_retire: bool,
    error_type: type[Exception],
    code: str,
) -> None:
    """Target-scope absence must not turn retire into an unaudited skip."""
    client, store = _lifecycle_client()
    if first_retire:
        store.insert("Employee", {"id": employee_id}, SRC)
        client.execute("OffboardEmployee", {"employee_id": employee_id})

    with raises_code(error_type, code):
        client.execute("OffboardEmployee", {"employee_id": employee_id})

    entry = store.audit_entries()[-1]
    assert entry.action == "OffboardEmployee"
    assert entry.outcome == "denied"
    assert entry.writes == []


def test_retired_vialink_target_still_reaches_its_own_scopes_precondition() -> None:
    """AC5's refusal must survive the cascade, not just `SelfScope`.

    The sibling above pins this for `SelfScope`, and CANNOT construct this
    case (L18): `Employee` reads its scope id off its own row, so the AC3
    link cascade cannot cost it its scope. `Worker` is `ViaLink`-scoped, and
    `ActionContext.retire` closes the only link that resolves it -- so the
    target gate must read the object's scope as of its retirement, or the
    operator who owns the object is denied instead of refused, and
    `OBJECT_ALREADY_RETIRED` becomes unreachable through the action path.

    Retired through `ctx.retire`, deliberately, NOT `store.retire_object`:
    the store verb does not cascade, so it leaves the link live and cannot
    reproduce this at all.
    """
    client, store = _vialink_lifecycle_client()
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("onTeam", "worker-1", "team-1")

    client.execute("OffboardWorker", {"worker_id": "worker-1"})
    assert store.links_from("onTeam", "worker-1") == []  # AC3 cascade ran

    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        client.execute("OffboardWorker", {"worker_id": "worker-1"})

    entry = store.audit_entries()[-1]
    assert entry.action == "OffboardWorker"
    assert entry.outcome == "denied"
    assert entry.writes == []


def _scoped_lifecycle_client(
    rule: Any,
    *,
    scope_id: str = "team-1",
    linked: bool = False,
    make_store: Callable[[OntologyRegistry], Store] | None = None,
) -> tuple[OntologyClient, Any]:
    """`_vialink_lifecycle_client`, with `Worker`'s scope rule swapped in.

    The pins below need every rule shape on one retire path, so the rule is
    the only variable between them. Two knobs follow from the rule and are
    not free choices: a `SelfScope`-scoped `Worker` resolves to its OWN id,
    so its operator must be scoped there rather than to the team, and only a
    `ViaLink` rule needs the `onTeam` link type declared.

    `make_store` lets the lifecycle matrix drive the SAME fixture across
    every backend. It defaults to the SQLite store every caller above
    already used, so those callers are unchanged.
    """
    ontology = Ontology(name="lifecycle-scoped", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(layer="L0", scope=[rule], owned=True)
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)
        team: str

    if linked:
        ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    build = make_store or ObjectStore
    store = build(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id="operator-1",
            role="Operator",
            scope_level="team",
            scope_id=scope_id,
            kind="human",
        ),
    )
    return client, store


def _resolver_reading_the_live_row(
    store: Store, obj_type: str, obj_id: str
) -> str | None:
    """The natural `CustomResolver` body: read the row, take the scope key.

    Deliberately written the obvious way -- `read_current`, the only object
    read the `Store` surface advertises to author code -- because that is the
    body whose retirement behavior the pin below documents.
    """
    row = store.read_current(obj_type, obj_id)
    return None if row is None else str(row.payload["team"])


def _two_team_vialink_client() -> tuple[Ontology, ObjectStore]:
    """`_vialink_lifecycle_client`'s ontology, returned unbound.

    The pin below needs TWO operators in different scopes reading one store,
    which that fixture cannot give: it builds its client with a single
    hard-coded consumer.
    """
    ontology = Ontology(name="lifecycle-two-team", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="onTeam", direction="from", parent_type="Team")],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


def _two_team_vialink_to_client() -> tuple[Ontology, ObjectStore]:
    """`_two_team_vialink_client` with the governing link the OTHER way.

    `direction="to"` reads `links_to`/`links_to_asof`, which are separate SQL
    templates with their own `ORDER BY`. Inverting the tie-break in those two
    alone left every gate-level file green **until arm 5 existed** -- measured
    with arm 4 already in place, so arm 5 is the sole discriminator for that
    mutation and the `from`-direction arms do not stand in for it. On this tree
    the same mutation reds (`M23`), which is the point of the arms.
    """
    ontology = Ontology(name="lifecycle-two-team-to", scope_levels=["team"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="team")], owned=True)
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            ViaLink(link_api_name="hasWorker", direction="to", parent_type="Team")
        ],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("hasWorker", Team, Worker, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


def _operator_for(
    ontology: Ontology, store: ObjectStore, team_id: str
) -> OntologyClient:
    return OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id=f"operator-{team_id}",
            role="Operator",
            scope_level="team",
            scope_id=team_id,
            kind="human",
        ),
    )


def test_live_target_resolves_through_its_live_parent_not_a_retired_one() -> None:
    """`include_retired` is the TARGET's own question, never an ancestor's.

    The gate asks it of every `scope_semantics="target"` param, live or not,
    so a LIVE object whose parent was retired had that parent resolved from
    `read_last` too -- and `_apply_rule`'s ViaLink loop returns the FIRST
    parent that resolves. A worker linked to a retired team and a live one
    then resolved to the retired team: the operator who has no present-tense
    relationship to the worker was let through the gate, and the operator
    who owns it was denied. Threading the flag into the ancestor frame
    inverts authorization; it belongs only to the frame of the object the
    gate was asked about.

    The retired-target pins cannot construct this (L18): every one of them
    retires the TARGET, so none has a live target at all, and none links one
    object to two parents -- the shape that makes first-parent-wins decide
    which scope answers.
    """
    ontology, store = _two_team_vialink_client()
    store.insert("Team", {"id": "team-old"}, SRC)
    store.insert("Team", {"id": "team-new"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("onTeam", "worker-1", "team-old")  # first -- wins the loop
    store.create_link("onTeam", "worker-1", "team-new")
    store.retire_object("Team", "team-old")

    # The worker is LIVE and belongs to team-new. team-old is history.
    assert store.read_current("Worker", "worker-1") is not None

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-old").execute(
            "OffboardWorker", {"worker_id": "worker-1"}
        )

    result = _operator_for(ontology, store, "team-new").execute(
        "OffboardWorker", {"worker_id": "worker-1"}
    )
    assert result["worker_id"] == "worker-1"


def test_the_declared_link_order_decides_which_operator_the_gate_admits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared link ORDER is what decides which operator gets through.

    `Store.links_from`/`links_to` declare "earliest link `valid_from` first,
    then lowest id", and `tests/test_store_conformance.py` pins that on every
    backend. `_apply_rule` takes the first parent whose chain resolves. Until
    this pin, NOTHING joined those two facts. Measured on the reconstructed
    PRE-T4 tree, whose control over `tests/test_actions.py` + `test_scope.py`
    + `test_multi_tenancy.py` is 180 passed / 4 skipped:
    inverting the tie-break on all three backends left that set **unchanged
    at 180 passed / 4 skipped**, entirely green. The published sentence "which
    operator this gate admits" therefore rested on composing two pins rather
    than on one measurement, which is the shape this round exists to stop
    shipping.

    FIVE arms — two keys x two directions x two reads, and reviewers
    measured that no arm stands in for another. Each of these mutations was
    green across all four gate-level files before its arm existed:

    1. the LIVE read (`links_from`), tie forced -> the SECONDARY key, id;
    2. the AS-OF read (`links_from_asof`) on the same fan-out, which is the
       path every RETIRED target takes -- inverting the tie-break in the two
       ASOF templates alone was green;
    3. the PRIMARY key, `valid_from`, with the two keys deliberately in
       CONFLICT: the earlier-linked parent sorts LAST by id, so only
       `valid_from` can pick it -- inverting `valid_from` ASC->DESC on all
       three backends was green;
    4. `direction="to"`, which reads `links_to`/`links_to_asof` -- two more
       templates with their own `ORDER BY` -- on the same conflict of keys;
    5. the SECONDARY key on those same `to` templates, tie forced, because
       arm 4 lets `valid_from` decide and inverting the `links_to` tie-break
       alone stayed green with arm 4 already in place.

    Two links made in ONE frozen tick share `valid_from` exactly, so the id
    is the only thing left to separate them -- and `team-a` sorts below
    `team-b` even though `team-b` was linked first. So the gate admits
    `team-a`'s operator and denies `team-b`'s. That is the accepted cost the
    round-9 ledger records: the winner is earliest `valid_from`, then lowest
    id, and NOT the order the links were created in.

    Freezing the clock makes the tie certain rather than a microsecond
    collision that happens a large fraction of the time -- the same reason
    the cascade-tick pin below freezes it.
    """
    frozen = "2026-08-29T09:00:00+00:00"
    monkeypatch.setattr(ontary.store.sqlite, "_utcnow_iso", lambda: frozen)

    ontology, store = _two_team_vialink_client()
    store.insert("Team", {"id": "team-a"}, SRC)
    store.insert("Team", {"id": "team-b"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("onTeam", "worker-1", "team-b")  # written FIRST
    store.create_link("onTeam", "worker-1", "team-a")

    # The shape this pin needs, forced rather than hoped for: one instant,
    # two parents, and the later-written one sorting first.
    assert store.links_from("onTeam", "worker-1") == ["team-a", "team-b"]

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-b").execute(
            "OffboardWorker", {"worker_id": "worker-1"}
        )
    assert _operator_for(ontology, store, "team-a").execute(
        "OffboardWorker", {"worker_id": "worker-1"}
    ) == {"worker_id": "worker-1"}

    # That arm exercised `links_from` -- the LIVE read, because the target was
    # live. The sentence above is quantified over the AS-OF read, which is the
    # gate's path for every RETIRED target and therefore the whole subject of
    # this branch. Measured by a reviewer: inverting the tie-break in the two
    # ASOF templates ALONE left every gate-level file green while reding the
    # store suite, so the arm above cannot stand in for this one. The handler
    # just retired `worker-1` under the frozen clock, so its own instant is
    # `frozen` and the same two links are still readable there.
    retired = store.read_last("Worker", "worker-1")
    assert retired is not None
    assert retired.lineage.valid_to == frozen
    assert store.links_from_asof("onTeam", "worker-1", frozen) == ["team-a", "team-b"]

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-b").execute(
            "OffboardWorker", {"worker_id": "worker-1"}
        )
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-1"}
        )

    # ARM 3: the PRIMARY key. `team-z` is linked FIRST and sorts LAST by id,
    # so `valid_from` and the tie-break disagree and only `valid_from` can
    # admit `team-z`'s operator. Inverting `valid_from` ASC->DESC on all three
    # backends was green across every gate-level file before this arm.
    early = "2026-08-29T09:00:00+00:00"
    late = "2026-08-29T10:00:00+00:00"
    clock = {"now": early}
    monkeypatch.setattr(ontary.store.sqlite, "_utcnow_iso", lambda: clock["now"])

    ontology, store = _two_team_vialink_client()
    store.insert("Team", {"id": "team-a"}, SRC)
    store.insert("Team", {"id": "team-z"}, SRC)
    store.insert("Worker", {"id": "worker-2"}, SRC)
    store.create_link("onTeam", "worker-2", "team-z")  # EARLIER, higher id
    clock["now"] = late
    store.create_link("onTeam", "worker-2", "team-a")  # later, LOWER id
    assert store.links_from("onTeam", "worker-2") == ["team-z", "team-a"]

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-2"}
        )
    assert _operator_for(ontology, store, "team-z").execute(
        "OffboardWorker", {"worker_id": "worker-2"}
    ) == {"worker_id": "worker-2"}

    # ...and again through the as-of read, at the worker's own instant.
    assert store.links_from_asof("onTeam", "worker-2", late) == ["team-z", "team-a"]
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-2"}
        )
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        _operator_for(ontology, store, "team-z").execute(
            "OffboardWorker", {"worker_id": "worker-2"}
        )

    # ARM 4: `direction="to"`, which reads `links_to`/`links_to_asof` -- two
    # templates the three arms above never touch. Same conflict of keys.
    clock["now"] = early
    ontology, store = _two_team_vialink_to_client()
    store.insert("Team", {"id": "team-a"}, SRC)
    store.insert("Team", {"id": "team-z"}, SRC)
    store.insert("Worker", {"id": "worker-3"}, SRC)
    store.create_link("hasWorker", "team-z", "worker-3")  # EARLIER, higher id
    clock["now"] = late
    store.create_link("hasWorker", "team-a", "worker-3")  # later, LOWER id
    assert store.links_to("hasWorker", "worker-3") == ["team-z", "team-a"]

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-3"}
        )
    assert _operator_for(ontology, store, "team-z").execute(
        "OffboardWorker", {"worker_id": "worker-3"}
    ) == {"worker_id": "worker-3"}

    assert store.links_to_asof("hasWorker", "worker-3", late) == ["team-z", "team-a"]
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-3"}
        )
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        _operator_for(ontology, store, "team-z").execute(
            "OffboardWorker", {"worker_id": "worker-3"}
        )

    # ARM 5: the SECONDARY key on the `to` templates. Arm 4 put the two keys
    # in conflict, so `valid_from` decided and the `links_to` tie-break was
    # never exercised -- measured: inverting it alone stayed green even with
    # arm 4 in place. One tick, two links, so only the id can separate them.
    store.insert("Worker", {"id": "worker-4"}, SRC)
    store.create_link("hasWorker", "team-z", "worker-4")
    store.create_link("hasWorker", "team-a", "worker-4")
    assert store.links_to("hasWorker", "worker-4") == ["team-a", "team-z"]

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-z").execute(
            "OffboardWorker", {"worker_id": "worker-4"}
        )
    assert _operator_for(ontology, store, "team-a").execute(
        "OffboardWorker", {"worker_id": "worker-4"}
    ) == {"worker_id": "worker-4"}

    assert store.links_to_asof("hasWorker", "worker-4", late) == ["team-a", "team-z"]
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _operator_for(ontology, store, "team-z").execute(
            "OffboardWorker", {"worker_id": "worker-4"}
        )
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        _operator_for(ontology, store, "team-a").execute(
            "OffboardWorker", {"worker_id": "worker-4"}
        )

    # DOCUMENTED: the whole sentence the canonical uses to promise it, in
    # both languages. A fragment would not do -- "separated by id" survives
    # inside a sentence that says the opposite about creation order. Re-derive
    # these from this measurement; do not edit them to match a reworded
    # canonical.
    assert (
        _normalize_prose(
            """It is not creation order — `links` declares no monotonic key,
            so two links made in one clock tick are separated by id, not by
            which was written first."""
        )
        in _SCOPE_PROSE_CANON[("scope-asof-invariant", "en")]
    )
    assert (
        _squeeze(
            """作成順ではありません。`links` は単調増加のキーを宣言していないため、
            同じ tick で作られた 2 本のリンクは、どちらを先に書いたかではなく id で
            並びます。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-asof-invariant", "ja")])
    )


def _company_hierarchy_client() -> tuple[Ontology, ObjectStore]:
    """A TWO-level hierarchy, so an ancestor can die without the scope dying.

    Every retired-target fixture above is one level deep -- `Worker -> Team`
    where `Team` IS the scope -- so a dead ancestor and an unresolvable scope
    are the same event and cannot be told apart (L18). Here `Company` outlives
    `Team`, which makes "the ancestor is retired but the scope it leads to is
    still live and still owns the row" constructible for the first time.

    `Worker` is `ViaLink`-scoped, so it exercises `_apply_rule`'s parent hop.
    `Contractor` is `DirectProperty`-scoped with no backing link, so asking it
    for "company" has no rule to answer and falls through to
    `_climb_hierarchy`'s canonical-instance hop -- the other ancestor frame,
    and the one no pin reached before.
    """
    ontology = Ontology(
        name="lifecycle-company", scope_levels=["team", "company"], min_n=1
    )

    @ontology.object(layer="L0", scope=[SelfScope(level="company")], owned=True)
    class Company(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="team"),
            ViaLink(
                link_api_name="inCompany", direction="from", parent_type="Company"
            ),
        ],
        owned=True,
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="onTeam", direction="from", parent_type="Team")],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="team", property_name="team")],
        owned=True,
    )
    class Contractor(OntologyObject):
        id: str = prop(primary_key=True)
        team: str = prop()

    ontology.link("inCompany", Team, Company, Cardinality.MANY_TO_MANY, owned=True)
    ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    class EndContractParams(ActionParams):
        contractor_id: str = target(Contractor)

    @ontology.action(
        EndContractParams,
        target=Contractor,
        roles=["Operator"],
        api_name="EndContract",
    )
    def end_contract(
        ctx: ActionContext, params: EndContractParams
    ) -> dict[str, str]:
        ctx.retire("Contractor", params.contractor_id)
        return {"contractor_id": params.contractor_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


def _company_operator(
    ontology: Ontology, store: ObjectStore, company_id: str
) -> OntologyClient:
    return OntologyClient(
        ontology,
        store,
        Consumer(
            actor_id=f"operator-{company_id}",
            role="Operator",
            scope_level="company",
            scope_id=company_id,
            kind="human",
        ),
    )


def test_retired_target_resolves_through_an_ancestor_that_was_live_when_it_died() -> (
    None
):
    """The ancestor frame is POINT-IN-TIME, not live-only (D13).

    Refusing every retired ancestor closes the inversion the sibling pin
    below covers, but it also refuses an ancestor that is the ONLY route to a
    scope that is still live and still owns the row -- so the operator who
    owns it is denied `SCOPE_DENIED`, permanently and unrecoverably, because
    a disbanded team cannot be un-retired.

    The rule that closes both directions at once: resolve the chain AS OF the
    target's own closing instant. The team was alive when the worker was
    offboarded, so it is on the worker's chain; the company it led to is
    alive now and still owns the row.
    """
    ontology, store = _company_hierarchy_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("inCompany", "team-1", "co-1")
    store.create_link("onTeam", "worker-1", "team-1")

    operator = _company_operator(ontology, store, "co-1")
    assert operator.execute("OffboardWorker", {"worker_id": "worker-1"}) == {
        "worker_id": "worker-1"
    }

    # The team is disbanded AFTER the worker leaves. The company is untouched
    # and still owns the worker's row.
    store.retire_object("Team", "team-1")
    assert store.read_current("Company", "co-1") is not None

    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("OffboardWorker", {"worker_id": "worker-1"})

    assert store.audit_entries()[-1].outcome == "denied"


def test_retired_target_resolves_through_an_ancestor_updated_after_it_died() -> None:
    """`_live_at` tests the upper bound ONLY, and that is deliberate (D13).

    An ancestor that was alive at the target's closing instant and is merely
    UPDATED afterwards has a newest row that OPENS after that instant.
    `read_last` answers with that newest row, so a `valid_from <= asof` test
    would reject it and deny the operator who does own the row -- which is
    exactly the round-6 defect D13 exists to close, reached through an
    ancestor UPDATE instead of an ancestor RETIREMENT.

    This pin exists because the decision had none in either direction: the
    opposite implementation (`valid_from > asof` rejects) passed the entire
    suite, both arms, unchanged. It is the guard on a specific future edit --
    adding the lower bound to `_live_at` to make the point-in-time claim
    literally true -- which is a reasonable-looking change that silently
    reinstates a permanent, unrecoverable denial. Making that claim true
    needs an as-of read of the ancestor's history, not a lower bound on its
    newest row; see the `read_asof` ticket.
    """
    ontology, store = _company_hierarchy_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("inCompany", "team-1", "co-1")
    store.create_link("onTeam", "worker-1", "team-1")

    operator = _company_operator(ontology, store, "co-1")
    assert operator.execute("OffboardWorker", {"worker_id": "worker-1"}) == {
        "worker_id": "worker-1"
    }

    # The team is UPDATED after the worker leaves -- still alive the whole
    # time, but its newest row now opens after the worker's closing instant.
    store.update("Team", "team-1", {"id": "team-1"}, SRC)
    team = store.read_last("Team", "team-1")
    assert team is not None
    worker = store.read_last("Worker", "worker-1")
    assert worker is not None
    assert worker.lineage.valid_to is not None
    assert team.lineage.valid_from > worker.lineage.valid_to  # the shape, forced

    # Resolution must still reach the company, so the owner gets the
    # handler's refusal and not `SCOPE_DENIED`.
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("OffboardWorker", {"worker_id": "worker-1"})


def test_retired_target_is_denied_when_its_ancestor_died_before_it() -> None:
    """The one case point-in-time resolution still denies, and why.

    The sibling above admits an ancestor that was live at the target's
    closing instant. This is the other side of that same rule: an ancestor
    already retired BEFORE the target closed was not on the target's chain
    then either, so there is no instant at which this row had a resolvable
    scope. Deny-by-default denies.

    Pinned so the fix for the sibling cannot quietly become "any retired
    ancestor resolves", which is the round-5 inversion coming back.
    """
    ontology, store = _company_hierarchy_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("inCompany", "team-1", "co-1")
    store.create_link("onTeam", "worker-1", "team-1")

    # Team dies FIRST, worker after it -- the reverse of the sibling's order.
    store.retire_object("Team", "team-1")
    store.retire_object("Worker", "worker-1")

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        _company_operator(ontology, store, "co-1").execute(
            "OffboardWorker", {"worker_id": "worker-1"}
        )

    assert store.audit_entries()[-1].outcome == "denied"


def test_retired_target_climbs_through_a_canonical_instance_live_when_it_died() -> (
    None
):
    """`_climb_hierarchy`'s canonical hop is the OTHER ancestor frame.

    `Contractor` is `DirectProperty`-scoped at "team" with no backing link,
    so nothing in its own rule set answers "company": resolution falls
    through to `_climb_hierarchy`, which resolves "team" for the contractor
    and then asks the canonical `Team` instance for "company". That is a hop
    to a DIFFERENT object, and it was hard-coded live-only with no pin at all
    -- reverting it alone left the whole suite green.

    Same shape as the `ViaLink` sibling, reached down the other code path.
    """
    ontology, store = _company_hierarchy_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Contractor", {"id": "c-1", "team": "team-1"}, SRC)
    store.create_link("inCompany", "team-1", "co-1")

    operator = _company_operator(ontology, store, "co-1")
    assert operator.execute("EndContract", {"contractor_id": "c-1"}) == {
        "contractor_id": "c-1"
    }

    store.retire_object("Team", "team-1")

    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("EndContract", {"contractor_id": "c-1"})

    assert store.audit_entries()[-1].outcome == "denied"


def test_retired_target_is_denied_when_the_canonical_instance_died_before_it() -> (
    None
):
    """The climb hop's DENY side -- and why a list of the four rule kinds is
    not a list of everything that can fail.

    The sibling above pins the ADMIT side of `_climb_hierarchy`'s canonical
    hop. Nothing pinned the other side, and a reviewer measured what that
    cost: the consequences prose, regrouped by RULE KIND, read as an
    exhaustive partition of what can fail -- while THIS denial comes from a
    hop that is not a declared rule kind at all.

    `Contractor` is `DirectProperty(level="team")`-scoped with no backing
    link, so asking it for "company" has no rule of its own to answer and
    falls through to `_climb_hierarchy`: resolve "team" for the contractor,
    then ask the canonical `Team` instance for "company". That `Team` is a
    DIFFERENT object, admitted on the same point-in-time terms as a `ViaLink`
    parent (`scope.py`'s `_live_at`), so one retired BEFORE the contractor's
    own row closed cannot answer for it.

    The contractor's own `DirectProperty` hop is intact, so neither of those
    bullets covers this denial: it is the kind-agnostic claim the canonical
    states after them, and this is its measurement.

    Two contractors of identical shape make the assertion mean something --
    the ONLY difference is whether the canonical `Team` closed before or
    after the contractor did. Without the second half this would also pass
    for a gate that denied every retired contractor.
    """
    ontology, store = _company_hierarchy_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-early"}, SRC)
    store.insert("Team", {"id": "team-late"}, SRC)
    store.create_link("inCompany", "team-early", "co-1")
    store.create_link("inCompany", "team-late", "co-1")
    store.insert("Contractor", {"id": "c-1", "team": "team-early"}, SRC)
    store.insert("Contractor", {"id": "c-2", "team": "team-late"}, SRC)

    operator = _company_operator(ontology, store, "co-1")

    # c-1's canonical instance closes BEFORE c-1 does.
    store.retire_object("Team", "team-early")
    store.retire_object("Contractor", "c-1")
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        operator.execute("EndContract", {"contractor_id": "c-1"})
    assert store.audit_entries()[-1].outcome == "denied"

    # c-2's closes AFTER -- same shape, same operator, opposite outcome.
    store.retire_object("Contractor", "c-2")
    store.retire_object("Team", "team-late")
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("EndContract", {"contractor_id": "c-2"})

    # DOCUMENTED: the kind-agnostic sentence, whole, in both languages. It is
    # what the four per-kind bullets do NOT say, and the reason they may be
    # read as per-hop rather than per-target. Re-derive it from this
    # measurement; do not edit it to match a reworded canonical.
    assert (
        _normalize_prose(
            """What the engine's own hops share is the frame: any object the
            engine has to read that had already been retired before the target
            closed is refused there, whichever of those hops reached it — so
            the hop that answers a target's level can fail on an object the
            target's other hops never touch."""
        )
        in _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    )
    assert (
        _squeeze(
            """engine 自身の hop に共通するのは断面です。engine が読まなければ
            ならないオブジェクトが、target が閉じるより前に retire されていれば、
            そのどの hop からたどり着いたかによらず、そこで拒否されます。
            したがって、target の level に答える hop は、その target の他の hop が
            まったく触れないオブジェクトで失敗することがあります。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")])
    )

    # The sentence naming the hop this fixture exercises, in BOTH languages.
    # Round 16 caught the JA half of it promising the canonical instance of the
    # level being ASKED FOR, where `_canonical_type_for_level` is called with
    # the NARROWER one -- a mistranslation nothing could see, because only the
    # English clause was tied. Both are tied now.
    assert (
        _normalize_prose(
            """a level no rule of its own can answer climbs to the canonical
            instance of a narrower scope, where a type declares one"""
        )
        in _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    )
    assert (
        _squeeze(
            """さらに、自身のルールでは答えられない level は、より狭い level の
            canonical なインスタンスを宣言する型があれば、そこへ登ります。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")])
    )


def test_retired_customresolver_target_is_denied_not_refused() -> None:
    """The documented boundary of the history-aware target gate.

    `SelfScope`, `DirectProperty` and `ViaLink` targets keep reaching the
    handler's `OBJECT_ALREADY_RETIRED` after a retirement (pinned above).
    `CustomResolver` does NOT: its callable is author code handed the raw
    `Store`, the engine does not reach inside it, and the natural body reads
    `read_current` -- `None` for a retired object. So the owning operator is
    denied `SCOPE_DENIED` instead.

    This pins the boundary rather than endorsing it. It fails CLOSED, so it
    is a usability limit and not a disclosure: the sibling pin two above
    covers the leak direction. If a later change makes `CustomResolver`
    retirement-aware, this row must be updated together with the paragraph
    in `docs/api-reference.md` and `CHANGELOG.md` that promises it -- which
    is the point of pinning it.
    """
    client, store = _scoped_lifecycle_client(
        CustomResolver(level="team", fn=_resolver_reading_the_live_row)
    )
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1", "team": "team-1"}, SRC)

    client.execute("OffboardWorker", {"worker_id": "worker-1"})

    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        client.execute("OffboardWorker", {"worker_id": "worker-1"})

    assert store.audit_entries()[-1].outcome == "denied"


def _resolver_climbing_the_last_rows(
    store: Store, obj_type: str, obj_id: str
) -> str | None:
    """A retirement-AWARE `CustomResolver` body: `read_last` at every hop.

    The sibling above (`_resolver_reading_the_live_row`) is the natural body
    and answers `None` for a retired object. This one is what an author
    writes to keep working after a retirement -- and because the engine hands
    author code the raw `Store` and never threads the target's closing
    instant into it, the hops THIS function takes are outside the as-of
    frame. That is the boundary the published prose draws, and the pin below
    measures it.
    """
    row = store.read_last(obj_type, obj_id)
    if row is None:
        return None
    team_id = row.payload.get("team")
    if team_id is None:
        return None
    team = store.read_last("Team", str(team_id))
    if team is None:
        return None
    company = team.payload.get("company")
    return None if company is None else str(company)


def _resolver_naming_its_team_from_the_last_row(
    store: Store, obj_type: str, obj_id: str
) -> str | None:
    """A `CustomResolver` that answers a NARROWER level than the gate asks.

    Its answer is not the end of resolution: `_climb_hierarchy` takes the id
    it returns and asks the canonical instance of that level for the broader
    one -- which is an ENGINE read, and therefore inside the as-of frame even
    though the resolver's own read was not.
    """
    row = store.read_last(obj_type, obj_id)
    if row is None:
        return None
    team_id = row.payload.get("team")
    return None if team_id is None else str(team_id)


def _customresolver_narrower_level_client() -> tuple[Ontology, ObjectStore]:
    """A `CustomResolver` at "team" in a policy whose consumers are at
    "company", so the engine must climb past the resolver's answer."""
    ontology = Ontology(
        name="lifecycle-resolver-climb", scope_levels=["team", "company"], min_n=1
    )

    @ontology.object(layer="L0", scope=[SelfScope(level="company")], owned=True)
    class Company(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="team"),
            DirectProperty(level="company", property_name="company"),
        ],
        owned=True,
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        company: str = prop()

    @ontology.object(
        layer="L0",
        scope=[
            CustomResolver(
                level="team", fn=_resolver_naming_its_team_from_the_last_row
            )
        ],
        owned=True,
    )
    class Consultant(OntologyObject):
        id: str = prop(primary_key=True)
        team: str = prop()

    class EndEngagementParams(ActionParams):
        consultant_id: str = target(Consultant)

    @ontology.action(
        EndEngagementParams,
        target=Consultant,
        roles=["Operator"],
        api_name="EndEngagement",
    )
    def end_engagement(
        ctx: ActionContext, params: EndEngagementParams
    ) -> dict[str, str]:
        ctx.retire("Consultant", params.consultant_id)
        return {"consultant_id": params.consultant_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


def _customresolver_ancestor_rule_client() -> (
    tuple[Ontology, ObjectStore, list[str]]
):
    """The ANCESTOR's own and only company rule is a `CustomResolver`.

    Returns the ids the callable was asked about, so the pin can assert the
    engine refused the ancestor's ROW before the callable ever ran -- which
    is the third arm of the published sentence and the one T4 first handed on
    as unconstructible. A reviewer built it; this is that fixture.
    """
    asked: list[str] = []

    def resolve_company(store: Store, obj_type: str, obj_id: str) -> str | None:
        asked.append(obj_id)
        row = store.read_last(obj_type, obj_id)
        if row is None:
            return None
        company = row.payload.get("company")
        return None if company is None else str(company)

    ontology = Ontology(
        name="lifecycle-resolver-ancestor-rule",
        scope_levels=["team", "company"],
        min_n=1,
    )

    @ontology.object(layer="L0", scope=[SelfScope(level="company")], owned=True)
    class Company(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="team"),
            CustomResolver(level="company", fn=resolve_company),
        ],
        owned=True,
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        company: str = prop()

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="onTeam", direction="from", parent_type="Team")],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry), asked


def _customresolver_ancestor_client() -> tuple[Ontology, ObjectStore]:
    """A `CustomResolver` whose callable climbs to an ancestor ITSELF.

    Every other `CustomResolver` fixture in this file resolves off the
    target's own row, so none of them can observe what the engine does --
    or does not do -- to the ancestor hops the callable takes on its own.
    """
    ontology = Ontology(
        name="lifecycle-resolver-ancestor", scope_levels=["company"], min_n=1
    )

    @ontology.object(layer="L0", scope=[SelfScope(level="company")], owned=True)
    class Company(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[DirectProperty(level="company", property_name="company")],
        owned=True,
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        company: str = prop()

    @ontology.object(
        layer="L0",
        scope=[
            CustomResolver(level="company", fn=_resolver_climbing_the_last_rows)
        ],
        owned=True,
    )
    class Consultant(OntologyObject):
        id: str = prop(primary_key=True)
        team: str = prop()

    class EndEngagementParams(ActionParams):
        consultant_id: str = target(Consultant)

    @ontology.action(
        EndEngagementParams,
        target=Consultant,
        roles=["Operator"],
        api_name="EndEngagement",
    )
    def end_engagement(
        ctx: ActionContext, params: EndEngagementParams
    ) -> dict[str, str]:
        ctx.retire("Consultant", params.consultant_id)
        return {"consultant_id": params.consultant_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


def test_the_asof_frame_stops_at_a_resolvers_reads_and_resumes_at_its_answer() -> (
    None
):
    """The published boundary of the point-in-time frame, BOTH sides.

    `_live_at` lives in `_resolve_level`, so it covers every object the
    ENGINE reads -- and only those. `_apply_rule`'s `CustomResolver` branch
    deliberately does not thread `asof` into the callable, so an ancestor the
    callable reaches for itself is read however the callable reads it.

    The shape below is the one that DENIES at every engine hop -- the
    ancestor closes BEFORE the target does, exactly as in
    `test_retired_target_is_denied_when_its_ancestor_died_before_it` -- and
    here it is ADMITTED, because the resolver's own `read_last` never asks
    the frame's question. Without this pin the published sentence "any object
    the engine has to read … is refused there, whichever of those hops
    reached it" reads as "whichever hop", which the engine refutes; a
    reviewer measured exactly this case.

    That is only half the boundary, and stating the other half is what
    review round 13 forced: the callable's ANSWER re-enters the engine. The
    second arm below puts a `CustomResolver` at the NARROWER level, so
    `_climb_hierarchy` takes the team id the resolver returned and re-reads
    that same `Team` under `asof` -- an engine read, so the frame applies and
    the ancestor the callable had read for itself IS refused. Only the
    ancestor's retirement ORDER differs between `k-3` and `k-4`.

    A THIRD arm follows both: the ancestor's own company rule is itself a
    `CustomResolver`, and the pin asserts the callable is never even asked
    when `_live_at` refuses that ancestor's row -- which is what "whose row
    is checked before its own resolver runs" promises. `M18` reds it alone.

    `k-2` is the first arm's control: the same gate, the same operator, a
    resolver that answers `None` because its ancestor row does not exist.
    Without it that arm would also pass for a gate that admitted every
    retired consultant.
    """
    ontology, store = _customresolver_ancestor_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-1", "company": "co-1"}, SRC)
    store.insert("Consultant", {"id": "k-1", "team": "team-1"}, SRC)
    store.insert("Consultant", {"id": "k-2", "team": "team-missing"}, SRC)

    operator = _company_operator(ontology, store, "co-1")

    # The ancestor closes BEFORE the target. An engine hop refuses this.
    store.retire_object("Team", "team-1")
    store.retire_object("Consultant", "k-1")
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("EndEngagement", {"consultant_id": "k-1"})

    # Control: the gate CAN deny here -- it denies whenever the resolver
    # itself answers `None`, which is the boundary's other side.
    store.retire_object("Consultant", "k-2")
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        operator.execute("EndEngagement", {"consultant_id": "k-2"})
    assert store.audit_entries()[-1].outcome == "denied"

    # ARM 2: the same callable shape, one level down. The resolver answers
    # "team"; the engine climbs from there and re-reads that `Team` under
    # `asof`. Revision 2's prose said an ancestor the callable read for itself
    # is "read however the callable reads it, retired or not" -- full stop,
    # which THIS arm refutes, and which a reviewer measured before it shipped.
    ontology, store = _customresolver_narrower_level_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-early", "company": "co-1"}, SRC)
    store.insert("Team", {"id": "team-late", "company": "co-1"}, SRC)
    store.insert("Consultant", {"id": "k-3", "team": "team-early"}, SRC)
    store.insert("Consultant", {"id": "k-4", "team": "team-late"}, SRC)
    operator = _company_operator(ontology, store, "co-1")

    store.retire_object("Team", "team-early")
    store.retire_object("Consultant", "k-3")
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        operator.execute("EndEngagement", {"consultant_id": "k-3"})
    assert store.audit_entries()[-1].outcome == "denied"

    # Same shape, opposite order: the engine's read admits it.
    store.retire_object("Consultant", "k-4")
    store.retire_object("Team", "team-late")
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("EndEngagement", {"consultant_id": "k-4"})

    # DOCUMENTED: one whole sentence per arm, both languages. Re-derive them
    # from these measurements; do not edit them to match a reworded canonical.
    assert (
        _normalize_prose(
            """A `CustomResolver` is outside that frame only for the reads its
            own callable makes: the engine does not thread the instant into
            author code, so an ancestor the callable reaches for itself is read
            however it reads it, retired or not."""
        )
        in _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    )
    assert (
        _normalize_prose(
            """Its ANSWER re-enters the engine, and every object the engine
            reads from there is refused on the frame's own terms — the
            canonical instance a narrower answer names, and any object whose
            rules the engine goes on to ask, whose row is checked before its
            own resolver runs."""
        )
        in _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    )
    assert (
        _squeeze(
            """`CustomResolver` がこの断面の外にあるのは、その callable 自身が行う
            read についてだけです。engine は作者のコードにその時刻を渡さないため、
            callable が自分でたどる祖先は、retire 済みかどうかに関わらず callable の
            読み方どおりに読まれます。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")])
    )
    assert (
        _squeeze(
            """その**答え**は engine に戻ります。そこから engine が読むオブジェクトは、
            この断面の規則どおりに拒否されます。より狭い level の答えが指す canonical な
            インスタンスも、engine が続けてルールを尋ねるオブジェクトも同じで、後者の行は
            その resolver が動く前に検査されます。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")])
    )

    # ARM 3: the ANCESTOR's own company rule is a `CustomResolver`. The
    # published sentence says such an object's ROW is checked before its own
    # resolver runs -- so the callable must not even be asked when the
    # ancestor closed first. T4 first handed this shape on as unconstructible
    # here; a reviewer built it from the public API, so it is pinned instead.
    ontology, store, asked = _customresolver_ancestor_rule_client()
    store.insert("Company", {"id": "co-1"}, SRC)
    store.insert("Team", {"id": "team-early", "company": "co-1"}, SRC)
    store.insert("Team", {"id": "team-late", "company": "co-1"}, SRC)
    store.insert("Worker", {"id": "w-early"}, SRC)
    store.insert("Worker", {"id": "w-late"}, SRC)
    store.create_link("onTeam", "w-early", "team-early")
    store.create_link("onTeam", "w-late", "team-late")
    operator = _company_operator(ontology, store, "co-1")

    store.retire_object("Team", "team-early")
    store.retire_object("Worker", "w-early")
    asked.clear()
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        operator.execute("OffboardWorker", {"worker_id": "w-early"})
    assert asked == [], (
        "the ancestor's row must be refused before its own resolver runs; "
        f"the callable was asked about {asked}"
    )

    # Same shape, opposite order: the row is admitted and the callable runs.
    store.retire_object("Worker", "w-late")
    store.retire_object("Team", "team-late")
    asked.clear()
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        operator.execute("OffboardWorker", {"worker_id": "w-late"})
    assert asked == ["team-late"]


def _payload_ancestor_client() -> tuple[Ontology, ObjectStore]:
    """A hierarchy whose ANCESTOR carries the scope key on its payload.

    `_company_hierarchy_client`'s `Team` reaches `Company` by `ViaLink`, so
    the ancestor hop never reads a payload at all. Here the ancestor hop is
    `DirectProperty`, which reads the ancestor's own scope key off its row.
    """
    ontology = Ontology(
        name="lifecycle-payload-ancestor", scope_levels=["team", "company"], min_n=1
    )

    @ontology.object(layer="L0", scope=[SelfScope(level="company")], owned=True)
    class Company(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.object(
        layer="L0",
        scope=[
            SelfScope(level="team"),
            DirectProperty(level="company", property_name="company"),
        ],
        owned=True,
    )
    class Team(OntologyObject):
        id: str = prop(primary_key=True)
        company: str = prop()

    @ontology.object(
        layer="L0",
        scope=[ViaLink(link_api_name="onTeam", direction="from", parent_type="Team")],
        owned=True,
    )
    class Worker(OntologyObject):
        id: str = prop(primary_key=True)

    ontology.link("onTeam", Worker, Team, Cardinality.MANY_TO_MANY, owned=True)

    class OffboardWorkerParams(ActionParams):
        worker_id: str = target(Worker)

    @ontology.action(
        OffboardWorkerParams,
        target=Worker,
        roles=["Operator"],
        api_name="OffboardWorker",
    )
    def offboard_worker(
        ctx: ActionContext, params: OffboardWorkerParams
    ) -> dict[str, str]:
        ctx.retire("Worker", params.worker_id)
        return {"worker_id": params.worker_id}

    ontology.validate()
    return ontology, ObjectStore(ontology.registry)


_REPO_ROOT = Path(__file__).resolve().parent.parent

# --- The `include_retired` invariant prose, single-sourced ----------------
#
# Rounds 3-8 each closed this hole by adding the one site, or the one banned
# phrasing, that had been missed -- and the membership moved every round. The
# fourth site (`src/ontary/scope.py`, which is what `help()` and IDE hover
# show) was never on the list, and reinstating the verbatim banned sentence
# there left the whole suite green. The blocklist that guarded the other
# three quantified over an UNBOUNDED set of wrong phrasings, so re-adding the
# overclaim in new words escaped it too.
#
# This instrument quantifies over the BOUNDED set of files instead, and
# derives that set by scanning the corpus rather than by listing it. One
# canonical text per (region id, language); every marked region found
# anywhere in the corpus must equal its canonical WHOLE; every function whose
# signature declares `include_retired` must carry the English canonicals in
# its docstring; and the phrases the canonical is anchored on may not appear
# outside a guarded region. Editing any site therefore reds until the
# canonical below is edited too -- which is the point at which the claim is
# re-approved against the measured outcome pinned next to it.

_SCOPE_ASOF_INVARIANT_EN = """\
A retired target resolves the scope it owned **as of the instant its own
row closed**. The links on the chain are read at that instant on both
bounds, so an edge the object had already left cannot answer for it.

Where a `ViaLink` hop finds more than one parent at that instant, the
first parent whose chain resolves wins, over an order the store declares
rather than each backend's own row order: earliest link `valid_from`
first, then lowest parent id, compared byte-wise. It is not creation
order — `links` declares no monotonic key, so two links made in one clock
tick are separated by id, not by which was written first. Every backend
answers in that order because it decides which scope owns the object, and
therefore which operator this gate admits.

An ancestor object is admitted when it had not already been retired by
then — but it is read at its newest row, so its payload, and any
`DirectProperty` key taken off that payload, is the one it carries now: an
ancestor updated after the instant answers with the scope it is in today,
not the one it was in then. Making the object side point-in-time too needs
an as-of read of an object's history, which `Store` does not have. A live
target has no closing instant and resolves in the present tense, exactly
as a consumer read does, so this gate loosens nothing for an object that
still exists.
"""

_SCOPE_ASOF_INVARIANT_JA = """\
gate はこの連鎖を、**target 自身の行が閉じた時刻の断面で**解決します。連鎖上のリンクは
上下どちらの境界もその時刻で読みます。そのため、target が既に離れていた辺が target の
ために答えることはありません。

その時刻に `ViaLink` の hop が複数の親を見つけた場合は、連鎖が解決できた最初の親が
勝ちます。その順序は各バックエンドの行順ではなく、store が宣言する順序です。すなわち、
リンクの `valid_from` が最も早いものから、次にバイト順で最も小さい親 id からです。
作成順ではありません。`links` は単調増加のキーを宣言していないため、同じ tick で作られた
2 本のリンクは、どちらを先に書いたかではなく id で並びます。この順序が、どのスコープが
そのオブジェクトを所有するか、したがってこの gate がどの操作者を通すかを決めます。その
ため、すべてのバックエンドがこの順序で答えます。

祖先のオブジェクトは、その時刻に retire 済みでなければ採用します。ただし読むのは最新行です。
したがって payload、および payload から読む `DirectProperty` のキーは、その祖先が「いま」
持っている値です。その時刻より後に更新された祖先は、当時のスコープではなく現在のスコープを
答えます。オブジェクト側も断面にするには履歴を時刻指定で読む必要がありますが、`Store` に
その read はありません。生きている target には閉じた時刻がないため、consumer の read と
まったく同じ現在時刻で解決します。まだ存在するオブジェクトについて、この gate が緩むことは
ありません。
"""

_SCOPE_DENIED_CONSEQUENCES_EN = """\
Where the chain resolves and the consumer covers it, the gate reaches the
handler's own refusal (`OBJECT_ALREADY_RETIRED`). `SCOPE_DENIED` does not
mean one thing: it is raised at two places. One is a defense-in-depth
refusal of a scope-bearing parameter that is not a `str` — parameter
validation rejects that first, so it is a floor under type confusion
rather than a path in normal use. The other fires wherever coverage cannot
be shown, and that is two situations rather than one: the chain resolved
and this consumer is outside it, which is the ordinary denial this gate
does not change; or the chain did not resolve at that instant, and
deny-by-default denies. Only the second belongs to this frame. Four rule
kinds are declared, and each meets retirement at its OWN hop:

- `SelfScope` answers with the object's own id, which retirement does not
  take away.
- `DirectProperty` reads the scope key off the payload, which retirement
  leaves alone: this gate reads the newest row rather than the live one.
- `ViaLink` climbs the `links` table. It resolves to nothing when none of
  the parents it reaches resolves in turn — among them a parent already
  retired BEFORE the target closed: its edge may still be readable at that
  instant, but its own row is not admitted, so it cannot answer at the one
  instant this gate asks about. One retired after the target — including in
  the same cascade tick — still answers for it.
- `CustomResolver` is author code handed the raw `Store`, and the engine
  does not reach inside it, so what a retired object resolves to is the
  resolver's own business rather than this frame's. The natural body reads
  `read_current`, which is `None` for a retired object, so a resolver
  written that way denies. A type that needs the precondition refusal after
  retirement declares a second rule — a `DirectProperty` on a scope-key
  column, which survives retirement.

Each bullet is about one hop, never about one target, and the four are not
the whole chain. `ScopePolicy.rules` maps each type to an ORDERED
list, so a target declares as many of these hops as that
list holds and is answered by the first that resolves; and a level no rule of its own can
answer climbs to the canonical instance of a narrower scope, where a type
declares one — which is none of the four. What the engine's own hops share is the
frame: any object the engine has to read that had already been retired
before the target closed is refused there, whichever of those hops reached
it — so the hop that answers a target's level can fail on an object the
target's other hops never touch. A `CustomResolver` is outside that frame only
for the reads its own callable makes: the engine does not thread the
instant into author code, so an ancestor the callable reaches for itself is
read however it reads it, retired or not. Its ANSWER re-enters the engine,
and every object the engine reads from there is refused on the frame's own
terms — the canonical instance a narrower answer names, and any object
whose rules the engine goes on to ask, whose row is checked before its own
resolver runs.

Every denial in this list fails closed — the object's own owner is denied,
nothing is disclosed.
"""

_SCOPE_DENIED_CONSEQUENCES_JA = """\
連鎖が解決でき、consumer がそのスコープを覆う場合、gate は handler 自身の拒否
（`OBJECT_ALREADY_RETIRED`）まで到達します。`SCOPE_DENIED` の意味は 1 つではありません。
raise は 2 か所にあります。1 つは、スコープを担うパラメータが `str` でない場合の多層防御の
拒否です。型の不一致は先にパラメータ検証が弾くため、通常の経路ではなく型混同に対する床です。
もう 1 つは、覆えることを示せないときに必ず発火します。これは 1 つではなく 2 つの状況です。
連鎖は解決できたが、この consumer がその外にいる場合。これはこの gate が変えていない従来
どおりの拒否です。あるいは、その時刻に連鎖が解決できない場合で、既定の deny が働きます。
この断面が扱うのは後者だけです。宣言されているルールの種類は 4 つで、それぞれが自身の hop で
retire に出会います。

- `SelfScope` はオブジェクト自身の id を返します。retire はこの id を奪いません。
- `DirectProperty` はスコープキーを payload から読みます。この gate が読むのは有効な行では
  なく最新行のため、retire は payload を変えません。
- `ViaLink` は `links` テーブルをたどります。たどり着いた親のどれも順に解決できないとき、
  この hop は何も解決しません。
  target が閉じるより前に retire された親もその一つです。その時刻に辺そのものは読める
  ことがありますが、親の行が採用されないため、この gate が尋ねる 1 つの時刻にその親は
  答えられません。target より後に retire された
  親は、同じ cascade の tick で閉じたものも含めて、いまも target のために解決します。
- `CustomResolver` は生の `Store` を受け取る作者のコードで、engine はその中に立ち入り
  ません。したがって、retire 済みのオブジェクトが何に解決するかは、この断面
  ではなく resolver 自身の責任です。素直な実装は `read_current` を読みますが、これは
  retire 済みのオブジェクトには `None` を返すため、そう書かれた resolver は拒否します。
  retire 後も precondition の拒否が必要な型は、2 つ目のルールを宣言してください。スコープ
  キーの列に置いた `DirectProperty` は retire 後も残ります。

各項目は 1 つの hop についての説明であり、1 つの target についての保証ではありません。
また、この 4 つが連鎖のすべてでもありません。`ScopePolicy.rules` は型ごとに**順序付きの
リスト**を持つため、target は宣言した数だけ hop を持ち、最初に解決できた hop が答えます。
さらに、自身のルールでは答えられない level は、より狭い level の canonical な
インスタンスを宣言する型があれば、そこへ登ります。これも 4 つのどれでもありません。engine 自身の hop に共通する
のは断面です。engine が読まなければならないオブジェクトが、target が閉じるより前に retire
されていれば、そのどの hop からたどり着いたかによらず、そこで拒否されます。したがって、
target の level に答える hop は、その target の他の hop がまったく触れないオブジェクトで
失敗することがあります。`CustomResolver` がこの断面の外にあるのは、その callable 自身が行う read に
ついてだけです。engine は作者のコードにその時刻を渡さないため、callable が自分でたどる祖先
は、retire 済みかどうかに関わらず callable の読み方どおりに読まれます。その**答え**は engine
に戻ります。そこから engine が読むオブジェクトは、この断面の規則どおりに拒否されます。より
狭い level の答えが指す canonical なインスタンスも、engine が続けてルールを尋ねるオブジェクト
も同じで、後者の行はその resolver が動く前に検査されます。

この一覧の拒否はすべて fail closed です。そのオブジェクト自身の所有者が拒否されるだけで
情報は出ません。
"""

# The exact sentences the retired blocklist banned. They are NOT reinstated as
# a whole-file scan -- that instrument is what failed twice, because the set of
# ways to say the wrong thing is unbounded and it could only enumerate three of
# them. They are asserted absent from ONE bounded text instead: the canonical
# every site is held to. That closes the escape a reviewer measured -- putting
# a banned literal INSIDE the canonical and letting all four sites follow it,
# which region equality alone cannot see because every site still agrees.
_SCOPE_PROSE_REFUTED = (
    "every object and every link on the chain is the one that was live",
    "every object and every link on it is the one that was live",
    "オブジェクトもリンクも、その時刻に生きていたものだけを使います",
)


def _normalize_prose(text: str) -> str:
    """Collapse every run of whitespace, so each site may re-wrap freely."""
    return " ".join(text.split())


def _squeeze(text: str) -> str:
    """Drop whitespace entirely -- for the JAPANESE measurement ties ONLY.

    Japanese has no inter-word spaces, so a JA line break normalizes to a
    space that exists nowhere in the sentence. A needle written to match the
    claim would then have to reproduce the site's line wrapping, and would red
    spuriously the day someone re-wraps it.

    It is deliberately NOT used for the English ties: dropping spaces there
    would let a broken word through (`payl oad` squeezes to `payload`), and
    `_normalize_prose` is strictly tighter with no cost, since English wraps
    on spaces the sentence already has. It is not used for region equality
    either -- there a re-wrap must be tolerated and nothing else.

    Which is true for English and NOT for Japanese, and T4 should know it: a
    JA line break normalizes to a space, so re-wrapping a JA region changes
    its normalized text and reds
    `test_every_marked_invariant_region_equals_its_canonical_text`. The JA
    sites must reproduce the canonical's exact line breaks. It fails safe --
    a spurious red, never a silent pass -- but it is a trap, not a feature.
    """
    return "".join(text.split())


_SCOPE_PROSE_RAW: dict[tuple[str, str], str] = {
    ("scope-asof-invariant", "en"): _SCOPE_ASOF_INVARIANT_EN,
    ("scope-asof-invariant", "ja"): _SCOPE_ASOF_INVARIANT_JA,
    ("scope-denied-consequences", "en"): _SCOPE_DENIED_CONSEQUENCES_EN,
    ("scope-denied-consequences", "ja"): _SCOPE_DENIED_CONSEQUENCES_JA,
}

_SCOPE_PROSE_CANON: dict[tuple[str, str], str] = {
    key: _normalize_prose(raw) for key, raw in _SCOPE_PROSE_RAW.items()
}


def _bullet_count(text: str) -> int:
    """Markdown list items in `text` -- the block shape normalization loses."""
    return sum(1 for line in text.splitlines() if line.lstrip().startswith("- "))

# Phrases distinctive enough that their presence IS the claim being made.
# Each is asserted to be a phrase of its own canonical, so they cannot drift
# away from the text they anchor.
_SCOPE_PROSE_ANCHORS: dict[tuple[str, str], tuple[str, ...]] = {
    ("scope-asof-invariant", "en"): (
        "as of the instant its own row closed",
        "read at its newest row",
    ),
    ("scope-asof-invariant", "ja"): (
        "target 自身の行が閉じた時刻の断面で",
        "ただし読むのは最新行です",
    ),
    ("scope-denied-consequences", "en"): (
        "each meets retirement at its OWN hop",
        "reads the scope key off the payload, which retirement",
    ),
    ("scope-denied-consequences", "ja"): (
        "`SelfScope` はオブジェクト自身の id を返します",
        "retire はこの id を奪いません",
    ),
}

# The `scope-` namespace, so this instrument does not collide with the
# `*-runnable` markers `tests/test_docs.py` already extracts from the same
# files -- but an unrecognised id INSIDE the namespace still fails.
_SCOPE_REGION_RE = re.compile(
    r"^[ \t]*<!-- (?P<id>scope-[a-z-]+):start -->[ \t]*$\n"
    r"(?P<body>.*?)"
    r"^[ \t]*<!-- (?P=id):end -->[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def _prose_corpus() -> list[Path]:
    """Every published prose file, derived by glob -- never a literal list.

    `tests/` is deliberately out: this module holds the canonical, so it
    states the claim by construction.

    `CHANGELOG.md` is out for a different reason: its released sections are a
    HISTORICAL record of what each version shipped, and a released section is
    never rewritten to match today's canonical. The copy of this region inside
    the 0.8.0 entry is therefore frozen history, not a live site. The live
    sites this instrument governs are `src/ontary/scope.py` and the two
    `docs/api-reference` pages; a new site still joins by glob.
    """
    return sorted(
        {
            *(path for path in _REPO_ROOT.glob("*.md") if path.name != "CHANGELOG.md"),
            *_REPO_ROOT.glob("docs/**/*.md"),
            *_REPO_ROOT.glob("src/ontary/**/*.py"),
        }
    )


def _language_of(path: Path) -> str:
    return "ja" if path.name.endswith(".ja.md") else "en"


def _marked_regions(path: Path) -> list[tuple[str, str]]:
    """(region id, raw body) for every marker pair in `path`."""
    return [
        (match.group("id"), match.group("body"))
        for match in _SCOPE_REGION_RE.finditer(path.read_text())
    ]


def _guarded_bodies(path: Path) -> list[str]:
    """The normalized text of `path` that IS under the canonical.

    Markdown carries markers. A `.py` site cannot: an HTML comment inside a
    docstring is what `help()` would print, and moving the text out to a
    named constant would leave the `def` with no literal docstring at all,
    which is exactly the surface (IDE hover) this site exists to protect. So
    a `.py` site is covered by verbatim containment of a canonical instead.
    """
    bodies = [_normalize_prose(body) for _, body in _marked_regions(path)]
    if path.suffix == ".py":
        source = _normalize_prose(path.read_text())
        for canonical in _SCOPE_PROSE_CANON.values():
            bodies.extend([canonical] * source.count(canonical))
    return bodies


def _interstitial(path: Path) -> str:
    """The normalized prose BETWEEN a file's guarded regions.

    Region equality can only see text a region contains. The measured escape
    it cannot see is a contradiction written next to a correct paragraph --
    so the gap between the first guarded region and the last is pinned too,
    by digest. That covers the neighbourhood without forcing four sites to
    share prose they do not state the same way.
    """
    normalized = _normalize_prose(path.read_text())
    spans: list[tuple[int, int]] = []
    for body in _guarded_bodies(path):
        assert normalized.count(body) == 1, (
            f"{path}: guarded body appears {normalized.count(body)}x -- a "
            "duplicate would silently collapse this file's interstitial"
        )
        start = normalized.find(body)
        spans.append((start, start + len(body)))
    spans.sort()
    return " ".join(
        normalized[left[1] : right[0]] for left, right in zip(spans, spans[1:], strict=False)
    )


# sha256 of `_interstitial(path)` for every file carrying more than one
# guarded region. A file that grows a region and is missing here FAILS -- the
# key set is checked against the derived one, never trusted as the list.
_SCOPE_INTERSTITIAL_DIGESTS: dict[str, str] = {
    # `CHANGELOG.md` is absent by design: `_prose_corpus` leaves it out, because
    # its released sections are frozen history rather than a live prose site.
    "docs/api-reference.ja.md": (
        "f921ef55b3f3f812fa454db4673bd63325d30eb7d6d7ca8a6e0d24aa6c4fd4df"
    ),
    "docs/api-reference.md": (
        "5ed033e51ded3f0452a18f42bcd328f18018bfbd0150325e71cc565386ed71a2"
    ),
    "src/ontary/scope.py": (
        "1815e8090f708f2cc259303e8855616c1feefae233d5ded1784b60a65dc404c5"
    ),
}


def _functions_declaring(flag: str) -> list[tuple[Path, str, str]]:
    """(path, function name, docstring) for every `src/` def taking `flag`."""
    found: list[tuple[Path, str, str]] = []
    for path in sorted(_REPO_ROOT.glob("src/ontary/**/*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            args = node.args
            declared = {
                arg.arg
                for arg in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if flag in declared:
                found.append((path, node.name, ast.get_docstring(node) or ""))
    return found


def test_every_marked_invariant_region_equals_its_canonical_text() -> None:
    """Every guarded region in the corpus is the canonical, whole.

    Whitespace-normalized equality, so a site may wrap to its own width and
    indent -- an EN doc paragraph, a two-space-indented CHANGELOG bullet and
    a four-space-indented docstring are the same text here. Nothing else is
    normalized: a swapped dash, an added clause, a colon for a semicolon all
    red, which is what a substring blocklist could never do.

    The file set is derived by glob, so a FIFTH site that carries the markers
    is covered the moment it exists, and an unknown region id fails rather
    than being skipped.
    """
    violations: list[str] = []
    for path in _prose_corpus():
        language = _language_of(path)
        for region_id, body in _marked_regions(path):
            key = (region_id, language)
            canonical = _SCOPE_PROSE_CANON.get(key)
            rel = path.relative_to(_REPO_ROOT)
            if canonical is None:
                violations.append(f"{rel}: unknown guarded region {key}")
                continue
            if _normalize_prose(body) != canonical:
                violations.append(f"{rel}: region {region_id} drifted from canonical")
    assert not violations, "invariant prose drifted:\n" + "\n".join(violations)


def test_every_canonical_invariant_region_is_realized_in_the_corpus() -> None:
    """A canonical no site states any more is a deletion, and reds here.

    This is the half `test_every_marked_invariant_region_equals_its_canonical
    _text` cannot see: removing a region's markers AND its text leaves that
    file simply not mentioning the claim, which equality alone reads as
    "nothing to check".
    """
    realized = {
        (region_id, _language_of(path))
        for path in _prose_corpus()
        for region_id, _ in _marked_regions(path)
    }
    for path in _prose_corpus():
        if path.suffix != ".py":
            continue
        source = _normalize_prose(path.read_text())
        realized.update(
            key for key, canonical in _SCOPE_PROSE_CANON.items() if canonical in source
        )
    missing = sorted(set(_SCOPE_PROSE_CANON) - realized)
    assert not missing, f"canonical invariant text no site states: {missing}"


def test_translated_reference_pages_carry_the_same_invariant_regions() -> None:
    """`X.ja.md` must guard exactly the regions `X.md` guards.

    The Japanese caveat cannot be string-equal to the English one, so its
    presence is derived from its English twin instead of listed. Deleting the
    JA region -- markers and all -- reds here even though no equality check
    could have noticed.

    The pairs are discovered by filename, so a future translated page is
    held to the same rule without being added anywhere.
    """
    for english in sorted(_REPO_ROOT.glob("docs/*.md")):
        if english.name.endswith(".ja.md"):
            continue
        translated = english.with_name(english.stem + ".ja.md")
        if not translated.exists():
            continue
        en_ids = {region_id for region_id, _ in _marked_regions(english)}
        ja_ids = {region_id for region_id, _ in _marked_regions(translated)}
        assert en_ids == ja_ids, (
            f"{english.name} guards {sorted(en_ids)} but "
            f"{translated.name} guards {sorted(ja_ids)}"
        )


def test_every_include_retired_signature_documents_the_invariant() -> None:
    """The flag's contract is documented AT the flag, on every def that takes it.

    This is what makes `src/ontary/scope.py` a member of the site set without
    being named as one: the set is every function in `src/` whose signature
    declares `include_retired`, read out of the AST. A second function
    growing the flag joins the set, and reds until it documents it.
    """
    english = [
        canonical
        for (_, language), canonical in _SCOPE_PROSE_CANON.items()
        if language == "en"
    ]
    declaring = _functions_declaring("include_retired")
    assert declaring, "no function declares `include_retired` -- the set went empty"
    for path, name, docstring in declaring:
        normalized = _normalize_prose(docstring)
        for canonical in english:
            assert normalized.count(canonical) == 1, (
                f"{path.relative_to(_REPO_ROOT)}::{name} does not carry the "
                "canonical invariant text exactly once"
            )


def test_the_prose_between_the_guarded_regions_is_pinned_too() -> None:
    """A contradiction written NEXT TO a correct paragraph reds here.

    Measured this session: the two escapes the deleted blocklist let through
    both red on region equality when the wrong claim lands inside a region,
    and both stayed green when it landed in the paragraph next door. So the
    interstitial -- everything between a file's first guarded region and its
    last -- is pinned by digest as well. Re-approving prose there means
    updating the digest, which is the same re-approval the canonical asks
    for.

    The file set is derived, not listed: a file that grows a second guarded
    region and has no digest here fails rather than being skipped.
    """
    derived = {
        str(path.relative_to(_REPO_ROOT)): _interstitial(path)
        for path in _prose_corpus()
        if len(_guarded_bodies(path)) > 1
    }
    assert set(derived) == set(_SCOPE_INTERSTITIAL_DIGESTS), (
        "files carrying guarded regions changed; pinned digests cover "
        f"{sorted(_SCOPE_INTERSTITIAL_DIGESTS)} but the corpus has "
        f"{sorted(derived)}"
    )
    drifted = [
        name
        for name, text in derived.items()
        if hashlib.sha256(text.encode()).hexdigest()
        != _SCOPE_INTERSTITIAL_DIGESTS[name]
    ]
    assert not drifted, (
        "prose between the guarded invariant regions changed in "
        f"{drifted} -- re-approve it against the measured outcome and update "
        "`_SCOPE_INTERSTITIAL_DIGESTS`"
    )


# The canonical is a literal in THIS file, and so is every sentence the
# measurement ties below restate. One wrapping-tolerant search-and-replace
# over the prose therefore rewrites the claim and its restatement in the same
# sweep -- measured by a reviewer, who negated the newest-row sentence at the
# canonical, the three EN sites AND the needle at once and left every tie
# green. Prose guards prose, so prose can sweep them together.
#
# A digest is not prose. No text edit to a sentence updates a hex string, so
# re-approving a canonical means recomputing this number ON PURPOSE, which is
# exactly the deliberate act the ledger asks for ("re-approved against a
# measured outcome"). This and the interstitial pin are the two rules here a
# coordinated prose edit cannot satisfy by construction; every other rule
# compares prose against prose.
#
# Taken over the RAW literal, so a whitespace-only reflow -- which normalizes
# to the same string and would keep a normalized digest -- also reds.
_SCOPE_PROSE_DIGESTS: dict[tuple[str, str], str] = {
    ("scope-asof-invariant", "en"): (
        "5c89c972d6277dd57a15fad6c2cfc0ae8fe90e8fa5c6216e139d77a955501c41"
    ),
    ("scope-asof-invariant", "ja"): (
        "bec39565c433fcaf0c186a22b5a2d04a8b7a462a2e9109e7b43c5a17790939b2"
    ),
    ("scope-denied-consequences", "en"): (
        "cf29ba4f4b745c88010ae9a599d60b9bcb87365b37b72993bac9f39d4f187dea"
    ),
    ("scope-denied-consequences", "ja"): (
        "eda59ca6de0ea3dd6ca30133248f024a930c7173ff04a4362eb1deb077dd2047"
    ),
}


def test_every_canonical_is_pinned_by_a_digest_no_text_sweep_can_update() -> None:
    """Any change to a canonical LITERAL reds, however many sites move with it.

    Every prose rule in this instrument compares prose against prose, so one
    search-and-replace can satisfy them all at once while inverting the claim
    -- not a hypothetical, a reviewer's receipt. This is the backstop: the
    sweep has to stop and a human has to re-derive the number from a fresh
    measurement.

    Pinned over the RAW literal in `_SCOPE_PROSE_RAW`, not over its normalized
    form. Normalizing first would leave every whitespace-only restructuring
    green -- measured: flattening the consequences bullets to one line in the
    canonical AND reflowing all four sites to match kept the normalized digest
    character-for-character identical, destroying the published list at every
    site with nothing red, because
    `test_every_guarded_region_keeps_the_canonical_block_shape` derives its
    expectation from the same literal the edit changed. Over the raw literal
    that mutation reds here. The normalized text is a function of the raw, so
    this pin is strictly the stronger of the two.

    The key set is checked against the canonical map, so a canonical added
    without a digest fails rather than being silently unpinned.
    """
    assert set(_SCOPE_PROSE_DIGESTS) == set(_SCOPE_PROSE_RAW), (
        "every canonical must be digest-pinned; pinned "
        f"{sorted(_SCOPE_PROSE_DIGESTS)} vs canonical {sorted(_SCOPE_PROSE_RAW)}"
    )
    drifted = [
        key
        for key, raw in _SCOPE_PROSE_RAW.items()
        if hashlib.sha256(raw.encode()).hexdigest() != _SCOPE_PROSE_DIGESTS[key]
    ]
    assert not drifted, (
        f"canonical invariant text changed for {drifted} -- re-derive the claim "
        "from a measured outcome, then update `_SCOPE_PROSE_DIGESTS`"
    )


# Spelled counts, so the number the prose states can be DERIVED from the
# union rather than compared against a digit written by hand twice.
_COUNT_WORDS = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven")
# Japanese may write the count either way, so the guard accepts both rather
# than forcing the translation to keep an ASCII digit.
_COUNT_KANJI = ("零", "一", "二", "三", "四", "五", "六", "七")


def test_the_published_consequences_name_every_declared_scope_rule_kind() -> None:
    """The prose's member list is read out of `ScopeRule`, like the matrix's.

    T4 regrouped the published consequences by RULE KIND -- one bullet per
    kind, saying what retirement does to that hop -- because the
    old grouping (one bullet per lifecycle event) forced quantifiers the
    measurement does not support. A list per kind is only honest while it is
    the WHOLE kind list, and "a hand-written subset of a declared set" is the
    single defect shape this branch exists to close: three rounds shipped
    prose over three of the four shapes.

    So the set is derived here, never trusted. Every member of
    `get_args(ScopeRule)` must be named in the published English text, the
    count the English states must be the declared count, and the Japanese
    must state the same number. Declare a fifth kind and this reds on the
    same run as
    `test_the_lifecycle_matrix_covers_every_declared_scope_rule_kind` --
    before prose quantified over "four kinds" can ship against a union of
    five.

    It is the prose half of that matrix guard, and the reason the canonical
    may say "Four kinds are declared" at all.
    """
    kinds = get_args(ScopeRule)
    english = _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    japanese = _SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")]

    for language, published in (("en", english), ("ja", japanese)):
        unnamed = [
            kind.__name__
            for kind in kinds
            if f"`{kind.__name__}`" not in published
        ]
        assert not unnamed, (
            f"the published {language} consequences name no declared rule "
            f"kind {unnamed} -- prose over a subset of the declared set is "
            "what this round closes, in BOTH languages"
        )
    assert len(kinds) < min(len(_COUNT_WORDS), len(_COUNT_KANJI)), (
        f"{len(kinds)} declared kinds is past the spelled range -- extend "
        "BOTH `_COUNT_WORDS` and `_COUNT_KANJI` rather than letting either "
        "lookup raise"
    )
    assert f"{_COUNT_WORDS[len(kinds)]} rule kinds are declared" in english, (
        f"the English must state the declared count ({len(kinds)})"
    )
    ja_counts = (str(len(kinds)), _COUNT_KANJI[len(kinds)])
    assert any(
        f"宣言されているルールの種類は {form} つ" in japanese
        or f"宣言されているルールの種類は{form}つ" in japanese
        for form in ja_counts
    ), f"the Japanese must state the declared count ({len(kinds)})"


def test_the_canonical_does_not_restate_what_the_engine_refutes() -> None:
    """The canonical itself cannot carry the sentences the engine disproves.

    Measured by a reviewer: writing a banned literal INSIDE the canonical and
    letting all four sites follow it -- the workflow this design mandates --
    left every other test green, because each site still agreed with the
    canonical and with the others. Region equality is blind by construction to
    a change made at the source.

    So the three literals the retired blocklist banned are asserted absent
    HERE, from one bounded text, rather than scanned for across whole files.
    That is a different instrument from the one that failed: the old guard
    claimed those three strings stood in for an unbounded set of wrong
    phrasings, and `1815 passed` with the overclaim re-added in new words is
    the receipt that they did not. This assertion claims only what it can
    deliver -- that these exact previously-shipped sentences cannot be
    re-approved into the canonical without someone deleting this test.

    The general case is still region equality's job. This is the floor under
    it, and it is what restores L31 subsumption for the in-region case at all
    four sites at once.
    """
    for key, canonical in _SCOPE_PROSE_CANON.items():
        for refuted in _SCOPE_PROSE_REFUTED:
            assert refuted not in canonical, (
                f"{key} restates a claim the engine refutes: {refuted!r} -- see "
                "`test_ancestor_payload_staleness_is_measured_and_documented_"
                "the_same_way`, which measures the opposite"
            )


def test_every_guarded_region_keeps_the_canonical_block_shape() -> None:
    """Whitespace normalization is blind to Markdown block structure.

    Measured by a reviewer: reflowing the consequences bullets into one
    paragraph -- destroying the published list -- normalizes to the identical
    string and stays green. So the list shape is checked separately, derived
    from the canonical's own line structure rather than pinned as a number.
    """
    for path in _prose_corpus():
        language = _language_of(path)
        for region_id, body in _marked_regions(path):
            expected = _bullet_count(_SCOPE_PROSE_RAW[(region_id, language)])
            assert _bullet_count(body) == expected, (
                f"{path.relative_to(_REPO_ROOT)}: region {region_id} has "
                f"{_bullet_count(body)} list items, canonical has {expected}"
            )
    expected_py = sum(
        _bullet_count(raw)
        for (_, language), raw in _SCOPE_PROSE_RAW.items()
        if language == "en"
    )
    for path, name, docstring in _functions_declaring("include_retired"):
        assert _bullet_count(docstring) == expected_py, (
            f"{path.relative_to(_REPO_ROOT)}::{name} has "
            f"{_bullet_count(docstring)} list items, canonical has {expected_py}"
        )


def test_every_invariant_anchor_is_a_phrase_from_its_own_canonical() -> None:
    """An anchor that is not in the canonical would guard nothing."""
    for key, anchors in _SCOPE_PROSE_ANCHORS.items():
        for anchor in anchors:
            assert anchor in _SCOPE_PROSE_CANON[key], (key, anchor)
    assert set(_SCOPE_PROSE_ANCHORS) == set(_SCOPE_PROSE_CANON)


def test_invariant_anchors_appear_only_inside_a_guarded_region() -> None:
    """No file may state the claim outside the instrument.

    This is the rule that derives MEMBERSHIP from content rather than from a
    list: a fifth site that copies the paragraph carries its anchors with it,
    so it reds until it is marked and made canonical. It is the inverse of
    the blocklist that failed -- the phrases enumerated here are the
    canonical's OWN, a bounded set that cannot go stale, not a guess at the
    unbounded set of ways to say the wrong thing.
    """
    violations: list[str] = []
    for path in _prose_corpus():
        source = _normalize_prose(path.read_text())
        guarded = _guarded_bodies(path)
        for anchors in _SCOPE_PROSE_ANCHORS.values():
            for anchor in anchors:
                in_file = source.count(anchor)
                in_regions = sum(body.count(anchor) for body in guarded)
                if in_file != in_regions:
                    violations.append(
                        f"{path.relative_to(_REPO_ROOT)}: {anchor!r} appears "
                        f"{in_file}x but only {in_regions}x inside a guarded region"
                    )
    assert not violations, "invariant claim stated outside the instrument:\n" + "\n".join(
        violations
    )


def test_ancestor_payload_staleness_is_measured_and_documented_the_same_way() -> None:
    """The prose about the ancestor frame is pinned to a MEASURED outcome.

    Docs are executable in this repo for API names, the error-code table,
    snippets and version strings -- but nothing read this paragraph, and the
    paragraph is load-bearing: it tells an ontology author which operator the
    gate will admit after a retirement. Inverting it in all three files to say
    the engine resolves "entirely in the present tense" left the whole suite
    green, which is how the same claim shipped false in three earlier rounds.

    This test owns the MEASUREMENT half only: what the engine does when an
    ancestor's scope key changes after the target closed. The prose half is
    `_SCOPE_PROSE_CANON` above, which all four sites are held to whole. The
    tie between them is the assertion at the end: the canonical must contain,
    word for word, the whole sentence stating the outcome measured here. A
    fragment would not do -- "read at its newest row" survives inside "but it
    is NOT read at its newest row", so a substring tie certifies nothing.

    That tie is prose checking prose, so one search-and-replace can rewrite
    the canonical AND this restatement together; measured, and green. What
    stops it is
    `test_every_canonical_is_pinned_by_a_digest_no_text_sweep_can_update`,
    which pins a number no prose edit can touch.

    What this test does NOT do, though the docstring here used to claim it:
    rewording the promise back into a full point-in-time guarantee does not
    red THIS test. It reds
    `test_every_marked_invariant_region_equals_its_canonical_text`, and only
    when the reworded text lands inside a guarded region. A contradictory
    sentence placed in a NEIGHBOURING paragraph reds
    `test_the_prose_between_the_guarded_regions_is_pinned_too` -- but only
    BETWEEN two guarded regions, which is every site today. Before the first
    region or after the last, only
    `test_invariant_anchors_appear_only_inside_a_guarded_region` can catch it,
    and only if the sentence reuses an anchor phrase.
    """
    ontology, store = _payload_ancestor_client()
    store.insert("Company", {"id": "co-old"}, SRC)
    store.insert("Company", {"id": "co-new"}, SRC)
    store.insert("Team", {"id": "team-1", "company": "co-old"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("onTeam", "worker-1", "team-1")

    old_operator = _company_operator(ontology, store, "co-old")
    assert old_operator.execute("OffboardWorker", {"worker_id": "worker-1"}) == {
        "worker_id": "worker-1"
    }

    worker = store.read_last("Worker", "worker-1")
    assert worker is not None
    asof = worker.lineage.valid_to
    assert asof is not None

    # The team moves to another company AFTER the worker's row closed.
    store.update("Team", "team-1", {"id": "team-1", "company": "co-new"}, SRC)

    # MEASURED, through the gate itself: the ancestor answers with the scope
    # it is in NOW. The company that owned the worker at `asof` is denied, and
    # the company that never owned it resolves and reaches the handler's own
    # refusal. This is the approximation the prose must describe.
    with raises_code(PermissionDenied, "SCOPE_DENIED"):
        old_operator.execute("OffboardWorker", {"worker_id": "worker-1"})

    new_operator = _company_operator(ontology, store, "co-new")
    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        new_operator.execute("OffboardWorker", {"worker_id": "worker-1"})

    # DOCUMENTED: the canonical carries, VERBATIM, the whole sentence stating
    # the outcome measured above -- not a phrase of it. A phrase survives its
    # own negation ("but it is NOT read at its newest row" still contains
    # "read at its newest row"), which is how a fragment check certifies
    # nothing; the whole sentence does not. Equality of the four sites against
    # this canonical is `test_every_marked_invariant_region_equals_its_
    # canonical_text`, so a canonical edited away from the measured outcome
    # reds HERE, next to the measurement that refutes it, and the four sites
    # red there.
    #
    # TWO bounds, both measured, neither hidden. (1) A negation written in
    # entirely new words shares no sentence with the canonical and passes.
    # (2) The needle below is a literal in the same file as the canonical, so
    # ONE search-and-replace can rewrite both and this assertion alone would
    # not notice -- do not update it when re-approving the canonical; re-derive
    # it from a fresh measurement. The rule that catches the sweep regardless
    # is `test_every_canonical_is_pinned_by_a_digest_no_text_sweep_can_update`,
    # because a hex digest is not prose. Both bounds are in the task note.
    assert (
        _normalize_prose(
            """An ancestor object is admitted when it had not already been
            retired by then — but it is read at its newest row, so its
            payload, and any `DirectProperty` key taken off that payload, is
            the one it carries now: an ancestor updated after the instant
            answers with the scope it is in today, not the one it was in
            then."""
        )
        in _SCOPE_PROSE_CANON[("scope-asof-invariant", "en")]
    )
    assert (
        _squeeze(
            """祖先のオブジェクトは、その時刻に retire 済みでなければ採用します。
            ただし読むのは最新行です。したがって payload、および payload から
            読む `DirectProperty` のキーは、その祖先が「いま」持っている値です。
            その時刻より後に更新された祖先は、当時のスコープではなく現在の
            スコープを答えます。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-asof-invariant", "ja")])
    )


# --- The lifecycle matrix, derived from the declared rule-kind set ---------
#
# `scope.py`'s `ScopeRule` union IS the set of shapes an author may declare.
# Every lifecycle pin above enumerates a hand-written subset of it, and every
# round of this branch closed the gap by adding the one member that was
# missing.
#
# So the case table below is checked against the union itself rather than
# against a reviewer's memory: declare a fifth kind and
# `test_the_lifecycle_matrix_covers_every_declared_scope_rule_kind` reds on
# that same run, before any prose quantified over "every shape" can ship.


class _KindCase(NamedTuple):
    """How to build a `Worker` scoped by one rule kind.

    `scope_id` and `linked` are not free choices -- both follow from the
    rule, for the reasons `_scoped_lifecycle_client` gives.
    """

    rule: Any
    scope_id: str
    linked: bool


# The backend axis. `test_actions.py` had none before this matrix: every pin
# in the file ran on SQLite alone, so nothing here could ever have caught a
# gate that resolved differently per backend -- which is exactly the class of
# defect T1 found in the link ORDER BY.
#
# `test_store_conformance.py` keeps its own copy of
# this factory set with its own schema prefix; conftest's `make_store` is a
# fixture and cannot be used at module scope for `params=`. This follows that
# established shape rather than refactoring two large files from inside a
# test-instrument task.
_ACTIONS_POSTGRES_DSN = os.environ.get("ONTARY_TEST_POSTGRES_DSN")
_actions_schema_counter = itertools.count()


def _make_actions_postgres_store(registry: OntologyRegistry) -> Store:
    import psycopg

    from ontary.store.postgres import PostgresStore

    assert _ACTIONS_POSTGRES_DSN is not None
    schema = f"ontary_test_actions_{next(_actions_schema_counter)}"
    with psycopg.connect(_ACTIONS_POSTGRES_DSN, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{schema}"')
    separator = "&" if "?" in _ACTIONS_POSTGRES_DSN else "?"
    return PostgresStore(
        registry,
        f"{_ACTIONS_POSTGRES_DSN}{separator}options=-csearch_path%3D{schema}",
    )


_LIFECYCLE_BACKENDS: dict[str, Callable[[OntologyRegistry], Store]] = {
    "sqlite": ObjectStore,
    "in_memory": InMemoryStore,
}
if _ACTIONS_POSTGRES_DSN:
    _LIFECYCLE_BACKENDS["postgres"] = _make_actions_postgres_store


_LIFECYCLE_KIND_CASES: dict[Any, _KindCase] = {
    SelfScope: _KindCase(SelfScope(level="team"), "worker-1", False),
    DirectProperty: _KindCase(
        DirectProperty(level="team", property_name="team"), "team-1", False
    ),
    ViaLink: _KindCase(
        ViaLink(link_api_name="onTeam", direction="from", parent_type="Team"),
        "team-1",
        True,
    ),
    CustomResolver: _KindCase(
        CustomResolver(level="team", fn=_resolver_reading_the_live_row),
        "team-1",
        False,
    ),
}

_LIFECYCLE_SUBJECTS = ("target", "ancestor")
_LIFECYCLE_OPS = ("retired",)

# The MEASURED outcome of every cell, taken on all three backends before any
# of it was written down. Each value is what the owning operator gets when it
# re-runs the action after the lifecycle event: the handler's own refusal
# means the gate ADMITTED it, `SCOPE_DENIED` means the gate turned it away.
#
# Two cells are worth reading twice, because neither is obvious:
#
#   * every `ancestor` cell is ADMITTED, but only ONE of the four is
#     EVIDENCE about the ancestor, and the column must not be read as though
#     all four were. `ViaLink` is the only kind that resolves the ancestor
#     object at all: traced on entry to `_resolve_level`, `Team/team-1`
#     appears only in the `ViaLink`/`ancestor` cell. `SelfScope`
#     answers from the target's own id and `DirectProperty` from its payload
#     column, so neither ever reads the `Team` ROW -- their `ancestor`
#     cells re-record the TARGET-hop outcome under another name and are
#     structurally unobservable on this axis, exactly as `CustomResolver`'s
#     is (next bullet). For the cell that DOES observe it: the
#     ancestor is read as-of the instant the target closed, it was live
#     then, and retiring it afterwards cannot reach back.
#     Measured rather than argued -- a liveness defect confined to the
#     ancestor reds exactly the `ViaLink`/`ancestor` rows and none of the
#     others. Note what that means for a mutation that reds the whole
#     column: every cell here retires the TARGET first, so breaking
#     `_live_at` outright reds through the target hop as well and does NOT
#     isolate this axis (L18: THIS assertion must red). These counts are
#     OFFLINE ones, taken with no `ONTARY_TEST_POSTGRES_DSN`: the Postgres
#     backend adds a third row per cell. The conclusion is the row SHAPE,
#     not the total.
#   * both `CustomResolver` cells DENY, and the ancestor one denies for
#     a reason that has nothing to do with the ancestor -- see the
#     name-one-input note on the matrix test.
_LIFECYCLE_MATRIX: dict[tuple[str, str, str], tuple[type[Any], str]] = {
    ("SelfScope", "target", "retired"): (ConflictError, "OBJECT_ALREADY_RETIRED"),
    ("SelfScope", "ancestor", "retired"): (ConflictError, "OBJECT_ALREADY_RETIRED"),
    ("DirectProperty", "target", "retired"): (ConflictError, "OBJECT_ALREADY_RETIRED"),
    ("DirectProperty", "ancestor", "retired"): (
        ConflictError,
        "OBJECT_ALREADY_RETIRED",
    ),
    ("ViaLink", "target", "retired"): (ConflictError, "OBJECT_ALREADY_RETIRED"),
    ("ViaLink", "ancestor", "retired"): (ConflictError, "OBJECT_ALREADY_RETIRED"),
    ("CustomResolver", "target", "retired"): (PermissionDenied, "SCOPE_DENIED"),
    ("CustomResolver", "ancestor", "retired"): (PermissionDenied, "SCOPE_DENIED"),
}


def test_the_lifecycle_matrix_covers_every_declared_scope_rule_kind() -> None:
    """The matrix's member list is READ OUT of `ScopeRule`, never written.

    This is the guard the previous rounds lacked. A hand-written tuple of
    shapes cannot fail when the union grows -- it just silently covers less
    of it, which is how three rounds each shipped a claim quantified over
    "every shape" while one shape did the opposite.
    """
    assert set(_LIFECYCLE_KIND_CASES) == set(get_args(ScopeRule))


def test_the_lifecycle_matrix_records_an_outcome_for_every_cell() -> None:
    """Every cell of the product is MEASURED, and the product is derived.

    The sibling above pins the fixture table; this pins the expectations.
    Both derive from `get_args(ScopeRule)` independently, so declaring a
    fifth kind reds here even if someone extends the fixture table alone --
    the failure names the cells nobody has measured yet.
    """
    assert set(_LIFECYCLE_MATRIX) == {
        (kind.__name__, subject, op)
        for kind in get_args(ScopeRule)
        for subject in _LIFECYCLE_SUBJECTS
        for op in _LIFECYCLE_OPS
    }


@pytest.mark.parametrize("backend", list(_LIFECYCLE_BACKENDS))
@pytest.mark.parametrize("op", _LIFECYCLE_OPS)
@pytest.mark.parametrize("subject", _LIFECYCLE_SUBJECTS)
@pytest.mark.parametrize(
    "kind", list(_LIFECYCLE_KIND_CASES), ids=lambda k: str(k.__name__)
)
def test_lifecycle_outcome_is_the_measured_one_on_every_backend(
    kind: Any, subject: str, op: str, backend: str
) -> None:
    """One row per (rule kind x lifecycled object x lifecycle op x backend).

    The rule kinds come out of `ScopeRule`; the expectations come out of a
    table that must cover the same product. So this cannot silently shrink:
    a fifth kind multiplies the rows and reds at the lookup below with the
    cells that were never measured, instead of quietly testing four fifths
    of the promise.

    Name one breaking input this fixture structurally CANNOT construct: a
    `CustomResolver` whose body survives its target's retirement. Every
    `CustomResolver` cell here denies at the TARGET hop, because the shipped
    body reads `read_current` and the target is retired in every cell -- so
    the `ancestor` cell is masked and this fixture cannot observe the
    ancestor axis for that kind at all.

    And a SECOND input it cannot construct, which matters more because the
    matrix looks like it covers the case: an ANCESTOR whose own hop reads
    the payload. `_scoped_lifecycle_client` declares `Team` as
    `SelfScope`-scoped and that is not parametrized, so every ancestor in
    this matrix answers from its own id. So the `ancestor` column here says
    "admitted" about ONE ancestor shape, not about ancestors; prose
    quantified over "every ancestor" is not supported by this table.
    """
    rule, scope_id, linked = _LIFECYCLE_KIND_CASES[kind]
    expected = _LIFECYCLE_MATRIX.get((kind.__name__, subject, op))
    assert expected is not None, (
        f"no measured outcome recorded for {kind.__name__}/{subject}/{op} -- "
        "a newly declared rule kind must be MEASURED, never assumed"
    )
    error_cls, code = expected

    client, store = _scoped_lifecycle_client(
        rule,
        scope_id=scope_id,
        linked=linked,
        make_store=_LIFECYCLE_BACKENDS[backend],
    )
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1", "team": "team-1"}, SRC)
    if linked:
        store.create_link("onTeam", "worker-1", "team-1")

    assert client.execute("OffboardWorker", {"worker_id": "worker-1"}) == {
        "worker_id": "worker-1"
    }

    if subject == "ancestor":
        store.retire_object("Team", "team-1")

    with raises_code(error_cls, code):
        client.execute("OffboardWorker", {"worker_id": "worker-1"})

    assert store.audit_entries()[-1].outcome == "denied"


def test_retired_vialink_target_resolves_when_the_cascade_shares_its_timestamp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retire cascade routinely closes links in the object's OWN tick.

    `ActionContext.retire` closes the object and then its links, each on its
    own `_utcnow_iso()` call, so the link's `valid_to` is normally a hair
    LATER than the object's -- which an as-of read with an exclusive upper
    bound would still include, by luck. It is luck: two consecutive
    `datetime.now()` calls collide at microsecond resolution a large
    fraction of the time, and then link `valid_to` EQUALS object `valid_to`
    exactly. An exclusive bound drops the link in precisely those runs, so
    the sibling pin above would fail intermittently rather than never.

    Freezing the clock makes that collision certain instead of likely, which
    is the only way to pin the boundary rather than the timing.

    The same frozen tick is then used for the ANCESTOR: `Team` is retired at
    the instant the worker's own row closed, which is the case the published
    "including in the same cascade tick" bullet promises still resolves. That
    half had no measurement under it before T4.
    """
    frozen = "2026-08-28T09:00:00+00:00"
    monkeypatch.setattr(ontary.store.sqlite, "_utcnow_iso", lambda: frozen)

    client, store = _vialink_lifecycle_client()
    store.insert("Team", {"id": "team-1"}, SRC)
    store.insert("Worker", {"id": "worker-1"}, SRC)
    store.create_link("onTeam", "worker-1", "team-1")

    client.execute("OffboardWorker", {"worker_id": "worker-1"})
    retired = store.read_last("Worker", "worker-1")
    assert retired is not None
    # The case this test exists for: object and link closed in the same tick.
    assert retired.lineage.valid_to == frozen
    assert store.links_from_asof("onTeam", "worker-1", frozen) == ["team-1"]

    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        client.execute("OffboardWorker", {"worker_id": "worker-1"})
    assert store.audit_entries()[-1].outcome == "denied"

    # The ANCESTOR closed in that same tick, which is the half of the shipped
    # bullet nothing measured before T4. `_live_at`'s upper bound is
    # INCLUSIVE, so a team whose `valid_to` EQUALS the worker's `asof` is
    # still admitted and the worker's rightful owner still reaches the
    # handler's refusal. A strict `valid_to > asof` drops it and denies that
    # owner -- and the frozen clock is what makes the equality certain rather
    # than a microsecond-collision coin flip, exactly as for the link above.
    store.retire_object("Team", "team-1")
    team = store.read_last("Team", "team-1")
    assert team is not None
    assert team.lineage.valid_to == frozen == retired.lineage.valid_to

    with raises_code(ConflictError, "OBJECT_ALREADY_RETIRED"):
        client.execute("OffboardWorker", {"worker_id": "worker-1"})
    assert store.audit_entries()[-1].outcome == "denied"

    # DOCUMENTED: the whole sentence the canonical uses to promise it, in
    # both languages, next to the measurement that warrants it. Before T4
    # this claim was digest-pinned prose with no engine tie under it -- it
    # could not drift silently, but nothing said it was true. Re-derive it
    # from this measurement rather than editing it to match a reworded
    # canonical.
    assert (
        _normalize_prose(
            """One retired after the target — including in the same cascade
            tick — still answers for it."""
        )
        in _SCOPE_PROSE_CANON[("scope-denied-consequences", "en")]
    )
    assert (
        _squeeze(
            """target より後に retire された親は、同じ cascade の tick で閉じた
            ものも含めて、いまも target のために解決します。"""
        )
        in _squeeze(_SCOPE_PROSE_CANON[("scope-denied-consequences", "ja")])
    )


def test_unlink_missing_live_link_is_coded_and_audited_as_denied() -> None:
    client, store = _lifecycle_client()
    store.insert("Employee", {"id": "employee-1"}, SRC)
    store.insert("Department", {"id": "department-1"}, SRC)

    with raises_code(ValidationFailed, "LINK_NOT_FOUND"):
        client.execute(
            "UnassignEmployee",
            {"employee_id": "employee-1", "department_id": "department-1"},
        )

    entry = store.audit_entries()[-1]
    assert entry.action == "UnassignEmployee"
    assert entry.outcome == "denied"
    assert entry.writes == []


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

    `capability` is on this list as of M5 (spec `governed-effects` AC6). It
    does not weaken the guard: it hands back an author-supplied provider
    object, never the store and never the guarded query layer. What this test
    still forbids is the ENGINE
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
        "insert",
        "links_from",
        "links_to",
        "read_all",
        "read_current",
        "retire",
        "unlink",
        "update",
    }
    assert "retire_object" not in public_attrs
    assert "close_link" not in public_attrs
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


def test_action_context_retire_requires_action_transaction(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    store.insert("Book", {"id": "book-1"}, SRC)
    ctx = ActionContext(
        store,
        Source(source_system="action:Test"),
        _librarian(),
        registry=registry,
    )

    with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
        ctx.retire("Book", "book-1")

    assert store.read_current("Book", "book-1") is not None


def test_action_context_unlink_requires_action_transaction(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    registry = make_registry(
        object_types=ACTION_OBJECT_TYPES,
        link_types=ACTION_LINK_TYPES,
        action_types=ACTION_TYPES,
    )
    store = make_store(registry)
    store.create_link("onShelf", "book-1", "shelf-1")
    ctx = ActionContext(
        store,
        Source(source_system="action:Test"),
        _librarian(),
        registry=registry,
    )

    with raises_code(ConflictError, "CALLER_TRANSACTION_REFUSED"):
        ctx.unlink("onShelf", "book-1", "shelf-1")

    assert store.links_from("onShelf", "book-1") == ["shelf-1"]


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
