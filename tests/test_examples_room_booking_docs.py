"""Run every python block of the room_booking README, naming the heading when one fails."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

README = Path(__file__).resolve().parent.parent / "examples/room_booking/README.md"
EXPECTED_BLOCKS = 5
_HEADING = re.compile(r"^#{1,6}\s+(.*\S)\s*$")


def python_blocks(text: str) -> list[tuple[str, str]]:
    """Return (nearest preceding heading, code) for each python block, in order."""
    blocks: list[tuple[str, str]] = []
    heading = "(no heading)"
    fence: str | None = None
    code: list[str] = []
    for line in text.splitlines():
        if fence is None:
            if line.startswith("```"):
                fence = line[3:].strip()
                code = []
            elif match := _HEADING.match(line):
                heading = match.group(1)
        elif line.startswith("```"):
            if fence == "python":
                blocks.append((heading, "\n".join(code) + "\n"))
            fence = None
        else:
            code.append(line)
    return blocks


def run_blocks(text: str, label: str) -> int:
    """Exec the python blocks in one shared namespace; name the heading on failure."""
    blocks = python_blocks(text)
    namespace: dict[str, object] = {"__name__": "readme"}
    for number, (heading, code) in enumerate(blocks, start=1):
        try:
            # dont_inherit: this file's `from __future__ import annotations` must not leak in.
            exec(compile(code, f"{label}#{number}", "exec", dont_inherit=True), namespace)
        except Exception as exc:
            raise AssertionError(
                f"{label} python block {number} under heading '{heading}' failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
    return len(blocks)


def test_given_the_example_readme_when_its_python_blocks_run_then_every_claim_holds() -> None:
    text = README.read_text()

    assert text.count("```python") == EXPECTED_BLOCKS
    assert run_blocks(text, "examples/room_booking/README.md") == EXPECTED_BLOCKS


def test_given_a_failing_block_when_the_harness_runs_then_the_failure_names_its_heading() -> None:
    markdown = (
        "# Title\n\n## Fine section\n\n```python\nx = 1\n```\n\n"
        "```bash\nnot python\n```\n\n"
        "## Broken section\n\nprose\n\n```python\nassert x == 2\n```\n"
    )

    with pytest.raises(AssertionError, match=r"block 2 under heading 'Broken section'"):
        run_blocks(markdown, "synthetic.md")
