from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse


CONFIG_ENV_VAR = "LSG_CONFIG_PATH"
DEFAULT_CONFIG_VERSION = 1
DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 8178
DEFAULT_PROXY_CONNECT_TIMEOUT_MS = 10000
DEFAULT_PROXY_READ_TIMEOUT_MS = 120000
DEFAULT_PROXY_REQUEST_TIMEOUT_MS = 300000
ALIAS_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


DEFAULT_SETTINGS: dict[str, Any] = {
    "version": DEFAULT_CONFIG_VERSION,
    "server": {
        "host": DEFAULT_SERVER_HOST,
        "port": DEFAULT_SERVER_PORT,
    },
    "defaultUpstream": "default",
    "auth": {
        "enabled": False,
        "downstreamApiKeys": [],
    },
    "proxy": {
        "connectTimeoutMs": DEFAULT_PROXY_CONNECT_TIMEOUT_MS,
        "readTimeoutMs": DEFAULT_PROXY_READ_TIMEOUT_MS,
        "requestTimeoutMs": DEFAULT_PROXY_REQUEST_TIMEOUT_MS,
    },
    "upstreams": {
        "default": {
            "baseUrl": "",
            "apiKey": "",
            "routePrefix": None,
        },
    },
}


@dataclass(frozen=True)
class ConfigDiagnostic:
    path: str
    code: str
    message: str


@dataclass(frozen=True)
class ServerConfig:
    host: str
    port: int


@dataclass(frozen=True)
class AuthConfig:
    enabled: bool
    downstream_api_keys: tuple[str, ...]


@dataclass(frozen=True)
class ProxyConfig:
    connect_timeout_ms: int
    read_timeout_ms: int
    request_timeout_ms: int


@dataclass(frozen=True)
class UpstreamConfig:
    alias: str
    base_url: str
    api_key: str
    route_prefix: str | None


@dataclass(frozen=True)
class GatewayConfig:
    version: int
    server: ServerConfig
    auth: AuthConfig
    proxy: ProxyConfig
    default_upstream: str
    upstreams: dict[str, UpstreamConfig]
    source_path: Path
    fingerprint: str

    @property
    def default_upstream_config(self) -> UpstreamConfig:
        return self.upstreams[self.default_upstream]


class ConfigError(Exception):
    def __init__(self, diagnostics: list[ConfigDiagnostic]):
        self.diagnostics = diagnostics
        super().__init__("\n".join(d.message for d in diagnostics))


