"""Opt-in live context comparison on explicit seeded conversation fixtures, not production benchmarks."""
import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from graphics.config import Settings
from graphics.service import ChatService
from graphics.store import Store


PROFILES = {
    'small': dict(input_budget=12000, history_budget=3000, summary_budget=1500,
                  sources_budget=4000, tools_budget=700, explicit_budget=2000, max_tokens=2000),
    'medium': dict(input_budget=24000, history_budget=6000, summary_budget=2500,
                   sources_budget=7000, tools_budget=1200, explicit_budget=3000, max_tokens=3000),
    'large': dict(input_budget=32000, history_budget=10000, summary_budget=3000,
                  sources_budget=9000, tools_budget=1500, explicit_budget=4000, max_tokens=4096),
}
NOTES = [
    'Кодовое имя нашей учебной сцены — Базальт. Мы сначала предполагали Built-in в Unity 6.3. Это условный учебный пример, настоящую сцену никто не открывал.',
    'Мы обсуждали, что материал хранит параметры, а шейдер определяет вычисления поверхности. Текстура — данные, которые шейдер может использовать: цвет, нормали или маски.',
    'Проверять normal map договорились двумя способами: поворачивать источник света и смотреть на изменение блика; затем посмотреть на силуэт сбоку, который сама normal map не меняет.',
    'Цель разговора — понять наблюдаемый эффект, а не написать программу. Термины нужно объяснять на примере каменной поверхности. Формулы сейчас не требуются.',
    'Направление нормали влияет на расчёт освещения. Геометрия и UV задают другую часть представления объекта. Не следует смешивать изображение поверхности с её настоящими вершинами.',
    'Мы сравнивали гладкую и шероховатую поверхность. Разница блика зависит и от освещения; один только параметр не описывает вид при любой среде.',
    'Цветовая текстура и карта данных интерпретируются по-разному. Условия импорта важны; речь идёт о принципе и конкретной версии, а не о любом существующем редакторе.',
    'Поправка к началу разговора: окончательно выбрали URP, а не Built-in. Версия Unity 6.3 остаётся прежней. Имя учебной сцены не меняли.',
]


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    path.chmod(0o600)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-live', action='store_true')
    parser.add_argument('--profiles', nargs='+', choices=PROFILES, default=list(PROFILES))
    parser.add_argument('--output', type=Path, help='Private result path; existing files are never overwritten')
    args = parser.parse_args()
    if not args.run_live:
        parser.error('No calls made. Explicit --run-live is required.')
    base = Settings.from_env()
    target = args.output or base.data_dir / 'context-calibration.json'
    if target.exists():
        parser.error('Result already exists; choose another --output to preserve the previous run.')
    store = Store(base.db_path)
    user = store.create_user('context-' + secrets.token_hex(5), secrets.token_urlsafe(24))
    store.update_profile(user, {'role': 'QA', 'level': 'beginner', 'depth': 'short'})
    result = {'started_at': datetime.now(timezone.utc).isoformat(), 'user_id': user,
              'summary_trigger_ratio': base.summary_trigger_ratio,
              'method': 'same seeded user-note history; real summary/preparation/retrieval/filter/generation/validator; one run per profile',
              'limits': 'keyword screening is not semantic grading; no statistical latency or globally optimal claim',
              'profiles': []}
    glossary = (base.app_root / 'corpus/glossary.md').read_text(encoding='utf-8')
    width = max(1, len(glossary) // len(NOTES))
    for name in args.profiles:
        settings = replace(base, **PROFILES[name])
        chat = store.create_chat(user, f'Контекст: {name}')
        branch = chat['branch_id']
        # Authored user notes are explicit fixture input, never fabricated model answers.
        for index, note in enumerate(NOTES):
            end = (index + 1) * width if index + 1 < len(NOTES) else len(glossary)
            quoted = glossary[index * width:end]
            store.append_message(user, branch, 'user', note + '\nЦитата из учебных заметок, не факт о пользователе:\n' + quoted)
        service = ChatService(settings, store)
        profile = {'name': name, 'budgets_bytes': PROFILES[name], 'cases': []}
        result['profiles'].append(profile)
        fresh = store.create_chat(user, f'Учебный ответ: {name}')['branch_id']
        cases = [
            ('summary_recall', branch, 'Напомни имя учебной сцены, окончательно выбранный render pipeline и обе проверки normal map, о которых мы договорились. Почему они подходят? Ответь коротко.'),
            ('learning_comparison', fresh, 'В чём разница между normal map и displacement? Объясни коротко: что происходит с освещением, геометрией и силуэтом.'),
            ('project_help', fresh, '/help'),
        ]
        for case_name, where, question in cases:
            before = {call['id'] for call in store.ledger(user)['calls']}
            started = time.monotonic()
            response = service.ask(user, where, question)
            elapsed = time.monotonic() - started
            calls = [call for call in store.ledger(user)['calls'] if call['id'] not in before]
            answer = response.get('answer', '').lower()
            snapshot = store.get_branch(user, where)
            checks = ({'scene_name': 'базальт' in answer, 'corrected_pipeline': 'urp' in answer,
                       'shape_check': any(word in answer for word in ['силуэт', 'геометр']),
                       'lighting_check': any(word in answer for word in ['свет', 'освещ'])}
                      if case_name == 'summary_recall' else {})
            case = {'name': case_name, 'branch_id': where, 'question': question,
                    'status': response['status'], 'seconds': round(elapsed, 3),
                    'summary_through': snapshot['summary_through'], 'summary': snapshot['summary'],
                    'original_messages_retained': len(snapshot['messages']), 'response': response,
                    'calls': calls, 'keyword_checks': checks}
            profile['cases'].append(case)
            result['ledger'] = store.ledger(user)
            save(target, result)
            print(json.dumps({'profile': name, 'case': case_name, 'status': response['status'],
                              'seconds': case['seconds'], 'summary_through': snapshot['summary_through'],
                              'keyword_checks': checks}, ensure_ascii=False), flush=True)
            if any(call['status'] in {'pending', 'unknown'} for call in calls):
                result['stopped_for_unknown_usage'] = True
                save(target, result)
                return 1
    result['finished_at'] = datetime.now(timezone.utc).isoformat()
    save(target, result)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
