"""Complete, non-raising diagnostics for an authored ontology.

The core rules mirror the conditions checked by ``OntologyDef.validate`` and
its registry/policy validators.  They read descriptor data only; no rule
mutates the registry, policy, or store.  Later DX tasks can extend ``RULES``
with advisory rules without changing the collector contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict

from ontary.meta import (
    ActionTypeDef,
    LinkTypeDef,
    ObjectTypeDef,
    PropertyDef,
    Sensitivity,
)
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    ScopePolicy,
    ScopeRule,
    SelfScope,
    ViaLink,
    incoherent_scope_declarations,
)
from ontary.store.values import DEFAULT_BATCH
from ontary.typesys import validate_scalar

if TYPE_CHECKING:
    from ontary.ontology import OntologyDef
    from ontary.store import Store

__all__ = ["Finding"]


class Finding(BaseModel):
    """One actionable diagnostic reported by :meth:`Ontology.diagnose`."""

    model_config = ConfigDict(frozen=True)

    code: str
    severity: Literal["error", "warn", "info"]
    location: str
    message: str
    fix_hint: str


DiagnosticRule = Callable[["OntologyDef", "Store | None"], tuple[Finding, ...]]


# Advisory name heuristics from docs/ontology-design.md.  They are deliberately
# explicit module constants so a future design-guide revision can change the
# vocabulary without hiding policy in rule control flow.
AGGREGATE_PROPERTY_PREFIXES: tuple[str, ...] = (
    "avg_",
    "mean_",
    "total_",
    "count_",
    "sum_",
)
AGGREGATE_PROPERTY_SUFFIXES: tuple[str, ...] = (
    "_score",
    "_count",
    "_avg",
    "_total",
    "_rate",
)
@dataclass(frozen=True)
class StorageEnvelope:
    """One backend's measured storage envelope: mirrors ``ErrorCodeInfo``
    (``ontary.errors``) -- a catalog value type, not a public API model.

    ``rows`` is the row ceiling under which a governed ``aggregate`` or
    narrow-scope read stays under one second; ``seconds_per_row`` is the
    measured marginal cost past it; ``label`` is the human-readable backend
    name a :class:`Finding` message quotes. All three are ONE record per
    backend (E5, 2026-09-02 human decision) precisely so a backend with a
    row ceiling cannot structurally lack its cost or its label -- three
    parallel dicts, each independently ``.get(backend, <fallback>)``-ed,
    previously let a rows-only entry produce a self-contradictory message
    ("... is ~50,000 rows ... expect ~0.0s") that nothing caught, because
    the missing keys silently defaulted instead of the lookup failing.
    See docs/storage.md#storage-envelope for what each number means and
    the environment it was measured on.
    """

    rows: int
    seconds_per_row: float
    label: str


# E5 (2026-09-02) re-measured both backends against shipped 0.9 (the memo,
# `889b60f`, and the `exists()` early exit, `707cd3f`, both landed first --
# spec storage-envelope.md §6: "measure after the memo"). ObjectStore/SQLite
# is unchanged from E5 and remains a LOCAL, single-sample, single-laptop
# measurement -- AC1 requires only the Postgres curve to be CI-produced, and
# docs/storage.md#storage-envelope now says so in prose, not just here, now
# that the Postgres figure below carries a stated dispersion caveat and an
# unqualified SQLite number beside it would otherwise look equally well
# evidenced. Every field below is measured, none defaulted: `rows` is a
# round number just under the `seconds_per_row`-implied one-second crossing
# (never derived by the engine at runtime, only quoted in messages and
# docs). Every record here also satisfies this module's own invariant,
# `0.9 <= rows * seconds_per_row <= 1.0` (tests/test_diagnose_lints.py).
#
# ObjectStore/SQLite: `scripts/scan_curve.py 100000` (memo on), aggregate_mean
# 3089.4ms / 100,000 rows = 30.9us/row -> 0.000031; 1/0.000031 ~= 32,258, so
# 32,000 stays under a second (32,000 * 0.000031 = 0.99s). Machine: macOS
# 26.5.1 (Darwin 25.5.0), arm64, Python 3.12.13, SQLite 3.53.4 (Python's
# bundled `sqlite3`), 2026-09-02.
#
# PostgresStore/PostgreSQL -- E5c (2026-09-02), REPLACING E5b's derivation
# RULE, not just its number. E5b derived rows=3_200 from ONE CI run
# (33623316425, job 100225149283, `postgres:16`, ubuntu-latest,
# 2026-09-02T11:12Z) via "top of the tested range, on whichever class is slower
# in that run" -- narrow_page_1000 @ n=25,000, 311.7us/row. Pushing E5b
# triggered a SECOND run of the identical workflow and curve step, 33630090428
# (job 100247114881, same `postgres:16` service, same ubuntu-latest label,
# 2026-09-02T12:29Z, 77 minutes later). Re-read either job's raw log directly
# rather than trusting this comment's copy of the numbers: `gh run view --job
# 100225149283 --log` (run 1) or `gh run view --job 100247114881 --log` (run
# 2). On the two LINEAR classes (aggregate_mean, narrow_page_1000), EVERY
# point in run 2 measured slower than EVERY point in run 1 (run 2's fastest
# sample, 539.3us/row, still exceeds run 1's slowest, 344.0us/row) -- the flat
# bounded-page column does not hold this: run 2's bounded-page minimum
# (24.2us/row, at n=25,000) is faster than run 1's bounded-page maximum
# (311.0us/row, at n=1,000); see docs/storage.md#storage-envelope for that
# column's own table. **1.77x** slower at the pair E5b happened to publish
# (narrow @ n=25,000: 311.7 -> 550.2us/row) -- and the two runs also DISAGREE
# on which class is slower (run 1: narrow > aggregate by 8%; run 2: aggregate
# > narrow by 0.7%, the opposite direction). GitHub-hosted runners are shared
# VMs with variable co-tenancy, not the "fixed hardware" spec §3 assumed when
# it moved this measurement off a laptop -- so E5b's rule broke on exactly the
# two assumptions it made: a monotone curve, and a stable class ordering. No
# single CI sample can support a ceiling quoted to two significant figures.
#
# The rule is REPLACED, not patched: the published `rows` is the round
# number just under the one-second crossing implied by the SLOWEST per-row
# rate in any published sample, across every class (`aggregate_mean` and
# `narrow_page_1000`) and every steady-state n (runs 1-3 are flat from
# n=5,000 up; n=1,000's rate is noisier -- e.g. run 1's OWN worst point is
# narrow @ n=1,000, 344.0us/row, not its n=25,000 figure -- and across runs
# 1-3, n=1,000's own worst rate was 545.0us/row (run 3's narrow_page_1000),
# still below the 563.7us/row peak run 3 reached at n=5,000, which is why
# an earlier draft of this comment called whether n=1,000 counts MOOT --
# but the rule deliberately does not hard-code an exclusion for it, and
# run 4 (E5f, below) is exactly the slower n=1,000 sample that MOOT
# framing anticipated: run 4's global worst, 608.6us/row, sits AT n=1,000,
# not at n=5,000 or n=25,000 -- so n=1,000 counting was never actually
# moot, only unexercised until a fourth sample arrived). Adding a slower
# future sample therefore LOWERS the ceiling by construction -- deliberate:
# this envelope is an ADVISORY warning, never an enforced limit, so a lower
# ceiling warns earlier, which is the safe direction to be wrong in.
#
# Run 1 (33623316425) rates, us/row: aggregate_mean 254.9/298.9/287.4,
# narrow_page_1000 344.0/310.8/311.7 at n=1,000/5,000/25,000 -> worst 344.0.
# Run 2 (33630090428) rates, us/row: aggregate_mean 539.3/548.1/553.8,
# narrow_page_1000 542.3/542.5/550.2 at n=1,000/5,000/25,000 -> worst 553.8
# (aggregate @ n=25,000: 13845.2ms / 25,000 rows). The floor across runs 1
# and 2 alone was therefore run 2's aggregate @ n=25,000: 553.8us/row ->
# seconds_per_row=0.000554, rows=1_800 -- SUPERSEDED by E5d below, which
# adds a third, slower sample.
#
# PostgresStore/PostgreSQL -- E5d (2026-09-02) applied the SAME rule (not a
# new one) to a THIRD CI run. Pushing E5c+E7 triggered 33651633373 (job
# 100319828397, same `postgres:16` service, same ubuntu-latest label,
# 2026-09-02T15:56Z -- 3h27m after run 2, 4h44m after run 1). Re-read the
# raw log directly rather than trusting this comment's copy: `gh run view
# --job 100319828397 --log` (run 3). Run 3 (33651633373) rates, us/row:
# aggregate_mean 541.6/563.7/557.4, narrow_page_1000 545.0/552.7/558.1 at
# n=1,000/5,000/25,000 -> worst 563.7 (aggregate @ n=5,000: 2818.6ms /
# 5,000 rows = 563.720us/row).
#
# THE RULE EARNED ITS KEEP HERE: run 3's worst point is at n=5,000, NOT
# the top of the tested range (n=25,000, where the aggregate rate is only
# 557.4us/row) -- run 3's own aggregate rate rises from 541.6 (n=1,000) to
# a peak of 563.7 (n=5,000) and eases back to 557.4 (n=25,000), so the
# curve is non-monotone again, on a third independent sample. E5b's
# retired rule binds on whichever class is slower in that run -- at
# n=25,000 that is narrow_page_1000, 558.1us/row (13951.4ms / 25,000
# rows), not the aggregate figure above -- and would still have
# MISSED the true worst point entirely. E5c's max-across-every-class-and-
# every-n rule catches it because it never assumed monotonicity in the
# first place -- this is the first published evidence that replacing the
# rule (not just the number) was for cause, not for taste.
#
# The worst across ALL THREE runs and BOTH classes was run 3's
# aggregate @ n=5,000: 563.720us/row -> seconds_per_row=0.000564
# (rounded UP, the conservative direction). 1/0.000564 ~= 1,773.0, so
# 1,700 was the round hundred just under that crossing. Invariant
# check: 1,700 * 0.000564 = 0.9588, inside [0.9, 1.0]. E5c's published
# rows=1_800 sat 1.5% ABOVE the crossing this third sample implied
# (1,800 * 0.000564 = 1.0152s, over one second) -- not "close enough":
# D1's conservative-floor rule exists precisely to replace that
# judgment call with this arithmetic, so 1.5% over is over. That pair
# was SUPERSEDED by E5f below, which adds a fourth, slower sample.
#
# PostgresStore/PostgreSQL -- E5f (2026-09-02) applied the SAME rule (not
# a new one) to a FOURTH CI run. Pushing a97ba03..28f2aff triggered
# 33697469943 (job `postgres`, 100469367996, same `postgres:16` service,
# same ubuntu-latest label, 2026-09-02T23:59Z). Re-read the raw log
# directly rather than trusting this comment's copy: `gh run view --job
# 100469367996 --log` (run 4). Run 4 (33697469943) rates, us/row:
# aggregate_mean 593.6/596.5/587.7, narrow_page_1000 608.6/594.7/586.1 at
# n=1,000/5,000/25,000 -> worst 608.6 (narrow_page_1000 @ n=1,000:
# 608.6ms / 1,000 rows = 608.600us/row).
#
# THE RULE EARNED ITS KEEP AGAIN, DIFFERENTLY: run 4's GLOBAL worst point
# sits at the BOTTOM of the tested range (n=1,000) -- not a new position
# for a run's OWN worst (run 1's own worst sat there too, per the note
# above), but the first time n=1,000 has produced the worst
# point across ALL runs and BOTH classes: runs 1-3's own worst points sat
# at n=1,000, n=25,000, and n=5,000 respectively (run 1 narrow 344.0,
# run 2 aggregate 553.8, run 3 aggregate 563.7). The class holding each
# run's own global-worst point has moved back to narrow_page_1000 for the
# second time (run 1 narrow, runs 2-3 aggregate, run 4 narrow); read at
# n=25,000 the way the retired rule would, the slower class has flipped a
# third time, this time onto aggregate_mean rather than narrow_page_1000
# (run 1 narrow, run 2 aggregate, run 3 narrow, run 4 aggregate). At run
# 4's OWN n=25,000 the slower class is aggregate_mean, 587.7us/row
# (14693.7ms / 25,000 rows) -- not the narrow figure, 586.1us/row -- so
# the retired "top of the tested range, on whichever class is slower"
# rule would have read 587.7us/row, 3.4% below run 4's true worst point
# (608.6us/row), and MISSED it. E5c's max-across-every-class-and-every-n
# rule catches it regardless of where in the range, or on which class,
# the worst point falls.
#
# The new global worst across ALL FOUR runs and BOTH classes is
# therefore run 4's narrow_page_1000 @ n=1,000: 608.600us/row ->
# seconds_per_row=0.000609 (rounded UP, the conservative direction).
# 1/0.000609 ~= 1,642.0, so 1,600 is the round hundred just under the
# crossing. Invariant check: 1,600 * 0.000609 = 0.9744,
# inside [0.9, 1.0]. E5d's published rows=1_700 sat 1.035s at run 4's
# worst point (1,700 * 0.000609 = 1.0353, over one second) -- not
# "close enough": D1's conservative-floor rule exists precisely to
# replace that judgment call with this arithmetic. A hypothetical FASTER
# future run would leave 1,600 unchanged (the rule takes the WORST rate
# across every published sample, not the newest one); a SLOWER one would
# lower it again, exactly as this run just did to E5d's 1,700. All four
# runs' full tables, and the four-run dispersion stated in prose, are
# published side by side in docs/storage.md#storage-envelope;
# `tests/test_docs.py` parses those tables and re-derives this same
# floor from them (S2: one generating rule, not a fifth hand-copied
# constant).
#
# SUPERSEDED (E5d, 2026-09-02): rows=1_700, seconds_per_row=0.000564,
# derived from runs 1, 2, and 3 ALONE -- see above for why a fourth,
# slower sample lowers it.
#
# SUPERSEDED (E5c, 2026-09-02): rows=1_800, seconds_per_row=0.000554,
# derived from runs 1 and 2 ALONE -- see above for why a third, slower
# sample lowers it.
#
# SUPERSEDED (E5b, 2026-09-02): rows=3_200, seconds_per_row=0.000312,
# derived from run 33623316425 ALONE -- see above for why a single-run,
# fixed-class-ordering rule does not survive a second sample.
#
# SUPERSEDED (E5, 2026-09-02): rows=10_000, seconds_per_row=0.000097
# (97.0us/row), measured on LOCAL Homebrew PostgreSQL 17.11, same machine
# as ObjectStore above, because no CI number existed yet (no Docker daemon
# was running locally, spec §4). Run 1 alone already measured 2.96x SLOWER
# than that laptop (287.4 vs 97.0us/row at n=25,000) -- a laptop number was
# optimistic by about 3x, exactly what spec §4/§5's "a laptop number is not
# reproducible by a reader" existed to catch; CI then turned out to have
# its own, smaller version of the same non-reproducibility problem.
STORAGE_ENVELOPE: dict[str, StorageEnvelope] = {
    "ObjectStore": StorageEnvelope(rows=32_000, seconds_per_row=0.000031, label="SQLite"),
    "PostgresStore": StorageEnvelope(
        rows=1_600, seconds_per_row=0.000609, label="PostgreSQL"
    ),
}
CRUD_ACTION_PREFIXES: tuple[str, ...] = (
    "Set",
    "Update",
    "Delete",
    "Create",
    "Remove",
    "Erase",
)
FORBIDDEN_TYPE_SUFFIXES: tuple[str, ...] = ("V2", "V3", "History", "Snapshot")
DEFAULT_MIN_N = 3
# The design guide calls a first-class point-in-time value a snapshot.  This
# exact marker lets an author explicitly sanction the otherwise forbidden
# ``*Snapshot`` API-name suffix in the serializable ObjectTypeDef description.
DECLARED_SNAPSHOT_MARKER = "declared snapshot"

_DEFAULT_SENSITIVITY = Sensitivity()


def _error(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="error",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _warn(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="warn",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _info(code: str, location: str, message: str, fix_hint: str) -> Finding:
    return Finding(
        code=code,
        severity="info",
        location=location,
        message=message,
        fix_hint=fix_hint,
    )


def _is_aggregate_name(name: str) -> bool:
    return name.startswith(AGGREGATE_PROPERTY_PREFIXES) or name.endswith(
        AGGREGATE_PROPERTY_SUFFIXES
    )


def _is_sensitivity_marked(prop: PropertyDef) -> bool:
    """Treat a non-default Sensitivity policy as a sensitivity marker.

    ``PropertyDef`` is the frozen diagnostic IR and does not preserve whether
    an author explicitly passed ``Sensitivity()`` when that value equals the
    default.  Restricted, non-default policies are therefore the honest
    statically observable marker for these lints; this avoids treating every
    ordinary property as sensitive.
    """
    return prop.sensitivity != _DEFAULT_SENSITIVITY


def _linked_source_type(definition: "OntologyDef", object_type: str) -> str:
    """Return the sole link sibling, or ``source`` when it is ambiguous."""
    links = [
        link
        for link in definition.registry.link_types.values()
        if link.from_type == object_type or link.to_type == object_type
    ]
    if len(links) != 1:
        return "source"
    link = links[0]
    if link.from_type == object_type and link.to_type != object_type:
        return link.to_type
    if link.to_type == object_type and link.from_type != object_type:
        return link.from_type
    return "source"


def _registry_upcaster_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    findings: list[Finding] = []

    for (api_name, from_version) in sorted(registry.upcasters):
        obj = object_types.get(api_name)
        location = f"OntologyRegistry.upcasters[{api_name!r}, {from_version}]"
        if obj is None:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    location,
                    f"upcaster for unregistered object type {api_name!r} "
                    f"(from version {from_version})",
                    "Register the object type before declaring its upcaster, "
                    "or remove the orphaned upcaster.",
                )
            )
            continue
        if from_version >= obj.version:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    location,
                    f"upcaster for {api_name!r} reads from version "
                    f"{from_version}, which is not older than the declared "
                    f"version {obj.version} -- it could never apply to a "
                    "stored row",
                    "Declare the step from an older stored version, or remove "
                    "this upcaster.",
                )
            )

    for api_name in sorted(object_types):
        obj = object_types[api_name]
        if obj.version <= 1:
            continue
        missing = [
            version
            for version in range(1, obj.version)
            if (api_name, version) not in registry.upcasters
        ]
        if missing:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"ObjectTypeDef[{api_name!r}].version",
                    f"ObjectTypeDef {api_name!r} is declared version "
                    f"{obj.version} but has no upcaster from version(s) "
                    f"{missing} -- a row stored at any of those versions "
                    "could not be read. Declare one upcaster per step, or "
                    "migrate the rows and drop the version bump",
                    "Declare one upcaster for every missing version step, or "
                    "migrate those rows and remove the version bump.",
                )
            )
    return tuple(findings)


def _registry_owned_default_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    findings: list[Finding] = []

    for api_name in sorted(registry.object_types):
        obj = registry.object_types[api_name]
        owned_defaults = obj.owned_property_defaults()
        if not owned_defaults:
            continue
        prop_by_name = {prop.name: prop for prop in obj.properties}
        for prop_name in sorted(owned_defaults):
            default = owned_defaults[prop_name]
            location = f"ObjectTypeDef[{api_name!r}].owned[{prop_name!r}]"
            if prop_name == obj.primary_key:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} is the primary key, which can never "
                        "be individually owned (use owned=True for the "
                        "whole type)",
                        "Use owned=True for a whole-type declaration, or remove "
                        "the primary-key entry from owned properties.",
                    )
                )
                continue
            prop = prop_by_name.get(prop_name)
            if prop is None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} is not one of this type's properties",
                        "Declare the property on the object type or remove it "
                        "from owned properties.",
                    )
                )
                continue
            if default is None:
                if prop.required:
                    findings.append(
                        _error(
                            "ONTOLOGY_INVALID",
                            location,
                            f"ObjectTypeDef {api_name!r}: owned property "
                            f"{prop_name!r} default is None but the property "
                            "is required",
                            "Give the required property a value compatible with "
                            "its declaration, or mark it optional.",
                        )
                    )
                continue
            mismatch = validate_scalar(default, prop.type, prop.choices)
            if mismatch is not None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        location,
                        f"ObjectTypeDef {api_name!r}: owned property "
                        f"{prop_name!r} default {mismatch}",
                        "Replace the default with a value matching the declared "
                        "property type.",
                    )
                )
    return tuple(findings)


def _registry_link_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    findings: list[Finding] = []

    for api_name in sorted(registry.link_types):
        link = registry.link_types[api_name]
        if link.from_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"LinkTypeDef[{api_name!r}].from_type",
                    f"LinkTypeDef {api_name!r}: dangling from_type "
                    f"{link.from_type!r}",
                    "Register the link's from_type object or correct the link "
                    "declaration.",
                )
            )
        if link.to_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"LinkTypeDef[{api_name!r}].to_type",
                    f"LinkTypeDef {api_name!r}: dangling to_type "
                    f"{link.to_type!r}",
                    "Register the link's to_type object or correct the link "
                    "declaration.",
                )
            )
    return tuple(findings)


def _registry_action_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    object_types = registry.object_types
    capabilities = registry.capabilities
    effect_types = registry.effect_types
    findings: list[Finding] = []

    for api_name in sorted(registry.action_types):
        action: ActionTypeDef = registry.action_types[api_name]
        base = f"ActionTypeDef[{api_name!r}]"
        if action.target_type not in object_types:
            findings.append(
                _error(
                    "ONTOLOGY_INVALID",
                    f"{base}.target_type",
                    f"ActionTypeDef {api_name!r}: dangling target_type "
                    f"{action.target_type!r}",
                    "Register the target object type or point the action at a "
                    "declared object type.",
                )
            )

        for capability in sorted(action.capabilities):
            if capability not in capabilities:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"{base}.capabilities[{capability!r}]",
                        f"ActionTypeDef {api_name!r}: dangling capability "
                        f"{capability!r}",
                        "Declare the capability before using it on the action, "
                        "or remove the reference.",
                    )
                )

        for effect in sorted(action.effects):
            if effect not in effect_types:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"{base}.effects[{effect!r}]",
                        f"ActionTypeDef {api_name!r}: dangling effect "
                        f"{effect!r}",
                        "Declare the effect type before using it on the action, "
                        "or remove the reference.",
                    )
                )

        for index, param in enumerate(action.parameters):
            param_location = f"{base}.parameters[{index}]"
            if param.scope_semantics is not None and param.refers_to is None:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        param_location,
                        f"ActionTypeDef {api_name!r} parameter "
                        f"{param.name!r}: scope_semantics "
                        f"{param.scope_semantics!r} requires refers_to",
                        "Set refers_to on the parameter or remove its scope "
                        "semantics marker.",
                    )
                )
            if param.refers_to is not None and param.refers_to not in object_types:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        param_location,
                        f"ActionTypeDef {api_name!r} parameter "
                        f"{param.name!r}: dangling refers_to "
                        f"{param.refers_to!r}",
                        "Register the referenced object type or correct the "
                        "parameter reference.",
                    )
                )
    return tuple(findings)


def _registry_function_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    registry = definition.registry
    capabilities = registry.capabilities
    findings: list[Finding] = []

    for api_name in sorted(registry.functions):
        function = registry.functions[api_name]
        for capability in sorted(function.capabilities):
            if capability not in capabilities:
                findings.append(
                    _error(
                        "ONTOLOGY_INVALID",
                        f"FunctionDef[{api_name!r}].capabilities[{capability!r}]",
                        f"FunctionDef {api_name!r}: dangling capability "
                        f"{capability!r}",
                        "Declare the capability before using it on the function, "
                        "or remove the reference.",
                    )
                )
    return tuple(findings)


def _scope_policy_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    policy: ScopePolicy = definition.policy
    registry = definition.registry
    object_types = registry.object_types
    link_types = registry.link_types
    findings: list[Finding] = []

    levels = policy.levels
    if not levels:
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                "ScopePolicy.levels",
                "levels must not be empty",
                "Declare at least one unique scope level.",
            )
        )
    else:
        try:
            duplicate_levels = len(levels) != len(set(levels))
        except TypeError:
            duplicate_levels = True
        if duplicate_levels:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    "ScopePolicy.levels",
                    "levels must not contain duplicate entries",
                    "Declare each scope level exactly once.",
                )
            )

    if not isinstance(policy.min_n, int) or isinstance(policy.min_n, bool) or policy.min_n < 1:
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                "ScopePolicy.min_n",
                f"min_n must be greater than or equal to 1, got {policy.min_n!r}",
                "Set min_n to an integer greater than or equal to 1.",
            )
        )

    level_set = set(levels) if all(isinstance(level, str) for level in levels) else set()
    for obj_type in sorted(policy.unscoped_types):
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    f"ScopePolicy.unscoped_types[{obj_type!r}]",
                    f"ScopePolicy.unscoped_types: undeclared object type {obj_type!r}",
                    "Register the object type or remove it from unscoped_types.",
                )
            )

    for message in incoherent_scope_declarations(policy):
        findings.append(
            _error(
                "SCOPE_POLICY_ERROR",
                message.split(":", 1)[0],
                message,
                "Remove the type from unscoped_types, or drop its rules entry.",
            )
        )

    for obj_type in sorted(policy.row_visibility):
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    f"ScopePolicy.row_visibility[{obj_type!r}]",
                    f"ScopePolicy.row_visibility: undeclared object type {obj_type!r}",
                    "Register the object type or remove its row-visibility rule.",
                )
            )

    findings.extend(
        _scope_rule_list_findings(
            object_types,
            link_types,
            policy.rules,
            group="rules",
            check_level=True,
            level_set=level_set,
        )
    )
    findings.extend(
        _scope_rule_list_findings(
            object_types,
            link_types,
            policy.contributor_rules,
            group="contributor_rules",
            check_level=False,
            level_set=level_set,
            require_non_empty=True,
        )
    )
    return tuple(findings)


def _scope_rule_list_findings(
    object_types: dict[str, ObjectTypeDef],
    link_types: Mapping[str, LinkTypeDef],
    rule_lists: Mapping[str, list[ScopeRule]],
    *,
    group: str,
    check_level: bool,
    level_set: set[str],
    require_non_empty: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    for obj_type in sorted(rule_lists):
        rule_list = rule_lists[obj_type]
        prefix = f"ScopePolicy.{group}[{obj_type!r}]"
        if require_non_empty and not rule_list:
            # `ScopePolicy.validate` refuses this at startup; a sweep that
            # walked only the rules INSIDE each list could never see a defect
            # that is a property of the list itself, and reported clean.
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    prefix,
                    f"{prefix}: empty rule list -- a type declaring "
                    "contributor rules must supply at least one rule",
                    "Remove the entry to opt out of contributor "
                    "de-duplication, or declare a rule.",
                )
            )
        if obj_type not in object_types:
            findings.append(
                _error(
                    "SCOPE_POLICY_ERROR",
                    prefix,
                    f"{prefix}: undeclared object type {obj_type!r}",
                    "Register the object type or remove this policy rule list.",
                )
            )
            continue

        for index, rule in enumerate(rule_list):
            location = f"{prefix}[{index}]"
            if check_level and isinstance(rule, (SelfScope, DirectProperty, CustomResolver)):
                if rule.level not in level_set:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared scope level {rule.level!r}",
                            "Add the level to ScopePolicy.levels or correct the "
                            "rule's level.",
                        )
                    )

            if isinstance(rule, DirectProperty):
                prop_names = {prop.name for prop in object_types[obj_type].properties}
                if rule.property_name not in prop_names:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: property {rule.property_name!r} not "
                            f"declared on {obj_type!r}",
                            "Declare the direct-property field or correct the "
                            "policy rule.",
                        )
                    )

            if isinstance(rule, ViaLink):
                if rule.parent_type not in object_types:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared parent_type "
                            f"{rule.parent_type!r}",
                            "Register the parent object type or correct the "
                            "ViaLink rule.",
                        )
                    )
                if rule.link_api_name not in link_types:
                    findings.append(
                        _error(
                            "SCOPE_POLICY_ERROR",
                            location,
                            f"{location}: undeclared link {rule.link_api_name!r}",
                            "Register the link or correct the ViaLink name.",
                        )
                    )
                else:
                    link_def = link_types[rule.link_api_name]
                    if rule.direction == "from":
                        expected_from, expected_to = obj_type, rule.parent_type
                    else:
                        expected_from, expected_to = rule.parent_type, obj_type
                    if (link_def.from_type, link_def.to_type) != (
                        expected_from,
                        expected_to,
                    ):
                        findings.append(
                            _error(
                                "SCOPE_POLICY_ERROR",
                                location,
                                f"{location}: direction {rule.direction!r} expects "
                                f"link {rule.link_api_name!r} to run "
                                f"{expected_from!r} -> {expected_to!r}, but it is "
                                f"declared {link_def.from_type!r} -> "
                                f"{link_def.to_type!r}",
                                "Align the ViaLink direction and parent type with "
                                "the link declaration.",
                            )
                        )
    return findings


def _stored_derivable_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn on aggregate-shaped properties that look like stored rollups."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        sibling = _linked_source_type(definition, api_name)
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if not _is_aggregate_name(prop.name):
                continue
            findings.append(
                _warn(
                    "STORED_DERIVABLE",
                    f"object {api_name}, property {prop.name}",
                    "looks like a stored aggregate; facts are stored once and "
                    "derived by Functions",
                    "declare a Function that computes it from "
                    f"{sibling} rows, or mark the type as a declared snapshot",
                )
            )
    return tuple(findings)


def _crud_action_name_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn when an action API name exposes a storage-level CRUD operation."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.action_types):
        if not api_name.startswith(CRUD_ACTION_PREFIXES):
            continue
        findings.append(
            _warn(
                "CRUD_ACTION_NAME",
                f"action {api_name}",
                "action name looks like a CRUD operation rather than a "
                "business verb",
                "Rename the action with the business outcome it performs.",
            )
        )
    return tuple(findings)


def _forbidden_type_name_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn on version/history clones, except explicitly declared snapshots."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        suffix = next(
            (candidate for candidate in FORBIDDEN_TYPE_SUFFIXES if api_name.endswith(candidate)),
            None,
        )
        if suffix is None:
            continue
        if suffix == "Snapshot" and DECLARED_SNAPSHOT_MARKER in obj.description.casefold():
            continue
        findings.append(
            _warn(
                "FORBIDDEN_TYPE_NAME",
                f"object {api_name}",
                "object type name looks like a version, history, or snapshot "
                "clone",
                "Keep one object type and use versioning/upcasters; declare a "
                "snapshot explicitly when a point-in-time value is first-class.",
            )
        )
    return tuple(findings)


def _micro_action_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn on the declared shape of a likely one-property write action.

    The descriptor IR does not contain a write set or handler body, so this is
    intentionally only a shape heuristic: exactly one parameter without a
    ``refers_to`` target marker whose name is a declared property on the
    action's target object.  It does not claim that the handler actually writes
    that property.
    """
    findings: list[Finding] = []
    object_types = definition.registry.object_types
    for api_name in sorted(definition.registry.action_types):
        action = definition.registry.action_types[api_name]
        target = object_types.get(action.target_type)
        if target is None:
            continue
        non_target = [param for param in action.parameters if param.refers_to is None]
        if len(non_target) != 1:
            continue
        parameter = non_target[0]
        if parameter.name not in {prop.name for prop in target.properties}:
            continue
        findings.append(
            _warn(
                "MICRO_ACTION",
                f"action {api_name}, parameter {parameter.name}",
                "action shape looks like a single-property write",
                "Model the business transition as one invariant-preserving "
                "action instead of exposing a property setter.",
            )
        )
    return tuple(findings)


