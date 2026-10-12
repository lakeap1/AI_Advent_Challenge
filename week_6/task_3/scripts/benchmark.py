"""Bounded paired Day 28 run and read-only analysis of saved results."""

import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
QUESTIONS = ROOT / "evaluation" / "questions.json"


def _save(path, value):
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _stats(values):
    return {"count": len(values), "median": statistics.median(values) if values else None,
            "min": min(values) if values else None, "max": max(values) if values else None}


def _usage_totals(receipts):
    totals = {key: 0 for key in ("input_tokens", "output_tokens", "total_tokens")}
    incomplete = 0
    for receipt in receipts:
        usage = receipt.get("usage") or {}
        if any(not isinstance(usage.get(key), int) for key in totals):
            incomplete += 1
        for key in totals:
            if isinstance(usage.get(key), int):
                totals[key] += usage[key]
    return {"known": totals, "incomplete_receipts": incomplete}


def analyze_run(run, questions):
    """Compute observable metrics only; keyword matching is explicitly a proxy."""
    by_id = {question["id"]: question for question in questions}
    cases = run.get("cases", [])
    providers = {}
    for name in ("local", "cloud"):
        attempts = len(cases) + int(bool(run.get("pending_case")))
        successful = []
        durations = []
        receipts = []
        present = 0
        hashes = {}
        matched = 0
        possible = 0
        for case in cases:
            item = next((item for item in (case.get("result") or {}).get("results", [])
                         if item.get("provider") == name), None)
            if item:
                present += 1
                if isinstance(item.get("receipt"), dict):
                    receipts.append(item["receipt"])
            if not item or item.get("status") != "ok" or not isinstance(item.get("answer"), str):
                continue
            successful.append(item)
            if isinstance(item.get("elapsed_seconds"), (int, float)):
                durations.append(item["elapsed_seconds"])
            digest = hashlib.sha256(item["answer"].encode("utf-8")).hexdigest()
            hashes.setdefault(case["question_id"], set()).add(digest)
            for group in by_id.get(case["question_id"], {}).get("keyword_groups", []):
                possible += 1
                matched += any(word.casefold() in item["answer"].casefold() for word in group)
        providers[name] = {
            "attempts": attempts, "provider_calls": present, "not_attempted": attempts - present,
            "successes": len(successful), "failures": attempts - len(successful),
            "generation_seconds": _stats(durations), "usage_tokens": _usage_totals(receipts),
            "known_cost_usd": sum(receipt["cost_usd"] for receipt in receipts
                                  if isinstance(receipt.get("cost_usd"), (int, float))),
            "unknown_cost_receipts": sum(receipt.get("cost_usd") is None for receipt in receipts),
            "answer_hashes_by_question": {qid: {"distinct": len(values), "sha256": sorted(values)}
                                          for qid, values in hashes.items()},
            "keyword_proxy": {"matched_groups": matched, "possible_groups_in_successes": possible,
                              "semantic_grade": None,
                              "note": "Substring coverage only; human factual completeness/support is separate."},
        }
    hits = 0
    retrieval_count = 0
    identity = 0
    mismatch = 0
    query_times = []
    pair_times = []
    for case in cases:
        result = case.get("result") or {}
        retrieval = result.get("retrieval") or {}
        if retrieval:
            retrieval_count += 1
            expected = by_id.get(case["question_id"], {}).get("expected_file")
            hits += any(Path(source.get("file", "")).name == expected
                        for source in retrieval.get("sources", []))
        items = result.get("results") or []
        if len(items) == 2:
            hashes = [item.get("request_sha256") for item in items]
            if retrieval.get("request_sha256") and all(value == retrieval["request_sha256"] for value in hashes):
                identity += 1
            else:
                mismatch += 1
        if isinstance(case.get("query_embedding_seconds"), (int, float)):
            query_times.append(case["query_embedding_seconds"])
        if result.get("status") == "ok" and len(items) == 2 and all(item.get("status") == "ok" for item in items):
            if isinstance(case.get("elapsed_seconds"), (int, float)):
                pair_times.append(case["elapsed_seconds"])
    known_generation_cost = sum(provider["known_cost_usd"] for provider in providers.values())
    unknown_generation_cost = any(provider["unknown_cost_receipts"] for provider in providers.values())
    return {
        "protocol": run.get("protocol"), "run_status": run.get("status"),
        "attempts": len(cases) + int(bool(run.get("pending_case"))),
        "recorded_cases": len(cases), "pending_case": run.get("pending_case"),
        "retrieval": {"expected_file_hits": hits, "cases_with_retrieval": retrieval_count,
                      "expected_file_hit_rate": hits / len(cases) if cases else None,
                      "query_embedding_seconds": _stats(query_times)},
        "providers": providers, "pair_end_to_end_seconds": _stats(pair_times),
        "request_hash_identity": {"matching_cases": identity, "mismatching_cases": mismatch,
                                  "context_and_request_hashes": [
                                      {"question_id": case["question_id"], "repeat": case["repeat"],
                                       "context_sha256": ((case.get("result") or {}).get("retrieval") or {}).get("context_sha256"),
                                       "request_sha256": ((case.get("result") or {}).get("retrieval") or {}).get("request_sha256")}
                                      for case in cases]},
        "series_known_cost_usd": run.get("series_cost_usd", known_generation_cost),
        "series_total_unknown": (run.get("series_cost_usd", known_generation_cost) is None or
                                 unknown_generation_cost or bool(run.get("pending_case"))),
        "factual_grading": "NOT RUN by this utility; use frozen expected_facts and inspect complete answers",
    }


