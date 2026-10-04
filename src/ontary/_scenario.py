"""Eager scenario helpers, exported through :mod:`ontary.testing`."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar

from ontary._typed_api import api_name_for, declared_snapshot
from ontary.authoring import Ontology
from ontary.client import OntologyRuntime
from ontary.errors import ValidationFailed
from ontary.model import ActionParams, CapabilityHandle, LinkHandle, OntologyObject, hydrate
from ontary.security import Consumer
from ontary.store import Source, Store

SCENARIO_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)
_F = TypeVar("_F", bound=OntologyObject)
_T = TypeVar("_T", bound=OntologyObject)
_Snapshot = tuple[dict[tuple[str, str], dict[str, Any]], set[tuple[str, str, str]]]


@dataclass
class Ok:
    result: Any


@dataclass
class Err:
    code: str
    exc: Exception
    checked: bool = False


class Scenario:
    """An eagerly seeded and executed, deterministic action scenario."""

    def __init__(self, ontology: Ontology, store: Store, runtime: OntologyRuntime) -> None:
        self._ontology = ontology
        self._store = store
        self._runtime = runtime
        self._step = 0
        self._action = ""
        self._role = ""
        self._outcome: Ok | Err | None = None
        self._before: _Snapshot | None = None

    def _require_setup(self) -> None:
        if self._step:
            raise AssertionError("given: setup must come before the first when")

    def _object_id(self, obj: OntologyObject) -> str:
        api_name = api_name_for(type(obj), self._ontology.registry, owner="scenario")
        primary_key = self._ontology.registry.get_object_type(api_name).primary_key
        return str(getattr(obj, primary_key))

    def _endpoint_id(self, endpoint: OntologyObject | str) -> str:
        return self._object_id(endpoint) if isinstance(endpoint, OntologyObject) else endpoint

    def given(self, *objects: OntologyObject) -> Scenario:
        """Seed declared properties of typed objects before any action."""
        self._require_setup()
        for obj in objects:
            obj_id = str(getattr(obj, "id", "<unknown>"))
            try:
                api_name = api_name_for(type(obj), self._ontology.registry, owner="scenario")
                obj_id = self._object_id(obj)
                self._store.insert(
                    api_name,
                    declared_snapshot(obj, api_name, self._ontology.registry),
                    Source(source_system="ontary.testing"),
                )
            except Exception as exc:
                code = getattr(exc, "code", None)
                if not isinstance(code, str):
                    raise
                raise AssertionError(
                    f"given: {type(obj).__name__} {obj_id!r}: {code} {exc}"
                ) from exc
        return self

    def given_link(self, handle: LinkHandle[_F, _T], from_: _F | str, to: _T | str) -> Scenario:
        """Seed a typed link with object or string endpoints."""
        self._require_setup()
        from_id = str(getattr(from_, "id", from_))
        to_id = str(getattr(to, "id", to))
        try:
            api_name_for(handle.from_cls, self._ontology.registry, owner="scenario")
            api_name_for(handle.to_cls, self._ontology.registry, owner="scenario")
            self._ontology.registry.get_link_type(handle.api_name)
            from_id = self._endpoint_id(from_)
            to_id = self._endpoint_id(to)
            self._store.create_link(handle.api_name, from_id, to_id)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                raise
            raise AssertionError(
                f"given: {handle.api_name} {from_id!r} -> {to_id!r}: {code} {exc}"
            ) from exc
        return self

    def _snapshot(self) -> _Snapshot:
        """Detached current object payloads and links, excluding audit history."""
        rows = {
            api_name: self._store.read_all(api_name)
            for api_name in self._ontology.registry.object_types
        }
        objects = {
            (api_name, row.lineage.object_id): deepcopy(row.payload)
            for api_name, current in rows.items()
            for row in current
        }
        links = {
            (link.api_name, row.lineage.object_id, to_id)
            for link in self._ontology.registry.link_types.values()
            for row in rows[link.from_type]
            for to_id in self._store.links_from(link.api_name, row.lineage.object_id)
        }
        return objects, links

    def _step_description(self) -> str:
        return f"step {self._step} {self._action} by {self._role}"

    def when(self, params: ActionParams, *, by: Consumer) -> Scenario:
        """Execute one action through the real consumer-governed runtime."""
        if isinstance(self._outcome, Err) and not self._outcome.checked:
            raise AssertionError(
                f"{self._step_description()} failed with {self._outcome.code}"
            ) from self._outcome.exc
        self._before = self._snapshot()
        self._step += 1
        self._action = type(params).__name__
        self._role = by.role
        self._outcome = None
        try:
            result = self._runtime.for_consumer(by).execute(params)
        except Exception as exc:
            code = getattr(exc, "code", None)
            if not isinstance(code, str):
                raise
            self._outcome = Err(code, exc)
        else:
            self._outcome = Ok(result)
        return self

    def then(self, cls: type[_F], pk: str, **fields: Any) -> Scenario:
        """Check named properties against the unredacted current stored row."""
        api_name = api_name_for(cls, self._ontology.registry, owner="scenario")
        declared = {
            prop.name for prop in self._ontology.registry.get_object_type(api_name).properties
        }
        unknown = sorted(set(fields) - declared)
        if unknown:
            names = ", ".join(repr(name) for name in unknown)
            declared_names = ", ".join(sorted(declared))
            raise ValidationFailed(
                f"Scenario.then({cls.__name__}): unknown propert"
                f"{'y' if len(unknown) == 1 else 'ies'} {names}; "
                f"declared: {declared_names}",
                code="INVALID_RECORD",
            )

        if isinstance(self._outcome, Err):
            raise AssertionError(
                f"then: {self._step_description()} failed with {self._outcome.code}"
            ) from self._outcome.exc

        stored = self._store.read_current(api_name, pk)
        if stored is None:
            raise AssertionError(f"then: no current {cls.__name__} {pk!r}")
        actual_object = hydrate(cls, stored, "human")
        differences = [
            f"{name}: expected {expected!r}, got {getattr(actual_object, name)!r}"
            for name, expected in fields.items()
            if getattr(actual_object, name) != expected
        ]
        if differences:
            if self._step:
                state = (
                    f"after step {self._step} "
                    f"({self._action} by {self._role})"
                )
            else:
                state = "in seeded state"
            raise AssertionError(
                f"then: {cls.__name__} {pk!r} does not match {state}:\n  "
                + "\n  ".join(differences)
            )
        return self

    def then_absent(self, cls: type[_F], pk: str) -> Scenario:
        """Check that an object has no current row, regardless of outcome."""
        api_name = api_name_for(cls, self._ontology.registry, owner="scenario")
        if self._store.read_current(api_name, pk) is not None:
            raise AssertionError(f"then_absent: current {cls.__name__} {pk!r} exists")
        return self

    def then_link(
        self, handle: LinkHandle[_F, _T], from_: _F | str, to: _T | str
    ) -> Scenario:
        """Check that a typed link is present in current store state."""
        api_name_for(handle.from_cls, self._ontology.registry, owner="scenario")
        api_name_for(handle.to_cls, self._ontology.registry, owner="scenario")
        self._ontology.registry.get_link_type(handle.api_name)
        from_id = self._endpoint_id(from_)
        to_id = self._endpoint_id(to)
        if to_id not in self._store.links_from(handle.api_name, from_id):
            raise AssertionError(
                f"then_link: expected {handle.api_name} link "
                f"{from_id!r} -> {to_id!r} to exist"
            )
        return self

    def then_no_link(
        self, handle: LinkHandle[_F, _T], from_: _F | str, to: _T | str
    ) -> Scenario:
        """Check that a typed link is absent from current store state."""
        api_name_for(handle.from_cls, self._ontology.registry, owner="scenario")
        api_name_for(handle.to_cls, self._ontology.registry, owner="scenario")
        self._ontology.registry.get_link_type(handle.api_name)
        from_id = self._endpoint_id(from_)
        to_id = self._endpoint_id(to)
        if to_id in self._store.links_from(handle.api_name, from_id):
            raise AssertionError(
                f"then_no_link: unexpected {handle.api_name} link "
                f"{from_id!r} -> {to_id!r}"
            )
        return self

    def then_result(self, expected: Any) -> Scenario:
        """Check the last successful action's return value by equality."""
        if isinstance(self._outcome, Err):
            raise AssertionError(
                f"then_result: {self._step_description()} failed with {self._outcome.code}"
            ) from self._outcome.exc
        if not isinstance(self._outcome, Ok):
            raise AssertionError("then_result: no successful when to check")
        if self._outcome.result != expected:
            raise AssertionError(
                f"then_result: {self._step_description()}: "
                f"expected {expected!r}, got {self._outcome.result!r}"
            )
        return self

    def then_error(self, code: str) -> Scenario:
        """Check the last error code and prove current state did not change."""
        actual = self._outcome.code if isinstance(self._outcome, Err) else "no error"
        if not isinstance(self._outcome, Err) or actual != code:
            raise AssertionError(f"then_error: expected {code!r}, got {actual}")
        current = self._snapshot()
        if current != self._before:
            assert self._before is not None
            before_objects, before_links = self._before
            objects, links = current
            changes = [
                f"object {api_name} {obj_id!r}: "
                f"before {before_objects.get((api_name, obj_id))!r}, "
                f"after {objects.get((api_name, obj_id))!r}"
                for api_name, obj_id in sorted(before_objects.keys() | objects.keys())
                if before_objects.get((api_name, obj_id)) != objects.get((api_name, obj_id))
            ]
            changes.extend(f"link removed {link!r}" for link in sorted(before_links - links))
            changes.extend(f"link added {link!r}" for link in sorted(links - before_links))
            raise AssertionError(
                f"then_error: state changed after {self._step_description()}: "
                + "; ".join(changes)
            )
        self._outcome.checked = True
        return self


def scenario(
    ontology: Ontology,
    *,
    store: Store | None = None,
    clock: Callable[[], datetime] | None = None,
    id_factory: Callable[[], str] | None = None,
    capabilities: Mapping[CapabilityHandle[Any], object] | None = None,
) -> Scenario:
    """Bind the deterministic environment once, before any seed writes."""
    from ontary.testing import FixedClock, SequentialIds, make_store

    actual_store = make_store(ontology) if store is None else store
    runtime = ontology.bind(
        actual_store,
        clock=FixedClock(SCENARIO_EPOCH) if clock is None else clock,
        id_factory=SequentialIds("id") if id_factory is None else id_factory,
        capabilities=capabilities,
    )
    return Scenario(ontology, actual_store, runtime)
