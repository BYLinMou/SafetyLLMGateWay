from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Sequence

import httpx
import uvicorn

from .app import create_app
from .config import ConfigError, initialize_settings_file, load_settings, resolve_config_path
from .runtime_state import (
    RUNTIME_STATE_VERSION,
    RuntimeState,
    RuntimeStateError,
    clear_runtime_state,
    is_runtime_state_stale,
    read_runtime_state,
    resolve_runtime_state_path,
    utc_now_iso,
    write_runtime_state,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        return _start(args)

    if args.command == "health":
        return _health(args)

    if args.command == "config":
        if args.config_command == "init":
            return _config_init(args)
        if args.config_command == "path":
            return _config_path(args)

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lsg")
    subparsers = parser.add_subparsers(dest="command")

    start = subparsers.add_parser("start", help="Start the local gateway.")
    start.add_argument("--config", type=Path, help="Settings file path.")
    start.add_argument("--host", help="Override the configured server host.")
    start.add_argument("--port", type=int, help="Override the configured server port.")
    start.add_argument("--state", type=Path, help=argparse.SUPPRESS)

    health = subparsers.add_parser("health", help="Check the local gateway health endpoint.")
    health.add_argument("--config", type=Path, help="Settings file path.")
    health.add_argument("--host", help="Override the configured server host.")
    health.add_argument("--port", type=int, help="Override the configured server port.")
    health.add_argument("--state", type=Path, help=argparse.SUPPRESS)
    health.add_argument("--url", help="Gateway base URL to check instead of runtime state.")
    health.add_argument("--timeout", type=float, default=2.0, help="Request timeout in seconds.")

    config = subparsers.add_parser("config", help="Manage local gateway settings.")
    config_subparsers = config.add_subparsers(dest="config_command")

    config_init = config_subparsers.add_parser("init", help="Create a settings file template.")
    config_init.add_argument("--config", type=Path, help="Settings file path.")
    config_init.add_argument("--force", action="store_true", help="Overwrite an existing file.")

    config_path = config_subparsers.add_parser("path", help="Print the resolved settings path.")
    config_path.add_argument("--config", type=Path, help="Settings file path.")

    return parser


def _start(args: argparse.Namespace) -> int:
    try:
        config = load_settings(args.config)
    except ConfigError as exc:
        _print_config_error(exc)
        return 1

    host = args.host or config.server.host
    port = args.port or config.server.port
    if not _is_valid_port(port):
        print("server.port must be an integer between 1 and 65535.", file=sys.stderr)
        return 1

    active_state = _load_active_runtime_state(args.state)
    if active_state is not None:
        print(f"Managed gateway is already running at {active_state.url}.", file=sys.stderr)
        return 1

    try:
        sock = _create_server_socket(host, port)
    except OSError as exc:
        print(f"Could not bind gateway to {host}:{port}: {exc}", file=sys.stderr)
        return 1

    url = _build_local_http_url(_client_host(host), port)
    state = RuntimeState(
        version=RUNTIME_STATE_VERSION,
        pid=os.getpid(),
        host=host,
        port=port,
        url=url,
        started_at=utc_now_iso(),
        config_path=config.source_path.resolve(),
        config_fingerprint=config.fingerprint,
    )

    try:
        state_path = write_runtime_state(state, args.state)
    except OSError as exc:
        sock.close()
        print(f"Could not write runtime state: {exc}", file=sys.stderr)
        return 1

    print(f"Gateway running at {url}")
    print(f"Runtime state: {state_path}")

    server = uvicorn.Server(uvicorn.Config(create_app(config), host=host, port=port))
    try:
        server.run(sockets=[sock])
    finally:
        clear_runtime_state(args.state, expected_pid=state.pid)
    return 0


def _health(args: argparse.Namespace) -> int:
    url = args.url
    if url is None:
        try:
            state = read_runtime_state(args.state)
        except RuntimeStateError as exc:
            path = resolve_runtime_state_path(args.state)
            print(f"Health check failed: runtime state at {path} is invalid: {exc}", file=sys.stderr)
            return 1

        if state is not None:
            if is_runtime_state_stale(state):
                clear_runtime_state(args.state, expected_pid=state.pid)
                print(
                    f"Health check failed: runtime state was stale for PID {state.pid}.",
                    file=sys.stderr,
                )
                return 1
            url = state.url
        else:
            try:
                config = load_settings(args.config, require_usable_upstream=False)
            except ConfigError as exc:
                _print_config_error(exc)
                return 1

            host = _client_host(args.host or config.server.host)
            port = args.port or config.server.port
            url = _build_local_http_url(host, port)

    health_url = _with_health_path(url)
    try:
        response = httpx.get(health_url, timeout=args.timeout)
    except httpx.RequestError as exc:
        print(f"Health check failed: could not reach {health_url}: {exc}", file=sys.stderr)
        return 1

    if response.status_code != 200:
        print(
            f"Health check failed: {health_url} returned HTTP {response.status_code}.",
            file=sys.stderr,
        )
        if response.text:
            print(response.text, file=sys.stderr)
        return 1

    try:
        payload = response.json()
    except ValueError:
        print(response.text)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))

    return 0


def _config_init(args: argparse.Namespace) -> int:
    path = initialize_settings_file(args.config, force=args.force)
    print(f"Settings file ready at {path}")
    return 0


def _config_path(args: argparse.Namespace) -> int:
    print(resolve_config_path(args.config))
    return 0


def _print_config_error(error: ConfigError) -> None:
    print("Configuration error:", file=sys.stderr)
    for diagnostic in error.diagnostics:
        print(f"- {diagnostic.path}: {diagnostic.message} [{diagnostic.code}]", file=sys.stderr)


def _load_active_runtime_state(state_path: Path | None) -> RuntimeState | None:
    try:
        state = read_runtime_state(state_path)
    except RuntimeStateError as exc:
        path = resolve_runtime_state_path(state_path)
        print(f"Ignoring invalid runtime state at {path}: {exc}", file=sys.stderr)
        return None

    if state is None:
        return None

    if is_runtime_state_stale(state):
        clear_runtime_state(state_path, expected_pid=state.pid)
        return None

    return state


def _create_server_socket(host: str, port: int) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family=family)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.listen(2048)
    except OSError:
        sock.close()
        raise
    return sock


def _client_host(host: str) -> str:
    if host == "0.0.0.0":
        return "127.0.0.1"
    if host == "::":
        return "::1"
    return host


def _build_local_http_url(host: str, port: int, path: str = "") -> str:
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{port}{path}"


def _with_health_path(url: str) -> str:
    return f"{url.rstrip('/')}/health"


def _is_valid_port(port: int) -> bool:
    return isinstance(port, int) and not isinstance(port, bool) and 1 <= port <= 65535
