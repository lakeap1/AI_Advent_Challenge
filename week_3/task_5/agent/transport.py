"""HTTP-транспорт: авторизация, доставка JSON и классификация ошибок сети."""

import httpx


class TransportError(Exception):
    def __init__(self, code: str, *, request_started: bool = True, http_status=None, provider_code=None):
        self.code = code
        self.http_status = http_status
        self.provider_code = provider_code
        self.request_started = request_started
        super().__init__(code)


class ResponsesTransport:
    def __init__(self, api_key: str | None, *, client=None):
        self._api_key = (api_key or "").strip()
        self._client = client

    def create(self, payload: dict, timeout: float) -> object:
        if not self._api_key:
            raise TransportError("not_configured", request_started=False)
        if not all(33 <= ord(char) <= 126 for char in self._api_key):
            raise TransportError("authentication", request_started=False)
        try:
            if self._client is not None:
                response = self._post(self._client, payload, timeout)
            else:
                # Клиент закрывается после запроса; ретраи httpx по умолчанию выключены.
                with httpx.Client() as client:
                    response = self._post(client, payload, timeout)
            if not response.is_success:
                try:
                    body = response.json()
                    error = body.get('error') if isinstance(body, dict) else None
                    raw_code = error.get('code') if isinstance(error, dict) else None
                except (ValueError, UnicodeError):
                    raw_code = None
                safe_code = raw_code if isinstance(raw_code, str) and raw_code in ('context_length_exceeded', 'insufficient_quota', 'rate_limit_exceeded') else None
                code = 'provider_error'
                if response.status_code in (401, 403):
                    code = 'authentication'
                elif response.status_code == 429:
                    code = 'insufficient_quota' if safe_code == 'insufficient_quota' else 'rate_limit'
                elif response.status_code == 400 and safe_code == 'context_length_exceeded':
                    code = 'context_length_exceeded'
                raise TransportError(code, http_status=response.status_code, provider_code=safe_code)
            try:
                return response.json()
            except (ValueError, UnicodeError):
                raise TransportError("invalid_response") from None
        except httpx.TimeoutException:
            raise TransportError("timeout") from None
        except httpx.RequestError:
            raise TransportError("connection") from None

    def _post(self, client, payload, timeout):
        return client.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self._api_key}"},
            json=payload,
            timeout=timeout,
            follow_redirects=False,
        )
