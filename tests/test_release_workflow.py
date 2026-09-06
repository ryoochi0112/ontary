"""Pin the release workflow's gate/publish privilege split and release shape.

The workflow publishes to PyPI through trusted publishing: the `publish` job
holds `id-token: write` so PyPI can verify an OIDC token minted for THIS
workflow on THIS repository. That makes `publish` the one privileged job, and
every test here is about keeping it small: it depends on the gate, consumes
the gate's bytes without rebuilding, holds no long-lived secret, and exposes
no shell for an expression to be interpolated into.
"""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/release.yml"
_PARSER_HINT = "update this parser if the workflow shape changed"


def _workflow_parts() -> tuple[str, str]:
    text = WORKFLOW.read_text()
    parts = re.split(r"(?m)^jobs:\s*$", text, maxsplit=1)
    assert len(parts) == 2, f"release.yml has no top-level `jobs:` block; {_PARSER_HINT}"
    return parts[0], parts[1]


def _job_blocks(jobs_text: str) -> dict[str, str]:
    job_matches = list(re.finditer(r"(?m)^  ([A-Za-z_][A-Za-z0-9_-]*):$", jobs_text))
    assert job_matches, f"release.yml defines no top-level jobs; {_PARSER_HINT}"
    return {
        match.group(1): jobs_text[
            match.end() : (
                job_matches[index + 1].start()
                if index + 1 < len(job_matches)
                else len(jobs_text)
            )
        ]
        for index, match in enumerate(job_matches)
    }


def _step_blocks(job_block: str) -> list[str]:
    step_matches = list(
        re.finditer(r"(?m)^      - (?:uses|name):[^\n]+$", job_block)
    )
    assert step_matches, f"job defines no six-space-indented steps; {_PARSER_HINT}"
    return [
        job_block[
            match.start() : (
                step_matches[index + 1].start()
                if index + 1 < len(step_matches)
                else len(job_block)
            )
        ]
        for index, match in enumerate(step_matches)
    ]


def _run_bodies(job_block: str) -> list[tuple[str, str]]:
    """Return `(step name, shell body)` for every ``run:`` step of a job.

    Both YAML shapes are covered: a ``run: |`` literal block, and a one-line
    ``run: cmd``.
    """
    bodies: list[tuple[str, str]] = []
    for step in _step_blocks(job_block):
        name_match = re.search(r"(?m)^      - name: (.+)$", step)
        name = name_match.group(1) if name_match else step.splitlines()[0].strip()
        lines = step.splitlines()
        for index, line in enumerate(lines):
            if not line.startswith("        run:"):
                continue
            remainder = line[len("        run:") :].strip()
            if remainder not in {"|", "|-", ">", ">-"}:
                bodies.append((name, remainder))
                continue
            body: list[str] = []
            for following in lines[index + 1 :]:
                if following.strip() and not following.startswith("          "):
                    break
                body.append(following)
            bodies.append((name, "\n".join(body)))
    return bodies


def test_release_trigger_is_only_v_tag_pushes() -> None:
    header, _ = _workflow_parts()
    trigger_matches = re.findall(
        r"(?ms)^on:\s*\n(.*?)(?=^[a-z][a-z0-9_-]*:\s*$|\Z)", header
    )
    assert len(trigger_matches) == 1, (
        f"release.yml expected exactly one top-level `on:` block, found "
        f"{len(trigger_matches)}; {_PARSER_HINT}"
    )
    assert re.fullmatch(
        r"  push:\s*\n    tags:\s*\['v\*'\]\s*\n?", trigger_matches[0]
    ), f"release.yml must trigger only on push tags ['v*']; {_PARSER_HINT}"


