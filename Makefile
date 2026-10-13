LEVEL ?= 1
TRIALS ?= 1
PATHS ?= mcp,a2a
QUESTIONS ?=
PORT := $(shell echo $$((8000 + $(LEVEL))))

# Load local settings (git-ignored) if present.
ifneq (,$(wildcard .env))
include .env
export
endif

.PHONY: install test tally serve run report

install:
	uv sync --python 3.12

test:
	uv run --frozen pytest -q -p no:warnings

tally:
	uv run --frozen python test-app/build_tally.py

serve:
	cd demo-app && LEVEL=$(LEVEL) uv run --frozen uvicorn --factory warehouse.app:create_app --host 127.0.0.1 --port $(PORT)

run:
	cd test-app && uv run --frozen python -m harness.run --level $(LEVEL) --trials $(TRIALS) --paths $(PATHS) $(if $(QUESTIONS),--questions $(QUESTIONS),)

report:
	cd test-app && uv run --frozen python -m harness.report $(BATCH)
