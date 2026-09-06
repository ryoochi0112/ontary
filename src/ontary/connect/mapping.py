"""Declarative canonical -> ontology bindings (spec connector-framework.md
§5 Approach `mapping.py`, §3 AC7, §7 edge cases).

Bindings are Pydantic models, not a builder API, so they stay
introspectable and validatable *before* any pipeline run (spec §5: "rejects
references to undeclared object/link types, unknown ontology properties, and
canonical fields absent from the bound model -- messages name the offending
binding", AC7). Bindings that avoid the optional `transform` escape hatch are
also plain-data serializable (e.g. `model_dump()` round-trips); a bound
`transform` callable is Python-only and does not survive JSON/dict
serialization (§5 decision: soften the serializable-bindings claim rather
than redesign for serializable normalizers).

`ObjectBinding` binds one canonical entity name (a `CanonicalBatch` key,
see `canonical.py`) to one ontology object type: which canonical field is
the idempotent-id key, a plain field-rename `property_map`, and an optional
`transform` escape hatch for domain logic that exceeds a pure rename (spec
§10 Risks "Binding expressiveness"). `LinkBinding` declares a link between
two `ObjectBinding`-bound entities; the mapper (T3) resolves each side's key
value to the bound object's idempotent id at run time -- this module only
validates that the two entities/keys/types are declared and consistent with
the registry.

IMPORTANT: like `canonical.py`, this module imports `ontary.meta` (the
ontology *descriptors*, purely to validate against) but never
`ontary.store`/`ontary.security`/`ontary.actions`/`ontary.functions` --
binding *execution* against a live store is the mapper's job (T3), not
this one's.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ontary.connect.canonical import CanonicalRecord
from ontary.errors import ValidationFailed
from ontary.meta import OntologyRegistry


class MappingValidationError(Exception):
    """Raised by `MappingSpec.validate()` for a dangling/inconsistent
    binding. Every message names the offending binding (spec AC7)."""


class ObjectBinding(BaseModel):
    """Binds one canonical entity (a `CanonicalBatch` key) to one ontology
    object type.

    `key_field` is the canonical field whose value becomes the input to the
    mapper's idempotent-id derivation (T3). `property_map` renames
    canonical fields to ontology property names; `transform`, when given, is
    called with the canonical record and its output dict (ontology property
    -> value) is merged *over* the `property_map` result -- an escape hatch
    for domain logic beyond a pure field rename (spec §10 Risks).

    `record_model`, when given, lets `validate()` check that `key_field` and
    every `property_map` key actually exist on that canonical model (spec
    §7 "Binding references undeclared type/property/field").
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    entity: str
    object_type: str
    key_field: str
    property_map: dict[str, str] = Field(default_factory=dict)
    record_model: type[CanonicalRecord] | None = None
    transform: Callable[[CanonicalRecord], dict[str, Any]] | None = None


class LinkBinding(BaseModel):
    """Declares a link between two `ObjectBinding`-bound entities.

    `from_entity`/`to_entity` refer to `ObjectBinding.entity` names (not
    canonical entity data directly). BOTH `from_key_field` AND `to_key_field`
    name fields on the FROM-SIDE canonical record (mapper.py's `_map_links`
    iterates `batch.get(link.from_entity)` and reads both key values off the
    SAME record) -- `from_key_field`'s value is looked up against the
    from-side `ObjectBinding`'s own ids (keyed by its `key_field`), and
    `to_key_field`'s value is looked up against the to-side `ObjectBinding`'s
    ids (keyed by ITS `key_field`) -- so `to_key_field` is typically a
    foreign-key-shaped field on the from-side model (e.g. a `Membership`
    record's `team_id` field), not a field on the to-side model.
    """

    link_type: str
    from_entity: str
    from_key_field: str
    to_entity: str
    to_key_field: str


