"""Actual auxiliary model receipts survive invalid and paid Responses outcomes."""
import pytest

from agent.config import load_config
from agent.core import Agent, AgentResult
from agent.storage import SQLiteStore
from agent.orchestration_action import run_model_step
from orchestration.runner import OrchestrationError


class Transport:
    def __init__(self, response):
        self.response = response
        self.payloads = []

    def create(self, payload, timeout):
        self.payloads.append(payload)
        return self.response


def test_paid_invalid_tool_response_is_charged_once_without_chat_messages():
    response = {'status':'completed','model':'gpt-6-luna','service_tier':'default',
                'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30,
                         'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},
                         'output_tokens_details':{'reasoning_tokens':0}},
                'output':[{'type':'function_call','name':'foreign','call_id':'c1','arguments':'{}','status':'completed'}]}
    transport = Transport(response)
    agent = Agent(load_config(), transport, SQLiteStore(':memory:'))
    pending = agent._record(AgentResult('error','Pending',usage_status='unavailable'), {'kind':'answer'})
    pending['status'] = 'pending'
    parent = agent._store.begin(pending, 'Question')
    raw = run_model_step(agent,parent,[{'role':'user','content':'Question'}],[], 'auto')
    assert raw == response
    requests = agent._store.state()['requests']
    children = [r for r in requests if r['metadata'].get('kind') == 'orchestration_step']
    assert len(children) == 1
    assert children[0]['usage']['total_tokens'] == 30
    assert children[0]['cost_usd'] == '0.000007'
    assert len(agent._store.state()['messages']) == 1
    assert transport.payloads[0]['parallel_tool_calls'] is False
    assert 'Markdown' in transport.payloads[0]['instructions']


def test_transport_failure_leaves_unknown_cost_receipt():
    from agent.transport import TransportError
    class Failing:
        def create(self, payload, timeout):
            raise TransportError('timeout', request_started=True)
    agent = Agent(load_config(), Failing(), SQLiteStore(':memory:'))
    pending = agent._record(AgentResult('error','Pending',usage_status='unavailable'), {'kind':'answer'})
    pending['status'] = 'pending'
    parent = agent._store.begin(pending, 'Question')
    with pytest.raises(OrchestrationError):
        run_model_step(agent,parent,[{'role':'user','content':'Question'}],[], 'auto')
    child = next(r for r in agent._store.state()['requests'] if r['metadata'].get('kind')=='orchestration_step')
    assert child['usage_status']=='unavailable' and child['cost_usd'] is None


def test_paid_mixed_refusal_and_call_stops_before_mcp():
    response = {'status':'completed','model':'gpt-6-luna','service_tier':'default',
        'usage':{'input_tokens':20,'output_tokens':10,'total_tokens':30,
                 'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},
                 'output_tokens_details':{'reasoning_tokens':0}},
        'output':[{'type':'message','role':'assistant','status':'completed','content':[{'type':'refusal','refusal':'No'}]},
                  {'type':'function_call','name':'library__save_report','call_id':'c1','arguments':'{}','status':'completed'}]}
    agent = Agent(load_config(), Transport(response), SQLiteStore(':memory:'))
    pending = agent._record(AgentResult('error','Pending',usage_status='unavailable'), {'kind':'answer'})
    pending['status']='pending'
    parent = agent._store.begin(pending,'Question')
    with pytest.raises(OrchestrationError):
        run_model_step(agent,parent,[{'role':'user','content':'Question'}],[], 'auto')
    child = next(r for r in agent._store.state()['requests'] if r['metadata'].get('kind')=='orchestration_step')
    assert child['cost_usd']=='0.000007' and child['output_policy']['status']=='rejected'
