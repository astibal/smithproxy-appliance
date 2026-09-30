#!/bin/bash
set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
exec "$HERE/console/run-console.sh" "$@"
