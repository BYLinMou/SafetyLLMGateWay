from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

import uvicorn

from .app import create_app
from .config import ConfigError, initialize_settings_file, load_settings, resolve_config_path


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "start":
        return _start(args)

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
    uvicorn.run(create_app(config), host=host, port=port)
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
