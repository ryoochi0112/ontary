"""Pins the ontology design guide against the SDK and its translation."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

import pytest

import ontary
from ontary import OntologyObject, Sensitivity, Source
from ontary.actions import ActionContext
from ontary.meta import (
    ActionTypeDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
    TransitionDef,
)
from ontary.scope import ScopePolicy
from ontary.store import Store
from ontary.testing import consumer, scenario

_ROOT = Path(__file__).resolve().parent.parent
_DOCS = _ROOT / "docs"
DESIGN_GUIDES = (
    _DOCS / "ontology-design.md",
    _DOCS / "ontology-design.ja.md",
)
README = _ROOT / "README.md"
_SOURCE = Source(source_system="guide-example")

_IDENTIFIER = re.compile(
    r"`([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)`"
)
_HEADING = re.compile(r"^#{2,3} (.+)$", re.MULTILINE)

# Every addition is review-visible: keep deliberately non-SDK names minimal and sorted.
ILLUSTRATIVE = (
    "CancelSubscription",
    "DeleteEmployee",
    "EngagementScoreSnapshot",
    "Order",
    "OrderStatus",
    "OffboardEmployee",
    "OrderShipped",
    "RemoveEmployee",
    "ShipOrder",
    "Survey2024",
    "SurveyResponseV2",
    # Authority code named in the retirement section of both guides.
    "UNDECLARED_SOURCE_REMOVAL",
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
    "PropertyDef",
    "ScopePolicy",
    "TransitionDef",
}

# These are the actual objects behind the engine names that may be used as
# dotted-name owners.  A string allow-list would validate only the left side
# of a reference and let a renamed or nonexistent member become guide folklore.
ENGINE_SYMBOLS = {
    "ActionContext": ActionContext,
    "Store": Store,
}

RETIREMENT_DOTTED_IDENTIFIERS = frozenset(
    {
        "ActionContext.retire",
        "ActionContext.unlink",
        "Store.retire_object",
    }
)

_COVERAGE_INVENTORY = (
    "Start from the domain's language",
    "Don't repeat yourself (rule of three)",
    "Open for extension, closed for modification",
    "Composition over deep hierarchies",
    "Normalization and derived values",
    "Structs",
    "Events",
    "Interfaces",
    "Links and object-backed link types",
    "Naming conventions",
    "Retirement and removal",
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


def _retirement_section(path: Path) -> str:
    text = path.read_text()
    start_heading = "### Retirement and removal"
    start = text.find(start_heading)
    if start == -1:
        return ""
    end = text.find("\n### ", start + len(start_heading))
    return text[start:] if end == -1 else text[start:end]


def _resolve_dotted_identifier(identifier: str) -> object:
    owner_name, *attributes = identifier.split(".")
    if not attributes:
        raise ValueError(f"not a dotted identifier: {identifier!r}")

    if owner_name in ontary.__all__:
        value: object = getattr(ontary, owner_name)
    else:
        value = ENGINE_SYMBOLS[owner_name]
    for attribute in attributes:
        value = getattr(value, attribute)
    return value


def _headings(path: Path) -> list[str]:
    return _HEADING.findall(path.read_text())


def _sdk_vocabulary() -> set[str]:
    cited_models = (
        ObjectTypeDef,
        PropertyDef,
        TransitionDef,
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
    bare_identifiers = {
        identifier for identifier in _identifiers(path) if "." not in identifier
    }
    unknown = bare_identifiers - _sdk_vocabulary()
    assert unknown == set(), f"{path.name}: unknown identifiers: {sorted(unknown)}"


def test_design_guide_heading_order_matches_translation() -> None:
    """Bilingual drift must not reorder, add, or remove an EN/JA guide entry."""
    english, japanese = DESIGN_GUIDES
    assert _headings(english) == _headings(japanese)


def _rules_transitions_section(path: Path) -> str:
    match = re.search(
        r"(?ms)^### Rules and status transitions\n(.*?)(?=^### |^## |\Z)",
        path.read_text(),
    )
    return "" if match is None else match.group(1)


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_explains_rules_and_transitions(path: Path) -> None:
    """The two guides explain the same write guarantees and authoring choices."""
    section = _rules_transitions_section(path)
    assert section, f"{path.name}: missing Rules and status transitions section"
    assert "prop(transitions=TransitionDef(" in section
    assert "@_ontology.rule(Order," in section

    if path.name.endswith(".ja.md"):
        required = (
            "遷移グラフ",
            "アクションの事前条件",
            "別オブジェクト",
            "純粋",
            "すべてのプロパティ",
            "ingest",
            "宣言済みの任意の状態",
            "既存行",
            "ルール名",
        )
    else:
        required = (
            "transition graph",
            "action precondition",
            "cross-object",
            "pure",
            "every declared property",
            "ingest",
            "any declared state",
            "existing row",
            "rule name",
        )
    normalized = re.sub(r"\s+", " ", section).casefold()
    missing = [phrase for phrase in required if phrase.casefold() not in normalized]
    assert not missing, f"{path.name}: rules/transitions section omits {missing}"


@pytest.mark.parametrize(
    "path",
    (_DOCS / "api-reference.md", _DOCS / "api-reference.ja.md"),
    ids=lambda path: path.name,
)
def test_api_reference_documents_transitions_rules_and_mcp_schema(path: Path) -> None:
    """Both API references document the public authoring and MCP contracts."""
    text = path.read_text()
    required = [
        "prop(transitions=...",
        "Ontology.rule",
        "list_object_types",
        "`transitions`",
        "`rules`",
        "TRANSITION_NOT_ALLOWED",
        "RULE_VIOLATED",
        "model_dump()",
    ]
    if path.name.endswith(".ja.md"):
        required.extend(("`TransitionDef`", "`ontary.meta`", "`ontary.__all__`"))
    else:
        required.extend(("import `TransitionDef` from", "`ontary.meta`", "`ontary.__all__`"))
    missing = [phrase for phrase in required if phrase not in text]
    assert not missing, f"{path.name}: API reference omits {missing}"


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_rules_and_transitions_example_runs(path: Path) -> None:
    """The guide's example builds, and its transition and rule both refuse."""
    from ontary.errors import OntaryError
    from ontary.store import InMemoryStore

    blocks = re.findall(r"(?ms)^```python\n(.*?)^```", _rules_transitions_section(path))
    assert len(blocks) == 1, f"{path.name}: expected one python example"
    namespace: dict[str, object] = {"__name__": f"guide_example_{path.stem.replace('.', '_')}"}
    exec(compile(blocks[0], str(path), "exec", dont_inherit=True), namespace)

    ontology = namespace["_ontology"]
    assert isinstance(ontology, ontary.Ontology)
    store = InMemoryStore(ontology.registry)
    store.insert("Order", {"id": "o-1", "status": "pending", "paid": False}, source=_SOURCE)
    with pytest.raises(OntaryError) as moved:
        store.update("Order", "o-1", {"status": "shipped"}, source=_SOURCE)
    assert moved.value.code == "TRANSITION_NOT_ALLOWED"
    store.update("Order", "o-1", {"status": "paid"}, source=_SOURCE)
    with pytest.raises(OntaryError) as unpaid:
        store.update("Order", "o-1", {"status": "shipped"}, source=_SOURCE)
    assert unpaid.value.code == "RULE_VIOLATED"
    assert "shipped_needs_payment" in str(unpaid.value)


