"""Error-code foundation shared by every engine exception.

`OntaryError` gives an exception a stable, machine-readable `code: str` and a
`kind: Kind`. As of 0.6.0 `code` is a REQUIRED keyword at construction and
there is no class-attribute default: a default meant a construction reached
through a rebound name silently shipped it in place of the code the author
wrote. Subclasses still declare a `kind` class default, which a registered
code overrides per instance. `ERROR_CODES` is the single source of truth other tests (and the
README's error-code table) pin against -- every code an exception class
carries must be registered here, and every registered code must (eventually)
be carried by some exception class, unless explicitly marked
reserved-for-later.

Convention note: registry getters participate in this taxonomy directly:
an unknown public lookup raises `ValidationFailed` with its catalogued code
rather than leaking a bare `KeyError`. The backends' constructors still
raise `ValueError` for an empty tenant.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Kind = Literal[
    "visibility",
    "permission",
    "precondition",
    "validation",
    "authority",
    "conflict",
    "internal",
]


class OntaryError(Exception):
    """Mixin/base giving an exception a stable `code` and `kind`.

    `code` is REQUIRED at construction and there is no class-attribute
    fallback, which is the 0.6.0 change. A default meant that any
    construction reached through a rebound name -- an alias, a parameter, a
    factory -- silently shipped the class default in place of the code the
    author meant, and no source-level guard closes that in a language where
    any name can be rebound (five review rounds established this
    empirically). Requiring the argument makes the wrong construction
    unwriteable instead of undetectable.

    Subclasses still set `kind` as a class attribute, the taxonomy default
    for that exception type; a registered `code` overrides it per instance.
    """

    code: str
    kind: Kind = "internal"

    def __init__(self, message: str = "", *, code: str) -> None:
        super().__init__(message)
        if not code:
            raise ValueError(
                f"{type(self).__name__} requires a non-empty `code`: it is the "
                "stable wire surface consumers branch on"
            )
        self.code = code
        # A registered engine code carries its own catalogued `kind`
        # (e.g. `ActionError(code="UNKNOWN_ACTION")` is kind `validation`,
        # not the class's default `precondition`) so a caller distinguishes
        # taxonomy from `.kind` alone, never the message text. An
        # unregistered (author-supplied) code leaves `kind` at the class
        # default -- there is no catalog entry to consult.
        info = ERROR_CODES.get(code)
        if info is not None:
            self.kind = info.kind

    def __reduce__(self) -> tuple[object, ...]:
        """Round-trip `code`/`kind` through `pickle`, `copy` and `deepcopy`.

        `BaseException.__reduce__` rebuilds via `cls(*self.args)`, which
        stopped working the moment `code` became required -- so a coded
        refusal crossing a process boundary would die on unpickling and the
        caller would see a broken worker instead of the refusal. A coded
        refusal really does cross process boundaries (multi-process serving),
        so this is a real path, not a theoretical one.
        """
        return (
            _rebuild_error,
            (type(self), tuple(str(arg) for arg in self.args), self.code, self.kind),
        )


def _rebuild_error(
    cls: type[OntaryError], args: tuple[str, ...], code: str, kind: Kind
) -> OntaryError:
    """Reconstruct a coded exception for `pickle` / `copy`.

    Separate from `__reduce__` because pickle must be able to import the
    callable by name.

    `kind` is carried rather than re-derived for exactly one reason: a
    post-construction assignment. Re-deriving cannot lose anything else,
    because rebuilding runs the SAME constructor with the SAME code and so
    reproduces whatever that constructor did the first time -- the
    catalogued kind for a registered code, the class default for an
    unregistered one. So for any instance whose `kind` was never touched
    after construction this line is a no-op, and only a mutated `kind`
    distinguishes carried from re-derived.
    """
    error = cls(*args, code=code)
    error.kind = kind
    return error


@dataclass(frozen=True)
class ErrorCodeInfo:
    """One catalog entry: the `kind` a code belongs to, and a one-line
    human-readable description."""

    kind: Kind
    description: str


# Single source of truth: every stable code the engine can produce. Later
# tasks register more codes here as they retrofit more exception classes;
# codes not yet carried by any exception class must be commented as
# reserved-for-later at their registration site.
ERROR_CODES: dict[str, ErrorCodeInfo] = {
    "STORE_ERROR": ErrorCodeInfo(
        kind="internal",
        description=(
            "Fallback code for an unclassified store-layer error."
        ),
    ),
    "STORE_BUSY": ErrorCodeInfo(
        kind="conflict",
        description=(
            "A SQLite transaction could not acquire or retain its database "
            "lock within ObjectStore's configured busy timeout; retry after "
            "the competing writer finishes or increase busy_timeout. This is "
            "a conflict, not a precondition: retrying is the remedy, and the "
            "kind travels on the MCP wire so callers can branch on retryability."
        ),
    ),
    "UNKNOWN_OBJECT_TYPE": ErrorCodeInfo(
        kind="validation",
        description="An operation referenced an unregistered object type.",
    ),
    "UNKNOWN_LINK_TYPE": ErrorCodeInfo(
        kind="validation",
        description="An operation referenced an unregistered link type.",
    ),
    "UNKNOWN_EVENT_TYPE": ErrorCodeInfo(
        kind="validation",
        description="An operation referenced an unregistered event type.",
    ),
    "OBJECT_NOT_FOUND": ErrorCodeInfo(
        kind="validation",
        description="An update targeted a non-existent object.",
    ),
    "OBJECT_ALREADY_EXISTS": ErrorCodeInfo(
        kind="conflict",
        description=(
            "An insert used a primary key that already has a live row of the "
            "same object type; update that object instead, or retire it first."
        ),
    ),
    "PRIMARY_KEY_IMMUTABLE": ErrorCodeInfo(
        kind="validation",
        description=(
            "An update tried to change an object's primary key; a primary key "
            "is immutable, so retire the object and insert a new one instead."
        ),
    ),
    "OBJECT_NOT_LOADED": ErrorCodeInfo(
        kind="validation",
        description=(
            "ActionContext.save got an object this action context did not hand "
            "out; load it with ctx.get(...) or ctx.create(...) first, so only "
            "the fields the handler changed are written."
        ),
    ),
    "OBJECT_RETIRE_NOT_FOUND": ErrorCodeInfo(
        kind="validation",
        description="A retirement targeted an object with no stored row.",
    ),
    "OBJECT_ALREADY_RETIRED": ErrorCodeInfo(
        kind="conflict",
        description="A retirement targeted an object whose current row is already closed.",
    ),
    "LINK_NOT_FOUND": ErrorCodeInfo(
        kind="validation",
        description="A link closure found no matching live link.",
    ),
    "LINK_ENDPOINT_NOT_FOUND": ErrorCodeInfo(
        kind="validation",
        description=(
            "A link creation named an endpoint id with no live row of the "
            "link type's declared endpoint type -- missing or retired; a "
            "link needs a live object at both ends."
        ),
    ),
    "CARDINALITY_VIOLATION": ErrorCodeInfo(
        kind="conflict",
        description=(
            "A link creation would violate its LinkTypeDef cardinality."
        ),
    ),
    "INVALID_BATCH": ErrorCodeInfo(
        kind="validation",
        description=(
            "`Store.read_page`'s `batch` was < 1 (SQLite's LIMIT -1 means "
            "unlimited and InMemoryStore's negative slice drops rows -- "
            "both the opposite of a bounded read)."
        ),
    ),
    "INVALID_CURSOR": ErrorCodeInfo(
        kind="validation",
        description=(
            "Raised when `Store.read_page`'s `after_key` is malformed OR simply "
            "unknown. `after_key` is UNTRUSTED input: it reaches the store from "
            "an MCP client via a later page-filling loop, round-tripped from a "
            "previous page's cursor without any guarantee the caller did not "
            "tamper with it. As amended 2026-07-25, it is a "
            "random per-row PAGE TOKEN (`objects.page_token`, uuid4 hex), not a "
            "decimal row id. Resolving token to row id through the unique index "
            "is the ONLY way to turn a cursor into row identity, so every string "
            "never issued for a real row (malformed, tampered, or made up) raises "
            "this same error on both backends. There is no distinct well-formed "
            "but out-of-range case from the old integer design's "
            "`OverflowError`/silent-empty-page divergence. A token issued for a "
            "row since superseded by `update` still resolves because lookup uses "
            "`row_id` independently of `valid_to`, so an in-flight cursor remains "
            "a valid resume point."
        ),
    ),
    "STALE_CURSOR": ErrorCodeInfo(
        kind="validation",
        description=(
            "An ordered walk's or a link traversal's cursor no longer names a "
            "current row this consumer can resume from; restart from the first "
            "page."
        ),
    ),
    "PRECONDITION_FAILED": ErrorCodeInfo(
        kind="precondition",
        description=(
            "An action's precondition failed; the message names it. The "
            "conventional code for `ActionError` (kind precondition); an "
            "author may attach their own stable code instead, e.g. "
            "`raise ActionError(\"...\", code=\"GAP_NOT_ACKNOWLEDGED\")`. It "
            "is also used with overridden codes for unregistered/unhandled "
            "actions (`UNKNOWN_ACTION`) and parameter-validation failures "
            "(`INVALID_PARAMS`) -- see the `code=` overrides at those raise "
            "sites."
        ),
    ),
    "TRANSITION_NOT_ALLOWED": ErrorCodeInfo(
        kind="precondition",
        description=(
            "A governed property changed to a state not allowed by its "
            "declared transition graph; action starts must be initial states."
        ),
    ),
    "RULE_VIOLATED": ErrorCodeInfo(
        kind="validation",
        description=(
            "A declared object rule returned false or raised while checking "
            "the full new row."
        ),
    ),
    "PERMISSION_DENIED": ErrorCodeInfo(
        kind="permission",
        description=(
            "The consumer's role is not permitted to execute the action "
            "(code `PERMISSION_DENIED`). The permission kind also covers "
            "scope refusals under `SCOPE_DENIED`; each raise site supplies "
            "the specific code."
        ),
    ),
    "SCOPE_DENIED": ErrorCodeInfo(
        kind="permission",
        description=(
            "The consumer's scope does not cover the action's declared "
            "target, scope or reference (`ref()`) parameter (code "
            "`SCOPE_DENIED`). Role refusals use "
            "`PERMISSION_DENIED`; both are kind permission and each raise site "
            "supplies the specific code."
        ),
    ),
    "UNAUTHENTICATED": ErrorCodeInfo(
        kind="permission",
        description=(
            "A request carried no verified identity at all -- kind permission. "
            "`build_multi_consumer_mcp_server` raises this for every tool call, "
            "including introspection, that reaches it with no authenticated "
            "`AccessToken`: stdio (which has no auth context) or HTTP with no "
            "`token_verifier` configured. The fix is: configure authentication. "
            "It is deliberately separate from `CONSUMER_UNRESOLVED`: a missing "
            "credential; mapping the principal fixes a missing consumer, so "
            "callers can distinguish the two from `.code` alone."
        ),
    ),
    "CONSUMER_UNRESOLVED": ErrorCodeInfo(
        kind="permission",
        description=(
            "A verified principal existed, but the author's `resolve_consumer` "
            "callback returned no `Consumer` for it -- kind permission. "
            "`build_multi_consumer_mcp_server` raises this when the callback "
            "returns `None` for an otherwise verified `AccessToken`; the fix is: "
            "map this principal. It remains distinct from `UNAUTHENTICATED`, "
            "where no credential was presented at all, so the two failures are "
            "distinguishable from `.code` alone."
        ),
    ),
    "UNKNOWN_ACTION": ErrorCodeInfo(
        kind="validation",
        description=(
            "An action name is unregistered on the OntologyRegistry, or has "
            "no handler bound to it."
        ),
    ),
    "INVALID_PARAMS": ErrorCodeInfo(
        kind="validation",
        description=(
            "A call's parameters failed declared-shape validation: an action's "
            "params, or a read parameter whose SHAPE is wrong -- an `order_by` "
            "that is neither a field name nor a (field, direction) pair, or a "
            "`where=` that is not a mapping of field name to condition. A "
            "parameter naming something that does not exist is `UNKNOWN_FIELD` "
            "instead; this code is about the shape, not the name."
        ),
    ),
    "UNDECLARED_CAPABILITY": ErrorCodeInfo(
        kind="validation",
        description=(
            "A handler requested a capability its action or function did not declare."
        ),
    ),
    "UNDECLARED_EVENT": ErrorCodeInfo(
        kind="validation",
        description="An action emitted an event type it did not declare.",
    ),
    "EVENT_SUBJECT_INVALID": ErrorCodeInfo(
        kind="validation",
        description=(
            "An emitted event's subject could not be resolved to a valid target object."
        ),
    ),
    "CAPABILITY_NOT_PROVIDED": ErrorCodeInfo(
        kind="precondition",
        description="A declared capability had no provider bound for this call.",
    ),
    "MIN_N_VIOLATION": ErrorCodeInfo(
        kind="visibility",
        description=(
            "An aggregate would be computed over fewer than min_n distinct "
            "contributors."
        ),
    ),
    "VISIBILITY_DENIED": ErrorCodeInfo(
        kind="visibility",
        description=(
            "A single-object read/write targeted an object outside the "
            "consumer's scope."
        ),
    ),
    "SCOPE_POLICY_ERROR": ErrorCodeInfo(
        kind="validation",
        description=(
            "A ScopePolicy declaration is unusable: a rule references an "
            "undeclared object type, link type, or scope level; a type "
            "declares an empty contributor rule list; a type is listed "
            "in unscoped_types while also declaring scope rules; or an "
            "action's scope parameter refers to an unscoped type."
        ),
    ),
    "FUNCTION_ERROR": ErrorCodeInfo(
        kind="precondition",
        description=(
            "Registering/calling a Function failed: undeclared api_name, "
            "duplicate registration, or no handler bound."
        ),
    ),
    "RESULT_NOT_JSON": ErrorCodeInfo(
        kind="precondition",
        description=(
            "A Function or Action handler result could not be encoded for the "
            "JSON boundary both surfaces share. A handler may return JSON "
            "scalars, lists, dicts with `str` keys, and `date`/`datetime` "
            "objects, which become ISO 8601 strings in the spelling the store "
            "keeps. The message names the handler and the key path of the "
            "first offending value. Action results must also be dicts."
        ),
    ),
    "ONTOLOGY_INVALID": ErrorCodeInfo(
        kind="validation",
        description=(
            "A declaration was rejected: `validate()` found invalid "
            "cross-references, or an authoring call (`@ontology.object(...)`, "
            "`ontology.link(...)`, `.definition`) refused a kwarg of the wrong "
            "shape: a misspelled `scope`/`cardinality` literal, a rule not "
            "wrapped in a list, a non-callable `row_visibility`, empty "
            "`scope_levels`, or `min_n` below 1."
        ),
    ),
    "AUTHORITY_ERROR": ErrorCodeInfo(
        kind="authority",
        description=(
            "Fallback code for the authority-refusal family "
            "(`AuthorityError`); every concrete refusal a captured write "
            "can trigger carries its own more specific code instead (e.g. "
            "SOURCE_CREATE_REFUSED, UNDECLARED_SOURCE_WRITE). It is the base "
            "for `ObjectStore.capture_action_writes` refusals where a write "
            "inside an action's capture context crosses the source-backed/"
            "ontology-owned line."
        ),
    ),
    "SOURCE_CREATE_REFUSED": ErrorCodeInfo(
        kind="authority",
        description=(
            "A captured `insert` targeted an object type that is not declared "
            "whole-type ontology-owned (`ObjectTypeDef.owned is True`)."
        ),
    ),
    "UNDECLARED_SOURCE_WRITE": ErrorCodeInfo(
        kind="authority",
        description=(
            "A captured `update` touched a property, or a `create_link` "
            "targeted a link type, that is not declared ontology-owned."
        ),
    ),
    "UNDECLARED_SOURCE_REMOVAL": ErrorCodeInfo(
        kind="authority",
        description=(
            "A captured retirement or link closure targeted an object or "
            "link type that is not declared ontology-owned."
        ),
    ),
    "CALLER_TRANSACTION_REFUSED": ErrorCodeInfo(
        kind="conflict",
        description=(
            "Raised when `ActionExecutor.execute()` (or an ingest entry point, a "
            "later task) is called while the caller has already opened a "
            "`store.transaction()` block. "
            "`transaction()` is reentrant, so a caller-owned outer transaction "
            "could roll back an action after the executor reported success and "
            "audited `ok`. The engine must own the transaction/audit boundary and "
            "refuses to nest inside the caller's. Deliberately NOT audited: an "
            "audit row inside the caller's transaction could itself be "
            "rolled back, so the refusal is raised before any audit write."
        ),
    ),
    "OWNED_TYPE_REFUSED": ErrorCodeInfo(
        kind="authority",
        description=(
            "A bulk_upsert/bulk_link record targeted an object or link type "
            "that is declared whole-type ontology-owned; no source may "
            "supply its rows."
        ),
    ),
    "OWNED_PROPERTY_REFUSED": ErrorCodeInfo(
        kind="authority",
        description=(
            "A bulk_upsert record supplied a value for a property declared "
            "ontology-owned on an otherwise source-backed object type."
        ),
    ),
    "INVALID_RECORD": ErrorCodeInfo(
        kind="validation",
        description=(
            "A bulk_upsert record failed declared-shape validation (missing "
            "primary key, missing required property, unknown property, or a "
            "value that does not match its declared type). The validation kind "
            "carries the SAME `INVALID_RECORD` code that `bulk_upsert` already "
            "reports: from a caller's point of view, a record not matching the "
            "declaration is one failure regardless of which write path noticed. "
            "This closes the hole where only ingest checked: "
            "`Store.insert`/`update` and therefore `ActionContext.insert`/`update` "
            "could commit a row missing a required property or carrying a wrong-"
            "typed value, report success, and leave the typed reader unable to "
            "hydrate it. The same code wraps a Pydantic "
            "`ValidationError` while hydrating a stored `OntologyObject` payload "
            "(for example, a non-ISO datetime string), never surfacing a bare "
            "traceback; a stored row failing declared-shape validation on read-"
            "back is the same failure class ingest carries on write. "
            "`Ontology.diagnose(store=...)` reports, per type and property, "
            "the stored rows that would fail hydration under the current "
            "ontology, and `Ontology.validate(store=...)` raises this code for them."
        ),
    ),
    "UNKNOWN_FIELD": ErrorCodeInfo(
        kind="validation",
        description=(
            "A typed `get`/`list` call named a key that is not one of the target "
            "class's declared properties. The "
            "existence-only check runs client-side before the guarded read "
            "layer; a hidden-but-declared key still reaches the visibility kind "
            "unchanged, and the string-form surface keeps its silent-non-match "
            "behavior. The error lives here (previously `ontary.functions`, "
            "which re-exports it)."
        ),
    ),
    "UNKNOWN_NAME": ErrorCodeInfo(
        kind="validation",
        description=(
            "A typed `BoundQuery`/`OntologyClient` call named an unregistered "
            "object, link, action, or function -- e.g. an undecorated class, a "
            "class/`LinkHandle` registered on a different `Ontology`, or a link "
            "api_name absent from this registry. Typed lookup failures use the "
            "validation kind and live here so `ontary._typed_api` "
            "can raise them below the runtime modules."
        ),
    ),
    "UNKNOWN_OPERATOR": ErrorCodeInfo(
        kind="validation",
        description=(
            "A mapping-form `where` clause named an operator outside the "
            "declared set: `gt`, `gte`, `lt`, `lte`, `in`, `ne`, or `contains` "
            "-- or was an empty mapping. A mapping with several operators is "
            "validated key by key, so one unknown key refuses the whole clause."
        ),
    ),
    "OPERATOR_TYPE_MISMATCH": ErrorCodeInfo(
        kind="validation",
        description=(
            "A mapping-form `where` operator is not valid for the property's "
            "declared type (comparisons need `int`, `float`, `date`, or "
            "`datetime`; `contains` needs `str`), or its operand is not a "
            "declared-type scalar."
        ),
    ),
    "NON_NUMERIC_AGGREGATE": ErrorCodeInfo(
        kind="validation",
        description=(
            "`GuardedQuery.aggregate`'s `value_field` is declared a "
            "non-numeric `PropertyType` (anything other than `int`/`float`, "
            "such as str/json/datetime/bool). It is checked against the "
            "declared type before rows are iterated or coerced, so values that "
            'merely look numeric cannot bypass the type contract. `func="count"` '
            "is exempt and accepts any declared type."
        ),
    ),
    "PAGE_NOT_ITERABLE": ErrorCodeInfo(
        kind="validation",
        description=(
            "A `Page`/`TypedPage` was iterated, indexed or measured directly "
            "instead of through `.items`. Both are pydantic models, so the "
            "inherited `BaseModel.__iter__` would otherwise yield "
            "`(field_name, value)` pairs -- `for row in page` hands back "
            "`('items', [...])` and `('next_cursor', ...)`, and the failure "
            "surfaces later as `AttributeError: 'tuple' object has no "
            "attribute 'payload'` at whatever touched the row. This refuses at "
            "the iteration itself and names `.items` and `limit=None`."
        ),
    ),
    "INVALID_GROUP_BY": ErrorCodeInfo(
        kind="validation",
        description=(
            "`GuardedQuery.aggregate_by`'s (or `BoundQuery`'s/"
            "`OntologyClient`'s) `group_by` cannot be a group key. Either it "
            "was falsy (e.g. \"\") -- the shared aggregation body branches on "
            "`group_by`'s truthiness, so a falsy-but-non-None value would "
            "otherwise silently collapse to the ungrouped path and return a "
            "float instead of a `dict[str, float]` -- or it names a property "
            "whose declared `PropertyType` is not groupable (`json`, whose "
            "values may be a `dict` or `list` and so need not be hashable; "
            "grouping by one used to raise a bare `TypeError` from inside the "
            "grouping loop, and `INTERNAL_ERROR` once it crossed the MCP "
            "boundary). The declared type is checked, not the stored values, "
            "so a `json` column that happens to hold only scalars refuses too "
            "rather than working until the first `dict` arrives. Both are "
            "checked in `aggregate_by`, where the `GuardedQuery`, "
            "`BoundQuery`, and client surfaces converge, before `_aggregate` "
            "runs, rather than relying on an assert removed by `python -O`."
        ),
    ),
    "GROUP_KEY_COLLISION": ErrorCodeInfo(
        kind="validation",
        description=(
            "Two distinct `group_by` values in one selection release as the "
            "same dictionary key, so one cell would have to describe two "
            "populations. The released shape is `dict[str, ...]` -- a public "
            "return type and MCP's wire shape -- and `str()` is not injective "
            "over the values a group key can take: an optional property keys "
            "`None` on the rows that lack it, which collides with a row "
            "carrying the literal string `\"None\"`. The populations did not "
            "merge; the later one overwrote the earlier, so the released value "
            "(and, under `func=\"count\"`, the released size) described "
            "whichever rows were inserted last, decided by nothing the caller "
            "supplied or could observe. Raised per group as each is released, "
            "AFTER that group's min-N check, so the release floor keeps "
            "precedence over a shape refusal."
        ),
    ),
    "INVALID_LIMIT": ErrorCodeInfo(
        kind="validation",
        description=(
            "`GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) "
            "`limit` was < 1 -- a silently empty page would hide that the "
            "call was malformed rather than legitimately paginated."
        ),
    ),
    "AFTER_WITHOUT_LIMIT": ErrorCodeInfo(
        kind="validation",
        description=(
            "`GuardedQuery.get_objects`'s (or `OntologyClient.list`'s) "
            "`after` was given without `limit` -- the unpaginated "
            "`Store.read_all` path has no page to resume, "
            "so ignoring `after` would let a caller that lost track of its "
            "limit silently re-read every visible row and duplicate work; a "
            "caller that genuinely wants everything passes no `after` at all."
        ),
    ),
    "STORE_VERSION_UNSUPPORTED": ErrorCodeInfo(
        kind="conflict",
        description=(
            "Raised at store construction when the store's schema stamp is not "
            "this engine's `SCHEMA_VERSION` -- a SQLite file's `PRAGMA "
            "user_version`, or a Postgres database's `schema_meta` row. Neither "
            "backend carries a migration ladder: a store written by a different "
            "ontary schema shape is REFUSED, never migrated in place and never "
            "adopted. An unstamped store that already has an `objects` table is "
            "refused for the same reason -- stamping a shape this engine cannot "
            "read would be a lying stamp, and every later query would fail as a "
            "confusing uncoded SQL error instead. The message names BOTH the "
            "store's and the engine's versions, so an operator knows exactly "
            "what to upgrade; the way forward is a matching ontary version, or "
            "a fresh store the data is migrated into."
        ),
    ),
    "CLOCK_CONFLICT": ErrorCodeInfo(
        kind="precondition",
        description=(
            "A store already has a different clock installed; a store has one "
            "clock. Bind with the same clock object, or with no clock to use "
            "the one already installed."
        ),
    ),
    "CLOCK_REGRESSION": ErrorCodeInfo(
        kind="precondition",
        description=(
            "The store clock reads earlier than the valid_from of the version "
            "a write would close; the clock went backwards. Fix the clock "
            "(it must never run behind the data it wrote) and retry."
        ),
    ),
    "CLOCK_NOT_TIMEZONE_AWARE": ErrorCodeInfo(
        kind="validation",
        description=(
            "A clock returned a naive datetime; an instant must be "
            "timezone-aware. Return datetime values with a tzinfo, such as "
            "datetime.now(timezone.utc)."
        ),
    ),
    "INTERNAL_ERROR": ErrorCodeInfo(
        kind="internal",
        description=(
            "An unclassified failure the MCP surface refuses to describe "
            "further, to avoid leaking internals to the caller."
        ),
    ),
}


class VisibilityError(OntaryError):
    """Base class for visibility failures."""

    kind: Kind = "visibility"


class PermissionDenied(OntaryError):
    """Base class for permission failures."""

    kind: Kind = "permission"


class PreconditionFailed(OntaryError):
    """Base class for precondition failures."""

    kind: Kind = "precondition"


class ValidationFailed(OntaryError):
    """Base class for validation failures."""

    kind: Kind = "validation"


class AuthorityError(OntaryError):
    """Base class for authority failures."""

    kind: Kind = "authority"


class ConflictError(OntaryError):
    """Base class for conflict failures."""

    kind: Kind = "conflict"


class InternalError(OntaryError):
    """Base class for internal failures."""

    kind: Kind = "internal"
