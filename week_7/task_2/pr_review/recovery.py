"""Manual, one-call acknowledgment of lost local embedding usage in a review DB."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

from .review_store import ReviewStore


def _existing_review_database(data_dir):
    directory = Path(data_dir)
    if (not directory.is_absolute() or not directory.is_dir() or directory.is_symlink()
            or directory.resolve() != directory.absolute()):
        raise ValueError("unsafe_or_missing_review_data_dir")
    path = directory / "review.sqlite3"
    if not path.is_file() or path.is_symlink() or path.stat().st_size < 100:
        raise ValueError("missing_or_unsafe_review_database")
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            tables = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('calls','review_runs','users')")}
            if tables != {"calls", "review_runs", "users"}:
                raise ValueError("not_a_review_database")
            if db.execute("SELECT 1 FROM users WHERE username='__review_runtime__'").fetchone() is None:
                raise ValueError("not_a_review_database")
    except sqlite3.DatabaseError as exc:
        raise ValueError("invalid_review_database") from exc
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Acknowledge one lost Day 32 local embedding usage receipt")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--call-id", required=True)
    parser.add_argument("--authorization-ref", required=True)
    parser.add_argument("--reason", required=True)
    args = parser.parse_args(argv)
    path = _existing_review_database(args.data_dir)
    receipt = ReviewStore(path).ack_local_embedding_usage_loss(
        args.call_id, args.authorization_ref, args.reason)
    print(json.dumps({key: receipt[key] for key in ("call_id", "resolution", "resolved_at_utc")}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        raise SystemExit(2) from None
