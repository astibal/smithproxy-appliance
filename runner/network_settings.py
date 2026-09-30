from __future__ import annotations

import ipaddress
import json
import os
import threading
from pathlib import Path
from typing import Any

from .config import ConfigError


DEFAULTS = {
    "schema": 1,
    "namespace_cidr": "10.200.0.0/16",
    "allocation_prefix": 30,
    "egress_mode": "masquerade",
    "sas_route_via": "",
    "sas_interface": "",
    "route_table_start": 60000,
    "mark_start": 0x10000000,
}


class NetworkSettings:
    """Small JSON-backed networking configuration and /30 lease registry."""

    def __init__(self, settings_path: Path, allocations_path: Path) -> None:
        self.settings_path = settings_path
        self.allocations_path = allocations_path
        self.lock = threading.RLock()

    @staticmethod
    def validate(value: dict[str, Any]) -> dict[str, Any]:
        try:
            network = ipaddress.ip_network(str(value.get("namespace_cidr", "")), strict=True)
        except ValueError as exc:
            raise ConfigError(f"namespace_cidr must be a canonical IPv4 CIDR: {exc}") from exc
        if network.version != 4 or not 16 <= network.prefixlen <= 29:
            raise ConfigError("namespace_cidr must be IPv4 with prefix length /16 through /29")
        prefix = int(value.get("allocation_prefix", 30))
        if prefix != 30:
            raise ConfigError("allocation_prefix is fixed at /30")
        egress_mode = str(value.get("egress_mode", "masquerade"))
        if egress_mode not in {"masquerade", "routed"}:
            raise ConfigError("egress_mode must be masquerade or routed")
        route_via = str(value.get("sas_route_via", "")).strip()
        if route_via:
            try:
                if ipaddress.ip_address(route_via).version != 4:
                    raise ValueError
            except ValueError as exc:
                raise ConfigError("sas_route_via must be an IPv4 address") from exc
        interface = str(value.get("sas_interface", "")).strip()
        if interface and (len(interface) > 15 or not all(
            character.isalnum() or character in "_.-" for character in interface
        )):
            raise ConfigError("sas_interface is invalid")
        table_start = int(value.get("route_table_start", 60000))
        mark_start = int(value.get("mark_start", 0x10000000))
        capacity = network.num_addresses // 4
        if table_start < 1000 or table_start + capacity >= 2**31:
            raise ConfigError("route table range is invalid for this CIDR")
        if mark_start < 1 or mark_start + capacity > 0xFFFFFFFF:
            raise ConfigError("packet mark range is invalid for this CIDR")
        return {
            "schema": 1, "namespace_cidr": str(network), "allocation_prefix": 30,
            "egress_mode": egress_mode, "sas_route_via": route_via,
            "sas_interface": interface, "route_table_start": table_start,
            "mark_start": mark_start,
        }

    def get(self) -> dict[str, Any]:
        with self.lock:
            try:
                value = json.loads(self.settings_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                value = dict(DEFAULTS)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ConfigError(f"cannot read networking settings: {exc}") from exc
            if not isinstance(value, dict):
                raise ConfigError("networking settings must be an object")
            return self.validate(value)

    @staticmethod
    def _write(path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
        os.chmod(temporary, 0o600)
        temporary.replace(path)

    def update(self, value: dict[str, Any]) -> dict[str, Any]:
        validated = self.validate(value)
        with self.lock:
            allocations = self.allocations()
            if allocations:
                current = self.get()
                changed = [
                    key for key in ("namespace_cidr", "route_table_start", "mark_start")
                    if validated[key] != current[key]
                ]
                if changed:
                    raise ConfigError(
                        "cannot change allocation identity while leases are active: "
                        + ", ".join(changed)
                    )
            self._write(self.settings_path, validated)
        return validated

    def allocations(self) -> dict[str, dict[str, Any]]:
        with self.lock:
            try:
                value = json.loads(self.allocations_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                return {}
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise ConfigError(f"cannot read network allocations: {exc}") from exc
            return value if isinstance(value, dict) else {}

    def save_allocations(self, value: dict[str, dict[str, Any]]) -> None:
        with self.lock:
            self._write(self.allocations_path, value)

    def release(self, instance_id: str) -> None:
        with self.lock:
            values = self.allocations()
            if values.pop(instance_id, None) is not None:
                self._write(self.allocations_path, values)

    def view(self) -> dict[str, Any]:
        settings = self.get()
        allocations = self.allocations()
        capacity = ipaddress.ip_network(settings["namespace_cidr"]).num_addresses // 4
        return {
            **settings,
            "capacity": capacity,
            "allocated": len(allocations),
            "available": max(0, capacity - len(allocations)),
            "allocations": allocations,
            "sas_route_command": (
                f"ip route add {settings['namespace_cidr']} via {settings['sas_route_via']}"
                if settings["sas_route_via"] else ""
            ),
        }
