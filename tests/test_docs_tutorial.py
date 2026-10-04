"""Execute every stage of the leave-request tutorial as a fresh checkpoint."""

import ast
import re
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "docs/tutorial-leave-requests.md"
EXPECTED_STAGES = 2
EXPECTED_PYTHON_FENCES = 7
EXPECTED_TESTS = 0
MAX_LINES = 550
BANNED_TERMS = (
    r"aggregate root",
    r"bounded context",
    r"value object",
    r"ubiquitous language",
    r"domain event",
    r"\bentit(y|ies)\b",
)


def _stages(md: str) -> list[tuple[int, str, list[str]]]:
    sections = re.split(r"^## (.*)$", md, flags=re.MULTILINE)
    assert not re.findall(r"^```python\n", sections[0], re.MULTILINE), (
        "Python fences outside a numbered stage"
    )
    stages = []
    for title, body in zip(sections[1::2], sections[2::2], strict=True):
        fences = re.findall(r"^```python\n(.*?)^```\s*$", body, re.MULTILINE | re.DOTALL)
        heading = re.fullmatch(r"(\d+)\. (.+)", title)
        if heading is None:
            assert not fences, f"Python fences outside a numbered stage: {title}"
        else:
            stages.append((int(heading[1]), heading[2], fences))
    return stages


def _is_try_it(fence: str) -> bool:
    return fence.splitlines()[:1] == ["# Try it"]


def _bound_names(fence: str) -> set[str]:
    names: set[str] = set()
    for node in ast.parse(fence).body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(
                    item.id
                    for item in ast.walk(target)
                    if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
                )
    return names


def _checkpoints(
    stages: list[tuple[int, str, list[str]]],
) -> list[tuple[int, str, list[str]]]:
    live: list[tuple[set[str], str]] = []
    checkpoints = []
    for number, title, fences in stages:
        try_it = []
        for fence in fences:
            if _is_try_it(fence):
                try_it.append(fence)
                continue
            label = f"stage {number}: {title}"
            try:
                names = _bound_names(fence)
            except SyntaxError as error:
                raise AssertionError(f"{label}: {error}") from error
            overlaps = [index for index, (bound, _) in enumerate(live) if names & bound]
            assert len(overlaps) <= 1, (
                f"{label}: names {sorted(names)} overlap multiple live blocks"
            )
            if overlaps:
                index = overlaps[0]
                dropped = live[index][0] - names
                assert not dropped, f"{label}: revision drops names {sorted(dropped)}"
                live[index] = (names, fence)
            else:
                live.append((names, fence))
        checkpoints.append((number, "\n".join(fence for _, fence in live), try_it))
    return checkpoints


def test_revision_replaces_a_block_in_place() -> None:
    stages = _stages("""## 1. Original
```python
value: int = 1
left, right = 2, 3
```
```python
def answer():
    return value + left + right
result = answer()
```
## 2. Revision
```python
value: int = 10
left, right = 20, 30
extra = 40
```
""")
    checkpoints = _checkpoints(stages)
    namespace: dict[str, object] = {}
    exec(checkpoints[-1][1], namespace)
    assert namespace["result"] == 60
    assert "value: int = 1\n" not in checkpoints[-1][1]


@pytest.mark.parametrize(
    ("revision", "reason"),
    [("a, c = 4, 5", "multiple live blocks"), ("a = 4", "drops names.*b")],
)
def test_invalid_revision_names_the_stage(revision: str, reason: str) -> None:
    stages = _stages(f"""## 1. Original
```python
a, b = 1, 2
```
```python
c = 3
```
## 2. Revision
```python
{revision}
```
""")
    with pytest.raises(AssertionError, match=f"stage 2:.*{reason}"):
        _checkpoints(stages)


def test_try_it_fences_are_not_carried_forward() -> None:
    stages = _stages("""## 1. First
```python
value = 1
```
```python
# Try it
scratch = value
```
## 2. Second
```python
other = 2
```
```python
# Try it
assert 'scratch' not in globals()
```
""")
    first, second = _checkpoints(stages)
    assert first[2] == ["# Try it\nscratch = value\n"]
    assert second[2] == ["# Try it\nassert 'scratch' not in globals()\n"]
    assert "scratch" not in second[1]
    namespace: dict[str, object] = {}
    exec(second[1], namespace)
    exec(second[2][0], namespace)


def test_imports_do_not_count_as_names() -> None:
    stages = _stages("""## 1. First
```python
import math as shared
from pathlib import Path
a = 1
```
## 2. Second
```python
import math as shared
from pathlib import Path
b = 2
```
""")
    program = _checkpoints(stages)[-1][1]
    assert "a = 1" in program
    assert "b = 2" in program


@pytest.mark.parametrize("heading", ["", "## The whole program\n"])
def test_python_fences_outside_stages_are_rejected(heading: str) -> None:
    with pytest.raises(AssertionError, match="outside a numbered stage"):
        _stages(f"{heading}```python\nvalue = 1\n```\n")


def test_try_it_marker_must_be_the_exact_first_line() -> None:
    assert _is_try_it("# Try it\nprint(1)\n")
    assert not _is_try_it("\n# Try it\n")
    assert not _is_try_it("# Try it later\n")


def test_tutorial_structure_and_limits() -> None:
    md = PAGE.read_text()
    stages = _stages(md)
    assert [number for number, _, _ in stages] == list(range(1, EXPECTED_STAGES + 1))
    assert len(stages) <= 10
    assert all(fences for _, _, fences in stages)
    fences = [fence for _, _, blocks in stages for fence in blocks]
    assert len(fences) == EXPECTED_PYTHON_FENCES
    assert all(".run(" not in fence for fence in fences)
    assert len(md.splitlines()) <= MAX_LINES
    for term in BANNED_TERMS:
        assert not re.search(term, md, re.IGNORECASE), f"Banned term: {term}"


@pytest.mark.parametrize("number", range(1, EXPECTED_STAGES + 1), ids=lambda n: f"stage-{n}")
def test_tutorial_checkpoint(number: int) -> None:
    checkpoint = next(item for item in _checkpoints(_stages(PAGE.read_text())) if item[0] == number)
    _, program, try_it = checkpoint
    namespace: dict[str, object] = {}
    filename = f"docs/tutorial-leave-requests.md#stage-{number}"
    try:
        exec(compile(program, filename, "exec"), namespace)
        for fence in try_it:
            exec(compile(fence, filename, "exec"), namespace)
        if number == EXPECTED_STAGES:
            tests = [v for k, v in namespace.items() if k.startswith("test_") and callable(v)]
            assert len(tests) == EXPECTED_TESTS
            for test in tests:
                test()
    except Exception as error:
        raise AssertionError(f"stage {number}: {error}") from error
