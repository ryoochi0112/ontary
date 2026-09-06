"""Pins the ontology design guide against the SDK and its translation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import ontary
from ontary import OntologyObject, Sensitivity
from ontary.meta import (
    ActionTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
)
from ontary.scope import ScopePolicy

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _ROOT / "docs"
DESIGN_GUIDES = (
    _DOCS / "ontology-design.md",
    _DOCS / "ontology-design.ja.md",
)
README = _ROOT / "README.md"

_IDENTIFIER = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)`")
_HEADING = re.compile(r"^#{2,3} (.+)$", re.MULTILINE)

# Every addition is review-visible: keep deliberately non-SDK names minimal and sorted.
ILLUSTRATIVE = (
    "EngagementScoreSnapshot",
    "Survey2024",
    "SurveyResponseV2",
    "V2",
    "valid_from",
    "valid_to",
)

# These are deliberately documented engine descriptors, not front-door
# authoring names.  Keep them in the vocabulary guard after T10 demotes them;
# deleting one from its defining module or the design guide still fails the
# identifier check rather than turning into an unknown-name false positive.
ENGINE_IDENTIFIERS = {
    "ActionTypeDef",
    "AuditEntry",
    "FunctionDef",
    "LinkTypeDef",
    "ObjectTypeDef",
    "OntologyFingerprint",
    "PropertyDef",
    "ScopePolicy",
    "migrate_object_type",
}

_COVERAGE_INVENTORY = (
    "Domain-driven design",
    "Don't repeat yourself (rule of three)",
    "Open for extension, closed for modification",
    "Composition over deep hierarchies",
    "Normalization and derived values",
    "Structs",
    "Interfaces",
    "Links and object-backed link types",
    "Naming conventions",
    "Security design",
    "System Silos",
    "The Kitchen Sink",
    "Department Silos",
    "The God Object",
    "The Golden Hammer",
    "Action Sprawl",
    "The Time Machine",
    "The Misnomer",
)


def _identifiers(path: Path) -> set[str]:
    return set(_IDENTIFIER.findall(path.read_text()))


def _headings(path: Path) -> list[str]:
    return _HEADING.findall(path.read_text())


def _sdk_vocabulary() -> set[str]:
    cited_models = (
        ObjectTypeDef,
        PropertyDef,
        LinkTypeDef,
        ActionTypeDef,
        FunctionDef,
        ScopePolicy,
        Sensitivity,
        OntologyObject,
    )
    fields = {
        field_name
        for model in cited_models
        for field_name in model.model_fields
    }
    return set(ontary.__all__) | fields | set(ILLUSTRATIVE) | ENGINE_IDENTIFIERS


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_identifiers_resolve_against_sdk(path: Path) -> None:
    """Fictitious/renamed API names must fail instead of becoming doc folklore."""
    unknown = _identifiers(path) - _sdk_vocabulary()
    assert unknown == set(), f"{path.name}: unknown identifiers: {sorted(unknown)}"


def test_design_guide_heading_order_matches_translation() -> None:
    """Bilingual drift must not reorder, add, or remove an EN/JA guide entry."""
    english, japanese = DESIGN_GUIDES
    assert _headings(english) == _headings(japanese)


def test_design_guide_identifier_set_matches_translation() -> None:
    """Bilingual drift must not leave an SDK identifier in only one language."""
    english, japanese = DESIGN_GUIDES
    assert _identifiers(english) == _identifiers(japanese)


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_contains_complete_coverage_inventory(path: Path) -> None:
    """Silent entry deletion must fail even if both translations lose the entry."""
    missing = set(_COVERAGE_INVENTORY) - set(_headings(path))
    assert missing == set(), f"{path.name}: missing guide entries: {sorted(missing)}"


def test_readme_links_both_design_guides() -> None:
    """Discoverability drift must not strand either language's design guide."""
    readme = README.read_text()
    assert {
        "docs/ontology-design.md",
        "docs/ontology-design.ja.md",
    } - set(re.findall(r"\((docs/[^)]+)\)", readme)) == set()
