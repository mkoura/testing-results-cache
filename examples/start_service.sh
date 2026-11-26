#!/usr/bin/env bash

set -euo pipefail

# Copy this script to the checkout root after running `make install`.
script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
cd -- "${script_path%/*}"

if [[ ! -x .venv/bin/gunicorn ]]; then
    printf 'Gunicorn is missing or not executable at %s/.venv/bin/gunicorn.\n' "$PWD" >&2
    printf '%s\n' 'Run "make install" in the checkout root and place this script there.' >&2
    exit 1
fi

# Use an absolute path to share the instance directory with Flask CLI commands.
export INSTANCE_PATH="${INSTANCE_PATH:-$HOME/instance}"
if [[ ! -d "$INSTANCE_PATH" ]]; then
    printf 'INSTANCE_PATH is not an existing directory: %s\n' "$INSTANCE_PATH" >&2
    printf '%s\n' 'Check INSTANCE_PATH or follow the README setup for a new deployment.' >&2
    exit 1
fi

# Extra Gunicorn options can be passed on the command line, e.g. --timeout 330.
exec .venv/bin/gunicorn -w 3 -b 127.0.0.1:8000 "$@" 'testing_results_cache.app:create_app()'
