# CI hardening — make the gate enforce, test what we claim, watch the supply chain

**Status:** design approved in conversation 2026-09-07 (2 decisions, Ryo Ochi).
**Repo:** `github.com/ryoochi0112/ontary` (local `~/ontary`). Base: `main` at 0.11.0,
`make verify` green, three workflows (`verify`, `docs`, `release`) all passing.
**Maintainer model:** one contributor (Ryo). The changes below make CI *enforce* what it
already checks; they do not add review requirements a single maintainer cannot satisfy.

## 1. Why

An audit of the repository on 2026-09-07 found the workflows sound in shape and the
enforcement around them missing:

- Branch protection on `main` has **no required status checks**. Two pushes to `main` on
  2026-09-06 (`9742bf3`, `e643931`) failed `verify` and landed anyway. CI is advisory.
- The package **claims Python 3.13** in its classifiers; every job runs 3.12 only.
- `renovate.json` exists but the Renovate app was **never installed** (no dependency
  dashboard issue, zero Renovate PRs). Dependabot alerts and security updates are off.
  Nothing watches dependencies.
- Every `uses:` is **tag-pinned**, `sha_pinning_required` is off, no job has a
  `timeout-minutes`, `verify` has no `concurrency` group, and `verify`/`docs` declare no
  top-level `permissions:` (they inherit the read-only repo default, but implicitly).
- No CodeQL, no `SECURITY.md`, private vulnerability reporting disabled.

Performance is **not** a problem: the suite is 1,504 tests in about 10 s locally, and
`verify` completes in about one minute in CI with the uv cache. The matrix below roughly
doubles that; nothing else here adds wall-clock to the PR path except CodeQL, which is
not a required check.

## 2. Decisions (recorded for the spec, all human)

| # | Question | Decision |
|---|---|---|
| Q1 | Scope | **Tiers A+B**: enforcement + hardening + supply chain. Contributor surface (CONTRIBUTING, templates, GitHub Release on tag) deferred to a follow-up. |
| Q2 | Dependency-update engine | **Dependabot** replaces Renovate. Native, no app install, supports the `uv` and `github-actions` ecosystems, maintains SHA-pin version comments. |

## 3. Workflow hardening (`verify.yml`, `docs.yml`, `release.yml`)

Applies uniformly to all three workflows. No job logic changes.

- Top-level `permissions: contents: read`. Jobs that need more (`docs/deploy`,
  `release/publish`) keep their existing job-level grants.
- `timeout-minutes` on every job: 15 for `verify` jobs, 10 for `docs`, 20 for the
  `release` gate and 10 for `publish`.
- `verify` gets a `concurrency` group keyed on workflow and ref, with
  `cancel-in-progress` true **only** for `pull_request` events. Pushes to `main` and tag
  runs are never cancelled. `docs` already has a group; it is left as is (deploys must not
  be cancelled mid-flight). `release` already serialises per tag.
- Every `actions/checkout` sets `persist-credentials: false`. No job pushes back to the
  repository, so the token is never needed after checkout.
- Every `uses:` is pinned to a **full 40-character commit SHA** with a trailing
  `# vX.Y.Z` comment. Dependabot reads the comment and bumps both together.

## 4. Python matrix (`verify.yml`)

The `verify` job becomes `strategy: matrix: python: ["3.12", "3.13"]` with
`fail-fast: false` so one interpreter's failure does not hide the other's result. The
matrix value feeds `setup-uv`'s `python-version`. The resulting check names are
`verify (3.12)` and `verify (3.13)`.

`postgres` and `package` stay on 3.12: they test a backend and the distribution, not
interpreter compatibility. `[tool.mypy] python_version = "3.12"` stays as the typing
target; mypy's own runtime interpreter varies with the matrix, which is fine.

## 5. Workflow lint job (`verify.yml`)

A new job `workflows` runs `uvx zizmor .github/` with default settings and fails on any
finding. It is the check that keeps § 3 from regressing (an unpinned `uses:`, a
`persist-credentials` default, a template-injection pattern). Findings raised during
implementation are **fixed, not suppressed**; if a suppression is ever needed it goes in
`.github/zizmor.yml` with a one-line reason.

