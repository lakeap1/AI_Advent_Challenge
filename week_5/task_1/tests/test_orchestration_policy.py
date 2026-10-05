"""Save gate receipts are durable and never double-charge model usage."""
import json

from agent.config import load_config
from agent.core import Agent, AgentResult
from agent.orchestration_action import OrchestrationAction, run_model_step
from agent.storage import SQLiteStore
from orchestration.store import OrchestrationStore


def response(output, input_tokens=20, output_tokens=10):
    return {'status':'completed','model':'gpt-6-luna','service_tier':'default',
        'usage':{'input_tokens':input_tokens,'output_tokens':output_tokens,
                 'total_tokens':input_tokens+output_tokens,
                 'input_tokens_details':{'cached_tokens':0,'cache_write_tokens':0},
                 'output_tokens_details':{'reasoning_tokens':0}},'output':output}


class Transport:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def create(self, payload, timeout):
        self.calls.append(payload)
        return self.replies.pop(0)


def setup(tmp_path, verdict=None):
    selected = response([{'type':'function_call','name':'library__save_report',
                          'call_id':'c-save','arguments':'{"report_id":"r1"}',
                          'status':'completed'}])
    replies = [selected]
    if verdict is not None:
        replies.append(response([{'type':'message','role':'assistant','status':'completed',
            'content':[{'type':'output_text','text':json.dumps({'checks':[{'id':'R1','violated':verdict}]})}]}]))
    transport = Transport(replies)
    agent = Agent(load_config(),transport,SQLiteStore(':memory:'))
    parent_record = agent._record(AgentResult('error','Pending',usage_status='unavailable'),{'kind':'answer'})
    parent_record['status']='pending'
    parent = agent._store.begin(parent_record,'Question')
    run_store = OrchestrationStore(tmp_path)
    run = run_store.create(1,2,1,'Question',parent)
    receipts = []
    run_model_step(agent,parent,[{'role':'user','content':'Question'}],[],'auto',receipt_ids=receipts)
    return agent, transport, OrchestrationAction(run_store), run, receipts[0]


def check_receipt(agent, action, run, selection_id, expected_code, expected_calls):
    rows = agent._store.state()['requests']
    selected = next(r for r in rows if r['id']==selection_id)
    policy = next(r for r in rows if r['metadata'].get('kind')=='orchestration_policy')
    assert selected['cost_usd']=='0.000007' and selected['usage']['total_tokens']==30
    assert policy['usage_status']=='not_requested' and policy['usage'] is None and policy['cost_usd'] is None
    assert policy['status']=='rejected' and policy['code']==expected_code
    assert policy['output_policy']['status']=='rejected'
    assert policy['metadata']['parent_request_id']==run['parent_request_id']
    assert policy['metadata']['selection_request_id']==selection_id
    event = action.store.get(run['id'])['events'][-1]
    assert event['stage']=='save' and event['status']=='rejected'
    assert event['policy_request_id']==policy['id'] and event['selection_request_id']==selection_id
    assert agent.state()['summary']['api_requests']==expected_calls
    assert len(agent._store.state()['messages'])==1
    assert not list(action.store.results.glob('*.txt'))


def test_rejected_report_policy_has_unbilled_receipt_and_no_save(tmp_path):
    agent, transport, action, run, selection_id = setup(tmp_path)
    agent._output_policy = lambda response: AgentResult('rejected','Report denied','report_policy')
    denied = action.check_before_save(agent,run,selection_id,'Report text')
    assert denied.startswith('Политика:')
    assert len(transport.calls)==1
    check_receipt(agent,action,run,selection_id,'report_policy',1)


def test_invariant_rejection_links_existing_paid_guard_without_double_charge(tmp_path):
    agent, transport, action, run, selection_id = setup(tmp_path,verdict=True)
    agent._invariants = lambda: {'task_id':1,'revision':1,
                                 'rules':[{'id':'R1','text':'Use Blender Cycles.'}]}
    denied = action.check_before_save(agent,run,selection_id,'Use another renderer.')
    assert denied.startswith('Политика:')
    assert len(transport.calls)==2
    check_receipt(agent,action,run,selection_id,'invariant_output_conflict',2)
    rows = agent._store.state()['requests']
    guard = next(r for r in rows if r['metadata'].get('kind')=='invariant_output')
    policy = next(r for r in rows if r['metadata'].get('kind')=='orchestration_policy')
    assert guard['usage_status']=='reported' and guard['cost_usd'] is not None
    assert policy['metadata']['invariant_request_id']==guard['id']
