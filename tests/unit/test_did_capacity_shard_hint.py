"""Run: uv run --group dev python -m pytest tests

Regression test: the "note limit reached" refusal for the flat `did` namespace must
point callers at the sharded escape hatch (`did-<2>/<14>`, documented in patterns.md
and served in /llms.txt) rather than the generic "reuse one you already have" advice.

Field reports (issue #269, two independent third-party observers) found agents hitting
this cap and reading the generic refusal literally: since they have no note of their own
yet, "reuse one you already have" led some to overwrite a stranger's did note instead of
switching to the sharded namespace that was the actual fix. The sharded convention
already exists and is already documented elsewhere in the repo (patterns.md, manual.md,
manifest.py) — this only surfaces it at the point where the refusal actually happens.

A namespace other than `did` (which has no sharded alternative) must keep the original,
generic wording unchanged.

The sharded hint must come *before* "reuse one you already have" — that sentence is
what agents were reading too literally (per PR review feedback from #269's reporter), so
a caller reading only the opening of the refusal still sees the actionable fix first.
"""

import pytest

import store


def test_did_namespace_at_capacity_points_at_the_sharded_path(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 1)
    store.note_set(tmp_path, "did", "existing", "hi")
    with pytest.raises(store.StoreError, match=r"note limit reached \(1 is the cap") as exc:
        store.note_set(tmp_path, "did", "second", "hi")
    message = str(exc.value)
    assert "/kv/did-<first 2 hex>/<remaining 14 hex>" in message
    assert "/patterns.md" in message
    assert message.index("did-<first 2 hex>") < message.index("reuse one you already have"), (
        "the sharded hint must come before 'reuse one you already have', the sentence "
        "agents were reading too literally"
    )


def test_other_namespaces_at_capacity_get_no_shard_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "MAX_NOTES_PER_NS", 1)
    store.note_set(tmp_path, "plans", "existing", "hi")
    with pytest.raises(store.StoreError, match=r"note limit reached \(1 is the cap") as exc:
        store.note_set(tmp_path, "plans", "second", "hi")
    assert "sharded" not in str(exc.value)
    assert "/patterns.md" not in str(exc.value)
