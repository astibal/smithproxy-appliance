import concurrent.futures
import io
import json
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from runner.l2_segments import L2Segments, LinuxL2
from runner.systemd import BackendError
from runner.app import handler_factory
from sas_client.cli import build_parser, dispatch


class L2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.targets = {}
        self.backend = Mock(spec=LinuxL2)
        self.backend.namespace.side_effect = LinuxL2.namespace
        self.path = Path(self.tmp.name) / "l2.json"
        self.store = L2Segments(self.path, self.targets.get, self.backend)

    def target(self, state="running"):
        value = str(uuid.uuid4())
        self.targets[value] = {"state": state, "namespace": "cz-" + value[:8]}
        return {"instance_id": value, "interface": "cable0"}

    def segment(self, kind="virtual-cable"):
        return self.store.create({"name": "lab", "kind": kind})["id"]

    def test_cable_capacity_and_idempotent_attach(self):
        segment = self.segment()
        payload = self.target()
        first = self.store.attach(segment, payload)
        self.store.attach(segment, payload)
        self.store.attach(segment, self.target())
        with self.assertRaisesRegex(BackendError, "two reserved"):
            self.store.attach(segment, self.target())
        self.assertEqual(len(self.store.get(segment)["endpoints"]), 2)
        with self.assertRaises(BackendError):
            self.store.attach(self.segment(), payload)
        self.assertEqual(first["endpoints"][0]["state"], "connected")

    def test_stopped_reservation_survives_restart_deleted_releases(self):
        segment, payload = self.segment(), self.target()
        self.store.attach(segment, payload)
        self.targets[payload["instance_id"]]["state"] = "stopped"
        store = L2Segments(self.path, self.targets.get, self.backend)
        store.reconcile()
        self.assertEqual(store.get(segment)["endpoints"][0]["state"], "reserved")
        self.targets[payload["instance_id"]]["state"] = "running"
        store.reconcile()
        self.assertEqual(store.get(segment)["endpoints"][0]["state"], "connected")
        del self.targets[payload["instance_id"]]
        store.reconcile()
        self.assertEqual(store.get(segment)["endpoints"], [])

    def test_switch_accepts_more_than_two(self):
        segment = self.segment("virtual-switch")
        for _ in range(4):
            self.store.attach(segment, self.target())
        self.assertEqual(len(self.store.get(segment)["endpoints"]), 4)

    def test_parallel_reservations_cannot_overfill(self):
        segment = self.segment()
        payloads = [self.target() for _ in range(8)]
        def reserve(payload):
            try:
                self.store.attach(segment, payload)
                return True
            except BackendError:
                return False
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertEqual(sum(pool.map(reserve, payloads)), 2)

    def test_failed_kernel_operation_retains_reservation(self):
        segment = self.segment()
        self.backend.attach.side_effect = BackendError("already exists")
        with self.assertRaisesRegex(BackendError, "reservation retained"):
            self.store.attach(segment, self.target())
        ep = self.store.get(segment)["endpoints"][0]
        self.assertEqual(ep["state"], "error")
        self.backend.detach.side_effect = BackendError("cannot detach")
        with self.assertRaises(BackendError):
            self.store.detach(segment, ep["id"])
        self.assertEqual(len(self.store.get(segment)["endpoints"]), 1)

    def test_slow_kernel_operation_does_not_block_status(self):
        segment = self.segment()
        entered, release = threading.Event(), threading.Event()
        def slow(*args):
            entered.set()
            release.wait(3)
        self.backend.attach.side_effect = slow
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            task = pool.submit(self.store.attach, segment, self.target())
            try:
                self.assertTrue(entered.wait(2))
                status = pool.submit(self.store.list).result(timeout=1)
                self.assertEqual(len(status[0]["endpoints"]), 1)
            finally:
                release.set()
            task.result()

    def test_rejects_unmanaged_target_invalid_port_and_tap(self):
        segment = self.segment()
        for payload in ({"instance_id": str(uuid.uuid4()), "interface": "x"},
                        {**self.target(), "interface": "../lo"},
                        {**self.target(), "type": "tap"}):
            with self.assertRaises(BackendError):
                self.store.attach(segment, payload)

    def test_unreadable_target_retains_reservation(self):
        segment = self.segment()
        self.store.attach(segment, self.target())
        self.store.resolve = Mock(side_effect=BackendError("unreadable state"))
        self.store.reconcile()
        self.assertEqual(len(self.store.get(segment)["endpoints"]), 1)
        self.assertEqual(self.store.get(segment)["endpoints"][0]["state"], "error")

    def test_cli_routes_mutations_to_async_api(self):
        client = Mock()
        client.post.return_value = {"task_id": "pending"}
        dispatch(client, build_parser().parse_args(["l2", "create", "virtual-cable", "lab"]))
        client.post.assert_called_once_with("/v1/l2-segments", {"kind": "virtual-cable", "name": "lab"})


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.backend = LinuxL2()
        self.segment = {"id": str(uuid.uuid4()), "endpoints": []}
        self.backend.run = Mock(return_value="")
        self.backend.exists = Mock(return_value=True)
        self.bridge = {"ifname": "br0", "ifalias": LinuxL2.tag(self.segment)}

    def test_foreign_unaliased_port_disables_bridge(self):
        self.backend.links = Mock(return_value=[self.bridge, {"ifname": "foreign0"}])
        with self.assertRaisesRegex(BackendError, "foreign interface"):
            self.backend.bridge(self.segment)
        self.assertEqual(self.backend.run.call_args.args[-1], "down")

    def test_does_not_adopt_foreign_bridge(self):
        self.backend.links = Mock(return_value=[{"ifname": "br0"}])
        with self.assertRaises(BackendError):
            self.backend.bridge(self.segment)
        self.backend.run.assert_not_called()

    def test_does_not_overwrite_foreign_target(self):
        ep = {"id": str(uuid.uuid4()), "interface": "di0"}
        self.backend.links = Mock(side_effect=[[], [{"ifname": "di0"}]])
        with self.assertRaises(BackendError):
            self.backend.attach(self.segment, ep, "cz-example")
        self.backend.run.assert_not_called()

    def test_new_bridge_has_no_address_routes_or_nat(self):
        self.backend.exists.return_value = False
        self.backend.links = Mock(return_value=[self.bridge])
        self.backend.bridge(self.segment)
        commands = [call.args for call in self.backend.run.call_args_list]
        self.assertTrue(any("addrgenmode" in cmd and "none" in cmd for cmd in commands))
        for cmd in commands:
            self.assertFalse(set(cmd) & {"address", "addr", "route", "rule", "iptables", "nft"})

    def test_nonempty_segment_cannot_be_deleted(self):
        self.segment["endpoints"] = [{"id": str(uuid.uuid4())}]
        with self.assertRaises(BackendError):
            self.backend.delete(self.segment)
        self.backend.run.assert_not_called()


