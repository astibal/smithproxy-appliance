from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ConfigurationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClientConfig:
    url: str = "http://127.0.0.1:9080"
    ws_url: str = "ws://127.0.0.1:9081"
    token: str = ""
    ca_file: str = ""
    cert_file: str = ""
    key_file: str = ""
    insecure: bool = False
    timeout: float = 30.0
    context: str = "default"


def default_config_path() -> Path:
    override = os.environ.get("SASCTL_CONFIG")
    if override:
        return Path(override).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "sasctl" / "config.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"cannot read client config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("sasctl config must be a JSON object")
    return value


def _read_token(path_value: str) -> str:
    path = Path(path_value).expanduser()
    try:
        metadata = path.stat()
        if stat.S_ISREG(metadata.st_mode) and metadata.st_mode & 0o077:
            raise ConfigurationError(f"token file must not be accessible by group/others: {path}")
        token = path.read_text(encoding="utf-8").strip()
    except ConfigurationError:
        raise
    except OSError as exc:
        raise ConfigurationError(f"cannot read token file {path}: {exc}") from exc
    if token.startswith("CZ_RUNNER_TOKEN="):
        token = token.split("=", 1)[1].strip().strip("'\"")
    if len(token) < 32:
        raise ConfigurationError("runner token must contain at least 32 characters")
    return token


def _expand_path(value: Any) -> str:
    """Expand user-relative paths while preserving an unset value."""
    text = str(value or "")
    return str(Path(text).expanduser()) if text else ""


def load_config(*, path: Path | None = None, context: str = "", url: str = "",
                ws_url: str = "", token_file: str = "", insecure: bool | None = None,
                timeout: float | None = None, require_token: bool = True) -> ClientConfig:
    document = _read_json(path or default_config_path())
    selected = context or str(document.get("current", "default"))
    contexts = document.get("contexts", {})
    if contexts and not isinstance(contexts, dict):
        raise ConfigurationError("sasctl contexts must be a JSON object")
    raw = contexts.get(selected, {}) if isinstance(contexts, dict) else {}
    if raw and not isinstance(raw, dict):
        raise ConfigurationError(f"sasctl context {selected!r} must be an object")

    resolved_url = url or os.environ.get("SAS_RUNNER_URL") or str(raw.get("url", "http://127.0.0.1:9080"))
    resolved_ws = ws_url or os.environ.get("SAS_RUNNER_WS_URL") or str(raw.get("ws_url", ""))
    if not resolved_ws:
        scheme = "wss" if resolved_url.startswith("https://") else "ws"
        authority = resolved_url.split("://", 1)[-1].rstrip("/")
        host = authority.rsplit(":", 1)[0] if ":" in authority else authority
        resolved_ws = f"{scheme}://{host}:9081"
    if not resolved_url.startswith(("http://", "https://")):
        raise ConfigurationError("runner URL must use http:// or https://")
    if not resolved_ws.startswith(("ws://", "wss://")):
        raise ConfigurationError("runner WebSocket URL must use ws:// or wss://")

    token = os.environ.get("SAS_RUNNER_TOKEN") or os.environ.get("CZ_RUNNER_TOKEN", "")
    resolved_token_file = token_file or os.environ.get("SAS_RUNNER_TOKEN_FILE") or str(raw.get("token_file", ""))
    if resolved_token_file:
        token = _read_token(resolved_token_file)
    if require_token and len(token) < 32:
        raise ConfigurationError(
            "runner token is missing; use a protected token_file, SAS_RUNNER_TOKEN, or CZ_RUNNER_TOKEN"
        )

    return ClientConfig(
        url=resolved_url.rstrip("/"), ws_url=resolved_ws.rstrip("/"), token=token,
        ca_file=_expand_path(raw.get("ca_file", "")),
        cert_file=_expand_path(raw.get("cert_file", "")),
        key_file=_expand_path(raw.get("key_file", "")),
        insecure=bool(raw.get("insecure", False)) if insecure is None else insecure,
        timeout=float(raw.get("timeout", 30.0)) if timeout is None else timeout,
        context=selected,
    )
