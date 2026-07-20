#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
OUT_DIR="$ROOT_DIR/src/openevent/sdk/proto"

mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

protoc \
  -I "$ROOT_DIR/proto" \
  --python_out="$OUT_DIR" \
  --grpc_python_out="$OUT_DIR" \
  --plugin=protoc-gen-grpc_python="$(command -v grpc_python_plugin)" \
  "$ROOT_DIR/proto/openevent.proto" \
  "$ROOT_DIR/proto/admin.proto"

sed -i 's/^import openevent_pb2 as openevent__pb2$/from . import openevent_pb2 as openevent__pb2/' \
  "$OUT_DIR/openevent_pb2_grpc.py"
sed -i 's/^import openevent_pb2 as openevent__pb2$/from . import openevent_pb2 as openevent__pb2/' \
  "$OUT_DIR/admin_pb2.py"
sed -i 's/^import admin_pb2 as admin__pb2$/from . import admin_pb2 as admin__pb2/' \
  "$OUT_DIR/admin_pb2_grpc.py"
