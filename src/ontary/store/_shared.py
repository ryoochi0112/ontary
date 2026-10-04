"""Cross-backend pure contract helpers.

Logic the spec requires to be byte-identical across all three `Store`
backends -- refusal checks, validation, and codecs -- lives here ONCE,
instead of being hand-copied per backend. Doctrine (refining the "no shared
base class" rule the backends' module docstrings state): this module may
contain only pure functions and value objects with no storage side effects.
Anything that touches rows -- SQL, dict mutation, transactions -- stays
per-backend, so the conformance suite (`tests/test_store_conformance.py`)
still proves the storage seam rather than exercising one backend's helpers
by another name.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, NamedTuple, Protocol

from ontary import _runtime
from ontary.audit import (
    AuditEntry,
    CapabilityAccessRecord,
    EmittedEvent,
    WriteRecord,
    _safe_json_dumps,
)
from ontary.errors import AuthorityError, ConflictError, PreconditionFailed, ValidationFailed
from ontary.meta import (
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    declared_shape_violation,
    normalize_declared_payload,
)
from ontary.store.values import StoredObject
from ontary.typesys import _to_storage_scalar


class AuditRowLike(Protocol):
    """A persisted audit row readable by field name -- satisfied by
    `sqlite3.Row` and by a plain `dict` (which the Postgres and in-memory
    backends build)."""

    def __getitem__(self, key: str) -> Any: ...


class AuditRowFields(NamedTuple):
    """The persisted string forms of one `AuditEntry` -- the single place
    the field enumeration exists on the write side. The in-memory backend's
    hand-maintained copy of this list silently dropped `invocation_id` and
    `kind` when they were added (see its `append_audit` history); with the
    enumeration living only in `encode_audit_entry`/`decode_audit_entry`,
    that class of bug is structurally impossible -- and
    `test_audit_entries_round_trip_every_field` still guards both
    functions."""

    ts: str
    actor: str
    role: str
    action: str
    target_type: str
    target_id: str | None
    params: str
    outcome: str
    writes: str
    capability_accesses: str
    invocation_id: str | None
    kind: str
    principal: str | None
    unscoped_params: str
    error_code: str | None
    events: str


def encode_audit_entry(entry: AuditEntry) -> AuditRowFields:
    """`AuditEntry` -> persisted string forms, exactly as every backend
    already wrote them: `_safe_json_dumps` so an unencodable value degrades
    to a placeholder rather than raising (declared-contracts §3 AC12)."""
    return AuditRowFields(
        ts=entry.ts.isoformat(),
        actor=entry.actor,
        role=entry.role,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        params=_safe_json_dumps(entry.params),
        outcome=entry.outcome,
        writes=_safe_json_dumps([w.model_dump() for w in entry.writes]),
        capability_accesses=_safe_json_dumps(
            [access.model_dump() for access in entry.capability_accesses]
        ),
        invocation_id=entry.invocation_id,
        kind=entry.kind,
        principal=entry.principal,
        unscoped_params=_safe_json_dumps(entry.unscoped_params),
        error_code=entry.error_code,
        events=_safe_json_dumps([
            {**event.model_dump(), "payload": json.loads(_safe_json_dumps(event.payload))}
            for event in entry.events
        ]),
    )


def decode_audit_entry(row: AuditRowLike) -> AuditEntry:
    """Persisted string forms -> `AuditEntry`, the single read-side
    reconstruction. `row` is read by field name, so extra columns a backend
    also stores (`seq`, `tenant`) are simply ignored."""
    return AuditEntry(
        ts=datetime.fromisoformat(row["ts"]),
        actor=row["actor"],
        role=row["role"],
        action=row["action"],
        target_type=row["target_type"],
        target_id=row["target_id"],
        params=json.loads(row["params"]),
        outcome=row["outcome"],
        writes=[WriteRecord(**w) for w in json.loads(row["writes"])],
        capability_accesses=[
            CapabilityAccessRecord(**access)
            for access in json.loads(row["capability_accesses"])
        ],
        invocation_id=row["invocation_id"],
        # `kind` goes straight into the `AuditEntry` Literal without validation.
        # That is safe only because the schema gate refuses every store a prior
        # release could have written, so no row here predates the current Literal;
        # any future adopt/migrate path MUST validate `kind` before this call.
        kind=row["kind"],
        principal=row["principal"],
        unscoped_params=json.loads(row["unscoped_params"]),
        error_code=row["error_code"],
        events=[EmittedEvent(**event) for event in json.loads(row["events"])],
    )


# -- write-path contract helpers (B4) ---------------------------------------
#
# The refusal/validation sequence every backend ran as a hand-copied preamble.
# Message strings, exception types, and check ORDER are frozen behavior -- do
# not reorder the checks or reword the messages -- and are preserved verbatim
# from the copies these helpers replace.


def canonical_id(value: object) -> str:
    """The plain string every store keys an object or link endpoint by (#91).

    A `str` subclass whose `str()` is not its own value -- a `(str, Enum)`
    mixin member stringifies as `'Mixin.A'` -- is reduced to its underlying
    string value (`'a'`), the value the payload persists. Every store applies
    this to each id it derives or receives, so the three backends agree.
    Anything that is not a `str` keeps its plain `str()` spelling.
    """
    if isinstance(value, str):
        return str.__str__(value)
    return str(value)


def resolve_object_type(registry: OntologyRegistry, obj_type: str) -> ObjectTypeDef:
    """Registry lookup using the public coded unknown-object refusal."""
    return registry.get_object_type(obj_type)


def resolve_link_type(registry: OntologyRegistry, link_type: str) -> LinkTypeDef:
    """Registry lookup using the public coded unknown-link refusal."""
    return registry.get_link_type(link_type)


def retire_object_refusal(
    object_type: str, obj_id: str, *, has_history: bool
) -> ValidationFailed | ConflictError:
    """Build the state-specific coded refusal shared by every backend."""
    if has_history:
        return ConflictError(
            f"{object_type} object {obj_id!r} is already retired",
            code="OBJECT_ALREADY_RETIRED",
        )
    return ValidationFailed(
        f"{object_type} object {obj_id!r} has no stored row to retire",
        code="OBJECT_RETIRE_NOT_FOUND",
    )


def object_already_exists(object_type: str, obj_id: str) -> ConflictError:
    """Build the coded refusal for an insert whose primary key is already
    live. Each backend checks inside its own serialized transaction; the SQL
    backends also carry a partial unique index as a storage backstop."""
    return ConflictError(
        f"{object_type} object {obj_id!r} already exists -- update it, or "
        "retire it before inserting the same primary key again",
        code="OBJECT_ALREADY_EXISTS",
    )


def check_link_endpoints(
    link_def: LinkTypeDef,
    link_type: str,
    from_id: str,
    to_id: str,
    *,
    read_current: Callable[[str, str], StoredObject | None],
    read_last: Callable[[str, str], StoredObject | None],
) -> None:
    """Refuse a link whose endpoint has no live row (#36).

    Each side is looked up under the link type's DECLARED endpoint type, so
    an id that is live as some other type is still not an endpoint. The
    `from` side is checked first, then `to`; both before cardinality. A
    retired endpoint is refused with the same code -- it is not a live
    endpoint either -- and the message says "retired", because the remedy
    (never retry) differs from a typo or an out-of-order ingest.

    The reads are injected rather than performed here so this module stays
    storage-free: each backend passes its own `read_current`/`read_last`,
    which run on the backend's transaction connection and therefore see
    an object inserted earlier in the same action transaction.
    """
    for side, endpoint_type, obj_id in (
        ("from", link_def.from_type, from_id),
        ("to", link_def.to_type, to_id),
    ):
        if read_current(endpoint_type, obj_id) is not None:
            continue
        state = "is retired" if read_last(endpoint_type, obj_id) is not None else "does not exist"
        raise ValidationFailed(
            f"{link_type}: {side}_id {obj_id!r} {state} as a live "
            f"{endpoint_type} -- a link needs a live object at both ends",
            code="LINK_ENDPOINT_NOT_FOUND",
        )


def live_link_not_found(
    link_type: str, from_id: str, to_id: str
) -> ValidationFailed:
    """Build the coded refusal for a close with no matching live link."""
    return ValidationFailed(
        f"{link_type} has no live link from {from_id!r} to {to_id!r}",
        code="LINK_NOT_FOUND",
    )


def check_object_removal_authority(
    registry: OntologyRegistry, object_type: str, *, capturing: bool
) -> ObjectTypeDef:
    """Resolve an object type and gate captured retirement by ownership."""
    obj_def = resolve_object_type(registry, object_type)
    if capturing and not obj_def.is_owned_type:
        raise AuthorityError(
            f"{object_type!r} is not declared ontology-owned "
            "(owned=True) -- an action may not retire a source-backed "
            "object",
            code="UNDECLARED_SOURCE_REMOVAL",
        )
    return obj_def


def check_link_removal_authority(
    registry: OntologyRegistry, link_type: str, *, capturing: bool
) -> LinkTypeDef:
    """Resolve a link type and gate captured closure by ownership."""
    link_def = resolve_link_type(registry, link_type)
    if capturing and link_def.owned is not True:
        raise AuthorityError(
            f"link type {link_type!r} is not declared ontology-owned "
            "(owned=True) -- an action may not close a source-backed link",
            code="UNDECLARED_SOURCE_REMOVAL",
        )
    return link_def


class PreparedInsert(NamedTuple):
    """`prepare_insert`'s result. `obj_id_raw` is the value as found in (or
    minted into) the payload; `obj_id` is its string form. SQLite binds the
    RAW value to SQL while the other two backends bind the string -- each
    backend keeps its exact current bytes by picking its field."""

    payload: dict[str, Any]
    obj_id_raw: Any
    obj_id: str


def _normalize_payload_scalars(
    obj_def: ObjectTypeDef, payload: dict[str, Any]
) -> dict[str, Any]:
    """Copy ``payload`` with declared scalars converted to storage forms."""
    props = {prop.name: prop for prop in obj_def.properties}
    return {
        key: _to_storage_scalar(value, props[key].type, fields=props[key].fields)
        if key in props and value is not None
        else value
        for key, value in payload.items()
    }


def _transition_refusal(
    obj_type: str,
    obj_id: str,
    prop: PropertyDef,
    old: Any,
    new: Any,
    allowed: tuple[str, ...],
) -> PreconditionFailed:
    """Describe a refused move without disclosing a restricted current state."""
    prefix = f"{obj_type} {obj_id!r}: {prop.name} cannot move from "
    if not prop.sensitivity.ai_usable or not prop.sensitivity.human_visible:
        message = f"{prefix}its current value to {new!r}"
    else:
        message = f"{prefix}{old!r} to {new!r}; allowed from {old!r}: {list(allowed)!r}"
    return PreconditionFailed(message, code="TRANSITION_NOT_ALLOWED")


def _check_rules(
    obj_def: ObjectTypeDef, obj_type: str, obj_id: str, row: dict[str, Any]
) -> None:
    """Evaluate every declared rule against the complete storage-form row."""
    failures: list[str] = []
    for rule in obj_def.rules:
        try:
            passed = rule.check(row)
        except Exception as exc:
            failures.append(f"rule {rule.name!r} raised {type(exc).__name__}")
        else:
            if not passed:
                failures.append(f"rule {rule.name!r}: {rule.message}")
    if failures:
        raise ValidationFailed(
            f"{obj_type} {obj_id!r}: {'; '.join(failures)}", code="RULE_VIOLATED"
        )


def prepare_insert(
    registry: OntologyRegistry,
    obj_type: str,
    payload: dict[str, Any],
    *,
    capturing: bool,
) -> PreparedInsert:
    """Everything `insert` does before the first storage touch: resolve the
    type, refuse a captured create of a non-owned type, copy the payload,
    mint the primary key if absent, and refuse a declared-shape violation.
    The shape check runs AFTER the primary key is minted (a caller may
    legitimately omit it) and BEFORE anything is written, so a refused row
    leaves no trace (spec `ontology-evolution` AC9). The missing-primary-key
    UUID fallback here is no longer the only lever: an action handler's
    `ctx.insert` fills the primary key from the runtime's configured
    `id_factory` before the store is ever called, so this fallback now only
    fires for a caller that reaches a `Store` directly, bypassing an
    `ActionContext` -- a direct `store.insert` call therefore still needs an
    explicit primary key to get a deterministic id."""
    obj_def = resolve_object_type(registry, obj_type)

    if capturing and not obj_def.is_owned_type:
        raise AuthorityError(
            f"{obj_type!r} is not declared ontology-owned "
            "(owned=True) -- an action may not create a source-backed "
            "object",
            code="SOURCE_CREATE_REFUSED",
        )

    payload = normalize_declared_payload(obj_def, payload)
    obj_id = payload.get(obj_def.primary_key)
    if obj_id is None:
        obj_id = str(uuid.uuid4())
        payload[obj_def.primary_key] = obj_id
    elif isinstance(obj_id, str):
        # #91: key the row by the plain string value, never a subclass's
        # `str()`; a non-str PK stays as given so the shape check refuses it.
        obj_id = canonical_id(obj_id)
        payload[obj_def.primary_key] = obj_id

    violation = declared_shape_violation(obj_def, payload)
    if violation is not None:
        raise ValidationFailed(
            f"insert into {obj_type!r}: {violation}",
            code="INVALID_RECORD",
        )

    payload = _normalize_payload_scalars(obj_def, payload)

    if capturing:
        for prop in obj_def.properties:
            graph = prop.transitions
            if graph is not None:
                value = payload.get(prop.name)
                if value is not None and value not in graph.initial:
                    raise _transition_refusal(
                        obj_type, str(obj_id), prop, None, value, graph.initial
                    )
    _check_rules(obj_def, obj_type, str(obj_id), payload)

    return PreparedInsert(payload=payload, obj_id_raw=obj_id, obj_id=str(obj_id))


def check_update_authority(
    registry: OntologyRegistry,
    obj_type: str,
    payload_changes: dict[str, Any],
    *,
    capturing: bool,
) -> ObjectTypeDef:
    """`update`'s pre-read half: resolve the type and refuse a captured
    write that touches a source-backed property. The backend then performs
    its own `read_current` and hands the result to `merge_update`."""
    obj_def = resolve_object_type(registry, obj_type)

    if capturing and not obj_def.is_owned_type:
        owned_defaults = obj_def.owned_property_defaults()
        offending = [key for key in payload_changes if key not in owned_defaults]
        if offending:
            raise AuthorityError(
                f"{obj_type!r} update touches source-backed "
                f"propert{'y' if len(offending) == 1 else 'ies'} "
                f"{sorted(offending)!r} -- only declared owned "
                "properties may be written by an action",
                code="UNDECLARED_SOURCE_WRITE",
            )
    return obj_def


def check_read_page_batch(batch: int) -> None:
    """`read_page`'s batch refusal, worded once for all backends."""
    if batch < 1:
        raise ValidationFailed(
            f"read_page batch must be >= 1, got {batch!r}",
            code="INVALID_BATCH",
        )


def unknown_page_token(obj_type: str, token: str) -> ValidationFailed:
    """`read_page`'s cursor refusal: raised by the caller so the traceback
    lands in the backend that failed to resolve the token."""
    return ValidationFailed(
        f"read_page after_key does not match any known page token "
        f"for object_type {obj_type!r}: {token!r}",
        code="INVALID_CURSOR",
    )


def merge_update(
    obj_def: ObjectTypeDef,
    obj_type: str,
    obj_id: str,
    current: StoredObject | None,
    payload_changes: dict[str, Any],
    *,
    capturing: bool = False,
) -> dict[str, Any]:
    """`update`'s post-read half: refuse a missing row, merge the changes
    over the current payload, and check the MERGED row -- an update that
    blanks a required property, or whose changes are individually fine but
    leave the row incomplete, is exactly the case a changes-only check
    would miss. A primary key is immutable (#75): a change that gives it a
    different value is refused, so the store id and the payload pk always
    agree and no update can move a row onto another live object's id.
    Repeating the current value is not a change and is accepted."""
    if current is None:
        raise ValidationFailed(
            f"no current row for {obj_type}/{obj_id}",
            code="OBJECT_NOT_FOUND",
        )

    payload_changes = normalize_declared_payload(obj_def, payload_changes)
    merged = {**current.payload, **payload_changes}
    violation = declared_shape_violation(obj_def, merged)
    if violation is not None:
        raise ValidationFailed(
            f"update of {obj_type}/{obj_id}: {violation}",
            code="INVALID_RECORD",
        )
    normalized = _normalize_payload_scalars(obj_def, merged)
    pk = obj_def.primary_key
    if pk in payload_changes and normalized.get(pk) != current.payload.get(pk):
        raise ValidationFailed(
            f"update of {obj_type}/{obj_id}: primary key {pk!r} is immutable "
            f"(current {current.payload.get(pk)!r}, requested "
            f"{normalized.get(pk)!r}); retire this object and insert a new "
            "one instead",
            code="PRIMARY_KEY_IMMUTABLE",
        )
    for prop in obj_def.properties:
        graph = prop.transitions
        if graph is None:
            continue
        old = current.payload.get(prop.name)
        new = normalized.get(prop.name)
        if old == new:
            continue
        if old is None:
            if capturing and new not in graph.initial:
                raise _transition_refusal(
                    obj_type, obj_id, prop, old, new, graph.initial
                )
        elif new is None or new not in graph.moves.get(old, ()):
            raise _transition_refusal(
                obj_type, obj_id, prop, old, new, graph.moves.get(old, ())
            )
    _check_rules(obj_def, obj_type, obj_id, normalized)
    return normalized


def check_link_write_authority(
    registry: OntologyRegistry, link_type: str, *, capturing: bool
) -> LinkTypeDef:
    """`create_link`'s preamble: resolve the type and refuse a captured
    write of a non-owned link type. Cardinality enforcement stays
    per-backend -- the SQL backends check it inside their own transactions
    and their messages are not identical, so it is storage mechanics, not
    shared contract."""
    link_def = resolve_link_type(registry, link_type)

    if capturing and link_def.owned is not True:
        raise AuthorityError(
            f"link type {link_type!r} is not declared ontology-owned "
            "(owned=True) -- an action may not create a source-backed link",
            code="UNDECLARED_SOURCE_WRITE",
        )
    return link_def


class WriteCapture:
    """The `capture_action_writes` state machine, shared by composition
    (never inheritance -- see the module docstring's doctrine). Each backend
    holds one instance and keeps a one-line `capture_action_writes()`
    returning `self._write_capture.capture()`; authority gates read
    `.active` and allowed writes go through `.record(...)`."""

    def __init__(self) -> None:
        self._capture: list[WriteRecord] | None = None
        self._instant: str | None = None

    @property
    def active(self) -> bool:
        return self._capture is not None

    @property
    def instant(self) -> str | None:
        """The instant fixed for the current capture, or `None` outside one."""
        return self._instant

    @contextmanager
    def capture(self, at: str | None = None) -> Iterator[list[WriteRecord]]:
        if self._capture is not None:
            raise RuntimeError(
                "capture_action_writes does not support nesting: a capture "
                "context is already active on this store"
            )
        records: list[WriteRecord] = []
        self._capture = records
        self._instant = at
        try:
            yield records
        finally:
            self._capture = None
            self._instant = None

    def record(self, record: WriteRecord) -> None:
        if self._capture is not None:
            self._capture.append(record)


def iso_instant(dt: datetime) -> str:
    """Spell an aware datetime as the store's canonical UTC instant string
    (32 characters, same spelling as `values._utcnow_iso`)."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValidationFailed(
            f"clock returned a naive datetime ({dt!r}); an instant must be "
            "timezone-aware -- return e.g. datetime.now(timezone.utc)",
            code="CLOCK_NOT_TIMEZONE_AWARE",
        )
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


class StoreClock:
    """The clock a store stamps `valid_from`/`valid_to` with, shared by
    composition like `WriteCapture`. No clock is installed at construction."""

    def __init__(self) -> None:
        self._clock: Callable[[], datetime] | None = None

    def bind(self, clock: Callable[[], datetime] | None) -> Callable[[], datetime]:
        """Apply the one-clock-per-store rules and return the effective clock."""
        installed = self._clock
        if clock is None:
            return installed if installed is not None else _runtime.default_clock
        if installed is None:
            self._clock = clock
            return clock
        if installed is clock:
            return clock
        raise PreconditionFailed(
            f"this store already has clock {installed!r} installed; cannot "
            f"bind {clock!r}. A store has one clock -- bind with the same "
            "clock, or with none to use the installed one",
            code="CLOCK_CONFLICT",
        )

    def now_iso(self, capture: WriteCapture) -> str:
        """The instant to stamp: the capture's fixed instant if set, else the
        installed (or default) clock's reading."""
        if capture.instant is not None:
            return capture.instant
        clock = self._clock if self._clock is not None else _runtime.default_clock
        return iso_instant(clock())


def ensure_not_before(closing_valid_from: str, now_iso: str, *, what: str) -> None:
    """Refuse a write whose instant precedes the `valid_from` it would close.
    Equal passes."""
    if datetime.fromisoformat(now_iso) < datetime.fromisoformat(closing_valid_from):
        raise PreconditionFailed(
            f"{what}: the store clock reads {now_iso}, earlier than the "
            f"version's valid_from {closing_valid_from}; the clock went "
            "backwards -- fix the clock and retry",
            code="CLOCK_REGRESSION",
        )
