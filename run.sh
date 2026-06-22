#!/usr/bin/env bash
# Keep TENTACLE's trading loop running during market hours.
# Loads .env (if present) so ALPACA_* and other keys are in the environment, then
# runs the engine. The loop itself adapts its cadence to whether the market is open.
#
# Run directly:        ./run.sh
# Or keep it alive:    see com.tentacle.run.plist (launchd) in this directory.
set -euo pipefail
cd "$(dirname "$0")"
if [ -f .env ]; then
  set -a
  . ./.env
  set +a
fi
exec .venv/bin/python -m tentacle run
