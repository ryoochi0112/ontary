# HANDOFF — review Dependabot PRs #10 (mcp 2.x) and #9 (grouped minors)

Session scratch for the next session. Drop or update before any branch carrying it merges.

## Verified state (at `main` = `71871a6`, working tree clean except two unrelated untracked specs)

DONE and verified on 2026-09-07:

- CI hardening PR #7 merged. `main` requires six checks (`verify (3.12)`, `verify (3.13)`,
  `postgres`, `package`, `workflows`, `build`), strict. Verified by probe PR #8: all six
  reported `isRequired: true`, merge state `CLEAN`; probe closed, branch deleted.
- `sha_pinning_required: true`; Dependabot alerts + security updates on; private
  vulnerability reporting on. Verified by `gh api` readback of each setting.
- Dependabot accepted `.github/dependabot.yml` and opened #9 and #10 within minutes.
- **PR #10** (`mcp` 1.28.1 → 2.1.1) is **red** on `verify (3.12)`, `verify (3.13)`,
  `postgres`, `package`. Verified from the run log: `mcp.server.fastmcp` no longer exists in
  2.x (`FastMCP` renamed to `MCPServer` at `mcp.server.mcpserver`); mypy fails at
  `src/ontary/mcp_server.py:84`, and the stranger test raises ontary's own
  "unsupported mcp version" `ImportError`. The `<2` ceiling in `pyproject.toml` did its job.
- **PR #9** (pydantic 2.13.5, ruff 0.15.22 → 0.16.5, mypy 2.3.1) is **red** on both
  `verify` legs at `make lint`: ruff 0.16.5 reports 159 errors, all in rule families the
  project does not select (`RUF059` ×32, `UP047` ×18, `SIM117` ×16, `BLE001` ×16,
  `PLC0414` ×12, `UP046` ×6). `pyproject.toml` has `extend-select = ["I", "B", "C901"]`
  only, so ruff 0.16 appears to have changed its default rule set or how
  `extend-select` composes. Not yet investigated.

Unverified: whether ruff 0.16's change is a default-set change or a config-key rename
(read the ruff 0.16.0 release notes first). Whether `mcp` 2.x keeps the auth surface
(`AccessToken.subject`, `TokenVerifier`, `get_access_token`) that `mcp_server.py` relies on.

In flight: nothing.

## Open decisions

1. **#10 — pin or migrate?** (a) Close #10 and keep `mcp>=1.27.2,<2`; Dependabot will
   stop proposing 2.x only if the ceiling stays and you add an `ignore` for `mcp` majors
   in `.github/dependabot.yml`, otherwise it reopens on each 2.x release. (b) Migrate
   `mcp_server.py` to 2.x on a branch, dropping 1.x. Context: the `<2` rationale comment
   above `mcp = [...]` in `pyproject.toml`, and `tests/test_mcp_multi_consumer.py` (the
   suite that established the 1.27.2 floor). A 2.x migration is a feature-sized change,
   not a dependency bump; recommend (a) now, (b) as its own spec.
2. **#9 — how to take ruff 0.16?** Either pin `ruff<0.16` in the `dev` extra for now, or
   fix/ignore the 159 findings in the same PR. Decide after reading the ruff 0.16 notes.
3. Stale remote branches (`docs/oss-v0-roadmap`, `feat/oss-v0-docs`,
   `fix/skip-specs-in-install-ref-walk`) are merged; delete or keep is your call.

## Environment

```bash
cd /Users/ryoochi/ontary
git checkout main && git pull --ff-only
uv sync --all-extras
make verify                      # offline gate: ruff + mypy --strict + pytest, ~10 s
uvx zizmor==1.30.0 .github/      # must print "No findings to report" before touching workflows or dependabot.yml
```

Postgres arm (optional, mirrors the `postgres` CI job):

```bash
export ONTARY_TEST_POSTGRES_DSN=...   # local DB ontary_test works; value not recorded here
uv run pytest tests/test_store_conformance.py tests/test_store_postgres.py -q
```

Inspect the two PRs:

```bash
gh pr view 10 --json title,files,statusCheckRollup
gh pr view 9  --json title,files,statusCheckRollup
gh run view "$(gh run list --branch dependabot/uv/python-minor-and-patch-0a44b4a3fb --workflow verify --limit 1 --json databaseId --jq '.[0].databaseId')" --log-failed | grep -E "^.*[A-Z]+[0-9]{3,4} " | head
```

## Next actions

1. Read the ruff 0.16.0 release notes and decide #9's path (decision 2). Smallest cold
   start: check out `dependabot/uv/python-minor-and-patch-0a44b4a3fb`, run `make lint`,
   confirm the 159 count locally.
2. For #10, take decision 1. If (a): close #10 with a comment pointing at the
   `pyproject.toml` rationale, and add to `.github/dependabot.yml` under the `uv` entry:
   `ignore: [{dependency-name: mcp, update-types: ["version-update:semver-major"]}]`.
   Run zizmor before pushing that change; it is in the `workflows` job's scope.
3. Merge #9 once green (Ryo merges; say "merged" and the loop syncs main + deletes the branch).
4. Later, own spec: mcp 2.x migration; Tier C of the CI plan (CONTRIBUTING, issue/PR
   templates, GitHub Release on tag).

## Pointers

- CI hardening spec and plan: `specs/2026-09-07-ci-hardening-design.md`,
  `specs/plans/2026-09-07-ci-hardening.md` (Task 8 has the branch-protection PUT payload).
- mcp pin rationale: `pyproject.toml`, comment above `mcp = [...]` in
  `[project.optional-dependencies]`.
- mcp integration surface: `src/ontary/mcp_server.py` (imports at lines 82–85, 167, 176, 196).
- Version-compat suite for mcp: `tests/test_mcp_multi_consumer.py`.
- PRs: https://github.com/ryoochi0112/ontary/pull/10 , https://github.com/ryoochi0112/ontary/pull/9
- Releasing runbook (SHA pins are Dependabot-owned): `docs/releasing.md`.
