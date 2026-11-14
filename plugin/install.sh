#!/usr/bin/env sh
set -eu

# eisy-ui/UDX runs this hook as the isolated plugin service account. Keep the
# install bounded to that account; never modify the system Python environment.
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

exec python3 -m pip install \
    --user \
    --disable-pip-version-check \
    --no-warn-script-location \
    --requirement "$script_dir/requirements.txt"
