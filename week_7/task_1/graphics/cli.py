"""Local administrator entry point; never exposed as a web route."""
import argparse
from getpass import getpass
import json
import sys

from .config import Settings
from .retrieval import Retriever
from .store import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-user', help='Create a private account; password is requested interactively')
    create.add_argument('username')
    commands.add_parser('index', help='Build the complete Qwen retrieval index from the manifest')
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    try:
        if args.command == 'create-user':
            password = getpass('Пароль: ')
            confirmation = getpass('Повторите пароль: ')
            if password != confirmation:
                print('Пароли не совпадают; пользователь не создан.', file=sys.stderr)
                return 1
            user_id = Store(settings.db_path).create_user(args.username, password)
            print(json.dumps({'created': True, 'user_id': user_id}, ensure_ascii=False))
        else:
            print(json.dumps(Retriever(settings).build_index(), ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError) as error:
        print(f'Операция не выполнена: {type(error).__name__}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
