#!/usr/bin/env bash
set -euo pipefail

suite=$1
shift
case "$suite" in
    unit) tests=(tests --ignore=tests/test_e2e.py) ;;
    e2e)
        if [[ ! -x "${OPENEVENT_SERVER_BIN:-}" ]]; then
            echo 'Set OPENEVENT_SERVER_BIN to an executable openevent_server before running e2e tests.' >&2
            exit 1
        fi
        tests=(tests/test_e2e.py)
        ;;
    *) echo "Usage: $0 unit|e2e [pytest options]" >&2; exit 2 ;;
esac

work_dir="$PWD/build/$suite"
export TMPDIR="$work_dir/tmp"
mkdir -p "$TMPDIR"
exec "$PYTHON" -B -m pytest "${tests[@]}" \
    -o "cache_dir=$work_dir/pytest-cache" --basetemp="$work_dir/cases" "$@"
