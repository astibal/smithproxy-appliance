import json
import ipaddress
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from runner.l2_segments import L2Segments, LinuxL2
from runner.wiring import addressing, network_tree, overlaps, bindings
from runner.runtime_profiles import RuntimeProfileLibrary
from runner.systemd import BackendError
from sas_client.cli import build_parser, dispatch


class WiringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.instance = str(uuid.uuid4())
        self.target = SimpleNamespace(id=self.instance, namespace='cz-' + self.instance[:8])
        self.backend = Mock(spec=LinuxL2)
        self.backend.namespace.side_effect = LinuxL2.namespace
        self.backend.run.return_value = '[]'
        self.actual = set()
        self.backend.run.side_effect = self.kernel
        self.backend.links.return_value = [{'ifname': 'cable0'}]
        self.store = L2Segments(Path(self.temp.name) / 'l2.json',
            lambda _: {'state': 'running', 'namespace': self.target.namespace}, self.backend)
        self.segment = self.store.create({'kind': 'virtual-cable', 'name': 'test'})['id']
        result = self.store.attach(self.segment, {'instance_id': self.instance, 'interface': 'cable0'})
        self.endpoint = result['endpoints'][0]['id']

    def kernel(self, *args):
        if 'addr' in args:
            if '-j' in args:
                return json.dumps([{'addr_info': [{'local': str(ipaddress.ip_interface(a).ip),
                    'prefixlen': ipaddress.ip_interface(a).network.prefixlen} for a in self.actual]}])
            if 'replace' in args:
                self.actual.add(args[args.index('replace') + 1])
            if 'del' in args:
                self.actual.discard(args[args.index('del') + 1])
        return '[]'

    def configure(self, **kwargs):
        return self.store.configure_addressing(self.segment, self.endpoint,
            {'mode': 'sas', 'addresses': ['10.100.30.1/24', 'fd42:1::1/64'], **kwargs})

    def test_validation_and_canonicalization(self):
        self.assertEqual(addressing({'mode': 'sas', 'addresses': ['fd42:0001::1/64']})['addresses'], ['fd42:1::1/64'])
        for data in ({'mode': 'bad'}, {'mode': 'none', 'addresses': ['10.0.0.1/24']},
                     {'mode': 'sas', 'addresses': ['10.0.0.1']},
                     {'mode': 'sas', 'addresses': ['::%lo/64']},
                     {'mode': 'sas', 'routes': [{'destination': '::/0', 'gateway': '10.0.0.1'}]},
                     {'mode': 'sas', 'addresses': '10.0.0.1/24'}):
            with self.assertRaises(BackendError):
                addressing(data)

    def test_detach_and_reattach_preserve_port_config(self):
        self.configure()
        self.store.detach(self.segment, self.endpoint)
        inventory = self.store.inventory()['entries']
        self.assertEqual(len(inventory), 2)
        self.assertFalse(inventory[0]['connected'])
        new_segment = self.store.create({'kind': 'virtual-cable', 'name': 'new'})['id']
        result = self.store.attach(new_segment, {'instance_id': self.instance, 'interface': 'cable0'})
        self.assertEqual(result['endpoints'][0]['addressing']['desired']['mode'], 'sas')
        self.assertEqual(self.store.inventory()['entries'][0]['segment_name'], 'new')

    def test_guest_does_not_touch_kernel(self):
        self.configure(mode='guest')
        self.backend.run.reset_mock()
        self.store.prepare_instance(self.target)
        self.backend.run.assert_not_called()
        self.assertEqual(self.store.inventory()['entries'][0]['record_type'], 'declared')

    def test_apply_and_noop_and_owned_removal(self):
        self.configure()
        self.store.prepare_instance(self.target)
        commands = [call.args for call in self.backend.run.call_args_list]
        self.assertTrue(any('10.100.30.1/24' in cmd for cmd in commands))
        self.assertTrue(any('fd42:1::1/64' in cmd for cmd in commands))
        def read(*args):
            if 'addr' in args and '-j' in args:
                return json.dumps([{'addr_info': [{'local': '10.100.30.1', 'prefixlen': 24},
                    {'local': 'fd42:1::1', 'prefixlen': 64}, {'local': '192.0.2.10', 'prefixlen': 24}]}])
            return '[]'
        self.backend.run.side_effect = read
        self.backend.run.reset_mock()
        self.store.prepare_instance(self.target)
        self.assertFalse(any('replace' in c.args for c in self.backend.run.call_args_list))
        self.configure(mode='none', addresses=[])
        self.backend.run.reset_mock()
        self.store.prepare_instance(self.target)
        deletes = [c.args for c in self.backend.run.call_args_list if 'del' in c.args]
        self.assertEqual(len(deletes), 2)
        self.assertFalse(any('192.0.2.10/24' in cmd for cmd in deletes))

    def test_foreign_port_refused_and_failed_apply_retains_desired(self):
        self.configure()
        self.backend.owned.side_effect = BackendError('foreign port')
        with self.assertRaises(BackendError):
            self.store.prepare_instance(self.target)
        value = self.store.get(self.segment)['endpoints'][0]['addressing']
        self.assertEqual(value['state'], 'error')
        self.assertEqual(len(value['desired']['addresses']), 2)
        self.backend.run.assert_not_called()

    def test_tree_groups_are_not_occupied_and_ipv6_remains_exact(self):
        self.configure()
        entries = self.store.inventory()['entries']
        tree = network_tree(entries)
        self.assertEqual(tree[0]['prefix'], '10.100.30.0/24')
        self.assertFalse(tree[0]['summary'])
        self.assertEqual(tree[0]['children'], [])
        self.assertEqual(tree[1]['version'], 6)
        self.assertFalse(self.store.inventory()['discovery_enabled'])

    def test_tree_retains_branching_and_configured_parent(self):
        tree = network_tree([{'prefix': p} for p in ('10.1.1.0/24', '10.2.1.0/24')])
        self.assertEqual(tree[0]['prefix'], '10.0.0.0/8')
        self.assertEqual([c['prefix'] for c in tree[0]['children']], ['10.1.1.0/24', '10.2.1.0/24'])
        tree = network_tree([{'prefix': p} for p in ('10.1.0.0/16', '10.1.1.0/24')])
        self.assertEqual(tree[0]['prefix'], '10.1.0.0/16')
        self.assertEqual(len(tree[0]['usages']), 1)
        self.assertEqual(tree[0]['children'][0]['prefix'], '10.1.1.0/24')

    def test_tree_compacts_ipv6_and_keeps_multiple_usages(self):
        tree = network_tree([{'prefix': 'fd42:1::/64'}] * 2)
        self.assertEqual(tree[0]['prefix'], 'fd42:1::/64')
        self.assertEqual(len(tree[0]['usages']), 2)
        self.assertEqual(network_tree([]), [])

    def test_same_segment_subnet_is_normal_but_duplicate_is_warning(self):
        self.configure()
        entries = self.store.inventory()['entries']
        self.assertEqual(overlaps(entries, ['10.100.30.2/24'], self.segment), [])
        self.assertEqual(overlaps(entries, ['10.100.30.1/24'], self.segment)[0]['kind'], 'duplicate')
        self.assertEqual(overlaps(entries, ['10.100.0.1/16'])[0]['kind'], 'overlap')
        self.assertEqual(overlaps(entries, ['fd42:1::2/64'])[0]['kind'], 'overlap')
        self.assertEqual(overlaps(entries, ['10.100.30.1/24'], self.segment, self.endpoint), [])

    def test_invalid_edit_does_not_replace_saved_configuration(self):
        before = self.configure()
        with self.assertRaises(BackendError):
            self.configure(addresses=['nonsense'])
        after = self.store.get(self.segment)['endpoints'][0]['addressing']
        self.assertEqual(before['desired'], after['desired'])

    def test_cli_parity_for_inventory_and_configuration(self):
        client = Mock()
        dispatch(client, build_parser().parse_args(['l2', 'addressing']))
        client.get.assert_called_with('/v1/l2-segments/addressing')
        client.get.return_value = {'segments': [self.store.get(self.segment)]}
        client.post.return_value = {'task_id': 'test'}
        dispatch(client, build_parser().parse_args(['l2', 'configure-port', self.segment,
            self.endpoint, '--mode', 'sas', '--address', '10.0.0.1/24',
            '--route', '10.20.0.0/16 10.0.0.254']))
        client.post.assert_called_with(f'/v1/l2-segments/{self.segment}/endpoints/{self.endpoint}/addressing',
            {'mode': 'sas', 'addresses': ['10.0.0.1/24'],
             'routes': [{'destination': '10.20.0.0/16', 'gateway': '10.0.0.254'}]})

    def test_multi_binding_reservation_is_all_or_nothing(self):
        free = self.store.create({'kind': 'virtual-cable', 'name': 'free'})['id']
        occupied = self.segment
        self.store.reserve_instance(str(uuid.uuid4()), [{'segment_id': occupied, 'interface': 'cable0'}])
        new = str(uuid.uuid4())
        with self.assertRaises(BackendError):
            self.store.reserve_instance(new, [{'segment_id': free, 'interface': 'cable0'},
                                               {'segment_id': occupied, 'interface': 'cable1'}])
        self.assertEqual(self.store.get(free)['endpoints'], [])
        self.assertEqual(len(self.store.get(occupied)['endpoints']), 2)

    def test_reserve_and_release_only_target_instance(self):
        new = str(uuid.uuid4())
        self.store.reserve_instance(new, [{'segment_id': self.segment, 'interface': 'lab0',
            'addressing': {'mode': 'sas', 'addresses': ['10.1.0.1/24']}}])
        self.store.release_instance(new)
        self.assertEqual([ep['instance_id'] for ep in self.store.get(self.segment)['endpoints']], [self.instance])
        self.assertTrue(any(e['instance_id'] == new and not e['connected'] for e in self.store.inventory()['entries']))

    def test_profile_only_stores_links_and_update_omission_preserves_them(self):
        library = RuntimeProfileLibrary(Path(self.temp.name) / 'profiles.json')
        links = [{'segment_id': self.segment, 'interface': 'lab0'}]
        item = library.create('lab', 'build', 'config', wiring=links)
        updated = library.update(item['profile_id'], 'lab2', 'build', 'config')
        self.assertEqual(updated['wiring'], links)
        self.assertEqual(library.update(item['profile_id'], 'lab', 'build', 'config', wiring=[])['wiring'], [])
        with self.assertRaises(BackendError):
            library.create('bad', 'build', 'config', wiring=[{**links[0], 'addressing': {'mode':'none'}}])

    def test_invalid_bindings_are_rejected(self):
        for value in ({}, [{'segment_id': self.segment, 'interface':'di0'}],
                      [{'segment_id': '../bad', 'interface':'lab0'}],
                      [{'segment_id': self.segment, 'interface':'lab0'}] * 2):
            with self.assertRaises(BackendError):
                bindings(value)
