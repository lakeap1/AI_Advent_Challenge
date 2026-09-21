from app import create_app
from test_strategies import Fake, response


def test_http_dialogue_task_recovery_and_memory(tmp_path):
    fake = Fake([response('Ответ'), response('{"operations": []}')])
    app = create_app(data_dir=tmp_path, transport=fake)
    client = app.test_client()
    state = client.get('/api/state').json
    first = state['workspace']['active_dialogue']
    assert client.post('/api/memory', json=dict(layer='working', category='context', scope='task', key='Свет', value='Мягкий', reason='Задача')).status_code == 200
    assert client.post('/api/ask', json={'prompt': 'Почему тень мягкая?'}).json['status'] == 'ok'
    new = client.post('/api/dialogue', json=dict(name='Продолжение', task_id=first['task_id'], mode='sliding')).json['state']
    assert not new['messages'] and new['workspace']['layers']['working'][0]['value'] == 'Мягкий'
    other = client.post('/api/task', json=dict(name='UV', project='Иной', mode='facts')).json['state']
    assert not other['workspace']['layers']['working']
    restored = client.post('/api/open', json={'dialogue_id': first['id']}).json['state']
    assert len(restored['messages']) == 2
    app.extensions['workspace'].close()
    again = create_app(data_dir=tmp_path, transport=fake).test_client().get('/api/state').json
    assert again['workspace']['active_dialogue']['id'] == first['id']
    assert len(again['messages']) == 2


def test_http_bad_types_do_not_call_model(tmp_path):
    fake = Fake()
    client = create_app(data_dir=tmp_path, transport=fake).test_client()
    for body in [[], None, {'prompt':'Вопрос','use_working':'false'}]:
        assert client.post('/api/ask', json=body).status_code == 400
    assert client.post('/api/open', json={'dialogue_id': True}).status_code == 400
    assert not fake.calls
