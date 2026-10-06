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


ROOT = Path(__file__).resolve().parent.parent
_LINKS_HEADING = "### Links and object-backed link types"


@pytest.mark.parametrize(
    ("relative", "link", "section"),
    [
        ("README.md", "(examples/room_booking/README.md)", None),
        ("docs/getting-started.md", "(../examples/room_booking/README.md)", None),
        ("docs/getting-started.ja.md", "(../examples/room_booking/README.md)", None),
        ("docs/ontology-design.md", "(../examples/room_booking/README.md)", _LINKS_HEADING),
        ("docs/ontology-design.ja.md", "(../examples/room_booking/README.md)", _LINKS_HEADING),
    ],
)
def test_room_booking_is_linked_from_docs(relative: str, link: str, section: str | None) -> None:
    text = (ROOT / relative).read_text(encoding="utf-8")
    if section is not None:
        assert section in text, f"{relative}: missing heading {section!r}"
        text = text.split(section, 1)[1].split("\n### ", 1)[0]
    assert link in text, f"{relative}: missing link {link}" + (
        f" inside {section!r}" if section else ""
    )
