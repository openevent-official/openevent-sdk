#!/usr/bin/env bash
set -euo pipefail

mode=$1
python=${PYTHON:-python3}
install_args=()
if [[ "$mode" == test-env ]]; then
    install_args=(--test-env)
fi
if [[ "$mode" != build ]]; then
    "$python" -B scripts/install.py "${install_args[@]}" --check-environment
fi

export TMPDIR="$PWD/build/tmp"
mkdir -p "$TMPDIR" dist
output=$(mktemp -d "$TMPDIR/wheel.XXXXXXXX")
trap 'rm -r -- "$output"' EXIT
"$python" -B scripts/build_wheel.py "$output"

shopt -s nullglob
wheels=("$output"/openevent_sdk-*.whl)
if (( ${#wheels[@]} != 1 )); then
    echo "Expected exactly one SDK wheel from this build" >&2
    exit 1
fi
release="dist/${wheels[0]##*/}"
mv "${wheels[0]}" "$release"
printf 'Built %s\n' "$release"

if [[ "$mode" != build ]]; then
    "$python" -B scripts/install.py "${install_args[@]}" "$release"
fi
