#!/bin/bash
# GitReins MCP server wrapper — ensures correct CWD for config loading
source ~/.hermes/.env 2>/dev/null

# Load LLM credentials for Tier 2 evaluator
export GITREINS_LLM_API_KEY="${GITREINS_LLM_API_KEY:-${DEEPSEEK_API_KEY:-}}"
export GITREINS_LLM_BASE_URL="${GITREINS_LLM_BASE_URL:-https://api.deepseek.com/v1}"
export GITREINS_LLM_MODEL="${GITREINS_LLM_MODEL:-deepseek-v4-flash}"

cd /home/kara/gitreins-poc
exec /home/kara/gitreins-poc/.venv/bin/python3 gitreins_mcp/server.py "$@"
