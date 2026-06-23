from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .config import GatewayConfig, UpstreamConfig


PROXY_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]
LOGGER = logging.getLogger("lsg_gateway.proxy")

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
    "x-api-key",
    "x-request-id",
}

RESPONSE_HEADERS_TO_DROP = HOP_BY_HOP_HEADERS | {
    "content-encoding",
    "content-length",
    "x-request-id",
}

SENSITIVE_FIELD_NAMES = {
    "apikey",
    "api_key",
    "authorization",
    "client_secret",
    "password",
    "refresh_token",
    "secret",
    "token",
    "x_api_key",
    "x-api-key",
}


@dataclass
class ProxyContext:
    request: Request
    path: str
    config: GatewayConfig
    upstream: UpstreamConfig
    target_url: str
    request_id: str
    started_at: float = field(default_factory=time.perf_counter)
    auth_result: str = "not_checked"
    body: bytes = b""
    redaction_summary: list[dict[str, object]] = field(default_factory=list)

    @property
    def method(self) -> str:
        return self.request.method

    @property
    def log_path(self) -> str:
        return self.request.url.path

    @property
    def query(self) -> str:
        return self.request.url.query


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
        }

    @app.api_route("/", methods=PROXY_METHODS)
    async def proxy_root(request: Request) -> Response:
        return await _proxy(request, "")

    @app.api_route("/{path:path}", methods=PROXY_METHODS)
    async def proxy_path(request: Request, path: str) -> Response:
        return await _proxy(request, path)

    return app


async def _proxy(request: Request, path: str) -> Response:
    context = _build_proxy_context(request, path)
    is_authorized, auth_result = _authorize_downstream(request.headers, context.config)
    context.auth_result = auth_result
    if not is_authorized:
        response = _gateway_error(
            status_code=401,
            code="downstream_auth_failed",
            message="Missing or invalid downstream API key.",
            request_id=context.request_id,
        )
        _log_proxy_event(context, response.status_code, "downstream_auth_failed")
        return response

    context.body = await request.body()
    context.redaction_summary = _build_redaction_summary(context.body)

    if _is_streaming_request(request, context.body):
        return await _proxy_streaming(context)

    return await _proxy_buffered(context)


def _build_proxy_context(request: Request, path: str) -> ProxyContext:
    config: GatewayConfig = request.app.state.gateway_config
    upstream = config.default_upstream_config
    return ProxyContext(
        request=request,
        path=path,
        config=config,
        upstream=upstream,
        target_url=_build_target_url(upstream.base_url, path, request.url.query),
        request_id=_extract_request_id(request.headers),
    )


async def _proxy_buffered(context: ProxyContext) -> Response:
    client: httpx.AsyncClient = context.request.app.state.upstream_client

    try:
        upstream_response = await asyncio.wait_for(
            client.request(
                context.method,
                context.target_url,
                content=context.body,
                headers=_build_upstream_headers(
                    context.request.headers,
                    context.upstream.api_key,
                    context.request_id,
                ),
                timeout=_httpx_timeout(context, read_timeout=True),
            ),
            timeout=_request_timeout_seconds(context),
        )
    except (asyncio.TimeoutError, httpx.TimeoutException):
        response = _gateway_error(
            status_code=504,
            code="upstream_timeout",
            message="The configured upstream did not respond before the gateway timeout.",
            request_id=context.request_id,
        )
        _log_proxy_event(context, response.status_code, "upstream_timeout")
        return response
    except httpx.RequestError:
        response = _gateway_error(
            status_code=502,
            code="upstream_request_failed",
            message="The gateway could not reach the configured upstream.",
            request_id=context.request_id,
        )
        _log_proxy_event(context, response.status_code, "upstream_request_failed")
        return response

    response = Response(
        content=upstream_response.content,
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers, context.request_id),
    )
    _log_proxy_event(context, response.status_code, None)
    return response


async def _proxy_streaming(context: ProxyContext) -> Response:
    client: httpx.AsyncClient = context.request.app.state.upstream_client
    request = client.build_request(
        context.method,
        context.target_url,
        content=context.body,
        headers=_build_upstream_headers(
            context.request.headers,
            context.upstream.api_key,
            context.request_id,
        ),
        timeout=_httpx_timeout(context, read_timeout=False),
    )
    context.body = b""

    deadline = time.monotonic() + _request_timeout_seconds(context)

    try:
        upstream_response = await asyncio.wait_for(
            client.send(request, stream=True),
            timeout=_remaining_timeout(deadline),
        )
        upstream_iterator = upstream_response.aiter_raw().__aiter__()
        try:
            first_chunk = await asyncio.wait_for(
                upstream_iterator.__anext__(),
                timeout=_remaining_timeout(deadline),
            )
        except StopAsyncIteration:
            first_chunk = b""
    except (asyncio.TimeoutError, httpx.TimeoutException):
        if "upstream_response" in locals():
            await upstream_response.aclose()
        response = _gateway_error(
            status_code=504,
            code="upstream_timeout",
            message="The configured upstream did not send the first streaming chunk before the gateway timeout.",
            request_id=context.request_id,
        )
        _log_proxy_event(context, response.status_code, "upstream_timeout")
        return response
    except httpx.RequestError:
        response = _gateway_error(
            status_code=502,
            code="upstream_request_failed",
            message="The gateway could not reach the configured upstream.",
            request_id=context.request_id,
        )
        _log_proxy_event(context, response.status_code, "upstream_request_failed")
        return response

    async def body_iterator() -> AsyncIterator[bytes]:
        error_category: str | None = None
        try:
            if first_chunk:
                yield first_chunk
            async for chunk in upstream_iterator:
                yield chunk
        except asyncio.CancelledError:
            error_category = "client_disconnected"
            raise
        except httpx.RequestError:
            error_category = "upstream_stream_failed"
            raise
        finally:
            await upstream_response.aclose()
            _log_proxy_event(context, upstream_response.status_code, error_category)

    return StreamingResponse(
        body_iterator(),
        status_code=upstream_response.status_code,
        headers=_response_headers(upstream_response.headers, context.request_id),
    )


