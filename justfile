default: 
    just --list
# Run tests using pytest
test *flags='-q':
    uv run pytest {{flags}}

# Run linting using ruff and pyright
lint *flags='--fix':
    uv run ruff --config pyproject.toml check . {{flags}}
    uv run pyright

# Format sources and tests
format *flags:
    uv run ruff format . {{flags}}

# Check generated CLI references
references *flags:
    uv run python scripts/generate_skill_references.py {{flags}}

# Audit both the locked environment and the currently resolvable runtime range
audit:
    uv run --locked pip-audit --local --skip-editable --progress-spinner off
    uv run --locked pip-audit --strict --progress-spinner off .

# Build and validate the wheel and source distribution metadata
build:
    uv run python -m build
    uv run twine check --strict dist/*
    uv run python scripts/validate_package.py dist

# Run the deterministic source checks
check:
    just test
    just lint
    just references --check