## 6. CodeQL (`codeql.yml`, new)

Language `python`. Triggers: `pull_request`, `push` to `main`, and `schedule` weekly on
Monday. Permissions: `security-events: write`, `contents: read`, `actions: read`. Uses
`github/codeql-action` (init + analyze), SHA-pinned like everything else. **Not a required
check** in this iteration: its value is the Security tab alert surface, and a code-scanning
job on the required path would add two to three minutes to every merge for a pure-Python
repository that has not yet produced an alert.

## 7. Dependabot replaces Renovate

Delete `renovate.json`. Add `.github/dependabot.yml`:

- `package-ecosystem: uv`, `directory: /`, weekly on Monday. Groups: one PR for all
  minor and patch updates; majors open individually.
- `package-ecosystem: github-actions`, `directory: /`, weekly on Monday, one grouped PR.
- No automerge (Dependabot has none by default; nothing is added). This preserves the
  Renovate config's stated intent: a lockfile rewrite is read by a human before it lands.

`uv.lock` remains the single source of truth for pinned versions. The `[[tool.uv.index]]`
pin in `pyproject.toml` is unaffected.

## 8. `SECURITY.md` (root)

Contents, in this order:

1. **Supported versions**: the latest `0.x` minor only; pre-1.0 per
   `docs/compatibility.md`.
2. **Reporting**: GitHub private vulnerability reporting on the repository (link to the
   `security/advisories/new` page). No public issues for security reports.
3. **Response target**: acknowledgement within 7 days.
4. **Scope note**: the engine enforces `ScopePolicy`, min-N, and `Sensitivity` on behalf of
   every adopter, so a way to read or act past any of them without the declared grant is in
   scope, as is any audit-trail gap (an operation that mutates state without an
   `AuditEntry`).

`README.md` links it from the existing footer links line. The file carries no version
pins, so `test_documented_install_ref_matches_the_current_version` is unaffected.

## 9. Repository settings (after merge, on Ryo's go, via `gh api`)

Applied **in this order**, because SHA pinning must be on `main` before it is enforced:

1. **Required status checks on `main`**: contexts `verify (3.12)`, `verify (3.13)`,
   `postgres`, `package`, `workflows`, `build` (the docs build job). `strict: true`, so a
   branch must be current with `main` before it can merge. CI is about a minute, so the
   cost of re-running after a `main` change is low and it removes semantic-merge risk.
2. **Actions**: `sha_pinning_required: true`.
3. **Security**: enable Dependabot alerts, Dependabot security updates, and private
   vulnerability reporting.

Not changed: `required_approving_review_count` stays 0 and `enforce_admins` stays off
(single maintainer needs the escape hatch), `delete_branch_on_merge` stays off (branch
cleanup is a deliberate manual step in the maintainer's workflow).

## 10. Verification (definition of done)

- `make verify` green locally.
- `uvx zizmor .github/` reports no findings locally.
- The PR's own CI run shows `verify (3.12)`, `verify (3.13)`, `postgres`, `package`,
  `workflows`, `build`, and CodeQL `analyze (python)` all green.
- After § 9 step 1, a probe PR from a throwaway branch shows the six contexts listed as
  **Required** in the merge box; the probe PR is then closed and its branch deleted.
- After § 9 step 3, the repository Security tab shows Dependabot alerts enabled and
  "Report a vulnerability" available.

## 11. Docs and changelog

- `CHANGELOG.md` `[Unreleased]`: under **Added** — CodeQL, workflow lint, Python 3.13 in
  CI, `SECURITY.md`, Dependabot; under **Changed** — SHA-pinned Actions, explicit
  permissions/timeouts/concurrency, Renovate config removed.
- `docs/releasing.md`: one line noting that Dependabot maintains the Action SHA pins, so a
  maintainer never edits a `uses:` line by hand.
- No other documentation changes.

## 12. Out of scope (deliberate)

Coverage upload (third-party dependency, little signal at 1,500 tests), OpenSSF Scorecard
action (useful later, noisy now), signed commits, `enforce_admins`, CodeQL as a required
check, and the Tier C contributor surface (CONTRIBUTING.md, issue and PR templates, GitHub
Release creation on tag), which gets its own spec.
