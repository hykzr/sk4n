set windows-shell := ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoLogo", "-Command"]
default: 
    just --list
# Run tests using pytest
test *flags='-q':
    uv run pytest {{flags}}

# Run Python and Markdown linting
lint *flags='--fix':
    uv run ruff --config pyproject.toml check . {{flags}}
    uv run pyright
    just lint-markdown

# Fix automatically-fixable Markdown issues using the shared editor rules
format-markdown:
    npm run format:markdown

# Check Markdown using the shared editor rules
lint-markdown:
    npm run lint:markdown

# Format Python and Markdown sources
format *flags:
    uv run ruff format . {{flags}}
    just {{if flags =~ '(^| )--check( |$)' { 'lint-markdown' } else { 'format-markdown' }}}

# Check generated CLI references
references *flags:
    uv run python scripts/generate_skill_references.py {{flags}}

# Audit Python, npm, and GitHub Actions against current vulnerability reports
audit:
    uv run --locked python scripts/audit_github_actions.py
    npm audit
    # Check the installed runtime and development dependencies.
    uv run --locked pip-audit --local --skip-editable --progress-spinner off
    # Check a fresh resolution of the runtime requirements from pyproject.toml.
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
