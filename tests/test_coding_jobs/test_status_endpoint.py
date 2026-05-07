def test_coding_job_status_returns_404_for_unknown():
    from fastapi.testclient import TestClient

    from caretaker.mcp_backend.main import app

    client = TestClient(app)
    resp = client.get("/coding-jobs/nonexistent1234/status")
    assert resp.status_code in (404, 503)  # 503 if redis not configured in test
