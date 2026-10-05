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
    "ingress_cidr": "10.100.0.0/16",
    "ingress_cidr_v6": "fd42:ca7:100::/64",
    "namespace_cidr": "10.200.0.0/16",
    "allocation_prefix": 30,
    "namespace_cidr_v6": "fd42:ca7:200::/64",
    "allocation_prefix_v6": 126,
    "fabric_cidr": "10.240.0.0/24",
    "fabric_cidr_v6": "fd42:ca7:240::/120",
    "fabric_interface": "",
    "fabric_link_mode": "ipvlan-l3",
    "egress_mode": "masquerade",
    "sas_route_via": "",
    "sas_route_via_v6": "",
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
        try:
            ingress_network = ipaddress.ip_network(
                str(value.get("ingress_cidr", DEFAULTS["ingress_cidr"])), strict=True,
            )
        except ValueError as exc:
            raise ConfigError(f"ingress_cidr must be a canonical IPv4 CIDR: {exc}") from exc
        if ingress_network.version != 4 or not 16 <= ingress_network.prefixlen <= 29:
            raise ConfigError("ingress_cidr must be IPv4 with prefix length /16 through /29")
        if ingress_network.overlaps(network):
            raise ConfigError("ingress and egress IPv4 pools must not overlap")
        prefix = int(value.get("allocation_prefix", 30))
        if prefix != 30:
            raise ConfigError("allocation_prefix is fixed at /30")
        try:
            network_v6 = ipaddress.ip_network(
                str(value.get("namespace_cidr_v6", DEFAULTS["namespace_cidr_v6"])),
                strict=True,
            )
        except ValueError as exc:
            raise ConfigError(f"namespace_cidr_v6 must be a canonical IPv6 CIDR: {exc}") from exc
        if network_v6.version != 6 or not 48 <= network_v6.prefixlen <= 120:
            raise ConfigError("namespace_cidr_v6 must be IPv6 with prefix length /48 through /120")
        try:
            ingress_network_v6 = ipaddress.ip_network(
                str(value.get("ingress_cidr_v6", DEFAULTS["ingress_cidr_v6"])), strict=True,
            )
        except ValueError as exc:
            raise ConfigError(f"ingress_cidr_v6 must be a canonical IPv6 CIDR: {exc}") from exc
        if ingress_network_v6.version != 6 or not 48 <= ingress_network_v6.prefixlen <= 120:
            raise ConfigError("ingress_cidr_v6 must be IPv6 with prefix length /48 through /120")
        if ingress_network_v6.overlaps(network_v6):
            raise ConfigError("ingress and egress IPv6 pools must not overlap")
        try:
            fabric_network = ipaddress.ip_network(
                str(value.get("fabric_cidr", DEFAULTS["fabric_cidr"])), strict=True,
            )
            fabric_network_v6 = ipaddress.ip_network(
                str(value.get("fabric_cidr_v6", DEFAULTS["fabric_cidr_v6"])), strict=True,
            )
        except ValueError as exc:
            raise ConfigError(f"fabric CIDR must be canonical: {exc}") from exc
        if fabric_network.version != 4 or not 24 <= fabric_network.prefixlen <= 29:
            raise ConfigError("fabric_cidr must be IPv4 with prefix length /24 through /29")
        if fabric_network_v6.version != 6 or not 64 <= fabric_network_v6.prefixlen <= 120:
            raise ConfigError("fabric_cidr_v6 must be IPv6 with prefix length /64 through /120")
        if fabric_network.overlaps(network) or fabric_network.overlaps(ingress_network):
            raise ConfigError("fabric IPv4 pool must not overlap ingress or egress pools")
        if fabric_network_v6.overlaps(network_v6) or fabric_network_v6.overlaps(ingress_network_v6):
            raise ConfigError("fabric IPv6 pool must not overlap ingress or egress pools")
        prefix_v6 = int(value.get("allocation_prefix_v6", 126))
        if prefix_v6 != 126:
            raise ConfigError("allocation_prefix_v6 is fixed at /126")
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
        route_via_v6 = str(value.get("sas_route_via_v6", "")).strip()
        if route_via_v6:
            try:
                if ipaddress.ip_address(route_via_v6).version != 6:
                    raise ValueError
            except ValueError as exc:
                raise ConfigError("sas_route_via_v6 must be an IPv6 address") from exc
        interface = str(value.get("sas_interface", "")).strip()
        if interface and (len(interface) > 15 or not all(
            character.isalnum() or character in "_.-" for character in interface
        )):
            raise ConfigError("sas_interface is invalid")
        fabric_interface = str(value.get("fabric_interface", "")).strip()
        if fabric_interface and (len(fabric_interface) > 15 or not all(
            character.isalnum() or character in "_.-" for character in fabric_interface
        )):
            raise ConfigError("fabric_interface is invalid")
        fabric_link_mode = str(value.get("fabric_link_mode", "ipvlan-l3"))
        if fabric_link_mode not in {"ipvlan-l3", "ipvlan-l2"}:
            raise ConfigError("fabric_link_mode must be ipvlan-l3 or ipvlan-l2")
        table_start = int(value.get("route_table_start", 60000))
        mark_start = int(value.get("mark_start", 0x10000000))
        capacity = min(
            network.num_addresses // 4, network_v6.num_addresses // 4,
            ingress_network.num_addresses // 4, ingress_network_v6.num_addresses // 4,
            fabric_network.num_addresses - 2, fabric_network_v6.num_addresses - 1,
        )
        if table_start < 1000 or table_start + (capacity * 2) >= 2**31:
            raise ConfigError("route table range is invalid for this CIDR")
        if mark_start < 1 or mark_start + (capacity * 2) > 0xFFFFFFFF:
            raise ConfigError("packet mark range is invalid for this CIDR")
        return {
            "schema": 1, "ingress_cidr": str(ingress_network),
            "ingress_cidr_v6": str(ingress_network_v6),
            "namespace_cidr": str(network), "allocation_prefix": 30,
            "namespace_cidr_v6": str(network_v6), "allocation_prefix_v6": 126,
            "fabric_cidr": str(fabric_network),
            "fabric_cidr_v6": str(fabric_network_v6),
            "fabric_interface": fabric_interface,
            "fabric_link_mode": fabric_link_mode,
            "egress_mode": egress_mode, "sas_route_via": route_via,
            "sas_route_via_v6": route_via_v6,
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
                    key for key in (
                        "ingress_cidr", "ingress_cidr_v6", "namespace_cidr",
                        "namespace_cidr_v6", "route_table_start", "mark_start",
                        "fabric_cidr", "fabric_cidr_v6", "fabric_interface",
                        "fabric_link_mode",
                    )
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
        capacity = min(
            ipaddress.ip_network(settings["namespace_cidr"]).num_addresses // 4,
            ipaddress.ip_network(settings["namespace_cidr_v6"]).num_addresses // 4,
            ipaddress.ip_network(settings["ingress_cidr"]).num_addresses // 4,
            ipaddress.ip_network(settings["ingress_cidr_v6"]).num_addresses // 4,
            ipaddress.ip_network(settings["fabric_cidr"]).num_addresses - 2,
            ipaddress.ip_network(settings["fabric_cidr_v6"]).num_addresses - 1,
        )
        allocated_instances = len({
            str(value.get("owner_id") or allocation_id)
            for allocation_id, value in allocations.items()
            if isinstance(value, dict) and value.get("role", "egress") == "egress"
        })
        return {
            **settings,
            "capacity": capacity,
            "allocated": len(allocations),
            "allocated_instances": allocated_instances,
            "available": max(0, capacity - allocated_instances),
            "allocations": allocations,
            "sas_route_command": (
                f"ip route add {settings['namespace_cidr']} via {settings['sas_route_via']}"
                if settings["sas_route_via"] else ""
            ),
            "sas_route_command_v6": (
                f"ip -6 route add {settings['namespace_cidr_v6']} via {settings['sas_route_via_v6']}"
                if settings["sas_route_via_v6"] else ""
            ),
        }
