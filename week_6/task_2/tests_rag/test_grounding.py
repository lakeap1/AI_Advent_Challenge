"""Grounding is a host checked boundary, rather than a citation suggestion."""

import json
import re

import pytest
from jsonschema import ValidationError, validate

from rag.grounding import (GroundingError, NO_CONTEXT_TEXT, ordinary_parts_format,
                           parse_grounding, parse_part_grounding, response_format)


SOURCES = [
    {'label': 'S1', 'source': 'alpha.md', 'section': ['Overview'],
     'chunk_id': 'chunk-1', 'text': 'Alpha evidence.'},
    {'label': 'S2', 'source': 'beta.md', 'section': ['Details'],
     'chunk_id': 'chunk-2', 'text': 'Beta evidence.'},
]

PARTS = [{'id': 'q1', 'question': 'First question.'},
         {'id': 'q2', 'question': 'Second question.'}]
ANSWERED = {'status': 'answered', 'claims': [
    {'text': 'A supported answer.', 'source_labels': ['S1']}], 'clarification': ''}
UNKNOWN = {'status': 'unknown', 'claims': [], 'clarification': 'Please clarify.'}


def test_multipart_schema_rejects_actual_answered_with_clarification_draft():
    # Embedded rejected provider response used to check incompatible answer fields.
    draft = {'parts': {
        'q1': {'status': 'answered', 'claims': [{
            'text': 'В поле UV Map узла Normal Map укажите UVMap — имя UV-развёртки, из которой узел должен получать касательные для tangent-space карты.',
            'source_labels': ['S1']}], 'clarification': ''},
        'q2': {'status': 'answered', 'claims': [{
            'text': 'Для Image Texture используйте координаты той же UV-карты UVMap, чтобы они совпадали с развёрткой, используемой узлом Normal Map.',
            'source_labels': ['S1']}],
            'clarification': 'Фрагмент подтверждает необходимость использовать UVMap, но не уточняет конкретное подключение или узел, выход которого следует подать на вход Vector.'}}}
    with pytest.raises(GroundingError, match='invalid_grounding'):
        parse_part_grounding(json.dumps(draft), PARTS, SOURCES,
                             {'q1': ['S1'], 'q2': ['S1']})
    with pytest.raises(ValidationError):
        validate(draft, ordinary_parts_format(PARTS)['schema'])


@pytest.mark.parametrize(('first', 'second'), [
    (ANSWERED, ANSWERED), (UNKNOWN, UNKNOWN), (ANSWERED, UNKNOWN)])
def test_multipart_schema_accepts_exclusive_answer_and_unknown_states(first, second):
    draft = {'parts': {'q1': first, 'q2': second}}
    validate(draft, ordinary_parts_format(PARTS)['schema'])
    _, _, _, coverage = parse_part_grounding(json.dumps(draft), PARTS, SOURCES,
                                             {'q1': ['S1'], 'q2': ['S1']})
    assert [coverage[pid]['status'] for pid in ('q1', 'q2')] == [first['status'], second['status']]


@pytest.mark.parametrize('invalid', [
    {**ANSWERED, 'status': 'partial'},
    {**ANSWERED, 'claims': []},
    {**ANSWERED, 'clarification': 'A gap.'},
    {**ANSWERED, 'clarification': ' '},
    {**UNKNOWN, 'claims': ANSWERED['claims']},
    {**UNKNOWN, 'clarification': ''},
    {**UNKNOWN, 'clarification': ' \t\n\u00a0'},
    {key: value for key, value in ANSWERED.items() if key != 'clarification'},
    {**UNKNOWN, 'extra': 'unaccepted'},
])
def test_multipart_schema_rejects_invalid_status_claim_clarification_combinations(invalid):
    draft = {'parts': {'q1': ANSWERED, 'q2': invalid}}
    with pytest.raises(ValidationError):
        validate(draft, ordinary_parts_format(PARTS)['schema'])
    with pytest.raises(GroundingError, match='invalid_grounding'):
        parse_part_grounding(json.dumps(draft), PARTS, SOURCES,
                             {'q1': ['S1'], 'q2': ['S1']})


def test_provider_schema_requires_a_source_for_each_answer_claim():
    schema = response_format()['schema']
    cited = {'status': 'answered', 'claims': [
        {'text': 'A supported technical answer.', 'source_labels': ['S1']}],
        'clarification': ''}
    validate(cited, schema)
    with pytest.raises(ValidationError):
        validate({'status': 'answered', 'claims': [
            cited['claims'][0],
            {'text': 'A separate uncited reminder of dialogue state.', 'source_labels': []}],
            'clarification': ''}, schema)
    validate({'status': 'unknown', 'claims': [], 'clarification': 'Уточните вопрос.'}, schema)


