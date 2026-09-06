"""Ontology fingerprinting: detect that a declared type changed under live data.

The store's physical schema has been versioned since M4c. The *ontology
definition* -- the author's declared object/link/action/function types -- had
nothing, so changing a declared type against existing rows was undefined
behavior (spec `ontology-evolution` §1 measured what "undefined" means: five
silent misbehaviours, of which the worst is a row the typed reader refuses and
the string reader happily returns).

This module answers one question: **is the ontology declared right now the same
one this store's rows were written under?** It computes a digest of the
descriptor IR, per type and overall. `ontary.store` persists it and refuses a
mismatch; `ontary.migrate` is what you run when the answer is no.

What is deliberately NOT here: any notion of a type *version*, or an upcaster.
Detection and refusal only (option A of the spec's four). Evolving a type is
`ontary.migrate.migrate_object_type`, which rewrites rows explicitly rather
than reinterpreting them on read.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict

from ontary.meta import ObjectTypeDef, OntologyRegistry, PropertyDef

__all__ = [
    "DOCUMENTATION_FIELDS",
    "OntologyFingerprint",
    "fingerprint_ontology",
]


DOCUMENTATION_FIELDS = frozenset(
    {"description", "display_name", "input_description", "output_description"}
)
"""IR fields excluded from every digest: prose for humans, not shape for data.

CEO decision 2026-07-26, answering the spec's one open question. The digest
exists to detect changes that affect *stored data*, and a store refused because
someone improved a docstring is a store whose drift check gets switched off --
at which point the check protects nothing. `description` was the field named;
`display_name` and the two Function prose fields are the same category and are
excluded for the same reason, which is a decision this module states rather than
hides.

Everything else is IN, including fields that look cosmetic but are not:
`layer` (an organizing claim other tooling reads), `identity_revealing` and
`sensitivity` (governance), `audit` (whether a function is audited at all).
Pinned field-by-field by `tests/test_fingerprint.py`, driven from
`model_fields`, so a NEW IR field fails a test rather than silently landing
outside the digest.
"""


def _canonical(value: Any) -> Any:
    """Strip documentation fields recursively and sort every mapping.

    Sorting is what makes the digest independent of declaration ORDER: two
    authors who declare the same two properties in a different sequence must
    fingerprint identically, or the check fires on a refactor that changed
    nothing about the data.
    """
    if isinstance(value, ObjectTypeDef):
        dumped = value.model_dump(mode="json", exclude={"properties"})
        dumped["properties"] = [_canonical(prop) for prop in value.properties]
        return _canonical(dumped)
    if isinstance(value, BaseModel):
        dumped = value.model_dump(mode="json")
        if isinstance(value, PropertyDef) and value.choices is None:
            dumped.pop("choices")
        return _canonical(dumped)
    if isinstance(value, dict):
        return {
            key: _canonical(item)
            for key, item in sorted(value.items())
            if key not in DOCUMENTATION_FIELDS
        }
    if isinstance(value, (list, tuple)):
        items = [_canonical(item) for item in value]
        # Declared collections (properties, parameters, roles) are sets in
        # spirit: the IR keeps them ordered because Python does, and nothing
        # about the data depends on that order.
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def _digest(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(_canonical(payload), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class OntologyFingerprint(BaseModel):
    """One ontology's shape, as a value that can be stored and compared.

    `types` is per-type so a mismatch can say *which* type changed instead of
    "something did" -- the difference between a refusal an operator can act on
    and one they learn to bypass (spec AC3).
    """

    model_config = ConfigDict(frozen=True)

    digest: str
    """sha256 over the whole canonical IR. The single value the store compares."""

    types: dict[str, str]
    """`"object:Widget"` / `"link:inQueue"` / `"action:Advance"` / ... -> digest.
    Namespaced by kind because api_names are only unique WITHIN a kind, so a
    flat map would silently merge two declarations into one entry."""

    versions: dict[str, int] = {}
    """Object api_name -> declared `version` at the time this fingerprint was
    recorded (M9b). Stored alongside the digests because a digest can only say
    THAT a type changed; deciding whether the change is *covered by upcasters*
    needs to know which version the rows are at. Empty on a fingerprint written
    before versions existed, which reads as "everything was at version 1"."""


def fingerprint_ontology(registry: OntologyRegistry) -> OntologyFingerprint:
    """Fingerprint every declared type on `registry`.

    Covers the registry's descriptors: object, link, action, function, and
    capability types.

    **The `ScopePolicy` is deliberately NOT included**, and this is a real
    narrowing of the spec's AC1 rather than an oversight. A `ScopePolicy` lives
    on `OntologyDef`, not on the registry, and the store is constructed with a
    registry alone (`ObjectStore(ontology.registry)`) -- so including scope
    rules would mean either changing every store construction site or
    fingerprinting something the store cannot see. The exposure is small and
    already covered elsewhere: a scope rule referencing a property that does not
    exist is refused at `ontology.validate()` (`SCOPE_POLICY_ERROR`), and
    changing which *declared* property a rule reads changes visibility for new
    and old rows alike, with no stored data to migrate. Stated here because a
    reader of AC1 would otherwise expect it.
    """
    types: dict[str, str] = {}
    for kind, definitions in (
        ("object", registry.object_types),
        ("link", registry.link_types),
        ("action", registry.action_types),
        ("function", registry.functions),
        ("capability", registry.capabilities),
    ):
        for api_name, definition in definitions.items():
            types[f"{kind}:{api_name}"] = _digest(definition)
    return OntologyFingerprint(
        digest=_digest(types),
        types=types,
        versions={
            api_name: definition.version
            for api_name, definition in registry.object_types.items()
        },
    )
