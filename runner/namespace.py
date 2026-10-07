from __future__ import annotations
from .restart_policy import policy as restart_policy, properties as restart_properties

import ipaddress
import json
import math
import os
import pty
import pwd
import re
import select
import signal
import subprocess
import time
import uuid
import threading
import hashlib
from datetime import datetime, timezone
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .systemd import BackendError, UnitStatus
from .network_settings import NetworkSettings


SAFE_ID = re.compile(r"^[0-9a-f-]{36}$")
UNIT_RE = re.compile(r"^capture-zone-smithproxy-([0-9a-f-]{36})\.service$")
TEST_DRIVE_UNIT_RE = re.compile(r"^capture-zone-testdrive-([0-9a-f-]{36})\.service$")
TEST_DRIVE_INGRESS_NAMESPACE = uuid.UUID("7fa9bc87-6a15-44d2-90cf-f20188b07c42")
INSTANCE_INGRESS_NAMESPACE = uuid.UUID("15f53cb7-6dc9-49e7-9d8d-4df3ff1aa3f2")


def systemd_timespan_microseconds(value: str) -> int:
    """Convert systemctl's human RuntimeMaxUSec rendering back to microseconds."""
    raw = value.strip()
    if raw.isdigit():
        return int(raw)
    completed = subprocess.run(
        ["systemd-analyze", "timespan", raw], capture_output=True, text=True,
        timeout=5, check=False, env={**os.environ, "LC_ALL": "C"},
    )
    match = re.search(r"^\s*(?:μs|us):\s*([0-9]+)\s*$", completed.stdout, re.MULTILINE)
    if completed.returncode or not match:
        raise BackendError("cannot parse systemd Test Drive runtime limit")
    return int(match.group(1))


class NamespaceCliTransport:
    """Byte stream to a loopback-only CLI inside one network namespace."""

    def __init__(self, process: subprocess.Popen) -> None:
        self.process = process
        self.timeout = 0.25

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        if self.process.stdout is None:
            return b""
        readable, _, _ = select.select([self.process.stdout], [], [], self.timeout)
        if not readable:
            raise TimeoutError
        return os.read(self.process.stdout.fileno(), size)

    def sendall(self, value: bytes) -> None:
        if self.process.stdin is None or self.process.poll() is not None:
            raise BrokenPipeError("namespace CLI transport is closed")
        self.process.stdin.write(value)
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)


class GdbPtyTransport:
    """Interactive GDB process connected to a real pseudo-terminal."""

    def __init__(self, process: subprocess.Popen, master_fd: int) -> None:
        self.process = process
        self.master_fd = master_fd
        self.timeout = 0.25

    def settimeout(self, timeout: float) -> None:
        self.timeout = timeout

    def recv(self, size: int) -> bytes:
        readable, _, _ = select.select([self.master_fd], [], [], self.timeout)
        if not readable:
            raise TimeoutError
        try:
            return os.read(self.master_fd, size)
        except OSError:
            return b""

    def sendall(self, value: bytes) -> None:
        if self.process.poll() is not None:
            raise BrokenPipeError("GDB terminal is closed")
        os.write(self.master_fd, value)

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
                self.process.wait(timeout=2)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                if self.process.poll() is None:
                    try:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    self.process.wait(timeout=2)
        try:
            os.close(self.master_fd)
        except OSError:
            pass


def parse_unit_status(output: str) -> UnitStatus:
    properties = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value
    try:
        pid = int(properties.get("MainPID", "0"))
    except ValueError:
        pid = 0
    return UnitStatus(
        properties.get("ActiveState", "inactive"),
        properties.get("SubState", "dead"),
        properties.get("Result", "unknown"),
        pid,
    )
@dataclass(frozen=True)
class NetworkAllocation:
    namespace: str
    host_if: str
    guest_if: str
    host_ip: str
    guest_ip: str
    table: str
    route_table: int
    mark: int
    subnet: str = ""
    egress_mode: str = "masquerade"
    sas_interface: str = ""
    subnet_v6: str = ""
    host_ip_v6: str = ""
    guest_ip_v6: str = ""
    role: str = "egress"
    owner_id: str = ""
    topology: str = "split-veth"
    fabric_if: str = ""
    fabric_ip: str = ""
    fabric_ip_v6: str = ""
    fabric_parent: str = ""
    fabric_link_mode: str = ""
    tuntom_tunnel_id: int = 0


