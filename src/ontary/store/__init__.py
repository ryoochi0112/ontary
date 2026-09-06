"""The storage seam: `Store` protocol, value models, errors, and backends.

Compatibility front door (B2 of the staged refactor): this package replaced
the single `ontary/store.py` module, and every name that module exposed is
re-exported here explicitly, so `from ontary.store import X` keeps working
for all of them. The pieces now live in:

- `ontary.store.errors` -- the store exception types
- `ontary.store.values` -- `Source`/`Lineage`/`StoredObject`/`PagedRow`,
  `DEFAULT_TENANT`/`DEFAULT_BATCH`
- `ontary.store.protocol` -- the `Store` protocol
- `ontary.store.schema` -- `SCHEMA_VERSION` and the SQLite DDL
- `ontary.store.migration` -- the SQLite schema-migration engine
- `ontary.store.sqlite` -- the SQLite `ObjectStore` backend
- `ontary.store.inmemory` -- the dict-backed `InMemoryStore` backend
- `ontary.store.postgres` -- the `PostgresStore` backend (psycopg imported
  on demand, so this re-export is safe without the `postgres` extra)

Both backends import their shared pieces from the submodules directly
(never this front door), so re-exporting them here cannot hit a partially
initialized package.
"""

from __future__ import annotations

from ontary.audit import AuditEntry as AuditEntry
from ontary.audit import CapabilityAccessRecord as CapabilityAccessRecord
from ontary.audit import WriteRecord as WriteRecord
from ontary.store.inmemory import InMemoryStore as InMemoryStore
from ontary.store.postgres import PostgresStore as PostgresStore
from ontary.store.protocol import Store as Store
from ontary.store.schema import SCHEMA_VERSION as SCHEMA_VERSION
from ontary.store.sqlite import ObjectStore as ObjectStore
from ontary.store.values import DEFAULT_BATCH as DEFAULT_BATCH
from ontary.store.values import DEFAULT_TENANT as DEFAULT_TENANT
from ontary.store.values import Lineage as Lineage
from ontary.store.values import PagedRow as PagedRow
from ontary.store.values import Source as Source
from ontary.store.values import StoredObject as StoredObject
