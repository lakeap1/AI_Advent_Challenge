"""Strict, shared contract for answers grounded in selected local fragments."""

import json
import re


UNKNOWN_PREFIX = 'Не знаю.'
CLARIFICATION_TEXT = 'Уточните вопрос о материалах локальной базы.'
NO_CONTEXT_TEXT = f'{UNKNOWN_PREFIX} {CLARIFICATION_TEXT}'
POLICY_NAME = 'grounded_claims_selected_sources_v1'
_LABEL_IN_TEXT = re.compile(r'\[S[^\]]*\]')


class GroundingError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GroundingError('duplicate_json_key')
        result[key] = value
    return result


def strict_json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_unique_pairs,
                          parse_constant=lambda _value: (_ for _ in ()).throw(GroundingError('invalid_grounding')))
    except GroundingError:
        raise
    except (TypeError, ValueError, RecursionError) as error:
        raise GroundingError('invalid_grounding') from error


def grounding_schema():
    claim = {'type': 'object', 'properties': {
        'text': {'type': 'string'},
        'source_labels': {'type': 'array', 'items': {'type': 'string'}}},
        'required': ['text', 'source_labels'], 'additionalProperties': False}
    return {'type': 'object', 'properties': {
        'status': {'type': 'string', 'enum': ['answered', 'unknown']},
        'claims': {'type': 'array', 'items': claim},
        'clarification': {'type': 'string'}},
        'required': ['status', 'claims', 'clarification'], 'additionalProperties': False}


def response_format():
    return {'type': 'json_schema', 'name': 'grounded_answer', 'strict': True,
            'schema': grounding_schema()}


def parse_grounding_data(data, sources):
    if type(data) is not dict or set(data) != {'status', 'claims', 'clarification'}:
        raise GroundingError('invalid_grounding')
    if type(data['claims']) is not list or type(data['clarification']) is not str:
        raise GroundingError('invalid_grounding')
    labels = {}
    for source in sources:
        if type(source) is not dict or any(key not in source for key in ('label', 'source', 'section', 'chunk_id', 'text')):
            raise GroundingError('invalid_grounding')
        label = source['label']
        if (type(label) is not str or label in labels or not label
                or type(source['text']) is not str or not source['text'].strip()):
            raise GroundingError('invalid_grounding')
        labels[label] = source
    if data['status'] == 'unknown':
        clarification = data['clarification'].strip()
        if data['claims'] or not clarification or _LABEL_IN_TEXT.search(clarification):
            raise GroundingError('invalid_grounding')
        return NO_CONTEXT_TEXT, {
            'status': 'unknown', 'claims': [], 'clarification': CLARIFICATION_TEXT}, []
    if data['status'] != 'answered' or not data['claims'] or data['clarification']:
        raise GroundingError('invalid_grounding')
    claims = []
    rendered = []
    used_labels = set()
    for claim in data['claims']:
        if type(claim) is not dict or set(claim) != {'text', 'source_labels'}:
            raise GroundingError('invalid_grounding')
        content, cited = claim['text'], claim['source_labels']
        if type(content) is not str or not content.strip() or _LABEL_IN_TEXT.search(content):
            raise GroundingError('invalid_grounding')
        if type(cited) is not list or not cited:
            raise GroundingError('missing_source')
        if any(type(label) is not str or not label for label in cited):
            raise GroundingError('missing_source')
        if any(label not in labels for label in cited):
            raise GroundingError('unknown_source')
        if len(cited) != len(set(cited)):
            raise GroundingError('invalid_grounding')
        content = content.strip().replace('\r\n', '\n').replace('\r', '\n')
        paragraphs = re.split(r'\n(?:[ \t]*\n)+', content)
        for paragraph in paragraphs:
            paragraph = paragraph.rstrip()
            if not paragraph.strip():
                raise GroundingError('invalid_grounding')
            claims.append({'text': paragraph, 'source_labels': list(cited)})
            rendered.append(paragraph + ' ' + ' '.join(f'[{label}]' for label in cited))
        used_labels.update(cited)
    used_sources = [source for source in sources if source['label'] in used_labels]
    return '\n\n'.join(rendered), {'status': 'answered', 'claims': claims, 'clarification': ''}, used_sources


def parse_grounding(raw, sources):
    return parse_grounding_data(strict_json(raw), sources)