class APITests(unittest.TestCase):
    def handler(self, method, path, payload=None, authenticated=True):
        self.store, self.tasks = Mock(), Mock()
        self.store.inventory.return_value = {'entries': [], 'tree': []}
        self.tasks.submit.return_value = (object(), True)
        self.tasks.view.return_value = {"task_id": "queued"}
        self.manager = Mock()
        cls = handler_factory(self.manager, "test-token", tasks=self.tasks, l2_segments=self.store)
        handler = object.__new__(cls)
        body = json.dumps(payload).encode() if payload is not None else b""
        handler.headers = {"Content-Length": str(len(body))}
        if authenticated:
            handler.headers["Authorization"] = "Bearer test-token"
        handler.path, handler.rfile = path, io.BytesIO(body)
        handler._json = Mock()
        getattr(handler, "do_" + method)()
        return handler

    def test_create_is_queued_not_executed_in_request(self):
        payload = {"kind": "virtual-cable", "name": "lab"}
        handler = self.handler("POST", "/v1/l2-segments", payload)
        handler._json.assert_called_once_with(202, {"task_id": "queued"})
        self.store.create.assert_not_called()
        self.tasks.submit.call_args.args[-1]()
        self.store.create.assert_called_once_with(payload)

    def test_all_endpoints_require_authentication(self):
        for method in ("GET", "POST", "DELETE"):
            handler = self.handler(method, "/v1/l2-segments", {}, authenticated=False)
            self.assertEqual(handler._json.call_args.args[0], 401)
            self.tasks.submit.assert_not_called()

    def test_attach_requests_per_instance_check_after_attachment(self):
        payload = {'instance_id': str(uuid.uuid4()), 'interface': 'cable0'}
        segment = str(uuid.uuid4())
        handler = self.handler('POST', f'/v1/l2-segments/{segment}/endpoints', payload)
        handler._json.assert_called_once_with(202, {'task_id': 'queued'})
        self.manager.check_microservices.assert_not_called()
        events = []
        self.store.attach.side_effect = lambda *args: events.append('attach') or {'id': segment}
        self.manager.check_microservices.side_effect = lambda *args: events.append('check') or {'state': 'checked'}
        result = self.tasks.submit.call_args.args[-1]()
        self.assertEqual(['attach', 'check'], events)
        self.manager.check_microservices.assert_called_once_with(payload['instance_id'])
        self.assertEqual(segment, result['id'])

    def test_failed_attachment_does_not_request_check(self):
        payload = {'instance_id': str(uuid.uuid4()), 'interface': 'cable0'}
        self.handler('POST', f'/v1/l2-segments/{uuid.uuid4()}/endpoints', payload)
        self.store.attach.side_effect = BackendError('attachment failed')
        with self.assertRaises(BackendError):
            self.tasks.submit.call_args.args[-1]()
        self.manager.check_microservices.assert_not_called()

    def test_check_failure_propagates_to_task(self):
        payload = {'instance_id': str(uuid.uuid4()), 'interface': 'cable0'}
        self.handler('POST', f'/v1/l2-segments/{uuid.uuid4()}/endpoints', payload)
        self.store.attach.return_value = {'id': 'segment'}
        self.manager.check_microservices.side_effect = BackendError('00-start failed')
        with self.assertRaisesRegex(BackendError, '00-start failed'):
            self.tasks.submit.call_args.args[-1]()
        self.store.detach.assert_not_called()

    def test_delete_is_queued(self):
        segment_id = str(uuid.uuid4())
        handler = self.handler("DELETE", f"/v1/l2-segments/{segment_id}")
        self.assertEqual(handler._json.call_args.args[0], 202)
        self.store.delete.assert_not_called()
        self.tasks.submit.call_args.args[-1]()
        self.store.delete.assert_called_once_with(segment_id)

    def test_addressing_is_queued_and_preview_is_read_only(self):
        segment, endpoint, instance = [str(uuid.uuid4()) for _ in range(3)]
        payload = {'mode': 'sas', 'addresses': ['10.0.0.1/24'], 'routes': []}
        handler = self.handler('POST', f'/v1/l2-segments/{segment}/endpoints/{endpoint}/addressing', payload)
        self.assertEqual(handler._json.call_args.args[0], 202)
        self.store.configure_addressing.assert_not_called()
        self.store.get.return_value = {'endpoints': [{'id': endpoint, 'instance_id': instance}]}
        self.tasks.submit.call_args.args[-1]()
        self.store.configure_addressing.assert_called_once_with(segment, endpoint, payload)
        preview = self.handler('POST', '/v1/l2-segments/addressing/preview', {'addresses': ['10.0.0.1/24']})
        preview._json.assert_called_once_with(200, {'warnings': []})
        self.tasks.submit.assert_not_called()

    def test_addressing_routes_require_auth(self):
        for method, path in [('GET', '/v1/l2-segments/addressing'),
                             ('POST', '/v1/l2-segments/addressing/preview'),
                             ('POST', '/v1/l2-segments/x/endpoints/y/addressing')]:
            handler = self.handler(method, path, {}, authenticated=False)
            self.assertEqual(handler._json.call_args.args[0], 401)
            self.store.inventory.assert_not_called()
            self.tasks.submit.assert_not_called()


