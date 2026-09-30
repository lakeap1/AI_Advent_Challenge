#!/bin/sh
# Run on the capsule after installing the Day18 systemd service.
set -eu
app_dir="${1:-$HOME/ai-advent-day18}"
case "$app_dir" in *[!A-Za-z0-9_./-]*) echo 'Use an absolute application path without spaces' >&2; exit 1;; esac
case "$app_dir" in /*) ;; *) echo 'Application path must be absolute' >&2; exit 1;; esac
test -x "$app_dir/.venv/bin/python"
# Debian cron schedules in system time; TZ in a crontab would only change the child environment.
zone=$(timedatectl show -p Timezone --value)
case "$zone" in UTC|Etc/UTC|Asia/Omsk) ;; *) echo "Unsupported system timezone: $zone (expected UTC or Asia/Omsk)" >&2; exit 1;; esac
mkdir -p "$app_dir/data/cron-backups"
backup="$app_dir/data/cron-backups/crontab-$(date -u +%Y%m%dT%H%M%SZ).txt"
if crontab -l > "$backup" 2> "$backup.err"; then :
elif grep -q 'no crontab' "$backup.err"; then :
else cat "$backup.err" >&2; exit 1; fi
python3 - "$backup" "$app_dir" <<'PY'
import pathlib, subprocess, sys
path, app = pathlib.Path(sys.argv[1]), sys.argv[2]
begin, end = '# BEGIN AI-ADVENT-DAY18', '# END AI-ADVENT-DAY18'
lines, inside = [], False
for line in path.read_text().splitlines():
    if line == begin:
        if inside: raise SystemExit('Malformed managed cron block')
        inside = True
    elif line == end:
        if not inside: raise SystemExit('Malformed managed cron block')
        inside = False
    elif not inside:
        lines.append(line)
if inside: raise SystemExit('Unclosed managed cron block')
command = f'cd {app} && {app}/.venv/bin/python -m digest_server.cron_tick 2>&1 | /usr/bin/systemd-cat -t ai-advent-day18-cron'
lines.extend([begin,
    '# Collection: Asia/Omsk 00,06,12,18 (same six-hour set on UTC).',
    '# Publication: once daily at 00:00 Asia/Omsk = previous date 18:00 UTC.',
    '0 0,6,12,18 * * * ' + command,
    '# Recovery ticks only collect when persisted MCP schedule is due.',
    '1-59/5 * * * * ' + command,
    '@reboot ' + command, end])
subprocess.run(['crontab', '-'], input='\n'.join(lines)+'\n', text=True, check=True)
print('Installed capsule cron; previous crontab:', path)
PY
