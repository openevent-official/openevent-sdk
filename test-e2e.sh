#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON_BIN="${PYTHON:-python3}"
BUILD_DIR="$ROOT_DIR/build"
E2E_DIR="$BUILD_DIR/e2e"
PYCACHE_DIR="$E2E_DIR/pycache"
PYTEST_CACHE_DIR="$E2E_DIR/pytest-cache"
TMP_WORK_DIR="$E2E_DIR/tmp"

SERVER_BIN="${OPENEVENT_SERVER_BIN:-}"

rm -rf "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"
mkdir -p "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"

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
export OPENEVENT_SERVER_BIN="$SERVER_BIN"

"$PYTHON_BIN" - <<'PY'
import importlib.metadata
import importlib.util
import sys

required = {
    "grpc": "grpcio",
    "packaging": "packaging",
    "pytest": "pytest",
    "openevent.sdk": "openevent-sdk>=0.10.0",
}
missing = []
for module, requirement in required.items():
    try:
        present = importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        present = False
    if not present:
        missing.append(requirement)
if missing:
    print("missing e2e Python dependencies in the current environment: " + ", ".join(missing), file=sys.stderr)
    sys.exit(2)
try:
    version = importlib.metadata.version("openevent-sdk")
except importlib.metadata.PackageNotFoundError:
    print("missing e2e Python dependency in the current environment: openevent-sdk>=0.10.0", file=sys.stderr)
    sys.exit(2)
from packaging.version import InvalidVersion, Version

try:
    installed_version = Version(version)
except InvalidVersion:
    print(f"installed openevent-sdk has an invalid version: {version}", file=sys.stderr)
    sys.exit(2)
if installed_version < Version("0.10.0"):
    print(f"openevent-sdk>=0.10.0 is required for e2e tests, found {version}", file=sys.stderr)
    sys.exit(2)

from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import admin_pb2, openevent_pb2
from google.protobuf.descriptor import FieldDescriptor

for message, requirements in (
    (admin_pb2.AddTokenRequest, (("principal", 1, FieldDescriptor.TYPE_UINT64),)),
    (admin_pb2.DeleteTokenRequest, (
        ("principal", 1, FieldDescriptor.TYPE_UINT64),
        ("token", 2, FieldDescriptor.TYPE_STRING),
    )),
):
    for name, number, field_type in requirements:
        field = message.DESCRIPTOR.fields_by_name.get(name)
        if field is None or field.number != number or field.type != field_type:
            print(f"installed openevent-sdk does not provide {message.__name__}.{name}={number}", file=sys.stderr)
            sys.exit(2)

token_field = admin_pb2.AddTokenResponse.DESCRIPTOR.fields_by_name.get("token")
if token_field is None or token_field.number != 1 or token_field.type != token_field.TYPE_STRING:
    print("installed openevent-sdk does not provide AddTokenResponse.token=1", file=sys.stderr)
    sys.exit(2)

service = admin_pb2.DESCRIPTOR.services_by_name.get("AdminService")
if service is None or "ListMessages" not in service.methods_by_name or not hasattr(AdminClient, "list_messages"):
    print("installed openevent-sdk does not provide AdminService.ListMessages", file=sys.stderr)
    sys.exit(2)
object_service = openevent_pb2.DESCRIPTOR.services_by_name.get("ObjectStorageService")
required_object_methods = {"WriteObject", "GetObjectMetadata", "ReadObject"}
if object_service is None or not required_object_methods.issubset(object_service.methods_by_name):
    print("installed openevent-sdk does not provide the required ObjectStorageService protocol", file=sys.stderr)
    sys.exit(2)
event_fields = openevent_pb2.EventMessage.DESCRIPTOR.fields_by_name
event_service = openevent_pb2.DESCRIPTOR.services_by_name.get("EventService")
if "object_keys" not in event_fields or "uuid" not in event_fields:
    print("installed openevent-sdk does not provide EventMessage.object_keys and uuid", file=sys.stderr)
    sys.exit(2)
if event_service is None or not {"AllocateUuids", "GetSeqByUuid"}.issubset(event_service.methods_by_name):
    print("installed openevent-sdk does not provide the required UUID EventService methods", file=sys.stderr)
    sys.exit(2)
subscribe_fields = openevent_pb2.SubscribeRequest.DESCRIPTOR.fields_by_name
if "channels" not in subscribe_fields or subscribe_fields["channels"].number != 5:
    print("installed openevent-sdk does not provide SubscribeRequest.channels=5", file=sys.stderr)
    sys.exit(2)
required_client_methods = {
    "get_uuid",
    "get_seq_by_uuid",
    "write_object",
    "get_object_metadata",
    "read_object",
}
if any(not hasattr(OpenEventClient, name) for name in required_client_methods):
    print("installed openevent-sdk does not provide high-level object methods", file=sys.stderr)
    sys.exit(2)
PY

cd "$ROOT_DIR"
"$PYTHON_BIN" -m pytest tests/test_e2e.py --basetemp="$E2E_DIR/cases" "$@"
