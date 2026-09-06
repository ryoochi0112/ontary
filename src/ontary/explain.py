"""Frozen operator-level decision traces for governed reads.

The public models in this module are intentionally not re-exported from the
``ontary`` front door.  Explain is an operator oracle: it reveals hidden-row
existence and therefore belongs at the same trust level as the raw store.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from ontary.security import ConsumerKind

__all__ = [
    "DecisionTrace",
    "MinNTrace",
    "RedactionTrace",
    "ScanReport",
    "ScopePathStep",
    "ScopeRuleTrace",
]

Verdict = Literal["visible", "redacted", "denied", "not_found"]
RuleKind = Literal["SelfScope", "DirectProperty", "ViaLink", "CustomResolver"]
ResolutionKind = Literal["scope", "contributor"]


class ScopePathStep(BaseModel):
    """One object/id in a rule's resolution path.

    ``via_link`` and ``direction`` describe the edge used to arrive at this
    step; both are ``None`` on the root object.
    """

    model_config = ConfigDict(frozen=True)

    object_type: str
    object_id: str
    via_link: str | None = None
    direction: Literal["from", "to"] | None = None


class ScopeRuleTrace(BaseModel):
    """The result of evaluating one declarative scope/contributor rule."""

    model_config = ConfigDict(frozen=True)

    resolution_kind: ResolutionKind
    rule_kind: RuleKind
    object_type: str
    object_id: str
    requested_level: str | None
    matched: bool
    resolved_scope_id: str | None
    scope_path: tuple[ScopePathStep, ...]
    rule_level: str | None = None
    property_name: str | None = None
    link_api_name: str | None = None
    direction: Literal["from", "to"] | None = None
    parent_type: str | None = None
    candidate_parent_ids: tuple[str, ...] = ()


class RedactionTrace(BaseModel):
    """Sensitivity fields removed for one consumer kind."""

    model_config = ConfigDict(frozen=True)

    consumer_kind: ConsumerKind
    fields: tuple[str, ...]


class MinNTrace(BaseModel):
    """Aggregate relevance of the explained selection.

    A single-object read is ``not_applicable`` because ordinary reads are not
    min-N gated.  ``explain_list`` reports ``passed``/``failed`` for the
    selected visible population without changing ordinary list semantics.
    """

    model_config = ConfigDict(frozen=True)

    outcome: Literal["passed", "failed", "not_applicable"]
    threshold: int
    count: int | None = None


class DecisionTrace(BaseModel):
    """Complete per-row explanation returned by :class:`OntologyRuntime`."""

    model_config = ConfigDict(frozen=True)

    object_type: str
    object_id: str
    rules: tuple[ScopeRuleTrace, ...]
    redactions: tuple[RedactionTrace, ...]
    min_n: MinNTrace
    verdict: Verdict
    error_code: str | None = None


class ScanReport(BaseModel):
    """Operator counts from one unbounded governed-read walk."""

    model_config = ConfigDict(frozen=True)

    rows_scanned: int
    rows_returned: int
    rows_hidden_by_scope: int


class _ScanCollector:
    """Mutable request-local counts; never constructed on ordinary reads."""

    def __init__(self) -> None:
        self.rows_scanned = 0
        self.rows_returned = 0
        self.rows_hidden_by_scope = 0

    def build(self) -> ScanReport:
        return ScanReport(
            rows_scanned=self.rows_scanned,
            rows_returned=self.rows_returned,
            rows_hidden_by_scope=self.rows_hidden_by_scope,
        )


class _TraceCollector:
    """Mutable request-local collector; never constructed on ordinary reads."""

    def __init__(self, object_type: str, object_id: str, min_n: int) -> None:
        self.object_type = object_type
        self.object_id = object_id
        self.rules: list[ScopeRuleTrace] = []
        self.redactions: list[RedactionTrace] = []
        self.min_n = MinNTrace(outcome="not_applicable", threshold=min_n)
        self.last_resolved_path: tuple[ScopePathStep, ...] | None = None

    def record_rule(self, entry: ScopeRuleTrace) -> None:
        self.rules.append(entry)
        if entry.matched:
            self.last_resolved_path = entry.scope_path

    def record_redaction(self, consumer_kind: ConsumerKind, fields: tuple[str, ...]) -> None:
        if fields:
            self.redactions.append(RedactionTrace(consumer_kind=consumer_kind, fields=fields))

    def record_min_n(self, *, count: int, threshold: int, passed: bool) -> None:
        self.min_n = MinNTrace(
            outcome="passed" if passed else "failed",
            threshold=threshold,
            count=count,
        )

    def build(
        self,
        verdict: Verdict,
        *,
        error_code: str | None = None,
        min_n: MinNTrace | None = None,
    ) -> DecisionTrace:
        return DecisionTrace(
            object_type=self.object_type,
            object_id=self.object_id,
            rules=tuple(self.rules),
            redactions=tuple(self.redactions),
            min_n=self.min_n if min_n is None else min_n,
            verdict=verdict,
            error_code=error_code,
        )
