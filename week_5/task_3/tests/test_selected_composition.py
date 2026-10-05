"""Model selection -> real MCP wire -> continuation, accounting and failure stop."""
import asyncio
import json
import os
from pathlib import Path
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent import Agent, SQLiteStore, load_config
from agent.composition import CompositionAgent
from agent.composition_action import CompositionAction
from composition.store import CompositionStore
from test_automatic_mcp import tool_reply
from test_strategies import Fake, response


class WirePipeline(CompositionAgent):
    def execute(self, run_id, before_save=None):
        async def wire():
            params = StdioServerParameters(command=sys.executable,
                args=[str(Path(__file__).with_name('mcp_fixture_server.py'))],
                env={**os.environ, 'TEST_COMPOSITION_DIR': str(self.store.root), 'PYTHONIOENCODING': 'utf-8'})
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await self.run_session(run_id, session, before_save=before_save)
        asyncio.run(wire())


@pytest.mark.parametrize('source', ['unknown', 'https://example.org', None])
def test_invalid_model_source_does_not_start_composition(tmp_path, source):
    transport = Fake([tool_reply('research_and_save', dict(question='Разбор',query='normal mapping',source=source))])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path/'chat.sqlite3'))
    agent.profile_id = 1; agent._dialogue_id = 1
    store = CompositionStore(tmp_path)
    agent._composition_action = CompositionAction(store, WirePipeline(store))
    result = agent.run('Подготовь обзор normal mapping')
    assert result.code == 'invalid_tool_call'
    assert not store.runs(1, 1) and not list(store.results.glob('*.txt'))
    assert agent.state()['summary']['api_requests'] == 1
    agent.close()


@pytest.mark.parametrize('refusal', [False, True])
@pytest.mark.parametrize('source,host', [('blender','blender.stackexchange.com'),
    ('computergraphics','computergraphics.stackexchange.com'), ('gamedev','gamedev.stackexchange.com'),
    ('wikipedia_en','en.wikipedia.org'), ('wikipedia_ru','ru.wikipedia.org')])
def test_selected_chain_wire_data_continuation_and_cost(tmp_path, monkeypatch, refusal, source, host):
    monkeypatch.setenv('TEST_REFUSAL', '1' if refusal else '0')
    store = CompositionStore(tmp_path)
    transport = Fake([tool_reply('research_and_save', {'question':'Проверь normal map', 'query':'normal map seams', 'source':source}),
                      response('Разбор сохранён.')])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path/'chat.sqlite3'))
    agent.profile_id = 1
    agent._dialogue_id = 1
    agent._composition_action = CompositionAction(store, WirePipeline(store))
    result = agent.run('Найди материалы о швах normal map, обработай и сохрани разбор в файл')
    run = store.latest(1, 1)
    observed = [json.loads(line) for line in (tmp_path/'wire-observations.jsonl').read_text(encoding='utf-8').splitlines()]
    assert observed[1]['arguments'] == run['materials']
    assert run['source'] == run['materials']['source'] == source
    assert host in run['materials']['sources'][0]['url']
    payload = json.loads((tmp_path/'summary-payload.json').read_text(encoding='utf-8'))
    assert host in payload['input'][0]['content']
    assert run['materials']['sources'][0]['excerpt'] in payload['input'][0]['content']
    assert run['parent_request_id'] == result.request_id
    assert len(agent.state()['messages']) == (1 if refusal else 2)
    assert agent.state()['summary']['api_requests'] == (2 if refusal else 3)
    assert agent.state()['token_accounting']['known_total_tokens'] == (230 if refusal else 340)
    if refusal:
        assert result.status == 'error'
        assert [row['step'] for row in observed] == ['search','summarize']
        assert len(transport.calls) == 1 and not list(store.results.glob('*.txt'))
    else:
        assert result.status == 'ok'
        assert host in run['saved']['content']
        if source != 'blender':
            assert 'Blender Stack Exchange' not in run['saved']['content']
        assert [row['step'] for row in observed] == ['search','summarize','save']
        assert observed[2]['arguments'] == run['summary']
        assert (store.results/run['saved']['filename']).read_text(encoding='utf-8') == run['summary']['content']
        output = transport.calls[1]['input'][-1]
        assert output['type'] == 'function_call_output'
        assert json.loads(output['output'])['saved']['content'] == run['summary']['content']
        transport.replies.append(response('Деталь сохранённого разбора.'))
        agent.run('Что сказано про UV в сохранённом разборе?')
        artifact_message = next(m['content'] for m in transport.calls[-1]['input']
                                if m.get('content','').startswith('Сохранённые материалы'))
        assert run['summary']['content'] in json.loads(artifact_message.split('\n',1)[1])
    agent.accept_composition(run)
    assert agent.state()['summary']['api_requests'] == (2 if refusal else 4)
    agent.close()


