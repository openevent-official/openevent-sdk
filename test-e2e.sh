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
unset PYTHONPATH PYTHONHOME

cd "$ROOT_DIR"
make test-env "PYTHON=$PYTHON_BIN"
"$BUILD_DIR/test-env/bin/python" -B -m pytest tests/test_e2e.py --basetemp="$E2E_DIR/cases" "$@"
