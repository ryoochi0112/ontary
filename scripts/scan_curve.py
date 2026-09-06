"""Measure the governed-read scan curve for the shipped ontary backend.

Seeds N Tickets into an isolated store (99% in queue Q0, 1% in Q1) and times
the public consumer read paths at each N. Q0's consumer is the BROAD case
(sees ~everything); Q1's is the NARROW case (sees ~1%).

Run with SQLite, which is the default::

    uv run python scripts/scan_curve.py 1000 5000

Pass ``--dsn`` to measure a PostgresStore instead. Each row printed is one
JSON result for one requested row count; the script reports measurements and
does not assert a timing threshold. Against Postgres, the seeded schema is
``VACUUM ANALYZE``d before anything is timed (``vacuum_analyze_ms`` reports
that cost separately): reading immediately after ``seed()``'s one large
transaction otherwise measures a bulk-load artifact -- unset visibility-map
hint bits and absent planner statistics -- rather than steady-state
performance (a no-op, always ~0.0, against SQLite).

Pass ``--uncached`` to additionally measure the ``aggregate`` and
narrow-scope ``list`` paths with the scope-resolution memo
(``ontary.scope._ScopeReadCache``, see docs/storage.md#storage-envelope)
disabled, so AC10's ">= 2x" ratio is reproducible from this committed script
alone rather than asserted from memory. The toggle is script-local: it
patches the symbol ``ontary.query`` resolves for ``_ScopeReadCache`` with a
pass-through stand-in for the duration of one timed call, then restores it.
No product code changes shape or gains a public knob.
"""

from __future__ import annotations

import argparse
import importlib
import importlib.util
import json
import os
import statistics
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import cast

import ontary.query as _query_module
from ontary import Consumer, ObjectStore, Ontology, OntologyClient, OntologyObject, Source
from ontary.scope import _ScopeReadCache
from ontary.store import PostgresStore, Store, StoredObject

SRC = Source(source_system="bench")
STATUSES = ["open", "pending", "closed"]
CHANNELS = ["email", "chat", "phone"]
DEFAULT_SIZES = [1000, 5000, 10000]


class _UncachedScopeReads(_ScopeReadCache):
    """A same-shaped stand-in for the scope memo that never memoizes.

    Every ``GuardedQuery`` call site that resolves scope constructs its own
    memo with the bare name ``_ScopeReadCache()`` (``src/ontary/query.py``);
    Python looks that name up in ``ontary.query``'s module namespace at call
    time, so ``_memo_disabled`` below can swap in this stand-in for the
    duration of one call without touching ``src/``. Overriding every
    ``read_*``/``links_*`` method to call straight through to the store
    exercises the SAME code path the memo does
    (``resolve_owning_scope`` -> ``_visible``) with only the memoization
    removed, so the timing delta it produces is attributable to the memo
    alone.
    """

    def read_current(
        self, store: Store, obj_type: str, obj_id: str
    ) -> StoredObject | None:
        return store.read_current(obj_type, obj_id)

    def read_last(
        self, store: Store, obj_type: str, obj_id: str
    ) -> StoredObject | None:
        return store.read_last(obj_type, obj_id)

    def links_from(self, store: Store, link: str, obj_id: str) -> list[str]:
        return list(store.links_from(link, obj_id))

    def links_to(self, store: Store, link: str, obj_id: str) -> list[str]:
        return list(store.links_to(link, obj_id))

    def links_from_asof(
        self, store: Store, link: str, obj_id: str, asof: str
    ) -> list[str]:
        return list(store.links_from_asof(link, obj_id, asof))

    def links_to_asof(
        self, store: Store, link: str, obj_id: str, asof: str
    ) -> list[str]:
        return list(store.links_to_asof(link, obj_id, asof))


@contextmanager
def _memo_disabled() -> Iterator[None]:
    """Disable the scope-resolution memo for one timed call, then restore it.

    Patches the ``_ScopeReadCache`` attribute ``ontary.query`` resolves, not
    the class itself, so every other import of ``ontary.scope._ScopeReadCache``
    (tests, other call sites) is untouched and the swap cannot leak past the
    ``with`` block even if the timed call raises.
    """
    # Indexed through __dict__, not `_query_module._ScopeReadCache`: that
    # attribute is a private name `ontary.query` never re-exports, so under
    # --strict mypy treats plain attribute syntax on it as undefined. The
    # module's own namespace mapping is what `_ScopeReadCache()` call sites
    # inside query.py actually resolve against at call time.
    namespace = _query_module.__dict__
    original = namespace["_ScopeReadCache"]
    namespace["_ScopeReadCache"] = _UncachedScopeReads
    try:
        yield
    finally:
        namespace["_ScopeReadCache"] = original


