import tempfile
import unittest
from pathlib import Path

from runner.app import Manager
from runner.snapshots import SnapshotManager
from test_runner import FakeBackend


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        template = self.root / "source.cfg"
        template.write_text('settings={ socks_port="{{SOCKS_PORT}}"; };')
        self.backend = FakeBackend()
        self.manager = Manager(self.root / "state", self.root / "instances", template, self.backend)
        self.snapshots = SnapshotManager(self.root / "snapshots", self.manager)
        self.instance = self.manager.create({
            "source_ip": "192.0.2.9", "user_id": "snapshot-test",
            "runtime_seconds": 300, "config_mode": "rw",
            "parameters": {"socks_port": 1080},
        })

    def tearDown(self):
        self.temp.cleanup()

    def test_cold_snapshot_is_named_and_preserves_namespace(self):
        work = self.root / "instances" / self.instance.id
        (work / "data.txt").write_text("one")
        item = self.snapshots.create(self.instance.id, "baseline")
        self.assertEqual("cold", item["mode"])
        self.assertNotIn("deployment", item)
        self.assertNotIn("instance", item)
        self.assertEqual(["baseline"], item["snapshot_path"])
        self.assertEqual(self.instance.unit, self.backend.frozen_unit)
        self.assertEqual(self.instance.unit, self.backend.thawed_unit)
        self.assertEqual("active", self.backend.units[self.instance.unit])
        self.assertEqual("one", (self.snapshots._path(self.instance.id, item["snapshot_id"]) / "data" / "data.txt").read_text())

    def test_lineage_restore_and_drop_materialized_snapshot(self):
        work = self.root / "instances" / self.instance.id
        data = work / "data.txt"
        data.write_text("one")
        first = self.snapshots.create(self.instance.id, "one", "hot")
        data.write_text("two")
        second = self.snapshots.create(self.instance.id, "two", "hot")
        self.assertEqual(["one", "two"], second["snapshot_path"])
        restored = self.snapshots.restore(self.instance.id, first["snapshot_id"])
        self.assertEqual("one", data.read_text())
        self.assertEqual(["one"], restored["snapshot_path"])
        self.snapshots.delete(self.instance.id, first["snapshot_id"])
        self.assertEqual("two", (self.snapshots._path(self.instance.id, second["snapshot_id"]) / "data" / "data.txt").read_text())

    def test_stop_snapshot_stops_and_restarts_main_process(self):
        item = self.snapshots.create(self.instance.id, "stopped-copy", "stop")
        self.assertEqual("stop", item["mode"])
        self.assertEqual("active", self.backend.units[self.instance.unit])
        self.assertEqual(self.instance.id, self.backend.instance_upgrade["instance_id"])


if __name__ == "__main__":
    unittest.main()
