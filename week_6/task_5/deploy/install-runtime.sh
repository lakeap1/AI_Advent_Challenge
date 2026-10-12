#!/usr/bin/env bash
set -Eeuo pipefail

# Run as the existing unprivileged VPS user. All writes stay in this task's root.
version=v0.40.3
archive_name=ollama-linux-amd64.tar.zst
expected_sha256=ce6f8b671a699bf1ebafb28d60dbbabf4f37b30b499145fa308a8ba72de021d6
root="$HOME/ai-advent-day30"
runtime="$root/.runtime/$version"
archive="$root/.downloads/$archive_name"
unit_name=ai-advent-day30-ollama.service
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
source_unit="$script_dir/$unit_name"
target_unit="$HOME/.config/systemd/user/$unit_name"

[[ "$(id -u)" -ne 0 ]] || { echo 'Refusing root execution' >&2; exit 1; }
[[ "$(uname -s)" == Linux && "$(uname -m)" == x86_64 ]] || { echo 'Requires Linux x86_64' >&2; exit 1; }
for tool in curl tar zstd sha256sum python3 systemctl; do
  command -v "$tool" >/dev/null || { echo "Missing required tool: $tool" >&2; exit 1; }
done
[[ -f "$source_unit" ]] || { echo "Missing unit template: $source_unit" >&2; exit 1; }
[[ "$root" == "$HOME/ai-advent-day30" && ! -L "$root" ]] || { echo 'Unsafe deployment root' >&2; exit 1; }
for path in "$root/.runtime" "$root/.downloads" "$root/.models" "$root/data" "$root/data/ollama-home" "$runtime"; do
  [[ ! -L "$path" ]] || { echo "Refusing symlink: $path" >&2; exit 1; }
done

# A foreign listener or pre-existing unit must be resolved by the operator.
if command -v ss >/dev/null && ss -H -ltn '( sport = :11435 )' | grep -q .; then
  if ! systemctl --user is-active --quiet "$unit_name"; then
    echo 'Port 11435 is occupied outside this named user service' >&2; exit 1
  fi
fi
if [[ -e "$target_unit" ]] && ! cmp -s "$source_unit" "$target_unit"; then
  echo "Existing unit differs: $target_unit" >&2; exit 1
fi
mkdir -p "$root/.runtime" "$root/.downloads" "$root/.models" "$root/data/ollama-home"
chmod 700 "$root" "$root/.runtime" "$root/.downloads" "$root/.models" "$root/data" "$root/data/ollama-home"
if [[ -e "$runtime" ]]; then
  [[ -x "$runtime/bin/ollama" ]] || { echo "Existing runtime is incomplete: $runtime" >&2; exit 1; }
  [[ "$("$runtime/bin/ollama" --version 2>&1)" == *"0.40.3"* ]] || { echo 'Existing runtime version differs' >&2; exit 1; }
else
  if [[ -e "$archive" ]]; then
    [[ "$(sha256sum "$archive" | cut -d' ' -f1)" == "$expected_sha256" ]] || { echo "Existing archive checksum mismatch: $archive" >&2; exit 1; }
  else
    tmp_archive="$(mktemp "$root/.downloads/.ollama-download.XXXXXXXX")"
    trap 'rm -f -- "$tmp_archive"' EXIT
    curl --fail --location --proto '=https' --tlsv1.2 --retry 2 --connect-timeout 20 --output "$tmp_archive" "https://github.com/ollama/ollama/releases/download/$version/$archive_name"
    [[ "$(sha256sum "$tmp_archive" | cut -d' ' -f1)" == "$expected_sha256" ]] || { echo 'Downloaded archive SHA256 mismatch' >&2; exit 1; }
    mv -- "$tmp_archive" "$archive"
    trap - EXIT
  fi
  staging="$(mktemp -d "$root/.runtime/.ollama-extract.XXXXXXXX")"
  trap 'rm -rf -- "$staging"' EXIT
  # Python's data filter permits internal symlinks while rejecting external links,
  # path traversal, device files, and unsafe ownership/permissions.
  python3 - "$archive" "$staging" <<'PY'
import subprocess, sys, tarfile
archive, staging = sys.argv[1:]
stream = subprocess.Popen(["zstd", "-dc", archive], stdout=subprocess.PIPE)
try:
    with tarfile.open(fileobj=stream.stdout, mode="r|") as members:
        members.extractall(staging, filter="data")
finally:
    stream.stdout.close()
    if stream.wait() != 0:
        raise SystemExit("Archive decompression failed")
PY
  [[ -x "$staging/bin/ollama" ]] || { echo 'Archive lacks executable bin/ollama' >&2; exit 1; }
  [[ "$("$staging/bin/ollama" --version 2>&1)" == *"0.40.3"* ]] || { echo 'Archive runtime version mismatch' >&2; exit 1; }
  mv -- "$staging" "$runtime"
  trap - EXIT
fi

mkdir -p "$HOME/.config/systemd/user"
chmod 700 "$HOME/.config/systemd/user"
if [[ ! -e "$target_unit" ]]; then
  cp -- "$source_unit" "$target_unit"
  chmod 600 "$target_unit"
fi
systemctl --user daemon-reload
echo "Ollama runtime ready: $runtime"
echo "Unit installed: $target_unit"
echo "Start only after inspecting it: systemctl --user start $unit_name"
