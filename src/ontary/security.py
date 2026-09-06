"""Consumer identity + the coverage rule the guarded query layer and the
action executor both apply against a `ScopePolicy` resolution.

Generalized replacement for `dso.security`'s `Consumer`/`Role`/`ScopeType`/
`covers_scope`: role and scope level are author-defined strings here (no
`Literal["TeamOwner", ...]`, no `Literal["team", "company"]`), and the
resolution itself lives in `ontary.scope` so it can be shared by both the
read path and the write path without disagreeing about who owns an object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel

if TYPE_CHECKING:
    # Only used for a function type hint below (`covers_scope`); imported
    # lazily to avoid a circular import with `ontary.scope`, which needs
    # `Consumer` itself (at runtime, not just for typing) for
    # `ScopePolicy.row_visibility`'s `RowVisibilityFn` (see that module's
    # docstring).
    from ontary.scope import ScopePolicy

ConsumerKind = Literal["human", "ai"]


class Consumer(BaseModel):
    """Identifies who/what is issuing a guarded query or action.

    `role` and `scope_level` are author-defined strings (declared by the
    ontology's `ScopePolicy.levels` and whatever role names the ontology's
    actions use) -- nothing here hardcodes a domain's vocabulary.
    """

    actor_id: str
    role: str
    scope_level: str
    scope_id: str
    kind: ConsumerKind
    # The identity the TRANSPORT proved -- distinct from `actor_id`, which is
    # who this consumer RESOLVED to. `None` means no transport proved an
    # identity for this call: direct Python use, or the single-consumer stdio
    # server (`build_mcp_server`), where the process itself is the only proof
    # there is. This field is meant to be a CARRIER, not an input author code
    # is trusted to set: the intent (spec `multi-consumer-mcp` AC9) is for a
    # multi-consumer MCP server to overwrite it, after `resolve_consumer`
    # returns, from the verified access token's `subject` (falling back to
    # `client_id`) -- so that a resolver cannot forge who authenticated.
    # That overwrite is the multi-consumer server's job (M10 T6), not this
    # model's, and until it lands this field is UNENFORCED: any caller can
    # set it to whatever it wants.
    principal: str | None = None


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

    Decision B (`docs/v1-gate.md`), resolved 2026-09-04: exact-scope-id
    match is the v1 contract. A consumer scoped at a *parent* level (e.g.
    company) does NOT automatically cover objects owned by a *child* scope
    (e.g. one of that company's teams); this function denies in that case
    rather than inferring hierarchy. Parent-covers-child is opt-in and
    post-1.0. Revisit `levels` ordering on `ScopePolicy` if/when that rule
    is needed.
    """
    resolved_id = resolved.get(consumer.scope_level)
    return resolved_id is not None and resolved_id == consumer.scope_id
