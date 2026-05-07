def test_coding_job_status_returns_404_for_unknown():
    from fastapi.testclient import TestClient

    from caretaker.mcp_backend.main import app

    client = TestClient(app)
    # CARETAKER_MCP_AUTH_MODE defaults to "none" so no token needed
    resp = client.get("/coding-jobs/nonexistent1234/status")
    assert resp.status_code in (404, 503)  # 503 if redis not configured in test
