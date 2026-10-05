"""Local RAG command line: answer, compare, evaluate, or transfer a verified index."""
import argparse
from contextlib import closing
import hashlib
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
    gold_path = service.task_root / "evaluation" / "questions.json"
    gold_bytes = gold_path.read_bytes()
    gold_sha256 = hashlib.sha256(gold_bytes).hexdigest()
    gold = json.loads(gold_bytes)
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
    frozen_items = json.loads(json.dumps(items, ensure_ascii=False, allow_nan=False))
    report = {"version": 2, "config": service.settings(), "gold_sha256": gold_sha256,
              "session_id": session, "questions": [],
              "summary": {"assessment": "pending", "known_cost_usd": 0, "complete": True, "unknown_calls": 0},
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
    for item in frozen_items:
        pair = service.compare(item["question"], session)
        report["questions"].append({"id": item["id"], "question": item["question"],
                                    "expected_facts": item["expected_facts"],
                                    "expected_sources": item["expected_sources"],
                                    "unanswerable": item["unanswerable"],
                                    "results": pair["results"],
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
    parser = argparse.ArgumentParser(description="Day 23 relevance and rewrite RAG assistant")
    commands = parser.add_subparsers(dest="command", required=True)
    ask = commands.add_parser("ask")
    ask.add_argument("question")
    ask.add_argument("--mode", choices=("plain", "rag", "rewrite", "filter", "rewrite_filter"), required=True)
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
    return 0 if result["status"] in ("ok", "complete", "no_context") else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError, sqlite3.Error) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
