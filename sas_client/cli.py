from __future__ import annotations

import argparse
import json
import os
import re
import select
import sys
import termios
import threading
import tty
from pathlib import Path
from typing import Any

from websockets.sync.client import connect as websocket_connect
from websockets.exceptions import ConnectionClosed

from .api import APIError, RunnerClient
from .config import ConfigurationError, load_config
from .output import emit


INSTANCE_COLUMNS = [
    ("alias", "ALIAS"),
    ("id", "ID"), ("state", "STATE"), ("desired_state", "DESIRED"), ("source_ip", "SOURCE"),
    ("user_id", "USER"), ("profile", "CONFIG PROFILE"),
    ("slice_unit", "SLICE"), ("slice_rss_bytes", "SLICE RSS"),
    ("deadline", "DEADLINE"),
]
TASK_COLUMNS = [
    ("task_id", "ID"), ("state", "STATE"), ("kind", "KIND"),
    ("label", "LABEL"), ("created_at", "CREATED"),
]
PROFILE_COLUMNS = [
    ("profile_id", "ID"), ("name", "NAME"), ("ttl_seconds", "TTL"),
    ("build_id", "BUILD"), ("config_id", "CONFIG"), ("available", "READY"),
]
CONFIG_COLUMNS = [
    ("config_id", "ID"), ("name", "NAME"), ("profile", "PROFILE"),
    ("native", "NATIVE"), ("created_at", "CREATED"),
]
BUNDLE_COLUMNS = [
    ("bundle_id", "ID"), ("name", "NAME"), ("ca_common_name", "CA"),
    ("created_at", "CREATED"),
]
BUILD_COLUMNS = [
    ("build_id", "ID"), ("ref", "REF"), ("build_type", "TYPE"),
    ("commit_id", "COMMIT"), ("built_at", "BUILT"),
]
EXPORT_COLUMNS = [
    ("export_id", "ID"), ("name", "NAME"), ("filesystem_mode", "MODE"),
    ("ref", "REF"), ("commit_id", "COMMIT"), ("size_bytes", "SIZE"),
]


def duration(value: str, *, unlimited: bool = False) -> int | None:
    lowered = value.strip().lower()
    if unlimited and lowered in {"unlimited", "infinite", "none", "∞"}:
        return None
    match = re.fullmatch(r"([0-9]+)([smhd]?)", lowered)
    if not match:
        raise argparse.ArgumentTypeError("duration must be an integer with s/m/h/d suffix")
    factor = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    return int(match.group(1)) * factor


def ttl_duration(value: str) -> int | None:
    return duration(value, unlimited=True)


def assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"expected NAME=VALUE, got {value!r}")
        key, item = value.split("=", 1)
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", key):
            raise ValueError(f"invalid placeholder name {key!r}")
        result[key.upper()] = item
    return result


def resolve(items: list[dict[str, Any]], value: str, id_key: str,
            name_key: str = "name") -> dict[str, Any]:
    exact = [item for item in items if str(item.get(id_key, "")) == value]
    if len(exact) == 1:
        return exact[0]
    names = [item for item in items if str(item.get(name_key, "")) == value]
    if len(names) == 1:
        return names[0]
    prefixes = [item for item in items if str(item.get(id_key, "")).startswith(value)]
    if len(prefixes) == 1:
        return prefixes[0]
    if len(names) + len(prefixes) > 1:
        raise ValueError(f"ambiguous identifier or name: {value}")
    raise ValueError(f"object not found: {value}")


def complete(client: RunnerClient, value: Any, args: argparse.Namespace,
             force_wait: bool = False) -> Any:
    if isinstance(value, dict) and value.get("task_id") and (args.wait or force_wait):
        finished = client.wait_task(value["task_id"], timeout=args.task_timeout)
        return finished.get("completed_result")
    return value


def confirm(args: argparse.Namespace, what: str) -> None:
    if not getattr(args, "yes", False):
        raise ValueError(f"refusing to {what} without --yes")


def _terminal_send(connection: Any, data: bytes) -> bool:
    """Return false when the peer closed between stdin polling and send."""
    try:
        connection.send(data)
        return True
    except ConnectionClosed:
        return False


def terminal(client: RunnerClient, instance_id: str, kind: str) -> None:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError("interactive terminal requires a TTY")
    connection = websocket_connect(
        client.terminal_url(instance_id, kind),
        additional_headers={"Authorization": f"Bearer {client.config.token}"},
        ssl=client.ssl_context, open_timeout=client.config.timeout,
        compression=None, max_size=64 * 1024,
    )
    finished = threading.Event()

    def reader() -> None:
        try:
            for message in connection:
                data = message if isinstance(message, bytes) else message.encode()
                os.write(sys.stdout.fileno(), data)
        except ConnectionClosed:
            pass
        finally:
            finished.set()

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    old = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        previous_size = None
        while not finished.is_set():
            if kind == 'netns':
                size = os.get_terminal_size(sys.stdout.fileno())
                if size != previous_size:
                    if not _terminal_send(connection, json.dumps({'type': 'resize', 'cols': size.columns, 'rows': size.lines}).encode()):
                        break
                    previous_size = size
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.1)
            if not readable:
                continue
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                break
            if data == b"\x1d":  # Ctrl+]
                break
            if kind == 'netns':
                data = json.dumps({'type': 'input', 'data': data.decode('utf-8', 'replace')}).encode()
            if not _terminal_send(connection, data):
                break
    except KeyboardInterrupt:
        pass
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        try:
            connection.close()
        except ConnectionClosed:
            pass
        thread.join(timeout=2)


