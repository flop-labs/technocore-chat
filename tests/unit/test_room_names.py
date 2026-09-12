"""Unit tests for the pure room-name predicates in src/store.py.

valid_name() and the room-class predicates (room_classes/unlisted/is_mailbox/
is_ephemeral/ownable) are pure string functions used on the hot path of every
listing and write. They were only covered transitively through the HTTP tests;
these pin their exact behavior so a regex or class-set change cannot silently
alter what names the service accepts or enumerates.

The valid_name() error message is part of the contract: it tells a caller the
exact rule and the usual causes, because the overwhelming majority of rejections
are an uppercase name or a space. Tests assert the message names the rule.
"""

from __future__ import annotations

import pytest
import store
from store import StoreError


def test_valid_name_accepts_canonical_names():
    for n in ["a", "room", "p-x", "mb-p-x", "e-p-x", "a1", "a-b-c", "x" * 48, "a0" * 24]:
        assert store.valid_name(n) == n


def test_valid_name_rejects_bad_input_with_the_rule_in_the_message():
    bad = ["", "A", "Room", "room name", "room.name", "room/", "room\n", "-x",
           "x" * 49, "room..x"]  # p/mb/e/d alone ARE valid names (class marker + body optional)
    for n in bad:
        with pytest.raises(StoreError) as exc:
            store.valid_name(n)
        assert "a-z0-9" in str(exc.value), f"{n!r} should name the rule"
        assert "uppercase" in str(exc.value) or "space" in str(exc.value), f"{n!r} should hint the cause"


def test_room_classes_composes_by_prefix():
    assert store.room_classes("p-x") == {"p"}
    assert store.room_classes("mb-p-x") == {"mb", "p"}
    assert store.room_classes("e-p-x") == {"e", "p"}
    assert store.room_classes("pastel") == frozenset()  # no hyphen -> no class
    assert store.room_classes("d-x") == {"d"}          # d IS a room class
    # "p" has no hyphen, so split("-")[:-1] is empty -> no classes (class needs a body)
    assert store.room_classes("p") == frozenset()


def test_unlisted_is_capability_url():
    assert store.unlisted("p-x")
    assert store.unlisted("mb-p-x")
    assert store.unlisted("e-p-x")
    assert not store.unlisted("room")
    assert not store.unlisted("mb-x-")  # mb without p is not unlisted


def test_is_mailbox_signed_only():
    assert store.is_mailbox("mb-p-x")
    assert not store.is_mailbox("p-x")
    assert not store.is_mailbox("room")


def test_is_ephemeral():
    assert store.is_ephemeral("e-p-x")
    assert not store.is_ephemeral("p-x")
    assert not store.is_ephemeral("room")


def test_ownable():
    assert store.ownable("d-x")
    assert not store.ownable("p-x")
    assert not store.ownable("room")
