"""Equivalent Accept field-line layouts must negotiate the same document label."""

import _client
import pytest

client = _client.client


@pytest.mark.parametrize("path", ["/skill.md", "/patterns.md", "/interop.md", "/auth.md"])
@pytest.mark.parametrize(
    ("values", "media_type"),
    [
        (["text/plain;q=0.2", "text/markdown"], "text/markdown"),
        (["text/markdown;q=0.2", "text/plain"], "text/plain"),
        (
            ["text/*;q=1", "text/markdown;q=0.8", "text/plain;q=0.1"],
            "text/markdown",
        ),
        (["text/markdown;q=0", "text/plain;q=0.2"], "text/plain"),
        (["*/*", "text/plain;q=0.5"], "text/plain"),
        (["text/plain;q=0.2, text/markdown;q=0.8"], "text/markdown"),
        ([], "text/plain"),
    ],
    ids=[
        "prefer-markdown",
        "prefer-plain",
        "specificity",
        "q-zero",
        "wildcard",
        "one-line",
        "absent",
    ],
)
def test_document_accept_field_lines_match_the_combined_value(client, path, values, media_type):
    # A list preserves separate field lines; a dict would hide the regression.
    split = client.get(path, headers=[("Accept", value) for value in values])
    combined = client.get(path, headers={"Accept": ", ".join(values)})

    assert split.status_code == combined.status_code == 200
    assert combined.headers["content-type"].split(";", 1)[0] == media_type
    assert split.headers["content-type"] == combined.headers["content-type"]
    assert split.content == combined.content
    assert split.headers["cache-control"] == combined.headers["cache-control"]
    assert split.headers.get("vary") == combined.headers.get("vary")
    assert "accept" in {name.strip().lower() for name in split.headers["vary"].split(",")}


@pytest.mark.parametrize("path", ["/", "/llms.txt"])
def test_repeated_accept_does_not_relabel_the_plain_text_manual(client, path):
    response = client.get(
        path, headers=[("Accept", "text/plain;q=0.1"), ("Accept", "text/markdown")]
    )
    ordinary = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.content == ordinary.content
    assert "accept" not in {
        name.strip().lower() for name in response.headers.get("vary", "").split(",")
    }
