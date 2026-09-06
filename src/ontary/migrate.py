"""Rewrite stored rows to match a changed declaration (spec
`ontology-evolution` option D).

`ontary.fingerprint` tells you the ontology changed and `ObjectStore` refuses to
open until you deal with it. This is what you run to deal with it: walk every
current row of one object type, transform it, and write the result back --
explicitly, with a dry run, a per-row report, and an audit trail.

**Not a read-time upcaster.** The spec's options B and C would reinterpret an old
row on every read; this rewrites it once. The trade is a maintenance window in
exchange for a store whose rows actually match the declarations, and for reads
that mean exactly what they say. `docs/compatibility.md` records that trade.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from ontary.audit import AuditEntry
from ontary.errors import ValidationFailed
from ontary.meta import OntologyRegistry, declared_shape_violation
from ontary.store import DEFAULT_BATCH, Source, Store

__all__ = [
    "MigrationFailure",
    "MigrationReport",
    "migrate_object_type",
    "upcast_object_type",
]


class MigrationFailure(BaseModel):
    """One row the migration could not rewrite, and why.

    Frozen, and it carries the object id rather than the payload: a report that
    is safe to log should not be a second copy of the data. The id is enough to
    find the row again."""

    model_config = ConfigDict(frozen=True)

    object_id: str
    reason: str


class MigrationReport(BaseModel):
    """What one `migrate_object_type` call did.

    `scanned == changed + unchanged + len(failures)` always holds, so a caller
    can assert the whole accounting rather than one number -- the same shape
    `DrainReport` uses for effect delivery."""

    model_config = ConfigDict(frozen=True)

    object_type: str
    dry_run: bool
    scanned: int = 0
    changed: int = 0
    unchanged: int = 0
    """The transform returned a payload equal to the stored one. Counted, not
    written: rewriting an identical row would burn a version in the store's
    close-old-insert-new history and make the migration look like an edit to
    every row it touched."""
    failures: list[MigrationFailure] = []

    @property
    def ok(self) -> bool:
        return not self.failures


def migrate_object_type(
    store: Store,
    registry: OntologyRegistry,
    api_name: str,
    transform: Callable[[dict[str, Any]], dict[str, Any]],
    *,
    source: Source | None = None,
    dry_run: bool = False,
    batch: int = DEFAULT_BATCH,
    force_rewrite: bool = False,
) -> MigrationReport:
    """Rewrite every current row of `api_name` through `transform`.

    `transform` receives a copy of the stored payload and returns the payload it
    should become. Returning it unchanged is fine and cheap (counted as
    `unchanged`, nothing written); raising is fine too and lands in
    `failures` rather than aborting the run -- one poisonous row out of a million
    should not leave the other 999,999 unmigrated.

    Each result is validated against the type's CURRENT declaration before it is
    written, so a migration cannot itself produce the very rows this milestone
    exists to eliminate. A row whose result fails validation is a failure, not a
    write.

    **`transform` must be idempotent.** Batches commit as they go, so a crash
    mid-run leaves some rows migrated and some not, and the fix is to run it
    again -- the same contract the effect outbox puts on a dispatcher, for the
    same reason.

    `dry_run=True` reports exactly what a real run would do and writes nothing:
    every row is still transformed and validated, so a dry run finds the failures
    too.

    `force_rewrite=True` writes even when the transform returns the payload
    unchanged. Exists for exactly one caller -- `upcast_object_type`, where the
    *payload* is already current (reads upcast at the store boundary) and the
    point of the write is to move the row's stored `type_version` forward. For a
    value-changing transform it would only burn a version in the store's history
    for every row, so it defaults off.

    Does NOT re-stamp the ontology fingerprint. Call
    `ontary.store.accept_ontology_fingerprint` when every affected type is
    migrated (spec AC8) -- "these rows are migrated" and "this declaration is now
    the shape of record" are separate claims, and a migration usually touches one
    type at a time.
    """
    obj_def = registry.get_object_type(api_name)

    if batch < 1:
        # Mirrors `read_page`'s own refusal rather than inventing a second
        # convention for the same mistake.
        raise ValidationFailed(
            f"migrate_object_type: batch must be >= 1, got {batch}",
            code="INVALID_BATCH",
        )

    write_source = source or Source(source_system=f"migration:{api_name}")
    scanned = changed = unchanged = 0
    failures: list[MigrationFailure] = []

    # TWO PHASES, and the reason is a bug this tool had on its first run:
    # rewriting a row gives it a NEW row identity (the store is
    # close-old-insert-new), which lands it AFTER the cursor -- so a
    # walk-and-rewrite loop visits every row it just rewrote a second time. That
    # double-counted `scanned` at best, and with a transform whose output is not
    # value-stable it never terminated at all.
    #
    # So: walk first, collecting only object ids (a fraction of the payload
    # bytes, which is what makes this affordable on the large table a migration
    # is actually run against), then rewrite each id exactly once.
    #
    # Known limitation, inherited rather than introduced: the store does not
    # enforce payload-pk uniqueness among current rows, so two current rows
    # sharing an id are migrated as one (`read_current` returns one of them).
    object_ids: list[str] = []
    seen: set[str] = set()
    after: str | None = None
    while True:
        page = store.read_page(api_name, after_key=after, batch=batch)
        if not page:
            break
        for row in page:
            object_id = row.obj.lineage.object_id
            if object_id not in seen:
                seen.add(object_id)
                object_ids.append(object_id)
        after = page[-1].key

    for object_id in object_ids:
        current = store.read_current(api_name, object_id)
        if current is None:  # pragma: no cover - concurrent delete is not a thing here
            continue
        scanned += 1
        stored = dict(current.payload)
        try:
            result = transform(dict(stored))
        except Exception as exc:  # author code; one bad row is not fatal
            failures.append(
                MigrationFailure(
                    object_id=object_id,
                    reason=f"transform raised {type(exc).__name__}: {exc}",
                )
            )
            continue
        if result == stored and not force_rewrite:
            unchanged += 1
            continue
        violation = declared_shape_violation(obj_def, result)
        if violation is not None:
            failures.append(
                MigrationFailure(
                    object_id=object_id,
                    reason=f"result violates the declared shape: {violation}",
                )
            )
            continue
        changed += 1
        if not dry_run:
            # A key the transform DROPPED is explicitly nulled, not just
            # omitted: `Store.update` merges over the current row, so omitting a
            # key leaves the old value in place -- silently turning "drop this
            # property" into a no-op, which is one of the five silent failures
            # §1 of the spec measured.
            #
            # The key itself survives as `null`, because merge is the only write
            # primitive the `Store` protocol has. That is a real difference from
            # deletion and it is the right side of the line: the old VALUE is
            # gone, so re-declaring the property later resurrects `null` rather
            # than stale data. A true key-removing write would need a new
            # protocol method on both backends, which is more surface than this
            # buys.
            removed = {key: None for key in stored if key not in result}
            with store.transaction():
                store.update(api_name, object_id, {**removed, **result}, write_source)

    report = MigrationReport(
        object_type=api_name,
        dry_run=dry_run,
        scanned=scanned,
        changed=changed,
        unchanged=unchanged,
        failures=failures,
    )
    if not dry_run:
        _audit_migration(store, report)
    return report


def _audit_migration(store: Store, report: MigrationReport) -> None:
    """Record the rewrite in the audit log, as a migration (spec AC7).

    `kind="migration"`, not `"action"`: no consumer executed anything, and the
    window in which the data stopped matching the declarations is exactly what an
    auditor will look for. Filing it as an ordinary write would hide it among the
    writes it is supposed to explain.

    Best-effort, like the effect outbox's finalization append: the rows are
    already rewritten, and raising here would report a completed migration as a
    failure.
    """
    try:
        store.append_audit(
            AuditEntry(
                kind="migration",
                actor="ontary",
                role="system",
                action="MigrateObjectType",
                target_type=report.object_type,
                params={
                    "scanned": report.scanned,
                    "changed": report.changed,
                    "unchanged": report.unchanged,
                    "failed": [f.model_dump() for f in report.failures],
                },
                outcome="ok" if report.ok else "partial",
            )
        )
    except Exception:  # pragma: no cover - append_audit never raises today
        pass


def upcast_object_type(
    store: Store,
    registry: OntologyRegistry,
    api_name: str,
    *,
    dry_run: bool = False,
    batch: int = DEFAULT_BATCH,
) -> MigrationReport:
    """Make a lazy upcast permanent: rewrite every row through its declared
    upcaster chain and stamp it at the current version (M9b).

    The other half of versioned evolution. `@ontology.upcaster` keeps old rows
    READABLE -- the chain runs on every read, forever, and can never be deleted
    while one row still needs it. This walks the rows once so that stops being
    true: afterwards the read path does nothing but compare two integers, and the
    upcaster becomes dead code the author can delete on the next version bump.

    Nothing to declare beyond the chain itself: the transform IS
    `upcast_payload`, which is also what reads apply, so a rewrite cannot disagree
    with a lazy read of the same row. That is the point of routing both through
    one function rather than asking an author to write the migration twice.

    Reads already return upcast payloads, so from the outside this looks like a
    no-op rewrite: `store.update` writes back what the read produced, and the
    write is what lands the row at the declared version. It therefore passes
    `force_rewrite=True` -- without it, value equality would count every row as
    `unchanged` and the version would never move. Rows already at the current
    version are rewritten too, harmlessly; if that matters for a very large type,
    check the report's `scanned` against what you expected before running it
    without `dry_run`.
    """
    return migrate_object_type(
        store,
        registry,
        api_name,
        # `read_page`/`read_current` inside `migrate_object_type` already upcast
        # at the store boundary, so by the time a payload reaches here it is
        # current-shape. Returning it unchanged would count `unchanged` and write
        # nothing -- which is why the transform re-stamps by returning a copy:
        # the write is what moves `type_version` forward.
        lambda payload: dict(payload),
        source=Source(source_system=f"upcast:{api_name}"),
        dry_run=dry_run,
        batch=batch,
        # The payload a read hands back is ALREADY upcast, so it equals what the
        # transform returns and nothing would be written without this. The write
        # is the whole point here: it is what advances the row's stored
        # `type_version` so the read path stops applying the chain.
        force_rewrite=True,
    )
