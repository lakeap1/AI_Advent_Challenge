"""Fake-provider checks for the isolated ten-question export; no API call."""

import json
from pathlib import Path

import pytest

from agent.core import AgentResult
from scripts.evaluate_grounding import PayloadCapture, evaluate_questions, load_questions


SOURCE = {'label': 'S1', 'source': 'orchestration/service.py', 'section': 'save_report',
          'chunk_id': 'chunk-1', 'text': "target.open('xb')"}
UNUSED = {'label': 'S2', 'source': 'orchestration/other.py', 'section': 'other',
          'chunk_id': 'chunk-2', 'text': 'Selected but never cited'}


class FakeMemory:
    def __init__(self):
        self.active = {'id': 1, 'task_id': 1}
        self.created = []

    def workspace(self):
        return {'active_dialogue': self.active}

    def create_dialogue(self, name, task_id, mode):
        self.active = {'id': len(self.created) + 2, 'task_id': task_id}
        self.created.append((name, task_id, mode))


class FakeCapture:
    def __init__(self):
        self.payloads = []


class FakeAgent:
    def __init__(self, memory, capture, *, fail_on=None):
        self.memory = memory
        self.capture = capture
        self.fail_on = fail_on
        self.calls = []
        self.question = ''

    def run(self, question, **options):
        self.question = question
        self.calls.append(options)
        self.capture.payloads.append({'input': question, 'text': {'format': 'strict-json'}})
        if len(self.calls) == self.fail_on:
            raise RuntimeError('fake provider interrupted after payload capture')
        return AgentResult('ok', 'Отчёт сохраняется как .txt. [S1]',
                           request_id=len(self.calls))

    def state(self):
        rid = len(self.calls)
        return {'messages': [{'role': 'user', 'content': self.question}],
                'requests': [{'id': rid, 'status': 'ok',
                              'usage_status': 'reported',
                              'usage': {'input_tokens': 1, 'output_tokens': 2, 'total_tokens': 3},
                              'cost_usd': '0.000001',
                              'metadata': {'rag': {'grounding': {
                                  'status': 'answered', 'claims': [{
                                      'text': 'Отчёт сохраняется как .txt.',
                                      'source_labels': ['S1']}], 'clarification': ''},
                                  'used_sources': [SOURCE], 'sources': [SOURCE, UNUSED]}}},
                             {'id': rid + 100, 'status': 'ok', 'usage_status': 'reported',
                              'usage': {'input_tokens': 4, 'output_tokens': 1, 'total_tokens': 5},
                              'cost_usd': '0.000002',
                              'metadata': {'kind': 'relevance_filter',
                                           'parent_request_id': rid}}],
                'retrievals': [], 'summary': {'known_cost_usd': '0.000001',
                                               'cost_complete': True},
                'token_accounting': {'known_total_tokens': 3}}


class FakeWorkspace:
    def __init__(self, capture, *, fail_on=None):
        self.memory = FakeMemory()
        self.worker = FakeAgent(self.memory, capture, fail_on=fail_on)

    def agent(self):
        return self.worker


def questions():
    return [{'id': f'q{n:02}', 'question': f'Вопрос {n}',
             'expected_facts': ['Факт'], 'expected_sources': [{'file': 'source'}],
             'unanswerable': False} for n in range(1, 11)]


def test_ten_isolated_main_agent_calls_export_payloads_and_verified_fragments(tmp_path):
    capture = FakeCapture()
    workspace = FakeWorkspace(capture)
    output = tmp_path / 'evidence.json'
    report = evaluate_questions(workspace, questions(), capture,
                                {'status': 'running', 'questions': []}, output)

    saved = json.loads(output.read_text(encoding='utf-8'))
    assert report['status'] == saved['status'] == 'complete_structural'
    assert len(saved['questions']) == 10
    assert len({row['dialogue_id'] for row in saved['questions']}) == 10
    assert all(call == {'rag_mode': 'filter', 'use_working': False, 'use_long_term': False}
               for call in workspace.worker.calls)
    assert all(row['structural']['structural_pass'] for row in saved['questions'])
    assert all(row['payloads'] == [{'input': row['question'],
                                   'text': {'format': 'strict-json'}}]
               for row in saved['questions'])
    assert all(row['requests'][0]['metadata']['rag']['used_sources'] == [SOURCE]
               for row in saved['questions'])
    assert all(row['assessment'] == 'pending_independent_review'
               for row in saved['questions'])
    assert saved['totals'] == {'api_requests': 20, 'known_cost_usd': '0.000030',
                               'cost_complete': True, 'unknown_cost_requests': 0,
                               'known_total_tokens': 80}


def test_failed_attempt_keeps_its_payload_in_partial_export(tmp_path):
    capture = FakeCapture()
    workspace = FakeWorkspace(capture, fail_on=2)
    output = tmp_path / 'partial.json'
    with pytest.raises(RuntimeError):
        evaluate_questions(workspace, questions(), capture,
                           {'status': 'running', 'questions': []}, output)
    saved = json.loads(output.read_text(encoding='utf-8'))
    assert saved['status'] == 'interrupted'
    assert len(saved['questions']) == 2
    assert saved['questions'][1]['payloads'] == [
        {'input': 'Вопрос 2', 'text': {'format': 'strict-json'}}]


def test_shipped_gold_has_ten_answerable_questions():
    loaded = load_questions(Path(__file__).resolve().parents[1] /
                            'evaluation' / 'questions.json')
    assert len(loaded) == 10
    assert loaded[-1]['id'] == 'q10'
    assert 'кодировке' in loaded[-1]['question']


def test_real_main_agent_exports_ten_grounded_answers_with_fake_provider(tmp_path, monkeypatch):
    from rag.chat import RagProfileWorkspace
    from tests_rag.grounded_fake import GroundedTransport, install_local_index

    class FakeCounter:
        description = {'method': 'synthetic', 'encoding': 'none', 'version': 'test'}

        def count(self, text):
            return len(text) // 4

        def history(self, messages):
            return sum(self.count(message['content']) for message in messages)

        def measure(self, prompt, messages, instructions):
            return {'new_message_tokens_estimate': self.count(prompt),
                    'history_tokens_estimate': self.history(messages),
                    'instructions_tokens_estimate': self.count(instructions),
                    'input_text_tokens_estimate': self.count(prompt) +
                    self.history(messages) + self.count(instructions)}

    monkeypatch.setattr('agent.core.TokenCounter', FakeCounter)
    install_local_index(monkeypatch)
    provider = GroundedTransport(answer='Факт из проверенного фрагмента.')
    capture = PayloadCapture(provider)
    workspace = RagProfileWorkspace(tmp_path / 'isolated', capture)
    try:
        report = evaluate_questions(workspace, questions(), capture,
                                    {'status': 'running', 'questions': []},
                                    tmp_path / 'evidence.json')
    finally:
        workspace.close()
    assert report['status'] == 'complete_structural'
    assert len(report['questions']) == 10
    assert all(row['structural']['structural_pass'] for row in report['questions'])
    assert all(next(request for request in row['requests']
                    if request['metadata']['kind'] == 'answer')['metadata']['rag']['used_sources'][0]['text']
               == 'A local fragment about the assistant and its features.'
               for row in report['questions'])
    assert all(any(payload.get('text', {}).get('format', {}).get('name')
                   in ('grounded_answer', 'task_response')
                   for payload in row['payloads']) for row in report['questions'])
    assert report['totals']['api_requests'] >= 20
