"""JSON-only command line boundary for preparation and mechanical validation."""

import argparse
import json
from pathlib import Path
import sys

from .findings import snapshot_digest, validate_findings
from .git_context import PreparationError, prepare_snapshot
from .impact import assess_rag_impact


_MAX_INPUT_BYTES = 8 * 1024 * 1024


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments:" + message)


def _parser():
    parser = _Parser(description="Подготовить Git-контекст, выполнить AI-ревью или проверить отчёт.")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    prepare = commands.add_parser("prepare", help="Подготовить ограниченный Git snapshot и план проверок")
    prepare.add_argument("--repo", required=True, help="Корень локального Git-репозитория")
    prepare.add_argument("--base", required=True, help="Базовый commit/ref")
    prepare.add_argument("--head", required=True, help="Проверяемый commit/ref")
    prepare.add_argument("--related", action="append", default=[], help="Дополнительный путь в репозитории")
    prepare.add_argument("--output", help="Новый JSON-файл; существующий файл не перезаписывается")
    validate = commands.add_parser("validate", help="Проверить структуру и ссылки на переданный snapshot")
    validate.add_argument("--preparation", required=True, help="JSON от prepare")
    validate.add_argument("--findings", required=True, help="JSON-отчёт с findings")
    validate.add_argument("--output", help="Новый JSON-файл; существующий файл не перезаписывается")
    review = commands.add_parser("review", help="AI/RAG-ревью через доверенные компоненты сервиса")
    review.add_argument("--preparation", required=True)
    review.add_argument("--data-dir", required=True, help="Отдельный постоянный каталог расходов ревью")
    review.add_argument("--run-key", required=True, help="Устойчивая идентичность события, без повторных расходов")
    review.add_argument("--service-root", help="Доверенная установленная папка task_1")
    review.add_argument("--run-live", action="store_true", help="Явно разрешить настоящие обращения к моделям")
    review.add_argument("--output", help="Новый JSON-файл")
    review.add_argument("--html", help="Новый HTML-файл отчёта")
    render = commands.add_parser("render", help="Показать сохранённый результат в HTML")
    render.add_argument("--report", required=True)
    render.add_argument("--output", required=True)
    return parser


def _read_json(path):
    with open(path, "rb") as stream:
        raw = stream.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("json_input_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except RecursionError as exc:
        raise ValueError("json_nesting_too_deep") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid_json_input") from exc


def _emit(value, output=None):
    try:
        encoded = json.dumps(value, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")) + "\n"
    except RecursionError as exc:
        raise ValueError("json_nesting_too_deep") from exc
    if len(encoded.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("json_output_too_large")
    if output is None:
        sys.stdout.write(encoded)
    else:
        with open(output, "x", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)


def _configure_utf8_output():
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="backslashreplace")


def _write_html(value, path):
    from .report import render_report
    html = render_report(value)
    if len(html.encode("utf-8")) > _MAX_INPUT_BYTES:
        raise ValueError("html_output_too_large")
    with open(path, "x", encoding="utf-8", newline="\n") as stream:
        stream.write(html)


def _new_paths(*paths):
    selected = [Path(path).resolve() for path in paths if path]
    if len(set(selected)) != len(selected):
        raise ValueError("output_paths_must_differ")
    if any(path.exists() for path in selected):
        raise ValueError("output_already_exists")


def main(argv=None):
    _configure_utf8_output()
    try:
        args = _parser().parse_args(argv)
        if args.command == "render":
            _new_paths(args.output)
            _write_html(_read_json(args.report), args.output)
            _emit({"status": "rendered", "output": args.output})
            return 0
        if args.command == "review":
            if not args.run_live:
                raise ValueError("live_confirmation_required")
            _new_paths(args.output, args.html)
            from .runtime import run_review
            result = run_review(_read_json(args.preparation), args.run_key,
                                data_dir=args.data_dir, service_root=args.service_root)
            _emit(result, args.output)
            if args.html:
                _write_html(result, args.html)
            return 0 if result.get("review_accepted") is True else 2
        if args.command == "prepare":
            snapshot = prepare_snapshot(args.repo, args.base, args.head,
                                        related_paths=args.related)
            impact = assess_rag_impact(snapshot["changed_files"])
            snapshot["snapshot_sha256"] = snapshot_digest(snapshot)
            snapshot.update({"impact": impact, "status": "prepared",
                             "review_status": "integration_required", "review_accepted": False,
                             "llm_calls": 0, "usage": None, "cost_usd": None,
                             "accounting_status": "no_request", "semantic_validation": "not_run"})
            _emit(snapshot, args.output)
            return 0
        snapshot = _read_json(args.preparation)
        findings = _read_json(args.findings)
        outcome = validate_findings(findings, snapshot)
        _emit(outcome, args.output)
        return 0 if outcome["valid"] else 2
    except (PreparationError, ValueError, OSError, TypeError, ImportError) as exc:
        reason = str(exc) if isinstance(exc, (PreparationError, ValueError)) else type(exc).__name__
        _emit({"status": "error", "error": reason})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
