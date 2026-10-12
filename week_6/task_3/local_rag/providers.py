"""Narrow Ollama and Responses HTTP boundaries and response contracts."""

import json
import math

import httpx


ANSWER_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'answer': {'type': 'string'},
        'source_labels': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['answer', 'source_labels'],
}


class ProviderError(Exception):
    """Safe provider failure without raw request or secret material."""


class OllamaTransport:
    BASE_URL = 'http://127.0.0.1:11434'
    ROUTES = {('GET', '/api/tags'), ('POST', '/api/show'),
              ('POST', '/api/embed'), ('POST', '/api/chat')}

    def __init__(self, timeout_seconds=240):
        self.timeout_seconds = timeout_seconds

    def __call__(self, method, path, payload=None):
        if (method, path) not in self.ROUTES:
            raise ProviderError('Недопустимый маршрут Ollama.')
        try:
            with httpx.Client(base_url=self.BASE_URL, trust_env=False,
                              follow_redirects=False, timeout=self.timeout_seconds) as client:
                response = client.request(method, path, json=payload)
            if response.is_redirect:
                raise ProviderError('Ollama вернула перенаправление.')
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ProviderError('Ollama вернула неверный JSON.')
            return value
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as error:
            raise ProviderError(f'Ollama недоступна или вернула ошибку ({type(error).__name__}).') from None


class ResponsesTransport:
    URL = 'https://api.openai.com/v1/responses'

    def __init__(self, api_key, timeout_seconds=240):
        if not api_key:
            raise ValueError('Cloud API key required')
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def __call__(self, payload):
        try:
            with httpx.Client(trust_env=False, follow_redirects=False,
                              timeout=self.timeout_seconds) as client:
                response = client.post(self.URL, json=payload,
                                       headers={'Authorization': f'Bearer {self.api_key}'})
            if response.is_redirect:
                raise ProviderError('Cloud API вернул перенаправление.')
            response.raise_for_status()
            value = response.json()
            if not isinstance(value, dict):
                raise ProviderError('Cloud API вернул неверный JSON.')
            return value
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as error:
            raise ProviderError(f'Cloud API недоступен или вернул ошибку ({type(error).__name__}).') from None


def valid_vector(value, dimensions):
    if not isinstance(value, list) or len(value) != dimensions:
        return False
    if any(type(item) not in (int, float) or not math.isfinite(item) for item in value):
        return False
    return any(item != 0 for item in value)


def _count(value):
    return value if type(value) is int and value >= 0 else None


def ollama_usage(body, *, embedding=False):
    input_tokens = _count(body.get('prompt_eval_count')) if isinstance(body, dict) else None
    output_tokens = 0 if embedding else _count(body.get('eval_count')) if isinstance(body, dict) else None
    complete = input_tokens is not None and output_tokens is not None
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens if complete else None,
            'cached_input_tokens': None, 'reasoning_output_tokens': None}, complete


def cloud_usage(body):
    raw = body.get('usage') if isinstance(body, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    input_tokens = _count(raw.get('input_tokens'))
    output_tokens = _count(raw.get('output_tokens'))
    reported_total = _count(raw.get('total_tokens'))
    details = raw.get('input_tokens_details') or {}
    cached = _count(details.get('cached_tokens')) if isinstance(details, dict) else None
    write = _count(details.get('cache_write_tokens', 0)) if isinstance(details, dict) else None
    output_details = raw.get('output_tokens_details') or {}
    reasoning = _count(output_details.get('reasoning_tokens')) if isinstance(output_details, dict) else None
    complete = (input_tokens is not None and output_tokens is not None and
                reported_total == input_tokens + output_tokens and
                cached is not None and write is not None and cached + write <= input_tokens)
    return {'input_tokens': input_tokens, 'output_tokens': output_tokens,
            'total_tokens': reported_total if complete else None,
            'reported_total_tokens': reported_total,
            'cached_input_tokens': cached, 'cache_write_input_tokens': write,
            'reasoning_output_tokens': reasoning}, complete


def cloud_cost(usage, tariff):
    regular = usage['input_tokens'] - usage['cached_input_tokens'] - usage['cache_write_input_tokens']
    return round((regular * tariff['input_usd_per_million'] +
                  usage['cached_input_tokens'] * tariff['cached_input_usd_per_million'] +
                  usage['cache_write_input_tokens'] * tariff['cache_write_usd_per_million'] +
                  usage['output_tokens'] * tariff['output_usd_per_million']) / 1_000_000, 12)


def response_text(body):
    if not isinstance(body, dict) or body.get('status') != 'completed':
        return None
    outputs = body.get('output')
    if not isinstance(outputs, list):
        return None
    pieces = []
    for output in outputs:
        if not isinstance(output, dict) or not isinstance(output.get('content'), list):
            continue
        for content in output['content']:
            if isinstance(content, dict) and content.get('type') == 'output_text':
                pieces.append(content.get('text'))
    return ''.join(pieces) if pieces and all(isinstance(piece, str) for piece in pieces) else None


def validate_answer(content, labels, *, done=True, max_chars=1800):
    if not done or not isinstance(content, str):
        return None, 'incomplete_response'
    try:
        value = json.loads(content)
    except json.JSONDecodeError:
        return None, 'invalid_json'
    if not isinstance(value, dict) or set(value) != {'answer', 'source_labels'}:
        return None, 'invalid_schema'
    answer, cited = value['answer'], value['source_labels']
    if not isinstance(answer, str) or not answer.strip() or len(answer) > max_chars:
        return None, 'invalid_answer'
    if not isinstance(cited, list) or not cited or any(type(label) is not str for label in cited):
        return None, 'invalid_labels'
    if not set(cited).issubset(labels):
        return None, 'unknown_labels'
    return {'answer': answer.strip(), 'source_labels': list(dict.fromkeys(cited))}, None
