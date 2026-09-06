"""The `Store` protocol and the ontology-fingerprint gate functions.

Moved verbatim from the original `ontary/store.py` (B2 of the staged
refactor). This is the storage seam the engine is written against; the
fingerprint gates live beside it because they are backend-independent --
they go through the protocol, never through SQL.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AbstractContextManager
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from ontary.audit import AuditEntry, WriteRecord
from ontary.errors import ConflictError
from ontary.fingerprint import (
    OntologyFingerprint,
    fingerprint_ontology,
)
from ontary.meta import OntologyRegistry
from ontary.outbox import OutboxRecord, OutboxState
from ontary.store.values import DEFAULT_BATCH, PagedRow, Source, StoredObject


@runtime_checkable
class Store(Protocol):
    """The storage seam the engine (`query`, `actions`, `ingest`, `scope`,
    `client`) is written against -- PEP 544 structural protocol, not an
    ABC: `ObjectStore` (the SQLite implementation below) satisfies it
    without inheriting from it, and any other backend can too by simply
    matching these method signatures.

    Layering rule: consumer reads go only via `GuardedQuery` --
    `Store.read_current`/`read_all`/`read_page` are engine-internal raw
    reads. No `_`-prefixed method exists on this protocol or is ever called
    from outside `store.py`: `read_current`/`read_all`/`read_page` are the
    public, documented raw-read members trusted engine/handler code may call
    directly (see `ObjectStore.read_current`'s docstring for the narrow,
    trusted-caller exception this covers); a CONSUMER (human/AI client, MCP
    tool, Function body) must never reach a `Store` method directly --
    `ontary.query.GuardedQuery` is the only read path a consumer may use.
    """

    def insert(self, obj_type: str, payload: dict[str, Any], source: "Source") -> str:
        """Insert a new object, returning its id (minted if not supplied
        in `payload`)."""
        ...

    def update(
        self,
        obj_type: str,
        obj_id: str,
        payload_changes: dict[str, Any],
        source: "Source",
    ) -> None:
        """Close the current row for `(obj_type, obj_id)` and insert a new
        current row merging `payload_changes` over it."""
        ...

    def create_link(self, link_type: str, from_id: str, to_id: str) -> None:
        """Create a link, enforcing the link type's declared cardinality."""
        ...

    def links_from(self, link_type: str, from_id: str) -> list[str]:
        """Ids of every current target reachable from `from_id` via
        `link_type`."""
        ...

    def links_to(self, link_type: str, to_id: str) -> list[str]:
        """Ids of every current source reaching `to_id` via `link_type`."""
        ...

    def read_current(self, obj_type: str, obj_id: str) -> StoredObject | None:
        """Raw (unredacted, unscoped) read of one object's current row, or
        `None` if it does not exist. Engine-internal / trusted-caller-only
        -- see the protocol docstring's layering rule."""
        ...

    def read_all(self, obj_type: str) -> list[StoredObject]:
        """Raw (unredacted, unscoped) read of every current row of
        `obj_type`, ordered the same way as `read_page` (spec AC2).
        Engine-internal / trusted-caller-only -- see the protocol
        docstring's layering rule."""
        ...

    def read_page(
        self, obj_type: str, after_key: str | None = None, batch: int = DEFAULT_BATCH
    ) -> list[PagedRow]:
        """Raw (unredacted, unscoped) read of current rows of `obj_type`,
        ordered by the store's own per-row identity -- NOT the payload
        primary key: spec pagination-hardening §5's amended decision
        explains why a payload-pk-keyed cursor silently drops rows -- and
        starting strictly AFTER `after_key` (or from the beginning when
        `None`), capped at `batch` rows.

        Returns a `PagedRow` per row -- `PagedRow.key` alongside `.obj` --
        rather than one key for the whole batch: the row identity is never
        placed on `StoredObject`/`Lineage` itself (it would let a narrow
        consumer infer how many rows its scope hid, or another tenant's
        write volume, by gap arithmetic -- see `Lineage`'s docstring), so a
        random per-row PAGE TOKEN travels OUT-OF-BAND, per row, instead
        (spec §5's T2 amendment -- see `PagedRow`'s docstring for why the
        token, not the row id itself, and why per-row rather than one key
        for the whole batch). Exhaustion has NO separate flag: fewer than
        `batch` rows back means the walk is exhausted (mirroring `read_all`
        -- there is nothing left to fetch), exactly `batch` rows means
        there may be more.

        Raises `ValidationFailed(code=INVALID_BATCH)` if `batch < 1`, and
        `ValidationFailed(code=INVALID_CURSOR)` if `after_key`
        is not `None` and does not resolve (via the unique `page_token`
        index) to a real row (`after_key` is UNTRUSTED input -- see
        the page-token validation rule).

        A keyset walk driven by repeated `read_page` calls never SKIPS a
        row, but MAY REPEAT one that was updated mid-walk: the store is
        close-old-insert-new, so an update's new current version gets a
        higher order key and can re-enter the walk behind the cursor (spec
        pagination-hardening §8). Engine-internal / trusted-caller-only --
        see the protocol docstring's layering rule."""
        ...

    def append_audit(self, entry: AuditEntry) -> None:
        """Persist one audit entry. Never raises (declared-contracts §3
        AC12)."""
        ...

    def audit_entries(self) -> list[AuditEntry]:
        """Every persisted audit entry, in append order."""
        ...

    def enqueue_effects(self, records: Sequence[OutboxRecord]) -> None:
        """Persist emitted effects as durable work items (spec
        `durable-effect-outbox` AC1). Called INSIDE the emitting action's
        transaction, so the rows commit with the ontology writes or not at
        all. Unlike `append_audit`, this one MAY raise: a failure here must
        roll the action back rather than silently drop the delivery."""
        ...

    def claim_due_effects(
        self, *, limit: int, now: datetime, lease: timedelta
    ) -> list[OutboxRecord]:
        """Atomically lease up to `limit` deliverable rows and return them
        (spec AC5): `state = 'pending'`, `next_attempt_at <= now`, and no live
        lease. Each returned record carries the NEW `lease_until`. Ordered by
        `(emitted_at, seq)`, so a batch follows emission order."""
        ...

    def release_effect_claim(self, effect_id: str, *, now: datetime) -> None:
        """Drop the lease without recording an attempt -- the claimer could
        not dispatch (spec AC10: no dispatcher bound). `attempts` and
        `next_attempt_at` are untouched, so the row is immediately claimable
        by a client that CAN send it."""
        ...

    def resolve_effect(
        self,
        effect_id: str,
        *,
        state: OutboxState,
        next_attempt_at: datetime,
        error: str | None,
        now: datetime,
    ) -> None:
        """Record the outcome of one attempt: increment `attempts`, set
        `state`/`next_attempt_at`/`last_error`, and clear the lease. The
        caller (which owns the `RetryPolicy`) decides whether a failure is
        `pending` again or terminally `failed`; the store just writes it."""
        ...

    def outbox_entries(self) -> list[OutboxRecord]:
        """Every outbox row, oldest emission first -- including `delivered`
        and `failed` ones, which are kept as the delivery record rather than
        deleted."""
        ...

    def read_ontology_fingerprint(self) -> OntologyFingerprint | None:
        """The ontology shape this store's rows were written under, or `None`
        for a store that has never recorded one (spec `ontology-evolution`
        AC2)."""
        ...

    def write_ontology_fingerprint(
        self, fingerprint: OntologyFingerprint, *, adopted: bool
    ) -> None:
        """Record (or replace) the fingerprint. `adopted=True` marks one this
        engine stamped onto a store that already held rows -- an upgrade or an
        accepted drift -- rather than one observed from the first write."""
        ...

    def transaction(self) -> AbstractContextManager[Any]:
        """Atomic, reentrant transaction whose outermost level serializes a
        read followed by a write against concurrent writers on the same store."""
        ...

    @property
    def in_transaction(self) -> bool:
        """True while a `transaction()` block (at any nesting depth) is
        open on this store."""
        ...

    def capture_action_writes(self) -> AbstractContextManager[list[WriteRecord]]:
        """Context manager that, while active, enforces authority
        declarations on every `insert`/`update`/`create_link` call and
        records each allowed write as a `WriteRecord` in the yielded list."""
        ...


