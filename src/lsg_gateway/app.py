from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from .config import GatewayConfig


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

REQUEST_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {
    "host",
    "content-length",
    "authorization",
}

RESPONSE_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {
    "content-encoding",
    "content-length",
}


def create_app(
    config: GatewayConfig,
    *,
    upstream_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.gateway_config = config
        app.state.upstream_client = httpx.AsyncClient(
            transport=upstream_transport,
            follow_redirects=False,
        )
        try:
            yield
        finally:
            await app.state.upstream_client.aclose()

    app = FastAPI(title="SafetyLLMGateWay", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "defaultUpstream": config.default_upstream,
        }

    @app.api_route("/", methods=PROXY_METHODS)
    async def proxy_root(request: Request) -> Response:
        return await _proxy(request, "")

    @app.api_route("/{path:path}", methods=PROXY_METHODS)
    async def proxy_path(request: Request, path: str) -> Response:
        return await _proxy(request, path)

    return app


async def _proxy(request: Request, path: str) -> Response:
    config: GatewayConfig = request.app.state.gateway_config
    upstream = config.default_upstream_config
    target_url = _build_target_url(upstream.base_url, path, request.url.query)
    headers = _build_upstream_headers(request.headers, upstream.api_key)
    body = await request.body()
    client: httpx.AsyncClient = request.app.state.upstream_client

    try:
        upstream_response = await client.request(
            request.method,
            target_url,
            content=body,
            headers=headers,
        )
    except httpx.RequestError:
        return JSONResponse(
            status_code=502,
            content={
                "error": {
                    "code": "upstream_request_failed",
                    "message": "The gateway could not reach the configured upstream.",
                }
            },
        )

    return Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_filter_response_headers(upstream_response.headers),
    )


def _build_target_url(base_url: str, path: str, query: str) -> str:
    target = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        target = f"{target}?{query}"
    return target


def _build_upstream_headers(incoming_headers: httpx.Headers, upstream_api_key: str) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming_headers.items()
        if name.lower() not in REQUEST_HEADERS_TO_DROP
    }
    headers["authorization"] = f"Bearer {upstream_api_key}"
    return headers


def _filter_response_headers(upstream_headers: httpx.Headers) -> dict[str, str]:
    return {
        name: value
        for name, value in upstream_headers.items()
        if name.lower() not in RESPONSE_HEADERS_TO_DROP
    }