def test_release_privileges_are_confined_to_publish_job() -> None:
    header, jobs_text = _workflow_parts()
    jobs = _job_blocks(jobs_text)
    assert set(jobs) == {"gate", "publish"}, (
        f"release.yml must define exactly the gate and publish jobs, found {sorted(jobs)}; "
        f"{_PARSER_HINT}"
    )
    assert "id-token" not in header, (
        f"release.yml must not grant workflow-level id-token access; {_PARSER_HINT}"
    )
    assert "permissions" not in header, (
        f"release.yml must not declare workflow-level permissions; {_PARSER_HINT}"
    )
    assert "permissions" not in jobs["gate"], (
        f"gate must not declare permissions; {_PARSER_HINT}"
    )
    assert "id-token" not in jobs["gate"], (
        f"gate must not declare id-token access; {_PARSER_HINT}"
    )
    assert jobs_text.count("permissions") == 1, (
        f"only publish may contain a permissions key; {_PARSER_HINT}"
    )

    publish = jobs["publish"]
    permission_blocks = re.findall(
        r"(?ms)^    permissions:\s*\n((?:      [^\n]+\n)+)", publish
    )
    assert len(permission_blocks) == 1, (
        f"publish must have exactly one block-style permissions mapping; {_PARSER_HINT}"
    )
    permissions = re.findall(
        r"(?m)^      ([A-Za-z_-]+):\s*([A-Za-z]+)\s*$", permission_blocks[0]
    )
    assert permissions == [("id-token", "write"), ("contents", "read")], (
        f"publish permissions must be only id-token: write and contents: read, found "
        f"{permissions}; {_PARSER_HINT}"
    )


def test_publish_runs_in_the_pypi_environment() -> None:
    """The trusted publisher registered on PyPI names the `pypi` environment.

    PyPI matches the OIDC token's `environment` claim against the registration,
    so a publish from any other environment (or none) is refused. The
    environment is also where GitHub's own tag restriction and optional
    reviewer gate live, so it must be the job-level key, not a step detail.
    """
    _, jobs_text = _workflow_parts()
    publish = _job_blocks(jobs_text).get("publish", "")
    assert re.search(r"(?m)^    environment: pypi$", publish), (
        f"publish must run in the `pypi` environment; {_PARSER_HINT}"
    )


def test_gate_checks_tag_version_before_running_copied_gate_steps() -> None:
    _, jobs_text = _workflow_parts()
    gate = _job_blocks(jobs_text).get("gate", "")
    assert gate.strip(), f"release.yml gate job block not found or empty; {_PARSER_HINT}"
    steps = _step_blocks(gate)
    step_heads = [step.splitlines()[0].strip() for step in steps]
    assert step_heads == [
        "- uses: actions/checkout@v4",
        "- uses: astral-sh/setup-uv@v5",
        "- name: Check tag matches project version",
        "- name: Install dependencies",
        "- name: make verify",
        "- name: Build sdist + wheel",
        "- name: Install the wheel into an empty environment",
        "- name: Smoke-test the installed package",
        "- name: Upload the distributions",
    ], f"gate steps are missing or out of order: {step_heads}; {_PARSER_HINT}"

    guard = steps[2]
    assert re.search(r"(?m)^          tag_version=\"\$\{GITHUB_REF_NAME#v\}\"$", guard), (
        f"gate guard must strip the v prefix from GITHUB_REF_NAME; {_PARSER_HINT}"
    )
    assert re.search(
        r"(?m)^          project_version=\"\$\(python -c "
        r"'import tomllib; print\(tomllib\.load\(open\(\"pyproject\.toml\", \"rb\"\)\)"
        r"\[\"project\"\]\[\"version\"\]\)'\)\"$",
        guard,
    ), f"gate guard must parse project.version from pyproject.toml; {_PARSER_HINT}"
    assert re.search(
        r"(?ms)^          if \[ \"\$tag_version\" != \"\$project_version\" \]; then\n"
        r"(?:            .+\n)*?            exit 1\n          fi$",
        guard.rstrip(),
    ), f"gate guard must exit non-zero on a version mismatch; {_PARSER_HINT}"

    expected_bodies = (
        "run: uv sync --all-extras",
        "run: make verify",
        "run: uv build --out-dir dist",
        "run: |\n          uv venv --python 3.12 /tmp/clean\n"
        "          VIRTUAL_ENV=/tmp/clean uv pip install dist/*.whl",
        "run: /tmp/clean/bin/python scripts/install_smoke.py",
    )
    for step, expected in zip(steps[3:8], expected_bodies, strict=True):
        assert expected in step, (
            f"gate step `{step.splitlines()[0].strip()}` does not copy `{expected}`; "
            f"{_PARSER_HINT}"
        )


