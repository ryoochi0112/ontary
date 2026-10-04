"""Execute every stage of the leave-request tutorial as a fresh checkpoint."""

import ast
import contextlib
import difflib
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PAGE = Path(__file__).resolve().parent.parent / "docs/tutorial-leave-requests.md"
EXPECTED_STAGES = 9
EXPECTED_PYTHON_FENCES = 25
EXPECTED_TESTS = 4
EXPECTED_OUTPUT_BLOCKS = 9
PYTEST_COMMAND = "pytest test_leave_requests.py -q"
MAX_LINES = 550
BANNED_TERMS = (
    r"aggregate root",
    r"bounded context",
    r"value object",
    r"ubiquitous language",
    r"domain event",
    r"\bentit(y|ies)\b",
)
BLOCKING_RUN = re.compile(r"(?:\.run\(\)|\bserver\.run\()")


def _sections(md: str) -> tuple[str, list[tuple[str, str]]]:
    sections = re.split(r"^## (.*)$", md, flags=re.MULTILINE)
    return sections[0], list(zip(sections[1::2], sections[2::2], strict=True))


def _stages(md: str) -> list[tuple[int, str, list[str]]]:
    preamble, sections = _sections(md)
    assert not re.findall(r"^```python\n", preamble, re.MULTILINE), (
        "Python fences outside a numbered stage"
    )
    stages = []
    for title, body in sections:
        fences = re.findall(r"^```python\n(.*?)^```\s*$", body, re.MULTILINE | re.DOTALL)
        heading = re.fullmatch(r"(\d+)\. (.+)", title)
        if heading is None:
            assert not fences, f"Python fences outside a numbered stage: {title}"
        else:
            stages.append((int(heading[1]), heading[2], fences))
    return stages


def _is_try_it(fence: str) -> bool:
    return fence.splitlines()[:1] == ["# Try it"]


def _outputs(md: str) -> dict[int, list[tuple[str, str]]]:
    """Pair each `text` block with the Try it or `pytest` fence it shows."""
    preamble, sections = _sections(md)
    outputs: dict[int, list[tuple[str, str]]] = {}
    for title, body in [("", preamble), *sections]:
        heading = re.fullmatch(r"(\d+)\. (.+)", title)
        label = f"stage {heading[1]}" if heading else f"outside a numbered stage: {title!r}"
        pairs: list[tuple[str, str]] = []
        previous: tuple[str, str] | None = None
        for lang, fence in re.findall(r"^```(\w*)\n(.*?)^```\s*$", body, re.MULTILINE | re.DOTALL):
            after_try_it = previous is not None and previous[0] == "python" and _is_try_it(previous[1])
            if lang == "text":
                if heading and after_try_it:
                    pairs.append(("try_it", fence))
                elif heading and previous is not None and previous[0] == "bash" and PYTEST_COMMAND in previous[1]:
                    pairs.append(("pytest", fence))
                else:
                    raise AssertionError(f"{label}: output block does not follow a Try it")
            elif after_try_it:
                raise AssertionError(f"{label}: Try it has no output block")
            previous = (lang, fence)
        if previous is not None and previous[0] == "python" and _is_try_it(previous[1]):
            raise AssertionError(f"{label}: Try it has no output block")
        if heading:
            outputs[int(heading[1])] = pairs
    return outputs


def _normalise(output: str) -> str:
    return "\n".join(line.rstrip() for line in output.splitlines()).strip("\n")


def _mask_duration(output: str) -> str:
    return re.sub(r" in \d+(?:\.\d+)?s\b", " in <N>s", output)


def _output_diff(expected: str, actual: str) -> str:
    return "\n".join(
        difflib.unified_diff(expected.splitlines(), actual.splitlines(), "text block", "actual", lineterm="")
    )


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


def test_blocking_server_run_is_rejected_but_asyncio_run_is_allowed() -> None:
    assert BLOCKING_RUN.search("server.run()")
    assert BLOCKING_RUN.search('server.run(transport="stdio")')
    assert not BLOCKING_RUN.search("asyncio.run(server.list_tools())")


