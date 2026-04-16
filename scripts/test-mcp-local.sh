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
curl -s -f "$ENDPOINT/mcp/tools" | grep -q "example_tool" && echo "✅ Capability check passed" || echo "❌ Capability check failed"

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
