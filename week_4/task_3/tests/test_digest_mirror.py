import json
import sqlite3
from datetime import datetime, timedelta, timezone
import pytest
from agent.digest import DigestAgent, DigestError


def digest(run_id=1, source='blender'):
    start = datetime(2026, 9, 1, tzinfo=timezone(timedelta(hours=6))) + timedelta(days=run_id-1)
    stamp = int(start.timestamp())
    return dict(run_id=run_id, digest_date=start.date().isoformat(), window_start=stamp, window_end=stamp+86400, generated_at=stamp+86400,
                partial=False, text='Summary', question_count=1, unanswered_count=1,
                top_tags=[dict(tag='rendering', count=1)], source_counts={source: 1},
                source_status={source: 'ok'}, questions=[dict(source=source, question_id=1,
                title='Rendering question', url=f'https://{source}.stackexchange.com/questions/1/test',
                answer_count=0, tags=['rendering'], created_at=stamp+100, excerpt='Question excerpt')])


def test_daily_migration_hides_legacy_feed_and_resets_cursor_without_deleting_archive(tmp_path, monkeypatch):
    path = tmp_path / 'digest-audit.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE mirror(run_id INTEGER PRIMARY KEY,payload TEXT);'
                         'CREATE TABLE mirror_state(id INTEGER PRIMARY KEY,cursor INTEGER,state TEXT);'
                         'CREATE TABLE digest_selection(profile_id INTEGER PRIMARY KEY,run_id INTEGER);')
        db.execute('INSERT INTO mirror VALUES(?,?)', (50,json.dumps(digest(50))))
        db.execute('INSERT INTO mirror_state VALUES(1,50,NULL)')
        db.execute('INSERT INTO digest_selection VALUES(7,50)')
    agent = DigestAgent(tmp_path)
    assert agent.cached_digests() == []
    assert agent.selected(7) is None
    seen = []
    def call(tool, args=None):
        if tool == 'get_digest_state':
            return {}
        seen.append(args['after_run_id'])
        return dict(digests=[digest()],next_cursor=1,has_more=False)
    monkeypatch.setattr(agent, 'call', call)
    agent.sync()
    assert seen == [0]
    assert [d['run_id'] for d in agent.cached_digests()] == [1]
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT count(*) FROM mirror').fetchone()[0] == 1


@pytest.mark.parametrize('mutation', ['missing_date','wrong_window','same_date'])
def test_daily_cache_rejects_non_daily_or_duplicate_date_atomically(tmp_path, mutation):
    agent = DigestAgent(tmp_path)
    first, invalid = digest(), digest(2)
    if mutation == 'missing_date':
        invalid.pop('digest_date')
    elif mutation == 'wrong_window':
        invalid['window_start'] += 3600
    else:
        invalid = dict(first, run_id=2)
    with pytest.raises(DigestError):
        agent.cache_digests([first, invalid])
    assert agent.cached_digests() == []


def test_cross_source_ids_are_distinct_but_wrong_link_is_rejected(tmp_path):
    agent = DigestAgent(tmp_path)
    value = digest()
    value['questions'] += digest(source='graphicdesign')['questions']
    value.update(question_count=2, unanswered_count=2, top_tags=[dict(tag='rendering', count=2)],
                 source_counts={'blender':1, 'graphicdesign':1}, source_status={'blender':'ok', 'graphicdesign':'ok'})
    assert agent._validate_digest(value) == value
    value['questions'][1]['url'] = 'https://blender.stackexchange.com/questions/1/test'
    with pytest.raises(DigestError):
        agent._validate_digest(value)


