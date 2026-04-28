#!/bin/bash
cd "$CLAUDE_PROJECT_DIR"
source .venv/bin/activate

output=$(pytest --tb=short -q 2>&1)
exit_code=$?

if [ $exit_code -ne 0 ]; then
  echo "$output"
  echo ""
  echo "pytest failed — commit blocked. Fix failing tests before committing."
  exit 2
fi

exit 0