def fixture_grounding(text):
    """Adapt legacy fake provider prose to the new provider JSON wire contract."""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        parsed = None
    if isinstance(parsed, dict) and parsed.get('status') in ('answered', 'unknown'):
        return parsed
    labels = list(dict.fromkeys(re.findall(r'\[(S\d+)\]', text)))
    claim = re.sub(r'\s*\[S\d+\]', '', text).strip()
    return {'status': 'answered', 'claims': [
        {'text': claim, 'source_labels': labels}], 'clarification': ''}


def test_paraphrase_renders_only_verified_claims_and_original_fragments():
    raw = json.dumps({'status': 'answered', 'claims': [
        {'text': 'Alpha is covered here.', 'source_labels': ['S1']},
        {'text': 'The second detail is covered too.', 'source_labels': ['S2', 'S1']}],
        'clarification': ''})
    rendered, grounding, used = parse_grounding(raw, SOURCES)
    assert rendered == 'Alpha is covered here. [S1]\n\nThe second detail is covered too. [S2] [S1]'
    assert grounding['status'] == 'answered'
    assert grounding['claims'][1]['source_labels'] == ['S2', 'S1']
    assert [item['label'] for item in used] == ['S1', 'S2']
    assert used[0]['text'] == 'Alpha evidence.'


def test_redundant_known_inline_labels_are_rendered_once_from_source_labels():
    raw = json.dumps({'status': 'answered', 'claims': [
        {'text': 'Alpha and beta are covered. [S1] [S2]',
         'source_labels': ['S1', 'S2']}], 'clarification': ''})
    rendered, grounding, used = parse_grounding(raw, SOURCES)
    assert rendered == 'Alpha and beta are covered. [S1] [S2]'
    assert grounding['claims'] == [
        {'text': 'Alpha and beta are covered.', 'source_labels': ['S1', 'S2']}]
    assert [source['label'] for source in used] == ['S1', 'S2']


@pytest.mark.parametrize(('raw', 'code'), [
    ('{"status":"answered","claims":[{"text":"Uncited","source_labels":[]}],"clarification":""}', 'missing_source'),
    ('{"status":"answered","claims":[{"text":"Invented","source_labels":["S9"]}],"clarification":""}', 'unknown_source'),
    ('{"status":"answered","claims":[],"clarification":""}', 'invalid_grounding'),
    ('{"status":"answered","claims":[{"text":"A","source_labels":["S1"]}],"clarification":"","status":"unknown"}', 'duplicate_json_key'),
    ('{"status":"unknown","claims":[{"text":"A","source_labels":["S1"]}],"clarification":"Why?"}', 'invalid_grounding'),
    ('{"status":"unknown","claims":[],"clarification":""}', 'invalid_grounding'),
    ('{"status":"answered","claims":[{"text":"Fake [S9]","source_labels":["S1"]}],"clarification":""}', 'invalid_grounding'),
    ('{"status":"answered","claims":[{"text":"Mismatched [S2]","source_labels":["S1"]}],"clarification":""}', 'invalid_grounding'),
    ('{"status":"answered","claims":[{"text":"Unknown [S1] [S99]","source_labels":["S1"]}],"clarification":""}', 'invalid_grounding'),
    ('{"status":"answered","claims":[{"text":"Malformed [S1","source_labels":["S1"]}],"clarification":""}', 'invalid_grounding'),
])
def test_invalid_contract_rejected(raw, code):
    with pytest.raises(GroundingError) as error:
        parse_grounding(raw, SOURCES)
    assert error.value.code == code


def test_unknown_uses_no_sources_and_asks_for_clarification():
    raw = '{"status":"unknown","claims":[],"clarification":"Уточните, о каком модуле речь."}'
    rendered, grounding, used = parse_grounding(raw, SOURCES)
    assert rendered == NO_CONTEXT_TEXT
    assert grounding == {'status': 'unknown', 'claims': [],
                         'clarification': 'Уточните вопрос о материалах локальной базы.'}
    assert used == []


def test_unknown_does_not_publish_factual_model_clarification():
    raw = json.dumps({'status': 'unknown', 'claims': [],
                      'clarification': 'Модуль удалён в версии 12. Уточните ветку.'})
    rendered, grounding, used = parse_grounding(raw, SOURCES)
    assert rendered == NO_CONTEXT_TEXT
    assert 'версии 12' not in rendered
    assert 'версии 12' not in json.dumps(grounding, ensure_ascii=False)
    assert used == []


def test_each_paragraph_becomes_its_own_cited_canonical_claim():
    raw = json.dumps({'status': 'answered', 'claims': [
        {'text': 'Первый факт.\n\nФормула: f(x) = a + b.\nЕё продолжение.',
         'source_labels': ['S1']}], 'clarification': ''})
    rendered, grounding, used = parse_grounding(raw, SOURCES)
    assert rendered == ('Первый факт. [S1]\n\n'
                        'Формула: f(x) = a + b.\nЕё продолжение. [S1]')
    assert grounding['claims'] == [
        {'text': 'Первый факт.', 'source_labels': ['S1']},
        {'text': 'Формула: f(x) = a + b.\nЕё продолжение.', 'source_labels': ['S1']}]
    assert [source['label'] for source in used] == ['S1']
