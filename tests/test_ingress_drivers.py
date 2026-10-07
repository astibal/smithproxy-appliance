import re
import tempfile
import unittest
from pathlib import Path

from jinja2 import Template
from runner.network_profiles import NetworkProfileLibrary


class IngressDriversTest(unittest.TestCase):
    def test_authorized_and_legacy_are_equivalent(self):
        for driver in ('split-veth', 'authorized-veth'):
            profile = NetworkProfileLibrary.validate({
                'kind': 'ingress', 'name': 'Authorized', 'driver': driver,
                'require_authorization': False,
            })
            self.assertEqual('authorized-veth', profile['driver'])
            self.assertTrue(profile['require_authorization'])
            self.assertTrue(profile['implemented'])

    def test_unlimited_round_trip_is_transport(self):
        with tempfile.TemporaryDirectory() as directory:
            library = NetworkProfileLibrary(Path(directory) / 'profiles.json')
            profile = library.create({
                'kind': 'ingress', 'name': 'Transport', 'driver': 'unlimited-veth',
            })
            actual = library.get(profile['network_profile_id'])
            self.assertEqual('unlimited-veth', actual['driver'])
            self.assertEqual('transport', actual['namespace_role'])
            self.assertEqual('transport0', actual['interface_name'])
            self.assertFalse(actual['require_authorization'])
            self.assertTrue(actual['implemented'])
            self.assertEqual([], actual['start_parameters'])

    def test_ingress_editor_options_are_exactly_the_requested_modes(self):
        root = Path(__file__).resolve().parents[1] / 'console/templates'
        for name in ('network_profiles.html', 'network_profile_editor.html'):
            source = (root / name).read_text()
            select = re.search(r'<select name="driver">(.*?)</select>', source).group(1)
            for driver in ('authorized-veth', 'unlimited-veth', 'split-veth'):
                html = Template(select).render(profile={'driver': driver})
                options = re.findall(r'<option value="([^"]+)"[^>]*>(.*?)</option>', html)
                self.assertEqual([
                    ('authorized-veth', 'Authorized veth'),
                    ('unlimited-veth', 'Unlimited veth'),
                    ('none', 'no ingress'),
                ], options)

    def test_egress_editor_options_and_case(self):
        root = Path(__file__).resolve().parents[1] / 'console/templates'
        for name in ('network_profiles.html', 'network_profile_editor.html'):
            selects = re.findall(r'<select name="driver">(.*?)</select>', (root / name).read_text())
            html = Template(selects[1]).render(profile={'driver': 'none'})
            self.assertEqual([('veth-out', 'veth out'), ('none', 'no egress')],
                             re.findall(r'<option value="([^"]+)"[^>]*>(.*?)</option>', html))

    def test_disabled_sides_are_implemented(self):
        for kind in ('ingress', 'egress'):
            actual = NetworkProfileLibrary.validate({'name': 'Off', 'kind': kind, 'driver': 'none'})
            self.assertTrue(actual['implemented'])
            self.assertEqual('', actual['interface_name'])
