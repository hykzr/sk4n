default: 
    just --list
# Run tests using pytest
test *flags='-q':
    uv run pytest {{flags}}
# Run linting using ruff and pyright
lint *flags='--fix':
    uvx ruff --config pyproject.toml check . {{flags}}
    uvx pyright
# Format EPUB sources and tests
format *flags:
    uvx ruff format . {{flags}}