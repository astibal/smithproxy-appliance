"""Opt-in real Linux netns smoke test; no proxy, service or host firewall policy changes.

SAS_NETNS_INTEGRATION=1 python -m unittest discover -s tests -p test_transport_integration.py -v
"""
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

from runner.namespace import NamespaceBackend
from runner.network_settings import NetworkSettings


@unittest.skipUnless(os.environ.get('SAS_NETNS_INTEGRATION') == '1' and os.geteuid() == 0,
                     'requires explicit opt-in and root on the integration server')
class TransportIntegrationTests(unittest.TestCase):
    def test_real_transport_ipv4_ipv6_and_cleanup(self):
        with tempfile.TemporaryDirectory(prefix='sas-netns-test-') as directory:
            root = Path(directory)
            settings = NetworkSettings(root / 'settings.json', root / 'leases.json')
            settings.update({**settings.get(), 'egress_mode': 'routed',
                             'route_table_start': 180000, 'mark_start': 0x44000000})
            backend = NamespaceBackend(network_settings=settings)
            # Exercise actual networking, but don't launch a proxy or a service.
            backend._launch_proxy = lambda *args, **kwargs: 'network-only-test'
            instance_id = str(uuid.uuid4())
            config = root / 'unused.cfg'
            config.write_text('')
            try:
                backend.start(instance_id, config, 60, ingress_driver='unlimited-veth',
                              egress_driver='none')
                transport = backend.ingress_allocation(instance_id)
                proxy = backend.allocation(instance_id)
                def links(namespace):
                    output = subprocess.check_output(['ip', '-j', '-n', namespace, 'link'], text=True)
                    return {item['ifname'] for item in json.loads(output)}
                self.assertEqual({'lo'}, links(proxy.namespace))
                self.assertEqual({'lo', 'transport0'}, links(transport.namespace))
                for family, destination in (('-4', transport.host_ip), ('-6', transport.host_ip_v6)):
                    result = subprocess.run(['ip', 'netns', 'exec', transport.namespace,
                                             'ping', family, '-c', '3', '-W', '2', destination],
                                            capture_output=True, text=True, timeout=12)
                    self.assertEqual(0, result.returncode, result.stderr + result.stdout)
                resumed = NamespaceBackend(network_settings=settings)
                self.assertEqual(transport, resumed.ingress_allocation(instance_id))
                self.assertEqual(proxy, resumed.allocation(instance_id))
            finally:
                for key, link in ((backend.ingress_id(instance_id), backend.ingress_allocation(instance_id)),
                                  (instance_id, backend.allocation(instance_id))):
                    backend._cleanup_network(link)
                    settings.release(key)
                self.assertFalse(Path('/run/netns', f'czt-{instance_id[:8]}').exists())
                self.assertFalse(Path('/run/netns', f'cz-{instance_id[:8]}').exists())