def test_active_output_rule_rejects_summary_before_mcp_save(tmp_path, monkeypatch):
    from profiles import ProfileWorkspace
    from test_invariants import verdict, RULE
    monkeypatch.setenv('TEST_REFUSAL', '0')
    store = CompositionStore(tmp_path)
    transport = Fake([verdict(), tool_reply('research_and_save',
        {'question':'Проверь normal map', 'query':'normal map seams', 'source':'blender'}), verdict(True)])
    ws = ProfileWorkspace(tmp_path/'profiles-test', transport)
    ws.memory.set_invariants(1, 0, [RULE])
    agent = ws.agent()
    agent._composition_action = CompositionAction(store, WirePipeline(store))
    result = agent.run('Найди и сохрани разбор normal map')
    run = store.latest(1, 1)
    observed = [json.loads(line)['step'] for line in (tmp_path/'wire-observations.jsonl').read_text(encoding='utf-8').splitlines()]
    assert result.status == 'error' and run['status'] == 'rejected'
    assert observed == ['search','summarize'] and not list(store.results.glob('*.txt'))
    assert ws.state()['summary']['api_requests'] == 4  # input guard, selection, summary, output guard
    assert len(ws.state()['messages']) == 1
    ws.close()


def test_repeated_composition_is_not_executed_again(tmp_path, monkeypatch):
    monkeypatch.setenv('TEST_REFUSAL', '0')
    store = CompositionStore(tmp_path)
    args = {'question':'Проверь normal map', 'query':'normal map seams', 'source':'blender'}
    transport = Fake([tool_reply('research_and_save',args), tool_reply('research_and_save',args,'second')])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path/'chat.sqlite3'))
    agent.profile_id = 1; agent._dialogue_id = 1
    agent._composition_action = CompositionAction(store, WirePipeline(store))
    result = agent.run('Найди и сохрани разбор normal map')
    assert result.code == 'composition_limit'
    assert len(store.runs(1, 1)) == 1 and len(list(store.results.glob('*.txt'))) == 1
    assert agent.state()['summary']['api_requests'] == 3
    agent.close()


def test_progress_remains_available_during_ask_lock_and_checks_owner(tmp_path):
    from app import create_app
    from threading import Thread, Event
    app = create_app(data_dir=tmp_path, transport=Fake([]))
    client = app.test_client(); client.get('/api/state')
    run = app.extensions['composition_store'].create(1,1,'Вопрос','query',parent_request_id=1)
    finished = Event(); results = []
    def poll():
        results.append(app.test_client().get('/api/composition/progress?profile_id=1&dialogue_id=1'))
        finished.set()
    with app.extensions['workspace'].lock:
        thread = Thread(target=poll); thread.start()
        available = finished.wait(3)
    thread.join(3)
    assert available and results[0].json['run']['id'] == run['id']
    assert client.get('/api/composition/progress?profile_id=2&dialogue_id=1').status_code == 404
    app.extensions['workspace'].close()


def test_direct_answer_and_input_rejection_do_not_start_pipeline(tmp_path):
    store = CompositionStore(tmp_path)
    transport = Fake([response('Объясню понятие.')])
    agent = Agent(load_config(), transport, SQLiteStore(tmp_path/'chat.sqlite3'))
    agent._composition_action = CompositionAction(store, WirePipeline(store))
    assert agent.run('Объясни нормаль поверхности').status == 'ok'
    assert agent.run('').status == 'rejected'
    assert len(transport.calls) == 1 and store.latest(1, 1) is None
    agent.close()
