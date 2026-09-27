.PHONY: verify test lint typecheck fmt setup docs docs-build

setup:
	uv sync --all-extras

verify: lint typecheck test

lint:
	uv run ruff check src tests examples scripts

# `tests/` as a whole is NOT type-checked: 249 errors across 44 files, measured
# at 44a9cf9. Named test files are admitted one at a time, and only files that
# are already clean under --strict.
typecheck:
	uv run mypy src examples scripts tests/test_typing_surface.py tests/test_typed_action_context.py tests/test_choice_properties.py tests/test_struct_properties_core.py tests/test_struct_properties_writes.py tests/test_struct_properties_authoring.py tests/test_struct_properties_reads.py tests/test_struct_properties_actions.py tests/test_struct_properties_mcp.py tests/test_rules_transitions_authoring.py tests/test_rules_transitions_actions.py tests/test_rules_transitions_core.py tests/test_rules_transitions_writes.py tests/test_rules_transitions_ingest.py tests/test_rules_transitions_mcp.py

test:
	uv run pytest -q

fmt:
	uv run ruff format src tests examples && uv run ruff check --fix src tests examples

# Docs site. `docs` serves a live preview; `docs-build` is the CI gate
# (`--strict` turns every MkDocs warning, e.g. a broken link, into a failure).
docs:
	uv run --group docs mkdocs serve

docs-build:
	uv run --group docs mkdocs build --strict
