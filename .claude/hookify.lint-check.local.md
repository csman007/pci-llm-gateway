---
name: lint-check-after-code
enabled: true
event: stop
action: warn
conditions:
  - field: transcript
    operator: regex_match
    pattern: (Edit|Write)\(.*\.py
  - field: transcript
    operator: not_contains
    pattern: ruff check
---

**Lint check not detected after Python file modifications!**

Python files were edited but `ruff check` was not run. Before finishing:

1. Fix lint errors: `ruff check --fix services/ scripts/`
2. Fix formatting: `ruff format services/ scripts/`
3. Verify clean: `ruff check services/ scripts/ && ruff format --check services/ scripts/`

The pre-commit hook and CI will fail if these checks are skipped.