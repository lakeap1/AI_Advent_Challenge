#!/usr/bin/env bash
set -Eeuo pipefail

root="$HOME/ai-advent-day30"
app="$root/app"
task="$app/week_6/task_5"
data="$root/data"
token_file="$data/access-token"
venv="$root/.venv"
unit_name=ai-advent-day30.service
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_unit="$script_dir/$unit_name"
target_unit="$HOME/.config/systemd/user/$unit_name"

[[ "$(id -u)" -ne 0 ]] || { echo 'Refusing root execution' >&2; exit 1; }
for tool in python3 systemctl; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
for path in "$root" "$app" "$task" "$data" "$token_file" "$venv"; do
  [[ ! -L "$path" ]] || { echo "Refusing symlink: $path" >&2; exit 1; }
done
[[ -f "$source_unit" && -f "$task/server.py" && -f "$task/app.py" && -f "$task/requirements.txt" && -f "$task/config.toml" ]] || { echo 'Application upload incomplete' >&2; exit 1; }
[[ -f "$app/week_6/task_2/local_agent/service.py" ]] || { echo 'Missing explicit local_agent dependency' >&2; exit 1; }
if [[ -e "$target_unit" ]] && ! cmp -s "$source_unit" "$target_unit"; then
  echo "Existing unit differs: $target_unit" >&2; exit 1
fi
if command -v ss >/dev/null && ss -H -ltn '( sport = :5030 )' | grep -q .; then
  if ! systemctl --user is-active --quiet "$unit_name"; then
    echo 'Port 5030 is occupied outside this named user service' >&2; exit 1
  fi
fi
mkdir -p "$data" "$HOME/.config/systemd/user"
chmod 700 "$root" "$data" "$HOME/.config/systemd/user"
if [[ ! -e "$token_file" ]]; then
  python3 - "$token_file" <<'PY'
import os, pathlib, secrets, sys
path = pathlib.Path(sys.argv[1])
flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
fd = os.open(path, flags, 0o600)
try:
    with os.fdopen(fd, 'w', encoding='ascii') as stream:
        stream.write(secrets.token_urlsafe(32) + '\n')
except BaseException:
    path.unlink(missing_ok=True)
    raise
PY
else
  [[ -f "$token_file" ]] || { echo 'Token path is not a regular file' >&2; exit 1; }
  [[ "$(stat -c '%a' "$token_file")" == 600 ]] || { echo 'Existing token must have mode 0600' >&2; exit 1; }
fi
if [[ ! -d "$venv" ]]; then
  python3 -m venv "$venv"
fi
"$venv/bin/python" -m pip install --disable-pip-version-check -r "$task/requirements.txt"
(cd "$app" && "$venv/bin/python" -B -c 'import flask, httpx, waitress; from week_6.task_5.app import create_app') \
  || { echo 'Import check failed; confirm app tree and dependencies' >&2; exit 1; }
if [[ ! -e "$target_unit" ]]; then
  cp -- "$source_unit" "$target_unit"
  chmod 600 "$target_unit"
fi
systemctl --user daemon-reload
echo "Application environment ready: $venv"
echo "Access token created or preserved at $token_file (not printed)"
echo "Unit installed: $target_unit"
echo "Start only after runtime readiness: systemctl --user start $unit_name"
