"""Shared test factories.

These fixtures are the SANCTIONED path for test builders. Test files must use
them rather than hand-rolling a local duplicate of a shared builder.

Fixtures return *factories* (callables), because nearly every per-file helper
takes parameters (role, scope, api_name, extra properties).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from typing import Any, Literal, cast

import pytest

from ontary.authoring import Ontology
from ontary.errors import OntaryError
from ontary.meta import (
    ActionTypeDef,
    CapabilityDef,
    FunctionDef,
    LinkTypeDef,
    ObjectTypeDef,
    OntologyRegistry,
    PropertyDef,
    PropertyType,
)
from ontary.scope import RowVisibilityFn, ScopePolicy, ScopeRule
from ontary.security import Consumer, ConsumerKind
from ontary.store import DEFAULT_TENANT, ObjectStore, Store

CallFactory = Callable[..., dict[str, Any]]
ConsumerFactory = Callable[..., Consumer]
OntologyFactory = Callable[..., Ontology]
ObjectTypeFactory = Callable[..., ObjectTypeDef]
PolicyFactory = Callable[..., ScopePolicy]
RegistryFactory = Callable[..., OntologyRegistry]
StoreFactory = Callable[..., Store]


@contextmanager
def raises_code(
    kind_cls: type[OntaryError], code: str
) -> Iterator[pytest.ExceptionInfo[OntaryError]]:
    """Assert that a block raises ``kind_cls`` with the exact ``code``."""
    with pytest.raises(kind_cls) as excinfo:
        yield excinfo
    assert excinfo.value.code == code


class _BlockMCPImports:
    """A meta-path finder that makes `mcp` look uninstalled."""

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> None:
        if fullname == "mcp" or fullname.startswith("mcp."):
            raise ModuleNotFoundError(f"No module named {fullname!r}", name=fullname)
        return None


@contextmanager
def _mcp_uninstalled() -> Iterator[None]:
    """Temporarily block every `mcp` import while restoring cached modules."""
    cached = {
        name: module
        for name, module in sys.modules.items()
        if name == "mcp" or name.startswith("mcp.")
    }
    for name in cached:
        del sys.modules[name]
    finder = _BlockMCPImports()
    sys.meta_path.insert(0, finder)
    try:
        yield
    finally:
        sys.meta_path.remove(finder)
        sys.modules.update(cached)


@pytest.fixture
def make_consumer() -> ConsumerFactory:
    """Factory for the ubiquitous test `Consumer`; every field overridable."""

    def _make(
        *,
        actor_id: str = "u1",
        role: str = "Member",
        scope_level: str = "org",
        scope_id: str = "org-1",
        kind: ConsumerKind = "human",
        principal: str | None = None,
    ) -> Consumer:
        if principal is not None:
            return Consumer(
                actor_id=actor_id,
                role=role,
                scope_level=scope_level,
                scope_id=scope_id,
                kind=kind,
                principal=principal,
            )
        return Consumer(
            actor_id=actor_id, role=role, scope_level=scope_level, scope_id=scope_id, kind=kind
        )

    return _make


@pytest.fixture
def make_policy() -> PolicyFactory:
    """Factory for a `ScopePolicy`; every field is overridable."""

    def _make(
        *,
        levels: list[str] | None = None,
        unscoped_types: set[str] | None = None,
        rules: dict[str, list[ScopeRule]] | None = None,
        contributor_rules: dict[str, list[ScopeRule]] | None = None,
        row_visibility: dict[str, RowVisibilityFn] | None = None,
        min_n: int = 3,
    ) -> ScopePolicy:
        return ScopePolicy(
            levels=["org"] if levels is None else levels,
            unscoped_types=set() if unscoped_types is None else unscoped_types,
            rules={} if rules is None else rules,
            contributor_rules={} if contributor_rules is None else contributor_rules,
            row_visibility={} if row_visibility is None else row_visibility,
            min_n=min_n,
        )

    return _make


@pytest.fixture
def make_ontology() -> OntologyFactory:
    """Factory for an authoring `Ontology`; every constructor field is overridable."""

    def _make(
        *,
        name: str = "test",
        scope_levels: list[str] | None = None,
        min_n: int = 3,
    ) -> Ontology:
        return Ontology(
            name=name,
            scope_levels=["org"] if scope_levels is None else scope_levels,
            min_n=min_n,
        )

    return _make


@pytest.fixture
def make_object_type() -> ObjectTypeFactory:
    """Factory for the standard minimal `ObjectTypeDef`: str-`id` primary key,
    plus any extra properties passed as ``(name, type)`` pairs."""

    def _make(
        api_name: str,
        *extra_props: tuple[str, PropertyType],
        description: str | None = None,
        layer: str = "L0",
    ) -> ObjectTypeDef:
        props = [PropertyDef(name="id", type="str")]
        props.extend(PropertyDef(name=name, type=type_) for name, type_ in extra_props)
        return ObjectTypeDef(
            api_name=api_name,
            display_name=api_name,
            description=description or f"A {api_name}",
            layer=layer,
            properties=props,
            primary_key="id",
        )

    return _make


@pytest.fixture
def make_registry(make_object_type: ObjectTypeFactory) -> RegistryFactory:
    """Factory for standard object types plus any registered descriptors."""

    def _make(
        *api_names: str,
        object_types: Iterable[ObjectTypeDef] = (),
        link_types: Iterable[LinkTypeDef] = (),
        action_types: Iterable[ActionTypeDef] = (),
        functions: Iterable[FunctionDef] = (),
        capabilities: Iterable[CapabilityDef] = (),
    ) -> OntologyRegistry:
        registry = OntologyRegistry()
        for api_name in api_names:
            registry.register_object_type(make_object_type(api_name))
        for object_type in object_types:
            registry.register_object_type(object_type)
        for link_type in link_types:
            registry.register_link_type(link_type)
        for action_type in action_types:
            registry.register_action_type(action_type)
        for function in functions:
            registry.register_function(function)
        for capability in capabilities:
            registry.register_capability(capability)
        return registry

    return _make


@pytest.fixture
def make_store() -> StoreFactory:
    """Factory for a fresh SQLite store, or a Postgres store when requested."""

    def _make(
        registry: OntologyRegistry,
        path: str = ":memory:",
        *,
        dsn: str | None = None,
        backend: Literal["sqlite", "postgres"] = "sqlite",
        tenant: str = DEFAULT_TENANT,
        busy_timeout: float = 5.0,
        rls: bool = True,
    ) -> Store:
        store_path = path if dsn is None else dsn
        if backend == "postgres":
            from ontary.store.postgres import PostgresStore

            return PostgresStore(
                registry,
                store_path,
                tenant=tenant,
                rls=rls,
            )
        return ObjectStore(
            registry,
            store_path,
            tenant=tenant,
            busy_timeout=busy_timeout,
        )

    return _make


@pytest.fixture
def make_call() -> CallFactory:
    """Factory for an in-process MCP tool call with decoded JSON output."""

    def _make(server: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = asyncio.run(server.call_tool(name, arguments))
        if result.structured_content is not None:
            return cast(dict[str, Any], result.structured_content)
        payload: dict[str, Any] = json.loads(result.content[0].text)
        return payload

    return _make
