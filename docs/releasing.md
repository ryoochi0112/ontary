# Releasing

[← README](../README.md) · [Changelog](../CHANGELOG.md)

Maintainer runbook. `X.Y.Z` is the version being released. Pushing an
annotated tag `vX.Y.Z` runs
[`release.yml`](../.github/workflows/release.yml): the `gate` job checks the
tag against `project.version`, runs `make verify`, builds the sdist and wheel,
installs the wheel into an empty venv and smoke-tests it; only then does the
`publish` job upload exactly those artifacts to PyPI through trusted publishing.

## One-time setup (already done for `ontary`)

PyPI trusted publisher: project `ontary`, owner `ryoochi0112`, repository
`ontary`, workflow `release.yml`, environment `pypi`. The `pypi` environment
exists in the repository settings and is restricted to `v*` tags. No API token
is stored anywhere.

Every `uses:` in the workflows is pinned to a commit SHA with a `# vX.Y.Z`
comment. Dependabot bumps the SHA and the comment together in a weekly PR;
never edit a `uses:` line by hand. The `workflows` job in `verify.yml` fails
the PR if a pin is missing.

## Cutting a release

1. On a branch, set `project.version` in `pyproject.toml` to `X.Y.Z`.
2. In `README.md`, update the version badge line (`version **X.Y.Z**, store
   schema **vN**`), the tag line (``annotated tags (`vX.Y.Z`)``), both
   `==X.Y.Z` pins under `## Install` (the `uv add` fence and the
   `pyproject.toml` sample), and the git-ref install fallback line (the one
   pinned to the release tag).
3. In `CHANGELOG.md`, rename `## [Unreleased]` to `## [X.Y.Z] — YYYY-MM-DD`,
   put the git-ref install fence (the `uv add "ontary @ git+…"` line from
   the previous release section, re-pinned to `vX.Y.Z`) under the heading,
   and open a fresh empty `## [Unreleased]` above it.
   `make verify` fails if any of these versioned references is missed.
4. `make verify`, open the PR, merge it.
5. Tag the merge commit and push the tag:

   ```bash
   git checkout main && git pull --ff-only
   git tag -a vX.Y.Z -m "Release vX.Y.Z"
   git cat-file -t vX.Y.Z    # must print: tag
   git push origin vX.Y.Z
   ```

6. Watch the `release` workflow. If a job fails, fix and use **Re-run failed
   jobs**, never **Re-run all jobs**: PyPI refuses a filename it has seen, so a
   rebuild fails at upload even when the bytes match.
7. Confirm from an empty environment:

   ```bash
   uv venv --python 3.12 /tmp/ontary-pypi-X.Y.Z
   VIRTUAL_ENV=/tmp/ontary-pypi-X.Y.Z uv pip install "ontary==X.Y.Z"
   /tmp/ontary-pypi-X.Y.Z/bin/python scripts/install_smoke.py
   ```

   PyPI's index can lag by a minute; retry rather than re-publish.

## If GitHub Actions cannot run

Build from a clean detached worktree of the tag (`uv build --out-dir dist`),
run `scripts/install_smoke.py` against the wheel in an empty venv, and publish
with `UV_PUBLISH_TOKEN` set to a project-scoped PyPI token that you revoke
afterwards. Never write the token to a file in the repository.
