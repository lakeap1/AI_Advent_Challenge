"""Polling remains readable during a long ask; downloads validate the file."""
from concurrent.futures import ThreadPoolExecutor
import hashlib

from app import create_app


def test_progress_without_workspace_lock_and_restore(tmp_path):
    app = create_app(data_dir=tmp_path)
    client = app.test_client()
    state = client.get('/api/state').json
    identity = (state['personalization']['selected_id'],state['workspace']['active_dialogue']['id'],state['active_branch'])
    store = app.extensions['orchestration_store']
    run = store.create(*identity,'Normal mapping',42)
    params = dict(zip(('profile_id','dialogue_id','branch_id'),identity))
    with app.extensions['workspace'].lock:
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(lambda: app.test_client().get('/api/orchestration/progress',query_string=params)).result(timeout=3)
    assert response.status_code == 200 and response.json['run']['id'] == run['id']
    assert client.get('/api/orchestration/progress',query_string={**params,'branch_id':identity[2]+1}).status_code == 404
    assert client.get('/api/orchestration/'+run['id']).status_code == 200
    app.extensions['workspace'].close()
    restarted = create_app(data_dir=tmp_path)
    assert restarted.test_client().get('/api/state').json['orchestration_runs'][0]['status'] == 'interrupted'
    restarted.extensions['workspace'].close()


def test_download_rechecks_actual_bytes(tmp_path):
    app = create_app(data_dir=tmp_path)
    client = app.test_client()
    state = client.get('/api/state').json
    identity = (state['personalization']['selected_id'],state['workspace']['active_dialogue']['id'],state['active_branch'])
    store = app.extensions['orchestration_store']
    run = store.create(*identity,'Question',42)
    content = 'Normal mapping report.\n'
    filename = 'fixture.txt'
    path = store.results / filename
    path.write_bytes(content.encode())
    saved = dict(report_id='fixture',filename=filename,content=content,bytes_written=len(content.encode()),
                 sha256=hashlib.sha256(content.encode()).hexdigest())
    store.update(run['id'],status='success',saved=saved,verified=True)
    assert client.get('/api/orchestration/'+run['id']+'/file').data == content.encode()
    path.write_bytes(b'Changed file')
    assert client.get('/api/orchestration/'+run['id']+'/file').status_code == 409
    app.extensions['workspace'].close()
