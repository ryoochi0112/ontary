"""Encode a Function or Action handler result for the JSON boundary (#167).

Both handler kinds hand their result to callers that may be remote (MCP) or
in-process, and both surfaces must see the same value. This module is the one
place that turns a handler result into that value: ``date``/``datetime``
objects become the ISO 8601 strings the store itself keeps, and anything else
that JSON cannot carry unchanged is refused with ``RESULT_NOT_JSON``.

The walk exists in addition to the final ``json.dumps`` round trip because a
round trip only says *that* a result is unencodable. It cannot say *which*
value, and the error an author needs names the handler and the key path to the
first offending value (``result["rows"][0]["when"]``). The round trip stays
as a belt-and-braces guard behind the walk.
"""

from __future__ import annotations

import json
import math
from datetime import date, datetime
from typing import Any

from .errors import PreconditionFailed
from .typesys import _to_storage_scalar


def _refuse(api_name: str, path: str, label: str) -> PreconditionFailed:
    return PreconditionFailed(
        f"{api_name!r}: result is not JSON-serializable at {path} ({label})",
        code="RESULT_NOT_JSON",
    )


def _walk(api_name: str, value: Any, path: str) -> Any:
    if isinstance(value, datetime):
        return _to_storage_scalar(value, "datetime")
    if isinstance(value, date):
        return _to_storage_scalar(value, "date")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _refuse(api_name, path, "non-finite float")
        return value
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, dict):
        for key in value:
            if not isinstance(key, str):
                raise _refuse(api_name, path, f"non-str key {key!r}")
        return {
            key: _walk(
                api_name, inner, f"{path}[{json.dumps(key, ensure_ascii=False)}]"
            )
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [
            _walk(api_name, inner, f"{path}[{index}]")
            for index, inner in enumerate(value)
        ]
    raise _refuse(api_name, path, type(value).__name__)


def encode_result(api_name: str, result: Any, *, require_dict: bool) -> Any:
    """Return an encoded copy of ``result``; never mutate the input.

    ``require_dict`` is set for Actions, whose results must be dictionaries.
    Raises ``PreconditionFailed(code="RESULT_NOT_JSON")`` naming ``api_name``
    and the key path of the first value that cannot cross the JSON boundary.
    """
    if require_dict and not isinstance(result, dict):
        raise PreconditionFailed(
            f"{api_name!r}: handler returned {type(result).__name__}; "
            "action results must be JSON-safe dictionaries",
            code="RESULT_NOT_JSON",
        )
    encoded = _walk(api_name, result, "result")
    try:
        round_tripped = json.loads(json.dumps(encoded, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise _refuse(api_name, "result", "unencodable") from exc
    if round_tripped != encoded:
        raise _refuse(api_name, "result", "unencodable")
    return encoded
