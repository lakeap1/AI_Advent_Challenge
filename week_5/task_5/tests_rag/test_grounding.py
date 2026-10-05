"""Grounding is a host checked boundary, rather than a citation suggestion."""

import json
import re

import pytest

from rag.grounding import GroundingError, NO_CONTEXT_TEXT, parse_grounding


SOURCES = [
    {'label': 'S1', 'source': 'alpha.md', 'section': ['Overview'],
     'chunk_id': 'chunk-1', 'text': 'Alpha evidence.'},
    {'label': 'S2', 'source': 'beta.md', 'section': ['Details'],
     'chunk_id': 'chunk-2', 'text': 'Beta evidence.'},
]


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
