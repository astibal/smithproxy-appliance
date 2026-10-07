# ELF program storage

Programs use a separate content-addressed library from Smithproxy builds:
`<runtime-profiles parent>/program-artifacts/<SHA-256>/` contains `program` and
`metadata.json`. New contents mean a new immutable version. Identical bytes
return the existing artifact, including its original metadata and filename.

Import through **Profiles → Storage · ELF programs**, either from an uploaded
file or an absolute path on the runner origin. Maximum size: 16 MiB. Import
is an asynchronous task, parses ELF metadata without executing the input, and
requires the origin's ELF architecture. Executables must be trusted: rootfs
and namespaces share the host kernel, they are not VM isolation.

Select an artifact and rootfs variant in an ELF program profile. Arguments
are passed directly, never through a shell. Programs must stay in the
foreground. The executable retains its imported filename under `/opt/program/`;
libraries are copied into an immutable rootfs snapshot. Only static ELF
dependencies are discovered: dlopen modules, data files, plugins and runtime
configuration may need additional packaging. No arbitrary host mounts or
automatic package installation are supported. Wiring, TTL, restart and `/work`
files use the existing managed-program lifecycle. `/work` and `/logs` are writable.

API (authenticated):

- `GET /v1/program-artifacts` → `{artifacts: [...]}`.
- `POST /v1/program-artifacts` → queued task; `name`, optional `version`, and
  exactly one of `path` (origin absolute path) or `content_base64` (upload).
  Uploads should also supply `filename` to preserve argv[0] semantics.
- `POST/PUT /v1/runtime-profiles[/<id>]`: `application: "elf"`,
  `program_settings: {artifact_id: "<SHA-256>", argv: ["--foreground"]}`,
  plus existing profile options. The selected version and rootfs remain pinned.

CLI equivalents:

```sh
sasctl --wait program-artifact import --path /usr/bin/example --name Example --version 1
sasctl --wait program-artifact import --file ./example --name Example
sasctl program-artifact list
sasctl --wait profile create Example --application elf \
  --artifact-id SHA256 --argv-json '["--foreground"]' --rootfs-variant barebone
```

Artifact deletion/garbage collection is not exposed yet; imports and older
versions remain available. Smithproxy stays in its existing build library.

## Restart policy

Profiles and standalone instance API requests accept
`auto_restart: {"on_exit": true, "on_failure": true}`. `on_exit` means successful
termination (`Restart=on-success`), `on_failure` means error/crash
(`Restart=on-failure`); both select `always`, neither selects `no`.
Legacy booleans map to the on-failure flag. GUI exposes two checkboxes; CLI
profile create/update accepts `--restart-on-exit yes|no` and
`--restart-on-failure yes|no`. Restart delay is two seconds with at most five
starts per sixty seconds. New-policy instances do not bypass systemd's
start limit with runner retries. Manual Stop and TTL remain authoritative.
Profile edits affect subsequent deployments, not already-running units.
