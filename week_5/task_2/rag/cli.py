"""Local RAG command line: answer, compare, evaluate, or transfer a verified index."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

from dotenv import load_dotenv

from .config import load_config
from .retrieval import read_index
from .service import RagService


def import_index(task_root, source, data_dir):
    """Backup then verify a complete source snapshot before publishing once."""
    origin, directory = Path(source), Path(data_dir)
    destination = directory / "indexing.sqlite3"
    if not origin.is_file() or origin.is_symlink():
        raise ValueError("Source index is missing or unsafe")
    directory.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError("An active target index already exists")
    temporary = directory / ("indexing." + uuid.uuid4().hex + ".tmp")
    try:
        with closing(sqlite3.connect(origin.resolve().as_uri() + "?mode=ro", uri=True)) as incoming:
            incoming.execute("PRAGMA query_only=ON")
            with closing(sqlite3.connect(temporary)) as outgoing:
                incoming.backup(outgoing)
        # Reader checks corpus bytes, contract, every structural row and vector.
        if destination.exists():
            raise ValueError("An active target index already exists")
        # Validate under its final basename in an isolated temporary directory.
        verify_dir = directory / ("verify." + uuid.uuid4().hex)
        verify_dir.mkdir()
        verify_path = verify_dir / "indexing.sqlite3"
        temporary.replace(verify_path)
        try:
            chunks = read_index(task_root, verify_dir, load_config())
            if destination.exists():
                raise ValueError("An active target index already exists")
            os.link(verify_path, destination)
        finally:
            if verify_path.exists():
                verify_path.unlink()
            verify_dir.rmdir()
        return {"status": "ok", "index": str(destination), "structural_chunks": len(chunks)}
    finally:
        if temporary.exists():
            temporary.unlink()


def evaluate(service, output):
    gold = service.questions()
    items = gold.get("questions") if isinstance(gold, dict) else None
    if not isinstance(gold, dict) or gold.get("version") != 1 or not isinstance(items, list) or len(items) != 10:
        raise ValueError("Evaluation requires exactly ten fixed questions")
    ids = set()
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"] or item["id"] in ids
            or not isinstance(item.get("question"), str) or not item["question"].strip()
            or not isinstance(item.get("expected_facts"), list) or not all(isinstance(x, str) and x for x in item["expected_facts"])
            or not isinstance(item.get("expected_sources"), list)
            or not all(isinstance(x, dict) and isinstance(x.get("file"), str) and isinstance(x.get("evidence"), str) for x in item["expected_sources"])
            or type(item.get("unanswerable")) is not bool):
            raise ValueError("Invalid fixed question entry")
        ids.add(item["id"])
    session = str(uuid.uuid4())
    report = {"version": 1, "config": {"model": service.config.model,
                                       "reasoning_effort": service.config.reasoning_effort,
                                       "service_tier": service.config.service_tier,
                                       "embedding_model": service.config.embedding_model,
                                       "embedding_dimensions": service.config.embedding_dimensions,
                                       "max_question_chars": service.config.max_question_chars,
                                       "max_output_tokens": service.config.max_output_tokens,
                                       "input_policy": service.config.input_policy,
                                       "output_policy": service.config.output_policy,
                                       "top_k": service.config.top_k,
                                       "max_context_tokens": service.config.max_context_tokens,
                                       "context_budget_method": service.config.context_budget_method,
                                       "tariff": service.config.tariff},
              "session_id": session, "questions": [],
              "summary": {"assessment": "pending", "known_cost_usd": 0, "complete": True},
              "status": "running"}
    output = Path(output)

    def save():
        report["summary"].update(service.state(session)["cumulative"])
        serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        for target in (service.data_dir / "evaluation.json", output):
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".tmp")
            temporary.write_text(serialized, encoding="utf-8", newline="\n")
            temporary.replace(target)

    save()
    for item in items:
        pair = service.compare(item["question"], session)
        rag = pair["rag"]
        report["questions"].append({"id": item["id"], "question": item["question"],
                                    "expected_facts": item["expected_facts"],
                                    "expected_sources": item["expected_sources"],
                                    "unanswerable": item["unanswerable"],
                                    "plain": pair["plain"], "rag": rag,
                                    "retrieval": {"sources": rag["sources"], "context": rag["context"]} if rag else None,
                                    "assessment": "pending"})
        if pair["status"] != "ok":
            report["status"] = "failed"
            save()
            return report
        save()
    report["status"] = "complete"
    save()
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Day 22 verified-index RAG assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--mode", choices=("rag", "plain"), required=True)
    ask.add_argument("--data-dir")
    ask.add_argument("--session-id")
    compare = commands.add_parser("compare")
    compare.add_argument("question")
    compare.add_argument("--data-dir")
    compare.add_argument("--session-id")
    evaluation = commands.add_parser("evaluate")
    evaluation.add_argument("--data-dir")
    evaluation.add_argument("--output", required=True)
    transfer = commands.add_parser("import-index")
    transfer.add_argument("--source", required=True)
    transfer.add_argument("--data-dir", required=True)
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    directory = Path(args.data_dir) if args.data_dir else root / "data"
    if args.command == "import-index":
        result = import_index(root, args.source, directory)
    else:
        load_dotenv(root / ".env", override=False)
        service = RagService(root, directory)
        try:
            if args.command == "ask":
                result = service.ask(args.question, args.mode, args.session_id or str(uuid.uuid4()))
            elif args.command == "compare":
                result = service.compare(args.question, args.session_id or str(uuid.uuid4()))
            else:
                result = evaluate(service, args.output)
        finally:
            service.close()
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["status"] in ("ok", "complete") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
