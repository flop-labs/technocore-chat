# SPDX-License-Identifier: Apache-2.0
"""Pin the served mobile author policy; the browser probe measures its layout."""

import re

from starlette.testclient import TestClient

import app


def test_mobile_author_can_wrap_without_consuming_the_message_column():
    page = TestClient(app.app).get("/humans").text
    mobile = re.search(r"@media\s*\(max-width:\s*34rem\)\s*\{(.*?)\n\s*\}", page, re.S)
    assert mobile is not None
    author = re.search(r"\.who\s*\{([^}]+)\}", mobile[1])
    assert author is not None, "the desktop nowrap marker needs a mobile override"
    policy = dict(re.findall(r"([\w-]+)\s*:\s*([^;]+)", author[1]))
    assert policy["white-space"].strip() == "normal"
    assert policy["overflow-wrap"].strip() == "anywhere"
    width = policy["max-width"].strip()
    assert width.endswith("%") and 0 < float(width[:-1]) <= 50
    assert policy["flex-shrink"].strip() == "0"
