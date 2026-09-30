import json

import httpx
import pytest

from agent.transport import ResponsesTransport, TransportError


def test_http_request_uses_official_endpoint_and_timeout():
    seen = []

    def handle(request):
        seen.append(request)
        return httpx.Response(200, json={"status": "completed", "output": []})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        transport = ResponsesTransport("test-key", client=client)
        assert transport.create({"input": "Привет"}, 7)["status"] == "completed"
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.openai.com/v1/responses"
    assert seen[0].method == "POST"
    assert seen[0].headers["authorization"] == "Bearer test-key"
    assert json.loads(seen[0].content) == {"input": "Привет"}
    assert seen[0].extensions["timeout"]["read"] == 7


@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "authentication"), (429, "rate_limit"), (500, "provider_error"), (400, "provider_error"), (302, "provider_error")])
def test_http_failures_are_not_retried_or_exposed(status, code):
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(status, text="secret-provider-detail")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError) as error:
            ResponsesTransport("test-key", client=client).create({}, 5)
    assert error.value.code == code
    assert "secret" not in str(error.value)
    assert len(calls) == 1


@pytest.mark.parametrize("exception,code", [(httpx.ReadTimeout, "timeout"), (httpx.ConnectError, "connection")])
def test_network_errors(exception, code):
    def handle(request):
        raise exception("secret", request=request)

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError) as error:
            ResponsesTransport("test-key", client=client).create({}, 5)
    assert error.value.code == code


def test_non_json_response():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, text="not JSON"))) as client:
        with pytest.raises(TransportError, match="invalid_response"):
            ResponsesTransport("test-key", client=client).create({}, 5)


def test_missing_key_never_calls_network():
    def handle(_):
        pytest.fail("Network must not be called")

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError, match="not_configured"):
            ResponsesTransport("", client=client).create({}, 5)

@pytest.mark.parametrize('key', ['ключ', 'bad\nkey', 'bad\x00key'])
def test_unusable_header_key_is_safe_error(key):
    def handle(_):
        pytest.fail('Invalid key must not reach network')
    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        with pytest.raises(TransportError, match='authentication'):
            ResponsesTransport(key, client=client).create({}, 5)
