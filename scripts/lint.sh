#!/bin/sh
# Runs Mozilla's add-on linter on the package scripts/build.sh produces, so it
# checks only the files that ship.
set -eu
cd "$(dirname "$0")/.."
src="$(mktemp -d)"
trap 'rm -rf "$src"' EXIT
unzip -q "$(scripts/build.sh)" -d "$src"
scripts/web-ext.sh lint --source-dir "$src"
