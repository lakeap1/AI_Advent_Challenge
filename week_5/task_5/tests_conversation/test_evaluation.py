"""Day 25 scenario contract and evidence checks, independent of model responses."""
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.evaluate_conversation import _contains, assess_turn, load_scenarios, write_incremental


ROOT = Path(__file__).resolve().parents[1]


def test_frozen_scenarios_have_two_independent_twelve_turn_dialogues():
    scenarios = load_scenarios(ROOT / 'evaluation' / 'conversation_scenarios.json')
    assert len(scenarios) == 2
    assert [len(item['turns']) for item in scenarios] == [12, 12]
    assert scenarios[0]['id'] != scenarios[1]['id']
    for scenario in scenarios:
        assert scenario['dialogue_kind'] == 'ordinary'
        assert all(turn['prompt'].strip() and turn['supporting_facts'] for turn in scenario['turns'])
        assert scenario['turns'][-1]['expected_state']['goal_contains']
        assert scenario['turns'][-1]['expected_state']['constraints_present']
        assert scenario['turns'][-1]['expected_state']['terms_present']
    normal = scenarios[0]['turns'][-1]['expected_state']
    lights = scenarios[1]['turns'][-1]['expected_state']
    assert any('0.5' in value for value in normal['constraints_present'])
    assert any('три' in value.lower() for value in lights['constraints_present'])
    assert any('два' in value.lower() for value in lights['constraints_absent'])


def test_late_turn_assessment_reads_persisted_values_and_sources_not_summary_flag():
    turn = {'prompt': 'Вернись к цели', 'supporting_facts': ['UV coordinates match'],
            'expected_state': {'goal_contains': 'normal map',
                               'constraints_present': ['Strength 0.5'],
                               'constraints_absent': ['Strength 1'],
                               'terms_present': ['рельеф']}}
    state = {'goal': 'Разобрать normal map', 'constraints': [
        {'key': 'strength', 'value': 'Strength 1'}], 'terms': [], 'clarifications': []}
    source = {'source': 'official', 'section': ['UV'], 'chunk_id': 'a', 'text': 'UV coordinates match'}
    response = {'status': 'ok', 'text': 'Краткий ответ', 'state': {
        'conversation_state': state, 'requests': [{'id': 1, 'status': 'ok',
            'metadata': {'kind': 'answer', 'rag': {'used_sources': [source], 'sources': [source]}}}],
        'retrievals': [{'request_id': 1, 'provider': 'local_index', 'status': 'ok'}]}}
    findings = assess_turn(turn, response)
    assert findings['passed'] is False
    assert any('Strength 0.5' in problem for problem in findings['problems'])
    assert any('Strength 1' in problem for problem in findings['problems'])
    assert any('рельеф' in problem for problem in findings['problems'])
    state['constraints'][0]['value'] = 'Strength 0.5'
    state['terms'] = [{'key': 'relief', 'value': 'рельеф normal map'}]
    assert assess_turn(turn, response)['passed'] is True
    response['text'] = 'Не знаю. Уточните вопрос.'
    assert assess_turn(turn, response)['passed'] is False


