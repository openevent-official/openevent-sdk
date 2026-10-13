#!/usr/bin/env bash
set -euo pipefail
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_COMPILE=1

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_DIR="$ROOT_DIR/build"
PYTHON_BIN="${PYTHON:-python3}"
TEST_DIR="$BUILD_DIR/unit"
PYCACHE_DIR="$TEST_DIR/pycache"
PYTEST_CACHE_DIR="$TEST_DIR/pytest-cache"
TMP_WORK_DIR="$TEST_DIR/tmp"

rm -rf "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"
mkdir -p "$PYCACHE_DIR" "$PYTEST_CACHE_DIR" "$TMP_WORK_DIR"

export PYTHONPYCACHEPREFIX="$PYCACHE_DIR"
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -o cache_dir=$PYTEST_CACHE_DIR"
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1
export TMPDIR="$TMP_WORK_DIR"
unset PYTHONPATH PYTHONHOME
unset OPENEVENT_E2E

cd "$ROOT_DIR"
make test-env "PYTHON=$PYTHON_BIN"
"$BUILD_DIR/test-env/bin/python" -B -m pytest tests --basetemp="$TEST_DIR/cases" "$@"
