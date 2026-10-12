"""Read-only metrics + independently graded quality, bound to exact raw bytes."""
import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.benchmark import analyze, save, stats


def read(path):
    data = path.read_bytes()
    return json.loads(data), hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--review', type=Path, required=True)
    parser.add_argument('--q8-run', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--selected', choices=['baseline', 'optimized'], required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('Output exists; preserve earlier evidence')
    run, raw_hash = read(args.run)
    review, review_hash = read(args.review)
    if not run.get('completed') or review['scope']['input_sha256'] != raw_hash:
        parser.error('Quality review must match a completed run exactly')
    report = analyze(run)
    for name, summary in report['presets'].items():
        warmups = [w for w in run.get('warmups', []) if w.get('preset') == name]
        summary['warmup_wall_seconds'] = stats([w.get('elapsed_seconds') for w in warmups])
    report.update(selected_preset=args.selected, quality=review['quality'],
                  raw_sha256=raw_hash, review_sha256=review_hash,
                  quality_method=review['rubric'],
                  selection_reason='Приоритет — полнота и обоснованность ответа по источникам; скорость показана отдельно.')
    report['limitations'].extend([
        'Это пять development-вопросов, использованных при настройке, а не независимый holdout-набор.',
        'Время запроса генерации включает HTTP /api/chat и проверку ответа; поиск, metadata и запись receipt вне таймера. Ollama eval приведён отдельно.',
        'Ноль USD относится к известному токенному API этой успешной серии; инфраструктура не измерена.',
        'Прежние неудачные эксперименты сохранены отдельно с unknown usage и блокировкой продолжения.'
    ])
    if args.q8_run:
        q8, q8_hash = read(args.q8_run)
        report['quantization_experiment'] = {
            'run_sha256': q8_hash, 'completed': q8.get('completed', False),
            'quality_status': 'NOT RUN' if not q8.get('cases') else 'NOT GRADED',
            'measured_answers': sum(len(c['results']) for c in q8.get('cases', [])),
            'error': q8.get('error'), 'ledger': q8.get('ledger'),
            'warmups': [{k: w.get(k) for k in ('preset', 'model', 'status', 'elapsed_seconds', 'error', 'receipt', 'resources')}
                        for w in q8.get('warmups', [])],
        }
        report['limitations'].append('Q8_0 установлена, но запуск завершился CUDA-ошибкой; ответов нет, качество Q8 — NOT RUN, повтор не выполнялся.')
    save(args.output, report)
    if read(args.run)[1] != raw_hash or read(args.review)[1] != review_hash:
        raise RuntimeError('Read-only analysis changed its input')
    print(json.dumps({'output': str(args.output), 'raw_sha256': raw_hash,
                      'selected': args.selected, 'quality': report['quality']}, ensure_ascii=False))


if __name__ == '__main__':
    main()