def test_hypothetical_zero_does_not_revoke_half_strength_and_term_can_live_in_key():
    turn = {'prompt': 'А если Strength 0?', 'supporting_facts': ['Strength controls effect'],
            'expected_state': {'constraints_present': ['Strength 0.5'],
                               'constraints_absent': ['Strength 0'],
                               'terms_present': ['рельеф']}}
    source = {'source': 'https://docs.blender.org/manual/en/4.4/test.html',
              'section': ['Strength'], 'chunk_id': 'c', 'text': 'Strength controls effect'}
    response = {'status': 'ok', 'text': 'Strength меняет силу эффекта.', 'request_id': 7,
                'state': {'conversation_state': {'goal': '', 'clarifications': [],
                    'constraints': [{'key': 'strength', 'value': 'Strength 0.5'}],
                    'terms': [{'key': 'рельеф', 'value': 'эффект normal map на нормали'}]},
                    'requests': [{'id': 7, 'status': 'ok', 'metadata': {'kind': 'answer',
                        'rag': {'sources': [source], 'used_sources': [source]}}}],
                    'retrievals': [{'request_id': 7, 'provider': 'local_index', 'status': 'ok'}]}}
    assert assess_turn(turn, response)['passed'] is True
    response['state']['conversation_state']['constraints'][0]['value'] = 'Strength 0,5'
    assert assess_turn(turn, response)['passed'] is True
    response['state']['conversation_state']['constraints'].append(
        {'key': 'hypothetical', 'value': 'Strength 0'})
    assert assess_turn(turn, response)['passed'] is False
    response['state']['conversation_state']['constraints'][1]['value'] = 'Strength 0.0'
    assert assess_turn(turn, response)['passed'] is False
    response['state']['conversation_state']['constraints'][1]['value'] = 'Strength 0,00'
    assert assess_turn(turn, response)['passed'] is False
    response['state']['conversation_state']['constraints'].pop()
    assert assess_turn(turn, response)['passed'] is True


def test_stale_decimal_one_fails_correction_despite_valid_half_strength():
    turn = {'prompt': 'Исправь силу', 'supporting_facts': ['Strength controls effect'],
            'expected_state': {'constraints_present': ['Strength 0.5'],
                               'constraints_absent': ['Strength 1']}}
    source = {'source': 'official', 'section': ['Strength'], 'chunk_id': 'c',
              'text': 'Strength controls effect'}
    response = {'status': 'ok', 'text': 'Установлено 0.5.', 'request_id': 7,
                'state': {'conversation_state': {'constraints': [
                    {'key': 'current', 'value': 'Strength 0.5'},
                    {'key': 'obsolete', 'value': 'Strength 1.0'}]},
                    'requests': [{'id': 7, 'status': 'ok', 'metadata': {'kind': 'answer',
                        'rag': {'sources': [source], 'used_sources': [source]}}}],
                    'retrievals': [{'request_id': 7, 'provider': 'local_index', 'status': 'ok'}]}}
    assert assess_turn(turn, response)['passed'] is False
    response['state']['conversation_state']['constraints'][1]['value'] = 'Strength 1,00'
    assert assess_turn(turn, response)['passed'] is False
    response['state']['conversation_state']['constraints'].pop()
    assert assess_turn(turn, response)['passed'] is True


def test_numeric_equivalence_preserves_fraction_boundary():
    assert _contains(['Strength 1.0', 'Strength 1,00'], 'Strength 1')
    assert _contains(['Strength 0.0', 'Strength 0,00'], 'Strength 0')
    assert not _contains(['Strength 0.5'], 'Strength 0')
    assert not _contains(['Strength 10'], 'Strength 1')


def test_reopen_and_isolation_history_comparison_detects_retrieval_loss_or_mutation():
    from scripts.evaluate_conversation import _same_saved_state

    expected = {'conversation_state': {'goal': 'normal map'},
                'messages': [{'id': 1, 'text': 'answer'}],
                'requests': [{'id': 7, 'status': 'ok'}],
                'retrievals': [{'id': 3, 'request_id': 7, 'provider': 'local_index',
                                'status': 'ok', 'query': 'normal map'}]}
    assert _same_saved_state(expected, json.loads(json.dumps(expected))) is True
    without_retrievals = json.loads(json.dumps(expected))
    without_retrievals['retrievals'] = []
    assert _same_saved_state(expected, without_retrievals) is False
    mutated_retrievals = json.loads(json.dumps(expected))
    mutated_retrievals['retrievals'][0]['query'] = 'other dialogue'
    assert _same_saved_state(expected, mutated_retrievals) is False


def test_light_count_lexical_check_accepts_number_and_word_without_substring_leak():
    assert _contains(['Разрешены 3 источника Area'], 'три')
    assert _contains(['Разрешены три источника Area'], 'три')
    assert not _contains(['Разрешены 3 источника Area'], 'два')
    assert _contains(['Старое условие: 2 источника Area'], 'два')


