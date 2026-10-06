"""Rebuild browser translations: python3 console/build-i18n.py."""
import json
from pathlib import Path
from i18n import CATALOGUE

script = "window.sasTr = (() => { const catalogue = " + json.dumps(CATALOGUE, ensure_ascii=False) + r""";
  const lang = document.documentElement.lang;
  return (key, values = {}) => (catalogue[lang]?.[key] ?? catalogue.en[key] ?? key)
    .replace(/\{([a-zA-Z0-9_]+)\}/g, (match, name) => Object.hasOwn(values, name) ? String(values[name]) : match);
})();
"""
Path(__file__).with_name("static").joinpath("ui-i18n.js").write_text(script, encoding="utf-8")
