"""M6 step 3: the package as a consumer receives it.

Everything else in this suite tests the SDK from the source tree with every
extra installed. Nobody had ever checked that the DISTRIBUTION is coherent --
that the version a consumer pins is the version the runtime reports, that the
typing marker ships, that the core install really is pydantic-only, and that
the metadata does not accidentally authorize publishing code whose license is
still an open question.

Building and installing a wheel is a CI job (`.github/workflows/verify.yml`),
not a test: it needs the network and a clean interpreter. These are the checks
that can run offline in `make verify`.
"""

from __future__ import annotations

import re
import tomllib
from importlib.metadata import metadata, requires, version
from pathlib import Path

import ontary

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _pyproject() -> dict[str, object]:
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


def _project() -> dict[str, object]:
    project = _pyproject()["project"]
    assert isinstance(project, dict)
    return project


def test_version_matches_pyproject() -> None:
    """`ontary.__version__` is read from installed metadata, so this pins the
    two against their single source rather than against each other."""
    assert ontary.__version__ == _project()["version"]
    assert ontary.__version__ == version("ontary")


def test_version_is_exported_from_the_front_door() -> None:
    """A consumer pinning a version wants to log the one actually running,
    without importing a private module to get it."""
    assert "__version__" in ontary.__all__


def test_py_typed_marker_ships_in_the_package() -> None:
    """PEP 561. Without this file INSIDE the installed package, a downstream
    `mypy --strict` silently sees `ontary` as untyped `Any` -- every guarantee
    the type surface makes would evaporate at the install boundary, and nothing
    in this repo's own type checking would notice, because it runs against the
    source tree."""
    marker = Path(ontary.__file__).parent / "py.typed"
    assert marker.is_file()


def test_core_install_depends_only_on_pydantic() -> None:
    """The claim the README makes in its first three lines is a packaging fact,
    not a stylistic one: an adopter should be able to take the core without
    inheriting an MCP server, dlt, or duckdb."""
    core = [
        dependency
        for dependency in (requires("ontary") or [])
        # `extra == "..."` markers are the optional groups, not core deps.
        if "extra ==" not in dependency
    ]
    assert [dependency.split(">=")[0].strip() for dependency in core] == ["pydantic"]


def test_optional_extras_are_declared_for_every_optional_import() -> None:
    optional = _project()["optional-dependencies"]
    assert isinstance(optional, dict)
    assert {"mcp", "dlt", "bq", "dev"} <= set(optional)


def test_metadata_carries_the_pointers_an_outside_consumer_needs() -> None:
    project = _project()
    assert project["readme"] == "README.md"
    urls = project["urls"]
    assert isinstance(urls, dict)
    assert {"Repository", "Changelog", "Documentation", "Compatibility"} <= set(urls)
    for target in ("CHANGELOG.md", "docs/compatibility.md"):
        assert (PYPROJECT.parent / target).is_file(), (
            f"{target} is linked from package metadata but does not exist"
        )


def test_licensing_is_declared_coherently() -> None:
    """MIT, decided 2026-07-26 (CEO), copyright Atrae, Inc.

    Replaces the interlock that used to live here. While the terms were
    undecided, this test asserted the OPPOSITE -- no `license` field, no LICENSE
    file, and a `Private :: Do Not Upload` classifier so PyPI would reject an
    accidental upload of code whose redistribution rights nobody had settled.
    The decision retires the tripwire and this test takes its place, checking
    the three facts that must now agree with each other rather than the three
    that had to be absent.

    The copyright holder is asserted because it is the one line in the
    distribution that makes a legal claim, and it is easy to "tidy" into the
    individual author's name: this is employee work, so the company holds it.
    """
    project = _project()
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]

    license_text = (PYPROJECT.parent / "LICENSE").read_text()
    assert license_text.startswith("MIT License")
    assert "Copyright (c) 2026 Atrae, Inc." in license_text

    # PEP 639: the expression supersedes the old `License ::` trove
    # classifiers, and declaring both is an error in current build backends.
    classifiers = project["classifiers"]
    assert isinstance(classifiers, list)
    assert not any(c.startswith("License ::") for c in classifiers)
    assert "Private :: Do Not Upload" not in classifiers, (
        "the publishing interlock was removed when MIT was chosen -- putting it "
        "back means a deliberate decision to stop distributing, not a lint fix"
    )


def test_installed_metadata_carries_the_license_expression() -> None:
    """The declaration is only worth what the built artifact says.

    `license = "MIT"` in `pyproject.toml` is a source-tree fact;
    `License-Expression: MIT` in the installed metadata is what a consumer, an
    SBOM scanner, or a corporate license audit actually reads."""
    assert metadata("ontary")["License-Expression"] == "MIT"


