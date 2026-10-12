import pytest
from agent.digest import DigestAgent, DigestError


def digest(run_id=1, source='blender'):
    return dict(run_id=run_id, window_start=100, window_end=200, generated_at=200,
                partial=False, text='Summary', question_count=1, unanswered_count=1,
                top_tags=[dict(tag='rendering', count=1)], source_counts={source: 1},
                source_status={source: 'ok'}, questions=[dict(source=source, question_id=1,
                title='Rendering question', url=f'https://{source}.stackexchange.com/questions/1/test',
                answer_count=0, tags=['rendering'], created_at=150, excerpt='Question excerpt')])


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
