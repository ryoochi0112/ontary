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

Object types describe employees and requests. A choice property limits status:

```python
from datetime import date, datetime
from typing import Literal

from ontary import (
    ActionContext, ActionError, ActionParams, Ontology, OntologyObject, prop, ref,
)

ontology = Ontology("leave-requests", scope_levels=["team"])
```

An employee has a stable primary key, a team, and an allowance in working days.

```python
@ontology.object(layer="L0", scope="unscoped")
class Employee(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    team_id: str
    allowance_days: int
```

A request records dates, working days, and status. `Literal` limits status values.
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

Each request links to one employee, who holds the team and allowance facts.
Actions own the link because submitting a request creates it.

```python
request_employee = ontology.link(
    "request_employee", LeaveRequest, Employee,
    cardinality="MANY_TO_ONE", owned=True,
)
```

Both types use `scope="unscoped"`, so every consumer can read them for now.
Use `fresh()` in each **Try it** to bind a store, seed Amina, and create clients.
The helper runs only when called. `consumer()` is from `ontary.testing`:

```python
from ontary import ObjectStore, OntologyClient, Source
from ontary.testing import consumer

def fresh() -> tuple[OntologyClient, OntologyClient]:
    ontology.validate()
    store = ObjectStore(ontology.registry)
    runtime = ontology.bind(store)
    store.insert("Employee", {
        "id": "e-1", "name": "Amina", "team_id": "team-a", "allowance_days": 25,
    }, Source(source_system="staff"))
    employee = consumer(actor_id="e-1", role="Employee", scope_level="team", scope_id="team-a")
    manager = consumer(actor_id="m-1", role="Manager", scope_level="team", scope_id="team-a")
    return runtime.for_consumer(employee), runtime.for_consumer(manager)
```

Read the employee in **Try it**:

```python
# Try it
client, manager = fresh()
stored_employee = client.get(Employee, "e-1")
assert stored_employee is not None
print(stored_employee.name, stored_employee.allowance_days)
```
```text
Amina 25
```

## 2. Submit a request — your first action

An action expresses the business operation of submitting a request.
Add this block after the link. `SubmitRequest` types the inputs.
`ref(Employee)` declares which employee the input names.

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

`ActionContext` supplies reads and writes. `ctx.now()` supplies the timestamp.
`ctx.create()` generates the id. The request and employee link commit together.
`roles=["Employee"]` declares who can submit.

In **Try it**, submit two working days and read the request:

```python
# Try it
client, manager = fresh()
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

## 3. Only allowed moves: approve, reject, cancel

A transition graph declares allowed status moves. **Replace the `LeaveRequest` block**:

```python
from ontary.meta import TransitionDef

@ontology.object(layer="L0", owned=True, scope="unscoped")
class LeaveRequest(OntologyObject):
    id: str = prop(primary_key=True)
    start_date: date
    days: int
    status: Literal["submitted", "approved", "rejected", "cancelled"] = prop(
        transitions=TransitionDef(
            initial=("submitted",),
            moves={
                "submitted": ("approved", "rejected", "cancelled"),
                "approved": ("cancelled",),
                "rejected": (), "cancelled": (),
            },
        ),
    )
    submitted_at: datetime
```

A new request starts as submitted. A manager can approve or reject it.
An employee can cancel a submitted or approved request.
Rejected and cancelled requests have no further moves.
Add a rule requiring at least one day. It reads only the request:

```python
@ontology.rule(LeaveRequest, "positive_days", message="request at least one day")
def positive_days(request: LeaveRequest) -> bool:
    return request.days >= 1
```

Add the decision actions after `submit`. Each action owns one complete move.
`target(LeaveRequest)` declares the request whose status changes.

```python
from ontary import target

class ApproveRequest(ActionParams):
    request_id: str = target(LeaveRequest)

class RejectRequest(ActionParams):
    request_id: str = target(LeaveRequest)

@ontology.action(ApproveRequest, target=LeaveRequest, roles=["Manager"])
def approve(ctx: ActionContext, params: ApproveRequest) -> dict[str, str]:
    request = ctx.get(LeaveRequest, params.request_id)
    if request is None:
        raise ActionError("request does not exist", code="PRECONDITION_FAILED")
    request.status = "approved"
    ctx.save(request)
    return {"request_id": request.id}

