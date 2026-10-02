from __future__ import annotations

import hmac
import base64
import gzip
import ipaddress
import difflib
import hashlib
import json
import os
import re
import shutil
import signal
import threading
import time
import uuid
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from websockets.sync.server import ServerConnection, serve

from .config import ConfigError, PARAMETERS, TOKEN_RE, render_template, validate_parameters
from .systemd import BackendError
from .namespace import NamespaceBackend
from .builder import SmithproxyBuilder
from .config_library import ConfigLibrary
from .runtime_profiles import RuntimeProfileLibrary
from .cert_library import CertBundleLibrary
from .config_previews import ConfigPreviewLibrary
from .task_queue import TaskQueue
from .network_settings import NetworkSettings
from .test_drive import TestDriveManager
from .instance_layout import ensure_type_link, prepare_layout, remove_type_link


@dataclass
class Instance:
    id: str
    unit: str
    state: str
    created_at: str
    deadline: str
    runtime_seconds: int
    result: str = ""
    source_ip: str = ""
    namespace: str = ""
    pid: int = 0
    rss_bytes: int = 0
    cli_port: int = 0
    resources_cleaned: bool = False
    build_id: str = "active"
    config_id: str = "active"
    user_id: str = ""
    rewrite_sni: str = ""
    rewrite_sni_to: str = ""
    profile: str = "custom"
    template_values: dict[str, str] = field(default_factory=dict)
    config_mode: str = "ro"
    runtime_profile_id: str = ""
    cert_bundle_id: str = ""
    debug_unit: str = ""
    debug_address: str = ""
    debug_port: int = 0
    auto_restart: bool = False
    restart_count: int = 0
    last_restart_at: str = ""
    crash_pid: int = 0
    crash_at: str = ""
    crash_trace: str = ""
    stopped_at: str = ""
    persistent: bool = False


@dataclass
class CliSession:
    instance_id: str
    connection: Any
    lock: threading.Lock
    last_used: float


