from __future__ import annotations

import ctypes
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping


RUNTIME_STATE_ENV_VAR = "LSG_RUNTIME_STATE_PATH"
RUNTIME_STATE_VERSION = 1


@dataclass(frozen=True)
class RuntimeState:
    version: int
    pid: int
    host: str
    port: int
    url: str
    started_at: str
    config_path: Path
    config_fingerprint: str

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "pid": self.pid,
            "host": self.host,
            "port": self.port,
            "url": self.url,
            "startedAt": self.started_at,
            "configPath": str(self.config_path),
            "configFingerprint": self.config_fingerprint,
        }


class RuntimeStateError(Exception):
    pass


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def resolve_runtime_state_path(
    state_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    platform: str | None = None,
    home: Path | None = None,
) -> Path:
    if state_path is not None:
        return Path(state_path).expanduser()

    resolved_env = os.environ if env is None else env
    override = resolved_env.get(RUNTIME_STATE_ENV_VAR)
    if override:
        return Path(override).expanduser()

    resolved_platform = sys.platform if platform is None else platform
    resolved_home = Path.home() if home is None else home

    if resolved_platform == "darwin":
        return resolved_home / "Library" / "Caches" / "lsg" / "state.json"

    if resolved_platform.startswith("win"):
        local_app_data = resolved_env.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else resolved_home / "AppData" / "Local"
        return base / "lsg" / "runtime" / "state.json"

    runtime_dir = resolved_env.get("XDG_RUNTIME_DIR")
    if runtime_dir:
        return Path(runtime_dir) / "lsg" / "state.json"

    xdg_cache_home = resolved_env.get("XDG_CACHE_HOME")
    base = Path(xdg_cache_home) if xdg_cache_home else resolved_home / ".cache"
    return base / "lsg" / "state.json"


def write_runtime_state(
    state: RuntimeState,
    state_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> Path:
    path = resolve_runtime_state_path(state_path, env=env)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(state.to_json(), indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return path


def read_runtime_state(
    state_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> RuntimeState | None:
    path = resolve_runtime_state_path(state_path, env=env)
    if not path.exists():
        return None

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeStateError(f"Runtime state is not valid JSON: {exc.msg}.") from exc

    return parse_runtime_state(raw)


def parse_runtime_state(raw: Any) -> RuntimeState:
    if not isinstance(raw, dict):
        raise RuntimeStateError("Runtime state must be a JSON object.")

    version = raw.get("version")
    pid = raw.get("pid")
    host = raw.get("host")
    port = raw.get("port")
    url = raw.get("url")
    started_at = raw.get("startedAt")
    config_path = raw.get("configPath")
    config_fingerprint = raw.get("configFingerprint")

    if version != RUNTIME_STATE_VERSION:
        raise RuntimeStateError(f"Runtime state version must be {RUNTIME_STATE_VERSION}.")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid < 1:
        raise RuntimeStateError("Runtime state pid must be a positive integer.")
    if not isinstance(host, str) or not host:
        raise RuntimeStateError("Runtime state host must be a non-empty string.")
    if not isinstance(port, int) or isinstance(port, bool) or port < 1 or port > 65535:
        raise RuntimeStateError("Runtime state port must be an integer between 1 and 65535.")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise RuntimeStateError("Runtime state url must be an http or https URL.")
    if not isinstance(started_at, str) or not started_at:
        raise RuntimeStateError("Runtime state startedAt must be a non-empty string.")
    if not isinstance(config_path, str) or not config_path:
        raise RuntimeStateError("Runtime state configPath must be a non-empty string.")
    if not isinstance(config_fingerprint, str) or not config_fingerprint:
        raise RuntimeStateError("Runtime state configFingerprint must be a non-empty string.")

    return RuntimeState(
        version=version,
        pid=pid,
        host=host,
        port=port,
        url=url,
        started_at=started_at,
        config_path=Path(config_path),
        config_fingerprint=config_fingerprint,
    )


def clear_runtime_state(
    state_path: str | Path | None = None,
    *,
    env: Mapping[str, str] | None = None,
    expected_pid: int | None = None,
) -> bool:
    path = resolve_runtime_state_path(state_path, env=env)
    if not path.exists():
        return False

    if expected_pid is not None:
        current = read_runtime_state(path)
        if current is None or current.pid != expected_pid:
            return False

    path.unlink()
    return True


def is_runtime_state_stale(
    state: RuntimeState,
    *,
    process_checker: Callable[[int], bool] | None = None,
) -> bool:
    checker = is_process_running if process_checker is None else process_checker
    return not checker(state.pid)


def is_process_running(pid: int) -> bool:
    if pid < 1:
        return False

    if sys.platform.startswith("win"):
        return _is_windows_process_running(pid)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _is_windows_process_running(pid: int) -> bool:
    kernel32 = ctypes.windll.kernel32
    synchronize = 0x00100000
    wait_timeout = 0x00000102
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        return False

    try:
        return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
    finally:
        kernel32.CloseHandle(handle)
