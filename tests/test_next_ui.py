import unittest
from flask import Flask, g
from console_next.api import register, OPERATIONS


class NextApiTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = 'test-only'
        self.calls = []
        self.authorized = True

        @self.app.before_request
        def admin():
            g.admin = {'email': 'test@example.invalid'} if self.authorized else None

        def api(*args):
            self.calls.append(args)
            return {'instances': [], 'task_id': 'queued'}

        register(self.app, api, api)
        self.client = self.app.test_client()

    def test_catalog_and_authentication(self):
        self.assertEqual(self.client.get('/next-api/catalog/instances').json, {'items': []})
        self.assertEqual(self.client.get('/next-api/catalog/unknown').status_code, 404)
        self.authorized = False
        self.assertEqual(self.client.get('/next-api/catalog/instances').status_code, 401)

    def test_actions_have_fixed_routes(self):
        response = self.client.post('/next-api/action', json={
            'resource': 'instances', 'action': 'stop', 'id': 'abc-123'})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.calls[-1][:2], ('DELETE', '/v1/instances/abc-123'))
        response = self.client.post('/next-api/action', json={
            'resource': 'instances', 'action': 'stop', 'id': '../settings'})
        self.assertEqual(response.status_code, 400)

    def test_unknown_action_not_forwarded(self):
        response = self.client.post('/next-api/action', json={
            'resource': 'http://other-host', 'action': 'create'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.calls, [])

    def test_all_declared_operations_have_exact_destinations(self):
        for (resource, action), (method, path, direct) in OPERATIONS.items():
            with self.subTest(resource=resource, action=action):
                response = self.client.post('/next-api/action', json={
                    'resource': resource, 'action': action, 'id': 'abc-123',
                    'payload': {'approved': True, 'endpoint_id': 'endpoint-123'}})
                self.assertEqual(response.status_code, 202)
                self.assertEqual(self.calls[-1][:2], (method, path.format(id='abc-123', endpoint='endpoint-123')))
                self.assertEqual(len(self.calls[-1]), 3 if direct else 5)

    def test_approval_identity_is_server_owned(self):
        self.client.post('/next-api/action', json={
            'resource': 'configs', 'action': 'commit',
            'payload': {'approved_by': 'spoofed'}})
        self.assertEqual(self.calls[-1][2]['approved_by'], 'test@example.invalid')

    def test_invalid_operations_and_missing_identifiers(self):
        for payload in [None, [], {'resource': [], 'action': {}},
                        {'resource': 'configs', 'action': 'delete'}]:
            self.assertEqual(self.client.post('/next-api/action', json=payload).status_code, 400)

    def test_detail_allowlist(self):
        self.assertEqual(self.client.get('/next-api/detail/tasks/abc/result').status_code, 200)
        self.assertEqual(self.calls[-1], ('GET', '/v1/tasks/abc/result'))
        self.assertEqual(self.client.get('/next-api/detail/settings/abc/private').status_code, 404)

    def test_locale_is_validated_and_persisted_without_runner_access(self):
        for locale in ('cs', 'en', 'fr'):
            self.assertEqual(self.client.post('/next-api/locale', json={'locale': locale}).status_code, 200)
            with self.client.session_transaction() as session:
                self.assertEqual(session['locale'], locale)
        self.assertEqual(self.client.post('/next-api/locale', json={'locale': 'xx'}).status_code, 400)
        self.assertEqual(self.calls, [])
        self.authorized = False
        self.assertEqual(self.client.post('/next-api/locale', json={'locale': 'en'}).status_code, 401)
