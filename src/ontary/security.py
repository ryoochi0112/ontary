"""Consumer identity + the coverage rule the guarded query layer and the
action executor both apply against a `ScopePolicy` resolution.

Generalized replacement for `dso.security`'s `Consumer`/`Role`/`ScopeType`/
`covers_scope`: role and scope level are author-defined strings here (no
`Literal["TeamOwner", ...]`, no `Literal["team", "company"]`), and the
resolution itself lives in `ontary.scope` so it can be shared by both the
read path and the write path without disagreeing about who owns an object.
"""

from __future__ import annotations

from ontary._consumer import Consumer as Consumer
from ontary._consumer import ConsumerKind as ConsumerKind
from ontary.scope import ScopePolicy


def covers_scope(
    policy: ScopePolicy, consumer: Consumer, resolved: dict[str, str | None]
) -> bool:
    """Does `consumer` cover an object whose owning scope resolved to
    `resolved` (a `level -> scope id` dict, see
    `ontary.scope.resolve_owning_scope`)?

    The consumer covers the object iff the scope id resolved at the
    consumer's own `scope_level` matches the consumer's `scope_id` exactly.
    An unresolved (`None`) dimension DENIES -- scope enforcement never
    fails open, matching the prototype's `covers_scope`. `policy` is
    accepted (rather than relying on `resolved` alone) so callers always
    pass the policy the resolution came from; today's rule only needs
    `resolved`/`consumer`, but keeping `policy` in the signature lets a
    future rule (e.g. "a company-scoped consumer also covers its child
    teams") change without breaking callers.

    Resolved 2026-09-04: exact-scope-id match is the contract. A consumer
    scoped at a *parent* level (e.g. company) does NOT automatically cover
    objects owned by a *child* scope (e.g. one of that company's teams);
    this function denies in that case rather than inferring hierarchy.
    Parent-covers-child is opt-in and post-1.0. Revisit `levels` ordering
    on `ScopePolicy` if/when that rule is needed.
    """
    resolved_id = resolved.get(consumer.scope_level)
    return resolved_id is not None and resolved_id == consumer.scope_id
