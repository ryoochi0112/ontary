"""Shared typed-API helpers (C3 of the staged refactor).

The fail-closed resolution/validation logic that `OntologyClient`,
`BoundQuery`, and `ActionContext` each used to carry as verbatim (or
near-verbatim) private copies. One implementation each; the only
caller-specific variation is the OWNER/SUBJECT word in the message
("client"/"function", "action"/"function"), parametrized -- every other
byte of every message is preserved from the copies these replace.

The read mixin and read helpers below are consumed by both typed façades;
nothing here is exported from the `ontary` front door.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, TypeVar, overload

from ontary.audit import CapabilityAccessRecord
from ontary.errors import InternalError, PreconditionFailed, ValidationFailed
from ontary.meta import OntologyRegistry
from ontary.model import (
    CapabilityHandle,
    LinkHandle,
    OntologyObject,
    _class_stamp,
    hydrate,
)
from ontary.query import GuardedQuery, Page, TypedPage
from ontary.security import Consumer
from ontary.store import StoredObject

T = TypeVar("T", bound=OntologyObject)
F = TypeVar("F", bound=OntologyObject)


def api_name_for(
    cls: type[OntologyObject], registry: OntologyRegistry | None, *, owner: str
) -> str:
    """Resolve a decorated `OntologyObject` subclass to its registered
    `api_name`, fail-closed with `ValidationFailed` code `UNKNOWN_NAME` for an undecorated class, one
    stamped by a different `Ontology`, or an api_name not on `registry`.
    Reads both stamps via `_class_stamp` (`cls.__dict__`, never plain
    attribute access -- see that helper's docstring for the
    undecorated-subclass hazard). `registry=None` only ever occurs for a
    directly-constructed `BoundQuery`; `owner` is `"client"` or
    `"function"` and appears verbatim in the messages."""
    name, cls_registry = _class_stamp(cls)
    if name is None or cls_registry is None:
        raise ValidationFailed(
            f"{cls.__name__!r} is not decorated with "
            "@ontology.object(...) -- it has no registered api_name",
            code="UNKNOWN_NAME",
        )
    if registry is None:
        raise ValidationFailed(
            f"{cls.__name__!r} (api_name {name!r}): no registry bound "
            "to this BoundQuery -- construct it via "
            "OntologyClient.call_function (or pass a registry "
            "explicitly) to resolve typed classes/LinkHandles",
            code="UNKNOWN_NAME",
        )
    if cls_registry is not registry:
        raise ValidationFailed(
            f"{cls.__name__!r} (api_name {name!r}) was registered on a "
            f"different Ontology -- it is not part of this {owner}'s "
            "ontology",
            code="UNKNOWN_NAME",
        )
    try:
        registry.get_object_type(name)
    except ValidationFailed as exc:
        raise ValidationFailed(
            f"{cls.__name__!r} (api_name {name!r}) is not registered "
            f"on this {owner}'s ontology",
            code="UNKNOWN_NAME",
        ) from exc
    return name


def validate_where_keys(
    cls: type[OntologyObject], where: dict[str, Any] | None
) -> None:
    """Existence-only check of `where=` keys against `cls.model_fields`
    for a TYPED call (spec AC7) -- unknown key -> `ValidationFailed` code
    `UNKNOWN_FIELD`. A
    hidden-but-declared key passes this check unchanged and still hits
    `GuardedQuery`'s `VISIBILITY_DENIED` visibility gate downstream."""
    if not where:
        return
    unknown = set(where) - set(cls.model_fields)
    if unknown:
        raise ValidationFailed(
            f"{cls.__name__}: where= names unknown field(s) "
            f"{sorted(unknown)!r}",
            code="UNKNOWN_FIELD",
        )


@overload
def list_objects(
    query: GuardedQuery,
    consumer: Consumer,
    obj_type: type[T] | str,
    where: dict[str, Any] | None,
    *,
    api_name_for: Callable[[type[OntologyObject]], str],
    validate_where_keys: Callable[
        [type[OntologyObject] | str, dict[str, Any] | None], None
    ],
    limit: None,
    after: str | None,
) -> list[T] | list[StoredObject]: ...


