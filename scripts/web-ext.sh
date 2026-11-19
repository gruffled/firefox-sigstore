#!/bin/sh
# Runs Mozilla's web-ext tool at a pinned version. Bump the version here.
exec npx --yes web-ext@10.8.0 "$@"
