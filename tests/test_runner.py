import json
import gzip
import hashlib
import shutil
import tempfile
import unittest
import uuid
import subprocess
import threading
import time
from unittest.mock import patch, Mock
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from runner.app import Manager
from runner.builder import SmithproxyBuilder
from runner.config import ConfigError, render_template, validate_parameters
from runner.config_library import ConfigLibrary
from runner.config_previews import ConfigPreviewLibrary
from runner.namespace import NamespaceBackend, parse_unit_status, systemd_timespan_microseconds
from runner.network_settings import NetworkSettings
from runner.network_profiles import NetworkProfileLibrary
from runner.headless_endpoints import HeadlessEndpointLibrary
from runner.runtime_profiles import RuntimeProfileLibrary
from runner.cert_library import CertBundleLibrary
from runner.systemd import BackendError, UnitStatus
from runner.task_queue import TaskQueue
from runner.test_drive import TestDriveManager
from runner.firewall import FirewallManager


class FakeCliSocket:
    def __init__(self, config_path):
        self.config_path = Path(config_path)
        self.responses = [b"smithproxy# ", b"Config saved successfully\r\n"]
        self.sent = b""
        self.closed = False

    def settimeout(self, _timeout):
        return None

    def recv(self, _size):
        if self.responses:
            return self.responses.pop(0)
        raise TimeoutError

    def sendall(self, payload):
        self.sent += payload
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8") + "\n# native save\n",
            encoding="utf-8",
        )

    def close(self):
        self.closed = True


class FakeBackend:
    def __init__(self):
        self.units = {}
        self.orphans = {}

    @staticmethod
    def unit_name(instance_id):
        return f"test-{instance_id}.service"

    def start(self, instance_id, config_path, runtime_seconds, **_network):
        unit = f"test-{instance_id}.service"
        self.units[unit] = "active"
        self.last_network = _network
        return unit

    def stop(self, unit):
        self.units[unit] = "inactive"

    def restart(self, unit):
        if unit not in self.units:
            raise BackendError("unit is unknown")
        self.units[unit] = "active"
        self.restart_count = getattr(self, "restart_count", 0) + 1

    def extend_runtime(self, unit, total_seconds):
        if unit not in self.units:
            raise BackendError("unit is unknown")
        self.extended_runtime = (unit, total_seconds)

    def attach_source(self, instance_id, source_ip, profile, socks_port, http_port,
                      tls_port=50443, plaintext_port=50080):
        self.attached_source = (instance_id, source_ip, profile, socks_port, http_port)

    def status(self, unit):
        state = self.units.get(unit, "inactive")
        result = "exit-code" if state == "failed" else "success"
        return UnitStatus(state, "running" if state == "active" else "dead", result,
                          4242 if state == "active" else 0)

    def rss_bytes(self, pid):
        return 64 * 1024 if pid == 4242 else 0

    def native_save(self, _binary, config_path, _cli_port=50000):
        if getattr(self, "native_save_error", None):
            raise BackendError(self.native_save_error)
        self.native_save_calls = getattr(self, "native_save_calls", 0) + 1
        return config_path.read_text(encoding="utf-8")

    def open_cli(self, _instance_id, _cli_port):
        return self.cli_transport

    def open_gdb(self, instance_id, binary, address, port):
        class FakeGdbTransport:
            def __init__(self): self.closed = False
            def close(self): self.closed = True
        self.gdb_transport = FakeGdbTransport()
        self.gdb_target = (instance_id, binary, address, port)
        return self.gdb_transport

    def discover_units(self):
        return list(self.orphans.items())

    def start_debug(self, instance_id, pid, port=2345):
        return f"gdb-{instance_id}.service", "10.0.0.2", port

    def stop_debug(self, _instance_id):
        return None

    def capture_stacktrace(self, pid, _since=""):
        return f"trace for {pid}"

    def start_test_drive(self, drive_id, _binary, _config, _private_run, _resolver, _ttl,
                         _config_mode="ro"):
        unit = f"capture-zone-testdrive-{drive_id}.service"
        self.units[unit] = "active"
        egress = SimpleNamespace(
            namespace=f"cz-{drive_id[:8]}", guest_if="do0", guest_ip="10.0.0.2",
            host_if=f"czo{drive_id[:8]}", host_ip="10.0.0.1", subnet="10.0.0.0/30",
        )
        ingress = SimpleNamespace(
            namespace=f"cz-{drive_id[:8]}", guest_if="di0", guest_ip="10.0.0.6",
            host_if=f"czi{drive_id[:8]}", host_ip="10.0.0.5", subnet="10.0.0.4/30",
        )
        return unit, egress, ingress, str(uuid.uuid4())

    def stop_test_drive(self, _drive_id, unit, *_ingress):
        self.units[unit] = "inactive"

    def upgrade_test_drive(self, unit, namespace, binary, config, private_run,
                           resolver, ttl_seconds, config_mode="rw"):
        self.test_drive_upgrade = {
            "unit": unit, "namespace": namespace, "binary": str(binary),
            "config": str(config), "private_run": str(private_run),
            "resolver": str(resolver), "ttl_seconds": ttl_seconds,
            "config_mode": config_mode,
        }
        self.units[unit] = "active"

    def stop_test_drive_process(self, unit):
        self.units[unit] = "inactive"

    def extend_test_drive(self, unit, remaining_seconds):
        self.test_drive_extension = (unit, remaining_seconds)

    def discover_test_drives(self):
        return []

    def cleanup_test_drive_shells(self):
        return None

    def open_test_drive_cli(self, _namespace, _cli_port):
        return self.test_drive_cli


