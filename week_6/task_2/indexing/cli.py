"""CLI for inspection, build, status, and complete JSON export."""
import argparse
import json
import os
from pathlib import Path
import sys

from .client import OpenAIEmbedder
from .config import load_config
from .corpus import read_corpus
from .service import IndexingService
from .store import IndexStore


def main(argv=None):
    parser = argparse.ArgumentParser(description="Index the bundled graphics assistant sources")
    parser.add_argument("command", choices=("inspect", "build", "summary", "export"))
    parser.add_argument("--data-dir", default=str(Path(__file__).resolve().parent.parent / "data"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    if args.command == "inspect":
        result = read_corpus(root).summary
    else:
        if args.data_dir == ":memory:":
            db_path = ":memory:"
        else:
            directory = Path(args.data_dir)
            directory.mkdir(parents=True, exist_ok=True)
            db_path = directory / "indexing.sqlite3"
        store = IndexStore(db_path)
        try:
            if args.command == "build":
                cfg = load_config()
                service = IndexingService(root, store, OpenAIEmbedder(cfg, api_key=os.environ.get("OPENAI_API_KEY")), cfg)
                result = service.build(force=args.force)
            elif args.command == "summary":
                result = store.summary()
            else:
                result = store.export()
        finally:
            store.close()
    serialized = json.dumps(result, ensure_ascii=False, allow_nan=False)
    if args.output:
        Path(args.output).write_text(serialized + "\n", encoding="utf-8")
    else:
        print(serialized)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
