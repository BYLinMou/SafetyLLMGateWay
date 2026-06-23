from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from lsg_gateway.app import create_app
from lsg_gateway.config import DEFAULT_SETTINGS, parse_settings


class ClosingAsyncByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def make_config(*, auth_enabled: bool = True):
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["upstreams"]["default"]["baseUrl"] = "https://api.example.test"
    settings["upstreams"]["default"]["apiKey"] = "secret-upstream-key"
    settings["auth"]["enabled"] = auth_enabled
    settings["auth"]["downstreamApiKeys"] = ["secret-downstream-key"]
    return parse_settings(settings, source_path=Path("settings.json"))


def test_public_health_endpoint_returns_minimal_liveness_without_auth() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("/health must not be proxied to an upstream")

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {"status": "ok"}
    raw_response = response.text
    privileged_values = [
        "configLoaded",
        "127.0.0.1",
        "8178",
        "defaultUpstream",
        "upstreamAliases",
        "default",
        "secret-upstream-key",
        "secret-downstream-key",
        "settings.json",
    ]
    for value in privileged_values:
        assert value not in raw_response


def test_proxy_forwards_method_path_query_body_and_upstream_authorization() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["authorization"] = request.headers.get("authorization")
        seen["x_api_key"] = request.headers.get("x-api-key")
        seen["content_type"] = request.headers.get("content-type")
        return httpx.Response(
            201,
            headers={"content-type": "application/json", "x-upstream": "ok"},
            json={"proxied": True},
        )

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions?stream=false",
            headers={
                "authorization": "Bearer secret-downstream-key",
                "x-api-key": "must-not-forward",
                "content-type": "application/json",
            },
            json={"model": "example", "messages": []},
        )

    assert response.status_code == 201
    assert response.headers["x-upstream"] == "ok"
    assert response.json() == {"proxied": True}
    assert seen["method"] == "POST"
    assert seen["url"] == "https://api.example.test/v1/chat/completions?stream=false"
    assert json.loads(seen["body"]) == {"model": "example", "messages": []}
    assert seen["authorization"] == "Bearer secret-upstream-key"
    assert seen["x_api_key"] is None
    assert seen["content_type"] == "application/json"


def test_downstream_auth_accepts_x_api_key_and_strips_client_credentials() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization")
        seen["x_api_key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"ok": True})

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.get("/v1/models", headers={"x-api-key": "secret-downstream-key"})

    assert response.status_code == 200
    assert seen["authorization"] == "Bearer secret-upstream-key"
    assert seen["x_api_key"] is None


def test_downstream_auth_rejects_missing_or_invalid_credentials_before_upstream() -> None:
    upstream_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal upstream_calls
        upstream_calls += 1
        return httpx.Response(200, json={"should_not": "happen"})

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        missing = client.get("/v1/models")
        invalid = client.get("/v1/models", headers={"authorization": "Bearer wrong"})

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert missing.json()["error"]["requestId"] == missing.headers["x-request-id"]
    assert invalid.json()["error"]["code"] == "downstream_auth_failed"
    assert upstream_calls == 0


def test_proxy_returns_safe_gateway_error_for_upstream_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not connect", request=request)

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.get("/v1/models", headers={"authorization": "Bearer secret-downstream-key"})

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_request_failed"
    assert response.json()["error"]["requestId"] == response.headers["x-request-id"]
    assert "secret-upstream-key" not in response.text


def test_proxy_returns_safe_gateway_error_for_upstream_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.get("/v1/models", headers={"authorization": "Bearer secret-downstream-key"})

    assert response.status_code == 504
    assert response.json()["error"]["code"] == "upstream_timeout"
    assert response.json()["error"]["requestId"] == response.headers["x-request-id"]
    assert "secret-upstream-key" not in response.text


def test_streaming_request_returns_upstream_chunks_and_closes_stream() -> None:
    stream = ClosingAsyncByteStream([b"data: one\n\n", b"data: two\n\n"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=stream,
        )

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        with client.stream(
            "POST",
            "/v1/chat/completions",
            headers={"authorization": "Bearer secret-downstream-key"},
            json={"model": "example", "stream": True},
        ) as response:
            body = b"".join(response.iter_bytes())

    assert response.status_code == 200
    assert body == b"data: one\n\ndata: two\n\n"
    assert stream.closed


def test_browser_style_get_root_and_models_pass_through_without_json_parsing() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html>not json</html>",
        )

    app = create_app(make_config(auth_enabled=False), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        root = client.get("/")
        models = client.get("/v1/models")

    assert root.status_code == 200
    assert root.content == b"<html>not json</html>"
    assert models.status_code == 200
    assert models.content == b"<html>not json</html>"
    assert seen_paths == ["/", "/v1/models"]


def test_proxy_logs_structured_sanitized_event(caplog) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with caplog.at_level(logging.INFO, logger="lsg_gateway.proxy"):
        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                headers={
                    "authorization": "Bearer secret-downstream-key",
                    "x-request-id": "req-123",
                },
                json={
                    "model": "example",
                    "apiKey": "raw-body-secret",
                    "messages": [{"content": "hello"}],
                },
            )

    assert response.status_code == 200
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "secret-downstream-key" not in log_text
    assert "secret-upstream-key" not in log_text
    assert "raw-body-secret" not in log_text

    event = json.loads(caplog.records[-1].getMessage())
    assert event["requestId"] == "req-123"
    assert event["method"] == "POST"
    assert event["path"] == "/v1/chat/completions"
    assert event["upstreamAlias"] == "default"
    assert event["routePrefix"] is None
    assert event["authResult"] == "bearer"
    assert event["status"] == 200
    assert event["errorCategory"] is None
    assert event["redactionSummary"] == [
        {"path": "$.apiKey", "reason": "sensitive_field", "count": 1}
    ]