class Manager:
    def __init__(self, state_dir: Path, runtime_root: Path, template: Path, backend: Any,
                 min_runtime: int = 5, max_runtime: int = 3600, max_instances: int = 32,
                 sources_path: Path | None = None, max_total_runtime: int = 86400,
                 stopped_retention_seconds: int = 3 * 3600,
                 config_archive_dir: Path | None = None) -> None:
        self.state_dir = state_dir
        self.runtime_root = runtime_root
        self.template = template
        self.backend = backend
        self.min_runtime = min_runtime
        self.max_runtime = max_runtime
        self.max_instances = max_instances
        self.max_total_runtime = max_total_runtime
        self.sources_path = sources_path
        if stopped_retention_seconds < 0:
            raise ValueError("stopped instance retention must not be negative")
        self.stopped_retention_seconds = stopped_retention_seconds
        self.config_archive_dir = config_archive_dir or state_dir.parent / "instance-config-archive"
        self.lock = threading.RLock()
        self.cli_sessions: dict[str, CliSession] = {}
        self.gdb_sessions: dict[str, Any] = {}
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_index = prepare_layout(runtime_root, "managed")
        self.config_archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _remove_runtime(self, instance_id: str) -> None:
        shutil.rmtree(self.runtime_root / instance_id, ignore_errors=True)
        remove_type_link(self.runtime_root, "managed", instance_id)

    def _state_path(self, instance_id: str) -> Path:
        return self.state_dir / f"{instance_id}.json"

    def _config_path(self, instance_id: str) -> Path:
        return self.state_dir / f"{instance_id}.cfg"

    def _save(self, instance: Instance) -> None:
        target = self._state_path(instance.id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(instance), separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    def _snapshot_runtime_config(self, instance_id: str) -> None:
        live = self.runtime_root / instance_id / "smithproxy.cfg"
        try:
            content = live.read_bytes()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise BackendError(f"cannot preserve instance configuration: {exc}") from exc
        if not content or len(content) > 1024 * 1024 or b"\0" in content:
            raise BackendError("instance configuration cannot be preserved safely")
        target = self._config_path(instance_id)
        temporary = target.with_suffix(".cfg.tmp")
        temporary.write_bytes(content)
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    @staticmethod
    def _clear_debug(instance: Instance) -> None:
        instance.debug_unit = ""
        instance.debug_address = ""
        instance.debug_port = 0

    def _load(self, instance_id: str) -> Instance | None:
        if not uuid_is_valid(instance_id):
            return None
        try:
            return Instance(**json.loads(self._state_path(instance_id).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None

    def _reconcile(self, instance: Instance) -> Instance:
        status = self.backend.status(instance.unit)
        deadline = datetime.fromisoformat(instance.deadline) if instance.deadline else None
        if (status.active_state in {"active", "activating", "reloading"}
                and deadline and datetime.now(timezone.utc) >= deadline):
            for session_id, session in list(self.cli_sessions.items()):
                if session.instance_id == instance.id:
                    session.connection.close()
                    del self.cli_sessions[session_id]
            self.backend.stop(instance.unit)
            self._clear_debug(instance)
            self._snapshot_runtime_config(instance.id)
            self._remove_runtime(instance.id)
            instance.state = "expired"
            instance.stopped_at = datetime.now(timezone.utc).isoformat()
            instance.pid = 0
            instance.rss_bytes = 0
            instance.result = "ttl-expired"
            instance.resources_cleaned = True
            self._save(instance)
            return instance
        if status.active_state in {"active", "activating", "reloading"}:
            instance.state = "orphaned" if instance.state == "orphaned" else "running"
            instance.stopped_at = ""
            instance.pid = status.main_pid
            instance.rss_bytes = self.backend.rss_bytes(status.main_pid) if status.main_pid else 0
            instance.resources_cleaned = False
        else:
            previous_pid = instance.pid
            instance.pid = 0
            instance.rss_bytes = 0
            failed = status.result not in {"success", ""}
            if failed and previous_pid > 1 and instance.crash_pid != previous_pid:
                instance.crash_pid = previous_pid
                instance.crash_at = datetime.now(timezone.utc).isoformat()
                try:
                    instance.crash_trace = self.backend.capture_stacktrace(
                        previous_pid, instance.created_at
                    )
                except (AttributeError, BackendError) as exc:
                    instance.crash_trace = f"automatic stack trace unavailable: {exc}"
            if (failed and instance.auto_restart and instance.restart_count < 5
                    and instance.state not in {"stopped", "expired"}
                    and (not deadline or datetime.now(timezone.utc) < deadline)):
                try:
                    self.backend.restart(instance.unit)
                    instance.restart_count += 1
                    instance.last_restart_at = datetime.now(timezone.utc).isoformat()
                    instance.state = "starting"
                    instance.result = f"auto-restart-{instance.restart_count}"
                    self._clear_debug(instance)
                    self._save(instance)
                    return instance
                except BackendError as exc:
                    instance.result = f"auto-restart failed: {exc}"
            if instance.state not in {"stopped", "failed", "expired"}:
                instance.state = "expired" if deadline and datetime.now(timezone.utc) >= deadline else (
                    "stopped" if status.result in {"success", ""} else "failed"
                )
            if instance.state in {"stopped", "failed", "expired"} and not instance.stopped_at:
                instance.stopped_at = datetime.now(timezone.utc).isoformat()
            instance.result = status.result
            if not instance.resources_cleaned:
                try:
                    self.backend.stop(instance.unit)
                except BackendError:
                    pass
                self._clear_debug(instance)
                self._snapshot_runtime_config(instance.id)
                self._remove_runtime(instance.id)
                instance.resources_cleaned = True
        self._save(instance)
        return instance

    def list(self) -> list[Instance]:
        with self.lock:
            result = []
            for path in sorted(self.state_dir.glob("*.json")):
                item = self._load(path.stem)
                if item:
                    result.append(self._reconcile(item))
            return result

    def snapshot(self) -> list[Instance]:
        """Return the last reconciled read model without touching systemd.

        The reaper refreshes these atomic JSON records every two seconds.  HTTP
        polling must not wait behind a slow spawn/native-save operation which
        holds the manager lock, nor trigger an O(instances) systemd scan itself.
        """
        result = []
        for path in sorted(self.state_dir.glob("*.json")):
            item = self._load(path.stem)
            if item:
                result.append(item)
        return result

    def peek(self, instance_id: str) -> Instance | None:
        """Read one last-reconciled instance without serializing on mutations."""
        return self._load(instance_id)

    def reconcile_orphans(self) -> list[str]:
        """Adopt unrecorded portal-owned units as visible orphaned instances."""
        with self.lock:
            known = {
                path.stem for path in self.state_dir.glob("*.json")
                if uuid_is_valid(path.stem)
            }
            removed = []
            for instance_id, unit in self.backend.discover_units():
                if instance_id in known:
                    continue
                status = self.backend.status(unit)
                if status.active_state not in {"active", "activating", "reloading"}:
                    continue
                allocation = getattr(self.backend, "allocation", lambda _id: None)(instance_id)
                instance = Instance(
                    instance_id, unit, "orphaned", datetime.now(timezone.utc).isoformat(),
                    "", 0, result=status.result,
                    namespace=getattr(allocation, "namespace", ""), pid=status.main_pid,
                    rss_bytes=self.backend.rss_bytes(status.main_pid) if status.main_pid else 0,
                    resources_cleaned=False, build_id="unknown", config_id="unknown",
                    persistent=True,
                )
                print(f"orphan instance discovered; tracking without stopping {unit}")
                self._save(instance)
                removed.append(instance_id)
            return removed

    def sources(self) -> list[str]:
        if self.sources_path is None:
            return []
        try:
            value = json.loads(self.sources_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"cannot read source IP pool: {exc}") from exc
        if not isinstance(value, list):
            raise ConfigError("source IP pool must be a JSON array")
        result = []
        for item in value:
            try:
                normalized = str(__import__("ipaddress").ip_address(str(item)))
            except ValueError as exc:
                raise ConfigError(f"invalid source IP in pool: {item}") from exc
            if normalized not in result:
                result.append(normalized)
        return result

    def get(self, instance_id: str) -> Instance | None:
        with self.lock:
            item = self._load(instance_id)
            return self._reconcile(item) if item else None

    def create(self, payload: object, binary_path: Path | None = None,
               template_path: Path | None = None, assets_dir: Path | None = None,
               cert_bundle_dir: Path | None = None) -> Instance:
        if not isinstance(payload, dict):
            raise ConfigError("request body must be an object")
        allowed = {
            "runtime_seconds", "parameters", "source_ip", "build_id", "config_id", "user_id",
            "rewrite_sni", "rewrite_sni_to", "profile", "template_values", "config_mode",
            "runtime_profile_id",
            "cert_bundle_id",
            "auto_restart",
            "persistent",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ConfigError(f"unsupported fields: {', '.join(sorted(unknown))}")
        runtime_profile_id = str(payload.get("runtime_profile_id", ""))
        runtime = payload.get("runtime_seconds")
        if isinstance(runtime, bool) or not isinstance(runtime, int):
            raise ConfigError("runtime_seconds must be an integer")
        runtime_max = self.max_total_runtime if runtime_profile_id else self.max_runtime
        if runtime == 0 and not runtime_profile_id:
            raise ConfigError("unlimited runtime requires a runtime profile")
        if runtime != 0 and not self.min_runtime <= runtime <= runtime_max:
            raise ConfigError(f"runtime_seconds must be between {self.min_runtime} and {runtime_max}")
        parameters = validate_parameters(payload.get("parameters", {}))
        source_ip = str(payload.get("source_ip", "")).strip()
        build_id = str(payload.get("build_id", "active"))
        config_id = str(payload.get("config_id", "active"))
        if runtime_profile_id and not uuid_is_valid(runtime_profile_id):
            raise ConfigError("runtime_profile_id must be a UUID")
        cert_bundle_id = str(payload.get("cert_bundle_id", ""))
        if cert_bundle_id and not uuid_is_valid(cert_bundle_id):
            raise ConfigError("cert_bundle_id must be a UUID")
        auto_restart = payload.get("auto_restart", False)
        if not isinstance(auto_restart, bool):
            raise ConfigError("auto_restart must be a boolean")
        persistent = payload.get("persistent", False)
        if not isinstance(persistent, bool):
            raise ConfigError("persistent must be a boolean")
        user_id = str(payload.get("user_id", "")).strip()
        if not re.fullmatch(r"[A-Za-z0-9@._:+-]{1,128}", user_id):
            raise ConfigError("user_id must contain 1 to 128 safe identifier characters")
        rewrite_sni = str(payload.get("rewrite_sni", "")).strip().lower()
        rewrite_sni_to = str(payload.get("rewrite_sni_to", "")).strip().lower()
        raw_template_values = payload.get("template_values", {})
        if not isinstance(raw_template_values, dict):
            raise ConfigError("template_values must be an object")
        template_values: dict[str, str] = {}
        for raw_key, raw_value in raw_template_values.items():
            key = str(raw_key).upper()
            value = str(raw_value).strip()
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", key):
                raise ConfigError("invalid template placeholder name")
            if len(value) > 512 or not re.fullmatch(r"[A-Za-z0-9._:@/+,-]+", value):
                raise ConfigError(f"unsafe value for template placeholder {key}")
            template_values[key] = value
        if rewrite_sni:
            template_values.setdefault("REWRITE_SNI", rewrite_sni)
        if rewrite_sni_to:
            template_values.setdefault("REWRITE_SNI_TO", rewrite_sni_to)
        managed_placeholders = {key.upper() for key in PARAMETERS} | {"RUNTIME_DIR"}
        requested_placeholders = set(TOKEN_RE.findall(
            (template_path or self.template).read_text(encoding="utf-8")
        )) - managed_placeholders
        unknown_values = set(template_values) - requested_placeholders
        if unknown_values:
            raise ConfigError(f"unused template values: {', '.join(sorted(unknown_values))}")
        missing_values = requested_placeholders - set(template_values)
        if missing_values:
            raise ConfigError(f"missing template values: {', '.join(sorted(missing_values))}")
        rewrite_sni = template_values.get("REWRITE_SNI", "").lower()
        rewrite_sni_to = template_values.get("REWRITE_SNI_TO", "").lower()
        if rewrite_sni:
            template_values["REWRITE_SNI"] = rewrite_sni
        if rewrite_sni_to:
            template_values["REWRITE_SNI_TO"] = rewrite_sni_to
        profile = str(payload.get("profile", "custom"))
        config_mode = str(payload.get("config_mode", "ro"))
        if config_mode not in {"ro", "rw"}:
            raise ConfigError("config_mode must be ro or rw")
        if profile not in {"custom", "magic-sni", "socks", "http-proxy"}:
            raise ConfigError("invalid configuration profile")
        if profile == "magic-sni" and (not rewrite_sni or not rewrite_sni_to):
            raise ConfigError("Magic SNI profile requires rewrite_sni and rewrite_sni_to")
        if bool(rewrite_sni) != bool(rewrite_sni_to):
            raise ConfigError("rewrite_sni and rewrite_sni_to must be provided together")
        for label, hostname in (("rewrite_sni", rewrite_sni), ("rewrite_sni_to", rewrite_sni_to)):
            if hostname and (len(hostname) > 253 or not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", hostname
            )):
                raise ConfigError(f"{label} must be a valid hostname")
        if not source_ip:
            raise ConfigError("source_ip is required")
        pool = self.sources()
        if pool and source_ip not in pool:
            raise ConfigError("source_ip is not present in the configured pool")
        with self.lock:
            active = sum(i.state in {"starting", "running", "orphaned"} for i in self.list())
            if active >= self.max_instances:
                raise ConfigError("instance limit reached")
            if any(i.source_ip == source_ip and i.state in {"starting", "running", "orphaned"} for i in self.list()):
                raise ConfigError("source_ip already has an active instance")
            instance_id = str(uuid.uuid4())
            runtime_dir = self.runtime_root / instance_id
            runtime_dir.mkdir(mode=0o700)
            ensure_type_link(self.runtime_root, "managed", instance_id, runtime_dir)
            config_path = runtime_dir / "smithproxy.cfg"
            try:
                # Build/config assets are immutable library inputs. Smithproxy
                # creates per-runtime certificate caches below certs/default,
                # so every instance needs its own writable copy. Never let one
                # process write into an archived build or another instance.
                effective_assets = None
                if assets_dir:
                    effective_assets = runtime_dir / "smithproxy.assets"
                    shutil.copytree(assets_dir, effective_assets)
                ca_key_password = None
                if cert_bundle_dir:
                    if effective_assets is None:
                        effective_assets = runtime_dir / "smithproxy.assets"
                        effective_assets.mkdir(mode=0o700)
                    cert_target = effective_assets / "certs" / "default"
                    cert_target.mkdir(parents=True, exist_ok=True, mode=0o700)
                    shutil.copytree(cert_bundle_dir, cert_target, dirs_exist_ok=True)
                    for key_path in cert_target.glob("*-key.pem"):
                        os.chmod(key_path, 0o600)
                    ca_key_password = ""
                if effective_assets:
                    cert_cache = effective_assets / "certs" / "default"
                    cert_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
                    for cache_name in ("sni", "ip", "cc-sni", "cc-ip"):
                        cache_dir = cert_cache / cache_name
                        cache_dir.mkdir(mode=0o700, exist_ok=True)
                        os.chmod(cache_dir, 0o700)
                rendered_config = render_template(
                    template_path or self.template, parameters, runtime_dir,
                    assets_dir=effective_assets, ca_key_password=ca_key_password,
                    text_parameters={
                        key.lower(): value for key, value in template_values.items()
                    },
                )
                config_path.write_text(rendered_config, encoding="utf-8")
                # Keep it writable while canonical native-save is applied;
                # read-only mode is enforced only after the final content is
                # installed and again by the transient systemd mount policy.
                os.chmod(config_path, 0o600)
                if binary_path and hasattr(self.backend, "native_save"):
                    # Validate the exact post-overlay runtime config before
                    # allocating the persistent namespace/routing. Validate a
                    # copy because Smithproxy's `save config` mutates its file.
                    # Its output is the canonical runtime config: discarding it
                    # would make a read-only instance start from a source config
                    # without Smithproxy's *_internal_* version/schema section,
                    # then fail its automatic startup save with FileIOException.
                    preflight_path = runtime_dir / ".preflight.cfg"
                    preflight_path.write_text(rendered_config, encoding="utf-8")
                    os.chmod(preflight_path, 0o600)
                    try:
                        rendered_config = self.backend.native_save(
                            binary_path, preflight_path,
                            parameters.get("cli_port", 50000),
                        )
                    finally:
                        preflight_path.unlink(missing_ok=True)
                    if not rendered_config or "\0" in rendered_config:
                        raise BackendError("native-save returned an invalid runtime configuration")
                    config_path.write_text(rendered_config, encoding="utf-8")
                os.chmod(config_path, 0o600 if config_mode == "rw" else 0o400)
                snapshot_path = self._config_path(instance_id)
                snapshot_path.write_text(rendered_config, encoding="utf-8")
                os.chmod(snapshot_path, 0o600)
                now = datetime.now(timezone.utc)
                unit = self.backend.start(
                    instance_id, config_path, runtime, source_ip=source_ip,
                    tls_port=parameters.get("tls_port", 50443),
                    plaintext_port=parameters.get("plaintext_port", 50080),
                    socks_port=parameters.get("socks_port", 1080),
                    cli_port=parameters.get("cli_port", 50000),
                    http_port=parameters.get("http_port", 3128),
                    smithproxy_binary=str(binary_path) if binary_path else "",
                    profile=profile,
                    config_mode=config_mode,
                    assets_path=str(effective_assets) if effective_assets else "",
                    auto_restart=auto_restart,
                    hard_runtime_seconds=0 if runtime == 0 else self.max_total_runtime,
                )
            except Exception:
                shutil.rmtree(runtime_dir, ignore_errors=True)
                remove_type_link(self.runtime_root, "managed", instance_id)
                self._config_path(instance_id).unlink(missing_ok=True)
                raise
            allocation = getattr(self.backend, "allocation", lambda _id: None)(instance_id)
            instance = Instance(
                instance_id, unit, "starting", now.isoformat(),
                (now + timedelta(seconds=runtime)).isoformat() if runtime else "", runtime,
                source_ip=source_ip,
                namespace=getattr(allocation, "namespace", ""),
                cli_port=parameters.get("cli_port", 50000),
                build_id=build_id,
                config_id=config_id,
                user_id=user_id,
                rewrite_sni=rewrite_sni,
                rewrite_sni_to=rewrite_sni_to,
                profile=profile,
                template_values=template_values,
                config_mode=config_mode,
                runtime_profile_id=runtime_profile_id,
                cert_bundle_id=cert_bundle_id,
                auto_restart=auto_restart,
                persistent=persistent,
            )
            self._save(instance)
            return instance

    @staticmethod
    def _cli_read(connection: Any) -> tuple[bytes, bool]:
        chunks = []
        closed = False
        while sum(map(len, chunks)) < 256 * 1024:
            try:
                value = connection.recv(65536)
            except TimeoutError:
                break
            if not value:
                closed = True
                break
            chunks.append(value)
            # Once the first byte arrives, drain the current terminal burst
            # without adding the full first-byte timeout after every echo byte.
            connection.settimeout(0.005)
        # libcli starts with three-byte TELNET WILL/DO negotiations.  The HTTP
        # bridge is line-oriented, so those transport bytes are not terminal
        # output and must not reach xterm.
        output = re.sub(rb"\xff[\xfb-\xfe].", b"", b"".join(chunks))
        return output, closed

    def expire_cli_sessions(self, max_idle_seconds: float = 300) -> None:
        cutoff = time.monotonic() - max_idle_seconds
        with self.lock:
            for session_id, session in list(self.cli_sessions.items()):
                if session.last_used < cutoff:
                    session.connection.close()
                    del self.cli_sessions[session_id]

    def close_cli(self, instance_id: str, session_id: str) -> None:
        if not isinstance(session_id, str) or not uuid_is_valid(session_id):
            raise ConfigError("session_id must be a UUID")
        with self.lock:
            session = self.cli_sessions.get(session_id)
            if session and session.instance_id != instance_id:
                raise ConfigError("CLI session belongs to another instance")
            if session:
                session.connection.close()
                del self.cli_sessions[session_id]

    def cli(self, instance_id: str, input_data: str, session_id: str) -> dict[str, str]:
        if not isinstance(input_data, str) or len(input_data.encode("utf-8")) > 4096:
            raise ConfigError("input must contain at most 4096 UTF-8 bytes")
        if not isinstance(session_id, str) or not uuid_is_valid(session_id):
            raise ConfigError("session_id must be a UUID")
        instance = self.get(instance_id)
        if not instance or instance.state != "running":
            raise ConfigError("instance is not running")
        try:
            with self.lock:
                self.expire_cli_sessions()
                session = self.cli_sessions.get(session_id)
                if session and session.instance_id != instance_id:
                    raise ConfigError("CLI session belongs to another instance")
                if session is None:
                    connection = self.backend.open_cli(instance.id, instance.cli_port)
                    connection.settimeout(0.25)
                    session = CliSession(instance_id, connection, threading.Lock(), time.monotonic())
                    self.cli_sessions[session_id] = session
            with session.lock:
                session.connection.settimeout(0.25 if not input_data else 0.01)
                output, closed = self._cli_read(session.connection)
                if closed:
                    raise OSError("Smithproxy closed the CLI session")
                if input_data:
                    session.connection.sendall(input_data.encode("utf-8"))
                    session.connection.settimeout(0.25)
                    response, closed = self._cli_read(session.connection)
                    output += response
                    if closed:
                        raise OSError("Smithproxy closed the CLI session")
                session.last_used = time.monotonic()
                session.connection.settimeout(0.25)
            return {"output": output.decode("utf-8", "replace"), "session_id": session_id}
        except OSError as exc:
            with self.lock:
                failed = self.cli_sessions.pop(session_id, None)
            if failed:
                failed.connection.close()
            raise BackendError(f"CLI connection failed: {exc}") from exc

    def logs(self, instance_id: str, lines: int = 200) -> str:
        instance = self.get(instance_id)
        if not instance:
            raise ConfigError("instance not found")
        if isinstance(lines, bool) or not 1 <= lines <= 2000:
            raise ConfigError("lines must be between 1 and 2000")
        return self.backend.logs(instance.unit, lines)

    def config_content(self, instance_id: str) -> tuple[Instance, str]:
        instance = self.get(instance_id)
        if not instance:
            raise ConfigError("instance not found")
        paths = [self.runtime_root / instance_id / "smithproxy.cfg", self._config_path(instance_id)]
        for path in paths:
            try:
                return instance, path.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise ConfigError(f"cannot read instance configuration: {exc}") from exc
        raise ConfigError("configuration snapshot is unavailable for this legacy instance")

    def save_live_config(self, instance_id: str) -> tuple[Instance, str, str]:
        """Execute Smithproxy's own save command and return before/after text."""
        with self.lock:
            instance = self.get(instance_id)
            if not instance or instance.state != "running":
                raise ConfigError("native instance import requires a running instance")
            path = self.runtime_root / instance_id / "smithproxy.cfg"
            try:
                before = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read live instance config: {exc}") from exc
            connection = self.backend.open_cli(instance.id, instance.cli_port)
            try:
                connection.settimeout(0.5)
                self._cli_read(connection)
                connection.sendall(b"enable\r\nsave config\r\n")
                output = bytearray()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    connection.settimeout(0.5)
                    chunk, closed = self._cli_read(connection)
                    output.extend(chunk)
                    lowered = bytes(output).lower()
                    if b"config saved successfully" in lowered:
                        break
                    if closed or b"error writing config" in lowered:
                        raise BackendError("Smithproxy failed to save the running config")
                else:
                    raise BackendError("Smithproxy live save config timed out")
            finally:
                connection.close()
            try:
                after = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read saved native config: {exc}") from exc
            self._snapshot_runtime_config(instance.id)
            return instance, before, after

    def open_cli_transport(self, instance_id: str):
        instance = self.get(instance_id)
        if not instance or instance.state != "running":
            raise ConfigError("instance is not running")
        return self.backend.open_cli(instance.id, instance.cli_port)

    def open_gdb_transport(self, instance_id: str, binary: Path):
        with self.lock:
            instance = self.get(instance_id)
            if not instance or instance.state != "running" or not instance.pid:
                raise ConfigError("instance is not running")
            previous = self.gdb_sessions.pop(instance.id, None)
            if previous:
                previous.close()
            unit, address, port = self.backend.start_debug(instance.id, instance.pid)
            instance.debug_unit = unit
            instance.debug_address = address
            instance.debug_port = port
            self._save(instance)
            try:
                transport = self.backend.open_gdb(instance.id, binary, address, port)
            except Exception:
                self.backend.stop_debug(instance.id)
                self._clear_debug(instance)
                self._save(instance)
                raise
            self.gdb_sessions[instance.id] = transport
            return transport

    def close_gdb_transport(self, instance_id: str, transport: Any) -> None:
        with self.lock:
            current = self.gdb_sessions.get(instance_id)
            transport.close()
            if current is not transport:
                return
            del self.gdb_sessions[instance_id]
            self.backend.stop_debug(instance_id)
            instance = self._load(instance_id)
            if instance:
                self._clear_debug(instance)
                self._save(instance)

    def stop(self, instance_id: str) -> Instance | None:
        with self.lock:
            for session_id, session in list(self.cli_sessions.items()):
                if session.instance_id == instance_id:
                    session.connection.close()
                    del self.cli_sessions[session_id]
            gdb = self.gdb_sessions.pop(instance_id, None)
            if gdb:
                gdb.close()
            instance = self._load(instance_id)
            if not instance:
                return None
            if instance.state not in {"stopped", "failed", "expired"}:
                self.backend.stop(instance.unit)
                self._clear_debug(instance)
                self._snapshot_runtime_config(instance.id)
                instance.state = "stopped"
                instance.stopped_at = datetime.now(timezone.utc).isoformat()
                instance.pid = 0
                instance.rss_bytes = 0
                instance.resources_cleaned = True
                self._save(instance)
                self._remove_runtime(instance.id)
            return instance

    def restart(self, instance_id: str) -> Instance | None:
        """Restart Smithproxy inside the existing unit/netns without rebuilding routing."""
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                return None
            instance = self._reconcile(instance)
            if instance.state != "running":
                raise ConfigError("only a running instance can restart in the same container")
            for session_id, session in list(self.cli_sessions.items()):
                if session.instance_id == instance_id:
                    session.connection.close()
                    del self.cli_sessions[session_id]
            gdb = self.gdb_sessions.pop(instance_id, None)
            if gdb:
                gdb.close()
            if instance.debug_unit:
                self.backend.stop_debug(instance.id)
                instance.debug_unit = ""
                instance.debug_address = ""
                instance.debug_port = 0
            self.backend.restart(instance.unit)
            now = datetime.now(timezone.utc)
            instance.state = "starting"
            instance.deadline = (
                (now + timedelta(seconds=instance.runtime_seconds)).isoformat()
                if instance.runtime_seconds else ""
            )
            instance.pid = 0
            instance.rss_bytes = 0
            instance.result = "restarted"
            instance.resources_cleaned = False
            self._save(instance)
            return instance

    def extend(self, instance_id: str, additional_seconds: int) -> Instance:
        if isinstance(additional_seconds, bool) or not isinstance(additional_seconds, int):
            raise ConfigError("additional_seconds must be an integer")
        if not 1 <= additional_seconds <= self.max_runtime:
            raise ConfigError(
                f"additional_seconds must be between 1 and {self.max_runtime}"
            )
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                raise ConfigError("instance not found")
            instance = self._reconcile(instance)
            if instance.state not in {"starting", "running", "orphaned"}:
                raise ConfigError("only an active instance can be extended")
            if not instance.deadline:
                raise ConfigError("an unlimited instance does not need a TTL extension")
            now = datetime.now(timezone.utc)
            created = datetime.fromisoformat(instance.created_at)
            current = datetime.fromisoformat(instance.deadline) if instance.deadline else now
            deadline = max(now, current) + timedelta(seconds=additional_seconds)
            total_seconds = int((deadline - created).total_seconds())
            if total_seconds > self.max_total_runtime:
                raise ConfigError(
                    f"total instance runtime cannot exceed {self.max_total_runtime} seconds"
                )
            self.backend.extend_runtime(instance.unit, total_seconds)
            instance.deadline = deadline.isoformat()
            instance.runtime_seconds = total_seconds
            instance.result = f"ttl-extended-by-{additional_seconds}s"
            self._save(instance)
            return instance

    def start_debug(self, instance_id: str) -> Instance | None:
        with self.lock:
            instance = self.get(instance_id)
            if not instance:
                return None
            if instance.state != "running" or not instance.pid:
                raise ConfigError("debug attach requires a running instance with a PID")
            unit, address, port = self.backend.start_debug(instance.id, instance.pid)
            instance.debug_unit = unit
            instance.debug_address = address
            instance.debug_port = port
            self._save(instance)
            return instance

    def stop_debug(self, instance_id: str) -> Instance | None:
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                return None
            gdb = self.gdb_sessions.pop(instance_id, None)
            if gdb:
                gdb.close()
            self.backend.stop_debug(instance.id)
            instance.debug_unit = ""
            instance.debug_address = ""
            instance.debug_port = 0
            self._save(instance)
            return instance

    def delete(self, instance_id: str) -> Instance | None:
        """Delete only a revalidated, fully stopped instance record."""
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                return None
            instance = self._reconcile(instance)
            if instance.state in {"starting", "running", "orphaned"} or instance.pid:
                raise ConfigError("running instance cannot be deleted")
            if not instance.resources_cleaned:
                raise ConfigError("instance resources are not cleaned up")
            for session_id, session in list(self.cli_sessions.items()):
                if session.instance_id == instance_id:
                    session.connection.close()
                    del self.cli_sessions[session_id]
            gdb = self.gdb_sessions.pop(instance_id, None)
            if gdb:
                gdb.close()
            self._remove_runtime(instance.id)
            try:
                self._state_path(instance.id).unlink()
            except FileNotFoundError:
                return None
            self._config_path(instance.id).unlink(missing_ok=True)
            return instance

    def _archive_stopped_config(self, instance: Instance) -> Path:
        snapshot = self._config_path(instance.id)
        try:
            content = snapshot.read_bytes()
        except OSError as exc:
            raise BackendError(f"cannot archive stopped instance configuration: {exc}") from exc
        if not content or len(content) > 1024 * 1024 or b"\0" in content:
            raise BackendError("stopped instance configuration cannot be archived safely")
        try:
            stopped = datetime.fromisoformat(instance.stopped_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise BackendError("stopped instance timestamp is invalid") from exc
        if stopped.tzinfo is None:
            stopped = stopped.replace(tzinfo=timezone.utc)
        stamp = stopped.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
        target = self.config_archive_dir / f"{stamp}_{instance.id}.cfg.gz"
        if target.is_file():
            return target
        temporary = target.with_suffix(".gz.tmp")
        try:
            with temporary.open("wb") as output:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
                    compressed.write(content)
            os.chmod(temporary, 0o600)
            temporary.replace(target)
        except Exception as exc:
            temporary.unlink(missing_ok=True)
            raise BackendError(f"cannot archive stopped instance configuration: {exc}") from exc
        return target

    def cleanup_stopped(self, now: datetime | None = None) -> list[dict[str, str]]:
        """Archive and remove fully stopped records after the retention period."""
        current = now or datetime.now(timezone.utc)
        cleaned = []
        with self.lock:
            for path in sorted(self.state_dir.glob("*.json")):
                instance = self._load(path.stem)
                if (not instance or instance.persistent
                        or instance.state not in {"stopped", "failed", "expired"}):
                    continue
                if not instance.stopped_at:
                    instance.stopped_at = current.isoformat()
                    self._save(instance)
                    continue
                try:
                    stopped = datetime.fromisoformat(instance.stopped_at.replace("Z", "+00:00"))
                    if stopped.tzinfo is None:
                        stopped = stopped.replace(tzinfo=timezone.utc)
                except (TypeError, ValueError):
                    instance.stopped_at = current.isoformat()
                    self._save(instance)
                    continue
                if (current - stopped).total_seconds() < self.stopped_retention_seconds:
                    continue
                if instance.pid or not instance.resources_cleaned:
                    continue
                try:
                    archive = self._archive_stopped_config(instance)
                except BackendError as exc:
                    print(f"stopped instance cleanup deferred for {instance.id}: {exc}")
                    continue
                self._remove_runtime(instance.id)
                self._state_path(instance.id).unlink(missing_ok=True)
                self._config_path(instance.id).unlink(missing_ok=True)
                cleaned.append({"instance_id": instance.id, "config_archive": str(archive)})
        return cleaned

    def cleanup_nonpersistent(self) -> dict[str, Any]:
        """Immediately archive and delete every eligible non-persistent record."""
        cleaned: list[dict[str, str]] = []
        skipped_persistent: list[str] = []
        with self.lock:
            for path in sorted(self.state_dir.glob("*.json")):
                instance = self._load(path.stem)
                if not instance:
                    continue
                instance = self._reconcile(instance)
                # Unknown adopted units are retained even when reading an old
                # record created before the explicit persistent flag existed.
                if instance.persistent or instance.build_id == "unknown":
                    if instance.state not in {"starting", "running", "orphaned"}:
                        skipped_persistent.append(instance.id)
                    continue
                if (instance.state not in {"stopped", "failed", "expired"}
                        or instance.pid or not instance.resources_cleaned):
                    continue
                archive = ""
                if self._config_path(instance.id).is_file():
                    archive = str(self._archive_stopped_config(instance))
                deleted = self.delete(instance.id)
                if deleted:
                    cleaned.append({
                        "instance_id": instance.id,
                        "config_archive": archive,
                    })
        return {
            "cleaned": cleaned,
            "cleaned_count": len(cleaned),
            "skipped_persistent": skipped_persistent,
            "skipped_persistent_count": len(skipped_persistent),
        }

    def shutdown(self, stop_instances: bool = False) -> None:
        """Close runner-owned transports; preserve systemd instances by default."""
        with self.lock:
            for session in self.cli_sessions.values():
                session.connection.close()
            self.cli_sessions.clear()
            for instance_id, transport in list(self.gdb_sessions.items()):
                self.close_gdb_transport(instance_id, transport)
            if not stop_instances:
                return
            for instance in self.list():
                if instance.state in {"starting", "running", "orphaned"}:
                    try:
                        self.stop(instance.id)
                    except BackendError as exc:
                        print(f"cleanup failed for {instance.id}: {exc}")


def uuid_is_valid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False


def openapi_document() -> dict[str, Any]:
    return {
        "openapi": "3.1.0",
        "info": {"title": "Capture Zone Smithproxy Runner API", "version": "1.0.0"},
        "servers": [{"url": "http://127.0.0.1:9080"}],
        "components": {"securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}}},
        "security": [{"bearer": []}],
        "paths": {
            "/v1/status": {"get": {"summary": "Runner, build and instance summary"}},
            "/v1/build": {
                "get": {"summary": "Build status and bounded log"},
                "post": {"summary": "Start an asynchronous source build"},
            },
            "/v1/refs/refresh": {
                "post": {"summary": "Start an asynchronous remote branch fetch"},
            },
            "/v1/instances/cleanup": {
                "post": {"summary": "Archive configs and delete stopped non-persistent instances"},
            },
            "/v1/test-drives/{id}/upgrade": {
                "post": {"summary": "Replace a Test Drive binary without changing its config or network"},
            },
            "/v1/test-drives/{id}/extend": {
                "post": {"summary": "Extend a Test Drive deadline"},
            },
            "/v1/test-drives/{id}/restart": {
                "post": {"summary": "Restart the same Test Drive binary and preserved config"},
            },
            "/v1/test-drives/{id}/config-mode": {
                "post": {"summary": "Switch Test Drive config between RO and RW"},
            },
            "/v1/test-drives/{id}/config/preview": {
                "post": {"summary": "Native-save Test Drive config into an approval preview"},
            },
            "/v1/builds/{id}": {
                "delete": {"summary": "Delete an unused archived binary"},
            },
            "/v1/builds/{id}/config/preview": {
                "post": {"summary": "Extract and native-save an archived build default config"},
            },
            "/v1/configs": {
                "get": {"summary": "List stored configuration snapshots"},
            },
            "/v1/configs/preview": {
                "post": {"summary": "Native-save a proposed configuration and return its approval diff"},
            },
            "/v1/configs/commit": {
                "post": {"summary": "Commit one explicitly approved native configuration preview"},
            },
            "/v1/configs/previews/{id}": {
                "delete": {"summary": "Cancel an uncommitted native configuration preview"},
            },
            "/v1/configs/{id}": {
                "get": {"summary": "Read configuration metadata and content"},
                "delete": {"summary": "Delete an unused configuration snapshot"},
            },
            "/v1/configs/{id}/metadata": {
                "put": {"summary": "Rename or describe a configuration without changing its content"},
            },
            "/v1/runtime-profiles": {
                "get": {"summary": "List binary and configuration bindings"},
                "post": {"summary": "Create a runtime profile"},
            },
            "/v1/runtime-profiles/{id}": {
                "get": {"summary": "Read one runtime profile and its usage"},
                "put": {"summary": "Update a binary and configuration binding"},
                "delete": {"summary": "Delete an unused runtime profile"},
            },
            "/v1/cert-bundles": {
                "get": {"summary": "List CA and certificate bundles"},
                "post": {"summary": "Generate a new CA keypair bundle"},
            },
            "/v1/cert-bundles/{id}": {
                "get": {"summary": "Read certificate bundle metadata"},
                "delete": {"summary": "Delete an unused certificate bundle"},
            },
            "/v1/cert-bundles/{id}/certificates": {
                "post": {"summary": "Generate or import an additional certificate"},
            },
            "/v1/cert-bundles/{id}/ca.pem": {
                "get": {"summary": "Download the public CA certificate"},
            },
            "/v1/sources": {"get": {"summary": "Configured source IP pool"}},
            "/v1/instances": {
                "get": {"summary": "List instances"},
                "post": {"summary": "Create an isolated instance"},
            },
            "/v1/instances/{id}": {
                "get": {"summary": "Read one instance"},
                "delete": {"summary": "Stop one instance"},
            },
            "/v1/instances/{id}/restart": {
                "post": {"summary": "Restart Smithproxy inside its existing unit and netns"},
            },
            "/v1/instances/{id}/extend": {
                "post": {"summary": "Add time to an active instance TTL"},
            },
            "/v1/tasks": {"get": {"summary": "List asynchronous tasks"}},
            "/v1/tasks/{id}": {"get": {"summary": "Read one asynchronous task"}},
            "/v1/tasks/{id}/result": {
                "get": {"summary": "Read the complete result of a successful task"},
            },
            "/v1/task-actions": {
                "post": {"summary": "Queue one allowlisted runner mutation"},
            },
            "/v1/settings/networking": {
                "get": {"summary": "Read namespace allocation and SAS routing settings"},
                "put": {"summary": "Update namespace allocation and SAS routing settings"},
            },
            "/v1/instances/{id}/diagnostics": {
                "get": {"summary": "Read execution model, paths and namespace details"},
            },
            "/v1/instances/{id}/debug": {
                "post": {"summary": "Attach a scoped gdbserver helper to a Debug build"},
                "delete": {"summary": "Stop the gdbserver helper"},
            },
            "/v1/instances/{id}/record": {
                "delete": {"summary": "Delete one revalidated stopped instance record"},
            },
            "/v1/instances/{id}/logs": {"get": {"summary": "Read bounded journal output"}},
            "/v1/instances/{id}/config": {
                "get": {"summary": "Download the effective configuration snapshot"},
            },
            "/v1/instances/{id}/config/save": {
                "post": {"summary": "Disabled legacy endpoint; use config/preview"},
            },
            "/v1/instances/{id}/config/preview": {
                "post": {"summary": "Live-save an RW instance config and return its approval diff"},
            },
            "/v1/instances/{id}/cli": {"post": {"summary": "Execute one Smithproxy CLI command"}},
            "/v1/config-observer": {
                "post": {"summary": "Native-save a config and its build default, then return their diff"},
            },
        },
    }


def store_build_default_config(
    builder: SmithproxyBuilder,
    config_library: ConfigLibrary,
    normalize: Callable[[str, str, Path], tuple[str, str, bool]],
    build_id: str,
) -> dict:
    """Persist one canonical native default for a newly completed build.

    Build defaults establish a new comparison baseline, so unlike imports and
    edits they don't require an administrator approval preview.  The logical
    identity includes ref as well as commit/build type and makes retries cheap.
    """
    artifact = builder.artifact(build_id)
    ref = str(artifact.get("ref", "detached"))
    for item in config_library.list():
        if (
            item.get("source_kind") == "native-build-default"
            and item.get("normalized_build_id") == build_id
            and item.get("source_ref") == ref
            and item.get("native") is True
        ):
            try:
                config_library.resolve(str(item["config_id"]))
                return {**item, "auto_imported": False}
            except BackendError:
                break

    binary = builder.resolve_binary(build_id)
    source = binary.parent / "smithproxy.cfg"
    assets = binary.parent / "smithproxy.assets"
    if not source.is_file() or not assets.is_dir():
        raise BackendError("archived build default config bundle is unavailable")
    content = source.read_text(encoding="utf-8")
    native, source_sha, _cache_hit = normalize(content, build_id, assets)
    commit_id = str(artifact.get("commit_id", ""))
    build_type = str(artifact.get("build_type", "Release"))
    item = config_library.import_text(
        f"{ref} @ {commit_id[:12]} ({build_type}) — default",
        native,
        assets,
        description="Default config automatically extracted from a completed build.",
        source_kind="native-build-default",
        source_commit=commit_id,
        source_ref=ref,
        profile="default",
        native=True,
        normalized_build_id=build_id,
        source_sha256=source_sha,
        approved_by="",
    )
    return {**item, "auto_imported": True}


def handler_factory(manager: Manager, token: str, max_body: int = 64 * 1024,
                    max_config_body: int = 2 * 1024 * 1024,
                    builder: SmithproxyBuilder | None = None,
                    config_library: ConfigLibrary | None = None,
                    runtime_profiles: RuntimeProfileLibrary | None = None,
                    cert_library: CertBundleLibrary | None = None,
                    config_previews: ConfigPreviewLibrary | None = None,
                    tasks: TaskQueue | None = None,
                    network_settings: NetworkSettings | None = None,
                    test_drives: TestDriveManager | None = None):
    def runtime_profile_ttl(payload: dict, default: int | None = 1800) -> int | None:
        ttl = payload.get("ttl_seconds", default)
        if ttl is None:
            return None
        if isinstance(ttl, bool) or not isinstance(ttl, int):
            raise ConfigError("ttl_seconds must be an integer or null for unlimited")
        if not manager.min_runtime <= ttl <= manager.max_total_runtime:
            raise ConfigError(
                f"ttl_seconds must be between {manager.min_runtime} and "
                f"{manager.max_total_runtime}, or null for unlimited"
            )
        return ttl

    def profile_view(item: dict, *, artifacts: list[dict] | None = None,
                     instances: list[Instance] | None = None) -> dict:
        result = dict(item)
        result["available"] = True
        result["newer_build_available"] = False
        if config_library:
            try:
                config = config_library.get(item["config_id"])
                result.update({
                    "config_name": config.get("name", item["config_id"]),
                    "config_profile": config.get("profile", "custom"),
                    "placeholders": config.get("placeholders", []),
                    "config_native": bool(config.get("native")),
                })
                if not config.get("native"):
                    result["available"] = False
            except BackendError:
                result.update({
                    "config_name": "missing", "config_profile": "unknown", "placeholders": [],
                    "available": False,
                })
        if builder:
            artifacts = artifacts if artifacts is not None else builder.status().get("artifacts", [])
            artifact = next(
                (candidate for candidate in artifacts
                 if candidate.get("build_id", candidate.get("commit_id")) == item["build_id"]), None,
            )
            result["build_ref"] = artifact.get("ref", "") if artifact else ""
            result["build_type"] = artifact.get("build_type", "") if artifact else ""
            if artifact is None:
                result["available"] = False
            else:
                try:
                    selected_at = datetime.fromisoformat(
                        str(artifact.get("commit_at", "")).replace("Z", "+00:00")
                    )
                    candidates = []
                    for candidate in artifacts:
                        if (candidate.get("ref", "") != artifact.get("ref", "")
                                or candidate.get("build_type", "Release")
                                != artifact.get("build_type", "Release")):
                            continue
                        try:
                            candidate_at = datetime.fromisoformat(
                                str(candidate.get("commit_at", "")).replace("Z", "+00:00")
                            )
                        except (TypeError, ValueError):
                            continue
                        candidates.append((candidate_at, candidate))
                    if candidates:
                        newest_at, newest = max(candidates, key=lambda value: value[0])
                        if newest_at > selected_at:
                            result.update({
                                "newer_build_available": True,
                                "newer_build_id": newest.get(
                                    "build_id", newest.get("commit_id", "")
                                ),
                                "newer_commit_id": newest.get("commit_id", ""),
                                "code_behind_days": max(
                                    0, int((newest_at - selected_at).total_seconds() // 86400)
                                ),
                            })
                except (TypeError, ValueError):
                    pass
        cert_bundle_id = str(item.get("cert_bundle_id", ""))
        result["cert_bundle_name"] = ""
        if cert_bundle_id and cert_library:
            try:
                result["cert_bundle_name"] = cert_library.get(cert_bundle_id).get(
                    "name", cert_bundle_id
                )
            except BackendError:
                result["cert_bundle_name"] = "missing"
                result["available"] = False
        result["usage"] = {"instances": [
            {
                "id": instance.id, "state": instance.state,
                "user_id": instance.user_id, "source_ip": instance.source_ip,
            }
            for instance in (instances if instances is not None else manager.snapshot())
            if instance.runtime_profile_id == item.get("profile_id")
        ]}
        return result

    def config_usage(config_id: str) -> dict:
        profiles = []
        if runtime_profiles:
            profiles = [
                {"profile_id": item.get("profile_id", ""), "name": item.get("name", "")}
                for item in runtime_profiles.list() if item.get("config_id") == config_id
            ]
        instances = [
            {
                "id": item.id, "state": item.state, "user_id": item.user_id,
                "source_ip": item.source_ip,
            }
            for item in manager.snapshot() if item.config_id == config_id
        ]
        return {"runtime_profiles": profiles, "instances": instances}

    def build_usage(build_id: str) -> dict:
        profiles = []
        if runtime_profiles:
            profiles = [
                {"profile_id": item.get("profile_id", ""), "name": item.get("name", "")}
                for item in runtime_profiles.list() if item.get("build_id") == build_id
            ]
        instances = [
            {
                "id": item.id, "state": item.state, "user_id": item.user_id,
                "source_ip": item.source_ip,
            }
            for item in manager.snapshot()
            if item.build_id == build_id
            and item.state in {"starting", "running", "orphaned"}
        ]
        drives = [
            {"id": item.id, "state": item.state}
            for item in (test_drives.snapshot() if test_drives else [])
            if item.build_id == build_id
        ]
        return {"runtime_profiles": profiles, "instances": instances, "test_drives": drives}

    def build_status_view() -> dict | None:
        if not builder:
            return None
        status = builder.status()
        instance_snapshot = manager.snapshot()
        drive_snapshot = test_drives.snapshot() if test_drives else []
        profile_snapshot = runtime_profiles.list() if runtime_profiles else []
        instance_usage: dict[str, list[dict]] = {}
        for item in instance_snapshot:
            if item.state not in {"starting", "running", "orphaned"}:
                continue
            instance_usage.setdefault(item.build_id, []).append({
                "id": item.id, "state": item.state, "user_id": item.user_id,
                "source_ip": item.source_ip,
            })
        drive_usage: dict[str, list[dict]] = {}
        for item in drive_snapshot:
            drive_usage.setdefault(item.build_id, []).append({
                "id": item.id, "state": item.state,
            })
        profile_usage: dict[str, list[dict]] = {}
        for item in profile_snapshot:
            profile_usage.setdefault(str(item.get("build_id", "")), []).append({
                "profile_id": item.get("profile_id", ""), "name": item.get("name", ""),
            })
        status["artifacts"] = [
            {
                **item,
                "usage": {
                    "runtime_profiles": profile_usage.get(
                        str(item.get("build_id", item.get("commit_id", ""))), []
                    ),
                    "instances": instance_usage.get(
                        str(item.get("build_id", item.get("commit_id", ""))), []
                    ),
                    "test_drives": drive_usage.get(
                        str(item.get("build_id", item.get("commit_id", ""))), []
                    ),
                },
            }
            for item in status.get("artifacts", [])
        ]
        return status

    def latest_master_build() -> dict:
        if not builder:
            raise BackendError("builder is unavailable")
        masters = [item for item in builder.artifacts() if item.get("ref") == "master"]
        releases = [item for item in masters if item.get("build_type") == "Release"]
        candidates = releases or masters
        if not candidates:
            raise BackendError("no archived master build is available for native normalization")
        return max(candidates, key=lambda item: item.get("commit_at") or item.get("built_at") or "")

    def verify_native_runtime(content: str, build_id: str) -> None:
        """Require a saved config to survive a second real Smithproxy load."""
        if not builder:
            raise BackendError("builder is unavailable")
        binary = builder.resolve_binary(build_id)
        with tempfile.TemporaryDirectory(prefix="native-reload-") as scratch_name:
            candidate = Path(scratch_name) / "smithproxy.cfg"
            candidate.write_text(content, encoding="utf-8")
            os.chmod(candidate, 0o600)
            manager.backend.native_save(binary, candidate, 50000)

    def normalize_native(content: str, build_id: str, assets: Path) -> tuple[str, str, bool]:
        if not builder:
            raise BackendError("builder is unavailable")
        binary = builder.resolve_binary(build_id)
        artifact = builder.artifact(build_id)
        build_key = builder.native_cache_key(
            str(artifact.get("commit_id", "")), str(artifact.get("ref", "")),
            str(artifact.get("build_type", "Release")),
        )
        source_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        root = builder.native_cache_dir / build_key
        sources = root / "sources"
        runtime = root / "canonical-runtime"
        for directory in (root, sources, runtime):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(directory, 0o700)
        target = sources / f"{source_sha}.cfg"
        verification = sources / f"{source_sha}.verified.json"
        binary_stat = binary.stat()
        verification_identity = {
            "native_sha256": "",
            "binary_size": binary_stat.st_size,
            "binary_mtime_ns": binary_stat.st_mtime_ns,
        }
        if target.is_file():
            cached = target.read_text(encoding="utf-8")
            verification_identity["native_sha256"] = hashlib.sha256(cached.encode()).hexdigest()
            try:
                recorded = json.loads(verification.read_text(encoding="utf-8"))
                if recorded != verification_identity:
                    verify_native_runtime(cached, build_id)
                    verification.write_text(
                        json.dumps(verification_identity, separators=(",", ":")),
                        encoding="utf-8",
                    )
                    os.chmod(verification, 0o600)
                return cached, source_sha, True
            except (BackendError, OSError, ValueError, TypeError, json.JSONDecodeError):
                # A build artifact may have been rebuilt in place or an older
                # normalizer may have cached a lossy `save config` result.
                target.unlink(missing_ok=True)
                verification.unlink(missing_ok=True)
        parameters = {
            "socks_port": 1080, "http_port": 3128,
            "plaintext_port": 50080, "tls_port": 50443,
            "cli_port": 50000, "workers": 1, "pcap_quota_mb": 100,
        }
        with builder.lock:
            if target.is_file():
                return target.read_text(encoding="utf-8"), source_sha, True
            with tempfile.TemporaryDirectory(prefix="native-config-") as scratch_name:
                scratch = Path(scratch_name)
                source = scratch / "source.cfg"
                source.write_text(content, encoding="utf-8")
                text_values = {
                    token.lower(): "{{" + token + "}}"
                    for token in TOKEN_RE.findall(content)
                    if token not in ({name.upper() for name in PARAMETERS} | {"RUNTIME_DIR"})
                }
                rendered = render_template(
                    source, parameters, runtime, assets_dir=assets,
                    text_parameters=text_values,
                )
                checked = scratch / "smithproxy.cfg"
                checked.write_text(rendered, encoding="utf-8")
                os.chmod(checked, 0o600)
                native = manager.backend.native_save(binary, checked, parameters["cli_port"])
                # `save config` is only accepted as canonical when the same
                # binary can load its output again. This catches lossy saves
                # which omit required top-level sections such as signatures.
                verify_native_runtime(native, build_id)
            temporary = target.with_suffix(".cfg.new")
            temporary.write_text(native, encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(target)
            verification_identity["native_sha256"] = hashlib.sha256(native.encode()).hexdigest()
            verification.write_text(
                json.dumps(verification_identity, separators=(",", ":")), encoding="utf-8"
            )
            os.chmod(verification, 0o600)
            return native, source_sha, False

    def create_native_preview(payload: dict) -> dict:
        if not builder or not config_library or not config_previews:
            raise BackendError("native config workflow is unavailable")
        action = str(payload.get("action", "create"))
        if action not in {"create", "update"}:
            raise ConfigError("config preview action must be create or update")
        content = payload.get("content", "")
        if not isinstance(content, str) or not content or len(content.encode("utf-8")) > 1024 * 1024:
            raise ConfigError("configuration must contain 1 to 1048576 UTF-8 bytes")
        target_id = str(payload.get("config_id", ""))
        current_content = ""
        if action == "update":
            config_library.get(target_id)
            current_content = config_library.read_content(target_id)
        requested_build_id = str(payload.get("build_id", "")).strip()
        if not requested_build_id:
            raise ConfigError("build_id is required for native config validation")
        normalizer = builder.artifact(requested_build_id)
        build_id = str(normalizer.get("build_id", normalizer.get("commit_id", "")))
        binary = builder.resolve_binary(build_id)
        assets = binary.parent / "smithproxy.assets"
        native, source_sha, cache_hit = normalize_native(content, build_id, assets)
        baseline_sha = ""
        baseline_cache_hit = False
        if action == "update":
            baseline_native, baseline_sha, baseline_cache_hit = normalize_native(
                current_content, build_id, assets
            )
            before_diff = baseline_native
            from_name = "stored-native.cfg"
            comparison = "stored-native-to-proposed-native"
        else:
            before_diff = content
            from_name = "uploaded-source.cfg"
            comparison = "upload-source-to-native"
        diff = "".join(difflib.unified_diff(
            before_diff.splitlines(keepends=True), native.splitlines(keepends=True),
            fromfile=from_name, tofile="proposed-native.cfg",
        ))
        document = config_previews.create({
            "action": action, "config_id": target_id,
            "name": str(payload.get("name", "Configuration"))[:128],
            "description": str(payload.get("description", ""))[:1000],
            "profile": str(payload.get("profile", "custom")),
            "requested_content": content, "native_content": native,
            "source_sha256": source_sha, "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
            "baseline_sha256": baseline_sha,
            "normalized_build_id": build_id,
        }, assets=assets)
        return {
            **{key: document[key] for key in (
                "preview_id", "expires_at", "action", "config_id", "name", "description",
                "profile", "source_sha256", "native_sha256", "normalized_build_id",
            )},
            "diff": diff, "changed": before_diff != native, "cache_hit": cache_hit,
            "baseline_cache_hit": baseline_cache_hit, "comparison": comparison,
            "baseline_sha256": baseline_sha,
            "normalizer": {
                "build_id": build_id,
                "commit_id": normalizer.get("commit_id", ""),
                "ref": normalizer.get("ref", ""),
                "build_type": normalizer.get("build_type", "Release"),
            },
        }

    def create_build_default_preview(build_id: str) -> dict:
        if not builder or not config_library or not config_previews:
            raise BackendError("native config workflow is unavailable")
        artifact = builder.artifact(build_id)
        binary = builder.resolve_binary(build_id)
        source = binary.parent / "smithproxy.cfg"
        assets = binary.parent / "smithproxy.assets"
        if not source.is_file() or not assets.is_dir():
            raise BackendError("archived build default config bundle is unavailable")
        content = source.read_text(encoding="utf-8")
        native, source_sha, cache_hit = normalize_native(content, build_id, assets)
        diff = "".join(difflib.unified_diff(
            content.splitlines(keepends=True), native.splitlines(keepends=True),
            fromfile="build-default-source.cfg", tofile="build-default-native.cfg",
        ))
        ref = str(artifact.get("ref", "detached"))
        build_type = str(artifact.get("build_type", "Release"))
        commit_id = str(artifact.get("commit_id", ""))
        document = config_previews.create({
            "action": "create", "config_id": "",
            "name": f"{ref} @ {commit_id[:12]} ({build_type}) — default",
            "description": "Default config extracted from an archived build bundle.",
            "profile": "custom", "requested_content": content,
            "native_content": native, "source_sha256": source_sha,
            "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
            "baseline_sha256": "", "normalized_build_id": build_id,
            "source_build_default": build_id, "source_commit": commit_id,
            "source_ref": ref,
        }, assets=assets)
        return {
            **{key: document[key] for key in (
                "preview_id", "expires_at", "action", "config_id", "name", "description",
                "profile", "source_sha256", "native_sha256", "normalized_build_id",
            )},
            "diff": diff, "changed": content != native, "cache_hit": cache_hit,
            "baseline_cache_hit": False, "comparison": "build-default-source-to-native",
            "baseline_sha256": "",
            "normalizer": {"commit_id": commit_id, "ref": ref, "build_type": build_type},
        }

    def commit_native_preview(payload: dict) -> dict:
        if not builder or not config_library or not config_previews:
            raise BackendError("native config workflow is unavailable")
        if payload.get("approved") is not True:
            raise ConfigError("explicit native config approval is required")
        preview_id = str(payload.get("preview_id", ""))
        preview = config_previews.claim(preview_id)
        try:
            build_id = str(preview["normalized_build_id"])
            assets = config_previews.resolve_assets(preview_id)
            verify_native_runtime(str(preview["native_content"]), build_id)
            common = {
                "description": str(preview.get("description", "")),
                "profile": str(preview.get("profile", "custom")),
                "native": True,
                "normalized_build_id": build_id,
                "source_sha256": str(preview.get("source_sha256", "")),
                "approved_by": str(payload.get("approved_by", "admin"))[:128],
            }
            if preview["action"] == "update":
                item = config_library.update_text(
                    str(preview["config_id"]), str(preview["name"]),
                    str(preview["native_content"]), assets_from=assets, **common,
                )
            else:
                source_kind = "native-upload"
                if preview.get("source_test_drive"):
                    source_kind = "native-test-drive"
                elif preview.get("source_instance"):
                    source_kind = "native-instance"
                elif preview.get("source_build_default"):
                    source_kind = "native-build-default"
                item = config_library.import_text(
                    str(preview["name"]), str(preview["native_content"]), assets,
                    source_kind=source_kind,
                    source_instance=str(
                        preview.get("source_test_drive", preview.get("source_instance", ""))
                    ), **common,
                    source_commit=str(preview.get("source_commit", "")),
                    source_ref=str(preview.get("source_ref", "")),
                )
        except Exception:
            config_previews.release(preview_id)
            raise
        config_previews.delete(preview_id)
        return item

    def submit_task(kind: str, label: str, dedupe_key: str, resource: str,
                    function: Any) -> dict:
        if not tasks:
            raise BackendError("task queue is unavailable")
        task, created = tasks.submit(kind, label, dedupe_key, resource, function)
        return tasks.view(task, deduplicated=not created)

    def wait_for_build() -> dict:
        if not builder:
            raise BackendError("builder is unavailable")
        while True:
            with builder.lock:
                state = asdict(builder.state)
            if state.get("state") != "running":
                if state.get("state") == "failed":
                    raise BackendError(str(state.get("error") or "build failed"))
                return state
            time.sleep(0.5)

    def wait_for_refs() -> dict:
        if not builder:
            raise BackendError("builder is unavailable")
        while True:
            with builder.refs_lock:
                state = json.loads(json.dumps(builder.refs_state))
            if state.get("state") != "running":
                if state.get("state") == "failed":
                    raise BackendError(str(state.get("error") or "ref refresh failed"))
                return state
            time.sleep(0.25)

    action_routes = {
        "POST": (
            r"/v1/builds/[A-Za-z0-9._-]+/config/preview",
            r"/v1/configs/(?:preview|commit)",
            r"/v1/config-observer",
            r"/v1/instances/[0-9a-f-]+/(?:debug|restart|config/preview)",
            r"/v1/cert-bundles",
            r"/v1/cert-bundles/[0-9a-f-]+/certificates",
            r"/v1/runtime-profiles",
            r"/v1/test-drives",
            r"/v1/test-drives/[0-9a-f-]+/upgrade",
            r"/v1/test-drives/[0-9a-f-]+/(?:extend|restart|config-mode|config/preview)",
            r"/v1/instances/cleanup",
        ),
        "PUT": (
            r"/v1/runtime-profiles/[0-9a-f-]+",
            r"/v1/configs/[0-9a-f-]+/metadata",
            r"/v1/settings/networking",
        ),
        "DELETE": (
            r"/v1/builds/[A-Za-z0-9._-]+",
            r"/v1/configs/previews/[0-9a-f-]+",
            r"/v1/instances/[0-9a-f-]+/debug",
            r"/v1/runtime-profiles/[0-9a-f-]+",
            r"/v1/cert-bundles/[0-9a-f-]+",
            r"/v1/configs/[0-9a-f-]+",
            r"/v1/instances/[0-9a-f-]+/record",
            r"/v1/instances/[0-9a-f-]+",
            r"/v1/test-drives/[0-9a-f-]+",
        ),
    }

    def action_resource(path: str) -> str:
        if path == "/v1/instances/cleanup":
            return "instances:cleanup"
        match = re.fullmatch(r"/v1/test-drives/([0-9a-f-]+)(?:/.*)?", path)
        if match:
            return f"test-drive:{match.group(1)}"
        if path == "/v1/test-drives":
            return "test-drives"
        match = re.fullmatch(r"/v1/instances/([0-9a-f-]+)(?:/.*)?", path)
        if match:
            return f"instance:{match.group(1)}"
        match = re.fullmatch(r"/v1/(?:cert-bundles|runtime-profiles|configs|builds)/([^/]+)(?:/.*)?", path)
        if match:
            return f"library:{path.split('/')[2]}:{match.group(1)}"
        if path == "/v1/config-observer" or path == "/v1/configs/preview":
            return "native-config"
        return f"library:{path.split('/')[2]}"

    class Handler(BaseHTTPRequestHandler):
        server_version = "CaptureZoneRunner/0.1"

        def _json(self, status: int, value: object) -> None:
            body = json.dumps(value, separators=(",", ":")).encode()
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                # Browsers and reverse proxies routinely cancel superseded
                # polling requests.  The requested operation has already
                # completed, so there is nothing to retry or report here.
                self.close_connection = True

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return bool(token) and hmac.compare_digest(supplied, expected)

        def _path(self) -> list[str]:
            return [part for part in urlsplit(self.path).path.split("/") if part]

        def do_GET(self) -> None:
            parts = self._path()
            if parts == ["healthz"]:
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if parts == ["v1", "openapi.json"]:
                self._json(HTTPStatus.OK, openapi_document())
            elif parts == ["v1", "status"]:
                instances = manager.snapshot()
                task_items = tasks.list() if tasks else []
                self._json(HTTPStatus.OK, {
                    "status": "ok", "api_version": "v1",
                    "instances": {
                        "total": len(instances),
                        "running": sum(item.state in {"running", "orphaned"} for item in instances),
                        "orphaned": sum(item.state == "orphaned" for item in instances),
                    },
                    "tasks": {
                        "pending": sum(item["state"] == "pending" for item in task_items),
                        "running": sum(item["state"] == "running" for item in task_items),
                    },
                    "build": build_status_view(),
                })
            elif parts == ["v1", "tasks"] and tasks:
                self._json(HTTPStatus.OK, {"tasks": tasks.list()})
            elif len(parts) == 3 and parts[:2] == ["v1", "tasks"] and tasks:
                item = tasks.get(parts[2])
                self._json(HTTPStatus.OK, tasks.view(item)) if item else self._json(
                    HTTPStatus.NOT_FOUND, {"error": "task not found"}
                )
            elif (len(parts) == 4 and parts[:2] == ["v1", "tasks"]
                  and parts[3] == "result" and tasks):
                item = tasks.get(parts[2])
                if not item:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "task not found"})
                elif item.state != "succeeded":
                    self._json(HTTPStatus.CONFLICT, {"error": "task has no completed result"})
                else:
                    try:
                        self._json(HTTPStatus.OK, tasks.read_result(parts[2]))
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            elif parts == ["v1", "instances"]:
                self._json(HTTPStatus.OK, {
                    "instances": [asdict(i) for i in manager.snapshot()],
                    "reconciliation": "background",
                })
            elif parts == ["v1", "test-drives"] and test_drives:
                self._json(HTTPStatus.OK, {
                    "test_drives": [asdict(item) for item in test_drives.snapshot()],
                    "reconciliation": "background",
                })
            elif len(parts) == 3 and parts[:2] == ["v1", "test-drives"] and test_drives:
                item = test_drives.peek(parts[2])
                self._json(HTTPStatus.OK, asdict(item)) if item else self._json(
                    HTTPStatus.NOT_FOUND, {"error": "test drive not found"}
                )
            elif (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                  and parts[3] == "logs" and test_drives):
                try:
                    self._json(HTTPStatus.OK, {"output": test_drives.logs(parts[2])})
                except (ConfigError, BackendError) as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                  and parts[3] == "files" and test_drives):
                try:
                    query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(
                        urlsplit(self.path).query
                    )
                    requested = str(query.get("path", [""])[0])
                    if requested:
                        name, content = test_drives.read_file(parts[2], requested)
                        self._json(HTTPStatus.OK, {
                            "name": name, "path": requested,
                            "content_base64": base64.b64encode(content).decode("ascii"),
                        })
                    else:
                        self._json(HTTPStatus.OK, {"files": test_drives.files(parts[2])})
                except (ConfigError, BackendError, OSError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            elif parts == ["v1", "sources"]:
                active = {
                    item.source_ip for item in manager.snapshot()
                    if item.state in {"starting", "running", "orphaned"}
                }
                self._json(HTTPStatus.OK, {"sources": [
                    {"ip": item, "available": item not in active} for item in manager.sources()
                ]})
            elif parts == ["v1", "settings", "networking"] and network_settings:
                try:
                    self._json(HTTPStatus.OK, {
                        **network_settings.view(),
                        "authorized_source_ips": manager.sources(),
                    })
                except (ConfigError, OSError) as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            elif parts == ["v1", "build"] and builder:
                self._json(HTTPStatus.OK, build_status_view())
            elif parts == ["v1", "configs"] and config_library:
                self._json(HTTPStatus.OK, {
                    "configs": builder.configs() if builder else config_library.list()
                })
            elif parts == ["v1", "runtime-profiles"] and runtime_profiles:
                try:
                    build_artifacts = builder.status().get("artifacts", []) if builder else []
                    instance_snapshot = manager.snapshot()
                    self._json(HTTPStatus.OK, {
                        "profiles": [
                            profile_view(
                                item, artifacts=build_artifacts, instances=instance_snapshot,
                            )
                            for item in runtime_profiles.list()
                        ]
                    })
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            elif len(parts) == 3 and parts[:2] == ["v1", "runtime-profiles"] and runtime_profiles:
                try:
                    self._json(HTTPStatus.OK, profile_view(runtime_profiles.get(parts[2])))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif parts == ["v1", "cert-bundles"] and cert_library:
                self._json(HTTPStatus.OK, {"bundles": cert_library.list()})
            elif len(parts) == 3 and parts[:2] == ["v1", "cert-bundles"] and cert_library:
                try:
                    self._json(HTTPStatus.OK, cert_library.get(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif (len(parts) == 4 and parts[:2] == ["v1", "cert-bundles"]
                  and parts[3] == "ca.pem" and cert_library):
                try:
                    self._json(HTTPStatus.OK, {
                        "bundle_id": parts[2],
                        "content": cert_library.read_ca_certificate(parts[2]),
                    })
                except (BackendError, OSError) as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif len(parts) == 3 and parts[:2] == ["v1", "configs"] and config_library:
                try:
                    item = config_library.get(parts[2])
                    self._json(HTTPStatus.OK, {
                        **item, "content": config_library.read_content(parts[2]),
                        "usage": config_usage(parts[2]),
                    })
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif len(parts) == 3 and parts[:2] == ["v1", "instances"]:
                item = manager.peek(parts[2])
                self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            elif len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "diagnostics":
                item = manager.peek(parts[2])
                if not item:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                else:
                    try:
                        binary_path = str(builder.resolve_binary(item.build_id)) if builder else "unknown"
                    except BackendError:
                        binary_path = "unavailable"
                    try:
                        build_type = builder.artifact(item.build_id).get("build_type", "Release") if builder else "unknown"
                    except BackendError:
                        build_type = "unknown"
                    runtime_dir = manager.runtime_root / item.id
                    try:
                        network = manager.backend.network_diagnostics(item.id)
                    except (AttributeError, BackendError):
                        network = {}
                    self._json(HTTPStatus.OK, {
                        "instance": asdict(item),
                        "execution": {
                            "model": "systemd transient unit + network namespace",
                            "unit": item.unit,
                            "rootfs": "/",
                            "rootfs_mode": "host-shared, ProtectSystem=strict",
                            "network_namespace": item.namespace,
                            "network_namespace_path": f"/run/netns/{item.namespace}" if item.namespace else "unknown",
                            "runtime_dir": str(runtime_dir),
                            "live_config": str(runtime_dir / "smithproxy.cfg"),
                            "saved_config_snapshot": str(manager._config_path(item.id)),
                            "private_run": str(runtime_dir / "run"),
                            "binary": binary_path,
                            "build_type": build_type,
                            "network": network,
                            "debug": {
                                "unit": item.debug_unit,
                                "address": item.debug_address,
                                "port": item.debug_port,
                                "ssh_tunnel": (
                                    f"ssh -L 2345:{item.debug_address}:{item.debug_port} "
                                    "<ssh-user>@<appliance-host>"
                                    if item.debug_address and item.debug_port else ""
                                ),
                                "copy_binary": (
                                    f"scp <ssh-user>@<appliance-host>:{binary_path} ./smithproxy-debug"
                                    if binary_path not in {"unknown", "unavailable"} else ""
                                ),
                                "gdb_commands": "gdb ./smithproxy-debug\n(gdb) target remote 127.0.0.1:2345",
                            },
                        },
                    })
            elif len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "logs":
                try:
                    query = __import__("urllib.parse", fromlist=["parse_qs"]).parse_qs(urlsplit(self.path).query)
                    lines = int(query.get("lines", ["200"])[0])
                    self._json(HTTPStatus.OK, {"output": manager.logs(parts[2], lines)})
                except (ConfigError, BackendError, ValueError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            elif len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "config":
                try:
                    item, content = manager.config_content(parts[2])
                    self._json(HTTPStatus.OK, {
                        "instance_id": item.id, "state": item.state,
                        "build_id": item.build_id, "config_id": item.config_id,
                        "content": content,
                    })
                except ConfigError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def do_PUT(self) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            parts = self._path()
            if parts == ["v1", "settings", "networking"] and network_settings:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid networking settings size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("networking settings must be an object")
                    raw_sources = payload.pop("authorized_source_ips", [])
                    if not isinstance(raw_sources, list) or len(raw_sources) > 4096:
                        raise ConfigError("authorized_source_ips must be an array")
                    validated_settings = NetworkSettings.validate(payload)
                    namespace_network = ipaddress.ip_network(
                        validated_settings["namespace_cidr"]
                    )
                    sources = []
                    for raw_source in raw_sources:
                        try:
                            source = str(ipaddress.ip_address(str(raw_source)))
                        except ValueError as exc:
                            raise ConfigError(f"invalid authorized source IP: {raw_source}") from exc
                        if ":" in source:
                            raise ConfigError("authorized source IPs currently support IPv4 only")
                        if ipaddress.ip_address(source) in namespace_network:
                            raise ConfigError(
                                f"authorized source IP overlaps namespace CIDR: {source}"
                            )
                        if source not in sources:
                            sources.append(source)
                    if not sources:
                        raise ConfigError("at least one authorized source IP is required")
                    settings = network_settings.update(validated_settings)
                    NetworkSettings._write(manager.sources_path, sources)
                    self._json(HTTPStatus.OK, {
                        **network_settings.view(), "authorized_source_ips": sources,
                    })
                except (ConfigError, OSError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 3 and parts[:2] == ["v1", "runtime-profiles"]
                    and builder and config_library and runtime_profiles):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid runtime profile update size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    build_id = str(payload.get("build_id", ""))
                    config_id = str(payload.get("config_id", ""))
                    cert_bundle_id = str(payload.get("cert_bundle_id", ""))
                    auto_restart = payload.get("auto_restart", False)
                    if not isinstance(auto_restart, bool):
                        raise ConfigError("auto_restart must be a boolean")
                    current_profile = runtime_profiles.get(parts[2])
                    ttl_seconds = runtime_profile_ttl(
                        payload, current_profile.get("ttl_seconds", 1800)
                    )
                    if build_id == "active":
                        raise ConfigError("runtime profiles must use an archived build")
                    builder.resolve_binary(build_id)
                    selected_config = config_library.get(config_id)
                    if not selected_config.get("native"):
                        raise ConfigError("runtime profiles require an approved native config")
                    if cert_bundle_id:
                        if not cert_library:
                            raise ConfigError("certificate bundles are unavailable")
                        cert_library.get(cert_bundle_id)
                    item = runtime_profiles.update(
                        parts[2], str(payload.get("name", "")), build_id,
                        config_id, cert_bundle_id,
                        auto_restart, ttl_seconds,
                    )
                    self._json(HTTPStatus.OK, profile_view(item))
                except (ConfigError, BackendError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "configs"]
                    and parts[3] == "metadata" and config_library):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid configuration metadata size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    item = config_library.update_metadata(
                        parts[2], str(payload.get("name", "")),
                        str(payload.get("description", "")),
                    )
                    self._json(HTTPStatus.OK, item)
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) != 3 or parts[:2] != ["v1", "configs"]
                    or not builder or not config_library):
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            self._json(HTTPStatus.CONFLICT, {
                "error": "direct config updates are disabled; use /v1/configs/preview then /v1/configs/commit"
            })
            return

        def do_POST(self) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            parts = self._path()
            if parts == ["v1", "instances", "cleanup"]:
                try:
                    self._json(HTTPStatus.OK, manager.cleanup_nonpersistent())
                except (BackendError, ConfigError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                    and parts[3] == "upgrade" and test_drives and builder):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid test drive upgrade request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("test drive upgrade request must be an object")
                    build_id = str(payload.get("build_id", ""))
                    binary = builder.resolve_binary(build_id)
                    item = test_drives.upgrade(parts[2], build_id, binary)
                    self._json(HTTPStatus.OK, asdict(item))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                    and parts[3] == "extend" and test_drives):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid test drive extension request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("test drive extension request must be an object")
                    item = test_drives.extend(parts[2], payload.get("additional_seconds"))
                    self._json(HTTPStatus.OK, asdict(item))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                    and parts[3] == "restart" and test_drives):
                try:
                    item = test_drives.restart(parts[2])
                    self._json(HTTPStatus.OK, asdict(item))
                except (ConfigError, BackendError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                    and parts[3] == "config-mode" and test_drives):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid Test Drive config mode request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("Test Drive config mode request must be an object")
                    item = test_drives.set_config_mode(
                        parts[2], str(payload.get("config_mode", ""))
                    )
                    self._json(HTTPStatus.OK, asdict(item))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 5 and parts[:2] == ["v1", "test-drives"]
                    and parts[3:] == ["config", "preview"] and test_drives
                    and builder and config_previews):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid Test Drive config preview request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("Test Drive config preview request must be an object")
                    drive, source, config_path = test_drives.config_content(parts[2])
                    assets = config_path.parent.parent / "assets"
                    if drive.state == "running" and drive.config_mode == "rw":
                        drive, before, native, config_path = test_drives.save_live_config(parts[2])
                        source = before
                        source_sha = hashlib.sha256(source.encode()).hexdigest()
                        cache_hit = False
                        comparison = "test-drive-live-save-to-native"
                    else:
                        native, source_sha, cache_hit = normalize_native(
                            source, drive.build_id, assets
                        )
                        comparison = "test-drive-file-to-native"
                    diff = "".join(difflib.unified_diff(
                        source.splitlines(keepends=True), native.splitlines(keepends=True),
                        fromfile="test-drive.cfg", tofile="test-drive-native.cfg",
                    ))
                    artifact = builder.artifact(drive.build_id)
                    document = config_previews.create({
                        "action": "create", "config_id": "",
                        "name": str(payload.get("name", "")).strip()[:128]
                        or f"Test Drive {drive.id[:12]}",
                        "description": str(payload.get("description", ""))[:1000],
                        "profile": "custom", "requested_content": source,
                        "native_content": native, "source_sha256": source_sha,
                        "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
                        "baseline_sha256": "", "normalized_build_id": drive.build_id,
                        "source_test_drive": drive.id,
                    }, assets=assets)
                    self._json(HTTPStatus.OK, {
                        **{key: document[key] for key in (
                            "preview_id", "expires_at", "action", "config_id", "name",
                            "description", "profile", "source_sha256", "native_sha256",
                            "normalized_build_id", "source_test_drive",
                        )},
                        "diff": diff, "changed": source != native,
                        "cache_hit": cache_hit, "baseline_cache_hit": False,
                        "comparison": comparison, "baseline_sha256": "",
                        "normalizer": {
                            "build_id": drive.build_id,
                            "commit_id": artifact.get("commit_id", ""),
                            "ref": artifact.get("ref", ""),
                            "build_type": artifact.get("build_type", "Release"),
                        },
                    })
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if parts == ["v1", "test-drives"] and test_drives and builder:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(payload, dict):
                        raise ConfigError("test drive request must be an object")
                    build_id = str(payload.get("build_id", ""))
                    artifact = builder.artifact(build_id)
                    binary = builder.resolve_binary(build_id)
                    root = binary.parent
                    item = test_drives.create(
                        build_id, binary, root / "smithproxy.cfg",
                        root / "smithproxy.assets", payload.get("ttl_seconds"),
                        str(payload.get("config_mode", "ro")),
                    )
                    self._json(HTTPStatus.CREATED, asdict(item))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
                    and parts[3] == "files" and test_drives):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 24 * 1024 * 1024:
                        raise ConfigError("invalid test drive upload size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("test drive upload must be an object")
                    self._json(HTTPStatus.CREATED, test_drives.write_file(
                        parts[2], str(payload.get("path", "")),
                        str(payload.get("content_base64", "")),
                    ))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "task-actions"]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_config_body:
                        raise ConfigError("invalid task action size")
                    request_payload = json.loads(self.rfile.read(length))
                    if not isinstance(request_payload, dict):
                        raise ConfigError("task action must be an object")
                    method = str(request_payload.get("method", "")).upper()
                    path = str(request_payload.get("path", ""))
                    payload = request_payload.get("payload")
                    if payload is not None and not isinstance(payload, dict):
                        raise ConfigError("task action payload must be an object")
                    if method not in action_routes or not any(
                        re.fullmatch(pattern, path) for pattern in action_routes[method]
                    ):
                        raise ConfigError("task action is not allowlisted")
                    kind = str(request_payload.get("kind", "runner-action"))
                    if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", kind):
                        raise ConfigError("invalid task kind")
                    label = str(request_payload.get("label", "Runner action"))[:200]
                    canonical = json.dumps(
                        {"method": method, "path": path, "payload": payload},
                        sort_keys=True, separators=(",", ":"),
                    )

                    def execute_action() -> Any:
                        body = json.dumps(payload).encode() if payload is not None else None
                        host, port = self.server.server_address[:2]
                        if host in {"0.0.0.0", "::"}:
                            host = "127.0.0.1"
                        request = Request(
                            f"http://{host}:{port}{path}", data=body, method=method,
                            headers={
                                "Authorization": f"Bearer {token}",
                                "Content-Type": "application/json",
                            },
                        )
                        try:
                            with urlopen(request, timeout=1800) as response:
                                return json.load(response)
                        except HTTPError as exc:
                            try:
                                message = json.load(exc).get("error", str(exc))
                            except Exception:
                                message = str(exc)
                            raise BackendError(message) from exc
                        except (URLError, OSError, TimeoutError) as exc:
                            raise BackendError(f"internal runner action failed: {exc}") from exc

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        kind, label,
                        "action:" + hashlib.sha256(canonical.encode()).hexdigest(),
                        action_resource(path), execute_action,
                    ))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 5 and parts[:2] == ["v1", "builds"]
                    and parts[3:] == ["config", "preview"]):
                try:
                    self._json(HTTPStatus.OK, create_build_default_preview(parts[2]))
                except (ConfigError, BackendError, OSError, ValueError, TypeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "configs", "preview"]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_config_body:
                        raise ConfigError("invalid native config preview size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    self._json(HTTPStatus.OK, create_native_preview(payload))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "configs", "commit"]:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid native config commit size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    self._json(HTTPStatus.CREATED, commit_native_preview(payload))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "refs", "refresh"] and builder:
                def refresh_refs_task() -> dict:
                    builder.request_ref_refresh()
                    return wait_for_refs()

                self._json(HTTPStatus.ACCEPTED, submit_task(
                    "refs-refresh", "Obnovit vzdálené branche", "refs:refresh", "git",
                    refresh_refs_task,
                ))
                return
            if parts == ["v1", "config-observer"] and builder and config_library:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid config observer request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    build_id = str(payload.get("build_id", ""))
                    config_id = str(payload.get("config_id", ""))
                    if build_id == "active":
                        raise ConfigError("config observer requires an archived build")
                    binary = builder.resolve_binary(build_id)
                    artifact = builder.artifact(build_id)
                    build_identity = {
                        "commit_id": str(artifact.get("commit_id", "")),
                        "ref": str(artifact.get("ref", "")),
                        "build_type": str(artifact.get("build_type", "Release")),
                    }
                    build_cache_key = builder.native_cache_key(
                        build_identity["commit_id"], build_identity["ref"],
                        build_identity["build_type"],
                    )
                    default_config = binary.parent / "smithproxy.cfg"
                    default_assets = binary.parent / "smithproxy.assets"
                    candidate_config = config_library.resolve(config_id)
                    candidate_metadata = config_library.get(config_id)
                    if not default_config.is_file() or not default_assets.is_dir():
                        raise BackendError("matching build default config is unavailable")
                    parameters = {
                        "socks_port": 1080, "http_port": 3128,
                        "plaintext_port": 50080, "tls_port": 50443,
                        "cli_port": 50000, "workers": 1, "pcap_quota_mb": 100,
                    }
                    cache_root = builder.native_cache_dir / build_cache_key
                    runtime = cache_root / "runtime"
                    configs_cache = cache_root / "configs"
                    for directory in (cache_root, runtime, configs_cache):
                        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.chmod(directory, 0o700)
                    normalizer_version = "smithproxy-native-save-v1"

                    def fingerprint(template: Path, identity: str) -> str:
                        digest = hashlib.sha256()
                        digest.update(normalizer_version.encode())
                        digest.update(
                            b"\0" + json.dumps(build_identity, sort_keys=True).encode()
                            + b"\0" + identity.encode() + b"\0"
                        )
                        digest.update(template.read_bytes())
                        digest.update(json.dumps(parameters, sort_keys=True).encode())
                        return digest.hexdigest()

                    def cached_native(template: Path, target: Path, identity: str) -> tuple[str, bool]:
                        wanted = fingerprint(template, identity)
                        metadata_path = target.with_suffix(".json")
                        try:
                            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                            if metadata.get("fingerprint") == wanted and target.is_file():
                                return target.read_text(encoding="utf-8"), True
                        except (OSError, ValueError, TypeError, json.JSONDecodeError):
                            pass
                        with tempfile.TemporaryDirectory(prefix="config-observer-") as scratch_name:
                            scratch = Path(scratch_name)
                            raw = template.read_text(encoding="utf-8")
                            text_values = {
                                token.lower(): f"observer-{token.lower()}.example"
                                for token in TOKEN_RE.findall(raw)
                                if token not in ({name.upper() for name in PARAMETERS} | {"RUNTIME_DIR"})
                            }
                            rendered = render_template(
                                template, parameters, runtime, assets_dir=default_assets,
                                text_parameters=text_values,
                            )
                            input_path = scratch / "smithproxy.cfg"
                            input_path.write_text(rendered, encoding="utf-8")
                            os.chmod(input_path, 0o600)
                            native = manager.backend.native_save(
                                binary, input_path, parameters["cli_port"]
                            )
                        temporary = target.with_suffix(".cfg.new")
                        temporary.write_text(native, encoding="utf-8")
                        os.chmod(temporary, 0o600)
                        temporary.replace(target)
                        metadata_temporary = metadata_path.with_suffix(".json.new")
                        metadata_temporary.write_text(json.dumps({
                            "fingerprint": wanted,
                            "normalizer": normalizer_version,
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                            "build_id": build_id,
                            "config_id": config_id if identity != "default" else "",
                        }, separators=(",", ":")), encoding="utf-8")
                        os.chmod(metadata_temporary, 0o600)
                        metadata_temporary.replace(metadata_path)
                        return native, False

                    # One lock serializes the fixed observer ports and atomic
                    # cache writes. Build IDs are immutable; config SHA changes
                    # automatically invalidate the candidate snapshot.
                    with builder.lock:
                        native_default, default_cached = cached_native(
                            default_config, binary.parent / "smithproxy.native.cfg",
                            "default",
                        )
                        native_candidate, config_cached = cached_native(
                            candidate_config, configs_cache / f"{config_id}.cfg",
                            str(candidate_metadata.get("sha256", "")),
                        )
                    diff = "".join(difflib.unified_diff(
                        native_default.splitlines(keepends=True),
                        native_candidate.splitlines(keepends=True),
                        fromfile=f"build/{build_id}/default-native.cfg",
                        tofile=f"config/{config_id}/native.cfg",
                    ))
                    self._json(HTTPStatus.OK, {
                        "build_id": build_id, "config_id": config_id,
                        "build_identity": build_identity,
                        "default_native": native_default,
                        "config_native": native_candidate,
                        "diff": diff,
                        "identical": native_default == native_candidate,
                        "normalizer": "smithproxy CLI: save config",
                        "cache": {
                            "default_hit": default_cached,
                            "config_hit": config_cached,
                            "directory": str(cache_root),
                        },
                    })
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "debug":
                try:
                    item = manager.get(parts[2])
                    if not item:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    if not builder or builder.artifact(item.build_id).get("build_type") != "Debug":
                        raise ConfigError("gdbserver attach is allowed only for an archived Debug build")
                    item = manager.start_debug(parts[2])
                    self._json(HTTPStatus.OK, asdict(item))
                except ConfigError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "restart":
                try:
                    item = manager.restart(parts[2])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "not found"}
                    )
                except ConfigError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if (len(parts) == 5 and parts[:2] == ["v1", "instances"]
                    and parts[3:] == ["config", "preview"] and config_previews):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 0 or length > max_body:
                        raise ConfigError("invalid instance config preview size")
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    instance, before, native = manager.save_live_config(parts[2])
                    if instance.config_id != "unknown":
                        preview_assets = config_library.resolve_assets(instance.config_id)
                    else:
                        preview_assets = builder.resolve_binary(instance.build_id).parent / "smithproxy.assets"
                    runtime_assets = manager.runtime_root / instance.id / "smithproxy.assets"
                    if runtime_assets.is_dir():
                        preview_assets = runtime_assets
                    document = config_previews.create({
                        "action": "create", "config_id": "",
                        "name": str(payload.get("name", f"Instance {instance.id[:12]}"))[:128],
                        "description": str(payload.get("description", ""))[:1000],
                        "profile": instance.profile,
                        "requested_content": before, "native_content": native,
                        "source_sha256": hashlib.sha256(before.encode()).hexdigest(),
                        "native_sha256": hashlib.sha256(native.encode()).hexdigest(),
                        "normalized_build_id": instance.build_id,
                        "source_instance": instance.id,
                    }, assets=preview_assets)
                    diff = "".join(difflib.unified_diff(
                        before.splitlines(keepends=True), native.splitlines(keepends=True),
                        fromfile=f"instance/{instance.id}/before-save.cfg",
                        tofile=f"instance/{instance.id}/after-save.cfg",
                    ))
                    artifact = builder.artifact(instance.build_id) if builder else {}
                    self._json(HTTPStatus.OK, {
                        "preview_id": document["preview_id"],
                        "expires_at": document["expires_at"], "action": "create",
                        "source_sha256": document["source_sha256"],
                        "native_sha256": document["native_sha256"],
                        "normalized_build_id": instance.build_id,
                        "diff": diff, "changed": before != native,
                        "cache_hit": False,
                        "normalizer": {
                            "commit_id": artifact.get("commit_id", ""),
                            "ref": artifact.get("ref", "instance"),
                            "build_type": artifact.get("build_type", ""),
                        },
                    })
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "cert-bundles"] and cert_library:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid certificate bundle request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    item = cert_library.create(
                        str(payload.get("name", "")),
                        str(payload.get("common_name", "")),
                        int(payload.get("days", 3650)),
                    )
                    self._json(HTTPStatus.CREATED, item)
                except (ConfigError, BackendError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "cert-bundles"]
                    and parts[3] == "certificates" and cert_library):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_config_body:
                        raise ConfigError("invalid certificate request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    action = str(payload.get("action", "generate"))
                    if action == "generate":
                        item = cert_library.generate_certificate(
                            parts[2], str(payload.get("name", "")),
                            str(payload.get("common_name", "")),
                            int(payload.get("days", 825)),
                            str(payload.get("file_stem", "")),
                        )
                    elif action == "import":
                        item = cert_library.import_certificate(
                            parts[2], str(payload.get("name", "")),
                            str(payload.get("content", "")),
                            str(payload.get("filename", "")),
                        )
                    else:
                        raise ConfigError("certificate action must be generate or import")
                    self._json(HTTPStatus.CREATED, item)
                except (ConfigError, BackendError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "runtime-profiles"] and builder and config_library and runtime_profiles:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid runtime profile request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    build_id = str(payload.get("build_id", ""))
                    config_id = str(payload.get("config_id", ""))
                    if build_id == "active":
                        raise ConfigError("runtime profiles must use an archived build")
                    builder.resolve_binary(build_id)
                    selected_config = config_library.get(config_id)
                    if not selected_config.get("native"):
                        raise ConfigError("runtime profiles require an approved native config")
                    cert_bundle_id = str(payload.get("cert_bundle_id", ""))
                    auto_restart = payload.get("auto_restart", False)
                    if not isinstance(auto_restart, bool):
                        raise ConfigError("auto_restart must be a boolean")
                    ttl_seconds = runtime_profile_ttl(payload)
                    if cert_bundle_id:
                        if not cert_library:
                            raise ConfigError("certificate bundles are unavailable")
                        cert_library.get(cert_bundle_id)
                    item = runtime_profiles.create(
                        str(payload.get("name", "")), build_id, config_id, cert_bundle_id,
                        auto_restart, ttl_seconds,
                    )
                    self._json(HTTPStatus.CREATED, profile_view(item))
                except (ConfigError, BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "configs"] and builder and config_library:
                self._json(HTTPStatus.CONFLICT, {
                    "error": "direct config imports are disabled; use /v1/configs/preview then /v1/configs/commit"
                })
                return
            if parts == ["v1", "build"] and builder:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    ref = str(payload.get("ref", "master"))
                    build_type = str(payload.get("build_type", "Release"))

                    def build_task() -> dict:
                        builder.start(ref, build_type)
                        state = wait_for_build()
                        revision = str(state.get("revision", ""))
                        artifact = next((
                            item for item in builder.artifacts()
                            if item.get("commit_id") == revision
                            and item.get("ref") == ref
                            and item.get("build_type", "Release") == build_type
                        ), None)
                        if not artifact:
                            raise BackendError(
                                "completed build artifact is unavailable for default config import"
                            )
                        default_config = store_build_default_config(
                            builder, config_library, normalize_native,
                            str(artifact.get("build_id", artifact.get("commit_id", ""))),
                        )
                        return {**state, "default_config": default_config}

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "build", f"Build {ref} ({build_type})",
                        f"build:{ref}:{build_type}", "build", build_task,
                    ))
                except (BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "cli":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if payload.get("action") == "close":
                        manager.close_cli(parts[2], payload.get("session_id", ""))
                        self._json(HTTPStatus.OK, {"closed": True})
                    else:
                        self._json(HTTPStatus.OK, manager.cli(
                            parts[2], payload.get("input", ""), payload.get("session_id", "")
                        ))
                except (ConfigError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "instances"]
                    and parts[3] == "extend"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    additional = payload.get("additional_seconds")
                    if isinstance(additional, bool):
                        raise ConfigError("additional_seconds must be an integer")
                    additional = int(additional)

                    def extend_task() -> dict:
                        return asdict(manager.extend(parts[2], additional))

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "instance-extend", f"Prodloužit instanci {parts[2][:12]} o {additional} s",
                        f"extend:{parts[2]}:{additional}", f"instance:{parts[2]}", extend_task,
                    ))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 5 and parts[:2] == ["v1", "instances"]
                    and parts[3:] == ["config", "save"] and builder and config_library):
                self._json(HTTPStatus.CONFLICT, {
                    "error": "direct instance config imports are disabled; use config/preview then configs/commit"
                })
                return
            if parts != ["v1", "instances"]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > max_body:
                    raise ConfigError("invalid request size")
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ConfigError("request body must be an object")

                def spawn_task() -> dict:
                    if builder:
                        effective_payload = dict(payload)
                        runtime_profile_id = str(payload.get("runtime_profile_id", ""))
                        if runtime_profile_id:
                            if not runtime_profiles:
                                raise ConfigError("runtime profiles are unavailable")
                            binding = runtime_profiles.get(runtime_profile_id)
                            effective_payload["build_id"] = binding["build_id"]
                            effective_payload["config_id"] = binding["config_id"]
                            effective_payload["cert_bundle_id"] = binding.get("cert_bundle_id", "")
                            effective_payload["auto_restart"] = bool(binding.get("auto_restart", False))
                            profile_ttl = binding.get("ttl_seconds", 1800)
                            effective_payload["runtime_seconds"] = (
                                0 if profile_ttl is None else profile_ttl
                            )
                        requested_build = str(effective_payload.get("build_id", "active"))
                        requested_config = str(effective_payload.get("config_id", "active"))
                        binary = builder.resolve_binary(requested_build)
                        if requested_config == "active" and config_library:
                            raise ConfigError(
                                "active raw configuration cannot be spawned; select an approved native config"
                            )
                        if config_library and requested_config != "active":
                            selected_config = config_library.get(requested_config)
                            if not selected_config.get("native"):
                                raise ConfigError("instance spawn requires an approved native config")
                            effective_payload["profile"] = selected_config.get("profile", "custom")
                        config = builder.resolve_config(requested_config)
                        assets = builder.resolve_config_assets(requested_config)
                        builder.validate_config_text(
                            config.read_text(encoding="utf-8"), requested_build, assets
                        )
                        cert_bundle_id = str(effective_payload.get("cert_bundle_id", ""))
                        certs = cert_library.resolve(cert_bundle_id) if cert_bundle_id and cert_library else None
                        if cert_bundle_id and not cert_library:
                            raise BackendError("certificate bundles are unavailable")
                        item = manager.create(
                            effective_payload, binary, config, assets_dir=assets,
                            cert_bundle_dir=certs,
                        )
                    else:
                        item = manager.create(payload)
                    return asdict(item)

                canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
                source = str(payload.get("source_ip", "unknown"))
                task = submit_task(
                    "instance-spawn", f"Spustit instanci {source} / {payload.get('user_id', '')}",
                    "spawn:" + hashlib.sha256(canonical.encode()).hexdigest(),
                    f"source:{source}", spawn_task,
                )
                self._json(HTTPStatus.ACCEPTED, task)
                return
            except (ConfigError, json.JSONDecodeError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            except BackendError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

        def do_DELETE(self) -> None:
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            parts = self._path()
            if len(parts) == 3 and parts[:2] == ["v1", "test-drives"] and test_drives:
                try:
                    item = test_drives.destroy(parts[2])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "test drive not found"}
                    )
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["v1", "builds"] and builder:
                build_id = parts[2]
                usage = build_usage(build_id)
                if usage["instances"] or usage["runtime_profiles"] or usage["test_drives"]:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "archived binary is referenced by an active instance, test drive, or runtime profile",
                        "usage": usage,
                    })
                    return
                try:
                    item = builder.delete_artifact(build_id)
                    self._json(HTTPStatus.OK, item) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "not found"}
                    )
                except BackendError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:3] == ["v1", "configs", "previews"]
                    and config_previews):
                try:
                    config_previews.get(parts[3])
                    config_previews.delete(parts[3])
                    self._json(HTTPStatus.OK, {"preview_id": parts[3], "cancelled": True})
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "debug":
                try:
                    item = manager.stop_debug(parts[2])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "not found"}
                    )
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["v1", "runtime-profiles"] and runtime_profiles:
                profile_id = parts[2]
                if any(item.runtime_profile_id == profile_id for item in manager.list()):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "runtime profile is referenced by an undeleted instance"
                    })
                    return
                item = runtime_profiles.delete(profile_id)
                self._json(HTTPStatus.OK, item) if item else self._json(
                    HTTPStatus.NOT_FOUND, {"error": "not found"}
                )
                return
            if len(parts) == 3 and parts[:2] == ["v1", "cert-bundles"] and cert_library:
                bundle_id = parts[2]
                if any(item.cert_bundle_id == bundle_id for item in manager.list()):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "certificate bundle is referenced by an undeleted instance"
                    })
                    return
                if runtime_profiles and any(
                    item.get("cert_bundle_id") == bundle_id for item in runtime_profiles.list()
                ):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "certificate bundle is referenced by a runtime profile"
                    })
                    return
                item = cert_library.delete(bundle_id)
                self._json(HTTPStatus.OK, item) if item else self._json(
                    HTTPStatus.NOT_FOUND, {"error": "not found"}
                )
                return
            if len(parts) == 3 and parts[:2] == ["v1", "configs"] and config_library:
                config_id = parts[2]
                if any(item.config_id == config_id for item in manager.list()):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "configuration is referenced by an undeleted instance"
                    })
                    return
                if runtime_profiles and any(
                    item.get("config_id") == config_id for item in runtime_profiles.list()
                ):
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "configuration is referenced by a runtime profile"
                    })
                    return
                item = config_library.delete(config_id)
                if item and builder:
                    for cached in builder.native_cache_dir.glob(
                        f"*/configs/{config_id}.*"
                    ):
                        try:
                            cached.unlink()
                        except OSError:
                            pass
                self._json(HTTPStatus.OK, item) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "record":
                try:
                    item = manager.delete(parts[2])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                except ConfigError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if len(parts) != 3 or parts[:2] != ["v1", "instances"]:
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            try:
                item = manager.stop(parts[2])
                self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            except BackendError as exc:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})

        def log_message(self, fmt: str, *args: object) -> None:
            print(f"{self.address_string()} {fmt % args}")

    return Handler