class MappingSpec(BaseModel):
    """The full set of bindings an author declares for one connector run."""

    object_bindings: list[ObjectBinding] = Field(default_factory=list)
    link_bindings: list[LinkBinding] = Field(default_factory=list)

    def validate(self, registry: OntologyRegistry) -> None:  # type: ignore[override]
        """Raise `MappingValidationError` on any dangling/inconsistent
        binding reference (spec AC7), collecting every problem found rather
        than stopping at the first.

        Deliberately shadows pydantic's deprecated `BaseModel.validate`
        classmethod (spec-mandated method name/signature) -- the narrow
        `# type: ignore[override]` above is for that incompatible-override
        mismatch, nothing broader.
        """
        errors: list[str] = []
        bound_entities: dict[str, ObjectBinding] = {}

        for binding in self.object_bindings:
            if binding.entity in bound_entities:
                errors.append(
                    f"ObjectBinding for entity {binding.entity!r}: "
                    f"entity is bound more than once"
                )
            else:
                bound_entities[binding.entity] = binding
            errors.extend(self._validate_object_binding(binding, registry))

        for link in self.link_bindings:
            errors.extend(
                self._validate_link_binding(link, registry, bound_entities)
            )

        if errors:
            raise MappingValidationError("; ".join(errors))

    def _validate_object_binding(
        self, binding: ObjectBinding, registry: OntologyRegistry
    ) -> list[str]:
        errors: list[str] = []
        label = f"ObjectBinding(entity={binding.entity!r})"

        try:
            object_type = registry.get_object_type(binding.object_type)
        except ValidationFailed:
            errors.append(
                f"{label}: undeclared object_type {binding.object_type!r}"
            )
            object_type = None

        if object_type is not None:
            property_names = {p.name for p in object_type.properties}
            for canonical_field, ontology_property in binding.property_map.items():
                if ontology_property not in property_names:
                    errors.append(
                        f"{label}: property_map value {ontology_property!r} "
                        f"(from canonical field {canonical_field!r}) is not "
                        f"a property of object type {binding.object_type!r}"
                    )

        if binding.record_model is not None:
            model_fields = set(binding.record_model.model_fields)
            canonical_fields = {binding.key_field, *binding.property_map}
            for field in sorted(canonical_fields - model_fields):
                errors.append(
                    f"{label}: canonical field {field!r} not found on "
                    f"record_model {binding.record_model.__name__!r}"
                )

        return errors

    def _validate_link_binding(
        self,
        link: LinkBinding,
        registry: OntologyRegistry,
        bound_entities: dict[str, ObjectBinding],
    ) -> list[str]:
        errors: list[str] = []
        label = f"LinkBinding(link_type={link.link_type!r})"

        try:
            link_type = registry.get_link_type(link.link_type)
        except ValidationFailed:
            errors.append(f"{label}: undeclared link_type {link.link_type!r}")
            link_type = None

        from_binding = bound_entities.get(link.from_entity)
        if from_binding is None:
            errors.append(
                f"{label}: from_entity {link.from_entity!r} is not bound "
                f"by any ObjectBinding"
            )

        to_binding = bound_entities.get(link.to_entity)
        if to_binding is None:
            errors.append(
                f"{label}: to_entity {link.to_entity!r} is not bound by "
                f"any ObjectBinding"
            )

        if link_type is not None:
            if from_binding is not None and from_binding.object_type != link_type.from_type:
                errors.append(
                    f"{label}: from_entity {link.from_entity!r} is bound to "
                    f"object type {from_binding.object_type!r}, but "
                    f"link_type {link.link_type!r} declares from_type "
                    f"{link_type.from_type!r}"
                )
            if to_binding is not None and to_binding.object_type != link_type.to_type:
                errors.append(
                    f"{label}: to_entity {link.to_entity!r} is bound to "
                    f"object type {to_binding.object_type!r}, but "
                    f"link_type {link.link_type!r} declares to_type "
                    f"{link_type.to_type!r}"
                )

        if (
            from_binding is not None
            and from_binding.record_model is not None
            and link.from_key_field not in from_binding.record_model.model_fields
        ):
            errors.append(
                f"{label}: from_key_field {link.from_key_field!r} not found "
                f"on record_model {from_binding.record_model.__name__!r} "
                f"(bound by entity {link.from_entity!r})"
            )

        if (
            from_binding is not None
            and from_binding.record_model is not None
            and link.to_key_field not in from_binding.record_model.model_fields
        ):
            errors.append(
                f"{label}: to_key_field {link.to_key_field!r} not found on "
                f"record_model {from_binding.record_model.__name__!r} (bound "
                f"by entity {link.from_entity!r} -- the mapper resolves "
                f"to_key_field's VALUE from the from-side record at run "
                f"time, see mapper.py's _map_links)"
            )

        return errors
