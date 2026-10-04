#!/bin/bash
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
RUNTIME_ROOT=${CZ_RUNTIME_ROOT:-/tmp/capture-zone-runtime}
TOKEN_FILE=${CZ_RUNTIME_ENV:-/tmp/capture-zone-runtime.env}

if [[ ${EUID} -ne 0 ]]; then
    echo "run-root-runtime.sh must run from the authorized root shell" >&2
    exit 1
fi
if [[ ! -r "$TOKEN_FILE" ]]; then
    echo "missing runtime environment: $TOKEN_FILE" >&2
    exit 1
fi
for dependency in ip nft systemd-run systemctl journalctl socat openssl; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        echo "missing required runtime dependency: $dependency" >&2
        exit 1
    fi
done

set -a
# The file is created by the unprivileged portal launcher and contains only the
# shared loopback bearer token.
source "$TOKEN_FILE"
set +a

RUNNER_TOKEN=${CZ_RUNNER_TOKEN:-}
if (( ${#RUNNER_TOKEN} < 32 )); then
    echo "CZ_RUNNER_TOKEN must contain at least 32 characters" >&2
    exit 1
fi

install -d -m 0700 "$RUNTIME_ROOT" "$RUNTIME_ROOT/state" "$RUNTIME_ROOT/run" "$RUNTIME_ROOT/bin"
if [[ ! -f "$RUNTIME_ROOT/source-ips.json" ]]; then
    install -m 0600 "$HERE/config/source-ips.json" "$RUNTIME_ROOT/source-ips.json"
fi

export PYTHONPATH="$HERE"
export CZ_RUNNER_HOST=127.0.0.1
export CZ_RUNNER_PORT=9080
export CZ_RUNNER_WS_PORT=9081
export CZ_RUNNER_STATE_DIR="$RUNTIME_ROOT/state"
export CZ_RUNNER_RUNTIME_DIR="$RUNTIME_ROOT/instances"
export CZ_RUNNER_TEMPLATE="$RUNTIME_ROOT/bin/smithproxy.cfg"
export CZ_RUNNER_SOURCES="${CZ_RUNNER_SOURCES:-$RUNTIME_ROOT/source-ips.json}"
export CZ_RUNNER_NETWORK_SETTINGS="${CZ_RUNNER_NETWORK_SETTINGS:-$RUNTIME_ROOT/network-settings.json}"
export CZ_RUNNER_NETWORK_ALLOCATIONS="${CZ_RUNNER_NETWORK_ALLOCATIONS:-$RUNTIME_ROOT/network-allocations.json}"
export CZ_RUNNER_FIREWALL="${CZ_RUNNER_FIREWALL:-$RUNTIME_ROOT/firewall.json}"
export CZ_RUNNER_SOURCE_DIR="$RUNTIME_ROOT/smithproxy-src"
export CZ_RUNNER_CONFIG_LIBRARY="$RUNTIME_ROOT/config-library"
export CZ_RUNNER_RUNTIME_PROFILES="$RUNTIME_ROOT/runtime-profiles.json"
export CZ_RUNNER_NETWORK_PROFILES="$RUNTIME_ROOT/network-profiles.json"
export CZ_RUNNER_CERT_LIBRARY="$RUNTIME_ROOT/cert-library"
export CZ_RUNNER_NATIVE_CACHE="$RUNTIME_ROOT/native-config-cache"
export CZ_RUNNER_CONFIG_PREVIEWS="$RUNTIME_ROOT/config-previews"
export CZ_RUNNER_TASK_STATE="$RUNTIME_ROOT/tasks.json"
export CZ_RUNNER_INSTANCE_CONFIG_ARCHIVE="${CZ_RUNNER_INSTANCE_CONFIG_ARCHIVE:-$RUNTIME_ROOT/instance-config-archive}"
export CZ_RUNNER_TEST_DRIVE_STATE="${CZ_RUNNER_TEST_DRIVE_STATE:-$RUNTIME_ROOT/test-drives-state}"
export CZ_RUNNER_TEST_DRIVE_RUNTIME="${CZ_RUNNER_TEST_DRIVE_RUNTIME:-$RUNTIME_ROOT/instances}"
export CZ_RUNNER_TEST_DRIVE_TTL="${CZ_RUNNER_TEST_DRIVE_TTL:-1800}"
export CZ_RUNNER_TEST_DRIVE_MAX_TTL="${CZ_RUNNER_TEST_DRIVE_MAX_TTL:-7200}"
export CZ_RUNNER_TEST_DRIVE_RETENTION="${CZ_RUNNER_TEST_DRIVE_RETENTION:-10800}"
export CZ_RUNNER_STOPPED_RETENTION="${CZ_RUNNER_STOPPED_RETENTION:-10800}"
export CZ_RUNNER_TASK_WORKERS="${CZ_RUNNER_TASK_WORKERS:-4}"
export CZ_RUNNER_MAX_TOTAL_RUNTIME="${CZ_RUNNER_MAX_TOTAL_RUNTIME:-86400}"
export CZ_RUNNER_SMITHPROXY="$RUNTIME_ROOT/bin/smithproxy"
export CZ_RUNNER_REPOSITORY=${CZ_RUNNER_REPOSITORY:-https://github.com/astibal/smithproxy.git}

echo "Capture Zone root runtime: $RUNTIME_ROOT"
echo "Stop with Ctrl-C; active instances, namespaces and routes are preserved."
echo "Set CZ_RUNNER_STOP_INSTANCES_ON_EXIT=1 only for an explicit full teardown."
exec /usr/bin/python3 -m runner.app
