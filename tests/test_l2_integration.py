"""Opt-in kernel test; touches only randomly named test network namespaces."""
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from runner.l2_segments import L2Segments, LinuxL2
from runner.systemd import BackendError


@unittest.skipUnless(os.environ.get("SAS_L2_INTEGRATION") == "1" and os.geteuid() == 0,
                     "requires explicit opt-in and root on integration server")
class L2IntegrationTests(unittest.TestCase):
    def test_kernel_cable_dual_stack_restart_guards_and_cleanup(self):
        backend = LinuxL2()
        targets, created_namespaces = {}, []
        segment = None
        with tempfile.TemporaryDirectory(prefix="sas-l2-test-") as root:
            store = L2Segments(Path(root) / "l2.json", targets.get, backend)
            try:
                for _ in range(3):
                    instance_id = str(uuid.uuid4())
                    ns = "cz-" + instance_id[:8]
                    backend.run("netns", "add", ns)
                    created_namespaces.append(ns)
                    targets[instance_id] = {"state": "running", "namespace": ns}
                segment = store.create({"name": "integration-test", "kind": "virtual-cable"})
                ids = list(targets)
                for instance_id in ids[:2]:
                    store.reserve_instance(instance_id, [{'segment_id': segment['id'], 'interface': 'cable0'}])
                    store.prepare_instance(SimpleNamespace(id=instance_id, namespace=targets[instance_id]['namespace']),
                                           apply_addressing=False)
                    store.reconcile()
                    result = store.get(segment['id'])
                    self.assertEqual(result["state"], "ready", result)
                with self.assertRaises(BackendError):
                    store.attach(segment["id"], {"instance_id": ids[2], "interface": "cable0"})
                # Nothing was automatically addressed, including IPv6 link-local.
                for ns in [segment["namespace"], *created_namespaces[:2]]:
                    addresses = json.loads(backend.run("-n", ns, "-j", "addr", "show"))
                    self.assertFalse([addr for link in addresses if link["ifname"] != "lo"
                                      for addr in link["addr_info"]], addresses)
                # Managed endpoint addressing; bridge itself stays unaddressed.
                endpoints = store.get(segment['id'])['endpoints']
                for index, endpoint in enumerate(endpoints, 1):
                    store.configure_addressing(segment['id'], endpoint['id'], {'mode': 'sas',
                        'addresses': [f'198.18.251.{index}/24', f'fd00:5a5:ca::{index}/64'],
                        'routes': [{'destination': '203.0.113.0/24', 'gateway': ''}]})
                    target = SimpleNamespace(id=endpoint['instance_id'], namespace=created_namespaces[index - 1])
                    store.prepare_instance(target)
                    store.prepare_instance(target)  # Includes numeric protocol/metric no-op.
                for destination in ("198.18.251.2", "fd00:5a5:ca::2"):
                    backend.run("netns", "exec", created_namespaces[0], "ping", "-c", "2", "-W", "2", destination)
                # A new controller adopts only its tagged ports and preserves addresses.
                store = L2Segments(Path(root) / "l2.json", targets.get, backend)
                store.reconcile()
                self.assertEqual(store.get(segment["id"])["state"], "ready")
                backend.run("netns", "exec", created_namespaces[0], "ping", "-c", "1", "-W", "2", "198.18.251.2")
                # Editing removes only owned addresses/routes; foreign addresses survive.
                backend.run('-n', created_namespaces[0], 'addr', 'add', '192.0.2.1/24', 'dev', 'cable0')
                store.configure_addressing(segment['id'], endpoints[0]['id'], {'mode': 'sas',
                    'addresses': ['198.18.251.10/24', 'fd00:5a5:ca::10/64'], 'routes': []})
                store.prepare_instance(SimpleNamespace(id=ids[0], namespace=created_namespaces[0]))
                values = backend.run('-n', created_namespaces[0], '-j', 'addr', 'show', 'dev', 'cable0')
                self.assertIn('192.0.2.1', values)
                self.assertNotIn('"198.18.251.1"', values)
                self.assertIn('198.18.251.10', values)
                self.assertNotIn('203.0.113.0/24', backend.run('-n', created_namespaces[0], '-j', 'route', 'show'))
                # Foreign untagged interface fails closed, but is not removed.
                backend.run("-n", segment["namespace"], "link", "add", "foreign0", "type", "dummy")
                store.reconcile()
                self.assertEqual(store.get(segment["id"])["state"], "error")
                bridge = next(i for i in backend.links(segment["namespace"]) if i["ifname"] == "br0")
                self.assertNotIn("UP", bridge["flags"])
                backend.run("-n", segment["namespace"], "link", "delete", "foreign0")
                store.reconcile()
                # Namespace disappearance (stop) preserves capacity and can be reattached.
                backend.run("netns", "delete", created_namespaces[0])
                targets[ids[0]]["state"] = "stopped"
                store.reconcile()
                self.assertEqual(len(store.get(segment["id"])["endpoints"]), 2)
                backend.run("netns", "add", created_namespaces[0])
                targets[ids[0]]["state"] = "running"
                store.reconcile()
                self.assertEqual(store.get(segment["id"])["state"], "ready")
            finally:
                if segment:
                    for endpoint in store.get(segment["id"])["endpoints"]:
                        store.detach(segment["id"], endpoint["id"])
                    store.delete(segment["id"])
                    self.assertFalse(backend.exists(segment["namespace"]))
                for ns in created_namespaces:
                    if backend.exists(ns):
                        backend.run("netns", "delete", ns)
