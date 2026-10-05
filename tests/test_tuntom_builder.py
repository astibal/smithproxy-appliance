import json
import tempfile
import unittest
from pathlib import Path

from runner.systemd import BackendError
from runner.tuntom_builder import TuntomBuilder


class TuntomBuilderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.builder = TuntomBuilder(
            root / "source", root / "builds", "https://example.invalid/tuntom.git",
            jobs=2,
        )

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, commit: str, ref: str = "master", kind: str = "Release") -> str:
        build_id = f"{commit}-{kind.lower()}"
        target = self.builder.library_dir / build_id
        target.mkdir(parents=True)
        adapter = target / "tuntom-divert-adapter"
        adapter.write_bytes(b"#!/bin/sh\n")
        adapter.chmod(0o755)
        tunnel = target / "tuntom"
        tunnel.write_bytes(b"#!/bin/sh\n")
        tunnel.chmod(0o755)
        (target / "metadata.json").write_text(json.dumps({
            "commit_id": commit, "ref": ref, "build_type": kind,
            "built_at": "2026-10-04T10:00:00+00:00",
            "commit_at": "2026-10-03T10:00:00+00:00",
        }), encoding="utf-8")
        return build_id

    def test_resolves_only_archived_executable_adapter(self):
        build_id = self.archive("a" * 40)
        self.assertEqual(
            self.builder.library_dir / build_id / "tuntom-divert-adapter",
            self.builder.resolve_adapter(build_id),
        )
        self.assertEqual(
            self.builder.library_dir / build_id / "tuntom",
            self.builder.resolve_tunnel(build_id),
        )
        with self.assertRaises(BackendError):
            self.builder.resolve_adapter("master")

    def test_branch_status_marks_current_and_update_available_builds(self):
        self.archive("a" * 40, "master")
        self.archive("b" * 40, "stable", "Debug")
        self.builder.refs_state = {
            "state": "ready", "refreshed_at": "now", "error": "",
            "branches": [
                {"name": "master", "commit_id": "c" * 40, "commit_at": "now"},
                {"name": "stable", "commit_id": "b" * 40, "commit_at": "then"},
            ],
        }
        branches = {item["name"]: item for item in self.builder.status()["refs"]["branches"]}
        self.assertTrue(branches["master"]["update_available"])
        self.assertEqual([], branches["master"]["current_types"])
        self.assertFalse(branches["stable"]["update_available"])
        self.assertEqual(["Debug"], branches["stable"]["current_types"])


if __name__ == "__main__":
    unittest.main()
