"""HTTP view of the local document index; chat state and lock stay independent."""

from pathlib import Path

from flask import jsonify, render_template, request


def register_indexing(app, data_dir, workspace=None, service=None):
    if service is None:
        from indexing.client import OpenAIEmbedder
        from indexing.config import load_config
        from indexing.service import IndexingService
        from indexing.store import IndexStore

        task_root = Path(__file__).resolve().parent
        db_path = ':memory:' if str(data_dir) == ':memory:' else Path(data_dir) / 'indexing.sqlite3'
        store = IndexStore(db_path)
        config = load_config()
        service = IndexingService(task_root, store, OpenAIEmbedder(config), config)

    app.extensions['indexing_service'] = service

    def rejection(message):
        return jsonify(status='rejected', error=message), 400

    def internal_error():
        app.logger.exception('Indexing request failed')
        return jsonify(status='failed', error='Индексация не завершена. Проверьте серверный журнал.'), 503

    @app.get('/indexing')
    def indexing_page():
        return render_template('indexing.html')

    @app.get('/api/indexing/status')
    def indexing_status():
        try:
            return jsonify(service.summary())
        except Exception:
            return internal_error()

    @app.get('/api/indexing/inspect')
    def indexing_inspect():
        try:
            return jsonify(service.inspect())
        except ValueError:
            return rejection('Корпус недоступен или содержит небезопасный путь.')
        except Exception:
            return internal_error()

    @app.post('/api/indexing/build')
    def indexing_build():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict) or set(payload) != {'force'} or type(payload['force']) is not bool:
            return rejection('Передайте JSON-объект с логическим полем force.')
        try:
            result = service.build(force=payload['force'])
            return jsonify(result), 200 if result.get('status') not in ('failed', 'rejected') else 400
        except ValueError as error:
            code = getattr(error, 'metadata', {}).get('policy_code') if isinstance(getattr(error, 'metadata', None), dict) else None
            if code == 'unsafe_path':
                return rejection('Корпус содержит небезопасный путь; индекс не изменён.')
            return rejection(str(error))
        except Exception:
            return internal_error()

    @app.get('/api/indexing/chunks')
    def indexing_chunks():
        strategy = request.args.get('strategy', 'fixed')
        if strategy not in ('fixed', 'structural'):
            return rejection('Неизвестная стратегия разбиения.')
        try:
            offset = int(request.args.get('offset', '0'))
            limit = int(request.args.get('limit', '20'))
        except ValueError:
            return rejection('offset и limit должны быть целыми числами.')
        if offset < 0 or not 1 <= limit <= 100:
            return rejection('offset должен быть неотрицательным, limit — от 1 до 100.')
        try:
            return jsonify(service.chunks(strategy, offset=offset, limit=limit))
        except Exception:
            return internal_error()

    @app.get('/api/indexing/comparison')
    def indexing_comparison():
        try:
            return jsonify(service.summary().get('comparison', {}))
        except Exception:
            return internal_error()

    @app.get('/api/indexing/export')
    def indexing_export():
        try:
            return jsonify(service.export())
        except Exception:
            return internal_error()
