"""Unaddressed L2 links. Reservations, not running processes, consume cable ends.

Each bridge lives in its own network namespace: it has no host uplink. Only
managed namespace/veth endpoints are currently supported. TAP activation must
be added together with the QEMU lifecycle (a manifest alone is not a VM).
"""
from __future__ import annotations

import copy
import json
import logging
import re
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Callable

from .deployments import atomic_json
from .systemd import BackendError
from .wiring import Wiring, bindings


def identifier(value: object) -> str:
    try:
        result = str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise BackendError("invalid L2 object UUID") from exc
    if result != value:
        raise BackendError("L2 object UUID must be canonical")
    return result


class LinuxL2:
    """Never adopts an existing interface without checking its ownership alias."""

    @staticmethod
    def namespace(segment: dict) -> str:
        return "sas-l2-" + identifier(segment["id"])

    @staticmethod
    def port(endpoint: dict) -> str:
        return "p" + identifier(endpoint["id"]).replace("-", "")[:12]

    @staticmethod
    def tag(segment: dict, endpoint: dict | None = None) -> str:
        return "sas:l2:" + identifier(segment["id"]) + (
            ":" + identifier(endpoint["id"]) if endpoint else "")

    def run(self, *args: str) -> str:
        try:
            result = subprocess.run(["ip", *args], capture_output=True, text=True,
                                    timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"L2 ip operation failed: {exc}") from exc
        if result.returncode:
            raise BackendError(result.stderr.strip() or "L2 ip operation failed")
        return result.stdout

    def exists(self, namespace: str) -> bool:
        return Path("/run/netns", namespace).exists()

    def links(self, namespace: str) -> list[dict]:
        return json.loads(self.run("-n", namespace, "-j", "link", "show"))

    def owned(self, link: dict, tag: str) -> None:
        if link.get("ifalias") != tag:
            raise BackendError("L2 interface ownership mismatch; refusing to touch it")

    def bridge(self, segment: dict) -> None:
        ns = self.namespace(segment)
        tag = self.tag(segment)
        if not self.exists(ns):
            self.run("netns", "add", ns)
            try:
                if Path("/proc/sys/net/ipv6").exists():
                    # Disable the bridge namespace's L3 stack, not IPv6 Ethernet
                    # forwarding. SLAAC must not give the cable its own address.
                    self.run("netns", "exec", ns, "sysctl", "-q", "-w",
                             "net.ipv6.conf.all.disable_ipv6=1",
                             "net.ipv6.conf.default.disable_ipv6=1")
                self.run("-n", ns, "link", "add", "br0", "type", "bridge")
                self.run("-n", ns, "link", "set", "br0", "alias", tag)
                self.run("-n", ns, "link", "set", "br0", "addrgenmode", "none")
            except BackendError:
                self.run("netns", "delete", ns)
                raise
        links = self.links(ns)
        bridge = next((link for link in links if link["ifname"] == "br0"), {})
        self.owned(bridge, tag)
        expected = {self.port(ep): self.tag(segment, ep) for ep in segment["endpoints"]}
        # Fail closed if someone plugs in another port outside SAS. Do not delete
        # that foreign port or attempt to repair the operator's configuration.
        foreign = [link for link in links if link["ifname"] not in {"lo", "br0"}
                   and (link["ifname"] not in expected
                        or expected[link["ifname"]] != link.get("ifalias"))]
        if foreign:
            self.run("-n", ns, "link", "set", "br0", "down")
            raise BackendError("foreign interface in L2 namespace; bridge disabled")
        self.run("-n", ns, "link", "set", "br0", "up")

    def attach(self, segment: dict, endpoint: dict, namespace: str) -> None:
        ns, port, tag = self.namespace(segment), self.port(endpoint), self.tag(segment, endpoint)
        target_name = endpoint["interface"]
        local = next((i for i in self.links(ns) if i["ifname"] == port), None)
        target = next((i for i in self.links(namespace) if i["ifname"] == target_name), None)
        if target:
            self.owned(target, tag)
        if local:
            self.owned(local, tag)
            if target and target.get("link_index") == local["ifindex"] and local.get("link_index") == target["ifindex"]:
                if local.get("master") != "br0":
                    raise BackendError("L2 bridge attachment changed outside SAS")
                return  # Do not reset addresses, MTU or state configured by the guest.
            if target:
                raise BackendError("L2 peer mismatch; refusing to replace an existing port")
            self.run("-n", ns, "link", "delete", port)
        elif target:
            raise BackendError("L2 target exists without its bridge peer")
        temporary = "t" + port[1:]
        if any(i["ifname"] == temporary for i in self.links(ns)):
            raise BackendError("L2 temporary interface collision")
        self.run("-n", ns, "link", "add", port, "type", "veth", "peer", "name", temporary)
        try:
            for name in (port, temporary):
                self.run("-n", ns, "link", "set", name, "alias", tag)
                self.run("-n", ns, "link", "set", name, "addrgenmode", "none")
            self.run("-n", ns, "link", "set", temporary, "netns", namespace)
            self.run("-n", namespace, "link", "set", temporary, "name", target_name)
            # Moving a device resets namespace-scoped IPv6 settings.
            self.run("-n", namespace, "link", "set", target_name, "addrgenmode", "none")
            if Path("/proc/sys/net/ipv6").exists():
                self.run("netns", "exec", namespace, "sysctl", "-q", "-w",
                         f"net.ipv6.conf.{target_name}.accept_ra=0",
                         f"net.ipv6.conf.{target_name}.autoconf=0")
            self.run("-n", ns, "link", "set", port, "master", "br0")
            self.run("-n", ns, "link", "set", port, "up")
            self.run("-n", namespace, "link", "set", target_name, "up")
        except BackendError:
            self.run("-n", ns, "link", "delete", port)
            raise

    def detach(self, segment: dict, endpoint: dict) -> None:
        ns = self.namespace(segment)
        if not self.exists(ns):
            return
        port = next((i for i in self.links(ns) if i["ifname"] == self.port(endpoint)), None)
        if port:
            self.owned(port, self.tag(segment, endpoint))
            self.run("-n", ns, "link", "delete", port["ifname"])

    def delete(self, segment: dict) -> None:
        if segment["endpoints"]:
            raise BackendError("disconnect reserved endpoints first")
        ns = self.namespace(segment)
        if self.exists(ns):
            self.bridge(segment)  # Also refuses foreign interfaces.
            self.run("netns", "delete", ns)


