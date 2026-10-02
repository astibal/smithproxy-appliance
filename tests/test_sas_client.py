import json
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from websockets.exceptions import ConnectionClosedOK

from sas_client.api import APIError, RunnerClient
from sas_client.cli import _terminal_send, assignments, build_parser, duration, resolve, ttl_duration
from sas_client.config import ClientConfig, ConfigurationError, load_config


TOKEN = "a" * 64


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


class SasClientTests(unittest.TestCase):
    def client(self, token=TOKEN):
        return RunnerClient(ClientConfig(url="http://runner.test", token=token))

    def test_duration_and_placeholder_parsing(self):
        self.assertEqual(30, duration("30"))
        self.assertEqual(1800, duration("30m"))
        self.assertEqual(7200, duration("2h"))
        self.assertIsNone(ttl_duration("unlimited"))
        self.assertEqual({"HOST": "origin.test", "PORT": "443"}, assignments([
            "host=origin.test", "PORT=443",
        ]))
        with self.assertRaises(ValueError):
            assignments(["broken"])

    def test_resolve_accepts_exact_name_and_unique_prefix(self):
        values = [
            {"profile_id": "11111111-aaaa", "name": "stable"},
            {"profile_id": "22222222-bbbb", "name": "next"},
        ]
        self.assertEqual("11111111-aaaa", resolve(values, "stable", "profile_id")["profile_id"])
        self.assertEqual("22222222-bbbb", resolve(values, "2222", "profile_id")["profile_id"])
        with self.assertRaises(ValueError):
            resolve(values, "missing", "profile_id")

    def test_context_reads_protected_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            token = root / "token"
            token.write_text(TOKEN)
            token.chmod(0o600)
            config = root / "config.json"
            config.write_text(json.dumps({
                "current": "lab", "contexts": {"lab": {
                    "url": "https://runner.test", "token_file": str(token),
                    "ca_file": "~/ca.pem",
                }},
            }))
            with patch.dict("os.environ", {"HOME": str(root)}):
                loaded = load_config(path=config)
            self.assertEqual(TOKEN, loaded.token)
            self.assertEqual("lab", loaded.context)
            self.assertEqual("wss://runner.test:9081", loaded.ws_url)
            self.assertEqual(str(root / "ca.pem"), loaded.ca_file)
            token.chmod(0o644)
            with self.assertRaises(ConfigurationError):
                load_config(path=config)

    def test_http_transport_auth_errors_and_task_wait(self):
        client = self.client()
        calls = []

        def transport(request, **_kwargs):
            calls.append(request)
            self.assertEqual(f"Bearer {TOKEN}", request.get_header("Authorization"))
            if request.full_url.endswith("/v1/status"):
                return FakeResponse({"status": "ok", "api_version": "v1"})
            if request.full_url.endswith("/v1/task-actions"):
                body = json.loads(request.data)
                self.assertEqual("/v1/runtime-profiles", body["path"])
                return FakeResponse({"task_id": "task-1", "state": "pending"})
            if request.full_url.endswith("/v1/tasks/task-1/result"):
                return FakeResponse({"id": "result-1"})
            return FakeResponse({"task_id": "task-1", "state": "succeeded"})

        with patch("sas_client.api.urlopen", side_effect=transport):
            self.assertEqual("v1", client.get("/v1/status")["api_version"])
            queued = client.enqueue("POST", "/v1/runtime-profiles", {}, "Create", "profile-create")
            result = client.wait_task(queued["task_id"])
            self.assertEqual("result-1", result["completed_result"]["id"])
        self.assertGreaterEqual(len(calls), 4)

        error = HTTPError(
            "http://runner.test/v1/status", 401, "Unauthorized", {},
            io.BytesIO(b'{"error":"unauthorized"}'),
        )
        with patch("sas_client.api.urlopen", side_effect=error):
            with self.assertRaises(APIError) as raised:
                client.get("/v1/status")
        self.assertEqual(401, raised.exception.status)

    def test_parser_supports_profile_and_standalone_spawn(self):
        parser = build_parser()
        profile = parser.parse_args([
            "--wait", "instance", "spawn", "--profile", "stable",
            "--source-ip", "192.0.2.10", "--user", "alice",
        ])
        self.assertTrue(profile.wait)
        self.assertEqual("stable", profile.profile)
        standalone = parser.parse_args([
            "instance", "spawn", "--build", "abc", "--config-id", "cfg",
            "--source-ip", "192.0.2.11", "--user", "bob", "--ttl", "45m",
        ])
        self.assertEqual(2700, standalone.ttl)

    def test_terminal_send_treats_clean_peer_close_as_normal_completion(self):
        connection = Mock()
        connection.send.side_effect = ConnectionClosedOK(None, None)
        self.assertFalse(_terminal_send(connection, b"show status\r"))


if __name__ == "__main__":
    unittest.main()
