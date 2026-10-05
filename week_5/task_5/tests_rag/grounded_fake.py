"""Deterministic main-chat retrieval and provider wire responses for HTTP tests."""

import json


class Embedder:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.append(texts)
        return {'model': 'text-embedding-3-small',
                'vectors': [[1.0] + [0.0] * 1535],
                'usage': {'prompt_tokens': 5, 'total_tokens': 5}}


def install_local_index(monkeypatch):
    from rag import chat

    chunk = {'chunk_id': 'fixture-1', 'file': 'fixture.md', 'source': 'local',
             'title': 'Fixture', 'section': ['Evidence'], 'line_start': 1,
             'line_end': 1, 'start': 0, 'end': 50, 'document_hash': 'fixture',
             'text': 'A local fragment about the assistant and its features.',
             'embedding': [1.0] + [0.0] * 1535}
    monkeypatch.setattr(chat, 'read_index', lambda *_: [chunk])
    embedder = Embedder()
    monkeypatch.setattr(chat, 'OpenAIEmbedder', lambda _config: embedder)
    return embedder


def _response(text, usage):
    return {'status': 'completed', 'model': 'gpt-6-luna', 'service_tier': 'default',
            'output': [{'type': 'message', 'role': 'assistant', 'status': 'completed',
                        'content': [{'type': 'output_text', 'text': text}]}],
            'usage': usage}


class GroundedTransport:
    def __init__(self, answer='Ответ', answer_usage=None):
        self.answer = answer
        self.answer_usage = answer_usage or {
            'input_tokens': 100, 'output_tokens': 10, 'total_tokens': 110,
            'input_tokens_details': {'cached_tokens': 0, 'cache_write_tokens': 0},
            'output_tokens_details': {'reasoning_tokens': 0}}
        self.calls = []

    def create(self, payload, timeout):
        self.calls.append(payload)
        kind = payload.get('text', {}).get('format', {}).get('name')
        if kind == 'conversation_preparation':
            data = json.loads(payload['input'][0]['content'])
            return _response(json.dumps({'revision': data['state']['revision'],
                'search_query': data['current_message'], 'operations': []}, ensure_ascii=False),
                self.answer_usage)
        if kind == 'filter':
            candidates = json.loads(payload['input'][0]['content'])['candidates']
            scores = {item['chunk_id']: {'score': 3, 'reason': 'Fixture evidence'}
                      for item in candidates}
            return _response(json.dumps({'scores': scores}), self.answer_usage)
        if kind == 'task_response':
            state_text = payload['instructions'].split('TASK_STATE —', 1)[1].split('TASK_RESPONSE_JSON', 1)[0]
            state = json.loads(state_text[state_text.index('{'):].strip())
            grounded = {'status': 'answered', 'claims': [
                {'text': self.answer, 'source_labels': ['S1']}], 'clarification': ''}
            envelope = {'answer': grounded, 'event': 'stay', 'evidence': '',
                        **{key: state[key] for key in
                           ('goal', 'current_step', 'expected_action', 'notes', 'plan')}}
            return _response(json.dumps(envelope, ensure_ascii=False), self.answer_usage)
        if kind == 'grounded_answer':
            body = {'status': 'answered', 'claims': [
                {'text': self.answer, 'source_labels': ['S1']}], 'clarification': ''}
            return _response(json.dumps(body, ensure_ascii=False), self.answer_usage)
        return _response('{"operations": []}', self.answer_usage)
