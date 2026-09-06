"""Guard the staged error hierarchy and catalog registration."""

from __future__ import annotations

import ontary
from ontary.errors import OntaryError

KIND_CLASSES = {
    cls.kind: cls
    for cls in (
        ontary.VisibilityError,
        ontary.PermissionDenied,
        ontary.PreconditionFailed,
        ontary.ValidationFailed,
        ontary.AuthorityError,
        ontary.ConflictError,
        ontary.InternalError,
    )
}


def test_exported_coded_exceptions_match_kind_hierarchy_and_catalog() -> None:
    """Every exported coded exception is rooted at its catalogued kind.

    ``MappingValidationError`` is intentionally a plain ``Exception`` for
    connector preflight validation, so it is outside the coded taxonomy.
    ``OntaryError`` is the taxonomy root and therefore has no
    kind-class parent of their own.
    """
    exported_exceptions = [
        getattr(ontary, name)
        for name in ontary.__all__
        if isinstance(getattr(ontary, name), type)
        and issubclass(getattr(ontary, name), BaseException)
        and issubclass(getattr(ontary, name), OntaryError)
    ]

    assert exported_exceptions
    assert set(KIND_CLASSES) == {
        "visibility",
        "permission",
        "precondition",
        "validation",
        "authority",
        "conflict",
        "internal",
    }

    for exception in exported_exceptions:
        # `code` is per-instance and REQUIRED at construction as of 0.6.0 --
        # there is deliberately no class-level default left to check here,
        # because a default is what let a rebound name ship the wrong code.
        assert not hasattr(exception, "code"), (
            f"{exception.__name__} reintroduced a class-level `code` default; "
            "that fallback is what 0.6.0 removed"
        )
        if exception is OntaryError:
            continue
        assert issubclass(exception, KIND_CLASSES[exception.kind]), exception.__name__
