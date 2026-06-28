#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
PYTHON_BIN="${PYTHON:-python3}"
DEPS_DIR="$BUILD_DIR/test-deps"
PYCACHE_DIR="$BUILD_DIR/pycache"
PYTEST_CACHE_DIR="$BUILD_DIR/pytest-cache"
TMP_WORK_DIR="$BUILD_DIR/tmp"

rm -rf "$DEPS_DIR" "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"
mkdir -p "$DEPS_DIR" "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -o cache_dir=$PYTEST_CACHE_DIR"
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_WORK_DIR"

cd "$ROOT_DIR"

./generate_python_proto.sh "$ROOT_DIR"

"$PYTHON_BIN" -m pip install -q --upgrade --target "$DEPS_DIR" ".[test]"
export PYTHONPATH="$DEPS_DIR${PYTHONPATH:+:$PYTHONPATH}"

if [ -d tests ] && find tests -type f \( -name 'test_*.py' -o -name '*_test.py' \) | grep -q .; then
  "$PYTHON_BIN" -m pytest "$@"
else
  "$PYTHON_BIN" - <<'PY'
from openevent.sdk import AdminClient, OpenEventClient
from openevent.sdk.proto import openevent_pb2

assert AdminClient
assert OpenEventClient
assert openevent_pb2.VISIBILITY_PUBLIC == 0
assert openevent_pb2.CHANNEL_FILTER_ALL == 0
event = openevent_pb2.EventMessage(seq=1, channel_id=2, payload=b"ok")
request = openevent_pb2.FetchRequest(channels=[2])
response = openevent_pb2.FetchResponse(messages=[event], next_seq=2, last_seq=1)
assert list(request.channels) == [2]
assert response.messages[0].payload == b"ok"
assert response.last_seq == 1
print("sdk smoke check passed")
PY
fi
