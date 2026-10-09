"""Materialized, named instance snapshots with optional live forensic evidence."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .deployments import atomic_json
from .systemd import BackendError


NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,127}")
MODES = {"hot", "cold", "stop"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class SnapshotManager:
    def __init__(self, root: Path, manager: Any) -> None:
        self.root = Path(root)
        self.manager = manager
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _instance_root(self, instance_id: str) -> Path:
        return self.root / instance_id

    def _path(self, instance_id: str, snapshot_id: str) -> Path:
        try:
            if str(uuid.UUID(snapshot_id)) != snapshot_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid snapshot id") from exc
        return self._instance_root(instance_id) / snapshot_id

    @staticmethod
    def _metadata(path: Path) -> dict:
        try:
            value = json.loads((path / "snapshot.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError("snapshot metadata is unavailable") from exc
        if value.get("schema") != 1:
            raise BackendError("unsupported snapshot schema")
        return value

    @staticmethod
    def _public(value: dict) -> dict:
        """Never expose private deployment options, runtime secrets or config values."""
        return {key: item for key, item in value.items() if key not in {"deployment", "instance"}}

    def list(self, instance_id: str) -> list[dict]:
        root = self._instance_root(instance_id)
        if not root.is_dir():
            return []
        result = []
        for path in root.iterdir():
            if path.is_dir() and not path.name.startswith("."):
                try:
                    result.append(self._public(self._metadata(path)))
                except BackendError:
                    continue
        return sorted(result, key=lambda item: item.get("created_at", ""), reverse=True)

    def get(self, instance_id: str, snapshot_id: str) -> dict:
        return self._public(self._metadata(self._path(instance_id, snapshot_id)))

    def usage(self, component: str, version_id: str) -> list[dict]:
        result = []
        for instance_root in self.root.iterdir():
            if not instance_root.is_dir():
                continue
            for item in self.list(instance_root.name):
                if item.get("components", {}).get(component) == version_id:
                    result.append({"instance_id": instance_root.name,
                                   "snapshot_id": item["snapshot_id"], "name": item["name"]})
        return result

    @staticmethod
    def _tree_state(root: Path) -> dict[str, list[Any]]:
        state = {}
        for path in sorted(root.rglob("*")):
            try:
                stat = path.lstat()
            except FileNotFoundError:
                continue
            if path.is_file() and not path.is_symlink():
                state[path.relative_to(root).as_posix()] = [stat.st_size, stat.st_mtime_ns]
        return state

    @staticmethod
    def _copy_tree(source: Path, target: Path, skipped: list[str]) -> None:
        target.mkdir(parents=True, mode=0o700)
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            destination = target / relative
            try:
                if path.is_symlink():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(os.readlink(path))
                elif path.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    shutil.copystat(path, destination, follow_symlinks=False)
                elif path.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, destination, follow_symlinks=False)
                else:
                    skipped.append(relative.as_posix())
            except FileNotFoundError:
                skipped.append(relative.as_posix())

    @staticmethod
    def _fsync_tree(root: Path) -> list[str]:
        failed = []
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            except OSError:
                failed.append(path.relative_to(root).as_posix())
        try:
            fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            failed.append(".")
        return failed

    @staticmethod
    def _command(path: Path, name: str, command: list[str], timeout: int = 30) -> dict:
        try:
            completed = subprocess.run(command, capture_output=True, timeout=timeout, check=False)
            content = completed.stdout + (b"\n--- stderr ---\n" + completed.stderr if completed.stderr else b"")
            path.write_bytes(content)
            return {"collector": name, "status": "collected" if completed.returncode == 0 else "failed",
                    "returncode": completed.returncode, "sha256": _hash(path), "bytes": len(content)}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"collector": name, "status": "failed", "error": str(exc)}

    def _forensics(self, instance: Any, target: Path) -> dict:
        root = target / "forensics"
        processes = root / "processes"
        cores = root / "cores"
        systemd = root / "systemd"
        network = root / "network"
        for path in (processes, cores, systemd, network):
            path.mkdir(parents=True, mode=0o700)
        collectors = []
        members = [item for item in instance.members if int(item.get("pid", 0)) > 1]
        for index, member in enumerate(members):
            pid = int(member["pid"])
            role = re.sub(r"[^A-Za-z0-9_.-]", "_", str(member.get("role", "process")))[:64]
            process_root = processes / f"{index:03d}-{role}-{pid}"
            process_root.mkdir(mode=0o700)
            identity = {"pid": pid, "role": role, "unit": str(member.get("unit", "")),
                        "capture_started_at": now()}
            try:
                identity["start_time"] = (Path("/proc") / str(pid) / "stat").read_text().split()[21]
                identity["exe"] = os.readlink(Path("/proc") / str(pid) / "exe")
            except (OSError, IndexError):
                identity["identity_error"] = "process identity unavailable"
            try:
                executable = Path("/proc") / str(pid) / "exe"
                identity["executable_sha256"] = _hash(executable)
            except OSError as exc:
                identity["executable_hash_error"] = str(exc)
            for filename in ("status", "stat", "maps", "smaps_rollup", "limits",
                             "mountinfo", "cgroup", "cmdline", "environ", "auxv",
                             "io", "sched", "syscall", "wchan", "personality"):
                source = Path("/proc") / str(pid) / filename
                try:
                    content = source.read_bytes()
                    destination = process_root / filename
                    destination.write_bytes(content)
                    collectors.append({"collector": f"proc-{filename}", "pid": pid,
                                       "status": "collected", "sha256": _hash(destination),
                                       "bytes": len(content)})
                except OSError as exc:
                    collectors.append({"collector": f"proc-{filename}", "pid": pid,
                                       "status": "failed", "error": str(exc)})
            links = {}
            try:
                for fd in (Path("/proc") / str(pid) / "fd").iterdir():
                    try:
                        links[fd.name] = os.readlink(fd)
                    except OSError as exc:
                        links[fd.name] = f"unavailable: {exc}"
            except OSError as exc:
                links["error"] = str(exc)
            (process_root / "fd-links.json").write_text(json.dumps(links, indent=2))
            namespaces = {}
            try:
                for namespace in (Path("/proc") / str(pid) / "ns").iterdir():
                    try:
                        namespaces[namespace.name] = os.readlink(namespace)
                    except OSError as exc:
                        namespaces[namespace.name] = f"unavailable: {exc}"
            except OSError as exc:
                namespaces["error"] = str(exc)
            (process_root / "namespaces.json").write_text(json.dumps(namespaces, indent=2))
            core_base = cores / f"{index:03d}-{role}-{pid}.core"
            started = time.monotonic()
            core_result = self._command(
                process_root / "gcore.log", "live-core",
                ["gcore", "-o", str(core_base), str(pid)], timeout=300,
            )
            generated = Path(str(core_base) + f".{pid}")
            if generated.is_file():
                generated.replace(core_base)
                core_result.update(status="collected", path=core_base.relative_to(target).as_posix(),
                                   sha256=_hash(core_base), bytes=core_base.stat().st_size)
            else:
                core_result.update(status="failed", error=core_result.get("error", "gcore produced no core"))
            core_result.update(pid=pid, pause_duration_seconds=round(time.monotonic() - started, 6))
            collectors.append(core_result)
            identity["capture_finished_at"] = now()
            (process_root / "identity.json").write_text(json.dumps(identity, indent=2))
        collectors.extend([
            self._command(systemd / "show.txt", "systemd-show", ["systemctl", "show", instance.slice_unit]),
            self._command(systemd / "journal.txt", "journal", ["journalctl", "--no-pager", "--output=short-precise", "-u", instance.unit]),
            self._command(network / "addresses.json", "ip-address", ["ip", "-j", "-n", instance.namespace, "address"]),
            self._command(network / "routes.json", "ip-route", ["ip", "-j", "-n", instance.namespace, "route", "show", "table", "all"]),
            self._command(network / "rules.json", "ip-rule", ["ip", "-j", "-n", instance.namespace, "rule"]),
            self._command(network / "sockets.txt", "sockets", ["ip", "netns", "exec", instance.namespace, "ss", "-apne"]),
            self._command(network / "nftables.txt", "nftables", ["ip", "netns", "exec", instance.namespace, "nft", "list", "ruleset"]),
        ])
        complete = bool(members) and all(item.get("status") == "collected"
                                         for item in collectors)
        manifest = {"schema": 1, "captured_at": now(), "live_core_required": True,
                    "status": "complete" if complete else "partial", "collectors": collectors}
        atomic_json(root / "manifest.json", manifest)
        return manifest

    def create(self, instance_id: str, name: str, mode: str = "cold",
               forensic: bool = False) -> dict:
        if not isinstance(name, str) or not NAME.fullmatch(name.strip()):
            raise BackendError("snapshot name must contain 1 to 128 safe characters")
        if mode not in MODES:
            raise BackendError("snapshot mode must be hot, cold or stop")
        if type(forensic) is not bool:
            raise BackendError("forensic must be boolean")
        with self.manager.lock:
            instance = self.manager.get(instance_id)
            if not instance or instance.state != "running":
                raise BackendError("snapshot requires a running instance")
            source = self.manager.runtime_root / instance.id
            if not source.is_dir():
                raise BackendError("instance workspace is unavailable")
            snapshot_id = str(uuid.uuid4())
            destination_root = self._instance_root(instance.id)
            destination_root.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = Path(tempfile.mkdtemp(prefix=f".{snapshot_id}.", dir=destination_root))
            frozen = stopped = microservices_stopped = False
            before = self._tree_state(source)
            warnings = []
            forensic_result = None
            try:
                if forensic:
                    forensic_result = self._forensics(instance, temporary)
                if mode == "cold":
                    self.manager.backend.freeze_instance(instance.unit)
                    frozen = True
                elif mode == "stop":
                    if self.manager.microservices:
                        self.manager.microservices.stop_instance(instance.id)
                        microservices_stopped = True
                    self.manager.backend.stop_instance_process(instance.unit)
                    stopped = True
                sync_failures = self._fsync_tree(source)
                if sync_failures:
                    warnings.append({"fsync_failed": sync_failures})
                skipped = []
                self._copy_tree(source, temporary / "data", skipped)
                if forensic_result:
                    hashes = []
                    for copied in sorted((temporary / "data").rglob("*")):
                        if copied.is_file() and not copied.is_symlink():
                            hashes.append({"path": copied.relative_to(temporary / "data").as_posix(),
                                           "size": copied.stat().st_size, "sha256": _hash(copied)})
                    files = temporary / "forensics" / "files"
                    files.mkdir(mode=0o700, exist_ok=True)
                    (files / "hashes.json").write_text(json.dumps(hashes, indent=2))
                after = self._tree_state(source)
                changed = sorted(key for key in set(before) | set(after) if before.get(key) != after.get(key))
                if skipped:
                    warnings.append({"skipped_special_or_disappeared": skipped})
                if changed:
                    warnings.append({"changed_during_copy": changed})
                deployment = self.manager._load_deployment(instance.id)
                size_bytes = sum(path.stat().st_size for path in temporary.rglob("*")
                                 if path.is_file() and not path.is_symlink())
                metadata = {
                    "schema": 1, "snapshot_id": snapshot_id, "instance_id": instance.id,
                    "name": name.strip(), "parent_snapshot_id": instance.snapshot_id,
                    "snapshot_path": [*instance.snapshot_path, name.strip()],
                    "mode": mode, "consistency": {
                        "hot": "best-effort-live-copy", "cold": "main-process-frozen",
                        "stop": "all-instance-processes-stopped",
                    }[mode],
                    "forensic": forensic,
                    "forensic_status": forensic_result.get("status") if forensic_result else "not-requested",
                    "created_at": now(), "warnings": warnings,
                    "size_bytes": size_bytes,
                    "components": self.manager.component_versions(instance),
                    "instance": asdict(instance), "deployment": deployment,
                }
                atomic_json(temporary / "snapshot.json", metadata)
                self._fsync_tree(temporary)
                temporary.replace(self._path(instance.id, snapshot_id))
                instance.snapshot_id = snapshot_id
                instance.snapshot_path = metadata["snapshot_path"]
                self.manager._save(instance)
                return self._public(metadata)
            finally:
                if frozen:
                    self.manager.backend.thaw_instance(instance.unit)
                if stopped:
                    self.manager.restart_preserved(instance.id)
                if microservices_stopped and self.manager.microservices:
                    self.manager.microservices.check_instance(instance.id)
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)

    def restore(self, instance_id: str, snapshot_id: str) -> dict:
        with self.manager.lock:
            instance = self.manager.get(instance_id)
            if not instance or instance.state != "running":
                raise BackendError("snapshot restore requires a running instance")
            snapshot = self._metadata(self._path(instance.id, snapshot_id))
            source = self._path(instance.id, snapshot_id) / "data"
            workspace = self.manager.runtime_root / instance.id
            backup = workspace.with_name(f".{workspace.name}.restore-{uuid.uuid4().hex}")
            if self.manager.microservices:
                self.manager.microservices.stop_instance(instance.id)
            self.manager.backend.stop_instance_process(instance.unit)
            try:
                workspace.rename(backup)
                skipped = []
                self._copy_tree(source, workspace, skipped)
                if skipped:
                    raise BackendError("snapshot contains unsupported special files")
                atomic_json(self.manager._deployment_path(instance.id), snapshot["deployment"])
                restored = snapshot.get("instance", {})
                for field in ("build_id", "tuntom_build_id", "program_artifact_id",
                              "config_id", "cert_bundle_id", "filesystem_mode"):
                    if field in restored:
                        setattr(instance, field, restored[field])
                instance.snapshot_id = snapshot_id
                instance.snapshot_path = list(snapshot.get("snapshot_path", []))
                instance.result = f"snapshot-restored:{snapshot_id}"
                self.manager._save(instance)
                self.manager.restart_preserved(instance.id)
                if self.manager.microservices:
                    self.manager.microservices.check_instance(instance.id)
                shutil.rmtree(backup, ignore_errors=True)
                return {**self._public(snapshot), "restored_at": now()}
            except Exception:
                if workspace.exists():
                    shutil.rmtree(workspace, ignore_errors=True)
                if backup.exists():
                    backup.rename(workspace)
                self.manager.restart_preserved(instance.id)
                if self.manager.microservices:
                    self.manager.microservices.check_instance(instance.id)
                raise

    def delete(self, instance_id: str, snapshot_id: str) -> dict:
        path = self._path(instance_id, snapshot_id)
        metadata = self._metadata(path)
        shutil.rmtree(path)
        return {**self._public(metadata), "deleted": True}
