"""Owner-checked main-chat comparison routes."""

from flask import abort, jsonify, request

from .chat_evaluation import ChatEvaluationService


def register_chat_evaluation(app, data_dir, workspace, serialized, snapshot):
    service = ChatEvaluationService(data_dir, workspace)
    app.extensions['chat_evaluation_service'] = service
    app.extensions['augment_chat_evaluation'] = service.augment

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise ValueError('Отправьте JSON-объект.')
        return value

    @app.post('/api/rag/chat/start')
    @serialized
    def start():
        if workspace is None:
            raise ValueError('Контрольный прогон требует рабочее пространство.')
        run = service.start(body())
        return jsonify(status='ok', run=run, state=snapshot())

    @app.post('/api/rag/chat/question')
    @serialized
    def question():
        if workspace is None:
            raise ValueError('Контрольный прогон требует рабочее пространство.')
        run = service.question(body())
        return jsonify(status='ok', run=run, state=snapshot())

    @app.get('/api/rag/chat/trace/<path:receipt_id>')
    @serialized
    def chat_evaluation_trace(receipt_id):
        if workspace is None:
            abort(404)
        current = service._identity({
            'profile_id': workspace.state()['personalization']['selected_id'],
            'task_id': workspace.memory.workspace()['active_dialogue']['task_id'],
            'dialogue_id': workspace.memory.workspace()['active_dialogue']['id'],
            'branch_id': workspace.state()['active_branch']})
        try:
            return jsonify(service.trace(receipt_id,current))
        except ValueError:
            abort(404)
