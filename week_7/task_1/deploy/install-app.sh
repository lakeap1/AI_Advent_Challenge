#!/usr/bin/env bash
# Run as the selected unprivileged service user after corpus/index setup.
set -euo pipefail
graphics_base="$HOME/ai-advent-day31"
graphics_repo="$graphics_base/repo"
graphics_source="$graphics_repo/week_7/task_1"
graphics_env="$HOME/.config/ai-advent-day31/service.env"
test "$(git -C "$graphics_repo" rev-parse --show-toplevel)" = "$graphics_repo"
test -f "$graphics_env"
test "$(stat -c '%a' "$graphics_env")" = 600
if ! test -x "$graphics_base/.venv/bin/python"; then
    python3 -m venv "$graphics_base/.venv"
fi
"$graphics_base/.venv/bin/python" -m pip install --disable-pip-version-check -r "$graphics_source/requirements.txt"
mkdir -p "$HOME/.config/systemd/user" "$graphics_base/data"
chmod 700 "$graphics_base/data"
install -m 600 "$graphics_source/deploy/ai-advent-day31.service" "$HOME/.config/systemd/user/"
systemctl --user daemon-reload
systemctl --user enable ai-advent-day31.service
systemctl --user restart ai-advent-day31.service
systemctl --user is-active ai-advent-day31.service
# end
