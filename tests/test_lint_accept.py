"""Declaration-time guards for modelling lint acceptance."""

from typing import Any, get_args

import pytest

import ontary
from ontary import ActionParams, Ontology, OntologyObject, meta, prop
from ontary.errors import ValidationFailed

KINDS = {
    "PropertyDef": ("STORED_DERIVABLE", "FREE_TEXT_STATUS"),
    "ObjectTypeDef": ("FORBIDDEN_TYPE_NAME", "AUDIT_TYPE"),
    "ActionTypeDef": ("CRUD_ACTION_NAME", "MICRO_ACTION"),
    "FunctionDef": ("CRUD_ACTION_NAME",),
}


def descriptor(kind: str, **kwargs: Any) -> Any:
    if kind == "PropertyDef":
        return meta.PropertyDef(name="value", type="str", **kwargs)
    if kind == "ObjectTypeDef":
        return meta.ObjectTypeDef(
            api_name="Example", display_name="Example", description="", layer="L0",
            properties=[meta.PropertyDef(name="id", type="str")], primary_key="id", **kwargs,
        )
    if kind == "ActionTypeDef":
        return meta.ActionTypeDef(
            api_name="Example", display_name="Example", target_type="Example",
            executable_by_roles=[], description="", parameters=[], **kwargs,
        )
    return meta.FunctionDef(
        api_name="Example", description="", input_description="", output_description="", **kwargs,
    )


def decorate(kind: str, **kwargs: Any) -> Any:
    ontology = Ontology(name="accept", scope_levels=["org"])

    class Example(OntologyObject):
        id: str = prop(primary_key=True)

    if kind == "PropertyDef":
        class WithProperty(OntologyObject):
            id: str = prop(primary_key=True)
            value: str = prop(**kwargs)

        ontology.object(layer="L0", scope="unscoped")(WithProperty)
        return ontology.registry.object_types["WithProperty"].properties[1]
    if kind == "ObjectTypeDef":
        ontology.object(layer="L0", scope="unscoped", **kwargs)(Example)
        return ontology.registry.object_types["Example"]
    ontology.object(layer="L0", scope="unscoped")(Example)
    if kind == "ActionTypeDef":
        class Params(ActionParams):
            pass

        ontology.action(Params, target=Example, roles=[], api_name="Example", **kwargs)(
            lambda ctx, params: {}
        )
        return ontology.registry.action_types["Example"]
    ontology.function(api_name="Example", **kwargs)(lambda query: {})
    return ontology.registry.functions["Example"]


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("build,single", [(descriptor, False), (decorate, False), (decorate, True)])
@pytest.mark.parametrize("refusal", ["typo", "wrong-kind", "error", "security"])
def test_invalid_accept_refused(kind: str, build: Any, refusal: str, single: bool) -> None:
    code = {
        "typo": "STORED_DERIVABL",
        "wrong-kind": "CRUD_ACTION_NAME" if kind == "PropertyDef" else "STORED_DERIVABLE",
        "error": "ONTOLOGY_INVALID",
        "security": "UNSCOPED_SENSITIVE",
    }[refusal]
    name = "value" if kind == "PropertyDef" else "Example"
    with pytest.raises(ValidationFailed) as caught:
        build(kind, accept=code if single and build is decorate else (code,))
    assert caught.value.code == "ONTOLOGY_INVALID"
    assert str(caught.value) == (
        f"{kind} {name!r}: cannot accept {code!r}; "
        f"accepted codes: {', '.join(KINDS[kind])}"
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("build", [descriptor, decorate])
def test_accept_defaults_and_sequences(kind: str, build: Any) -> None:
    assert build(kind).accept == ()
    codes = KINDS[kind]
    assert build(kind, accept=codes).accept == codes
    assert build(kind, accept=(*codes, codes[0])).accept == codes


@pytest.mark.parametrize("kind", KINDS)
def test_decorator_accept_single_string_and_list(kind: str) -> None:
    codes = KINDS[kind]
    assert decorate(kind, accept=codes[0]).accept == (codes[0],)
    assert decorate(kind, accept=list(codes) + [codes[0]]).accept == codes
    assert decorate(kind, accept=None).accept == ()


@pytest.mark.parametrize("build", [descriptor, decorate])
def test_snapshot_default_and_explicit(build: Any) -> None:
    assert build("ObjectTypeDef").snapshot is False
    assert build("ObjectTypeDef", snapshot=True).snapshot is True


def test_literal_alias_members_and_no_top_level_exports() -> None:
    for kind, alias in (
        ("PropertyDef", "PropertyLint"), ("ObjectTypeDef", "ObjectLint"),
        ("ActionTypeDef", "ActionLint"), ("FunctionDef", "FunctionLint"),
    ):
        assert get_args(getattr(meta, alias)) == KINDS[kind]
        assert alias in meta.__all__
        assert alias not in ontary.__all__
        assert not hasattr(ontary, alias)
