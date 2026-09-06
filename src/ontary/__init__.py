"""ontary: a domain-agnostic ontology SDK -- declare an ontology, get a
governed runtime (typed objects/links, business-verb actions, derived
functions, scope/min-N/AI-use security), with no imports from any
DSO/domain module (spec AC1).

This module is the SDK's front door: an ontology author or app developer
should be able to author, ingest, read, act, and call functions through the
curated names in `__all__`. The rest of the engine surface remains available
from its canonical submodule (`ontary.meta`, `ontary.store`, `ontary.connect`,
and so on), rather than being flattened into the authoring vocabulary.

`PagedRow` is an engine-internal pagination row. If you are extending the
engine, import it from `ontary.store`.

A note on citations: docstrings throughout this package refer to "the
prototype" and cite `dso.*` module paths. That is the private, pre-SDK
prototype this engine was generalized from -- it is not part of this
repository and never shipped. The citations are provenance for design
decisions (especially security ones), not importable modules.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

from ontary.actions import (
    ActionContext,
    ActionError,
)
from ontary.authoring import (
    ActionParams,
    CapabilityHandle,
    EffectHandle,
    LinkHandle,
    Ontology,
    OntologyObject,
    prop,
    ref,
    scope_ref,
    target,
)
from ontary.client import OntologyClient
from ontary.connect import (
    BaseConnector,
    CanonicalBatch,
    CanonicalRecord,
    LinkBinding,
    MappingSpec,
    ObjectBinding,
    RawTables,
    oid,
    run_pipeline,
)
from ontary.declarations import Declarations, declarations
from ontary.diagnose import Finding
from ontary.effects import EffectDispatcher, EffectMeta, EffectPayload
from ontary.errors import (
    AuthorityError,
    ConflictError,
    InternalError,
    OntaryError,
    PermissionDenied,
    PreconditionFailed,
    ValidationFailed,
    VisibilityError,
)
from ontary.functions import BoundQuery

# `ontary.mcp_server` defers its OWN `mcp` import (`_load_fastmcp`/
# `_load_get_access_token`, called only from inside the builders), so importing
# it here is safe for a core-only install. The redundant `as` alias tells
# ruff this front-door re-export is deliberate.
from ontary.mcp_server import build_mcp_server as build_mcp_server
from ontary.meta import (
    Cardinality,
    Sensitivity,
)
from ontary.outbox import DrainReport, OutboxRecord, RetryPolicy
from ontary.query import Page, TypedPage
from ontary.scope import (
    CustomResolver,
    DirectProperty,
    RowVisibilityStore,
    ScopePolicy,
    SelfScope,
    ViaLink,
)
from ontary.security import Consumer
from ontary.store import (
    InMemoryStore,
    ObjectStore,
    Source,
    Store,
)

# Safe to import unconditionally: `store.postgres` imports psycopg lazily, so the
# front door stays available to a core-only install (pinned by
# tests/test_store_postgres.py::test_missing_postgres_extra_names_the_install_command
# and by the clean-install smoke script).
from ontary.store.postgres import PostgresStore

try:
    __version__ = _installed_version("ontary")
except PackageNotFoundError:  # pragma: no cover - only when run from a bare checkout
    # Read from installed metadata rather than hardcoded here, so the number a
    # consumer pins and the number the runtime reports cannot disagree --
    # `pyproject.toml` is the single source (pinned by
    # tests/test_packaging.py::test_version_matches_pyproject).
    #
    # `0+unknown` is the PEP 440 local version for "running from a source tree
    # that was never installed". Deliberately not a plausible-looking number: a
    # bug report quoting `0+unknown` says something true about the reporter's
    # environment, whereas a hardcoded fallback would quietly claim a release.
    __version__ = "0+unknown"

# Sorted with plain `sorted()` (Python's default string ordering: exact
# ASCII code-point comparison, case-SENSITIVE). This is the authoring
# vocabulary plus the deliberately small runtime/error entry points; engine
# names not listed here remain importable from their canonical submodule.
# Pinned by tests/test_docs.py::test_ontary_all_is_sorted_unique_and_importable
# and its demotion/importability guard.
__all__ = [
    "ActionContext",
    "ActionError",
    "ActionParams",
    "AuthorityError",
    "BaseConnector",
    "BoundQuery",
    "CanonicalBatch",
    "CanonicalRecord",
    "CapabilityHandle",
    "Cardinality",
    "ConflictError",
    "Consumer",
    "CustomResolver",
    "Declarations",
    "DirectProperty",
    "DrainReport",
    "EffectDispatcher",
    "EffectHandle",
    "EffectMeta",
    "EffectPayload",
    "Finding",
    "InMemoryStore",
    "InternalError",
    "LinkBinding",
    "LinkHandle",
    "MappingSpec",
    "ObjectBinding",
    "ObjectStore",
    "OntaryError",
    "Ontology",
    "OntologyClient",
    "OntologyObject",
    "OutboxRecord",
    "Page",
    "PermissionDenied",
    "PostgresStore",
    "PreconditionFailed",
    "RawTables",
    "RetryPolicy",
    "RowVisibilityStore",
    "ScopePolicy",
    "SelfScope",
    "Sensitivity",
    "Source",
    "Store",
    "TypedPage",
    "ValidationFailed",
    "ViaLink",
    "VisibilityError",
    "__version__",
    "build_mcp_server",
    "declarations",
    "oid",
    "prop",
    "ref",
    "run_pipeline",
    "scope_ref",
    "target",
]