def _unscoped_sensitive_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn when a non-default sensitive property lacks scope or an exemption."""
    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        obj = definition.registry.object_types[api_name]
        if (
            api_name in definition.policy.unscoped_types
            or definition.policy.rules.get(api_name)
        ):
            continue
        for prop in sorted(obj.properties, key=lambda item: item.name):
            if not _is_sensitivity_marked(prop):
                continue
            findings.append(
                _warn(
                    "UNSCOPED_SENSITIVE",
                    f"object {api_name}, property {prop.name}",
                    "sensitive property has no scope rule for its object type",
                    "Add a scope rule for the object type or explicitly mark "
                    "the type as unscoped.",
                )
            )
    return tuple(findings)


def _min_n_unset_findings(
    definition: "OntologyDef", _store: "Store | None"
) -> tuple[Finding, ...]:
    """Nudge authors to choose ``min_n`` when sensitivity is declared.

    Function bodies and contributor-resolution behavior are not statically
    inspectable here.  The honest closest heuristic is therefore the policy
    value ``3`` (the constructor default) plus at least one non-default
    sensitivity policy; it intentionally does not infer whether an aggregate
    will be called.
    """
    if definition.policy.min_n != DEFAULT_MIN_N:
        return ()
    if not any(
        _is_sensitivity_marked(prop)
        for obj in definition.registry.object_types.values()
        for prop in obj.properties
    ):
        return ()
    return (
        _info(
            "MIN_N_UNSET",
            "ScopePolicy.min_n",
            "min_n is left at the default while sensitivity is declared",
            "Set min_n explicitly for the ontology's privacy requirements.",
        ),
    )


def _storage_envelope_findings(
    definition: "OntologyDef", store: "Store | None"
) -> tuple[Finding, ...]:
    """Warn when a stored type has crossed its backend's measured envelope."""
    if store is None:
        return ()
    backend = type(store).__name__
    envelope = STORAGE_ENVELOPE.get(backend)
    if envelope is None:
        return ()

    findings: list[Finding] = []
    for api_name in sorted(definition.registry.object_types):
        row_count = 0
        after_key: str | None = None
        try:
            while True:
                page = store.read_page(
                    api_name, after_key=after_key, batch=DEFAULT_BATCH
                )
                row_count += len(page)
                if len(page) < DEFAULT_BATCH:
                    break
                after_key = page[-1].key
        except Exception:
            # Diagnostics are advisory: an unavailable or stale declared type
            # must not turn this rule into DIAGNOSE_RULE_FAILED.
            continue
        if row_count <= envelope.rows:
            continue
        findings.append(
            _warn(
                "STORAGE_ENVELOPE_EXCEEDED",
                api_name,
                f"{api_name} holds {row_count:,} rows. The measured envelope for "
                f"{backend} ({envelope.label}) is ~{envelope.rows:,} rows for a "
                f"governed aggregate or narrow-scope read under one second; at "
                f"this size expect ~{row_count * envelope.seconds_per_row:.1f}s. "
                "Bounded, unordered page reads are unaffected.",
                "Narrow the population with `where=` before aggregating, or split "
                "the type. See docs/storage.md#storage-envelope.",
            )
        )
    return tuple(findings)


RULES: tuple[DiagnosticRule, ...] = (
    _registry_upcaster_findings,
    _registry_owned_default_findings,
    _registry_link_findings,
    _registry_action_findings,
    _registry_function_findings,
    _scope_policy_findings,
    _stored_derivable_findings,
    _crud_action_name_findings,
    _forbidden_type_name_findings,
    _micro_action_findings,
    _unscoped_sensitive_findings,
    _min_n_unset_findings,
    _storage_envelope_findings,
)


def _collect_findings(
    definition: "OntologyDef", store: "Store | None"
) -> list[Finding]:
    """Run every rule, turning an unexpected rule failure into a finding."""
    findings: list[Finding] = []
    for rule in RULES:
        try:
            findings.extend(rule(definition, store))
        except Exception as exc:
            findings.append(
                _error(
                    "DIAGNOSE_RULE_FAILED",
                    rule.__name__,
                    f"diagnostic rule {rule.__name__!r} failed: {exc}",
                    "Fix the reported rule error and run diagnose() again.",
                )
            )
    return findings