class L2Segments:
    def __init__(self, path: Path, resolve: Callable[[str], dict | None], backend=None):
        self.path, self.resolve = path, resolve
        self.backend = backend or LinuxL2()
        self.lock = threading.RLock()
        self.operation_lock = threading.Lock()
        self.wiring = Wiring(path.with_name('wiring-addresses.json'))

    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except FileNotFoundError:
            return []
        except (ValueError, OSError) as exc:
            raise BackendError(f"cannot read L2 catalogue: {exc}") from exc
        if data.get("schema") != 1 or not isinstance(data.get("segments"), list):
            raise BackendError("unsupported L2 catalogue schema")
        return data["segments"]

    def _save(self, items: list[dict]) -> None:
        data = copy.deepcopy(items)
        for item in data:
            for endpoint in item['endpoints']:
                endpoint.pop('addressing', None)
        atomic_json(self.path, {"schema": 1, "segments": data})

    @staticmethod
    def _find(items: list[dict], segment_id: str) -> dict:
        identifier(segment_id)
        for item in items:
            if item["id"] == segment_id:
                return item
        raise BackendError("L2 segment not found")

    def list(self) -> list[dict]:
        with self.lock:
            items = copy.deepcopy(self._load())
            ports = self.wiring.load()
            for item in items:
                for endpoint in item['endpoints']:
                    endpoint['addressing'] = self.wiring.get(endpoint, ports)
            return items

    def inventory(self):
        with self.lock:
            return self.wiring.inventory(self._load())

    def configure_addressing(self, segment_id, endpoint_id, payload):
        identifier(endpoint_id)
        with self.operation_lock, self.lock:
            item = self._find(self._load(), segment_id)
            endpoint = next((ep for ep in item['endpoints'] if ep['id'] == endpoint_id), None)
            if not endpoint:
                raise BackendError('endpoint not found')
            return self.wiring.configure(endpoint, payload)

    def reserve_instance(self, instance_id, requested):
        """Reserve every requested end atomically before creating the namespace."""
        identifier(instance_id)
        requested = bindings(requested)
        with self.operation_lock, self.lock:
            items = self._load()
            for binding in requested:
                item = self._find(items, binding['segment_id'])
                existing = next((ep for segment in items for ep in segment['endpoints']
                                 if ep['instance_id'] == instance_id and ep['interface'] == binding['interface']), None)
                if existing:
                    if existing not in item['endpoints']:
                        raise BackendError('instance port is already reserved by another segment')
                    continue
                if item['kind'] == 'virtual-cable' and len(item['endpoints']) >= 2:
                    raise BackendError('virtual cable already has two reserved endpoints')
                item['endpoints'].append({'id': str(uuid.uuid4()), 'instance_id': instance_id,
                    'interface': binding['interface'], 'type': 'veth', 'state': 'reserved', 'error': ''})
            # All capacities validated against this in-memory transaction before committing.
            self._save(items)
            for binding in requested:
                if 'addressing' in binding:
                    self.wiring.configure({'instance_id': instance_id, 'interface': binding['interface']}, binding['addressing'])

    def release_instance(self, instance_id):
        """Failed-spawn rollback, only this instance's reservations."""
        identifier(instance_id)
        with self.operation_lock, self.lock:
            items = self._load()
            for item in items:
                for endpoint in list(item['endpoints']):
                    if endpoint['instance_id'] == instance_id:
                        self.backend.detach(item, endpoint)
                        item['endpoints'].remove(endpoint)
            self._save(items)

    def prepare_instance(self, instance, *, apply_addressing=True):
        """Attach reserved ports and apply addressing under the 00-start lifecycle."""
        expected = 'cz-' + instance.id[:8]
        if instance.namespace != expected:
            raise BackendError('unexpected Wiring instance namespace')
        checked = []
        with self.operation_lock:
            for segment in self.list():
                for endpoint in segment['endpoints']:
                    if endpoint['instance_id'] != instance.id:
                        continue
                    self.backend.bridge(segment)
                    self.backend.attach(segment, endpoint, expected)
                    if apply_addressing:
                        self.wiring.apply(segment, endpoint, expected, self.backend)
                    checked.append({'segment_id': segment['id'], 'endpoint_id': endpoint['id'],
                                    'interface': endpoint['interface'], **self.wiring.get(endpoint)})
        return checked

    def get(self, segment_id: str) -> dict:
        return self._find(self.list(), segment_id)

    def _result(self, segment_id: str) -> dict:
        item = self.get(segment_id)
        if item["state"] == "error":
            errors = [item["error"], *(ep["error"] for ep in item["endpoints"])]
            raise BackendError(f"L2 {segment_id}: " + "; ".join(e for e in errors if e)
                               + " (reservation retained; inspect segment)")
        return item

    def create(self, payload: dict) -> dict:
        name = str(payload.get("name", "")).strip()
        kind = payload.get("kind")
        if kind not in {"virtual-cable", "virtual-switch"}:
            raise BackendError("kind must be virtual-cable or virtual-switch")
        if not name or len(name) > 128 or any(ord(c) < 32 for c in name):
            raise BackendError("invalid L2 segment name")
        with self.operation_lock, self.lock:
            items = self._load()
            item = {"id": str(uuid.uuid4()), "name": name, "kind": kind,
                    "endpoints": [], "state": "pending", "error": ""}
            item["namespace"] = self.backend.namespace(item)
            items.append(item)
            self._save(items)
        self.reconcile()
        return self._result(item["id"])

    def attach(self, segment_id: str, payload: dict) -> dict:
        instance_id = identifier(payload.get("instance_id"))
        interface = str(payload.get("interface", ""))
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,14}", interface) or interface == "lo":
            raise BackendError("invalid endpoint interface name")
        if payload.get("type", "veth") != "veth":
            raise BackendError("TAP requires a running QEMU lifecycle, which is not implemented")
        # Resolve before taking catalogue locks (never invert manager/catalogue locks).
        target = self.resolve(instance_id)
        if not target or target.get("state") == "orphaned":
            raise BackendError("endpoint must belong to a managed instance")
        with self.operation_lock, self.lock:
            items = self._load()
            item = self._find(items, segment_id)
            existing = False
            for segment in items:
                for ep in segment["endpoints"]:
                    if (ep["instance_id"], ep["interface"]) == (instance_id, interface):
                        if segment["id"] == segment_id:
                            existing = True
                        else:
                            raise BackendError("instance port is already reserved by another segment")
            if not existing and item["kind"] == "virtual-cable" and len(item["endpoints"]) >= 2:
                raise BackendError("virtual cable already has two reserved endpoints")
            if not existing:
                item["endpoints"].append({"id": str(uuid.uuid4()), "instance_id": instance_id,
                    "interface": interface, "type": "veth", "state": "reserved", "error": ""})
            self._save(items)  # Reserve durably BEFORE any kernel operation.
        self.reconcile()
        return self._result(segment_id)

    def detach(self, segment_id: str, endpoint_id: str) -> dict:
        identifier(endpoint_id)
        with self.operation_lock:
            item = self.get(segment_id)
            endpoint = next((ep for ep in item["endpoints"] if ep["id"] == endpoint_id), None)
            if endpoint:
                self.backend.detach(item, endpoint)  # Failure retains the reservation.
                with self.lock:
                    items = self._load()
                    current = self._find(items, segment_id)
                    current["endpoints"] = [ep for ep in current["endpoints"] if ep["id"] != endpoint_id]
                    self._save(items)
        return self.get(segment_id)

    def delete(self, segment_id: str) -> dict:
        with self.operation_lock:
            item = self.get(segment_id)
            self.backend.delete(item)
            with self.lock:
                self._save([i for i in self._load() if i["id"] != segment_id])
            return item

    def reconcile(self) -> None:
        # Readers only take self.lock: slow ip operations never block GET requests.
        with self.operation_lock:
            items = self.list()
            for item in items:
                try:
                    self.backend.bridge(item)
                    for endpoint in list(item["endpoints"]):
                        try:
                            target = self.resolve(endpoint["instance_id"])
                            if target is None:  # Deleted record, not merely stopped.
                                self.backend.detach(item, endpoint)
                                item["endpoints"].remove(endpoint)
                                continue
                            if target["state"] in {"starting", "recovering", "running"} and target.get('desired_state', 'running') == 'running':
                                expected = "cz-" + endpoint["instance_id"][:8]
                                if target.get("namespace") != expected:
                                    raise BackendError("unexpected instance namespace")
                                self.backend.attach(item, endpoint, expected)
                                endpoint["state"] = "connected"
                            else:
                                self.backend.detach(item, endpoint)
                                endpoint["state"] = "reserved"
                            endpoint["error"] = ""
                        except (BackendError, OSError) as exc:
                            endpoint.update(state="error", error=str(exc))
                    item.update(state="error" if any(ep["error"] for ep in item["endpoints"]) else "ready", error="")
                except (BackendError, OSError) as exc:
                    item.update(state="error", error=str(exc))
                    for endpoint in item["endpoints"]:
                        endpoint.update(state="unknown", error=str(exc))
                with self.lock:
                    self._save(items)

    def run(self, stopping: threading.Event) -> None:
        while not stopping.is_set():
            try:
                self.reconcile()
            except Exception:
                logging.exception("L2 reconciliation failed; reservations retained")
            stopping.wait(5)
