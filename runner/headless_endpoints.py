from __future__ import annotations

import ipaddress
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .systemd import BackendError


SAFE_IDENTITY = re.compile(r"[A-Za-z0-9_.:/-]{1,160}")


class HeadlessEndpointLibrary:
    """Filesystem-backed catalogue of single-owner Fabric endpoint packages.

    The package is a deployment identity, not a reusable profile.  Its switch
    port and tunnel ID remain bound to one Slice even while that Slice is
    stopped, so a second spawn can never clone the same Fabric endpoint.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _load(self) -> list[dict[str, Any]]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as exc:
            raise BackendError(f"cannot read headless endpoint packages: {exc}") from exc
        items = document.get("packages") if isinstance(document, dict) else None
        if not isinstance(items, list):
            raise BackendError("headless endpoint JSON must contain a packages array")
        return [item for item in items if isinstance(item, dict)]

    def _save(self, items: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema": 1, "packages": items}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @staticmethod
    def _validate(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BackendError("headless endpoint package must be an object")
        package_id = str(payload.get("package_id", "")).strip()
        try:
            if str(uuid.UUID(package_id)) != package_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("package_id must be a canonical UUID") from exc
        kind = str(payload.get("kind", "tuntom-via")).strip()
        if kind != "tuntom-via":
            raise BackendError("only tuntom-via headless endpoint packages are supported")
        fabric_port_id = str(payload.get("fabric_port_id", "")).strip()
        if not SAFE_IDENTITY.fullmatch(fabric_port_id):
            raise BackendError("fabric_port_id is invalid")
        try:
            switch_ip = str(ipaddress.ip_address(str(payload.get("switch_ip", "")).strip()))
        except ValueError as exc:
            raise BackendError("switch_ip must be an IPv4 or IPv6 address") from exc
        secret = str(payload.get("secret", "")).strip()
        if not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
            raise BackendError("secret must contain exactly 32 hex characters")
        try:
            tunnel_id = int(payload.get("tunnel_id"))
        except (TypeError, ValueError) as exc:
            raise BackendError("tunnel_id must be an integer") from exc
        if not 1 <= tunnel_id <= 255:
            raise BackendError("tunnel_id must be between 1 and 255")
        name = str(payload.get("name", fabric_port_id)).strip()[:128]
        if not name or any(ord(char) < 32 for char in name):
            raise BackendError("package name is invalid")
        return {
            "package_id": package_id,
            "kind": kind,
            "name": name,
            "fabric_port_id": fabric_port_id,
            "switch_ip": switch_ip,
            "secret": secret.lower(),
            "tunnel_id": tunnel_id,
        }

    @staticmethod
    def _view(item: dict[str, Any]) -> dict[str, Any]:
        # Never return the transport credential from catalogue endpoints.
        return {
            key: value for key, value in item.items()
            if key != "secret"
        } | {"has_secret": bool(item.get("secret"))}

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            return [self._view(item) for item in self._load()]

    def get(self, package_id: str, *, reveal: bool = False) -> dict[str, Any]:
        with self.lock:
            item = next(
                (item for item in self._load() if item.get("package_id") == package_id), None
            )
            if not item:
                raise BackendError("headless endpoint package is unavailable")
            return dict(item) if reveal else self._view(item)

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        package = self._validate(payload)
        now = datetime.now(timezone.utc).isoformat()
        item = {
            **package,
            "state": "available",
            "bound_instance_id": "",
            "reservation_id": "",
            "created_at": now,
        }
        with self.lock:
            items = self._load()
            for current in items:
                if current.get("package_id") == package["package_id"]:
                    raise BackendError("headless endpoint package already exists")
                if current.get("fabric_port_id") == package["fabric_port_id"]:
                    raise BackendError("Fabric port is already represented by another package")
                if (current.get("switch_ip"), current.get("tunnel_id")) == (
                    package["switch_ip"], package["tunnel_id"]
                ):
                    raise BackendError("switch IP and tunnel ID are already assigned")
            items.append(item)
            self._save(items)
        return self._view(item)

    def reserve(self, package_id: str, reservation_id: str) -> dict[str, Any]:
        if not SAFE_IDENTITY.fullmatch(reservation_id):
            raise BackendError("endpoint reservation identity is invalid")
        with self.lock:
            items = self._load()
            item = next((item for item in items if item.get("package_id") == package_id), None)
            if not item:
                raise BackendError("headless endpoint package is unavailable")
            if item.get("state") != "available":
                owner = item.get("bound_instance_id") or item.get("reservation_id") or "unknown"
                raise BackendError(f"headless endpoint package is already owned by {owner}")
            item["state"] = "reserved"
            item["reservation_id"] = reservation_id
            item["reserved_at"] = datetime.now(timezone.utc).isoformat()
            self._save(items)
            return dict(item)

    def bind(self, package_id: str, reservation_id: str, instance_id: str) -> dict[str, Any]:
        with self.lock:
            items = self._load()
            item = next((item for item in items if item.get("package_id") == package_id), None)
            if not item or item.get("state") != "reserved" \
                    or item.get("reservation_id") != reservation_id:
                raise BackendError("headless endpoint reservation was lost")
            item["state"] = "bound"
            item["bound_instance_id"] = instance_id
            item["reservation_id"] = ""
            item["bound_at"] = datetime.now(timezone.utc).isoformat()
            self._save(items)
            return self._view(item)

    def cancel(self, package_id: str, reservation_id: str) -> None:
        with self.lock:
            items = self._load()
            item = next((item for item in items if item.get("package_id") == package_id), None)
            if not item or item.get("state") != "reserved" \
                    or item.get("reservation_id") != reservation_id:
                return
            item["state"] = "available"
            item["reservation_id"] = ""
            item.pop("reserved_at", None)
            self._save(items)

    def delete(self, package_id: str) -> dict[str, Any]:
        with self.lock:
            items = self._load()
            item = next((item for item in items if item.get("package_id") == package_id), None)
            if not item:
                raise BackendError("headless endpoint package is unavailable")
            if item.get("state") != "available":
                raise BackendError("owned headless endpoint package cannot be deleted")
            self._save([entry for entry in items if entry.get("package_id") != package_id])
            return self._view(item)
