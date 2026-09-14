from dataclasses import replace
import json
from agent import Agent, SQLiteStore, load_config
from test_agent import response

class TranscriptTransport:
    def __init__(self, reject_summary=False):
        self.calls = []
        self.reject_summary = reject_summary
    def create(self, payload, timeout):
        self.calls.append(payload)
        summary = 'Сожми' in payload['instructions']
        return response('Сундук: 1500 треугольников.' if summary else 'Ответ художнику.',
            status='incomplete' if summary and self.reject_summary else 'completed',
            model='gpt-5.6-luna', service_tier='default',
            usage={'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
                'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0}})

def build(path=':memory:', **settings):
    config = replace(load_config(), **{'context_mode':'compressed', 'keep_last_messages':3, 'summary_every_messages':1, **settings})
    transport = TranscriptTransport()
    return Agent(config, transport, SQLiteStore(path)), transport

def test_first_and_repeated_compression_partition_and_accounting(tmp_path):
    path = tmp_path / 'chat.db'
    agent, transport = build(path)
    for prompt in ('Бриф: сундук', '1500 треугольников', 'Текстура 512', 'Акцент бирюзовый'):
        assert agent.run(prompt).status == 'ok'
    answers = [p for p in transport.calls if 'Сожми' not in p['instructions']]
    summaries = [p for p in transport.calls if 'Сожми' in p['instructions']]
    assert len(summaries) == 2
    state = agent.state()
    memory = state['compression']
    assert memory['revisions'] == 2
    assert answers[-1]['input'][1:] == [
        {'role': m['role'], 'content': m['content']} for m in state['messages'][-4:-1]]
    assert len(answers[-1]['input']) == 4
    first = json.loads(summaries[0]['input'][0]['content'])
    second = json.loads(summaries[1]['input'][0]['content'])
    assert first['previous_summary'] == ''
    assert second['previous_summary'] == 'Сундук: 1500 треугольников.'
    assert set(m['id'] for m in first['messages']).isdisjoint(m['id'] for m in second['messages'])
    assert state['token_accounting']['known_total_tokens'] == 6 * 120
    agent.close()
    restored, _ = build(path)
    assert restored.state()['compression'] == memory
    restored.close()

def test_rejected_summary_stops_answer_and_keeps_previous_memory():
    agent, transport = build()
    agent.run('Первое'); agent.run('Второе')
    before = agent.state()['compression']
    transport.reject_summary = True
    result = agent.run('Третье')
    assert result.code == 'compression_failed'
    assert len(transport.calls) == 3
    after = agent.state()
    assert after['compression']['summary'] == before['summary']
    assert after['compression']['covered_through'] == 0
    assert after['token_accounting']['known_total_tokens'] == 360
    assert len(after['messages']) == 5
    agent.close()

def test_full_mode_never_summarizes():
    transport = TranscriptTransport()
    agent = Agent(replace(load_config(), context_mode='full'), transport, SQLiteStore(':memory:'))
    for i in range(5): agent.run(str(i))
    assert len(transport.calls) == 5
    assert len(transport.calls[-1]['input']) == 9
    assert agent.state()['compression']['revisions'] == 0
    agent.close()
