.DEFAULT_GOAL := help

## ---------------------------------------------------------------------------
## Setup
## ---------------------------------------------------------------------------

.PHONY: install
install: ## Install the package and its dependencies into a uv-managed virtual environment
	uv sync

## ---------------------------------------------------------------------------
## Linting
## ---------------------------------------------------------------------------

.PHONY: init-lint
init-lint: ## Initialize linters
	uv run pre-commit clean
	uv run pre-commit gc
	find . -path '*/.mypy_cache/*' -delete
	uv run pre-commit uninstall
	uv run pre-commit install --install-hooks

.PHONY: lint
lint: ## Run linters
	uv run pre-commit run -a --show-diff-on-failure --color=always

## ---------------------------------------------------------------------------
## Release
## ---------------------------------------------------------------------------

.PHONY: build
build: ## Build package distributions
	uv build

## ---------------------------------------------------------------------------
## Maintenance
## ---------------------------------------------------------------------------

.PHONY: clean
clean: ## Clean build artifacts and caches
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} +
	find . -type d -name .pytest_cache -not -path './.venv/*' -exec rm -rf {} +
	find . -type d -name .mypy_cache -not -path './.venv/*' -exec rm -rf {} +
	find . -type d -name '*.egg-info' -not -path './.venv/*' -exec rm -rf {} +
	find . -name '*.pyc' -not -path './.venv/*' -delete
	rm -rf dist/

## ---------------------------------------------------------------------------
## Help
## ---------------------------------------------------------------------------

.PHONY: help
help: ## Show this help message
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} \
		/^## [A-Z][a-zA-Z]*$$/ { section = substr($$0, 4); next } \
		/^[a-zA-Z_-]+:.*##/ { \
			if (section != last_section) { \
				printf "\n\033[1m%s\033[0m\n", section; \
				last_section = section; \
			} \
			printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2; \
		}' \
		$(MAKEFILE_LIST)