def test_gate_uploads_the_built_dist_artifact() -> None:
    _, jobs_text = _workflow_parts()
    gate = _job_blocks(jobs_text).get("gate", "")
    assert gate.strip(), f"release.yml gate job block not found or empty; {_PARSER_HINT}"
    upload = _step_blocks(gate)[-1]
    assert re.fullmatch(
        r"(?s)      - name: Upload the distributions\n"
        r"        uses: actions/upload-artifact@v4\n"
        r"        with:\n"
        r"          name: dist\n"
        r"          path: dist/\s*",
        upload,
    ), f"gate must upload dist/ as the dist artifact; {_PARSER_HINT}"


def test_publish_depends_on_gate_and_downloads_dist_without_building() -> None:
    _, jobs_text = _workflow_parts()
    publish = _job_blocks(jobs_text).get("publish", "")
    assert publish.strip(), (
        f"release.yml publish job block not found or empty; {_PARSER_HINT}"
    )
    assert re.search(r"(?m)^    needs: gate$", publish), (
        f"publish must depend on gate; {_PARSER_HINT}"
    )
    steps = _step_blocks(publish)
    step_heads = [step.splitlines()[0].strip() for step in steps]
    assert step_heads == [
        "- name: Download the distributions",
        "- name: Publish the distributions to PyPI",
    ], f"publish must not execute extra steps, found {step_heads}; {_PARSER_HINT}"
    assert re.fullmatch(
        r"(?s)      - name: Download the distributions\n"
        r"        uses: actions/download-artifact@v4\n"
        r"        with:\n"
        r"          name: dist\n"
        r"          path: dist/\s*",
        steps[0],
    ), f"publish must download the dist artifact into dist/; {_PARSER_HINT}"
    assert re.search(r"(?i)\b(?:build|wheel|sdist)\b", publish) is None, (
        f"publish must consume gate's bytes without any build step; {_PARSER_HINT}"
    )
    assert "actions/checkout" not in publish, (
        f"publish must not check out the repository; it only needs dist/; {_PARSER_HINT}"
    )


def test_publish_uses_trusted_publishing_and_no_long_lived_secret() -> None:
    """PyPI is reached through the official publish action and OIDC only.

    A `password:`/`UV_PUBLISH_TOKEN` in the workflow would be a long-lived
    credential living in repository settings, which is the thing trusted
    publishing exists to remove. `secrets.` may not appear anywhere in the file:
    the automatic `GITHUB_TOKEN` is not needed either, since the job neither
    checks out nor writes to the repository.
    """
    text = WORKFLOW.read_text()
    _, jobs_text = _workflow_parts()
    publish = _job_blocks(jobs_text).get("publish", "")
    steps = _step_blocks(publish)
    assert re.fullmatch(
        r"(?s)      - name: Publish the distributions to PyPI\n"
        r"        uses: pypa/gh-action-pypi-publish@release/v1\n"
        r"        with:\n"
        r"          packages-dir: dist/\s*",
        steps[-1],
    ), f"publish must upload dist/ with pypa/gh-action-pypi-publish; {_PARSER_HINT}"
    assert re.search(r"(?i)\bsecrets\b", text) is None, (
        f"release.yml must not reference any secret; {_PARSER_HINT}"
    )
    for credential in ("password", "UV_PUBLISH_TOKEN", "TWINE_PASSWORD", "api-token"):
        assert credential not in text, (
            f"release.yml must not carry a long-lived credential ({credential}); "
            f"{_PARSER_HINT}"
        )
    assert re.search(r"(?m)^\s+repository-url:", publish) is None, (
        f"publish must target PyPI itself, not an alternative repository URL; "
        f"{_PARSER_HINT}"
    )


def test_publish_job_exposes_no_shell() -> None:
    """The privileged job runs no ``run:`` step at all.

    GitHub substitutes ``${{ ... }}`` into a script before the shell parses it,
    so any shell in the job that holds `id-token: write` is a command-injection
    surface. The simplest proof that no expression reaches such a shell is that
    there is no shell: both publish steps are `uses:` actions.
    """
    _, jobs_text = _workflow_parts()
    publish = _job_blocks(jobs_text).get("publish", "")
    bodies = _run_bodies(publish)
    assert bodies == [], (
        f"publish must consist only of `uses:` steps, found run blocks in "
        f"{[name for name, _ in bodies]}; {_PARSER_HINT}"
    )
    assert "${{" not in publish, (
        f"publish must not interpolate any expression; {_PARSER_HINT}"
    )
