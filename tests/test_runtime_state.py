from __future__ import annotations

import json
from pathlib import Path

import pytest

from lsg_gateway.runtime_state import (
    RUNTIME_STATE_VERSION,
    RuntimeState,
    RuntimeStateError,
    clear_runtime_state,
    is_runtime_state_stale,
    read_runtime_state,
    resolve_runtime_state_path,
    write_runtime_state,
)


def make_state(tmp_path: Path) -> RuntimeState:
    return RuntimeState(
        version=RUNTIME_STATE_VERSION,
        pid=12345,
        host="127.0.0.1",
        port=8178,
        url="http://127.0.0.1:8178",
        started_at="2026-06-09T00:00:00Z",
        config_path=tmp_path / "settings.json",
        config_fingerprint="abc123",
    )


def test_resolve_runtime_state_path_uses_explicit_path_before_environment(tmp_path: Path) -> None:
    explicit = tmp_path / "explicit.json"
    env_path = tmp_path / "env.json"

    resolved = resolve_runtime_state_path(explicit, env={"LSG_RUNTIME_STATE_PATH": str(env_path)})

    assert resolved == explicit


def test_resolve_runtime_state_path_uses_environment_override(tmp_path: Path) -> None:
    env_path = tmp_path / "state.json"

    resolved = resolve_runtime_state_path(env={"LSG_RUNTIME_STATE_PATH": str(env_path)})

    assert resolved == env_path


def test_resolve_runtime_state_path_uses_platform_defaults(tmp_path: Path) -> None:
    linux_runtime = resolve_runtime_state_path(
        env={"XDG_RUNTIME_DIR": str(tmp_path / "runtime")},
        platform="linux",
        home=tmp_path,
    )
    linux_cache = resolve_runtime_state_path(
        env={"XDG_CACHE_HOME": str(tmp_path / "cache")},
        platform="linux",
        home=tmp_path,
    )
    macos = resolve_runtime_state_path(env={}, platform="darwin", home=tmp_path)
    windows = resolve_runtime_state_path(env={}, platform="win32", home=tmp_path)

    assert linux_runtime == tmp_path / "runtime" / "lsg" / "state.json"
    assert linux_cache == tmp_path / "cache" / "lsg" / "state.json"
    assert macos == tmp_path / "Library" / "Caches" / "lsg" / "state.json"
    assert windows == tmp_path / "AppData" / "Local" / "lsg" / "runtime" / "state.json"


def test_resolve_runtime_state_path_uses_windows_local_app_data(tmp_path: Path) -> None:
    local_app_data = tmp_path / "LocalAppData"

    resolved = resolve_runtime_state_path(
        env={"LOCALAPPDATA": str(local_app_data)},
        platform="win32",
        home=tmp_path,
    )

    assert resolved == local_app_data / "lsg" / "runtime" / "state.json"


def test_runtime_state_round_trips_without_raw_secrets(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state = make_state(tmp_path)

    written = write_runtime_state(state, state_path)
    loaded = read_runtime_state(state_path)

    assert written == state_path
    assert loaded == state
    raw_text = state_path.read_text(encoding="utf-8")
    assert "upstreamApiKey" not in raw_text
    assert "downstreamApiKeys" not in raw_text
    assert "secret-upstream-key" not in raw_text


def test_read_runtime_state_returns_none_when_missing(tmp_path: Path) -> None:
    assert read_runtime_state(tmp_path / "missing.json") is None


def test_read_runtime_state_rejects_invalid_json(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text("{", encoding="utf-8")

    with pytest.raises(RuntimeStateError):
        read_runtime_state(state_path)


def test_read_runtime_state_rejects_missing_required_fields(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"version": RUNTIME_STATE_VERSION}), encoding="utf-8")

    with pytest.raises(RuntimeStateError):
        read_runtime_state(state_path)


def test_clear_runtime_state_respects_expected_pid(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    write_runtime_state(make_state(tmp_path), state_path)

    assert clear_runtime_state(state_path, expected_pid=99999) is False
    assert state_path.exists()
    assert clear_runtime_state(state_path, expected_pid=12345) is True
    assert not state_path.exists()


def test_runtime_state_stale_detection_uses_injected_process_checker(tmp_path: Path) -> None:
    state = make_state(tmp_path)

    assert is_runtime_state_stale(state, process_checker=lambda pid: False)
    assert not is_runtime_state_stale(state, process_checker=lambda pid: True)
