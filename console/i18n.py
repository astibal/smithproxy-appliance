"""Compatibility import for legacy scripts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from console_shared.i18n import LANGUAGES, CATALOGUE, translate
