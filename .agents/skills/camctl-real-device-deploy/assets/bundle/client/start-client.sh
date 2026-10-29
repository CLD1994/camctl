#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export CAMCTL_DATA_DIR="${CAMCTL_DATA_DIR:-$root/data}"
cd "$root/apps/client"
exec npm start
