"""M9a: fingerprinted ontology drift, declared-shape enforcement, and rewrites.

The spec's problem statement was measured by running five drift scenarios against
an engine that had no defenses. These tests pin the defenses, and the last one
walks the whole scenario end to end: write under one declaration, change it,
be refused, migrate, accept, read.
"""

from __future__ import annotations

import ast
import functools
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Protocol

import pytest
from conftest import raises_code
from pydantic import BaseModel

from ontary import (
    ActionContext,
    ActionParams,
    BoundQuery,
    Consumer,
    EffectPayload,
    ObjectStore,
    Ontology,
    OntologyObject,
    SelfScope,
    Source,
    Store,
    prop,
    target,
)
from ontary import fingerprint as fingerprint_module
from ontary import meta as meta_module
from ontary.client import OntologyClient
from ontary.errors import ConflictError, ValidationFailed
from ontary.fingerprint import DOCUMENTATION_FIELDS, _canonical, fingerprint_ontology
from ontary.meta import (
    ActionParameterDef,
    ActionTypeDef,
    CapabilityDef,
    Cardinality,
    EffectTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
)
from ontary.migrate import migrate_object_type, upcast_object_type
from ontary.store import InMemoryStore, accept_ontology_fingerprint

SRC = Source(source_system="seed")


ConsumerFactory = Callable[..., Consumer]
ObjectTypeFactory = Callable[..., ObjectTypeDef]


def _evolution_ontology(
    *,
    described: str | None = None,
    with_owner: bool = False,
    label_type: str = "str",
    label_choices: list[str] | None = None,
    renamed: bool = False,
) -> Ontology:
    """One ontology, varied along the axes the fingerprint must (or must not)
    notice."""
    ontology = Ontology("evolution", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0",
        scope=[SelfScope(level="org")],
        owned=True,
        api_name="W",
        description=described,
    )
    class W(OntologyObject):
        id: str = prop(primary_key=True)
        if renamed:
            label_v2: str | None = None
        elif label_type == "int":
            label: int | None = None
        elif label_type == "date":
            label: date | None = None
        else:
            label: str | None = prop(default=None, choices=label_choices)
        if with_owner:
            owner: str = prop()

    class MakeParams(ActionParams):
        w_id: str = target(W)

    @ontology.action(MakeParams, target=W, roles=["Op"], api_name="Make")
    def make(ctx: ActionContext, params: MakeParams) -> dict[str, str]:
        # Deliberately writes ONLY the primary key. Harmless while `owner` is
        # undeclared or optional; the AC9 test below turns that into a refusal.
        ctx.insert("W", {"id": params.w_id})
        return {"id": params.w_id}

    ontology.validate()
    return ontology


_NON_OBJECT_KINDS = ("link", "action", "function", "capability", "effect")
"""Every kind `fingerprint_ontology` covers except `object`.

The classifier's newly-declared / no-longer-declared / declaration-changed
branches are all kind-GENERIC (`store/protocol.py:232-245`), so pinning them
with an object -- or with any single kind -- leaves the other five free to
regress silently. Kept as a literal rather than derived from the registry so
that a NEW fingerprinted kind reds `test_every_fingerprinted_kind_is_pinned`
instead of quietly widening the parametrization to nothing.
"""

_CHANGEABLE_NON_OBJECT_KINDS = ("link", "action", "function", "effect")
"""The subset whose declaration can change WITHOUT changing its api_name.

`capability` is absent for a structural reason, not an oversight: a
`CapabilityDef` carries `api_name` and `description` only, and `description`
is a `DOCUMENTATION_FIELD`, so a capability's digest cannot move unless it is
renamed -- which the classifier sees as a drop plus an add, not a change.
`test_only_capability_has_no_changeable_declaration` pins that reasoning
against `model_fields`, so adding a governance field to `CapabilityDef` reds
rather than silently leaving this branch unpinned for that kind.
"""

_KIND_REGISTRY_COLLECTIONS = {
    "object": "object_types",
    "link": "link_types",
    "action": "action_types",
    "function": "functions",
    "capability": "capabilities",
    "effect": "effect_types",
}
"""Each fingerprinted kind and the `OntologyRegistry` collection it lives in.

A kind comes into existence on the REGISTRY, which is what makes this the
list to pin -- see `test_every_registry_collection_is_fingerprinted`.
"""

_KIND_API_NAMES = {
    "object": "W",
    "link": "Rel",
    "action": "Make",
    "function": "Count",
    "capability": "Reader",
    "effect": "Notify",
}


class _Reader(Protocol):
    def read(self) -> str: ...


def _all_kinds_ontology(
    *, omit: str | None = None, changed: str | None = None
) -> Ontology:
    """One ontology declaring every fingerprinted kind, with any single
    non-object kind left undeclared or perturbed.

    Each perturbation moves a NON-documentation field (`identity_revealing`,
    `executable_by_roles`, `audit`, a payload field), so it changes that
    kind's digest and nothing else's -- the whole point being that the
    resulting refusal names the kind under test.

    One axis per kind is deliberate, not a gap: WHICH fields belong to the
    digest is a separate property, pinned exhaustively against `model_fields`
    by `test_the_digest_excludes_exactly_the_documented_prose_fields`. These
    tests only need the digest to move at all, so that the classifier's
    kind-generic branch is the thing under test.
    """
    assert omit is None or omit in _NON_OBJECT_KINDS
    assert changed is None or changed in _CHANGEABLE_NON_OBJECT_KINDS

    ontology = Ontology("all-kinds", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0", scope=[SelfScope(level="org")], owned=True, api_name="W"
    )
    class W(OntologyObject):
        id: str = prop(primary_key=True)
        label: str | None = None

    if omit != "link":
        ontology.link(
            "Rel",
            W,
            W,
            "MANY_TO_MANY",
            identity_revealing=(changed == "link"),
        )

    if omit != "capability":
        ontology.capability(_Reader, name="Reader")

    if omit != "effect":

        class Notify(EffectPayload):
            channel: str
            if changed == "effect":
                urgency: str = "low"

        ontology.effect(Notify, api_name="Notify")

    if omit != "action":

        class MakeParams(ActionParams):
            w_id: str = target(W)

        @ontology.action(
            MakeParams,
            target=W,
            roles=["Admin" if changed == "action" else "Op"],
            api_name="Make",
        )
        def make(ctx: ActionContext, params: MakeParams) -> dict[str, str]:
            ctx.insert("W", {"id": params.w_id})
            return {"id": params.w_id}

    if omit != "function":

        @ontology.function(
            api_name="Count", audit=True if changed == "function" else None
        )
        def count(query: BoundQuery, params: dict[str, Any]) -> Any:
            return 0

    ontology.validate()
    return ontology


