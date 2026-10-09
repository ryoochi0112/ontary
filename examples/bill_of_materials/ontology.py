"""BOM quantities belong to source-backed lines; reports walk their links."""

from __future__ import annotations

from typing import TypedDict

from ontary import (
    BoundQuery,
    Cardinality,
    FunctionParams,
    ObjectStore,
    Ontology,
    OntologyObject,
    PreconditionFailed,
    prop,
    ref,
)

# Ontary requires a scope level; every type here is unscoped.
ontology = Ontology("bill_of_materials", scope_levels=["plant"])


@ontology.object(layer="L0", scope="unscoped")
class Supplier(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped")
class Part(OntologyObject):
    id: str = prop(primary_key=True)
    name: str


@ontology.object(layer="L0", scope="unscoped")
class BomLine(OntologyObject):
    id: str = prop(primary_key=True)
    quantity: int


@ontology.rule(BomLine, "positive_quantity", message="BOM quantity must be positive")
def positive_quantity(line: BomLine) -> bool:
    return line.quantity > 0


bom_parent = ontology.link("bom_parent", BomLine, Part, Cardinality.MANY_TO_ONE)
bom_child = ontology.link("bom_child", BomLine, Part, Cardinality.MANY_TO_ONE)
part_supplier = ontology.link("part_supplier", Part, Supplier, Cardinality.MANY_TO_ONE)


class ExplodeBom(FunctionParams):
    part_id: str = ref(Part)


class WhereUsed(FunctionParams):
    part_id: str = ref(Part)


class PartsFromSupplier(FunctionParams):
    supplier_id: str = ref(Supplier)


class WalkResult(TypedDict):
    rows: list[dict[str, str | int]]
    cycles: list[list[str]]


def _require_part(query: BoundQuery, part_id: str) -> Part:
    part = query.get(Part, part_id)
    if part is None:
        raise PreconditionFailed(f"unknown part {part_id!r}", code="PRECONDITION_FAILED")
    return part


@ontology.function(ExplodeBom, description="Explode a BOM depth-first, reporting cycles as data.")
def explode_bom(query: BoundQuery, p: ExplodeBom) -> WalkResult:
    root = _require_part(query, p.part_id)
    result: WalkResult = {"rows": [], "cycles": []}

    lines_by_part: dict[str, list[BomLine]] = {}
    children_by_line: dict[str, list[Part]] = {}
    frontier = [root.id]
    seen_part_ids = {root.id}
    while frontier:
        level_lines = query.traverse_many(bom_parent, frontier, reverse=True)
        lines_by_part.update(level_lines)

        line_ids = [line.id for lines in level_lines.values() for line in lines]
        level_children = query.traverse_many(bom_child, line_ids)
        children_by_line.update(level_children)

        next_frontier: list[str] = []
        for children in level_children.values():
            for child in children:
                if child.id not in seen_part_ids:
                    seen_part_ids.add(child.id)
                    next_frontier.append(child.id)
        frontier = next_frontier

    def walk(part: Part, path: list[str], level: int, total: int) -> None:
        children: list[tuple[Part, BomLine]] = []
        for line in lines_by_part.get(part.id, []):
            for child in children_by_line.get(line.id, []):
                children.append((child, line))
        for child, line in sorted(children, key=lambda pair: pair[0].id):
            child_path = [*path, child.id]
            if child.id in path:
                result["cycles"].append(child_path)
                continue
            child_total = total * line.quantity
            result["rows"].append(
                {
                    "level": level,
                    "part_id": child.id,
                    "part_name": child.name,
                    "quantity_per_parent": line.quantity,
                    "total_quantity": child_total,
                }
            )
            walk(child, child_path, level + 1, child_total)

    walk(root, [root.id], 1, 1)
    return result


@ontology.function(WhereUsed, description="Walk every ancestor path, reporting cycles as data.")
def where_used(query: BoundQuery, p: WhereUsed) -> WalkResult:
    root = _require_part(query, p.part_id)
    result: WalkResult = {"rows": [], "cycles": []}

    def walk(part: Part, path: list[str], level: int) -> None:
        parents: list[tuple[Part, BomLine]] = []
        for line in query.traverse(bom_child, part.id, reverse=True):
            for parent in query.traverse(bom_parent, line.id):
                parents.append((parent, line))
        for parent, line in sorted(parents, key=lambda pair: pair[0].id):
            parent_path = [*path, parent.id]
            if parent.id in path:
                result["cycles"].append(parent_path)
                continue
            result["rows"].append(
                {
                    "level": level,
                    "part_id": parent.id,
                    "part_name": parent.name,
                    "quantity": line.quantity,
                }
            )
            walk(parent, parent_path, level + 1)

    walk(root, [root.id], 1)
    return result


@ontology.function(PartsFromSupplier, description="List a supplier's parts sorted by part id.")
def parts_from_supplier(query: BoundQuery, p: PartsFromSupplier) -> list[dict[str, str]]:
    supplier = query.get(Supplier, p.supplier_id)
    if supplier is None:
        raise PreconditionFailed(
            f"unknown supplier {p.supplier_id!r}", code="PRECONDITION_FAILED"
        )
    return [
        {"part_id": part.id, "part_name": part.name}
        for part in sorted(
            query.traverse(part_supplier, supplier.id, reverse=True), key=lambda part: part.id
        )
    ]


ontology.validate()


def build_ontology() -> tuple[Ontology, ObjectStore]:
    """Return the validated ontology with a fresh store."""
    return ontology, ObjectStore(ontology.registry)
