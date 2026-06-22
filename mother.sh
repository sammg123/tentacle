#!/usr/bin/env bash
# One pass of the Mother: financial guardian + BLIND process review + propose a
# challenger policy + (with --promote) promote it if it clearly wins. Run on a
# cadence (weekly is a sane default), NOT every cycle, so the review stays cheap.
#
# Run directly:   ./mother.sh
# Weekly:         see com.tentacle.mother.plist (launchd) in this directory.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec .venv/bin/python -m tentacle mother --promote