@overload
def list_objects(
    query: GuardedQuery,
    consumer: Consumer,
    obj_type: type[T] | str,
    where: dict[str, Any] | None,
    *,
    api_name_for: Callable[[type[OntologyObject]], str],
    validate_where_keys: Callable[
        [type[OntologyObject] | str, dict[str, Any] | None], None
    ],
    limit: int,
    after: str | None,
) -> TypedPage[T] | Page: ...


@overload
def list_objects(
    query: GuardedQuery,
    consumer: Consumer,
    obj_type: type[T] | str,
    where: dict[str, Any] | None,
    *,
    api_name_for: Callable[[type[OntologyObject]], str],
    validate_where_keys: Callable[
        [type[OntologyObject] | str, dict[str, Any] | None], None
    ],
    limit: int | None,
    after: str | None,
) -> list[T] | list[StoredObject] | TypedPage[T] | Page: ...


def list_objects(
    query: GuardedQuery,
    consumer: Consumer,
    obj_type: type[T] | str,
    where: dict[str, Any] | None,
    *,
    api_name_for: Callable[[type[OntologyObject]], str],
    validate_where_keys: Callable[
        [type[OntologyObject] | str, dict[str, Any] | None], None
    ],
    limit: int | None = None,
    after: str | None = None,
) -> list[T] | list[StoredObject] | TypedPage[T] | Page:
    """Shared list implementation for the client and a `BoundQuery`.

    `limit=None` keeps the existing list-every-row behavior, including passing
    `after` through to `GuardedQuery`. A positive `limit` preserves the
    client's page-filling behavior and hydrates typed page items. Callers that
    expose a non-paginated surface pass `limit=None` and do not expose the
    pagination keywords themselves.
    """
    if isinstance(obj_type, str):
        validate_where_keys(obj_type, where)
        if limit is None:
            return query.get_objects(consumer, obj_type, where, after=after)
        return query.get_objects(consumer, obj_type, where, limit=limit, after=after)

    validate_where_keys(obj_type, where)
    api_name = api_name_for(obj_type)
    if limit is None:
        stored_list = query.get_objects(consumer, api_name, where, after=after)
        return [hydrate(obj_type, so, consumer.kind) for so in stored_list]
    page = query.get_objects(consumer, api_name, where, limit=limit, after=after)
    items = [hydrate(obj_type, so, consumer.kind) for so in page.items]
    return TypedPage(items=items, next_cursor=page.next_cursor)


def traverse_via(
    query: GuardedQuery,
    consumer: Consumer,
    registry: OntologyRegistry | None,
    from_id: str,
    via: LinkHandle[F, T],
    *,
    api_name_for: Callable[[type[OntologyObject]], str],
    owner: str,
) -> list[T]:
    """Shared typed `via=` traversal branch for both typed façades."""
    api_name_for(via.from_cls)
    api_name_for(via.to_cls)
    # `api_name_for` above fails closed when a directly-constructed
    # `BoundQuery` has no registry. Keep the invariant explicit so optimized
    # Python cannot remove it.
    if registry is None:
        raise InternalError(
            f"typed traversal on {owner} reached link lookup without a registry",
            code="INTERNAL_ERROR",
        )
    try:
        registry.get_link_type(via.api_name)
    except ValidationFailed as exc:
        raise ValidationFailed(
            f"link {via.api_name!r} is not registered on this "
            f"{owner}'s ontology",
            code="UNKNOWN_NAME",
        ) from exc
    stored_list = query.traverse(consumer, via.api_name, from_id)
    return [hydrate(via.to_cls, so, consumer.kind) for so in stored_list]


