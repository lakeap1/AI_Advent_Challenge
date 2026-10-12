"""Provider contracts use mocked HTTP; no public API is needed for unit tests."""

import asyncio
from urllib.parse import quote

import httpx
import pytest
from mcp import types

from knowledge_server import server
from knowledge_server.providers import KnowledgeProviders, ProviderError


def providers(handler, *, clock=lambda: 0.0):
    transport = httpx.MockTransport(handler)
    return KnowledgeProviders(lambda: httpx.AsyncClient(transport=transport), clock=clock)


def response(request, data):
    return httpx.Response(200, json=data, request=request)


def run(awaitable):
    return asyncio.run(awaitable)


def test_wikipedia_extracts_text_and_bounds_results():
    def handler(request):
        assert request.url.host == "ru.wikipedia.org"
        assert request.url.params["generator"] == "search"
        assert request.url.params["gsrsearch"] == "цветовой круг"
        return response(request, {"query": {"pages": [
            {"index": 2, "title": "Второй", "extract": "<p>Второй</p>", "fullurl": "https://ru.wikipedia.org/wiki/Второй"},
            {"index": 1, "title": "Цветовой круг", "extract": "<script>ignore()</script><p>Цвет &amp; свет</p>", "fullurl": "https://evil.example/x"},
            {"index": 3, "title": "Третий", "extract": "Третий"},
        ]}})

    result = run(providers(handler).lookup_wikipedia("цветовой круг", "ru", 2))
    assert result["provider"] == "wikipedia"
    assert result["query"] == "цветовой круг"
    assert result["metadata"] == {"language": "ru", "result_count": 2, "empty": False}
    assert len(result["sources"]) == 2
    assert result["sources"][0]["excerpt"] == "Цвет & свет"
    assert result["sources"][0]["url"] == "https://ru.wikipedia.org/wiki/" + quote("Цветовой_круг")


def test_empty_wikipedia_is_success():
    result = run(providers(lambda request: response(request, {"query": {}})).lookup_wikipedia("nothing"))
    assert result["sources"] == []
    assert result["metadata"]["empty"] is True


def test_stackexchange_fetches_actual_answers_and_attribution():
    calls = []

    def handler(request):
        calls.append(request.url)
        assert request.url.host == "api.stackexchange.com"
        assert request.url.params["site"] == "blender"
        if request.url.path.endswith("/search/advanced"):
            assert request.url.params["answers"] == "1"
            return response(request, {"items": [{"question_id": 16354, "title": "Normal map &amp; seam"}], "quota_remaining": 100})
        assert request.url.path.endswith("/questions/16354/answers")
        return response(request, {"items": [
            {"question_id": 16354, "answer_id": 300, "body": "<p>Low vote</p>", "score": 1},
            {"question_id": 16354, "answer_id": 301, "body": "<p>Use <b>matching tangents</b> and padding.</p><script>attack()</script>", "score": 12, "owner": {"display_name": "A &amp; B"}, "creation_date": 1700000000},
        ], "quota_remaining": 99})

    result = run(providers(handler).search_stackexchange("normal map baking seams"))
    assert len(calls) == 2
    assert result["provider"] == "stackexchange"
    assert result["metadata"]["quota_remaining"] == 99
    assert result["sources"] == [{
        "title": "Normal map & seam", "url": "https://blender.stackexchange.com/a/301",
        "excerpt": "Use matching tangents and padding.", "question_id": 16354,
        "author": "A & B", "date": "2023-11-14", "score": 12,
    }]


def test_stackexchange_empty_search_does_not_fetch_answers():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return response(request, {"items": []})

    result = run(providers(handler).search_stackexchange("unknown topic"))
    assert len(calls) == 1
    assert result["sources"] == []
    assert result["metadata"]["empty"] is True


