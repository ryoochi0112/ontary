"""T5 provider plumbing: copied client-local maps on every Python path."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from unittest.mock import patch

from conftest import raises_code

from ontary.actions import ActionContext
from ontary.authoring import ActionParams, Ontology, OntologyObject, prop
from ontary.client import OntologyClient, OntologyRuntime
from ontary.effects import EffectMeta, EffectPayload
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

    class Notify(EffectPayload):
        message: str

    class Archive(EffectPayload):
        record_id: str

    notify = ontology.effect(Notify)
    archive = ontology.effect(Archive)

    class RunParams(ActionParams):
        pass

    @ontology.action(
        RunParams,
        target=Record,
        roles=["Operator"],
        capabilities=[read_primary, read_secondary],
        effects=[notify, archive],
        api_name="Run",
    )
    def run(_ctx: ActionContext, _params: RunParams) -> dict[str, str]:
        return {"status": "ok"}

    @ontology.function(
        api_name="boundProviders",
        capabilities=[read_primary, read_secondary],
    )
    def bound_providers(query: BoundQuery, _params: dict[str, Any]) -> dict[str, Any]:
        return {
            "capabilities": dict(query._capability_providers),
            # A Function's BoundQuery must carry NO effect dispatchers: a
            # Function cannot declare effects at all (AC4), so dispatchers it
            # could never use would be dead surface inviting exactly that
            # wiring. Reported out so the assertion sites below can pin the
            # ABSENCE -- re-adding the attribute turns those tests red.
            "has_effect_dispatchers": hasattr(query, "_effect_dispatchers"),
        }

    ontology.validate()
    return ontology, {
        "read_primary": read_primary,
        "read_secondary": read_secondary,
        "notify": notify,
        "archive": archive,
    }


def _dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
    pass


def _alternate_dispatch(_payload: EffectPayload, _meta: EffectMeta) -> None:
    pass


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
    effect_defaults = {
        handles["notify"]: _dispatch,
        handles["archive"]: _dispatch,
    }

    runtime = ontology.bind(
        store,
        capabilities=capability_defaults,
        effects=effect_defaults,
    )
    capability_defaults[handles["read_primary"]] = replacement
    effect_defaults[handles["archive"]] = _alternate_dispatch

    override = _Provider("consumer-primary")
    capability_overrides = {handles["read_primary"]: override}
    effect_overrides = {handles["notify"]: _alternate_dispatch}
    client = runtime.for_consumer(
        make_consumer(
            actor_id="one",
            role="Operator",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
        capabilities=capability_overrides,
        effects=effect_overrides,
    )
    capability_overrides[handles["read_primary"]] = replacement
    effect_overrides[handles["notify"]] = _dispatch

    bound = client.call_function("boundProviders", {})
    assert bound["capabilities"] == {
        handles["read_primary"]: override,
        handles["read_secondary"]: secondary,
    }
    assert bound["has_effect_dispatchers"] is False


def test_shared_runtime_clients_interleave_without_provider_crosstalk(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, handles = _provider_ontology()
    runtime = ontology.bind(
        ObjectStore(ontology.registry),
        capabilities={handles["read_secondary"]: _Provider("inherited")},
        effects={handles["archive"]: _dispatch},
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
        effects={handles["notify"]: _dispatch},
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
        effects={handles["notify"]: _alternate_dispatch},
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
        "_retry_policy",
        "_clock",
        "_id_factory",
    }

    first_a = client_a.call_function("boundProviders", {})
    only_b = client_b.call_function("boundProviders", {})
    second_a = client_a.call_function("boundProviders", {})
    assert first_a["capabilities"][handles["read_primary"]] is provider_a
    assert only_b["capabilities"][handles["read_primary"]] is provider_b
    assert second_a["capabilities"][handles["read_primary"]] is provider_a
    # The function path never carries dispatchers, for any consumer (AC4).
    assert not any(
        r["has_effect_dispatchers"] for r in (first_a, only_b, second_a)
    )


def test_direct_client_copies_maps_and_threads_them_to_action_execute(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, handles = _provider_ontology()
    provider = _Provider("direct")
    replacement = _Provider("mutated")
    # `Run` declares BOTH capabilities and BOTH effects, so bind all four:
    # T6's pre-flight refuses an action with an unprovided declared capability
    # (CAPABILITY_NOT_PROVIDED) before the transaction opens, and T7 adds the same
    # refusal for an undispatchable declared effect. This test is about
    # map-COPY isolation, so it must satisfy the pre-flight to reach that point.
    secondary = _Provider("direct-secondary")
    capabilities = {
        handles["read_primary"]: provider,
        handles["read_secondary"]: secondary,
    }
    effects = {handles["notify"]: _dispatch, handles["archive"]: _dispatch}
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
        effects=effects,
    )
    capabilities[handles["read_primary"]] = replacement
    effects[handles["notify"]] = _alternate_dispatch

    bound = client.call_function("boundProviders", {})
    assert bound["capabilities"][handles["read_primary"]] is provider
    assert bound["has_effect_dispatchers"] is False
    # Effect-map copy isolation is observed on the ACTION path instead (below):
    # the caller's `effects` dict was mutated after construction, so what
    # reaches `ActionExecutor.execute` must still be the ORIGINAL dispatcher.

    with patch.object(
        client.actions,
        "execute",
        wraps=client.actions.execute,
    ) as execute:
        assert client.execute("Run", {}) == {"status": "ok"}

    # The caller's dicts were mutated after construction; what reached the
    # executor must still be the ORIGINAL provider/dispatcher objects.
    assert execute.call_args.kwargs["capability_providers"] == {
        handles["read_primary"]: provider,
        handles["read_secondary"]: secondary,
    }
    assert execute.call_args.kwargs["effect_dispatchers"] == {
        handles["notify"]: _dispatch,
        handles["archive"]: _dispatch,
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
    effects: dict[Any, Any] = {handles["notify"]: _dispatch}

    runtime = OntologyRuntime(
        ontology,
        ObjectStore(ontology.registry),
        capabilities=capabilities,
        effects=effects,
    )

    capabilities[handles["read_primary"]] = _Provider("mutated")
    effects[handles["notify"]] = _alternate_dispatch
    capabilities[handles["read_secondary"]] = _Provider("sneaked in")

    assert runtime._capability_providers == {handles["read_primary"]: provider}
    assert runtime._effect_dispatchers == {handles["notify"]: _dispatch}


# -- whole-branch review remediations (2026-07-26) ---------------------------


def _second_ontology_with_same_names() -> tuple[Ontology, dict[str, Any]]:
    """A DIFFERENT Ontology declaring the SAME api_names -- which AC1 permits."""
    other = Ontology("providers-other", scope_levels=["org"], min_n=1)

    @other.object(layer="L0", scope="unscoped")
    class Record(OntologyObject):
        id: str = prop(primary_key=True)

    read_primary = other.capability(_Reader, name="readPrimary")

    class Notify(EffectPayload):
        message: str

    notify = other.effect(Notify, api_name="Notify")
    other.validate()
    return other, {"read_primary": read_primary, "notify": notify}


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


def test_binding_a_foreign_effect_handle_is_refused_at_bind_time(
    make_consumer: ConsumerFactory,
) -> None:
    ontology, _handles = _provider_ontology()
    _other, foreign = _second_ontology_with_same_names()

    with raises_code(ValidationFailed, "UNKNOWN_NAME") as exc_info:
        OntologyClient(
            ontology,
            ObjectStore(ontology.registry),
            make_consumer(
                actor_id="direct",
                role="Operator",
                scope_level="org",
                scope_id="org-1",
                kind="human",
            ),
            effects={foreign["notify"]: _dispatch},
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
        effects={handles["notify"]: _dispatch, handles["archive"]: _dispatch},
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


def test_wrong_kind_handle_is_refused_in_each_map(
    make_consumer: ConsumerFactory,
) -> None:
    """A CapabilityHandle cannot be bound as an effect dispatcher, or vice versa.

    Re-review finding (2026-07-26), and the one genuine FAIL-OPEN of the set:
    `CapabilityDef` and `EffectTypeDef` live in separate registry namespaces, so
    `capability(X, name="Shared")` and `effect(Y, api_name="Shared")` can BOTH be
    declared -- verified directly. Both pre-flights then matched on
    `(api_name, registry)` alone, so a `CapabilityHandle` bound in `effects=` would
    satisfy an effect declaration and be INVOKED as its dispatcher. Not a refusal
    -- the wrong object gets called.
    """
    ontology = Ontology("kind-check", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope="unscoped", owned=True)
    class Rec(OntologyObject):
        id: str = prop(primary_key=True)

    class Payload(EffectPayload):
        msg: str

    cap = ontology.capability(_Reader, name="Shared")
    eff = ontology.effect(Payload, api_name="Shared")
    assert cap.api_name == eff.api_name  # the collision is legal
    ontology.validate()
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
            effects={cap: _dispatch},
        )
    assert "cannot be bound in the effect map" in str(exc_info.value)

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
            capabilities={eff: _Provider("x")},
        )
    assert "cannot be bound in the capability map" in str(exc_info.value)