class _TypedReadMixin:
    """Shared typed-read façade operations.

    `OntologyClient` and `BoundQuery` both bind `_query`, `_consumer`, and
    `_registry`; the only resolution-message variation is `_typed_owner`.
    The inherited methods keep overloads and the fail-closed class-resolution
    discipline in one implementation while each façade retains its own
    string-form traversal and pagination surface.
    """

    _query: GuardedQuery
    _consumer: Consumer
    _registry: OntologyRegistry | None
    _typed_owner: str

    def _api_name_for(self, cls: type[OntologyObject]) -> str:
        """Resolves a decorated `OntologyObject` subclass to its registered
        `api_name` (stamped on the class by `Ontology.object()` -- spec §6)
        -- `ValidationFailed` code `UNKNOWN_NAME` (same code the string path's
        lookups carry) for an
        undecorated class, OR one registered on a different `Ontology`.

        Reads BOTH stamps via `cls.__dict__.get(...)`, never plain
        attribute access: a normal lookup would fall through to a base
        class's ClassVar on an undecorated SUBCLASS (`class Sub(Ticket)`
        inherits `_ontary_api_name`/`_ontary_registry` without being
        decorated itself), silently resolving/hydrating `Sub` as if it
        were `Ticket`. The api_name string alone also isn't enough --
        two separate `Ontology(...)` instances can independently register
        the same api_name with unrelated classes -- so this additionally
        requires the stamped registry to be THIS façade's registry
        object, by identity, not just a name-string match.

        Uses the shared `_class_stamp` helper for the actual `__dict__`
        read -- the stamp-reading discipline therefore lives in exactly
        one place for both `OntologyClient` and `BoundQuery`.
        """
        return api_name_for(cls, self._registry, owner=self._typed_owner)

    def _validate_where_keys(
        self,
        cls: type[OntologyObject] | str,
        where: dict[str, Any] | None,
    ) -> None:
        """Existence-only check of `where=` keys for both read forms.

        Typed calls use the author's model fields byte-for-byte as before;
        string calls use the registered object descriptor, which is the same
        declared payload schema without requiring a Python class.
        """
        if isinstance(cls, str):
            self._query._validate_where_keys(cls, where)
        else:
            validate_where_keys(cls, where)

    def _validate_field_name(
        self, cls: type[OntologyObject], field_name: str, what: str
    ) -> None:
        """Existence-only check of a single field name (`value_field`/
        `group_by`) against `cls.model_fields` for a TYPED `aggregate`
        call (spec AC7) -- unknown name -> `ValidationFailed` code
        `UNKNOWN_FIELD`. A hidden-but-
        declared field passes this check unchanged and still hits
        `GuardedQuery`'s own gates (`VISIBILITY_DENIED`/`MIN_N_VIOLATION`)
        downstream."""
        if field_name not in cls.model_fields:
            raise ValidationFailed(
                f"{cls.__name__}: {what} names unknown field {field_name!r}",
                code="UNKNOWN_FIELD",
            )

    def _traverse_via(self, from_id: str, via: LinkHandle[F, T]) -> list[T]:
        return traverse_via(
            self._query,
            self._consumer,
            self._registry,
            from_id,
            via,
            api_name_for=self._api_name_for,
            owner=self._typed_owner,
        )

    @overload
    def get(self, obj_type: type[T], obj_id: str) -> T | None: ...
    @overload
    def get(self, obj_type: str, obj_id: str) -> StoredObject | None: ...
    def get(self, obj_type: type[T] | str, obj_id: str) -> T | StoredObject | None:
        if isinstance(obj_type, str):
            return self._query.get_object(self._consumer, obj_type, obj_id)
        api_name = self._api_name_for(obj_type)
        stored = self._query.get_object(self._consumer, api_name, obj_id)
        if stored is None:
            return None
        return hydrate(obj_type, stored, self._consumer.kind)

    @overload
    def aggregate(
        self,
        obj_type: type[T],
        value_field: str,
        where: dict[str, Any] | None = None,
    ) -> float: ...
    @overload
    def aggregate(
        self,
        obj_type: str,
        value_field: str,
        where: dict[str, Any] | None = None,
    ) -> float: ...
    def aggregate(
        self,
        obj_type: type[T] | str,
        value_field: str,
        where: dict[str, Any] | None = None,
    ) -> float:
        """Ungrouped aggregate (spec pagination-hardening AC9): the mean of
        `value_field` over every visible row. Both forms validate `where=`
        keys before delegating; the typed overload additionally validates
        `value_field` against `cls.model_fields` (`UNKNOWN_FIELD`)."""
        if isinstance(obj_type, str):
            self._validate_where_keys(obj_type, where)
            return self._query.aggregate(self._consumer, obj_type, value_field, where)
        api_name = self._api_name_for(obj_type)
        self._validate_field_name(obj_type, value_field, "value_field")
        self._validate_where_keys(obj_type, where)
        return self._query.aggregate(self._consumer, api_name, value_field, where)

    @overload
    def aggregate_by(
        self,
        obj_type: type[T],
        value_field: str,
        group_by: str,
        where: dict[str, Any] | None = None,
    ) -> dict[str, float]: ...
    @overload
    def aggregate_by(
        self,
        obj_type: str,
        value_field: str,
        group_by: str,
        where: dict[str, Any] | None = None,
    ) -> dict[str, float]: ...
    def aggregate_by(
        self,
        obj_type: type[T] | str,
        value_field: str,
        group_by: str,
        where: dict[str, Any] | None = None,
    ) -> dict[str, float]:
        """Grouped aggregate (spec pagination-hardening AC9): one mean per
        distinct `group_by` value. Both forms validate `where=` keys before
        delegating; the typed overload additionally validates
        `value_field`/`group_by` against `cls.model_fields` (`UNKNOWN_FIELD`)."""
        if isinstance(obj_type, str):
            self._validate_where_keys(obj_type, where)
            return self._query.aggregate_by(
                self._consumer, obj_type, value_field, group_by, where
            )
        api_name = self._api_name_for(obj_type)
        self._validate_field_name(obj_type, value_field, "value_field")
        self._validate_where_keys(obj_type, where)
        self._validate_field_name(obj_type, group_by, "group_by")
        return self._query.aggregate_by(
            self._consumer, api_name, value_field, group_by, where
        )

    @overload
    def count_contributors(
        self,
        obj_type: type[T],
        where: dict[str, Any] | None = None,
    ) -> int: ...
    @overload
    def count_contributors(
        self,
        obj_type: str,
        where: dict[str, Any] | None = None,
    ) -> int: ...
    def count_contributors(
        self,
        obj_type: type[T] | str,
        where: dict[str, Any] | None = None,
    ) -> int:
        """Distinct contributors backing this selection -- the count that
        makes an aggregate legible as safe, with no identity field readable.
        Both forms validate `where=` keys before delegating; the typed
        overload uses `cls.model_fields` (`UNKNOWN_FIELD`).

        Refuses with `VisibilityError` code `MIN_N_VIOLATION` for a selection whose aggregate would
        also be withheld -- see `GuardedQuery.count_contributors` for why
        that gate is the point of the method rather than a precaution.
        """
        if isinstance(obj_type, str):
            self._validate_where_keys(obj_type, where)
            return self._query.count_contributors(self._consumer, obj_type, where)
        api_name = self._api_name_for(obj_type)
        self._validate_where_keys(obj_type, where)
        return self._query.count_contributors(self._consumer, api_name, where)


