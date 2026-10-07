"""The `Consumer` identity model, shared by `ontary.scope` and `ontary.security`.

Lives in its own module so neither of those two has to import the other.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

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
    # is trusted to set: the intent is for a multi-consumer MCP server to
    # overwrite it, after `resolve_consumer` returns, from the verified access
    # token's `subject` (falling back to `client_id`) -- so that a resolver
    # cannot forge who authenticated. That overwrite is the multi-consumer
    # server's job, not this model's, and until it lands this field is
    # UNENFORCED: any caller can set it to whatever it wants.
    principal: str | None = None