def resolve_config_path(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    if config_path is not None:
        return Path(config_path).expanduser()

    resolved_env = os.environ if env is None else env
    override = resolved_env.get(CONFIG_ENV_VAR)
    if override:
        return Path(override).expanduser()

    resolved_home = Path.home() if home is None else home
    return resolved_home / ".lsg" / "settings.json"


def initialize_settings_file(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    force: bool = False,
) -> Path:
    path = resolve_config_path(config_path, env=env)
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not force:
        return path

    path.write_text(json.dumps(DEFAULT_SETTINGS, indent=2) + "\n", encoding="utf-8")
    return path


def load_settings(
    config_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    require_usable_upstream: bool = True,
) -> GatewayConfig:
    path = resolve_config_path(config_path, env=env)

    if not path.exists():
        raise ConfigError(
            [
                ConfigDiagnostic(
                    path=str(path),
                    code="config.missing",
                    message=(
                        f"Settings file not found at {path}. Run 'lsg config init' "
                        "or pass --config with a settings file path."
                    ),
                )
            ]
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(
            [
                ConfigDiagnostic(
                    path=str(path),
                    code="config.invalid_json",
                    message=f"Settings file is not valid JSON: {exc.msg}.",
                )
            ]
        ) from exc

    return parse_settings(raw, source_path=path, require_usable_upstream=require_usable_upstream)


def parse_settings(
    raw: Any,
    *,
    source_path: Path,
    require_usable_upstream: bool = True,
) -> GatewayConfig:
    raw = _apply_default_settings(raw)
    diagnostics = _validate_settings(raw, require_usable_upstream=require_usable_upstream)
    if diagnostics:
        raise ConfigError(diagnostics)

    upstreams = {
        alias: UpstreamConfig(
            alias=alias,
            base_url=entry["baseUrl"].rstrip("/"),
            api_key=entry["apiKey"],
            route_prefix=entry["routePrefix"],
        )
        for alias, entry in raw["upstreams"].items()
    }

    fingerprint = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    return GatewayConfig(
        version=raw["version"],
        server=ServerConfig(host=raw["server"]["host"], port=raw["server"]["port"]),
        auth=AuthConfig(
            enabled=raw["auth"]["enabled"],
            downstream_api_keys=tuple(raw["auth"]["downstreamApiKeys"]),
        ),
        proxy=ProxyConfig(
            connect_timeout_ms=raw["proxy"]["connectTimeoutMs"],
            read_timeout_ms=raw["proxy"]["readTimeoutMs"],
            request_timeout_ms=raw["proxy"]["requestTimeoutMs"],
        ),
        default_upstream=raw["defaultUpstream"],
        upstreams=upstreams,
        source_path=source_path,
        fingerprint=fingerprint,
    )


def _apply_default_settings(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw

    normalized = dict(raw)
    proxy = normalized.get("proxy")
    if proxy is None:
        normalized["proxy"] = dict(DEFAULT_SETTINGS["proxy"])
    elif isinstance(proxy, dict):
        normalized["proxy"] = {
            **DEFAULT_SETTINGS["proxy"],
            **proxy,
        }
    return normalized


def _validate_settings(raw: Any, *, require_usable_upstream: bool) -> list[ConfigDiagnostic]:
    diagnostics: list[ConfigDiagnostic] = []

    if not isinstance(raw, dict):
        return [
            ConfigDiagnostic(
                path="$",
                code="config.type",
                message="Settings must be a JSON object.",
            )
        ]

    version = raw.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != DEFAULT_CONFIG_VERSION:
        diagnostics.append(
            ConfigDiagnostic(
                path="version",
                code="version.unsupported",
                message=f"version must be {DEFAULT_CONFIG_VERSION}.",
            )
        )

    server = raw.get("server")
    if not isinstance(server, dict):
        diagnostics.append(
            ConfigDiagnostic(
                path="server",
                code="server.type",
                message="server must be an object with host and port.",
            )
        )
    else:
        host = server.get("host")
        if not isinstance(host, str) or not host.strip():
            diagnostics.append(
                ConfigDiagnostic(
                    path="server.host",
                    code="server.host.invalid",
                    message="server.host must be a non-empty string.",
                )
            )

        port = server.get("port")
        if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
            diagnostics.append(
                ConfigDiagnostic(
                    path="server.port",
                    code="server.port.invalid",
                    message="server.port must be an integer between 1 and 65535.",
                )
            )

    auth = raw.get("auth")
    if not isinstance(auth, dict):
        diagnostics.append(
            ConfigDiagnostic(
                path="auth",
                code="auth.type",
                message="auth must be an object with enabled and downstreamApiKeys.",
            )
        )
    else:
        enabled = auth.get("enabled")
        if not isinstance(enabled, bool):
            diagnostics.append(
                ConfigDiagnostic(
                    path="auth.enabled",
                    code="auth.enabled.invalid",
                    message="auth.enabled must be a boolean.",
                )
            )

        downstream_keys = auth.get("downstreamApiKeys")
        if not isinstance(downstream_keys, list) or not all(
            isinstance(key, str) for key in downstream_keys
        ):
            diagnostics.append(
                ConfigDiagnostic(
                    path="auth.downstreamApiKeys",
                    code="auth.downstream_api_keys.invalid",
                    message="auth.downstreamApiKeys must be an array of strings.",
                )
            )

    proxy = raw.get("proxy")
    if not isinstance(proxy, dict):
        diagnostics.append(
            ConfigDiagnostic(
                path="proxy",
                code="proxy.type",
                message="proxy must be an object with timeout settings.",
            )
        )
    else:
        for field_name in ("connectTimeoutMs", "readTimeoutMs", "requestTimeoutMs"):
            value = proxy.get(field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                diagnostics.append(
                    ConfigDiagnostic(
                        path=f"proxy.{field_name}",
                        code="proxy.timeout.invalid",
                        message=f"proxy.{field_name} must be a positive integer in milliseconds.",
                    )
                )

    default_upstream = raw.get("defaultUpstream")
    if not isinstance(default_upstream, str) or not default_upstream:
        diagnostics.append(
            ConfigDiagnostic(
                path="defaultUpstream",
                code="default_upstream.invalid",
                message="defaultUpstream must be a non-empty upstream alias.",
            )
        )

    upstreams = raw.get("upstreams")
    if not isinstance(upstreams, dict) or not upstreams:
        diagnostics.append(
            ConfigDiagnostic(
                path="upstreams",
                code="upstreams.invalid",
                message="upstreams must be a non-empty object keyed by alias.",
            )
        )
        return diagnostics

    route_prefixes: dict[str, str] = {}
    for alias, entry in upstreams.items():
        if not isinstance(alias, str) or not ALIAS_PATTERN.fullmatch(alias):
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}",
                    code="upstream.alias.invalid",
                    message="upstream aliases must match ^[A-Za-z0-9_-]+$.",
                )
            )

        if not isinstance(entry, dict):
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}",
                    code="upstream.type",
                    message="each upstream must be an object.",
                )
            )
            continue

        base_url = entry.get("baseUrl")
        if not isinstance(base_url, str):
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}.baseUrl",
                    code="upstream.base_url.type",
                    message="upstream baseUrl must be a string.",
                )
            )
        elif require_usable_upstream and not _is_http_url(base_url):
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}.baseUrl",
                    code="upstream.base_url.invalid",
                    message="upstream baseUrl must be an absolute http or https URL.",
                )
            )

        api_key = entry.get("apiKey")
        if not isinstance(api_key, str):
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}.apiKey",
                    code="upstream.api_key.type",
                    message="upstream apiKey must be a string.",
                )
            )
        elif require_usable_upstream and not api_key:
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}.apiKey",
                    code="upstream.api_key.missing",
                    message="upstream apiKey must be set before starting the gateway.",
                )
            )

        if "routePrefix" not in entry:
            diagnostics.append(
                ConfigDiagnostic(
                    path=f"upstreams.{alias}.routePrefix",
                    code="upstream.route_prefix.missing",
                    message="routePrefix must be present and set to null or a slash-prefixed string.",
                )
            )
            continue

        route_prefix = entry.get("routePrefix")
        if route_prefix is not None:
            if not isinstance(route_prefix, str) or not route_prefix.startswith("/"):
                diagnostics.append(
                    ConfigDiagnostic(
                        path=f"upstreams.{alias}.routePrefix",
                        code="upstream.route_prefix.invalid",
                        message="routePrefix must be null or a slash-prefixed string.",
                    )
                )
            elif route_prefix in route_prefixes:
                diagnostics.append(
                    ConfigDiagnostic(
                        path=f"upstreams.{alias}.routePrefix",
                        code="upstream.route_prefix.duplicate",
                        message=(
                            "routePrefix must be unique; "
                            f"it is already used by upstream '{route_prefixes[route_prefix]}'."
                        ),
                    )
                )
            else:
                route_prefixes[route_prefix] = alias

    if isinstance(default_upstream, str) and default_upstream not in upstreams:
        diagnostics.append(
            ConfigDiagnostic(
                path="defaultUpstream",
                code="default_upstream.missing",
                message="defaultUpstream must reference an existing upstream alias.",
            )
        )

    return diagnostics


def _is_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
