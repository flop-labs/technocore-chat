import _client

client = _client.client


def test_room_schema_requires_the_retention_gap_signal(client):
    """Every JSON room view includes first_seq so a generated client can detect when
    retention dropped messages after its cursor."""
    schema = client.get("/openapi.json").json()["paths"]["/r/{room}"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]
    assert "first_seq" in schema["required"]
    assert "first_seq" in client.get("/r/openapi-required?format=json").json()