def _common_list(subparsers: Any, name: str, help_text: str) -> argparse.ArgumentParser:
    return subparsers.add_parser(name, help=help_text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sasctl", description="Smithproxy Appliance runner client")
    parser.add_argument("--config", type=Path, help="client JSON configuration")
    parser.add_argument("--context", default="", help="named client context")
    parser.add_argument("--url", default="", help="runner HTTP(S) URL")
    parser.add_argument("--ws-url", default="", help="runner WebSocket URL")
    parser.add_argument("--token-file", default="", help="protected runner token file")
    parser.add_argument("--insecure", action="store_true", default=None,
                        help="disable TLS certificate verification")
    parser.add_argument("--timeout", type=float, default=None, help="request timeout in seconds")
    parser.add_argument("--task-timeout", type=float, default=1800,
                        help="maximum --wait time in seconds")
    parser.add_argument("--wait", action="store_true", help="wait for asynchronous task completion")
    parser.add_argument("-o", "--output", choices=("table", "json"), default="table")
    groups = parser.add_subparsers(dest="group", required=True)

    groups.add_parser("health", help="check the unauthenticated health endpoint")
    groups.add_parser("status", help="show runner status")
    groups.add_parser("openapi", help="print runner OpenAPI document")
    drive = groups.add_parser('test-drive', help='Test Drive system microservices')
    drive_sub = drive.add_subparsers(dest='command', required=True)
    drive_check = drive_sub.add_parser('check-microservices')
    drive_check.add_argument('instance')
    drive_start = drive_sub.add_parser('system-start')
    drive_start.add_argument('instance')
    drive_start.add_argument('operation', choices=('status', 'enable', 'disable'))

    l2 = groups.add_parser("l2", help="Wiring: isolated cables, switches and port addressing")
    l2_sub = l2.add_subparsers(dest="command", required=True)
    l2_sub.add_parser("list")
    l2_sub.add_parser('addressing')
    create_l2 = l2_sub.add_parser("create")
    create_l2.add_argument("kind", choices=("virtual-cable", "virtual-switch"))
    create_l2.add_argument("name")
    for command in ("show", "delete", "attach", "detach", "configure-port"):
        sub = l2_sub.add_parser(command)
        sub.add_argument("segment", help="UUID, unique prefix, or exact name")
        if command == "attach":
            sub.add_argument("instance", help="managed instance ID or alias")
            sub.add_argument("interface", help="new unaddressed interface, e.g. cable0")
        elif command == "detach":
            sub.add_argument("endpoint", help="endpoint UUID")
        elif command == 'configure-port':
            sub.add_argument('endpoint', help='endpoint UUID')
            sub.add_argument('--mode', required=True, choices=['sas', 'guest', 'none'])
            sub.add_argument('--address', action='append', default=[], help='IP/prefix; repeat for dual stack')
            sub.add_argument('--route', action='append', default=[], help='"destination/prefix [gateway]"; repeatable')
        if command in {"delete", "detach"}:
            sub.add_argument("--yes", action="store_true")

    source = groups.add_parser("source", help="authorized source IP pool")
    source_sub = source.add_subparsers(dest="command", required=True)
    source_sub.add_parser("list")

    task = groups.add_parser("task", help="asynchronous tasks")
    task_sub = task.add_subparsers(dest="command", required=True)
    task_sub.add_parser("list")
    for name in ("show", "wait", "result"):
        item = task_sub.add_parser(name)
        item.add_argument("task")

    instance = groups.add_parser("instance", help="Smithproxy instances")
    instance_sub = instance.add_subparsers(dest="command", required=True)
    instance_sub.add_parser("list")
    check_services = instance_sub.add_parser("check-microservices", help="queue a check of this instance only")
    check_services.add_argument("instance")
    start_service = instance_sub.add_parser('system-start', help='show, enable or disable reserved 00-start')
    start_service.add_argument('instance')
    start_service.add_argument('operation', choices=('status', 'enable', 'disable'))
    alias = instance_sub.add_parser("alias", help="set a unique persistent alias; empty name clears it")
    alias.add_argument("instance")
    alias.add_argument("name")
    microservice = instance_sub.add_parser('microservice', help='Fabric V3 service status / verified stop')
    microservice.add_argument('instance')
    microservice.add_argument('prefix')
    microservice.add_argument('operation', choices=('status', 'stop'))
    microservice.add_argument('--run-id')
    microservice.add_argument('--owner')
    location = instance_sub.add_parser("location", help="host coordinates (read-only)")
    location.add_argument("instance")
    fields = location.add_mutually_exclusive_group()
    for field in ("namespace", "namespace-path", "transport-namespace", "transport-namespace-path", "work-dir", "microservices-dir", "unit", "slice", "state", "origin"):
        fields.add_argument("--" + field, dest="location_field", action="store_const", const=field.replace("-", "_"))
    for name in ("show", "diagnostics", "config", "cli", "gdb", "netns"):
        item = instance_sub.add_parser(name)
        item.add_argument("instance")
    logs = instance_sub.add_parser("logs")
    logs.add_argument("instance")
    logs.add_argument("--lines", type=int, default=200)
    spawn = instance_sub.add_parser("spawn")
    selection = spawn.add_mutually_exclusive_group(required=True)
    selection.add_argument("--profile", help="runtime profile ID, prefix, or exact name")
    selection.add_argument("--build", help="standalone build ID or prefix")
    spawn.add_argument("--config-id", help="standalone config ID or prefix")
    spawn.add_argument("--cert-bundle", default="", help="certificate bundle for standalone mode")
    spawn.add_argument("--source-ip", default="", help="required for Authorized veth only")
    spawn.add_argument("--ingress-driver", choices=("authorized-veth", "unlimited-veth", "none"))
    spawn.add_argument("--egress-driver", choices=("veth-out", "none"))
    spawn.add_argument("--user", required=True)
    spawn.add_argument("--ttl", type=lambda value: duration(value), default=1800)
    spawn.add_argument("--config-mode", choices=("ro", "rw"), default="ro")
    spawn.add_argument('--no-system-start', action='store_true', help='disable reserved 00-start reconciliation')
    spawn.add_argument('--wiring-file', type=Path, help='JSON list of Wiring bindings; overrides profile, [] clears')
    spawn.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    spawn.add_argument("--workers", type=int, default=1)
    spawn.add_argument("--pcap-quota-mb", type=int, default=100)
    spawn.add_argument("--socks-port", type=int, default=1080)
    spawn.add_argument("--http-port", type=int, default=3128)
    spawn.add_argument("--plaintext-port", type=int, default=50080)
    spawn.add_argument("--tls-port", type=int, default=50443)
    spawn.add_argument("--cli-port", type=int, default=50000)
    spawn.add_argument("--tuntom-local-ip")
    spawn.add_argument("--tuntom-peer-ip")
    spawn.add_argument("--tuntom-peer-host")
    spawn.add_argument(
        "--headless-endpoint",
        help="unique Fabric endpoint package UUID (required by tuntom-via profiles)",
    )
    spawn.add_argument(
        "--tuntom-secret-file", type=Path,
        help="read the ephemeral 32-hex secret from a file (not from argv)",
    )
    for name in ("stop", "restart", "delete", "debug-start", "debug-stop"):
        item = instance_sub.add_parser(name)
        item.add_argument("instance")
        if name in {"delete"}:
            item.add_argument("--yes", action="store_true")
    extend = instance_sub.add_parser("extend")
    extend.add_argument("instance")
    extend.add_argument("duration", type=lambda value: duration(value))
    command = instance_sub.add_parser("command", help="execute one CLI command without a TTY")
    command.add_argument("instance")
    command.add_argument("text")
    preview = instance_sub.add_parser("config-preview")
    preview.add_argument("instance")
    preview.add_argument("--name", default="")
    preview.add_argument("--description", default="")
    preview.add_argument("--approve", action="store_true")
    preview.add_argument("--approved-by", default="sasctl")

    profile = groups.add_parser("profile", help="runtime profiles")
    profile_sub = profile.add_subparsers(dest="command", required=True)
    profile_sub.add_parser("list")
    show = profile_sub.add_parser("show")
    show.add_argument("profile")
    create = profile_sub.add_parser("create")
    create.add_argument("name")
    create.add_argument("--build")
    create.add_argument("--config-id")
    create.add_argument('--application', choices=('smithproxy', 'router', 'webfsd'), default='smithproxy')
    create.add_argument('--http-port', type=int, default=8000)
    create.add_argument('--rootfs-variant', choices=('barebone', 'utils', 'network'), default='barebone')
    create.add_argument("--cert-bundle", default="")
    create.add_argument("--ttl", type=ttl_duration, default=1800)
    create.add_argument("--auto-restart", action="store_true")
    create.add_argument('--wiring-file', type=Path, help='JSON list of segment_id/interface bindings, no addresses')
    update = profile_sub.add_parser("update")
    update.add_argument("profile")
    update.add_argument("--name")
    update.add_argument('--http-port', type=int)
    update.add_argument('--rootfs-variant', choices=('barebone', 'utils', 'network'))
    update.add_argument("--build")
    update.add_argument("--config-id")
    update.add_argument("--cert-bundle")
    update.add_argument("--ttl", type=ttl_duration)
    update.add_argument("--auto-restart", choices=("yes", "no"))
    update.add_argument('--wiring-file', type=Path, help='JSON list replacing Wiring bindings; [] clears')
    delete = profile_sub.add_parser("delete")
    delete.add_argument("profile")
    delete.add_argument("--yes", action="store_true")

    build = groups.add_parser("build", help="source builds")
    build_sub = build.add_subparsers(dest="command", required=True)
    build_sub.add_parser("status")
    build_sub.add_parser("list")
    start = build_sub.add_parser("start")
    start.add_argument("ref")
    start.add_argument("--type", choices=("Release", "Debug", "RelWithDebInfo", "MinSizeRel"), default="Release")
    build_sub.add_parser("refs-refresh")
    for name in ("delete", "extract-config"):
        item = build_sub.add_parser(name)
        item.add_argument("build")
        if name == "delete":
            item.add_argument("--yes", action="store_true")

    tuntom = groups.add_parser("tuntom", help="Tuntom adapter builds")
    tuntom_sub = tuntom.add_subparsers(dest="command", required=True)
    tuntom_sub.add_parser("status")
    tuntom_sub.add_parser("list")
    tuntom_sub.add_parser("refs-refresh")
    tuntom_start = tuntom_sub.add_parser("build")
    tuntom_start.add_argument("ref")
    tuntom_start.add_argument("--type", choices=("Release", "Debug"), default="Release")
    tuntom_delete = tuntom_sub.add_parser("delete")
    tuntom_delete.add_argument("build")
    tuntom_delete.add_argument("--yes", action="store_true")

    config = groups.add_parser("config", help="native configuration library")
    config_sub = config.add_subparsers(dest="command", required=True)
    config_sub.add_parser("list")
    show = config_sub.add_parser("show")
    show.add_argument("config")
    for name in ("import", "update"):
        item = config_sub.add_parser(name)
        if name == "update":
            item.add_argument("config")
        item.add_argument("--file", type=Path, required=True)
        item.add_argument("--build", required=True, help="exact validating build ID or prefix")
        item.add_argument("--name", required=True)
        item.add_argument("--description", default="")
        item.add_argument("--profile", default="custom")
        item.add_argument("--approve", action="store_true")
        item.add_argument("--approved-by", default="sasctl")
    commit = config_sub.add_parser("commit")
    commit.add_argument("preview")
    commit.add_argument("--approved-by", default="sasctl")
    cancel = config_sub.add_parser("cancel")
    cancel.add_argument("preview")
    delete = config_sub.add_parser("delete")
    delete.add_argument("config")
    delete.add_argument("--yes", action="store_true")
    observer = config_sub.add_parser("diff-default")
    observer.add_argument("config")
    observer.add_argument("--build", required=True)

    cert = groups.add_parser("cert", help="CA and certificate bundles")
    cert_sub = cert.add_subparsers(dest="command", required=True)
    cert_sub.add_parser("list")
    show = cert_sub.add_parser("show")
    show.add_argument("bundle")
    ca = cert_sub.add_parser("create-ca")
    ca.add_argument("name")
    ca.add_argument("--common-name", required=True)
    ca.add_argument("--days", type=int, default=3650)
    generate = cert_sub.add_parser("generate")
    generate.add_argument("bundle")
    generate.add_argument("name")
    generate.add_argument("--common-name", required=True)
    generate.add_argument("--days", type=int, default=825)
    generate.add_argument("--file-stem", default="")
    imported = cert_sub.add_parser("import")
    imported.add_argument("bundle")
    imported.add_argument("name")
    imported.add_argument("--file", type=Path, required=True)
    imported.add_argument("--filename", default="")
    download = cert_sub.add_parser("download-ca")
    download.add_argument("bundle")
    download.add_argument("--file", type=Path)
    delete = cert_sub.add_parser("delete")
    delete.add_argument("bundle")
    delete.add_argument("--yes", action="store_true")

    network = groups.add_parser("network", help="routing and address allocation")
    network_sub = network.add_subparsers(dest="command", required=True)
    network_sub.add_parser("show")
    update = network_sub.add_parser("update")
    update.add_argument("--file", type=Path, required=True, help="complete networking JSON document")

    network_profile = groups.add_parser("network-profile", help="ingress and egress profiles")
    network_profile_sub = network_profile.add_subparsers(dest="command", required=True)
    network_profile_sub.add_parser("list")
    network_profile_show = network_profile_sub.add_parser("show")
    network_profile_show.add_argument("profile")
    network_profile_create = network_profile_sub.add_parser("create")
    network_profile_create.add_argument("--file", type=Path, required=True)
    network_profile_update = network_profile_sub.add_parser("update")
    network_profile_update.add_argument("profile")
    network_profile_update.add_argument("--file", type=Path, required=True)
    network_profile_delete = network_profile_sub.add_parser("delete")
    network_profile_delete.add_argument("profile")
    network_profile_delete.add_argument("--yes", action="store_true")

    endpoint = groups.add_parser("endpoint", help="unique headless Fabric packages")
    endpoint_sub = endpoint.add_subparsers(dest="command", required=True)
    endpoint_sub.add_parser("list")
    endpoint_show = endpoint_sub.add_parser("show")
    endpoint_show.add_argument("package")
    endpoint_import = endpoint_sub.add_parser("import")
    endpoint_import.add_argument("--file", type=Path, required=True)
    endpoint_delete = endpoint_sub.add_parser("delete")
    endpoint_delete.add_argument("package")
    endpoint_delete.add_argument("--yes", action="store_true")

    appliance_export = groups.add_parser("appliance-export", help="portable appliance archives")
    export_sub = appliance_export.add_subparsers(dest="command", required=True)
    export_sub.add_parser("list")
    export_create = export_sub.add_parser("create")
    export_create.add_argument("--name", required=True)
    export_create.add_argument("--build", required=True)
    export_create.add_argument("--config-id", required=True)
    export_create.add_argument("--mode", choices=("plain", "rootfs"), default="plain")
    export_create.add_argument("--set", action="append", default=[], metavar="NAME=VALUE")
    export_download = export_sub.add_parser("download")
    export_download.add_argument("export")
    export_download.add_argument("--file", type=Path)
    export_delete = export_sub.add_parser("delete")
    export_delete.add_argument("export")
    export_delete.add_argument("--yes", action="store_true")
    return parser


def _id(client: RunnerClient, kind: str, value: str) -> str:
    if kind == "instance":
        return resolve(client.get("/v1/instances")["instances"], value, "id", "alias")["id"]
    if kind == "profile":
        return resolve(client.get("/v1/runtime-profiles")["profiles"], value, "profile_id")["profile_id"]
    if kind == "config":
        return resolve(client.get("/v1/configs")["configs"], value, "config_id")["config_id"]
    if kind == "bundle":
        return resolve(client.get("/v1/cert-bundles")["bundles"], value, "bundle_id")["bundle_id"]
    if kind == "build":
        artifacts = client.get("/v1/build").get("artifacts", [])
        return resolve(artifacts, value, "build_id", "ref").get("build_id")
    if kind == "tuntom-build":
        artifacts = client.get("/v1/tuntom/build").get("artifacts", [])
        return resolve(artifacts, value, "build_id", "ref").get("build_id")
    if kind == "network-profile":
        profiles = client.get("/v1/network-profiles").get("profiles", [])
        return resolve(profiles, value, "network_profile_id")["network_profile_id"]
    if kind == "endpoint":
        packages = client.get("/v1/headless-endpoints").get("packages", [])
        return resolve(packages, value, "package_id", "name")["package_id"]
    raise ValueError(kind)


def _instance(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    command = args.command
    if command == "location":
        from urllib.parse import quote
        result = client.get(f"/v1/instances/{quote(args.instance, safe='')}/location")
        field = args.location_field
        if field:
            if field in {"namespace", "namespace_path"} and not result.get("namespace_exists"):
                raise ValueError("instance network namespace is unavailable")
            if field in {"transport_namespace", "transport_namespace_path"} and not result.get("transport_namespace_exists"):
                raise ValueError("instance transport namespace is unavailable")
            if field == "work_dir" and not result.get("work_dir_exists"):
                raise ValueError("instance work directory is unavailable")
            if field == "microservices_dir" and not result.get("microservices_dir_exists"):
                raise ValueError("instance microservices directory has not been provisioned")
            if not result.get(field):
                raise ValueError(f"instance {field} is unavailable")
            return result[field], None
        return result, None
    if command == "list":
        values = client.get("/v1/instances")["instances"]
        return values, INSTANCE_COLUMNS
    # Historical service runs remain queryable after the instance was deleted.
    instance_id = (args.instance if command == 'microservice' and re.fullmatch(
        r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', args.instance)
        else _id(client, "instance", getattr(args, "instance", "")) if command != "spawn" else "")
    if command == "alias":
        return client.post(f"/v1/instances/{instance_id}/alias", {"alias": args.name}), None
    if command == 'check-microservices':
        return complete(client, client.post(f'/v1/instances/{instance_id}/microservices/check', {}), args), None
    if command == 'system-start':
        path = f'/v1/instances/{instance_id}/microservices/00'
        if args.operation == 'status':
            return client.get(path), None
        return complete(client, client.post(path + '/configure', {'enabled': args.operation == 'enable'}), args), None
    if command == 'microservice':
        if not re.fullmatch(r'[1-9][0-9]{0,8}', args.prefix):
            raise ValueError('invalid microservice prefix')
        path = f'/v1/instances/{instance_id}/microservices/{args.prefix}'
        if args.operation == 'status':
            return client.get(path, {'run_id': args.run_id} if args.run_id else {}), None
        if not args.owner or not args.run_id:
            raise ValueError('stop requires --owner and --run-id; withdraw registration first')
        return client.request('POST', path + '/stop',
            {'contract_version': 3, 'owner': args.owner, 'run_id': args.run_id}, timeout=35), None
    if command == "show": return client.get(f"/v1/instances/{instance_id}"), None
    if command == "diagnostics": return client.get(f"/v1/instances/{instance_id}/diagnostics"), None
    if command == "logs": return client.get(f"/v1/instances/{instance_id}/logs", {"lines": args.lines})["output"], None
    if command == "config": return client.get(f"/v1/instances/{instance_id}/config")["content"], None
    if command in {"cli", "gdb", "netns"}:
        if command == 'netns':
            print('WARNING: root shell on HOST filesystem; only networking is isolated.', file=sys.stderr)
        terminal(client, instance_id, command)
        return None, None
    if command == "spawn":
        tuntom_secret = os.environ.get("SAS_TUNTOM_SECRET", "")
        if args.tuntom_secret_file:
            tuntom_secret = args.tuntom_secret_file.read_text(encoding="ascii").strip()
        payload: dict[str, Any] = {
            "source_ip": args.source_ip, "user_id": args.user,
            "runtime_seconds": args.ttl, "config_mode": args.config_mode,
            "system_start_enabled": not args.no_system_start,
            "template_values": assignments(args.set),
            "parameters": {
                "workers": args.workers, "pcap_quota_mb": args.pcap_quota_mb,
                "socks_port": args.socks_port, "http_port": args.http_port,
                "plaintext_port": args.plaintext_port, "tls_port": args.tls_port,
                "cli_port": args.cli_port,
            },
        }
        for side in ("ingress", "egress"):
            driver = getattr(args, f"{side}_driver", None)
            if driver:
                payload[f"network_{side}_driver"] = driver
        if args.wiring_file:
            payload['wiring'] = json.loads(args.wiring_file.read_text())
        payload["network_runtime"] = {
            name: value
            for name, value in (
                ("tuntom_local_ip", args.tuntom_local_ip),
                ("tuntom_peer_ip", args.tuntom_peer_ip),
                ("tuntom_peer_host", args.tuntom_peer_host),
                ("tuntom_secret", tuntom_secret),
                ("headless_endpoint_id", args.headless_endpoint),
            )
            if value
        }
        if args.profile:
            payload["runtime_profile_id"] = _id(client, "profile", args.profile)
            payload.update({"build_id": "active", "config_id": "active"})
        else:
            if not args.config_id:
                raise ValueError("standalone spawn requires --config-id")
            payload["build_id"] = _id(client, "build", args.build)
            payload["config_id"] = _id(client, "config", args.config_id)
            if args.cert_bundle:
                payload["cert_bundle_id"] = _id(client, "bundle", args.cert_bundle)
        return complete(client, client.post("/v1/instances", payload), args), None
    if command == "extend":
        value = client.post(f"/v1/instances/{instance_id}/extend", {"additional_seconds": args.duration})
        return complete(client, value, args), None
    if command == "command":
        return client.post(f"/v1/instances/{instance_id}/cli", {"input": args.text + "\r"}), None
    if command == "config-preview":
        value = client.enqueue("POST", f"/v1/instances/{instance_id}/config/preview", {
            "name": args.name or f"Instance {instance_id[:12]}", "description": args.description,
        }, f"Native-save config {instance_id[:12]}", "instance-config-preview")
        result = complete(client, value, args, force_wait=args.approve)
        if args.approve:
            result = complete(client, client.enqueue("POST", "/v1/configs/commit", {
                "preview_id": result["preview_id"], "approved": True,
                "approved_by": args.approved_by,
            }, "Commit instance config", "config-commit"), args, force_wait=True)
        return result, None
    paths = {
        "stop": ("DELETE", f"/v1/instances/{instance_id}"),
        "restart": ("POST", f"/v1/instances/{instance_id}/restart"),
        "delete": ("DELETE", f"/v1/instances/{instance_id}/record"),
        "debug-start": ("POST", f"/v1/instances/{instance_id}/debug"),
        "debug-stop": ("DELETE", f"/v1/instances/{instance_id}/debug"),
    }
    if command == "delete": confirm(args, "delete the instance record")
    method, path = paths[command]
    value = client.enqueue(method, path, {}, f"{command} {instance_id[:12]}", f"instance-{command}")
    return complete(client, value, args), None


def _profile(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    profiles = client.get("/v1/runtime-profiles")["profiles"]
    if args.command == "list": return profiles, PROFILE_COLUMNS
    profile_id = _id(client, "profile", args.profile) if hasattr(args, "profile") else ""
    if args.command == "show": return client.get(f"/v1/runtime-profiles/{profile_id}"), None
    if args.command == "delete":
        confirm(args, "delete the runtime profile")
        value = client.enqueue("DELETE", f"/v1/runtime-profiles/{profile_id}", None,
                               f"Delete profile {profile_id[:12]}", "profile-delete")
        return complete(client, value, args), None
    if args.command == "create":
        if args.application == 'smithproxy' and (not args.build or not args.config_id):
            raise ValueError('Smithproxy requires --build and --config-id')
        if args.application != 'smithproxy' and (args.build or args.config_id or args.cert_bundle):
            raise ValueError('program profiles do not use Smithproxy artifacts')
        payload = {
            "name": args.name, "build_id": _id(client, "build", args.build) if args.build else '',
            "config_id": _id(client, "config", args.config_id) if args.config_id else '',
            "cert_bundle_id": _id(client, "bundle", args.cert_bundle) if args.cert_bundle else "",
            "ttl_seconds": args.ttl, "auto_restart": args.auto_restart,
            "rootfs_variant": args.rootfs_variant,
        }
        if args.application != 'smithproxy':
            payload.update(application=args.application, filesystem_mode='rootfs',
                           program_settings={'port': args.http_port} if args.application == 'webfsd' else {})
        if args.wiring_file:
            payload['wiring'] = json.loads(args.wiring_file.read_text())
        value = client.enqueue("POST", "/v1/runtime-profiles", payload,
                               f"Create profile {args.name}", "profile-create")
        return complete(client, value, args), None
    current = client.get(f"/v1/runtime-profiles/{profile_id}")
    payload = {
        "name": args.name if args.name is not None else current["name"],
        "rootfs_variant": args.rootfs_variant or current.get('rootfs_variant', 'barebone'),
        "filesystem_mode": current.get('filesystem_mode', 'host'),
        "build_id": _id(client, "build", args.build) if args.build else current["build_id"],
        "config_id": _id(client, "config", args.config_id) if args.config_id else current["config_id"],
        "cert_bundle_id": (_id(client, "bundle", args.cert_bundle) if args.cert_bundle else "")
        if args.cert_bundle is not None else current.get("cert_bundle_id", ""),
        "ttl_seconds": args.ttl if args.ttl is not None else current.get("ttl_seconds", 1800),
        "auto_restart": (args.auto_restart == "yes") if args.auto_restart is not None
        else current.get("auto_restart", False),
    }
    if args.wiring_file:
        payload['wiring'] = json.loads(args.wiring_file.read_text())
    if current.get('application', 'smithproxy') != 'smithproxy':
        payload.update(application=current['application'], filesystem_mode='rootfs',
                       program_settings=current.get('program_settings', {}))
        if args.http_port is not None:
            payload['program_settings'] = {'port': args.http_port}
    value = client.enqueue("PUT", f"/v1/runtime-profiles/{profile_id}", payload,
                           f"Update profile {profile_id[:12]}", "profile-update")
    return complete(client, value, args), None


def _build(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    status = client.get("/v1/build")
    if args.command == "status": return status, None
    if args.command == "list": return status.get("artifacts", []), BUILD_COLUMNS
    if args.command == "start":
        return complete(client, client.post("/v1/build", {"ref": args.ref, "build_type": args.type}), args), None
    if args.command == "refs-refresh":
        return complete(client, client.post("/v1/refs/refresh", {}), args), None
    build_id = _id(client, "build", args.build)
    if args.command == "delete":
        confirm(args, "delete the archived build")
        value = client.enqueue("DELETE", f"/v1/builds/{build_id}", None,
                               f"Delete build {build_id[:12]}", "build-delete")
    else:
        value = client.enqueue("POST", f"/v1/builds/{build_id}/config/preview", {},
                               f"Extract config {build_id[:12]}", "build-config-preview")
    return complete(client, value, args), None


def _tuntom(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    status = client.get("/v1/tuntom/build")
    if args.command == "status":
        return status, None
    if args.command == "list":
        return status.get("artifacts", []), BUILD_COLUMNS
    if args.command == "build":
        value = client.post("/v1/tuntom/build", {
            "ref": args.ref, "build_type": args.type,
        })
        return complete(client, value, args), None
    if args.command == "refs-refresh":
        return complete(client, client.post("/v1/tuntom/refs/refresh", {}), args), None
    build_id = _id(client, "tuntom-build", args.build)
    confirm(args, "delete the archived Tuntom build")
    value = client.enqueue(
        "DELETE", f"/v1/tuntom/builds/{build_id}", None,
        f"Delete Tuntom build {build_id[:12]}", "tuntom-build-delete",
    )
    return complete(client, value, args), None


def _network_profile(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    if args.command == "list":
        return client.get("/v1/network-profiles")["profiles"], [
            ("network_profile_id", "ID"), ("kind", "KIND"),
            ("name", "NAME"), ("driver", "DRIVER"),
            ("address_family", "FAMILY"), ("implemented", "READY"),
        ]
    profile_id = _id(client, "network-profile", args.profile) \
        if hasattr(args, "profile") else ""
    if args.command == "show":
        return client.get(f"/v1/network-profiles/{profile_id}"), None
    if args.command == "delete":
        confirm(args, "delete the network profile")
        value = client.enqueue(
            "DELETE", f"/v1/network-profiles/{profile_id}", None,
            f"Delete network profile {profile_id[:12]}", "network-profile-delete",
        )
        return complete(client, value, args), None
    payload = json.loads(args.file.read_text(encoding="utf-8"))
    method = "POST" if args.command == "create" else "PUT"
    path = "/v1/network-profiles" if args.command == "create" \
        else f"/v1/network-profiles/{profile_id}"
    value = client.enqueue(
        method, path, payload,
        f"{args.command.title()} network profile", f"network-profile-{args.command}",
    )
    return complete(client, value, args), None


def _endpoint(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    if args.command == "list":
        return client.get("/v1/headless-endpoints")["packages"], [
            ("package_id", "ID"), ("state", "STATE"), ("name", "NAME"),
            ("fabric_port_id", "FABRIC PORT"), ("switch_ip", "SWITCH"),
            ("tunnel_id", "TUNNEL"), ("bound_instance_id", "INSTANCE"),
        ]
    package_id = _id(client, "endpoint", args.package) \
        if hasattr(args, "package") else ""
    if args.command == "show":
        return client.get(f"/v1/headless-endpoints/{package_id}"), None
    if args.command == "delete":
        confirm(args, "delete the available headless endpoint package")
        return client.delete(f"/v1/headless-endpoints/{package_id}"), None
    payload = json.loads(args.file.read_text(encoding="utf-8"))
    return client.post("/v1/headless-endpoints", payload), None


def _config(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    if args.command == "list": return client.get("/v1/configs")["configs"], CONFIG_COLUMNS
    if args.command == "commit":
        value = client.enqueue("POST", "/v1/configs/commit", {
            "preview_id": args.preview, "approved": True, "approved_by": args.approved_by,
        }, "Commit native config", "config-commit")
        return complete(client, value, args), None
    if args.command == "cancel": return client.delete(f"/v1/configs/previews/{args.preview}"), None
    config_id = _id(client, "config", args.config) if hasattr(args, "config") else ""
    if args.command == "show": return client.get(f"/v1/configs/{config_id}"), None
    if args.command == "delete":
        confirm(args, "delete the configuration")
        value = client.enqueue("DELETE", f"/v1/configs/{config_id}", None,
                               f"Delete config {config_id[:12]}", "config-delete")
        return complete(client, value, args), None
    if args.command == "diff-default":
        value = client.enqueue("POST", "/v1/config-observer", {
            "config_id": config_id, "build_id": _id(client, "build", args.build),
        }, "Diff config against build default", "config-observer")
        return complete(client, value, args), None
    content = args.file.read_text(encoding="utf-8")
    payload = {
        "action": "update" if args.command == "update" else "create",
        "config_id": config_id, "name": args.name, "description": args.description,
        "profile": args.profile, "content": content,
        "build_id": _id(client, "build", args.build),
    }
    value = client.enqueue("POST", "/v1/configs/preview", payload,
                           f"Preview config {args.name}", "config-preview")
    result = complete(client, value, args, force_wait=args.approve)
    if args.approve:
        result = complete(client, client.enqueue("POST", "/v1/configs/commit", {
            "preview_id": result["preview_id"], "approved": True,
            "approved_by": args.approved_by,
        }, f"Commit config {args.name}", "config-commit"), args, force_wait=True)
    return result, None


def _cert(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    if args.command == "list": return client.get("/v1/cert-bundles")["bundles"], BUNDLE_COLUMNS
    bundle_id = _id(client, "bundle", args.bundle) if hasattr(args, "bundle") else ""
    if args.command == "show": return client.get(f"/v1/cert-bundles/{bundle_id}"), None
    if args.command == "download-ca":
        content = client.get(f"/v1/cert-bundles/{bundle_id}/ca.pem")["content"]
        if args.file:
            args.file.write_text(content, encoding="utf-8")
            return {"file": str(args.file), "bundle_id": bundle_id}, None
        return content, None
    if args.command == "delete":
        confirm(args, "delete the certificate bundle")
        value = client.enqueue("DELETE", f"/v1/cert-bundles/{bundle_id}", None,
                               f"Delete cert bundle {bundle_id[:12]}", "cert-delete")
        return complete(client, value, args), None
    if args.command == "create-ca":
        path = "/v1/cert-bundles"
        payload = {"name": args.name, "common_name": args.common_name, "days": args.days}
    elif args.command == "generate":
        path = f"/v1/cert-bundles/{bundle_id}/certificates"
        payload = {"action": "generate", "name": args.name, "common_name": args.common_name,
                   "days": args.days, "file_stem": args.file_stem}
    else:
        path = f"/v1/cert-bundles/{bundle_id}/certificates"
        payload = {"action": "import", "name": args.name,
                   "content": args.file.read_text(encoding="utf-8"),
                   "filename": args.filename or args.file.name}
    value = client.enqueue("POST", path, payload, f"Certificate action {args.command}",
                           "cert-create" if args.command == "create-ca" else "cert-add")
    return complete(client, value, args), None


def dispatch(client: RunnerClient, args: argparse.Namespace) -> tuple[Any, Any]:
    if args.group == 'test-drive':
        items = client.get('/v1/test-drives')['test_drives']
        item = resolve(items, args.instance, 'id')
        path = f"/v1/test-drives/{item['id']}/microservices"
        if args.command == 'check-microservices':
            return complete(client, client.post(path + '/check', {}), args), None
        if args.operation == 'status':
            return client.get(path + '/00'), None
        return complete(client, client.post(path + '/00/configure', {'enabled': args.operation == 'enable'}), args), None
    if args.group == "l2":
        base = "/v1/l2-segments"
        if args.command == 'addressing':
            return client.get(base + '/addressing'), None
        if args.command == "create":
            value = client.post(base, {"kind": args.kind, "name": args.name})
        else:
            items = client.get(base)["segments"]
            if args.command == "list":
                return items, [("id", "ID"), ("kind", "KIND"), ("name", "NAME"), ("state", "STATE"), ("error", "ERROR")]
            segment = resolve(items, args.segment, "id")
            path = base + "/" + segment["id"]
            if args.command == "show":
                return segment, None
            if args.command == 'configure-port':
                endpoint = resolve(segment['endpoints'], args.endpoint, 'id')
                routes = []
                for route in args.route:
                    fields = route.split()
                    if len(fields) not in {1, 2}:
                        raise ValueError('route must be destination/prefix [gateway]')
                    routes.append({'destination': fields[0], 'gateway': fields[1] if len(fields) == 2 else ''})
                value = client.post(path + '/endpoints/' + endpoint['id'] + '/addressing',
                                    {'mode': args.mode, 'addresses': args.address, 'routes': routes})
                return complete(client, value, args), None
            if args.command == "attach":
                value = client.post(path + "/endpoints", {
                    "instance_id": _id(client, "instance", args.instance),
                    "interface": args.interface,
                })
            else:
                confirm(args, "disconnect endpoint" if args.command == "detach" else "delete L2 segment")
                if args.command == "detach":
                    endpoint = resolve(segment["endpoints"], args.endpoint, "id")
                    path += "/endpoints/" + endpoint["id"]
                value = client.delete(path)
        return complete(client, value, args), None
    if args.group == "health": return client.get("/healthz"), None
    if args.group == "status": return client.get("/v1/status"), None
    if args.group == "openapi": return client.get("/v1/openapi.json"), None
    if args.group == "source": return client.get("/v1/sources")["sources"], [("ip", "IP"), ("available", "AVAILABLE")]
    if args.group == "task":
        if args.command == "list": return client.get("/v1/tasks")["tasks"], TASK_COLUMNS
        if args.command == "show": return client.get(f"/v1/tasks/{args.task}"), None
        if args.command == "result": return client.get(f"/v1/tasks/{args.task}/result"), None
        return client.wait_task(args.task, timeout=args.task_timeout), None
    if args.group == "instance": return _instance(client, args)
    if args.group == "profile": return _profile(client, args)
    if args.group == "build": return _build(client, args)
    if args.group == "tuntom": return _tuntom(client, args)
    if args.group == "network-profile": return _network_profile(client, args)
    if args.group == "endpoint": return _endpoint(client, args)
    if args.group == "config": return _config(client, args)
    if args.group == "cert": return _cert(client, args)
    if args.group == "appliance-export":
        values = client.get("/v1/appliance-exports")["exports"]
        if args.command == "list": return values, EXPORT_COLUMNS
        export_id = ""
        if args.command in {"download", "delete"}:
            export_id = resolve(values, args.export, "export_id")["export_id"]
        if args.command == "download":
            item = resolve(values, args.export, "export_id")
            destination = args.file or Path(item.get("archive_name", f"{export_id}.tar.gz"))
            return {"file": client.download(
                f"/v1/appliance-exports/{export_id}/download", str(destination)
            )}, None
        if args.command == "delete":
            confirm(args, "delete the appliance export")
            value = client.enqueue("DELETE", f"/v1/appliance-exports/{export_id}", None,
                                   f"Delete appliance export {export_id[:12]}",
                                   "appliance-export-delete")
            return complete(client, value, args), None
        builds = client.get("/v1/build").get("artifacts", [])
        configs = client.get("/v1/configs").get("configs", [])
        build_id = resolve(builds, args.build, "build_id", "ref")["build_id"]
        config_id = resolve(configs, args.config_id, "config_id")["config_id"]
        value = client.enqueue("POST", "/v1/appliance-exports", {
            "name": args.name, "build_id": build_id, "config_id": config_id,
            "filesystem_mode": args.mode, "parameters": assignments(args.set),
        }, f"Export appliance {args.name}", "appliance-export")
        return complete(client, value, args), None
    if args.group == "network":
        if args.command == "show": return client.get("/v1/settings/networking"), None
        payload = json.loads(args.file.read_text(encoding="utf-8"))
        value = client.enqueue("PUT", "/v1/settings/networking", payload,
                               "Update networking", "network-settings-update")
        return complete(client, value, args), None
    raise ValueError("unsupported command")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(
            path=args.config, context=args.context, url=args.url, ws_url=args.ws_url,
            token_file=args.token_file, insecure=args.insecure, timeout=args.timeout,
            require_token=args.group != "health",
        )
        value, columns = dispatch(RunnerClient(config), args)
        if value is not None:
            emit(value, args.output, columns)
        return 0
    except (APIError, ConfigurationError, ValueError, OSError, json.JSONDecodeError) as exc:
        print(f"sasctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