def resolve_capability(
    handle: CapabilityHandle[Any],
    *,
    registry: OntologyRegistry | None,
    declared: frozenset[str],
    providers: Mapping[CapabilityHandle[Any], object],
    accesses: list[CapabilityAccessRecord] | None,
    subject: str,
) -> object:
    """The shared four-step fail-closed capability accessor (spec
    `governed-effects` AC6/AC8) behind `ActionContext.capability` and
    `BoundQuery.capability`: foreign registry -> `ValidationFailed` code
    `UNKNOWN_NAME`; undeclared -> `ValidationFailed` code
    `UNDECLARED_CAPABILITY` (even when a provider happens to be bound --
    declaration is the gate, not availability); declared-but-unbound ->
    `PreconditionFailed` code `CAPABILITY_NOT_PROVIDED`; then record the access
    on `accesses` (when a
    list is carried -- `count` counts provider RETRIEVALS, not calls made
    on the returned object). `subject` is `"action"` or `"function"` and
    appears verbatim in the undeclared message. The provider is returned
    unwrapped; callers keep their own typed `cast`."""
    if registry is None or handle.registry is not registry:
        raise ValidationFailed(
            f"capability {handle.api_name!r} is registered on a different "
            "Ontology",
            code="UNKNOWN_NAME",
        )
    if handle.api_name not in declared:
        raise ValidationFailed(
            f"{subject} did not declare capability {handle.api_name!r}",
            code="UNDECLARED_CAPABILITY",
        )
    try:
        provider = providers[handle]
    except KeyError as exc:
        raise PreconditionFailed(
            f"no provider bound for capability {handle.api_name!r}",
            code="CAPABILITY_NOT_PROVIDED",
        ) from exc
    if accesses is not None:
        for record in accesses:
            if record.api_name == handle.api_name:
                record.count += 1
                break
        else:
            accesses.append(
                CapabilityAccessRecord(api_name=handle.api_name, count=1)
            )
    return provider
