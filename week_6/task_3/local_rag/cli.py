"""Local index, state, and answer commands; JSON on stdout."""

import argparse
import json
import sys

from .service import LocalRag


def _write_json(value):
    if hasattr(sys.stdout, 'buffer'):
        sys.stdout.buffer.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        sys.stdout.buffer.flush()
    else:
        # Text-only stdout replacements still receive lossless JSON escapes.
        sys.stdout.write(json.dumps(value, ensure_ascii=True) + '\n')
        sys.stdout.flush()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Day 28 local graphics RAG')
    commands = parser.add_subparsers(dest='command', required=True)
    for name in ('build', 'status', 'ask'):
        command = commands.add_parser(name)
        command.add_argument('--data-dir')
        if name == 'ask':
            command.add_argument('--prompt', required=True)
            command.add_argument('--mode', choices=('local', 'cloud', 'compare'), default='local')
    args = parser.parse_args(argv)
    rag = LocalRag(args.data_dir or 'data')
    if args.command == 'build':
        result = rag.build_index()
    elif args.command == 'status':
        result = rag.state()
    else:
        result = rag.ask(args.prompt, mode=args.mode)
    _write_json(result)
    return 0 if args.command == 'status' or result['status'] in ('ok', 'no_context') else 2


if __name__ == '__main__':
    raise SystemExit(main())