def test_changelog_covers_declared_rules_and_transitions() -> None:
    """Version-agnostic: pins the release section that carries #44, so the
    test survives the Unreleased section being cut into a version."""
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    sections = re.findall(r"(?ms)^## \[[^\]]+\][^\n]*\n(.*?)(?=^## \[|\Z)", changelog)
    matching = [section for section in sections if "(#44)" in section]
    assert len(matching) == 1, "CHANGELOG must carry exactly one #44 release section"
    unreleased = matching[0]
    added_match = re.search(r"(?ms)^### Added\n(.*?)(?=^### |\Z)", unreleased)
    changed_match = re.search(r"(?ms)^### Changed\n(.*?)(?=^### |\Z)", unreleased)
    assert added_match is not None, "Unreleased changelog is missing ### Added"
    assert changed_match is not None, "Unreleased changelog is missing ### Changed"

    added = added_match.group(1)
    for phrase in (
        "#44",
        "transitions",
        "rules",
        "TRANSITION_NOT_ALLOWED",
        "RULE_VIOLATED",
        "SCHEMA_VERSION",
        "unchanged",
    ):
        assert phrase in added, f"Unreleased Added omits {phrase!r}"

    changed = changed_match.group(1)
    for phrase in (
        "current row",
        "transaction",
        "list_object_types",
        "`transitions`",
        "`rules`",
        "RuleDef",
        "callable",
        "model_dump",
    ):
        assert phrase in changed, f"Unreleased Changed omits {phrase!r}"


