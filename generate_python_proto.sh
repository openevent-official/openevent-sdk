#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
PYTHON_BIN="${PYTHON:-python3}"
PACKAGE_DIR="$ROOT_DIR/build/python"
OUT_DIR="$PACKAGE_DIR/openevent/sdk/proto"
CODEGEN_DIR="$ROOT_DIR/build/codegen-deps"

export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_COMPILE=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$ROOT_DIR/build/codegen-tmp"

rm -rf "$CODEGEN_DIR"
mkdir -p "$CODEGEN_DIR" "$TMPDIR"
"$PYTHON_BIN" -B -m pip install -q --target "$CODEGEN_DIR" \
  -r "$ROOT_DIR/requirements-codegen.txt"

"$PYTHON_BIN" -B - "$ROOT_DIR" "$PACKAGE_DIR" <<'PY'
from pathlib import Path
import shutil
import sys

root, package = map(Path, sys.argv[1:])
if package.exists():
    shutil.rmtree(package)
shutil.copytree(root / 'src', package, ignore=shutil.ignore_patterns(
    '__pycache__', '*.pyc',
))
PY

mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

PYTHONPATH="$CODEGEN_DIR" "$PYTHON_BIN" -B -m grpc_tools.protoc \
  -I "$ROOT_DIR/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  "$ROOT_DIR/proto/openevent.proto" \
  "$ROOT_DIR/proto/admin.proto"

sed -i 's/^import openevent_pb2 as openevent__pb2$/from . import openevent_pb2 as openevent__pb2/' \
  "$OUT_DIR/openevent_pb2_grpc.py"
sed -i 's/^import openevent_pb2 as openevent__pb2$/from . import openevent_pb2 as openevent__pb2/' \
  "$OUT_DIR/admin_pb2.py"
sed -i 's/^import admin_pb2 as admin__pb2$/from . import admin_pb2 as admin__pb2/' \
  "$OUT_DIR/admin_pb2_grpc.py"
