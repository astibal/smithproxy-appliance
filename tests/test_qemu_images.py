from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from runner.qemu_images import QemuImageLibrary
from runner.systemd import BackendError


class QemuImageLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.import_root = root / "import"
        self.import_root.mkdir()
        (self.import_root / "system.qcow2").write_bytes(b"QFI\xfb")
        (self.import_root / "data.qcow2").write_bytes(b"QFI\xfb")
        self.library = QemuImageLibrary(root / "library", self.import_root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_manifest_supports_multiple_disks_and_nics(self):
        item = self.library.create({
            "name": "inspection-box", "architecture": "x86_64", "machine": "q35",
            "disks": [
                {"source": "system.qcow2", "target": "vda", "role": "system"},
                {"source": "data.qcow2", "target": "vdb", "role": "data", "bus": "scsi"},
            ],
            "nics": [
                {"model": "virtio-net-pci", "purpose": "dataplane"},
                {"model": "e1000", "purpose": "telemetry"},
            ],
        })
        self.assertEqual(2, len(item["disks"]))
        self.assertEqual(2, len(item["nics"]))
        self.assertEqual([0, 1], [nic["slot"] for nic in item["nics"]])
        self.assertTrue(all(disk["base_read_only"] for disk in item["disks"]))
        self.assertTrue(all(disk["overlay"] == "per-instance" for disk in item["disks"]))
        self.assertEqual("discard-all-overlays", item["storage_policy"]["reset"])
        self.assertEqual("all-writable-disks", item["storage_policy"]["snapshot_scope"])
        self.assertTrue(item["forensic_policy"]["seal_before_reset"])
        self.assertEqual("immutable-export", item["forensic_policy"]["evidence"])
        self.assertIn("memory", item["forensic_policy"]["artifacts"])
        self.assertEqual(item["image_id"], self.library.get(item["image_id"])["image_id"])

    def test_forensic_policy_rejects_unknown_artifact(self):
        with self.assertRaisesRegex(BackendError, "unsupported forensic artifact"):
            self.library.create({"name": "bad", "disks": [{"source": "system.qcow2"}],
                                 "forensic": {"artifacts": ["host-secrets"]}})

    def test_source_cannot_escape_import_root(self):
        outside = Path(self.temporary.name, "outside.qcow2")
        outside.write_bytes(b"QFI\xfb")
        with self.assertRaisesRegex(BackendError, "below the QEMU import directory"):
            self.library.create({"name": "bad", "disks": [{"source": str(outside)}]})

    def test_disk_target_must_be_unique(self):
        with self.assertRaisesRegex(BackendError, "unique"):
            self.library.create({"name": "bad", "disks": [
                {"source": "system.qcow2", "target": "vda"},
                {"source": "data.qcow2", "target": "vda"},
            ]})


if __name__ == "__main__":
    unittest.main()
