#!/bin/bash
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
VENV=${SMITHPROXY_APPLIANCE_CONSOLE_VENV:-${SMITHPROXY_APPLIACE_CONSOLE_VENV:-$HERE/.venv}}
RUNNER_ENV=${CZ_RUNTIME_ENV:-/tmp/capture-zone-runtime.env}
CONSOLE_ENV=${SMITHPROXY_APPLIANCE_CONSOLE_ENV:-${SMITHPROXY_APPLIACE_CONSOLE_ENV:-}}
if [[ -z "$CONSOLE_ENV" ]]; then
    if [[ -f /tmp/smithproxy-appliace-console-runtime.env ]]; then
        CONSOLE_ENV=/tmp/smithproxy-appliace-console-runtime.env
    else
        CONSOLE_ENV=/tmp/smithproxy-appliance-console-runtime.env
    fi
fi
HOST=${SMITHPROXY_APPLIANCE_CONSOLE_HOST:-${SMITHPROXY_APPLIACE_CONSOLE_HOST:-127.0.0.1}}
PORT=${SMITHPROXY_APPLIANCE_CONSOLE_PORT:-${SMITHPROXY_APPLIACE_CONSOLE_PORT:-5000}}

if (( EUID == 0 )); then
    echo "run-console.sh must run as an unprivileged user; start the runner separately as root" >&2
    exit 1
fi
if [[ ! -x "$VENV/bin/python" ]]; then
    echo "missing console Python environment: $VENV" >&2
    exit 1
fi
for dependency in openssl mktemp stat; do
    if ! command -v "$dependency" >/dev/null 2>&1; then
        echo "missing required dependency: $dependency" >&2
        exit 1
    fi
done

create_secret_file() {
    local path=$1
    local key=$2
    local directory
    local temporary

    [[ -s "$path" ]] && return
    directory=$(dirname -- "$path")
    mkdir -p -- "$directory"
    temporary=$(mktemp "$directory/.smithproxy-console-env.XXXXXX")
    chmod 0600 "$temporary"
    printf '%s=%s\n' "$key" "$(openssl rand -hex 32)" >"$temporary"
    mv -f -- "$temporary" "$path"
}

create_secret_file "$RUNNER_ENV" CZ_RUNNER_TOKEN
create_secret_file "$CONSOLE_ENV" SMITHPROXY_APPLIANCE_CONSOLE_SECRET

for environment_file in "$RUNNER_ENV" "$CONSOLE_ENV"; do
    if [[ ! -O "$environment_file" || $(stat -c '%a' "$environment_file") != 600 ]]; then
        echo "runtime environment file must be owned by the current user with mode 0600: $environment_file" >&2
        exit 1
    fi
done

set -a
source "$RUNNER_ENV"
source "$CONSOLE_ENV"
set +a

runner_token=${CZ_RUNNER_TOKEN:-}
console_secret=${SMITHPROXY_APPLIANCE_CONSOLE_SECRET:-${SMITHPROXY_APPLIACE_CONSOLE_SECRET:-}}
if (( ${#runner_token} < 32 )); then
    echo "CZ_RUNNER_TOKEN must contain at least 32 characters" >&2
    exit 1
fi
if (( ${#console_secret} < 32 )); then
    echo "SMITHPROXY_APPLIANCE_CONSOLE_SECRET must contain at least 32 characters" >&2
    exit 1
fi

export CAPTURE_RUNNER_URL=${CAPTURE_RUNNER_URL:-http://127.0.0.1:9080}
export CAPTURE_RUNNER_WS_URL=${CAPTURE_RUNNER_WS_URL:-ws://127.0.0.1:9081}
export SMITHPROXY_APPLIANCE_CONSOLE_SECRET=$console_secret

echo "smithproxy-appliance console: http://$HOST:$PORT"
exec "$VENV/bin/python" -m flask \
    --app "$HERE/app.py:create_app" \
    run --host "$HOST" --port "$PORT"
