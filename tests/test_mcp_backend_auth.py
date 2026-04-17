from fastapi.testclient import TestClient

from caretaker.mcp_backend.main import app

client = TestClient(app)


def test_tools_allows_without_auth_when_mode_none(monkeypatch):
    monkeypatch.setenv("CARETAKER_MCP_AUTH_MODE", "none")

    response = client.get("/mcp/tools")

    assert response.status_code == 200
    assert "tools" in response.json()


def test_tools_requires_principal_header_when_mode_apim(monkeypatch):
    monkeypatch.setenv("CARETAKER_MCP_AUTH_MODE", "apim")
    monkeypatch.delenv("CARETAKER_MCP_ALLOWED_OBJECT_IDS", raising=False)

    response = client.get("/mcp/tools")

    assert response.status_code == 401


def test_tools_accepts_principal_when_mode_apim(monkeypatch):
    monkeypatch.setenv("CARETAKER_MCP_AUTH_MODE", "apim")
    monkeypatch.delenv("CARETAKER_MCP_ALLOWED_OBJECT_IDS", raising=False)

    response = client.get("/mcp/tools", headers={"x-ms-client-principal-id": "abc-123"})

    assert response.status_code == 200


def test_tools_rejects_non_allowlisted_principal(monkeypatch):
    monkeypatch.setenv("CARETAKER_MCP_AUTH_MODE", "apim")
    monkeypatch.setenv("CARETAKER_MCP_ALLOWED_OBJECT_IDS", "allowed-1,allowed-2")

    response = client.get("/mcp/tools", headers={"x-ms-client-principal-id": "other"})

    assert response.status_code == 403


def test_tools_accepts_allowlisted_principal(monkeypatch):
    monkeypatch.setenv("CARETAKER_MCP_AUTH_MODE", "apim")
    monkeypatch.setenv("CARETAKER_MCP_ALLOWED_OBJECT_IDS", "allowed-1,allowed-2")

    response = client.get(
        "/mcp/tools",
        headers={"x-ms-client-principal-id": "allowed-2"},
    )

    assert response.status_code == 200
