# systemd deployment

The two units are deliberately independent:

```text
sas-runner.service  (root, required)  <- private API -> Capture Zone Portal
        ^
        `-- sas-console.service (sas-console, optional admin UI)
```

Stopping or disabling `sas-console.service` has no effect on the runner or on
Smithproxy instances. A normal runner restart also preserves active transient
Smithproxy units, namespaces and routes. Full teardown remains explicit through
`CZ_RUNNER_STOP_INSTANCES_ON_EXIT=1` and must not be set in normal service
configuration.

## Files and identities

Expected production layout:

```text
/opt/smithproxy-appliance/                 application checkout, root-owned
/etc/smithproxy-appliance/runner.env       0600 root:root
/etc/smithproxy-appliance/runner-client.env 0600 root:root
/etc/smithproxy-appliance/console.env      0600 root:root
/etc/smithproxy-appliance/source-ips.json  0640 root:root
/etc/smithproxy-appliance/smithproxy.cfg.in 0640 root:root
/var/lib/smithproxy-appliance/             runner state, created by systemd
/var/lib/sas-console/                      admin JSON, created by systemd
```

The console account is a locked system identity with no shell. Example setup
commands are shown for a future appliance installation; do not run them on a
development workstation:

```bash
groupadd --system sas-console
useradd --system --gid sas-console --home-dir /nonexistent \
  --shell /usr/sbin/nologin sas-console

install -d -o root -g root -m 0755 /opt/smithproxy-appliance
install -d -o root -g root -m 0700 /etc/smithproxy-appliance
install -o root -g root -m 0600 deploy/runner.env.example \
  /etc/smithproxy-appliance/runner.env
install -o root -g root -m 0600 deploy/runner-client.env.example \
  /etc/smithproxy-appliance/runner-client.env
install -o root -g root -m 0600 deploy/console.env.example \
  /etc/smithproxy-appliance/console.env
```

Generate independent secrets for `CZ_RUNNER_TOKEN` and
`SMITHPROXY_APPLIANCE_CONSOLE_SECRET` with `openssl rand -hex 32`. The runner
token is the only secret shared between the services. The console never reads
the runner's root-only settings.

Install console dependencies, including the production Gunicorn server, in the
console virtual environment:

```bash
python3 -m venv /opt/smithproxy-appliance/console/.venv
/opt/smithproxy-appliance/console/.venv/bin/pip install \
  -r /opt/smithproxy-appliance/console/requirements.txt
```

## Unit installation

```bash
install -o root -g root -m 0644 deploy/sas-runner.service \
  /etc/systemd/system/sas-runner.service
install -o root -g root -m 0644 deploy/sas-console.service \
  /etc/systemd/system/sas-console.service
systemctl daemon-reload
systemctl enable --now sas-runner.service
systemctl enable --now sas-console.service  # optional
```

For a headless appliance, do not enable `sas-console.service`. The portal talks
directly to the runner API. Before binding the API to a private network address,
put authenticated TLS/mTLS transport in front of it; the runner itself currently
provides bearer authentication over plain HTTP/WebSocket.

Useful checks:

```bash
systemctl status sas-runner.service sas-console.service
journalctl -u sas-runner.service -u sas-console.service -f
systemd-analyze security sas-runner.service sas-console.service
```
