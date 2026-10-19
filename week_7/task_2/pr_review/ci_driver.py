"""Trusted workflow_run handler. No PR code, shell text or artifact is executed."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import quote
from urllib.request import Request, urlopen

from .cli import _MAX_INPUT_BYTES, _emit, _read_json, _write_html
from .findings import snapshot_digest
from .git_context import prepare_snapshot
from .impact import assess_rag_impact

_OID = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RELATED = tuple("week_7/task_1/" + name for name in (
    "README.md", "docs/usage.md", "docs/architecture.md", "docs/api.md",
    "graphics/config.py", "graphics/provider.py", "graphics/policies.py",
    "graphics/store.py", "graphics/service.py", "graphics/retrieval.py",
    "graphics/chunking.py", "graphics/context.py", "corpus/manifest.json"))


def select_request(event, repository, fetch_pr):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("invalid_repository")
    run = event.get("workflow_run") if isinstance(event, dict) else None
    if (not isinstance(run, dict) or run.get("event") != "pull_request"
            or run.get("path") != ".github/workflows/day32-review-request.yml"
            or run.get("repository", {}).get("full_name") != repository):
        raise ValueError("untrusted_workflow_event")
    if run.get("head_repository", {}).get("full_name") != repository:
        return {"skip": "fork"}
    if run.get("conclusion") != "success":
        return {"skip": "request_failed"}
    requests = run.get("pull_requests")
    if not isinstance(requests, list) or len(requests) != 1:
        return {"skip": "no_single_pull_request"}
    number = requests[0].get("number") if isinstance(requests[0], dict) else None
    if type(number) is not int or number < 1:
        raise ValueError("invalid_pull_request_number")
    current = fetch_pr(number)
    if current.get("number") != number or current.get("base", {}).get("repo", {}).get("full_name") != repository:
        raise ValueError("repository_mismatch")
    if current.get("head", {}).get("repo", {}).get("full_name") != repository:
        return {"skip": "fork"}
    if current.get("state") != "open" or current.get("draft") is True:
        return {"skip": "closed_or_draft"}
    base, head = current.get("base", {}).get("sha"), current.get("head", {}).get("sha")
    if not isinstance(base, str) or not _OID.fullmatch(base) or not isinstance(head, str) or not _OID.fullmatch(head):
        raise ValueError("invalid_pull_request_revisions")
    if run.get("head_sha") != head:
        return {"skip": "stale_head"}
    return {"number": number, "base": base, "head": head}


def ssh_command(host, key_file, known_hosts_file):
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*@[A-Za-z0-9][A-Za-z0-9.-]*", host):
        raise ValueError("invalid_capsule_host")
    return ["ssh", "-F", os.devnull, "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes",
            "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=15",
            "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=2",
            "-o", "ClearAllForwardings=yes", "-o", "RequestTTY=no",
            "-o", "UserKnownHostsFile=" + str(known_hosts_file), "-i", str(key_file), host, "review"]


def _still_current(current, selected, repository):
    return (isinstance(current, dict) and current.get("number") == selected["number"]
            and current.get("state") == "open" and current.get("draft") is False
            and current.get("base", {}).get("repo", {}).get("full_name") == repository
            and current.get("head", {}).get("repo", {}).get("full_name") == repository
            and current.get("base", {}).get("sha") == selected["base"]
            and current.get("head", {}).get("sha") == selected["head"])


def _validate_worker_result(result, payload, returncode):
    if (not isinstance(result, dict) or type(result.get("review_accepted")) is not bool
            or result.get("status") not in {"accepted", "rejected", "incomplete", "error", "in_progress"}
            or result["review_accepted"] != (result["status"] == "accepted")
            or result.get("run_key") != payload["run_key"]
            or result.get("base_revision") != payload["snapshot"]["base_revision"]
            or result.get("head_revision") != payload["snapshot"]["head_revision"]
            or result["review_accepted"] and returncode != 0):
        raise ValueError("worker_status_or_identity_mismatch")
    return result


def _github_json(url, token):
    request = Request(url,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "grani-review"})
    with urlopen(request, timeout=20) as response:
        raw = response.read(1024 * 1024 + 1)
    if len(raw) > 1024 * 1024:
        raise ValueError("github_response_too_large")
    return json.loads(raw)


def _valid_branch_ref(ref):
    if not isinstance(ref, str) or not 1 <= len(ref) <= 255 or ref == "@":
        return False
    if (".." in ref or "@{" in ref or "//" in ref or ref.startswith("/")
            or ref.endswith("/") or any(char.isspace() or ord(char) < 32
                                       or 127 <= ord(char) <= 159 or char in "~^:?*[\\"
                                       for char in ref)):
        return False
    return all(part and not part.startswith((".", "-")) and not part.endswith((".", ".lock"))
               for part in ref.split("/"))


def _fetch_pr(repository, token, number):
    if (not isinstance(repository, str)
            or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
            or type(number) is not int or number < 1):
        raise ValueError("invalid_pull_request_lookup")
    current = _github_json(f"https://api.github.com/repos/{repository}/pulls/{number}", token)
    if not isinstance(current, dict) or not isinstance(current.get("base"), dict):
        raise ValueError("invalid_pull_request_metadata")
    base = current["base"]
    if not isinstance(base.get("repo"), dict) or base["repo"].get("full_name") != repository:
        raise ValueError("repository_mismatch")
    # These PRs cannot reach the worker; preserve their skip path without a ref lookup.
    head = current.get("head")
    if (current.get("state") != "open" or current.get("draft") is True
            or not isinstance(head, dict) or not isinstance(head.get("repo"), dict)
            or head["repo"].get("full_name") != repository):
        return current.copy()
    ref = base.get("ref")
    if not _valid_branch_ref(ref):
        raise ValueError("invalid_base_ref")
    url = f"https://api.github.com/repos/{repository}/git/ref/heads/{quote(ref, safe='')}"
    branch = _github_json(url, token)
    if (not isinstance(branch, dict) or branch.get("ref") != "refs/heads/" + ref
            or not isinstance(branch.get("object"), dict)
            or branch["object"].get("type") != "commit"
            or not isinstance(branch["object"].get("sha"), str)
            or not _OID.fullmatch(branch["object"]["sha"])):
        raise ValueError("invalid_branch_response")
    return {**current, "base": {**base, "sha": branch["object"]["sha"]}}


def _remote(payload, host, key, known_hosts):
    raw = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("worker_input_too_large")
    with tempfile.TemporaryDirectory(prefix="grani-review-ssh-") as directory:
        key_path = Path(directory) / "key"
        hosts_path = Path(directory) / "known_hosts"
        key_path.write_text(key.rstrip() + "\n", encoding="utf-8")
        hosts_path.write_text(known_hosts.rstrip() + "\n", encoding="utf-8")
        key_path.chmod(0o600)
        hosts_path.chmod(0o600)
        # API/SSH secrets are not inherited by the remote process environment.
        clean = {name: value for name, value in os.environ.items()
                 if name in {"PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "TEMP", "TMP"}}
        completed = subprocess.run(ssh_command(host, key_path, hosts_path), input=raw,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   timeout=1200, env=clean, check=False)
    if len(completed.stdout) > _MAX_INPUT_BYTES:
        raise ValueError("worker_output_too_large")
    try:
        result = json.loads(completed.stdout.decode("utf-8"))
    except (UnicodeError, ValueError):
        raise ValueError("worker_response_unavailable") from None
    return _validate_worker_result(result, payload, completed.returncode)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    output = Path(args.output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any((output / name).exists() for name in ("review.json", "review.html")):
        raise SystemExit("output_already_exists")
    result = {"status": "error", "review_accepted": False, "findings": [],
              "execution": "GitHub workflow_run → капсула", "reason": "CI не завершён"}
    try:
        repository = os.environ["GITHUB_REPOSITORY"]
        token = os.environ["GH_TOKEN"]
        lookup = lambda number: _fetch_pr(repository, token, number)
        selected = select_request(_read_json(os.environ["GITHUB_EVENT_PATH"]), repository, lookup)
        if "skip" in selected:
            result.update(status="skipped", reason=selected["skip"])
        else:
            host, key, known_hosts = (os.environ.get(name, "") for name in (
                "PR_REVIEW_SSH_HOST", "PR_REVIEW_SSH_KEY", "PR_REVIEW_KNOWN_HOSTS"))
            if not all((host, key, known_hosts)):
                raise ValueError("capsule_review_not_configured")
            snapshot = prepare_snapshot(args.repo, selected["base"], selected["head"], related_paths=_RELATED)
            snapshot["snapshot_sha256"] = snapshot_digest(snapshot)
            snapshot["impact"] = assess_rag_impact(snapshot["changed_files"])
            identity = f"day32-v1:{repository}:{selected['number']}:{selected['base']}:{selected['head']}"
            run_key = hashlib.sha256(identity.encode("ascii")).hexdigest()
            result = _remote({"schema_version": 1, "run_key": run_key, "snapshot": snapshot}, host, key, known_hosts)
            result["execution"] = "GitHub workflow_run → капсула"
            if (result.get("base_revision") != snapshot["base_revision"]
                    or result.get("head_revision") != snapshot["head_revision"]):
                raise ValueError("worker_revision_mismatch")
            current = lookup(selected["number"])
            if not _still_current(current, selected, repository):
                result.update(status="stale", review_accepted=False, findings=[], reason="PR изменился во время ревью")
    except Exception as exc:
        safe_codes = {"capsule_review_not_configured", "invalid_capsule_host", "worker_input_too_large",
                      "worker_output_too_large", "worker_response_unavailable", "invalid_worker_response",
                      "worker_status_mismatch", "worker_revision_mismatch", "untrusted_workflow_event",
                      "worker_status_or_identity_mismatch",
                      "invalid_pull_request_number", "invalid_pull_request_revisions", "repository_mismatch",
                      "invalid_pull_request_lookup", "invalid_pull_request_metadata", "invalid_base_ref",
                      "invalid_branch_response"}
        reason = str(exc) if type(exc) is ValueError and str(exc) in safe_codes else "ci_execution_failed"
        result.update(status="error", review_accepted=False, findings=[], reason=reason)
    _emit(result, output / "review.json")
    _write_html(result, output / "review.html")
    print(json.dumps({"status": result["status"], "review_accepted": result["review_accepted"]}))
    return 0 if result.get("review_accepted") is True or result["status"] == "skipped" else 2


if __name__ == "__main__":
    raise SystemExit(main())
