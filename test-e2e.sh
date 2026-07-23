#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
BUILD_DIR="$ROOT_DIR/build"
E2E_DIR="$BUILD_DIR/e2e"
PYCACHE_DIR="$E2E_DIR/pycache"
PYTEST_CACHE_DIR="$E2E_DIR/pytest-cache"
TMP_WORK_DIR="$E2E_DIR/tmp"
SERVER_DIR="$E2E_DIR/server"
CONFIG_PATH="$E2E_DIR/openevent-server.yaml"
LOG_PATH="$E2E_DIR/openevent-server.log"

SERVER_BIN="${OPENEVENT_SERVER_BIN:-}"
GRPC_ADDR="${OPENEVENT_E2E_GRPC_ADDR:-127.0.0.1:19527}"
ADMIN_ADDR="${OPENEVENT_E2E_ADMIN_ADDR:-127.0.0.1:19528}"

rm -rf "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR" "$SERVER_DIR"
mkdir -p "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR" "$SERVER_DIR"

if [ -z "$SERVER_BIN" ] || [ ! -x "$SERVER_BIN" ]; then
  printf 'Set OPENEVENT_SERVER_BIN to an executable openevent_server binary before running e2e tests.\n' >&2
  printf 'Current value: %s\n' "${SERVER_BIN:-<empty>}" >&2
  exit 1
fi

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -o cache_dir=$PYTEST_CACHE_DIR"
export PIP_NO_CACHE_DIR=1
export PIP_NO_COMPILE=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_WORK_DIR"
export OPENEVENT_E2E=1
export OPENEVENT_E2E_TARGET="$GRPC_ADDR"
export OPENEVENT_E2E_ADMIN_TARGET="$ADMIN_ADDR"

"$PYTHON_BIN" - <<'PY'
import importlib.metadata
import importlib.util
import sys

required = {
    "grpc": "grpcio",
    "pytest": "pytest",
    "openevent.sdk": "openevent-sdk>=0.4.1",
}
missing = [requirement for module, requirement in required.items() if importlib.util.find_spec(module) is None]
if missing:
    print("missing e2e Python dependencies in the current environment: " + ", ".join(missing), file=sys.stderr)
    sys.exit(2)
try:
    version = importlib.metadata.version("openevent-sdk")
except importlib.metadata.PackageNotFoundError:
    print("missing e2e Python dependency in the current environment: openevent-sdk>=0.4.1", file=sys.stderr)
    sys.exit(2)
parts = tuple(int(part) for part in version.split(".")[:3] if part.isdigit())
if parts < (0, 4, 1):
    print(f"openevent-sdk>=0.4.1 is required for e2e tests, found {version}", file=sys.stderr)
    sys.exit(2)

from openevent.sdk import AdminClient
from openevent.sdk.proto import admin_pb2

service = admin_pb2.DESCRIPTOR.services_by_name.get("AdminService")
if service is None or "ListMessages" not in service.methods_by_name or not hasattr(AdminClient, "list_messages"):
    print("installed openevent-sdk does not provide AdminService.ListMessages", file=sys.stderr)
    sys.exit(2)
PY

cat > "$CONFIG_PATH" <<YAML
grpc:
  listen_addr: "$GRPC_ADDR"

admin:
  listen_addr: "$ADMIN_ADDR"

storage:
  path: "$SERVER_DIR/data"

limits:
  max_payload_bytes: 16777216
YAML

"$SERVER_BIN" "$CONFIG_PATH" >"$LOG_PATH" 2>&1 &
SERVER_PID=$!

cleanup() {
  if kill -0 "$SERVER_PID" 2>/dev/null; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT

"$PYTHON_BIN" - <<'PY'
import os
import sys
import time

import grpc

from openevent.sdk import AdminClient, OpenEventClient

admin_target = os.environ["OPENEVENT_E2E_ADMIN_TARGET"]
event_target = os.environ["OPENEVENT_E2E_TARGET"]
deadline = time.time() + 10
last_error = None
while time.time() < deadline:
    try:
        AdminClient(admin_target).list_tokens()
        try:
            OpenEventClient(event_target).get_status(principal=0, token="readiness-invalid-token")
        except grpc.RpcError as exc:
            if exc.code() != grpc.StatusCode.UNAUTHENTICATED:
                raise
        break
    except grpc.RpcError as exc:
        last_error = exc
        time.sleep(0.1)
else:
    print(f"OpenEvent endpoints did not become ready: {last_error}", file=sys.stderr)
    sys.exit(1)
PY

cd "$ROOT_DIR"
"$PYTHON_BIN" -m pytest tests/test_e2e.py "$@"
