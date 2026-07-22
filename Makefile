# Make is a convenience wrapper on Unix. uv and the Python check script are the
# platform-neutral interface used by Windows, macOS, Linux, and CI.
UV ?= uv

.PHONY: setup test lint format format-check typecheck check clean

setup:
	$(UV) sync --extra dev --locked

test:
	$(UV) run --extra dev pytest

lint:
	$(UV) run --extra dev ruff check .

format:
	$(UV) run --extra dev ruff format .

format-check:
	$(UV) run --extra dev ruff format --check .

typecheck:
	$(UV) run --extra dev mypy src tests scripts

check:
	$(UV) run --extra dev python scripts/check.py

clean:
	$(UV) run python -c "import shutil; [shutil.rmtree(path, ignore_errors=True) for path in ('build', 'dist', '.pytest_cache', '.mypy_cache', '.ruff_cache')]"
