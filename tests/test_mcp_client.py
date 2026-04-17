from __future__ import annotations

import pytest
import respx
from httpx import Response

from caretaker.config import MCPConfig
from caretaker.mcp.client import MCPClient


@pytest.mark.asyncio
async def test_mcp_client_connect_and_health() -> None:
    config = MCPConfig(enabled=True, endpoint="http://mcp.local", auth_mode="none")
    client = MCPClient(config)

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.get("http://mcp.local/health").respond(
            status_code=200,
            json={"status": "ok", "version": "0.1.0"},
        )
        await client.connect()

    assert client._connected is True

    await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_tool_apim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CARETAKER_MCP_CLIENT_PRINCIPAL_ID", "principal-123")
    config = MCPConfig(
        enabled=True,
        endpoint="http://mcp.local",
        auth_mode="apim",
        allowed_tools=["example_tool"],
    )
    client = MCPClient(config)

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.get("http://mcp.local/health").respond(
            status_code=200,
            json={"status": "ok", "version": "0.1.0"},
        )

        def _call_handler(request):
            assert request.headers.get("x-ms-client-principal-id") == "principal-123"
            return Response(
                status_code=200,
                json={
                    "status": "success",
                    "tool_name": "example_tool",
                    "result": {"message": "ok"},
                },
            )

        mock_router.post("http://mcp.local/mcp/tools/call").mock(side_effect=_call_handler)

        await client.connect()
        response = await client.call_tool("example_tool", {"param1": "hello"})

    assert response["status"] == "success"
    assert response["tool_name"] == "example_tool"

    await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_call_tool_rejects_disallowed_tool() -> None:
    config = MCPConfig(
        enabled=True,
        endpoint="http://mcp.local",
        auth_mode="none",
        allowed_tools=["example_tool"],
    )
    client = MCPClient(config)

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.get("http://mcp.local/health").respond(
            status_code=200,
            json={"status": "ok", "version": "0.1.0"},
        )
        await client.connect()

    with pytest.raises(ValueError, match="not permitted"):
        await client.call_tool("another_tool", {})

    await client.disconnect()


@pytest.mark.asyncio
async def test_mcp_client_apim_requires_principal(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CARETAKER_MCP_CLIENT_PRINCIPAL_ID", raising=False)
    config = MCPConfig(
        enabled=True,
        endpoint="http://mcp.local",
        auth_mode="apim",
        allowed_tools=["example_tool"],
    )
    client = MCPClient(config)

    with respx.mock(assert_all_called=True) as mock_router:
        mock_router.get("http://mcp.local/health").respond(
            status_code=200,
            json={"status": "ok", "version": "0.1.0"},
        )
        await client.connect()

    with pytest.raises(RuntimeError, match="CARETAKER_MCP_CLIENT_PRINCIPAL_ID"):
        await client.call_tool("example_tool", {})

    await client.disconnect()
