#!/usr/bin/env bash
# Launches the public.com MCP server for Claude Code.
#
# Credentials are pulled ONLY from the trader .env (the single secret store), so
# the API secret never lands in any MCP config file or in ~/.claude.json. We
# extract just the two variables the server needs (not `source`, so no other API
# keys leak into the server's environment). The secret is never printed.
set -euo pipefail

ENV_FILE="${TRADER_ENV:-$HOME/claude-configs/trader/.env}"
MCP_BIN="${PUBLIC_MCP_BIN:-$HOME/.local/bin/publicdotcom-mcp-server}"

if [ ! -f "$ENV_FILE" ]; then
  echo "public_mcp_launch: .env not found at $ENV_FILE" >&2
  exit 1
fi

secret="$(grep -E '^PUBLIC_BROKER_API=' "$ENV_FILE" | head -1 | cut -d= -f2-)"
account="$(grep -E '^PUBLIC_COM_ACCOUNT_ID=' "$ENV_FILE" | head -1 | cut -d= -f2-)"

if [ -z "$secret" ] || [ -z "$account" ]; then
  echo "public_mcp_launch: missing PUBLIC_BROKER_API or PUBLIC_COM_ACCOUNT_ID in $ENV_FILE" >&2
  exit 1
fi

export PUBLIC_COM_SECRET="$secret"
export PUBLIC_COM_ACCOUNT_ID="$account"

exec "$MCP_BIN"