def test_incremental_evidence_is_atomic_and_refuses_existing_output(tmp_path):
    output = tmp_path / 'result.json'
    write_incremental(output, {'scenarios': [{'turns': [{'prompt': 'first'}]}]}, create=True)
    assert json.loads(output.read_text(encoding='utf-8'))['scenarios'][0]['turns'][0]['prompt'] == 'first'
    assert sorted(path.name for path in tmp_path.iterdir()) == ['result.json']
    write_incremental(output, {'scenarios': [{'turns': [{'prompt': 'second'}]}]})
    assert json.loads(output.read_text(encoding='utf-8'))['scenarios'][0]['turns'][0]['prompt'] == 'second'
    with pytest.raises(FileExistsError):
        write_incremental(output, {}, create=True)


def test_controlled_evaluator_uses_actual_routes_and_saves_each_turn(tmp_path, monkeypatch):
    from scripts import evaluate_conversation as evaluator
    from rag import chat
    from tests_rag.grounded_fake import GroundedTransport

    source_data = tmp_path / 'index'
    source_data.mkdir()
    with sqlite3.connect(source_data / 'indexing.sqlite3') as database:
        database.execute('CREATE TABLE fixture (id INTEGER)')
    chunk = {'chunk_id': 'fixture-1', 'file': 'fixture.md', 'source': 'fixture.md',
             'title': 'Fixture', 'section': ['Evidence'], 'line_start': 1,
             'line_end': 1, 'start': 0, 'end': 50, 'document_hash': 'fixture',
             'text': 'Controlled fragment about normal maps and lights.',
             'embedding': [1.0] + [0.0] * 1535}
    monkeypatch.setattr(evaluator, 'read_index', lambda *_: [chunk])
    monkeypatch.setattr(chat, 'read_index', lambda *_: [chunk])

    class StaticEmbedder:
        def __init__(self, config):
            self.config = config

        def embed(self, texts):
            return {'model': self.config.model, 'vectors': [[1.0] + [0.0] * 1535],
                    'usage': {'prompt_tokens': 5, 'total_tokens': 5}}

    monkeypatch.setattr(evaluator, 'OpenAIEmbedder', StaticEmbedder)
    monkeypatch.setattr(evaluator, 'ResponsesTransport', lambda _key: GroundedTransport())
    monkeypatch.setenv('OPENAI_API_KEY', 'controlled-test-key')
    original_write = evaluator.write_incremental
    snapshots = []

    def observed_write(path, value, *, create=False):
        original_write(path, value, create=create)
        snapshots.append(json.loads(path.read_text(encoding='utf-8')))

    monkeypatch.setattr(evaluator, 'write_incremental', observed_write)
    output = tmp_path / 'controlled.json'
    result = evaluator.run(source_data, output, ROOT / 'evaluation' / 'conversation_scenarios.json')
    assert [len(scenario['turns']) for scenario in result['scenarios']] == [12, 12]
    assert len(snapshots) >= 27  # initial, dialogue records, each turn, reopen, isolation
    assert result['status'] == 'failed_structural'  # fake intentionally stores no user conditions
    assert all(turn['semantic_assessment'] == 'pending_independent_review'
               for scenario in result['scenarios'] for turn in scenario['turns'])
    assert all(len(turn['provider_payloads']) >= 4 and turn['requests'] and turn['retrievals']
               for scenario in result['scenarios'] for turn in scenario['turns'])
    assert all(scenario['reopen']['passed'] for scenario in result['scenarios'])
    assert result['isolation']['passed'] is True
    assert result['totals']['api_requests'] > 24
    assert json.loads(output.read_text(encoding='utf-8')) == result
    with pytest.raises(FileExistsError):
        evaluator.run(source_data, output, ROOT / 'evaluation' / 'conversation_scenarios.json')
