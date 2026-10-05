# sasctl

`sasctl` is the headless command-line client for the runner API. It contains no
runner business logic: authorization, TTL, profile expansion, config validation,
routing and task deduplication remain authoritative on `sas-runner`.

## Authentication and contexts

The default config is `~/.config/sasctl/config.json`; override it with
`SASCTL_CONFIG` or `--config`. See `deploy/sasctl.config.example.json`.

Tokens are never accepted as command-line arguments. Configure a mode-0600
`token_file`, `SAS_RUNNER_TOKEN`, or `CZ_RUNNER_TOKEN`. HTTPS contexts support a
private CA and an optional mTLS certificate/key. `--insecure` exists only for
controlled diagnostics and must not be used in normal operation.

```bash
install -m 0755 sasctl /usr/local/bin/sasctl
install -d -m 0700 ~/.config/sasctl
install -m 0600 deploy/sasctl.config.example.json ~/.config/sasctl/config.json
```

## Common commands

Global flags precede the command. Add `--wait` when the caller needs the final
result rather than the queued task descriptor; `--output json` is stable for
automation.

```bash
sasctl --context local status
sasctl --context local --output json instance list
sasctl --context local task list
sasctl --context local task wait TASK_ID

sasctl --context local --wait instance spawn \
  --profile magic-sni-prod --source-ip 192.0.2.10 --user user-123 \
  --set REWRITE_SNI=magic.example --set REWRITE_SNI_TO=origin.example

sasctl --context local endpoint import --file fabric-endpoint.json
sasctl --context local endpoint list
sasctl --context local --wait instance spawn \
  --profile via-prod --headless-endpoint 10ea565a-8c05-41d4-989c-b6927cdf21ac \
  --source-ip 192.0.2.12 --user user-789

sasctl --context local --wait instance spawn \
  --build f31d127ff3d0 --config-id f7cda821 \
  --source-ip 192.0.2.11 --user user-456 --ttl 30m

sasctl --context local instance logs INSTANCE --lines 300
sasctl --context local instance cli INSTANCE
sasctl --context local instance command INSTANCE "show status"
sasctl --context local --wait instance extend INSTANCE 30m
sasctl --context local --wait instance stop INSTANCE

sasctl --context local --wait appliance-export create \
  --name proxy-lab --build master --config-id CONFIG --mode rootfs \
  --set REWRITE_SNI=magic.example --set REWRITE_SNI_TO=origin.example
sasctl --context local appliance-export list
sasctl --context local appliance-export download EXPORT --file proxy-lab.tar.gz
```

Objects may be selected by a full ID, an unambiguous ID prefix, or an exact
name where the object type has names. Destructive library/record deletion also
requires `--yes`.

Coverage groups:

```text
health, status, openapi
source list
task list|show|wait|result
instance list|show|spawn|stop|restart|extend|delete|logs|diagnostics
         config|config-preview|command|cli|debug-start|debug-stop|gdb
profile list|show|create|update|delete
build status|list|start|refs-refresh|extract-config|delete
config list|show|import|update|commit|cancel|diff-default|delete
cert list|show|create-ca|generate|import|download-ca|delete
network show|update
network-profile list|show|create|update|delete
endpoint list|show|import|delete
appliance-export list|create|download|delete
```

Interactive `instance cli` and `instance gdb` use the runner WebSocket directly
and place the local terminal in raw mode. For GDB, attach the scoped helper first
with `instance debug-start`. Press `Ctrl+]` to close a local terminal session.
