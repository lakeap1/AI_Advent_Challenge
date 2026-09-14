from app import create_app
from test_strategies import make


def test_http_modes_and_branch_validation():
    workers = {mode:make(mode)[0] for mode in ('sliding','facts','branching')}
    client = create_app(workers).test_client()
    assert client.get('/api/state?mode=other').status_code == 400
    assert client.post('/api/ask',json=[]).status_code == 400
    assert client.post('/api/ask',json={'mode':'sliding','prompt':'UV'}).json['status']=='ok'
    assert client.get('/api/state?mode=facts').json['messages']==[]
    assert client.post('/api/checkpoint',json={'mode':'sliding','name':'x'}).status_code==400
    cp=client.post('/api/checkpoint',json={'mode':'branching','name':'Старт'}).json['item']['id']
    a=client.post('/api/branch',json={'mode':'branching','checkpoint_id':cp,'name':'A'}).json['item']['id']
    assert client.post('/api/switch',json={'mode':'branching','branch_id':a}).status_code==200
    assert client.post('/api/switch',json={'mode':'branching','branch_id':999}).status_code==400
    assert client.get('/api/state?mode=branching').headers['Cache-Control']=='no-store'


def test_memory_oracle_rejects_wrong_values():
    import json
    from comparison import check_memory, EXPECTED
    assert check_memory(json.dumps(EXPECTED[6]), EXPECTED[6])['passed']
    assert not check_memory(json.dumps(EXPECTED[6]), EXPECTED[12])['passed']
    assert not check_memory('Красивый ответ без фактов', EXPECTED[6])['passed']

def test_numeric_representation_is_not_memory_loss():
    from comparison import check_memory, EXPECTED
    import json
    value={**EXPECTED[6],'texture':'512'}
    assert check_memory(json.dumps(value),EXPECTED[6])['passed']
    value['texture']='512.9'
    assert not check_memory(json.dumps(value),EXPECTED[6])['passed']


def test_escaped_surrogate_in_facts_is_rejected_before_commit():
    from test_strategies import make, response
    agent,fake=make('facts',[response('{"x":"\\ud800"}')])
    result=agent.run('test')
    assert result.code=='facts_failed'
    assert agent.state()['memory']['facts']=={}
    assert agent.state()['summary']['api_requests']==1
