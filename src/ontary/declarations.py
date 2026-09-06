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
    """This runtime's answers to authority, capabilities, write-back,
    re-ingest, visibility, transaction-ownership, idempotency, audit-scope,
    and min-N (spec AC10/AC15). Frozen: a `Declarations` value
    is a read-only snapshot, not something a consumer can mutate and expect
    to change runtime behavior."""

    model_config = ConfigDict(frozen=True)

    authority: str = "model-declared-runtime-checked"
    # Wording note (2026-07-26): the string below was tightened after the
    # implementing agent pointed out that the first draft overclaimed.
    # "injected per call" read as though every call took a provider argument,
    # when providers are bound per client and RESOLVED per invocation. A
    # `Declarations` value exists so a consumer need not read this source to
    # know what the runtime does, so an imprecision here is a defect, not a
    # stylistic quibble.
    capabilities: str = (
        "declared per action/function; provider bound per client, resolved "
        "per invocation, never process-global; fail-closed when unprovided "
        "or undeclared; the provider itself is trusted author code the "
        "runtime does not sandbox"
    )
    writeback: str = (
        "ontology writes are all ontology-owned; the runtime has no outward "
        "write path of its own — but a capability provider is unsandboxed "
        "author code that can write outward inline, so this is a declared "
        "convention, not an enforced boundary"
    )
    reingest: str = (
        "upsert-merge; sources supply only source-backed state; owned "
        "values survive by merge; no deletion"
    )
    visibility_default: str = "deny-by-default — unresolved scope hides"
    transaction_ownership: str = (
        "runtime-owned — refuses caller-opened transactions"
    )
    idempotency: str = "none — retries are distinct audited attempts"
    audit_scope: str = (
        "tenant-scoped administrative view; actions always audited, functions "
        "audited iff they declare capabilities unless overridden per function, "
        "and always when a call releases a hidden field through the "
        "contributor exemption"
    )
    """M8b tightened the first clause. It said "unscoped", which was true when a
    store served one tenant by construction; now a store is bound to a tenant and
    its audit log shows that tenant's entries only. Still administrative WITHIN a
    tenant -- no consumer scope, no redaction -- which is the part that matters for
    an auditor."""

    tenancy: str = (
        "one tenant per store instance, bound at construction; every object, "
        "link and audit read and write is scoped to it; primary keys are "
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
    # silent single point of failure.
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
        "than hidden by it; not every audited row has one"
    )
    min_n: int


def declarations(ontology: OntologyDef) -> Declarations:
    """Build this ontology's `Declarations` value: the runtime's fixed
    answers plus `min_n` read off `ontology.policy.min_n`."""
    return Declarations(min_n=ontology.policy.min_n)