def _build_target_url(base_url: str, path: str, query: str) -> str:
    target = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    if query:
        target = f"{target}?{query}"
    return target


def _build_upstream_headers(
    incoming_headers: httpx.Headers,
    upstream_api_key: str,
    request_id: str,
) -> dict[str, str]:
    headers = {
        name: value
        for name, value in incoming_headers.items()
        if name.lower() not in REQUEST_HEADERS_TO_DROP
    }
    headers["authorization"] = f"Bearer {upstream_api_key}"
    headers["x-request-id"] = request_id
    return headers


def _response_headers(upstream_headers: httpx.Headers, request_id: str) -> dict[str, str]:
    headers = {
        name: value
        for name, value in upstream_headers.items()
        if name.lower() not in RESPONSE_HEADERS_TO_DROP
    }
    headers["x-request-id"] = request_id
    return headers


def _extract_request_id(headers: httpx.Headers) -> str:
    request_id = headers.get("x-request-id", "").strip()
    if request_id and "\r" not in request_id and "\n" not in request_id:
        return request_id
    return uuid.uuid4().hex


def _authorize_downstream(
    headers: httpx.Headers,
    config: GatewayConfig,
) -> tuple[bool, str]:
    if not config.auth.enabled:
        return True, "disabled"

    bearer_token = _bearer_token(headers.get("authorization"))
    if bearer_token and _matches_downstream_key(bearer_token, config.auth.downstream_api_keys):
        return True, "bearer"

    x_api_key = headers.get("x-api-key")
    if x_api_key and _matches_downstream_key(x_api_key.strip(), config.auth.downstream_api_keys):
        return True, "x_api_key"

    if bearer_token or x_api_key:
        return False, "invalid"
    return False, "missing"


def _bearer_token(value: str | None) -> str | None:
    if not value:
        return None
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return None


def _matches_downstream_key(candidate: str, allowed_keys: tuple[str, ...]) -> bool:
    return any(secrets.compare_digest(candidate, allowed_key) for allowed_key in allowed_keys)


def _build_redaction_summary(body: bytes) -> list[dict[str, object]]:
    if not body:
        return []

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return []

    summary: Counter[tuple[str, str]] = Counter()
    _collect_redactions(payload, "$", summary)
    return [
        {
            "path": path,
            "reason": reason,
            "count": count,
        }
        for (path, reason), count in sorted(summary.items())
    ]


def _collect_redactions(value: object, path: str, summary: Counter[tuple[str, str]]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = _join_json_path(path, str(key))
            if _is_sensitive_field_name(str(key)):
                summary[(child_path, "sensitive_field")] += _count_json_leaves(child)
            else:
                _collect_redactions(child, child_path, summary)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _collect_redactions(child, f"{path}[{index}]", summary)


def _is_sensitive_field_name(name: str) -> bool:
    normalized = name.replace("-", "_").lower()
    compact = normalized.replace("_", "")
    return (
        normalized in SENSITIVE_FIELD_NAMES
        or compact in SENSITIVE_FIELD_NAMES
        or compact.endswith("apikey")
        or compact.endswith("token")
        or compact.endswith("secret")
    )


def _count_json_leaves(value: object) -> int:
    if isinstance(value, dict):
        return sum(_count_json_leaves(child) for child in value.values()) or 1
    if isinstance(value, list):
        return sum(_count_json_leaves(child) for child in value) or 1
    return 1


def _join_json_path(path: str, key: str) -> str:
    if key.replace("_", "").replace("-", "").isalnum():
        return f"{path}.{key}"
    return f"{path}[{json.dumps(key)}]"


def _is_streaming_request(request: Request, body: bytes) -> bool:
    query_stream = request.query_params.get("stream")
    if isinstance(query_stream, str) and query_stream.lower() == "true":
        return True

    if not body:
        return False

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return False

    return isinstance(payload, dict) and payload.get("stream") is True


def _httpx_timeout(context: ProxyContext, *, read_timeout: bool) -> httpx.Timeout:
    connect_timeout = context.config.proxy.connect_timeout_ms / 1000
    read = context.config.proxy.read_timeout_ms / 1000 if read_timeout else None
    return httpx.Timeout(
        connect=connect_timeout,
        read=read,
        write=_request_timeout_seconds(context),
        pool=connect_timeout,
    )


def _request_timeout_seconds(context: ProxyContext) -> float:
    return context.config.proxy.request_timeout_ms / 1000


def _remaining_timeout(deadline: float) -> float:
    return max(deadline - time.monotonic(), 0.001)


def _gateway_error(
    *,
    status_code: int,
    code: str,
    message: str,
    request_id: str,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "requestId": request_id,
            }
        },
        headers={"x-request-id": request_id},
    )


def _log_proxy_event(
    context: ProxyContext,
    status_code: int,
    error_category: str | None,
) -> None:
    event = {
        "requestId": context.request_id,
        "method": context.method,
        "path": context.log_path,
        "upstreamAlias": context.upstream.alias,
        "routePrefix": context.upstream.route_prefix,
        "authResult": context.auth_result,
        "status": status_code,
        "duration": round(time.perf_counter() - context.started_at, 6),
        "errorCategory": error_category,
        "redactionSummary": context.redaction_summary,
    }
    LOGGER.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
