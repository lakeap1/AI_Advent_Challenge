#!/usr/bin/env bash
set -Eeuo pipefail

root="$HOME/ai-advent-day30"
runtime="$root/.runtime/v0.40.3/bin/ollama"
manifest="$root/data/model-manifest.json"
[[ "$(id -u)" -ne 0 ]] || { echo 'Refusing root execution' >&2; exit 1; }
[[ -x "$runtime" && -d "$root/.models" && -d "$root/data/ollama-home" ]] || { echo 'Run install-runtime.sh first' >&2; exit 1; }
[[ ! -L "$manifest" ]] || { echo 'Refusing symlink manifest' >&2; exit 1; }
[[ ! -e "$manifest" || -f "$manifest" ]] || { echo 'Existing manifest is not a regular file' >&2; exit 1; }
if ! systemctl --user is-active --quiet ai-advent-day30-ollama.service; then
  echo 'Named Ollama user service is not active' >&2; exit 1
fi
export HOME="$root/data/ollama-home"
export OLLAMA_HOST=127.0.0.1:11435
export OLLAMA_MODELS="$root/.models"
export OLLAMA_NO_CLOUD=1

# An existing manifest fences subsequent runs against mutable upstream tags.
if [[ -f "$manifest" ]]; then
  curl --fail --silent --show-error --max-time 10 http://127.0.0.1:11435/api/tags |
    python3 -c 'import json,sys; tags=json.load(sys.stdin).get("models", []); current=next((m.get("digest") for m in tags if m.get("name")=="qwen3.5:4b"),None); saved=json.load(open(sys.argv[1],encoding="utf-8"))["digest"]; sys.exit(0 if current==saved else 1)' "$manifest" || {
      echo 'Existing model digest differs from saved manifest or model is missing' >&2; exit 1;
    }
  echo "Model already pinned by $manifest"
  exit 0
fi

"$runtime" pull qwen3.5:4b
tmp="$(mktemp "$root/data/.model-manifest.XXXXXXXX")"
trap 'rm -f -- "$tmp"' EXIT
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:11435/api/tags |
  python3 -c '
import json, pathlib, sys
target = pathlib.Path(sys.argv[1])
body = json.load(sys.stdin)
model = next((item for item in body.get("models", []) if item.get("name") == "qwen3.5:4b"), None)
if not isinstance(model, dict) or not isinstance(model.get("digest"), str):
    raise SystemExit("Pulled model/digest missing from Ollama tags")
quantization = (model.get("details") or {}).get("quantization_level")
if quantization != "Q4_K_M":
    raise SystemExit(f"Unexpected model quantization: {quantization!r}")
target.write_text(json.dumps({
    "model": model["name"], "digest": model["digest"],
    "quantization": quantization, "runtime_version": "0.40.3",
    "runtime_asset_sha256": "ce6f8b671a699bf1ebafb28d60dbbabf4f37b30b499145fa308a8ba72de021d6",
}, sort_keys=True, indent=2) + "\n", encoding="utf-8")
' "$tmp"
chmod 600 "$tmp"
mv -- "$tmp" "$manifest"
trap - EXIT
echo "Model digest recorded at $manifest"
