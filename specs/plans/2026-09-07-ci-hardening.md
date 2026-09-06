# CI Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing CI gate enforce (required checks, SHA-pinned and least-privilege workflows), test the Python versions the package claims, and put dependency and vulnerability scanning in place.

**Architecture:** Three existing workflows (`verify`, `docs`, `release`) are hardened in place. One new workflow (`codeql`) is added. Dependabot replaces the never-installed Renovate config. A `SECURITY.md` and changelog entries land in the same PR. Repository settings are flipped after merge, in a fixed order, because SHA pinning must be on `main` before it is enforced.

**Tech Stack:** GitHub Actions, uv (`astral-sh/setup-uv`), zizmor 1.30.0 (workflow linter), CodeQL, Dependabot, `gh` CLI for settings.

**Spec:** [`specs/2026-09-07-ci-hardening-design.md`](../2026-09-07-ci-hardening-design.md)

## Global Constraints

- Branch: `ci/hardening` (already exists, holds the spec commit). `main` is protected; everything lands via one PR.
- Every `uses:` must be a full 40-character commit SHA followed by ` # vX.Y.Z`. Resolved on 2026-09-07:

  | Action | Version | SHA |
  |---|---|---|
  | `actions/checkout` | v7.0.1 | `3d3c42e5aac5ba805825da76410c181273ba90b1` |
  | `astral-sh/setup-uv` | v10.0.1 | `20cfd1bf945f4377ade1205e4dbc17946fc9a30d` |
  | `actions/upload-pages-artifact` | v5.0.0 | `fc324d3547104276b827a68afc52ff2a11cc49c9` |
  | `actions/deploy-pages` | v5.0.1 | `368f82528645a54fb793d4d04e342629a3f51346` |
  | `actions/upload-artifact` | v7.0.1 | `043fb46d1a93c77aae656e7c1c64a875d1fc6a0a` |
  | `actions/download-artifact` | v8.0.1 | `3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c` |
  | `pypa/gh-action-pypi-publish` | v1.14.2 | `dc37677b2e1c63e2034f94d8a5b11f265b73ba33` |
  | `github/codeql-action/*` | v4.37.9 | `cdf488f595d80d6e07e03d4674febd5ab45fa938` |

- Every workflow has top-level `permissions: contents: read`. Every job has `timeout-minutes`. Every `actions/checkout` has `persist-credentials: false`.
- No job logic changes except one: `release.yml`'s `gate` job sets `enable-cache: false` on `setup-uv`. zizmor flags `enable-cache: true` in a tag-triggered publishing workflow as `cache-poisoning` (a poisoned uv cache could alter the published wheel). This is a deliberate deviation from the spec's "no job logic changes" and is recorded in the CHANGELOG.
- zizmor version is pinned to `1.30.0` in the workflow. Dependabot does not track `uvx` invocations; bumping zizmor is a manual edit.
- `make verify` is the offline definition of done and must stay green.
- Workflow files are validated locally with `uvx zizmor==1.30.0 <file>` (lint) and `uv run python -c "import yaml; yaml.safe_load(open('<file>'))"` (parse). PyYAML is transitively available in the `docs` group; if the import fails, run the parse check as `uv run --group docs python -c ...`.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.

---

### Task 1: Harden the three existing workflows

**Files:**
- Modify: `.github/workflows/verify.yml`
- Modify: `.github/workflows/docs.yml`
- Modify: `.github/workflows/release.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: SHA-pinned, least-privilege workflow files that Task 2 extends. Task 2 depends on the exact `verify.yml` text below.

- [ ] **Step 1: Record the zizmor baseline (the failing test)**

Run:
```bash
uvx zizmor==1.30.0 .github/ 2>&1 | tail -1
```
Expected: a summary line ending in findings, e.g. `41 findings (11 suppressed, 6 unsafe fixes): 0 informational, 5 low, 8 medium, 17 high`. Categories present: `unpinned-uses`, `excessive-permissions`, `artipacked`, `cache-poisoning`.

- [ ] **Step 2: Rewrite `verify.yml`**

Replace the file's entire contents with:

```yaml
name: verify

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