def _load_tickets_module() -> ModuleType:
    """Load the repo example when this file is invoked by path or by module.

    The example is a source-tree package rather than a wheel package. Normal
    imports work for ``python -m`` and test runners; direct execution from
    ``scripts/`` needs a file-based import, not a ``sys.path`` mutation.
    """
    try:
        return importlib.import_module("examples.tickets.ontology")
    except ModuleNotFoundError as exc:
        if exc.name != "examples":
            raise
        module_path = Path(__file__).resolve().parents[1] / "examples" / "tickets" / "ontology.py"
        spec = importlib.util.spec_from_file_location(
            "examples.tickets.ontology", module_path
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load tickets ontology from {module_path}") from None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module


def _tickets_api() -> tuple[type[OntologyObject], Callable[[], tuple[Ontology, ObjectStore]]]:
    module = _load_tickets_module()
    return (
        cast(type[OntologyObject], module.Ticket),
        cast(Callable[[], tuple[Ontology, ObjectStore]], module.build_ontology),
    )


def seed(store: Store, n: int) -> tuple[str, str]:
    org_id = store.insert("Org", {"name": "Bench"}, SRC)
    q0 = store.insert("Queue", {"name": "Q0"}, SRC)
    q1 = store.insert("Queue", {"name": "Q1"}, SRC)
    store.create_link("queueOfOrg", q0, org_id)
    store.create_link("queueOfOrg", q1, org_id)
    narrow_every = 100
    with store.transaction():
        for i in range(n):
            qid = q1 if i % narrow_every == 0 else q0
            store.insert(
                "Ticket",
                {
                    "subject": f"ticket {i}",
                    "age_hours": float(i % 720),
                    "status": STATUSES[i % 3],
                    "queue_id": qid,
                    "channel": CHANNELS[i % 3],
                },
                SRC,
            )
    return q0, q1


def timed(fn: Callable[[], object], repeats: int = 3) -> tuple[float, object]:
    samples: list[float] = []
    result: object = None
    for _ in range(repeats):
        start = time.perf_counter()
        result = fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return statistics.median(samples), result


def size_of(result: object) -> int:
    if isinstance(result, list):
        return len(result)
    items = getattr(result, "items", None)
    if isinstance(items, list):
        return len(items)
    return -1


def _dsn_for(dsn: str, schema: str) -> str:
    separator = "&" if "?" in dsn else "?"
    return f"{dsn}{separator}options=-csearch_path%3D{schema}"


@contextmanager
def _postgres_schema(dsn: str) -> Iterator[str]:
    import psycopg

    name = f"ontary_scan_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(dsn, autocommit=True) as setup:
        setup.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')
        setup.execute(f'CREATE SCHEMA "{name}"')
    try:
        yield name
    finally:
        with psycopg.connect(dsn, autocommit=True) as cleanup:
            cleanup.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


def _vacuum_analyze(dsn: str, schema: str) -> None:
    """Settle a just-seeded Postgres table before timing reads against it.

    A row lookup issued immediately after ``seed()``'s single large
    transaction pays a real, escalating per-row cost that a steadily
    running deployment never sees: freshly committed heap pages carry no
    transaction-status "hint bits" yet, so early readers pay to consult
    commit status per row (and to set the bit for the next reader), and
    ``ANALYZE`` has not yet produced planner statistics for the brand new
    table. Confirmed by profiling this script's own ``aggregate()`` call
    at 8,000 rows: ~4.4s cold immediately after seeding versus ~1.1-1.2s
    either after this step or on an already-settled schema -- the same
    ~4x gap reproduced whether the table was vacuumed or simply left to
    sit. Without it the published curve would measure a bulk-load
    artifact, not steady-state Postgres. SQLite has no equivalent
    concept, so ``_open_store`` makes this a no-op there.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as maint:
        maint.execute(f'VACUUM ANALYZE "{schema}".objects')
        maint.execute(f'VACUUM ANALYZE "{schema}".links')


@contextmanager
def _open_store(
    ontology: Ontology, n: int, dsn: str | None
) -> Iterator[tuple[Store, str | None, Callable[[], None]]]:
    if dsn is None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, f"bench_{n}.sqlite")
            yield ObjectStore(ontology.registry, path), path, lambda: None
        return

    with _postgres_schema(dsn) as schema:
        store = PostgresStore(ontology.registry, _dsn_for(dsn, schema))
        try:
            yield store, None, lambda: _vacuum_analyze(dsn, schema)
        finally:
            store.close()


def run(n: int, dsn: str | None = None, *, uncached: bool = False) -> dict[str, object]:
    """Measure one row count. ``uncached`` additionally times the
    ``aggregate`` and narrow-scope ``list`` paths with the memo disabled
    (see ``_memo_disabled``), so the ratio AC10 requires is computed from
    two numbers this same run produced, not pasted in from elsewhere."""
    ticket_type, build = _tickets_api()
    ontology, _ = build()
    with _open_store(ontology, n, dsn) as (store, path, vacuum_analyze):
        seed_start = time.perf_counter()
        q0, q1 = seed(store, n)
        seed_ms = (time.perf_counter() - seed_start) * 1000.0

        vacuum_start = time.perf_counter()
        vacuum_analyze()
        vacuum_ms = (time.perf_counter() - vacuum_start) * 1000.0

        broad = OntologyClient(
            ontology,
            store,
            Consumer(
                actor_id="broad",
                role="Manager",
                scope_level="queue",
                scope_id=q0,
                kind="human",
            ),
        )
        narrow = OntologyClient(
            ontology,
            store,
            Consumer(
                actor_id="narrow",
                role="Manager",
                scope_level="queue",
                scope_id=q1,
                kind="human",
            ),
        )

        ops: dict[str, object] = {
            "backend": type(store).__name__,
            "n": n,
            "seed_ms": round(seed_ms, 1),
            "vacuum_analyze_ms": round(vacuum_ms, 1),
            "db_bytes": os.path.getsize(path) if path is not None else None,
        }

        ms, res = timed(lambda: store.read_all("Ticket"))
        ops["read_all_raw"] = round(ms, 1)
        ops["read_all_rows"] = size_of(res)

        ms, res = timed(lambda: broad.list(ticket_type, limit=1000))
        ops["page_1000"] = round(ms, 1)
        ops["page_1000_rows"] = size_of(res)

        ms, _ = timed(lambda: broad.list(ticket_type, {"status": "open"}, limit=1000))
        ops["page_where"] = round(ms, 1)

        ms, _ = timed(
            lambda: broad.list(ticket_type, limit=10, order_by=("age_hours", "desc"))
        )
        ops["top10_ordered"] = round(ms, 1)

        ms, res = timed(lambda: broad.count(ticket_type, {"status": "open"}))
        ops["count"] = round(ms, 1)
        ops["count_value"] = res

        ms, _ = timed(lambda: broad.exists(ticket_type, {"status": "open"}))
        ops["exists"] = round(ms, 1)

        ms, _ = timed(lambda: broad.aggregate(ticket_type, "age_hours", func="mean"))
        ops["aggregate_mean"] = round(ms, 1)

        ms, res = timed(lambda: narrow.list(ticket_type, limit=1000))
        ops["narrow_page_1000"] = round(ms, 1)
        ops["narrow_page_rows"] = size_of(res)

        if uncached:
            with _memo_disabled():
                ms, _ = timed(
                    lambda: broad.aggregate(ticket_type, "age_hours", func="mean")
                )
            ops["aggregate_mean_uncached"] = round(ms, 1)
            ops["aggregate_mean_memo_speedup"] = _speedup(
                cast(float, ops["aggregate_mean_uncached"]),
                cast(float, ops["aggregate_mean"]),
            )

            with _memo_disabled():
                ms, res = timed(lambda: narrow.list(ticket_type, limit=1000))
            ops["narrow_page_1000_uncached"] = round(ms, 1)
            ops["narrow_page_1000_memo_speedup"] = _speedup(
                cast(float, ops["narrow_page_1000_uncached"]),
                cast(float, ops["narrow_page_1000"]),
            )

        return ops


def _speedup(uncached_ms: float, cached_ms: float) -> float | None:
    """``uncached / cached``, or ``None`` when ``cached_ms`` rounded to 0 and
    the ratio would be a division by zero rather than a measurement."""
    if cached_ms <= 0.0:
        return None
    return round(uncached_ms / cached_ms, 2)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sizes", metavar="ROWS", nargs="*", type=int)
    parser.add_argument("--dsn", help="Postgres DSN; omit to measure SQLite")
    parser.add_argument(
        "--uncached",
        action="store_true",
        help=(
            "also time the aggregate and narrow-scope paths with the scope "
            "memo disabled, and report the memo speedup ratio (AC10)"
        ),
    )
    args = parser.parse_args()
    if not args.sizes:
        args.sizes = DEFAULT_SIZES
    return args


def main() -> None:
    args = _parse_args()
    for n in args.sizes:
        print(json.dumps(run(n, args.dsn, uncached=args.uncached)), flush=True)


if __name__ == "__main__":
    main()