def test_mirror_recovers_multiple_pages_and_is_idempotent(tmp_path, monkeypatch):
    agent = DigestAgent(tmp_path)
    async def response(tool, args):
        if tool == 'get_digest_state':
            return dict(schedule=dict(enabled=True, mode='cron', timezone='Asia/Omsk', hours=[0,6,12,18],
                        publication_hour=0, publication_timezone='Asia/Omsk', publication_window='previous_calendar_day',
                        window_hours=24, sources=['blender'], next_due=300, backoff_until=None), runs=[], latest=digest(3))
        cursor = args['after_run_id']
        return dict(digests=[digest(cursor+1)] if cursor<3 else [], next_cursor=min(cursor+1,3), has_more=cursor<2)
    monkeypatch.setattr(agent, '_request', response)
    agent.sync()
    agent.sync()
    assert [d['run_id'] for d in agent.cached_digests()] == [1,2,3]
    assert DigestAgent(tmp_path).get_cached(2)['text'] == 'Summary'


def test_selected_snapshot_is_immutable_and_survives_restart(tmp_path):
    agent = DigestAgent(tmp_path)
    agent.cache_digests([digest()])
    agent.select(7,1)
    changed = digest()
    changed['text'] = 'Mutated'
    with pytest.raises(DigestError):
        agent.cache_digests([changed])
    reopened = DigestAgent(tmp_path)
    assert reopened.selected(7) == 1
    assert reopened.selected(8) is None
    assert reopened.get_cached(1)['text'] == 'Summary'


def analysis(status='ok', **changes):
    value = dict(status=status, text='Проверьте свет и материалы.' if status == 'ok' else '',
                 attempt_id=1, started_at=1790000000, finished_at=1790000010,
                 usage=dict(input_tokens=100, output_tokens=20, total_tokens=120),
                 usage_status='known', cost_usd='0.0001',
                 input_policy=dict(status='passed'), output_policy=dict(status='passed'),
                 metadata=dict(run_id=1, digest_date='2026-09-01', actual_model='gpt-5.6-luna'))
    value.update(changes)
    return value


def test_analysis_updates_existing_publication_and_restores_without_changing_raw(tmp_path, monkeypatch):
    agent = DigestAgent(tmp_path)
    agent.cache_digests([digest()])
    agent.select(7, 1)
    added = dict(digest(), analysis=analysis())
    def call(tool, args=None):
        if tool == 'get_digest_state':
            return dict(latest=added)
        return dict(digests=[], next_cursor=1, has_more=False)
    monkeypatch.setattr(agent, 'call', call)
    agent.sync()
    restored = DigestAgent(tmp_path)
    assert restored.selected(7) == 1
    assert restored.get_cached(1) == added
    changed = dict(added, text='Подмена исходной сводки')
    with pytest.raises(DigestError):
        restored.cache_digests([changed])
    assert restored.get_cached(1) == added


def test_terminal_analysis_cannot_be_replaced_and_invalid_usage_is_rejected(tmp_path):
    agent = DigestAgent(tmp_path)
    saved = dict(digest(), analysis=analysis())
    agent.cache_digests([saved])
    changed = dict(digest(), analysis=analysis(text='Другой разбор'))
    with pytest.raises(DigestError):
        agent.cache_digests([changed])
    invalid = dict(digest(2), analysis=analysis(metadata=dict(run_id=2, digest_date='2026-09-02'),
                 usage=dict(input_tokens=100, output_tokens=20, total_tokens=999)))
    with pytest.raises(DigestError):
        agent.cache_digests([invalid])
    assert len(agent.cached_digests()) == 1


def test_canonical_legacy_link_without_slug_is_valid(tmp_path):
    value = digest()
    value['questions'][0]['url'] = 'https://blender.stackexchange.com/questions/1'
    assert DigestAgent(tmp_path)._validate_digest(value) == value


@pytest.mark.parametrize('field,value', [('source_counts', {'blender':100}), ('source_status', {'blender':'made-up'}), ('source_errors', {'unknown':'bad'})])
def test_contradictory_source_metadata_is_rejected(tmp_path, field, value):
    payload = digest()
    payload[field] = value
    with pytest.raises(DigestError):
        DigestAgent(tmp_path)._validate_digest(payload)
