from dataclasses import replace
import json
from agent import Agent, SQLiteStore, load_config
from dialogue_experiment import run, scenario, check_reply
from test_compression import TranscriptTransport


def test_experiment_starts_empty_generates_every_reply_and_counts_all_calls(tmp_path):
    agents={}
    transports={}
    def factory(mode):
        transport=TranscriptTransport()
        transports[mode]=transport
        agent=Agent(replace(load_config(),context_mode=mode),transport,SQLiteStore(':memory:'))
        agents[mode]=agent
        return agent
    result=run(tmp_path/'report.json',agent_factory=factory)
    assert len(scenario())==24
    assert len({t['topic'] for t in scenario()})==24
    assert result['complete']
    for mode, count in [('full',24),('compressed',31)]:
        item=result['modes'][mode]
        assert item['final_messages']==48
        assert item['cost']['api_requests']==count
        assert item['totals']['known_total_tokens']==count*120
        assert [t['history_messages_before'] for t in item['turns']]==list(range(0,48,2))
        assert all(r['metadata']['kind']!='fixture' for r in item['requests'])
    payloads=transports['full'].calls
    assert [len(p['input']) for p in payloads]==list(range(1,48,2))
    assert payloads[1]['input'][1]['content']=='Ответ художнику.'
    assert payloads[0]['input'][0]['content']==scenario()[0]['prompt']
    assert json.loads((tmp_path/'report.json').read_text(encoding='utf-8'))['complete']


def test_memory_check_requires_explanation_exact_fields_and_types():
    assert check_reply('{"explanation":"Теория","triangles":1200}',{'triangles':1200})
    assert not check_reply('{"explanation":"","triangles":1200}',{'triangles':1200})
    assert not check_reply('{"explanation":"Теория","triangles":true}',{'triangles':1})
    assert not check_reply('{"explanation":"Теория","triangles":1200,"extra":0}',{'triangles':1200})