class RunnerTests(unittest.TestCase):
    def test_systemd_runtime_parser_accepts_raw_microseconds(self):
        self.assertEqual(1_861_000_000, systemd_timespan_microseconds("1861000000\n"))
        self.assertEqual(1_861_000_000, systemd_timespan_microseconds("31min 1s\n"))

    def test_test_drive_service_keeps_temp_and_captures_inside_workspace(self):
        root = Path(self.temp.name)
        workspace = root / "work"
        workspace.mkdir()
        config = workspace / "smithproxy.cfg"
        config.write_text("settings={};", encoding="utf-8")
        private_run = root / "run"
        private_run.mkdir(exist_ok=True)
        resolver = root / "resolv.conf"
        resolver.write_text("nameserver 1.1.1.1\n", encoding="utf-8")
        binary = root / "smithproxy"
        binary.write_bytes(b"binary")
        commands = []
        backend = object.__new__(NamespaceBackend)
        backend._clear_test_drive_runtime_override = lambda _unit: None
        backend._run = lambda command: commands.append(command)

        backend._start_test_drive_service(
            "capture-zone-testdrive-test.service", "cz-test", binary, config,
            private_run, resolver, 300, "rw",
        )

        command = commands[0]
        self.assertIn(f"--setenv=TMPDIR={workspace / 'tmp'}", command)
        self.assertIn(
            f"--property=BindPaths={workspace / 'captures'}:/var/smithproxy/data",
            command,
        )
        self.assertIn(
            "--property=CapabilityBoundingSet=CAP_NET_RAW CAP_DAC_OVERRIDE CAP_FOWNER",
            command,
        )
        self.assertIn("--property=PrivateTmp=yes", command)
        self.assertIn(f"--property=BindPaths={workspace}:{workspace}", command)
        self.assertIn(f"--property=BindPaths={workspace}:/work", command)
        self.assertIn(f"--property=ReadWritePaths={workspace} /work", command)
        self.assertIn(
            f"--property=BindReadOnlyPaths={binary.parent}:{binary.parent}", command,
        )
        self.assertIn("--property=NoNewPrivileges=yes", command)
        self.assertTrue((workspace / "tmp").is_dir())
        self.assertTrue((workspace / "captures").is_dir())

    def test_rootfs_execution_preserves_current_runtime_mountpoints(self):
        root = Path(self.temp.name)
        rootfs = root / "rootfs"
        (rootfs / "usr/bin").mkdir(parents=True)
        (rootfs / "usr/bin/smithproxy").write_bytes(b"binary")
        config = root / "instances" / "instance-id" / "smithproxy.cfg"
        config.parent.mkdir(parents=True)
        config.write_text("settings={};", encoding="utf-8")
        properties, executable = NamespaceBackend._rootfs_execution_properties(
            config, str(rootfs),
        )
        self.assertEqual("/usr/bin/smithproxy", executable)
        self.assertIn(f"--property=RootDirectory={rootfs}", properties)
        self.assertIn("--property=MountAPIVFS=yes", properties)
        self.assertIn("--property=WorkingDirectory=/work", properties)
        self.assertIn(
            f"--property=TemporaryFileSystem={config.parent.parent}", properties,
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.template = root / "template.cfg"
        self.template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; dir="{{RUNTIME_DIR}}"; };')
        self.backend = FakeBackend()
        self.manager = Manager(root / "state", root / "run", self.template, self.backend)

    def tearDown(self):
        self.temp.cleanup()

    def test_create_and_stop(self):
        item = self.manager.create({"runtime_seconds": 30, "source_ip": "198.51.100.10", "user_id": "test-user", "parameters": {"socks_port": 1080}})
        self.assertEqual("starting", item.state)
        managed_link = Path(self.temp.name, "run", "managed", item.id)
        self.assertTrue(managed_link.is_symlink())
        self.assertEqual(Path(self.temp.name, "run", item.id), managed_link.resolve())
        self.assertEqual("running", self.manager.get(item.id).state)
        config = Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text()
        self.assertIn('socks_port="1080"', config)
        self.manager.stop(item.id)
        self.assertFalse(Path(self.temp.name, "run", item.id).exists())
        self.assertFalse(managed_link.exists())
        stopped, snapshot = self.manager.config_content(item.id)
        self.assertEqual("stopped", stopped.state)
        self.assertIn('socks_port="1080"', snapshot)

    def test_spawn_wiring_reserved_before_start_and_failed_spawn_released(self):
        self.manager.l2_segments = Mock()
        links = [{'segment_id': str(uuid.uuid4()), 'interface': 'lab0'}]
        original = self.backend.start
        def start(instance_id, *args, **kwargs):
            self.manager.l2_segments.reserve_instance.assert_called_once_with(instance_id, links)
            return original(instance_id, *args, **kwargs)
        self.backend.start = start
        item = self.manager.create({'runtime_seconds': 30, 'source_ip': '198.51.100.10',
            'user_id': 'test', 'wiring': links, 'system_start_enabled': False, 'parameters': {'socks_port': 1080}})
        self.assertEqual(item.wiring, links)
        self.manager.stop(item.id)
        self.manager.l2_segments.reset_mock()
        self.backend.start = Mock(side_effect=BackendError('launch failed'))
        with self.assertRaises(BackendError):
            self.manager.create({'runtime_seconds': 30, 'source_ip': '198.51.100.10',
                'user_id': 'test', 'wiring': links, 'parameters': {'socks_port': 1080}})
        self.manager.l2_segments.release_instance.assert_called_once()
        failed_id = self.manager.l2_segments.release_instance.call_args.args[0]
        self.assertEqual(self.manager.peek(failed_id).desired_state, 'stopped')

    def test_proxy_launch_orders_wiring_then_system_start_then_program(self):
        backend = NamespaceBackend()
        events = []
        instance = SimpleNamespace(id=str(uuid.uuid4()))
        backend.system_start = Mock()
        backend.system_start.manager.peek.return_value = instance
        backend.system_start.check.side_effect = lambda *a, **k: events.append('00-start')
        backend.wiring = Mock()
        backend.wiring.prepare_instance.side_effect = lambda *a, **k: events.append('wiring')
        backend._instance_storage_properties = Mock(return_value=[])
        backend._run = lambda *_args: events.append('program')
        backend._launch_proxy(instance.id, SimpleNamespace(namespace='test'), self.template,
                              '/bin/true', '', 'rw', '', False, 0)
        self.assertEqual(events, ['wiring', '00-start', 'program'])
        backend.wiring.prepare_instance.side_effect = BackendError('cannot connect')
        events.clear()
        with self.assertRaises(BackendError):
            backend._launch_proxy(instance.id, SimpleNamespace(namespace='test'), self.template,
                                  '/bin/true', '', 'rw', '', False, 0)
        self.assertEqual(events, [])

    def test_interrupted_spawn_recovers_reservation_once_not_after_user_detach(self):
        self.manager.l2_segments = Mock()
        links = [{'segment_id': str(uuid.uuid4()), 'interface': 'lab0'}]
        item = self.manager.create({'runtime_seconds': 30, 'source_ip': '198.51.100.10',
            'user_id': 'test', 'wiring': links, 'parameters': {'socks_port': 1080}})
        path = self.manager._deployment_path(item.id)
        document = json.loads(path.read_text())
        document['wiring_reserved'] = False
        path.write_text(json.dumps(document))
        self.manager.l2_segments.reset_mock()
        self.manager._recover(item)
        self.manager.l2_segments.reserve_instance.assert_called_once_with(item.id, links)
        self.assertTrue(json.loads(path.read_text())['wiring_reserved'])
        self.manager.l2_segments.reset_mock()
        item.recovery_after = 0
        self.manager._recover(item)
        self.manager.l2_segments.reserve_instance.assert_not_called()

    def test_transport_instances_do_not_require_or_reserve_source_ip(self):
        payload = {"runtime_seconds": 30, "user_id": "test-user", "parameters": {"socks_port": 1080},
                   "network_ingress_driver": "unlimited-veth", "network_egress_driver": "none"}
        first = self.manager.create(payload)
        second = self.manager.create(payload)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual("", first.source_ip)
        self.assertEqual([], first.source_ips)
        self.assertEqual("unlimited-veth", self.backend.last_network["ingress_driver"])
        self.assertEqual("none", self.backend.last_network["egress_driver"])
        with self.assertRaisesRegex(ConfigError, "Authorized veth"):
            self.manager.attach_source(first.id, "192.0.2.10")
        stored = json.loads(self.manager._deployment_path(first.id).read_text())
        self.assertEqual("unlimited-veth", stored["start_options"]["ingress_driver"])
        self.assertEqual("none", stored["start_options"]["egress_driver"])

    def test_instance_snapshot_never_calls_lifecycle_backend(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "snapshot-user", "parameters": {"socks_port": 1080},
        })
        self.backend.status = lambda _unit: (_ for _ in ()).throw(
            AssertionError("snapshot must not query systemd")
        )

        snapshot = self.manager.snapshot()

        self.assertEqual([item.id], [current.id for current in snapshot])
        self.assertEqual("starting", self.manager.peek(item.id).state)

    def test_test_drive_is_disposable_and_reports_lab_coordinates(self):
        root = Path(self.temp.name)
        binary = root / "smithproxy"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        assets = root / "assets"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(root / "td-state", root / "td-run", self.backend)
        item = manager.create("a" * 40 + "-release", binary, self.template, assets, 300)
        current = manager.get(item.id)
        self.assertEqual("running", current.state)
        drive_link = Path(self.temp.name, "td-run", "test-drive", item.id)
        self.assertTrue(drive_link.is_symlink())
        self.assertEqual(Path(self.temp.name, "td-run", item.id), drive_link.resolve())
        self.assertEqual("ro", current.config_mode)
        self.assertEqual("di0", current.ingress_interface)
        self.assertEqual("10.0.0.6", current.ingress_ip)
        self.assertEqual("10.0.0.4/30", current.ingress_subnet)
        self.assertEqual("do0", current.egress_interface)
        self.assertTrue(Path(current.workspace, "smithproxy.cfg").is_file())
        manager.destroy(item.id)
        self.assertFalse(Path(self.temp.name, "td-run", item.id).exists())
        self.assertFalse(drive_link.exists())
        self.assertFalse(Path(self.temp.name, "td-state", f"{item.id}.json").exists())

    def test_test_drive_config_mode_can_be_changed_in_same_lab(self):
        root = Path(self.temp.name)
        binary = root / "smithproxy-mode"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        assets = root / "assets-mode"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(root / "td-mode-state", root / "td-mode-run", self.backend)
        item = manager.create("mode-release", binary, self.template, assets, 300, "ro")

        changed = manager.set_config_mode(item.id, "rw")

        self.assertEqual("rw", changed.config_mode)
        self.assertEqual(item.namespace, self.backend.test_drive_upgrade["namespace"])
        self.assertEqual("rw", self.backend.test_drive_upgrade["config_mode"])
        self.assertEqual(item.deadline, changed.deadline)

    def test_test_drive_rw_config_can_be_saved_for_library_preview(self):
        root = Path(self.temp.name)
        binary = root / "smithproxy-save"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        assets = root / "assets-save"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(root / "td-save-state", root / "td-save-run", self.backend)
        item = manager.create("save-release", binary, self.template, assets, 300, "rw")
        self.backend.test_drive_cli = FakeCliSocket(item.config_path)

        drive, before, after, path = manager.save_live_config(item.id)

        self.assertEqual(item.id, drive.id)
        self.assertEqual(Path(item.config_path), path)
        self.assertNotIn("# native save", before)
        self.assertIn("# native save", after)
        self.assertEqual(b"enable\r\nsave config\r\n", self.backend.test_drive_cli.sent)
        self.assertTrue(self.backend.test_drive_cli.closed)

    def test_test_drive_dirty_upgrade_preserves_workspace_and_deadline(self):
        root = Path(self.temp.name)
        original = root / "smithproxy-old"
        replacement = root / "smithproxy-new"
        original.write_bytes(b"old")
        replacement.write_bytes(b"new")
        original.chmod(0o700)
        replacement.chmod(0o700)
        assets = root / "assets-upgrade"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(root / "td-up-state", root / "td-up-run", self.backend)
        item = manager.create("old-release", original, self.template, assets, 300)
        config = Path(item.config_path)
        config.write_text(
            config.read_text()
            + '\ncaptures = { local = { dir = "/srv/operator-captures"; }; };'
            + '\ncerts_path = "/tmp/capture-zone-runtime/test-drives/old-id/assets/certs/default/";'
            + "\n# dirty local edit\n"
        )
        original_deadline = item.deadline

        upgraded = manager.upgrade(item.id, "new-release", replacement)

        self.assertEqual("new-release", upgraded.build_id)
        self.assertEqual("old-release", upgraded.previous_build_id)
        self.assertEqual(original_deadline, upgraded.deadline)
        self.assertEqual(1, upgraded.upgrade_count)
        self.assertIn("# dirty local edit", config.read_text())
        self.assertIn(
            f'{Path(item.workspace).parent / "assets"}/certs/default/',
            config.read_text(),
        )
        self.assertNotIn("/tmp/capture-zone-runtime/test-drives/old-id", config.read_text())
        self.assertIn('/srv/operator-captures', config.read_text())
        self.assertEqual(item.namespace, self.backend.test_drive_upgrade["namespace"])
        self.assertEqual(str(config), self.backend.test_drive_upgrade["config"])
        self.assertGreaterEqual(self.backend.test_drive_upgrade["ttl_seconds"], 295)

    def test_test_drive_expiry_retains_config_then_extend_and_restart(self):
        root = Path(self.temp.name)
        binary = root / "smithproxy-recovery"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        assets = root / "assets-recovery"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(
            root / "td-recovery-state", root / "td-recovery-run", self.backend,
            expired_retention_seconds=3 * 3600,
        )
        item = manager.create("recovery-release", binary, self.template, assets, 300)
        config = Path(item.config_path)
        config.write_text(config.read_text() + "\n# keep me\n")
        stored = manager._load(item.id)
        stored.deadline = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        manager._save(stored)

        expired = manager.get(item.id)
        self.assertEqual("expired", expired.state)
        self.assertTrue(config.is_file())
        self.assertIn("# keep me", config.read_text())
        restarted = manager.restart(item.id)
        self.assertEqual("starting", restarted.state)
        self.assertGreater(datetime.fromisoformat(restarted.deadline), datetime.now(timezone.utc))
        self.assertEqual(item.namespace, self.backend.test_drive_upgrade["namespace"])
        self.assertIn("# keep me", config.read_text())
        extended = manager.extend(item.id, 300)
        self.assertEqual("running", extended.state)
        self.assertEqual(item.unit, self.backend.test_drive_extension[0])
        self.assertGreaterEqual(self.backend.test_drive_extension[1], 300)

    def test_test_drive_expired_recovery_window_eventually_destroys_lab(self):
        root = Path(self.temp.name)
        binary = root / "smithproxy-expired"
        binary.write_bytes(b"binary")
        binary.chmod(0o700)
        assets = root / "assets-expired"
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        manager = TestDriveManager(
            root / "td-expired-state", root / "td-expired-run", self.backend,
            expired_retention_seconds=60,
        )
        item = manager.create("expired-release", binary, self.template, assets, 300)
        stored = manager._load(item.id)
        stored.deadline = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        stored.expired_at = stored.deadline
        manager._save(stored)

        self.assertIsNone(manager.get(item.id))
        self.assertFalse(Path(item.workspace).exists())

    def test_rejects_duplicate_active_source(self):
        payload = {"runtime_seconds": 30, "source_ip": "198.51.100.10", "user_id": "test-user", "parameters": {"socks_port": 1080}}
        self.manager.create(payload)
        with self.assertRaises(ConfigError):
            self.manager.create(payload)

    def test_attach_additional_ipv6_source_to_live_instance(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user", "parameters": {"socks_port": 1080},
        })
        updated = self.manager.attach_source(item.id, "2001:db8::10")
        self.assertEqual(["198.51.100.10", "2001:db8::10"], updated.source_ips)
        self.assertEqual(
            (item.id, "2001:db8::10", "custom", 1080, 3128),
            self.backend.attached_source,
        )
        self.assertEqual(updated.source_ips, self.manager.get(item.id).source_ips)

    def test_create_records_and_passes_selected_build(self):
        selected_binary = Path(self.temp.name, "builds", "a" * 40, "smithproxy")
        selected_template = Path(self.temp.name, "selected.cfg")
        selected_template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; };')
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "build_id": "a" * 40, "config_id": "b" * 40,
            "parameters": {"socks_port": 1080},
        }, selected_binary, selected_template)
        self.assertEqual("a" * 40, item.build_id)
        self.assertEqual("b" * 40, item.config_id)
        self.assertEqual(str(selected_binary), self.backend.last_network["smithproxy_binary"])
        self.assertEqual(1, self.backend.native_save_calls)

    def test_create_passes_prepared_rootfs_as_opt_in_variant(self):
        selected_binary = Path(self.temp.name, "builds", "a" * 40, "smithproxy")
        selected_template = Path(self.temp.name, "selected-rootfs.cfg")
        selected_template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; };')
        rootfs = Path(self.temp.name, "rootfs")
        rootfs.mkdir()
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "rootfs-user", "filesystem_mode": "rootfs",
            "build_id": "a" * 40, "config_id": "b" * 40,
            "parameters": {"socks_port": 1080},
        }, selected_binary, selected_template, rootfs_path=rootfs)
        self.assertEqual("rootfs", item.filesystem_mode)
        self.assertEqual(str(rootfs), self.backend.last_network["rootfs_path"])

    def test_program_uses_existing_lifecycle_and_persists_launch_contract(self):
        rootfs = Path(self.temp.name) / 'program-root'
        rootfs.mkdir()
        config = rootfs / 'program.cfg'
        config.write_text('# program\n')
        program = {'application': 'router', 'argv': ['/usr/bin/sleep', 'infinity']}
        item = self.manager.create({
            'runtime_seconds': 30, 'user_id': 'test', 'filesystem_mode': 'rootfs',
            'network_ingress_driver': 'none', 'network_egress_driver': 'none',
        }, template_path=config, rootfs_path=rootfs, program=program)
        self.assertEqual('router', item.application)
        document = json.loads(self.manager._deployment_path(item.id).read_text())
        self.assertEqual(program, document['start_options']['program'])
        self.assertEqual(str(rootfs), document['start_options']['rootfs_path'])

    def test_create_records_network_bindings_and_passes_egress_policy(self):
        ingress_id = str(uuid.uuid4())
        egress_id = str(uuid.uuid4())
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "network-profile-user",
            "ingress_network_profile_id": ingress_id,
            "egress_network_profile_id": egress_id,
            "network_egress_mode": "routed",
            "network_egress_driver": "on-a-stick",
            "network_sas_interface": "lab0",
            "parameters": {"socks_port": 1080},
        })
        self.assertEqual(ingress_id, item.ingress_network_profile_id)
        self.assertEqual(egress_id, item.egress_network_profile_id)
        self.assertEqual("routed", self.backend.last_network["egress_mode"])
        self.assertEqual("on-a-stick", self.backend.last_network["egress_driver"])
        self.assertEqual("lab0", self.backend.last_network["sas_interface"])

    def test_build_default_config_profile_uses_transparent_dataplane(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "default-config-user", "profile": "default",
            "parameters": {"socks_port": 1080},
        })
        self.assertEqual("default", item.profile)
        self.assertEqual("default", self.backend.last_network["profile"])

    def test_exact_runtime_preflight_rejects_before_backend_start(self):
        selected_binary = Path(self.temp.name, "builds", "smithproxy")
        selected_template = Path(self.temp.name, "preflight.cfg")
        selected_template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; };')
        self.backend.native_save_error = "missing required native section"
        with self.assertRaisesRegex(BackendError, "missing required native section"):
            self.manager.create({
                "runtime_seconds": 30, "source_ip": "198.51.100.10",
                "user_id": "test-user", "parameters": {"socks_port": 1080},
            }, selected_binary, selected_template)
        self.assertEqual({}, self.backend.units)
        self.assertEqual([], list(Path(self.temp.name, "run", "managed").glob("*")))

    def test_runtime_uses_canonical_native_save_output_before_ro_lock(self):
        binary = Path(self.temp.name, "smithproxy-native")
        binary.write_bytes(b"binary")
        binary.chmod(0o700)

        def native_save(_binary, config_path, _cli_port=50000):
            source = config_path.read_text(encoding="utf-8")
            return '*_internal_* = { version = "0.9.32"; schema = 1039; };\n' + source

        self.backend.native_save = native_save
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "native-runtime", "parameters": {"socks_port": 1080},
            "config_mode": "ro",
        }, binary_path=binary)

        runtime_path = Path(self.temp.name, "run", item.id, "smithproxy.cfg")
        runtime_config = runtime_path.read_text(encoding="utf-8")
        state_config = Path(
            self.temp.name, "state", f"{item.id}.cfg",
        ).read_text(encoding="utf-8")
        self.assertTrue(runtime_config.startswith("*_internal_*"))
        self.assertEqual(runtime_config, state_config)
        self.assertEqual(0o400, runtime_path.stat().st_mode & 0o777)

    def test_shutdown_preserves_active_instances_by_default(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        self.manager.shutdown()
        self.assertEqual("active", self.backend.units[item.unit])

    def test_explicit_teardown_stops_active_instances(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user", "parameters": {"socks_port": 1080},
        })
        self.manager.shutdown(stop_instances=True)
        self.assertEqual("inactive", self.backend.units[item.unit])

    def test_reconcile_revalidates_pid_and_recorded_state(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        item.state = "failed"
        item.members = [{"role": "smithproxy", "pid": 999999, "rss_bytes": 1}]
        item.resources_cleaned = True
        self.manager._save(item)
        current = self.manager.get(item.id)
        self.assertEqual("running", current.state)
        self.assertEqual(4242, current.members[0]["pid"])
        self.assertEqual(64 * 1024, current.slice_rss_bytes)
        self.assertFalse(current.resources_cleaned)

    def test_reconcile_clears_dead_pid_and_marks_cleanup(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        item.members = [{"role": "smithproxy", "pid": 4242, "rss_bytes": 64 * 1024}]
        self.manager._save(item)
        self.backend.units[item.unit] = "inactive"
        current = self.manager.get(item.id)
        self.assertEqual([], current.members)
        self.assertTrue(current.resources_cleaned)
        self.assertEqual(0, current.slice_rss_bytes)

    def test_auto_restart_preserves_resources_and_records_crash(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user", "auto_restart": True,
            "parameters": {"socks_port": 1080},
        })
        item.members = [{"role": "smithproxy", "pid": 4242, "rss_bytes": 64 * 1024}]
        self.manager._save(item)
        self.backend.units[item.unit] = "failed"
        current = self.manager.get(item.id)
        self.assertEqual("starting", current.state)
        self.assertEqual(1, current.restart_count)
        self.assertEqual("trace for 4242", current.crash_trace)
        self.assertFalse(current.resources_cleaned)
        self.assertTrue(Path(self.temp.name, "run", item.id).exists())

    def test_delete_revalidates_and_removes_only_stopped_record(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        with self.assertRaises(ConfigError):
            self.manager.delete(item.id)
        self.manager.stop(item.id)
        deleted = self.manager.delete(item.id)
        self.assertEqual(item.id, deleted.id)
        self.assertIsNone(self.manager.get(item.id))

    def test_stopped_instance_cleanup_archives_gzip_after_three_hours(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "retention-user", "parameters": {"socks_port": 1080},
        })
        self.manager.stop(item.id)
        stopped = self.manager._load(item.id)
        self.assertIsNotNone(stopped)
        stopped.stopped_at = (datetime.now(timezone.utc) - timedelta(hours=3, seconds=1)).isoformat()
        self.manager._save(stopped)
        expected = self.manager._config_path(item.id).read_text(encoding="utf-8")

        cleaned = self.manager.cleanup_stopped()
        self.assertEqual(item.id, cleaned[0]["instance_id"])
        archive = Path(cleaned[0]["config_archive"])
        self.assertRegex(archive.name, rf"^\d{{8}}T\d{{6}}\.\d{{6}}Z_{item.id}\.cfg\.gz$")
        with gzip.open(archive, "rt", encoding="utf-8") as stored:
            self.assertEqual(expected, stored.read())
        self.assertIsNone(self.manager._load(item.id))
        self.assertFalse(self.manager._config_path(item.id).exists())

    def test_stopped_cleanup_retains_recent_or_unarchivable_record(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "recent-user", "parameters": {"socks_port": 1080},
        })
        self.manager.stop(item.id)
        self.assertEqual([], self.manager.cleanup_stopped())
        self.assertIsNotNone(self.manager._load(item.id))

        stopped = self.manager._load(item.id)
        stopped.stopped_at = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        self.manager._save(stopped)
        self.manager._config_path(item.id).unlink()
        self.assertEqual([], self.manager.cleanup_stopped())
        self.assertIsNotNone(self.manager._load(item.id))
        self.assertFalse(Path(self.temp.name, "state", f"{item.id}.cfg").exists())

    def test_manual_cleanup_deletes_stopped_nonpersistent_and_keeps_persistent(self):
        disposable = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "cleanup-user", "parameters": {"socks_port": 1080},
        })
        self.manager.stop(disposable.id)
        retained = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.11",
            "user_id": "persistent-user", "persistent": True,
            "parameters": {"socks_port": 1080},
        })
        self.manager.stop(retained.id)

        result = self.manager.cleanup_nonpersistent()

        self.assertEqual(1, result["cleaned_count"])
        self.assertEqual(disposable.id, result["cleaned"][0]["instance_id"])
        self.assertTrue(Path(result["cleaned"][0]["config_archive"]).is_file())
        self.assertIsNone(self.manager._load(disposable.id))
        self.assertIsNotNone(self.manager._load(retained.id))
        self.assertEqual([retained.id], result["skipped_persistent"])

        old = self.manager._load(retained.id)
        old.stopped_at = (datetime.now(timezone.utc) - timedelta(hours=4)).isoformat()
        self.manager._save(old)
        self.assertEqual([], self.manager.cleanup_stopped())
        self.assertIsNotNone(self.manager._load(retained.id))

    def test_discovery_tracks_unrecorded_unit_without_stopping_it(self):
        orphan_id = str(uuid.uuid4())
        unit = f"capture-zone-smithproxy-{orphan_id}.service"
        self.backend.units[unit] = "active"
        self.backend.orphans[orphan_id] = unit
        removed = self.manager.reconcile_orphans()
        self.assertEqual([orphan_id], removed)
        self.assertEqual("active", self.backend.units[unit])
        discovered = self.manager.get(orphan_id)
        self.assertEqual("orphaned", discovered.state)
        self.assertEqual(4242, discovered.members[0]["pid"])

    def test_rejects_unknown_parameter(self):
        with self.assertRaises(ConfigError):
            validate_parameters({"shell": 1})

    def test_systemd_status_is_parsed_by_property_name(self):
        status = parse_unit_status("MainPID=4242\nResult=\nSubState=running\nActiveState=active\n")
        self.assertEqual("active", status.active_state)
        self.assertEqual("running", status.sub_state)
        self.assertEqual("", status.result)
        self.assertEqual(4242, status.main_pid)

    def test_template_requires_values(self):
        self.template.write_text("x={{TLS_PORT}}")
        with self.assertRaises(ConfigError):
            render_template(self.template, {}, Path("/tmp/example"))

    def test_complete_config_gets_safe_runtime_overrides(self):
        self.template.write_text('''settings = {
 accept_tproxy = FALSE; accept_redirect = TRUE; accept_socks = TRUE;
 certs_path = "/etc/smithproxy/certs/default/"; messages_dir = "/etc/smithproxy/msg/en/";
 plaintext_port = "1"; ssl_port = "2"; plaintext_workers = 9; ssl_workers = 9;
 udp_workers = 9; dtls_workers = 9; socks_workers = 9;
 write_payload_dir = "/old"; write_pcap_single_quota = 999;
 cli = { port = 3; enable_password = ""; };
};
captures = { local = { dir = "/old-capture"; }; };
starttls_signatures = {};
''')
        config = render_template(self.template, {
            "plaintext_port": 50080, "tls_port": 50443, "cli_port": 50000,
            "workers": 1, "pcap_quota_mb": 100,
        }, Path("/tmp/lease"))
        self.assertIn("accept_tproxy = FALSE", config)
        self.assertIn("accept_socks = TRUE", config)
        self.assertIn(f'certs_path = "{self.template.parent}/template.assets/certs/default/"', config)
        self.assertIn(f'messages_dir = "{self.template.parent}/template.assets/msg/en/"', config)
        self.assertIn('ssl_port = "50443"', config)
        self.assertIn("dtls_workers = -1", config)
        self.assertIn("udp_workers = -1", config)
        self.assertIn('dir = "/old-capture"', config)
        self.assertIn("starttls_signatures", config)

    def test_missing_dtls_workers_is_inserted(self):
        self.template.write_text("settings = { plaintext_workers = 0; udp_workers = 0; };")
        config = render_template(self.template, {"workers": 1}, Path("/tmp/lease"))
        self.assertIn("dtls_workers = -1;", config)
        self.assertIn("udp_workers = -1", config)

    def test_native_save_scalars_without_semicolons_are_replaced_in_place(self):
        self.template.write_text('''settings =
{
    accept_cli = FALSE
    certs_path = "/old/certs/"
    certs_ca_key_password = "smithproxy"
    plaintext_port = "1"
    ssl_port = "2"
    socks_port = "3"
}
starttls_signatures = (
    { name = "smtp/starttls" }
)
''')
        config = render_template(
            self.template, {
                "plaintext_port": 50080, "tls_port": 50443,
                "socks_port": 1080, "workers": 1,
            }, Path("/tmp/lease"), assets_dir=Path("/tmp/runtime-assets"),
            ca_key_password="",
        )
        self.assertEqual(1, config.count("certs_path"))
        self.assertEqual(1, config.count("certs_ca_key_password"))
        self.assertIn("accept_cli = FALSE", config)
        self.assertIn('certs_path = "/tmp/runtime-assets/certs/default/"', config)
        self.assertIn('certs_ca_key_password = ""', config)
        self.assertIn('plaintext_port = "50080"', config)
        self.assertIn('ssl_port = "50443"', config)
        self.assertIn('socks_port = "1080"', config)
        self.assertIn("starttls_signatures", config)

    def test_known_magic_sni_placeholders_are_expanded(self):
        self.template.write_text('routing={ magic={ rewrite_sni="{{REWRITE_SNI}}"; rewrite_sni_to="{{REWRITE_SNI_TO}}"; }; };')
        config = render_template(
            self.template, {}, Path("/tmp/lease"),
            text_parameters={"rewrite_sni": "magic.example", "rewrite_sni_to": "real.example"},
        )
        self.assertIn('rewrite_sni="magic.example"', config)
        self.assertIn('rewrite_sni_to="real.example"', config)

    def test_magic_sni_profile_requires_and_passes_rewrite_values(self):
        self.template.write_text(
            'routing={ magic={ rewrite_sni="{{REWRITE_SNI}}"; '
            'rewrite_sni_to="{{REWRITE_SNI_TO}}"; }; };'
        )
        payload = {
            "runtime_seconds": 30,
            "source_ip": "198.51.100.10",
            "user_id": "magic-user",
            "profile": "magic-sni",
            "rewrite_sni": "magic.example",
            "rewrite_sni_to": "origin.example",
            "parameters": {},
        }
        item = self.manager.create(payload)
        self.assertEqual("magic-sni", item.profile)
        self.assertEqual("magic.example", item.rewrite_sni)
        self.assertEqual("magic-sni", self.backend.last_network["profile"])
        rendered = Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text()
        self.assertIn('rewrite_sni="magic.example"', rendered)
        self.assertIn('rewrite_sni_to="origin.example"', rendered)

        missing = dict(payload)
        missing["source_ip"] = "198.51.100.11"
        missing["rewrite_sni_to"] = ""
        with self.assertRaises(ConfigError):
            self.manager.create(missing)

    def test_reconcile_enforces_expired_deadline_even_if_systemd_is_active(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10", "user_id": "ttl-user",
            "parameters": {"socks_port": 1080},
        })
        item.deadline = "2000-01-01T00:00:00+00:00"
        self.manager._save(item)
        expired = self.manager.get(item.id)
        self.assertEqual("expired", expired.state)
        self.assertEqual("inactive", self.backend.units[item.unit])
        self.assertEqual("ttl-expired", expired.result)

    def test_cli_session_is_persistent_and_uses_carriage_return(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        session_id = str(uuid.uuid4())

        class ScriptedCliSocket:
            def __init__(self):
                self.responses = [b"\xff\xfb\x03\xff\xfd\x01\r\nsmithproxy> "]
                self.sent = []

            def settimeout(self, _timeout): pass
            def close(self): pass
            def recv(self, _size):
                if self.responses:
                    return self.responses.pop(0)
                raise TimeoutError

            def sendall(self, value):
                self.sent.append(value)
                self.responses.append(
                    b"en\r\nsmithproxy# " if value == b"en\r"
                    else b"show status\r\nreal status\r\nsmithproxy# "
                )

        client = ScriptedCliSocket()
        self.backend.cli_transport = client
        greeting = self.manager.cli(item.id, "", session_id)
        enabled = self.manager.cli(item.id, "en\r", session_id)
        status = self.manager.cli(item.id, "show status\r", session_id)
        self.assertEqual([b"en\r", b"show status\r"], client.sent)
        self.assertIn("smithproxy> ", greeting["output"])
        self.assertNotIn("\ufffd", greeting["output"])
        self.assertIn("smithproxy# ", enabled["output"])
        self.assertIn("real status", status["output"])
        self.manager.close_cli(item.id, session_id)
        self.assertNotIn(session_id, self.manager.cli_sessions)

    def test_gdb_transport_starts_helper_and_uses_selected_binary(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "debug-user", "parameters": {"socks_port": 1080},
        })
        binary = Path(self.temp.name, "smithproxy-debug")
        binary.write_bytes(b"debug binary")
        transport = self.manager.open_gdb_transport(item.id, binary)
        attached = self.manager.get(item.id)
        self.assertIs(transport, self.backend.gdb_transport)
        self.assertEqual(
            (item.id, binary, attached.debug_address, attached.debug_port),
            self.backend.gdb_target,
        )
        self.manager.close_gdb_transport(item.id, transport)
        self.assertTrue(transport.closed)
        self.assertFalse(self.manager.get(item.id).debug_unit)

    def test_builder_resolves_remote_branch_before_local_branch(self):
        repository = Path(self.temp.name, "builder-source")
        repository.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.name", "Capture Zone Test"], cwd=repository, check=True)
        subprocess.run(["git", "config", "user.email", "test@capture.zone"], cwd=repository, check=True)
        (repository / "revision").write_text("local")
        subprocess.run(["git", "add", "revision"], cwd=repository, check=True)
        subprocess.run(["git", "commit", "-qm", "local"], cwd=repository, check=True)
        local_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        (repository / "revision").write_text("remote")
        subprocess.run(["git", "commit", "-qam", "remote"], cwd=repository, check=True)
        remote_revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repository, text=True).strip()
        subprocess.run(["git", "branch", "mem-constrained", local_revision], cwd=repository, check=True)
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/mem-constrained", remote_revision],
            cwd=repository, check=True,
        )
        builder = SmithproxyBuilder(repository, Path(self.temp.name, "smithproxy"))
        self.assertEqual(remote_revision, builder._resolve_ref("mem-constrained"))
        builder._command(["git", "switch", "--detach", remote_revision], repository)
        self.assertEqual(remote_revision, subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
        ).strip())

    def test_builder_archives_and_resolves_complete_build_bundle(self):
        revision = "a" * 40
        candidate = Path(self.temp.name, "candidate")
        candidate.write_bytes(b"smithproxy-binary")
        candidate.chmod(0o755)
        config = Path(self.temp.name, "smithproxy.cfg")
        config.write_text("settings = {};")
        certs = Path(self.temp.name, "certs")
        messages = Path(self.temp.name, "msg")
        certs.mkdir(); messages.mkdir()
        (certs / "ca.pem").write_text("cert")
        (messages / "en.txt").write_text("message")
        builder = SmithproxyBuilder(Path(self.temp.name, "source"), Path(self.temp.name, "bin", "smithproxy"))
        builder._archive_bundle(
            revision, "mem-constrained", candidate, config, certs, messages,
            commit_at="2026-09-20T12:00:00+00:00",
        )
        artifacts = builder.status()["artifacts"]
        self.assertEqual(1, len(artifacts))
        self.assertEqual(revision, artifacts[0]["commit_id"])
        self.assertEqual(f"{revision}-release", artifacts[0]["build_id"])
        self.assertEqual("Release", artifacts[0]["build_type"])
        self.assertEqual("mem-constrained", artifacts[0]["ref"])
        self.assertEqual("2026-09-20T12:00:00+00:00", artifacts[0]["commit_at"])
        self.assertTrue(artifacts[0]["built_at"])
        self.assertEqual(b"smithproxy-binary", (
            builder.library_dir / f"{revision}-release" / "smithproxy"
        ).read_bytes())
        bundle = builder.resolve(f"{revision}-release")
        self.assertEqual(f"{revision}-release", bundle.build_id)
        self.assertEqual("settings = {};", bundle.config.read_text())
        self.assertEqual(f"{revision}-release", builder.configs()[0]["config_id"])
        self.assertEqual(bundle.config, builder.resolve_config(f"{revision}-release"))
        self.assertGreaterEqual(builder.status()["jobs"], 1)
        with builder.refs_lock:
            builder.refs_state = {
                "state": "ready", "refreshed_at": "now", "error": "",
                "branches": [{
                    "name": "mem-constrained", "commit_id": revision,
                    "commit_at": "2026-09-20T12:00:00+00:00",
                }],
            }
        branch = builder.status()["refs"]["branches"][0]
        self.assertFalse(branch["update_available"])
        self.assertEqual(["Release"], branch["current_types"])
        with builder.refs_lock:
            builder.refs_state["branches"][0]["commit_id"] = "b" * 40
        self.assertTrue(builder.status()["refs"]["branches"][0]["update_available"])
        deleted = builder.delete_artifact(f"{revision}-release")
        self.assertEqual(f"{revision}-release", deleted["build_id"])
        self.assertEqual([], builder.artifacts())
        with self.assertRaises(BackendError):
            builder.resolve_binary(f"{revision}-release")

    def test_builder_prepares_verified_minimal_rootfs_without_executing_elf(self):
        revision = "c" * 40
        build_id = f"{revision}-release"
        builder = SmithproxyBuilder(
            Path(self.temp.name, "source"), Path(self.temp.name, "bin", "smithproxy")
        )
        artifact = builder.library_dir / build_id
        artifact.mkdir(parents=True)
        shutil.copy2("/bin/true", artifact / "smithproxy")
        (artifact / "smithproxy").chmod(0o755)
        info = builder.prepare_rootfs(build_id)
        rootfs = Path(info["rootfs_path"])
        self.assertTrue(info["rootfs_ready"])
        self.assertTrue((rootfs / "usr/bin/smithproxy").is_file())
        self.assertTrue((rootfs / "bin/sh").is_file())
        self.assertTrue((rootfs / "opt/sas/bin").is_dir())
        self.assertTrue((rootfs / "etc/passwd").is_file())
        self.assertTrue(any(
            path.is_file() and path.name.startswith("lib") and ".so" in path.name
            for path in rootfs.rglob("*")
        ))
        first_created = info["rootfs_created_at"]
        self.assertEqual(first_created, builder.prepare_rootfs(build_id)["rootfs_created_at"])

    def test_builder_accepts_explicit_parallelism(self):
        builder = SmithproxyBuilder(
            Path(self.temp.name, "source"), Path(self.temp.name, "bin", "smithproxy"), jobs=7,
        )
        self.assertEqual(7, builder.status()["jobs"])

    def test_config_library_is_json_backed_deduplicated_and_integrity_checked(self):
        root = Path(self.temp.name)
        config = root / "source.cfg"
        assets = root / "source.assets"
        config.write_text("settings = {};", encoding="utf-8")
        (assets / "certs").mkdir(parents=True)
        (assets / "certs" / "ca.pem").write_text("certificate", encoding="utf-8")
        library = ConfigLibrary(root / "config-library")
        first = library.import_bundle(
            "master config", config, assets, source_commit="a" * 40, source_ref="master",
            source_kind="build",
        )
        second = library.import_bundle(
            "duplicate", config, assets, source_commit="a" * 40, source_ref="master",
            source_kind="build",
        )
        self.assertEqual(first["config_id"], second["config_id"])
        self.assertEqual(1, len(library.list()))
        self.assertEqual(config.read_text(), library.resolve(first["config_id"]).read_text())
        metadata = json.loads((library.root / first["config_id"] / "metadata.json").read_text())
        self.assertEqual("master", metadata["source_ref"])
        stored = library.root / first["config_id"] / "smithproxy.cfg"
        stored.write_text("tampered", encoding="utf-8")
        with self.assertRaises(BackendError):
            library.resolve(first["config_id"])
        self.assertEqual(first["config_id"], library.delete(first["config_id"])["config_id"])
        self.assertEqual([], library.list())

    def test_config_library_extracts_placeholders(self):
        root = Path(self.temp.name)
        assets = root / "config.assets"
        assets.mkdir()
        library = ConfigLibrary(root / "config-library-placeholders")
        item = library.import_text("template", 'x="{{TARGET}}";', assets)
        self.assertEqual(["TARGET"], item["placeholders"])

    def test_config_library_can_update_bundle_without_changing_identity(self):
        root = Path(self.temp.name)
        assets = root / "update.assets"
        assets.mkdir()
        (assets / "ca.pem").write_text("certificate", encoding="utf-8")
        library = ConfigLibrary(root / "config-library-update")
        original = library.import_text("original", 'x="one";', assets)
        replacement_assets = root / "replacement.assets"
        replacement_assets.mkdir()
        (replacement_assets / "new.pem").write_text("replacement", encoding="utf-8")
        updated = library.update_text(
            original["config_id"], "production", 'x="{{TARGET}}";',
            description="updated in place", profile="magic-sni",
            assets_from=replacement_assets,
        )
        self.assertEqual(original["config_id"], updated["config_id"])
        self.assertEqual("production", updated["name"])
        self.assertEqual(["TARGET"], updated["placeholders"])
        self.assertEqual('x="{{TARGET}}";', library.read_content(original["config_id"]))
        self.assertTrue((library.resolve_assets(original["config_id"]) / "new.pem").is_file())
        self.assertTrue(updated["updated_at"])

    def test_native_config_metadata_is_explicit(self):
        root = Path(self.temp.name)
        assets = root / "native.assets"
        assets.mkdir()
        library = ConfigLibrary(root / "config-library-native")
        item = library.import_text(
            "approved", 'settings={};', assets,
            native=True, normalized_build_id="build-123",
            source_sha256="a" * 64, approved_by="admin@example.test",
        )
        self.assertTrue(item["native"])
        self.assertEqual("build-123", item["normalized_build_id"])
        self.assertEqual("a" * 64, item["source_sha256"])
        self.assertEqual("admin@example.test", item["approved_by"])
        self.assertEqual(hashlib.sha256(b'settings={};').hexdigest(), item["native_sha256"])

    def test_missing_build_defaults_are_pruned_without_touching_manual_configs(self):
        root = Path(self.temp.name)
        assets = root / "prune.assets"
        assets.mkdir()
        library = ConfigLibrary(root / "config-library-prune")
        present = library.import_text(
            "present default", "settings={};", assets,
            source_kind="native-build-default", native=True,
            normalized_build_id="present-release",
        )
        missing = library.import_text(
            "missing default", "settings={x=1;};", assets,
            source_kind="native-build-default", native=True,
            normalized_build_id="missing-release",
        )
        manual = library.import_text(
            "manual", "settings={x=2;};", assets,
            source_kind="native-upload", native=True,
            normalized_build_id="missing-release",
        )

        removed = library.prune_missing_build_defaults({"present-release"})

        self.assertEqual([missing["config_id"]], [item["config_id"] for item in removed])
        self.assertEqual(
            {present["config_id"], manual["config_id"]},
            {item["config_id"] for item in library.list()},
        )

    def test_config_metadata_update_does_not_change_native_bundle(self):
        root = Path(self.temp.name)
        assets = root / "metadata.assets"
        assets.mkdir()
        library = ConfigLibrary(root / "config-library-metadata")
        original = library.import_text(
            "old title", "settings={};", assets,
            source_kind="native-upload", native=True,
            normalized_build_id="build-release",
        )

        updated = library.update_metadata(original["config_id"], "new title", "new description")

        self.assertEqual("new title", updated["name"])
        self.assertEqual("new description", updated["description"])
        self.assertEqual(original["sha256"], updated["sha256"])
        self.assertEqual("settings={};", library.read_content(original["config_id"]))

    def test_config_preview_is_file_backed_and_deletable(self):
        root = Path(self.temp.name, "config-previews")
        previews = ConfigPreviewLibrary(root, ttl_seconds=60)
        assets = Path(self.temp.name, "preview.assets")
        assets.mkdir()
        (assets / "ca.pem").write_text("certificate", encoding="utf-8")
        created = previews.create(
            {"action": "create", "native_content": "settings={};"}, assets=assets,
        )
        preview_id = created["preview_id"]
        self.assertEqual("settings={};", previews.get(preview_id)["native_content"])
        self.assertEqual(
            "certificate", (previews.resolve_assets(preview_id) / "ca.pem").read_text()
        )
        self.assertEqual(preview_id, previews.claim(preview_id)["preview_id"])
        with self.assertRaises(BackendError):
            previews.claim(preview_id)
        previews.release(preview_id)
        previews.delete(preview_id)
        self.assertFalse((root / f"{preview_id}.assets").exists())
        with self.assertRaises(BackendError):
            previews.get(preview_id)

    def test_rw_instance_preserves_locally_saved_config_on_stop(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "rw-user", "config_mode": "rw",
            "parameters": {"socks_port": 1080},
        })
        live = Path(self.temp.name, "run", item.id, "smithproxy.cfg")
        self.assertEqual("rw", item.config_mode)
        self.assertEqual("rw", self.backend.last_network["config_mode"])
        live.write_text('settings={ saved_by_cli="yes"; };')
        self.manager.stop(item.id)
        _, content = self.manager.config_content(item.id)
        self.assertIn('saved_by_cli="yes"', content)

    def test_restart_preserves_instance_container_and_deadline(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "restart-user", "parameters": {"socks_port": 1080},
        })
        namespace = item.namespace
        unit = item.unit
        old_deadline = item.deadline
        restarted = self.manager.restart(item.id)
        self.assertEqual(item.id, restarted.id)
        self.assertEqual(unit, restarted.unit)
        self.assertEqual(namespace, restarted.namespace)
        self.assertEqual("starting", restarted.state)
        self.assertEqual(restarted.deadline, old_deadline)
        self.assertEqual(1, self.backend.restart_count)

    def test_extend_adds_runtime_and_updates_backend_limit(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "extend-user", "parameters": {"socks_port": 1080},
        })
        old_deadline = item.deadline
        extended = self.manager.extend(item.id, 300)
        self.assertGreater(extended.deadline, old_deadline)
        self.assertGreaterEqual(extended.runtime_seconds, 329)
        self.assertEqual(item.unit, self.backend.extended_runtime[0])
        self.assertEqual(extended.runtime_seconds, self.backend.extended_runtime[1])

    def test_unlimited_runtime_has_no_deadline_and_cannot_be_extended(self):
        with self.assertRaises(ConfigError):
            self.manager.create({
                "runtime_seconds": 0, "source_ip": "198.51.100.10",
                "user_id": "manual-unlimited", "parameters": {"socks_port": 1080},
            })
        item = self.manager.create({
            "runtime_seconds": 0, "source_ip": "198.51.100.10",
            "user_id": "unlimited-user", "parameters": {"socks_port": 1080},
            "runtime_profile_id": str(uuid.uuid4()),
        })
        self.assertEqual("", item.deadline)
        self.assertEqual(0, item.runtime_seconds)
        self.assertEqual(0, self.backend.last_network["hard_runtime_seconds"])
        restarted = self.manager.restart(item.id)
        self.assertEqual("", restarted.deadline)
        with self.assertRaises(ConfigError):
            self.manager.extend(item.id, 300)

    def test_task_queue_deduplicates_only_active_work(self):
        queue = TaskQueue(Path(self.temp.name, "tasks.json"), workers=1)
        gate = threading.Event()
        calls = []

        def work():
            calls.append("run")
            gate.wait(2)
            return {"ok": True}

        first, created = queue.submit("test", "Test", "same", "resource", work)
        duplicate, duplicate_created = queue.submit("test", "Test", "same", "resource", work)
        self.assertTrue(created)
        self.assertFalse(duplicate_created)
        self.assertEqual(first.task_id, duplicate.task_id)
        gate.set()
        deadline = time.time() + 3
        while queue.get(first.task_id).state not in {"succeeded", "failed"} and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual("succeeded", queue.get(first.task_id).state)
        again, again_created = queue.submit("test", "Test", "same", "resource", lambda: None)
        self.assertTrue(again_created)
        self.assertNotEqual(first.task_id, again.task_id)
        self.assertEqual(["run"], calls)
        queue.work.join()

    def test_task_queue_stores_large_results_outside_state_json(self):
        queue = TaskQueue(Path(self.temp.name, "large-tasks.json"), workers=1)
        task, _ = queue.submit(
            "preview", "Large result", "large", "config",
            lambda: {"preview_id": "preview-1", "diff": "x" * (70 * 1024)},
        )
        queue.work.join()
        current = queue.get(task.task_id)
        self.assertEqual("succeeded", current.state)
        self.assertTrue(current.result["stored"])
        self.assertEqual("preview-1", current.result["preview_id"])
        self.assertEqual("x" * (70 * 1024), queue.read_result(task.task_id)["diff"])
        persisted = Path(self.temp.name, "large-tasks.json").read_text(encoding="utf-8")
        self.assertNotIn("x" * 1000, persisted)

    def test_network_settings_allocate_stable_sequential_30_leases(self):
        settings = NetworkSettings(
            Path(self.temp.name, "network.json"), Path(self.temp.name, "allocations.json")
        )
        settings.update({
            "namespace_cidr": "10.250.0.0/29", "allocation_prefix": 30,
            "egress_mode": "routed", "sas_route_via": "192.0.2.10",
            "sas_interface": "eth1", "route_table_start": 60000,
            "mark_start": 0x200000,
        })
        backend = NamespaceBackend(network_settings=settings)
        backend._live_subnets = lambda: set()
        first_id = str(uuid.uuid4())
        second_id = str(uuid.uuid4())
        first = backend._allocate(first_id)
        second = backend._allocate(second_id)
        self.assertEqual("10.250.0.0/30", first.subnet)
        self.assertEqual("10.250.0.1", first.host_ip)
        self.assertEqual("10.250.0.2", first.guest_ip)
        self.assertEqual("fd42:ca7:200::/126", first.subnet_v6)
        self.assertEqual("fd42:ca7:200::1", first.host_ip_v6)
        self.assertEqual("fd42:ca7:200::2", first.guest_ip_v6)
        self.assertEqual("10.250.0.4/30", second.subnet)
        self.assertEqual("routed", first.egress_mode)
        self.assertEqual("eth1", first.sas_interface)
        self.assertEqual("do0", first.guest_if)
        self.assertTrue(first.host_if.startswith("czo"))
        ingress_id = backend.ingress_id(first_id)
        ingress = backend._allocate(ingress_id, "ingress", first_id)
        self.assertEqual("10.100.0.0/30", ingress.subnet)
        self.assertEqual("fd42:ca7:100::/126", ingress.subnet_v6)
        self.assertEqual("di0", ingress.guest_if)
        self.assertTrue(ingress.host_if.startswith("czi"))
        self.assertEqual(first.namespace, ingress.namespace)
        self.assertNotEqual(first.route_table, ingress.route_table)
        self.assertNotEqual(first.mark, ingress.mark)
        self.assertEqual(2, settings.view()["allocated_instances"])
        self.assertEqual(first, backend.allocation(first_id))
        self.assertEqual("ip route add 10.250.0.0/29 via 192.0.2.10",
                         settings.view()["sas_route_command"])
        with self.assertRaisesRegex(ConfigError, "leases are active"):
            settings.update({
                "namespace_cidr": "10.251.0.0/29", "allocation_prefix": 30,
                "egress_mode": "routed", "sas_route_via": "192.0.2.10",
                "sas_interface": "eth1", "route_table_start": 60000,
                "mark_start": 0x200000,
            })
        with self.assertRaisesRegex(BackendError, "exhausted"):
            backend._allocate(str(uuid.uuid4()))
        settings.release(first_id)
        reused = backend._allocate(str(uuid.uuid4()))
        self.assertEqual("10.250.0.0/30", reused.subnet)
        self.assertEqual("10.240.0.1", first.fabric_ip)
        self.assertEqual("fd42:ca7:240::1", first.fabric_ip_v6)
        self.assertEqual(1, first.tuntom_tunnel_id)

    def test_on_a_stick_uses_only_di0_for_ingress_and_proxy_egress(self):
        settings = NetworkSettings(
            Path(self.temp.name, "stick-network.json"),
            Path(self.temp.name, "stick-allocations.json"),
        )
        backend = NamespaceBackend("/bin/true", network_settings=settings)
        backend._live_subnets = lambda: set()
        commands = []
        backend._run = lambda command, **_kwargs: commands.append(command)
        instance_id = str(uuid.uuid4())
        workspace = Path(self.temp.name, "stick-runtime", instance_id)
        workspace.mkdir(parents=True)
        config = workspace / "smithproxy.cfg"
        config.write_text("settings = {};", encoding="utf-8")
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("runner.namespace.subprocess.run", return_value=completed):
            backend.start(
                instance_id, config, 60, source_ip="198.51.100.10",
                smithproxy_binary="/bin/true", egress_driver="on-a-stick",
            )
        rendered = [" ".join(command) for command in commands]
        self.assertFalse(any("do0" in command or "czo" in command for command in rendered))
        self.assertTrue(any("route add default" in command and "dev di0" in command
                            for command in rendered))
        allocation = backend.allocation(instance_id)
        self.assertEqual("on-a-stick", allocation.topology)
        self.assertEqual("di0", allocation.guest_if)

    def test_tuntom_via_uses_adapter_owned_tuns_without_host_veth(self):
        settings = NetworkSettings(
            Path(self.temp.name, "via-network.json"),
            Path(self.temp.name, "via-allocations.json"),
        )
        backend = NamespaceBackend("/bin/true", network_settings=settings)
        backend._live_subnets = lambda: set()
        commands = []
        backend._run = lambda command, **_kwargs: commands.append(command)
        backend._setup_fabric_link = lambda allocation, switch_ip: commands.append(
            ["fabric-link", allocation.fabric_if, switch_ip]
        )
        relay_socket = Path(self.temp.name, "relay.sock")
        relay_calls = []
        backend._start_tuntom_relay = lambda *args, **kwargs: (
            relay_calls.append(args) or backend.tuntom_relay_unit_name(args[0]), relay_socket
        )
        backend._start_tuntom_adapter = lambda *args, **kwargs: (
            commands.append(["tuntom-adapter", args[0], args[1]])
            or backend.tuntom_unit_name(args[0])
        )
        instance_id = str(uuid.uuid4())
        workspace = Path(self.temp.name, "via-runtime", instance_id)
        workspace.mkdir(parents=True)
        config = workspace / "smithproxy.cfg"
        config.write_text("settings = {};", encoding="utf-8")
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch("runner.namespace.subprocess.run", return_value=completed):
            backend.start(
                instance_id, config, 60, source_ip="198.51.100.10",
                smithproxy_binary="/bin/true", egress_driver="tuntom-via",
                egress_mode="routed", tuntom_switch_ip="192.0.2.10",
                tuntom_secret="a" * 32, tuntom_tunnel_id=77,
            )
        rendered = [" ".join(command) for command in commands]
        self.assertTrue(any(command.startswith("tuntom-adapter") for command in rendered))
        self.assertFalse(any("type veth" in command for command in rendered))
        self.assertTrue(any("route add default dev do0" in command for command in rendered))
        self.assertTrue(any("198.51.100.10/32 dev di0" in command for command in rendered))
        self.assertEqual("tuntom-via", backend.allocation(instance_id).topology)
        self.assertEqual(77, backend.allocation(instance_id).tuntom_tunnel_id)
        self.assertEqual(77, relay_calls[0][5])

    def test_tuntom_adapter_unit_uses_via_identity_and_unique_attachments(self):
        backend = NamespaceBackend()
        adapter = Path(self.temp.name, "tuntom-divert-adapter")
        adapter.write_text("#!/bin/sh\n", encoding="utf-8")
        adapter.chmod(0o700)
        socket_path = Path(self.temp.name, "via.sock")
        commands = []
        backend._run = lambda command, **_kwargs: commands.append(command)
        completed = SimpleNamespace(returncode=0, stdout="active\n", stderr="")
        instance_id = "12345678-1234-1234-1234-123456789abc"
        with patch("runner.namespace.subprocess.run", return_value=completed), \
                patch("runner.namespace.Path.is_socket", return_value=True):
            unit = backend._start_tuntom_adapter(
                instance_id, "cz-12345678", str(adapter), str(socket_path),
                "proxy-in-", "proxy-out-", "immediate", 1400, 120,
                Path(self.temp.name, "via-run"),
            )
        command = commands[0]
        self.assertEqual("capture-zone-tuntom-adapter-12345678-1234-1234-1234-123456789abc.service", unit)
        self.assertIn("sas#12345678", command)
        self.assertIn("proxy-in-12345678", command)
        self.assertIn("proxy-out-12345678", command)
        self.assertIn("--property=NetworkNamespacePath=/run/netns/cz-12345678", command)
        self.assertIn(
            "--slice=capture-zone-slice-12345678-1234-1234-1234-123456789abc.slice",
            command,
        )

    def test_tuntom_relay_uses_credential_not_secret_argv(self):
        backend = NamespaceBackend()
        binary = Path(self.temp.name, "tuntom")
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o700)
        via_run = Path(self.temp.name, "relay-run")
        commands = []
        backend._run = lambda command, **_kwargs: commands.append(command)
        instance_id = "12345678-1234-1234-1234-123456789abc"
        with patch("runner.namespace.Path.is_socket", return_value=True):
            unit, socket_path = backend._start_tuntom_relay(
                instance_id, "cz-12345678", str(binary), "192.0.2.10",
                "a" * 32, 42, 1400, 120, via_run,
            )
        command = commands[0]
        rendered = " ".join(command)
        self.assertEqual(backend.tuntom_relay_unit_name(instance_id), unit)
        self.assertEqual(via_run / "relay.sock", socket_path)
        self.assertNotIn("a" * 32, rendered)
        self.assertIn("LoadCredential=tuntom-secret:", rendered)
        self.assertIn("192.0.2.10", command)
        self.assertIn("42", command)
        self.assertEqual(
            "a" * 32,
            (via_run.parent / ".credentials" / "tuntom-secret").read_text().strip(),
        )

    def test_arbitrary_declared_placeholder_is_required_and_rendered(self):
        self.template.write_text('settings={ target="{{TARGET_HOST}}"; };')
        payload = {
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "template-user", "parameters": {},
        }
        with self.assertRaises(ConfigError):
            self.manager.create(payload)
        payload["template_values"] = {"TARGET_HOST": "origin.example"}
        item = self.manager.create(payload)
        rendered = Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text()
        self.assertIn('target="origin.example"', rendered)

    def test_runtime_profiles_are_json_backed_bindings(self):
        library = RuntimeProfileLibrary(Path(self.temp.name, "runtime-profiles.json"))
        item = library.create("Magic stable", "a" * 40, str(uuid.uuid4()), ttl_seconds=None)
        self.assertEqual("Magic stable", library.get(item["profile_id"])["name"])
        self.assertEqual("a" * 40, item["build_id"])
        self.assertIsNone(item["ttl_seconds"])
        updated = library.update(
            item["profile_id"], "Magic next", "b" * 40, str(uuid.uuid4()), "bundle-id",
            ttl_seconds=900, filesystem_mode="rootfs",
        )
        self.assertEqual(item["profile_id"], updated["profile_id"])
        self.assertEqual("Magic next", updated["name"])
        self.assertEqual("b" * 40, updated["build_id"])
        self.assertEqual(900, updated["ttl_seconds"])
        self.assertEqual("rootfs", updated["filesystem_mode"])
        self.assertTrue(updated["updated_at"])
        work_file = library.put_work_file(
            item["profile_id"], "ssh/id_ed25519", b"private-key\n", 0o600,
        )
        self.assertEqual("ssh/id_ed25519", work_file["path"])
        self.assertEqual("0600", work_file["mode"])
        destination = Path(self.temp.name, "instance-work")
        destination.mkdir()
        library.install_work_files(item["profile_id"], destination)
        installed = destination / "ssh" / "id_ed25519"
        self.assertEqual(b"private-key\n", installed.read_bytes())
        self.assertEqual(0o600, installed.stat().st_mode & 0o777)
        with self.assertRaises(BackendError):
            library.put_work_file(item["profile_id"], "../escape", b"no")
        with self.assertRaises(BackendError):
            library.put_work_file(item["profile_id"], "smithproxy.cfg", b"no")
        document = json.loads(Path(self.temp.name, "runtime-profiles.json").read_text())
        self.assertEqual(item["profile_id"], document["profiles"][0]["profile_id"])
        self.assertEqual(item["profile_id"], library.delete(item["profile_id"])["profile_id"])
        self.assertEqual([], library.list())
        self.assertFalse((library.work_root / item["profile_id"]).exists())

    def test_network_profiles_are_atomic_composable_bindings(self):
        library = NetworkProfileLibrary(Path(self.temp.name, "network-profiles.json"))
        ingress = library.create({
            "kind": "ingress", "name": "Authorized source · dual",
            "description": "current dataplane", "address_family": "dual",
            "driver": "split-veth", "selector": "source",
            "require_authorization": True, "interface_name": "di0",
            "destination_cidrs": [],
        })
        egress = library.create({
            "kind": "egress", "name": "Routed via lab uplink",
            "description": "current dataplane with an override",
            "address_family": "dual", "driver": "split-veth",
            "mode": "routed", "interface_name": "do0",
            "host_interface": "lab0",
        })
        self.assertTrue(ingress["implemented"])
        self.assertTrue(egress["implemented"])
        stick = library.create({
            "kind": "egress", "name": "No egress",
            "address_family": "dual", "driver": "none",
            "mode": "routed", "interface_name": "",
        })
        self.assertTrue(stick["implemented"])
        self.assertEqual("", stick["interface_name"])
        for driver in ("tuntom", "tuntom-via", "blackbox-link", "on-a-stick"):
            with self.subTest(driver=driver):
                with self.assertRaisesRegex(BackendError, "retired"):
                    library.create({"kind": "egress", "name": "Removed", "driver": driver})
                with self.assertRaisesRegex(BackendError, "retired"):
                    library.update(egress["network_profile_id"], {**egress, "driver": driver})
        # Retired records remain readable/deletable, but cannot start a new run.
        legacy = {
            **egress, "driver": "blackbox-link",
            "network_profile_id": str(uuid.uuid4()),
        }
        library._save([*library._load(), legacy])
        stored = library.get(legacy["network_profile_id"])
        self.assertTrue(stored["retired"])
        self.assertFalse(stored["implemented"])
        library.delete(legacy["network_profile_id"])
        design = library.create({
            "kind": "ingress", "name": "Destination design",
            "driver": "split-veth", "selector": "destination",
            "address_family": "dual", "interface_name": "di0",
            "require_authorization": True,
            "destination_cidrs": ["10.10.0.9/24"],
        })
        self.assertEqual("10.10.0.0/24", design["destination_cidrs"][0])
        self.assertFalse(design["implemented"])
        updated = library.update(egress["network_profile_id"], {
            **egress, "name": "Masquerade via lab uplink", "mode": "masquerade",
        })
        self.assertEqual("masquerade", updated["mode"])
        with self.assertRaisesRegex(BackendError, "destination selector"):
            library.create({
                "kind": "ingress", "name": "Broken destination",
                "driver": "split-veth", "selector": "destination",
                "address_family": "dual", "interface_name": "di0",
                "require_authorization": True, "destination_cidrs": [],
            })
        document = json.loads(Path(self.temp.name, "network-profiles.json").read_text())
        self.assertEqual(1, document["schema"])
        self.assertEqual(4, len(document["profiles"]))
        self.assertEqual(
            ingress["network_profile_id"],
            library.delete(ingress["network_profile_id"])["network_profile_id"],
        )

    def test_headless_endpoint_package_is_atomic_and_single_owner(self):
        library = HeadlessEndpointLibrary(Path(self.temp.name, "headless-endpoints.json"))
        package_id = str(uuid.uuid4())
        item = library.create({
            "package_id": package_id, "kind": "tuntom-via", "name": "port 17",
            "fabric_port_id": "fabric-a/proxy-17", "switch_ip": "2001:db8::10",
            "tunnel_id": 17, "secret": "ab" * 16,
        })
        self.assertEqual("available", item["state"])
        self.assertNotIn("secret", item)
        reserved = library.reserve(package_id, "spawn-one")
        self.assertEqual("ab" * 16, reserved["secret"])
        with self.assertRaisesRegex(BackendError, "already owned"):
            library.reserve(package_id, "spawn-two")
        bound = library.bind(package_id, "spawn-one", str(uuid.uuid4()))
        self.assertEqual("bound", bound["state"])
        with self.assertRaisesRegex(BackendError, "cannot be deleted"):
            library.delete(package_id)

    def test_headless_endpoint_identity_cannot_be_imported_twice(self):
        library = HeadlessEndpointLibrary(Path(self.temp.name, "headless-endpoints.json"))
        base = {
            "package_id": str(uuid.uuid4()), "kind": "tuntom-via", "name": "one",
            "fabric_port_id": "fabric-a/proxy-17", "switch_ip": "192.0.2.10",
            "tunnel_id": 17, "secret": "cd" * 16,
        }
        library.create(base)
        with self.assertRaisesRegex(BackendError, "Fabric port"):
            library.create({**base, "package_id": str(uuid.uuid4()), "name": "two"})
        with self.assertRaisesRegex(BackendError, "tunnel ID"):
            library.create({
                **base, "package_id": str(uuid.uuid4()), "name": "three",
                "fabric_port_id": "fabric-a/proxy-18",
            })

    def test_firewall_renders_scoped_input_and_instance_forward_allowlists(self):
        applied = []
        firewall = FirewallManager(
            Path(self.temp.name, "firewall.json"), executor=applied.append,
        )
        firewall.apply("10.200.0.0/16", ["198.51.100.10"])
        self.assertIn("table inet capture_zone_access", applied[-1])
        self.assertNotIn("SAS INPUT deny", applied[-1])
        firewall.update_settings(True, True, "10.200.0.0/16", ["198.51.100.10"])
        item = firewall.add({
            "source": "203.0.113.40", "chains": ["input", "forward"],
            "system": "capture-portal", "label": "user session", "ttl_seconds": 300,
        }, "10.200.0.0/16", ["198.51.100.10"], registered_source=True)
        ruleset = applied[-1]
        self.assertIn("203.0.113.40/32", ruleset)
        self.assertIn("oifname \"czi*\"", ruleset)
        self.assertIn("ip saddr 10.200.0.0/16 accept", ruleset)
        self.assertIn("ip6 saddr @input_sources_v6 accept", ruleset)
        self.assertIn("ip6 saddr fd42:ca7:200::/64 accept", ruleset)
        ipv6_item = firewall.add({
            "source": "2001:db8::40", "chains": ["input", "forward"],
            "system": "capture-portal", "label": "IPv6 session", "ttl_seconds": 300,
        }, "10.200.0.0/16", ["198.51.100.10"])
        self.assertIn("2001:db8::40/128", applied[-1])
        selected = firewall.add({
            "source": "203.0.113.41", "chains": ["input", "forward"],
            "system": "capture-portal", "protocol": "tcp",
            "destination": "10.20.30.40", "ports": "443, 8000-8010",
        }, "10.200.0.0/16", ["198.51.100.10"])
        self.assertEqual(["443", "8000-8010"], selected["ports"])
        self.assertIn("ip saddr 203.0.113.41/32 ip daddr 10.20.30.40/32 tcp dport { 443, 8000-8010 }", applied[-1])
        self.assertIn('oifname "czi*" ip saddr 203.0.113.41/32', applied[-1])
        with self.assertRaises(ConfigError):
            firewall.add({
                "source": "203.0.113.42", "chains": ["forward"],
                "system": "capture-portal", "protocol": "any", "ports": "443",
            }, "10.200.0.0/16", [])
        with self.assertRaises(ConfigError):
            firewall.add({
                "source": "203.0.113.42", "chains": ["forward"],
                "system": "capture-portal", "destination": "2001:db8::1",
            }, "10.200.0.0/16", [])
        self.assertTrue(firewall.view("10.200.0.0/16", ["198.51.100.10"])[
            "authorizations"
        ][0]["active"])
        refreshed = firewall.add({
            "source": "203.0.113.40", "chains": ["input", "forward"],
            "system": "capture-portal", "label": "refreshed session", "ttl_seconds": 600,
        }, "10.200.0.0/16", ["198.51.100.10"])
        self.assertEqual(item["authorization_id"], refreshed["authorization_id"])
        self.assertEqual("refreshed session", refreshed["label"])
        self.assertEqual(3, len(firewall.load()["authorizations"]))
        previous_expiry = datetime.fromisoformat(refreshed["expires_at"])
        extended = firewall.extend(
            item["authorization_id"], 300,
            "10.200.0.0/16", ["198.51.100.10"],
        )
        self.assertEqual(
            previous_expiry + timedelta(seconds=300),
            datetime.fromisoformat(extended["expires_at"]),
        )
        self.assertEqual(item["authorization_id"], firewall.delete(
            item["authorization_id"], "10.200.0.0/16", ["198.51.100.10"],
        )["authorization_id"])
        firewall.delete(
            ipv6_item["authorization_id"], "10.200.0.0/16", ["198.51.100.10"],
        )
        firewall.delete(
            selected["authorization_id"], "10.200.0.0/16", ["198.51.100.10"],
        )
        self.assertEqual([], firewall.load()["authorizations"])

    def test_certificate_library_generates_real_ca_and_leaf(self):
        library = CertBundleLibrary(Path(self.temp.name, "cert-library"))
        bundle = library.create("Lab trust", "Capture Zone Test CA", 30)
        certs = library.resolve(bundle["bundle_id"])
        self.assertTrue((certs / "ca-cert.pem").is_file())
        self.assertEqual(0o600, (certs / "ca-key.pem").stat().st_mode & 0o777)
        leaf = library.generate_certificate(
            bundle["bundle_id"], "Viewer", "viewer.test", 7, "portal"
        )
        self.assertEqual("generated", leaf["kind"])
        self.assertTrue((certs / leaf["filename"]).is_file())
        self.assertEqual("portal-cert.pem", leaf["filename"])
        self.assertEqual("portal-key.pem", leaf["key_filename"])
        self.assertEqual(2, len(library.get(bundle["bundle_id"])["certificates"]))

    def test_certificate_bundle_overlays_instance_assets(self):
        library = CertBundleLibrary(Path(self.temp.name, "cert-library"))
        bundle = library.create("Lab trust", "Capture Zone Test CA", 30)
        assets = Path(self.temp.name, "base-assets")
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "certs" / "default" / "ca-cert.pem").write_text("old")
        (assets / "msg" / "en").mkdir(parents=True)
        template = Path(self.temp.name, "cert-template.cfg")
        template.write_text(
            'settings={ socks_port="{{SOCKS_PORT}}"; certs_path="/old/"; '
            'certs_ca_key_password="smithproxy"; messages_dir="/old/msg/"; };'
        )
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10", "user_id": "test-user",
            "cert_bundle_id": bundle["bundle_id"], "parameters": {"socks_port": 1080},
        }, template_path=template, assets_dir=assets,
            cert_bundle_dir=library.resolve(bundle["bundle_id"]))
        runtime = Path(self.temp.name, "run", item.id)
        rendered = (runtime / "smithproxy.cfg").read_text()
        self.assertIn('certs_ca_key_password=""', rendered)
        self.assertNotEqual("old", (
            runtime / "smithproxy.assets" / "certs" / "default" / "ca-cert.pem"
        ).read_text())
        self.assertTrue((
            runtime / "smithproxy.assets" / "certs" / "default" / "sni"
        ).is_dir())

    def test_instance_gets_private_writable_copy_of_build_assets(self):
        assets = Path(self.temp.name, "private-base-assets")
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "certs" / "default" / "ca-cert.pem").write_text("shared")
        (assets / "msg" / "en").mkdir(parents=True)
        template = Path(self.temp.name, "private-assets-template.cfg")
        template.write_text(
            'settings={ socks_port="{{SOCKS_PORT}}"; certs_path="/old/"; '
            'messages_dir="/old/msg/"; };'
        )

        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "private-assets", "parameters": {"socks_port": 1080},
        }, template_path=template, assets_dir=assets)

        private_assets = Path(
            self.temp.name, "run", item.id, "smithproxy.assets",
        )
        self.assertIn(
            str(private_assets / "certs" / "default"),
            Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text(),
        )
        self.assertTrue((private_assets / "certs" / "default" / "cc-sni").is_dir())
        (private_assets / "certs" / "default" / "ca-cert.pem").write_text("instance")
        self.assertEqual(
            "shared", (assets / "certs" / "default" / "ca-cert.pem").read_text(),
        )

    def test_certificate_bundle_inserts_omitted_native_ca_settings(self):
        library = CertBundleLibrary(Path(self.temp.name, "cert-library-omitted"))
        bundle = library.create("Lab trust", "Capture Zone Test CA", 30)
        assets = Path(self.temp.name, "base-assets-omitted")
        (assets / "certs" / "default").mkdir(parents=True)
        (assets / "msg" / "en").mkdir(parents=True)
        template = Path(self.temp.name, "cert-template-omitted.cfg")
        template.write_text(
            'settings={ socks_port="{{SOCKS_PORT}}"; messages_dir="/old/msg/"; };'
        )
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user", "cert_bundle_id": bundle["bundle_id"],
            "parameters": {"socks_port": 1080},
        }, template_path=template, assets_dir=assets,
            cert_bundle_dir=library.resolve(bundle["bundle_id"]))
        rendered = Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text()
        self.assertIn('certs_ca_key_password = "";', rendered)
        self.assertIn("/smithproxy.assets/certs/default/", rendered)


if __name__ == "__main__":
    unittest.main()
