import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from runner.system_start import SystemStart
from runner.systemd import BackendError
from sas_client.cli import build_parser, dispatch

ID = '11111111-1111-4111-8111-111111111111'


class SystemStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = SimpleNamespace(id=ID, namespace='cz-11111111', state='running',
            desired_state='running', source_ips=['192.0.2.10'], network_ingress_driver='authorized-veth')
        self.backend = Mock()
        self.backend.network_settings = None
        self.backend.allocation.return_value = SimpleNamespace(namespace='cz-11111111',
            topology='split-veth', guest_if='do0', guest_ip='10.200.0.2', host_ip='10.200.0.1',
            guest_ip_v6='fd00:2::2', host_ip_v6='fd00:2::1')
        self.backend.ingress_allocation.return_value = SimpleNamespace(namespace='cz-11111111',
            topology='split-veth', guest_if='di0', guest_ip='10.100.0.2', host_ip='10.100.0.1',
            guest_ip_v6='fd00:1::2', host_ip_v6='fd00:1::1')
        self.manager = SimpleNamespace(state_dir=Path(self.tmp.name) / 'state',
            backend=self.backend, lock=threading.RLock(), peek=lambda _: self.instance)
        self.service = SystemStart(self.manager)

    def observe(self, command):
        plan = self.service.plan(self.instance)
        if 'addr' in command:
            return [{'ifname': device, 'addr_info': [
                {'local': a['address'].split('/')[0], 'prefixlen': int(a['address'].split('/')[1])}
                for a in plan['addresses'] if a['device'] == device]}
                for device in ['lo', 'di0', 'do0']]
        if 'rule' in command:
            return [{'priority': 100, 'fwmark': '0x1', 'table': 100}]
        version = 6 if '-6' in command else 4
        return [{'dst': ('default' if r['destination'] in {'default', '0.0.0.0/0', '::/0'}
                         else r['destination'].split('/')[0]),
                 'table': r['table'], 'gateway': r['gateway'], 'dev': r['device'], 'type': r['kind']}
                for r in plan['routes'] if r['version'] == version]

    def test_noop_when_desired_network_already_present(self):
        self.service.read = self.observe
        result = self.service.check(self.instance)
        self.assertEqual(result['applied_operations'], 0)
        self.backend._run.assert_not_called()

    def test_missing_address_repaired_without_touching_other_interfaces(self):
        def read(command):
            values = self.observe(command)
            if 'addr' in command:
                values[1]['addr_info'] = []
                values.append({'ifname': 'cable0', 'addr_info': [{'local': '198.18.0.1', 'prefixlen': 24}]})
            return values
        self.service.read = read
        self.assertEqual(self.service.check(self.instance)['applied_operations'], 2)
        for call in self.backend._run.call_args_list:
            self.assertIn('di0', call.args[0])
            self.assertNotIn('flush', call.args[0])

    def test_missing_interface_fails_before_mutations(self):
        self.service.read = lambda _: []
        with self.assertRaises(BackendError):
            self.service.check(self.instance)
        self.backend._run.assert_not_called()
        self.assertEqual(self.service.status(ID)['state'], 'failed')

    def test_rule_conflict_does_not_duplicate_or_overwrite(self):
        self.service.read = lambda cmd: ([{'priority': 100, 'table': 200, 'fwmark': '0x1'}]
                                        if 'rule' in cmd else self.observe(cmd))
        with self.assertRaisesRegex(BackendError, 'conflicting'):
            self.service.check(self.instance)
        self.backend._run.assert_not_called()

    def test_disabled_survives_controller_restart_and_does_no_work(self):
        self.service.configure(ID, False)
        service = SystemStart(self.manager)
        service.read = Mock(side_effect=AssertionError('must not inspect namespace'))
        self.assertEqual(service.check(self.instance)['state'], 'disabled')
        self.backend._run.assert_not_called()
        with self.assertRaises(BackendError):
            self.service.configure(ID, 'false')

    def test_wiring_is_checked_by_enabled_system_start_only(self):
        self.service.read = self.observe
        self.service.wiring = Mock()
        self.service.wiring.prepare_instance.return_value = []
        self.service.check(self.instance)
        self.service.wiring.prepare_instance.assert_called_once_with(self.instance)
        self.service.configure(ID, False)
        self.service.wiring.reset_mock()
        self.service.check(self.instance)
        self.service.wiring.prepare_instance.assert_not_called()

    def test_no_guessed_addresses_when_allocation_is_missing(self):
        self.backend.network_settings = Mock()
        self.backend.network_settings.allocations.return_value = {}
        with self.assertRaisesRegex(BackendError, 'refusing to guess'):
            self.service.check(self.instance)
        self.backend._run.assert_not_called()

    def test_cli_per_instance_request_and_toggle(self):
        client = Mock()
        client.get.return_value = {'instances': [{'id': ID, 'alias': 'lab'}]}
        client.post.return_value = {'task_id': 'queued'}
        dispatch(client, build_parser().parse_args(['instance', 'check-microservices', 'lab']))
        client.post.assert_called_with(f'/v1/instances/{ID}/microservices/check', {})
        dispatch(client, build_parser().parse_args(['instance', 'system-start', 'lab', 'disable']))
        client.post.assert_called_with(f'/v1/instances/{ID}/microservices/00/configure', {'enabled': False})

    def test_test_drive_uses_source_policy_101_not_tproxy(self):
        self.service.test_drive = True
        self.backend.test_drive_ingress_id.return_value = 'ingress'
        egress = self.backend.allocation.return_value
        ingress = self.backend.ingress_allocation.return_value
        self.backend.allocation.side_effect = lambda key: ingress if key == 'ingress' else egress
        del self.instance.network_ingress_driver
        del self.instance.source_ips
        plan = self.service.plan(self.instance)
        self.assertFalse(any(r['table'] == '100' for r in plan['routes']))
        rules = [r for r in plan['routes'] if r['table'] == '101']
        self.assertEqual({r['rule_source'] for r in rules}, {'10.100.0.2', 'fd00:1::2'})

    def test_passive_none_creates_no_addresses_or_routes(self):
        self.backend.allocation.return_value.topology = 'none'
        self.backend.ingress_allocation.return_value.topology = 'none'
        self.instance.network_ingress_driver = 'none'
        self.service.read = lambda _: [{'ifname': 'lo', 'addr_info': []}]
        result = self.service.check(self.instance)
        self.assertEqual(result['plan']['addresses'], [])
        self.assertEqual(result['plan']['routes'], [])
        self.backend._run.assert_not_called()
