# Tutorial: model a leave-request approval

*Tutorial* — You build one real operation from a first object to an agent that drives it, one step at a time; [Ontology design](ontology-design.md) explains the reasoning and the [API reference](api-reference.md) has every detail.

In this tutorial you model how a team asks for time off and how a manager
decides. By the end you have a tested ontology that an AI agent can use
over MCP. You need about 40 minutes and `pip install "ontary[mcp]"`.

Put the model blocks in `leave_requests.py` in the order shown.
Run each **Try it** in a scratch `try_it.py` that starts with
`from leave_requests import *`; the fences show only the body to add below it.
When a stage says **replace the block**, replace that earlier block in
`leave_requests.py` and run `try_it.py` in a fresh Python process.
Validation and binding freeze declarations, so they belong in the scratch file.

[← README](../README.md)

## 1. Declare employees and leave requests

Object types describe the employees and requests, with a choice property for
request status. Start the model with an ontology and its imports:

```python
from datetime import date, datetime
from typing import Literal

from ontary import (
    ActionContext, ActionError, ActionParams, Ontology, OntologyObject, prop, ref,
)

ontology = Ontology("leave-requests", scope_levels=["team"])
```

An employee has a team and an annual leave allowance in working days.
The primary key gives each employee a stable identifier.

```python
@ontology.object(layer="L0", scope="unscoped")
class Employee(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    team_id: str
    allowance_days: int
```

A request records a start date, the number of working days requested,
its status, and when it was submitted.
`Literal` declares the allowed status values, so writes cannot store arbitrary
status text. Allowed moves between these values come in a later stage.
`owned=True` lets actions create and change requests.

```python
@ontology.object(layer="L0", owned=True, scope="unscoped")
class LeaveRequest(OntologyObject):
    id: str = prop(primary_key=True)
    start_date: date
    days: int
    status: Literal["submitted", "approved", "rejected", "cancelled"]
    submitted_at: datetime
```

Each request belongs to one employee. Store that relationship as a link;
the employee's team and allowance stay on the employee.
Actions own this link because submitting a request creates the relationship.

```python
request_employee = ontology.link(
    "request_employee", LeaveRequest, Employee,
    cardinality="MANY_TO_ONE", owned=True,
)
```

Both types explicitly use `scope="unscoped"` for now.
Every consumer can read them. A later stage declares team visibility.

Validate the declarations, bind a store, and read an employee in **Try it**:

```python
# Try it
from ontary import Consumer, ObjectStore, Source

ontology.validate()
store = ObjectStore(ontology.registry)
runtime = ontology.bind(store)
store.insert(
    "Employee",
    {"id": "e-1", "name": "Amina", "team_id": "team-a", "allowance_days": 25},
    Source(source_system="staff"),
)
employee = Consumer(
    actor_id="e-1", role="Employee", scope_level="team",
    scope_id="team-a", kind="human",
)
client = runtime.for_consumer(employee)
stored_employee = client.get(Employee, "e-1")
assert stored_employee is not None
print(stored_employee.name, stored_employee.allowance_days)
```
```text
Amina 25
```

## 2. Submit a request — your first action

An action expresses the business operation of submitting a request.
Add this block to `leave_requests.py` after the link.
`SubmitRequest` defines typed inputs, and `ref(Employee)` declares which
employee the input names.

```python
class SubmitRequest(ActionParams):
    employee_id: str = ref(Employee)
    start_date: date
    days: int


@ontology.action(SubmitRequest, target=LeaveRequest, roles=["Employee"])
def submit(ctx: ActionContext, params: SubmitRequest) -> dict[str, str]:
    employee = ctx.get(Employee, params.employee_id)
    if employee is None:
        raise ActionError("employee does not exist", code="PRECONDITION_FAILED")
    request = ctx.create(
        LeaveRequest, start_date=params.start_date, days=params.days,
        status="submitted", submitted_at=ctx.now(),
    )
    ctx.link(request_employee, request, employee)
    return {"request_id": request.id}
```

The typed `ActionContext` supplies reads and writes for the invocation.
`ctx.now()` supplies its timestamp, and `ctx.create()` generates the request id.
The action creates the request and its employee link together.
`roles=["Employee"]` declares who can submit.

Replace the scratch body with this **Try it** to submit two working days of leave
and read the stored request:

```python
# Try it
from ontary import Consumer, ObjectStore, Source

ontology.validate()
store = ObjectStore(ontology.registry)
runtime = ontology.bind(store)
store.insert(
    "Employee",
    {"id": "e-1", "name": "Amina", "team_id": "team-a", "allowance_days": 25},
    Source(source_system="staff"),
)
employee = Consumer(
    actor_id="e-1", role="Employee", scope_level="team",
    scope_id="team-a", kind="human",
)
client = runtime.for_consumer(employee)
result = client.execute(SubmitRequest(
    employee_id="e-1", start_date=date(2026, 11, 2), days=2,
))
request = client.get(LeaveRequest, result["request_id"])
assert request is not None
print(request.status, request.start_date, request.days)
```
```text
submitted 2026-11-02 2
```