def _classify_fingerprint_changes(
    stored: OntologyFingerprint,
    current: OntologyFingerprint,
    registry: OntologyRegistry,
) -> tuple[list[str], list[str]]:
    """Return version-covered changes and changes that still require refusal.

    The public fingerprint surface only needs the value model and the digest
    constructor. The store gate retains this internal classification because it
    is part of the existing opening/refusal behavior: a declared upcaster chain
    can make an object-version change readable, while every other mismatch must
    remain explicit and auditable.
    """
    covered: list[str] = []
    uncovered: list[str] = []
    for key in sorted(set(stored.types) | set(current.types)):
        was = stored.types.get(key)
        now = current.types.get(key)
        kind, _, api_name = key.partition(":")
        if was == now:
            continue
        if was is None:
            # A newly declared type has no rows of its own yet -- nothing stored
            # can be unreadable because of it.
            covered.append(f"{kind} {api_name!r}: newly declared (no stored rows)")
            continue
        if now is None:
            uncovered.append(
                f"{kind} {api_name!r}: no longer declared "
                "(its rows, if any, are unreachable)"
            )
            continue
        if kind != "object":
            uncovered.append(f"{kind} {api_name!r}: declaration changed")
            continue

        stored_version = stored.versions.get(api_name, 1)
        current_version = current.versions.get(api_name, 1)
        if current_version == stored_version:
            uncovered.append(
                f"object {api_name!r}: declaration changed but `version` is still "
                f"{current_version} -- bump it and declare an upcaster per step, "
                "or migrate the rows"
            )
            continue
        if current_version < stored_version:
            uncovered.append(
                f"object {api_name!r}: this code declares version "
                f"{current_version} but the store was written at version "
                f"{stored_version} (a downgrade)"
            )
            continue
        missing = [
            version
            for version in range(stored_version, current_version)
            if registry.upcaster(api_name, version) is None
        ]
        if missing:
            uncovered.append(
                f"object {api_name!r}: version {stored_version} -> "
                f"{current_version}, but no upcaster from version(s) {missing}"
            )
            continue
        covered.append(
            f"object {api_name!r}: version {stored_version} -> {current_version}, "
            "covered by declared upcasters"
        )
    return covered, uncovered


