"""T5 provider plumbing: copied client-local maps on every Python path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from unittest.mock import patch

from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.client import OntologyClient, OntologyRuntime
from ontary.errors import ValidationFailed
from ontary.functions import BoundQuery
from ontary.security import Consumer
from ontary.store import ObjectStore


class _Reader(Protocol):
    def read(self) -> str: ...


class _Provider:
    def __init__(self, value: str) -> None:
        self.value = value

    def read(self) -> str:
        return self.value


ConsumerFactory = Callable[..., Consumer]


def _provider_ontology() -> tuple[Ontology, dict[str, Any]]:
    ontology = Ontology("providers", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    read_primary = ontology.capability(_Reader, name="readPrimary")
    read_secondary = ontology.capability(_Reader, name="readSecondary")

    class RunParams(ActionParams):
        pass

    @ontology.action(
        RunParams,
        target=Record,
        roles=["Operator"],
        capabilities=[read_primary, read_secondary],
        api_name="Run",
    )
    def run(_ctx: ActionContext, _params: RunParams) -> dict[str, str]:
        return {"status": "ok"}

    @ontology.function(
        api_name="boundProviders",
        capabilities=[read_primary, read_secondary],
    )
    def bound_providers(query: BoundQuery, _params: dict[str, Any]) -> dict[str, Any]:
        return {"capabilities": dict(query._capability_providers)}

    ontology.validate()
    return ontology, {
        "read_primary": read_primary,
        "read_secondary": read_secondary,
    }


def test_bind_copies_maps_and_for_consumer_overrides_per_key(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, handles = _provider_ontology()
    store = ObjectStore(ontology.registry)
    primary = _Provider("runtime-primary")
    secondary = _Provider("runtime-secondary")
    replacement = _Provider("mutated-after-bind")
    capability_defaults = {
        handles["read_primary"]: primary,
        handles["read_secondary"]: secondary,
    }

    runtime = ontology.bind(
        store,
        capabilities=capability_defaults,
    )
    capability_defaults[handles["read_primary"]] = replacement

    override = _Provider("consumer-primary")
    capability_overrides = {handles["read_primary"]: override}
    client = runtime.for_consumer(
        make_consumer(
            actor_id="one",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        capabilities=capability_overrides,
    )
    capability_overrides[handles["read_primary"]] = replacement

    bound = client.call_function("boundProviders", {})
    assert bound["capabilities"] == {
        handles["read_primary"]: override,
        handles["read_secondary"]: secondary,
    }


def test_shared_runtime_clients_interleave_without_provider_crosstalk(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, handles = _provider_ontology()
    runtime = ontology.bind(
        ObjectStore(ontology.registry),
        capabilities={handles["read_secondary"]: _Provider("inherited")},
    )
    provider_a = _Provider("a")
    provider_b = _Provider("b")
    client_a = runtime.for_consumer(
        make_consumer(
            actor_id="a",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        capabilities={handles["read_primary"]: provider_a},
    )
    client_b = runtime.for_consumer(
        make_consumer(
            actor_id="b",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        capabilities={handles["read_primary"]: provider_b},
    )

    assert client_a.actions is runtime.actions is client_b.actions
    assert set(vars(runtime.actions)) == {
        "_store",
        "_registry",
        "_policy",
        "_handlers",
        # Runtime-level configuration, like the scope policy -- deliberately
        # NOT per-consumer state, which is what this assertion guards the
        # shared executor against accumulating.
        "_clock",
        "_id_factory",
    }

    first_a = client_a.call_function("boundProviders", {})
    only_b = client_b.call_function("boundProviders", {})
    second_a = client_a.call_function("boundProviders", {})
    assert first_a["capabilities"][handles["read_primary"]] is provider_a
    assert only_b["capabilities"][handles["read_primary"]] is provider_b
    assert second_a["capabilities"][handles["read_primary"]] is provider_a


def test_direct_client_copies_maps_and_threads_them_to_action_execute(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, handles = _provider_ontology()
    provider = _Provider("direct")
    replacement = _Provider("mutated")
    # `Run` declares BOTH capabilities, so bind both: T6's pre-flight refuses
    # an action with an unprovided declared capability (CAPABILITY_NOT_PROVIDED)
    # before the transaction opens. This test is about map-COPY isolation, so it
    # must satisfy the pre-flight to reach that point.
    secondary = _Provider("direct-secondary")
    capabilities = {
        handles["read_primary"]: provider,
        handles["read_secondary"]: secondary,
    }
    client = OntologyClient(
        ontology,
        ObjectStore(ontology.registry),
        make_consumer(
            actor_id="direct",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        capabilities=capabilities,
    )
    capabilities[handles["read_primary"]] = replacement

    bound = client.call_function("boundProviders", {})
    assert bound["capabilities"][handles["read_primary"]] is provider

    with patch.object(
        client.actions,
        "execute",
        wraps=client.actions.execute,
    ) as execute:
        assert client.execute("Run", {}) == {"status": "ok"}

    # The caller's dict was mutated after construction; what reached the
    # executor must still be the ORIGINAL provider objects.
    assert execute.call_args.kwargs["capability_providers"] == {
        handles["read_primary"]: provider,
        handles["read_secondary"]: secondary,
    }


def test_bind_copies_the_callers_maps_at_the_runtime_itself() -> None:
    """Pin bind-time copying AT `OntologyRuntime`, not only end-to-end.

    Mutation-testing T5 showed why this test has to exist: the caller's dicts
    are copied twice on the direct-`OntologyClient` path (once by
    `OntologyRuntime.__init__`, again by `_init_from_runtime`), so DELETING
    EITHER COPY left the end-to-end test green. Defense in depth is fine, but
    it meant "maps are copied at bind time" (AC5) was not independently pinned
    anywhere -- a later refactor could collapse to a single alias and no test
    would notice. This asserts the runtime's own maps directly.
    """
    ontology, handles = _provider_ontology()
    provider = _Provider("original")
    capabilities: dict[Any, Any] = {handles["read_primary"]: provider}

    runtime = OntologyRuntime(
        ontology,
        ObjectStore(ontology.registry),
        capabilities=capabilities,
    )

    capabilities[handles["read_primary"]] = _Provider("mutated")
    capabilities[handles["read_secondary"]] = _Provider("sneaked in")

    assert runtime._capability_providers == {handles["read_primary"]: provider}


# -- whole-branch review remediations (2026-07-26) ---------------------------


def _second_ontology_with_same_names() -> tuple[Ontology, dict[str, Any]]:
    """A DIFFERENT Ontology declaring the SAME api_names -- which AC1 permits."""
    other = Ontology("providers-other", scope_levels=["org"], min_n=1)

    @other.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    read_primary = other.capability(_Reader, name="readPrimary")
    other.validate()
    return other, {"read_primary": read_primary}


def test_binding_a_foreign_capability_handle_is_refused_at_bind_time() -> None:
    """A handle from another Ontology must be UNKNOWN_NAME where it is BOUND.

    Review finding: this was enforced at ACCESS time but not at bind time. The
    map was copied unchecked and the pre-flight simply skipped keys belonging to
    another registry, so the author was told `CAPABILITY_NOT_PROVIDED` -- "you
    bound no provider" -- for a capability they had visibly just bound. Two
    ontologies declaring the same api_name (legal, AC1) made it easy to hit and
    near-impossible to diagnose from the message.
    """
    ontology, _handles = _provider_ontology()
    _other, foreign = _second_ontology_with_same_names()

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as exc_info:
        ontology.bind(
            ObjectStore(ontology.registry),
            capabilities={foreign["read_primary"]: _Provider("foreign")},
        )
    assert "different Ontology" in str(exc_info.value)


def test_for_consumer_also_refuses_a_foreign_override(
    make_consumer: ConsumerFactory,
) -> None:
    """The same check must cover `for_consumer`, not only construction."""
    ontology, handles = _provider_ontology()
    _other, foreign = _second_ontology_with_same_names()
    runtime = ontology.bind(
        ObjectStore(ontology.registry),
        capabilities={
            handles["read_primary"]: _Provider("a"),
            handles["read_secondary"]: _Provider("b"),
        },
    )

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as exc_info:
        runtime.for_consumer(
            make_consumer(
                actor_id="one",
                role="Operator",
                scope_level="org",
                scope_id="org-1",
                kind="human",
            ),
            capabilities={foreign["read_primary"]: _Provider("foreign")},
        )
    assert "different Ontology" in str(exc_info.value)


def test_falsey_nonempty_provider_map_is_still_validated() -> None:
    """A nonempty but FALSEY mapping must not skip validation.

    Re-review finding (2026-07-26): the guard opened with `if not provided`, so a
    `dict` subclass overriding `__bool__` was treated as empty -- validation was
    skipped, the map became `{}`, and execution later reported
    `CAPABILITY_NOT_PROVIDED` for a provider the author had visibly bound. Exactly
    the misleading diagnosis the check exists to prevent, reachable through the
    check itself. Now keyed on `provided is None`.
    """
    ontology, _handles = _provider_ontology()
    _other, foreign = _second_ontology_with_same_names()

    class FalseyDict(dict[Any, Any]):
        def __bool__(self) -> bool:
            return False

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as exc_info:
        ontology.bind(
            ObjectStore(ontology.registry),
            capabilities=FalseyDict({foreign["read_primary"]: _Provider("foreign")}),
        )
    assert "different Ontology" in str(exc_info.value)


def test_non_capability_handle_is_refused_in_the_capability_map(
    make_consumer: ConsumerFactory,
) -> None:
    """Only a `CapabilityHandle` may key the capability map.

    Fail-closed at BIND time: any other object -- a handle of another kind, a
    bare string -- is refused with `UNKNOWN_NAME` rather than silently copied
    into a map whose pre-flight matches on `(api_name, registry)` and could
    then invoke the wrong object.
    """
    ontology, _handles = _provider_ontology()
    store = ObjectStore(ontology.registry)

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as exc_info:
        OntologyClient(
            ontology,
            store,
            make_consumer(
                actor_id="k",
                role="Operator",
                scope_level="org",
                scope_id="org-1",
                kind="human",
            ),
            capabilities={"readPrimary": _Provider("x")},
        )
    assert "cannot be bound in the capability map" in str(exc_info.value)
