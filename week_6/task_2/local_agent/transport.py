"""Fixed loopback Ollama HTTP boundary, with no proxy or redirect support."""

import httpx


class LocalTransportError(Exception):
    pass


class OllamaTransport:
    BASE_URL = 'http://127.0.0.1:11434'

    def __init__(self, timeout_seconds=180):
        self.timeout_seconds = timeout_seconds

    def __call__(self, method, path, payload=None):
        if (method, path) not in {('GET', '/api/tags'), ('POST', '/api/chat')}:
            raise LocalTransportError('Недопустимый локальный маршрут Ollama.')
        try:
            with httpx.Client(base_url=self.BASE_URL, trust_env=False,
                              follow_redirects=False, timeout=self.timeout_seconds) as client:
                response = client.request(method, path, json=payload)
            if response.is_redirect:
                raise LocalTransportError('Ollama вернула перенаправление; локальный запрос остановлен.')
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise LocalTransportError('Ollama вернула неверный JSON.')
            return body
        except httpx.HTTPStatusError as error:
            raise LocalTransportError(f'Ollama вернула HTTP {error.response.status_code}.') from None
        except (httpx.RequestError, ValueError) as error:
            raise LocalTransportError(f'Нет ответа Ollama на 127.0.0.1:11434: {error}.') from None
