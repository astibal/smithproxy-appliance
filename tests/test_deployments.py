import json
import tempfile
import unittest
import threading
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from runner.app import Manager
from runner.deployments import acquire_runner_lock, atomic_json
from runner.namespace import NamespaceBackend
from runner.systemd import BackendError, UnitStatus
from runner.config import ConfigError
from runner.task_queue import TaskQueue
from runner.network_settings import NetworkSettings
from test_runner import FakeBackend


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.template = self.root / "source.cfg"
        self.template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; };')
        self.backend = FakeBackend()
        self.manager = self.new_manager("boot-one")

    def tearDown(self):
        self.temp.cleanup()

    def new_manager(self, boot):
        manager = Manager(self.root / "state", self.root / "instances", self.template, self.backend)
        manager.boot_id = boot
        return manager

    def spawn(self, runtime=300):
        return self.manager.create({"source_ip": "192.0.2.4", "user_id": "test",
                                    "runtime_seconds": runtime, "config_mode": "rw",
                                    "parameters": {"socks_port": 1080},
                                    "runtime_profile_id": "12345678-1234-1234-1234-123456789abc"})

    def test_restart_adopts_without_spawning_or_changing_deadline(self):
        item = self.spawn()
        with patch.object(self.backend, "start", side_effect=AssertionError("duplicate spawn")):
            restored = self.new_manager("boot-one").get(item.id)
        self.assertEqual("running", restored.state)
        self.assertEqual(item.deadline, restored.deadline)

    def test_boot_restores_exact_config_work_and_attached_sources(self):
        item = self.spawn()
        self.manager.attach_source(item.id, "2001:db8::4")
        work = self.root / "instances" / item.id
        (work / "smithproxy.cfg").write_text("locally edited config")
        (work / "private-key").write_text("keep me")
        self.template.write_text("do not use this changed profile")
        self.backend.units.clear()
        with patch.object(self.backend, "native_save", side_effect=AssertionError("must not normalize")):
            restored = self.new_manager("boot-two").get(item.id)
        self.assertEqual("starting", restored.state)
        self.assertEqual(item.id, restored.id)
        self.assertEqual(item.deadline, restored.deadline)
        self.assertEqual("locally edited config", (work / "smithproxy.cfg").read_text())
        self.assertEqual("keep me", (work / "private-key").read_text())
        self.assertEqual("2001:db8::4", self.backend.attached_source[1])
        self.assertTrue(self.backend.last_network["preserve_allocations_on_failure"])

    def test_stopped_does_not_resurrect(self):
        item = self.spawn()
        self.manager.stop(item.id)
        self.backend.units.clear()
        with patch.object(self.backend, "start", side_effect=AssertionError("resurrection")):
            self.assertEqual("stopped", self.new_manager("boot-two").get(item.id).state)

    def test_expired_offline_does_not_resurrect(self):
        item = self.spawn()
        item.deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        self.manager._save(item)
        self.backend.units.clear()
        with patch.object(self.backend, "start", side_effect=AssertionError("expired spawn")):
            restored = self.new_manager("boot-two").get(item.id)
        self.assertEqual("expired", restored.state)
        self.assertEqual("stopped", restored.desired_state)

    def test_unlimited_boot_restore(self):
        item = self.spawn(0)
        self.backend.units.clear()
        restored = self.new_manager("boot-two").get(item.id)
        self.assertEqual("", restored.deadline)
        self.assertEqual(0, self.backend.last_network["hard_runtime_seconds"])

    def test_failed_restore_keeps_files_and_backs_off(self):
        item = self.spawn()
        self.backend.units.clear()
        manager = self.new_manager("boot-two")
        with patch.object(self.backend, "start", side_effect=BackendError("network not ready")) as start:
            restored = manager.get(item.id)
            manager.get(item.id)
        self.assertEqual(1, start.call_count)
        self.assertEqual("recovering", restored.state)
        self.assertEqual("running", restored.desired_state)
        self.assertTrue((self.root / "instances" / item.id / "smithproxy.cfg").exists())
        self.assertEqual([], manager.cleanup_stopped(datetime.now(timezone.utc) + timedelta(days=1)))
        with self.assertRaises(ConfigError):
            manager.delete(item.id)
        with self.assertRaises(ConfigError):
            manager.create({"source_ip": "192.0.2.4", "user_id": "duplicate", "runtime_seconds": 60})

    def test_recovery_retries_successfully_after_dependency_returns(self):
        item = self.spawn()
        self.backend.units.clear()
        manager = self.new_manager("boot-two")
        with patch.object(self.backend, "start", side_effect=BackendError("not ready")):
            failed = manager.get(item.id)
        failed.recovery_after = 0
        manager._save(failed)
        recovered = manager.get(item.id)
        self.assertEqual("starting", recovered.state)
        self.assertEqual(2, recovered.recovery_attempts)

    def test_crash_keeps_desired_state_and_files_until_stop(self):
        item = self.spawn()
        self.backend.units[item.unit] = "failed"
        current = self.manager.get(item.id)
        self.assertEqual("failed", current.state)
        self.assertEqual("running", current.desired_state)
        self.assertTrue((self.root / "instances" / item.id).is_dir())
        self.manager.stop(item.id)
        self.assertFalse((self.root / "instances" / item.id).exists())

    def test_failed_stop_does_not_remove_workspace(self):
        item = self.spawn()
        with patch.object(self.backend, "stop", side_effect=BackendError("systemd unavailable")):
            with self.assertRaises(BackendError):
                self.manager.stop(item.id)
        self.assertTrue((self.root / "instances" / item.id).is_dir())
        self.assertEqual("stopped", self.manager.peek(item.id).desired_state)

    def test_missing_workspace_is_not_regenerated(self):
        item = self.spawn()
        (self.root / "instances" / item.id / "smithproxy.cfg").unlink()
        self.backend.units.clear()
        with patch.object(self.backend, "start") as start:
            restored = self.new_manager("boot-two").get(item.id)
        start.assert_not_called()
        self.assertIn("refusing to regenerate", restored.result)

    def test_interrupted_spawn_restores_on_same_boot(self):
        item = self.spawn()
        item.deployment_pending = True
        self.manager._save(item)
        self.backend.units.clear()
        restored = self.new_manager("boot-one").get(item.id)
        self.assertEqual("starting", restored.state)
        self.assertFalse(restored.deployment_pending)

    def test_interrupted_stop_is_completed_not_adopted(self):
        item = self.spawn()
        item.desired_state = "stopped"
        self.manager._save(item)
        restored = self.new_manager("boot-one").get(item.id)
        self.assertEqual("stopped", restored.state)
        self.assertEqual("inactive", self.backend.units[item.unit])

    def test_manifest_private_and_not_in_instance_response(self):
        item = self.spawn()
        path = self.manager._deployment_path(item.id)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertIn("start_options", json.loads(path.read_text()))
        self.assertNotIn("start_options", asdict(item))
        self.manager.stop(item.id)
        self.manager.delete(item.id)
        self.assertFalse(path.exists())

    def test_spawn_intent_precedes_backend_side_effect(self):
        real_start = self.backend.start
        def start(identity, config, runtime, **options):
            self.assertEqual("running", self.manager._load(identity).desired_state)
            self.assertTrue(self.manager._deployment_path(identity).is_file())
            return real_start(identity, config, runtime, **options)
        with patch.object(self.backend, "start", side_effect=start):
            self.spawn()

    def test_single_runner_lock(self):
        lock = acquire_runner_lock(self.root / "state")
        try:
            with self.assertRaises(SystemExit):
                acquire_runner_lock(self.root / "state")
        finally:
            lock.close()
        acquire_runner_lock(self.root / "state").close()

    def test_atomic_write_preserves_old_record_on_failed_replace(self):
        path = self.root / "record.json"
        atomic_json(path, {"v": 1})
        with patch("runner.deployments.os.replace", side_effect=OSError("full disk")):
            with self.assertRaises(OSError):
                atomic_json(path, {"v": 2})
        self.assertEqual({"v": 1}, json.loads(path.read_text()))
        self.assertFalse(list(self.root.glob(".write-*")))

    def test_deadline_arms_new_timer_before_removing_old(self):
        backend = NamespaceBackend()
        identity = "12345678-1234-1234-1234-123456789abc"
        old = f"capture-zone-deadline-{identity}-" + "a" * 16
        commands = []
        with patch.object(backend, "status", return_value=UnitStatus("inactive", "dead", "", 0)), \
                patch.object(backend, "_run", side_effect=lambda cmd, **kw: commands.append(cmd)):
            name = backend.schedule_deadline(identity, "2030-01-01T12:00:00+00:00", old)
        self.assertEqual("systemd-run", commands[0][0])
        self.assertIn(backend.slice_name(identity), commands[0])
        self.assertEqual(["systemctl", "stop", old + ".timer"], commands[1])
        self.assertNotEqual(old, name)

    def test_systemd_bus_failure_is_not_a_missing_unit(self):
        backend = NamespaceBackend()
        from types import SimpleNamespace
        with patch("runner.namespace.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout="", stderr="Failed to connect to bus")):
            with self.assertRaises(BackendError):
                backend.status("test.service")
        with patch("runner.namespace.subprocess.run", return_value=SimpleNamespace(
                returncode=1, stdout="LoadState=not-found\n", stderr="")):
            self.assertEqual("inactive", backend.status("test.service").active_state)

    def test_failed_network_recovery_keeps_address_leases(self):
        settings = NetworkSettings(self.root / "network.json", self.root / "leases.json")
        backend = NamespaceBackend("/bin/true", settings)
        identity = "12345678-1234-1234-1234-123456789abc"
        config = self.root / "smithproxy.cfg"
        config.write_text("settings={};")
        with patch.object(backend, "_live_subnets", return_value=set()), \
                patch.object(backend, "_cleanup_network"), \
                patch.object(backend, "_run", side_effect=BackendError("link unavailable")):
            with self.assertRaises(BackendError):
                backend.start(identity, config, 60, source_ip="192.0.2.4",
                              preserve_allocations_on_failure=True)
        self.assertIn(identity, settings.allocations())
        self.assertIn(backend.ingress_id(identity), settings.allocations())

    def test_shutdown_rejects_new_tasks_and_cancels_queued_callbacks(self):
        queue = TaskQueue(self.root / "tasks.json", workers=1)
        entered, release = threading.Event(), threading.Event()
        def running():
            entered.set()
            release.wait(5)
        first, _ = queue.submit("test", "first", "first", "one", running)
        self.assertTrue(entered.wait(2))
        second, _ = queue.submit("test", "second", "second", "two", lambda: self.fail("cancelled task ran"))
        try:
            queue.stop_accepting()
            self.assertEqual("running", first.state)
            self.assertEqual("failed", second.state)
            with self.assertRaises(BackendError):
                queue.submit("test", "third", "third", "three", lambda: None)
        finally:
            release.set()
            queue.work.join()
