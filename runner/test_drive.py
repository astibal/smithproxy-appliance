from __future__ import annotations

import base64
import json
import os
import pty
import pwd
import ipaddress
import math
import re
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import ConfigError, rebase_runtime_paths, render_template
from .namespace import GdbPtyTransport, NamespaceBackend
from .systemd import BackendError
from .instance_layout import ensure_type_link, prepare_layout, remove_type_link


SAFE_RELATIVE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+ -]{0,255}$")


@dataclass
class TestDrive:
    id: str
    build_id: str
    unit: str
    state: str
    created_at: str
    deadline: str
    namespace: str
    ingress_interface: str
    ingress_ip: str
    egress_interface: str
    egress_ip: str
    host_interface: str
    host_ip: str
    subnet: str
    workspace: str
    config_path: str
    binary_path: str
    cli_port: int = 50000
    pid: int = 0
    rss_bytes: int = 0
    ingress_host_interface: str = ""
    ingress_host_ip: str = ""
    ingress_subnet: str = ""
    ingress_allocation_id: str = ""
    previous_build_id: str = ""
    upgraded_at: str = ""
    upgrade_count: int = 0
    expired_at: str = ""
    config_mode: str = "rw"


class TestDriveShellTransport(GdbPtyTransport):
    def __init__(self, process: subprocess.Popen, master_fd: int, unit: str) -> None:
        super().__init__(process, master_fd)
        self.unit = unit
        self.close_lock = threading.Lock()
        self.closed = False

    def close(self) -> None:
        with self.close_lock:
            if self.closed:
                return
            self.closed = True
            # Tear down the PTY client first. Otherwise systemctl may wait for
            # systemd-run --pty while another websocket thread is still
            # blocked on the same descriptor.
            super().close()
            try:
                subprocess.run(
                    ["systemctl", "stop", self.unit], capture_output=True, text=True,
                    timeout=5, check=False,
                )
            except subprocess.TimeoutExpired:
                subprocess.run(
                    ["systemctl", "kill", "--kill-whom=all", "--signal=KILL", self.unit],
                    capture_output=True, text=True, timeout=5, check=False,
                )