def check_ontology_fingerprint(
    store: "Store",
    registry: OntologyRegistry,
    *,
    accept_drift: bool = False,
) -> None:
    """Compare the declared ontology against the one this store recorded, and
    refuse a mismatch (spec `ontology-evolution` AC2-AC5).

    Called from both backends' constructors, so drift detection is a property of
    opening a store rather than of remembering to check. Three cases:

    - **No fingerprint recorded.** A fresh store, or one written before v6.
      Stamp the current one. `adopted` is `True` when rows already exist -- this
      engine cannot know those rows were written under today's declarations, and
      recording that uncertainty is worth more than a tidy flag.
    - **Fingerprint matches.** Nothing to do; the common path costs one indexed
      read.
    - **Fingerprint differs.** Refuse with a `ConflictError` carrying
      `ONTOLOGY_DRIFT` and naming every changed type, unless `accept_drift` --
      which re-stamps and audits.
    """
    current = fingerprint_ontology(registry)
    stored = store.read_ontology_fingerprint()
    if stored is None:
        store.write_ontology_fingerprint(
            current, adopted=_store_has_rows(store, registry)
        )
        return
    if stored.digest == current.digest:
        return

    covered, changes = _classify_fingerprint_changes(stored, current, registry)
    if not changes:
        # Every change is either a new type with no rows or a versioned object
        # type whose upcaster chain covers the stored version (M9b). The store
        # opens without an acknowledgement, because there is nothing to
        # acknowledge: the rows remain readable, exactly as declared. Reads apply
        # the chain lazily; `ontary.migrate.upcast_object_type` makes it
        # permanent when the owner wants the read path to stop working for it.
        store.write_ontology_fingerprint(current, adopted=False)
        store.append_audit(
            AuditEntry(
                kind="migration",
                actor="ontary",
                role="system",
                action="AcceptVersionedOntologyChange",
                target_type="",
                params={
                    "previous_digest": stored.digest,
                    "current_digest": current.digest,
                    "covered": covered,
                },
                outcome="ok",
            )
        )
        return

    if not accept_drift:
        raise ConflictError(
            "the declared ontology does not match the one this store's rows were "
            "written under: "
            + "; ".join(changes)
            + ". Bump the type's `version` and declare an upcaster per step "
            "(ontology.upcaster), migrate the affected rows "
            "(ontary.migrate.migrate_object_type, then "
            "accept_ontology_fingerprint), or open the store with "
            "accept_ontology_drift=True to proceed and record that you did",
            code="ONTOLOGY_DRIFT",
        )

    store.write_ontology_fingerprint(current, adopted=True)
    # Audited, not merely allowed (AC5). `kind="migration"` rather than
    # `"action"`: no actor executed a governed action here, and filing this under
    # the same kind as ordinary writes would hide the one event an auditor most
    # needs to find when the data stops matching the declarations.
    store.append_audit(
        AuditEntry(
            kind="migration",
            actor="ontary",
            role="system",
            action="AcceptOntologyDrift",
            target_type="",
            params={
                "previous_digest": stored.digest,
                "current_digest": current.digest,
                "changes": changes,
            },
            outcome="ok",
        )
    )


def accept_ontology_fingerprint(store: "Store", registry: OntologyRegistry) -> None:
    """Re-stamp `store` with `registry`'s fingerprint, after a migration.

    Separate from `migrate_object_type` on purpose (spec AC8). "The rows are
    migrated" and "this declaration is now the shape of record" are two claims,
    and a tool that made the second one automatically on the success of the first
    would bless a store whose OTHER types are still unmigrated -- a migration
    almost always touches one type at a time.
    """
    store.write_ontology_fingerprint(fingerprint_ontology(registry), adopted=True)


def _store_has_rows(store: "Store", registry: OntologyRegistry) -> bool:
    """Whether any declared object type already holds a row.

    Deliberately goes through the `Store` protocol (`read_page` with a batch of
    1) rather than SQL, so the one implementation serves both backends. Cheap:
    it stops at the first type that has anything.
    """
    for api_name in registry.object_types:
        if store.read_page(api_name, batch=1):
            return True
    return False