@pytest.mark.parametrize("method,kwargs", [
    ("lookup_wikipedia", {"query": ""}),
    ("lookup_wikipedia", {"query": "x" * 257}),
    ("lookup_wikipedia", {"query": "test", "language": "fr"}),
    ("lookup_wikipedia", {"query": "test", "limit": True}),
    ("lookup_wikipedia", {"query": "test", "limit": 4}),
    ("search_stackexchange", {"query": "test", "community": "stackoverflow"}),
    ("search_stackexchange", {"query": "test", "limit": 0}),
])
def test_invalid_arguments_make_no_http_request(method, kwargs):
    def fail(_request):
        pytest.fail("invalid input reached network")

    with pytest.raises(ProviderError):
        run(getattr(providers(fail), method)(**kwargs))


def test_upstream_http_json_timeout_and_size_fail_as_errors():
    def status(request):
        return httpx.Response(503, request=request)

    def invalid_json(request):
        return httpx.Response(200, text="not json", request=request)

    def too_large(request):
        return httpx.Response(200, content=b"x" * (256 * 1024 + 1), request=request)

    def timeout(request):
        raise httpx.ReadTimeout("read expired", request=request)

    for handler, expected in [(status, "HTTP 503"), (invalid_json, "invalid JSON"), (too_large, "exceeded"), (timeout, "timed out")]:
        with pytest.raises(ProviderError, match=expected):
            run(providers(handler).lookup_wikipedia("colors"))


def test_stackexchange_api_error_and_backoff_block_following_calls():
    now = [100.0]
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/search/advanced"):
            return response(request, {"items": [{"question_id": 1, "title": "Q"}]})
        return response(request, {"items": [{"question_id": 1, "answer_id": 2, "body": "A"}], "backoff": 10 if len(calls) == 2 else 0})

    source = providers(handler, clock=lambda: now[0])
    assert run(source.search_stackexchange("normal map"))["sources"]
    with pytest.raises(ProviderError, match="backoff active"):
        run(source.search_stackexchange("normal map"))
    assert len(calls) == 2
    now[0] = 110.0
    assert run(source.search_stackexchange("normal map"))["sources"]

    failing = providers(lambda request: response(request, {"error_id": 502, "error_message": "throttle"}))
    with pytest.raises(ProviderError, match="reported an error"):
        run(failing.search_stackexchange("test"))


def test_stackexchange_second_request_failure_cannot_return_partial_sources():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/search/advanced"):
            return response(request, {"items": [{"question_id": 7, "title": "Q"}]})
        return httpx.Response(503, request=request)

    with pytest.raises(ProviderError, match="HTTP 503"):
        run(providers(handler).search_stackexchange("seams"))
    assert len(calls) == 2


def test_stackexchange_search_backoff_blocks_answer_fetch_and_next_call():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return response(request, {"items": [{"question_id": 7, "title": "Q"}], "backoff": 20})

    source = providers(handler)
    with pytest.raises(ProviderError, match="backoff before answer retrieval"):
        run(source.search_stackexchange("seams"))
    with pytest.raises(ProviderError, match="backoff active"):
        run(source.search_stackexchange("seams"))
    assert len(calls) == 1


def test_mcp_schemas_and_wire_error(monkeypatch):
    tools = {item.name: item for item in run(server.mcp.list_tools())}
    assert set(tools) == {"lookup_wikipedia", "search_stackexchange"}
    assert tools["lookup_wikipedia"].input_schema["properties"]["language"]["default"] == "en"
    assert tools["search_stackexchange"].input_schema["properties"]["community"]["default"] == "blender"

    monkeypatch.setattr(server, "providers", providers(lambda request: response(request, {"query": {}})))
    success = run(server.mcp.call_tool("lookup_wikipedia", {"query": "colors"}))
    assert success.structured_content["provider"] == "wikipedia"

    params = types.CallToolRequestParams(name="lookup_wikipedia", arguments={"query": ""})
    failure = run(server.mcp._handle_call_tool(None, params))
    assert failure.is_error is True
    assert "query" in failure.content[0].text
