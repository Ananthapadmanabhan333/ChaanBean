#!/usr/bin/env bash
# Run the import-linter contracts inside WSL. Separate from wsl-test.sh because
# lint-imports is a console script rather than a `python -m` entrypoint.
set -uo pipefail
cd /mnt/c/Users/Ananthapadmanabhan/Desktop/Projects/credit-recovery-platform/backend || exit 1
/opt/chaanbean-venv/bin/lint-imports
echo "LINT_EXIT=$?"
