from typing import Literal

import pytest
from conftest import raises_code
from pydantic import ValidationError

from ontary import ActionParams, FunctionParams
from ontary.errors import ValidationFailed
from ontary.meta import ActionParameterDef, FunctionDef
from ontary.model import _class_stamp


def _function_def(
    parameters: list[ActionParameterDef] | None = None,
) -> FunctionDef:
    return FunctionDef(
        api_name="ticketStats",
        description="Ticket statistics.",
        input_description="A queue id.",
        output_description="Statistics.",
        parameters=parameters,
    )


def test_function_params_is_a_separate_strict_model() -> None:
    class TicketStatsParams(FunctionParams):
        queue_id: str

    assert not issubclass(TicketStatsParams, ActionParams)
    assert TicketStatsParams(queue_id="q1").queue_id == "q1"
    assert _class_stamp(TicketStatsParams) == (None, None)
    with pytest.raises(ValidationError):
        TicketStatsParams.model_validate({"queue_id": "q1", "unexpected": True})


def test_function_def_distinguishes_legacy_and_no_inputs() -> None:
    assert _function_def().parameters is None
    assert _function_def([]).parameters == []
    assert _function_def([ActionParameterDef(name="queue_id", type="str")]).parameters == [
        ActionParameterDef(name="queue_id", type="str")
    ]


@pytest.mark.parametrize("semantics", ["target", "scope"])
def test_function_def_refuses_scope_semantics(
    semantics: Literal["target", "scope"],
) -> None:
    parameter = ActionParameterDef(
        name="queue_id",
        type="str",
        refers_to="Queue",
        scope_semantics=semantics,
    )
    with raises_code(ValidationFailed, "ONTOLOGY_INVALID") as exc_info:
        _function_def([parameter])
    message = str(exc_info.value)
    assert "ticketStats" in message
    assert "queue_id" in message
    assert "ref()" in message