class TestDriveManager:
    """Disposable two-interface binary labs with no persistent payload."""

    def __init__(self, state_dir: Path, runtime_root: Path, backend: NamespaceBackend,
                 default_ttl: int = 1800, max_ttl: int = 7200,
                 expired_retention_seconds: int = 3 * 3600) -> None:
        self.state_dir = state_dir
        self.runtime_root = runtime_root
        self.backend = backend
        self.system_start = None
        self.default_ttl = default_ttl
        self.max_ttl = max_ttl
        if expired_retention_seconds < 0:
            raise ValueError("test drive recovery retention must not be negative")
        self.expired_retention_seconds = expired_retention_seconds
        self.lock = threading.RLock()
        self.shells: dict[str, TestDriveShellTransport] = {}
        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.runtime_index = prepare_layout(runtime_root, "test-drive")

    def _state_path(self, drive_id: str) -> Path:
        return self.state_dir / f"{drive_id}.json"

    def _save(self, drive: TestDrive) -> None:
        ensure_type_link(
            self.runtime_root, "test-drive", drive.id, Path(drive.workspace).parent,
        )
        target = self._state_path(drive.id)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(asdict(drive), separators=(",", ":")), encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(target)

    def _load(self, drive_id: str) -> TestDrive | None:
        try:
            if str(uuid.UUID(drive_id)) != drive_id:
                return None
            return TestDrive(**json.loads(self._state_path(drive_id).read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _prepare_runtime_files(drive: TestDrive) -> None:
        """Repair private assets and rebase stale absolute paths before start."""
        workspace = Path(drive.workspace)
        root = workspace.parent
        assets = root / "assets"
        build_assets = Path(drive.binary_path).parent / "smithproxy.assets"
        if build_assets.is_dir():
            for source in build_assets.rglob("*"):
                relative = source.relative_to(build_assets)
                target = assets / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                elif source.is_file() and not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
        cert_cache = assets / "certs" / "default"
        cert_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        for cache_name in ("sni", "ip", "cc-sni", "cc-ip"):
            cache_dir = cert_cache / cache_name
            cache_dir.mkdir(mode=0o700, exist_ok=True)
            os.chmod(cache_dir, 0o700)
        config_path = Path(drive.config_path)
        try:
            original = config_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read Test Drive config: {exc}") from exc
        rebased = rebase_runtime_paths(original, workspace, assets)
        if rebased != original:
            temporary = config_path.with_suffix(".cfg.rebase")
            temporary.write_text(rebased, encoding="utf-8")
            os.chmod(temporary, config_path.stat().st_mode & 0o777)
            if os.geteuid() == 0:
                current = config_path.stat()
                os.chown(temporary, current.st_uid, current.st_gid)
            temporary.replace(config_path)

    def create(self, build_id: str, binary: Path, template: Path, assets: Path,
               ttl_seconds: int | None = None, config_mode: str = "ro") -> TestDrive:
        ttl = self.default_ttl if ttl_seconds is None else ttl_seconds
        if isinstance(ttl, bool) or not isinstance(ttl, int) or not 60 <= ttl <= self.max_ttl:
            raise ConfigError(f"ttl_seconds must be between 60 and {self.max_ttl}")
        if not binary.is_file() or not template.is_file() or not assets.is_dir():
            raise BackendError("test drive build bundle is incomplete")
        if config_mode not in {"ro", "rw"}:
            raise ConfigError("config_mode must be ro or rw")
        with self.lock:
            drive_id = str(uuid.uuid4())
            root = self.runtime_root / drive_id
            workspace = root / "work"
            private_run = root / "run"
            copied_assets = root / "assets"
            resolver_path = root / "resolv.conf"
            root.mkdir(mode=0o750)
            ensure_type_link(self.runtime_root, "test-drive", drive_id, root)
            workspace.mkdir(mode=0o770)
            private_run.mkdir(mode=0o700)
            shutil.copytree(assets, copied_assets)
            cert_cache = copied_assets / "certs" / "default"
            cert_cache.mkdir(parents=True, exist_ok=True, mode=0o700)
            for cache_name in ("sni", "ip", "cc-sni", "cc-ip"):
                cache_dir = cert_cache / cache_name
                cache_dir.mkdir(mode=0o700, exist_ok=True)
                os.chmod(cache_dir, 0o700)
            nobody = pwd.getpwnam("nobody")
            if os.geteuid() == 0:
                # The sandbox user may traverse the drive root, but only the
                # dedicated /work subtree is writable to it.
                os.chown(root, 0, nobody.pw_gid)
                os.chown(workspace, nobody.pw_uid, nobody.pw_gid)
            resolvers = []
            try:
                source = Path("/run/systemd/resolve/resolv.conf").read_text(encoding="utf-8")
                for line in source.splitlines():
                    if not line.startswith("nameserver "):
                        continue
                    address = line.split(maxsplit=1)[1]
                    if not ipaddress.ip_address(address).is_loopback:
                        resolvers.append(f"nameserver {address}")
            except (OSError, ValueError, IndexError):
                pass
            if not resolvers:
                resolvers = ["nameserver 1.1.1.1", "nameserver 8.8.8.8"]
            resolver_path.write_text("\n".join(resolvers) + "\noptions timeout:2 attempts:2\n")
            os.chmod(resolver_path, 0o644)
            config_path = workspace / "smithproxy.cfg"
            parameters = {
                "socks_port": 1080, "http_port": 3128,
                "plaintext_port": 50080, "tls_port": 50443,
                "cli_port": 50000, "workers": 1, "pcap_quota_mb": 100,
            }
            try:
                config_path.write_text(render_template(
                    template, parameters, workspace, assets_dir=copied_assets,
                ), encoding="utf-8")
                os.chmod(config_path, 0o660)
                if os.geteuid() == 0:
                    os.chown(config_path, nobody.pw_uid, nobody.pw_gid)
                unit, allocation, ingress, ingress_id = self.backend.start_test_drive(
                    drive_id, binary, config_path, private_run, resolver_path, ttl,
                    config_mode,
                )
            except Exception:
                shutil.rmtree(root, ignore_errors=True)
                remove_type_link(self.runtime_root, "test-drive", drive_id)
                raise
            now = datetime.now(timezone.utc)
            drive = TestDrive(
                id=drive_id, build_id=build_id, unit=unit, state="starting",
                created_at=now.isoformat(), deadline=(now + timedelta(seconds=ttl)).isoformat(),
                namespace=allocation.namespace,
                ingress_interface=ingress.guest_if, ingress_ip=ingress.guest_ip,
                egress_interface=allocation.guest_if, egress_ip=allocation.guest_ip,
                host_interface=allocation.host_if, host_ip=allocation.host_ip,
                subnet=allocation.subnet, workspace=str(workspace),
                config_path=str(config_path), binary_path=str(binary),
                ingress_host_interface=ingress.host_if,
                ingress_host_ip=ingress.host_ip,
                ingress_subnet=ingress.subnet,
                ingress_allocation_id=ingress_id,
                config_mode=config_mode,
            )
            self._save(drive)
            return drive

    def _reconcile(self, drive: TestDrive) -> TestDrive | None:
        status = self.backend.status(drive.unit)
        deadline = datetime.fromisoformat(drive.deadline)
        now = datetime.now(timezone.utc)
        if now >= deadline:
            if status.active_state in {"active", "activating", "reloading"}:
                self.backend.stop_test_drive_process(drive.unit)
            if not drive.expired_at:
                drive.expired_at = deadline.isoformat()
            expired = datetime.fromisoformat(drive.expired_at)
            drive.state = "expired"
            drive.pid = 0
            drive.rss_bytes = 0
            self._save(drive)
            if (now - expired).total_seconds() >= self.expired_retention_seconds:
                self.destroy(drive.id)
                return None
            return drive
        if status.active_state not in {"active", "activating", "reloading"}:
            drive.state = "stopped"
            drive.pid = 0
            drive.rss_bytes = 0
            self._save(drive)
            return drive
        drive.state = "running"
        drive.pid = status.main_pid
        drive.rss_bytes = self.backend.rss_bytes(status.main_pid)
        self._save(drive)
        return drive

    def list(self) -> list[TestDrive]:
        with self.lock:
            result = []
            for path in sorted(self.state_dir.glob("*.json")):
                drive = self._load(path.stem)
                if drive:
                    current = self._reconcile(drive)
                    if current:
                        result.append(current)
            return result

    def snapshot(self) -> list[TestDrive]:
        """Return atomically persisted state without blocking on lifecycle work."""
        result = []
        for path in sorted(self.state_dir.glob("*.json")):
            drive = self._load(path.stem)
            if drive:
                result.append(drive)
        return result

    def check_microservices(self, drive_id: str) -> dict:
        with self.lock:
            drive = self.peek(drive_id)
            if not drive or not self.system_start:
                raise BackendError('Test Drive or 00-start unavailable')
            return {'instance_id': drive_id, 'state': 'checked',
                    'system_start': self.system_start.check(drive),
                    'external_microservices': 'not_supported'}

    def peek(self, drive_id: str) -> TestDrive | None:
        """Read one last-reconciled Test Drive without taking the lifecycle lock."""
        return self._load(drive_id)

    def get(self, drive_id: str) -> TestDrive | None:
        with self.lock:
            drive = self._load(drive_id)
            return self._reconcile(drive) if drive else None

    def destroy(self, drive_id: str) -> TestDrive | None:
        with self.lock:
            drive = self._load(drive_id)
            if not drive:
                return None
            shell = self.shells.pop(drive_id, None)
            if shell:
                shell.close()
            self.backend.stop_test_drive(
                drive_id, drive.unit, drive.ingress_allocation_id,
                drive.ingress_host_interface,
            )
            shutil.rmtree(Path(drive.workspace).parent, ignore_errors=True)
            remove_type_link(self.runtime_root, "test-drive", drive_id)
            self._state_path(drive_id).unlink(missing_ok=True)
            return drive

    def upgrade(self, drive_id: str, build_id: str, binary: Path) -> TestDrive:
        """Dirty binary replacement preserving config, workspace and networking."""
        if not binary.is_file():
            raise BackendError("test drive upgrade binary is unavailable")
        with self.lock:
            drive = self._load(drive_id)
            if not drive:
                raise ConfigError("test drive not found")
            drive = self._reconcile(drive)
            if not drive or drive.state not in {"running", "stopped"}:
                raise ConfigError("only a running or stopped test drive can be upgraded")
            remaining = math.ceil(
                (datetime.fromisoformat(drive.deadline) - datetime.now(timezone.utc)).total_seconds()
            )
            if remaining < 5:
                raise ConfigError("test drive has less than five seconds remaining")
            root = Path(drive.workspace).parent
            drive.previous_build_id = drive.build_id
            drive.build_id = build_id
            drive.binary_path = str(binary)
            self._prepare_runtime_files(drive)
            self.backend.upgrade_test_drive(
                drive.unit, drive.namespace, binary, Path(drive.config_path),
                root / "run", root / "resolv.conf", remaining, drive.config_mode,
            )
            drive.upgraded_at = datetime.now(timezone.utc).isoformat()
            drive.upgrade_count += 1
            drive.state = "starting"
            drive.pid = 0
            drive.rss_bytes = 0
            self._save(drive)
            return drive

    def set_config_mode(self, drive_id: str, config_mode: str) -> TestDrive:
        if config_mode not in {"ro", "rw"}:
            raise ConfigError("config_mode must be ro or rw")
        with self.lock:
            drive = self._load(drive_id)
            if not drive:
                raise ConfigError("test drive not found")
            drive = self._reconcile(drive)
            if not drive:
                raise ConfigError("test drive recovery window has expired")
            if drive.config_mode == config_mode:
                return drive
            if drive.state == "running":
                remaining = math.ceil(
                    (datetime.fromisoformat(drive.deadline) - datetime.now(timezone.utc)).total_seconds()
                )
                if remaining < 5:
                    raise ConfigError("test drive has less than five seconds remaining")
                root = Path(drive.workspace).parent
                self._prepare_runtime_files(drive)
                self.backend.upgrade_test_drive(
                    drive.unit, drive.namespace, Path(drive.binary_path),
                    Path(drive.config_path), root / "run", root / "resolv.conf",
                    remaining, config_mode,
                )
                drive.state = "starting"
                drive.pid = 0
                drive.rss_bytes = 0
            drive.config_mode = config_mode
            self._save(drive)
            return drive

    def extend(self, drive_id: str, additional_seconds: int) -> TestDrive:
        if (isinstance(additional_seconds, bool) or not isinstance(additional_seconds, int)
                or not 60 <= additional_seconds <= self.max_ttl):
            raise ConfigError(f"additional_seconds must be between 60 and {self.max_ttl}")
        with self.lock:
            drive = self._load(drive_id)
            if not drive:
                raise ConfigError("test drive not found")
            drive = self._reconcile(drive)
            if not drive:
                raise ConfigError("test drive recovery window has expired")
            now = datetime.now(timezone.utc)
            current = datetime.fromisoformat(drive.deadline)
            deadline = max(now, current) + timedelta(seconds=additional_seconds)
            remaining = math.ceil((deadline - now).total_seconds())
            if remaining > self.max_ttl:
                raise ConfigError(f"test drive cannot have more than {self.max_ttl} seconds remaining")
            if drive.state == "running":
                self.backend.extend_test_drive(drive.unit, remaining)
            drive.deadline = deadline.isoformat()
            drive.expired_at = ""
            if drive.state == "expired":
                drive.state = "stopped"
            self._save(drive)
            return drive

    def restart(self, drive_id: str) -> TestDrive:
        with self.lock:
            drive = self._load(drive_id)
            if not drive:
                raise ConfigError("test drive not found")
            drive = self._reconcile(drive)
            if not drive:
                raise ConfigError("test drive recovery window has expired")
            remaining = math.ceil(
                (datetime.fromisoformat(drive.deadline) - datetime.now(timezone.utc)).total_seconds()
            )
            if remaining < 5:
                drive.deadline = (
                    datetime.now(timezone.utc) + timedelta(seconds=self.default_ttl)
                ).isoformat()
                remaining = self.default_ttl
            root = Path(drive.workspace).parent
            self._prepare_runtime_files(drive)
            self.backend.upgrade_test_drive(
                drive.unit, drive.namespace, Path(drive.binary_path),
                Path(drive.config_path), root / "run", root / "resolv.conf", remaining,
                drive.config_mode,
            )
            drive.state = "starting"
            drive.pid = 0
            drive.rss_bytes = 0
            drive.expired_at = ""
            self._save(drive)
            return drive

    def config_content(self, drive_id: str) -> tuple[TestDrive, str, Path]:
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive not found")
        path = Path(drive.config_path)
        try:
            return drive, path.read_text(encoding="utf-8"), path
        except OSError as exc:
            raise ConfigError(f"cannot read Test Drive config: {exc}") from exc

    def save_live_config(self, drive_id: str) -> tuple[TestDrive, str, str, Path]:
        with self.lock:
            drive = self.get(drive_id)
            if not drive or drive.state != "running":
                raise ConfigError("live Test Drive save requires a running process")
            if drive.config_mode != "rw":
                raise ConfigError("Test Drive config is read-only; enable write first")
            path = Path(drive.config_path)
            before = path.read_text(encoding="utf-8")
            connection = self.backend.open_test_drive_cli(drive.namespace, drive.cli_port)
            try:
                connection.settimeout(0.5)
                try:
                    connection.recv(65536)
                except TimeoutError:
                    pass
                connection.sendall(b"enable\r\nsave config\r\n")
                output = bytearray()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    try:
                        chunk = connection.recv(65536)
                    except TimeoutError:
                        continue
                    if not chunk:
                        raise BackendError("Smithproxy closed CLI during Test Drive save")
                    output.extend(chunk)
                    lowered = bytes(output).lower()
                    if b"config saved successfully" in lowered:
                        break
                    if b"error writing config" in lowered:
                        raise BackendError("Smithproxy failed to save the Test Drive config")
                else:
                    raise BackendError("Smithproxy Test Drive save config timed out")
            finally:
                connection.close()
            try:
                after = path.read_text(encoding="utf-8")
            except OSError as exc:
                raise ConfigError(f"cannot read saved Test Drive config: {exc}") from exc
            return drive, before, after, path

    def cleanup_orphans(self) -> None:
        with self.lock:
            self.backend.cleanup_test_drive_shells()
            known = {path.stem for path in self.state_dir.glob("*.json") if self._load(path.stem)}
            for drive_id, unit in self.backend.discover_test_drives():
                if drive_id not in known:
                    self.backend.stop_test_drive(drive_id, unit)
            for link in self.runtime_index.iterdir():
                if not link.is_symlink() or link.name in known:
                    continue
                target = link.resolve(strict=False)
                link.unlink()
                if target.parent == self.runtime_root:
                    shutil.rmtree(target, ignore_errors=True)
            self.list()

    def logs(self, drive_id: str, lines: int = 300) -> str:
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive not found")
        return self.backend.logs(drive.unit, lines)

    def open_cli(self, drive_id: str):
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive is not running")
        return self.backend.open_test_drive_cli(drive.namespace, drive.cli_port)

    def open_shell(self, drive_id: str):
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive is not running")
        previous = self.shells.pop(drive_id, None)
        if previous:
            previous.close()
        transport = self.backend.open_test_drive_shell(
            drive.id, drive.namespace, Path(drive.workspace),
            Path(drive.workspace).parent / "resolv.conf",
        )
        self.shells[drive_id] = transport
        return transport

    def close_shell(self, drive_id: str, transport: Any) -> None:
        with self.lock:
            current = self.shells.get(drive_id)
            transport.close()
            if current is transport:
                del self.shells[drive_id]

    @staticmethod
    def _relative(path: str) -> Path:
        if not isinstance(path, str) or not SAFE_RELATIVE.fullmatch(path) or path.startswith("/"):
            raise ConfigError("invalid workspace path")
        relative = Path(path)
        if ".." in relative.parts:
            raise ConfigError("workspace path must not escape /work")
        return relative

    def files(self, drive_id: str) -> list[dict[str, Any]]:
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive not found")
        root = Path(drive.workspace).resolve()
        result = []
        for path in sorted(root.rglob("*")):
            if len(result) >= 500:
                break
            if path.is_file() and not path.is_symlink():
                result.append({"path": str(path.relative_to(root)), "size": path.stat().st_size})
        return result

    def read_file(self, drive_id: str, path: str) -> tuple[str, bytes]:
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive not found")
        relative = self._relative(path)
        root = Path(drive.workspace).resolve()
        target = (root / relative).resolve()
        if target.parent != root and root not in target.parents:
            raise ConfigError("workspace path escapes /work")
        if not target.is_file() or target.is_symlink():
            raise ConfigError("workspace file not found")
        content = target.read_bytes()
        if len(content) > 16 * 1024 * 1024:
            raise ConfigError("workspace file exceeds 16 MiB download limit")
        return target.name, content

    def write_file(self, drive_id: str, path: str, encoded: str) -> dict[str, Any]:
        drive = self.get(drive_id)
        if not drive:
            raise ConfigError("test drive not found")
        relative = self._relative(path)
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError) as exc:
            raise ConfigError("invalid base64 file content") from exc
        if len(content) > 16 * 1024 * 1024:
            raise ConfigError("workspace file exceeds 16 MiB upload limit")
        root = Path(drive.workspace).resolve()
        target = (root / relative).resolve()
        if target.parent != root and root not in target.parents:
            raise ConfigError("workspace path escapes /work")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.upload-{uuid.uuid4().hex[:8]}")
        temporary.write_bytes(content)
        nobody = pwd.getpwnam("nobody")
        if os.geteuid() == 0:
            os.chown(temporary, nobody.pw_uid, nobody.pw_gid)
        os.chmod(temporary, 0o660)
        temporary.replace(target)
        return {"path": str(relative), "size": len(content)}