def test_design_guide_identifier_set_matches_translation() -> None:
    """Bilingual drift must not leave an SDK identifier in only one language."""
    english, japanese = DESIGN_GUIDES
    assert _identifiers(english) == _identifiers(japanese)


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_retirement_content_dotted_identifiers_resolve_against_engine(
    path: Path,
) -> None:
    """Retirement prose must cite live engine members, in both translations."""
    dotted = {
        identifier
        for identifier in _IDENTIFIER.findall(_retirement_section(path))
        if "." in identifier
    }
    unresolved: dict[str, str] = {}
    for identifier in sorted(dotted):
        try:
            _resolve_dotted_identifier(identifier)
        except (AttributeError, KeyError, ValueError) as exc:
            unresolved[identifier] = str(exc)
    assert unresolved == {}, f"{path.name}: unresolved dotted identifiers: {unresolved}"

    missing_content = RETIREMENT_DOTTED_IDENTIFIERS - dotted
    assert missing_content == set(), (
        f"{path.name}: retirement content omits {sorted(missing_content)}"
    )


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


def _events_section(path: Path) -> str:
    match = re.search(r"(?ms)^### Events\n(.*?)(?=^### |^## |\Z)", path.read_text())
    return "" if match is None else match.group(1)


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_events_section_example_runs(path: Path) -> None:
    """The Events sample executes and emits a fact about its resolved target."""
    section = _events_section(path)
    assert section, f"{path.name}: missing Events section"
    blocks = re.findall(r"(?ms)^```python\n(.*?)^```", section)
    assert len(blocks) == 1, f"{path.name}: expected one python example"
    namespace: dict[str, object] = {"__name__": f"guide_events_{path.stem.replace('.', '_')}"}
    exec(compile(blocks[0], str(path), "exec", dont_inherit=True), namespace)
    ontology = namespace["_ontology"]
    assert isinstance(ontology, ontary.Ontology)
    assert ontology.registry.get_event_type("OrderShipped") is not None
    Order = namespace["Order"]
    ShipOrder = namespace["ShipOrder"]
    OrderShipped = namespace["OrderShipped"]
    (
        scenario(ontology)
        .given(Order(id="o-1"))
        .when(
            ShipOrder(order_id="o-1", carrier="yamato"),
            by=consumer(role="ops", scope_level="team", scope_id="team-1"),
        )
        .then_event(OrderShipped(carrier="yamato"), about="o-1")
    )
    required = ("OrderShipped", "ShipOrder", "emits=", "about=", "Event")
    if path.name.endswith(".ja.md"):
        required += ("監査", "過去形", "*Event", "保持期間")
    else:
        required += ("audit", "past tense", "*Event", "retention")
    missing = [phrase for phrase in required if phrase not in section]
    assert not missing, f"{path.name}: events section omits {missing}"


def _relationship_section(path: Path) -> str:
    match = re.search(
        r"(?ms)^### Links and object-backed link types\n(.*?)(?=^### |^## |\Z)",
        path.read_text(),
    )
    return "" if match is None else match.group(1)


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_relationship_section_phrases(path: Path) -> None:
    """Each guide presents relationship objects as the chosen model."""
    section = _relationship_section(path)
    assert section, f"{path.name}: missing relationship section"
    for phrase in ("Assignment", "owned=True", "SessionPlacement"):
        assert phrase in section, f"{path.name}: relationship section omits {phrase!r}"
    for stale in ("No equivalent yet", "nearest approximation", "同等物はまだありません", "最も近い近似"):
        assert stale not in section, f"{path.name}: relationship section keeps {stale!r}"


