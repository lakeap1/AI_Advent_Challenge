"""Fixed snapshots, counterbalanced model blocks and read-only result analysis."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def stats(values):
    values = [v for v in values if type(v) in (int, float)]
    return {'count': len(values), 'median': statistics.median(values) if values else None,
            'min': min(values) if values else None, 'max': max(values) if values else None}


def execution_order(index, presets):
    return list(presets if index % 2 == 0 else reversed(presets))


def analyze(run):
    """Only observations; semantic grades are supplied by independent review."""
    items = [item for case in run.get('cases', []) for item in case.get('results', [])]
    names = list(dict.fromkeys(item['preset'] for item in items))
    presets = {}
    for name in names:
        all_items = [item for item in items if item['preset'] == name]
        good = [item for item in all_items if item['status'] == 'ok']
        resource = [item.get('resources') or {} for item in good]
        warmups = [item for item in run.get('warmups', []) if item.get('preset') == name]
        receipts = [item.get('receipt') or {} for item in all_items + warmups]
        presets[name] = {
            'attempts': len(all_items), 'successes': len(good), 'failures': len(all_items) - len(good),
            'wall_seconds': stats([item.get('elapsed_seconds') for item in good]),
            'generation_seconds': stats([(item.get('metrics') or {}).get('eval_seconds') for item in good]),
            'load_seconds': stats([(item.get('metrics') or {}).get('load_seconds') for item in good]),
            'tokens_per_second': stats([(item.get('metrics') or {}).get('eval_tokens_per_second') for item in good]),
            'resident_vram_bytes': stats([(r.get('resident') or {}).get('size_vram') for r in resource]),
            'sampled_gpu_peak_mib': stats([r.get('sampled_gpu_peak_mib') for r in resource]),
            'sampled_gpu_peak_power_watts': stats([r.get('sampled_gpu_peak_power_watts') for r in resource]),
            'python_rss_bytes': stats([r.get('python_rss_bytes') for r in resource]),
            'known_cost_usd': sum(r['cost_usd'] for r in receipts if type(r.get('cost_usd')) in (int, float)),
            'unknown_usage': any(r.get('usage_status') not in ('complete', 'not_requested') or r.get('cost_usd') is None for r in receipts),
            'warmup_calls': len(warmups),
            'parameters': good[0].get('parameters') if good else None,
        }
    return {'run_id': run.get('run_id'), 'created_at': run.get('created_at'),
            'completed': run.get('completed', False), 'selected_preset': run.get('selected_preset'),
            'ledger': run.get('ledger'),
            'presets': presets, 'quality': run.get('quality'),
            'limitations': ['Пять вопросов Unity/URP; три повтора на одной машине.',
                            'Чередование блоков моделей; внутри блока одинаковый порядок вопросов.',
                            'Cold/reconfiguration warmups отдельно от измеряемых ответов.',
                            'VRAM/utilization/power sampled по всей GPU; это не точный peak и не энергия.',
                            'Python RSS относится к процессу benchmark; /api/ps показывает resident allocation Ollama.',
                            'Качество требует проверки полных ответов и источников; параметры сами его не доказывают.']}


def measured_generate(service, question, snapshot, preset, measure):
    def generate():
        try:
            return service.generate(question['prompt'], snapshot, preset)
        except Exception as error:
            if not isinstance(getattr(error, 'code', None), str):
                raise
            return {'preset': preset, 'model': None, 'status': 'not_run', 'answer': None,
                    'source_labels': [], 'elapsed_seconds': None, 'metrics': {},
                    'receipt': {'usage_status': 'not_requested', 'cost_usd': 0},
                    'error': {'code': error.code, 'message': getattr(error, 'message', str(error))}}
    if not measure:
        return generate()
    from scripts.resources import ResourceSampler
    with ResourceSampler() as sampler:
        item = generate()
    item['resources'] = sampler.report(item.get('model'))
    return item


def run_series(service, questions, snapshots, output, *, repeats=3,
               presets=('baseline', 'optimized'), measure=True, warmups=None,
               reset_runtime=False):
    output = Path(output)
    if output.exists():
        raise FileExistsError(output)
    if type(repeats) is not int or repeats < 1 or not presets:
        raise ValueError('Invalid series configuration')
    run = {'version': 1, 'run_id': uuid4().hex,
           'created_at': datetime.now(timezone.utc).isoformat(), 'completed': False,
           'repeats': repeats, 'presets': list(presets), 'questions': questions,
           'snapshots': snapshots, 'cases': [], 'warmups': [], 'pending': None,
           'order': [], 'error': None, 'runtime_events': []}
    save(output, run)
    index = {}
    for repeat in range(repeats):
        for preset in execution_order(repeat, presets):
            run['order'].append({'repeat': repeat + 1, 'preset': preset})
            if reset_runtime:
                from scripts.resources import unload_models
                models = [service.presets[name]['model'] for name in presets]
                # Keep the embedder out of generator resource comparisons too.
                models.append(service.rag.config.local['embedding_model'])
                try:
                    events = unload_models(models)
                    run['runtime_events'].append({'repeat': repeat + 1, 'preset': preset, 'events': events})
                except Exception as error:
                    run['error'] = {'stage': 'runtime_unload', 'detail': type(error).__name__}
                    save(output, run)
                    return run
                save(output, run)
            # One explicitly recorded warmup per block avoids confusing runner
            # reconfiguration/loading with warm generation. Every usage remains
            # accounted in the service ledger and raw run, even for warmups.
            if (measure if warmups is None else warmups) and questions:
                first = questions[0]
                run['pending'] = {'warmup': True, 'repeat': repeat + 1, 'preset': preset}
                save(output, run)
                item = measured_generate(service, first, snapshots[first['id']], preset, measure)
                run['warmups'].append({'repeat': repeat + 1, **item})
                run['pending'] = None
                save(output, run)
                if item['status'] != 'ok':
                    run['error'] = {'stage': 'warmup', 'preset': preset, 'detail': item.get('error')}
                    save(output, run)
                    return run
            for question in questions:
                key = (repeat, question['id'])
                if key not in index:
                    case = {'repeat': repeat + 1, 'question_id': question['id'], 'results': []}
                    index[key] = case
                    run['cases'].append(case)
                run['pending'] = {'repeat': repeat + 1, 'question_id': question['id'], 'preset': preset}
                save(output, run)
                item = measured_generate(service, question, snapshots[question['id']], preset, measure)
                index[key]['results'].append(item)
                run['pending'] = None
                save(output, run)
                print(f"{repeat + 1} {question['id']} {preset}: {item['status']} {item.get('elapsed_seconds')}s", flush=True)
                if item['status'] != 'ok':
                    run['error'] = {'stage': 'generation', 'preset': preset, 'detail': item.get('error')}
                    save(output, run)
                    return run
    run['completed'] = True
    save(output, run)
    return run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, default=ROOT / 'data')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--presets', nargs='+', default=['baseline', 'optimized'],
                        choices=['baseline', 'optimized', 'quantized'])
    parser.add_argument('--snapshots', type=Path, help='Reuse frozen snapshots from another run')
    parser.add_argument('--import-index', type=Path)
    parser.add_argument('--analyze', type=Path)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('Output already exists; preserve previous evidence')
    if args.analyze:
        run = json.loads(args.analyze.read_text('utf-8'))
        save(args.output, analyze(run))
        return 0
    from optimization import OptimizationService
    from scripts.resources import runtime_metadata
    service = OptimizationService(args.data_dir)
    if args.import_index:
        result = service.import_index(args.import_index)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result['status'] != 'ok':
            return 1
    questions = json.loads((ROOT.parent / 'task_3/evaluation/questions.json').read_text('utf-8'))['questions']
    if args.snapshots:
        previous = json.loads(args.snapshots.read_text('utf-8'))
        if previous['questions'] != questions:
            parser.error('Frozen run uses different questions')
        snapshots = previous['snapshots']
    else:
        snapshots = {q['id']: service.prepare(q['prompt']) for q in questions}
    metadata = runtime_metadata()
    run = run_series(service, questions, snapshots, args.output,
                     repeats=args.repeats, presets=tuple(args.presets), reset_runtime=True)
    run['runtime'] = metadata
    run['ledger'] = service.state()['ledger']
    save(args.output, run)
    return 0 if run['completed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
