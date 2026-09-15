#!/usr/bin/env bash
# Launch the hass-mcp MCP server (github.com/voska/hass-mcp) for Claude Code,
# sourcing HA_URL/HA_TOKEN from ~/.config/crowdergy/ha.env.
#
# Wired up via .mcp.json (project scope, stdio). Mac/LAN only: Home Assistant
# is not reachable from remote sessions, and stdio servers do not run there.
#
#   scripts/hass-mcp.sh                  # what Claude Code runs (stdio)
#   scripts/hass-mcp.sh --register-user  # register in user scope for all repos
#
# stdout is the MCP channel: never print anything to stdout here.
set -euo pipefail

HA_ENV="${CROWDERGY_HA_ENV:-$HOME/.config/crowdergy/ha.env}"
HASS_MCP_VERSION="${HASS_MCP_VERSION:-0.6.0}"
HASS_MCP_PYTHON="${HASS_MCP_PYTHON:-3.13}"

die() { echo "hass-mcp.sh: $*" >&2; exit 1; }

if [ "${1:-}" = "--register-user" ]; then
  command -v claude >/dev/null 2>&1 || die "claude CLI not found in PATH"
  self="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  exec claude mcp add --scope user --transport stdio hass -- "$self"
fi

[ -r "$HA_ENV" ] || die "cannot read $HA_ENV (expects HA_URL=... and HA_TOKEN=...)"
if [ "$(stat -f '%Lp' "$HA_ENV" 2>/dev/null || stat -c '%a' "$HA_ENV")" != "600" ]; then
  echo "hass-mcp.sh: warning: $HA_ENV should be chmod 600" >&2
fi

set -a
# shellcheck disable=SC1090
. "$HA_ENV"
set +a
[ -n "${HA_URL:-}" ] || die "HA_URL missing in $HA_ENV"
[ -n "${HA_TOKEN:-}" ] || die "HA_TOKEN missing in $HA_ENV"
export HA_URL HA_TOKEN

export PATH="$HOME/.local/bin:$PATH"
command -v uvx >/dev/null 2>&1 || die "uvx not found (install uv: https://docs.astral.sh/uv/)"

exec uvx --python "$HASS_MCP_PYTHON" "hass-mcp@${HASS_MCP_VERSION}" "$@"
