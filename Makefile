.PHONY: help install lint typecheck test-unit test-integration test

help:
	@echo "Available targets:"
	@echo "  install           Install the package with dev and test extras"
	@echo "  lint              Run ruff over the source tree"
	@echo "  typecheck         Run mypy in strict mode"
	@echo "  test-unit         Run unit tests (tests/unit)"
	@echo "  test-integration  Run integration tests (tests/integration)"
	@echo "  test              Run both unit and integration tests"

install:
	pip install -e ".[dev,test]"

lint:
	ruff check .

typecheck:
	mypy

test-unit:
	pytest tests/unit

test-integration:
	pytest tests/integration

test: test-unit test-integration
