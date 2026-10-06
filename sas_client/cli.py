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
        while not finished.is_set():
            readable, _, _ = select.select([sys.stdin.fileno()], [], [], 0.1)
            if not readable:
                continue
            data = os.read(sys.stdin.fileno(), 4096)
            if not data:
                break
            if data == b"\x1d":  # Ctrl+]
                break
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
    for name in ("show", "diagnostics", "config", "cli", "gdb"):
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
    spawn.add_argument("--source-ip", required=True)
    spawn.add_argument("--user", required=True)
    spawn.add_argument("--ttl", type=lambda value: duration(value), default=1800)
    spawn.add_argument("--config-mode", choices=("ro", "rw"), default="ro")
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
    create.add_argument("--build", required=True)
    create.add_argument("--config-id", required=True)
    create.add_argument("--cert-bundle", default="")
    create.add_argument("--ttl", type=ttl_duration, default=1800)
    create.add_argument("--auto-restart", action="store_true")
    update = profile_sub.add_parser("update")
    update.add_argument("profile")
    update.add_argument("--name")
    update.add_argument("--build")
    update.add_argument("--config-id")
    update.add_argument("--cert-bundle")
    update.add_argument("--ttl", type=ttl_duration)
    update.add_argument("--auto-restart", choices=("yes", "no"))
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
        return resolve(client.get("/v1/instances")["instances"], value, "id", "id")["id"]
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
    if command == "list":
        values = client.get("/v1/instances")["instances"]
        return values, INSTANCE_COLUMNS
    instance_id = _id(client, "instance", getattr(args, "instance", "")) if command != "spawn" else ""
    if command == "show": return client.get(f"/v1/instances/{instance_id}"), None
    if command == "diagnostics": return client.get(f"/v1/instances/{instance_id}/diagnostics"), None
    if command == "logs": return client.get(f"/v1/instances/{instance_id}/logs", {"lines": args.lines})["output"], None
    if command == "config": return client.get(f"/v1/instances/{instance_id}/config")["content"], None
    if command in {"cli", "gdb"}:
        terminal(client, instance_id, command)
        return None, None
    if command == "spawn":
        tuntom_secret = os.environ.get("SAS_TUNTOM_SECRET", "")
        if args.tuntom_secret_file:
            tuntom_secret = args.tuntom_secret_file.read_text(encoding="ascii").strip()
        payload: dict[str, Any] = {
            "source_ip": args.source_ip, "user_id": args.user,
            "runtime_seconds": args.ttl, "config_mode": args.config_mode,
            "template_values": assignments(args.set),
            "parameters": {
                "workers": args.workers, "pcap_quota_mb": args.pcap_quota_mb,
                "socks_port": args.socks_port, "http_port": args.http_port,
                "plaintext_port": args.plaintext_port, "tls_port": args.tls_port,
                "cli_port": args.cli_port,
            },
        }
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
        payload = {
            "name": args.name, "build_id": _id(client, "build", args.build),
            "config_id": _id(client, "config", args.config_id),
            "cert_bundle_id": _id(client, "bundle", args.cert_bundle) if args.cert_bundle else "",
            "ttl_seconds": args.ttl, "auto_restart": args.auto_restart,
        }
        value = client.enqueue("POST", "/v1/runtime-profiles", payload,
                               f"Create profile {args.name}", "profile-create")
        return complete(client, value, args), None
    current = client.get(f"/v1/runtime-profiles/{profile_id}")
    payload = {
        "name": args.name if args.name is not None else current["name"],
        "build_id": _id(client, "build", args.build) if args.build else current["build_id"],
        "config_id": _id(client, "config", args.config_id) if args.config_id else current["config_id"],
        "cert_bundle_id": (_id(client, "bundle", args.cert_bundle) if args.cert_bundle else "")
        if args.cert_bundle is not None else current.get("cert_bundle_id", ""),
        "ttl_seconds": args.ttl if args.ttl is not None else current.get("ttl_seconds", 1800),
        "auto_restart": (args.auto_restart == "yes") if args.auto_restart is not None
        else current.get("auto_restart", False),
    }
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
