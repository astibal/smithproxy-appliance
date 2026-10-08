"""Serve only shared third-party assets, never another console's pages."""
from pathlib import Path
from flask import send_from_directory


def install(app):
    vendor = Path(__file__).parent / 'static' / 'vendor'

    @app.get('/static/vendor/<path:filename>')
    def shared_vendor(filename):
        return send_from_directory(vendor, filename)
