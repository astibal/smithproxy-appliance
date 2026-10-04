from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


CONSOLE_DIR = Path(__file__).resolve().parents[1] / "console"
sys.path.insert(0, str(CONSOLE_DIR))
from app import create_app  # noqa: E402


class ConsoleAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.admin_file = Path(self.temporary.name, "admins.json")
        self.admin_file.write_text(json.dumps({"admins": [{
            "id": "admin-1", "email": "admin@example.test",
            "password_hash": generate_password_hash("old-password-123"),
        }]}), encoding="utf-8")
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret-not-for-production",
            "ADMIN_FILE": str(self.admin_file),
        })
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def login(self):
        response = self.client.post("/login", data={
            "email": "admin@example.test", "password": "old-password-123",
        })
        self.assertEqual(302, response.status_code)

    def csrf(self):
        with self.client.session_transaction() as current:
            return current["csrf_token"]

    def test_language_switch_is_available_before_login_and_survives_login(self):
        login = self.client.get("/login")
        self.assertIn(b'action="/language/en"', login.data)
        response = self.client.post("/language/fr", data={
            "csrf_token": self.csrf(), "next": "/login",
        })
        self.assertEqual(302, response.status_code)
        translated = self.client.get("/login")
        self.assertIn("Connexion".encode(), translated.data)
        self.login()
        with self.client.session_transaction() as current:
            self.assertEqual("fr", current["locale"])

    def test_password_change_revalidates_current_password_and_persists_hash(self):
        self.login()
        rejected = self.client.post("/preferences", data={
            "csrf_token": self.csrf(), "current_password": "wrong",
            "new_password": "new-password-456", "confirm_password": "new-password-456",
        })
        self.assertEqual(400, rejected.status_code)

        changed = self.client.post("/preferences", data={
            "csrf_token": self.csrf(), "current_password": "old-password-123",
            "new_password": "new-password-456", "confirm_password": "new-password-456",
        })
        self.assertEqual(302, changed.status_code)
        document = json.loads(self.admin_file.read_text(encoding="utf-8"))
        admin = document["admins"][0]
        self.assertTrue(check_password_hash(admin["password_hash"], "new-password-456"))
        self.assertIn("password_changed_at", admin)

        self.client.post("/logout", data={"csrf_token": self.csrf()})
        old = self.client.post("/login", data={
            "email": "admin@example.test", "password": "old-password-123",
        })
        self.assertEqual(200, old.status_code)
        new = self.client.post("/login", data={
            "email": "admin@example.test", "password": "new-password-456",
        })
        self.assertEqual(302, new.status_code)


if __name__ == "__main__":
    unittest.main()
