from __future__ import annotations

import json
from pathlib import Path

import pytest

from lsg_gateway.config import (
    DEFAULT_SETTINGS,
    ConfigError,
    initialize_settings_file,
    load_settings,
    resolve_config_path,
)


def valid_settings() -> dict:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["upstreams"]["default"]["baseUrl"] = "https://api.example.test"
    settings["upstreams"]["default"]["apiKey"] = "secret-upstream-key"
    return settings


def test_resolve_config_path_uses_explicit_path_before_environment(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.json"
    env_path = tmp_path / "env.json"

    resolved = resolve_config_path(explicit, env={"LSG_CONFIG_PATH": str(env_path)})

    assert resolved == explicit


def test_resolve_config_path_uses_environment_override(tmp_path: Path) -> None:
    env_path = tmp_path / "settings.json"

    resolved = resolve_config_path(env={"LSG_CONFIG_PATH": str(env_path)})

    assert resolved == env_path


def test_resolve_config_path_uses_dotfile_default(tmp_path: Path) -> None:
    resolved = resolve_config_path(env={}, home=tmp_path)

    assert resolved == tmp_path / ".lsg" / "settings.json"


def test_initialize_settings_file_creates_default_json(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "settings.json"

    created = initialize_settings_file(path)

    assert created == path
    assert json.loads(path.read_text(encoding="utf-8")) == DEFAULT_SETTINGS


def test_load_settings_reads_valid_config(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(valid_settings()), encoding="utf-8")

    config = load_settings(path)

    assert config.source_path == path
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8178
    assert config.default_upstream_config.base_url == "https://api.example.test"
    assert config.default_upstream_config.api_key == "secret-upstream-key"
    assert len(config.fingerprint) == 64


def test_load_settings_preserves_custom_server_port(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["server"]["port"] = 9000
    path.write_text(json.dumps(settings), encoding="utf-8")

    config = load_settings(path)

    assert config.server.port == 9000


def test_load_settings_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError) as exc_info:
        load_settings(tmp_path / "missing.json")

    assert exc_info.value.diagnostics[0].code == "config.missing"


def test_load_settings_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text("{", encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert exc_info.value.diagnostics[0].code == "config.invalid_json"


def test_load_settings_rejects_invalid_alias_without_printing_secret(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["upstreams"]["bad alias"] = {
        "baseUrl": "https://other.example.test",
        "apiKey": "very-secret-value",
        "routePrefix": "/other",
    }
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    messages = "\n".join(d.message for d in exc_info.value.diagnostics)
    assert "upstream.alias.invalid" in {d.code for d in exc_info.value.diagnostics}
    assert "very-secret-value" not in messages


def test_load_settings_rejects_missing_default_upstream(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["defaultUpstream"] = "missing"
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert "default_upstream.missing" in {d.code for d in exc_info.value.diagnostics}


def test_load_settings_rejects_duplicate_route_prefix(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["upstreams"]["one"] = {
        "baseUrl": "https://one.example.test",
        "apiKey": "secret-one",
        "routePrefix": "/proxy",
    }
    settings["upstreams"]["two"] = {
        "baseUrl": "https://two.example.test",
        "apiKey": "secret-two",
        "routePrefix": "/proxy",
    }
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert "upstream.route_prefix.duplicate" in {d.code for d in exc_info.value.diagnostics}


def test_load_settings_rejects_missing_route_prefix(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    del settings["upstreams"]["default"]["routePrefix"]
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert "upstream.route_prefix.missing" in {d.code for d in exc_info.value.diagnostics}


def test_load_settings_rejects_invalid_server_port(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["server"]["port"] = 70000
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert "server.port.invalid" in {d.code for d in exc_info.value.diagnostics}


def test_load_settings_rejects_malformed_base_url_for_startup(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    settings = valid_settings()
    settings["upstreams"]["default"]["baseUrl"] = "not-a-url"
    path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ConfigError) as exc_info:
        load_settings(path)

    assert "upstream.base_url.invalid" in {d.code for d in exc_info.value.diagnostics}
