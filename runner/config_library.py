from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .systemd import BackendError


PLACEHOLDER_RE = re.compile(r"{{([A-Z][A-Z0-9_]*)}}")
SERVER_PLACEHOLDERS = {
    "RUNTIME_DIR", "SOCKS_PORT", "HTTP_PORT", "PLAINTEXT_PORT", "TLS_PORT",
    "CLI_PORT", "WORKERS", "PCAP_QUOTA_MB",
}


class ConfigLibrary:
    """Persistent Smithproxy config bundles with matching assets."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = threading.RLock()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    @staticmethod
    def _digest(config: Path, assets: Path) -> str:
        digest = hashlib.sha256()
        digest.update(b"smithproxy.cfg\0")
        digest.update(config.read_bytes())
        for path in sorted(item for item in assets.rglob("*") if item.is_file()):
            relative = path.relative_to(assets).as_posix().encode("utf-8")
            digest.update(relative + b"\0")
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def import_bundle(self, name: str, config: Path, assets: Path, *,
                      description: str = "", source_commit: str = "",
                      source_ref: str = "", source_kind: str = "manual",
                      source_instance: str = "", profile: str = "custom",
                      native: bool = False, normalized_build_id: str = "",
                      source_sha256: str = "", approved_by: str = "") -> dict:
        if not config.is_file() or not assets.is_dir():
            raise BackendError("configuration bundle is incomplete")
        name = name.strip()[:128]
        if not name or any(ord(char) < 32 for char in name):
            raise BackendError("configuration name is invalid")
        checksum = self._digest(config, assets)
        with self.lock:
            for item in self.list():
                same_build = source_kind == "build" and source_commit and (
                    item.get("source_kind") == "build"
                    and item.get("source_commit") == source_commit
                )
                same_active = source_kind == "active" and item.get("source_kind") == "active"
                same_preset = source_kind == "preset" and item.get("profile") == profile
                if item["sha256"] == checksum and (same_build or same_active or same_preset):
                    if source_commit and not item.get("source_commit"):
                        item.update({
                            "name": name, "source_commit": source_commit,
                            "source_ref": source_ref,
                        })
                        metadata_path = self.root / item["config_id"] / "metadata.json"
                        temporary_metadata = metadata_path.with_suffix(".tmp")
                        temporary_metadata.write_text(
                            json.dumps(item, separators=(",", ":")), encoding="utf-8"
                        )
                        os.chmod(temporary_metadata, 0o600)
                        temporary_metadata.replace(metadata_path)
                    return item
            config_id = str(uuid.uuid4())
            target = self.root / config_id
            temporary = self.root / f".{config_id}.new"
            shutil.rmtree(temporary, ignore_errors=True)
            temporary.mkdir(mode=0o700)
            stored_config = temporary / "smithproxy.cfg"
            shutil.copy2(config, stored_config)
            os.chmod(stored_config, 0o600)
            shutil.copytree(assets, temporary / "smithproxy.assets")
            metadata = {
                "config_id": config_id,
                "name": name,
                "description": description.strip()[:1000],
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_commit": source_commit,
                "source_ref": source_ref,
                "source_kind": source_kind,
                "source_instance": source_instance,
                "profile": profile,
                "native": bool(native),
                "normalized_build_id": normalized_build_id,
                "source_sha256": source_sha256,
                "approved_by": approved_by,
                "approved_at": datetime.now(timezone.utc).isoformat() if approved_by else "",
                "placeholders": sorted(
                    set(PLACEHOLDER_RE.findall(config.read_text(encoding="utf-8")))
                    - SERVER_PLACEHOLDERS
                ),
                "sha256": checksum,
                "native_sha256": hashlib.sha256(stored_config.read_bytes()).hexdigest() if native else "",
                "size_bytes": stored_config.stat().st_size,
            }
            metadata_path = temporary / "metadata.json"
            metadata_path.write_text(json.dumps(metadata, separators=(",", ":")), encoding="utf-8")
            os.chmod(metadata_path, 0o600)
            temporary.replace(target)
            return metadata

    def import_text(self, name: str, content: str, assets: Path, **metadata) -> dict:
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1024 * 1024 or b"\0" in encoded:
            raise BackendError("configuration must contain 1 to 1048576 UTF-8 bytes without NUL")
        scratch = self.root / f".upload-{uuid.uuid4()}"
        try:
            scratch.mkdir(mode=0o700)
            config = scratch / "smithproxy.cfg"
            config.write_text(content, encoding="utf-8")
            os.chmod(config, 0o600)
            return self.import_bundle(name, config, assets, **metadata)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    def update_text(self, config_id: str, name: str, content: str, *,
                    description: str = "", profile: str = "custom",
                    native: bool = False, normalized_build_id: str = "",
                    source_sha256: str = "", approved_by: str = "",
                    assets_from: Path | None = None) -> dict:
        encoded = content.encode("utf-8")
        if not encoded or len(encoded) > 1024 * 1024 or b"\0" in encoded:
            raise BackendError("configuration must contain 1 to 1048576 UTF-8 bytes without NUL")
        clean_name = name.strip()[:128]
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise BackendError("configuration name is invalid")
        with self.lock:
            current = self.get(config_id)
            target = self.root / config_id
            source_assets = assets_from or target / "smithproxy.assets"
            if not source_assets.is_dir():
                raise BackendError("configuration assets are unavailable")
            temporary = self.root / f".{config_id}.update-{uuid.uuid4()}"
            backup = self.root / f".{config_id}.backup-{uuid.uuid4()}"
            temporary.mkdir(mode=0o700)
            temporary_config = temporary / "smithproxy.cfg"
            assets = temporary / "smithproxy.assets"
            temporary_config.write_text(content, encoding="utf-8")
            os.chmod(temporary_config, 0o600)
            shutil.copytree(source_assets, assets)
            metadata = {
                **current,
                "name": clean_name,
                "description": description.strip()[:1000],
                "profile": profile,
                "native": bool(native),
                "normalized_build_id": normalized_build_id,
                "source_sha256": source_sha256,
                "approved_by": approved_by,
                "approved_at": datetime.now(timezone.utc).isoformat() if approved_by else "",
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "placeholders": sorted(
                    set(PLACEHOLDER_RE.findall(content)) - SERVER_PLACEHOLDERS
                ),
                "sha256": self._digest(temporary_config, assets),
                "native_sha256": hashlib.sha256(encoded).hexdigest() if native else "",
                "size_bytes": temporary_config.stat().st_size,
            }
            temporary_metadata = temporary / "metadata.json"
            temporary_metadata.write_text(
                json.dumps(metadata, separators=(",", ":")), encoding="utf-8"
            )
            os.chmod(temporary_metadata, 0o600)
            # Readers use the same lock. Swapping the complete directory keeps
            # config, metadata and assets from one approval transaction together.
            target.replace(backup)
            try:
                temporary.replace(target)
            except Exception:
                backup.replace(target)
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            shutil.rmtree(backup, ignore_errors=True)
            return metadata

    def _load(self, path: Path) -> dict | None:
        try:
            metadata = json.loads((path / "metadata.json").read_text(encoding="utf-8"))
            if metadata.get("config_id") != path.name or str(uuid.UUID(path.name)) != path.name:
                return None
            if not (path / "smithproxy.cfg").is_file() or not (path / "smithproxy.assets").is_dir():
                return None
            metadata.setdefault("profile", "custom")
            metadata.setdefault("native", False)
            metadata.setdefault("normalized_build_id", "")
            metadata["placeholders"] = sorted(
                set(PLACEHOLDER_RE.findall(
                    (path / "smithproxy.cfg").read_text(encoding="utf-8")
                )) - SERVER_PLACEHOLDERS
            )
            return metadata
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    def list(self) -> list[dict]:
        with self.lock:
            try:
                items = [item for path in self.root.iterdir() if path.is_dir() and (item := self._load(path))]
            except OSError:
                return []
            return sorted(items, key=lambda item: item["created_at"], reverse=True)

    def resolve(self, config_id: str) -> Path:
        try:
            if str(uuid.UUID(config_id)) != config_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid config_id") from exc
        with self.lock:
            root = self.root / config_id
            metadata = self._load(root)
            if not metadata:
                raise BackendError("selected configuration is unavailable")
            config = root / "smithproxy.cfg"
            assets = root / "smithproxy.assets"
            if self._digest(config, assets) != metadata.get("sha256"):
                raise BackendError("selected configuration failed integrity verification")
            return config

    def get(self, config_id: str) -> dict:
        self.resolve(config_id)
        metadata = self._load(self.root / config_id)
        if not metadata:
            raise BackendError("selected configuration is unavailable")
        return metadata

    def resolve_assets(self, config_id: str) -> Path:
        config = self.resolve(config_id)
        return config.parent / "smithproxy.assets"

    def read_content(self, config_id: str) -> str:
        return self.resolve(config_id).read_text(encoding="utf-8")

    def delete(self, config_id: str) -> dict | None:
        with self.lock:
            try:
                if str(uuid.UUID(config_id)) != config_id:
                    return None
            except ValueError:
                return None
            target = self.root / config_id
            metadata = self._load(target)
            if not metadata:
                return None
            shutil.rmtree(target)
            return metadata
