from __future__ import annotations

import ipaddress
import json
import math
import os
import pty
import re
import select
import signal
import subprocess
import time
import uuid
import threading
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from .systemd import BackendError, UnitStatus
from .network_settings import NetworkSettings


SAFE_ID = re.compile(r"^[0-9a-f-]{36}$")
UNIT_RE = re.compile(r"^capture-zone-smithproxy-([0-9a-f-]{36})\.service$")
TEST_DRIVE_UNIT_RE = re.compile(r"^capture-zone-testdrive-([0-9a-f-]{36})\.service$")
TEST_DRIVE_INGRESS_NAMESPACE = uuid.UUID("7fa9bc87-6a15-44d2-90cf-f20188b07c42")


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


class NamespaceBackend:
    """Root backend: one netns, veth pair and nft table per Smithproxy."""

    def __init__(self, smithproxy_binary: str = "/usr/local/lib/capture-zone/smithproxy",
                 network_settings: NetworkSettings | None = None) -> None:
        self.smithproxy_binary = smithproxy_binary
        self.network_settings = network_settings
        self.allocation_lock = threading.RLock()

    @staticmethod
    def unit_name(instance_id: str) -> str:
        return f"capture-zone-smithproxy-{instance_id}.service"

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
    def _legacy_allocation(instance_id: str) -> NetworkAllocation:
        if not SAFE_ID.fullmatch(instance_id):
            raise BackendError("invalid instance id")
        compact = instance_id.replace("-", "")
        number = int(compact[:8], 16)
        second = 64 + ((number >> 14) % 64)
        third = (number >> 6) & 255
        fourth = (number & 63) * 4
        suffix = compact[:8]
        return NetworkAllocation(
            namespace=f"cz-{suffix}", host_if=f"czh{suffix[:8]}", guest_if="eth0",
            host_ip=f"10.{second}.{third}.{fourth + 1}", guest_ip=f"10.{second}.{third}.{fourth + 2}",
            table=f"cz_{suffix}",
            route_table=10000 + (number % 40000),
            mark=0x100000 + (number % 0xEFFFFF),
            subnet=f"10.{second}.{third}.{fourth}/30",
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
        return self._legacy_allocation(instance_id)

    def _live_subnets(self) -> set[str]:
        result = set()
        for interface in self._ip_json(["ip", "-j", "addr", "show"]):
            if not str(interface.get("ifname", "")).startswith(("czh", "czi")):
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

    def _allocate(self, instance_id: str) -> NetworkAllocation:
        if not self.network_settings:
            return self._legacy_allocation(instance_id)
        with self.allocation_lock, self.network_settings.lock:
            values = self.network_settings.allocations()
            existing = values.get(instance_id)
            if isinstance(existing, dict):
                return NetworkAllocation(**existing)
            settings = self.network_settings.get()
            network = ipaddress.ip_network(settings["namespace_cidr"])
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
            compact = instance_id.replace("-", "")
            suffix = compact[:8]
            hosts = list(selected.hosts())
            item = NetworkAllocation(
                namespace=f"cz-{suffix}", host_if=f"czh{suffix[:8]}", guest_if="eth0",
                host_ip=str(hosts[0]), guest_ip=str(hosts[1]), table=f"cz_{suffix}",
                route_table=settings["route_table_start"] + index,
                mark=settings["mark_start"] + index, subnet=str(selected),
                egress_mode=settings["egress_mode"],
                sas_interface=settings["sas_interface"],
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
        self._run(["nft", "delete", "table", "ip", allocation.table], tolerate_missing=True)
        self._run(["ip", "rule", "delete", "fwmark", hex(allocation.mark),
                   "table", str(allocation.route_table)], tolerate_missing=True)
        self._run(["ip", "route", "flush", "table", str(allocation.route_table)], tolerate_missing=True)
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
        subnet = allocation.subnet or str(ipaddress.ip_network(f"{allocation.host_ip}/30", strict=False))
        host_interfaces = self._ip_json([
            "ip", "-j", "addr", "show", "dev", allocation.host_if,
        ])
        namespace_interfaces = self._ip_json([
            "ip", "-j", "-n", allocation.namespace, "addr", "show",
        ])
        namespace_routes = self._ip_json([
            "ip", "-j", "-n", allocation.namespace, "route", "show", "table", "all",
        ])
        host_routes = self._ip_json([
            "ip", "-j", "route", "show", "table", str(allocation.route_table),
        ])
        host_rules = [
            item for item in self._ip_json(["ip", "-j", "rule", "show"])
            if str(item.get("table", "")) == str(allocation.route_table)
        ]
        return {
            "present": bool(host_interfaces and namespace_interfaces),
            "subnet": subnet,
            "host_interface": allocation.host_if,
            "guest_interface": allocation.guest_if,
            "expected_host_address": f"{allocation.host_ip}/30",
            "expected_guest_address": f"{allocation.guest_ip}/30",
            "route_table": allocation.route_table,
            "packet_mark": hex(allocation.mark),
            "host_interfaces": host_interfaces,
            "namespace_interfaces": namespace_interfaces,
            "namespace_routes": namespace_routes,
            "host_routes": host_routes,
            "host_rules": host_rules,
        }

    def start(self, instance_id: str, config_path: Path, runtime_seconds: int,
              source_ip: str = "", tls_port: int = 10443,
              plaintext_port: int = 10080, socks_port: int = 1080, cli_port: int = 10000,
              http_port: int = 3128, smithproxy_binary: str = "",
              profile: str = "custom", config_mode: str = "ro",
              assets_path: str = "", auto_restart: bool = False,
              hard_runtime_seconds: int = 86400) -> str:
        try:
            source = str(ipaddress.ip_address(source_ip))
        except ValueError as exc:
            raise BackendError("source_ip must be a valid IP address") from exc
        if ":" in source:
            raise BackendError("the MVP namespace backend currently supports IPv4 source addresses only")
        allocation = self._allocate(instance_id)
        self._cleanup_network(allocation)
        try:
            self._run(["ip", "netns", "add", allocation.namespace])
            self._run(["ip", "link", "add", allocation.host_if, "type", "veth", "peer", "name", allocation.guest_if])
            self._run(["ip", "link", "set", allocation.guest_if, "netns", allocation.namespace])
            self._run(["ip", "addr", "add", f"{allocation.host_ip}/30", "dev", allocation.host_if])
            self._run(["ip", "link", "set", allocation.host_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "addr", "add", f"{allocation.guest_ip}/30", "dev", allocation.guest_if])
            self._run(["ip", "-n", allocation.namespace, "link", "set", "lo", "up"])
            self._run(["ip", "-n", allocation.namespace, "link", "set", allocation.guest_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "route", "add", "default", "via", allocation.host_ip])

            self._run(["ip", "route", "add", "default", "via", allocation.guest_ip,
                       "dev", allocation.host_if, "table", str(allocation.route_table)])
            self._run(["ip", "rule", "add", "fwmark", hex(allocation.mark),
                       "table", str(allocation.route_table), "priority", "1000"])
            self._run(["ip", "-n", allocation.namespace, "rule", "add", "fwmark", "0x1",
                       "table", "100", "priority", "100"])
            self._run(["ip", "-n", allocation.namespace, "route", "add", "local", "0.0.0.0/0",
                       "dev", "lo", "table", "100"])

            explicit_port = socks_port if profile == "socks" else http_port
            explicit = profile in {"socks", "http-proxy"}
            match = f"ip saddr {source} tcp dport {explicit_port}" if explicit else f"ip saddr {source}"
            dnat_chain = (
                " chain proxy_dnat { type nat hook prerouting priority dstnat; policy accept;\n"
                f"  ip saddr {source} tcp dport {explicit_port} dnat to {allocation.guest_ip}:{explicit_port}\n }}\n"
                if explicit else ""
            )
            snat_rule = ""
            if allocation.egress_mode == "masquerade":
                interface_match = (
                    f'oifname "{allocation.sas_interface}" ' if allocation.sas_interface else ""
                )
                snat_rule = f"  {interface_match}ip saddr {allocation.guest_ip} masquerade\n"
            rules = (
                f"table ip {allocation.table} {{\n"
                " chain prerouting { type filter hook prerouting priority mangle; policy accept;\n"
                f"  {match} meta mark set {hex(allocation.mark)}\n"
                f" }}\n{dnat_chain} chain postrouting {{ type nat hook postrouting priority srcnat; policy accept;\n"
                f"{snat_rule} }}\n}}"
            )
            completed = subprocess.run(["nft", "-f", "/dev/stdin"], input=rules, capture_output=True,
                                       text=True, timeout=10, check=False)
            if completed.returncode:
                raise BackendError(completed.stderr.strip() or "nft failed")

            if not explicit:
                namespace_rules = (
                    "table ip capture_zone {\n"
                    " chain prerouting { type filter hook prerouting priority mangle; policy accept;\n"
                    f"  iifname \"{allocation.guest_if}\" tcp dport 443 tproxy to :{tls_port} meta mark set 0x1\n"
                    f"  iifname \"{allocation.guest_if}\" tcp dport != 443 tproxy to :{plaintext_port} meta mark set 0x1\n"
                    " }\n}"
                )
                completed = subprocess.run(
                    ["ip", "netns", "exec", allocation.namespace, "nft", "-f", "/dev/stdin"],
                    input=namespace_rules, capture_output=True, text=True, timeout=10, check=False,
                )
                if completed.returncode:
                    raise BackendError(completed.stderr.strip() or "namespace nft failed")

            unit = self.unit_name(instance_id)
            # Smithproxy uses the fixed /var/run/smithproxy.default.pid path.
            # Give every transient unit its own /run mount; /var/run is a
            # symlink to it on modern distributions.  A network namespace
            # alone does not isolate PID files.
            private_run = config_path.parent / "run"
            effective_binary = Path(smithproxy_binary or self.smithproxy_binary).resolve()
            command = [
                "systemd-run", "--quiet", f"--unit={unit}",
                "--property=Type=simple", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=10s",
                "--property=MemoryMax=1G", "--property=TasksMax=256",
                f"--property=NetworkNamespacePath=/run/netns/{allocation.namespace}",
                *self._instance_storage_properties(
                    config_path, private_run, effective_binary,
                ),
            ]
            if hard_runtime_seconds:
                command.insert(5, f"--property=RuntimeMaxSec={hard_runtime_seconds}s")
            if auto_restart:
                # The runner performs bounded restarts so TTL remains
                # authoritative. Keep the failed transient unit loaded long
                # enough to restart it and let systemd enforce a second limit.
                command.extend([
                    "--property=StartLimitIntervalSec=60s",
                    "--property=StartLimitBurst=5",
                ])
            else:
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
                "--", str(effective_binary), "--config-file", str(config_path),
            ])
            self._run(command)
            return unit
        except Exception:
            self._cleanup_network(allocation)
            if self.network_settings:
                self.network_settings.release(instance_id)
            raise

    def stop(self, unit: str) -> None:
        instance_id = unit.removeprefix("capture-zone-smithproxy-").removesuffix(".service")
        self.stop_debug(instance_id)
        completed = subprocess.run(["systemctl", "stop", unit], capture_output=True, text=True,
                                   timeout=20, check=False)
        if completed.returncode and "not loaded" not in completed.stderr.lower():
            raise BackendError(completed.stderr.strip() or "systemctl stop failed")
        self._cleanup_network(self.allocation(instance_id))
        if self.network_settings:
            self.network_settings.release(instance_id)

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
        allocation = replace(self._allocate(drive_id), guest_if="do0")
        ingress_id = self.test_drive_ingress_id(drive_id)
        try:
            ingress_raw = self._allocate(ingress_id)
        except Exception:
            if self.network_settings:
                self.network_settings.release(drive_id)
            raise
        ingress = replace(
            ingress_raw, namespace=allocation.namespace,
            host_if=f"czi{drive_id.replace('-', '')[:8]}", guest_if="di0",
        )
        self._cleanup_network(allocation)
        # The allocator gives the ingress lease its own synthetic namespace
        # identity. Test Drive joins both veths into the proxy namespace.
        self._cleanup_network(ingress_raw)
        try:
            self._run(["ip", "netns", "add", allocation.namespace])
            self._run(["ip", "link", "add", allocation.host_if, "type", "veth",
                       "peer", "name", allocation.guest_if])
            self._run(["ip", "link", "set", allocation.guest_if, "netns", allocation.namespace])
            self._run(["ip", "addr", "add", f"{allocation.host_ip}/30", "dev", allocation.host_if])
            self._run(["ip", "link", "set", allocation.host_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "addr", "add",
                       f"{allocation.guest_ip}/30", "dev", allocation.guest_if])
            self._run(["ip", "-n", allocation.namespace, "link", "set", "lo", "up"])
            self._run(["ip", "-n", allocation.namespace, "link", "set",
                       allocation.guest_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "route", "add", "default",
                       "via", allocation.host_ip])
            self._run(["ip", "link", "add", ingress.host_if, "type", "veth",
                       "peer", "name", ingress.guest_if])
            self._run(["ip", "link", "set", ingress.guest_if, "netns", allocation.namespace])
            self._run(["ip", "addr", "add", f"{ingress.host_ip}/30", "dev", ingress.host_if])
            self._run(["ip", "link", "set", ingress.host_if, "up"])
            self._run(["ip", "-n", allocation.namespace, "addr", "add",
                       f"{ingress.guest_ip}/30", "dev", ingress.guest_if])
            self._run(["ip", "-n", allocation.namespace, "link", "set",
                       ingress.guest_if, "up"])
            # Accepted connections must return through di0 while independent
            # proxy egress continues to use the default route on do0.
            self._run(["ip", "-n", allocation.namespace, "route", "add", "default",
                       "via", ingress.host_ip, "dev", ingress.guest_if,
                       "table", "101"])
            self._run(["ip", "-n", allocation.namespace, "rule", "add", "from",
                       f"{ingress.guest_ip}/32", "table", "101", "priority", "101"])
            interface_match = (
                f'oifname "{allocation.sas_interface}" ' if allocation.sas_interface else ""
            )
            rules = (
                f"table ip {allocation.table} {{\n"
                " chain postrouting { type nat hook postrouting priority srcnat; policy accept;\n"
                f"  {interface_match}ip saddr {allocation.guest_ip} masquerade\n"
                " }\n}"
            )
            completed = subprocess.run(
                ["nft", "-f", "/dev/stdin"], input=rules, capture_output=True,
                text=True, timeout=10, check=False,
            )
            if completed.returncode:
                raise BackendError(completed.stderr.strip() or "test drive nft failed")
            unit = self.test_drive_unit_name(drive_id)
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
        self._clear_test_drive_runtime_override(unit)
        command = [
                "systemd-run", "--quiet", "--collect", f"--unit={unit}",
                "--property=Type=simple", "--property=KillMode=control-group",
                "--property=TimeoutStopSec=10s", f"--property=RuntimeMaxSec={ttl_seconds}s",
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
        completed = subprocess.run(
            ["systemctl", "show", unit, "--property=ActiveState,SubState,Result,MainPID"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if completed.returncode:
            return UnitStatus("inactive", "dead", "unknown", 0)
        return parse_unit_status(completed.stdout)

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
