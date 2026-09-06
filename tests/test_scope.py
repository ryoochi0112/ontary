"""Unit tests for `ontary.scope` + `ontary.security` on a toy ontology.

The toy ontology is a small "library" domain (Book/Shelf/Library/Loan) --
deliberately NOT named after DSO -- chosen to exercise every rule kind, an
ordered-fallback rule list, a multi-hop `ViaLink` chain, hierarchy
climbing (shelf -> library), broken chains, and a rule cycle.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from conftest import raises_code

from ontary.errors import ValidationFailed
from ontary.meta import (
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
    SelfScope,
    ViaLink,
    resolve_owning_scope,
)
from ontary.security import Consumer, covers_scope
from ontary.store import ObjectStore, Source, Store

LEVELS = ["shelf", "library"]


RegistryFactory = Callable[..., OntologyRegistry]
PolicyFactory = Callable[..., ScopePolicy]
StoreFactory = Callable[..., Store]


def _library_registry(make_registry: RegistryFactory) -> OntologyRegistry:
    registry = make_registry()
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Library",
            display_name="Library",
            description="A library building",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Shelf",
            display_name="Shelf",
            description="A shelf inside a library",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="library_id", type="str", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Book",
            display_name="Book",
            description="A book, shelved somewhere",
            layer="L0",
            properties=[
                PropertyDef(name="id", type="str"),
                PropertyDef(name="shelf_id", type="str", required=False),
            ],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Loan",
            display_name="Loan",
            description="A book on loan",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Reservation",
            display_name="Reservation",
            description="A reservation on a book",
            layer="L0",
            properties=[PropertyDef(name="id", type="str")],
            primary_key="id",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="onShelf",
            from_type="Book",
            to_type="Shelf",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Book -> its shelf",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="inLibrary",
            from_type="Shelf",
            to_type="Library",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Shelf -> its library",
        )
    )
    registry.register_link_type(
        LinkTypeDef(
            api_name="loanedBook",
            from_type="Loan",
            to_type="Book",
            cardinality=Cardinality.MANY_TO_ONE,
            description="Loan -> the book on loan",
        )
    )
    # Reversed direction on purpose: the governing link for Reservation runs
    # Book -> Reservation (to-side resolution, mirrors `_TEAM_CHAIN_TO_SIDE`).
    registry.register_link_type(
        LinkTypeDef(
            api_name="reservedFor",
            from_type="Book",
            to_type="Reservation",
            cardinality=Cardinality.ONE_TO_ONE,
            description="Book -> its reservation",
        )
    )
    return registry


def _library_policy(make_policy: PolicyFactory) -> ScopePolicy:
    return make_policy(
        levels=LEVELS,
        rules={
            "Library": [SelfScope(level="library")],
            "Shelf": [
                SelfScope(level="shelf"),
                ViaLink(link_api_name="inLibrary", direction="from", parent_type="Library"),
            ],
            "Book": [
                DirectProperty(level="shelf", property_name="shelf_id"),
                ViaLink(link_api_name="onShelf", direction="from", parent_type="Shelf"),
            ],
            "Loan": [
                ViaLink(link_api_name="loanedBook", direction="from", parent_type="Book"),
            ],
            "Reservation": [
                ViaLink(
                    link_api_name="reservedFor", direction="to", parent_type="Book"
                ),
            ],
        },
        min_n=3,
    )

SRC = Source(source_system="test")


def test_default_min_n_is_three() -> None:
    policy = ScopePolicy(levels=LEVELS)
    assert policy.min_n == 3


def test_min_n_zero_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        ScopePolicy(levels=LEVELS, min_n=0)


def test_min_n_negative_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="greater than or equal to 1"):
        ScopePolicy(levels=LEVELS, min_n=-1)


def test_min_n_one_is_accepted_at_construction() -> None:
    assert ScopePolicy(levels=LEVELS, min_n=1).min_n == 1


def test_empty_levels_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="levels must not be empty"):
        ScopePolicy(levels=[])


def test_duplicate_levels_are_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="levels must not contain duplicate entries"):
        ScopePolicy(levels=["shelf", "shelf"])


def test_validate_passes_for_well_formed_policy(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = _library_policy(make_policy)
    policy.validate(registry)  # no raise


def test_self_scope_resolves_directly(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Library", "lib-1"
    )
    assert resolved == {"shelf": None, "library": "lib-1"}


def test_via_link_from_direction_resolves_parent(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Shelf", "shelf-1"
    )
    assert resolved == {"shelf": "shelf-1", "library": "lib-1"}


def test_include_retired_default_is_off_and_a_retired_row_resolves_nothing(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    """`resolve_owning_scope`'s default is the guard, pinned directly.

    Every other caller of this function reaches it through a live-only read,
    so flipping the default was unobservable across the whole suite -- the
    protection the docstring claims for consumer reads could be deleted in
    silence. Asked here about a RETIRED row directly, the two settings must
    differ: off resolves nothing, on resolves the scope the row owned.

    That difference is the disclosure boundary. A retired or erased row that
    still resolves puts its children back in a reader's visible set, which
    carries a population past min_n and releases an aggregate computed over
    the erased subject's own rows.
    """
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.retire_object("Shelf", "shelf-1")

    policy = _library_policy(make_policy)

    # The default -- and every consumer read -- sees nothing.
    assert resolve_owning_scope(policy, store, "Shelf", "shelf-1") == {
        "shelf": None,
        "library": None,
    }

    # The action target gate, and only it, sees the scope the row owned.
    assert resolve_owning_scope(
        policy, store, "Shelf", "shelf-1", include_retired=True
    ) == {"shelf": "shelf-1", "library": "lib-1"}


def test_direct_property_shortcut_resolves_without_link(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-9"}, SRC)

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Book", "book-1"
    )
    assert resolved["shelf"] == "shelf-9"


def test_ordered_fallback_tries_second_rule_when_first_misses(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    # Book has no shelf_id property value set -> DirectProperty rule misses,
    # falls through to the ViaLink rule.
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Book", "book-1"
    )
    assert resolved == {"shelf": "shelf-1", "library": "lib-1"}


def test_multi_hop_chain_grandchild_to_child_to_scope_object(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelf", "book-1", "shelf-1")
    store.insert("Loan", {"id": "loan-1"}, SRC)
    store.create_link("loanedBook", "loan-1", "book-1")

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Loan", "loan-1"
    )
    assert resolved == {"shelf": "shelf-1", "library": "lib-1"}


def test_hierarchy_climbing_narrow_type_resolves_broader_level(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    # Book only has direct rules reaching "shelf"; resolving "library"
    # (broader) works by climbing via the Shelf object's own ViaLink rule.
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Book", "book-1"
    )
    assert resolved["library"] == "lib-1"


def test_via_link_tries_all_parents_when_first_dead_ends(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    # Book has two links via a many-to-many link type: the first shelf has
    # no `inLibrary` link (dead end), the second one does. `ViaLink` must
    # iterate all parents in link order and return the first that
    # resolves, mirroring the prototype's Person fan-out over multiple
    # memberships.
    registry = _library_registry(make_registry)
    registry.register_link_type(
        LinkTypeDef(
            api_name="onShelfMulti",
            from_type="Book",
            to_type="Shelf",
            cardinality=Cardinality.MANY_TO_MANY,
            description="a book may sit on more than one shelf",
        )
    )
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-dead"}, SRC)  # no inLibrary link
    store.insert("Shelf", {"id": "shelf-live"}, SRC)
    store.create_link("inLibrary", "shelf-live", "lib-1")
    store.insert("Book", {"id": "book-1"}, SRC)
    store.create_link("onShelfMulti", "book-1", "shelf-dead")
    store.create_link("onShelfMulti", "book-1", "shelf-live")

    policy = ScopePolicy(
        levels=LEVELS,
        rules={
            "Library": [SelfScope(level="library")],
            "Shelf": [
                SelfScope(level="shelf"),
                ViaLink(
                    link_api_name="inLibrary", direction="from", parent_type="Library"
                ),
            ],
            "Book": [
                ViaLink(
                    link_api_name="onShelfMulti",
                    direction="from",
                    parent_type="Shelf",
                ),
            ],
        },
    )
    resolved = resolve_owning_scope(policy, store, "Book", "book-1")
    assert resolved["library"] == "lib-1"


def test_self_scope_uses_pk_value_not_hardcoded_id_property(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    # A type whose primary_key is not "id" but which also happens to carry
    # a conflicting "id" property -- SelfScope must resolve to the pk value
    # (obj_id), never to the unrelated "id" property.
    registry = _library_registry(make_registry)
    registry.register_object_type(
        ObjectTypeDef(
            api_name="Wing",
            display_name="Wing",
            description="A wing of a library, keyed by code",
            layer="L0",
            properties=[
                PropertyDef(name="code", type="str"),
                PropertyDef(name="id", type="str", required=False),
            ],
            primary_key="code",
        )
    )
    store = make_store(registry)
    store.insert("Wing", {"code": "wing-1", "id": "not-the-pk"}, SRC)

    policy = ScopePolicy(
        levels=LEVELS,
        rules={"Wing": [SelfScope(level="shelf")]},
    )
    resolved = resolve_owning_scope(policy, store, "Wing", "wing-1")
    assert resolved["shelf"] == "wing-1"


def test_via_link_to_direction_resolves_from_id_parent(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)
    store.insert("Shelf", {"id": "shelf-1"}, SRC)
    store.create_link("inLibrary", "shelf-1", "lib-1")
    store.insert("Book", {"id": "book-1", "shelf_id": "shelf-1"}, SRC)
    store.insert("Reservation", {"id": "res-1"}, SRC)
    store.create_link("reservedFor", "book-1", "res-1")

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Reservation", "res-1"
    )
    assert resolved == {"shelf": "shelf-1", "library": "lib-1"}


def test_custom_resolver_escape_hatch(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Library", {"id": "lib-1"}, SRC)

    def _weird_shelf(store: ObjectStore, obj_type: str, obj_id: str) -> str | None:
        return "shelf-special"

    policy = ScopePolicy(
        levels=LEVELS,
        rules={"Library": [CustomResolver(level="shelf", fn=_weird_shelf)]},
    )
    resolved = resolve_owning_scope(policy, store, "Library", "lib-1")
    assert resolved["shelf"] == "shelf-special"


def test_broken_chain_resolves_to_none_and_denies_coverage(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Book", {"id": "book-1"}, SRC)  # no shelf_id, no onShelf link

    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Book", "book-1"
    )
    assert resolved == {"shelf": None, "library": None}

    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is False


def test_nonexistent_object_resolves_to_none(
    make_registry: RegistryFactory,
    make_policy: PolicyFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    resolved = resolve_owning_scope(
        _library_policy(make_policy), store, "Book", "no-such-book"
    )
    assert resolved == {"shelf": None, "library": None}


def test_cycle_resolves_to_none_never_infinite_recursion(
    make_registry: RegistryFactory,
    make_store: StoreFactory,
) -> None:
    registry = _library_registry(make_registry)
    store = make_store(registry)
    store.insert("Shelf", {"id": "shelf-a"}, SRC)
    store.insert("Shelf", {"id": "shelf-b"}, SRC)

    # A pathological policy where Shelf's "library" rule points back at
    # another Shelf via a link that doesn't exist in the real registry --
    # here we simulate a cycle using two ViaLink rules through `onShelf`
    # misconfigured to point Shelf -> Shelf (not registry-valid, but the
    # resolution engine must not infinite-loop even so).
    registry.register_link_type(
        LinkTypeDef(
            api_name="shelfLoop",
            from_type="Shelf",
            to_type="Shelf",
            cardinality=Cardinality.MANY_TO_MANY,
            description="pathological self-loop for the cycle-guard test",
        )
    )
    store.create_link("shelfLoop", "shelf-a", "shelf-b")
    store.create_link("shelfLoop", "shelf-b", "shelf-a")

    policy = ScopePolicy(
        levels=LEVELS,
        rules={
            "Shelf": [
                ViaLink(
                    link_api_name="shelfLoop", direction="from", parent_type="Shelf"
                ),
            ],
        },
    )
    resolved = resolve_owning_scope(policy, store, "Shelf", "shelf-a")
    assert resolved["library"] is None


# -- policy.validate() rejections -------------------------------------------


def test_validate_rejects_undeclared_link(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = ScopePolicy(
        levels=LEVELS,
        rules={
            "Book": [
                ViaLink(
                    link_api_name="noSuchLink", direction="from", parent_type="Shelf"
                )
            ]
        },
    )
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "noSuchLink" in str(exc_info.value)


def test_validate_rejects_undeclared_object_type(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = ScopePolicy(
        levels=LEVELS,
        rules={"NoSuchType": [SelfScope(level="shelf")]},
    )
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "NoSuchType" in str(exc_info.value)


def test_validate_rejects_undeclared_level(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = ScopePolicy(
        levels=LEVELS,
        rules={"Library": [SelfScope(level="galaxy")]},
    )
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "galaxy" in str(exc_info.value)


def test_validate_rejects_direct_property_undeclared_property(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = ScopePolicy(
        levels=LEVELS,
        rules={
            "Book": [
                DirectProperty(level="shelf", property_name="no_such_property")
            ]
        },
    )
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "no_such_property" in str(exc_info.value)


def test_validate_rejects_direction_mismatch(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    # `onShelf` runs Book -> Shelf; declaring direction="to" with
    # parent_type=Shelf implies Shelf -> Book, which contradicts the
    # registered LinkTypeDef.
    policy = ScopePolicy(
        levels=LEVELS,
        rules={
            "Book": [
                ViaLink(link_api_name="onShelf", direction="to", parent_type="Shelf")
            ]
        },
    )
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "onShelf" in str(exc_info.value)


def test_validate_rejects_unscoped_type_not_in_registry(
    make_registry: RegistryFactory,
) -> None:
    registry = _library_registry(make_registry)
    policy = ScopePolicy(levels=LEVELS, unscoped_types={"NoSuchType"})
    with raises_code(ValidationFailed, "SCOPE_POLICY_ERROR") as exc_info:
        policy.validate(registry)
    assert "NoSuchType" in str(exc_info.value)


# -- covers_scope matrix -----------------------------------------------------


def test_covers_scope_matches_exact_level_and_id(
    make_policy: PolicyFactory,
) -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )
    resolved = {"shelf": "shelf-1", "library": "lib-1"}
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is True


def test_covers_scope_denies_on_mismatch(make_policy: PolicyFactory) -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )
    resolved = {"shelf": "shelf-2", "library": "lib-1"}
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is False


def test_covers_scope_denies_on_unresolved_dimension(
    make_policy: PolicyFactory,
) -> None:
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )
    resolved = {"shelf": None, "library": "lib-1"}
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is False


def test_covers_scope_broader_consumer_level_checks_its_own_level_only(
    make_policy: PolicyFactory,
) -> None:
    # A library-scoped consumer covers iff the *library* dimension matches
    # its scope_id -- it does not automatically cover every shelf under it
    # (that hierarchy-widening behavior would be a policy decision, not
    # baked into `covers_scope`).
    consumer = Consumer(
        actor_id="u1", role="Admin", scope_level="library", scope_id="lib-1", kind="human"
    )
    resolved = {"shelf": "shelf-1", "library": "lib-1"}
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is True

    resolved_other_library = {"shelf": "shelf-9", "library": "lib-2"}
    assert covers_scope(
        _library_policy(make_policy), consumer, resolved_other_library
    ) is False


def test_covers_scope_narrower_consumer_level_does_not_cover_broader_object(
    make_policy: PolicyFactory,
) -> None:
    # A shelf-scoped consumer whose scope_level is "shelf" is only ever
    # compared against the "shelf" dimension, even for objects whose
    # resolved shelf is None but library matches.
    consumer = Consumer(
        actor_id="u1", role="Member", scope_level="shelf", scope_id="shelf-1", kind="human"
    )
    resolved = {"shelf": None, "library": "lib-1"}
    assert covers_scope(_library_policy(make_policy), consumer, resolved) is False
