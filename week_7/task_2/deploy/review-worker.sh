#!/bin/sh
# A fixed trusted command, never interpolate SSH_ORIGINAL_COMMAND or PR input.
set -eu
umask 077
review_env="$HOME/.config/ai-advent-day32/review.env"
if [ ! -f "$review_env" ] || [ -L "$review_env" ] || [ "$(stat -c %a "$review_env" 2>/dev/null)" != 600 ]; then
    echo "review.env must be a regular file with mode 600" >&2
    exit 1
fi
set -a
. "$HOME/.config/ai-advent-day31/service.env"
unset GRAPHICS_BUDGET_USD
. "$review_env"
set +a
if [ -z "${GRAPHICS_BUDGET_USD:-}" ]; then
    echo "review.env must set GRAPHICS_BUDGET_USD" >&2
    exit 1
fi
PR_REVIEW_ENABLE_LIVE=1
PR_REVIEW_DATA_DIR="$HOME/ai-advent-day32/data"
PR_REVIEW_SERVICE_ROOT="$HOME/ai-advent-day32/runtime/week_7/task_1"
export PR_REVIEW_ENABLE_LIVE PR_REVIEW_DATA_DIR PR_REVIEW_SERVICE_ROOT
cd "$HOME/ai-advent-day32/runtime/week_7/task_2"
exec "$HOME/ai-advent-day31/.venv/bin/python" -B -m pr_review.worker
