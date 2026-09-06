# Releasing

This runbook covers the one-time PyPI setup, the normal tag-triggered release, the
manual laptop path, and rollback. In every command below, `X.Y.Z` is the version
being released.

## One-time setup

`ontary` publishes to PyPI through [trusted publishing](https://docs.pypi.org/trusted-publishers/):
PyPI accepts an OpenID Connect token minted by GitHub Actions for one specific
workflow on one specific repository, so no API token is stored anywhere. Register
it once, on PyPI, as the project owner:

| Field | Value |
| --- | --- |
| PyPI project name | `ontary` |
| Owner | `ryoochi0112` |
| Repository name | `ontary` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

Then create the `pypi` environment in the repository's GitHub settings. Restrict
it to tags matching `v*`; a required reviewer is optional and adds a manual
approval before the publish job runs. Until both exist, the `publish` job fails at
its upload step and nothing reaches PyPI.

## Normal path

1. Bump `project.version` in `pyproject.toml` to `X.Y.Z`.
2. Update every current-version document that the packaging guards check:
   - In `README.md`, update the PyPI pin and the git-ref install line to `X.Y.Z`.
   - Add the `X.Y.Z` release entry to `CHANGELOG.md`, including the install ref in
     the newest release section.
   - Update the current compatibility range and its example in
     `docs/compatibility.md`.
   - Commit the upgrade fixture and frozen writer for the new tag, and extend the
     `upgrade-fixture-honesty` matrix, as [the v1 gate](v1-gate.md#release-checklist)
     requires.

   `make verify` fails if any of these versioned references is missed.
3. From the release commit, run the local gate and inspect the diff:

   ```bash
   uv sync --all-extras
   make verify
   git diff --check
   ```

4. Create an annotated tag and push that tag:

   ```bash
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git cat-file -t vX.Y.Z
   git push origin vX.Y.Z
   ```

   `git cat-file` should report `tag`, confirming that the release tag is
   annotated. Pushing `vX.Y.Z` is what triggers
   [`.github/workflows/release.yml`](../.github/workflows/release.yml). Its `gate`
   job checks the tag against `project.version`, then re-runs `make verify`, builds
   the distributions, and performs the clean-venv install smoke on the tagged ref.
   Only after that gate passes does the `publish` job upload the artifacts produced
   by the gate; it does not rebuild them.

   If the release run needs another attempt, use **Re-run failed jobs**, not
   **Re-run all jobs**. PyPI never accepts a filename twice, so a re-run that
   rebuilds would fail at upload even when the bytes are identical.
5. Confirm that the exact version resolves from PyPI in an empty environment:

   ```bash
   uv venv --python 3.12 /tmp/ontary-pypi-X.Y.Z
   VIRTUAL_ENV=/tmp/ontary-pypi-X.Y.Z uv pip install "ontary==X.Y.Z"
   /tmp/ontary-pypi-X.Y.Z/bin/python scripts/install_smoke.py
   ```

   PyPI's index can lag the upload by a minute; retry rather than re-publishing.
   The version page is `https://pypi.org/project/ontary/X.Y.Z/`.

## Manual path

Use this path only when GitHub Actions cannot run the release, for example during
an outage. It is a laptop publish authenticated with a PyPI API token scoped to the
`ontary` project, and it runs the same checks the workflow would run. It is not a
shortcut: the tagged ref must pass the full gate and the clean-venv install smoke
before `uv publish` is allowed.

Start in a clean worktree for the tag, not in a working checkout that may contain
release edits:

```bash
git fetch --tags origin
git worktree add --detach ../ontary-release-vX.Y.Z vX.Y.Z
cd ../ontary-release-vX.Y.Z
test "$(uv run --no-project python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')" = "X.Y.Z"
uv sync --all-extras
make verify
```

The version assertion reaches its interpreter through `uv run --no-project python`
rather than a bare `python`, and that is load-bearing rather than stylistic. macOS with
Homebrew provides `python3` and no `python`, so the bare form dies with
`command not found`; because it dies *inside* `test "$(...)"`, the line exits 1 and
reads as a tag/version mismatch, sending you after the wrong problem. There is no
`.venv` to fall back on either — `uv sync` is still one line away. `--no-project` is
what keeps uv from trying to resolve a project it has not synced yet.

Before building, require a clean status and treat a failure as a hard stop:

```bash
git status --short
test -z "$(git status --porcelain)"
```

The sdist includes untracked working-tree files. Building from a dirty tree can
therefore publish stray files and produce an sdist that no CI rebuild can match.
Do not continue until the status check is clean.

Build and run the same clean-environment smoke that the workflow runs. Smoke the
**sdist** as well as the wheel: both are published, and an sdist that fails to build
from source is invisible to a wheel-only check.

```bash
uv build --out-dir dist
uv venv --python 3.12 /tmp/ontary-clean-X.Y.Z
VIRTUAL_ENV=/tmp/ontary-clean-X.Y.Z uv pip install dist/*.whl
/tmp/ontary-clean-X.Y.Z/bin/python scripts/install_smoke.py

uv venv --python 3.12 /tmp/ontary-sdist-X.Y.Z
VIRTUAL_ENV=/tmp/ontary-sdist-X.Y.Z uv pip install --no-binary :all: dist/*.tar.gz
/tmp/ontary-sdist-X.Y.Z/bin/python scripts/install_smoke.py
```

Finally publish the already-verified distributions from the laptop. Create a
project-scoped API token on PyPI, hand it to uv through the environment for this
one command, and revoke it afterwards; never write it to a file in the repository:

```bash
UV_PUBLISH_TOKEN="$(cat)" uv publish dist/*
```

`cat` reads the token from standard input so it never lands in shell history. If
some files of this version are already on PyPI, pass only the missing ones rather
than `dist/*` — for example `dist/ontary-X.Y.Z.tar.gz` when the wheel already
landed. PyPI refuses a filename it already holds, so a differing rebuild cannot
silently replace published bytes; it fails, and the answer is to bump the version.

## Rollback

PyPI supports PEP 592 yank. Roll back by **yanking** the bad version on PyPI, then
bumping `project.version` and the required documentation references to a new
`X.Y.Z` release. Yank from the version's "Options" on
`https://pypi.org/manage/project/ontary/release/X.Y.Z/`, with a reason.

A yanked version stays installable for a consumer whose lockfile already pins it,
and stops being chosen by a resolver that was given a range. That is the property a
rollback needs: nobody's resolved environment changes underneath them, and nobody
new receives the bad release.

**Do not delete** the release, and **never re-publish** changed bytes under the same
version. Deletion breaks every lockfile that pins the version, and PyPI permanently
retires a deleted filename, so the version number is spent either way. Yank the bad
version and publish a new version instead.

See the [compatibility policy](compatibility.md), the [README install paths](../README.md#install),
and the [CHANGELOG](../CHANGELOG.md) when preparing the next release.
