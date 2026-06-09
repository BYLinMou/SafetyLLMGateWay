from __future__ import annotations

import json
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from lsg_gateway.app import create_app
from lsg_gateway.config import DEFAULT_SETTINGS, parse_settings


def make_config():
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["upstreams"]["default"]["baseUrl"] = "https://api.example.test"
    settings["upstreams"]["default"]["apiKey"] = "secret-upstream-key"
    return parse_settings(settings, source_path=Path("settings.json"))


def test_health_endpoint_returns_gateway_metadata() -> None:
    app = create_app(make_config(), upstream_transport=httpx.MockTransport(lambda request: None))

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "defaultUpstream": "default"}


def test_proxy_forwards_method_path_query_body_and_upstream_authorization() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = request.content
        seen["authorization"] = request.headers.get("authorization")
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
                "authorization": "Bearer downstream-client-key",
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
    assert seen["content_type"] == "application/json"


def test_proxy_returns_safe_gateway_error_for_upstream_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("could not connect", request=request)

    app = create_app(make_config(), upstream_transport=httpx.MockTransport(handler))

    with TestClient(app) as client:
        response = client.get("/v1/models")

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "upstream_request_failed"
    assert "secret-upstream-key" not in response.text
