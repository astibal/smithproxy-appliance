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


SAFE_INTERFACE = re.compile(r"[A-Za-z0-9_.-]{1,15}")


class NetworkProfileLibrary:
    """Atomic JSON catalogue of composable ingress and egress policies."""

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
            raise BackendError(f"cannot read network profiles: {exc}") from exc
        items = document.get("profiles") if isinstance(document, dict) else None
        if not isinstance(items, list):
            raise BackendError("network profile JSON must contain a profiles array")
        return [item for item in items if isinstance(item, dict)]

    def _save(self, items: list[dict[str, Any]]) -> None:
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"schema": 1, "profiles": items}, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(self.path)

    @staticmethod
    def _base(payload: dict[str, Any], kind: str) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()[:128]
        description = str(payload.get("description", "")).strip()[:1000]
        if not name or any(ord(char) < 32 for char in name + description):
            raise BackendError("network profile name or description is invalid")
        family = str(payload.get("address_family", "dual"))
        if family not in {"dual", "ipv4", "ipv6"}:
            raise BackendError("address_family must be dual, ipv4, or ipv6")
        return {
            "kind": kind, "name": name, "description": description,
            "address_family": family,
        }

    @staticmethod
    def _interface(value: object, default: str) -> str:
        interface = str(value or default).strip()
        if not SAFE_INTERFACE.fullmatch(interface):
            raise BackendError("network profile interface name is invalid")
        return interface

    @staticmethod
    def _cidrs(value: object) -> list[str]:
        if value in (None, ""):
            return []
        if not isinstance(value, list) or len(value) > 64:
            raise BackendError("destination_cidrs must be an array with at most 64 entries")
        result = []
        for raw in value:
            try:
                network = str(ipaddress.ip_network(str(raw).strip(), strict=False))
            except ValueError as exc:
                raise BackendError(f"invalid destination CIDR: {raw}") from exc
            if network not in result:
                result.append(network)
        return result

    @classmethod
    def validate(cls, payload: dict[str, Any], expected_kind: str = "") -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise BackendError("network profile must be an object")
        kind = str(payload.get("kind", expected_kind))
        if expected_kind and kind != expected_kind:
            raise BackendError("network profile kind cannot be changed")
        if kind == "ingress":
            item = cls._base(payload, kind)
            driver = str(payload.get("driver", "split-veth"))
            selector = str(payload.get("selector", "source"))
            if driver != "split-veth":
                raise BackendError("ingress driver must be split-veth")
            if selector not in {"source", "destination", "source-destination"}:
                raise BackendError("unsupported ingress selector")
            authorization = payload.get("require_authorization", True)
            if not isinstance(authorization, bool):
                raise BackendError("require_authorization must be boolean")
            interface = cls._interface(payload.get("interface_name"), "di0")
            if interface != "di0":
                raise BackendError("split-veth ingress interface must be di0")
            cidrs = cls._cidrs(payload.get("destination_cidrs", []))
            if selector != "source" and not cidrs:
                raise BackendError("destination selector requires at least one destination CIDR")
            item.update({
                "driver": driver, "selector": selector,
                "require_authorization": authorization,
                "interface_name": interface, "destination_cidrs": cidrs,
                "implemented": (
                    selector == "source" and interface == "di0" and authorization
                    and item["address_family"] == "dual"
                ),
            })
            return item
        if kind == "egress":
            item = cls._base(payload, kind)
            driver = str(payload.get("driver", "split-veth"))
            mode = str(payload.get("mode", "masquerade"))
            if driver != "split-veth":
                raise BackendError("egress driver must be split-veth")
            if mode not in {"masquerade", "routed"}:
                raise BackendError("egress mode must be masquerade or routed")
            interface = cls._interface(payload.get("interface_name"), "do0")
            if interface != "do0":
                raise BackendError("split-veth egress interface must be do0")
            host_interface = str(payload.get("host_interface", "")).strip()
            if host_interface and not SAFE_INTERFACE.fullmatch(host_interface):
                raise BackendError("egress host interface is invalid")
            item.update({
                "driver": driver, "mode": mode, "interface_name": interface,
                "host_interface": host_interface,
                "implemented": (
                    interface == "do0" and item["address_family"] == "dual"
                ),
            })
            return item
        raise BackendError("network profile kind must be ingress or egress")

    def list(self, kind: str = "") -> list[dict[str, Any]]:
        with self.lock:
            result = []
            for raw in self._load():
                try:
                    profile_id = str(raw.get("network_profile_id", ""))
                    if str(uuid.UUID(profile_id)) != profile_id:
                        continue
                    item = {**raw, **self.validate(raw, str(raw.get("kind", "")))}
                    item["network_profile_id"] = profile_id
                    item["created_at"] = str(raw.get("created_at", ""))
                    if raw.get("updated_at"):
                        item["updated_at"] = str(raw["updated_at"])
                except (ValueError, BackendError):
                    continue
                if not kind or item["kind"] == kind:
                    result.append(item)
            return sorted(result, key=lambda item: (item["kind"], item["name"].lower()))

    def get(self, profile_id: str, kind: str = "") -> dict[str, Any]:
        try:
            if str(uuid.UUID(profile_id)) != profile_id:
                raise ValueError
        except ValueError as exc:
            raise BackendError("invalid network profile id") from exc
        item = next((item for item in self.list(kind) if item["network_profile_id"] == profile_id), None)
        if not item:
            raise BackendError("network profile is unavailable")
        return item

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        validated = self.validate(payload)
        now = datetime.now(timezone.utc).isoformat()
        item = {
            **validated, "network_profile_id": str(uuid.uuid4()), "created_at": now,
        }
        with self.lock:
            items = self._load()
            items.append(item)
            self._save(items)
        return item

    def update(self, profile_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        current = self.get(profile_id)
        validated = self.validate(payload, current["kind"])
        updated = {
            **current, **validated, "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        with self.lock:
            items = self._load()
            replaced = False
            for index, item in enumerate(items):
                if item.get("network_profile_id") == profile_id:
                    items[index] = updated
                    replaced = True
                    break
            if not replaced:
                raise BackendError("network profile is unavailable")
            self._save(items)
        return updated

    def delete(self, profile_id: str) -> dict[str, Any] | None:
        with self.lock:
            current = self.get(profile_id)
            items = self._load()
            self._save([
                item for item in items if item.get("network_profile_id") != profile_id
            ])
            return current
