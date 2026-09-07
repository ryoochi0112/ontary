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
from pathlib import Path, PurePosixPath

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
    inheriting an MCP server or a Postgres driver."""
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
    assert {"mcp", "postgres", "dev"} <= set(optional)


def test_metadata_carries_the_pointers_an_outside_consumer_needs() -> None:
    project = _project()
    assert project["readme"] == "README.md"
    urls = project["urls"]
    assert isinstance(urls, dict)
    assert {"Repository", "Changelog", "Documentation"} <= set(urls)
    for target in ("CHANGELOG.md", "docs/api-reference.md"):
        assert (PYPROJECT.parent / target).is_file(), (
            f"{target} is linked from package metadata but does not exist"
        )


def test_licensing_is_declared_coherently() -> None:
    """MIT, copyright Ryo Ochi.

    `ontary` continues `ontos`, which Atrae, Inc. authored as employee work and
    released to the author on 2026-09-06 for open-source development. The three
    facts that must agree with each other are checked here: the license
    expression, the license file, and the copyright holder. The holder is
    asserted because it is the one line in the distribution that makes a legal
    claim, and a "tidy-up" that restored the pre-fork holder would misstate who
    licenses this code.
    """
    project = _project()
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]

    license_text = (PYPROJECT.parent / "LICENSE").read_text()
    assert license_text.startswith("MIT License")
    assert "Copyright (c) 2026 Ryo Ochi" in license_text

    # PEP 639: the expression supersedes the old `License ::` trove
    # classifiers, and declaring both is an error in current build backends.
    classifiers = project["classifiers"]
    assert isinstance(classifiers, list)
    assert not any(c.startswith("License ::") for c in classifiers)
    assert "Private :: Do Not Upload" not in classifiers, (
        "ontary is published on PyPI -- putting the interlock back means a "
        "deliberate decision to stop distributing, not a lint fix"
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

    PyPI is the primary distribution, but the **pinned git ref** stays
    documented as the index-free fallback, and that command is load-bearing in
    a way a `pip install ontary` line is not: there is no resolver to correct
    it. A `0.2.0` release whose README still says `@v0.1.0` would send every
    consumer on that path to the old code and look like it worked.

    Walks **every** `*.md` in the repo, not a hand-maintained file list. The
    list used to be a hand-maintained trio of README/CHANGELOG/compatibility, and
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
    found = _walk_doc_install_refs(PYPROJECT.parent)
    for name, refs in found.items():
        for line in refs:
            assert version_tag in line, (
                f"{name} pins a stale tag: {line.strip()!r} does not name "
                f"{version_tag!r}"
            )
    missing = sorted(set(_REQUIRED_REFS) - set(found))
    assert not missing, f"no longer documents the git install ref: {missing}"


def _walk_doc_install_refs(root: Path) -> dict[str, list[str]]:
    """Every doc under `root` carrying an install ref, keyed by relative path.

    Split out of the test so the walk's SCOPE is drivable against a fixture
    root: which directories it refuses to police is a property in its own
    right, and pointing the real walk at the real repo can only ever observe
    the tree that happens to be on disk.
    """
    found: dict[str, list[str]] = {}
    candidates = sorted(p for suffix in _DOC_SUFFIXES for p in root.rglob(f"*{suffix}"))
    for path in candidates:
        rel = path.relative_to(root)
        # Matched against `rel`, never the absolute path. A Claude Code
        # worktree IS `<repo>/.claude/worktrees/<name>`, so the absolute form
        # skipped EVERY candidate the moment `make verify` ran from inside
        # one -- `.claude` was in the road to the root, not below it.
        parts = rel.parts
        if any(part in _SKIP_DIRS for part in parts):
            continue
        if any(
            parts[i : i + len(pair)] == pair
            for pair in _SKIP_SUBPATHS
            for i in range(len(parts))
        ):
            continue
        name = rel.as_posix()
        text = path.read_text()
        # Keyed on the FILENAME, not the whole relative path: a changelog is an
        # archive wherever it sits, and `wt/CHANGELOG.md` in a nested checkout
        # was otherwise read as a current-state doc, turning its true history of
        # past releases into drift.
        if PurePosixPath(name).name == "CHANGELOG.md":
            text = _newest_release_section(text)
        refs = [line for line in text.splitlines() if _INSTALL_REF in line]
        if refs:
            found[name] = refs
    return found


def test_install_ref_walk_refuses_to_police_worktrees_under_dot_claude(
    tmp_path: Path,
) -> None:
    """A git worktree under `.claude/worktrees/` is not repo content.

    That is where Claude Code puts a nested SECOND checkout of this repo. The
    walk used to descend into it, so every historical `@v0.10.0` in that copy's
    changelog read as a stale tag and `make verify` went red on a tree whose own
    docs were correct. This arm pins the skip.

    SCOPE, measured -- the skip names ONE path, so a worktree created anywhere
    ELSE inside the repo still reds the guard: `git worktree add ./wt-probe
    v0.10.0` then `make verify` exits 2 on `wt-probe/CHANGELOG.md`. The archive
    exemption does not rescue that one either, because a checkout of another
    version has a NEWEST section naming that other version. Deferred
    deliberately: our tooling only ever nests under `.claude/worktrees/`, and the
    residual is a false red, never a missed stale tag. Closing it needs a general
    nested-checkout probe -- a `.git` entry in some ancestor below the root --
    not another path name.

    A second, independent cause is pinned by
    `test_changelog_archive_exemption_follows_the_filename`: the
    newest-release-section narrowing was keyed on the relative path being exactly
    `CHANGELOG.md`, which a nested copy never is, so it lost the archive
    exemption and was checked whole.
    """
    (tmp_path / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [0.11.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"\n'
        "\n## [0.10.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"\n'
    )
    nested = tmp_path / ".claude" / "worktrees" / "some-worktree"
    nested.mkdir(parents=True)
    (nested / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [0.11.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"\n'
        "\n## [0.10.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"\n'
    )
    (nested / "README.md").write_text(
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.9.0"\n'
    )

    found = _walk_doc_install_refs(tmp_path)

    assert set(found) == {"CHANGELOG.md"}, (
        "the walk policed a nested worktree: "
        f"{sorted(set(found) - {'CHANGELOG.md'})}"
    )
    assert found["CHANGELOG.md"] == [
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"'
    ]


def test_install_ref_walk_polices_a_root_that_itself_sits_under_a_skipped_dir(
    tmp_path: Path,
) -> None:
    """`_SKIP_DIRS` names directories BELOW the root, not the road to it.

    The sibling test above is why this one exists. A Claude Code worktree IS
    `<repo>/.claude/worktrees/<name>`, so when `make verify` runs inside one,
    the walk's own ROOT carries `.claude` in its absolute parts. Matching
    `_SKIP_DIRS` against the absolute path then skipped every candidate,
    `found` came back empty, and the required-refs arm failed with
    `no longer documents the git install ref: ['CHANGELOG.md', 'README.md']`
    -- the guard reporting that the repo had dropped a command it was in fact
    still carrying. The exemption added to stop the walk policing a nested
    worktree stopped it policing ANYTHING from inside one.

    The fixture root is built under `.claude/worktrees/` on purpose: that path
    shape is the whole bug, and a fixture rooted anywhere else walks straight
    past it. Matching relative to `root` is what keeps both properties at
    once -- skip a `.claude` BENEATH the tree, ignore one ABOVE it.
    """
    root = tmp_path / ".claude" / "worktrees" / "some-worktree"
    root.mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(
        "## [Unreleased]\n\n## [0.11.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"\n'
        "\n## [0.10.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"\n'
    )
    (root / "README.md").write_text(
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"\n'
    )

    found = _walk_doc_install_refs(root)

    assert set(found) == {"CHANGELOG.md", "README.md"}, (
        "the walk skipped the tree it was pointed at, because the path TO that "
        f"root contains a _SKIP_DIRS name: found {sorted(found)}"
    )
    assert found["CHANGELOG.md"] == [
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"'
    ]


def test_changelog_archive_exemption_follows_the_filename(tmp_path: Path) -> None:
    """A changelog is an archive wherever it sits, not only at the repo root.

    The second cause of the nested-checkout bug, pinned on its own. The
    narrowing compared the whole relative path to `CHANGELOG.md`, so
    `wt/CHANGELOG.md` was read as a current-state doc and its true history of
    `@v0.10.0` releases read as drift. `git worktree add` takes any
    destination, so skipping `.claude/worktrees` does not reach this -- the two
    fixes have to stay independent, and this fixture is deliberately nested
    OUTSIDE `.claude` to keep them so.
    """
    nested = tmp_path / "wt"
    nested.mkdir()
    for target in (tmp_path / "CHANGELOG.md", nested / "CHANGELOG.md"):
        target.write_text(
            "## [Unreleased]\n\n## [0.11.0]\n"
            'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"\n'
            "\n## [0.10.0]\n"
            'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.10.0"\n'
        )

    found = _walk_doc_install_refs(tmp_path)

    current = ['uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.11.0"']
    assert found["wt/CHANGELOG.md"] == current, (
        f"a nested changelog lost its archive exemption: {found['wt/CHANGELOG.md']}"
    )
    assert found["CHANGELOG.md"] == current


def test_install_ref_walk_still_polices_committed_claude_assets(
    tmp_path: Path,
) -> None:
    """Skipping `.claude` wholesale would blind the guard to tracked docs.

    `.claude` is NOT git-excluded -- only `.claude/worktrees/` is, via
    `.git/info/exclude` -- and `.claude/skills`, `.claude/commands` and
    `.claude/agents` are conventionally committed. A stale install ref in one of
    those ships to readers, so the skip has to be the `.claude/worktrees` PAIR
    and never the parent directory. Skipping the parent is exactly the
    "passing by absence" hole `_REQUIRED_REFS` exists to close.
    """
    skill = tmp_path / ".claude" / "skills" / "install"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.9.0"\n'
    )

    found = _walk_doc_install_refs(tmp_path)

    assert ".claude/skills/install/SKILL.md" in found, (
        "a committed .claude asset escaped the walk; the skip must be the "
        ".claude/worktrees pair, not the .claude directory"
    )


def test_install_ref_walk_scope_survives_the_clone_location(tmp_path: Path) -> None:
    """The guard must not be disabled by where the repo happens to be cloned.

    Guards the single-component half of the skip. Once `.claude` moves out of
    `_SKIP_DIRS`, the sibling test above exercises only the PAIR, leaving
    `_SKIP_DIRS` itself unpinned: matching it against the ABSOLUTE path lets a
    checkout under any directory named `build`, `dist`, `site` (or a
    `~/specs/ontary`) match a skip entry for EVERY file.

    Measured, that fails LOUDLY rather than passing blind -- `_REQUIRED_REFS`
    then reports both canonical docs missing ("no longer documents the git
    install ref: ['CHANGELOG.md', 'README.md']"), which is the backstop doing
    its job. Saying otherwise would tell a future maintainer the backstop does
    not cover the skip list, inviting its deletion as redundant. The bug is a
    guard that reddens on a CORRECT tree because of where it was cloned.
    """
    root = tmp_path / "build" / "ontary"
    root.mkdir(parents=True)
    (root / "CHANGELOG.md").write_text(
        "## [0.11.0]\n"
        'uv add "ontary @ git+https://github.com/ryoochi0112/ontary@v0.9.0"\n'
    )

    found = _walk_doc_install_refs(root)

    assert "CHANGELOG.md" in found, (
        "the clone's own path disabled the guard; skips must be scoped to the "
        "path relative to the walk root"
    )


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
#: silently failing the walk. `site` is the MkDocs build output written by `make docs-build`.
#: `specs` holds design notes and implementation plans: they quote install refs
#: as placeholders and historical examples, and no reader installs from them.
#: Matched against the path RELATIVE to the walk root. An absolute match lets the
#: clone's own location decide: checking this repo out under any directory with a
#: component named `build` or `dist` skips every file, and the guard then reddens
#: through the `_REQUIRED_REFS` backstop on a tree whose docs were fine.
_SKIP_DIRS = frozenset(
    {
        ".venv",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        "node_modules",
        "build",
        "dist",
        "site",
        "specs",
    }
)

#: Skipped only as an adjacent PAIR, never as single components. `.claude` alone
#: would be wrong: it is NOT git-excluded (only `.claude/worktrees/` is, via
#: `.git/info/exclude`), and `.claude/skills`, `.claude/commands` and
#: `.claude/agents` are conventionally COMMITTED -- skipping the parent would let
#: a tracked doc carrying a stale install ref ship unpoliced, the exact
#: "passing by absence" hole `_REQUIRED_REFS` exists to close.
#: `.claude/worktrees` is where `git worktree add` puts a nested SECOND CHECKOUT
#: of this repo. It cannot hold repo content, and without this the walk descends
#: into that checkout and reads its changelog archive as drift -- `make verify`
#: went red while the tree's own docs were fine. Scope: this covers worktrees
#: under `.claude/worktrees` ONLY; one created elsewhere in the repo still reds
#: the guard (measured). See the nested-worktree test for why that is deferred.
_SKIP_SUBPATHS: tuple[tuple[str, ...], ...] = ((".claude", "worktrees"),)

#: Docs that must ALWAYS carry the install command. Without this, a canonical
#: file that drops the command entirely just stops being walked -- passing by
#: absence.
#: `docs/compatibility.md` left this list when it became a one-paragraph pre-1.0
#: policy that states no version and carries no install command.
_REQUIRED_REFS = (
    "README.md",
    "CHANGELOG.md",
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
#: The list covers every current-version statement this release has to
#: hand-edit. That is a deliberate limit worth naming: this is *not* a
#: content-following sweep like the install-ref walk beside it, because "every
#: version-shaped token in every doc" also matches a large, legitimate historical
#: corpus (the CHANGELOG archive, dated decision records, two spec design notes),
#: and an allowlist of those would be the same hand-maintained list from the other
#: direction. So this guards the four sites a release must touch; a NEW
#: current-version claim added later is not covered until it is added here.
#: `kind` says what the captured number should EQUAL, because the sites need not
#: all state the same thing: `exact` = this version; `minor` = its `MAJOR.MINOR`
#: floor (all a `>=X.Y,<Z` range asserts); `next_patch` = the hypothetical patch
#: after this one, illustrated rather than claimed. The two
#: `docs/compatibility.md` entries left this list when that page was cut to a
#: one-paragraph pre-1.0 policy that states no version at all -- the right fix if
#: it ever states one again is to add it back here, not to hand-edit prose.
_VERSION_BADGES = (
    # Ships inside the distribution -- `readme = "README.md"` makes this the
    # wheel/sdist `Description`, so a stale value here is on the package page.
    ("README.md", re.compile(r"version\n\*\*([0-9][^*]*)\*\*, store schema"), "exact"),
    ("README.md", re.compile(r"Releases are annotated tags \(`v([0-9][^`]*)`\)"), "exact"),
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


#: Every fenced TOML sample under `## Install` is parsed, then the one that pins
#: `ontary` as a project dependency is inspected. Anchoring to the fence and then
#: to a parsed dependency means a decoy mention in prose cannot shadow the real pin.
_README_TOML_FENCE = re.compile(r"(?ms)^```toml[ \t]*\n(.*?)^```[ \t]*$")
_README_BASH_FENCE = re.compile(r"(?ms)^```bash[ \t]*\n(?P<body>.*?)^```[ \t]*$")
_README_ONTARY_PIN = re.compile(
    r"^ontary(?:\[[^]]+\])?\s*==\s*"
    r"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)(?:\s*;.*)?$"
)
#: Fragments of the pre-fork private-index install path. None may survive in the
#: README: a public consumer following them would hit a 401 on an index they were
#: never meant to see.
_PRIVATE_INDEX_MARKERS = (
    "pkg.dev",
    "oauth2accesstoken",
    "gcloud",
    "keyring",
    "explicit = true",
    "git+ssh://",
)


def _readme_install_section() -> str:
    readme = (PYPROJECT.parent / "README.md").read_text()
    install_match = re.search(
        r"(?ms)^## Install[ \t]*\n(?P<body>.*?)(?=^## [^#]|\Z)", readme
    )
    assert install_match is not None, "README.md: missing ## Install section"
    return install_match.group("body")


def _readme_pinned_versions(text: str) -> list[str]:
    """Every exact `ontary==X.Y.Z` pin in `text`, from TOML dependencies and
    from bash `uv add` lines alike."""
    versions: list[str] = []
    for block in _README_TOML_FENCE.findall(text):
        document = tomllib.loads(block)
        project = document.get("project")
        dependencies = project.get("dependencies") if isinstance(project, dict) else None
        if not isinstance(dependencies, list):
            continue
        for dependency in dependencies:
            if isinstance(dependency, str):
                match = _README_ONTARY_PIN.fullmatch(dependency.strip())
                if match is not None:
                    versions.append(match.group("version"))
    for match in _README_BASH_FENCE.finditer(text):
        for line in match.group("body").splitlines():
            add_match = re.fullmatch(r'\s*uv add "(?P<req>[^"]+)"\s*', line)
            if add_match is None:
                continue
            pin = _README_ONTARY_PIN.fullmatch(add_match.group("req").strip())
            if pin is not None:
                versions.append(pin.group("version"))
    return versions


def test_readme_install_pins_match_pyproject() -> None:
    """Both copyable PyPI pins under `## Install` name this release.

    The `uv add` line and the `pyproject.toml` sample are what a new consumer
    copies; a release whose README still pins the previous version would install
    the old code and look like it worked. Exactly one of each is required so a
    decoy cannot stand in for the real one.
    """
    versions = _readme_pinned_versions(_readme_install_section())
    assert len(versions) == 2, (
        f"README.md: expected exactly one TOML pin and one `uv add` pin for ontary "
        f"under ## Install, found {len(versions)}"
    )
    expected_version = str(_project()["version"])
    assert set(versions) == {expected_version}, (
        f"README.md: Install pins {sorted(set(versions))}, pyproject names "
        f"{expected_version}"
    )


def test_readme_install_has_no_private_index_path() -> None:
    """The README describes public PyPI only.

    `ontary` was forked from a package served by a private, credential-gated
    index. None of that path may survive in a public README: a consumer copying
    an `oauth2accesstoken@...pkg.dev` index or a `gcloud` login would fail on
    credentials they cannot obtain, and a `git+ssh://` ref demands a GitHub SSH
    key for a repository that is readable over HTTPS.
    """
    readme = (PYPROJECT.parent / "README.md").read_text()
    for marker in _PRIVATE_INDEX_MARKERS:
        assert marker not in readme, (
            f"README.md still carries the private-index install path: {marker!r}"
        )
    install = _readme_install_section()
    assert "pypi" in install.lower(), "README.md: ## Install must name PyPI"
    assert "git+https://github.com/ryoochi0112/ontary@v" in install, (
        "README.md: ## Install must keep the public git-ref fallback"
    )
