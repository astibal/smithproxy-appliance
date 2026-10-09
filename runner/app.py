from __future__ import annotations

from .restart_policy import flags as restart_flags
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
import socket
import threading
import time
import uuid
import tempfile
from .program_artifacts import ProgramArtifacts
from .runtime_profiles import elf_settings
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, parse_qs
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from websockets.sync.server import ServerConnection, serve

from .config import ConfigError, PARAMETERS, TOKEN_RE, render_template, validate_parameters
from .systemd import BackendError
from .namespace import NamespaceBackend
from .builder import SmithproxyBuilder
from .tuntom_builder import TuntomBuilder
from .config_library import ConfigLibrary
from .runtime_profiles import RuntimeProfileLibrary
from .cert_library import CertBundleLibrary
from .config_previews import ConfigPreviewLibrary
from .task_queue import TaskQueue
from .network_settings import NetworkSettings
from .test_drive import TestDriveManager
from .instance_layout import ensure_type_link, prepare_layout, remove_type_link
from .firewall import FirewallManager
from .network_profiles import NetworkProfileLibrary
from .l2_segments import L2Segments
from .wiring import overlaps, bindings
from .qemu_images import QemuImageLibrary
from .appliance_exports import ApplianceExportLibrary
from .headless_endpoints import HeadlessEndpointLibrary
from .deployments import atomic_json, boot_id, acquire_runner_lock, notify_systemd
from .microservices import Microservices, SystemdServices, ServiceError
from .system_start import SystemStart
from .snapshots import SnapshotManager
from . import runtime_images


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
    slice_unit: str = ""
    slice_rss_bytes: int = 0
    members: list[dict[str, Any]] = field(default_factory=list)
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
    application: str = 'smithproxy'
    wiring: list[dict] = field(default_factory=list)
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
    source_ips: list[str] = field(default_factory=list)
    socks_port: int = 1080
    http_port: int = 3128
    tls_port: int = 50443
    plaintext_port: int = 50080
    ingress_network_profile_id: str = ""
    egress_network_profile_id: str = ""
    network_egress_driver: str = "split-veth"
    network_ingress_driver: str = "authorized-veth"
    tuntom_build_id: str = ""
    headless_endpoint_id: str = ""
    filesystem_mode: str = "host"
    desired_state: str = ""
    boot_id: str = ""
    deployment_pending: bool = False
    recovery_attempts: int = 0
    recovery_after: float = 0
    deadline_timer: str = ""
    alias: str = ""
    indicate_old_build: bool = True
    previous_build_id: str = ""
    upgraded_at: str = ""
    upgrade_count: int = 0
    program_artifact_id: str = ""
    snapshot_id: str = ""
    snapshot_path: list[str] = field(default_factory=list)
    previous_components: dict[str, str] = field(default_factory=dict)


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
        self.boot_id = boot_id()
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
        self.netns_sessions: dict[str, Any] = {}
        self.microservices = None
        self.system_start = None
        self.l2_segments = None
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_index = prepare_layout(runtime_root, "managed")
        self.config_archive_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _remove_runtime(self, instance_id: str) -> None:
        if self.microservices:
            self.microservices.stop_instance(instance_id)
        self.close_netns_transport(instance_id)
        instance = self._load(instance_id)
        if instance and instance.deadline_timer and hasattr(self.backend, "cancel_deadline"):
            self.backend.cancel_deadline(instance_id, instance.deadline_timer)
        shutil.rmtree(self.runtime_root / instance_id, ignore_errors=True)
        remove_type_link(self.runtime_root, "managed", instance_id)

    def _state_path(self, instance_id: str) -> Path:
        return self.state_dir / f"{instance_id}.json"

    def _config_path(self, instance_id: str) -> Path:
        return self.state_dir / f"{instance_id}.cfg"

    def _save(self, instance: Instance) -> None:
        atomic_json(self._state_path(instance.id), asdict(instance))

    def _deployment_path(self, instance_id: str) -> Path:
        return self.state_dir / "deployments" / f"{instance_id}.json"

    def _load_deployment(self, instance_id: str) -> dict[str, Any]:
        try:
            document = json.loads(self._deployment_path(instance_id).read_text())
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError("instance deployment manifest is unavailable") from exc
        if document.get("schema") != 1 or not isinstance(document.get("start_options"), dict):
            raise BackendError("unsupported deployment manifest schema")
        return document

    def _schedule_deadline(self, instance: Instance) -> None:
        if hasattr(self.backend, "schedule_deadline"):
            instance.deadline_timer = self.backend.schedule_deadline(
                instance.id, instance.deadline, instance.deadline_timer)

    def _recover(self, instance: Instance) -> Instance:
        """Recreate a missing deployment without re-rendering its live config."""
        if time.time() < instance.recovery_after:
            return instance
        instance.state = "recovering"
        instance.members = []
        instance.slice_rss_bytes = 0
        instance.deployment_pending = True
        instance.recovery_attempts += 1
        instance.recovery_after = time.time() + min(300, 5 * 2 ** min(instance.recovery_attempts, 6))
        self._save(instance)
        try:
            document = json.loads(self._deployment_path(instance.id).read_text())
            if document.get("schema") != 1:
                raise BackendError("unsupported deployment manifest schema")
            if not document.get('wiring_reserved', True):
                if instance.wiring:
                    if not self.l2_segments:
                        raise BackendError('Wiring unavailable during recovery')
                    self.l2_segments.reserve_instance(instance.id, instance.wiring)
                document['wiring_reserved'] = True
                atomic_json(self._deployment_path(instance.id), document)
            config = self.runtime_root / instance.id / "smithproxy.cfg"
            if not config.is_file():
                raise BackendError("deployment workspace/config missing; refusing to regenerate it")
            options = dict(document["start_options"])
            options["preserve_allocations_on_failure"] = True
            remaining = (max(1, int((datetime.fromisoformat(instance.deadline) -
                                     datetime.now(timezone.utc)).total_seconds()))
                         if instance.deadline else 0)
            elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(instance.created_at)).total_seconds()
            options["hard_runtime_seconds"] = (max(1, int(document["start_options"]["hard_runtime_seconds"] - elapsed))
                                               if instance.deadline else 0)
            # The old host boot is gone, or an interrupted start has no live unit.
            # Only remove Smithproxy's private PID file, never its work/config.
            (config.parent / "run" / "smithproxy.default.pid").unlink(missing_ok=True)
            instance.unit = self.backend.start(instance.id, config, remaining, **options)
            for source in instance.source_ips:
                if source != instance.source_ip:
                    self.backend.attach_source(instance.id, source, instance.profile,
                                               instance.socks_port, instance.http_port,
                                               instance.tls_port, instance.plaintext_port)
            self._schedule_deadline(instance)
            instance.boot_id = self.boot_id
            instance.deployment_pending = False
            instance.recovery_after = 0
            instance.state = "starting"
            instance.result = "deployment-restored"
            instance.resources_cleaned = False
        except (OSError, ValueError, TypeError, KeyError, BackendError) as exc:
            instance.result = f"deployment recovery deferred: {exc}"
        self._save(instance)
        return instance

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
            document = json.loads(self._state_path(instance_id).read_text(encoding="utf-8"))
            # One-way state migration: keep live local units visible through
            # the first restart after moving from one PID to a Slice model.
            legacy_pid = int(document.pop("pid", 0) or 0)
            legacy_rss = int(document.pop("rss_bytes", 0) or 0)
            document.pop("processes", None)
            allowed = Instance.__dataclass_fields__
            instance = Instance(**{
                key: value for key, value in document.items() if key in allowed
            })
            if legacy_pid and not instance.members:
                instance.members = [{
                    "role": "smithproxy", "unit": instance.unit,
                    "pid": legacy_pid, "state": instance.state,
                    "rss_bytes": legacy_rss,
                }]
                instance.slice_rss_bytes = legacy_rss
            if not instance.source_ips and instance.source_ip:
                instance.source_ips = [instance.source_ip]
            return instance
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _member_pid(instance: Instance, role: str = "smithproxy") -> int:
        return next((
            int(item.get("pid", 0)) for item in instance.members
            if item.get("role") == role
        ), 0)

    def _reconcile(self, instance: Instance) -> Instance:
        status = self.backend.status(instance.unit)
        previous_smithproxy_pid = next((
            int(item.get("pid", 0)) for item in instance.members
            if item.get("role") == "smithproxy"
        ), 0)
        deadline = datetime.fromisoformat(instance.deadline) if instance.deadline else None
        expired = bool(deadline and datetime.now(timezone.utc) >= deadline)
        if (instance.desired_state == "stopped" and
                status.active_state in {"active", "activating", "reloading"}):
            # Complete an explicit stop interrupted between intent and systemctl.
            return self.stop(instance.id) or instance
        if (instance.desired_state == "running" and not expired
                and status.active_state not in {"active", "activating", "reloading", "deactivating"}
                and (instance.boot_id != self.boot_id or instance.deployment_pending)):
            return self._recover(instance)
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
            instance.desired_state = "stopped"
            instance.stopped_at = datetime.now(timezone.utc).isoformat()
            instance.members = []
            instance.slice_rss_bytes = 0
            instance.result = "ttl-expired"
            instance.resources_cleaned = True
            self._save(instance)
            return instance
        if status.active_state in {"active", "activating", "reloading"}:
            if instance.deployment_pending:
                for source in instance.source_ips:
                    if source != instance.source_ip:
                        self.backend.attach_source(instance.id, source, instance.profile,
                                                   instance.socks_port, instance.http_port,
                                                   instance.tls_port, instance.plaintext_port)
            self._schedule_deadline(instance)
            instance.boot_id = self.boot_id
            instance.deployment_pending = False
            instance.state = "orphaned" if instance.state == "orphaned" else "running"
            instance.stopped_at = ""
            instance.slice_unit = getattr(
                self.backend, "slice_name", lambda value: f"capture-zone-slice-{value}.slice"
            )(instance.id)
            instance.members = getattr(
                self.backend, "slice_processes",
                lambda _id, unit: [{
                    "role": "smithproxy", "unit": unit,
                    "pid": status.main_pid, "state": status.active_state,
                    "substate": status.sub_state, "result": status.result,
                    "rss_bytes": self.backend.rss_bytes(status.main_pid)
                    if status.main_pid else 0,
                }],
            )(instance.id, instance.unit)
            instance.slice_rss_bytes = sum(
                int(item.get("rss_bytes", 0)) for item in instance.members
            )
            instance.resources_cleaned = False
        else:
            previous_pid = previous_smithproxy_pid
            instance.members = []
            instance.slice_rss_bytes = 0
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
            if (failed and instance.auto_restart is True and status.result != "start-limit-hit"
                    and instance.restart_count < 5
                    and instance.desired_state != "stopped"
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
            if failed and instance.desired_state == "running" and not expired:
                # Retain the deployment for diagnosis and the next host boot.
                # Same-boot retries remain governed by auto_restart and its limit.
                instance.state = "failed"
                instance.result = status.result
                self._save(instance)
                return instance
            if instance.state not in {"stopped", "failed", "expired"}:
                instance.state = "expired" if deadline and datetime.now(timezone.utc) >= deadline else (
                    "stopped" if status.result in {"success", ""} else "failed"
                )
            if expired:
                instance.state = "expired"
            if instance.state in {"stopped", "failed", "expired"} and not instance.stopped_at:
                instance.stopped_at = datetime.now(timezone.utc).isoformat()
            instance.result = status.result
            instance.desired_state = "stopped"
            if not instance.resources_cleaned:
                # Never remove a workspace until systemd confirmed the stop.
                self._save(instance)
                self.backend.stop(instance.unit)
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
                    try:
                        item = self._reconcile(item)
                    except (BackendError, OSError, ValueError, TypeError) as exc:
                        # One broken deployment must not prevent TTL checks and
                        # recovery of all other deployments.
                        print(f"instance {item.id} reconciliation deferred: {exc}")
                    result.append(item)
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

    def location(self, instance_id: str) -> dict | None:
        """Read-only host coordinates; never infer paths from unchecked input."""
        if not uuid_is_valid(instance_id):
            instance_id = Manager.resolve_alias(self, instance_id)
            if not instance_id:
                return None
        item = self.peek(instance_id)
        if not item:
            return None
        work = self.runtime_root / item.id
        namespace_path = f"/run/netns/{item.namespace}" if item.namespace else ""
        transport_namespace = ""
        if getattr(item, "network_ingress_driver", "authorized-veth") == "unlimited-veth" and hasattr(self.backend, "ingress_allocation"):
            link = self.backend.ingress_allocation(item.id)
            if link.topology == "unlimited-veth":
                transport_namespace = link.namespace
        transport_path = f"/run/netns/{transport_namespace}" if transport_namespace else ""
        # Installation staging is separate from the appliance-writable work
        # tree. Lookup never creates directories or enables script execution.
        microservices = self.runtime_root.parent / "microservices" / item.id
        installation_exists = (
            microservices.is_dir() and not microservices.is_symlink()
            and not microservices.parent.is_symlink()
        )
        supervisor = getattr(self, 'microservices', None)
        enabled = bool(supervisor and getattr(item, 'filesystem_mode', '') == 'rootfs')
        return {
            "id": item.id, "state": item.state, "origin": socket.gethostname(),
            "alias": getattr(item, "alias", ""),
            "namespace": item.namespace, "namespace_path": namespace_path,
            "namespace_exists": bool(namespace_path and Path(namespace_path).exists()),
            "work_dir": str(work), "work_dir_exists": work.is_dir(),
            "unit": item.unit, "slice": item.slice_unit,
            "transport_namespace": transport_namespace,
            "transport_namespace_path": transport_path,
            "transport_namespace_exists": bool(transport_path and Path(transport_path).exists()),
            "microservice_contract_version": 3,
            "microservices_dir": str(microservices),
            "microservices_dir_exists": installation_exists,
            "microservice_mode": "managed" if enabled else "installation_only",
            "microservice_execution_enabled": enabled,
            "microservice_rootfs": ({key: supervisor.backend.manifest.get(key) for key in
                ('schema', 'sha256', 'architecture', 'libc', 'runtime_uid', 'runtime_gid')}
                if supervisor else None),
            "microservice_scan_interval_seconds": supervisor.interval if supervisor else None,
            "microservice_capabilities": {
                "installation": installation_exists,
                "supervision": enabled, "status": bool(supervisor), "stop": bool(supervisor),
            },
        }

    def resolve_alias(self, value: str) -> str | None:
        if not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", value):
            return None
        matches = [item.id for item in self.snapshot() if item.alias == value]
        return matches[0] if len(matches) == 1 else None

    def check_microservices(self, instance_id: str) -> dict:
        if self.microservices:
            return self.microservices.check_instance(instance_id)
        with self.lock:
            instance = self.peek(instance_id)
            if not instance:
                raise ServiceError('instance_not_found', 404)
            if not self.system_start:
                raise ServiceError('microservices_unavailable')
            return {'instance_id': instance_id, 'state': 'checked',
                    'system_start': self.system_start.check(instance),
                    'external_microservices': 'unavailable'}

    def set_build_warning(self, instance_id: str, enabled: bool) -> Instance | None:
        if type(enabled) is not bool:
            raise ConfigError('indicate_old_build must be a boolean')
        with self.lock:
            item = self._load(instance_id)
            if item:
                item.indicate_old_build = enabled
                self._save(item)
            return item

    def set_alias(self, instance_id: str, alias: str) -> Instance | None:
        if not isinstance(alias, str) or (alias and (
            not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", alias) or uuid_is_valid(alias)
        )):
            raise ConfigError("alias must be 1–63 lowercase letters, digits or hyphens, starting with a letter; empty clears it")
        with self.lock:
            item = self._load(instance_id)
            if not item:
                return None
            if alias and any(other.alias == alias and other.id != item.id for other in self.snapshot()):
                raise ConfigError("alias already belongs to another instance")
            item.alias = alias
            self._save(item)
            return item

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
                    namespace=getattr(allocation, "namespace", ""),
                    slice_unit=getattr(
                        self.backend, "slice_name",
                        lambda value: f"capture-zone-slice-{value}.slice",
                    )(instance_id),
                    members=getattr(
                        self.backend, "slice_processes",
                        lambda _id, member_unit: [{
                            "role": "smithproxy", "unit": member_unit,
                            "pid": status.main_pid, "state": status.active_state,
                            "rss_bytes": self.backend.rss_bytes(status.main_pid)
                            if status.main_pid else 0,
                        }],
                    )(instance_id, unit),
                    resources_cleaned=False, build_id="unknown", config_id="unknown",
                    persistent=True,
                )
                instance.slice_rss_bytes = sum(
                    int(member.get("rss_bytes", 0)) for member in instance.members
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
               cert_bundle_dir: Path | None = None,
               work_installer: Callable[[Path], None] | None = None,
               rootfs_path: Path | None = None, program: dict | None = None) -> Instance:
        if not isinstance(payload, dict):
            raise ConfigError("request body must be an object")
        allowed = {
            "runtime_seconds", "parameters", "source_ip", "build_id", "config_id", "user_id",
            "rewrite_sni", "rewrite_sni_to", "profile", "template_values", "config_mode",
            "runtime_profile_id",
            "cert_bundle_id",
            "auto_restart",
            "persistent",
            "ingress_network_profile_id", "egress_network_profile_id",
            "network_egress_mode", "network_sas_interface", "network_egress_driver",
            "network_ingress_driver",
            "network_tuntom_socket", "network_tuntom_adapter",
            "network_tuntom_binary",
            "network_tuntom_build_id",
            "network_tuntom_in_prefix", "network_tuntom_out_prefix",
            "network_tuntom_admission", "network_tuntom_mtu",
            "network_tuntom_tunnel_id", "headless_endpoint_id",
            "network_runtime",
            "filesystem_mode",
            'wiring', 'system_start_enabled',
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ConfigError(f"unsupported fields: {', '.join(sorted(unknown))}")
        requested_wiring = bindings(payload.get('wiring', []))
        if requested_wiring and not self.l2_segments:
            raise ConfigError('Wiring is unavailable')
        runtime_profile_id = str(payload.get("runtime_profile_id", ""))
        runtime = payload.get("runtime_seconds", 0)
        if runtime is None:
            runtime = 0
        if isinstance(runtime, bool) or not isinstance(runtime, int):
            raise ConfigError("runtime_seconds must be an integer")
        runtime_max = self.max_total_runtime if runtime_profile_id else self.max_runtime
        if runtime != 0 and not self.min_runtime <= runtime <= runtime_max:
            raise ConfigError(f"runtime_seconds must be between {self.min_runtime} and {runtime_max}")
        parameters = validate_parameters(payload.get("parameters", {}))
        network_runtime = payload.get("network_runtime", {})
        if not isinstance(network_runtime, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in network_runtime.items()
        ):
            raise ConfigError("network_runtime must be an object of string values")
        network_ingress_driver = str(payload.get("network_ingress_driver", "authorized-veth"))
        if network_ingress_driver not in {"authorized-veth", "unlimited-veth", "none"}:
            raise ConfigError("network_ingress_driver must be authorized-veth, unlimited-veth or none")
        source_ip = str(payload.get("source_ip", "")).strip() if network_ingress_driver == "authorized-veth" else ""
        try:
            if source_ip or network_ingress_driver == "authorized-veth":
                source_ip = str(ipaddress.ip_address(source_ip))
        except ValueError as exc:
            raise ConfigError("source_ip must be an IPv4 or IPv6 address") from exc
        build_id = str(payload.get("build_id", "active"))
        config_id = str(payload.get("config_id", "active"))
        if runtime_profile_id and not uuid_is_valid(runtime_profile_id):
            raise ConfigError("runtime_profile_id must be a UUID")
        cert_bundle_id = str(payload.get("cert_bundle_id", ""))
        if cert_bundle_id and not uuid_is_valid(cert_bundle_id):
            raise ConfigError("cert_bundle_id must be a UUID")
        ingress_network_profile_id = str(payload.get("ingress_network_profile_id", ""))
        egress_network_profile_id = str(payload.get("egress_network_profile_id", ""))
        for field_name, value in (
            ("ingress_network_profile_id", ingress_network_profile_id),
            ("egress_network_profile_id", egress_network_profile_id),
        ):
            if value and not uuid_is_valid(value):
                raise ConfigError(f"{field_name} must be a UUID")
        network_egress_mode = str(payload.get("network_egress_mode", ""))
        if network_egress_mode not in {"", "masquerade", "routed"}:
            raise ConfigError("network_egress_mode must be masquerade or routed")
        network_egress_driver = str(payload.get("network_egress_driver", "split-veth"))
        if network_egress_driver not in {"split-veth", "veth-out", "on-a-stick", "tuntom-via", "none"}:
            raise ConfigError(
                "network_egress_driver must be veth-out or none (legacy stored drivers are also accepted)"
            )
        network_sas_interface = str(payload.get("network_sas_interface", ""))
        if network_sas_interface and not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", network_sas_interface):
            raise ConfigError("network_sas_interface is invalid")
        network_tuntom_socket = str(payload.get(
            "network_tuntom_socket", "/run/tuntom/via.sock"
        ))
        network_tuntom_build_id = str(payload.get("network_tuntom_build_id", ""))
        network_tuntom_adapter = str(payload.get(
            "network_tuntom_adapter", "/usr/local/bin/tuntom-divert-adapter"
        ))
        network_tuntom_binary = str(payload.get(
            "network_tuntom_binary", "/usr/local/bin/tuntom"
        ))
        network_tuntom_in_prefix = str(payload.get(
            "network_tuntom_in_prefix", "proxy-in-"
        ))
        network_tuntom_out_prefix = str(payload.get(
            "network_tuntom_out_prefix", "proxy-out-"
        ))
        network_tuntom_admission = str(payload.get(
            "network_tuntom_admission", "immediate"
        ))
        headless_endpoint_id = str(payload.get("headless_endpoint_id", ""))
        if headless_endpoint_id and not uuid_is_valid(headless_endpoint_id):
            raise ConfigError("headless_endpoint_id must be a UUID")
        try:
            network_tuntom_tunnel_id = int(payload.get("network_tuntom_tunnel_id", 0))
        except (TypeError, ValueError) as exc:
            raise ConfigError("network_tuntom_tunnel_id must be an integer") from exc
        if network_tuntom_tunnel_id and not 1 <= network_tuntom_tunnel_id <= 255:
            raise ConfigError("network_tuntom_tunnel_id must be between 1 and 255")
        try:
            network_tuntom_mtu = int(payload.get("network_tuntom_mtu", 1500))
        except (TypeError, ValueError) as exc:
            raise ConfigError("network_tuntom_mtu must be an integer") from exc
        if network_egress_driver == "tuntom-via":
            if not re.fullmatch(r"[0-9a-f]{40,64}-(?:release|debug)", network_tuntom_build_id):
                raise ConfigError("tuntom-via requires a valid archived tuntom build id")
            if network_egress_mode not in {"", "routed"}:
                raise ConfigError("tuntom-via requires routed egress mode")
            for label, value in (
                ("network_tuntom_socket", network_tuntom_socket),
                ("network_tuntom_adapter", network_tuntom_adapter),
                ("network_tuntom_binary", network_tuntom_binary),
            ):
                candidate = Path(value)
                if not candidate.is_absolute() or ".." in candidate.parts:
                    raise ConfigError(f"{label} must be an absolute normalized path")
            if network_tuntom_admission not in {"immediate", "warmup"}:
                raise ConfigError("network_tuntom_admission is invalid")
            if not 576 <= network_tuntom_mtu <= 9000:
                raise ConfigError("network_tuntom_mtu must be between 576 and 9000")
            for label, value in (
                ("network_tuntom_in_prefix", network_tuntom_in_prefix),
                ("network_tuntom_out_prefix", network_tuntom_out_prefix),
            ):
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,32}", value):
                    raise ConfigError(f"{label} is invalid")
        filesystem_mode = str(payload.get("filesystem_mode", "host"))
        if filesystem_mode not in {"host", "rootfs"}:
            raise ConfigError("filesystem_mode must be host or rootfs")
        if filesystem_mode == "rootfs" and not rootfs_path:
            raise ConfigError("rootfs mode requires a prepared build rootfs")
        auto_restart = payload.get("auto_restart", False)
        restart_flags(auto_restart)
        persistent = payload.get("persistent", False)
        system_start_enabled = payload.get('system_start_enabled', True)
        if not isinstance(system_start_enabled, bool):
            raise ConfigError('system_start_enabled must be boolean')
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
        # Native configs extracted directly from a build are catalogued as
        # "default".  At runtime they use the same transparent dataplane as a
        # custom config; rejecting that library classification made freshly
        # extracted defaults impossible to spawn (including rootfs profiles).
        if profile not in {"default", "custom", "magic-sni", "socks", "http-proxy"}:
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
        if not source_ip and network_ingress_driver == "authorized-veth":
            raise ConfigError("source_ip is required")
        pool = self.sources()
        if source_ip and pool and source_ip not in pool:
            raise ConfigError("source_ip is not present in the configured pool")
        with self.lock:
            active = sum(i.desired_state == "running" or i.state in {"starting", "running", "orphaned"}
                         for i in self.list())
            if active >= self.max_instances:
                raise ConfigError("instance limit reached")
            if source_ip and any(source_ip in (i.source_ips or [i.source_ip])
                   and (i.desired_state == "running" or i.state in {"starting", "running", "orphaned"})
                   for i in self.list()):
                raise ConfigError("source_ip already has an active instance")
            instance_id = str(uuid.uuid4())
            runtime_dir = self.runtime_root / instance_id
            runtime_dir.mkdir(mode=0o700)
            if self.microservices:
                self.microservices.provision(instance_id)
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
                if binary_path and not program and hasattr(self.backend, "native_save"):
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
                if work_installer:
                    work_installer(runtime_dir)
                now = datetime.now(timezone.utc)
                start_options = dict(
                    source_ip=source_ip,
                    tls_port=parameters.get("tls_port", 50443),
                    plaintext_port=parameters.get("plaintext_port", 50080),
                    socks_port=parameters.get("socks_port", 1080),
                    cli_port=parameters.get("cli_port", 50000),
                    http_port=parameters.get("http_port", 3128),
                    smithproxy_binary=(str(Path(binary_path or self.backend.smithproxy_binary).resolve())
                                       if binary_path or getattr(self.backend, "smithproxy_binary", "") else ""),
                    profile=profile,
                    config_mode=config_mode,
                    assets_path=str(effective_assets) if effective_assets else "",
                    auto_restart=auto_restart,
                    hard_runtime_seconds=0 if runtime == 0 else self.max_total_runtime,
                    egress_mode=network_egress_mode,
                    egress_driver=network_egress_driver,
                    ingress_driver=network_ingress_driver,
                    sas_interface=network_sas_interface,
                    tuntom_socket=network_tuntom_socket,
                    tuntom_binary=network_tuntom_binary,
                    tuntom_adapter=network_tuntom_adapter,
                    tuntom_in_prefix=network_tuntom_in_prefix,
                    tuntom_out_prefix=network_tuntom_out_prefix,
                    tuntom_admission=network_tuntom_admission,
                    tuntom_mtu=network_tuntom_mtu,
                    tuntom_switch_ip=str(network_runtime.get("tuntom_switch_ip", "")),
                    tuntom_secret=str(network_runtime.get("tuntom_secret", "")),
                    tuntom_tunnel_id=network_tuntom_tunnel_id,
                    rootfs_path=str(rootfs_path) if rootfs_path else "",
                )
                if program:
                    start_options['program'] = program
            except Exception:
                shutil.rmtree(runtime_dir, ignore_errors=True)
                remove_type_link(self.runtime_root, "managed", instance_id)
                self._config_path(instance_id).unlink(missing_ok=True)
                raise
            instance = Instance(
                instance_id, getattr(self.backend, "unit_name", NamespaceBackend.unit_name)(instance_id),
                "starting", now.isoformat(),
                (now + timedelta(seconds=runtime)).isoformat() if runtime else "", runtime,
                source_ip=source_ip,
                namespace=f"cz-{instance_id[:8]}",
                slice_unit=getattr(
                    self.backend, "slice_name",
                    lambda value: f"capture-zone-slice-{value}.slice",
                )(instance_id),
                cli_port=0 if program else parameters.get("cli_port", 50000),
                build_id=build_id,
                config_id=config_id,
                user_id=user_id,
                rewrite_sni=rewrite_sni,
                rewrite_sni_to=rewrite_sni_to,
                profile=program['application'] if program else profile,
                template_values=template_values,
                config_mode=config_mode,
                runtime_profile_id=runtime_profile_id,
                application=program['application'] if program else 'smithproxy',
                program_artifact_id=str((program or {}).get('artifact_id', '')),
                wiring=requested_wiring,
                cert_bundle_id=cert_bundle_id,
                auto_restart=auto_restart,
                persistent=persistent,
                source_ips=[source_ip] if source_ip else [],
                socks_port=parameters.get("socks_port", 1080),
                http_port=parameters.get("http_port", 3128),
                tls_port=parameters.get("tls_port", 50443),
                plaintext_port=parameters.get("plaintext_port", 50080),
                ingress_network_profile_id=ingress_network_profile_id,
                egress_network_profile_id=egress_network_profile_id,
                network_egress_driver=network_egress_driver,
                network_ingress_driver=network_ingress_driver,
                tuntom_build_id=network_tuntom_build_id,
                headless_endpoint_id=headless_endpoint_id,
                filesystem_mode=filesystem_mode,
                desired_state="running", boot_id=self.boot_id,
                deployment_pending=True,
            )
            atomic_json(self._deployment_path(instance_id), {
                "schema": 1, "start_options": start_options, 'wiring_reserved': False,
            })
            self._save(instance)
            backend_started = False
            try:
                if requested_wiring:
                    self.l2_segments.reserve_instance(instance_id, requested_wiring)
                atomic_json(self._deployment_path(instance_id), {
                    'schema': 1, 'start_options': start_options, 'wiring_reserved': True,
                })
                if self.system_start:
                    self.system_start.configure(instance_id, system_start_enabled)
                backend_started = True
                instance.unit = self.backend.start(instance_id, config_path, runtime, **start_options)
                allocation = getattr(self.backend, "allocation", lambda _id: None)(instance_id)
                instance.namespace = getattr(allocation, "namespace", "")
                self._schedule_deadline(instance)
                instance.deployment_pending = False
                self._save(instance)
            except Exception:
                # Persist cancellation before cleanup; a service restart must not
                # turn a rejected spawn into a running deployment.
                instance.desired_state = "stopped"
                self._save(instance)
                if backend_started:
                    self.backend.stop(instance.unit)
                if requested_wiring:
                    self.l2_segments.release_instance(instance_id)
                instance.state = "failed"
                instance.resources_cleaned = True
                instance.stopped_at = datetime.now(timezone.utc).isoformat()
                self._save(instance)
                self._remove_runtime(instance_id)
                raise
            return instance

    def attach_source(self, instance_id: str, source_ip: object) -> Instance:
        try:
            source = str(ipaddress.ip_address(str(source_ip).strip()))
        except ValueError as exc:
            raise ConfigError("source must be an IPv4 or IPv6 address") from exc
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                raise ConfigError("instance not found")
            if instance.network_ingress_driver != "authorized-veth":
                raise ConfigError("source attachment requires Authorized veth")
            instance = self._reconcile(instance)
            if instance.state not in {"starting", "running", "orphaned"}:
                raise ConfigError("source can only be attached to an active instance")
            sources = [item for item in (instance.source_ips or [instance.source_ip]) if item]
            if source in sources:
                return instance
            pool = self.sources()
            if pool and source not in pool:
                raise ConfigError("source is not present in the configured pool")
            self.backend.attach_source(
                instance.id, source, instance.profile,
                instance.socks_port, instance.http_port,
                instance.tls_port, instance.plaintext_port,
            )
            instance.source_ips = [*sources, source]
            instance.result = f"source-attached:{source}"
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

    def open_netns_transport(self, instance_id: str):
        from .netns_shell import open_shell
        with self.lock:
            instance = self.get(instance_id)
            if not instance or instance.state != 'running' or not self._member_pid(instance):
                raise ConfigError('instance is not running')
            self.close_netns_transport(instance_id)
            transport = open_shell(instance.id, instance.namespace)
            self.netns_sessions[instance_id] = transport
            return transport

    def close_netns_transport(self, instance_id: str, transport=None) -> None:
        with self.lock:
            current = self.netns_sessions.get(instance_id)
            target = transport or current
            if target:
                target.close()
            if current is target:
                self.netns_sessions.pop(instance_id, None)

    def open_gdb_transport(self, instance_id: str, binary: Path):
        with self.lock:
            instance = self.get(instance_id)
            if (not instance or instance.state != "running"
                    or not self._member_pid(instance)):
                raise ConfigError("instance is not running")
            previous = self.gdb_sessions.pop(instance.id, None)
            if previous:
                previous.close()
            unit, address, port = self.backend.start_debug(
                instance.id, self._member_pid(instance)
            )
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
            self.close_netns_transport(instance_id)
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
            instance.desired_state = "stopped"
            self._save(instance)
            if not instance.resources_cleaned:
                self.backend.stop(instance.unit)
                self._clear_debug(instance)
                self._snapshot_runtime_config(instance.id)
                instance.state = "stopped"
                instance.stopped_at = datetime.now(timezone.utc).isoformat()
                instance.members = []
                instance.slice_rss_bytes = 0
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
            instance.state = "starting"
            instance.members = []
            instance.slice_rss_bytes = 0
            instance.result = "restarted"
            instance.resources_cleaned = False
            self._save(instance)
            return instance

    def upgrade(self, instance_id: str, build_id: str, binary: Path,
                rootfs_path: Path | None = None) -> Instance:
        """Compatibility wrapper for Smithproxy component upgrades."""
        if not binary.is_file():
            raise BackendError("instance upgrade binary is unavailable")
        changes = {"smithproxy_binary": str(binary.resolve())}
        if rootfs_path:
            changes["rootfs_path"] = str(rootfs_path.resolve())
        return self.upgrade_component(instance_id, "smithproxy", build_id, changes)

    def upgrade_component(self, instance_id: str, component: str, version_id: str,
                          changes: dict[str, Any]) -> Instance:
        """Replace one managed component and restart without rebuilding networking."""
        if component not in {"smithproxy", "program", "tuntom"}:
            raise ConfigError("unsupported upgrade component")
        with self.lock:
            instance = self._load(instance_id)
            if not instance:
                raise ConfigError("instance not found")
            instance = self._reconcile(instance)
            if instance.state != "running":
                raise ConfigError("only a running instance can be upgraded")
            if component == "smithproxy" and instance.application != "smithproxy":
                raise ConfigError("instance has no Smithproxy component")
            if component == "program" and instance.application != "elf":
                raise ConfigError("instance has no versioned ELF component")
            if component == "tuntom" and not instance.tuntom_build_id:
                raise ConfigError("instance has no Tuntom component")
            deployment = self._load_deployment(instance.id)
            options = dict(deployment.get("start_options", {}))
            options.update(changes)
            for session_id, session in list(self.cli_sessions.items()):
                if session.instance_id == instance_id:
                    session.connection.close()
                    del self.cli_sessions[session_id]
            self.close_netns_transport(instance_id)
            gdb = self.gdb_sessions.pop(instance_id, None)
            if gdb:
                gdb.close()
            if instance.debug_unit:
                self.backend.stop_debug(instance.id)
                self._clear_debug(instance)
            config_path = self.runtime_root / instance.id / "smithproxy.cfg"
            try:
                self.backend.upgrade_instance(
                    instance.id, config_path, **options,
                    **({"_replace_tuntom": True} if component == "tuntom" else {}),
                )
            except Exception:
                # The durable manifest still describes the old deployment.
                # Best-effort rollback keeps a failed file replacement from
                # silently turning into a stopped instance.
                try:
                    self.backend.upgrade_instance(
                        instance.id, config_path, **deployment["start_options"],
                        **({"_replace_tuntom": True} if component == "tuntom" else {}),
                    )
                except Exception as rollback:
                    instance.result = f"upgrade rollback failed: {rollback}"
                    self._save(instance)
                raise
            deployment["start_options"] = options
            atomic_json(self._deployment_path(instance.id), deployment)
            previous = self.component_versions(instance).get(component, "")
            instance.previous_components = {**instance.previous_components, component: previous}
            if component == "smithproxy":
                instance.previous_build_id = instance.build_id
                instance.build_id = version_id
            elif component == "program":
                instance.program_artifact_id = version_id
                instance.build_id = str(changes.get("rootfs_path", "")).rstrip("/").split("/")[-1]
            else:
                instance.tuntom_build_id = version_id
            instance.upgraded_at = datetime.now(timezone.utc).isoformat()
            instance.upgrade_count += 1
            instance.state = "starting"
            instance.members = []
            instance.slice_rss_bytes = 0
            instance.result = "upgraded"
            instance.resources_cleaned = False
            self._save(instance)
            return instance

    def component_versions(self, instance: Instance) -> dict[str, str]:
        result = {}
        if instance.application == "smithproxy":
            result["smithproxy"] = instance.build_id
        if instance.application == "elf" and instance.program_artifact_id:
            result["program"] = instance.program_artifact_id
        if instance.tuntom_build_id:
            result["tuntom"] = instance.tuntom_build_id
        return result

    def restart_preserved(self, instance_id: str) -> Instance:
        """Restart from the durable manifest without changing network resources."""
        instance = self._load(instance_id)
        if not instance:
            raise ConfigError("instance not found")
        deployment = self._load_deployment(instance.id)
        self.backend.upgrade_instance(
            instance.id, self.runtime_root / instance.id / "smithproxy.cfg",
            **deployment["start_options"],
            **({"_replace_tuntom": True}
               if deployment["start_options"].get("egress_driver") == "tuntom-via" else {}),
        )
        instance.state = "starting"
        instance.members = []
        instance.slice_rss_bytes = 0
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
            self._schedule_deadline(instance)
            self._save(instance)
            return instance

    def start_debug(self, instance_id: str) -> Instance | None:
        with self.lock:
            instance = self.get(instance_id)
            if not instance:
                return None
            if instance.state != "running" or not self._member_pid(instance):
                raise ConfigError("debug attach requires a running instance with a PID")
            unit, address, port = self.backend.start_debug(
                instance.id, self._member_pid(instance)
            )
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
            if (instance.desired_state == "running"
                    or instance.state in {"starting", "running", "orphaned"}
                    or any(int(item.get("pid", 0)) > 0 for item in instance.members)):
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
            self._deployment_path(instance.id).unlink(missing_ok=True)
            if self.microservices:
                self.microservices.mark_removed(instance.id)
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
                if (not instance or instance.persistent or instance.desired_state == "running"
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
                if (any(int(item.get("pid", 0)) > 0 for item in instance.members)
                        or not instance.resources_cleaned):
                    continue
                try:
                    archive = self._archive_stopped_config(instance)
                except BackendError as exc:
                    print(f"stopped instance cleanup deferred for {instance.id}: {exc}")
                    continue
                self._remove_runtime(instance.id)
                self._state_path(instance.id).unlink(missing_ok=True)
                self._config_path(instance.id).unlink(missing_ok=True)
                self._deployment_path(instance.id).unlink(missing_ok=True)
                if self.microservices:
                    self.microservices.mark_removed(instance.id)
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
                        or any(int(item.get("pid", 0)) > 0 for item in instance.members)
                        or not instance.resources_cleaned):
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
            for instance_id in list(self.netns_sessions):
                self.close_netns_transport(instance_id)
            if not stop_instances:
                return
            for instance in self.list():
                if instance.desired_state == "running" or instance.state in {"starting", "running", "orphaned"}:
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
                "post": {"summary": "Build a Git ref and optionally adopt its config and rootfs"},
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
            "/v1/appliance-exports": {
                "get": {"summary": "List portable appliance archives"},
                "post": {"summary": "Build a portable appliance archive"},
            },
            "/v1/appliance-exports/{id}": {
                "get": {"summary": "Read portable appliance metadata"},
                "delete": {"summary": "Delete a portable appliance archive"},
            },
            "/v1/appliance-exports/{id}/download": {
                "get": {"summary": "Stream a portable appliance tar.gz"},
            },
            "/v1/builds/{id}": {
                "delete": {"summary": "Delete an unused archived binary"},
            },
            "/v1/builds/{id}/config/preview": {
                "post": {"summary": "Extract and native-save an archived build default config"},
            },
            "/v1/builds/{id}/rootfs": {
                "post": {"summary": "Prepare the immutable rootfs image for an archived build"},
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
            "/v1/program-artifacts": {
                "get": {"summary": "List immutable ELF program artifacts", "responses": {"200": {"description": "Artifact list"}}},
                "post": {"summary": "Queue an ELF import (maximum 16 MiB)",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {
                        "type": "object", "required": ["name"], "properties": {
                            "name": {"type": "string"}, "version": {"type": "string"},
                            "path": {"type": "string", "description": "Absolute path on origin; mutually exclusive with content_base64"},
                            "content_base64": {"type": "string"}, "filename": {"type": "string"}
                        }}}}}, "responses": {"202": {"description": "Queued import task"}, "400": {"description": "Invalid request"}}}
            },
            "/v1/runtime-profiles/{id}": {
                "get": {"summary": "Read one runtime profile and its usage"},
                "put": {"summary": "Update a binary and configuration binding"},
                "delete": {"summary": "Delete an unused runtime profile"},
            },
            "/v1/l2-segments": {
                "get": {"summary": "List isolated virtual cables and switches"},
                "post": {"summary": "Queue segment creation; returns a task (202)"},
            },
            "/v1/l2-segments/{id}": {
                "get": {"summary": "Read segment reservations and observed status"},
                "delete": {"summary": "Queue deletion of an empty segment"},
            },
            "/v1/l2-segments/{id}/endpoints": {
                "post": {"summary": "Queue a managed instance veth reservation and attachment"},
            },
            "/v1/l2-segments/{id}/endpoints/{endpoint_id}": {
                "delete": {"summary": "Queue disconnection and reservation release"},
            },
            '/v1/l2-segments/{id}/endpoints/{endpoint_id}/addressing': {
                'post': {'summary': 'Queue desired port addressing update and per-instance 00-start check'},
            },
            '/v1/l2-segments/addressing': {
                'get': {'summary': 'Wiring IPv4/IPv6 inventory and prefix tree; no discovery'},
            },
            '/v1/l2-segments/addressing/preview': {
                'post': {'summary': 'Read-only overlap warnings for proposed addresses'},
            },
            "/v1/network-profiles": {
                "get": {"summary": "List ingress and egress network profiles"},
                "post": {"summary": "Create an ingress or egress network profile"},
            },
            "/v1/network-profiles/{id}": {
                "get": {"summary": "Read one network profile and its usage"},
                "put": {"summary": "Update one network profile"},
                "delete": {"summary": "Delete an unused network profile"},
            },
            "/v1/headless-endpoints": {
                "get": {"summary": "List unique Fabric endpoint packages"},
                "post": {"summary": "Import one single-owner Fabric endpoint package"},
            },
            "/v1/headless-endpoints/{id}": {
                "get": {"summary": "Read endpoint package metadata without its secret"},
                "delete": {"summary": "Delete an available, never-bound endpoint package"},
            },
            "/v1/tuntom/build": {
                "get": {"summary": "Read Tuntom adapter build and branch status"},
                "post": {"summary": "Queue an immutable Tuntom adapter build"},
            },
            "/v1/tuntom/refs/refresh": {
                "post": {"summary": "Queue a Tuntom remote branch refresh"},
            },
            "/v1/tuntom/builds/{id}": {
                "delete": {"summary": "Delete an unused Tuntom adapter build"},
            },
            "/v1/qemu-images": {
                "get": {"summary": "List staged QEMU blackbox manifests"},
                "post": {"summary": "Import a QEMU manifest with one or more QCOW2 disks and NICs"},
            },
            "/v1/qemu-images/{id}": {
                "get": {"summary": "Read one staged QEMU blackbox manifest"},
                "delete": {"summary": "Delete a manifest without deleting staged source files"},
            },
            "/v1/runtime-profiles/{id}/work-files": {
                "put": {"summary": "Add or replace a file copied into new instance /work"},
                "delete": {"summary": "Remove a /work file from a runtime profile"},
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
            "/v1/instances/{id}/upgrade": {
                "post": {"summary": "Upgrade a managed Smithproxy, Tuntom or ELF component without changing instance configuration or networking"},
            },
            "/v1/instances/{id}/snapshots": {
                "get": {"summary": "List materialized named instance snapshots"},
                "post": {"summary": "Create a hot, cold or stop-path snapshot with optional live forensic cores"},
            },
            "/v1/instances/{id}/snapshots/{snapshot_id}": {
                "get": {"summary": "Read snapshot metadata"},
                "delete": {"summary": "Drop a materialized snapshot"},
            },
            "/v1/instances/{id}/snapshots/{snapshot_id}/restore": {
                "post": {"summary": "Restore snapshot data and component versions while preserving networking"},
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
            "/v1/firewall": {
                "get": {"summary": "Read the SAS-owned nft INPUT/FORWARD allow-list"},
                "put": {"summary": "Enable or disable INPUT and instance FORWARD enforcement"},
            },
            "/v1/firewall/authorizations": {
                "post": {"summary": "Authorize an IPv4/IPv6 source and optionally assign or spawn an instance"},
            },
            "/v1/instances/{id}/sources": {
                "post": {"summary": "Attach another exact source IP to a live instance"},
            },
            "/v1/firewall/authorizations/{id}": {
                "delete": {"summary": "Revoke one source authorization"},
            },
            "/v1/firewall/authorizations/{id}/extend": {
                "post": {"summary": "Add time to an expiring source authorization"},
            },
            "/v1/instances/{id}/diagnostics": {
                "get": {"summary": "Read execution model, paths and namespace details"},
            },
            "/v1/instances/{id}/location": {
                "get": {"summary": "Read host coordinates for an exact UUID or unique alias; returns canonical UUID"},
            },
            "/v1/instances/{id}/alias": {
                "post": {"summary": "Set unique alias using {alias: name}; empty alias clears it (UUID required)"},
            },
            "/v1/instances/{id}/microservices/{prefix}": {
                "get": {"summary": "V3 status; optional run_id query selects an exact historical run"},
            },
            "/v1/instances/{id}/microservices/check": {
                "post": {"summary": "Queue a deduplicated check of this instance only (202)"},
            },
            "/v1/instances/{id}/microservices/00/configure": {
                "post": {"summary": "Queue persistent 00-start opt-out/in using {enabled: boolean}"},
            },
            "/v1/test-drives/{id}/microservices/00": {
                "get": {"summary": "Read reserved 00-start status for a Test Drive"},
            },
            "/v1/test-drives/{id}/microservices/check": {
                "post": {"summary": "Queue this Test Drive's system-service check"},
            },
            "/v1/test-drives/{id}/microservices/00/configure": {
                "post": {"summary": "Queue Test Drive 00-start opt-out/in using {enabled: boolean}"},
            },
            "/v1/instances/{id}/microservices/{prefix}/stop": {
                "post": {"summary": "V3 verified stop: contract_version=3, owner and run_id required; withdraw registration first"},
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
                    test_drives: TestDriveManager | None = None,
                    firewall: FirewallManager | None = None,
                    network_profiles: NetworkProfileLibrary | None = None,
                    l2_segments: L2Segments | None = None,
                    tuntom_builder: TuntomBuilder | None = None,
                    qemu_images: QemuImageLibrary | None = None,
                    appliance_exports: ApplianceExportLibrary | None = None,
                    headless_endpoints: HeadlessEndpointLibrary | None = None,
                    snapshots: SnapshotManager | None = None):
    program_artifacts = ProgramArtifacts(runtime_profiles.path.parent / 'program-artifacts') if runtime_profiles else None
    if snapshots is None and isinstance(getattr(manager, "state_dir", None), Path):
        snapshots = SnapshotManager(manager.state_dir.parent / "instance-snapshots", manager)

    def firewall_context(sources: list[str] | None = None) -> tuple[str, list[str]]:
        if not network_settings:
            raise BackendError("network settings are unavailable")
        settings = network_settings.get()
        if firewall:
            firewall.namespace_cidr_v6 = settings["namespace_cidr_v6"]
        return settings["namespace_cidr"], (
            manager.sources() if sources is None else sources
        )

    def firewall_state_view() -> dict[str, Any]:
        if not firewall:
            raise BackendError("firewall is unavailable")
        namespace_cidr, sources = firewall_context()
        state = firewall.view(namespace_cidr, sources)
        active_states = {"starting", "running", "orphaned"}
        instances = [item for item in manager.snapshot() if item.state in active_states]
        profile_names = {
            str(item.get("profile_id", "")): str(item.get("name", ""))
            for item in (runtime_profiles.list() if runtime_profiles else [])
        }
        entries: dict[str, dict[str, Any]] = {}

        def topology_entry(source: str) -> dict[str, Any]:
            raw_source = str(source).strip()
            try:
                network = ipaddress.ip_network(raw_source, strict=False)
                key = str(network)
                display_source = (
                    str(network.network_address) if network.prefixlen == network.max_prefixlen
                    else key
                )
            except ValueError:
                key = raw_source
                display_source = raw_source
            return entries.setdefault(key, {
                "source": display_source, "source_cidr": key,
                "input_allowed": False, "forward_allowed": False,
                "spawn_allowed": False, "active": False, "systems": [], "instances": [],
                "expirations": [], "selectors": [],
            })

        def add_selector(entry: dict[str, Any], authorization: dict[str, Any]) -> None:
            selector = {
                "authorization_id": str(authorization.get("authorization_id", "")),
                "protocol": str(authorization.get("protocol", "any")),
                "destination": str(authorization.get("destination", "")),
                "ports": list(authorization.get("ports", [])),
                "chains": list(authorization.get("chains", [])),
            }
            if (selector["protocol"] != "any" or selector["destination"] or selector["ports"]):
                if selector not in entry["selectors"]:
                    entry["selectors"].append(selector)

        def add_expiration(entry: dict[str, Any], authorization: dict[str, Any]) -> None:
            expires_at = str(authorization.get("expires_at", ""))
            authorization_id = str(authorization.get("authorization_id", ""))
            if not expires_at or any(
                item.get("authorization_id") == authorization_id
                for item in entry["expirations"]
            ):
                return
            entry["expirations"].append({
                "authorization_id": authorization_id,
                "expires_at": expires_at,
                "system": str(authorization.get("system", "")),
                "chains": list(authorization.get("chains", [])),
                "active": bool(authorization.get("active")),
            })

        for source in sources:
            entry = topology_entry(str(source))
            entry["spawn_allowed"] = True
            entry["forward_allowed"] = True
            entry["active"] = True
        for authorization in state.get("authorizations", []):
            entry = topology_entry(str(authorization.get("source", "")))
            system = str(authorization.get("system", ""))
            if system and system not in entry["systems"]:
                entry["systems"].append(system)
            add_expiration(entry, authorization)
            add_selector(entry, authorization)
            if authorization.get("active"):
                entry["active"] = True
                entry["input_allowed"] |= "input" in authorization.get("chains", [])
                entry["forward_allowed"] |= "forward" in authorization.get("chains", [])

        for instance in instances:
            for source in (instance.source_ips or [instance.source_ip or "unknown"]):
                if instance.network_ingress_driver != "authorized-veth":
                    continue
                topology_entry(source)["active"] = True
        for source, entry in entries.items():
            try:
                network = ipaddress.ip_network(source, strict=False)
            except ValueError:
                network = None
            if network and network.prefixlen == network.max_prefixlen:
                address = network.network_address
                for authorization in state.get("authorizations", []):
                    if not authorization.get("active"):
                        continue
                    try:
                        authorized_network = ipaddress.ip_network(
                            str(authorization.get("source", "")), strict=False,
                        )
                    except ValueError:
                        continue
                    if address.version == authorized_network.version and address in authorized_network:
                        add_expiration(entry, authorization)
                        add_selector(entry, authorization)
                        entry["input_allowed"] |= "input" in authorization.get("chains", [])
                        entry["forward_allowed"] |= "forward" in authorization.get("chains", [])
            for instance in instances:
                try:
                    matches = network is not None and any(
                        ipaddress.ip_address(candidate) in network
                        for candidate in (instance.source_ips or [instance.source_ip])
                    )
                except ValueError:
                    matches = source in (instance.source_ips or [instance.source_ip])
                if not matches:
                    continue
                network_view: dict[str, Any] = {}
                try:
                    allocation = manager.backend.allocation(instance.id)
                    ingress = manager.backend.ingress_allocation(instance.id)
                    network_view = {
                        "subnet": ingress.subnet,
                        "host_interface": ingress.host_if,
                        "host_ip": ingress.host_ip,
                        "guest_interface": ingress.guest_if,
                        "guest_ip": ingress.guest_ip,
                        "subnet_v6": ingress.subnet_v6,
                        "host_ip_v6": ingress.host_ip_v6,
                        "guest_ip_v6": ingress.guest_ip_v6,
                        "egress_subnet": allocation.subnet,
                        "egress_host_interface": allocation.host_if,
                        "egress_host_ip": allocation.host_ip,
                        "egress_guest_interface": allocation.guest_if,
                        "egress_guest_ip": allocation.guest_ip,
                        "egress_subnet_v6": allocation.subnet_v6,
                        "egress_host_ip_v6": allocation.host_ip_v6,
                        "egress_guest_ip_v6": allocation.guest_ip_v6,
                        "egress_mode": allocation.egress_mode,
                        "sas_interface": allocation.sas_interface,
                    }
                except (AttributeError, BackendError, TypeError):
                    pass
                entry["instances"].append({
                    "id": instance.id, "state": instance.state,
                    "slice_unit": instance.slice_unit, "members": instance.members,
                    "namespace": instance.namespace, "user_id": instance.user_id,
                    "profile": profile_names.get(instance.runtime_profile_id)
                    or instance.profile or "custom",
                    "network": network_view,
                })
        state["topology"] = sorted(
            entries.values(), key=lambda item: (
                not item["instances"],
                int(ipaddress.ip_network(item["source"], strict=False).network_address)
                if item["source"] != "unknown" else 2 ** 32,
                item["source"],
            )
        )
        route_via = network_settings.get().get("sas_route_via", "") if network_settings else ""
        egress_groups: dict[str, dict[str, Any]] = {}
        seen_instances: set[str] = set()
        for entry in state["topology"]:
            for instance in entry["instances"]:
                if instance["id"] in seen_instances:
                    continue
                seen_instances.add(instance["id"])
                network = instance.get("network", {})
                mode = str(network.get("egress_mode", "unknown"))
                interface = str(network.get("sas_interface", ""))
                key = f"{mode}|{interface}"
                group = egress_groups.setdefault(key, {
                    "key": key, "mode": mode, "interface": interface,
                    "route_via": route_via, "instances": [],
                    "effect": (
                        "SNAT / MASQUERADE na adresu SAS hostu"
                        if mode == "masquerade" else
                        "Routed egress — guest IP proxy zůstává zachována"
                        if mode == "routed" else "Neznámý egress efekt"
                    ),
                })
                group["instances"].append({
                    "id": instance["id"], "namespace": instance["namespace"],
                    "profile": instance["profile"],
                    "guest_ip": network.get("egress_guest_ip", ""),
                    "guest_ip_v6": network.get("egress_guest_ip_v6", ""),
                    "guest_interface": network.get("egress_guest_interface", ""),
                })
        state["egress_groups"] = sorted(
            egress_groups.values(), key=lambda item: (item["mode"], item["interface"])
        )
        state["active_instances"] = [{
            "id": item.id, "namespace": item.namespace, "state": item.state,
            "profile": profile_names.get(item.runtime_profile_id) or item.profile,
            "user_id": item.user_id, "source_ips": item.source_ips or [item.source_ip],
        } for item in instances]
        state["runtime_profiles"] = [{
            "profile_id": item.get("profile_id", ""), "name": item.get("name", ""),
            "ttl_seconds": item.get("ttl_seconds"),
        } for item in (runtime_profiles.list() if runtime_profiles else [])]
        state["topology_updated_at"] = datetime.now(timezone.utc).isoformat()
        return state

    def runtime_profile_ttl(payload: dict, default: int | None = None) -> int | None:
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

    def network_binding(payload: dict, current: dict | None = None) -> tuple[str, str]:
        values = []
        selected: list[dict | None] = []
        for kind in ("ingress", "egress"):
            field = f"{kind}_network_profile_id"
            value = str(payload.get(field, (current or {}).get(field, "")))
            if value:
                if not network_profiles:
                    raise ConfigError("network profiles are unavailable")
                selected.append(network_profiles.get(value, kind))
            else:
                selected.append(None)
            values.append(value)
        duplex = [
            item for item in selected
            if item and item.get("consumes") == ["ingress", "egress"]
        ]
        duplex_ids = {str(item["network_profile_id"]) for item in duplex}
        if len(duplex_ids) > 1:
            raise ConfigError("runtime profile cannot combine two duplex network profiles")
        if duplex_ids:
            # One Tuntom VIA binding owns both di0 and do0. Canonical storage
            # repeats the same immutable profile ID in both slots so API, CLI
            # and diagnostics all expose that both sides are consumed.
            profile_id = duplex_ids.pop()
            return profile_id, profile_id
        return values[0], values[1]

    def validate_tuntom_network_build(payload: dict) -> None:
        driver = str(payload.get("driver", ""))
        if driver not in {"tuntom", "tuntom-via"}:
            return
        if not tuntom_builder:
            raise ConfigError("tuntom build library is unavailable")
        build_id = str(payload.get("tuntom_build_id", ""))
        tuntom_builder.resolve_tunnel(build_id)
        if driver == "tuntom-via":
            tuntom_builder.resolve_adapter(build_id)

    def profile_wiring(payload, current=None):
        selected = bindings(payload.get('wiring', (current or {}).get('wiring', [])), profile=True)
        for binding in selected:
            if not l2_segments:
                raise BackendError('Wiring unavailable')
            l2_segments.get(binding['segment_id'])
        return selected

    def prepare_profile_image(payload, current=None):
        selected = runtime_images.variant(payload.get('rootfs_variant', (current or {}).get('rootfs_variant', 'barebone')))
        application = payload.get('application', (current or {}).get('application', 'smithproxy'))
        if current and current.get('rootfs_image') and not payload.get('refresh_rootfs', False):
            unchanged = (selected == current.get('rootfs_variant', 'barebone')
                         and application == current.get('application', 'smithproxy')
                         and payload.get('build_id', current.get('build_id', '')) == current.get('build_id', '')
                         and payload.get('program_settings', current.get('program_settings', {})) == current.get('program_settings', {}))
            cached = runtime_profiles.path.parent / 'runtime-images' / current['rootfs_image']
            if unchanged and (cached / 'runtime-image.json').is_file():
                return cached
        base = None
        if application == 'smithproxy':
            build_id = str(payload.get('build_id', (current or {}).get('build_id', '')))
            builder.prepare_rootfs(build_id)
            base = builder.resolve_rootfs(build_id)
        executable = None
        executable_name = 'program'
        if application == 'elf':
            settings = elf_settings(payload.get('program_settings', {}))
            executable = program_artifacts.binary(settings['artifact_id'])
            artifact = program_artifacts.get(settings['artifact_id'])
            executable_name = artifact.get('filename', Path(artifact.get('origin', 'program')).name)
        return runtime_images.prepare(runtime_profiles.path.parent / 'runtime-images', selected,
            application='' if application == 'smithproxy' else application,
            settings=payload.get('program_settings', {}), base=base, executable=executable, executable_name=executable_name)

    def profile_view(item: dict, *, artifacts: list[dict] | None = None,
                     instances: list[Instance] | None = None) -> dict:
        result = dict(item)
        result["work_files"] = (
            runtime_profiles.list_work_files(item["profile_id"]) if runtime_profiles else []
        )
        result["available"] = True
        result["newer_build_available"] = False
        if item.get('application', 'smithproxy') != 'smithproxy':
            image = item.get('rootfs_image', '')
            ready = bool(image and (runtime_profiles.path.parent / 'runtime-images' / image / 'runtime-image.json').is_file())
            result.update(available=ready, runtime_supported=True, rootfs_ready=ready,
                          usage={'instances': [{'id': i.id, 'state': i.state} for i in manager.snapshot()
                                               if i.runtime_profile_id == item['profile_id']]})
            return result
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
            filesystem_mode = str(item.get("filesystem_mode", "host"))
            result["filesystem_mode"] = filesystem_mode
            result["rootfs_ready"] = filesystem_mode != "rootfs"
            if artifact is None:
                result["available"] = False
            else:
                if filesystem_mode == "rootfs":
                    rootfs = builder.rootfs_info(str(item.get("build_id", "")))
                    result["rootfs_ready"] = bool(rootfs.get("rootfs_ready"))
                    result["rootfs_path"] = str(rootfs.get("rootfs_path", ""))
                    if not result["rootfs_ready"]:
                        result["available"] = False
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
        result["network_start_parameters"] = []
        result["network_drivers"] = {}
        for kind in ("ingress", "egress"):
            field = f"{kind}_network_profile_id"
            network_id = str(item.get(field, ""))
            result[f"{kind}_network_profile_name"] = "global/default"
            result[f"{kind}_network_profile_ready"] = True
            if network_id and network_profiles:
                try:
                    selected = network_profiles.get(network_id, kind)
                    result[f"{kind}_network_profile_name"] = selected["name"]
                    result[f"{kind}_network_profile_ready"] = bool(selected["implemented"])
                    result["network_drivers"][kind] = selected.get("driver", "")
                    for parameter in selected.get("start_parameters", []):
                        if parameter not in result["network_start_parameters"]:
                            result["network_start_parameters"].append(parameter)
                    if not selected["implemented"]:
                        result["available"] = False
                except BackendError:
                    result["available"] = False
                    result[f"{kind}_network_profile_name"] = "unavailable"
                    result[f"{kind}_network_profile_ready"] = False
        return result

    def network_profile_view(item: dict) -> dict:
        profile_id = str(item.get("network_profile_id", ""))
        usage = []
        if runtime_profiles:
            usage = [
                {"profile_id": profile["profile_id"], "name": profile["name"]}
                for profile in runtime_profiles.list()
                if profile_id in {
                    profile.get("ingress_network_profile_id"),
                    profile.get("egress_network_profile_id"),
                }
            ]
        return {**item, "usage": {"runtime_profiles": usage}}

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
            and (item.desired_state == "running" or item.state in {"starting", "running", "orphaned"})
        ]
        drives = [
            {"id": item.id, "state": item.state}
            for item in (test_drives.snapshot() if test_drives else [])
            if item.build_id == build_id
        ]
        snapshot_usage = snapshots.usage("smithproxy", build_id) if snapshots else []
        return {"runtime_profiles": profiles, "instances": instances,
                "test_drives": drives, "snapshots": snapshot_usage}

    def build_status_view() -> dict | None:
        if not builder:
            return None
        status = builder.status()
        instance_snapshot = manager.snapshot()
        drive_snapshot = test_drives.snapshot() if test_drives else []
        profile_snapshot = runtime_profiles.list() if runtime_profiles else []
        instance_usage: dict[str, list[dict]] = {}
        for item in instance_snapshot:
            if item.desired_state != "running" and item.state not in {"starting", "running", "orphaned"}:
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

    def spawn_instance(payload: dict[str, Any]) -> Instance:
        if not builder:
            return manager.create(payload)
        effective_payload = dict(payload)
        required_network_runtime: list[str] = []
        runtime_profile_id = str(payload.get("runtime_profile_id", ""))
        if runtime_profile_id:
            if not runtime_profiles:
                raise ConfigError("runtime profiles are unavailable")
            binding = runtime_profiles.get(runtime_profile_id)
            if binding.get('application', 'smithproxy') != 'smithproxy':
                image_id = binding.get('rootfs_image', '')
                if not re.fullmatch(r'[0-9a-f]{64}', image_id):
                    raise ConfigError('save the program profile to prepare its rootfs')
                root = runtime_profiles.path.parent / 'runtime-images' / image_id
                contract = json.loads((root / 'runtime-image.json').read_text())
                effective_payload.update(
                    runtime_seconds=0 if binding['ttl_seconds'] is None else binding['ttl_seconds'],
                    filesystem_mode='rootfs', network_ingress_driver='none', network_egress_driver='none',
                    auto_restart=binding['auto_restart'], build_id=image_id, config_id='',
                    wiring=payload.get('wiring', binding.get('wiring', [])),
                )
                return manager.create(effective_payload, template_path=root / 'program.cfg', rootfs_path=root,
                    work_installer=lambda destination: runtime_profiles.install_work_files(runtime_profile_id, destination),
                    program={'application': binding['application'], 'argv': contract['argv'],
                             'artifact_id': str(binding.get('program_settings', {}).get('artifact_id', ''))})
            if 'wiring' not in payload:
                effective_payload['wiring'] = binding.get('wiring', [])
            effective_payload["build_id"] = binding["build_id"]
            effective_payload["config_id"] = binding["config_id"]
            effective_payload["cert_bundle_id"] = binding.get("cert_bundle_id", "")
            effective_payload["auto_restart"] = binding.get("auto_restart", False)
            effective_payload["filesystem_mode"] = str(
                binding.get("filesystem_mode", "host")
            )
            for kind in ("ingress", "egress"):
                field = f"{kind}_network_profile_id"
                network_id = str(binding.get(field, ""))
                effective_payload[field] = network_id
                if not network_id:
                    continue
                if not network_profiles:
                    raise ConfigError("network profiles are unavailable")
                selected_network = network_profiles.get(network_id, kind)
                for parameter in selected_network.get("start_parameters", []):
                    if parameter not in required_network_runtime:
                        required_network_runtime.append(parameter)
                if not selected_network.get("implemented"):
                    raise ConfigError(
                        f"{kind} network profile uses a driver/selector not implemented by this runner"
                    )
                if kind == "ingress":
                    effective_payload["network_ingress_driver"] = selected_network["driver"]
                if kind == "egress":
                    effective_payload["network_egress_mode"] = selected_network["mode"]
                    effective_payload["network_sas_interface"] = selected_network["host_interface"]
                    effective_payload["network_egress_driver"] = selected_network["driver"]
                    for field in (
                        "socket", "build_id", "in_prefix", "out_prefix", "admission", "mtu",
                    ):
                        if f"tuntom_{field}" in selected_network:
                            effective_payload[f"network_tuntom_{field}"] = selected_network[f"tuntom_{field}"]
                    if selected_network["driver"] == "tuntom-via":
                        if not tuntom_builder:
                            raise ConfigError("tuntom build library is unavailable")
                        effective_payload["network_tuntom_adapter"] = str(
                            tuntom_builder.resolve_adapter(selected_network["tuntom_build_id"])
                        )
                        effective_payload["network_tuntom_binary"] = str(
                            tuntom_builder.resolve_tunnel(selected_network["tuntom_build_id"])
                        )
            profile_ttl = binding.get("ttl_seconds", 1800)
            effective_payload["runtime_seconds"] = 0 if profile_ttl is None else profile_ttl
        network_runtime = effective_payload.get("network_runtime", {})
        if not isinstance(network_runtime, dict):
            raise ConfigError("network_runtime must be an object")
        unexpected = sorted(set(network_runtime) - set(required_network_runtime))
        missing = [name for name in required_network_runtime if not network_runtime.get(name)]
        if unexpected:
            raise ConfigError(
                "unexpected network runtime parameters: " + ", ".join(unexpected)
            )
        if missing:
            raise ConfigError("missing network runtime parameters: " + ", ".join(missing))
        headless_endpoint_id = str(network_runtime.get("headless_endpoint_id", ""))
        if headless_endpoint_id and not uuid_is_valid(headless_endpoint_id):
            raise ConfigError("headless_endpoint_id must be a UUID")
        if "tuntom_secret" in network_runtime and not re.fullmatch(
            r"[0-9a-fA-F]{32}", str(network_runtime["tuntom_secret"])
        ):
            raise ConfigError("tuntom_secret must contain exactly 32 hex characters")
        for name in (
            "tuntom_local_ip", "tuntom_peer_ip", "tuntom_peer_host", "tuntom_switch_ip",
        ):
            if name in network_runtime:
                try:
                    network_runtime[name] = str(ipaddress.ip_address(str(network_runtime[name])))
                except ValueError as exc:
                    raise ConfigError(f"{name} must be an IPv4 or IPv6 address") from exc
        effective_payload["network_runtime"] = network_runtime
        if str(effective_payload.get("network_egress_driver", "split-veth")) == "tuntom-via":
            if not tuntom_builder:
                raise ConfigError("tuntom build library is unavailable")
            effective_payload["network_tuntom_adapter"] = str(
                tuntom_builder.resolve_adapter(
                    str(effective_payload.get("network_tuntom_build_id", ""))
                )
            )
            effective_payload["network_tuntom_binary"] = str(
                tuntom_builder.resolve_tunnel(
                    str(effective_payload.get("network_tuntom_build_id", ""))
                )
            )
        requested_build = str(effective_payload.get("build_id", "active"))
        requested_config = str(effective_payload.get("config_id", "active"))
        binary = builder.resolve_binary(requested_build)
        filesystem_mode = str(effective_payload.get("filesystem_mode", "host"))
        if filesystem_mode not in {"host", "rootfs"}:
            raise ConfigError("filesystem_mode must be host or rootfs")
        rootfs = None
        if filesystem_mode == "rootfs":
            # Spawn itself is already a background task. Existing archived
            # builds are therefore upgraded to an immutable rootfs lazily,
            # without blocking the runner HTTP thread.
            builder.prepare_rootfs(requested_build)
            rootfs = builder.resolve_rootfs(requested_build)
            if runtime_profile_id and binding.get('rootfs_image'):
                image_id = binding['rootfs_image']
                if not re.fullmatch(r'[0-9a-f]{64}', image_id):
                    raise ConfigError('invalid pinned runtime image')
                rootfs = runtime_profiles.path.parent / 'runtime-images' / image_id
                if not (rootfs / 'runtime-image.json').is_file():
                    raise ConfigError('pinned runtime image is missing; refusing host fallback')
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
        builder.validate_config_text(config.read_text(encoding="utf-8"), requested_build, assets)
        cert_bundle_id = str(effective_payload.get("cert_bundle_id", ""))
        certs = cert_library.resolve(cert_bundle_id) if cert_bundle_id and cert_library else None
        if cert_bundle_id and not cert_library:
            raise BackendError("certificate bundles are unavailable")
        reservation_id = "spawn-" + str(uuid.uuid4())
        reserved_endpoint: dict[str, Any] | None = None
        if headless_endpoint_id:
            if not headless_endpoints:
                raise ConfigError("headless endpoint package library is unavailable")
            reserved_endpoint = headless_endpoints.reserve(
                headless_endpoint_id, reservation_id,
            )
            effective_payload["headless_endpoint_id"] = headless_endpoint_id
            effective_payload["network_tuntom_tunnel_id"] = int(
                reserved_endpoint["tunnel_id"]
            )
            effective_payload["network_runtime"] = {
                "tuntom_switch_ip": str(reserved_endpoint["switch_ip"]),
                "tuntom_secret": str(reserved_endpoint["secret"]),
            }
        try:
            item = manager.create(
                effective_payload, binary, config, assets_dir=assets,
                cert_bundle_dir=certs,
                work_installer=(
                    lambda destination: runtime_profiles.install_work_files(
                        runtime_profile_id, destination,
                    )
                ) if runtime_profile_id and runtime_profiles else None,
                rootfs_path=rootfs,
            )
            if reserved_endpoint and headless_endpoints:
                try:
                    headless_endpoints.bind(headless_endpoint_id, reservation_id, item.id)
                except Exception:
                    manager.stop(item.id)
                    raise
            return item
        except Exception:
            if reserved_endpoint and headless_endpoints:
                headless_endpoints.cancel(headless_endpoint_id, reservation_id)
            raise

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

    def wait_for_tuntom_build() -> dict:
        if not tuntom_builder:
            raise BackendError("tuntom builder is unavailable")
        while True:
            with tuntom_builder.lock:
                state = asdict(tuntom_builder.state)
            if state.get("state") != "running":
                if state.get("state") == "failed":
                    raise BackendError(str(state.get("error") or "tuntom build failed"))
                return state
            time.sleep(0.5)

    def wait_for_tuntom_refs() -> dict:
        if not tuntom_builder:
            raise BackendError("tuntom builder is unavailable")
        while True:
            with tuntom_builder.refs_lock:
                state = json.loads(json.dumps(tuntom_builder.refs_state))
            if state.get("state") != "running":
                if state.get("state") == "failed":
                    raise BackendError(str(state.get("error") or "tuntom ref refresh failed"))
                return state
            time.sleep(0.25)

    action_routes = {
        "POST": (
            r"/v1/builds/[A-Za-z0-9._-]+/config/preview",
            r"/v1/builds/[A-Za-z0-9._-]+/rootfs",
            r"/v1/configs/(?:preview|commit)",
            r"/v1/config-observer",
            r"/v1/instances/[0-9a-f-]+/(?:debug|restart|upgrade|config/preview)",
            r"/v1/instances/[0-9a-f-]+/snapshots",
            r"/v1/instances/[0-9a-f-]+/snapshots/[0-9a-f-]+/restore",
            r"/v1/instances/[0-9a-f-]+/sources",
            r"/v1/cert-bundles",
            r"/v1/cert-bundles/[0-9a-f-]+/certificates",
            r"/v1/runtime-profiles",
            r"/v1/network-profiles",
            r"/v1/test-drives",
            r"/v1/appliance-exports",
            r"/v1/test-drives/[0-9a-f-]+/upgrade",
            r"/v1/test-drives/[0-9a-f-]+/(?:extend|restart|config-mode|config/preview)",
            r"/v1/instances/cleanup",
            r"/v1/firewall/authorizations",
            r"/v1/firewall/authorizations/[0-9a-f-]+/extend",
        ),
        "PUT": (
            r"/v1/runtime-profiles/[0-9a-f-]+",
            r"/v1/network-profiles/[0-9a-f-]+",
            r"/v1/configs/[0-9a-f-]+/metadata",
            r"/v1/settings/networking",
            r"/v1/firewall",
        ),
        "DELETE": (
            r"/v1/builds/[A-Za-z0-9._-]+",
            r"/v1/tuntom/builds/[A-Za-z0-9._-]+",
            r"/v1/configs/previews/[0-9a-f-]+",
            r"/v1/instances/[0-9a-f-]+/debug",
            r"/v1/instances/[0-9a-f-]+/snapshots/[0-9a-f-]+",
            r"/v1/runtime-profiles/[0-9a-f-]+",
            r"/v1/network-profiles/[0-9a-f-]+",
            r"/v1/cert-bundles/[0-9a-f-]+",
            r"/v1/configs/[0-9a-f-]+",
            r"/v1/instances/[0-9a-f-]+/record",
            r"/v1/instances/[0-9a-f-]+",
            r"/v1/test-drives/[0-9a-f-]+",
            r"/v1/appliance-exports/[0-9a-f-]+",
            r"/v1/firewall/authorizations/[0-9a-f-]+",
        ),
    }

    def action_resource(path: str) -> str:
        if path == "/v1/instances/cleanup":
            return "instances:cleanup"
        if path.startswith("/v1/firewall"):
            return "firewall"
        match = re.fullmatch(r"/v1/test-drives/([0-9a-f-]+)(?:/.*)?", path)
        if match:
            return f"test-drive:{match.group(1)}"
        if path == "/v1/test-drives":
            return "test-drives"
        if path == "/v1/appliance-exports":
            return "appliance-exports"
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

        def _download(self, path: Path, filename: str) -> None:
            try:
                size = path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "application/gzip")
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "private, no-store")
                self.end_headers()
                with path.open("rb") as source:
                    shutil.copyfileobj(source, self.wfile, length=1024 * 1024)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True

        def _authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            expected = f"Bearer {token}"
            return bool(token) and hmac.compare_digest(supplied, expected)

        def _path(self) -> list[str]:
            return [part for part in urlsplit(self.path).path.split("/") if part]

        def _l2_request(self, method: str) -> bool:
            parts = self._path()
            if parts[:2] != ["v1", "l2-segments"]:
                return False
            try:
                if not l2_segments:
                    raise BackendError("L2 segments are unavailable")
                if method == "GET":
                    if len(parts) == 2:
                        self._json(HTTPStatus.OK, {"segments": l2_segments.list()})
                    elif parts == ['v1', 'l2-segments', 'addressing']:
                        self._json(HTTPStatus.OK, l2_segments.inventory())
                    elif len(parts) == 3:
                        self._json(HTTPStatus.OK, l2_segments.get(parts[2]))
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return True
                payload = {}
                if method == "POST":
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= max_body:
                        raise BackendError("invalid L2 request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise BackendError("L2 request must be an object")
                if method == 'POST' and parts == ['v1', 'l2-segments', 'addressing', 'preview']:
                    warnings = overlaps(l2_segments.inventory()['entries'], payload.get('addresses', []),
                                        payload.get('segment_id', ''), payload.get('endpoint_id', ''))
                    self._json(HTTPStatus.OK, {'warnings': warnings})
                    return True
                if method == "POST" and len(parts) == 2:
                    operation = lambda: l2_segments.create(payload)
                elif method == "POST" and len(parts) == 4 and parts[3] == "endpoints":
                    def operation():
                        value = l2_segments.attach(parts[2], payload)
                        # Attachment is durable and catalogue locks are released before
                        # entering the per-instance controller (manager -> Wiring).
                        # A disabled 00-start remains disabled; stopped instances defer
                        # application until startup. Failures surface in this task.
                        check = manager.check_microservices(payload['instance_id'])
                        return {**value, 'check': check}
                elif method == 'POST' and len(parts) == 6 and parts[3] == 'endpoints' and parts[5] == 'addressing':
                    def operation():
                        value = l2_segments.configure_addressing(parts[2], parts[4], payload)
                        endpoint = next(ep for ep in l2_segments.get(parts[2])['endpoints'] if ep['id'] == parts[4])
                        # Persist first; failures retain the operator's edits and surface on the task.
                        return {'addressing': value, 'check': manager.check_microservices(endpoint['instance_id'])}
                elif method == "DELETE" and len(parts) == 3:
                    operation = lambda: l2_segments.delete(parts[2])
                elif method == "DELETE" and len(parts) == 5 and parts[3] == "endpoints":
                    operation = lambda: l2_segments.detach(parts[2], parts[4])
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return True
                canonical = json.dumps([method, parts, payload], sort_keys=True)
                self._json(HTTPStatus.ACCEPTED, submit_task(
                    "l2-segment", f"L2 {method} {'/'.join(parts[2:]) or payload.get('name', '')}",
                    "l2:" + hashlib.sha256(canonical.encode()).hexdigest(),
                    "l2-segments", operation,
                ))
            except (BackendError, ValueError, OSError) as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return True

        def do_GET(self) -> None:
            parts = self._path()
            if parts == ["healthz"]:
                self._json(HTTPStatus.OK, {"status": "ok"})
                return
            if not self._authorized():
                self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                return
            if self._l2_request("GET"):
                return
            if len(parts) == 5 and parts[:2] == ['v1', 'test-drives'] and parts[3:] == ['microservices', '00']:
                try:
                    if not test_drives or not test_drives.system_start or not uuid_is_valid(parts[2]) or not test_drives.peek(parts[2]):
                        raise ServiceError('instance_not_found', 404)
                    self._json(HTTPStatus.OK, test_drives.system_start.status(parts[2]))
                except (BackendError, OSError, ValueError) as exc:
                    self._json(getattr(exc, 'http', 503), {'error': str(exc)})
                return
            if len(parts) == 5 and parts[:2] == ['v1', 'instances'] and parts[3] == 'microservices':
                try:
                    if parts[4] == '00' and manager.system_start:
                        if not uuid_is_valid(parts[2]) or not manager.peek(parts[2]):
                            raise ServiceError('instance_not_found', 404)
                        self._json(HTTPStatus.OK, manager.system_start.status(parts[2]))
                        return
                    if not manager.microservices:
                        raise ServiceError('microservices_unavailable')
                    query = parse_qs(urlsplit(self.path).query)
                    value = manager.microservices.status(parts[2], parts[4], query.get('run_id', [None])[0])
                    self._json(HTTPStatus.OK, value)
                except (ServiceError, OSError, ValueError) as exc:
                    self._json(getattr(exc, 'http', 503), {'contract_version': 3,
                        'error': getattr(exc, 'error', 'state_unknown'), 'state': 'unknown',
                        'process_group_empty': None})
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
            elif parts == ["v1", "appliance-exports"] and appliance_exports:
                self._json(HTTPStatus.OK, {"exports": appliance_exports.list()})
            elif (len(parts) == 3 and parts[:2] == ["v1", "appliance-exports"]
                  and appliance_exports):
                try:
                    self._json(HTTPStatus.OK, appliance_exports.get(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif (len(parts) == 4 and parts[:2] == ["v1", "appliance-exports"]
                  and parts[3] == "download" and appliance_exports):
                try:
                    item, archive = appliance_exports.archive(parts[2])
                    self._download(archive, str(item["archive_name"]))
                except (BackendError, OSError) as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
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
            elif parts == ["v1", "firewall"] and firewall:
                try:
                    self._json(HTTPStatus.OK, firewall_state_view())
                except (ConfigError, BackendError, OSError) as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            elif parts == ["v1", "build"] and builder:
                self._json(HTTPStatus.OK, build_status_view())
            elif parts == ["v1", "tuntom", "build"] and tuntom_builder:
                self._json(HTTPStatus.OK, tuntom_builder.status())
            elif parts == ["v1", "configs"] and config_library:
                self._json(HTTPStatus.OK, {
                    "configs": builder.configs() if builder else config_library.list()
                })
            elif parts == ['v1', 'program-artifacts'] and program_artifacts:
                self._json(HTTPStatus.OK, {'artifacts': program_artifacts.list()})
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
            elif parts == ["v1", "network-profiles"] and network_profiles:
                try:
                    self._json(HTTPStatus.OK, {
                        "profiles": [network_profile_view(item) for item in network_profiles.list()],
                    })
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
            elif parts == ["v1", "headless-endpoints"] and headless_endpoints:
                self._json(HTTPStatus.OK, {"packages": headless_endpoints.list()})
            elif (len(parts) == 3 and parts[:2] == ["v1", "headless-endpoints"]
                  and headless_endpoints):
                try:
                    self._json(HTTPStatus.OK, headless_endpoints.get(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif parts == ["v1", "qemu-images"] and qemu_images:
                self._json(HTTPStatus.OK, {"images": qemu_images.list()})
            elif len(parts) == 3 and parts[:2] == ["v1", "qemu-images"] and qemu_images:
                try:
                    self._json(HTTPStatus.OK, qemu_images.get(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif (len(parts) == 3 and parts[:2] == ["v1", "network-profiles"]
                  and network_profiles):
                try:
                    self._json(HTTPStatus.OK, network_profile_view(network_profiles.get(parts[2])))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
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
            elif (len(parts) == 4 and parts[:2] == ["v1", "instances"]
                  and parts[3] == "snapshots"):
                item = manager.peek(parts[2])
                self._json(HTTPStatus.OK, {"snapshots": snapshots.list(parts[2])}) if item and snapshots else self._json(
                    HTTPStatus.NOT_FOUND, {"error": "not found"}
                )
            elif (len(parts) == 5 and parts[:2] == ["v1", "instances"]
                  and parts[3] == "snapshots"):
                try:
                    if not snapshots:
                        raise BackendError("snapshot storage is unavailable")
                    self._json(HTTPStatus.OK, snapshots.get(parts[2], parts[4]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
            elif len(parts) == 3 and parts[:2] == ["v1", "instances"]:
                item = manager.peek(parts[2])
                self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            elif len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "location":
                result = manager.location(parts[2])
                self._json(HTTPStatus.OK, result) if result else self._json(HTTPStatus.NOT_FOUND, {"error": "instance not found"})
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
                    rootfs_path = "/"
                    rootfs_mode = "host-shared, ProtectSystem=strict"
                    if item.filesystem_mode == "rootfs" and builder:
                        rootfs = builder.rootfs_info(item.build_id)
                        rootfs_path = str(rootfs.get("rootfs_path") or "unavailable")
                        rootfs_mode = "isolated RootDirectory, ProtectSystem=strict"
                    network_bindings = {}
                    for kind in ("ingress", "egress"):
                        profile_id = str(getattr(item, f"{kind}_network_profile_id", ""))
                        selected = {
                            "network_profile_id": profile_id,
                            "name": "global/default",
                            "kind": kind,
                            "implemented": True,
                        }
                        if profile_id and network_profiles:
                            try:
                                selected = network_profiles.get(profile_id, kind)
                            except BackendError:
                                selected = {
                                    "network_profile_id": profile_id,
                                    "name": "unavailable",
                                    "kind": kind,
                                    "implemented": False,
                                }
                        network_bindings[kind] = selected
                    self._json(HTTPStatus.OK, {
                        "instance": asdict(item),
                        "system_start": manager.system_start.status(item.id) if manager.system_start else None,
                        "execution": {
                            "model": "systemd Slice + transient members + network namespace",
                            "slice_unit": item.slice_unit,
                            "slice_rss_bytes": item.slice_rss_bytes,
                            "members": item.members,
                            "unit": item.unit,
                            "rootfs": rootfs_path,
                            "rootfs_mode": rootfs_mode,
                            "network_namespace": item.namespace,
                            "network_namespace_path": f"/run/netns/{item.namespace}" if item.namespace else "unknown",
                            "runtime_dir": str(runtime_dir),
                            "live_config": str(runtime_dir / "smithproxy.cfg"),
                            "saved_config_snapshot": str(manager._config_path(item.id)),
                            "private_run": str(runtime_dir / "run"),
                            "binary": binary_path,
                            "build_type": build_type,
                            "network": network,
                            "network_profiles": network_bindings,
                            "headless_endpoint": (
                                headless_endpoints.get(item.headless_endpoint_id)
                                if item.headless_endpoint_id and headless_endpoints else None
                            ),
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
            if parts == ["v1", "firewall"] and firewall:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid firewall settings size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("firewall settings must be an object")
                    namespace_cidr, sources = firewall_context()
                    item = firewall.update_settings(
                        payload.get("input_enforced", False),
                        payload.get("forward_enforced", False),
                        namespace_cidr, sources,
                    )
                    self._json(HTTPStatus.OK, item)
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "runtime-profiles"]
                    and parts[3] == "work-files" and runtime_profiles):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid profile work file request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    try:
                        content = base64.b64decode(
                            str(payload.get("content_base64", "")), validate=True,
                        )
                        mode = int(str(payload.get("mode", "0600")), 8)
                    except (ValueError, TypeError) as exc:
                        raise ConfigError("invalid work file content or mode") from exc
                    item = runtime_profiles.put_work_file(
                        parts[2], str(payload.get("path", "")), content, mode,
                    )
                    self._json(HTTPStatus.OK, item)
                except (ConfigError, BackendError, OSError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
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
                    namespace_network_v6 = ipaddress.ip_network(
                        validated_settings["namespace_cidr_v6"]
                    )
                    ingress_network = ipaddress.ip_network(validated_settings["ingress_cidr"])
                    ingress_network_v6 = ipaddress.ip_network(
                        validated_settings["ingress_cidr_v6"]
                    )
                    fabric_network = ipaddress.ip_network(validated_settings["fabric_cidr"])
                    fabric_network_v6 = ipaddress.ip_network(
                        validated_settings["fabric_cidr_v6"]
                    )
                    sources = []
                    for raw_source in raw_sources:
                        try:
                            source = str(ipaddress.ip_address(str(raw_source)))
                        except ValueError as exc:
                            raise ConfigError(f"invalid authorized source IP: {raw_source}") from exc
                        address = ipaddress.ip_address(source)
                        own_network = namespace_network_v6 if address.version == 6 else namespace_network
                        own_ingress_network = (
                            ingress_network_v6 if address.version == 6 else ingress_network
                        )
                        own_fabric_network = (
                            fabric_network_v6 if address.version == 6 else fabric_network
                        )
                        if (
                            address in own_network or address in own_ingress_network
                            or address in own_fabric_network
                        ):
                            raise ConfigError(
                                f"authorized source IP overlaps ingress/egress CIDR: {source}"
                            )
                        if source not in sources:
                            sources.append(source)
                    if not sources:
                        raise ConfigError("at least one authorized source IP is required")
                    previous_sources = manager.sources()
                    settings = network_settings.update(validated_settings)
                    NetworkSettings._write(manager.sources_path, sources)
                    if firewall:
                        try:
                            firewall.namespace_cidr_v6 = settings["namespace_cidr_v6"]
                            firewall.reconcile(settings["namespace_cidr"], sources)
                        except Exception:
                            NetworkSettings._write(manager.sources_path, previous_sources)
                            previous_settings = network_settings.get()
                            firewall.namespace_cidr_v6 = previous_settings["namespace_cidr_v6"]
                            firewall.reconcile(previous_settings["namespace_cidr"], previous_sources)
                            raise
                    self._json(HTTPStatus.OK, {
                        **network_settings.view(), "authorized_source_ips": sources,
                    })
                except (ConfigError, BackendError, OSError, ValueError, TypeError,
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
                    restart_flags(auto_restart)
                    current_profile = runtime_profiles.get(parts[2])
                    if current_profile.get('application', 'smithproxy') != 'smithproxy':
                        payload.setdefault('application', current_profile['application'])
                        payload.setdefault('rootfs_variant', current_profile.get('rootfs_variant', 'barebone'))
                        payload['wiring'] = profile_wiring(payload, current_profile)
                        image = prepare_profile_image(payload, current_profile)
                        item = runtime_profiles.save_program(payload, parts[2], image_id=image.name)
                        self._json(HTTPStatus.OK, profile_view(item))
                        return
                    if payload.get('application', 'smithproxy') != 'smithproxy':
                        raise ConfigError('profile application type cannot be changed')
                    filesystem_mode = str(payload.get(
                        "filesystem_mode", current_profile.get("filesystem_mode", "host")
                    ))
                    if filesystem_mode not in {"host", "rootfs"}:
                        raise ConfigError("filesystem_mode must be host or rootfs")
                    ttl_seconds = runtime_profile_ttl(
                        payload, current_profile.get("ttl_seconds", 1800)
                    )
                    ingress_network_id, egress_network_id = network_binding(
                        payload, current_profile,
                    )
                    if build_id == "active":
                        raise ConfigError("runtime profiles must use an archived build")
                    builder.resolve_binary(build_id)
                    if filesystem_mode == "rootfs":
                        builder.prepare_rootfs(build_id)
                    selected_config = config_library.get(config_id)
                    if not selected_config.get("native"):
                        raise ConfigError("runtime profiles require an approved native config")
                    if cert_bundle_id:
                        if not cert_library:
                            raise ConfigError("certificate bundles are unavailable")
                        cert_library.get(cert_bundle_id)
                    image = prepare_profile_image(payload, current_profile) if filesystem_mode == 'rootfs' else None
                    item = runtime_profiles.update(
                        parts[2], str(payload.get("name", "")), build_id,
                        config_id, cert_bundle_id,
                        auto_restart, ttl_seconds,
                        ingress_network_id, egress_network_id,
                        filesystem_mode, wiring=profile_wiring(payload, current_profile),
                        rootfs_variant=payload.get('rootfs_variant', current_profile.get('rootfs_variant', 'barebone')),
                        rootfs_image=image.name if image else '',
                    )
                    self._json(HTTPStatus.OK, profile_view(item))
                except (ConfigError, BackendError, ValueError, TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 3 and parts[:2] == ["v1", "network-profiles"]
                    and network_profiles):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid network profile size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("network profile must be an object")
                    validate_tuntom_network_build({
                        **network_profiles.get(parts[2]), **payload,
                    })
                    item = network_profiles.update(parts[2], payload)
                    self._json(HTTPStatus.OK, network_profile_view(item))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
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
            if self._path() == ['v1', 'program-artifacts'] and program_artifacts:
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 23 * 1024 * 1024:
                        raise BackendError('invalid artifact request size')
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise BackendError('expected JSON object')
                    if bool(payload.get('path')) == bool(payload.get('content_base64')):
                        raise BackendError('provide exactly one of path or content_base64')
                    content = base64.b64decode(payload['content_base64'], validate=True) if payload.get('content_base64') else None
                    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
                    def import_artifact():
                        if content is not None:
                            return program_artifacts.import_bytes(content, payload.get('name', ''), payload.get('version', ''), filename=payload.get('filename', 'program'))
                        return program_artifacts.import_file(payload['path'], payload.get('name', ''), payload.get('version', ''))
                    self._json(HTTPStatus.ACCEPTED, submit_task('program-import', 'Import ELF', key, 'program-artifacts', import_artifact))
                except (ValueError, TypeError, BackendError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'error': str(exc)})
                return
            if self._l2_request("POST"):
                return
            parts = self._path()
            if (len(parts) == 4 and parts[:2] == ["v1", "instances"]
                    and parts[3] == "snapshots"):
                try:
                    if not snapshots:
                        raise BackendError("snapshot storage is unavailable")
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096:
                        raise ConfigError("invalid snapshot request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) - {"name", "mode", "forensic"}:
                        raise ConfigError("snapshot accepts name, mode and forensic")
                    name = str(payload.get("name", ""))
                    mode = str(payload.get("mode", "cold"))
                    forensic = payload.get("forensic", False)
                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "instance-snapshot", f"Snapshot {name} / {parts[2][:12]}",
                        f"snapshot:{parts[2]}:{hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()}",
                        f"instance:{parts[2]}",
                        lambda: snapshots.create(parts[2], name, mode, forensic),
                    ))
                except (ConfigError, BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 6 and parts[:2] == ["v1", "instances"]
                    and parts[3] == "snapshots" and parts[5] == "restore"):
                try:
                    if not snapshots:
                        raise BackendError("snapshot storage is unavailable")
                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "instance-snapshot-restore", f"Obnovit snapshot {parts[4][:12]}",
                        f"snapshot-restore:{parts[2]}:{parts[4]}", f"instance:{parts[2]}",
                        lambda: snapshots.restore(parts[2], parts[4]),
                    ))
                except (ConfigError, BackendError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if len(parts) == 5 and parts[0] == 'v1' and parts[1] in {'instances', 'test-drives'} and parts[3:] == ['microservices', 'check']:
                try:
                    owner = manager if parts[1] == 'instances' else test_drives
                    if not owner or (not owner.system_start and not getattr(owner, 'microservices', None)):
                        raise ServiceError('microservices_unavailable')
                    instance_id = parts[2]
                    if not uuid_is_valid(instance_id) or not owner.peek(instance_id):
                        raise ServiceError('instance_not_found', 404)
                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        'microservices-check', f'Check microservices {instance_id[:8]}',
                        f'microservices-check:{parts[1]}:{instance_id}',
                        f"{'instance' if parts[1] == 'instances' else 'test-drive'}:{instance_id}",
                        lambda: owner.check_microservices(instance_id),
                    ))
                except BackendError as exc:
                    self._json(getattr(exc, 'http', 503), {'error': str(exc)})
                return
            if len(parts) == 6 and parts[0] == 'v1' and parts[1] in {'instances', 'test-drives'} and parts[3:] == ['microservices', '00', 'configure']:
                try:
                    owner = manager if parts[1] == 'instances' else test_drives
                    if not owner or not owner.system_start:
                        raise ServiceError('microservices_unavailable')
                    if not uuid_is_valid(parts[2]) or not owner.peek(parts[2]):
                        raise ServiceError('instance_not_found', 404)
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024:
                        raise ServiceError('invalid_request', 400)
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {'enabled'} or not isinstance(payload['enabled'], bool):
                        raise ServiceError('invalid_request', 400)
                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        'system-start-configure', f'00-start {parts[2][:8]}',
                        f"system-start:{parts[1]}:{parts[2]}:{payload['enabled']}",
                        f"{'instance' if parts[1] == 'instances' else 'test-drive'}:{parts[2]}",
                        lambda: owner.system_start.configure(parts[2], payload['enabled']),
                    ))
                except (BackendError, ValueError) as exc:
                    self._json(getattr(exc, 'http', 400), {'error': str(exc)})
                return
            if len(parts) == 6 and parts[:2] == ['v1', 'instances'] and parts[3] == 'microservices' and parts[5] == 'stop':
                try:
                    if not manager.microservices:
                        raise ServiceError('microservices_unavailable')
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 4096:
                        raise ServiceError('invalid_request', 400)
                    try:
                        payload = json.loads(self.rfile.read(length))
                    except ValueError:
                        raise ServiceError('invalid_request', 400)
                    if not isinstance(payload, dict) or payload.get('contract_version') != 3:
                        raise ServiceError('invalid_request', 400)
                    result = manager.microservices.stop(parts[2], parts[4], payload.get('owner'), payload.get('run_id'))
                    self._json(HTTPStatus.OK, result)
                except (ServiceError, OSError, ValueError) as exc:
                    self._json(getattr(exc, 'http', 503), {'contract_version': 3,
                        'error': getattr(exc, 'error', 'state_unknown'), 'process_group_empty': None})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "build-warning":
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 1024:
                        raise ConfigError('invalid request size')
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {'indicate_old_build'}:
                        raise ConfigError('expected indicate_old_build only')
                    item = manager.set_build_warning(parts[2], payload['indicate_old_build'])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {'error': 'instance not found'})
                except (ValueError, ConfigError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {'error': str(exc)})
                return
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "alias":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 1024:
                        raise ConfigError("invalid alias request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) != {"alias"}:
                        raise ConfigError("expected an object containing only alias")
                    item = manager.set_alias(parts[2], payload["alias"])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(HTTPStatus.NOT_FOUND, {"error": "instance not found"})
                except (ValueError, ConfigError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "instances", "cleanup"]:
                try:
                    self._json(HTTPStatus.OK, manager.cleanup_nonpersistent())
                except (BackendError, ConfigError, OSError) as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if parts == ["v1", "firewall", "authorizations"] and firewall:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid firewall authorization size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("firewall authorization must be an object")
                    source_network = ipaddress.ip_network(
                        str(payload.get("source", "")).strip(), strict=False,
                    )
                    register_source = payload.get(
                        "register_source", source_network.prefixlen == source_network.max_prefixlen,
                    )
                    if not isinstance(register_source, bool):
                        raise ConfigError("register_source must be boolean")
                    if register_source and source_network.prefixlen != source_network.max_prefixlen:
                        raise ConfigError("only a single IPv4 or IPv6 address can be registered for instance spawn")
                    instance_id = str(payload.get("instance_id", ""))
                    runtime_profile_id = str(payload.get("runtime_profile_id", ""))
                    if instance_id and runtime_profile_id:
                        raise ConfigError("choose an existing instance or a runtime profile, not both")
                    if (instance_id or runtime_profile_id) and not register_source:
                        raise ConfigError("instance assignment requires register_source")
                    if instance_id:
                        target = manager.get(instance_id)
                        if not target or target.state not in {"starting", "running", "orphaned"}:
                            raise ConfigError("selected instance is not active")
                    if runtime_profile_id:
                        if not runtime_profiles:
                            raise ConfigError("runtime profiles are unavailable")
                        runtime_profiles.get(runtime_profile_id)
                    current_sources = manager.sources()
                    source_ip = str(source_network.network_address)
                    prospective_sources = list(current_sources)
                    source_added = register_source and source_ip not in prospective_sources
                    if source_added:
                        prospective_sources.append(source_ip)
                    namespace_cidr, _ = firewall_context(prospective_sources)
                    item = firewall.add(
                        payload, namespace_cidr, prospective_sources,
                        registered_source=source_added,
                    )
                    if source_added:
                        NetworkSettings._write(manager.sources_path, prospective_sources)
                    result: dict[str, Any] = dict(item)
                    if instance_id:
                        result["assigned_instance"] = asdict(
                            manager.attach_source(instance_id, source_ip)
                        )
                    elif runtime_profile_id:
                        result["spawned_instance"] = asdict(spawn_instance({
                            "source_ip": source_ip,
                            "runtime_profile_id": runtime_profile_id,
                            "runtime_seconds": 1800,
                            "user_id": str(payload.get("user_id", "admin-console")),
                            "config_mode": "ro", "persistent": False,
                            "parameters": {
                                "socks_port": 1080, "plaintext_port": 50080,
                                "tls_port": 50443, "http_port": 3128,
                                "cli_port": 50000, "workers": 1, "pcap_quota_mb": 100,
                            },
                        }))
                    self._json(HTTPStatus.CREATED, result)
                except (ConfigError, BackendError, OSError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "instances"]
                    and parts[3] == "sources"):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid source attachment size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("source attachment must be an object")
                    self._json(HTTPStatus.OK, asdict(
                        manager.attach_source(parts[2], payload.get("source", ""))
                    ))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 5 and parts[:3] == ["v1", "firewall", "authorizations"]
                    and parts[4] == "extend" and firewall):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid firewall extension size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("firewall extension must be an object")
                    namespace_cidr, sources = firewall_context()
                    item = firewall.extend(
                        parts[3], payload.get("additional_seconds"),
                        namespace_cidr, sources,
                    )
                    self._json(HTTPStatus.OK, item)
                except (ConfigError, BackendError, OSError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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
                        root / "smithproxy.assets", 0 if payload.get("ttl_seconds") is None else payload["ttl_seconds"],
                        str(payload.get("config_mode", "ro")),
                    )
                    self._json(HTTPStatus.CREATED, asdict(item))
                except (ConfigError, BackendError, OSError, ValueError,
                        TypeError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (parts == ["v1", "appliance-exports"] and appliance_exports
                    and builder and config_library):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_config_body:
                        raise ConfigError("invalid appliance export request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("appliance export request must be an object")
                    build_id = str(payload.get("build_id", ""))
                    config_id = str(payload.get("config_id", ""))
                    mode = str(payload.get("filesystem_mode", "plain"))
                    config_meta = config_library.get(config_id)
                    if not config_meta.get("native"):
                        raise ConfigError("appliance export requires an approved native config")
                    raw_parameters = payload.get("parameters", {})
                    if not isinstance(raw_parameters, dict):
                        raise ConfigError("parameters must be an object")
                    parameters = {str(key).upper(): str(value) for key, value in raw_parameters.items()}
                    missing = sorted(set(config_meta.get("placeholders", [])) - set(parameters))
                    if missing:
                        raise ConfigError("missing config parameters: " + ", ".join(missing))
                    artifact = builder.artifact(build_id)
                    binary = builder.resolve_binary(build_id)
                    rootfs = None
                    if mode == "rootfs":
                        builder.prepare_rootfs(build_id)
                        rootfs = builder.resolve_rootfs(build_id)
                    item = appliance_exports.create(
                        name=str(payload.get("name", "")), build=artifact, binary=binary,
                        config=config_library.resolve(config_id),
                        assets=config_library.resolve_assets(config_id),
                        filesystem_mode=mode, rootfs=rootfs,
                        profile=str(config_meta.get("profile", "custom")),
                        config_id=config_id, config_name=str(config_meta.get("name", "")),
                        parameters=parameters,
                    )
                    self._json(HTTPStatus.CREATED, item)
                except (ConfigError, BackendError, OSError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
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
            if (len(parts) == 4 and parts[:2] == ["v1", "builds"]
                    and parts[3] == "rootfs" and builder):
                try:
                    self._json(HTTPStatus.OK, builder.prepare_rootfs(parts[2]))
                except (BackendError, OSError, ValueError) as exc:
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
            if len(parts) == 4 and parts[:2] == ["v1", "instances"] and parts[3] == "upgrade":
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid request size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict) or set(payload) - {"component", "version_id", "build_id"}:
                        raise ConfigError("upgrade accepts component and version_id")
                    component = str(payload.get("component", "smithproxy"))
                    version_id = str(payload.get("version_id", payload.get("build_id", "")))
                    current = manager.peek(parts[2])
                    if not current:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    changes = {}
                    if component == "smithproxy":
                        if not builder:
                            raise BackendError("build library is unavailable")
                        changes["smithproxy_binary"] = str(builder.resolve_binary(version_id).resolve())
                        if current.filesystem_mode == "rootfs":
                            changes["rootfs_path"] = str(builder.resolve_rootfs(version_id).resolve())
                    elif component == "program":
                        if not program_artifacts or not runtime_profiles:
                            raise BackendError("program artifact library is unavailable")
                        deployment = manager._load_deployment(current.id)
                        old = deployment["start_options"]
                        contract = old.get("program", {})
                        if current.application != "elf" or not isinstance(contract.get("argv"), list):
                            raise ConfigError("instance has no versioned ELF component")
                        old_root = Path(str(old.get("rootfs_path", "")))
                        old_meta = json.loads((old_root / "runtime-image.json").read_text())
                        artifact = program_artifacts.get(version_id)
                        image = runtime_images.prepare(
                            runtime_profiles.path.parent / "runtime-images",
                            str(old_meta.get("variant", "barebone")), application="elf",
                            settings={"argv": contract["argv"][1:]},
                            executable=program_artifacts.binary(version_id),
                            executable_name=artifact.get("filename", "program"),
                        )
                        new_contract = json.loads((image / "runtime-image.json").read_text())
                        changes.update(rootfs_path=str(image.resolve()), program={
                            "application": "elf", "argv": new_contract["argv"],
                            "artifact_id": version_id,
                        })
                    elif component == "tuntom":
                        if not tuntom_builder:
                            raise BackendError("Tuntom build library is unavailable")
                        changes.update(
                            tuntom_binary=str(tuntom_builder.resolve_tunnel(version_id).resolve()),
                            tuntom_adapter=str(tuntom_builder.resolve_adapter(version_id).resolve()),
                        )
                    else:
                        raise ConfigError("unsupported upgrade component")

                    def upgrade_task() -> dict:
                        return asdict(manager.upgrade_component(
                            parts[2], component, version_id, changes,
                        ))

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "instance-upgrade", f"Upgradovat instanci {parts[2][:12]}",
                        f"upgrade:{parts[2]}:{component}:{version_id}",
                        f"instance:{parts[2]}", upgrade_task,
                    ))
                except (ConfigError, BackendError, OSError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
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
                    if payload.get('application', 'smithproxy') != 'smithproxy':
                        payload['wiring'] = profile_wiring(payload)
                        image = prepare_profile_image(payload)
                        item = runtime_profiles.save_program(payload, image_id=image.name)
                        self._json(HTTPStatus.CREATED, profile_view(item))
                        return
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
                    restart_flags(auto_restart)
                    ttl_seconds = runtime_profile_ttl(payload)
                    filesystem_mode = str(payload.get("filesystem_mode", "host"))
                    if filesystem_mode not in {"host", "rootfs"}:
                        raise ConfigError("filesystem_mode must be host or rootfs")
                    ingress_network_id, egress_network_id = network_binding(payload)
                    if filesystem_mode == "rootfs":
                        builder.prepare_rootfs(build_id)
                    if cert_bundle_id:
                        if not cert_library:
                            raise ConfigError("certificate bundles are unavailable")
                        cert_library.get(cert_bundle_id)
                    image = prepare_profile_image(payload) if filesystem_mode == 'rootfs' else None
                    item = runtime_profiles.create(
                        str(payload.get("name", "")), build_id, config_id, cert_bundle_id,
                        auto_restart, ttl_seconds,
                        ingress_network_id, egress_network_id,
                        filesystem_mode, wiring=profile_wiring(payload),
                        rootfs_variant=payload.get('rootfs_variant', 'barebone'),
                        rootfs_image=image.name if image else '',
                    )
                    self._json(HTTPStatus.CREATED, profile_view(item))
                except (ConfigError, BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "network-profiles"] and network_profiles:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid network profile size")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("network profile must be an object")
                    validate_tuntom_network_build(payload)
                    item = network_profiles.create(payload)
                    self._json(HTTPStatus.CREATED, network_profile_view(item))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "headless-endpoints"] and headless_endpoints:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid headless endpoint package size")
                    payload = json.loads(self.rfile.read(length))
                    self._json(HTTPStatus.CREATED, headless_endpoints.create(payload))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "qemu-images"] and qemu_images:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid QEMU image manifest size")
                    self._json(HTTPStatus.CREATED, qemu_images.create(
                        json.loads(self.rfile.read(length))
                    ))
                except (ConfigError, BackendError, ValueError, TypeError,
                        json.JSONDecodeError) as exc:
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
                    adopt = payload.get("adopt", True)
                    if not isinstance(adopt, bool):
                        raise ConfigError("adopt must be a boolean")

                    def build_task() -> dict:
                        builder.start(ref, build_type)
                        state = wait_for_build()
                        if not adopt:
                            return {**state, "adopted": False}
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
                        build_id = str(artifact.get("build_id", artifact.get("commit_id", "")))
                        rootfs = builder.prepare_rootfs(build_id)
                        return {
                            **state, "adopted": True, "build_id": build_id,
                            "default_config": default_config, "rootfs": rootfs,
                        }

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "adopt" if adopt else "build",
                        f"{'Adopt' if adopt else 'Build'} {ref} ({build_type})",
                        f"{'adopt' if adopt else 'build'}:{ref}:{build_type}",
                        "build", build_task,
                    ))
                except (ConfigError, BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "tuntom", "build"] and tuntom_builder:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    payload = json.loads(self.rfile.read(length)) if length else {}
                    ref = str(payload.get("ref", "master"))
                    build_type = str(payload.get("build_type", "Release"))

                    def tuntom_build_task() -> dict:
                        tuntom_builder.start(ref, build_type)
                        state = wait_for_tuntom_build()
                        artifact = next((
                            item for item in tuntom_builder.artifacts()
                            if item.get("commit_id") == state.get("revision")
                            and item.get("ref") == ref
                            and item.get("build_type") == build_type
                        ), None)
                        if not artifact:
                            raise BackendError("completed tuntom artifact is unavailable")
                        return {**state, "build_id": artifact["build_id"]}

                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "tuntom-build", f"Build Tuntom {ref} ({build_type})",
                        f"tuntom-build:{ref}:{build_type}", "tuntom-build",
                        tuntom_build_task,
                    ))
                except (ConfigError, BackendError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if parts == ["v1", "tuntom", "refs", "refresh"] and tuntom_builder:
                try:
                    self._json(HTTPStatus.ACCEPTED, submit_task(
                        "tuntom-fetch", "Fetch Tuntom branches", "tuntom-fetch",
                        "tuntom-build", lambda: (
                            tuntom_builder.request_ref_refresh(), wait_for_tuntom_refs()
                        )[1],
                    ))
                except BackendError as exc:
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
                    return asdict(spawn_instance(payload))

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
            if self._l2_request("DELETE"):
                return
            parts = self._path()
            if (len(parts) == 5 and parts[:2] == ["v1", "instances"]
                    and parts[3] == "snapshots"):
                try:
                    if not snapshots:
                        raise BackendError("snapshot storage is unavailable")
                    self._json(HTTPStatus.OK, snapshots.delete(parts[2], parts[4]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if (len(parts) == 3 and parts[:2] == ["v1", "headless-endpoints"]
                    and headless_endpoints):
                try:
                    self._json(HTTPStatus.OK, headless_endpoints.delete(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["v1", "qemu-images"] and qemu_images:
                try:
                    self._json(HTTPStatus.OK, qemu_images.delete(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:3] == ["v1", "firewall", "authorizations"]
                    and firewall):
                try:
                    item = firewall.get(parts[3])
                    if not item:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "authorization not found"})
                        return
                    current_sources = manager.sources()
                    prospective_sources = list(current_sources)
                    if item.get("registered_source"):
                        network = ipaddress.ip_network(str(item["source"]), strict=False)
                        source_ip = str(network.network_address)
                        still_registered = any(
                            current.get("authorization_id") != parts[3]
                            and current.get("registered_source")
                            and current.get("source") == item.get("source")
                            for current in firewall.load()["authorizations"]
                        )
                        if not still_registered and source_ip in prospective_sources:
                            prospective_sources.remove(source_ip)
                    namespace_cidr, _ = firewall_context(prospective_sources)
                    deleted = firewall.delete(parts[3], namespace_cidr, prospective_sources)
                    if prospective_sources != current_sources:
                        NetworkSettings._write(manager.sources_path, prospective_sources)
                    self._json(HTTPStatus.OK, deleted)
                except (ConfigError, BackendError, OSError, ValueError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if (len(parts) == 4 and parts[:2] == ["v1", "runtime-profiles"]
                    and parts[3] == "work-files" and runtime_profiles):
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > max_body:
                        raise ConfigError("invalid profile work file delete request")
                    payload = json.loads(self.rfile.read(length))
                    if not isinstance(payload, dict):
                        raise ConfigError("request body must be an object")
                    item = runtime_profiles.delete_work_file(
                        parts[2], str(payload.get("path", "")),
                    )
                    self._json(HTTPStatus.OK, item) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "work file not found"}
                    )
                except (ConfigError, BackendError, OSError, json.JSONDecodeError) as exc:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["v1", "test-drives"] and test_drives:
                try:
                    item = test_drives.destroy(parts[2])
                    self._json(HTTPStatus.OK, asdict(item)) if item else self._json(
                        HTTPStatus.NOT_FOUND, {"error": "test drive not found"}
                    )
                except BackendError as exc:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(exc)})
                return
            if (len(parts) == 3 and parts[:2] == ["v1", "appliance-exports"]
                    and appliance_exports):
                try:
                    self._json(HTTPStatus.OK, appliance_exports.delete(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
                return
            if len(parts) == 3 and parts[:2] == ["v1", "builds"] and builder:
                build_id = parts[2]
                usage = build_usage(build_id)
                if any(usage.values()):
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
            if (len(parts) == 4 and parts[:3] == ["v1", "tuntom", "builds"]
                    and tuntom_builder):
                build_id = parts[3]
                usage = [
                    {"network_profile_id": item.get("network_profile_id", ""),
                     "name": item.get("name", "")}
                    for item in (network_profiles.list("egress") if network_profiles else [])
                    if item.get("tuntom_build_id") == build_id
                ]
                active_instances = [
                    {"instance_id": item.id, "state": item.state}
                    for item in manager.snapshot()
                    if item.tuntom_build_id == build_id
                    and (item.desired_state == "running" or item.state in {"starting", "running", "orphaned"})
                ]
                snapshot_usage = snapshots.usage("tuntom", build_id) if snapshots else []
                if usage or active_instances or snapshot_usage:
                    self._json(HTTPStatus.CONFLICT, {
                        "error": "tuntom build is referenced by a network profile or active Slice",
                        "network_profiles": usage,
                        "instances": active_instances,
                        "snapshots": snapshot_usage,
                    })
                    return
                try:
                    item = tuntom_builder.delete_artifact(build_id)
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
            if (len(parts) == 3 and parts[:2] == ["v1", "network-profiles"]
                    and network_profiles):
                try:
                    item = network_profiles.get(parts[2])
                    if runtime_profiles and any(
                        parts[2] in {
                            profile.get("ingress_network_profile_id"),
                            profile.get("egress_network_profile_id"),
                        }
                        for profile in runtime_profiles.list()
                    ):
                        self._json(HTTPStatus.CONFLICT, {
                            "error": "network profile is referenced by a runtime profile",
                        })
                        return
                    self._json(HTTPStatus.OK, network_profiles.delete(parts[2]))
                except BackendError as exc:
                    self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
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
           firewall: FirewallManager | None = None,
           network_settings: NetworkSettings | None = None,
           interval_seconds: float = 2.0, stopping: threading.Event | None = None) -> None:
    stopping = stopping or threading.Event()
    while not stopping.is_set():
        try:
            manager.expire_cli_sessions()
            manager.reconcile_orphans()
            manager.list()
            manager.cleanup_stopped()
            if test_drives:
                test_drives.list()
            if firewall and network_settings:
                firewall.namespace_cidr_v6 = network_settings.get()["namespace_cidr_v6"]
                firewall.reconcile(
                    network_settings.get()["namespace_cidr"], manager.sources(),
                )
        except Exception as exc:
            print(f"instance reconciliation failed: {exc}")
        stopping.wait(interval_seconds)


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
        allowed = {"cli", "shell"} if drive_terminal else {"cli", "gdb", "netns"}
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
            elif terminal_kind == "netns":
                transport = manager.open_netns_transport(parts[2])
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
                if terminal_kind == 'netns':
                    control = json.loads(data)
                    if control.get('type') == 'resize':
                        transport.resize(control.get('cols'), control.get('rows'))
                    elif control.get('type') == 'input' and isinstance(control.get('data'), str):
                        transport.sendall(control['data'].encode('utf-8'))
                    else:
                        raise ValueError('invalid NetNS terminal message')
                else:
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
            elif terminal_kind == "netns":
                manager.close_netns_transport(parts[2], transport)
            else:
                transport.close()
            reader.join(timeout=2)

    return handler


def main() -> None:
    token = os.environ.get("CZ_RUNNER_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("CZ_RUNNER_TOKEN must contain at least 32 characters")
    # Keep this reference alive until process exit. Children do not inherit fd.
    runner_lock = acquire_runner_lock(Path(os.environ.get(
        "CZ_RUNNER_STATE_DIR", "/var/lib/capture-zone-runner")))
    network_settings = NetworkSettings(
        Path(os.environ.get(
            "CZ_RUNNER_NETWORK_SETTINGS", "/var/lib/capture-zone-runner/network-settings.json"
        )),
        Path(os.environ.get(
            "CZ_RUNNER_NETWORK_ALLOCATIONS", "/var/lib/capture-zone-runner/network-allocations.json"
        )),
    )
    firewall = FirewallManager(Path(os.environ.get(
        "CZ_RUNNER_FIREWALL", "/var/lib/capture-zone-runner/firewall.json"
    )))
    namespace_backend = NamespaceBackend(
        os.environ.get("CZ_RUNNER_SMITHPROXY", "/usr/local/lib/capture-zone/smithproxy"),
        network_settings,
    )
    manager = Manager(
        Path(os.environ.get("CZ_RUNNER_STATE_DIR", "/var/lib/capture-zone-runner")),
        Path(os.environ.get(
            "CZ_RUNNER_RUNTIME_DIR", "/var/lib/capture-zone-runner/instances",
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
    network_profiles = NetworkProfileLibrary(Path(os.environ.get(
        "CZ_RUNNER_NETWORK_PROFILES", "/var/lib/capture-zone-runner/network-profiles.json"
    )))
    headless_endpoints = HeadlessEndpointLibrary(Path(os.environ.get(
        "CZ_RUNNER_HEADLESS_ENDPOINTS",
        "/var/lib/capture-zone-runner/headless-endpoints.json",
    )))
    qemu_images = QemuImageLibrary(
        Path(os.environ.get("CZ_RUNNER_QEMU_IMAGES", "/var/lib/capture-zone-runner/qemu-images")),
        Path(os.environ.get("CZ_RUNNER_QEMU_IMPORT", "/var/lib/capture-zone-runner/import/qemu")),
    )
    cert_library = CertBundleLibrary(Path(os.environ.get(
        "CZ_RUNNER_CERT_LIBRARY", "/var/lib/capture-zone-runner/cert-library"
    )))
    config_previews = ConfigPreviewLibrary(Path(os.environ.get(
        "CZ_RUNNER_CONFIG_PREVIEWS", "/var/lib/capture-zone-runner/config-previews"
    )))
    appliance_exports = ApplianceExportLibrary(Path(os.environ.get(
        "CZ_RUNNER_APPLIANCE_EXPORTS", "/var/lib/capture-zone-runner/appliance-exports"
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
    tuntom_builder = TuntomBuilder(
        Path(os.environ.get(
            "CZ_RUNNER_TUNTOM_SOURCE_DIR", "/var/lib/capture-zone-runner/tuntom-src"
        )),
        Path(os.environ.get(
            "CZ_RUNNER_TUNTOM_BUILDS", "/var/lib/capture-zone-runner/tuntom-builds"
        )),
        os.environ.get(
            "CZ_RUNNER_TUNTOM_REPOSITORY", "https://github.com/astibal/tuntom.git"
        ),
        int(os.environ["CZ_RUNNER_BUILD_JOBS"])
        if os.environ.get("CZ_RUNNER_BUILD_JOBS") else None,
    )
    tuntom_builder.start_ref_refresh(
        int(os.environ.get("CZ_RUNNER_REF_REFRESH_SECONDS", "300"))
    )
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
        default_ttl=int(os.environ.get("CZ_RUNNER_TEST_DRIVE_TTL", "0")),
        max_ttl=int(os.environ.get("CZ_RUNNER_TEST_DRIVE_MAX_TTL", "7200")),
        expired_retention_seconds=int(os.environ.get(
            "CZ_RUNNER_TEST_DRIVE_RETENTION", "10800"
        )),
    )
    # Make portal-owned processes visible even when their state record was lost.
    # Discovery is intentionally non-destructive; an administrator decides
    # whether an orphaned unit should be stopped.
    initial_network = network_settings.get()
    firewall.namespace_cidr_v6 = initial_network["namespace_cidr_v6"]
    firewall.apply(initial_network["namespace_cidr"], manager.sources())
    host = os.environ.get("CZ_RUNNER_HOST", "127.0.0.1")
    port = int(os.environ.get("CZ_RUNNER_PORT", "9080"))
    ws_port = int(os.environ.get("CZ_RUNNER_WS_PORT", "9081"))
    stopping = threading.Event()
    manager.system_start = SystemStart(manager)
    namespace_backend.system_start = manager.system_start
    test_drives.system_start = SystemStart(test_drives, test_drive=True)
    namespace_backend.test_drive_system_start = test_drives.system_start
    def resolve_l2_instance(instance_id: str) -> dict | None:
        # Atomic files avoid manager -> L2 / L2 -> manager lock inversion.
        try:
            return json.loads(manager._state_path(instance_id).read_text())
        except FileNotFoundError:
            return None

    l2_segments = L2Segments(manager.state_dir.parent / "l2-segments.json", resolve_l2_instance)
    manager.system_start.wiring = l2_segments
    manager.l2_segments = l2_segments
    namespace_backend.wiring = l2_segments
    # Recovery must have the same Wiring/00-start hooks as a new spawn.
    manager.reconcile_orphans()
    test_drives.cleanup_orphans()
    threading.Thread(target=l2_segments.run, args=(stopping,), daemon=True,
                     name="l2-reconciler").start()
    microservice_image = Path(os.environ.get('CZ_RUNNER_MICROSERVICE_ROOTFS',
        str(manager.runtime_root.parent / 'microservice-rootfs' / 'current')))
    if microservice_image.is_dir():
        manager.microservices = Microservices(manager, SystemdServices(microservice_image),
            interval=float(os.environ.get('CZ_RUNNER_MICROSERVICE_INTERVAL', '60')))
        namespace_backend.microservices = manager.microservices
        for instance in manager.snapshot():
            manager.microservices.provision(instance.id)
        threading.Thread(target=manager.microservices.run, args=(stopping,),
                         daemon=True, name='microservice-supervisor').start()
    def system_start_supervisor():
        while not stopping.is_set():
            if not manager.microservices:
                for instance in manager.snapshot():
                    try:
                        manager.check_microservices(instance.id)
                    except Exception as exc:
                        print(f'00-start {instance.id}: {type(exc).__name__}: {exc}')
            for drive in test_drives.snapshot():
                try:
                    with test_drives.lock:
                        current = test_drives.peek(drive.id)
                        if current:
                            test_drives.system_start.check(current)
                except Exception as exc:
                    print(f'00-start Test Drive {drive.id}: {type(exc).__name__}: {exc}')
            stopping.wait(60)
    threading.Thread(target=system_start_supervisor, daemon=True, name='system-start').start()
    threading.Thread(
        target=reaper, args=(manager, test_drives, firewall, network_settings),
        kwargs={"stopping": stopping},
        daemon=True, name="instance-reaper"
    ).start()
    server = ThreadingHTTPServer((host, port), handler_factory(
        manager, token, builder=builder, config_library=config_library,
        runtime_profiles=runtime_profiles,
        cert_library=cert_library,
        config_previews=config_previews,
        tasks=tasks,
        network_settings=network_settings,
        test_drives=test_drives,
        firewall=firewall,
        network_profiles=network_profiles,
        l2_segments=l2_segments,
        tuntom_builder=tuntom_builder,
        qemu_images=qemu_images,
        appliance_exports=appliance_exports,
        headless_endpoints=headless_endpoints,
    ))
    ws_server = serve(
        websocket_handler_factory(manager, token, builder, test_drives), host, ws_port,
        compression=None, max_size=64 * 1024, server_header="CaptureZoneRunner/0.1",
    )
    threading.Thread(target=ws_server.serve_forever, daemon=True, name="runner-websocket").start()

    def request_shutdown(_signum, _frame) -> None:
        stopping.set()
        tasks.stop_accepting()
        notify_systemd("STOPPING=1")
        # BaseServer.shutdown must be called from a thread other than serve_forever.
        threading.Thread(target=server.shutdown, daemon=True, name="runner-shutdown").start()

    signal.signal(signal.SIGINT, request_shutdown)
    signal.signal(signal.SIGTERM, request_shutdown)
    try:
        notify_systemd("READY=1\nSTATUS=API ready; reconciling deployments in background")
        server.serve_forever()
    finally:
        stopping.set()
        tasks.stop_accepting()
        ws_server.shutdown()
        manager.shutdown(
            stop_instances=os.environ.get("CZ_RUNNER_STOP_INSTANCES_ON_EXIT", "0") == "1"
        )
        server.server_close()


if __name__ == "__main__":
    main()
