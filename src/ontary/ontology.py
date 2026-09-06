"""`OntologyDef`: the single authored bundle a client/MCP server binds to.

Spec §5: "`OntologyDef` -- one authored object bundling registry + scope
policy + config, the unit the client/MCP server binds to." Spec §7 requires
two ontologies to coexist in one process with no cross-talk -- there is
deliberately NO module-global registry/policy/functions-table anywhere in
this SDK; every one of those lives on an `OntologyDef` *instance* instead.

LEGACY (store-closing) action handlers are deliberately NOT stored here --
typed class-authored handlers, which never close over a store, DO ride on
`OntologyDef.action_handlers` since C2 of the staged refactor (see the
class docstring). The original reasoning, which still holds for the legacy
kind: `dso.actions`'s prototype
handlers (see `tests/test_actions.py::_make_checkout_handler` and friends)
close over the `ObjectStore` they write through -- and `OntologyDef` has no
store (only `OntologyClient` does, once one is constructed against a
concrete `ObjectStore`). Forcing handlers onto `OntologyDef` would either
deny them store access (breaking every handler that needs to read/write
related objects) or require threading a store into `OntologyDef` and
re-introducing per-store global state. Instead, `OntologyClient` builds and
exposes its own `ActionExecutor` as `client.actions` (descriptor-authored
ontologies wire it via the internal `_register` seam -- see
`tests/test_client.py`); class-authored (`Ontology.action(...)`) handlers,
by contrast, ARE declared on the `Ontology` and auto-bind to every runtime
built from it (spec `typed-actions.md` §6 -- the old public per-client
`register_handler`/`.handler` surface was removed, AC7). The seam an
ontology author writes once for descriptor authoring is a
`make_handlers(store) -> dict[api_name, fn]`-shaped factory function,
which is still a per-ontology *authoring* artifact -- not per-consumer, since
the handler bodies never vary by which `Consumer` calls `execute` (that's an
argument `ActionExecutor.execute` passes through, not something the handler
closes over) -- it is simply re-applied to each client's own store/executor
at construction time, the same way `bulk_upsert`/`bulk_link` are re-applied
per store rather than memoized on the registry.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from ontary.functions import FunctionRegistry
from ontary.meta import OntologyRegistry
from ontary.scope import ScopePolicy

if TYPE_CHECKING:
    from ontary.actions import TypedHandler
    from ontary.model import ActionParams


class OntologyDef:
    """The authored artifact: a named `registry` + `policy` + `functions`
    bundle -- the unit `OntologyClient` (and, later, an MCP server) binds
    to.

    `functions` defaults to a fresh `FunctionRegistry` bound to `registry` if
    not supplied.

    `action_handlers` (C2 of the staged refactor) carries class-authored
    (`Ontology.action(...)`) typed handlers so a runtime built from this
    definition can auto-bind them -- mirroring how `Ontology.definition`
    already bakes `_function_handlers` into `functions`. This does NOT
    revisit the module docstring's rule about legacy store-closing handlers:
    a typed handler is a `(ctx, params)` callable that never closes over a
    store, so carrying it here adds no per-store state. Empty for
    descriptor-authored ontologies (which keep wiring `_register` per
    client).
    """

    def __init__(
        self,
        name: str,
        registry: OntologyRegistry,
        policy: ScopePolicy,
        functions: FunctionRegistry | None = None,
        action_handlers: Mapping[str, tuple[TypedHandler, type[ActionParams]]]
        | None = None,
    ) -> None:
        self.name = name
        self.registry = registry
        self.policy = policy
        self.functions = (
            functions if functions is not None else FunctionRegistry(registry)
        )
        self.action_handlers = dict(action_handlers or {})

    def validate(self) -> None:
        """Raise on any dangling reference in `registry` or `policy`
        (a `ValidationFailed` with code `ONTOLOGY_INVALID` or
        `SCOPE_POLICY_ERROR`) -- the single
        validation entry point an ontology author calls once after
        declaring everything."""
        self.registry.validate()
        self.policy.validate(self.registry)


@runtime_checkable
class SupportsDefinition(Protocol):
    """The structural face of `ontary.authoring.Ontology` (or anything
    else that can produce an `OntologyDef`): what `resolve_definition`
    accepts besides an `OntologyDef` itself."""

    @property
    def definition(self) -> OntologyDef: ...


def resolve_definition(ontology: OntologyDef | SupportsDefinition) -> OntologyDef:
    """THE single normalization point for the `OntologyDef | Ontology`
    union every public entry (client, MCP server builders, connect
    pipeline) accepts. Call it once at the boundary; everything below works
    with a plain `OntologyDef`."""
    return ontology if isinstance(ontology, OntologyDef) else ontology.definition
