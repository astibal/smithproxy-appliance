import tempfile
import unittest
from pathlib import Path
from runner.builder import SmithproxyBuilder, ROOTFS_SCHEMA
from runner.systemd import BackendError


class RootfsCertificateTests(unittest.TestCase):
    def test_store_is_self_contained_and_keeps_hash_links(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'host/etc/ssl/certs'
            source.mkdir(parents=True)
            external = base / 'host/usr/share/ca-certificates/test.crt'
            external.parent.mkdir(parents=True)
            external.write_text('test CA certificate')
            (source / 'test.pem').symlink_to(external)
            (source / '01234567.0').symlink_to('test.pem')
            (source / '89abcdef.0').symlink_to(source / 'test.pem')
            target = base / 'image/etc/ssl/certs'
            SmithproxyBuilder._copy_rootfs_certificates(source, target)
            external.unlink()
            self.assertFalse((target / 'test.pem').is_symlink())
            for name in ('01234567.0', '89abcdef.0'):
                self.assertTrue((target / name).is_symlink())
                self.assertEqual('test CA certificate', (target / name).read_text())
                self.assertTrue((target / name).resolve().is_relative_to(target))

    def test_broken_source_link_fails_instead_of_publishing_image(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'certs'
            source.mkdir()
            (source / 'missing.pem').symlink_to('/nonexistent-sas-test-ca.crt')
            with self.assertRaises(BackendError):
                SmithproxyBuilder._copy_rootfs_certificates(source, base / 'image')

    def test_old_images_require_rebuild(self):
        self.assertGreater(ROOTFS_SCHEMA, 3)
