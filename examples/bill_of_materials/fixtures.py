"""ERP source snapshots, loaded only through the client's bulk ingest APIs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TypedDict

from ontary import Consumer, ObjectStore, Ontology, OntologyClient, Source
from ontary.ingest import IngestReport

from .ontology import build_ontology

SOURCE = Source(source_system="erp")


class MasterData(TypedDict):
    suppliers: list[dict[str, Any]]
    parts: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    bom_parent: list[tuple[str, str]]
    bom_child: list[tuple[str, str]]
    part_supplier: list[tuple[str, str]]


SOURCE_V1: MasterData = {
    "suppliers": [{"id": "acme", "name": "Acme"}, {"id": "boltco", "name": "Boltco"}],
    "parts": [
        {"id": "bike", "name": "Bike"},
        {"id": "scooter", "name": "Scooter"},
        {"id": "frame", "name": "Frame"},
        {"id": "wheel", "name": "Wheel"},
        {"id": "hub", "name": "Hub"},
        {"id": "spoke", "name": "Spoke"},
        {"id": "bearing", "name": "Bearing"},
        {"id": "deck", "name": "Deck"},
    ],
    "lines": [
        {"id": "bike-frame", "quantity": 1},
        {"id": "bike-wheel", "quantity": 2},
        {"id": "wheel-hub", "quantity": 1},
        {"id": "wheel-spoke", "quantity": 32},
        {"id": "hub-bearing", "quantity": 2},
        {"id": "scooter-deck", "quantity": 1},
        {"id": "scooter-wheel", "quantity": 2},
    ],
    "bom_parent": [
        ("bike-frame", "bike"),
        ("bike-wheel", "bike"),
        ("wheel-hub", "wheel"),
        ("wheel-spoke", "wheel"),
        ("hub-bearing", "hub"),
        ("scooter-deck", "scooter"),
        ("scooter-wheel", "scooter"),
    ],
    "bom_child": [
        ("bike-frame", "frame"),
        ("bike-wheel", "wheel"),
        ("wheel-hub", "hub"),
        ("wheel-spoke", "spoke"),
        ("hub-bearing", "bearing"),
        ("scooter-deck", "deck"),
        ("scooter-wheel", "wheel"),
    ],
    "part_supplier": [
        ("frame", "acme"),
        ("deck", "acme"),
        ("spoke", "acme"),
        ("hub", "boltco"),
        ("bearing", "boltco"),
    ],
}

SOURCE_V2: MasterData = {
    "suppliers": [{"id": "acme", "name": "Acme"}, {"id": "boltco", "name": "Boltco"}],
    "parts": [
        {"id": "bike", "name": "Bike"},
        {"id": "scooter", "name": "Scooter"},
        {"id": "frame", "name": "Frame"},
        {"id": "wheel", "name": "Wheel"},
        {"id": "hub", "name": "Hub"},
        {"id": "spoke", "name": "Spoke"},
        {"id": "bearing", "name": "Bearing"},
        {"id": "deck", "name": "Deck"},
        {"id": "bell", "name": "Bell"},
    ],
    "lines": [
        {"id": "bike-frame", "quantity": 1},
        {"id": "bike-wheel", "quantity": 2},
        {"id": "wheel-hub", "quantity": 1},
        {"id": "wheel-spoke", "quantity": 36},
        {"id": "hub-bearing", "quantity": 2},
        {"id": "scooter-wheel", "quantity": 2},
        {"id": "bike-bell", "quantity": 1},
        {"id": "scooter-ghost", "quantity": 1},
    ],
    "bom_parent": [
        ("bike-frame", "bike"),
        ("bike-wheel", "bike"),
        ("wheel-hub", "wheel"),
        ("wheel-spoke", "wheel"),
        ("hub-bearing", "hub"),
        ("scooter-deck", "scooter"),
        ("scooter-wheel", "scooter"),
        ("bike-bell", "bike"),
        ("scooter-ghost", "scooter"),
    ],
    "bom_child": [
        ("bike-frame", "frame"),
        ("bike-wheel", "wheel"),
        ("wheel-hub", "hub"),
        ("wheel-spoke", "spoke"),
        ("hub-bearing", "bearing"),
        ("scooter-deck", "deck"),
        ("scooter-wheel", "wheel"),
        ("bike-bell", "bell"),
        ("scooter-ghost", "ghost"),
    ],
    "part_supplier": [
        ("frame", "acme"),
        ("deck", "acme"),
        ("spoke", "acme"),
        ("hub", "boltco"),
        ("bearing", "boltco"),
    ],
}

CYCLIC_SOURCE: MasterData = {
    "suppliers": [],
    "parts": [
        {"id": "a", "name": "A"},
        {"id": "b", "name": "B"},
        {"id": "c", "name": "C"},
        {"id": "d", "name": "D"},
    ],
    "lines": [
        {"id": "a-b", "quantity": 1},
        {"id": "b-c", "quantity": 1},
        {"id": "c-a", "quantity": 1},
        {"id": "a-d", "quantity": 1},
    ],
    "bom_parent": [("a-b", "a"), ("b-c", "b"), ("c-a", "c"), ("a-d", "a")],
    "bom_child": [("a-b", "b"), ("b-c", "c"), ("c-a", "a"), ("a-d", "d")],
    "part_supplier": [],
}


def planner_consumer() -> Consumer:
    """A planner reading the unscoped master data."""
    return Consumer(
        actor_id="planner", role="Planner", scope_level="plant", scope_id="all", kind="human"
    )


def load_master_data(client: OntologyClient, source: MasterData) -> dict[str, IngestReport]:
    """Upsert objects before links; each batch reports errors and retains valid rows."""
    reports = {
        "suppliers": client.ingest("Supplier", source["suppliers"], SOURCE, on_error="report"),
        "parts": client.ingest("Part", source["parts"], SOURCE, on_error="report"),
        "lines": client.ingest("BomLine", source["lines"], SOURCE, on_error="report"),
    }
    for key, pairs in (
        ("bom_parent", source["bom_parent"]),
        ("bom_child", source["bom_child"]),
        ("part_supplier", source["part_supplier"]),
    ):
        reports[key] = client.ingest_links(key, pairs, SOURCE, on_error="report")
    return reports


@dataclass
class World:
    """The loaded ontology, store, planner client and per-batch reports."""

    ontology: Ontology
    store: ObjectStore
    client: OntologyClient
    reports: dict[str, IngestReport]


def load_world(source: MasterData = SOURCE_V1) -> World:
    """Load a source snapshot into a fresh world through the single loading path."""
    ontology, store = build_ontology()
    client = ontology.bind(store).for_consumer(planner_consumer())
    reports = load_master_data(client, source)
    return World(ontology, store, client, reports)
