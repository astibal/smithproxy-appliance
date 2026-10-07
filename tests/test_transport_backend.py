import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runner.namespace import NamespaceBackend
from runner.network_settings import NetworkSettings
from runner.systemd import BackendError


class TransportBackendTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = NetworkSettings(root / 'network.json', root / 'leases.json')
        self.backend = NamespaceBackend('/bin/true', self.settings)
        self.backend._live_subnets = lambda: set()
        self.commands = []
        self.backend._run = lambda command, **kw: self.commands.append(command)
        self.config = root / 'smithproxy.cfg'
        self.config.write_text('settings = {};')
        self.id = str(uuid.uuid4())
        self.subprocess = patch('runner.namespace.subprocess.run', return_value=SimpleNamespace(
            returncode=0, stdout='', stderr=''))
        self.calls = self.subprocess.start()
        self.addCleanup(self.subprocess.stop)

    def start(self, ingress, egress, **kwargs):
        return self.backend.start(self.id, self.config, 60, source_ip='192.0.2.10',
                                  ingress_driver=ingress, egress_driver=egress, **kwargs)

    def test_transport_is_outside_proxy_and_recovers_its_lease(self):
        self.start('unlimited-veth', 'none')
        proxy = self.backend.allocation(self.id)
        transport = self.backend.ingress_allocation(self.id)
        self.assertNotEqual(proxy.namespace, transport.namespace)
        creates = [c for c in self.commands if c[:3] == ['ip', 'link', 'add']]
        self.assertEqual(1, len(creates))
        self.assertIn('transport0', creates[0])
        self.assertEqual(transport.namespace, creates[0][-1])
        self.assertTrue(transport.host_if.startswith('czt'))
        self.assertFalse(any('route' in c and 'add' in c and proxy.namespace in c for c in self.commands))
        self.assertFalse(any('rule' in c and 'add' in c for c in self.commands))
        nft = '\n'.join(c.kwargs.get('input', '') for c in self.calls.call_args_list)
        self.assertNotIn('tproxy', nft)
        self.assertNotIn('drop', nft)
        self.assertNotIn('fwmark', nft)
        resumed = NamespaceBackend('/bin/true', self.settings)
        self.assertEqual(transport, resumed.ingress_allocation(self.id))
        self.assertEqual(proxy, resumed.allocation(self.id))
        self.assertEqual('none', proxy.topology)
        resumed._run = lambda command, **kw: self.commands.append(command)
        resumed.stop_debug = lambda _: None
        resumed.stop(resumed.unit_name(self.id))
        self.assertEqual({}, self.settings.allocations())
        self.assertIn(['ip', 'netns', 'delete', transport.namespace], self.commands)

    def test_no_ingress_no_egress_is_loopback_only(self):
        self.start('none', 'none')
        self.assertFalse(any(c[:3] == ['ip', 'link', 'add'] for c in self.commands))
        self.assertFalse(any('route' in c and 'add' in c for c in self.commands))
        self.assertEqual(1, sum(c[:3] == ['ip', 'netns', 'add'] for c in self.commands))

    def test_no_ingress_can_have_egress(self):
        self.start('none', 'veth-out')
        creates = [c for c in self.commands if c[:3] == ['ip', 'link', 'add']]
        self.assertEqual(1, len(creates))
        self.assertIn('do0', creates[0])
        self.assertFalse(any('di0' in c for c in creates))

    def test_transport_and_egress_have_distinct_namespaces(self):
        self.start('unlimited-veth', 'veth-out')
        creates = [c for c in self.commands if c[:3] == ['ip', 'link', 'add']]
        self.assertEqual(2, len(creates))
        self.assertEqual(2, len({c[-1] for c in creates}))
        self.assertTrue(any('-6' in c and 'default' in c for c in self.commands))

    def test_authorized_without_egress_has_no_do0_or_default_route(self):
        self.start('authorized-veth', 'none')
        creates = [c for c in self.commands if c[:3] == ['ip', 'link', 'add']]
        self.assertEqual(1, len(creates))
        self.assertIn('di0', creates[0])
        proxy = self.backend.allocation(self.id)
        self.assertEqual('none', proxy.topology)
        self.assertFalse(any(proxy.namespace in c and 'default' in c for c in self.commands))

    def test_failed_transport_rolls_back_and_releases_leases(self):
        def fail(command, **kwargs):
            self.commands.append(command)
            if command[:3] == ['ip', 'link', 'add']:
                raise BackendError('test failure')
        self.backend._run = fail
        with self.assertRaisesRegex(BackendError, 'test failure'):
            self.start('unlimited-veth', 'none')
        self.assertEqual({}, self.settings.allocations())

    def test_rootfs_launch_uses_proxy_not_transport_namespace(self):
        rootfs = Path(self.temp.name) / 'rootfs'
        (rootfs / 'usr/bin').mkdir(parents=True)
        (rootfs / 'usr/bin/smithproxy').write_bytes(b'fixture')
        self.start('unlimited-veth', 'none', rootfs_path=str(rootfs))
        launch = next(c for c in self.commands if c[0] == 'systemd-run')
        self.assertIn(f'--property=RootDirectory={rootfs}', launch)
        self.assertIn(f'--property=NetworkNamespacePath=/run/netns/{self.backend.allocation(self.id).namespace}', launch)
        self.assertIn(f'--property=BindPaths={self.config.parent}:/work', launch)
        self.assertNotIn(f'--property=NetworkNamespacePath=/run/netns/{self.backend.ingress_allocation(self.id).namespace}', launch)

    def test_routed_transport_does_not_add_nat(self):
        self.settings.update({**self.settings.get(), 'egress_mode': 'routed'})
        self.start('unlimited-veth', 'none')
        self.assertFalse(any('masquerade' in c.kwargs.get('input', '') for c in self.calls.call_args_list))