def reaper(manager: Manager, test_drives: TestDriveManager | None = None,
           interval_seconds: float = 2.0) -> None:
    while True:
        try:
            manager.expire_cli_sessions()
            manager.reconcile_orphans()
            manager.list()
            manager.cleanup_stopped()
            if test_drives:
                test_drives.list()
        except Exception as exc:
            print(f"instance reconciliation failed: {exc}")
        time.sleep(interval_seconds)


def websocket_handler_factory(manager: Manager, token: str,
                              builder: SmithproxyBuilder | None = None,
                              test_drives: TestDriveManager | None = None):
    def handler(connection: ServerConnection) -> None:
        supplied = connection.request.headers.get("Authorization", "")
        if not token or not hmac.compare_digest(supplied, f"Bearer {token}"):
            connection.close(1008, "unauthorized")
            return
        parts = [part for part in urlsplit(connection.request.path).path.split("/") if part]
        drive_terminal = len(parts) == 4 and parts[:2] == ["v1", "test-drives"]
        instance_terminal = len(parts) == 4 and parts[:2] == ["v1", "instances"]
        allowed = {"cli", "shell"} if drive_terminal else {"cli", "gdb"}
        if (not (drive_terminal or instance_terminal)
                or parts[3] not in allowed or not uuid_is_valid(parts[2])):
            connection.close(1008, "invalid terminal path")
            return
        terminal_kind = parts[3]
        try:
            if drive_terminal:
                if not test_drives:
                    raise BackendError("test drives are unavailable")
                transport = (
                    test_drives.open_shell(parts[2])
                    if terminal_kind == "shell" else test_drives.open_cli(parts[2])
                )
            elif terminal_kind == "gdb":
                if not builder:
                    raise BackendError("builder is unavailable")
                instance = manager.get(parts[2])
                if not instance:
                    raise ConfigError("instance not found")
                artifact = builder.artifact(instance.build_id)
                if artifact.get("build_type") != "Debug":
                    raise ConfigError("GDB terminal requires an archived Debug build")
                transport = manager.open_gdb_transport(
                    parts[2], builder.resolve_binary(instance.build_id),
                )
            else:
                transport = manager.open_cli_transport(parts[2])
        except (ConfigError, BackendError) as exc:
            connection.close(1011, str(exc)[:120])
            return

        finished = threading.Event()

        def copy_output() -> None:
            try:
                while not finished.is_set():
                    transport.settimeout(0.25)
                    if terminal_kind == "cli":
                        output, closed = manager._cli_read(transport)
                    else:
                        try:
                            output, closed = transport.recv(65536), False
                            if not output:
                                closed = True
                        except TimeoutError:
                            output, closed = b"", False
                    if output:
                        connection.send(output.decode("utf-8", "replace"))
                    if closed:
                        print(f"{terminal_kind} transport closed for instance {parts[2]}")
                        connection.close(1000, f"{terminal_kind} terminal closed")
                        break
            except Exception as exc:
                if not finished.is_set():
                    print(f"{terminal_kind} output failed for instance {parts[2]}: {exc}")
            finally:
                finished.set()
                try:
                    connection.close()
                except Exception:
                    pass

        reader = threading.Thread(
            target=copy_output, daemon=True,
            name=f"{terminal_kind}-output-{parts[2][:8]}",
        )
        reader.start()
        try:
            for message in connection:
                if finished.is_set():
                    break
                data = message if isinstance(message, bytes) else message.encode("utf-8")
                if len(data) > 64 * 1024:
                    connection.close(1009, "terminal input too large")
                    break
                transport.sendall(data)
        except Exception as exc:
            if not finished.is_set():
                print(f"{terminal_kind} input failed for instance {parts[2]}: {exc}")
        finally:
            finished.set()
            if drive_terminal and terminal_kind == "shell" and test_drives:
                test_drives.close_shell(parts[2], transport)
            elif terminal_kind == "gdb":
                manager.close_gdb_transport(parts[2], transport)
            else:
                transport.close()
            reader.join(timeout=2)

    return handler


