#!/bin/sh
# Packages the extension into dist/firefox-sigstore-<version>.zip and prints
# the path.
set -eu
cd "$(dirname "$0")/.."

version="$(python3 -c 'import json; print(json.load(open("manifest.json"))["version"])')"
out="dist/firefox-sigstore-${version}.zip"

mkdir -p dist
rm -f "$out"
zip -q -r -X "$out" manifest.json background.js LICENSE images -x '*.DS_Store'
echo "$out"
