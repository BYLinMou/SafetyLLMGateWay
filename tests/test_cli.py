from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from lsg_gateway import cli
from lsg_gateway.config import DEFAULT_SETTINGS
from lsg_gateway.runtime_state import RUNTIME_STATE_VERSION, RuntimeState, read_runtime_state, write_runtime_state


def write_settings(path: Path) -> None:
    settings = json.loads(json.dumps(DEFAULT_SETTINGS))
    settings["upstreams"]["default"]["baseUrl"] = "https://api.example.test"
    settings["upstreams"]["default"]["apiKey"] = "secret-upstream-key"
    path.write_text(json.dumps(settings), encoding="utf-8")


def make_state(tmp_path: Path, *, pid: int | None = None, port: int = 9000) -> RuntimeState:
    return RuntimeState(
        version=RUNTIME_STATE_VERSION,
        pid=os.getpid() if pid is None else pid,
        host="127.0.0.1",
        port=port,
        url=f"http://127.0.0.1:{port}",
        started_at="2026-06-09T00:00:00Z",
        config_path=tmp_path / "settings.json",
        config_fingerprint="abc123",
    )


def test_health_command_reads_runtime_state_before_settings(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state_path = tmp_path / "state.json"
    write_runtime_state(make_state(tmp_path, port=9000), state_path)
    monkeypatch.setattr(cli, "is_runtime_state_stale", lambda state: False)

    def fake_get(url: str, timeout: float) -> httpx.Response:
        assert url == "http://127.0.0.1:9000/health"
        assert timeout == 2.0
        return httpx.Response(200, json={"status": "ok", "defaultUpstream": "default"})

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    exit_code = cli.main(["health", "--state", str(state_path)])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "defaultUpstream": "default",
        "status": "ok",
    }


def test_health_command_falls_back_to_settings_when_runtime_state_is_missing(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "settings.json"
    write_settings(path)

    def fake_get(url: str, timeout: float) -> httpx.Response:
        assert url == "http://127.0.0.1:8178/health"
        assert timeout == 2.0
        return httpx.Response(200, json={"status": "ok", "defaultUpstream": "default"})

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    exit_code = cli.main(["health", "--config", str(path), "--state", str(tmp_path / "missing.json")])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "defaultUpstream": "default",
        "status": "ok",
    }


def test_health_command_allows_placeholder_upstream_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(DEFAULT_SETTINGS), encoding="utf-8")

    monkeypatch.setattr(
        cli.httpx,
        "get",
        lambda url, timeout: httpx.Response(200, json={"status": "ok"}),
    )

    assert cli.main(["health", "--config", str(path), "--state", str(tmp_path / "missing.json")]) == 0


def test_health_command_returns_failure_when_gateway_is_unreachable(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    path = tmp_path / "settings.json"
    write_settings(path)

    def fake_get(url: str, timeout: float) -> httpx.Response:
        request = httpx.Request("GET", url)
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    exit_code = cli.main(["health", "--config", str(path), "--state", str(tmp_path / "missing.json")])

    assert exit_code == 1
    assert "could not reach http://127.0.0.1:8178/health" in capsys.readouterr().err


def test_health_command_clears_stale_runtime_state_and_fails(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    state_path = tmp_path / "state.json"
    write_runtime_state(make_state(tmp_path, pid=12345), state_path)
    monkeypatch.setattr(cli, "is_runtime_state_stale", lambda state: True)

    def fake_get(url: str, timeout: float) -> httpx.Response:
        raise AssertionError("stale runtime state should not be probed")

    monkeypatch.setattr(cli.httpx, "get", fake_get)

    exit_code = cli.main(["health", "--state", str(state_path)])

    assert exit_code == 1
    assert not state_path.exists()
    assert "runtime state was stale for PID 12345" in capsys.readouterr().err


def test_start_command_writes_runtime_state_and_cleans_it_after_exit(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    write_settings(settings_path)

    class FakeSocket:
        def close(self) -> None:
            pass

    class FakeServer:
        def __init__(self, config) -> None:
            self.config = config

        def run(self, sockets) -> None:
            assert sockets == [fake_socket]
            state = read_runtime_state(state_path)
            assert state is not None
            assert state.pid == os.getpid()
            assert state.host == "127.0.0.1"
            assert state.port == 8178
            assert state.url == "http://127.0.0.1:8178"
            assert state.config_path == settings_path.resolve()
            assert state.config_fingerprint

    fake_socket = FakeSocket()
    monkeypatch.setattr(cli, "_create_server_socket", lambda host, port: fake_socket)
    monkeypatch.setattr(cli.uvicorn, "Config", lambda app, host, port: {"host": host, "port": port})
    monkeypatch.setattr(cli.uvicorn, "Server", FakeServer)

    exit_code = cli.main(["start", "--config", str(settings_path), "--state", str(state_path)])

    assert exit_code == 0
    assert not state_path.exists()
    assert f"Configuration path: {settings_path.resolve()}" in capsys.readouterr().out


def test_start_command_refuses_existing_active_runtime_state(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    settings_path = tmp_path / "settings.json"
    state_path = tmp_path / "state.json"
    write_settings(settings_path)
    write_runtime_state(make_state(tmp_path), state_path)
    monkeypatch.setattr(cli, "is_runtime_state_stale", lambda state: False)

    def fake_create_socket(host: str, port: int):
        raise AssertionError("start should not bind when runtime state is active")

    monkeypatch.setattr(cli, "_create_server_socket", fake_create_socket)

    exit_code = cli.main(["start", "--config", str(settings_path), "--state", str(state_path)])

    assert exit_code == 1
    assert "already running at http://127.0.0.1:9000" in capsys.readouterr().err
