"""Regression checks for console localization (including external browser assets)."""
import json
from pathlib import Path
import re
import subprocess
import sys
import unittest

CONSOLE = Path(__file__).resolve().parents[1] / 'console'
sys.path.insert(0, str(CONSOLE))
from app import create_app
from i18n import CATALOGUE, translate


class LocalizationTests(unittest.TestCase):
    def test_literal_catalogue_has_complete_languages_and_placeholders(self):
        catalog = json.loads((CONSOLE / 'ui-translations.json').read_text())
        self.assertEqual({'cs', 'en', 'fr'}, set(catalog))
        for lang in ('en', 'fr'):
            self.assertEqual(set(catalog['cs']), set(catalog[lang]))
            for key, source in catalog['cs'].items():
                self.assertTrue(catalog[lang][key].strip(), (lang, key))
                self.assertEqual(set(re.findall(r'\{\w+\}', source)),
                                 set(re.findall(r'\{\w+\}', catalog[lang][key])), (lang, key))

    def test_all_templates_compile_and_keys_exist(self):
        app = create_app({'TESTING': True, 'SECRET_KEY': 'localization-test'})
        for path in (CONSOLE / 'templates').glob('*.html'):
            app.jinja_env.get_template(path.name)
            for key in re.findall(r"_\('([^']+)'\)", path.read_text()):
                for lang in ('cs', 'en', 'fr'):
                    self.assertIn(key, CATALOGUE[lang], (path.name, lang, key))

    def test_browser_catalogue_is_up_to_date(self):
        path = CONSOLE / 'static/ui-i18n.js'
        before = path.read_bytes()
        subprocess.run([sys.executable, str(CONSOLE / 'build-i18n.py')], check=True)
        self.assertEqual(before, path.read_bytes(), 'Run python3 console/build-i18n.py')

    def test_editor_labels_follow_language(self):
        key = next(key for key, value in CATALOGUE['cs'].items() if value == 'Validující build')
        self.assertEqual('Validation build', translate('en', key))
        self.assertEqual('Build de validation', translate('fr', key))


if __name__ == '__main__':
    unittest.main()
