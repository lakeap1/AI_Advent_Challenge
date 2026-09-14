from dataclasses import replace
import pytest
from agent import Agent, SQLiteStore, load_config
from agent.transport import TransportError
from agent.storage import StorageError
from test_compression import build, TranscriptTransport
from comparison import fact_check

def test_summary_token_limit_rejects_without_changing_boundary():
    agent, transport=build(summary_max_tokens=1)
    agent.run('Бриф'); agent.run('UV')
    result=agent.run('Экспорт')
    assert result.code=='compression_failed'
    state=agent.state()
    assert state['compression']['covered_through']==0
    summary=[r for r in state['requests'] if r['metadata'].get('kind')=='summary'][0]
    assert summary['code']=='summary_too_long'
    assert summary['usage']['total_tokens']==120
    assert summary['output_policy']['status']=='rejected'
    assert state['summary']['api_requests']==3

def test_invalid_input_does_not_start_compression():
    agent,transport=build()
    agent.run('Бриф');agent.run('UV')
    before=len(transport.calls)
    assert agent.run('  ').status=='rejected'
    assert len(transport.calls)==before
    assert agent.state()['compression']['revisions']==0

def test_transport_error_in_summary_preserves_memory_and_unknown_cost():
    agent,transport=build()
    for s in ('Бриф','UV','Материалы'): agent.run(s)
    before=agent.state()['compression']
    def fail(payload,timeout): raise TransportError('timeout')
    transport.create=fail
    assert agent.run('Импорт').code=='compression_failed'
    state=agent.state()
    assert state['compression']['summary']==before['summary']
    assert state['compression']['covered_through']==before['covered_through']
    assert state['summary']['cost_complete'] is False
    assert state['token_accounting']['complete'] is False

def test_cannot_open_same_database_in_different_modes(tmp_path):
    path=tmp_path/'chat.db'
    agent,_=build(path);agent.close()
    store=SQLiteStore(path)
    with pytest.raises(StorageError):
        Agent(replace(load_config(),context_mode='full'),TranscriptTransport(),store)
    store.close()

def test_fact_check_does_not_accept_boolean_as_integer():
    assert not fact_check('{"triangles":true}',{'triangles':1})
    assert not fact_check('{"logos":0}',{'logos':False})
    assert fact_check('{"logos":false}',{'logos':False})