def test_design_guide_relationship_examples_match_translation() -> None:
    """The English and Japanese relationship examples are the same code."""
    english, japanese = (
        re.findall(r"(?ms)^```python\n(.*?)^```", _relationship_section(p))
        for p in DESIGN_GUIDES
    )
    assert english == japanese


@pytest.mark.parametrize("path", DESIGN_GUIDES, ids=lambda path: path.name)
def test_design_guide_relationship_object_example_runs(path: Path) -> None:
    """The relationship object example creates both links and reads its placement."""
    section = _relationship_section(path)
    assert section, f"{path.name}: missing relationship section"
    blocks = re.findall(r"(?ms)^```python\n(.*?)^```", section)
    assert len(blocks) == 1, f"{path.name}: expected one python example"
    namespace: dict[str, object] = {
        "__name__": f"guide_relationship_{path.stem.replace('.', '_')}"
    }
    exec(compile(blocks[0], str(path), "exec", dont_inherit=True), namespace)

    ontology = namespace["_ontology"]
    assert isinstance(ontology, ontary.Ontology)
    store = ontary.InMemoryStore(ontology.registry)
    store.insert("Session", {"id": "session-1"}, source=_SOURCE)
    store.insert("Room", {"id": "room-1"}, source=_SOURCE)
    client = ontology.bind(store).for_consumer(
        consumer(role="ops", scope_level="team", scope_id="team-1")
    )

    SessionPlacement = namespace["SessionPlacement"]
    ScheduleSession = namespace["ScheduleSession"]
    assert client.call_function(SessionPlacement(session_id="session-1")) is None

    starts_at = datetime(2026, 10, 6, 9, tzinfo=timezone.utc)
    ends_at = datetime(2026, 10, 6, 10, tzinfo=timezone.utc)
    result = client.execute(
        ScheduleSession(
            session_id="session-1",
            room_id="room-1",
            starts_at=starts_at,
            ends_at=ends_at,
        )
    )
    assignment_id = result["assignment_id"]
    assert store.links_from("assignment_session", assignment_id) == ["session-1"]
    assert store.links_from("assignment_room", assignment_id) == ["room-1"]

    placement = client.call_function(SessionPlacement(session_id="session-1"))
    assert placement is not None
    assert placement["room_id"] == "room-1"
    # The Function returns datetime objects, so compare the aware UTC values directly.
    assert placement["starts_at"] == starts_at
    assert placement["ends_at"] == ends_at

    assert ontology.registry.get_link_type("assignment_session").owned is True
    assert ontology.registry.get_link_type("assignment_room").owned is True


def test_roadmap_records_links_will_not_carry_properties() -> None:
    roadmap = (_ROOT / "docs" / "roadmap.md").read_text()
    assert "## Decided against" in roadmap
    assert "## Later" in roadmap
    assert roadmap.index("## Decided against") < roadmap.index("## Later")
    section = roadmap.split("## Decided against", 1)[1].split("\n## ", 1)[0]
    assert "Links will not carry properties" in section
    assert "ontology-design.md#links-and-object-backed-link-types" in section


def test_changelog_changed_list_mentions_relationship_objects() -> None:
    """Version-agnostic: finds the release section that carries #58."""
    changelog = (_ROOT / "CHANGELOG.md").read_text()
    sections = re.findall(r"(?ms)^## \[[^\]]+\][^\n]*\n(.*?)(?=^## \[|\Z)", changelog)
    matching = [section for section in sections if "(#58)" in section]
    assert len(matching) == 1, "CHANGELOG must carry exactly one #58 release section"
    changed_match = re.search(r"(?ms)^### Changed\n(.*?)(?=^### |\Z)", matching[0])
    assert changed_match is not None, "the #58 section is missing ### Changed"
    assert "#58" in changed_match.group(1)