def test_each_try_it_pairs_with_one_output_block() -> None:
    outputs = _outputs(f"""## 1. First
```python
# Try it
print(1)
```

```text
1
```
```bash
{PYTEST_COMMAND}
```
```text
1 passed in 0.01s
```
""")
    assert outputs == {1: [("try_it", "1\n"), ("pytest", "1 passed in 0.01s\n")]}


@pytest.mark.parametrize(
    ("md", "reason"),
    [
        ("## 1. A\n```python\n# Try it\nprint(1)\n```\n", "Try it has no output block"),
        ("## 1. A\n```python\n# Try it\nprint(1)\n```\n```python\nx = 1\n```\n", "Try it has no output block"),
        ("## 1. A\n```python\nx = 1\n```\n```text\n1\n```\n", "does not follow a Try it"),
        ("## 1. A\n```python\n# Try it\nprint(1)\n```\n```text\n1\n```\n```text\n1\n```\n", "does not follow a Try it"),
        ("## 1. A\n```bash\nontary validate m:o\n```\n```text\nok\n```\n", "does not follow a Try it"),
        ("## Next\n```text\n1\n```\n", "outside a numbered stage"),
    ],
)
def test_output_block_pairing_errors_name_the_stage(md: str, reason: str) -> None:
    with pytest.raises(AssertionError, match=reason):
        _outputs(md)


def test_output_normalisation_and_duration_mask() -> None:
    assert _normalise("\n\n  a  \nb\t\n\n") == "  a\nb"
    assert _mask_duration("4 passed in 0.16s") == "4 passed in <N>s"
    assert _mask_duration("4 passed in 12s") == "4 passed in <N>s"
    assert _normalise("a\n") != _normalise("b\n")


def test_tutorial_structure_and_limits() -> None:
    md = PAGE.read_text()
    stages = _stages(md)
    assert [number for number, _, _ in stages] == list(range(1, EXPECTED_STAGES + 1))
    assert len(stages) <= 10
    assert all(fences for _, _, fences in stages)
    fences = [fence for _, _, blocks in stages for fence in blocks]
    assert len(fences) == EXPECTED_PYTHON_FENCES
    assert all(not BLOCKING_RUN.search(fence) for fence in fences)
    outputs = _outputs(md)
    assert sum(len(pairs) for pairs in outputs.values()) == EXPECTED_OUTPUT_BLOCKS
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
        expected = [text for kind, text in _outputs(PAGE.read_text())[number] if kind == "try_it"]
        for fence, text in zip(try_it, expected, strict=True):
            printed = io.StringIO()
            with contextlib.redirect_stdout(printed):
                exec(compile(fence, filename, "exec"), namespace)
            actual, wanted = _normalise(printed.getvalue()), _normalise(text)
            assert actual == wanted, (
                "Try it output differs from the text block\n" + _output_diff(wanted, actual)
            )
        if number == EXPECTED_STAGES:
            tests = [v for k, v in namespace.items() if k.startswith("test_") and callable(v)]
            assert len(tests) == EXPECTED_TESTS
            for test in tests:
                test()
    except Exception as error:
        raise AssertionError(f"stage {number}: {error}") from error


def test_stage_8_pytest_output_matches_its_text_block(tmp_path: Path) -> None:
    md = PAGE.read_text()
    checkpoints = {number: program for number, program, _ in _checkpoints(_stages(md))}
    tests = [fence for number, _, fences in _stages(md) if number == 8 for fence in fences]
    (tmp_path / "leave_requests.py").write_text(checkpoints[7])
    (tmp_path / "test_leave_requests.py").write_text("from leave_requests import *\n" + "\n".join(tests))
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_") and key not in {"PYTHONPATH", "FORCE_COLOR", "PY_COLORS", "NO_COLOR"}
    }
    env["COLUMNS"] = "80"
    result = subprocess.run(
        [sys.executable, "-m", *PYTEST_COMMAND.split()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    expected = [text for kind, text in _outputs(md)[8] if kind == "pytest"]
    assert len(expected) == 1
    actual = _normalise(_mask_duration(result.stdout))
    wanted = _normalise(_mask_duration(expected[0]))
    assert actual == wanted, (
        f"stage 8: pytest output differs from the text block\n{_output_diff(wanted, actual)}\n{result.stderr}"
    )