@ontology.action(RejectRequest, target=LeaveRequest, roles=["Manager"])
def reject(ctx: ActionContext, params: RejectRequest) -> dict[str, str]:
    request = ctx.get(LeaveRequest, params.request_id)
    if request is None:
        raise ActionError("request does not exist", code="PRECONDITION_FAILED")
    request.status = "rejected"
    ctx.save(request)
    return {"request_id": request.id}
```

Add cancellation in its own block:

```python
class CancelRequest(ActionParams):
    request_id: str = target(LeaveRequest)

@ontology.action(CancelRequest, target=LeaveRequest, roles=["Employee"])
def cancel(ctx: ActionContext, params: CancelRequest) -> dict[str, str]:
    request = ctx.get(LeaveRequest, params.request_id)
    if request is None:
        raise ActionError("request does not exist", code="PRECONDITION_FAILED")
    request.status = "cancelled"
    ctx.save(request)
    return {"request_id": request.id}
```

In **Try it**, approve a request, then attempt to reject it; submit zero days
to see a rule refusal. The engine checks both on writes and rolls back refusals.

```python
# Try it
from ontary import OntaryError

client, manager = fresh()
result = client.execute(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=2))
manager.execute(ApproveRequest(request_id=result["request_id"]))
try:
    manager.execute(RejectRequest(request_id=result["request_id"]))
except OntaryError as error:
    assert error.code == "TRANSITION_NOT_ALLOWED"
    print(error.code)
else:
    raise AssertionError("rejecting an approved request must fail")
try:
    client.execute(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=0))
except OntaryError as error:
    assert error.code == "RULE_VIOLATED"
    print(error.code)
else:
    raise AssertionError("requesting zero days must fail")
```
```text
TRANSITION_NOT_ALLOWED
RULE_VIOLATED
```

## 4. Remaining days — derive, don't store

A Function derives remaining days from the employee's allowance and approved
requests. Add this block after the actions:

```python
from ontary import BoundQuery, FunctionParams

class RemainingDays(FunctionParams):
    employee_id: str = ref(Employee)

@ontology.function(RemainingDays)
def remaining_days(query: BoundQuery, params: RemainingDays) -> int:
    employee = query.get(Employee, params.employee_id)
    if employee is None:
        raise ActionError("employee is not visible", code="PRECONDITION_FAILED")
    requests = query.traverse(request_employee, employee.id, reverse=True)
    used = sum(request.days for request in requests if request.status == "approved")
    return employee.allowance_days - used
```

`FunctionParams` types the inputs; `BoundQuery` reads visible rows via the link.
No property stores the result, so cancellation changes the next answer.

In **Try it**, approval reduces remaining days by two:

```python
# Try it
client, manager = fresh()
result = client.execute(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=2))
params = RemainingDays(employee_id="e-1")
before = client.call_function(params)
manager.execute(ApproveRequest(request_id=result["request_id"]))
after = client.call_function(params)
assert (before, after) == (25, 23)
print("before:", before, "after:", after)
```
```text
before: 25 after: 23
```

## 5. Tell the rest of the system: the RequestDecided event

An event records the decision so other parts of the system can read it.
**Replace the decision actions block** from stage 3 with this block.

```python
from ontary import Event, target

@ontology.event(description="A manager decided a leave request.")
class RequestDecided(Event):
    decision: Literal["approved", "rejected"]

class ApproveRequest(ActionParams):
    request_id: str = target(LeaveRequest)

class RejectRequest(ActionParams):
    request_id: str = target(LeaveRequest)

@ontology.action(
    ApproveRequest, target=LeaveRequest, roles=["Manager"], emits=[RequestDecided],
)
def approve(ctx: ActionContext, params: ApproveRequest) -> dict[str, str]:
    request = ctx.get(LeaveRequest, params.request_id)
    if request is None:
        raise ActionError("request does not exist", code="PRECONDITION_FAILED")
    request.status = "approved"
    ctx.save(request)
    ctx.emit(RequestDecided(decision="approved"))
    return {"request_id": request.id}

