"""Restricted Ollama loopback transport; never honors proxy variables or redirects."""

import httpx


class LocalOllamaTransport:
    BASE_URL = 'http://127.0.0.1:11435'
    ROUTES = {('GET', '/api/version'), ('GET', '/api/tags'),
              ('GET', '/api/ps'), ('POST', '/api/chat')}

    def __init__(self, timeout_seconds=180):
        self.timeout_seconds = timeout_seconds

    def __call__(self, method, path, payload=None):
        if (method, path) not in self.ROUTES:
            raise ValueError('Недопустимый локальный маршрут Ollama.')
        try:
            with httpx.Client(base_url=self.BASE_URL, trust_env=False,
                              follow_redirects=False, timeout=self.timeout_seconds) as client:
                response = client.request(method, path, json=payload)
            if response.is_redirect:
                raise ValueError('Ollama вернула перенаправление.')
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError('Ollama вернула неверный JSON.')
            return body
        except httpx.HTTPStatusError as error:
            raise ValueError(f'Ollama вернула HTTP {error.response.status_code}.') from None
        except (httpx.RequestError, ValueError) as error:
            raise ValueError(f'Локальная Ollama недоступна: {error}.') from None
