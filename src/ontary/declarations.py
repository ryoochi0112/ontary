"""The declarations value (spec `declared-contracts` §3 AC10, §6.6): a
single enumerable value stating this runtime's answers to the questions a
consumer would otherwise have to trust prose (docstrings, this spec) for.

`declarations(ontology)` combines the runtime's fixed answers -- true for
every `OntologyClient`/MCP server this SDK builds, because they are baked
into the engine (`ActionExecutor`, `ObjectStore`, `ingest`), not something
an ontology author configures -- with the one answer that *is* per-ontology
config: `min_n`, read off the ontology's own `ScopePolicy`. Every field here
is pinned by a test in `tests/test_declarations.py` asserting the declared
value IS the actual runtime behavior, not just a string.

`identity` is the one field whose answer is NOT the same across every
surface: how a Consumer's identity comes to be trusted differs between a
multi-consumer MCP server (`build_multi_consumer_mcp_server`, a proven
principal) and everything else -- a single-consumer server
(`build_mcp_server`) or direct Python use (an operator-asserted `Consumer`,
never proven). The string says both, scoped, rather than picking one and
being false about the other.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from ontary.ontology import OntologyDef


class Declarations(BaseModel):
    """This runtime's answers to authority, capabilities, effects,
    write-back, re-ingest, visibility, transaction-ownership, idempotency,
    audit-scope, and min-N (spec AC10/AC15). Frozen: a `Declarations` value
    is a read-only snapshot, not something a consumer can mutate and expect
    to change runtime behavior."""

    model_config = ConfigDict(frozen=True)

    authority: str = "model-declared-runtime-checked"
    # Wording note 2 (whole-branch review, 2026-07-26): `writeback` and
    # `capabilities` were tightened AGAIN. The capability/effect split is by
    # DIRECTION, but it is enforced only for the runtime's own machinery: a
    # provider is arbitrary author code returned unwrapped (`actions.py`,
    # `functions.py`), so a "capability" called `Mailer` can perform an outward
    # WRITE inline -- including from a Function, which AC4 otherwise forbids from
    # writing outward at all, and including before an Action then raises and rolls
    # its ontology writes back. The runtime cannot see that, let alone stop it.
    # The previous `writeback` string ("outward writes only via declared effects")
    # therefore asserted a boundary that does not exist. §11 already admitted
    # effects are "declared and audited, not sandboxed or filtered"; this makes
    # the same admission where a consumer actually reads it.
    #
    # Wording note (2026-07-26): both strings below were tightened after the
    # implementing agent pointed out that the first drafts overclaimed.
    # "injected per call" read as though every call took a provider argument,
    # when providers are bound per client and RESOLVED per invocation; and
    # "a dispatch failure is audited" stood in flat contradiction to
    # "finalization is best-effort" one clause later -- it is audited only when
    # the follow-up append itself succeeds. A `Declarations` value exists so a
    # consumer need not read this source to know what the runtime does, so an
    # imprecision here is a defect, not a stylistic quibble.
    capabilities: str = (
        "declared per action/function; provider bound per client, resolved "
        "per invocation, never process-global; fail-closed when unprovided "
        "or undeclared; the provider itself is trusted author code the "
        "runtime does not sandbox"
    )
    # Rewritten 2026-07-26 (`durable-effect-outbox` AC8). The old string ended
    # "at-most-once, no retry ... a pending effect may or may not have been
    # delivered", which was the honest answer while delivery state lived in an
    # append-only audit row nothing ever read again. It is now a durable outbox
    # row a drain retries, so the guarantee flipped to AT-LEAST-ONCE -- and the
    # redelivery window is named here rather than left for a reader to infer,
    # because a dispatcher that is not idempotent is now the caller's bug and
    # they can only know that if this says so.
    effects: str = (
        "declared per action; emitted as data inside the transaction; the "
        "durable outbox row and the pending audit record commit with the "
        "writes; dispatched after commit in emission order; at-least-once "
        "with bounded retries -- a dispatcher must be idempotent on "
        "EffectMeta.effect_id, since a crash between the outside call and "
        "the delivered mark redelivers; retries run only when the embedder "
        "calls drain_effects (no background thread); an Exception from a "
        "dispatcher is never raised to the caller and never rolled back, "
        "and is audited on a best-effort basis, while a KeyboardInterrupt "
        "or SystemExit does propagate and stops later dispatches, leaving "
        "those effects pending for a later drain; an exhausted row is "
        "failed and never retried again"
    )
    writeback: str = (
        "ontology writes are all ontology-owned; the runtime's own outward "
        "write path is declared effects, dispatched after commit — but a "
        "capability provider is unsandboxed author code that can also write "
        "outward inline, so this is a declared convention, not an enforced "
        "boundary"
    )
    reingest: str = (
        "upsert-merge; sources supply only source-backed state; owned "
        "values survive by merge; no deletion"
    )
    visibility_default: str = "deny-by-default — unresolved scope hides"
    transaction_ownership: str = (
        "runtime-owned — refuses caller-opened transactions"
    )
    ontology_evolution: str = (
        "fingerprinted; a store refuses to open under a declaration it was not "
        "written under (ONTOLOGY_DRIFT) unless the change is a declared type "
        "version bump whose upcaster chain covers the stored version, or the "
        "drift is explicitly accepted -- both audited; an older row is upcast "
        "lazily at the store read boundary, so every surface sees one shape, and "
        "upcast_object_type rewrites it permanently; documentation-only fields "
        "are excluded from the fingerprint"
    )
    """Added by M9a, rewritten by M9b when versioned changes stopped needing an
    acknowledgement. Its counterpart in `docs/compatibility.md` is the procedure;
    this is the runtime-readable claim, so a consumer can ask the engine rather
    than trust a document -- which is the whole point of `Declarations`.

    "never upcast on read" was true for one milestone and is now false, which is
    exactly the kind of sentence `docs/compatibility.md` calls a breaking change
    even when no signature moved."""

    idempotency: str = "none — retries are distinct audited attempts"
    audit_scope: str = (
        "tenant-scoped administrative view; actions always audited, functions "
        "audited iff they declare capabilities unless overridden per function"
    )
    """M8b tightened the first clause. It said "unscoped", which was true when a
    store served one tenant by construction; now a store is bound to a tenant and
    its audit log shows that tenant's entries only. Still administrative WITHIN a
    tenant -- no consumer scope, no redaction -- which is the part that matters for
    an auditor."""

    tenancy: str = (
        "one tenant per store instance, bound at construction; every object, "
        "link, audit and outbox read and write is scoped to it; primary keys are "
        "unique per tenant, not globally; on Postgres, row-level security "
        "policies enforce the same boundary in the database (unless rls=False), "
        "and a superuser bypasses them by Postgres design"
    )
    # Added by M10 (spec `multi-consumer-mcp` AC11), rewritten after a
    # review caught clause 1 making an unscoped whole-SDK claim that is
    # false on `build_mcp_server`: that server has no verifier and refuses
    # NOTHING, so "a deployment with no configured verifier refuses every
    # call" is only true of the multi-consumer server. The string now says,
    # in order: (1) on a multi-consumer server, identity is proven by the
    # transport, not this runtime, and a deployment with no configured
    # verifier refuses every call; (2) on a single-consumer server or in
    # direct Python use, the Consumer is asserted by the operator at
    # construction and nothing proves it -- the honest counterpart to (1),
    # not merely a weaker "carries no principal"; (3) the resolver that maps
    # a verified principal to a Consumer (multi-consumer only) is trusted
    # author code, returned no proxy and inspected by nothing here -- the
    # same admission `capabilities` already makes about a provider, made
    # here about a resolver; (4) the principal and the actor it resolved to
    # are BOTH audited, which is what makes a resolver that maps every
    # principal onto one privileged actor visible in the log instead of a
    # silent single point of failure; (5) the one disclosed exception to
    # (4): `actions.py`'s `drain_effect_outbox` redelivery entry restates
    # the actor but deliberately carries no principal of its own (spec
    # §4.2) -- named here rather than left to be discovered as a silent gap
    # in (4)'s "both ... are audited".
    #
    # Deliberately NOT claimed: anything about a resolver's own error
    # messages. A resolver's deliberate `OntaryError` is re-raised to the
    # caller verbatim (`mcp_server.py`); if that message embeds token
    # material, this runtime cannot see it and cannot stop it -- the exact
    # boundary `writeback` already draws around a capability provider's own
    # outward writes.
    identity: str = (
        "on a multi-consumer MCP server, proven by the transport, not by "
        "this runtime -- no token is verified or issued here, so such a "
        "deployment with no configured verifier refuses every call rather "
        "than assuming a default identity; on a single-consumer server or "
        "in direct Python use, the Consumer is asserted by the operator at "
        "construction and nothing proves it; the verified principal (multi-"
        "consumer only) is mapped to a Consumer by a resolver, which is "
        "trusted author code the runtime does not sandbox, on the same "
        "footing as a capability provider; both that principal and the "
        "actor it resolved to are audited, so a resolver mapping every "
        "principal onto one privileged actor is visible in the log rather "
        "than hidden by it; a redelivered effect's audit row restates the "
        "actor but not the principal, joined back by invocation_id -- so "
        "principal is None on those rows; not every audited row has one"
    )
    min_n: int


def declarations(ontology: OntologyDef) -> Declarations:
    """Build this ontology's `Declarations` value: the runtime's fixed
    answers plus `min_n` read off `ontology.policy.min_n`."""
    return Declarations(min_n=ontology.policy.min_n)