# A new push to a PR branch supersedes the run in flight. On main every push
# runs to completion so the green/red history stays honest.
concurrency:
  group: verify-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
      - name: Install dependencies
        run: uv sync --all-extras
      - name: make verify
        run: make verify

  # The Postgres backend's real gate (M8a). `verify` above runs the conformance
  # suite against SQLite and in-memory only, because `make verify` is defined as
  # offline and no-DB and breaking that would make the gate unrunnable on a
  # laptop. So the third backend gets a job with a real server: same suite, same
  # assertions, one more `STORE_FACTORIES` entry. Without this, `PostgresStore`
  # would be exactly the "nothing ever tested this" hole M6 found in packaging.
  postgres:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: postgres
        ports:
          - 5432:5432
        options: >-
          --health-cmd pg_isready
          --health-interval 10s
          --health-timeout 5s
          --health-retries 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
      - name: Install dependencies
        run: uv sync --all-extras
      - name: Conformance suite + backend tests against Postgres
        env:
          ONTARY_TEST_POSTGRES_DSN: postgresql://postgres:postgres@localhost:5432/postgres
        run: |
          uv run pytest tests/test_store_conformance.py tests/test_store_postgres.py \
            tests/test_multi_tenancy.py tests/test_actions.py \
            tests/test_examples_tickets_e2e.py -q

  # The check that had never existed (M6 step 3) -- now the stranger test too
  # (OSS v0 spec § 4.3). `verify` above runs from the source tree with every
  # extra installed -- precisely the environment an outside adopter does NOT
  # have. Their first command is `pip install ontary`, and nothing verified
  # that path until a manual clean-venv install turned up `ontary-mcp` dying
  # on a bare `ModuleNotFoundError`. The first venv below is deliberately
  # no-extras and no dev dependencies, so `scripts/install_smoke.py` checks
  # the bare `pip install ontary` path (which is why it is a plain script,
  # not a test module). The second venv installs the `mcp` extra, so
  # `scripts/stranger_smoke.py` can run the README quickstart and the
  # tickets example exactly as a reader would (OSS v0 spec § 4.3).
  package:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
      - name: Build sdist + wheel
        run: uv build --out-dir dist
      - name: Install the wheel into an empty environment
        run: |
          uv venv --python 3.12 /tmp/clean
          VIRTUAL_ENV=/tmp/clean uv pip install dist/*.whl
      - name: Smoke-test the installed package (no extras)
        run: /tmp/clean/bin/python scripts/install_smoke.py
      # The stranger test (OSS v0 spec § 4.3): the README's own install line,
      # `pip install "ontary[mcp]"`, into a second empty venv, then the README
      # quickstart and the tickets example exactly as a reader would run them.
      - name: Install the wheel with the mcp extra into a second empty environment
        run: |
          uv venv --python 3.12 /tmp/stranger
          VIRTUAL_ENV=/tmp/stranger uv pip install "$(ls dist/*.whl)[mcp]"
      - name: Stranger test — README quickstart and tickets example from the wheel
        run: /tmp/stranger/bin/python scripts/stranger_smoke.py
      - name: Upload the distributions
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: dist
          path: dist/
```

- [ ] **Step 3: Rewrite `docs.yml`**

Replace the file's entire contents with:

```yaml
name: docs

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read

# Never cancel: a Pages deploy interrupted mid-flight is worse than a stale run.
concurrency:
  group: docs-${{ github.ref }}
  cancel-in-progress: false

jobs:
  # Runs on every PR too, so a broken link or a missing nav page blocks the
  # merge instead of breaking the live site.
  build:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
      - name: Install the docs group
        run: uv sync --group docs
      - name: Build the site (strict)
        run: make docs-build
      - name: Upload the site as the Pages artifact
        if: github.event_name == 'push'
        uses: actions/upload-pages-artifact@fc324d3547104276b827a68afc52ff2a11cc49c9 # v5.0.0
        with:
          path: site/

  deploy:
    if: github.event_name == 'push'
    needs: build
    runs-on: ubuntu-latest
    timeout-minutes: 10
    permissions:
      pages: write
      id-token: write
    environment:
      name: github-pages
      url: ${{ steps.deployment.outputs.page_url }}
    steps:
      - id: deployment
        uses: actions/deploy-pages@368f82528645a54fb793d4d04e342629a3f51346 # v5.0.1
```

- [ ] **Step 4: Rewrite `release.yml`**

Replace the file's entire contents with:

```yaml
name: release

on:
  push:
    tags: ['v*']

permissions:
  contents: read

concurrency:
  group: release-${{ github.ref_name }}
  cancel-in-progress: false

jobs:
  gate:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      # No uv cache here: this job builds the bytes that `publish` uploads, and
      # a cache shared with PR runs is a way to smuggle a different dependency
      # into the release build (zizmor `cache-poisoning`). A cold resolve costs
      # under a minute per release.
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: false
      - name: Check tag matches project version
        run: |
          tag_version="${GITHUB_REF_NAME#v}"
          project_version="$(python -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')"
          if [ "$tag_version" != "$project_version" ]; then
            echo "Tag version $tag_version does not match project version $project_version"
            exit 1
          fi
      - name: Install dependencies
        run: uv sync --all-extras
      - name: make verify
        run: make verify
      - name: Build sdist + wheel
        run: uv build --out-dir dist
      - name: Install the wheel into an empty environment
        run: |
          uv venv --python 3.12 /tmp/clean
          VIRTUAL_ENV=/tmp/clean uv pip install dist/*.whl
      - name: Smoke-test the installed package
        run: /tmp/clean/bin/python scripts/install_smoke.py
      - name: Upload the distributions
        uses: actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a # v7.0.1
        with:
          name: dist
          path: dist/

  publish:
    # The only job that can reach PyPI. It holds `id-token: write` so PyPI's
    # trusted publishing can mint a short-lived token from the OIDC identity of
    # THIS workflow on THIS repository (registered once on PyPI as a trusted
    # publisher). No long-lived API token is stored anywhere. It publishes
    # exactly the bytes `gate` built and smoke-tested; it never rebuilds.
    needs: gate
    runs-on: ubuntu-latest
    timeout-minutes: 10
    environment: pypi
    permissions:
      id-token: write
      contents: read
    steps:
      - name: Download the distributions
        uses: actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c # v8.0.1
        with:
          name: dist
          path: dist/
      - name: Publish the distributions to PyPI
        uses: pypa/gh-action-pypi-publish@dc37677b2e1c63e2034f94d8a5b11f265b73ba33 # v1.14.2
        with:
          packages-dir: dist/
```

- [ ] **Step 5: Verify zizmor is clean and the YAML parses**

Run:
```bash
uvx zizmor==1.30.0 .github/ 2>&1 | tail -1
for f in .github/workflows/*.yml; do uv run --group docs python -c "import yaml,sys; yaml.safe_load(open('$f')); print('ok', '$f')"; done
```
Expected: zizmor prints `No findings to report. Good job!` (or a summary with `0 informational, 0 low, 0 medium, 0 high`); each file prints `ok`. If zizmor still reports anything, fix the workflow, do not add a suppression.

- [ ] **Step 6: Confirm the pinned SHAs are the tags they claim**

Run:
```bash
for pair in actions/checkout:v7.0.1 astral-sh/setup-uv:v10.0.1 actions/upload-pages-artifact:v5.0.0 actions/deploy-pages:v5.0.1 actions/upload-artifact:v7.0.1 actions/download-artifact:v8.0.1 pypa/gh-action-pypi-publish:v1.14.2; do
  repo=${pair%%:*}; tag=${pair##*:}
  sha=$(gh api "repos/$repo/git/ref/tags/$tag" --jq '.object.sha'); type=$(gh api "repos/$repo/git/ref/tags/$tag" --jq '.object.type')
  [ "$type" = "tag" ] && sha=$(gh api "repos/$repo/git/tags/$sha" --jq '.object.sha')
  grep -q "$repo@$sha # $tag" .github/workflows/*.yml && echo "ok $repo $tag" || echo "MISMATCH $repo $tag expected $sha"
done
```
Expected: seven `ok` lines, zero `MISMATCH`.

- [ ] **Step 7: Commit**

```bash
git add .github/workflows/verify.yml .github/workflows/docs.yml .github/workflows/release.yml
git commit -m "ci: SHA-pin every action, declare read-only permissions, add timeouts and PR-only cancellation

Also disables the uv cache in the release gate: the job builds the bytes that
publish uploads, and a cache shared with PR runs is a cache-poisoning surface
(zizmor). zizmor 1.30.0 baseline went from 41 findings to 0.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Python matrix and the workflow-lint job

**Files:**
- Modify: `.github/workflows/verify.yml` (the `verify` job and a new `workflows` job)

**Interfaces:**
- Consumes: the `verify.yml` written in Task 1.
- Produces: check contexts `verify (3.12)`, `verify (3.13)`, `workflows`. Task 8 lists these as required status checks by exactly these names.

- [ ] **Step 1: Edit the `verify` job into a matrix**

In `.github/workflows/verify.yml`, replace this block:

```yaml
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: true
```

with:

```yaml
  # One leg per interpreter the classifiers claim. `postgres` and `package`
  # below stay on 3.12: they test a backend and the distribution, not the
  # interpreter. mypy's typing target stays `python_version = "3.12"`.
  verify:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    strategy:
      fail-fast: false
      matrix:
        python: ["3.12", "3.13"]
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: ${{ matrix.python }}
          enable-cache: true
```

- [ ] **Step 2: Append the `workflows` job**

At the end of `.github/workflows/verify.yml`, after the `package` job's final `path: dist/` line, append:

```yaml

  # Lints the workflows themselves: an unpinned `uses:`, a checkout that keeps
  # its credentials, a template-injection pattern, a publish job with a shared
  # cache. zizmor is pinned because a new audit in a new release would
  # otherwise turn an unrelated PR red. `GH_TOKEN` lets the online audits
  # (known-vulnerable-actions, impostor-commit) run; `contents: read` is enough.
  workflows:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d # v10.0.1
        with:
          python-version: "3.12"
          enable-cache: false
      - name: Lint the workflows with zizmor
        env:
          GH_TOKEN: ${{ github.token }}
        run: uvx zizmor==1.30.0 .github/
```

- [ ] **Step 3: Verify locally**

Run:
```bash
uvx zizmor==1.30.0 .github/ 2>&1 | tail -1
uv run --group docs python -c "import yaml; d=yaml.safe_load(open('.github/workflows/verify.yml')); print(sorted(d['jobs'])); print(d['jobs']['verify']['strategy']['matrix']['python'])"
```
Expected: zizmor clean; `['package', 'postgres', 'verify', 'workflows']`; `['3.12', '3.13']`.

- [ ] **Step 4: Confirm 3.13 resolves locally (cheap pre-check of the new leg)**

Run:
```bash
uv python find 3.13 >/dev/null 2>&1 || uv python install 3.13
UV_PROJECT_ENVIRONMENT=/tmp/ontary-py313 uv sync --all-extras --python 3.13 && UV_PROJECT_ENVIRONMENT=/tmp/ontary-py313 uv run --python 3.13 pytest -q -x 2>&1 | tail -1
```
Expected: last line `N passed, M skipped ...` with zero failures. If a dependency has no 3.13 wheel or a test fails, stop and report; do not drop 3.13 from the matrix silently.

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/verify.yml
git commit -m "ci: test on 3.12 and 3.13; lint the workflows with zizmor on every run

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: CodeQL workflow

**Files:**
- Create: `.github/workflows/codeql.yml`

**Interfaces:**
- Consumes: nothing.
- Produces: check context `analyze (python)`. Not a required check.

- [ ] **Step 1: Confirm CodeQL default setup is off (it conflicts with an advanced workflow)**

Run:
```bash
gh api repos/ryoochi0112/ontary/code-scanning/default-setup --jq .state
```
Expected: `not-configured`. If it prints `configured`, stop and report; the two modes cannot coexist.

- [ ] **Step 2: Write the workflow**

Create `.github/workflows/codeql.yml`:

```yaml
name: codeql

on:
  pull_request:
  push:
    branches: [main]
  schedule:
    # Mondays 06:17 UTC. Off the hour so it does not queue behind everyone
    # else's Monday-morning cron.
    - cron: '17 6 * * 1'

permissions:
  contents: read

concurrency:
  group: codeql-${{ github.ref }}
  cancel-in-progress: ${{ github.event_name == 'pull_request' }}

jobs:
  analyze:
    name: analyze (python)
    runs-on: ubuntu-latest
    timeout-minutes: 20
    permissions:
      security-events: write
      contents: read
      actions: read
    steps:
      - uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1 # v7.0.1
        with:
          persist-credentials: false
      - uses: github/codeql-action/init@cdf488f595d80d6e07e03d4674febd5ab45fa938 # v4.37.9
        with:
          languages: python
          build-mode: none
      - uses: github/codeql-action/analyze@cdf488f595d80d6e07e03d4674febd5ab45fa938 # v4.37.9
        with:
          category: "/language:python"
```

- [ ] **Step 3: Verify locally**

Run:
```bash
uvx zizmor==1.30.0 .github/ 2>&1 | tail -1
uv run --group docs python -c "import yaml; d=yaml.safe_load(open('.github/workflows/codeql.yml')); print(d['jobs']['analyze']['name'], d['jobs']['analyze']['permissions'])"
sha=$(gh api repos/github/codeql-action/git/ref/tags/v4.37.9 --jq '.object.sha'); type=$(gh api repos/github/codeql-action/git/ref/tags/v4.37.9 --jq '.object.type'); [ "$type" = "tag" ] && sha=$(gh api "repos/github/codeql-action/git/tags/$sha" --jq '.object.sha'); grep -c "codeql-action/.*@$sha # v4.37.9" .github/workflows/codeql.yml
```
Expected: zizmor clean; `analyze (python) {'security-events': 'write', 'contents': 'read', 'actions': 'read'}`; `2`.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/codeql.yml
git commit -m "ci: CodeQL for Python on PRs, main, and a weekly schedule

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Dependabot replaces Renovate

**Files:**
- Create: `.github/dependabot.yml`
- Delete: `renovate.json`

**Interfaces:**
- Consumes: the ` # vX.Y.Z` comments Task 1 put on every `uses:` line (Dependabot updates SHA and comment together).
- Produces: nothing other tasks consume.

- [ ] **Step 1: Confirm nothing else references Renovate**

Run:
```bash
grep -rn -i renovate --include='*.md' --include='*.yml' --include='*.yaml' --include='*.toml' --include='*.py' . | grep -v '^./.venv' | grep -v '^./renovate.json' | grep -v '^./specs/'
```
Expected: no output. (The spec and this plan mention Renovate on purpose.)

- [ ] **Step 2: Write `dependabot.yml`**

Create `.github/dependabot.yml`:

```yaml
# Replaces renovate.json (removed 2026-09-07): the Renovate app was never
# installed on this repository, so that config had been silently dead.
# Nothing is automerged. A lockfile rewrite is read by a human before it lands
# on main; uv.lock stays the single source of truth for pinned versions.
version: 2
updates:
  - package-ecosystem: uv
    directory: /
    schedule:
      interval: weekly
      day: monday
    groups:
      # One PR for every minor and patch bump. Majors match no group, so each
      # opens its own PR and gets read on its own.
      python-minor-and-patch:
        update-types: [minor, patch]

  - package-ecosystem: github-actions
    directory: /
    schedule:
      interval: weekly
      day: monday
    groups:
      # Actions are SHA-pinned with a `# vX.Y.Z` comment; Dependabot bumps the
      # SHA and rewrites the comment in the same PR.
      actions:
        patterns: ["*"]
```

- [ ] **Step 3: Delete `renovate.json`**

```bash
git rm renovate.json
```

- [ ] **Step 4: Verify**

Run:
```bash
uv run --group docs python -c "import yaml; d=yaml.safe_load(open('.github/dependabot.yml')); print(d['version'], [u['package-ecosystem'] for u in d['updates']])"
test ! -e renovate.json && echo "renovate.json gone"
make verify 2>&1 | tail -3
```
Expected: `2 ['uv', 'github-actions']`; `renovate.json gone`; `make verify` ends with the pytest summary line showing `passed` and no `failed`. (Deleting a root JSON file could in principle trip a packaging test; this run proves it does not.)

- [ ] **Step 5: Commit**

```bash
git add .github/dependabot.yml renovate.json
git commit -m "ci: Dependabot for uv and Actions, weekly and grouped; drop the never-installed Renovate config

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: SECURITY.md and the README link

**Files:**
- Create: `SECURITY.md`
- Modify: `README.md` (the "Learn about" table, currently around line 108–117)

**Interfaces:**
- Consumes: nothing.
- Produces: `SECURITY.md` at the repo root, which Task 8 step 3 (private vulnerability reporting) makes functional.

- [ ] **Step 1: Write the failing check**

Run:
```bash
test -f SECURITY.md && echo exists || echo missing
grep -c "SECURITY.md" README.md
```
Expected: `missing` and `0`.

- [ ] **Step 2: Create `SECURITY.md`**

```markdown
# Security policy

[← README](README.md)

## Supported versions

`ontary` is pre-1.0. Only the latest `0.x` minor receives security fixes; a fix
ships as a new patch or minor release, never as a backport. See
[docs/compatibility.md](docs/compatibility.md) for the versioning contract.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository:

https://github.com/ryoochi0112/ontary/security/advisories/new

Do not open a public issue for a security report. You will get an
acknowledgement within 7 days. Once a fix is released, the advisory is
published and the fix is listed in [CHANGELOG.md](CHANGELOG.md).

## What is in scope

The engine enforces `ScopePolicy`, min-N, and `Sensitivity` on behalf of every
adopter, and records every state change as an `AuditEntry`. The following are
security vulnerabilities in `ontary` itself:

- Reading or acting on an object, link, or property past a declared
  `ScopePolicy`, min-N threshold, or `Sensitivity` without the declared grant.
- A mutation (an Action, an ingest, or a store write reachable through the
  public API) that completes without a corresponding `AuditEntry`.
- A tenant boundary crossing in any store backend (in-memory, SQLite,
  Postgres).
- Unsafe deserialization or code execution from an ontology definition, a
  query, or MCP tool input.

Out of scope: an adopter's own ontology that declares a policy too loosely, and
vulnerabilities in dependencies that have their own advisory process (report
those upstream; a bump here follows).
```

- [ ] **Step 3: Link it from the README**

In `README.md`, find the table row:

```markdown
| Cutting a release (maintainers) | [Releasing](docs/releasing.md) |
```

and insert this row immediately after it:

```markdown
| Reporting a vulnerability | [Security policy](SECURITY.md) |
```

- [ ] **Step 4: Verify**

Run:
```bash
grep -c "SECURITY.md" README.md
wc -l README.md
uv run pytest tests/test_docs.py tests/test_packaging.py tests/test_actions.py -q 2>&1 | tail -1
```
Expected: `1`; a line count at or under `200` (the README has a hard 200-line budget); the pytest line shows `passed` and no `failed`. `test_readme_and_new_pages_have_no_broken_relative_markdown_links` covers the new link; the root-markdown glob in `test_actions.py` scans `SECURITY.md` for scope-region markers and finds none, which is correct.

- [ ] **Step 5: Commit**

```bash
git add SECURITY.md README.md
git commit -m "docs: SECURITY.md — supported versions, private reporting, what counts as a vulnerability

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Changelog and releasing runbook

**Files:**
- Modify: `CHANGELOG.md` (the `## [Unreleased]` section, currently empty, near line 12)
- Modify: `docs/releasing.md` (after the "One-time setup" paragraph, around line 12–17)

**Interfaces:**
- Consumes: the facts established by Tasks 1–5.
- Produces: nothing other tasks consume.

- [ ] **Step 1: Fill in `[Unreleased]`**

In `CHANGELOG.md`, replace the line `## [Unreleased]` (followed by a blank line and then `## [0.11.0] — 2026-09-06`) with:

```markdown
## [Unreleased]

### Added

- CI tests on Python 3.13 as well as 3.12, matching the classifiers.
- `codeql.yml`: CodeQL for Python on every PR, on `main`, and weekly.
- A `workflows` job in `verify.yml` lints the workflows with zizmor, so an
  unpinned action or a checkout that keeps its credentials fails the PR.
- `SECURITY.md`: supported versions, private vulnerability reporting, and what
  counts as a vulnerability in the engine.
- Dependabot (`.github/dependabot.yml`) for `uv` and GitHub Actions, weekly,
  grouped, never automerged.

### Changed

- Every workflow action is pinned to a commit SHA; every workflow declares
  `permissions: contents: read` at the top, a timeout on every job, and
  `persist-credentials: false` on checkout. `verify` cancels a superseded run
  on a PR branch only.
- The release gate no longer uses the uv cache: the job builds the bytes that
  are published, and a cache shared with PR runs is a poisoning surface.
- `renovate.json` removed. The Renovate app had never been installed on the
  repository, so the config was inert; Dependabot replaces it.
```

Keep the blank line and the `## [0.11.0] — 2026-09-06` heading that follow.

- [ ] **Step 2: Add the pin-maintenance note to `docs/releasing.md`**

After the paragraph that ends `No API token\nis stored anywhere.` (end of "One-time setup"), insert a blank line and then:

```markdown
Every `uses:` in the workflows is pinned to a commit SHA with a `# vX.Y.Z`
comment. Dependabot bumps the SHA and the comment together in a weekly PR;
never edit a `uses:` line by hand. The `workflows` job in `verify.yml` fails
the PR if a pin is missing.
```

- [ ] **Step 3: Verify**

Run:
```bash
make verify 2>&1 | tail -3
make docs-build 2>&1 | tail -2
```
Expected: `make verify` passes (`test_changelog_new_error_codes_match_catalog_diff` reads the `[Unreleased]` section and must still pass; the section adds no error codes). `make docs-build` completes with no `WARNING` lines (strict mode).

- [ ] **Step 4: Commit**

```bash
git add CHANGELOG.md docs/releasing.md
git commit -m "docs: changelog for the CI hardening; releasing.md notes Dependabot owns the action pins

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Gate, push, PR

**Files:** none modified.

**Interfaces:**
- Consumes: all commits on `ci/hardening`.
- Produces: an open PR whose CI run is the first real test of Tasks 1–3. Task 8 requires this PR to be merged first.

- [ ] **Step 1: Full local gate**

Run:
```bash
make verify 2>&1 | tail -3
uvx zizmor==1.30.0 .github/ 2>&1 | tail -1
git status --short
```
Expected: verify green, zizmor clean, working tree clean.

- [ ] **Step 2: Push and open the PR (needs the maintainer's go, per the repo's Git rules)**

```bash
git push -u origin ci/hardening
gh pr create --title "ci: enforce the gate — SHA pins, 3.13 matrix, zizmor, CodeQL, Dependabot, SECURITY.md" --body "$(cat <<'EOF'
Implements specs/2026-09-07-ci-hardening-design.md (Tiers A+B).

- Every action SHA-pinned; top-level `contents: read`; timeouts; `persist-credentials: false`; PR-only cancellation on `verify`.
- `verify` runs on 3.12 and 3.13. New `workflows` job (zizmor 1.30.0). New `codeql.yml`.
- Dependabot replaces the never-installed Renovate config.
- `SECURITY.md` + README link; CHANGELOG + releasing.md.
- Deviation from spec § 3 "no job logic changes": the release gate sets `enable-cache: false` (zizmor `cache-poisoning`).

After merge, repo settings flip in this order (spec § 9): required status checks → `sha_pinning_required` → Dependabot alerts + security updates + private vulnerability reporting.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 3: Watch the PR's checks**

Run:
```bash
gh pr checks --watch
```
Expected: `verify (3.12)`, `verify (3.13)`, `postgres`, `package`, `workflows`, `build`, `analyze (python)` all pass. If `verify (3.13)` fails, read the log and fix the cause on the branch; do not remove the leg. If `workflows` fails, zizmor's online audits found something the offline run did not; fix the workflow.

- [ ] **Step 4: Hand back**

Report the PR URL and the check results. Merge is the maintainer's decision.

---

### Task 8: Repository settings after merge

**Files:** none. All changes are via `gh api`. Run only after the PR from Task 7 is merged and only on the maintainer's explicit go; these are outward-facing settings.

**Interfaces:**
- Consumes: check contexts `verify (3.12)`, `verify (3.13)`, `postgres`, `package`, `workflows` (Task 2) and `build` (docs.yml, unchanged name).
- Produces: an enforced gate.

- [ ] **Step 1: Sync `main` and confirm the pins are on it**

```bash
git checkout main && git pull --ff-only
grep -L "@[0-9a-f]\{40\} # v" .github/workflows/*.yml
```
Expected: no file listed (every workflow file contains at least one SHA pin). Do not continue to step 3 if any file is listed.

- [ ] **Step 2: Required status checks on `main`**

The protection endpoint's `PUT` replaces the whole configuration, so the payload restates the current values (`required_approving_review_count: 0`, `enforce_admins: false`, no force pushes, no deletions).

```bash
cat > "$TMPDIR/protection.json" <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["verify (3.12)", "verify (3.13)", "postgres", "package", "workflows", "build"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": false,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 0
  },
  "restrictions": null,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_linear_history": false,
  "required_conversation_resolution": false
}
EOF
gh api -X PUT repos/ryoochi0112/ontary/branches/main/protection --input "$TMPDIR/protection.json" --jq '.required_status_checks'
```
Expected: `{"strict":true,"contexts":["verify (3.12)","verify (3.13)","postgres","package","workflows","build"], ...}`.

- [ ] **Step 3: Require SHA pinning for Actions**

```bash
gh api -X PUT repos/ryoochi0112/ontary/actions/permissions -F enabled=true -f allowed_actions=all -F sha_pinning_required=true
gh api repos/ryoochi0112/ontary/actions/permissions --jq .sha_pinning_required
```
Expected: `true`.

- [ ] **Step 4: Dependabot alerts, security updates, private vulnerability reporting**

```bash
gh api -X PUT repos/ryoochi0112/ontary/vulnerability-alerts
gh api -X PUT repos/ryoochi0112/ontary/automated-security-fixes
gh api -X PUT repos/ryoochi0112/ontary/private-vulnerability-reporting
gh api repos/ryoochi0112/ontary/vulnerability-alerts >/dev/null && echo "alerts on"
gh api repos/ryoochi0112/ontary --jq .security_and_analysis.dependabot_security_updates.status
gh api repos/ryoochi0112/ontary/private-vulnerability-reporting --jq .enabled
```
Expected: `alerts on`, `enabled`, `true`.

- [ ] **Step 5: Probe PR proves the checks are required**

```bash
git checkout -b probe/required-checks main
git commit --allow-empty -m "probe: required status checks (do not merge)"
git push -u origin probe/required-checks
gh pr create --title "probe: required status checks (do not merge)" --body "Throwaway. Verifies the six required contexts appear in the merge box."
gh pr checks --watch
gh api "repos/ryoochi0112/ontary/branches/main/protection/required_status_checks" --jq '.contexts'
gh pr view --json mergeStateStatus --jq .mergeStateStatus
```
Expected: all checks pass; the contexts array lists the six names; `mergeStateStatus` is `CLEAN` (or `BLOCKED` while checks are still running, then `CLEAN`). If any of the six never reports, its name is wrong in step 2; fix the context list and re-run step 2.

- [ ] **Step 6: Clean up the probe**

```bash
gh pr close probe/required-checks --delete-branch
git checkout main && git branch -D probe/required-checks 2>/dev/null; git branch -D ci/hardening
```
Expected: the probe PR is closed and its remote branch deleted; local `ci/hardening` deleted (it is merged).

- [ ] **Step 7: Confirm Dependabot accepted the config**

Open https://github.com/ryoochi0112/ontary/network/updates in a browser (there is no `gh` read for config validation). Expected: both `uv` and `github-actions` listed with "Last checked" timestamps and no red configuration error. First PRs arrive the following Monday.
