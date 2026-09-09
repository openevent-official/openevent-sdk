#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_COMPILE=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
OUT_DIR="$ROOT_DIR/dist"
PYTHON_BIN="${PYTHON:-python3}"
BUILD_DEPS_DIR="$BUILD_DIR/build-deps"
PYCACHE_DIR="$BUILD_DIR/pycache"
TMP_WORK_DIR="$BUILD_DIR/tmp"

rm -rf "$BUILD_DEPS_DIR" "$PYCACHE_DIR" "$TMP_WORK_DIR"
rm -f "$OUT_DIR"/*.whl "$OUT_DIR"/*.tar.gz
mkdir -p "$BUILD_DEPS_DIR" "$OUT_DIR" "$PYCACHE_DIR" "$TMP_WORK_DIR"

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_WORK_DIR"

"$PYTHON_BIN" -m pip install -q --upgrade --target "$BUILD_DEPS_DIR" build
export PYTHONPATH="$BUILD_DEPS_DIR${PYTHONPATH:+:$PYTHONPATH}"

cd "$ROOT_DIR"

./generate_python_proto.sh "$ROOT_DIR"

"$PYTHON_BIN" -m build --wheel --outdir "$OUT_DIR"

printf 'build artifacts: %s\n' "$OUT_DIR"
