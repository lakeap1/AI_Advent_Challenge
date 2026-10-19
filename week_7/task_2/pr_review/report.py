"""A standalone, escaped report. Rejected drafts are never rendered."""
from html import escape
import json


_STATUS = {"accepted": "Ревью принято", "rejected": "Ревью отклонено",
           "incomplete": "Проверка неполная", "error": "Ошибка проверки",
           "in_progress": "Проверка выполняется", "skipped": "Проверка пропущена",
           "stale": "Ревизия PR изменилась"}


def _text(value):
    if value is None:
        return "неизвестно"
    return escape(str(value), quote=True)


def _json(value):
    return _text(json.dumps(value, ensure_ascii=False, indent=2))


def render_report(report: dict) -> str:
    if not isinstance(report, dict):
        raise ValueError("invalid_report")
    status = report.get("status", "error")
    accepted = status == "accepted" and report.get("review_accepted") is True
    ledger = report.get("ledger") or {}
    if not isinstance(ledger, dict):
        ledger = {}
    findings = report.get("findings", []) if accepted else []
    if not isinstance(findings, list):
        findings = []
    parts = ['<!doctype html><html lang="ru"><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width, initial-scale=1">',
             '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'">',
             '<title>Грани — ревью изменений</title>',
             '<style>body{margin:0;background:#10131b;color:#e8edf7;font:19px/1.55 "Segoe UI",sans-serif}'
             'main{max-width:1240px;margin:auto;padding:42px}h1{font-size:34px;line-height:1.2}'
             'h2{font-size:24px}h3{font-size:21px}header{border-bottom:2px solid #839fdd;padding-bottom:24px}'
             '.eyebrow{letter-spacing:.14em;color:#a3b8e4;font-size:14px}.status{font-size:26px;color:#a3c0ff}'
             'article,section{background:#191e2a;border:1px solid #343e52;border-radius:12px;margin:24px 0;padding:26px}'
             '.grid{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px}.stat{background:#25334e;padding:14px;border-radius:8px}'
             '.muted{color:#b3bed0}pre,code{font-family:Consolas,monospace;white-space:pre-wrap;overflow-wrap:anywhere;font-size:16px}'
             'table{border-collapse:collapse;width:100%;font-size:16px}td,th{border-bottom:1px solid #343e52;text-align:left;padding:10px;overflow-wrap:anywhere}'
             'blockquote{border-left:3px solid #839fdd;margin-left:0;padding-left:18px;white-space:pre-wrap}'
             'p{white-space:pre-wrap;overflow-wrap:anywhere}@media(max-width:800px){main{padding:20px}.grid{grid-template-columns:1fr}}</style>',
             '<main><header><div class="eyebrow">ГРАНИ / КАЧЕСТВО СЕРВИСА</div>',
             '<h1>Ревью изменений кода и знаний</h1>',
             f'<div class="status">{_text(_STATUS.get(status, "Неизвестный статус"))}</div>',
             f'<p class="muted">{_text(report.get("reason", report.get("message", "")))}</p>',
             f'<p>Прогон: <code>{_text(report.get("run_key", report.get("run_id")))}</code></p>',
             f'<p>Base: <code>{_text(report.get("base_revision"))}</code><br>'
             f'Head: <code>{_text(report.get("head_revision"))}</code></p></header>',
             '<section><h2>Результат и границы</h2><div class="grid">',
             f'<div class="stat">Принятое ревью<br><strong>{"да" if accepted else "нет"}</strong></div>',
             f'<div class="stat">Смысловая проверка<br>{_text(report.get("semantic_validation", "not_run"))}</div>',
             f'<div class="stat">Подтверждённые находки<br>{len(findings)}</div></div>',
             f'<p>Уровень прогона: {_text(report.get("execution", "CLI / обработчик ревью"))}</p>',
             '<p class="muted">Принятие означает прохождение предусмотренных проверок. Оно не гарантирует отсутствия всех дефектов.</p></section>']
    if accepted and not findings:
        parts.append('<section><h2>Существенных проблем не найдено</h2><p>Принятый отчёт содержит пустой список находок.</p></section>')
    for index, item in enumerate(findings, 1):
        if not isinstance(item, dict):
            continue
        location = item.get("location") or {}
        if not isinstance(location, dict):
            location = {}
        parts.extend([f'<article><div class="eyebrow">{index} / {_text(item.get("severity"))} / {_text(item.get("kind"))}</div>',
                      f'<h2>{_text(item.get("title"))}</h2>',
                      f'<p><code>{_text(location.get("path"))}:{_text(location.get("start_line"))}–{_text(location.get("end_line"))}</code><br>'
                      f'<code>{_text(location.get("revision"))}</code></p>'])
        for key, label in (("problem", "Проблема"), ("conditions", "Условия проявления"),
                           ("consequence", "Последствия"), ("recommendation", "Рекомендация")):
            parts.append(f'<h3>{label}</h3><p>{_text(item.get(key))}</p>')
        for evidence in item.get("evidence", []):
            if isinstance(evidence, dict):
                parts.append(f'<h3>Основание</h3><blockquote>{_text(evidence.get("quote"))}</blockquote>'
                             f'<code>{_text(evidence.get("source_id"))}</code>')
        parts.append('</article>')
    if not accepted:
        parts.append('<section><h2>Черновик не опубликован</h2><p>Непринятые находки не выдаются как результат ревью.</p></section>')
    parts.extend(['<section><h2>Проверки RAG и покрытие</h2>',
                  f'<pre>{_json(report.get("rag_checks", {}))}</pre>',
                  f'<pre>{_json(report.get("coverage", {}))}</pre></section>',
                  '<section><h2>Токены и расходы</h2>',
                  f'<p>Вход: {_text(ledger.get("input_tokens"))} · Выход: {_text(ledger.get("output_tokens"))} · Всего: {_text(ledger.get("total_tokens"))}</p>',
                  f'<p>Оценка API, USD: {_text(ledger.get("cost_estimated_usd"))}<br>'
                  f'Известная часть, USD: {_text(ledger.get("known_cost_estimated_usd"))}<br>'
                  f'Полнота учёта: {"полный" if ledger.get("complete") is True else "неполный"}</p>',
                  '<p class="muted">Списание предоплаченного bundle неизвестно. API-оценка не подтверждает фактическое списание.</p>',
                  '<table><thead><tr><th>Этап</th><th>Статус</th><th>Input</th><th>Output</th><th>Total</th><th>Cached / Reasoning</th><th>USD</th></tr></thead><tbody>'])
    for call in ledger.get("calls", []):
        if not isinstance(call, dict):
            continue
        usage = call.get("usage") or {}
        parts.append('<tr>' + ''.join(f'<td>{_text(value)}</td>' for value in (
            call.get("stage"), call.get("status"), usage.get("input_tokens"), usage.get("output_tokens"),
            usage.get("total_tokens"), f'{usage.get("cached_input_tokens")} / {usage.get("reasoning_tokens")}',
            call.get("cost_estimated_usd"))) + '</tr>')
    parts.append('</tbody></table></section>')
    instance = ledger.get("instance_accounting")
    if isinstance(instance, dict):
        parts.extend(['<section><h2>Учёт всего контура</h2>',
                      f'<p>Известная часть API, USD: {_text(instance.get("known_cost_estimated_usd"))}<br>'
                      f'Полная оценка API, USD: {_text(instance.get("cost_estimated_usd"))}<br>'
                      f'Общий лимит, USD: {_text(instance.get("budget_usd"))}<br>'
                      f'Полнота учёта: {"полный" if instance.get("complete") is True else "неполный"}</p>',
                      '<p>Вызовы с неизвестным расходом: ' +
                      ', '.join(_text(call_id) for call_id in instance.get("unknown_call_ids", [])) + '</p>',
                      '<h3>Ручные решения оператора</h3>'])
        resolutions = instance.get("operator_resolutions", [])
        if isinstance(resolutions, list):
            for resolution in resolutions:
                if isinstance(resolution, dict):
                    parts.append('<p>' + ' · '.join(_text(resolution.get(key)) for key in
                                                    ("call_id", "resolution", "resolved_at_utc")) + '</p>')
        parts.append('</section>')
    parts.extend([
                  f'<section><h2>Путь выполнения</h2><pre>{_json(report.get("events", []))}</pre></section>',
                  '</main></html>'])
    return '\n'.join(parts)
