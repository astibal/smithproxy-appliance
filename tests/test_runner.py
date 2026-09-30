import json
import hashlib
import tempfile
import unittest
import uuid
import subprocess
import threading
import time
from pathlib import Path

from runner.app import Manager
from runner.builder import SmithproxyBuilder
from runner.config import ConfigError, render_template, validate_parameters
from runner.config_library import ConfigLibrary
from runner.config_previews import ConfigPreviewLibrary
from runner.namespace import NamespaceBackend, parse_unit_status
from runner.network_settings import NetworkSettings
from runner.runtime_profiles import RuntimeProfileLibrary
from runner.cert_library import CertBundleLibrary
from runner.systemd import BackendError, UnitStatus
from runner.task_queue import TaskQueue


class FakeBackend:
    def __init__(self):
        self.units = {}
        self.orphans = {}

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


class RunnerTests(unittest.TestCase):
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
        self.assertEqual("running", self.manager.get(item.id).state)
        config = Path(self.temp.name, "run", item.id, "smithproxy.cfg").read_text()
        self.assertIn('socks_port="1080"', config)
        self.manager.stop(item.id)
        self.assertFalse(Path(self.temp.name, "run", item.id).exists())
        stopped, snapshot = self.manager.config_content(item.id)
        self.assertEqual("stopped", stopped.state)
        self.assertIn('socks_port="1080"', snapshot)

    def test_rejects_duplicate_active_source(self):
        payload = {"runtime_seconds": 30, "source_ip": "198.51.100.10", "user_id": "test-user", "parameters": {"socks_port": 1080}}
        self.manager.create(payload)
        with self.assertRaises(ConfigError):
            self.manager.create(payload)

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
        self.assertEqual([], list(Path(self.temp.name, "run").glob("*")))

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
        item.pid = 999999
        item.resources_cleaned = True
        self.manager._save(item)
        current = self.manager.get(item.id)
        self.assertEqual("running", current.state)
        self.assertEqual(4242, current.pid)
        self.assertEqual(64 * 1024, current.rss_bytes)
        self.assertFalse(current.resources_cleaned)

    def test_reconcile_clears_dead_pid_and_marks_cleanup(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user",
            "parameters": {"socks_port": 1080},
        })
        item.pid = 4242
        self.manager._save(item)
        self.backend.units[item.unit] = "inactive"
        current = self.manager.get(item.id)
        self.assertEqual(0, current.pid)
        self.assertTrue(current.resources_cleaned)
        self.assertEqual(0, current.rss_bytes)

    def test_auto_restart_preserves_resources_and_records_crash(self):
        item = self.manager.create({
            "runtime_seconds": 30, "source_ip": "198.51.100.10",
            "user_id": "test-user", "auto_restart": True,
            "parameters": {"socks_port": 1080},
        })
        item.pid = 4242
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
        self.assertFalse(Path(self.temp.name, "state", f"{item.id}.cfg").exists())

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
        self.assertEqual(4242, discovered.pid)

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
        self.assertIn('dir = "/tmp/lease"', config)
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

    def test_restart_preserves_instance_container_and_resets_ttl(self):
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
        self.assertGreater(restarted.deadline, old_deadline)
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
        self.assertEqual("10.250.0.4/30", second.subnet)
        self.assertEqual("routed", first.egress_mode)
        self.assertEqual("eth1", first.sas_interface)
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
        item = library.create("Magic stable", "a" * 40, str(uuid.uuid4()))
        self.assertEqual("Magic stable", library.get(item["profile_id"])["name"])
        self.assertEqual("a" * 40, item["build_id"])
        updated = library.update(
            item["profile_id"], "Magic next", "b" * 40, str(uuid.uuid4()), "bundle-id"
        )
        self.assertEqual(item["profile_id"], updated["profile_id"])
        self.assertEqual("Magic next", updated["name"])
        self.assertEqual("b" * 40, updated["build_id"])
        self.assertTrue(updated["updated_at"])
        document = json.loads(Path(self.temp.name, "runtime-profiles.json").read_text())
        self.assertEqual(item["profile_id"], document["profiles"][0]["profile_id"])
        self.assertEqual(item["profile_id"], library.delete(item["profile_id"])["profile_id"])
        self.assertEqual([], library.list())

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
