# HANDOFF — ontary roadmap M0, issue #39 (authoring mistakes escape as bare errors)

Session scratch for the next session. Drop this file before the branch merges.

## Verified state

Anchored to `main @ e5f150d`, working tree clean, 2026-09-27.

- **DONE, verified:** #38 (aware `datetime` object on write) merged as PR #81 at `e5f150d`.
  `make verify` = 1644 passed / 13 skipped; Postgres conformance + backend = 362 passed;
  `make docs-build` strict built. Branch `fix/datetime-object-on-write-38` deleted local + origin.
- **DONE, verified:** #37 merged as PR #80 at `0e178c9`; branch deleted local + origin.
- **In flight (作業中):** #39 design is locked (see decisions below); **no code written yet**.
  This branch `fix/authoring-errors-39` holds only this file.
- **Unverified (未検証):** nothing.

Reproduced at `e5f150d` (Python one-liners, not tests yet):

| Mistake | Today |
| --- | --- |
| `ontology.link("ab", A, B, "MANY_TO_MNY")` | bare `ValueError: 'MANY_TO_MNY' is not a valid Cardinality` from `Cardinality(str)` in `authoring.py` `link()` |
| `@ontology.object(layer="L0", scope="unscpoed")` then `validate()` | raw pydantic `ValidationError` for `ScopePolicy.rules.C` ("Input should be a valid list") from `_build_policy` |
| `@ontology.object(layer="L9", ...)` | accepted silently — **out of scope for #39, belongs to #40** |

## Decisions (locked with Ryo, 2026-09-27, lettered options)

1. **Error code = reuse `ONTOLOGY_INVALID`** and broaden its catalogue description in
   `src/ontary/errors.py` (today: "cross-references were invalid"). No new code, so
   `ADDED_SINCE_080_ERROR_CODES` in `tests/test_docs.py` stays untouched. The description
   text is copied into the error tables of `docs/api-reference.md` + `.ja.md` — check
   whether `test_docs.py` pins the description string (it pins the row set; verify the text).
2. **Refuse at decoration / call time**, not at `validate()`: `@ontology.object(scope=…,
   contributor=…, row_visibility=…)` validates the shape immediately and raises
   `ValidationFailed(code="ONTOLOGY_INVALID")` naming the class, the kwarg, what was given,
   and the accepted forms. `link()` wraps the `Cardinality(...)` `ValueError` the same way,
   listing the four valid names.
3. **Literal-strict typing:**
   - `cardinality: Cardinality | Literal["ONE_TO_ONE", "ONE_TO_MANY", "MANY_TO_ONE", "MANY_TO_MANY"]`
   - `scope: Literal["unscoped"] | Sequence[ScopeRule] | None`
   - `contributor: Sequence[ScopeRule] | None`
   - `row_visibility: RowVisibilityFn | None`
   (`ScopeRule`, `RowVisibilityFn` live in `src/ontary/scope.py`.) Runtime still accepts any
   `str` cardinality that names a member; only the annotation narrows.

## Open decisions

None blocking. Two judgment calls the next session may make alone (mention in the PR):
- Whether `Ontology(scope_levels=[], …)` / `min_n=0` (pydantic `field_validator` errors in
  `ScopePolicy`, raised lazily from `_build_policy`) get wrapped as `ONTOLOGY_INVALID` too.
  Recommendation: yes, at `Ontology.__init__` if cheap; otherwise wrap in `_build_policy`.
- Whether `contributor="unscoped"` (a plausible typo) gets a message pointing at `scope=`.

## Environment

```bash
cd /Users/ryoochi/ontary
git switch fix/authoring-errors-39
make setup            # uv sync --all-extras
make verify           # ruff + mypy --strict + pytest (offline DoD)
export ONTARY_TEST_POSTGRES_DSN=postgresql://ryoochi@localhost:5432/ontary_test
uv run pytest -q tests/test_store_conformance.py tests/test_store_postgres.py   # Postgres gate
make docs-build       # strict mkdocs
```

`tests/test_typing_surface.py` is the only test file under `mypy --strict`; add a
misspelled-literal negative case there with `# type: ignore[arg-type]` + a comment, or a
dedicated `tests/test_typing_authoring.py` admitted to the `mypy` target in `Makefile`
only if it is clean.

## Next actions (in order)

1. **Failing tests first** (TDD): in `tests/test_authoring.py` add
   `test_misspelled_cardinality_is_ontology_invalid` (message contains the four names),
   `test_misspelled_scope_literal_is_ontology_invalid_at_decoration`,
   `test_scope_of_wrong_type_is_ontology_invalid_at_decoration` (e.g. `scope=SelfScope(...)`
   not wrapped in a list), `test_contributor_and_row_visibility_shape_checked`. Run, watch
   them fail with today's bare errors.
2. Implement in `src/ontary/authoring.py`: a `_check_scope_kwargs(cls_name, scope,
   contributor, row_visibility)` helper called inside `object()`'s decorator before
   registration; wrap `Cardinality(cardinality)` in `link()`. Narrow the annotations
   (decision 3). Keep `_build_policy` / `_diagnose_policy` unchanged.
3. Broaden the `ONTOLOGY_INVALID` description in `errors.py`; regenerate/patch the error
   table rows in `docs/api-reference.md` and `docs/api-reference.ja.md`; update the
   `@ontology.object(...)` / `ontology.link(...)` signature blocks in both docs
   (EN line ~175 and ~194; JA counterparts) to show the new types; CHANGELOG
   `[Unreleased] / Fixed` entry with `Fixes ontary#39`.
4. `make verify` + Postgres gate + `make docs-build`; delete this HANDOFF.md; then
   commit → push → PR (`Fixes #39`), merge is Ryo's. On "merged": sync main, delete branch,
   grill #40 next.

## Pointers

- Issue: https://github.com/ryoochi0112/ontary/issues/39 · milestone M0 · next: #40
- Roadmap: `docs/roadmap.md`
- Code: `src/ontary/authoring.py` (`object()` ~L506, `link()` ~L569, `_build_policy` ~L730),
  `src/ontary/scope.py` (`ScopeRule` L123, `RowVisibilityFn` L165, `ScopePolicy` L246),
  `src/ontary/meta.py` (`Cardinality` L43), `src/ontary/errors.py` (`ONTOLOGY_INVALID` L343)
- Precedent for this PR shape: PR #81 (tests-first, EN+JA docs, CHANGELOG, gates table)
- Memory: `~/.claude/projects/-Users-ryoochi-ontary/memory/ontary-datetime-object-pr-38.md`
