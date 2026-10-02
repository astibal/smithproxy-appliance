from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .systemd import BackendError


class RuntimeProfileLibrary:
    """Small atomic JSON store binding one archived binary to one config bundle."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

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
               auto_restart: bool = False, ttl_seconds: int | None = 1800) -> dict:
        clean_name = name.strip()[:128]
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise BackendError("runtime profile name is invalid")
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
               ttl_seconds: int | None = 1800) -> dict:
        clean_name = name.strip()[:128]
        if not clean_name or any(ord(char) < 32 for char in clean_name):
            raise BackendError("runtime profile name is invalid")
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
            return item