class ConsoleTests(unittest.TestCase):
    def test_page_renders_in_all_locales_and_requires_login(self):
        from console.app import create_app
        with tempfile.TemporaryDirectory() as root:
            admin_file = Path(root) / "admins.json"
            admin_file.write_text(json.dumps({"admins": [{"id": "test", "email": "admin@example.test"}]}))
            app = create_app({"TESTING": True, "SECRET_KEY": "test", "RUNNER_TOKEN": "test",
                              "ADMIN_FILE": str(admin_file)})
            client = app.test_client()
            self.assertEqual(client.get("/l2-segments").status_code, 302)
            segment = {"id": str(uuid.uuid4()), "name": "test cable", "kind": "virtual-cable",
                       "state": "ready", "namespace": "test", "endpoints": [], "error": ""}
            for locale, title in (("cs", "Volný konec"), ("en", "Free end"),
                                  ("fr", "Extrémité libre")):
                with client.session_transaction() as session:
                    session["admin_id"], session["locale"] = "test", locale
                def response(request, **kwargs):
                    value = {"segments": [segment]} if request.full_url.endswith("l2-segments") else {"instances": []}
                    stream = io.BytesIO(json.dumps(value).encode())
                    stream.__enter__ = lambda: stream
                    return stream
                with patch("console_shared.runner_client.urlopen", side_effect=response):
                    result = client.get("/l2-segments")
                    self.assertEqual(result.status_code, 200)
                    self.assertIn(title, result.text)
                    self.assertIn("Wiring", result.text)
                    self.assertIn("test cable", result.text)
