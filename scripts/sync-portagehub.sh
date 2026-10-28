#!/usr/bin/env bash
# Usage: scripts/sync-portagehub.sh [--source DIR] [--source-name TEXT] [--dest DIR] [--patches DIR] [COMMIT]
#
# The same as `scripts/sync-app.sh portagehub ...`: bring the user's PortageHub into apps/portagehub.
exec "$(dirname -- "${BASH_SOURCE[0]}")/sync-app.sh" portagehub "$@"