class NamespaceBackend:
    """Root backend: one netns with veth or tuntom VIA links per proxy."""

    def __init__(self, smithproxy_binary: str = "/usr/local/lib/capture-zone/smithproxy",
                 network_settings: NetworkSettings | None = None) -> None:
        self.smithproxy_binary = smithproxy_binary
        self.network_settings = network_settings
        self.allocation_lock = threading.RLock()
        self._ephemeral_allocations: dict[str, NetworkAllocation] = {}

    @staticmethod
    def unit_name(instance_id: str) -> str:
        return f"capture-zone-smithproxy-{instance_id}.service"

    @staticmethod
    def tuntom_unit_name(instance_id: str) -> str:
        return f"capture-zone-tuntom-adapter-{instance_id}.service"

    @staticmethod
    def tuntom_relay_unit_name(instance_id: str) -> str:
        return f"capture-zone-tuntom-relay-{instance_id}.service"

    @staticmethod
    def slice_name(instance_id: str) -> str:
        return f"capture-zone-slice-{instance_id}.slice"

    @staticmethod
    def _instance_storage_properties(config_path: Path, private_run: Path,
                                     binary_path: Path) -> list[str]:
        """Build one unit's writable filesystem and minimal capability set."""
        workspace = config_path.parent
        owner = workspace.stat()
        capture_dir = workspace / "captures"
        temp_dir = workspace / "tmp"
        for directory, mode in (
            (private_run, 0o700), (capture_dir, 0o770), (temp_dir, 0o770),
        ):
            directory.mkdir(mode=mode, exist_ok=True)
            os.chown(directory, owner.st_uid, owner.st_gid)
            os.chmod(directory, mode)
        capabilities = "CAP_NET_RAW CAP_DAC_OVERRIDE CAP_FOWNER"
        return [
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=yes",
            "--property=PrivateDevices=yes",
            "--property=PrivateTmp=yes",
            "--property=NoNewPrivileges=yes",
            f"--property=CapabilityBoundingSet={capabilities}",
            f"--property=AmbientCapabilities={capabilities}",
            f"--property=BindPaths={private_run}:/run",
            f"--property=BindPaths={capture_dir}:/var/smithproxy/data",
            # PrivateTmp hides the development runtime below host /tmp. Expose
            # only this unit's workspace and selected immutable build subtree.
            f"--property=BindPaths={workspace}:{workspace}",
            # Stable in-appliance alias shared with the Test Drive shell. This
            # keeps paths such as SSH MITM host keys portable across labs.
            f"--property=BindPaths={workspace}:/work",
            f"--property=BindReadOnlyPaths={binary_path.parent}:{binary_path.parent}",
            f"--property=ReadWritePaths={workspace} /work",
            f"--setenv=TMPDIR={temp_dir}",
        ]

    @staticmethod
    def _rootfs_execution_properties(config_path: Path, rootfs_path: str) -> tuple[list[str], str]:
        if not rootfs_path:
            return [], ""
        effective_rootfs = Path(rootfs_path).resolve()
        if not effective_rootfs.is_dir() or not (
            effective_rootfs / "usr/bin/smithproxy"
        ).is_file():
            raise BackendError("prepared Smithproxy rootfs is unavailable")
        return ([
            f"--property=RootDirectory={effective_rootfs}",
            "--property=MountAPIVFS=yes",
            "--property=WorkingDirectory=/work",
            # Preserve the current absolute runtime mountpoint without
            # creating per-instance directories in the shared rootfs.
            f"--property=TemporaryFileSystem={config_path.parent.parent}",
        ], "/usr/bin/smithproxy")

    @staticmethod
    def _legacy_allocation(instance_id: str, role: str = "egress",
                           owner_id: str = "") -> NetworkAllocation:
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance id")
        compact = instance_id.replace("-", "")
        number = int(compact[:8], 16)
        second = 100 if role == "ingress" else 200
        third = (number >> 6) & 255
        fourth = (number & 63) * 4
        owner_id = owner_id or instance_id
        suffix = owner_id.replace("-", "")[:8]
        host_prefix = "czi" if role == "ingress" else "czo"
        guest_if = "di0" if role == "ingress" else "do0"
        return NetworkAllocation(
            namespace=f"cz-{suffix}", host_if=f"{host_prefix}{suffix}", guest_if=guest_if,
            host_ip=f"10.{second}.{third}.{fourth + 1}", guest_ip=f"10.{second}.{third}.{fourth + 2}",
            table=f"cz_{suffix}",
            route_table=10000 + (number % 40000),
            mark=0x100000 + (number % 0xEFFFFF),
            subnet=f"10.{second}.{third}.{fourth}/30",
            role=role, owner_id=owner_id,
        )

    def allocation(self, instance_id: str) -> NetworkAllocation:
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance id")
        if self.network_settings:
            value = self.network_settings.allocations().get(instance_id)
            if isinstance(value, dict):
                try:
                    return NetworkAllocation(**value)
                except TypeError as exc:
                    raise BackendError(f"invalid saved network allocation: {exc}") from exc
        return self._ephemeral_allocations.get(instance_id) or self._legacy_allocation(instance_id)

    @staticmethod
    def ingress_id(instance_id: str) -> str:
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance id")
        return str(uuid.uuid5(INSTANCE_INGRESS_NAMESPACE, instance_id))

    def ingress_allocation(self, instance_id: str) -> NetworkAllocation:
        ingress_id = self.ingress_id(instance_id)
        if self.network_settings:
            value = self.network_settings.allocations().get(ingress_id)
            if isinstance(value, dict):
                try:
                    return NetworkAllocation(**value)
                except TypeError as exc:
                    raise BackendError(f"invalid saved ingress allocation: {exc}") from exc
        return self._ephemeral_allocations.get(ingress_id) or self._legacy_allocation(ingress_id, "ingress", instance_id)

    def _live_subnets(self) -> set[str]:
        result = set()
        for interface in self._ip_json(["ip", "-j", "addr", "show"]):
            if not str(interface.get("ifname", "")).startswith(("czi", "czo", "czt")):
                continue
            for address in interface.get("addr_info", []):
                if address.get("family") == "inet" and address.get("prefixlen") == 30:
                    try:
                        result.add(str(ipaddress.ip_network(
                            f"{address.get('local')}/30", strict=False
                        )))
                    except ValueError:
                        pass
        return result

    def _allocate(self, instance_id: str, role: str = "egress",
                  owner_id: str = "") -> NetworkAllocation:
        if role not in {"ingress", "egress"}:
            raise BackendError("invalid network allocation role")
        owner_id = owner_id or instance_id
        if not self.network_settings:
            return self._legacy_allocation(instance_id, role, owner_id)
        with self.allocation_lock, self.network_settings.lock:
            values = self.network_settings.allocations()
            existing = values.get(instance_id)
            if isinstance(existing, dict):
                item = NetworkAllocation(**existing)
                if item.role != role or item.owner_id not in {"", owner_id}:
                    raise BackendError("saved network allocation has an incompatible role")
                if item.guest_if not in {"di0", "do0", "transport0"}:
                    raise BackendError("legacy network allocation must be cleaned before spawn")
                return item
            settings = self.network_settings.get()
            network_key = "ingress_cidr" if role == "ingress" else "namespace_cidr"
            network_v6_key = "ingress_cidr_v6" if role == "ingress" else "namespace_cidr_v6"
            network = ipaddress.ip_network(settings[network_key])
            used = {
                str(value.get("subnet")) for value in values.values()
                if isinstance(value, dict) and value.get("subnet")
            } | self._live_subnets()
            selected = next(
                (subnet for subnet in network.subnets(new_prefix=30) if str(subnet) not in used),
                None,
            )
            if selected is None:
                raise BackendError("namespace /30 allocation pool is exhausted")
            index = (int(selected.network_address) - int(network.network_address)) // 4
            network_v6 = ipaddress.ip_network(settings[network_v6_key])
            selected_v6 = ipaddress.ip_network(
                (int(network_v6.network_address) + index * 4, 126)
            )
            if not selected_v6.subnet_of(network_v6):
                raise BackendError("namespace /126 allocation pool is exhausted")
            suffix = owner_id.replace("-", "")[:8]
            hosts = list(selected.hosts())
            hosts_v6 = list(selected_v6.hosts())
            capacity = min(
                ipaddress.ip_network(settings["ingress_cidr"]).num_addresses // 4,
                ipaddress.ip_network(settings["ingress_cidr_v6"]).num_addresses // 4,
                ipaddress.ip_network(settings["namespace_cidr"]).num_addresses // 4,
                ipaddress.ip_network(settings["namespace_cidr_v6"]).num_addresses // 4,
            )
            identity_offset = 0 if role == "ingress" else capacity
            host_prefix = "czi" if role == "ingress" else "czo"
            guest_if = "di0" if role == "ingress" else "do0"
            item = NetworkAllocation(
                namespace=f"cz-{suffix}", host_if=f"{host_prefix}{suffix}", guest_if=guest_if,
                host_ip=str(hosts[0]), guest_ip=str(hosts[1]), table=f"cz_{suffix}",
                route_table=settings["route_table_start"] + identity_offset + index,
                mark=settings["mark_start"] + identity_offset + index, subnet=str(selected),
                egress_mode=settings["egress_mode"],
                sas_interface=settings["sas_interface"],
                subnet_v6=str(selected_v6), host_ip_v6=str(hosts_v6[0]),
                guest_ip_v6=str(hosts_v6[1]),
                role=role, owner_id=owner_id,
                fabric_if="fabric0" if role == "egress" else "",
                fabric_ip=(
                    str(ipaddress.ip_address(
                        int(ipaddress.ip_network(settings["fabric_cidr"]).network_address)
                        + index + 1
                    )) if role == "egress" else ""
                ),
                fabric_ip_v6=(
                    str(ipaddress.ip_address(
                        int(ipaddress.ip_network(settings["fabric_cidr_v6"]).network_address)
                        + index + 1
                    )) if role == "egress" else ""
                ),
                fabric_parent=settings["fabric_interface"] if role == "egress" else "",
                fabric_link_mode=settings["fabric_link_mode"] if role == "egress" else "",
                tuntom_tunnel_id=(index % 255) + 1 if role == "egress" else 0,
            )
            values[instance_id] = asdict(item)
            self.network_settings.save_allocations(values)
            return item

    @staticmethod
    def _run(command: list[str], timeout: int = 20, tolerate_missing: bool = False) -> None:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
        if completed.returncode and not tolerate_missing:
            raise BackendError((completed.stderr or completed.stdout).strip() or f"{command[0]} failed")

    def _cleanup_network(self, allocation: NetworkAllocation) -> None:
        if allocation.topology == "tuntom-via" and allocation.owner_id:
            self._run([
                "systemctl", "stop", self.tuntom_unit_name(allocation.owner_id),
            ], tolerate_missing=True)
            self._run([
                "systemctl", "stop", self.tuntom_relay_unit_name(allocation.owner_id),
            ], tolerate_missing=True)
        self._run(["nft", "delete", "table", "ip", allocation.table], tolerate_missing=True)
        self._run(["nft", "delete", "table", "inet", allocation.table], tolerate_missing=True)
        self._run(["ip", "rule", "delete", "fwmark", hex(allocation.mark),
                   "table", str(allocation.route_table)], tolerate_missing=True)
        self._run(["ip", "route", "flush", "table", str(allocation.route_table)], tolerate_missing=True)
        self._run(["ip", "-6", "rule", "delete", "fwmark", hex(allocation.mark),
                   "table", str(allocation.route_table)], tolerate_missing=True)
        self._run(["ip", "-6", "route", "flush", "table", str(allocation.route_table)], tolerate_missing=True)
        self._run(["ip", "netns", "delete", allocation.namespace], tolerate_missing=True)
        self._run(["ip", "link", "delete", allocation.host_if], tolerate_missing=True)

    @staticmethod
    def _ip_json(command: list[str]) -> list:
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if completed.returncode:
            return []
        try:
            value = json.loads(completed.stdout or "[]")
            return value if isinstance(value, list) else []
        except json.JSONDecodeError:
            return []

    def network_diagnostics(self, instance_id: str) -> dict:
        allocation = self.allocation(instance_id)
        ingress = self.ingress_allocation(instance_id)
        namespace_interfaces = self._ip_json([
            "ip", "-j", "-n", allocation.namespace, "addr", "show",
        ])
        namespace_routes = self._ip_json([
            "ip", "-j", "-n", allocation.namespace, "route", "show", "table", "all",
        ])
        host_routes = self._ip_json([
            "ip", "-j", "route", "show", "table", str(ingress.route_table),
        ])
        host_rules = [
            item for item in self._ip_json(["ip", "-j", "rule", "show"])
            if str(item.get("table", "")) == str(ingress.route_table)
        ]
        def link_view(item: NetworkAllocation) -> dict:
            if item.topology == "none":
                return {"role": item.role, "enabled": False, "namespace": item.namespace,
                        "host_interface": "", "guest_interface": "", "host_interfaces": []}
            subnet = item.subnet or str(ipaddress.ip_network(
                f"{item.host_ip}/30", strict=False,
            ))
            via_tuntom = item.topology == "tuntom-via"
            return {
                "role": item.role, "subnet": subnet, "enabled": True, "namespace": item.namespace,
                "host_interface": "" if via_tuntom else item.host_if,
                "guest_interface": item.guest_if,
                "expected_host_address": "" if via_tuntom else f"{item.host_ip}/30",
                "expected_guest_address": (
                    f"{item.guest_ip}/32" if via_tuntom else f"{item.guest_ip}/30"
                ),
                "subnet_v6": item.subnet_v6,
                "expected_host_address_v6": (
                    f"{item.host_ip_v6}/126" if item.host_ip_v6 and not via_tuntom else ""
                ),
                "expected_guest_address_v6": (
                    (f"{item.guest_ip_v6}/128" if via_tuntom else f"{item.guest_ip_v6}/126")
                    if item.guest_ip_v6 else ""
                ),
                "host_interfaces": [] if via_tuntom else self._ip_json([
                    "ip", "-j", "addr", "show", "dev", item.host_if,
                ]),
            }
        ingress_view = link_view(ingress)
        egress_view = link_view(allocation)
        tuntom_interfaces = {
            str(item.get("ifname", "")) for item in namespace_interfaces
            if isinstance(item, dict)
        }
        present = (
            {"di0", "do0"}.issubset(tuntom_interfaces)
            if allocation.topology == "tuntom-via"
            else bool(
                (ingress.topology == "none" or ingress_view["host_interfaces"])
                and (allocation.topology == "none" or egress_view["host_interfaces"])
                and namespace_interfaces
            )
        )
        return {
            "present": present,
            "ingress": ingress_view, "egress": egress_view,
            "transport": ({
                **ingress_view,
                "namespace_path": f"/run/netns/{ingress.namespace}",
                "mode": ingress.egress_mode,
                "interfaces": self._ip_json(["ip", "-j", "-n", ingress.namespace, "addr", "show"]),
                "routes": self._ip_json(["ip", "-j", "-n", ingress.namespace, "route", "show"]),
                "routes_v6": self._ip_json(["ip", "-j", "-6", "-n", ingress.namespace, "route", "show"]),
            } if ingress.topology == "unlimited-veth" else None),
            "route_table": ingress.route_table,
            "packet_mark": hex(ingress.mark),
            "namespace_interfaces": namespace_interfaces,
            "namespace_routes": namespace_routes,
            "host_routes": host_routes,
            "host_rules": host_rules,
            "link_driver": allocation.topology,
            "fabric": {
                "interface": allocation.fabric_if,
                "address": allocation.fabric_ip,
                "address_v6": allocation.fabric_ip_v6,
                "parent": allocation.fabric_parent,
                "mode": allocation.fabric_link_mode,
                "tunnel_id": allocation.tuntom_tunnel_id,
                "udp_port": (
                    40000 + allocation.tuntom_tunnel_id
                    if allocation.tuntom_tunnel_id else 0
                ),
            },
            "tuntom_unit": (
                self.tuntom_unit_name(instance_id)
                if allocation.topology == "tuntom-via" else ""
            ),
            "tuntom_relay_unit": (
                self.tuntom_relay_unit_name(instance_id)
                if allocation.topology == "tuntom-via" else ""
            ),
        }

    def _start_tuntom_adapter(
        self, instance_id: str, namespace: str, adapter: str, relay_socket: str,
        in_prefix: str, out_prefix: str, admission: str, mtu: int,
        hard_runtime_seconds: int, via_run: Path, rootfs_path: str = "",
    ) -> str:
        adapter_path = Path(adapter)
        socket_path = Path(relay_socket)
        if not adapter_path.is_absolute() or not adapter_path.is_file():
            raise BackendError("tuntom divert adapter is unavailable")
        if not os.access(adapter_path, os.X_OK):
            raise BackendError("tuntom divert adapter is not executable")
        if not socket_path.is_absolute() or not socket_path.is_socket():
            raise BackendError("tuntom VIA relay socket is unavailable")
        if admission not in {"immediate", "warmup"}:
            raise BackendError("invalid tuntom admission mode")
        if not 576 <= mtu <= 9000:
            raise BackendError("invalid tuntom MTU")
        attachment_re = re.compile(r"[A-Za-z0-9_.-]{1,32}")
        if not attachment_re.fullmatch(in_prefix) or not attachment_re.fullmatch(out_prefix):
            raise BackendError("invalid tuntom attachment prefix")
        suffix = instance_id.replace("-", "")[:8]
        in_port = f"{in_prefix}{suffix}"
        out_port = f"{out_prefix}{suffix}"
        if len(in_port) > 48 or len(out_port) > 48:
            raise BackendError("tuntom attachment name is too long")
        unit = self.tuntom_unit_name(instance_id)
        executable = str(adapter_path)
        command = [
            "systemd-run", "--quiet", "--collect", f"--unit={unit}",
            f"--slice={self.slice_name(instance_id)}",
            "--property=Type=simple", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s", "--property=NoNewPrivileges=yes",
            "--property=CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW",
            "--property=AmbientCapabilities=CAP_NET_ADMIN CAP_NET_RAW",
            f"--property=NetworkNamespacePath=/run/netns/{namespace}",
            f"--property=BindPaths={via_run}:/run/sas",
            f"--property=BindsTo={self.tuntom_relay_unit_name(instance_id)}",
            f"--property=After={self.tuntom_relay_unit_name(instance_id)}",
        ]
        if rootfs_path:
            rootfs = Path(rootfs_path).resolve()
            if not rootfs.is_dir() or not (rootfs / "bin/sh").exists():
                raise BackendError("prepared rootfs cannot execute Tuntom adapter")
            executable = "/opt/sas/bin/tuntom-divert-adapter"
            command.extend([
                f"--property=RootDirectory={rootfs}",
                "--property=MountAPIVFS=yes",
                f"--property=BindReadOnlyPaths={adapter_path}:{executable}",
            ])
        if hard_runtime_seconds:
            command.append(f"--property=RuntimeMaxSec={hard_runtime_seconds}s")
        command.extend([
            "--", executable, "di0", "do0",
            "--switch-socket", "/run/sas/relay.sock",
            "--via-instance", f"sas#{suffix}",
            "--divert-in-port", in_port,
            "--divert-out-port", out_port,
            "--admission", admission, "--mtu", str(mtu),
        ])
        self._run(command)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            ready = all(subprocess.run(
                ["ip", "-n", namespace, "link", "show", "dev", interface],
                capture_output=True, text=True, timeout=2, check=False,
            ).returncode == 0 for interface in ("di0", "do0"))
            if ready:
                return unit
            status = subprocess.run(
                ["systemctl", "show", unit, "--property=ActiveState", "--value"],
                capture_output=True, text=True, timeout=2, check=False,
            )
            if status.returncode or status.stdout.strip() in {"failed", "inactive"}:
                break
            time.sleep(0.1)
        self._run(["systemctl", "stop", unit], tolerate_missing=True)
        raise BackendError("tuntom VIA adapter did not create di0/do0")

    def _start_tuntom_relay(
        self, instance_id: str, namespace: str, binary: str, switch_ip: str,
        secret: str, tunnel_id: int, mtu: int, hard_runtime_seconds: int,
        via_run: Path, rootfs_path: str = "",
    ) -> tuple[str, Path]:
        binary_path = Path(binary).resolve()
        if not binary_path.is_file() or not os.access(binary_path, os.X_OK):
            raise BackendError("tuntom relay binary is unavailable")
        try:
            switch = str(ipaddress.ip_address(switch_ip))
        except ValueError as exc:
            raise BackendError("Tuntom VIA switch IP is invalid") from exc
        if not re.fullmatch(r"[0-9a-fA-F]{32}", secret):
            raise BackendError("Tuntom VIA secret must contain exactly 32 hex characters")
        if not 1 <= tunnel_id <= 255:
            raise BackendError("Tuntom VIA tunnel ID is outside 1..255")
        via_run.mkdir(parents=True, exist_ok=True, mode=0o770)
        try:
            account = pwd.getpwnam("tuntom")
            os.chown(via_run, account.pw_uid, account.pw_gid)
        except (KeyError, OSError):
            # Development builds may run without a dedicated account. The
            # binary reports that condition; do not silently broaden access.
            os.chmod(via_run, 0o700)
        relay_socket = via_run / "relay.sock"
        relay_socket.unlink(missing_ok=True)
        secret_dir = via_run.parent / ".credentials"
        secret_dir.mkdir(mode=0o700, exist_ok=True)
        secret_file = secret_dir / "tuntom-secret"
        secret_file.write_text(secret.lower() + "\n", encoding="ascii")
        os.chmod(secret_file, 0o400)
        unit = self.tuntom_relay_unit_name(instance_id)
        executable = str(binary_path)
        command = [
            "systemd-run", "--quiet", "--collect", f"--unit={unit}",
            f"--slice={self.slice_name(instance_id)}",
            "--property=Type=simple", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s", "--property=NoNewPrivileges=yes",
            f"--property=NetworkNamespacePath=/run/netns/{namespace}",
            f"--property=BindPaths={via_run}:/run/sas",
            f"--property=LoadCredential=tuntom-secret:{secret_file}",
        ]
        if rootfs_path:
            rootfs = Path(rootfs_path).resolve()
            if not rootfs.is_dir() or not (rootfs / "bin/sh").exists():
                raise BackendError("prepared rootfs cannot execute Tuntom relay")
            executable = "/opt/sas/bin/tuntom"
            command.extend([
                f"--property=RootDirectory={rootfs}",
                "--property=MountAPIVFS=yes",
                f"--property=BindReadOnlyPaths={binary_path}:{executable}",
            ])
        if hard_runtime_seconds:
            command.append(f"--property=RuntimeMaxSec={hard_runtime_seconds}s")
        shell = (
            'export TUNTOM_SECRET="$(cat "$CREDENTIALS_DIRECTORY/tuntom-secret")"; '
            'exec "$1" client "$2" - "$3" --relay-listen /run/sas/relay.sock '
            '--mtu "$4"'
        )
        command.extend([
            "--", "/bin/sh", "-eu", "-c", shell, "sas-tuntom",
            executable, str(tunnel_id), switch, str(mtu),
        ])
        self._run(command)
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if relay_socket.is_socket():
                return unit, relay_socket
            state = self.status(unit)
            if state.active_state in {"failed", "inactive"}:
                break
            time.sleep(0.1)
        self._run(["systemctl", "stop", unit], tolerate_missing=True)
        raise BackendError("tuntom VIA relay did not create its local socket")

    def _setup_fabric_link(
        self, allocation: NetworkAllocation, switch_ip: str,
    ) -> None:
        if not allocation.fabric_parent:
            raise BackendError("Tuntom VIA requires a configured fabric parent interface")
        if allocation.fabric_link_mode not in {"ipvlan-l3", "ipvlan-l2"}:
            raise BackendError("Tuntom VIA fabric link mode is invalid")
        switch = ipaddress.ip_address(switch_ip)
        local = allocation.fabric_ip_v6 if switch.version == 6 else allocation.fabric_ip
        if not local or ipaddress.ip_address(local).version != switch.version:
            raise BackendError("Tuntom VIA fabric pool does not match the switch IP family")
        parent = subprocess.run(
            ["ip", "link", "show", "dev", allocation.fabric_parent],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if parent.returncode:
            raise BackendError("configured fabric parent interface is unavailable")
        temporary = f"czf{allocation.owner_id.replace('-', '')[:8]}"
        mode = allocation.fabric_link_mode.removeprefix("ipvlan-")
        self._run([
            "ip", "link", "add", "link", allocation.fabric_parent,
            "name", temporary, "type", "ipvlan", "mode", mode,
        ])
        self._run(["ip", "link", "set", temporary, "netns", allocation.namespace])
        self._run([
            "ip", "-n", allocation.namespace, "link", "set", temporary,
            "name", "fabric0",
        ])
        family = ["-6"] if switch.version == 6 else []
        prefix = 128 if switch.version == 6 else 32
        self._run([
            "ip", "-n", allocation.namespace, *family, "addr", "add",
            f"{local}/{prefix}", "dev", "fabric0",
        ])
        self._run(["ip", "-n", allocation.namespace, "link", "set", "fabric0", "up"])
        self._run([
            "ip", "-n", allocation.namespace, *family, "route", "replace",
            f"{switch}/{prefix}", "dev", "fabric0", "src", local,
        ])
        protocol = "ip6" if switch.version == 6 else "ip"
        port = 40000 + allocation.tuntom_tunnel_id
        rules = (
            "table inet sas_fabric {\n"
            " chain input { type filter hook input priority -20; policy accept;\n"
            f'  iifname "fabric0" {protocol} saddr {switch} udp sport {port} accept\n'
            '  iifname "fabric0" meta l4proto { icmp, ipv6-icmp } accept\n'
            '  iifname "fabric0" drop\n'
            " }\n"
            " chain output { type filter hook output priority -20; policy accept;\n"
            f'  oifname "fabric0" {protocol} daddr {switch} udp dport {port} accept\n'
            '  oifname "fabric0" meta l4proto { icmp, ipv6-icmp } accept\n'
            '  oifname "fabric0" drop\n'
            " }\n}"
        )
        completed = subprocess.run(
            ["ip", "netns", "exec", allocation.namespace, "nft", "-f", "/dev/stdin"],
            input=rules, capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "fabric namespace nft failed")

    def start(self, instance_id: str, config_path: Path, runtime_seconds: int,
              source_ip: str = "", tls_port: int = 10443,
              plaintext_port: int = 10080, socks_port: int = 1080, cli_port: int = 10000,
              http_port: int = 3128, smithproxy_binary: str = "",
              profile: str = "custom", config_mode: str = "ro",
              assets_path: str = "", auto_restart: bool = False,
              hard_runtime_seconds: int = 86400, egress_mode: str = "",
              egress_driver: str = "split-veth", sas_interface: str = "",
              tuntom_socket: str = "/run/tuntom/via.sock",
              tuntom_binary: str = "/usr/local/bin/tuntom",
              tuntom_adapter: str = "/usr/local/bin/tuntom-divert-adapter",
              tuntom_in_prefix: str = "proxy-in-",
              tuntom_out_prefix: str = "proxy-out-",
              tuntom_admission: str = "immediate", tuntom_mtu: int = 1500,
              tuntom_switch_ip: str = "", tuntom_secret: str = "",
              tuntom_tunnel_id: int = 0,
              rootfs_path: str = "", preserve_allocations_on_failure: bool = False,
              ingress_driver: str = "authorized-veth", program: dict | None = None) -> str:
        if program and (not rootfs_path or ingress_driver != 'none' or egress_driver != 'none'):
            raise BackendError('program runtime requires rootfs and Wiring-only networking')
        if egress_driver == "veth-out":
            egress_driver = "split-veth"
        if ingress_driver not in {"authorized-veth", "unlimited-veth", "none"}:
            raise BackendError("unsupported ingress network driver")
        if egress_driver not in {"split-veth", "on-a-stick", "tuntom-via", "none"}:
            raise BackendError("unsupported egress network driver")
        if ingress_driver != "authorized-veth":
            if egress_driver not in {"split-veth", "none"}:
                raise BackendError("passive ingress requires veth out or no egress")
            return self._start_passive(
                instance_id, config_path, ingress_driver, egress_driver,
                smithproxy_binary, rootfs_path, config_mode, assets_path,
                auto_restart, hard_runtime_seconds, egress_mode, sas_interface,
                preserve_allocations_on_failure,
                program,
            )
        try:
            source = str(ipaddress.ip_address(source_ip))
        except ValueError as exc:
            raise BackendError("source_ip must be a valid IP address") from exc
        allocation = self._allocate(instance_id, "egress", instance_id)
        ingress_id = self.ingress_id(instance_id)
        try:
            ingress = self._allocate(ingress_id, "ingress", instance_id)
        except Exception:
            if self.network_settings and not preserve_allocations_on_failure:
                self.network_settings.release(instance_id)
            raise
        if egress_driver == "on-a-stick":
            # Keep both allocation IDs for lifecycle compatibility, but make
            # the egress record an explicit alias of the single physical di0
            # link. No do0/czo* interface is created or retained.
            if self.network_settings:
                values = self.network_settings.allocations()
                values.pop(instance_id, None)
                allocation = replace(
                    ingress, role="egress", owner_id=instance_id,
                    topology="on-a-stick",
                )
                values[instance_id] = asdict(allocation)
                self.network_settings.save_allocations(values)
            else:
                allocation = replace(
                    ingress, role="egress", owner_id=instance_id,
                    topology="on-a-stick",
                )
        elif egress_driver == "tuntom-via":
            if self.network_settings and not allocation.fabric_ip:
                settings = self.network_settings.get()
                egress_pool = ipaddress.ip_network(settings["namespace_cidr"])
                lease = ipaddress.ip_network(allocation.subnet)
                index = (int(lease.network_address) - int(egress_pool.network_address)) // 4
                allocation = replace(
                    allocation,
                    fabric_if="fabric0",
                    fabric_ip=str(ipaddress.ip_address(
                        int(ipaddress.ip_network(settings["fabric_cidr"]).network_address)
                        + index + 1
                    )),
                    fabric_ip_v6=str(ipaddress.ip_address(
                        int(ipaddress.ip_network(settings["fabric_cidr_v6"]).network_address)
                        + index + 1
                    )),
                    fabric_parent=settings["fabric_interface"],
                    fabric_link_mode=settings["fabric_link_mode"],
                    tuntom_tunnel_id=(index % 255) + 1,
                )
            allocation = replace(allocation, topology="tuntom-via")
            if tuntom_tunnel_id:
                allocation = replace(allocation, tuntom_tunnel_id=tuntom_tunnel_id)
            ingress = replace(ingress, topology="tuntom-via")
            if self.network_settings:
                allocations = self.network_settings.allocations()
                allocations[instance_id] = asdict(allocation)
                allocations[ingress_id] = asdict(ingress)
                self.network_settings.save_allocations(allocations)
        if egress_mode or sas_interface or egress_driver == "none":
            allocation = replace(
                allocation,
                egress_mode=egress_mode or allocation.egress_mode,
                sas_interface=sas_interface or allocation.sas_interface,
                topology=egress_driver,
            )
            self._remember_allocation(instance_id, allocation)
        source_address = ipaddress.ip_address(source)
        if source_address.version == 6 and not ingress.guest_ip_v6:
            raise BackendError("IPv6 requires a dual-stack network allocation")
        self._cleanup_network(ingress)
        self._cleanup_network(allocation)
        try:
            self._run(["ip", "netns", "add", allocation.namespace])
            self._run(["ip", "-n", allocation.namespace, "link", "set", "lo", "up"])
            tuntom_unit = ""
            if egress_driver == "tuntom-via":
                self._setup_fabric_link(allocation, tuntom_switch_ip)
                via_run = config_path.parent / "via-run"
                _relay_unit, relay_socket = self._start_tuntom_relay(
                    instance_id, allocation.namespace, tuntom_binary,
                    tuntom_switch_ip, tuntom_secret, allocation.tuntom_tunnel_id,
                    tuntom_mtu, hard_runtime_seconds, via_run, rootfs_path,
                )
                tuntom_unit = self._start_tuntom_adapter(
                    instance_id, allocation.namespace, tuntom_adapter, str(relay_socket),
                    tuntom_in_prefix, tuntom_out_prefix, tuntom_admission,
                    tuntom_mtu, hard_runtime_seconds, via_run, rootfs_path,
                )
                self._run(["ip", "-n", allocation.namespace, "link", "set", "di0", "up"])
                self._run(["ip", "-n", allocation.namespace, "link", "set", "do0", "up"])
                # TUN links have no peer address. Give Smithproxy stable local
                # identities while keeping all remote destinations on-link.
                self._run(["ip", "-n", allocation.namespace, "addr", "add",
                           f"{ingress.guest_ip}/32", "dev", "di0"])
                self._run(["ip", "-n", allocation.namespace, "addr", "add",
                           f"{allocation.guest_ip}/32", "dev", "do0"])
                if ingress.guest_ip_v6 and allocation.guest_ip_v6:
                    self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add",
                               f"{ingress.guest_ip_v6}/128", "dev", "di0"])
                    self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add",
                               f"{allocation.guest_ip_v6}/128", "dev", "do0"])
                self._run(["ip", "-n", allocation.namespace, "route", "add", "default", "dev", "do0"])
                self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "default", "dev", "do0"])
                source_prefix = "/128" if source_address.version == 6 else "/32"
                family_flag = ["-6"] if source_address.version == 6 else []
                self._run([
                    "ip", "-n", allocation.namespace, *family_flag, "route", "replace",
                    f"{source}{source_prefix}", "dev", "di0",
                ])
            # Egress pair: proxy-originated traffic follows the namespace
            # default route through do0.
            if egress_driver == "split-veth":
                self._run(["ip", "link", "add", allocation.host_if, "type", "veth", "peer", "name", allocation.guest_if])
                self._run(["ip", "link", "set", allocation.guest_if, "netns", allocation.namespace])
                self._run(["ip", "addr", "add", f"{allocation.host_ip}/30", "dev", allocation.host_if])
                if allocation.host_ip_v6:
                    self._run(["ip", "-6", "addr", "add", f"{allocation.host_ip_v6}/126", "dev", allocation.host_if])
                self._run(["ip", "link", "set", allocation.host_if, "up"])
                self._run(["ip", "-n", allocation.namespace, "addr", "add", f"{allocation.guest_ip}/30", "dev", allocation.guest_if])
                if allocation.guest_ip_v6:
                    self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add", f"{allocation.guest_ip_v6}/126", "dev", allocation.guest_if])
                self._run(["ip", "-n", allocation.namespace, "link", "set", allocation.guest_if, "up"])
                self._run(["ip", "-n", allocation.namespace, "route", "add", "default", "via", allocation.host_ip])
                if allocation.host_ip_v6:
                    self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "default", "via", allocation.host_ip_v6])

            # Ingress pair: selected client traffic enters exclusively through
            # di0. A host route keeps replies to that client off do0.
            if egress_driver != "tuntom-via":
                self._run(["ip", "link", "add", ingress.host_if, "type", "veth",
                           "peer", "name", ingress.guest_if])
                self._run(["ip", "link", "set", ingress.guest_if, "netns", allocation.namespace])
                self._run(["ip", "addr", "add", f"{ingress.host_ip}/30", "dev", ingress.host_if])
                if ingress.host_ip_v6:
                    self._run(["ip", "-6", "addr", "add", f"{ingress.host_ip_v6}/126",
                               "dev", ingress.host_if])
                self._run(["ip", "link", "set", ingress.host_if, "up"])
                self._run(["ip", "-n", allocation.namespace, "addr", "add",
                           f"{ingress.guest_ip}/30", "dev", ingress.guest_if])
                if ingress.guest_ip_v6:
                    self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add",
                               f"{ingress.guest_ip_v6}/126", "dev", ingress.guest_if])
                self._run(["ip", "-n", allocation.namespace, "link", "set", ingress.guest_if, "up"])
            if egress_driver == "on-a-stick":
                self._run(["ip", "-n", allocation.namespace, "route", "add", "default",
                           "via", ingress.host_ip, "dev", ingress.guest_if])
                if ingress.host_ip_v6:
                    self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "default",
                               "via", ingress.host_ip_v6, "dev", ingress.guest_if])
            if egress_driver == "tuntom-via":
                pass
            elif source_address.version == 6:
                self._run(["ip", "-n", allocation.namespace, "-6", "route", "replace",
                           f"{source}/128", "via", ingress.host_ip_v6, "dev", ingress.guest_if])
            else:
                self._run(["ip", "-n", allocation.namespace, "route", "replace",
                           f"{source}/32", "via", ingress.host_ip, "dev", ingress.guest_if])

            if egress_driver != "tuntom-via":
                self._run(["ip", "route", "add", "default", "via", ingress.guest_ip,
                           "dev", ingress.host_if, "table", str(ingress.route_table)])
                self._run(["ip", "rule", "add", "fwmark", hex(ingress.mark),
                           "table", str(ingress.route_table), "priority", "1000"])
                if ingress.guest_ip_v6:
                    self._run(["ip", "-6", "route", "add", "default", "via", ingress.guest_ip_v6,
                               "dev", ingress.host_if, "table", str(ingress.route_table)])
                    self._run(["ip", "-6", "rule", "add", "fwmark", hex(ingress.mark),
                               "table", str(ingress.route_table), "priority", "1000"])
            self._run(["ip", "-n", allocation.namespace, "rule", "add", "fwmark", "0x1",
                       "table", "100", "priority", "100"])
            self._run(["ip", "-n", allocation.namespace, "route", "add", "local", "0.0.0.0/0",
                       "dev", "lo", "table", "100"])
            if allocation.guest_ip_v6:
                self._run(["ip", "-n", allocation.namespace, "-6", "rule", "add", "fwmark", "0x1",
                           "table", "100", "priority", "100"])
                self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "local", "::/0",
                           "dev", "lo", "table", "100"])

            explicit_port = socks_port if profile == "socks" else http_port
            explicit = profile in {"socks", "http-proxy"}
            family = "ip6" if source_address.version == 6 else "ip"
            if egress_driver != "tuntom-via":
                destination = (
                    ingress.guest_ip_v6 if source_address.version == 6 else ingress.guest_ip
                )
                match = (
                    f"{family} saddr {source} tcp dport {explicit_port}"
                    if explicit else f"{family} saddr {source}"
                )
                dnat_chain = (
                    " chain proxy_dnat { type nat hook prerouting priority dstnat; policy accept;\n"
                    f"  {family} saddr {source} tcp dport {explicit_port} "
                    f"dnat {'ip6 ' if source_address.version == 6 else ''}to "
                    f"{'[' if source_address.version == 6 else ''}{destination}"
                    f"{']' if source_address.version == 6 else ''}:{explicit_port}\n }}\n"
                    if explicit else ""
                )
                snat_rule = ""
                if allocation.egress_mode == "masquerade" and egress_driver != "none":
                    interface_match = (
                        f'oifname "{allocation.sas_interface}" '
                        if allocation.sas_interface else ""
                    )
                    snat_rule = (
                        f"  {interface_match}ip saddr {allocation.guest_ip} masquerade\n"
                    )
                    if allocation.guest_ip_v6:
                        snat_rule += (
                            f"  {interface_match}ip6 saddr {allocation.guest_ip_v6} masquerade\n"
                        )
                rules = (
                    f"table inet {allocation.table} {{\n"
                    " chain prerouting { type filter hook prerouting priority mangle; policy accept;\n"
                    f"  {match} meta mark set {hex(ingress.mark)}\n"
                    f" }}\n{dnat_chain} chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;\n"
                    f"{snat_rule} }}\n}}"
                )
                completed = subprocess.run(
                    ["nft", "-f", "/dev/stdin"], input=rules, capture_output=True,
                    text=True, timeout=10, check=False,
                )
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "nft failed")

            if egress_driver == "tuntom-via":
                authorized = f'iifname "di0" {family} saddr {source}'
                action = (
                    f"tcp dport {explicit_port} accept"
                    if explicit else (
                        f"tcp dport 443 tproxy to :{tls_port} meta mark set 0x1\n"
                        f"  {authorized} tcp dport != 443 "
                        f"tproxy to :{plaintext_port} meta mark set 0x1"
                    )
                )
                namespace_rules = (
                    "table inet capture_zone {\n"
                    " chain prerouting { type filter hook prerouting priority mangle; policy accept;\n"
                    f"  {authorized} {action}\n"
                    '  iifname "di0" drop\n'
                    " }\n}"
                )
                completed = subprocess.run(
                    ["ip", "netns", "exec", allocation.namespace, "nft", "-f", "/dev/stdin"],
                    input=namespace_rules, capture_output=True, text=True, timeout=10, check=False,
                )
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "tuntom namespace nft failed")
            elif not explicit:
                namespace_rules = (
                    "table inet capture_zone {\n"
                    " chain prerouting { type filter hook prerouting priority mangle; policy accept;\n"
                    f"  iifname \"{ingress.guest_if}\" tcp dport 443 tproxy to :{tls_port} meta mark set 0x1\n"
                    f"  iifname \"{ingress.guest_if}\" tcp dport != 443 tproxy to :{plaintext_port} meta mark set 0x1\n"
                    " }\n}"
                )
                completed = subprocess.run(
                    ["ip", "netns", "exec", allocation.namespace, "nft", "-f", "/dev/stdin"],
                    input=namespace_rules, capture_output=True, text=True, timeout=10, check=False,
                )
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "namespace nft failed")

            return self._launch_proxy(
                instance_id, allocation, config_path, smithproxy_binary, rootfs_path,
                config_mode, assets_path, auto_restart, hard_runtime_seconds, tuntom_unit,
            )
        except Exception:
            self._cleanup_network(ingress)
            self._cleanup_network(allocation)
            if self.network_settings and not preserve_allocations_on_failure:
                self.network_settings.release(instance_id)
                self.network_settings.release(ingress_id)
            raise

    def _remember_allocation(self, key: str, allocation: NetworkAllocation) -> None:
        if self.network_settings:
            with self.network_settings.lock:
                values = self.network_settings.allocations()
                values[key] = asdict(allocation)
                self.network_settings.save_allocations(values)
        else:
            self._ephemeral_allocations[key] = allocation

    def _routed_veth(self, link: NetworkAllocation) -> None:
        """A routed link, not a selector or a firewall exception."""
        self._run(["ip", "link", "add", link.host_if, "type", "veth",
                   "peer", "name", link.guest_if, "netns", link.namespace])
        for family, host, guest, prefix in (
            ([], link.host_ip, link.guest_ip, 30),
            (["-6"], link.host_ip_v6, link.guest_ip_v6, 126),
        ):
            if not host or not guest:
                continue
            self._run(["ip", *family, "addr", "add", f"{host}/{prefix}", "dev", link.host_if])
            self._run(["ip", "-n", link.namespace, *family, "addr", "add",
                       f"{guest}/{prefix}", "dev", link.guest_if])
        self._run(["ip", "link", "set", link.host_if, "up"])
        self._run(["ip", "-n", link.namespace, "link", "set", link.guest_if, "up"])
        for family, gateway in (([], link.host_ip), (["-6"], link.host_ip_v6)):
            if gateway:
                self._run(["ip", "-n", link.namespace, *family, "route", "add",
                           "default", "via", gateway, "dev", link.guest_if])
        if link.egress_mode == "masquerade":
            uplink = f'oifname "{link.sas_interface}" ' if link.sas_interface else ""
            rules = [f'table inet {link.table} {{',
                     ' chain postrouting { type nat hook postrouting priority srcnat; policy accept;']
            for family, address in (("ip", link.guest_ip), ("ip6", link.guest_ip_v6)):
                if address:
                    rules.append(f'  iifname "{link.host_if}" {uplink}{family} saddr {address} masquerade')
            rules.append(' }\n}')
            result = subprocess.run(["nft", "-f", "/dev/stdin"], input="\n".join(rules),
                                    capture_output=True, text=True, timeout=10, check=False)
            if result.returncode:
                raise BackendError(result.stderr.strip() or "veth NAT setup failed")

    def _start_passive(
        self, instance_id: str, config_path: Path, ingress_driver: str, egress_driver: str,
        smithproxy_binary: str, rootfs_path: str, config_mode: str, assets_path: str,
        auto_restart: bool, hard_runtime_seconds: int, egress_mode: str,
        sas_interface: str, preserve_allocations_on_failure: bool, program: dict | None = None,
    ) -> str:
        # Keep identities in the same durable lease store used by restart/stop.
        ingress_id = self.ingress_id(instance_id)
        allocation = self._allocate(instance_id, "egress", instance_id)
        ingress = None
        try:
            ingress = self._allocate(ingress_id, "ingress", instance_id)
            allocation = replace(allocation, topology=egress_driver,
                                 egress_mode=egress_mode or allocation.egress_mode,
                                 sas_interface=sas_interface or allocation.sas_interface)
            if ingress_driver == "unlimited-veth":
                suffix = instance_id.replace("-", "")[:8]
                ingress = replace(ingress, namespace=f"czt-{suffix}",
                                  host_if=f"czt{suffix}", guest_if="transport0",
                                  table=f"czt_{suffix}", topology="unlimited-veth")
            else:
                ingress = replace(ingress, topology="none")
            self._remember_allocation(instance_id, allocation)
            self._remember_allocation(ingress_id, ingress)
            self._cleanup_network(ingress)
            self._cleanup_network(allocation)
            self._run(["ip", "netns", "add", allocation.namespace])
            self._run(["ip", "-n", allocation.namespace, "link", "set", "lo", "up"])
            if egress_driver != "none":
                self._routed_veth(allocation)
            if ingress_driver == "unlimited-veth":
                self._run(["ip", "netns", "add", ingress.namespace])
                self._run(["ip", "-n", ingress.namespace, "link", "set", "lo", "up"])
                self._routed_veth(ingress)
            # No source selector, host policy route, DNAT or interception here.
            # Host forwarding/firewall and upstream return routing remain explicit
            # operator prerequisites; this path never overrides them.
            return self._launch_proxy(
                instance_id, allocation, config_path, smithproxy_binary, rootfs_path,
                config_mode, assets_path, auto_restart, hard_runtime_seconds,
                program=program,
            )
        except Exception:
            self._run(["systemctl", "stop", self.unit_name(instance_id)], tolerate_missing=True)
            if ingress is not None:
                self._cleanup_network(ingress)
            self._cleanup_network(allocation)
            if not preserve_allocations_on_failure:
                for key in (instance_id, ingress_id):
                    if self.network_settings:
                        self.network_settings.release(key)
                    self._ephemeral_allocations.pop(key, None)
            raise

    def _launch_proxy(
        self, instance_id: str, allocation: NetworkAllocation, config_path: Path,
        smithproxy_binary: str, rootfs_path: str, config_mode: str, assets_path: str,
        auto_restart: bool, hard_runtime_seconds: int, tuntom_unit: str = "", program: dict | None = None,
    ) -> str:
        system_start = getattr(self, 'system_start', None)
        if system_start:
            instance = system_start.manager.peek(instance_id)
            if instance:
                wiring = getattr(self, 'wiring', None)
                if wiring:
                    wiring.prepare_instance(instance, apply_addressing=False)
                system_start.check(instance, starting=True)
        if program:
            from .program_runtime import launch
            return launch(self, instance_id, allocation, config_path.parent, Path(rootfs_path),
                          program, auto_restart, hard_runtime_seconds)
        unit = self.unit_name(instance_id)
        # Smithproxy uses the fixed /var/run/smithproxy.default.pid path.
        # Give every transient unit its own /run mount; /var/run is a
        # symlink to it on modern distributions.  A network namespace
        # alone does not isolate PID files.
        private_run = config_path.parent / "run"
        effective_binary = Path(smithproxy_binary or self.smithproxy_binary).resolve()
        rootfs_properties = []
        executable = str(effective_binary)
        if rootfs_path:
            rootfs_properties, executable = self._rootfs_execution_properties(
                config_path, rootfs_path
            )
        command = [
            "systemd-run", "--quiet", f"--unit={unit}",
            f"--slice={self.slice_name(instance_id)}",
            "--property=Type=simple", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=10s",
            "--property=MemoryMax=1G", "--property=TasksMax=256",
            f"--property=NetworkNamespacePath=/run/netns/{allocation.namespace}",
            *rootfs_properties,
            *self._instance_storage_properties(
                config_path, private_run, effective_binary,
            ),
        ]
        if tuntom_unit:
            command.extend([
                f"--property=BindsTo={tuntom_unit}",
                f"--property=After={tuntom_unit}",
            ])
        if hard_runtime_seconds:
            command.insert(5, f"--property=RuntimeMaxSec={hard_runtime_seconds}s")
        command.extend(restart_properties(auto_restart))
        if restart_policy(auto_restart) == 'no':
            command.insert(2, "--collect")
        if config_mode == "ro":
            command.append(f"--property=ReadOnlyPaths={config_path}")
        if assets_path:
            asset_root = Path(assets_path).resolve()
            if not asset_root.is_dir():
                raise BackendError("configuration assets are unavailable")
            # Normal instances receive a private copy below their runtime
            # workspace. The workspace bind above already exposes it and
            # ReadWritePaths permits Smithproxy's generated cert caches.
            if not asset_root.is_relative_to(config_path.parent.resolve()):
                raise BackendError("instance assets must be private to its runtime workspace")
        command.extend([
            "--", executable, "--config-file", str(config_path),
        ])
        self._run(command)
        return unit

    def stop(self, unit: str) -> None:
        instance_id = unit.removeprefix("capture-zone-smithproxy-").removesuffix(".service")
        if getattr(self, 'microservices', None):
            self.microservices.stop_instance(instance_id)
        ingress_id = self.ingress_id(instance_id)
        self.stop_debug(instance_id)
        completed = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True,
                                   timeout=20, check=False)
        if completed.returncode and "not loaded" not in completed.stderr.lower():
            raise BackendError(completed.stderr.strip() or "systemctl stop failed")
        self._cleanup_network(self.ingress_allocation(instance_id))
        self._cleanup_network(self.allocation(instance_id))
        if self.network_settings:
            self.network_settings.release(instance_id)
            self.network_settings.release(ingress_id)
        self._ephemeral_allocations.pop(instance_id, None)
        self._ephemeral_allocations.pop(ingress_id, None)

    def attach_source(self, instance_id: str, source_ip: str, profile: str,
                      socks_port: int = 1080, http_port: int = 3128,
                      tls_port: int = 50443, plaintext_port: int = 50080) -> None:
        allocation = self.allocation(instance_id)
        ingress = self.ingress_allocation(instance_id)
        if ingress.topology in {"unlimited-veth", "none"}:
            raise BackendError("source attachment requires Authorized veth")
        source = ipaddress.ip_address(source_ip)
        if source.version == 6 and not ingress.guest_ip_v6:
            raise BackendError("IPv6 attachment requires a dual-stack instance")
        if allocation.topology == "tuntom-via":
            family = "ip6" if source.version == 6 else "ip"
            explicit_port = socks_port if profile == "socks" else http_port
            rules = []
            if profile in {"socks", "http-proxy"}:
                rules.append(
                    f'insert rule inet capture_zone prerouting iifname "di0" '
                    f"{family} saddr {source} tcp dport {explicit_port} accept"
                )
            else:
                rules.extend([
                    f'insert rule inet capture_zone prerouting iifname "di0" '
                    f"{family} saddr {source} tcp dport != 443 "
                    f"tproxy to :{plaintext_port} meta mark set 0x1",
                    f'insert rule inet capture_zone prerouting iifname "di0" '
                    f"{family} saddr {source} tcp dport 443 "
                    f"tproxy to :{tls_port} meta mark set 0x1",
                ])
            completed = subprocess.run(
                ["ip", "netns", "exec", allocation.namespace, "nft", "-f", "/dev/stdin"],
                input="\n".join(rules) + "\n", capture_output=True, text=True,
                timeout=10, check=False,
            )
            if completed.returncode:
                raise BackendError(completed.stderr.strip() or "cannot attach tuntom source")
            prefix = 128 if source.version == 6 else 32
            family_flag = ["-6"] if source.version == 6 else []
            self._run([
                "ip", "-n", allocation.namespace, *family_flag, "route", "replace",
                f"{source}/{prefix}", "dev", "di0",
            ])
            return
        table_family = "inet" if allocation.guest_ip_v6 else "ip"
        address_family = "ip6" if source.version == 6 else "ip"
        destination = ingress.guest_ip_v6 if source.version == 6 else ingress.guest_ip
        explicit_port = socks_port if profile == "socks" else http_port
        lines = [
            f"add rule {table_family} {allocation.table} prerouting {address_family} saddr {source} "
            f"meta mark set {hex(ingress.mark)}"
        ]
        if profile in {"socks", "http-proxy"}:
            lines.append(
                f"add rule {table_family} {allocation.table} proxy_dnat {address_family} saddr {source} "
                f"tcp dport {explicit_port} dnat {'ip6 ' if source.version == 6 else ''}to "
                f"{'[' if source.version == 6 else ''}{destination}"
                f"{']' if source.version == 6 else ''}:{explicit_port}"
            )
        completed = subprocess.run(
            ["nft", "-f", "/dev/stdin"], input="\n".join(lines) + "\n",
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "cannot attach source route")
        route = ["ip", "-n", allocation.namespace]
        if source.version == 6:
            route.extend(["-6", "route", "replace", f"{source}/128", "via",
                          ingress.host_ip_v6, "dev", ingress.guest_if])
        else:
            route.extend(["route", "replace", f"{source}/32", "via",
                          ingress.host_ip, "dev", ingress.guest_if])
        self._run(route)

    @staticmethod
    def test_drive_unit_name(drive_id: str) -> str:
        if not SAFE_ID.fullmatch(drive_id):
            raise BackendError("invalid test drive id")
        return f"capture-zone-testdrive-{drive_id}.service"

    @staticmethod
    def test_drive_ingress_id(drive_id: str) -> str:
        if not SAFE_ID.fullmatch(drive_id):
            raise BackendError("invalid test drive id")
        return str(uuid.uuid5(TEST_DRIVE_INGRESS_NAMESPACE, drive_id))

    def start_test_drive(self, drive_id: str, binary: Path, config_path: Path,
                         private_run: Path, resolver_path: Path, ttl_seconds: int,
                         config_mode: str = "ro"):
        allocation = self._allocate(drive_id, "egress", drive_id)
        ingress_id = self.test_drive_ingress_id(drive_id)
        try:
            ingress = self._allocate(ingress_id, "ingress", drive_id)
        except Exception:
            if self.network_settings:
                self.network_settings.release(drive_id)
            raise
        self._cleanup_network(allocation)
        self._cleanup_network(ingress)
        try:
            self._run(["ip", "netns", "add", allocation.namespace])
            self._run(["ip", "link", "add", allocation.host_if, "type", "veth",
                       "peer", "name", allocation.guest_if])
            self._run(["ip", "link", "set", allocation.guest_if, "netns", allocation.namespace])
            self._run(["ip", "addr", "add", f"{allocation.host_ip}/30", "dev", allocation.host_if])
            self._run(["ip", "-6", "addr", "add", f"{allocation.host_ip_v6}/126",
                       "dev", allocation.host_if])
            self._run(["ip", "link", "set", allocation.host_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "addr", "add",
                       f"{allocation.guest_ip}/30", "dev", allocation.guest_if])
            self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add",
                       f"{allocation.guest_ip_v6}/126", "dev", allocation.guest_if])
            self._run(["ip", "-n", allocation.namespace, "link", "set", "lo", "up"])
            self._run(["ip", "-n", allocation.namespace, "link", "set",
                       allocation.guest_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "route", "add", "default",
                       "via", allocation.host_ip])
            self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "default",
                       "via", allocation.host_ip_v6])
            self._run(["ip", "link", "add", ingress.host_if, "type", "veth",
                       "peer", "name", ingress.guest_if])
            self._run(["ip", "link", "set", ingress.guest_if, "netns", allocation.namespace])
            self._run(["ip", "addr", "add", f"{ingress.host_ip}/30", "dev", ingress.host_if])
            self._run(["ip", "-6", "addr", "add", f"{ingress.host_ip_v6}/126",
                       "dev", ingress.host_if])
            self._run(["ip", "link", "set", ingress.host_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "addr", "add",
                       f"{ingress.guest_ip}/30", "dev", ingress.guest_if])
            self._run(["ip", "-n", allocation.namespace, "-6", "addr", "add",
                       f"{ingress.guest_ip_v6}/126", "dev", ingress.guest_if])
            self._run(["ip", "-n", allocation.namespace, "link", "set",
                       ingress.guest_if, "up"])
            # Accepted connections must return through di0 while independent
            # proxy egress continues to use the default route on do0.
            self._run(["ip", "-n", allocation.namespace, "route", "add", "default",
                       "via", ingress.host_ip, "dev", ingress.guest_if,
                       "table", "101"])
            self._run(["ip", "-n", allocation.namespace, "rule", "add", "from",
                       f"{ingress.guest_ip}/32", "table", "101", "priority", "101"])
            self._run(["ip", "-n", allocation.namespace, "-6", "route", "add", "default",
                       "via", ingress.host_ip_v6, "dev", ingress.guest_if, "table", "101"])
            self._run(["ip", "-n", allocation.namespace, "-6", "rule", "add", "from",
                       f"{ingress.guest_ip_v6}/128", "table", "101", "priority", "101"])
            interface_match = (
                f'oifname "{allocation.sas_interface}" ' if allocation.sas_interface else ""
            )
            rules = (
                f"table inet {allocation.table} {{\n"
                " chain postrouting { type nat hook postrouting priority srcnat; policy accept;\n"
                f"  {interface_match}ip saddr {allocation.guest_ip} masquerade\n"
                f"  {interface_match}ip6 saddr {allocation.guest_ip_v6} masquerade\n"
                " }\n}"
            )
            completed = subprocess.run(
                ["nft", "-f", "/dev/stdin"], input=rules, capture_output=True,
                text=True, timeout=10, check=False,
            )
            if completed.returncode:
                raise BackendError(completed.stderr.strip() or "test drive nft failed")
            unit = self.test_drive_unit_name(drive_id)
            system_start = getattr(self, 'test_drive_system_start', None)
            if system_start:
                from types import SimpleNamespace
                system_start.check(SimpleNamespace(id=drive_id, namespace=allocation.namespace), starting=True)
            self._start_test_drive_service(
                unit, allocation.namespace, binary, config_path,
                private_run, resolver_path, ttl_seconds, config_mode,
            )
            return unit, allocation, ingress, ingress_id
        except Exception:
            self._run(["ip", "link", "delete", ingress.host_if], tolerate_missing=True)
            self._cleanup_network(allocation)
            if self.network_settings:
                self.network_settings.release(drive_id)
                self.network_settings.release(ingress_id)
            raise

    def _start_test_drive_service(self, unit: str, namespace: str, binary: Path,
                                  config_path: Path, private_run: Path,
                                  resolver_path: Path, ttl_seconds: int,
                                  config_mode: str = "ro") -> None:
        if config_mode not in {"ro", "rw"}:
            raise BackendError("invalid Test Drive config mode")
        system_start = getattr(self, 'test_drive_system_start', None)
        if system_start:
            match = TEST_DRIVE_UNIT_RE.fullmatch(unit)
            drive = system_start.manager.peek(match.group(1)) if match else None
            if drive:
                system_start.check(drive, starting=True)
        self._clear_test_drive_runtime_override(unit)
        command = [
                "systemd-run", "--quiet", "--collect", f"--unit={unit}",
                "--property=Type=simple", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=10s", f"--property=RuntimeMaxSec={str(ttl_seconds) + 's' if ttl_seconds else 'infinity'}",
                "--property=MemoryMax=1G", "--property=TasksMax=256",
                f"--property=NetworkNamespacePath=/run/netns/{namespace}",
                *self._instance_storage_properties(config_path, private_run, binary.resolve()),
                f"--property=BindReadOnlyPaths={resolver_path}:/run/systemd/resolve/stub-resolv.conf",
        ]
        asset_root = config_path.parent.parent / "assets"
        if asset_root.is_dir():
            # Test Drive also owns this copied asset tree. Smithproxy writes
            # generated SNI/IP certificate caches here; the build bundle stays
            # immutable and is mounted separately by storage properties.
            command.extend([
                f"--property=BindPaths={asset_root}:{asset_root}",
                f"--property=ReadWritePaths={asset_root}",
            ])
        if config_mode == "ro":
            command.append(f"--property=ReadOnlyPaths={config_path}")
        command.extend(["--", str(binary), "--config-file", str(config_path)])
        self._run(command)

    def upgrade_test_drive(self, unit: str, namespace: str, binary: Path,
                           config_path: Path, private_run: Path,
                           resolver_path: Path, ttl_seconds: int,
                           config_mode: str = "rw") -> None:
        """Replace only the Test Drive process; preserve netns and workspace."""
        self._run(["systemctl", "stop", unit], timeout=20, tolerate_missing=True)
        self._clear_test_drive_runtime_override(unit)
        self._run(["systemctl", "reset-failed", unit], tolerate_missing=True)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            completed = subprocess.run(
                ["systemctl", "show", unit, "--property=LoadState", "--value"],
                capture_output=True, text=True, timeout=3, check=False,
            )
            if completed.returncode or completed.stdout.strip() in {"", "not-found"}:
                break
            time.sleep(0.1)
        else:
            raise BackendError("old Test Drive unit did not unload")
        # A killed Smithproxy may leave its fixed private PID file behind.
        (private_run / "smithproxy.default.pid").unlink(missing_ok=True)
        self._start_test_drive_service(
            unit, namespace, binary, config_path,
            private_run, resolver_path, ttl_seconds, config_mode,
        )

    def stop_test_drive_process(self, unit: str) -> None:
        """Stop Smithproxy without removing the Test Drive network or files."""
        self._run(["systemctl", "stop", unit], timeout=20, tolerate_missing=True)
        self._clear_test_drive_runtime_override(unit)

    @staticmethod
    def _test_drive_runtime_override(unit: str) -> Path:
        if not TEST_DRIVE_UNIT_RE.fullmatch(unit):
            raise BackendError("invalid Test Drive unit")
        return Path("/run/systemd/system") / f"{unit}.d" / "50-capture-zone-runtime-max.conf"

    def _clear_test_drive_runtime_override(self, unit: str) -> None:
        override = self._test_drive_runtime_override(unit)
        changed = False
        try:
            override.unlink(missing_ok=True)
            changed = True
            override.parent.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            # Another administrator-owned drop-in may share this directory.
            # Never remove anything except our exact file.
            pass
        if changed:
            self._run(["systemctl", "daemon-reload"], timeout=20)

    def extend_test_drive(self, unit: str, remaining_seconds: int) -> None:
        if remaining_seconds < 1:
            raise BackendError("test drive extension must remain positive")
        completed = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveEnterTimestampMonotonic", "--value"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        try:
            entered_us = int(completed.stdout.strip())
            uptime_us = int(float(Path("/proc/uptime").read_text().split()[0]) * 1_000_000)
        except (OSError, ValueError, IndexError) as exc:
            raise BackendError("cannot determine Test Drive unit runtime") from exc
        elapsed_seconds = max(0, math.ceil((uptime_us - entered_us) / 1_000_000))
        total_seconds = elapsed_seconds + remaining_seconds
        override = self._test_drive_runtime_override(unit)
        override.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        temporary = override.with_suffix(".tmp")
        temporary.write_text(
            f"[Service]\nRuntimeMaxSec={total_seconds}s\n", encoding="utf-8"
        )
        os.chmod(temporary, 0o644)
        temporary.replace(override)
        try:
            self._run(["systemctl", "daemon-reload"], timeout=20)
            completed = subprocess.run(
                ["systemctl", "show", unit, "--property=RuntimeMaxUSec", "--value"],
                capture_output=True, text=True, timeout=5, check=False,
            )
            actual_us = systemd_timespan_microseconds(completed.stdout)
            if completed.returncode or abs(actual_us - total_seconds * 1_000_000) > 1_000_000:
                raise BackendError("systemd did not apply the extended Test Drive runtime")
        except Exception:
            override.unlink(missing_ok=True)
            try:
                override.parent.rmdir()
            except OSError:
                pass
            self._run(["systemctl", "daemon-reload"], timeout=20)
            raise

    def stop_test_drive(self, drive_id: str, unit: str, ingress_id: str = "",
                        ingress_host_interface: str = "") -> None:
        ingress_id = ingress_id or self.test_drive_ingress_id(drive_id)
        ingress_host_interface = (
            ingress_host_interface or f"czi{drive_id.replace('-', '')[:8]}"
        )
        self._run(["systemctl", "stop", unit], timeout=20, tolerate_missing=True)
        self._clear_test_drive_runtime_override(unit)
        self._run(["ip", "link", "delete", ingress_host_interface], tolerate_missing=True)
        self._cleanup_network(self.allocation(drive_id))
        if self.network_settings:
            self.network_settings.release(drive_id)
            self.network_settings.release(ingress_id)

    def discover_test_drives(self) -> list[tuple[str, str]]:
        completed = subprocess.run(
            ["systemctl", "list-units", "--all", "--full", "--plain", "--no-legend",
             "--no-pager", "capture-zone-testdrive-*.service"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "cannot discover test drives")
        result = []
        for line in completed.stdout.splitlines():
            unit = line.split(maxsplit=1)[0] if line.strip() else ""
            match = TEST_DRIVE_UNIT_RE.fullmatch(unit)
            if match:
                result.append((match.group(1), unit))
        return result

    @staticmethod
    def open_test_drive_cli(namespace: str, cli_port: int) -> NamespaceCliTransport:
        process = subprocess.Popen(
            ["ip", "netns", "exec", namespace, "socat", "-",
             f"TCP:127.0.0.1:{cli_port},connect-timeout=2"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return NamespaceCliTransport(process)

    @staticmethod
    def open_test_drive_shell(drive_id: str, namespace: str,
                              workspace: Path, resolver_path: Path) -> GdbPtyTransport:
        unit = f"capture-zone-testdrive-shell-{drive_id}-{uuid.uuid4().hex[:6]}.service"
        master_fd, slave_fd = pty.openpty()
        environment = {**os.environ, "TERM": "xterm-256color", "HOME": str(workspace)}
        command = [
            "systemd-run", "--quiet", "--wait", "--collect", "--pty", f"--unit={unit}",
            "--property=KillMode=control-group", "--property=TimeoutStopSec=2s",
            "--property=User=nobody", "--property=Group=nogroup",
            "--property=ProtectSystem=strict", "--property=ProtectHome=yes",
            "--property=PrivateDevices=yes", "--property=NoNewPrivileges=yes",
            "--property=CapabilityBoundingSet=", "--property=RestrictSUIDSGID=yes",
            f"--property=NetworkNamespacePath=/run/netns/{namespace}",
            f"--property=BindPaths={workspace}:/work",
            f"--property=BindReadOnlyPaths={resolver_path}:/run/systemd/resolve/stub-resolv.conf",
            "--property=ReadWritePaths=/work",
            "--property=WorkingDirectory=/work",
            "--setenv=TERM=xterm-256color", "--setenv=HOME=/work",
            "--", "/bin/bash", "--noprofile", "--norc", "-i",
        ]
        try:
            process = subprocess.Popen(
                command, env=environment, stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True, close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        from .test_drive import TestDriveShellTransport
        return TestDriveShellTransport(process, master_fd, unit)

    def cleanup_test_drive_shells(self) -> None:
        completed = subprocess.run(
            ["systemctl", "list-units", "--all", "--full", "--plain", "--no-legend",
             "--no-pager", "capture-zone-testdrive-shell-*.service"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        for line in completed.stdout.splitlines():
            unit = line.split(maxsplit=1)[0] if line.strip() else ""
            if not unit.startswith("capture-zone-testdrive-shell-"):
                continue
            subprocess.run(
                ["systemctl", "kill", "--kill-whom=all", "--signal=KILL", unit],
                capture_output=True, text=True, timeout=5, check=False,
            )
            subprocess.run(
                ["systemctl", "reset-failed", unit], capture_output=True, text=True,
                timeout=5, check=False,
            )

    @staticmethod
    def debug_unit_name(instance_id: str) -> str:
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance id")
        return f"capture-zone-gdb-{instance_id}.service"

    def start_debug(self, instance_id: str, pid: int, port: int = 2345) -> tuple[str, str, int]:
        if pid <= 1 or not 1024 <= port <= 65535:
            raise BackendError("invalid debug target")
        if not Path("/usr/bin/gdbserver").is_file():
            raise BackendError("gdbserver is not installed on the appliance")
        allocation = self.allocation(instance_id)
        unit = self.debug_unit_name(instance_id)
        self.stop_debug(instance_id)
        command = [
            "systemd-run", "--quiet", "--collect", f"--unit={unit}",
            "--property=Type=simple", "--property=KillMode=control-group",
            "--property=TimeoutStopSec=5s", "--property=ProtectSystem=strict",
            "--property=ProtectHome=read-only", "--property=PrivateTmp=yes",
            "--property=AmbientCapabilities=CAP_SYS_PTRACE",
            "--property=CapabilityBoundingSet=CAP_SYS_PTRACE",
            f"--property=NetworkNamespacePath=/run/netns/{allocation.namespace}",
            "--", "/usr/bin/gdbserver", "--once", f"{allocation.guest_ip}:{port}",
            "--attach", str(pid),
        ]
        self._run(command)
        return unit, allocation.guest_ip, port

    def stop_debug(self, instance_id: str) -> None:
        unit = self.debug_unit_name(instance_id)
        self._run(["systemctl", "stop", unit], timeout=10, tolerate_missing=True)

    def restart(self, unit: str) -> None:
        """Restart only the Smithproxy process; preserve its netns and routing."""
        completed = subprocess.run(
            ["systemctl", "restart", unit], capture_output=True, text=True,
            timeout=30, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "systemctl restart failed")

    def schedule_deadline(self, instance_id: str, deadline: str, previous: str = "") -> str:
        """Let PID 1 enforce TTL even while the runner is stopped."""
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance ID")
        name = ""
        if deadline:
            instant = datetime.fromisoformat(deadline).astimezone(timezone.utc)
            digest = hashlib.sha256(deadline.encode()).hexdigest()[:16]
            name = f"capture-zone-deadline-{instance_id}-{digest}"
            status = self.status(name + ".timer")
            if status.active_state != "active":
                self._run([
                    "systemd-run", "--quiet", "--collect", f"--unit={name}",
                    f"--on-calendar={instant.strftime('%Y-%m-%d %H:%M:%S.%f UTC')}",
                    "--timer-property=AccuracySec=1s",
                    "--timer-property=RemainAfterElapse=no",
                    "/usr/bin/systemctl", "stop", self.slice_name(instance_id),
                ])
        if previous and previous != name:
            self.cancel_deadline(instance_id, previous)
        return name

    def cancel_deadline(self, instance_id: str, name: str) -> None:
        if not re.fullmatch(r"capture-zone-deadline-" + re.escape(instance_id) + r"-[0-9a-f]{16}", name):
            raise BackendError("invalid deadline timer")
        self._run(["systemctl", "stop", name + ".timer"], timeout=10, tolerate_missing=True)

    def extend_runtime(self, unit: str, total_seconds: int) -> None:
        """Validate an extension against the unit's preallocated hard ceiling.

        RuntimeMaxSec cannot be changed on a running transient service.  Units
        therefore start with the manager's maximum lifetime as a fail-safe;
        the runner reaper enforces the shorter, extendable deadline.
        """
        if total_seconds < 1:
            raise BackendError("invalid total runtime")
        status = self.status(unit)
        if status.active_state not in {"active", "activating", "reloading"}:
            raise BackendError("cannot extend an inactive systemd unit")

    @staticmethod
    def capture_stacktrace(pid: int, since: str = "") -> str:
        if pid <= 1:
            raise BackendError("invalid crashed PID")
        command = [
            "coredumpctl", "debug", "--quiet", "--no-pager", "-1",
            "--debugger=/usr/bin/gdb",
            "--debugger-arguments=-batch -nx -ex set pagination off -ex thread apply all bt full -ex quit",
        ]
        if since:
            command.append(f"--since={since}")
        command.append(str(pid))
        try:
            completed = subprocess.run(
                command, capture_output=True, text=True, timeout=8, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"coredumpctl failed: {exc}") from exc
        output = (completed.stdout + "\n" + completed.stderr).strip()
        if completed.returncode:
            raise BackendError(output[-2000:] or "no coredump is available")
        if len(output) > 65536:
            output = output[:32768] + "\n... stack trace truncated ...\n" + output[-32768:]
        return output

    def discover_units(self) -> list[tuple[str, str]]:
        completed = subprocess.run(
            ["systemctl", "list-units", "--all", "--full", "--plain", "--no-legend",
             "--no-pager", "capture-zone-smithproxy-*.service"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "cannot discover Smithproxy units")
        discovered = []
        for line in completed.stdout.splitlines():
            unit = line.split(maxsplit=1)[0] if line.strip() else ""
            match = UNIT_RE.fullmatch(unit)
            if match and SAFE_ID.fullmatch(match.group(1)):
                discovered.append((match.group(1), unit))
        return discovered

    def status(self, unit: str) -> UnitStatus:
        try:
            completed = subprocess.run(
                ["systemctl", "show", unit, "--property=LoadState,ActiveState,SubState,Result,MainPID"],
                capture_output=True, text=True, timeout=10, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"cannot query systemd unit status: {exc}") from exc
        if "LoadState=not-found" in completed.stdout:
            return UnitStatus("inactive", "dead", "success", 0)
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "cannot query systemd unit status")
        return parse_unit_status(completed.stdout)

    def slice_processes(self, instance_id: str, smithproxy_unit: str) -> list[dict]:
        """Return live process members of one logical SAS Slice."""
        members = [("smithproxy", smithproxy_unit)]
        if getattr(self, 'microservices', None):
            members.extend((f"microservice:{record['prefix']}", record['unit'])
                           for record in self.microservices.runs(instance_id)
                           if record['state'] != 'stopped')
        try:
            if self.allocation(instance_id).topology == "tuntom-via":
                members.append(("tuntom-relay", self.tuntom_relay_unit_name(instance_id)))
                members.append(("tuntom-adapter", self.tuntom_unit_name(instance_id)))
        except BackendError:
            pass
        result = []
        for role, unit in members:
            status = self.status(unit)
            result.append({
                "role": role, "unit": unit, "pid": status.main_pid,
                "state": status.active_state, "substate": status.sub_state,
                "result": status.result,
                "rss_bytes": self.rss_bytes(status.main_pid),
            })
        return result

    @staticmethod
    def rss_bytes(pid: int) -> int:
        """Read the main process resident set without invoking another tool."""
        if pid <= 0:
            return 0
        try:
            for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
        except (OSError, ValueError, IndexError):
            pass
        return 0

    def open_cli(self, instance_id: str, cli_port: int) -> NamespaceCliTransport:
        allocation = self.allocation(instance_id)
        process = subprocess.Popen(
            ["ip", "netns", "exec", allocation.namespace, "socat", "-",
             f"TCP:127.0.0.1:{cli_port},connect-timeout=2"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        return NamespaceCliTransport(process)

    def open_gdb(self, instance_id: str, binary: Path, address: str,
                 port: int) -> GdbPtyTransport:
        if not SAFE_ID.fullmatch(instance_id) or not binary.is_file():
            raise BackendError("invalid GDB target")
        if not Path("/usr/bin/gdb").is_file():
            raise BackendError("gdb is not installed on the appliance")
        allocation = self.allocation(instance_id)
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise BackendError("invalid gdbserver address") from exc
        if str(parsed_address) != allocation.guest_ip or not 1024 <= port <= 65535:
            raise BackendError("gdbserver target does not match the instance allocation")
        master_fd, slave_fd = pty.openpty()
        environment = {
            **os.environ,
            "TERM": "xterm-256color",
            "HOME": str(binary.parent),
            "GDBHISTFILE": "/dev/null",
        }
        try:
            process = subprocess.Popen(
                [
                    "/usr/bin/gdb", "--quiet", "--nx", str(binary),
                    "-ex", "set pagination off",
                    "-ex", "set confirm off",
                    "-ex", f"target remote {parsed_address}:{port}",
                ],
                cwd=binary.parent, env=environment,
                stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
                start_new_session=True, close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            raise
        finally:
            os.close(slave_fd)
        return GdbPtyTransport(process, master_fd)

    def native_save(self, binary: Path, config_path: Path, cli_port: int = 50000) -> str:
        """Round-trip one rendered config through Smithproxy's own `save config`.

        The short-lived process gets an isolated network namespace and /run,
        so its listeners and fixed PID file cannot collide with appliances.
        Callers own the temporary config and may compare the returned canonical
        native text. No comment stripping or third-party parser is involved.
        """
        if not binary.is_file() or not config_path.is_file():
            raise BackendError("native config normalizer input is unavailable")
        suffix = uuid.uuid4().hex[:8]
        namespace = f"cz-norm-{suffix}"
        unit = f"capture-zone-normalize-{suffix}.service"
        private_run = config_path.parent / "native-run"
        private_run.mkdir(mode=0o700, exist_ok=True)
        try:
            self._run(["ip", "netns", "add", namespace])
            self._run(["ip", "-n", namespace, "link", "set", "lo", "up"])
            command = [
                "systemd-run", "--quiet", "--collect", f"--unit={unit}",
                "--property=Type=simple", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=5s", "--property=RuntimeMaxSec=45s",
                "--property=MemoryMax=1G", "--property=TasksMax=256",
                "--property=ProtectSystem=strict",
                f"--property=NetworkNamespacePath=/run/netns/{namespace}",
                f"--property=BindPaths={private_run}:/run",
                f"--property=ReadWritePaths={config_path.parent}",
                "--", str(binary), "--config-file", str(config_path),
            ]
            self._run(command, timeout=15)
            transport = None
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                status = self.status(unit)
                if status.active_state in {"failed", "inactive"} and status.result not in {"", "success"}:
                    details = self.logs(unit, 40)
                    raise BackendError(
                        "Smithproxy rejected the exact runtime config during preflight:\n"
                        + details[-4000:]
                    )
                candidate = subprocess.Popen(
                    ["ip", "netns", "exec", namespace, "socat", "-",
                     f"TCP:127.0.0.1:{cli_port},connect-timeout=1"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                )
                time.sleep(0.15)
                if candidate.poll() is None:
                    transport = NamespaceCliTransport(candidate)
                    break
                candidate.wait(timeout=1)
                time.sleep(0.2)
            if transport is None:
                details = self.logs(unit, 40)
                raise BackendError(
                    "Smithproxy native-save CLI did not become ready; runtime config log:\n"
                    + details[-4000:]
                )
            try:
                transport.sendall(b"enable\r\nsave config\r\n")
                response = bytearray()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    transport.settimeout(0.4)
                    try:
                        chunk = transport.recv(8192)
                    except TimeoutError:
                        continue
                    if not chunk:
                        break
                    response.extend(chunk)
                    lowered = bytes(response).lower()
                    if b"config saved successfully" in lowered:
                        break
                    if b"error writing config" in lowered:
                        raise BackendError("Smithproxy failed to write its native config")
                else:
                    raise BackendError("Smithproxy native save timed out")
                if b"config saved successfully" not in bytes(response).lower():
                    raise BackendError("Smithproxy did not confirm native config save")
            finally:
                transport.close()
            content = config_path.read_text(encoding="utf-8")
            if not content.strip() or "\0" in content:
                raise BackendError("Smithproxy produced an invalid native config")
            return content
        finally:
            self._run(["systemctl", "stop", unit], timeout=10, tolerate_missing=True)
            self._run(["ip", "netns", "delete", namespace], timeout=10, tolerate_missing=True)
            # Smithproxy does not unlink its fixed PID file reliably during a
            # forced/slow shutdown. This directory is private to the observer.
            try:
                (private_run / "smithproxy.default.pid").unlink(missing_ok=True)
            except OSError:
                pass

    def logs(self, unit: str, lines: int = 200) -> str:
        completed = subprocess.run(
            ["journalctl", "--no-pager", "--output=short-iso", f"--lines={lines}",
             f"--unit={unit}"], capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            raise BackendError(completed.stderr.strip() or "journalctl failed")
        return completed.stdout[-256 * 1024:]
