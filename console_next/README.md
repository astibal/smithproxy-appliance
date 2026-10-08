# SAS Console Next

Independent Flask application and responsive browser workspace. It does not
import `console.app` or register legacy HTML form routes.

```
console/          legacy HTML console (temporary fallback)
console_next/     app.py, API allowlist, templates, browser workspace
console_shared/   authentication, admin JSON, runner client, terminal bridge,
                  translation catalogue and third-party browser assets
```

Run from the repository root with the console dependencies installed:

```
python -m pip install -r console_next/requirements.txt
gunicorn --workers 2 --threads 8 --worker-class gthread \
  --bind '[::]:6001' 'console_next.app:create_app()'
```

Production settings still come from the existing console/runner environment
files. Admin records remain outside the checkout in `/var/lib/sas-console`.
The `sas_next_session` cookie and secret remain unchanged across this migration,
so existing logins survive. The runner token is never sent to the browser.

`deploy/sas-console-next.service` uses the repository root as its working
directory. For this transition it reuses the already installed Python runtime
in `console/.venv`; there is no dependency on legacy application code. Before
deleting `console/`, install that runtime outside it and update the service path.
Do not move a virtualenv in place: its executable paths may be absolute.

Shared CodeMirror assets are rebuilt in `console_shared/` using `npm ci` and
`npm run build:editor`. Both consoles serve the shared vendor files at the
unchanged `/static/vendor/` URLs. Workspace assets belong only to `console_next`.

Tests: `python -m unittest discover -s tests`, plus
`for f in tests/test_next_*.cjs; do node "$f"; done` from the repository root.

The legacy `console.app.create_next_app()` factory remains a compatibility
import for existing operator tools; the service no longer uses it. The original
console remains on port 5000 until the operator chooses to retire it.
