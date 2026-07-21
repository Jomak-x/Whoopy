# Keep these commands as the stable contributor interface. CI calls `make check`
# so local verification and pull-request verification cannot silently diverge.
PYTHON ?= python3.11
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: setup test lint format format-check typecheck check clean

setup:
	$(PYTHON) -m venv $(VENV)
	# --no-user makes setup reproducible even when a contributor has configured
	# pip to install into their user site by default.
	$(BIN)/python -m pip install --no-user --upgrade pip
	$(BIN)/python -m pip install --no-user -e ".[dev]"

test:
	$(BIN)/pytest

lint:
	$(BIN)/ruff check .

format:
	$(BIN)/ruff format .

format-check:
	$(BIN)/ruff format --check .

typecheck:
	$(BIN)/mypy src tests

check: lint format-check typecheck test

clean:
	$(PYTHON) -c "import shutil; [shutil.rmtree(path, ignore_errors=True) for path in ('build', 'dist', '.pytest_cache', '.mypy_cache', '.ruff_cache')]"
