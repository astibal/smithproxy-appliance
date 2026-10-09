import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock
from runner.app import Instance, Manager
from runner.config import ConfigError

class BuildWarningTests(unittest.TestCase):
    def test_persistence_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp); ident='11111111-1111-4111-8111-111111111111'
            manager=Manager(p/'state',p/'instances',p/'template',Mock())
            manager._save(Instance(ident,'unit','stopped','','',0))
            self.assertTrue(manager._load(ident).indicate_old_build)
            manager.set_build_warning(ident,False)
            other=Manager(p/'state',p/'instances',p/'template',Mock())
            self.assertFalse(other._load(ident).indicate_old_build)
            for invalid in (0,1,'false',None):
                with self.assertRaises(ConfigError): manager.set_build_warning(ident,invalid)
