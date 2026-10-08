import base64
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from werkzeug.security import generate_password_hash
from console_next.app import create_app


class StandaloneConsoleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        admins = Path(self.tmp.name) / 'admins.json'
        admins.write_text(json.dumps({'admins': [{'id': 'admin', 'email': 'a@example.invalid',
            'password_hash': generate_password_hash('test-password-only')}]}))
        self.calls = []
        self.result = {'content': 'example\n'}
        self.upstream = BytesIO(b'archive')
        self.upstream.headers = {'Content-Length': '7'}
        def api(*args):
            self.calls.append(args)
            return self.result
        transport = SimpleNamespace(api=api, enqueue=api, download=lambda path: self.upstream)
        with patch('console_next.app.client', return_value=transport):
            self.app = create_app({'TESTING': True, 'SECRET_KEY': 'test-only', 'ADMIN_FILE': str(admins)})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id'] = 'admin'
            session['csrf_token'] = 'csrf'

    def test_no_legacy_app_import(self):
        code = 'from console_next.app import create_app; import sys; assert "console.app" not in sys.modules; assert "app" not in sys.modules'
        subprocess.run([sys.executable, '-c', code], check=True)

    def test_routes_and_static_are_owned_by_new_console(self):
        self.assertEqual(self.app.config['SESSION_COOKIE_NAME'], 'sas_next_session')
        self.assertIn('console_next', self.app.static_folder)
        self.assertEqual(self.client.get('/').status_code, 200)
        for path in ['/binaries', '/program-profiles', '/runtime-profiles', '/preferences']:
            self.assertEqual(self.client.get(path).status_code, 404)
        for path in ['/static/next/workspace.js', '/static/vendor/xterm/xterm.js',
                     '/static/vendor/codemirror/config-editor.js']:
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/static/app.js').status_code, 404)

    def test_downloads_keep_content_and_safe_names(self):
        for path in ['/configs/cfg/download', '/instances/instance/config/download', '/cert-bundles/bundle/ca.pem']:
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.data, b'example\n')
                self.assertIn('attachment;', response.headers['Content-Disposition'])
        self.result = {'name': 'file with spaces.txt', 'content_base64': base64.b64encode(b'data').decode()}
        with self.client.get('/test-drives/drive/files/download?path=dir%2Ffile.txt') as response:
            self.assertEqual(response.data, b'data')
            self.assertIn('filename="file with spaces.txt"', response.headers['Content-Disposition'])
        self.assertEqual(self.calls[-1][1], '/v1/test-drives/drive/files?path=dir%2Ffile.txt')
        with self.client.get('/appliance-exports/export/download') as response:
            self.assertEqual(response.data, b'archive')
        self.assertTrue(self.upstream.closed)

    def test_diagnostics_allowlist_and_no_empty_csrf(self):
        self.assertEqual(self.client.get('/api/instances/i/diagnostics').status_code, 200)
        self.assertEqual(self.client.get('/api/instances/i/private').status_code, 404)
        with self.client.session_transaction() as session:
            session.pop('csrf_token')
        self.assertEqual(self.client.post('/next-api/action', json={}).status_code, 400)

    def test_unauthenticated_downloads_and_api(self):
        with self.client.session_transaction() as session:
            session.clear()
        self.assertEqual(self.client.get('/next-api/catalog/profiles').status_code, 401)
        self.assertEqual(self.client.get('/configs/cfg/download').status_code, 302)

    def test_expired_csrf_is_structured_and_never_reaches_runner(self):
        response = self.client.post('/next-api/action', json={
            'resource': 'instances', 'action': 'stop', 'id': 'test-only'},
            headers={'X-CSRF-Token': 'old-token'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json['error_code'], 'csrf')
        self.assertEqual(self.calls, [])
        session = self.client.get('/next-api/session').json
        self.assertEqual(session['csrf'], 'csrf')
        self.assertEqual(session['id'], 'admin')
        self.assertIn(b'data-admin-id="admin"', self.client.get('/').data)

    def test_http_errors_are_json_only_for_api_routes(self):
        response = self.client.get('/api/instances/i/unsupported')
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json['error_code'], 'http_error')
        self.assertEqual(self.client.get('/unknown-page').mimetype, 'text/html')
