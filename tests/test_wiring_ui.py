"""Render the wiring view without starting services or changing networking."""
import sys
import unittest
from pathlib import Path

from flask import g, render_template, url_for

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'console'))
from app import create_app


class WiringViewTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app({'TESTING': True, 'SECRET_KEY': 'wiring-test'})

    def render(self, count, inventory=None):
        instance_id = '11111111-1111-4111-8111-111111111111'
        endpoints = [{'id': str(i), 'instance_id': instance_id,
                      'interface': f'cable{i}', 'type': 'veth',
                      'state': 'reserved', 'error': ''} for i in range(count)]
        with self.app.test_request_context('/network/wiring'):
            g.locale = 'cs'
            g.admin = None
            return render_template('l2_segments.html', error=None, inventory=inventory or {},
                instances=[{'id': instance_id, 'alias': '<script>bad</script>', 'state': 'stopped'}],
                segments=[{'id': instance_id, 'name': 'Lab cable', 'kind': 'virtual-cable',
                           'namespace': 'sas-l2-test', 'state': 'ready', 'error': '',
                           'endpoints': endpoints}])

    def test_canonical_route_and_legacy_route(self):
        with self.app.test_request_context():
            self.assertEqual('/network/wiring', url_for('l2_segments'))
        client = self.app.test_client()
        for path in ('/network/wiring', '/l2-segments'):
            self.assertEqual(302, client.get(path).status_code)  # Authentication required.

    def test_free_end_and_filter_with_escaped_alias(self):
        page = self.render(1)
        self.assertIn('data-wiring-filter', page)
        self.assertIn('wiring-empty', page)
        self.assertIn('&lt;script&gt;bad&lt;/script&gt;', page)
        self.assertNotIn('<script>bad</script>', page)
        self.assertIn('name="action" value="attach"', page)
        self.assertIn('name="action" value="detach"', page)

    def test_full_cable_has_no_attach_control(self):
        page = self.render(2)
        self.assertNotIn('name="action" value="attach"', page)
        self.assertNotIn('name="action" value="delete"', page)
        self.assertEqual(2, page.count('data-state="reserved"'))

    def test_empty_cable_can_be_deleted(self):
        self.assertIn('name="action" value="delete"', self.render(0))

    def test_console_with_mixed_runtime_profiles(self):
        profiles = [dict(profile_id=kind, name=kind, application=kind,
                         available=True, ttl_seconds=1800)
                    for kind in ('router', 'webfsd')]
        profiles.append(dict(profile_id='smith', name='Smith', available=True,
                             ttl_seconds=None, build_id='abcdef', config_name='Default',
                             network_drivers={'ingress': 'authorized-veth'}))
        for locale in ('cs', 'en', 'fr'):
            with self.app.test_request_context('/'):
                g.locale = locale
                g.admin = None
                page = render_template('console.html', runtime_profiles=profiles,
                    status={'build': {}, 'instances': {'running': 0}},
                    instances=[], sources=[], headless_endpoints=[], error=None)
                self.assertIn('value="router"', page)
                self.assertIn('value="webfsd"', page)
                self.assertIn('abcdef + Default', page)
                self.assertIn('data-ingress-driver="none"', page)

    def test_program_profile_forms(self):
        self.app.jinja_env.globals['wiring_choices'] = lambda: []
        for locale in ('cs', 'en', 'fr'):
            for application in ('router', 'webfsd', 'elf'):
                with self.app.test_request_context('/program-profiles'):
                    g.locale = locale
                    g.admin = None
                    page = render_template('program_profiles.html', profiles=[], profile=None,
                                           application=application, error=None)
                    self.assertNotIn('programs.pending', page)
                    self.assertIn('class="program-cards"', page)
                    self.assertNotIn('>+ Router</a>', page)
                    self.assertIn('class="program-card program-add" href="/program-storage"', page)
                    self.assertNotIn('name="build_id"', page)
                    self.assertEqual(application == 'webfsd', 'name="port"' in page)

    def test_address_editor_and_inventory_links(self):
        from runner.wiring import network_tree
        usage = {'prefix': '10.100.30.0/24', 'address': '10.100.30.1', 'version': 4,
                 'managed': True, 'instance_id': 'test', 'interface': 'cable0',
                 'segment_id': 'segment', 'segment_name': 'lab', 'endpoint_id': '0'}
        page = self.render(1, {'tree': network_tree([usage])})
        self.assertNotIn('10.0.0.0/8', page)
        self.assertIn('10.100.30.0/24', page)
        self.assertIn('href="#endpoint-0"', page)
        self.assertIn('data-wiring-address-form', page)
        self.assertIn('action="/network/wiring" class="stack" data-wiring-address-form', page)
        self.assertIn('name="routes"', page)
        self.assertIn('name="addresses"', page)
