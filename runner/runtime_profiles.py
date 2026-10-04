from __future__ import annotations

import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .systemd import BackendError


class RuntimeProfileLibrary:
    """Small atomic JSON store binding one archived binary to one config bundle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.work_root = path.with_suffix(".work")
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.work_root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _work_path(value: str) -> Path:
        path = Path(value.strip())
        if (not value.strip() or path.is_absolute() or "\0" in value
                or any(part in {"", ".", ".."} for part in path.parts)):
            raise BackendError("work file path must be a relative path below /work")
        if path.parts[0] in {"smithproxy.cfg", "smithproxy.assets", "captures", "tmp", "run"}:
            raise BackendError(f"/work/{path.parts[0]} is managed by the runner")
        if len(path.as_posix()) > 240:
            raise BackendError("work file path is too long")
        return path

    def _profile_work_root(self, profile_id: str) -> Path:
        self.get(profile_id)
        return self.work_root / profile_id

    def list_work_files(self, profile_id: str) -> list[dict]:
        root = self._profile_work_root(profile_id)
        if not root.exists():
            return []
        result = []
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                raise BackendError("profile work directory contains a symbolic link")
            if path.is_file():
                stat = path.stat()
                result.append({
                    "path": path.relative_to(root).as_posix(),
                    "size": stat.st_size,
                    "mode": f"{stat.st_mode & 0o777:04o}",
                })
        return result

    def put_work_file(self, profile_id: str, relative_path: str, content: bytes,
                      mode: int = 0o600) -> dict:
        if len(content) > 40 * 1024:
            raise BackendError("profile work file exceeds 40 KiB")
        if mode not in {0o600, 0o640, 0o644, 0o700, 0o750, 0o755}:
            raise BackendError("unsupported work file mode")
        relative = self._work_path(relative_path)
        with self.lock:
            root = self._profile_work_root(profile_id)
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for parent in (root, *target.parents):
                if parent == self.work_root.parent:
                    break
                if parent.exists() and parent.is_symlink():
                    raise BackendError("profile work path contains a symbolic link")
                if parent == root:
                    break
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
            temporary.write_bytes(content)
            os.chmod(temporary, mode)
            temporary.replace(target)
            return next(item for item in self.list_work_files(profile_id)
                        if item["path"] == relative.as_posix())

    def delete_work_file(self, profile_id: str, relative_path: str) -> dict | None:
        relative = self._work_path(relative_path)
        with self.lock:
            root = self._profile_work_root(profile_id)
            target = root / relative
            if target.is_symlink():
                raise BackendError("profile work file must not be a symbolic link")
            if not target.is_file():
                return None
            target.unlink()
            parent = target.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            return {"path": relative.as_posix(), "deleted": True}

    def install_work_files(self, profile_id: str, destination: Path) -> None:
        """Copy the profile's immutable file snapshot into a new instance /work."""
        root = self._profile_work_root(profile_id)
        if not root.exists():
            return
        with self.lock:
            for item in self.list_work_files(profile_id):
                relative = self._work_path(item["path"])
                source = root / relative
                target = destination / relative
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, target, follow_symlinks=False)
                os.chmod(target, int(item["mode"], 8))

    def list(self) -> list[dict]:
        with self.lock:
            try:
                document = json.loads(self.path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return []
            except (OSError, json.JSONDecodeError) as exc:
                raise BackendError(f"cannot read runtime profiles: {exc}") from exc
            items = document.get("profiles") if isinstance(document, dict) else None
            if not isinstance(items, list):
                raise BackendError("runtime profile JSON must contain a profiles array")
            valid = []
            for item in items:
                try:
                    if (not isinstance(item, dict)
                            or str(uuid.UUID(str(item.get("profile_id", "")))) != item.get("profile_id")):
                        continue
                except ValueError:
                    continue
                item.setdefault("auto_restart", False)
                # Profiles created before TTL became part of the binding keep
                # the original intended 30 minute session lifetime.
                item.setdefault("ttl_seconds", 1800)
                item.setdefault("ingress_network_profile_id", "")
                item.setdefault("egress_network_profile_id", "")
                item.setdefault("filesystem_mode", "host")
                valid.append(item)
            return sorted(valid, key=lambda item: item.get("created_at", ""), reverse=True)

    def _save(self, items: list[dict]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"profiles": items}, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    def create(self, name: str, build_id: str, config_id: str, cert_bundle_id: str = "",
               auto_restart: bool = False, ttl_seconds: int | None = 1800,
               ingress_network_profile_id: str = "",
               egress_network_profile_id: str = "",
               filesystem_mode: str = "host") -> dict:
        clean_name = name.strip()[:128]
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise BackendError("runtime profile name is invalid")
        if filesystem_mode not in {"host", "rootfs"}:
            raise BackendError("filesystem_mode must be host or rootfs")
        with self.lock:
            items = self.list()
            item = {
                "profile_id": str(uuid.uuid4()),
                "name": clean_name,
                "build_id": build_id,
                "config_id": config_id,
                "cert_bundle_id": cert_bundle_id,
                "auto_restart": auto_restart,
                "ttl_seconds": ttl_seconds,
                "ingress_network_profile_id": ingress_network_profile_id,
                "egress_network_profile_id": egress_network_profile_id,
                "filesystem_mode": filesystem_mode,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            items.append(item)
            self._save(items)
            return item

    def get(self, profile_id: str) -> dict:
        try:
            if str(uuid.UUID(profile_id)) != profile_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid runtime profile id") from exc
        item = next((item for item in self.list() if item["profile_id"] == profile_id), None)
        if not item:
            raise BackendError("runtime profile is unavailable")
        return item

    def update(self, profile_id: str, name: str, build_id: str, config_id: str,
               cert_bundle_id: str = "", auto_restart: bool = False,
               ttl_seconds: int | None = 1800,
               ingress_network_profile_id: str = "",
               egress_network_profile_id: str = "",
               filesystem_mode: str = "host") -> dict:
        clean_name = name.strip()[:128]
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise BackendError("runtime profile name is invalid")
        if filesystem_mode not in {"host", "rootfs"}:
            raise BackendError("filesystem_mode must be host or rootfs")
        with self.lock:
            items = self.list()
            item = next((candidate for candidate in items if candidate["profile_id"] == profile_id), None)
            if not item:
                raise BackendError("runtime profile is unavailable")
            item.update({
                "name": clean_name,
                "build_id": build_id,
                "config_id": config_id,
                "cert_bundle_id": cert_bundle_id,
                "auto_restart": auto_restart,
                "ttl_seconds": ttl_seconds,
                "ingress_network_profile_id": ingress_network_profile_id,
                "egress_network_profile_id": egress_network_profile_id,
                "filesystem_mode": filesystem_mode,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })
            self._save(items)
            return item

    def delete(self, profile_id: str) -> dict | None:
        with self.lock:
            items = self.list()
            item = next((item for item in items if item["profile_id"] == profile_id), None)
            if not item:
                return None
            self._save([candidate for candidate in items if candidate["profile_id"] != profile_id])
            shutil.rmtree(self.work_root / profile_id, ignore_errors=True)
            return item