@ontology.action(
    RejectRequest, target=LeaveRequest, roles=["Manager"], emits=[RequestDecided],
)
def reject(ctx: ActionContext, params: RejectRequest) -> dict[str, str]:
    request = ctx.get(LeaveRequest, params.request_id)
    if request is None:
        raise ActionError("request does not exist", code="PRECONDITION_FAILED")
    request.status = "rejected"
    ctx.save(request)
    ctx.emit(RequestDecided(decision="rejected"))
    return {"request_id": request.id}
```

`emits` declares which event an action may record.
`ctx.emit()` records it about the action's target request.
Readers see events for visible requests.

In **Try it**, approve the request and read its event back:

```python
# Try it
client, manager = fresh()
result = client.execute(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=2))
manager.execute(ApproveRequest(request_id=result["request_id"]))
records = client.events(RequestDecided, about=(LeaveRequest, result["request_id"]))
assert len(records) == 1
assert records[0].payload.decision == "approved"
print(records[0].event_type, records[0].payload.decision)
```
```text
RequestDecided approved
```

## 6. Each team sees only its own requests

Declared scope limits each team to its own requests. The ontology already has `scope_levels=["team"]`.
**Replace the `Employee` block**; `DirectProperty` reads the marked team key:
```python
from ontary import DirectProperty
@ontology.object(layer="L0", scope=[DirectProperty(level="team", property_name="team_id")])
class Employee(OntologyObject):
    id: str = prop(primary_key=True)
    name: str
    team_id: str = prop(scope_level="team")
    allowance_days: int
```
**Replace the `LeaveRequest` block**; `ViaLink` follows the employee link, so requests do not copy team facts:
```python
from ontary import ViaLink
from ontary.meta import TransitionDef
@ontology.object(layer="L0", owned=True, scope=[ViaLink(link_api_name="request_employee", direction="from", parent_type="Employee")])
class LeaveRequest(OntologyObject):
    id: str = prop(primary_key=True)
    start_date: date
    days: int
    status: Literal["submitted", "approved", "rejected", "cancelled"] = prop(transitions=TransitionDef(
        initial=("submitted",), moves={"submitted": ("approved", "rejected", "cancelled"), "approved": ("cancelled",), "rejected": (), "cancelled": ()},
    ))
    submitted_at: datetime
```
**Replace the `fresh()` block** to seed employees without consumer scope and submit a team-B request through its client:
```python
from ontary import ObjectStore, OntologyClient, Source
from ontary.testing import consumer
def fresh() -> tuple[OntologyClient, OntologyClient]:
    ontology.validate()
    store = ObjectStore(ontology.registry)
    runtime = ontology.bind(store)
    for employee_id, name, team_id in [("e-1", "Amina", "team-a"), ("e-2", "Bo", "team-b")]:
        store.insert("Employee", {"id": employee_id, "name": name, "team_id": team_id, "allowance_days": 25}, Source(source_system="staff"))
    team_b = runtime.for_consumer(consumer(actor_id="e-2", role="Employee", scope_level="team", scope_id="team-b"))
    team_b.execute(SubmitRequest(employee_id="e-2", start_date=date(2026, 11, 2), days=3))
    employee = consumer(actor_id="e-1", role="Employee", scope_level="team", scope_id="team-a")
    manager = consumer(actor_id="m-1", role="Manager", scope_level="team", scope_id="team-a")
    return runtime.for_consumer(employee), runtime.for_consumer(manager)
```
In **Try it**, `list().items` contains only team A's request; the engine applies scope to reads:
```python
# Try it
client, manager = fresh()
result = client.execute(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=2))
requests = client.list(LeaveRequest).items
assert [request.id for request in requests] == [result["request_id"]]
print("team A sees:", [request.days for request in requests])
```
```text
team A sees: [2]
```

## 7. Check the model: validate and diagnose

Validation checks the model, and diagnosis reports errors and design advisories. Run this command beside `leave_requests.py`:
```bash
ontary validate leave_requests:ontology
```
In **Try it**, run the same checks in Python. `validate()` raises on invalid declarations; `diagnose()` returns findings:
```python
# Try it
ontology.validate()
findings = ontology.diagnose()
assert findings == []
print("No findings.")
```
```text
No findings.
```

## 8. Test the operation with given / when / then

Put these `scenario()` tests in `test_leave_requests.py`, which starts with `from leave_requests import *`.

```python
from datetime import timezone
from ontary.testing import FixedClock, scenario
def test_submit_request():
    (scenario(ontology, clock=FixedClock(datetime(2026, 11, 1, tzinfo=timezone.utc)))
     .given(Employee(id="e-1", name="Amina", team_id="team-a", allowance_days=25))
     .when(SubmitRequest(employee_id="e-1", start_date=date(2026, 11, 2), days=2), by=consumer(actor_id="e-1", role="Employee", scope_level="team", scope_id="team-a"))
     .then(LeaveRequest, "id-2", status="submitted"))
