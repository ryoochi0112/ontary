.PHONY: verify test lint typecheck fmt setup

setup:
	uv sync --all-extras

verify: lint typecheck test

lint:
	uv run ruff check src tests examples scripts

typecheck:
	uv run mypy src examples scripts tests/test_typing_surface.py

test:
	uv run pytest -q

fmt:
	uv run ruff format src tests examples && uv run ruff check --fix src tests examples
