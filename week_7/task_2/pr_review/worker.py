"""Fixed SSH command: bounded JSON data in, safe review result out."""
import json
import os
import re
import sys

_LIMIT = 8 * 1024 * 1024


def execute_request(payload, run):
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "run_key", "snapshot"}
            or type(payload["schema_version"]) is not int or payload["schema_version"] != 1
            or not isinstance(payload["snapshot"], dict)
            or not isinstance(payload["run_key"], str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", payload["run_key"])):
        raise ValueError("invalid_worker_request")
    return run(payload["snapshot"], payload["run_key"])


def main():
    from .cli import _configure_utf8_output, _emit
    _configure_utf8_output()
    try:
        if os.environ.get("PR_REVIEW_ENABLE_LIVE") != "1":
            raise ValueError("live_confirmation_required")
        data_dir = os.environ.get("PR_REVIEW_DATA_DIR")
        if not data_dir:
            raise ValueError("review_data_directory_required")
        raw = sys.stdin.buffer.read(_LIMIT + 1)
        if len(raw) > _LIMIT:
            raise ValueError("json_input_too_large")
        payload = json.loads(raw.decode("utf-8"))
        from .runtime import run_review
        result = execute_request(payload, lambda snapshot, run_key: run_review(
            snapshot, run_key, data_dir=data_dir, service_root=os.environ.get("PR_REVIEW_SERVICE_ROOT")))
        _emit(result)
        return 0 if result.get("review_accepted") is True else 2
    except (ValueError, OSError, RecursionError, TypeError, ImportError) as exc:
        reason = str(exc) if type(exc) is ValueError else "worker_failed"
        _emit({"status": "error", "review_accepted": False, "error": reason})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
