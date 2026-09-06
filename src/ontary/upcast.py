"""Read a row stored under an older declaration (spec `ontology-evolution`
options B and C, built as M9b).

M9a made drift *visible*: a store refuses to open under a declaration it was not
written under. That is the right default and it is also a wall — the only ways
through were "rewrite every row now" or "accept the drift and hope". This is the
third way: an author bumps a type's `version` and declares one upcaster per step,
and the engine reads an older row *as if* it had been written under today's shape.

**Where this applies matters more than how it works.** Upcasting happens at the
STORE READ boundary, so every reader above it -- the typed client, the string and
MCP surfaces, the guarded query layer, aggregates, scope resolution -- sees rows in
the current shape. Doing it only in typed hydration would have been less code and
would have recreated the exact defect M9a exists to prevent: one row, two readers,
two different realities.

**The honest cost, stated once here and again in `docs/compatibility.md`:** a lazy
upcast is applied on EVERY read of that row, forever, and the chain can never be
deleted while a row still needs it. `ontary.migrate.upcast_object_type` is the
remedy -- it rewrites the rows through the same declared chain, once, and stamps
them at the current version, after which the read path stops doing any work.
"""

from __future__ import annotations

from typing import Any

from ontary.errors import ConflictError, ValidationFailed
from ontary.meta import OntologyRegistry

__all__ = ["upcast_payload"]


def upcast_payload(
    registry: OntologyRegistry,
    obj_type: str,
    payload: dict[str, Any],
    stored_version: int,
    *,
    object_id: str | None = None,
) -> dict[str, Any]:
    """Bring one stored payload up to `obj_type`'s declared version.

    Returns the payload unchanged (same object) when the row is already current,
    which is the overwhelmingly common case and costs one integer comparison --
    worth caring about, because this sits on every read of every row.

    A row stamped NEWER than the declared version raises a conflict-kind
    `UPCAST_FAILED` failure rather than being read
    with today's shape: that is a downgrade, and the engine has no way to remove
    a change it never saw. Same reasoning as the
    `STORE_VERSION_UNSUPPORTED` conflict for the physical schema.
    """
    try:
        obj_def = registry.get_object_type(obj_type)
    except ValidationFailed:
        # An unregistered type has no declared version to reach; the caller's
        # own unknown-type error is the better one to surface, so leave the
        # payload alone rather than inventing a failure here.
        return payload

    target = obj_def.version
    if stored_version == target:
        return payload
    if stored_version > target:
        raise ConflictError(
            f"{obj_type}/{object_id or '?'} was written under version "
            f"{stored_version} of {obj_type!r}, but this code declares version "
            f"{target} -- a store cannot be read by an ontology older than the "
            "one that wrote it; upgrade the code, or restore a copy of the data "
            "from before the newer declaration ran",
            code="UPCAST_FAILED",
        )

    current = dict(payload)
    for version in range(stored_version, target):
        step = registry.upcaster(obj_type, version)
        if step is None:
            # `validate()` refuses an incomplete chain, so this is reachable only
            # for a registry that was never validated. Still refuse rather than
            # return a half-upcast row.
            raise ConflictError(
                f"{obj_type}/{object_id or '?'}: no upcaster from version "
                f"{version} of {obj_type!r} (needed to reach version {target})",
                code="UPCAST_FAILED",
            )
        try:
            current = step(current)
        except Exception as exc:
            raise ConflictError(
                f"{obj_type}/{object_id or '?'}: the upcaster from version "
                f"{version} of {obj_type!r} raised "
                f"{type(exc).__name__}: {exc}",
                code="UPCAST_FAILED",
            ) from exc
    return current
