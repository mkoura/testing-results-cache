#!/usr/bin/env bash

set -euo pipefail

SCRIPT_DIR="$(readlink -m "${0%/*}")"

pushd "$SCRIPT_DIR" > /dev/null

# set environment variables, activate virtualenv, etc.
# shellcheck disable=SC1091
. .env/bin/activate
export INSTANCE_PATH="$HOME/instance"
mkdir -p "$INSTANCE_PATH"

exec gunicorn -w 3 -b 127.0.0.1:8000 'testing_results_cache.app:create_app()'