def main() -> None:
    token = os.environ.get("CZ_RUNNER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CZ_RUNNER_TOKEN must contain at least 32 characters")
    network_settings = NetworkSettings(
        Path(os.environ.get(
            "CZ_RUNNER_NETWORK_SETTINGS", "/var/lib/capture-zone-runner/network-settings.json"
        )),
        Path(os.environ.get(
            "CZ_RUNNER_NETWORK_ALLOCATIONS", "/var/lib/capture-zone-runner/network-allocations.json"
        )),
    )
    namespace_backend = NamespaceBackend(
        os.environ.get("CZ_RUNNER_SMITHPROXY", "/usr/local/lib/capture-zone/smithproxy"),
        network_settings,
    )
    manager = Manager(
        Path(os.environ.get("CZ_RUNNER_STATE_DIR", "/var/lib/capture-zone-runner")),
        Path(os.environ.get(
            "CZ_RUNNER_RUNTIME_DIR", "/run/capture-zone-runner/instances",
        )),
        Path(os.environ.get("CZ_RUNNER_TEMPLATE", "/etc/capture-zone-runner/smithproxy.cfg.in")),
        namespace_backend,
        max_runtime=int(os.environ.get("CZ_RUNNER_MAX_RUNTIME", "3600")),
        max_total_runtime=int(os.environ.get("CZ_RUNNER_MAX_TOTAL_RUNTIME", "86400")),
        max_instances=int(os.environ.get("CZ_RUNNER_MAX_INSTANCES", "32")),
        sources_path=Path(os.environ.get("CZ_RUNNER_SOURCES", "/etc/capture-zone-runner/source-ips.json")),
        stopped_retention_seconds=int(os.environ.get("CZ_RUNNER_STOPPED_RETENTION", "10800")),
        config_archive_dir=Path(os.environ.get(
            "CZ_RUNNER_INSTANCE_CONFIG_ARCHIVE",
            "/var/lib/capture-zone-runner/instance-config-archive",
        )),
    )
    config_library = ConfigLibrary(Path(os.environ.get(
        "CZ_RUNNER_CONFIG_LIBRARY", "/var/lib/capture-zone-runner/config-library"
    )))
    runtime_profiles = RuntimeProfileLibrary(Path(os.environ.get(
        "CZ_RUNNER_RUNTIME_PROFILES", "/var/lib/capture-zone-runner/runtime-profiles.json"
    )))
    cert_library = CertBundleLibrary(Path(os.environ.get(
        "CZ_RUNNER_CERT_LIBRARY", "/var/lib/capture-zone-runner/cert-library"
    )))
    config_previews = ConfigPreviewLibrary(Path(os.environ.get(
        "CZ_RUNNER_CONFIG_PREVIEWS", "/var/lib/capture-zone-runner/config-previews"
    )))
    builder = SmithproxyBuilder(
        Path(os.environ.get("CZ_RUNNER_SOURCE_DIR", "/var/lib/capture-zone-runner/smithproxy-src")),
        Path(os.environ.get("CZ_RUNNER_SMITHPROXY", "/usr/local/lib/capture-zone/smithproxy")),
        os.environ.get("CZ_RUNNER_REPOSITORY", "https://github.com/astibal/smithproxy.git"),
        int(os.environ["CZ_RUNNER_BUILD_JOBS"]) if os.environ.get("CZ_RUNNER_BUILD_JOBS") else None,
        config_library,
        Path(os.environ.get(
            "CZ_RUNNER_NATIVE_CACHE", "/var/lib/capture-zone-runner/native-config-cache"
        )),
    )
    builder.start_ref_refresh(int(os.environ.get("CZ_RUNNER_REF_REFRESH_SECONDS", "300")))
    tasks = TaskQueue(
        Path(os.environ.get(
            "CZ_RUNNER_TASK_STATE", "/var/lib/capture-zone-runner/tasks.json"
        )),
        workers=int(os.environ.get("CZ_RUNNER_TASK_WORKERS", "4")),
    )
    test_drives = TestDriveManager(
        Path(os.environ.get(
            "CZ_RUNNER_TEST_DRIVE_STATE", "/var/lib/capture-zone-runner/test-drives"
        )),
        Path(os.environ.get(
            "CZ_RUNNER_TEST_DRIVE_RUNTIME", "/run/capture-zone-runner/instances"
        )),
        namespace_backend,
        default_ttl=int(os.environ.get("CZ_RUNNER_TEST_DRIVE_TTL", "1800")),
        max_ttl=int(os.environ.get("CZ_RUNNER_TEST_DRIVE_MAX_TTL", "7200")),
        expired_retention_seconds=int(os.environ.get(
            "CZ_RUNNER_TEST_DRIVE_RETENTION", "10800"
        )),
    )
    # Make portal-owned processes visible even when their state record was lost.
    # Discovery is intentionally non-destructive; an administrator decides
    # whether an orphaned unit should be stopped.
    manager.reconcile_orphans()
    test_drives.cleanup_orphans()
    host = os.environ.get("CZ_RUNNER_HOST", "127.0.0.1")
    port = int(os.environ.get("CZ_RUNNER_PORT", "9080"))
    ws_port = int(os.environ.get("CZ_RUNNER_WS_PORT", "9081"))
    threading.Thread(
        target=reaper, args=(manager, test_drives), daemon=True, name="instance-reaper"
    ).start()
    server = ThreadingHTTPServer((host, port), handler_factory(
        manager, token, builder=builder, config_library=config_library,
        runtime_profiles=runtime_profiles,
        cert_library=cert_library,
        config_previews=config_previews,
        tasks=tasks,
        network_settings=network_settings,
        test_drives=test_drives,
    ))
    ws_server = serve(
        websocket_handler_factory(manager, token, builder, test_drives), host, ws_port,
        compression=None, max_size=64 * 1024, server_header="CaptureZoneRunner/0.1",
    )
    threading.Thread(target=ws_server.serve_forever, daemon=True, name="runner-websocket").start()

    def request_shutdown(_signum, _frame) -> None:
        # BaseServer.shutdown must be called from a thread other than serve_forever.
        threading.Thread(target=server.shutdown, daemon=True, name="runner-shutdown").start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        server.serve_forever()
    finally:
        ws_server.shutdown()
        manager.shutdown(
            stop_instances=os.environ.get("CZ_RUNNER_STOP_INSTANCES_ON_EXIT", "0") == "1"
        )
        server.server_close()


if __name__ == "__main__":
    main()
