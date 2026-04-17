#!/usr/bin/env bash
set -e

echo "Testing local MCP Backend endpoints..."

if ! command -v curl >/dev/null 2>&1; then
    echo "curl is required to run this test."
    exit 1
fi

ENDPOINT=${1:-"http://localhost:8000"}

echo "1. Checking /health"
curl -s -f "$ENDPOINT/health" | grep -q '"status":"ok"' && echo "✅ Health check passed" || echo "❌ Health check failed"

echo "2. Checking /mcp/tools"
TOOLS_RES=$(curl -s -f "$ENDPOINT/mcp/tools")
if printf '%s' "$TOOLS_RES" | python -c 'import json, sys
data = json.load(sys.stdin)
target = "example_tool"

def contains(value):
    if isinstance(value, dict):
        return any(contains(v) for v in value.values())
    if isinstance(value, list):
        return any(contains(v) for v in value)
    return value == target

raise SystemExit(0 if contains(data) else 1)
'; then
    echo "✅ Capability check passed"
else
    echo "❌ Capability check failed"
fi
echo "3. Calling example_tool via /mcp/tools/call"
RES=$(curl -s -f -X POST "$ENDPOINT/mcp/tools/call" \
     -H "Content-Type: application/json" \
     -d '{"tool_name":"example_tool","arguments":{"param1":"test"}}')

if echo "$RES" | grep -q "success"; then
    echo "✅ Tool invocation passed"
else
    echo "❌ Tool invocation failed: $RES"
fi

echo "Done."
