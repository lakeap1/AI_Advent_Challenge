"""HTTP-транспорт: авторизация, доставка JSON и классификация ошибок сети."""

import httpx


class TransportError(Exception):
    def __init__(self, code: str, *, request_started: bool = True):
        self.code = code
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
            if response.status_code in (401, 403):
                raise TransportError("authentication")
            if response.status_code == 429:
                raise TransportError("rate_limit")
            if not response.is_success:
                raise TransportError("provider_error")
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