def test_installed_metadata_matches_the_declared_description() -> None:
    """Catches a stale build: an editable install whose metadata predates the
    current `pyproject.toml` would pass every other test in this file while
    reporting a different package to anyone who inspects it."""
    assert metadata("ontary")["Summary"] == _project()["description"]


def test_documented_install_ref_matches_the_current_version() -> None:
    """The install command in the docs names a tag, and the tag must be this
    version.

    Distribution is a **pinned git ref from a private repo**, not an index
    (decision 2026-07-26), which makes the documented command load-bearing in a
    way a `pip install ontary` line never was: there is no resolver to
    correct it. A `0.2.0` release whose README still says `@v0.1.0` would send
    every new consumer to the old code and look like it worked.

    Walks **every** `*.md` in the repo, not a hand-maintained file list. The
    list used to be `README.md`/`CHANGELOG.md`/`docs/compatibility.md`, and
    cutting `0.2.0` proved that was not the complete set: another Markdown doc
    kept handing readers `@v0.1.0`, and no test could see it. A doc carrying the
    install command is in scope by virtue of carrying it.

    `_REQUIRED_REFS` is the other half: a walk alone would let a file that
    *dropped* the command silently fall out of coverage, so the three canonical
    docs must still be found carrying one.

    **The changelog is scoped to its NEWEST release section**
    (`_newest_release_section`); every other file is checked whole. Those
    describe current state, so every ref in them must be current. A changelog is
    the opposite -- an archive, whose `## [0.1.0]` section documenting the
    `@v0.1.0` command is a true historical record, not drift. Checking it whole
    made the SECOND release of this package impossible without falsifying the
    history of the first (found 2026-07-27; this test had never been exercised
    by a second release). Narrowing keeps the property that matters: the command
    a reader copies out of the newest entry installs the version it describes.
    """
    version_tag = f"@v{_project()['version']}"
    root = PYPROJECT.parent
    seen: set[str] = set()
    candidates = sorted(p for suffix in _DOC_SUFFIXES for p in root.rglob(f"*{suffix}"))
    for path in candidates:
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        name = path.relative_to(root).as_posix()
        text = path.read_text()
        if name == "CHANGELOG.md":
            text = _newest_release_section(text)
        refs = [line for line in text.splitlines() if _INSTALL_REF in line]
        if not refs:
            continue
        seen.add(name)
        for line in refs:
            assert version_tag in line, (
                f"{name} pins a stale tag: {line.strip()!r} does not name "
                f"{version_tag!r}"
            )
    missing = sorted(set(_REQUIRED_REFS) - seen)
    assert not missing, f"no longer documents the git install ref: {missing}"


#: What makes a line an install ref. Deliberately the ref FRAGMENT
#: (`ontary@v…`) and not the full `git+ssh://git@github.com` URL: docs
#: legitimately elide the host (`git+ssh://.../ontary@v0.1.0`, a historical
#: decision record), and matching the full URL let exactly that line keep pointing
#: at a stale tag through three reviews of the 0.2.0 release. The fragment appears
#: in every form the docs use -- plain, `[mcp]`-extra'd, and elided -- so a doc
#: carrying an install command is in scope by virtue of carrying one, which is what
#: this test's docstring already claimed before the matcher could deliver it.
_INSTALL_REF = "ontary@v"

#: Which files count as docs. `.html` is here because an HTML documentation page
#: can carry an install command too, and an `*.md`-only walk would sail straight
#: past it while advertising a stale tag. Widening by *extension* is the narrow
#: lesson; the general one is that the guard must follow the CONTENT, so add a
#: suffix here the moment a doc of that kind carries an install command.
_DOC_SUFFIXES = (".md", ".html")

#: Directories not ours to police -- dependency trees and tool caches. `.venv`
#: alone would do today; the rest keep a future build/vendor directory from
#: silently failing the walk.
_SKIP_DIRS = frozenset(
    {".venv", ".git", ".mypy_cache", ".pytest_cache", "node_modules", "build", "dist"}
)

#: Docs that must ALWAYS carry the install command. Without this, a canonical
#: file that drops the command entirely just stops being walked -- passing by
#: absence.
_REQUIRED_REFS = (
    "README.md",
    "CHANGELOG.md",
    "docs/compatibility.md",
)

#: A *released* section heading: `## [` followed by a digit. Deliberately not
#: "any `## ` that isn't `[Unreleased]`" -- that earlier form returned the body
#: of the first non-Unreleased heading of ANY kind (a `## Migration notes`
#: between Unreleased and the newest release), which carries no install ref, so
#: the changelog arm passed VACUOUSLY. A guard whose whole job is catching a
#: missed bump must not degrade to a silent pass.
_RELEASE_HEADING = re.compile(r"^## \[\d")