# -- AC1: what the fingerprint does and does not notice -----------------------


def test_pre_0_7_all_kinds_ontology_keeps_legacy_fingerprint_digest() -> None:
    """Adding SDK property features must not drift declarations that omit them.

    This digest was captured from the unmodified 0.6-compatible implementation.
    The fixture includes every fingerprinted descriptor kind while using no 0.7
    property feature, so later property extensions can add their own moving-
    digest assertion without weakening this legacy baseline.
    """
    assert fingerprint_ontology(_all_kinds_ontology().registry).digest == (
        "943ac552e0c31da5d688fb2db8b2e7649abbefcda8d223dd22db331ff30bc4be"
    )


def test_choices_declaration_changes_the_fingerprint_and_names_the_type() -> None:
    before = fingerprint_ontology(_evolution_ontology().registry)
    after = fingerprint_ontology(
        _evolution_ontology(label_choices=["open", "closed"]).registry
    )

    assert before.digest != after.digest
    assert before.types["object:W"] != after.types["object:W"]


def test_documentation_only_changes_do_not_change_the_fingerprint() -> None:
    """CEO decision 2026-07-26. A digest that refuses a store because someone
    improved a docstring is a digest whose check gets switched off, at which
    point it protects nothing."""
    plain = fingerprint_ontology(_evolution_ontology().registry)
    described = fingerprint_ontology(
        _evolution_ontology(described="now documented").registry
    )

    assert plain.digest == described.digest
    assert plain.types == described.types


@pytest.mark.parametrize(
    "changed",
    [
        pytest.param({"with_owner": True}, id="property-added"),
        pytest.param({"label_type": "int"}, id="property-retyped"),
        pytest.param({"label_type": "date"}, id="property-retyped-to-date"),
        pytest.param({"renamed": True}, id="property-renamed"),
    ],
)
def test_shape_changes_change_the_fingerprint_and_name_the_type(
    changed: dict[str, Any],
) -> None:
    before = fingerprint_ontology(_evolution_ontology().registry)
    after = fingerprint_ontology(_evolution_ontology(**changed).registry)

    assert before.digest != after.digest
    assert before.types["object:W"] != after.types["object:W"]


def test_declaration_order_does_not_change_the_fingerprint() -> None:
    """Two authors declaring the same properties in a different sequence must
    fingerprint identically, or the check fires on a refactor that changed
    nothing about the data."""

    def build(reverse: bool) -> Ontology:
        ontology = Ontology("order", scope_levels=["org"], min_n=1)
        names = ["b", "a"] if reverse else ["a", "b"]
        namespace: dict[str, Any] = {
            "__annotations__": {"id": str, **{name: (str | None) for name in names}},
            "id": prop(primary_key=True),
            **{name: None for name in names},
        }
        cls = type("Ordered", (OntologyObject,), namespace)
        ontology.object(layer="L0", scope=[SelfScope(level="org")], api_name="O")(cls)
        ontology.validate()
        return ontology

    assert (
        fingerprint_ontology(build(False).registry).digest
        == fingerprint_ontology(build(True).registry).digest
    )


def test_the_digest_excludes_exactly_the_documented_prose_fields() -> None:
    """The exclusion list is a decision, so it is asserted as one.

    Canonicalizes a real descriptor of every IR kind and compares the surviving
    keys against the declared field set minus `DOCUMENTATION_FIELDS`. Driven from
    `model_fields`, so a NEW IR field lands inside the digest by default and a
    future decision to exclude it has to be made here, explicitly -- rather than
    a field quietly falling outside the digest and taking drift detection for
    whatever it governs with it.
    """
    samples: list[BaseModel] = [
        ObjectTypeDef(
            api_name="W",
            display_name="W",
            description="prose",
            layer="L0",
            properties=[
                PropertyDef(
                    name="id",
                    type="str",
                    required=True,
                    choices=("one", "two"),
                )
            ],
            primary_key="id",
        ),
        LinkTypeDef(
            api_name="l",
            from_type="W",
            to_type="W",
            cardinality=Cardinality.MANY_TO_ONE,
            description="prose",
        ),
        ActionTypeDef(
            api_name="A",
            display_name="A",
            target_type="W",
            executable_by_roles=["Op"],
            description="prose",
            parameters=[
                ActionParameterDef(name="p", type="str", required=True)
            ],
        ),
        FunctionDef(
            api_name="f",
            description="prose",
            input_description="prose",
            output_description="prose",
        ),
        CapabilityDef(api_name="c", description="prose"),
        EffectTypeDef(api_name="e", description="prose", payload=[]),
    ]

    for sample in samples:
        canonical = _canonical(sample)
        assert isinstance(canonical, dict)
        expected = set(type(sample).model_fields) - DOCUMENTATION_FIELDS
        assert set(canonical) == expected, (
            f"{type(sample).__name__}: digest covers {sorted(canonical)}, "
            f"expected {sorted(expected)}"
        )

    assert DOCUMENTATION_FIELDS == {
        "description",
        "display_name",
        "input_description",
        "output_description",
    }


# -- AC2-AC5: the store refuses drift, and the escape hatch is audited --------


