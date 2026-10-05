import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path

from runner.appliance_exports import ApplianceExportLibrary
from runner.systemd import BackendError


def inputs(tmp_path: Path):
    binary = tmp_path / "smithproxy"
    shutil.copy2("/bin/true", binary)
    binary.chmod(0o755)
    config = tmp_path / "smithproxy.cfg"
    config.write_text('settings={ plaintext_port="1"; }; marker="{{SITE}}";\n')
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "marker").write_text("asset")
    return binary, config, assets


class ApplianceExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_plain_export_contains_portable_control_scripts(self):
        binary, config, assets = inputs(self.root)
        library = ApplianceExportLibrary(self.root / "library")
        item = library.create(
            name="lab-one", build={"build_id": "a" * 40 + "-release",
            "commit_id": "a" * 40, "ref": "master", "build_type": "Release"},
            binary=binary, config=config, assets=assets, filesystem_mode="plain",
            rootfs=None, profile="custom", parameters={"SITE": "example.test"},
        )
        metadata, archive = library.archive(item["export_id"])
        self.assertEqual(metadata["sha256"], item["sha256"])
        with tarfile.open(archive, "r:gz") as bundle:
            names = set(bundle.getnames())
            self.assertIn("lab-one/start.sh", names)
            self.assertIn("lab-one/repack-from-binary.sh", names)
            self.assertIn("lab-one/bin/run-smithproxy", names)
            self.assertIn("lab-one/assets/marker", names)
            config_text = bundle.extractfile("lab-one/config/smithproxy.cfg").read().decode()
            self.assertIn("example.test", config_text)
            start = bundle.extractfile("lab-one/start.sh").read().decode()
            self.assertIn('peer name di0 netns "$NS"', start)
            self.assertIn('peer name do0 netns "$NS"', start)
            self.assertNotIn("iptables", start)
            extracted = self.root / "extracted"
            bundle.extractall(extracted, filter="data")
        repack_out = self.root / "repack-out"
        repack_out.mkdir()
        subprocess.run(
            [str(extracted / "lab-one/repack-from-binary.sh"), "/bin/echo", "lab-two"],
            cwd=repack_out, check=True, capture_output=True, text=True,
        )
        self.assertTrue((repack_out / "lab-two.tar.gz").is_file())
        self.assertTrue((repack_out / "lab-two/deps").is_dir())
        repacked_start = (repack_out / "lab-two/start.sh").read_text()
        self.assertIn("lab-two started", repacked_start)
        self.assertNotIn("lab-one started", repacked_start)
        self.assertEqual(library.delete(item["export_id"])["name"], "lab-one")
        self.assertEqual(library.list(), [])

    def test_export_rejects_invalid_name_and_missing_rootfs(self):
        binary, config, assets = inputs(self.root)
        library = ApplianceExportLibrary(self.root / "library")
        common = dict(build={}, binary=binary, config=config, assets=assets,
                      profile="custom", parameters={"SITE": "x"})
        with self.assertRaises(BackendError):
            library.create(name="bad/name", filesystem_mode="plain", rootfs=None, **common)
        with self.assertRaises(BackendError):
            library.create(name="good", filesystem_mode="rootfs", rootfs=None, **common)