def _newest_release_section(changelog: str) -> str:
    """The newest RELEASED section of a Keep-a-Changelog file -- from its
    heading up to the next `## ` heading, returned VERBATIM.

    `## [Unreleased]` is skipped deliberately: it is a staging area that by
    definition names no tag, so including it would make this test demand an
    install command from the one section that must not carry one. Everything
    below the newest release is history and is not this test's business.

    Fences are tracked so a ``` ## [9.9.9] ``` inside a worked example cannot pose
    as a heading -- otherwise a fenced block carrying a current-LOOKING ref could
    stand in for the real section while the actual newest entry pinned a stale
    tag. Note the asymmetry, which is the whole reason this is a line scan and not
    a `re.split` over fence-stripped text: fences are ignored when deciding what
    is a HEADING, but the returned section keeps them, because this section's real
    install command lives inside a ```bash fence and stripping it would leave the
    guard with nothing to check.
    """
    start: int | None = None
    lines, fenced = changelog.splitlines(), False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue
        if start is None:
            if _RELEASE_HEADING.match(line):
                start = i
        elif line.startswith("## "):
            return "\n".join(lines[start:i])
    if start is None:
        raise AssertionError("CHANGELOG.md has no released section")
    return "\n".join(lines[start:])


#: `(file, regex)` pairs whose capture group is a version this doc states as
#: CURRENT. Every pattern is anchored by neighbouring words unique to its line, and
#: each must match EXACTLY ONCE -- an unanchored `re.search` returns the first hit,
#: so a decoy mention earlier in the file would silently shadow the real badge and
#: the guard's protection would be coincidental rather than structural.
#:
#: The list covers all four current-version statements this release had to
#: hand-edit. That is a deliberate limit worth naming: this is *not* a
#: content-following sweep like the install-ref walk beside it, because "every
#: version-shaped token in every doc" also matches a large, legitimate historical
#: corpus (the CHANGELOG archive, dated decision records, two spec design notes),
#: and an allowlist of those would be the same hand-maintained list from the other
#: direction. So this guards the four sites a release must touch; a NEW
#: current-version claim added later is not covered until it is added here.
#: `kind` says what the captured number should EQUAL, because the four sites do
#: not all state the same thing: `exact` = this version; `minor` = its `MAJOR.MINOR`
#: floor (all a `>=X.Y,<Z` range asserts); `next_patch` = the hypothetical patch
#: after this one, which `docs/compatibility.md`'s "Moving to `v0.2.1` is an edit"
#: is illustrating rather than claiming. Collapsing that last one into `exact` is
#: the obvious mistake -- it fails immediately, but the tempting "fix" is to write
#: the current version into a sentence whose whole point is that it names a
#: DIFFERENT one.
_VERSION_BADGES = (
    # Ships inside the distribution -- `readme = "README.md"` makes this the
    # wheel/sdist `Description`, so a stale value here is on the package page.
    ("README.md", re.compile(r"version\n\*\*([0-9][^*]*)\*\*, store schema"), "exact"),
    ("README.md", re.compile(r"Releases are annotated tags \(`v([0-9][^`]*)`\)"), "exact"),
    # A copyable dependency pin: the most consequential of the four.
    (
        "docs/compatibility.md",
        re.compile(r"ontary>=([0-9]+\.[0-9]+),<[0-9]"),
        "minor",
    ),
    (
        "docs/compatibility.md",
        re.compile(r"Moving to\n`v([0-9][^`]*)` is an edit"),
        "next_patch",
    ),
)


def test_doc_version_badges_match_pyproject() -> None:
    """Docs that state "the current version" state THIS one.

    Distinct from `test_documented_install_ref_matches_the_current_version`, which
    only inspects lines carrying an install COMMAND. That is how this bug shipped:
    cutting `0.2.0` left `README.md`'s header advertising `0.1.0` while every
    install command beside it was correctly bumped, and no guard could see the
    difference -- in a release whose entire reason for existing was a doc pointing
    consumers at the wrong version.
    """
    version = str(_project()["version"])
    major, minor, patch = (int(part) for part in version.split("."))
    expected_for = {
        "exact": version,
        "minor": f"{major}.{minor}",
        "next_patch": f"{major}.{minor}.{patch + 1}",
    }
    root = PYPROJECT.parent
    for name, pattern, kind in _VERSION_BADGES:
        found = pattern.findall((root / name).read_text())
        assert len(found) == 1, (
            f"{name}: {pattern.pattern!r} matched {len(found)} times, expected "
            f"exactly 1 -- a second match means a decoy could shadow the real one"
        )
        assert found[0] == expected_for[kind], (
            f"{name} states version {found[0]!r} where pyproject {version!r} "
            f"implies {expected_for[kind]!r} ({kind})"
        )
