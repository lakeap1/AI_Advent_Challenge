from dataclasses import replace
import json
import pytest
from agent import Agent, SQLiteStore, load_config
from test_compression import TranscriptTransport


def make(path=':memory:'):
    config = replace(load_config(), keep_last_messages=6, summary_every_messages=6)
    transport = TranscriptTransport()
    return Agent(config, transport, SQLiteStore(path)), transport


def test_six_message_interval_counts_both_roles_and_keeps_pending_prefix():
    agent, transport = make()
    compressed_at = []
    covered_ids = []
    for turn in range(1, 25):
        before = len(transport.calls)
        assert agent.run(f'Вопрос {turn}').status == 'ok'
        calls = transport.calls[before:]
        if len(calls) == 2:
            compressed_at.append(turn)
            outgoing = json.loads(calls[0]['input'][0]['content'])['messages']
            covered_ids.extend(m['id'] for m in outgoing)
            assert len(calls[1]['input']) == 7  # summary + last six originals
        state = agent.state()
        raw = [m for m in state['messages'][:-1] if m['id'] > state['compression']['covered_through']]
        payload = calls[-1]['input']
        assert payload[-len(raw):] == [{'role':m['role'], 'content':m['content']} for m in raw]
    assert compressed_at == [4, 7, 10, 13, 16, 19, 22]
    assert len(covered_ids) == len(set(covered_ids))
    assert len(agent.state()['messages']) == 48
    assert len(transport.calls) == 31
    agent.close()


def test_interval_survives_restart_and_failed_summary_does_not_advance(tmp_path):
    path=tmp_path/'chat.db'
    agent,_=make(path)
    for i in range(5): agent.run(str(i))
    checkpoint=agent.state()['compression']['last_summary_message_id']
    agent.close()
    agent, transport=make(path)
    assert agent.state()['compression']['last_summary_message_id'] == checkpoint
    agent.run('Шестой')
    assert len(transport.calls) == 1
    transport.reject_summary=True
    assert agent.run('Седьмой').code == 'compression_failed'
    assert agent.state()['compression']['last_summary_message_id'] == checkpoint
    transport.reject_summary=False
    assert agent.run('Продолжить').status == 'ok'
    assert agent.state()['compression']['revisions'] == 2
    agent.close()


@pytest.mark.parametrize('value', [0, -1, True, 1.5, '6'])
def test_invalid_interval_rejected(value):
    with pytest.raises(ValueError): replace(load_config(), summary_every_messages=value)
