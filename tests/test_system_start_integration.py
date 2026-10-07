"""Opt-in 00-start kernel test. Only a disposable namespace with dummy links."""
import os
import subprocess
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace

from runner.system_start import SystemStart
from runner.namespace import NamespaceBackend


@unittest.skipUnless(os.environ.get('SAS_SYSTEM_START_INTEGRATION') == '1' and os.geteuid() == 0,
                     'requires explicit integration opt-in and root')
class SystemStartIntegrationTests(unittest.TestCase):
    def test_repair_dual_stack_and_idempotence_without_host_changes(self):
        instance_id = str(uuid.uuid4())
        ns = 'cz-' + instance_id[:8]
        run = NamespaceBackend._run
        with tempfile.TemporaryDirectory(prefix='sas-00-test-') as root:
            created = False
            try:
                run(['ip', 'netns', 'add', ns])
                created = True
                run(['ip', '-n', ns, 'link', 'set', 'lo', 'up'])
                for device, v4, v6 in [('di0', '10.100.0.2/30', 'fd00:1::2/126'),
                                       ('do0', '10.200.0.2/30', 'fd00:2::2/126')]:
                    run(['ip', '-n', ns, 'link', 'add', device, 'type', 'dummy'])
                    run(['ip', '-n', ns, 'link', 'set', device, 'up'])
                    run(['ip', '-n', ns, 'addr', 'add', v4, 'dev', device])
                    run(['ip', '-n', ns, '-6', 'addr', 'add', v6, 'dev', device, 'nodad'])
                ingress = SimpleNamespace(namespace=ns, topology='split-veth', guest_if='di0',
                    guest_ip='10.100.0.2', host_ip='10.100.0.1', guest_ip_v6='fd00:1::2', host_ip_v6='fd00:1::1')
                egress = SimpleNamespace(namespace=ns, topology='split-veth', guest_if='do0',
                    guest_ip='10.200.0.2', host_ip='10.200.0.1', guest_ip_v6='fd00:2::2', host_ip_v6='fd00:2::1')
                backend = SimpleNamespace(network_settings=None, allocation=lambda _: egress,
                    ingress_id=lambda _: 'ingress', ingress_allocation=lambda _: ingress,
                    test_drive_ingress_id=lambda _: 'drive-ingress', _run=run)
                instance = SimpleNamespace(id=instance_id, namespace=ns, state='running', desired_state='running',
                    source_ips=['192.0.2.10', '2001:db8::10'], network_ingress_driver='authorized-veth')
                manager = SimpleNamespace(state_dir=Path(root) / 'state', backend=backend,
                    lock=threading.RLock(), peek=lambda _: instance)
                service = SystemStart(manager)
                first = service.check(instance)
                self.assertGreater(first['applied_operations'], 0)
                self.assertEqual(service.check(instance)['applied_operations'], 0)
                run(['ip', '-n', ns, 'addr', 'delete', '10.100.0.2/30', 'dev', 'di0'])
                self.assertGreater(service.check(instance)['applied_operations'], 0)
                self.assertEqual(service.check(instance)['applied_operations'], 0)
                run(['ip', '-n', ns, '-6', 'addr', 'delete', 'fd00:1::2/126', 'dev', 'di0'])
                self.assertGreater(service.check(instance)['applied_operations'], 0)
                self.assertEqual(service.check(instance)['applied_operations'], 0)
                # Test Drive policy must use source-based table 101, not fwmark.
                backend.allocation = lambda key: ingress if key == 'drive-ingress' else egress
                drive = SimpleNamespace(id=instance_id, namespace=ns, state='running')
                drive_service = SystemStart(manager, test_drive=True)
                self.assertGreater(drive_service.check(drive)['applied_operations'], 0)
                self.assertEqual(drive_service.check(drive)['applied_operations'], 0)
                service.configure(instance_id, False)
                run(['ip', '-n', ns, 'addr', 'delete', '10.100.0.2/30', 'dev', 'di0'])
                self.assertEqual(service.check(instance)['state'], 'disabled')
                service.configure(instance_id, True)
                self.assertGreater(service.check(instance)['applied_operations'], 0)
                self.assertEqual(service.check(instance)['applied_operations'], 0)
            finally:
                if created:
                    run(['ip', 'netns', 'delete', ns])
