import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from werkzeug.security import generate_password_hash
from console_next.app import create_app as create_next_app


class AdminAccountsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name) / 'admins.json'
        path.write_text(json.dumps({'admins': [{'id': 'first', 'email': 'first@example.invalid',
            'password_hash': generate_password_hash('initial-password')}]}))
        self.app = create_next_app({'TESTING': True, 'SECRET_KEY': 'test-only', 'ADMIN_FILE': str(path)})
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session['admin_id'] = 'first'
            session['csrf_token'] = 'csrf'

    def mutate(self, method, path, value):
        return self.client.open(path, method=method, json=value, headers={'X-CSRF-Token': 'csrf'})

    def test_create_update_delete_and_no_hash_disclosure(self):
        result = self.mutate('POST', '/next-api/admins', {'email': 'second@example.invalid', 'password': 'second-password'})
        self.assertEqual(result.status_code, 201)
        self.assertNotIn('password_hash', result.json)
        id_ = result.json['id']
        listing = self.client.get('/next-api/admins')
        self.assertNotIn('password_hash', listing.get_data(as_text=True))
        self.assertNotIn('auth_version', listing.get_data(as_text=True))
        self.assertEqual(self.mutate('PUT', '/next-api/admins/'+id_, {'disabled': True}).status_code, 200)
        self.assertEqual(self.mutate('DELETE', '/next-api/admins/'+id_, {}).status_code, 200)

    def test_self_protection_and_csrf(self):
        self.assertEqual(self.mutate('DELETE', '/next-api/admins/first', {}).status_code, 409)
        self.assertEqual(self.mutate('PUT', '/next-api/admins/first', {'disabled': True}).status_code, 409)
        self.assertEqual(self.client.post('/next-api/admins', json={}).status_code, 400)
        self.assertEqual(self.mutate('POST', '/next-api/admins', {'email': 'bad', 'password': 'short'}).status_code, 400)

    def test_privileged_maintenance_preserves_catalogue_and_lock_owner(self):
        path = Path(self.app.config['ADMIN_FILE'])
        owner = path.stat()
        with patch('os.geteuid', return_value=0), patch('os.chown') as chown, patch('os.fchown') as fchown:
            result = self.mutate('POST', '/next-api/admins', {
                'email': 'maintenance@example.invalid', 'password': 'maintenance-password'})
        self.assertEqual(result.status_code, 201)
        chown.assert_called_once_with(path.with_suffix('.tmp'), owner.st_uid, owner.st_gid)
        self.assertEqual(fchown.call_args.args[1:], (owner.st_uid, owner.st_gid))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reset_revokes_existing_session(self):
        result = self.mutate('POST', '/next-api/admins', {'email': 'second@example.invalid', 'password': 'second-password'})
        second = self.app.test_client()
        self.assertEqual(second.post('/login', data={'email': 'second@example.invalid', 'password': 'second-password'}).status_code, 302)
        self.assertEqual(second.get('/next-api/admins').status_code, 200)
        self.mutate('PUT', '/next-api/admins/'+result.json['id'], {'password': 'changed-password'})
        self.assertEqual(second.get('/next-api/admins').status_code, 401)

    def test_preferences_rotates_csrf_and_retains_own_session(self):
        result = self.mutate('POST', '/next-api/preferences', {'current_password': 'initial-password', 'new_password': 'replacement-password', 'confirm_password': 'replacement-password'})
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(result.json['csrf'], 'csrf')
        self.assertEqual(self.client.get('/next-api/admins').status_code, 200)