def _reserve_usd(service, prompt):
    from local_rag.service import SYSTEM_TEXT

    tariff = service.config.cloud
    input_rate = max(tariff["input_usd_per_million"],
                     tariff.get("cached_input_usd_per_million", 0),
                     tariff.get("cache_write_usd_per_million", 0))
    # A UTF-8 byte upper bound with a 2x margin for JSON/wrapper overhead.
    input_bound = 2 * (len(prompt.encode("utf-8")) +
                       len(SYSTEM_TEXT.encode("utf-8")) +
                       service.config.retrieval["context_bytes"] + 128)
    return (input_bound * input_rate +
            tariff["max_output_tokens"] * tariff["output_usd_per_million"]) / 1_000_000


def run_benchmark(service, questions, output, repeats=3, budget_usd=0.05):
    """One local-first compare per case. Persist a pending marker before each request."""
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing evidence: {output}")
    ledger = service.store.ledger()
    base = ledger.get("total_usd")
    run = {"protocol": "day28-paired-v1", "order": "local_then_cloud",
           "order_bias": "Fixed local-first order; no counterbalancing.",
           "base_cost_usd": base, "budget_usd": budget_usd, "cases": [],
           "status": "running", "series_cost_usd": 0 if base is not None else None}
    _save(output, run)
    if ledger.get("incomplete") or base is None:
        run["status"] = "stopped_unknown_cost"
        _save(output, run)
        return run
    if hasattr(service, "state") and service.state().get("index", {}).get("status") != "ready":
        run["status"] = "stopped_index_not_ready"
        run["error"] = "index_missing_or_stale"
        _save(output, run)
        return run
    for question in questions:
        for repeat in range(1, repeats + 1):
            ledger = service.store.ledger()
            known = ledger.get("total_usd")
            if ledger.get("incomplete") or known is None:
                run["status"] = "stopped_unknown_cost"
                _save(output, run)
                return run
            run["series_cost_usd"] = known - base
            if run["series_cost_usd"] + _reserve_usd(service, question["prompt"]) > budget_usd:
                run["status"] = "stopped_budget"
                _save(output, run)
                return run
            before = {row["id"] for row in service.store.receipts(limit=1_000_000)}
            run["pending_case"] = {"question_id": question["id"], "repeat": repeat}
            _save(output, run)
            started = time.monotonic()
            try:
                result = service.ask(question["prompt"], mode="compare")
            except Exception as error:
                run["status"] = "stopped_exception"
                run["pending_case"]["exception_type"] = type(error).__name__
                _save(output, run)
                return run
            elapsed = time.monotonic() - started
            new = [row for row in service.store.receipts(limit=1_000_000) if row["id"] not in before]
            embedding = next((row.get("elapsed_seconds") for row in new
                              if row.get("stage") == "query_embed"), None)
            run["cases"].append({"question_id": question["id"], "repeat": repeat,
                                 "elapsed_seconds": elapsed, "query_embedding_seconds": embedding,
                                 "result": result, "receipt_ids": sorted(row["id"] for row in new)})
            run.pop("pending_case", None)
            ledger = service.store.ledger()
            run["series_cost_usd"] = (ledger["total_usd"] - base
                                      if ledger.get("total_usd") is not None else None)
            if ledger.get("incomplete") or run["series_cost_usd"] is None:
                run["status"] = "stopped_unknown_cost"
            elif result.get("status") != "ok":
                run["status"] = "stopped_case_failure"
            _save(output, run)
            if run["status"] != "running":
                return run
    run["status"] = "complete"
    _save(output, run)
    return run


def _load_env_file(path):
    if path is None:
        return
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = stripped.partition("=")
        if separator and key.strip() == "OPENAI_API_KEY" and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = value.strip().strip('"\'')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--budget-usd", type=float, default=0.05)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--analyze", type=Path, help="Analyze an existing JSON without model calls")
    args = parser.parse_args(argv)
    if args.repeats < 1 or not math.isfinite(args.budget_usd) or args.budget_usd <= 0:
        parser.error("repeats and budget must be positive")
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    if args.analyze:
        report = analyze_run(json.loads(args.analyze.read_text(encoding="utf-8")), questions)
        if args.output:
            if args.output.exists():
                parser.error("refusing to overwrite an existing report")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            _save(args.output, report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    if not args.output:
        parser.error("--output is required for a live run")
    _load_env_file(args.env_file)
    from local_rag.service import LocalRag
    run = run_benchmark(LocalRag(args.data_dir), questions, args.output,
                        repeats=args.repeats, budget_usd=args.budget_usd)
    print(json.dumps({"status": run["status"], "cases": len(run["cases"]),
                      "output": str(args.output), "series_cost_usd": run["series_cost_usd"]},
                     ensure_ascii=False))
    return 0 if run["status"] == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())
