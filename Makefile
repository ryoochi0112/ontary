.PHONY: verify test lint typecheck fmt setup

setup:
	uv sync --all-extras

verify: lint typecheck test

lint:
	uv run ruff check src tests examples scripts

# `tests/` as a whole is NOT type-checked: 249 errors across 44 files, measured
# at 44a9cf9. Named test files are admitted one at a time, and only files that
# are already clean under --strict. test_v1_gate_coverage.py is here because it
# IS the v1 acceptance gate -- a type error in the guard is a hole in the gate.
typecheck:
	uv run mypy src examples scripts tests/test_typing_surface.py tests/test_v1_gate_coverage.py

test:
	uv run pytest -q

fmt:
	uv run ruff format src tests examples && uv run ruff check --fix src tests examples