def decision_case(status: str, role: str):
    request = LeaveRequest(id="r-1", start_date=date(2026, 11, 2), days=2, status=status, submitted_at=datetime(2026, 10, 1, tzinfo=timezone.utc))
    case = scenario(ontology).given(Employee(id="e-1", name="Amina", team_id="team-a", allowance_days=25), request)
    case.given_link(request_employee, request, "e-1")
    actor = consumer(actor_id="test", role=role, scope_level="team", scope_id="team-a")
    return case, actor
def test_rejecting_approved_request_fails():
    case, manager = decision_case("approved", "Manager")
    case.when(RejectRequest(request_id="r-1"), by=manager).then_error("TRANSITION_NOT_ALLOWED")
def test_approval_emits_decision():
    case, manager = decision_case("submitted", "Manager")
    case.when(ApproveRequest(request_id="r-1"), by=manager).then_event(RequestDecided(decision="approved"), about="r-1")
def test_employee_cannot_approve():
    case, employee = decision_case("submitted", "Employee")
    case.when(ApproveRequest(request_id="r-1"), by=employee).then_error("PERMISSION_DENIED")
```

Run the tests with pytest; the final checkpoint runs them again:

```bash
pytest test_leave_requests.py -q
```
```text
....                                                                     [100%]
4 passed in 0.16s
```

## 9. Let an agent drive it over MCP

Replace `fresh()` to expose its seeded store to the in-process MCP server, which lists tools and submits a request.

```python
from ontary import Consumer, ObjectStore, OntologyClient, Source
from ontary.testing import SequentialIds, consumer
def fresh() -> tuple[OntologyClient, OntologyClient, ObjectStore, Consumer]:
    ontology.validate()
    store = ObjectStore(ontology.registry)
    runtime = ontology.bind(store, id_factory=SequentialIds("mcp"))
    store.insert("Employee", {"id": "e-1", "name": "Amina", "team_id": "team-a", "allowance_days": 25}, Source(source_system="staff"))
    employee_consumer = consumer(actor_id="e-1", role="Employee", scope_level="team", scope_id="team-a")
    manager = consumer(actor_id="m-1", role="Manager", scope_level="team", scope_id="team-a")
    return runtime.for_consumer(employee_consumer), runtime.for_consumer(manager), store, employee_consumer
```

```python
# Try it
import asyncio
from ontary.mcp_server import build_mcp_server
client, manager, store, employee = fresh()
server = build_mcp_server(ontology, store, employee)
tools = [tool.name for tool in asyncio.run(server.list_tools())]
print(len(tools), "execute_action" in tools, "call_function" in tools)
result = asyncio.run(server.call_tool("execute_action", {"api_name": "SubmitRequest", "params": {"employee_id": "e-1", "start_date": "2026-11-02", "days": 2}}))
print(sorted(result.structured_content["result"]))
```
```text
12 True True
['request_id']
```

The CLI starts a localhost development server. `--dev` is required:

```bash
ontary serve leave_requests:ontology --dev --store ./leave-requests.sqlite --port 8000
```

## The whole program

Apply each marked replacement to `leave_requests.py`. Keep the **Try it** snippets in scratch files. Put the stage 8 tests in `test_leave_requests.py`; they use the same declarations and exercise the operation with fresh scenario stores.

## Where to go next

Continue with [Ontology design](ontology-design.md), [Testing](testing.md), [MCP serving](mcp-serving.md), and the [API reference](api-reference.md).