def test_a_fresh_store_records_the_fingerprint(tmp_path: Path) -> None:
    ontology = _evolution_ontology()
    store = ObjectStore(ontology.registry, str(tmp_path / "s.db"))

    recorded = store.read_ontology_fingerprint()
    assert recorded is not None
    assert recorded.digest == fingerprint_ontology(ontology.registry).digest


def test_reopening_with_the_same_ontology_is_fine(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    ObjectStore(_evolution_ontology().registry, path)
    ObjectStore(_evolution_ontology().registry, path)  # must not raise


def test_reopening_with_a_changed_ontology_is_refused_by_name(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    ObjectStore(_evolution_ontology().registry, path)

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(_evolution_ontology(with_owner=True).registry, path)

    message = str(exc_info.value)
    # AC3: names the type. "The ontology changed" is a message people route
    # around; this one points at the declaration to read.
    assert "object 'W'" in message
    assert "migrate_object_type" in message
    assert "accept_ontology_drift" in message
    # M9b: the third way out is named too, and first, because it is the one that
    # does not require either a rewrite or a shrug.
    assert "ontology.upcaster" in message


def test_drift_is_refused_before_any_read(tmp_path: Path) -> None:
    """AC4: at construction, not on first access to the affected type -- the
    same discipline as the store-version refusal follows."""
    path = str(tmp_path / "s.db")
    seeded = ObjectStore(_evolution_ontology().registry, path)
    seeded.insert("W", {"id": "org-1", "label": "x"}, SRC)

    with raises_code(ConflictError, "ONTOLOGY_DRIFT"):
        # Nothing is read here at all; construction alone must refuse.
        ObjectStore(_evolution_ontology(with_owner=True).registry, path)


def test_accepting_drift_proceeds_restamps_and_audits(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    ObjectStore(_evolution_ontology().registry, path)
    changed = _evolution_ontology(described="doc only, but forced").registry

    # Same shape, different prose -> no drift at all. Use a real shape change:
    changed = _evolution_ontology(with_owner=True).registry
    store = ObjectStore(changed, path, accept_ontology_drift=True)

    assert store.read_ontology_fingerprint() == fingerprint_ontology(changed)
    entry = store.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptOntologyDrift"
    # M9b sharpened the message: an unversioned shape change now says what to do
    # about it, because "declaration changed" was the message people route around.
    assert entry.params["changes"] == [
        "object 'W': declaration changed but `version` is still 1 -- bump it and "
        "declare an upcaster per step, or migrate the rows"
    ]
    assert entry.params["previous_digest"] != entry.params["current_digest"]
    # And the acceptance sticks: reopening with the accepted ontology is clean.
    ObjectStore(changed, path)


def test_accept_ontology_fingerprint_restamps_without_opening_twice(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "s.db")
    store = ObjectStore(_evolution_ontology().registry, path)
    changed = _evolution_ontology(with_owner=True).registry

    accept_ontology_fingerprint(store, changed)

    assert store.read_ontology_fingerprint() == fingerprint_ontology(changed)


# -- AC9: every write path enforces the declared shape ------------------------


@pytest.fixture(params=["sqlite", "in-memory"])
def store(request: pytest.FixtureRequest) -> Store:
    ontology = _evolution_ontology(with_owner=True)
    if request.param == "sqlite":
        return ObjectStore(ontology.registry)
    return InMemoryStore(ontology.registry)


def test_insert_missing_a_required_property_is_refused(store: Store) -> None:
    with raises_code(ValidationFailed, "INVALID_RECORD") as exc_info:
        store.insert("W", {"id": "org-1"}, SRC)

    assert "owner" in str(exc_info.value)
    assert store.read_current("W", "org-1") is None


def test_insert_with_a_wrong_typed_value_is_refused(store: Store) -> None:
    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.insert("W", {"id": "org-1", "owner": "o", "label": 7}, SRC)


def test_update_that_blanks_a_required_property_is_refused(store: Store) -> None:
    """The MERGED row is validated, which is the case a changes-only check
    would miss."""
    store.insert("W", {"id": "org-1", "owner": "o"}, SRC)

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        store.update("W", "org-1", {"owner": None}, SRC)

    current = store.read_current("W", "org-1")
    assert current is not None and current.payload["owner"] == "o"


def test_an_undeclared_key_is_still_allowed(store: Store) -> None:
    """Deliberate narrowing of AC9: hydration ignores extra keys, so they cannot
    cause the failure this enforcement exists to prevent -- and real rows carry
    them (a source's own foreign key on a type scoped `ViaLink`)."""
    store.insert("W", {"id": "org-1", "owner": "o", "source_ref": "abc"}, SRC)

    current = store.read_current("W", "org-1")
    assert current is not None and current.payload["source_ref"] == "abc"


def test_an_action_can_no_longer_commit_a_row_its_own_reader_refuses(
    make_consumer: ConsumerFactory,
) -> None:
    """THE defect this AC closes, stated as the spec measured it.

    Before M9a, `ctx.insert` skipped declared-shape validation entirely, so this
    action committed a row missing a required property and reported success --
    and the ontology's own typed read then raised a coded hydration error on it. The
    engine manufactured rows it could not read back.
    """
    ontology = _evolution_ontology(with_owner=True)
    store = InMemoryStore(ontology.registry)
    client = OntologyClient(
        ontology,
        store,
        make_consumer(
            actor_id="op",
            role="Op",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )

    with raises_code(ValidationFailed, "INVALID_RECORD"):
        client.execute("Make", {"w_id": "org-1"})

    assert store.read_current("W", "org-1") is None
    # The action failed, so the audit entry records an error rather than the
    # success the old behavior reported.
    assert store.audit_entries()[-1].outcome == "error"


# -- AC6/AC7: the rewrite tool ------------------------------------------------


def _seeded(tmp_path: Path) -> tuple[ObjectStore, Ontology]:
    ontology = _evolution_ontology()
    store = ObjectStore(ontology.registry, str(tmp_path / "s.db"))
    store.insert("W", {"id": "org-1", "label": "one"}, SRC)
    store.insert("W", {"id": "org-2", "label": "two"}, SRC)
    return store, ontology


def test_migrate_rewrites_every_row_and_audits_as_a_migration(tmp_path: Path) -> None:
    store, ontology = _seeded(tmp_path)

    report = migrate_object_type(
        store,
        ontology.registry,
        "W",
        lambda payload: {**payload, "label": str(payload["label"]).upper()},
    )

    assert (report.scanned, report.changed, report.unchanged) == (2, 2, 0)
    assert report.ok
    labels = {row.payload["label"] for row in store.read_all("W")}
    assert labels == {"ONE", "TWO"}

    entry = store.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "MigrateObjectType"
    assert entry.target_type == "W"
    assert entry.params["changed"] == 2


def test_dry_run_reports_without_writing(tmp_path: Path) -> None:
    store, ontology = _seeded(tmp_path)
    before = len(store.audit_entries())

    report = migrate_object_type(
        store,
        ontology.registry,
        "W",
        lambda payload: {**payload, "label": "rewritten"},
        dry_run=True,
    )

    assert (report.dry_run, report.scanned, report.changed) == (True, 2, 2)
    assert {row.payload["label"] for row in store.read_all("W")} == {"one", "two"}
    # No audit entry either: nothing happened, so nothing is recorded.
    assert len(store.audit_entries()) == before


def test_an_unchanged_row_is_not_rewritten(tmp_path: Path) -> None:
    """Rewriting an identical row would burn a version in the store's
    close-old-insert-new history and make the migration look like an edit to
    every row it touched."""
    store, ontology = _seeded(tmp_path)

    report = migrate_object_type(store, ontology.registry, "W", lambda payload: payload)

    assert (report.changed, report.unchanged) == (0, 2)
    assert len(store.read_all("W")) == 2


def test_one_poisonous_row_does_not_abort_the_run(tmp_path: Path) -> None:
    store, ontology = _seeded(tmp_path)

    def transform(payload: dict[str, Any]) -> dict[str, Any]:
        if payload["id"] == "org-1":
            raise ValueError("cannot migrate this one")
        return {**payload, "label": "fixed"}

    report = migrate_object_type(store, ontology.registry, "W", transform)

    assert (report.scanned, report.changed) == (2, 1)
    assert [f.object_id for f in report.failures] == ["org-1"]
    assert "cannot migrate this one" in report.failures[0].reason
    assert not report.ok
    # The healthy row was still migrated.
    labels = {row.payload["id"]: row.payload["label"] for row in store.read_all("W")}
    assert labels == {"org-1": "one", "org-2": "fixed"}
    assert store.audit_entries()[-1].outcome == "partial"


def test_a_result_violating_the_declared_shape_is_a_failure_not_a_write(
    tmp_path: Path,
) -> None:
    """A migration must not produce the very rows this milestone exists to
    eliminate."""
    store, ontology = _seeded(tmp_path)

    report = migrate_object_type(
        store,
        ontology.registry,
        "W",
        lambda payload: {**payload, "label": 42},  # declared `str`
    )

    assert report.changed == 0
    assert len(report.failures) == 2
    assert "declared shape" in report.failures[0].reason
    assert {row.payload["label"] for row in store.read_all("W")} == {"one", "two"}


def test_a_transform_that_drops_a_key_really_drops_it(tmp_path: Path) -> None:
    """`update` merges, so a naive implementation would silently turn "remove
    this property" into a no-op -- one of the five silent failures the spec
    measured."""
    store, ontology = _seeded(tmp_path)

    report = migrate_object_type(
        store,
        ontology.registry,
        "W",
        lambda payload: {"id": payload["id"]},
    )

    assert report.changed == 2
    for row in store.read_all("W"):
        # Nulled rather than absent -- see `migrate_object_type`'s comment on why
        # merge semantics make that the honest outcome.
        assert row.payload["label"] is None


# -- the whole scenario, end to end ------------------------------------------


def test_rename_a_property_under_live_data(tmp_path: Path) -> None:
    """What an author actually does, and what the engine now does about it.

    Before M9a: the new name read `None` while the old name kept the value in
    the payload -- data hiding that looked like data loss, with nothing refused
    and nothing recorded.
    """
    path = str(tmp_path / "s.db")
    old = _evolution_ontology()
    store = ObjectStore(old.registry, path)
    store.insert("W", {"id": "org-1", "label": "keep me"}, SRC)

    new = _evolution_ontology(renamed=True)

    # 1. The store refuses to open under the new declaration.
    with raises_code(ConflictError, "ONTOLOGY_DRIFT"):
        ObjectStore(new.registry, path)

    # 2. Open it deliberately to migrate: acknowledging the drift is the
    #    documented way in, and it is audited.
    migrating = ObjectStore(new.registry, path, accept_ontology_drift=True)
    report = migrate_object_type(
        migrating,
        new.registry,
        "W",
        lambda payload: {
            "id": payload["id"],
            "label_v2": payload.get("label"),
        },
    )
    assert (report.changed, report.ok) == (1, True)

    # 3. The value moved rather than vanished, and the old one is cleared.
    row = store.read_current("W", "org-1")
    assert row is not None
    assert row.payload["label_v2"] == "keep me"
    # `null`, not absent: `Store.update` merges, so the migration nulls a dropped
    # key rather than deleting it. The old VALUE is what mattered -- re-declaring
    # `label` later resurrects `None`, not "keep me".
    assert row.payload["label"] is None

    # 4. The store reopens clean under the new declaration.
    accept_ontology_fingerprint(migrating, new.registry)
    reopened = ObjectStore(new.registry, path)
    assert reopened.read_current("W", "org-1") is not None

    # 5. The whole episode is in the audit log, as migrations.
    kinds = [entry.kind for entry in reopened.audit_entries()]
    assert kinds.count("migration") == 2  # accepted drift + the rewrite


# =============================================================================
# M9b: type versions and declared upcasters (spec options B and C)
# =============================================================================


def _versioned(
    *,
    version: int = 1,
    with_upcaster: bool = True,
    broken_upcaster: bool = False,
) -> Ontology:
    """The same type at v1 (`label`) or v2 (`label_v2`), with the step between
    them declared or deliberately absent."""
    ontology = Ontology("evolution", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0",
        scope=[SelfScope(level="org")],
        owned=True,
        api_name="W",
        version=version,
    )
    class W(OntologyObject):
        id: str = prop(primary_key=True)
        if version >= 2:
            label_v2: str | None = None
        else:
            label: str | None = None

    if version >= 2 and with_upcaster:

        @ontology.upcaster(W, from_version=1)
        def v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
            if broken_upcaster:
                raise ValueError("this upcaster is broken")
            payload["label_v2"] = payload.pop("label", None)
            return payload

    ontology.validate()
    return ontology


def _seed_v1(tmp_path: Path, ontology: Ontology | None = None) -> str:
    path = str(tmp_path / "versioned.db")
    store = ObjectStore((ontology or _versioned()).registry, path)
    store.insert("W", {"id": "org-1", "label": "keep me"}, SRC)
    return path


# -- the chain is validated at declaration time -------------------------------


def test_a_version_bump_without_an_upcaster_is_refused_at_validate() -> None:
    """The gap is refused where it is cheap to fix, not on the first read of the
    one row that still needs the missing step -- possibly months later, in
    production."""
    with pytest.raises(Exception) as exc_info:
        _versioned(version=2, with_upcaster=False)

    message = str(exc_info.value)
    assert "no upcaster from version(s) [1]" in message
    assert exc_info.value.code == "ONTOLOGY_INVALID"  # type: ignore[attr-defined]


def test_an_upcaster_pointing_at_the_current_version_is_refused() -> None:
    ontology = Ontology("nope", scope_levels=["org"], min_n=1)

    @ontology.object(layer="L0", scope=[SelfScope(level="org")], api_name="V")
    class V(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.upcaster(V, from_version=1)
    def pointless(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    with pytest.raises(Exception) as exc_info:
        ontology.validate()
    assert "could never apply to a stored row" in str(exc_info.value)


def test_one_step_per_registration_a_jump_is_two_declarations() -> None:
    """A v1 -> v3 move needs two functions, not one.

    Built as two ontologies rather than one, because a failed `validate()` freezes
    registration -- which is itself the right behavior and worth not working
    around in a test.
    """

    def build(steps: int) -> Ontology:
        ontology = Ontology("steps", scope_levels=["org"], min_n=1)

        @ontology.object(
            layer="L0", scope=[SelfScope(level="org")], api_name="V", version=3
        )
        class V(OntologyObject):
            id: str = prop(primary_key=True)

        for from_version in range(1, steps + 1):

            @ontology.upcaster(V, from_version=from_version)
            def step(payload: dict[str, Any]) -> dict[str, Any]:
                return payload

        return ontology

    # Only the 1->2 step declared: the 2->3 gap is what `validate` names, because
    # a row sitting at v2 would otherwise be unreadable.
    with pytest.raises(Exception) as exc_info:
        build(1).validate()
    assert "version(s) [2]" in str(exc_info.value)

    build(2).validate()  # complete chain, no complaint


def test_a_duplicate_upcaster_is_refused() -> None:
    ontology = Ontology("dupe", scope_levels=["org"], min_n=1)

    @ontology.object(
        layer="L0", scope=[SelfScope(level="org")], api_name="V", version=2
    )
    class V(OntologyObject):
        id: str = prop(primary_key=True)

    @ontology.upcaster(V, from_version=1)
    def first(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    with pytest.raises(Exception, match="duplicate upcaster"):

        @ontology.upcaster(V, from_version=1)
        def second(payload: dict[str, Any]) -> dict[str, Any]:
            return payload


# -- a versioned, covered change opens without an acknowledgement -------------


def test_a_versioned_covered_change_opens_and_is_audited(tmp_path: Path) -> None:
    """The governance property M9b buys: **you may evolve, but you must version
    it.** No `accept_ontology_drift` needed here -- the rows are readable exactly
    as declared, so there is nothing for a human to acknowledge. It is still
    recorded, because a change happened."""
    path = _seed_v1(tmp_path)
    new = _versioned(version=2)

    store = ObjectStore(new.registry, path)  # must not raise

    entry = store.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptVersionedOntologyChange"
    assert entry.params["covered"] == [
        "object 'W': version 1 -> 2, covered by declared upcasters"
    ]
    recorded = store.read_ontology_fingerprint()
    assert recorded is not None and recorded.versions == {"W": 2}


def test_a_shape_change_without_a_version_bump_is_still_refused(
    tmp_path: Path,
) -> None:
    """The other half of the same property. Without this, "bump the version" is
    advice; with it, it is the only way through."""
    path = _seed_v1(tmp_path)
    unversioned = _evolution_ontology(with_owner=True)  # changed shape, still version 1

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(unversioned.registry, path)

    assert "`version` is still 1" in str(exc_info.value)


def test_a_downgrade_is_refused_by_name(tmp_path: Path) -> None:
    path = _seed_v1(tmp_path)
    ObjectStore(_versioned(version=2).registry, path)  # store now records v2

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(_versioned().registry, path)  # back to v1

    assert "downgrade" in str(exc_info.value)


def test_a_newly_declared_type_is_covered_not_refused(
    tmp_path: Path, make_object_type: ObjectTypeFactory
) -> None:
    path = _seed_v1(tmp_path)
    new = _versioned()

    new.registry.register_object_type(make_object_type("N"))
    new.registry.validate()
    store = ObjectStore(new.registry, path)  # no accept_ontology_drift needed

    entry = store.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptVersionedOntologyChange"
    assert entry.params["covered"] == [
        "object 'N': newly declared (no stored rows)"
    ]


def test_a_dropped_type_is_refused_and_the_drift_is_audited(
    tmp_path: Path, make_registry: Callable[..., Any]
) -> None:
    path = _seed_v1(tmp_path)
    current = make_registry()
    current.validate()

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(current, path)

    message = str(exc_info.value)
    assert "object 'W': no longer declared" in message
    assert "its rows, if any, are unreachable" in message

    accepted = ObjectStore(current, path, accept_ontology_drift=True)
    entry = accepted.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptOntologyDrift"
    assert entry.params["changes"] == [
        "object 'W': no longer declared (its rows, if any, are unreachable)"
    ]


def _seed_all_kinds(tmp_path: Path, ontology: Ontology | None = None) -> str:
    """Seed a store under `ontology` with an object row AND, when the link is
    declared, a real link row.

    The dropped-type refusal claims the type's "rows, if any, are
    unreachable". Seeding an actual link row is what keeps that claim
    exercised against a store where rows exist, rather than only against one
    where the branch is right for a vacuous reason.
    """
    seeded = ontology if ontology is not None else _all_kinds_ontology()
    path = str(tmp_path / "all-kinds.db")
    store = ObjectStore(seeded.registry, path)
    store.insert("W", {"id": "org-1", "label": "keep me"}, SRC)
    if "Rel" in seeded.registry.link_types:
        store.insert("W", {"id": "org-2", "label": "and me"}, SRC)
        store.create_link("Rel", "org-1", "org-2")
    return path


@pytest.mark.parametrize("kind", _CHANGEABLE_NON_OBJECT_KINDS)
def test_a_changed_non_object_declaration_is_refused_and_audited(
    tmp_path: Path, kind: str
) -> None:
    """The classifier's `kind != "object"` branch is kind-generic, so pin it
    for every kind that can reach it -- not just for an action.

    Narrowed to one kind, an author who edits a declared link, function or
    effect against an existing store gets no `ONTOLOGY_DRIFT` refusal and no
    `AcceptOntologyDrift` entry: the store opens silently and the branch's own
    claim (`store/protocol.py:221-222`, "every other mismatch must remain
    explicit and auditable") is false with a green suite.
    """
    path = _seed_all_kinds(tmp_path)
    changed = _all_kinds_ontology(changed=kind)
    expected = f"{kind} {_KIND_API_NAMES[kind]!r}: declaration changed"

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(changed.registry, path)

    assert expected in str(exc_info.value)

    accepted = ObjectStore(changed.registry, path, accept_ontology_drift=True)
    entry = accepted.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptOntologyDrift"
    assert entry.params["changes"] == [expected]


@pytest.mark.parametrize("kind", _NON_OBJECT_KINDS)
def test_a_dropped_non_object_type_is_refused_and_audited(
    tmp_path: Path, kind: str
) -> None:
    """The `now is None` branch is kind-generic too: dropping a declared link,
    action, function, capability or effect must refuse exactly as dropping an
    object does."""
    path = _seed_all_kinds(tmp_path)
    dropped = _all_kinds_ontology(omit=kind)
    expected = (
        f"{kind} {_KIND_API_NAMES[kind]!r}: no longer declared "
        "(its rows, if any, are unreachable)"
    )

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(dropped.registry, path)

    assert expected in str(exc_info.value)

    accepted = ObjectStore(dropped.registry, path, accept_ontology_drift=True)
    entry = accepted.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptOntologyDrift"
    assert entry.params["changes"] == [expected]


@pytest.mark.parametrize("kind", _NON_OBJECT_KINDS)
def test_a_newly_declared_non_object_type_is_covered_not_refused(
    tmp_path: Path, kind: str
) -> None:
    """And so is `was is None`: declaring a NEW link, action, function,
    capability or effect has no stored rows of its own, so it must open the
    store rather than refuse it."""
    path = _seed_all_kinds(tmp_path, _all_kinds_ontology(omit=kind))
    store = ObjectStore(_all_kinds_ontology().registry, path)  # no accept needed

    entry = store.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptVersionedOntologyChange"
    assert entry.params["covered"] == [
        f"{kind} {_KIND_API_NAMES[kind]!r}: newly declared (no stored rows)"
    ]


def _static_ann_name(annotation: ast.Subscript) -> str | None:
    value = annotation.value
    if isinstance(value, ast.Name):
        return value.id
    return value.attr if isinstance(value, ast.Attribute) else None


def _fingerprinted_kinds() -> set[str]:
    """The kind labels `fingerprint_ontology` writes, read from its SOURCE.

    Deriving them from a fingerprint of a fixture cannot work, and the way it
    fails is the dangerous direction: a kind the fixture declares no instance
    of is simply absent from `types`, and a kind that does not exist yet is
    exactly that case -- so a fixture-derived list agrees with itself while a
    seventh kind lands outside all three branch parametrizations. Reading the
    literals means a new kind reds this file no matter what the fixture
    happens to declare.  Same technique as `_source_kind_classes` in
    `tests/test_errors.py`, for the same reason.
    """
    tree = ast.parse(Path(fingerprint_module.__file__).read_text())
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "fingerprint_ontology"
    )
    tuple_loops = [
        node
        for node in ast.walk(function)
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Tuple)
    ]
    assert len(tuple_loops) == 1, (
        "fingerprint_ontology no longer walks exactly one literal kind tuple, "
        "so this guard can no longer read the kind list from its source"
    )

    kinds: set[str] = set()
    for element in tuple_loops[0].iter.elts:  # type: ignore[union-attr]
        assert (
            isinstance(element, ast.Tuple)
            and len(element.elts) == 2
            and isinstance(element.elts[0], ast.Constant)
            and isinstance(element.elts[0].value, str)
        ), (
            "every entry of fingerprint_ontology's kind tuple must open with a "
            f"literal kind label; {ast.unparse(element)!r} does not"
        )
        kinds.add(element.elts[0].value)
    assert kinds, "read no kind labels out of fingerprint_ontology"
    return kinds


def test_every_registry_collection_is_fingerprinted() -> None:
    """Every declared-descriptor collection on the registry is a pinned kind.

    This is the guard that closes the class, and it is pinned at the registry
    rather than at `fingerprint.py` deliberately. Both previous versions read
    a CONSUMER -- first a fingerprint of the fixture, then the one literal
    tuple in `fingerprint_ontology` -- and both failed OPEN, because a kind
    added by any other shape is invisible to a consumer-shaped check. A kind
    comes into existence as a `self._x: dict[...]` collection in
    `OntologyRegistry.__init__`, so that is what this reads -- not the
    accessor, whose shape (property, method, inherited) is incidental and was
    how the previous version failed open. `ScopePolicy` is the one declared
    thing that lives elsewhere (on `OntologyDef`) and is documented as
    deliberately outside the fingerprint in `src/ontary/fingerprint.py`.
    """
    source = ast.parse(Path(meta_module.__file__).read_text())
    registry_class = next(
        node
        for node in ast.walk(source)
        if isinstance(node, ast.ClassDef) and node.name == "OntologyRegistry"
    )
    assert not registry_class.bases, (
        "OntologyRegistry gained a base class; a collection declared on a "
        "mixin would be invisible to this scan, so widen it before adding one"
    )
    init = next(
        node
        for node in registry_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    # Read where a collection is CREATED, not how it happens to be exposed.
    # The previous version read `vars()` for `property` descriptors, so a
    # collection reached through a mixin or a plain method was invisible --
    # and OntologyRegistry already ships method-shaped accessors.
    declared_collections = {
        node.target.attr.lstrip("_")
        for node in ast.walk(init)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Attribute)
        and isinstance(node.annotation, ast.Subscript)
        and _static_ann_name(node.annotation) == "dict"
    }
    assert declared_collections, "read no descriptor collections off __init__"

    # Union, not replacement. This scan reads how a collection is EXPOSED and
    # so catches one created out of `__init__` (in a helper, or under an
    # annotation this scan cannot classify); the creation-site scan above
    # catches one exposed as a method or through a mixin. Each covers the
    # other's blind spot, and swapping one for the other -- which an earlier
    # revision did -- trades one fail-open for another.
    exposed_collections = {
        name
        for klass in OntologyRegistry.__mro__
        for name, value in vars(klass).items()
        if not name.startswith("_")
        and not callable(value)
        and isinstance(value, (property, functools.cached_property))
    }

    allowed_unfingerprinted: set[str] = {
        # Upcasters are version-migration machinery rather than a declared
        # type: their effect on drift travels in `OntologyFingerprint.versions`
        # and is pinned by the version/upcaster tests in this file.
        "upcasters",
    }
    assert set(_KIND_REGISTRY_COLLECTIONS) == {"object", *_NON_OBJECT_KINDS}
    pinned = set(_KIND_REGISTRY_COLLECTIONS.values()) | allowed_unfingerprinted
    assert exposed_collections <= pinned, (
        "OntologyRegistry exposes a descriptor collection that is not a "
        f"pinned kind: {sorted(exposed_collections - pinned)}"
    )
    assert declared_collections == pinned, (
        "OntologyRegistry's descriptor collections must be exactly the "
        "fingerprinted kinds plus the explicit exemptions: registry declares "
        f"{sorted(declared_collections)}, pinned {sorted(pinned)}"
    )

    for kind, collection in _KIND_REGISTRY_COLLECTIONS.items():
        assert hasattr(OntologyRegistry, collection), (
            f"{kind!r} is pinned to registry collection {collection!r}, "
            "which OntologyRegistry does not expose"
        )


def test_every_fingerprinted_kind_is_pinned() -> None:
    """`fingerprint_ontology`'s own kind tuple matches the pinned list.

    Scope is deliberately narrow: this pins ONE consumer, and a kind
    fingerprinted by some other shape is invisible to it -- that is exactly
    how the two previous versions of this guard failed open, so the claim is
    not made again here. `test_every_registry_collection_is_fingerprinted`
    is what closes the class; this catches the tuple and the pinned list
    drifting apart.
    """
    assert _fingerprinted_kinds() == {"object", *_NON_OBJECT_KINDS}
    assert set(_KIND_API_NAMES) == {"object", *_NON_OBJECT_KINDS}

    # ...and the fixture really does declare one instance of each, so the
    # parametrizations above exercise what this list promises.
    declared = {
        key.partition(":")[0]
        for key in fingerprint_ontology(_all_kinds_ontology().registry).types
    }
    assert declared == {"object", *_NON_OBJECT_KINDS}


def test_only_capability_has_no_changeable_declaration() -> None:
    """Justify capability's absence from `_CHANGEABLE_NON_OBJECT_KINDS`.

    A kind can reach the `declaration changed` branch only if its IR carries
    some non-documentation field beyond `api_name`. Driven from `model_fields`
    so that giving `CapabilityDef` a governance field (the way `LinkTypeDef`
    has `identity_revealing`) reds this test and forces the parametrization to
    widen -- rather than leaving the branch quietly unpinned for that kind.
    """
    ir_by_kind: dict[str, type[BaseModel]] = {
        "link": LinkTypeDef,
        "action": ActionTypeDef,
        "function": FunctionDef,
        "capability": CapabilityDef,
        "effect": EffectTypeDef,
    }
    assert set(ir_by_kind) == set(_NON_OBJECT_KINDS)

    changeable = {
        kind
        for kind, model in ir_by_kind.items()
        if set(model.model_fields) - DOCUMENTATION_FIELDS - {"api_name"}
    }
    assert changeable == set(_CHANGEABLE_NON_OBJECT_KINDS)
    assert set(_NON_OBJECT_KINDS) - changeable == {"capability"}


def test_a_version_bump_without_a_store_upcaster_is_refused_and_audited(
    tmp_path: Path,
) -> None:
    path = _seed_v1(tmp_path)
    changed = _versioned(version=2)
    # The authoring validator catches this earlier; remove the declaration so
    # this store-level test reaches the fingerprint classifier's missing-step
    # branch specifically.
    del changed.registry._upcasters[("W", 1)]

    with raises_code(ConflictError, "ONTOLOGY_DRIFT") as exc_info:
        ObjectStore(changed.registry, path)

    message = str(exc_info.value)
    assert "object 'W': version 1 -> 2, but no upcaster from version(s) [1]" in message

    accepted = ObjectStore(
        changed.registry, path, accept_ontology_drift=True
    )
    entry = accepted.audit_entries()[-1]
    assert entry.kind == "migration"
    assert entry.action == "AcceptOntologyDrift"
    assert entry.params["changes"] == [
        "object 'W': version 1 -> 2, but no upcaster from version(s) [1]"
    ]


# -- lazy read-time upcasting (option C) -------------------------------------


def test_an_old_row_reads_as_the_current_version(tmp_path: Path) -> None:
    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)

    row = store.read_current("W", "org-1")

    assert row is not None
    assert row.payload == {"id": "org-1", "label_v2": "keep me"}


def test_every_read_surface_sees_the_upcast_shape(
    tmp_path: Path,
    make_consumer: ConsumerFactory,
) -> None:
    """Upcasting at the STORE boundary rather than in typed hydration is what
    makes this true. Doing it in hydration alone would have recreated the exact
    defect M9a exists to prevent: one row, two readers, two realities."""
    from ontary.client import OntologyClient

    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)
    client = OntologyClient(
        new,
        store,
        make_consumer(
            actor_id="op",
            role="Op",
            scope_level="org",
            scope_id="org-1",
            kind="human",
        ),
    )
    typed_cls = new._classes["W"]

    assert store.read_current("W", "org-1").payload["label_v2"] == "keep me"  # type: ignore[union-attr]
    assert store.read_all("W")[0].payload["label_v2"] == "keep me"
    assert store.read_page("W")[0].obj.payload["label_v2"] == "keep me"
    # The string surface (what MCP serves) and the typed surface agree.
    assert client.get("W", "org-1").payload["label_v2"] == "keep me"  # type: ignore[union-attr]
    assert client.get(typed_cls, "org-1").label_v2 == "keep me"  # type: ignore[attr-defined]


def test_the_stored_row_is_untouched_by_a_lazy_read(tmp_path: Path) -> None:
    """Lazy means lazy: the chain runs on the way out and nothing is written, which
    is the trade `upcast_object_type` exists to undo."""
    import sqlite3

    path = _seed_v1(tmp_path)
    store = ObjectStore(_versioned(version=2).registry, path)
    store.read_current("W", "org-1")

    conn = sqlite3.connect(path)
    try:
        version, payload = conn.execute(
            "SELECT type_version, payload FROM objects WHERE valid_to IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert version == 1
    assert set(json.loads(payload)) == {"id", "label"}


def test_a_raising_upcaster_fails_the_read_with_a_coded_error(
    tmp_path: Path,
) -> None:
    """Not a silent fallback to the raw payload: handing a consumer data in a
    shape the declaration says does not exist is the whole class of failure this
    milestone is about."""
    path = _seed_v1(tmp_path)
    store = ObjectStore(_versioned(version=2, broken_upcaster=True).registry, path)

    with raises_code(ConflictError, "UPCAST_FAILED") as exc_info:
        store.read_current("W", "org-1")

    message = str(exc_info.value)
    assert "org-1" in message  # names the row
    assert "version 1" in message  # names the step
    assert "this upcaster is broken" in message  # names the author's error


def test_a_write_after_a_lazy_read_lands_at_the_current_version(
    tmp_path: Path,
) -> None:
    """`update` merges a current-shape change over an already-upcast payload, so
    the result is current by construction -- which is why the write path stamps
    the declared version unconditionally."""
    import sqlite3

    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)

    store.update("W", "org-1", {"label_v2": "rewritten"}, SRC)

    conn = sqlite3.connect(path)
    try:
        version, payload = conn.execute(
            "SELECT type_version, payload FROM objects WHERE valid_to IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert version == 2
    assert set(json.loads(payload)) == {"id", "label_v2"}
    assert store.read_current("W", "org-1").payload["label_v2"] == "rewritten"  # type: ignore[union-attr]


# -- making it permanent (option B's batch half) ------------------------------


def test_upcast_object_type_rewrites_and_restamps(tmp_path: Path) -> None:
    import sqlite3

    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)

    report = upcast_object_type(store, new.registry, "W")

    assert (report.scanned, report.changed, report.ok) == (1, 1, True)
    conn = sqlite3.connect(path)
    try:
        version, payload = conn.execute(
            "SELECT type_version, payload FROM objects WHERE valid_to IS NULL"
        ).fetchone()
    finally:
        conn.close()
    assert version == 2
    assert set(json.loads(payload)) == {"id", "label_v2"}


def test_upcast_dry_run_writes_nothing(tmp_path: Path) -> None:
    import sqlite3

    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)

    report = upcast_object_type(store, new.registry, "W", dry_run=True)

    assert (report.dry_run, report.scanned, report.changed) == (True, 1, 1)
    conn = sqlite3.connect(path)
    try:
        version = conn.execute(
            "SELECT type_version FROM objects WHERE valid_to IS NULL"
        ).fetchone()[0]
    finally:
        conn.close()
    assert version == 1


def test_after_an_upcast_the_read_path_does_no_work(tmp_path: Path) -> None:
    """The point of the rewrite: afterwards the upcaster is dead code the author
    can delete on the next bump, because no stored row needs it any more."""
    path = _seed_v1(tmp_path)
    new = _versioned(version=2)
    store = ObjectStore(new.registry, path)
    upcast_object_type(store, new.registry, "W")

    # Same declaration, but with an upcaster that would EXPLODE if it ran.
    exploding = _versioned(version=2, broken_upcaster=True)
    reopened = ObjectStore(exploding.registry, path)

    row = reopened.read_current("W", "org-1")
    assert row is not None and row.payload["label_v2"] == "keep me"
