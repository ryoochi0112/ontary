"""Dedicated unit tests for `ontary.ontology.OntologyDef` -- the single
authored `registry` + `policy` + `functions` bundle, and its `validate()`
entry point (spec §5).

`OntologyDef.validate()` is a thin two-line delegation
(`registry.validate()` then `policy.validate(registry)`) -- these tests pin
that BOTH sides actually run (a registry-only error and a policy-only
error both surface through `OntologyDef.validate()`, not just through the
underlying `OntologyRegistry`/`ScopePolicy` objects directly, which
`tests/test_meta.py`/`tests/test_scope.py` already cover in isolation).
"""

from __future__ import annotations

from collections.abc import Callable

from conftest import raises_code

from ontary.errors import ValidationFailed
from ontary.functions import FunctionRegistry
from ontary.meta import (
    Cardinality,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.ontology import OntologyDef
from ontary.scope import ScopePolicy, SelfScope, ViaLink

RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]


def _library_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    return make_registry(
        object_types=[
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
        ],
        link_types=[
            LinkTypeDef(
                api_name="inLibrary",
                from_type="Shelf",
                to_type="Library",
                cardinality=Cardinality.MANY_TO_ONE,
                description="Shelf -> its library",
            )
        ],
    )


def _library_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=["shelf", "library"],
        rules={
            "Library": [SelfScope(level="library")],
            "Shelf": [
                SelfScope(level="shelf"),
                ViaLink(
                    link_api_name="inLibrary", direction="from", parent_type="Library"
                ),
            ],
        },
        min_n=3,
    )


def test_validate_passes_for_well_formed_ontology(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    ontology = OntologyDef(
        name="library",
        registry=_library_registry(make_registry),
        policy=_library_policy(make_policy),
    )
    ontology.validate()  # no raise


def test_default_functions_registry_is_bound_to_own_registry(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _library_registry(make_registry)
    ontology = OntologyDef(
        name="library", registry=registry, policy=_library_policy(make_policy)
    )
    assert isinstance(ontology.functions, FunctionRegistry)
    # constructed fresh, bound to THIS ontology's registry (module docstring:
    # "defaults to a fresh FunctionRegistry bound to registry if not
    # supplied")
    assert ontology.functions._registry is registry


def test_explicit_functions_registry_is_kept_as_is(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _library_registry(make_registry)
    functions = FunctionRegistry(registry)
    ontology = OntologyDef(
        name="library",
        registry=registry,
        policy=_library_policy(make_policy),
        functions=functions,
    )
    assert ontology.functions is functions


def test_validate_raises_ontology_validation_error_for_dangling_link_endpoint(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            )
        ],
        link_types=[
            LinkTypeDef(
                api_name="inLibrary",
                from_type="Shelf",
                to_type="Library",  # never registered -> dangling to_type
                cardinality=Cardinality.MANY_TO_ONE,
                description="Shelf -> its library",
            )
        ],
    )
    ontology = OntologyDef(
        name="broken",
        registry=registry,
        policy=make_policy(
            levels=["shelf"],
            rules={"Shelf": [SelfScope(level="shelf")]},
            min_n=3,
        ),
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.validate()
    assert "Library" in str(exc_info.value)


def test_validate_raises_scope_policy_error_for_undeclared_link_in_policy(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    # The registry itself is well-formed; the *policy* references a link
    # that was never registered -- `OntologyDef.validate()` must still
    # surface this (it does not stop at `registry.validate()` succeeding).
    registry = _library_registry(make_registry)
    policy = make_policy(
        levels=["shelf", "library"],
        rules={"Shelf": [ViaLink(link_api_name="noSuchLink", direction="from", parent_type="Library")]},
        min_n=3,
    )
    ontology = OntologyDef(name="library", registry=registry, policy=policy)

    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        ontology.validate()
    assert "noSuchLink" in str(exc_info.value)


def test_registry_duplicate_object_type_registration_raises_immediately(
    make_registry: RegistryFactory,
) -> None:
    # Duplicate-name rejection happens at *registration* time, not at
    # `validate()` -- pinned here since it's a failure mode an ontology
    # author hits before `OntologyDef` even exists.
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            )
        ]
    )
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        registry.register_object_type(
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf (dup)",
                description="A duplicate shelf type",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
            )
        )
    assert "duplicate" in str(exc_info.value)


def test_validate_raises_for_owned_default_on_primary_key_property(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    # OntologyRegistry.validate()'s owned-property-default checks (an
    # ObjectTypeDef-level rule, not a link/action dangling-reference check)
    # also surface through OntologyDef.validate().
    registry = make_registry(
        object_types=[
            ObjectTypeDef(
                api_name="Shelf",
                display_name="Shelf",
                description="A shelf",
                layer="L0",
                properties=[PropertyDef(name="id", type="str")],
                primary_key="id",
                owned={"id": "not-allowed"},
            )
        ]
    )
    ontology = OntologyDef(
        name="broken",
        registry=registry,
        policy=make_policy(
            levels=["shelf"],
            rules={"Shelf": [SelfScope(level="shelf")]},
            min_n=3,
        ),
    )

    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        ontology.validate()
    assert "primary key" in str(exc_info.value)
