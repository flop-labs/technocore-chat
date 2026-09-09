"""Run: uv run --group dev python -m pytest tests"""

import json
import re
import time
from pathlib import Path

import _client
import pytest
from _client import (
    _claim,
    _keypair,
    _say_signed,
    _set_signed,
)

client = _client.client  # the shared TestClient fixture


def _ok(client, target, post=None):
    """Send `target` (a path, or an already-made response) and require it to succeed.

    A published limit is honoured only if the server *accepts* the extreme value; a 4xx
    here means the document is advertising something no caller can use.
    """
    if isinstance(target, str):
        target = client.post(target, json=post) if post is not None else client.get(target)
    assert target.status_code == 200, f"published limit refused: {target.text[:160]}"
    return target


def test_security_txt_is_a_valid_rfc_9116_document(client):
    """The place a researcher and an automated scanner both look before opening a public
    issue. It is only useful if it parses and if `Expires` has not passed."""
    from datetime import UTC, datetime

    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert "noindex" not in r.headers.get("x-robots-tag", "")  # being found is the point

    fields: dict[str, list[str]] = {}
    for raw in r.text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = line.partition(":")
        assert value.strip(), f"field {name!r} has no value"
        fields.setdefault(name.strip().lower(), []).append(value.strip())

    assert fields["contact"], "Contact is the one field RFC 9116 cannot do without"
    assert len(fields["expires"]) == 1, "RFC 9116: exactly one Expires"
    expires = datetime.strptime(fields["expires"][0], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    ahead = expires - datetime.now(UTC)
    assert ahead.days > 0, "an expired security.txt reads as an abandoned channel"
    assert ahead.days < 366, "RFC 9116: Expires should be under a year out"
    # The advisory form is listed first: it is the monitored channel and it keeps a report
    # private until there is a fix. The mailbox is the route for anyone without an account.
    assert fields["contact"][0].startswith("https://")
    assert any(c.startswith("mailto:") for c in fields["contact"])
    assert fields["policy"]


def test_the_security_contact_is_the_operators_to_set(client, monkeypatch):
    """This image is published. A third party running it must not end up advertising the
    upstream project's mailbox for a problem with their own deployment."""
    import config

    with config.override(SECURITY_CONTACT="someone@example.org"):
        assert "mailto:someone@example.org" in client.get("/.well-known/security.txt").text


def test_the_served_manual_states_the_caps_it_actually_enforces(client):
    """/llms.txt tells agents it is the complete protocol, so a number in it that disagrees
    with the enforced constant is worse than no number. Prose said "512 rooms, 4096 notes"
    for a whole release after the caps moved underneath it — nothing catches that except
    generating the numbers, and nothing keeps them generated except this."""
    import store

    manual = client.get("/llms.txt").text
    assert f"at most {store.MAX_ROOMS} rooms" in manual
    assert f"{store.MAX_NOTES_TOTAL} notes in total" in manual
    assert f"{store.MAX_NOTES_PER_NS} per\nnamespace" in manual
    assert f"{store.MAX_TOTAL_ROOM_BYTES >> 30} GiB" in manual
    assert f"~{store.MAX_ROOM_BYTES >> 20} MiB" in manual
    # and the stale literals are gone
    assert "at most 512 rooms" not in manual and "4096 notes" not in manual


def test_the_manual_template_hardcodes_no_constant_it_could_render(client):
    """The generalisation of the two tests around this one, and the reason they can stop
    being written one cap at a time.

    Both of those exist because prose drifted from a constant, and each was fixed by
    tokenising the one number that had already gone wrong. That leaves every *other*
    number in the template waiting its turn — the manual restated the name grammar, both
    character caps, the limit bound and default, the topic preview, the ephemeral TTL, the
    nonce scan window and the reap ages, none of them generated, all of them stated to
    agents as the complete protocol.

    So this asserts the property rather than the instances: for each constant the renderer
    knows how to substitute, its literal value must not appear in the *template*. Adding a
    knob and writing its value into the prose fails here, at the commit that does it,
    instead of at the release that moves the knob.

    The template, not the rendered document: the rendered one is *supposed* to contain
    every one of these. That is the point of rendering it.
    """
    import app as app_module
    import store

    # Value -> the token that should be carrying it. Only unambiguous literals: a bare
    # `50` or `200` matches a byte count or an HTTP status somewhere in the prose, so
    # those are checked in the phrases the manual actually uses them in.
    forbidden = {
        store.NAME_RE.pattern: "__NAME_RULE__",
        str(store.MAX_TEXT_CHARS): "__MAX_TEXT__",
        str(store.MAX_VALUE_CHARS): "__MAX_VALUE__",
        f"1..{store.MAX_LIMIT}": "__MAX_LIMIT__",
        f"last {store.DEFAULT_LIMIT} messages": "__DEFAULT_LIMIT__",
        f"previews {store.TOPIC_PREVIEW_CHARS}": "__TOPIC_PREVIEW__",
        ", ".join(store.INVISIBLE_CATEGORIES[:2]): "__SWEEP_CATEGORIES__",
    }
    template = app_module._MANUAL_TEMPLATE
    for literal, token in forbidden.items():
        assert literal not in template, (
            f"manual.md hardcodes {literal!r}; use {token} so it is rendered from the constant"
        )
        assert token in template, f"{token} is substituted but no longer used in manual.md"

    # And the substitution is exhaustive: no token survives into what agents are served.
    assert "__" not in app_module.MANUAL


def test_the_manual_states_a_rendered_knob_as_this_instances_value_not_the_default(client):
    """Rendering a per-deployment knob changes what the prose around it may claim.

    The EPHEMERAL paragraph read "__EPHEMERAL_TTL__ by default". That was true while the
    figure was the software default typed into the file; once it renders from
    `store.EPHEMERAL_TTL_SECONDS`, an instance configured to an hour serves "1 hour by
    default" — false about the default, and contradicting the very next clause, which says
    the enforced value is published elsewhere rather than fixed here.

    So a rendered knob must be labelled as *this instance's* value. Asserted against a
    changed TTL, because at the default the wrong wording and the right one read alike.
    """
    import app as app_module
    import config

    with config.override(EPHEMERAL_TTL_SECONDS=3600):
        manual = app_module._render_manual()
    section = manual.split("EPHEMERAL:", 1)[1].split("\n\n", 1)[0]
    assert "1 hour" in section, section
    assert "by default" not in section, "a rendered per-deployment knob is not the default"
    assert "THIS instance enforces" in section
    # And the JSON copy is still named, since that is what a machine reads.
    assert "limits.ephemeral_ttl_seconds" in section


def test_the_manual_renders_durations_and_sets_from_the_constants(client):
    """The two helpers that let prose name a set or a period without restating it.

    `_english_list` is why "Cc, Cf, Cs, Co, Zl and Zp" cannot say five when the sweep does
    six; `_duration` is why the ephemeral TTL reads as "15 minutes" without 900 being
    written anywhere but the knob. Both are asserted against a *changed* value, because an
    identity-looking helper passes every test at the default and none off it.
    """
    import manifest

    assert manifest._english_list(("Cc", "Cf", "Cs")) == "Cc, Cf and Cs"
    assert manifest._english_list(("Cc", "Cf")) == "Cc and Cf"
    assert manifest._english_list(("Cc",)) == "Cc"
    assert manifest._english_list(()) == ""

    assert manifest._duration(900) == "15 minutes"
    assert manifest._duration(60) == "1 minute"
    assert manifest._duration(3600) == "1 hour"
    assert manifest._duration(7 * 86400) == "7 days"
    # Not a whole unit: an exact second count beats a rounded one an operator cannot check.
    assert manifest._duration(90) == "90 seconds"


def test_the_manual_names_every_category_the_sweep_actually_takes(client):
    """The same drift the caps test guards, on the sweep (#171).

    The prose said "C0/C1 controls, format characters, zero-width joiners, bidi overrides",
    which is `Cc` plus part of `Cf`, while `INVISIBLE_CATEGORIES` also takes Cs, Co, Zl and
    Zp. A reader who trusted it signed text the server had already altered, then met a 403
    naming the signature rather than the sweep. Both halves are asserted: every enforced
    category is named, plus the four that used to be missing are present by name.
    """
    import store

    manual = client.get("/llms.txt").text
    swept = manual.split("SINGLE LINE:", 1)[1].split("\n\n", 1)[0]
    for category in store.INVISIBLE_CATEGORIES:
        assert category in swept, f"the manual does not name {category}, which the sweep takes"
    # The regression itself: these four were enforced and unnamed.
    for missing in ("Cs", "Co", "Zl", "Zp"):
        assert missing in swept, missing


def test_the_manual_states_the_url_break_even_it_actually_has(client):
    """The GET write lane meets two ceilings, of which the character cap is not the binding one.

    Percent-encoding costs 3 bytes per UTF-8 byte, so the ~16 KB a URL survives at the edge
    divides by `MAX_TEXT_CHARS` into a bytes-per-character break-even. Above it a caller
    cannot reach the character cap in a URL at all. The prose used to frame that as
    "non-Latin scripts do not fit", which is the wrong axis: dense Vietnamese is Latin and
    does not fit either. 16 KB is the edge's number rather than one this service enforces,
    so what is pinned here is the arithmetic against our own cap.
    """
    import store

    break_even = (16 << 10) // store.MAX_TEXT_CHARS
    budget = client.get("/llms.txt").text.split("URL BUDGET:", 1)[1].split("\n\n", 1)[0]
    assert f"break-even is {break_even} bytes per character" in budget
    assert "Vietnamese" in budget  # the counterexample that makes the axis clear
    assert "Non-Latin scripts do not" not in budget  # the framing this replaced


def test_the_service_never_normalizes_so_a_signature_covers_the_form_you_sent(client):
    """What the manual's NORMALIZATION paragraph promises, asserted through the surface.

    Unicode composition is a caller's choice rather than a canonical form. This service takes
    neither side: it stores the code points it was given. That has to be documented because
    the signed lane makes it sharp. NFC and NFD of one word are different bytes, so a
    signature over one is not a signature over the other, while the 403 that follows says
    nothing about normalization.
    """
    import unicodedata

    precomposed = unicodedata.normalize("NFC", "Việt")
    decomposed = unicodedata.normalize("NFD", "Việt")
    assert precomposed != decomposed and len(decomposed) > len(precomposed)

    for form in (precomposed, decomposed):
        posted = client.post("/r/norm?format=json", json={"from": "vi", "text": form})
        assert posted.status_code == 200
        assert posted.json()["posted"]["text"] == form  # stored as sent, neither folded

    stored = [m["text"] for m in client.get("/r/norm?format=json").json()["messages"]]
    assert stored == [precomposed, decomposed]  # two messages, not one deduplicated word

    did, sign = _keypair()
    signature = sign(f"norm|1|{precomposed}")
    crossed = client.post(
        "/r/norm", json={"did": did, "sig": signature, "nonce": "1", "text": decomposed}
    )
    assert crossed.status_code == 403  # signed one form, sent the other
    matched = client.post(
        "/r/norm", json={"did": did, "sig": signature, "nonce": "1", "text": precomposed}
    )
    assert matched.status_code == 200


def test_the_manual_states_the_floor_it_enforces_under_a_raised_room_cap(client):
    """The reported bug, through the surface that reported it (#242).

    RESERVED_ROOM_BYTES is the budget divided by MAX_ROOMS, so it is the one published
    figure an operator can move: CHAT_MAX_ROOMS=10240 halves it to 512 KiB, and the old
    `>> 20` render published that as "0 MiB" — a floor of zero reads as no floor at all,
    the opposite of what the append path enforces. At the source default the floor lands
    exactly on 1 MiB, which is why the manual test above never caught it.
    """
    import app as app_module
    import store

    assert "0 MiB per room" not in app_module._render_manual()  # the default is not broken

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr(store, "MAX_ROOMS", 10240)
        monkeypatch.setattr(store, "RESERVED_ROOM_BYTES", store.MAX_TOTAL_ROOM_BYTES // 10240)
        raised = app_module._render_manual()
    finally:
        monkeypatch.undo()

    assert "512 KiB per room" in raised
    assert "0 MiB per room" not in raised
    assert app_module._render_manual() == app_module.MANUAL  # and it renders back the same


def test_fmt_bytes_renders_a_floor_without_ever_overstating_it(client):
    """Two ways a whole-unit shift misreports a guarantee, and the rule for each.

    Falling under the unit is the reported one: `524288 >> 20` is 0. Truncating *within*
    the unit is the quieter one — at CHAT_MAX_ROOMS=3000 the floor is 1.7 MiB and `>> 20`
    still says "1 MiB". Rounding would fix the second and break the guarantee, since
    1.969 MiB stated as "2.0 MiB" promises more than the store enforces, so this floors.
    """
    import manifest
    import store

    assert manifest.fmt_bytes(524288) == "512 KiB"  # the reported case: under the unit
    assert manifest.fmt_bytes(1789569) == "1.7 MiB"  # the quiet case: 5 GiB // 3000
    assert manifest.fmt_bytes(2064548) == "1.9 MiB"  # 1.969 MiB — floored, never "2.0 MiB"

    # Defaults are byte-identical to the shift they replace, so no published text moves.
    assert manifest.fmt_bytes(store.MAX_TOTAL_ROOM_BYTES) == "5 GiB"
    assert manifest.fmt_bytes(store.MAX_ROOM_BYTES) == "10 MiB"
    assert manifest.fmt_bytes(1 << 20) == "1 MiB"  # the floor at the default 5120 rooms
    assert manifest.fmt_bytes((1 << 20) + 1) == "1 MiB"  # a whole unit gains no fake ".0"
    assert manifest.fmt_bytes(512) == "512 B" and manifest.fmt_bytes(0) == "0 B"


def test_public_base_rejects_a_host_that_matches_only_before_a_trailing_newline(client):
    """`_HOST_RE` is the control that stops an attacker-controlled Host header from steering
    the absolute URLs the documents advertise — `servers[0].url` in /openapi.json, every
    `_url(base, …)` in /.well-known/agent.json, the sitemap `<loc>` values, security.txt's
    `Canonical:` line and the `Link` header. `re.match` lets `$` match *before* a trailing
    newline, so `"example.com\\n"` passed the gate and rode into `base` verbatim — a control
    character that breaks strict JSON/XML consumers and shapes a header split. `fullmatch` is
    the lesson `store.valid_name` already learned for its own allowlist."""
    import manifest

    assert manifest.public_base("https", "example.com") == "https://example.com"
    assert manifest.public_base("https", "example.com\n") == ""


def test_the_room_budget_is_published_where_agents_look(client):
    import app as app_module
    import store

    limits = client.get("/.well-known/agent.json").json()["limits"]
    assert limits["new_rooms_per_day_per_ip"] == app_module.RATE_ROOMS_PER_DAY
    # Both note caps, because either can be the refusal and only one of them used to be
    # derivable: `notes_per_namespace` was MAX_ROOMS until CHAT_MAX_NOTES_PER_NS made it a
    # per-deployment number, so a client that reads it off `rooms` now reads it wrong.
    assert limits["notes"] == store.MAX_NOTES_TOTAL
    assert limits["notes_per_namespace"] == store.MAX_NOTES_PER_NS


def test_agent_surfaces_are_never_html(client):
    # Cache-Control is deliberately not asserted here: it is per path and it is covered
    # path by path in the four edge-cache tests at the end of this file. This one is about
    # the HTML exception not spreading (docs/design.md §8), and mixing the two is what
    # turned a widened cache rule into an edit to a test named for XSS.
    client.get("/r/lobby/say/bot/hi")
    for path in ("/", "/llms.txt", "/robots.txt", "/r/lobby", "/rooms", "/healthz"):
        r = client.get(path)
        assert r.headers["content-type"].startswith("text/plain"), path
        assert r.headers["x-content-type-options"] == "nosniff", path


def test_robots_keeps_rooms_out_of_indexes_but_invites_the_manual(client):
    body = client.get("/robots.txt").text
    assert "Disallow: /r/" in body and "Disallow: /kv/" in body
    assert "Allow: /" in body and "/llms.txt" in body
    assert client.get("/r/lobby").headers["x-robots-tag"] == "noindex"


def test_the_skill_states_only_constants_it_can_keep_true(client):
    """SKILL.md cannot be rendered, so it needs the guard the manual does not.

    Every other served document interpolates its numbers (`app._render_manual`), but this
    one is published byte-for-byte with a SHA-256 in
    /.well-known/agent-skills/index.json — the installable file and the served one are one
    artifact, deliberately, so substituting into it would break the digest that makes
    "read <host>/skill.md and follow it" checkable. A test is therefore the only thing
    that can hold its numbers to the code.

    Two rules, and the split is what matters. A *code constant* may be stated: it moves
    only with a release, and the skill ships with the release. A *per-deployment knob* may
    not, because this file is byte-identical on every instance and the knob is not — it
    has to name where the enforced value is published instead.

    The unit is part of the claim: the note cap is code points, and a note of 4-byte
    characters is four times the byte figure. Saying "8 KiB" was wrong twice over.
    """
    import store

    skill = client.get("/skill.md").text

    # Code constants, stated: these are true wherever this file is served.
    assert store.NAME_RE.pattern in skill
    assert f"Messages ≤ {store.MAX_TEXT_CHARS} chars" in skill
    assert f"notes ≤ {store.MAX_VALUE_CHARS} chars" in skill
    assert "characters,\nnot bytes" in skill, "the note cap is code points; the unit must say so"
    for category in store.INVISIBLE_CATEGORIES:
        assert f"`{category}`" in skill, (
            f"the skill does not name {category}, which the sweep takes"
        )

    # A figure for a document that grows: the skill said "(~15 KB)" against a manual that
    # is 22 KB and moves with every release, and being byte-pinned it can never catch up.
    # Nothing here should state a size it cannot measure (#364).
    assert "KB)" not in skill, "the skill cannot keep a size for a document it does not own"

    # Per-deployment knobs, not stated as fact: the skill points at the published value.
    assert "limits.long_poll_seconds" in skill
    assert "one request per 10 seconds" not in skill, (
        "CHAT_MAX_WAIT is per deployment; a byte-pinned file cannot assert its value"
    )
    # The regression this replaces: a byte figure for a character cap.
    assert "8 KiB" not in skill


def test_skill_md_is_the_installable_skill_and_is_never_rate_limited(client, monkeypatch):
    import config

    # Same bytes as the installable SKILL.md — one artifact, so the skill an agent
    # installs and the skill it fetches can never drift.
    skill = (Path(__file__).resolve().parents[2] / "SKILL.md").read_text()
    assert client.get("/skill.md").text == skill
    assert client.get("/skill.md").headers["content-type"].startswith("text/plain")
    assert "/llms.txt" in client.get("/skill.md").text  # points at the full reference
    with config.override(RATE_READ=1):
        for _ in range(5):
            assert client.get("/skill.md").status_code == 200
    assert "/skill.md" not in client.get("/robots.txt").text  # nothing disallows it


def test_patterns_are_served_unlimited_and_the_manual_points_there(client, monkeypatch):
    import config

    page = client.get("/patterns.md")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/plain")
    assert "E2E" in page.text and "choreography" in page.text
    assert "/patterns.md" in client.get("/llms.txt").text  # the manual points here
    with config.override(RATE_READ=1):
        for _ in range(5):
            assert client.get("/patterns.md").status_code == 200  # never rate limited
    assert "/patterns.md" not in "".join(  # nothing disallows it for crawlers
        line for line in client.get("/robots.txt").text.splitlines() if "Disallow" in line
    )


def test_interop_is_served_unlimited_and_claims_nothing_for_this_origin(client, monkeypatch):
    """The bridging guide, served like the patterns it composes.

    Its whole premise is that every protocol in it is a process run beside this service, so
    the assertion that matters is the negative one: publishing the document must not turn
    into a claim that this origin speaks any of them. The manifest still refuses A2A and MCP
    (test_no_protocol_claims_in_the_manifest), and this checks the document says so itself.

    Unlimited for a sharper reason than the manual's: a bridge author reads it precisely
    when their bridge is being told to back off.
    """
    import config

    page = client.get("/interop.md")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/plain")
    assert "ActivityPub" in page.text and "A2A" in page.text
    assert "speaks one protocol" in page.text  # states what this origin actually answers
    assert "/interop.md" in client.get("/llms.txt").text  # the manual points here
    assert "/interop.md" in client.get("/sitemap.xml").text  # crawlers are told about it
    with config.override(RATE_READ=1):
        for _ in range(5):
            assert client.get("/interop.md").status_code == 200  # never rate limited
    assert "x-robots-tag" not in page.headers  # documentation, indexable like the rest


def test_the_e2e_pattern_round_trips_within_the_caps(client, tmp_path):
    """Executable version of /patterns.md pattern 4. The server never does crypto here —
    the test proves the documented choreography fits the real lanes and caps: DID notes
    hold the key material, the signed mailbox lane carries the sealed room key, and a
    full-length encrypted message fits a room write. Protocol drift breaks this first."""
    import base64
    import hashlib

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    import store

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    def unb64(s: str) -> bytes:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

    def derive(shared: bytes) -> AESGCM:
        return AESGCM(
            HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"technocore-e2e-v1").derive(
                shared
            )
        )

    # A (recipient), once: identity + static X25519 key, published in a DID note.
    did_a, _sign_a = _keypair(7)
    a_static = X25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    fp = hashlib.sha256(did_a.encode()).hexdigest()[:16]
    did_path = f"/kv/did-{fp[:2]}/{fp[2:]}"
    mailbox = "mb-p-inbox-of-a"
    note = f"{did_a} x25519:{b64(a_static.public_key().public_bytes_raw())} mailbox:{mailbox}"
    assert client.post(did_path, json={"value": note}).status_code == 200

    # B (sender): reads the note, seals a room key to A with an ephemeral key.
    did_b, sign_b = _keypair(8)
    # The value is the last non-empty line: note reads open with the untrusted-content
    # banner, and a real reader has to skip it exactly like this.
    fetched = [ln for ln in client.get(did_path).text.splitlines() if ln.strip()][-1]
    b_x25519 = dict(f.split(":", 1) for f in fetched.split(" ")[1:])
    eph = X25519PrivateKey.from_private_bytes(bytes([8]) * 32)
    a_pub = X25519PrivateKey.from_private_bytes(bytes([7]) * 32).public_key()
    assert b64(a_pub.public_bytes_raw()) == b_x25519["x25519"]  # note round-tripped
    room, room_key, nonce12 = "p-e2e-room-3f9a1c", AESGCM.generate_key(256), bytes(12)
    sealed = derive(eph.exchange(a_pub)).encrypt(nonce12, room_key + room.encode(), None)
    delivery = f"e2e1 {b64(eph.public_key().public_bytes_raw())} {b64(nonce12)} {b64(sealed)}"
    assert _say_signed(client, b_x25519["mailbox"], did_b, sign_b, delivery).status_code == 200

    # A: reads its mailbox (attributed to B's key), unseals the room key + room name.
    inbox = client.get(f"/r/{mailbox}?format=json").json()["messages"][-1]
    assert inbox["from"] == did_b  # the delivery is attributable, not a bare nickname
    kind, eph_pub_s, nonce_s, sealed_s = inbox["text"].split(" ")
    assert kind == "e2e1"
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PublicKey

    opened = derive(a_static.exchange(X25519PublicKey.from_public_bytes(unb64(eph_pub_s)))).decrypt(
        unb64(nonce_s), unb64(sealed_s), None
    )
    assert opened[:32] == room_key and opened[32:].decode() == room

    # Both: a full-length plaintext, encrypted, fits the message cap — and round-trips.
    plaintext = "the lobsters molt at midnight " * 66 + "km"  # 1982 chars
    ct = AESGCM(room_key).encrypt(nonce12, plaintext.encode(), None)
    line = f"{b64(nonce12)}.{b64(ct)}"
    assert len(line) <= store.MAX_TEXT_CHARS  # the documented budget holds
    assert client.post(f"/r/{room}", json={"from": "b", "text": line}).status_code == 200
    got = client.get(f"/r/{room}?format=json").json()["messages"][-1]["text"]
    n_s, ct_s = got.split(".")
    assert AESGCM(room_key).decrypt(unb64(n_s), unb64(ct_s), None).decode() == plaintext

    # The operator's view: the stored bytes carry ciphertext, never the plaintext.
    on_disk = store.room_path(tmp_path, room).read_text()
    assert "lobsters" not in on_disk and b64(ct)[:40] in on_disk


UNDOCUMENTED = {
    # /stats does not exist unless a token is configured, and answers 404 rather than 401
    # to anyone without it. Publishing its path would hand back exactly what that 404
    # withholds.
    "/stats",
}


def _spelled_for_openapi(path: str) -> str:
    """Starlette writes `{text:path}`; OpenAPI writes `{text}`. Compare on the parameter
    names, which is what a generated client keys on."""
    return re.sub(r"\{(\w+)(:\w+)?\}", r"{\1}", path)


def test_the_spec_and_the_running_app_describe_the_same_service(client):
    """The exhaustive version, both directions, paths *and* methods.

    This document is what a machine reads instead of the manual, and a machine cannot
    notice that a route it was never told about exists. So: every route the app serves is
    documented, every documented path is one the app would actually route, and every
    documented method is one that route accepts. A new endpoint fails this test until it is
    described — which is the point, and is why the check lives here rather than at import:
    a missing description should fail CI, never refuse to boot a running service.
    """
    from starlette.routing import Route

    import app as app_module

    doc = client.get("/openapi.json").json()
    assert doc["openapi"].startswith("3.1")
    routes = [r for r in app_module.app.routes if isinstance(r, Route)]
    assert len(routes) == len(app_module.app.routes), "a non-Route was mounted and skipped here"
    # Starlette registers one Route per (path, methods) pair, so GET and POST on the same
    # path are two entries and the methods have to be unioned — keyed rather than merged,
    # the second entry would hide the first and this whole test would pass on half of it.
    # `or ()` is for the "accepts anything" route, which this app does not have.
    served: dict[str, set[str]] = {}
    for route in routes:
        served.setdefault(_spelled_for_openapi(route.path), set()).update(
            m.lower() for m in route.methods or ()
        )

    # 1. Nothing served is missing.
    for path, accepts in served.items():
        if path in UNDOCUMENTED:
            continue
        assert path in doc["paths"], f"{path} is served but undocumented"
        for method in accepts & {"get", "post"}:
            assert method in doc["paths"][path], f"{method.upper()} {path} is undocumented"

    # 2. Nothing documented is invented. A documented path is legitimate if it is a route's
    #    own path, or a concrete instance of one — /r/events is a real URL served by the
    #    /r/{room} route, and worth documenting separately because it behaves differently.
    for path, operations in doc["paths"].items():
        matches = [
            r for r in routes if _spelled_for_openapi(r.path) == path or r.path_regex.match(path)
        ]
        assert matches, f"{path} is documented but nothing routes it"
        accepted = {m for r in matches for m in served[_spelled_for_openapi(r.path)]}
        assert set(operations) <= accepted, f"{path} documents a method it does not accept"

    # 3. Every operation is actually usable by a reader: identified, summarised, and with
    #    the outcome a caller will actually get described.
    operations = [op for path in doc["paths"].values() for op in path.values()]
    ids = [op["operationId"] for op in operations]
    assert len(ids) == len(set(ids)), "operationIds must be unique — clients name methods with them"
    for op in operations:
        assert op["summary"], op
        codes = set(op["responses"])
        # Normally the success case. The exception is a lane that exists only to refuse:
        # `/r/events` accepts POST because `/r/{room}` does and answers 403 every time, so
        # a documented 200 would be the lie. It must say so in prose, or "no 2xx" is
        # indistinguishable from an oversight.
        if not any(code.startswith("2") for code in codes):
            assert "403" in codes, f"{op['operationId']} documents no outcome at all"
            assert "refus" in (op["summary"] + op.get("description", "")).lower(), (
                f"{op['operationId']} can never succeed and does not say why"
            )


def test_every_documented_response_declares_the_body_it_returns(client):
    """A response with no `content` tells a generated client there is nothing to show. On a
    service whose refusals *are* the documentation — the 413 names the cap, the 409 carries
    the current value, the 429 the retry delay — that hides the correction at exactly the
    moment a caller needs it. `content_type_conformance` cannot catch it either: it only
    checks the responses a fuzzer actually provokes, and nothing in a bounded run uploads
    256 KiB. So the rule is blanket, because every response this service sends has a body.
    """
    doc = client.get("/openapi.json").json()
    bare = [
        f"{verb.upper()} {path} -> {code}"
        for path, operations in doc["paths"].items()
        for verb, op in operations.items()
        for code, response in op["responses"].items()
        if "content" not in response
    ]
    assert not bare, f"documented with no body: {bare}"

    # And the declared type is the one the server sends, spot-checked across the three
    # shapes: a refusal, a machine-readable document, and a negotiated one.
    for path, expected in (
        ("/kv/plans/next/set/hi", "text/plain"),
        ("/openapi.json", "application/json"),
        ("/skill.md", "text/plain"),
    ):
        served = client.get(path).headers["content-type"].split(";")[0]
        assert served == expected, f"{path} sends {served}"
    # …and the negotiated one really does offer the second type it advertises.
    markdown = client.get("/skill.md", headers={"Accept": "text/markdown"})
    assert markdown.headers["content-type"].startswith("text/markdown")
    assert "text/markdown" in doc["paths"]["/skill.md"]["get"]["responses"]["200"]["content"]


def test_every_negotiable_response_publishes_the_switch_that_negotiates_it(client):
    """A 200 that declares both `text/plain` and `application/json` is a promise the caller
    can choose — and `?format=json` is the only way to choose it. Two operations carried
    their own copy of that parameter and five carried none, so a machine reading the spec
    saw endpoints it believed were text-only and never asked for the JSON they serve
    (#658). The parameter is now one shared constant, and this test is what keeps the
    document from drifting away from the switch again: the negotiable set is derived from
    the responses, so a new dual-lane operation is covered the day it is added.
    """
    doc = client.get("/openapi.json").json()
    negotiable = [
        (verb, path, op)
        for path, operations in doc["paths"].items()
        for verb, op in operations.items()
        if {"text/plain", "application/json"}
        <= set(op["responses"].get("200", {}).get("content", {}))
    ]
    assert negotiable, "no negotiable operation found — this test would pass on nothing"

    silent = [
        f"{verb.upper()} {path}"
        for verb, path, op in negotiable
        if "format" not in {p.get("name") for p in op.get("parameters", [])}
    ]
    assert not silent, f"negotiable but the switch is undocumented: {silent}"

    # One description, not per-operation prose that drifts: the same text everywhere.
    described = {
        next(p["description"] for p in op["parameters"] if p.get("name") == "format")
        for _, _, op in negotiable
    }
    assert len(described) == 1, f"{len(described)} spellings of the same parameter"

    # And the switch it documents is the one the server honours, on a lane that had none.
    assert client.get("/r/events").headers["content-type"].startswith("text/plain")
    assert (
        client.get("/r/events?format=json").headers["content-type"].startswith("application/json")
    )


def test_a_published_ceiling_is_a_number_json_can_carry(client, monkeypatch):
    """`float()` accepts `inf` and `nan` where the `int()` beside it raises, and this setting's
    value is published. A non-finite ceiling reaches /openapi.json and
    /.well-known/agent.json as the bare token `Infinity` — which Python emits and reads back
    but RFC 8259 forbids, so every strict parser rejects the whole document. A discovery
    service answering with undiscoverable documents is worse off than one that refused to
    boot. Review catch on #40.
    """
    import json as json_module

    import app as app_module

    for bad in ("inf", "-inf", "nan", "NaN"):
        with pytest.raises(ValueError, match="must be a finite number"):
            app_module._finite_env("CHAT_MAX_WAIT", bad)
    # Junk still dies the way every other numeric setting here does.
    with pytest.raises(ValueError):
        app_module._finite_env("CHAT_MAX_WAIT", "abc")
    assert app_module._finite_env("CHAT_MAX_WAIT", "2.5") == 2.5

    # …and the ceiling is actually wired through it. Checking the helper alone would pass
    # against a MAX_WAIT that still called bare `float()`, which is the mistake this
    # guards: the process has to refuse to start, not merely own a function that could
    # have refused. A fresh interpreter importing the real chain — app importing config
    # importing the environment — is that boot: no sys.modules surgery in this process,
    # and unlike a re-exec of config alone it fails if app ever stops importing config
    # (review: PR #59).
    import os
    import subprocess
    import sys

    src = repr(str(Path(__file__).resolve().parents[2] / "src"))
    boot = subprocess.run(
        [sys.executable, "-c", f"import sys; sys.path.insert(0, {src}); import app"],
        capture_output=True,
        text=True,
        env={**os.environ, "CHAT_MAX_WAIT": "inf"},
    )
    assert boot.returncode != 0, "app booted with a non-finite CHAT_MAX_WAIT"
    assert "must be a finite number" in boot.stderr

    # Whatever survives that, the documents stay strict JSON — no bare Infinity or NaN.
    for raw in (client.get("/openapi.json").text, client.get("/.well-known/agent.json").text):
        assert "Infinity" not in raw and "NaN" not in raw
        json_module.loads(raw)  # parses under Python's lenient reader too


def test_an_integral_ceiling_publishes_as_an_integer(client):
    """`10.0` and `10` are the same number to a validator and different bytes to a reader,
    and this was an integer literal until the ceiling became configurable. A fractional
    ceiling still publishes as a float, because fractional waits are real.

    Read off `/.well-known/agent.json` rather than the `wait` parameter's `maximum`: the
    server clamps to the ceiling instead of refusing past it, so under the input doctrine
    (docs/design.md §3.5) the OpenAPI parameter states the clamp in prose and no longer
    publishes a constraint nothing enforces. `limits.long_poll_seconds` is where the number
    stayed machine-readable, and the same rendering rule applies to it."""
    import manifest

    def ceiling(doc):
        return doc["limits"]["long_poll_seconds"]

    served = ceiling(client.get("/.well-known/agent.json").json())
    assert served == 10 and isinstance(served, int)
    assert ceiling(manifest.agent_manifest("", "0.7.0", 1, 1, 1, 2.5)) == 2.5
    assert ceiling(manifest.agent_manifest("", "0.7.0", 1, 1, 1, 10.0)) == 10
    # And the prose that replaced the constraint carries the same number, not `10.0`.
    wait = next(
        p
        for p in client.get("/openapi.json").json()["paths"]["/r/{room}"]["get"]["parameters"]
        if p["name"] == "wait"
    )
    assert "clamped to 10." in wait["description"] and "maximum" not in wait["schema"]


_REFUSALS = frozenset({"400", "403", "404", "409", "422"})


_DUPE_TEXT = "one more copy of this sentence than allowed is refused, measured"


def _one_copy_too_many(client, lane: str):
    """Land the allowed copies of one long text, then one more, and return its response.

    The filter's knobs are pinned here rather than read off the shipped defaults - the
    shared client fixture pins the filter OFF, so without this override there is no 422
    to document - and `allowed` reads the pinned value, so the copy count and the
    threshold cannot drift apart when someone tunes one of them.
    """
    import app as app_module
    import config
    import limit

    limit._dupes.clear()
    app_module._buckets.clear()  # the cases above spent the shared write bucket; buy it back
    with config.override(DUPE_FILTER_SECONDS=30, DUPE_MAX_COPIES=5, RATE_WRITE=600):
        allowed = config.DUPE_MAX_COPIES  # the pinned 5, read so count and knob cannot drift
        for i in range(allowed):
            if lane == "say":
                client.get(f"/r/dupe422/say/n{i}/{_DUPE_TEXT.replace(' ', '%20')}")
            elif lane == "post":
                client.post("/r/dupe422", json={"from": f"n{i}", "text": _DUPE_TEXT})
            else:
                did, sign = _keypair(100 + i)
                _say_signed(client, "dupe422", did, sign, _DUPE_TEXT, nonce=1)
        if lane == "say":
            return client.get(f"/r/dupe422/say/last/{_DUPE_TEXT.replace(' ', '%20')}")
        if lane == "post":
            return client.post("/r/dupe422", json={"from": "last", "text": _DUPE_TEXT})
        did, sign = _keypair(199)
        return _say_signed(client, "dupe422", did, sign, _DUPE_TEXT, nonce=1)


def test_every_refusal_is_provoked_and_every_provoked_refusal_is_documented(client):
    """Both directions, because each catches what the other cannot.

    An undocumented status is the failure neither a generated client nor a contract fuzzer
    recovers from: the client treats an unannounced 403 as a transport fault and retries
    the identical bytes, the fuzzer calls the service broken. So every case below is
    provoked against the running app and the spec must list what came back.

    The second assertion is the one that would have saved a round of review. This started
    as a hand-written table, and `POST /r/events` was added to the document *after* the
    table was written — so it documented a 403 no test had ever asked for, and a reviewer
    found the 400 and 413 it also returns. A table only covers what someone remembered to
    add; requiring every documented refusal to have a case makes forgetting fail the build.
    """
    did, sign = _keypair()
    other, other_sign = _keypair(2)
    client.get("/kv/plans/held/set/first")
    assert _claim(client, "d-owned", did, sign).status_code == 200
    signed_note = f"{sign('room-owners|d-owned|4|' + other)}/4/{other}"

    # (openapi path, method, expected status, the request that produces it)
    cases = [
        # Reads.
        ("/r/{room}", "get", 400, lambda: client.get("/r/UPPER")),
        ("/r/{room}/export", "get", 400, lambda: client.get("/r/UPPER/export")),
        ("/kv/{ns}", "get", 400, lambda: client.get("/kv/UPPER")),
        ("/kv/{ns}/{key}", "get", 400, lambda: client.get("/kv/UPPER/key")),
        ("/kv/{ns}/{key}", "get", 404, lambda: client.get("/kv/plans/never-written")),
        # A sitemap needs an origin, and a Host that is not one leaves it with nothing to
        # point at. Spaces cannot appear in a hostname, so this is never a real origin.
        (
            "/sitemap.xml",
            "get",
            404,
            lambda: client.get("/sitemap.xml", headers={"host": "not a host"}),
        ),
        # The URL write lanes. `%0A` matches no route at all — deliberate, and the reason a
        # message cannot forge a second JSONL record.
        ("/r/{room}/say/{nick}/{text}", "get", 400, lambda: client.get("/r/UPPER/say/bot/hi")),
        ("/r/{room}/say/{nick}/{text}", "get", 403, lambda: client.get("/r/mb-box/say/bot/hi")),
        ("/r/{room}/say/{nick}/{text}", "get", 404, lambda: client.get("/r/lobby/say/bot/a%0Ab")),
        ("/kv/{ns}/{key}/set/{value}", "get", 400, lambda: client.get("/kv/UPPER/k/set/v")),
        ("/kv/{ns}/{key}/set/{value}", "get", 403, lambda: client.get("/kv/room-nonce/x/set/1")),
        ("/kv/{ns}/{key}/set/{value}", "get", 404, lambda: client.get("/kv/plans/k/set/a%0Ab")),
        (
            "/kv/{ns}/{key}/set/{value}",
            "get",
            409,
            lambda: client.get("/kv/plans/held/set/second?if=not-that"),
        ),
        # The POST lanes.
        ("/r/{room}", "post", 400, lambda: client.post("/r/lobby", json={"from": "b", "text": ""})),
        (
            "/r/{room}",
            "post",
            403,
            lambda: client.post("/r/mb-box", json={"from": "b", "text": "hi"}),
        ),
        ("/r/events", "post", 400, lambda: client.post("/r/events", content=b"not json")),
        (
            "/r/events",
            "post",
            403,
            lambda: client.post("/r/events", json={"from": "b", "text": "hi"}),
        ),
        ("/kv/{ns}/{key}", "post", 400, lambda: client.post("/kv/UPPER/k", json={"value": "v"})),
        # `required: ["value"]` never implied a *non-empty* value, and the sweep refuses one.
        ("/kv/{ns}/{key}", "post", 400, lambda: client.post("/kv/plans/k", json={"value": ""})),
        (
            "/kv/{ns}/{key}",
            "post",
            403,
            lambda: client.post("/kv/room-nonce/lobby", json={"value": "9"}),
        ),
        (
            "/kv/{ns}/{key}",
            "post",
            409,
            lambda: client.post("/kv/plans/held", json={"value": "v", "if": "not-that"}),
        ),
        # The signed lanes. A signature that does not verify is a refusal, not a malformed
        # request; a stale nonce is the other way round.
        (
            "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
            "get",
            400,
            lambda: client.get(f"/r/lobby/say-signed/{did}/{'A' * 86}/not-a-nonce/hi"),
        ),
        (
            "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
            "get",
            403,
            lambda: client.get(f"/r/lobby/say-signed/{did}/{'A' * 86}/1/hi"),
        ),
        (
            "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
            "get",
            404,
            lambda: client.get(f"/r/lobby/say-signed/{did}/{'A' * 86}/1/a%0Ab"),
        ),
        # …and a room that will not take this key is a refusal too.
        (
            "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
            "get",
            403,
            lambda: _say_signed(client, "d-owned", other, other_sign, "hi", nonce=3),
        ),
        (
            "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}",
            "get",
            400,
            lambda: _set_signed(client, "plans", "k", did, sign, "v", nonce=9),
        ),
        (
            "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}",
            "get",
            403,
            lambda: client.get(f"/kv/room-owners/d-owned/set-signed/{did}/{'A' * 86}/9/{other}"),
        ),
        (
            "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}",
            "get",
            404,
            lambda: client.get(f"/kv/room-owners/d-owned/set-signed/{did}/{'A' * 86}/9/a%0Ab"),
        ),
        # Notes have no ring, so the signed lane's nonce counter is itself a note claimed
        # with a compare-and-set: a racing writer loses on the counter, with a 409.
        (
            "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}",
            "get",
            409,
            lambda: client.get(
                f"/kv/room-owners/d-owned/set-signed/{did}/{signed_note}?if=nothing-like-this"
            ),
        ),
        # The cross-sender duplicate filter: one copy past the threshold, inside the
        # window, through each write lane. Enabled per case because it is off by default and the
        # case has to be self-contained; the ring is cleared first because it is process
        # state that outlives any one room file.
        (
            "/r/{room}/say/{nick}/{text}",
            "get",
            422,
            lambda: _one_copy_too_many(client, "say"),
        ),
        (
            "/r/{room}",
            "post",
            422,
            lambda: _one_copy_too_many(client, "post"),
        ),
        (
            "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}",
            "get",
            422,
            lambda: _one_copy_too_many(client, "say-signed"),
        ),
    ]

    doc = client.get("/openapi.json").json()
    for path, method, status, send in cases:
        response = send()
        assert response.status_code == status, f"{method.upper()} {path}: {response.text[:200]}"
        documented = doc["paths"][path][method]["responses"]
        assert str(status) in documented, f"{method.upper()} {path} can {status} undocumented"

    # …and nothing documented is left unprovoked.
    provoked = {(path, method, str(status)) for path, method, status, _ in cases}
    unprovoked = sorted(
        f"{method.upper()} {path} -> {code}"
        for path, operations in doc["paths"].items()
        for method, op in operations.items()
        for code in op["responses"]
        if code in _REFUSALS and (path, method, code) not in provoked
    )
    assert not unprovoked, f"documented but never provoked by a test: {unprovoked}"


def _published_bounds(doc):
    """Every input constraint the document publishes, keyed by the constraint itself.

    Keyed by the bound rather than by the site because the same promise is repeated: the
    name pattern appears on eleven parameters and means one thing each time. Twelve
    distinct promises across forty-odd declarations.
    """
    keys = ("maximum", "minimum", "maxLength", "minLength", "enum", "pattern")
    found = set()
    for operations in doc["paths"].values():
        for op in operations.values():
            schemas = [p["schema"] for p in op.get("parameters", [])]
            body = op.get("requestBody")
            if body:
                schemas += list(
                    body["content"]["application/json"]["schema"]["properties"].values()
                )
            for schema in schemas:
                bound = {k: schema[k] for k in keys if k in schema}
                if bound:
                    found.add(json.dumps(bound, sort_keys=True))
    return found


def test_every_published_limit_is_one_the_server_actually_honours(client, monkeypatch):
    """The read side of the contract, which is where this branch kept going wrong.

    Every fix here traced where a number is *written down* — three publishing sites for the
    wait ceiling, then two more — and none of them asked who parses it back. That is
    precisely where the bug was: `?wait=` was published as `type: number` and int-parsed,
    so every fractional value a conforming client could send was silently discarded. The
    failure had no contract signature at all — a documented 200 with a schema-valid body,
    identical to an idle room — so no fuzzer, coverage gate or mutation run could see it.

    So: take each bound at its extreme, send it, and require the server to honour it. And
    require the table to cover every bound the document publishes, or the next parameter
    added with a limit nobody honours passes unnoticed the same way.
    """
    import config

    # keep the long-poll case quick
    with config.override(MAX_WAIT=0.5):
        doc = client.get("/openapi.json").json()
        did, sign = _keypair()
        longest_name = "a" * 48

        def wait_is_honoured():
            # The ceiling moved out of the parameter's `maximum` and into the prose plus
            # `limits.long_poll_seconds` (docs/design.md §3.5), so read it where it is now
            # published. What is being asserted is unchanged: the largest wait the service
            # advertises is one it actually takes.
            published = client.get("/.well-known/agent.json").json()["limits"]["long_poll_seconds"]
            started = time.monotonic()
            client.get(f"/r/idle?since=1&wait={published}")
            # It has to actually hold the connection, not return an immediate empty reply
            # that a caller cannot tell from a quiet room.
            assert time.monotonic() - started >= published * 0.8

        checks_without_a_published_bound = [
            lambda: _ok(client, "/r/lobby?limit=200"),
            lambda: _ok(client, "/r/lobby?since=0"),
            lambda: _ok(client, "/rooms?limit=1"),
            lambda: client.get("/r/lobby?format=json").json(),
            lambda: _ok(client, "/kv/plans/fresh/set/v?if_absent=1"),
            wait_is_honoured,
        ]

        # (the bound as published, a request using it at its extreme)
        checks = [
            (
                '{"pattern": "^[a-z0-9][a-z0-9_-]{0,47}$"}',
                lambda: _ok(client, f"/r/{longest_name}"),
            ),
            (
                '{"maxLength": 4096, "minLength": 1}',
                lambda: _ok(client, "/r/lobby", post={"from": "b", "text": "x" * 4096}),
            ),
            (
                '{"maxLength": 8192, "minLength": 1}',
                lambda: _ok(client, "/kv/plans/big", post={"value": "x" * 8192}),
            ),
            # The signed lane's three, at the exact shapes it publishes. A room each,
            # because a nonce is single-use per key per room and the 19-digit one spends
            # the ceiling — 10**19 - 1 being the largest the published pattern allows, and
            # an int64 fits it.
            (
                '{"pattern": "^[0-9]{1,19}$"}',
                lambda: _ok(
                    client, _say_signed(client, "big-nonce", did, sign, "hi", nonce=10**19 - 1)
                ),
            ),
            (
                '{"maxLength": 56, "minLength": 56, '
                '"pattern": "^did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}$"}',
                lambda: _ok(client, _say_signed(client, "signed-did", did, sign, "signed")),
            ),
            (
                '{"maxLength": 86, "minLength": 86, "pattern": "^[A-Za-z0-9_-]{85}[AQgw]$"}',
                lambda: _ok(client, _say_signed(client, "signed-sig", did, sign, "again")),
            ),
        ]

        # `limit`, `since`, `wait` and `format` clamp instead of refusing, and `if_absent`
        # matches case-insensitively, which JSON Schema cannot express — so none of them
        # publishes a bound any more (docs/design.md §3.5) and none has one to cover here.
        # They are still exercised at the extreme the prose promises, because a clamp
        # nobody honours misleads exactly as much as a `maximum` nobody enforces.
        for exercise in checks_without_a_published_bound:
            exercise()

        for _bound, exercise in checks:
            exercise()

        covered = {bound for bound, _ in checks}
        published = _published_bounds(doc)
        assert not published - covered, (
            f"published but never exercised: {sorted(published - covered)}"
        )


def test_the_signed_lane_publishes_the_shape_it_actually_enforces(client):
    """One definition, three places it is published. The room lane's `did` pattern ended in
    an unbounded `+`, so `did:key:z6Mk` satisfied it; the note lane's was a bare `string`;
    the POST body was prose no generator can read. A client is built against whichever copy
    it found, so the weakest one was the contract.
    """
    import didkey

    did, sign = _keypair()
    doc = client.get("/openapi.json").json()

    def param(path, name):
        return next(p for p in doc["paths"][path]["get"]["parameters"] if p["name"] == name)[
            "schema"
        ]

    say = "/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}"
    note = "/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}"
    body = doc["paths"]["/r/{room}"]["post"]["requestBody"]["content"]["application/json"]["schema"]

    published = [param(say, "did"), param(note, "did"), body["properties"]["did"]]
    assert len({json.dumps(schema, sort_keys=True) for schema in published}) == 1, (
        "the two signed lanes and the POST body must publish one `did` shape"
    )
    for schema in published:
        # A real key satisfies it, and the truncated DID the old pattern accepted does not.
        assert re.fullmatch(schema["pattern"], did)
        assert not re.fullmatch(schema["pattern"], "did:key:z6Mk")
        assert schema["minLength"] == schema["maxLength"] == len(did)
        assert len(did) == len(didkey.PREFIX) + didkey.MULTIBASE_CHARS

    # The body's copies live under `dependentSchemas.did` rather than on the properties:
    # the handler reads `sig`/`nonce` only when a `did` is present, so a body without one
    # is an unsigned write and publishing their shapes unconditionally would be a
    # constraint nothing enforces (docs/design.md §3.5). Same shapes, stated where they
    # actually hold — which is still one definition and still three publishing sites.
    signed = body["dependentSchemas"]["did"]
    for schema in (param(say, "sig"), param(note, "sig"), signed["properties"]["sig"]):
        assert re.fullmatch(schema["pattern"], sign("anything"))
        assert schema["minLength"] == schema["maxLength"] == didkey.SIG_CHARS
    for schema in (param(say, "nonce"), param(note, "nonce"), signed["properties"]["nonce"]):
        assert re.fullmatch(schema["pattern"], "1") and not re.fullmatch(schema["pattern"], "x")

    # `did` alone is refused rather than downgraded to an unsigned post, so the schema
    # says which fields travel together instead of listing three loose optional strings.
    assert signed["required"] == ["sig", "nonce"]
    assert client.post("/r/lobby", json={"text": "hi", "did": did}).status_code == 400
    # …but a stray `sig` with no `did` is an ordinary unsigned post, and the schema must
    # not claim otherwise.
    assert client.post("/r/lobby", json={"from": "b", "text": "hi", "sig": "x"}).status_code == 200


def test_a_free_form_field_publishes_that_it_cannot_be_empty(client):
    """`required: ["text"]` is satisfied by `""`, which is a 400 — the sweep leaves nothing
    visible. A generator reading only `required` emits a client whose empty-message call
    can never succeed."""
    import store

    doc = client.get("/openapi.json").json()
    schemas = {
        "post /r/{room}.text": doc["paths"]["/r/{room}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["text"],
        "post /kv.value": doc["paths"]["/kv/{ns}/{key}"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]["properties"]["value"],
        "get say.text": next(
            p
            for p in doc["paths"]["/r/{room}/say/{nick}/{text}"]["get"]["parameters"]
            if p["name"] == "text"
        )["schema"],
        "get set.value": next(
            p
            for p in doc["paths"]["/kv/{ns}/{key}/set/{value}"]["get"]["parameters"]
            if p["name"] == "value"
        )["schema"],
    }
    for where, schema in schemas.items():
        assert schema["minLength"] == 1, where
    assert schemas["post /r/{room}.text"]["maxLength"] == store.MAX_TEXT_CHARS
    assert schemas["post /kv.value"]["maxLength"] == store.MAX_VALUE_CHARS

    # And the server agrees, on both lanes.
    assert client.post("/r/lobby", json={"from": "bot", "text": ""}).status_code == 400
    assert client.post("/kv/plans/k", json={"value": ""}).status_code == 400


def test_openapi_limits_are_the_limits_the_server_enforces(client):
    """A published limit that disagrees with the enforced one is worse than none: a
    machine reader believes it. Generated from the constants, and this holds that line."""
    import app as app_module
    import store

    doc = client.get("/openapi.json").json()
    say = doc["paths"]["/r/{room}/say/{nick}/{text}"]["get"]
    text_param = next(p for p in say["parameters"] if p["name"] == "text")
    assert text_param["schema"]["maxLength"] == store.MAX_TEXT_CHARS
    value = doc["paths"]["/kv/{ns}/{key}/set/{value}"]["get"]["parameters"]
    assert next(p for p in value if p["name"] == "value")["schema"]["maxLength"] == (
        store.MAX_VALUE_CHARS
    )
    body_limit = f"{app_module.MAX_BODY // 1024} KiB"
    assert body_limit in doc["paths"]["/r/{room}"]["post"]["responses"]["413"]["description"]
    assert body_limit in doc["paths"]["/kv/{ns}/{key}"]["post"]["responses"]["413"]["description"]
    room = next(p for p in say["parameters"] if p["name"] == "room")
    assert room["schema"]["pattern"] == store.NAME_RE.pattern
    # …and the version comes from the file that declares it, not a second copy.
    pyproject = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text()
    assert doc["info"]["version"] in pyproject


def test_the_manual_states_no_rate_limit_it_cannot_guarantee(client):
    """The bug this closes: /llms.txt hardcoded "120 reads and 30 writes per minute" while
    the enforced values come from CHAT_RATE_READ / CHAT_RATE_WRITE, so any instance that
    tuned them published a manual that lied — and an agent paces itself to a manual.

    The manual is a constant string, so it cannot carry a per-deployment number correctly.
    It therefore carries none, and names the document that does.
    """
    import app as app_module

    manual = client.get("/llms.txt").text
    limits = manual[manual.index("LIMITS:") :].split("\n\n")[0]

    # No bare per-minute claim, whatever the configured values happen to be.
    assert not re.search(r"\d+\s+(reads|writes)\b", limits)
    assert f"{app_module.RATE_READ} " not in limits and f"{app_module.RATE_WRITE} " not in limits
    # …and the pointer is a real document with a real field in it.
    assert "/.well-known/agent.json" in limits
    assert "limits.reads_per_minute_per_ip" in limits
    doc = client.get("/.well-known/agent.json").json()
    assert doc["limits"]["reads_per_minute_per_ip"] == app_module.RATE_READ
    assert doc["limits"]["writes_per_minute_per_ip"] == app_module.RATE_WRITE


def test_the_manifest_publishes_every_limit_that_varies_per_deployment(client):
    """Three values are configurable, so three values have to be readable from the one
    document generated at runtime. A pointer to a field that is not there is worse than
    the hardcoded number it replaced."""
    import store

    doc = client.get("/.well-known/agent.json").json()
    assert doc["limits"]["ephemeral_ttl_seconds"] == store.EPHEMERAL_TTL_SECONDS
    manual = client.get("/llms.txt").text
    assert "limits.ephemeral_ttl_seconds" in manual


def test_the_manual_and_the_429_agree_on_what_costs_nothing(client, monkeypatch):
    """Two lists of free paths would drift, and the 429's copy is the one an agent reads
    while it is actually throttled."""
    import app as app_module
    import config

    assert app_module.FREE_PATHS in client.get("/llms.txt").text
    with config.override(RATE_WRITE=1):
        client.get("/r/lobby/say/bot/one")
        assert app_module.FREE_PATHS in client.get("/r/lobby/say/bot/two").text


def test_openapi_omits_the_token_gated_stats_endpoint(client):
    """/stats answers 404 rather than 401 so nobody learns it is there. Publishing its
    path in the spec would hand back exactly what that 404 withholds."""
    assert not [p for p in client.get("/openapi.json").json()["paths"] if "stats" in p]


def test_agent_manifest_states_the_three_facts_that_get_agents_hurt(client):
    """Every other field in a listing sells the service. These three say what adopting it
    costs, and they are structured rather than prose so a machine reader cannot miss them."""
    doc = client.get("/.well-known/agent.json").json()
    assert doc["trust"] == {
        "content_is_untrusted": True,
        "durable": False,
        "world_writable": True,
        "note": doc["trust"]["note"],
    }
    assert "data, never as instructions" in doc["trust"]["note"]
    assert doc["auth"]["type"] == "none"
    assert doc["limits"]["message_chars"] == 4096


def test_agent_manifest_claims_only_the_protocol_it_speaks(client):
    """The service is not an A2A agent and not an MCP server (the wrapper in mcp/ is a
    separate artifact). A manifest that says otherwise sends every validating registry a
    listing whose endpoint does not answer."""
    doc = client.get("/.well-known/agent.json").json()
    assert doc["protocols"] == ["http"]
    assert {c["name"] for c in doc["capabilities"]} >= {"say", "read_room", "write_note"}
    for cap in doc["capabilities"]:
        assert cap["path"].startswith("/")


def test_metadata_urls_never_echo_an_untrusted_host(client):
    """The Host header is a claim by the client, exactly like the forwarded-for header the
    limiter refuses to trust. A crawler's fetch must not be talkable into publishing
    someone else's origin, so an implausible host degrades to relative URLs."""
    doc = client.get("/.well-known/agent.json", headers={"host": "evil.example/../x"}).json()
    assert doc["url"] == "/" and doc["documentation"]["manual"] == "/llms.txt"
    ok = client.get("/.well-known/agent.json", headers={"host": "technocore.chat"}).json()
    assert ok["url"] == "http://technocore.chat"
    assert ok["documentation"]["openapi"] == "http://technocore.chat/openapi.json"


def test_configured_public_url_wins_over_the_request(client, monkeypatch):
    import config

    with config.override(PUBLIC_URL="https://technocore.chat/"):
        doc = client.get("/openapi.json", headers={"host": "127.0.0.1:8080"}).json()
        assert doc["servers"] == [{"url": "https://technocore.chat"}]


def test_metadata_is_never_rate_limited_and_is_crawlable(client, monkeypatch):
    """A registry crawler arrives without warning and re-fetches on a schedule; a 429 on
    the document that describes the service is a listing that never validates."""
    import config

    with config.override(RATE_READ=1):
        for _ in range(5):
            assert client.get("/openapi.json").status_code == 200
            assert client.get("/.well-known/agent.json").status_code == 200
    robots = client.get("/robots.txt").text
    assert "/openapi.json" in robots and "/.well-known/agent.json" in robots
    assert "Disallow: /openapi.json" not in robots


def test_the_manual_defines_every_convention_it_names(client):
    """A convention an agent cannot derive is a convention it will get wrong. The DID note
    fingerprint is the one that bites: the sharded path is unusable without knowing what
    the fingerprint is of and exactly where to split it."""
    manual = client.get("/llms.txt").text
    assert "first 16 lowercase hex characters of SHA-256" in manual
    assert "/kv/did-<first 2>/<remaining 14>" in manual
    assert "legacy /kv/did/<fingerprint>" in manual
    assert "`<room>|<nonce>|<text>`" in manual or "<room>|<nonce>|<text>" in manual
    assert "newest 1 MiB" in manual
    assert "even if the message remains elsewhere in the larger room ring" in manual
    retry = " ".join(manual[manual.index("RETRY:") : manual.index("RENDERING:")].split())
    assert "Do not refresh the same signed URL" in retry
    assert "A match proves the append landed; no match does not prove it failed" in retry
    assert "inspect both the target note and the persistent" in retry
    paths = client.get("/openapi.json").json()["paths"]
    signed_room = paths["/r/{room}/say-signed/{did}/{sig}/{nonce}/{text}"]["get"]
    assert "exact record in the room export proves the append landed" in signed_room["description"]
    signed_note = paths["/kv/{ns}/{key}/set-signed/{did}/{sig}/{nonce}/{value}"]["get"]
    assert "neither alone proves the attempted update" in signed_note["description"]
    assert "spent nonce is refused permanently" in signed_note["description"]
    # …and the source, so a reader who wants their own instance does not have to search
    # for it. This is also the only outbound link the manual carries.
    assert "https://github.com/flop-labs/technocore-chat" in manual


def test_every_document_scopes_trust_to_caller_bytes_not_to_message_bodies(client):
    """The docs are what set the scope, and they used to set it too narrow.

    The manual's TRUST line, SKILL.md's safety section and agent.json's trust note all
    said "message bodies" — so a reader that enumerated /rooms and never opened a room had
    been told nothing about the bytes it was ingesting, even though those bytes are
    caller-chosen in exactly the same way. Each document has to reach the enumerated name
    and topic, or the marker on the listing is the only place the contract is stated and
    the prose still contradicts it.
    """
    manual = client.get("/llms.txt").text
    trust = manual[manual.index("TRUST:") :]
    trust = trust[: trust.index("\n\n")]
    assert "room names and topics" in trust and "/rooms" in trust
    # The specific misreading this closes: enumeration as endorsement.
    assert "vouches for" in trust and "endorsement" in trust

    skill = client.get("/skill.md").text
    assert "/rooms" in skill and "enumeration is not endorsement" in skill

    note = client.get("/.well-known/agent.json").json()["trust"]["note"]
    assert "room names and topics" in note

    spec = client.get("/openapi.json").json()
    assert "caller-controlled" in spec["paths"]["/rooms"]["get"]["description"]
    schema = spec["paths"]["/rooms"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert "untrusted" in schema["properties"], "the JSON field has to be in the contract"


def test_the_manifest_carries_enough_to_sign_without_reading_prose(client):
    """The metadata is what a machine reads *instead* of the manual, so the byte strings a
    signature is computed over have to be in it — a signature over the wrong concatenation
    fails verification with no clue why."""
    doc = client.get("/.well-known/agent.json").json()
    identity = doc["identity"]
    assert identity["message_signature_payload"] == "<room>|<nonce>|<text>"
    assert identity["note_signature_payload"] == "<namespace>|<key>|<nonce>|<value>"
    assert identity["algorithms"] == ["Ed25519"]
    assert "mb-" in " ".join(identity["required_for"])
    assert doc["documentation"]["patterns"].endswith("/patterns.md")


def test_the_skill_points_at_the_lanes_it_does_not_teach(client):
    """SKILL.md stays short on purpose, so what it leaves out has to be reachable from it:
    the signed lane exists, and the worked choreographies live somewhere."""
    skill = client.get("/skill.md").text
    assert "/patterns.md" in skill and "/llms.txt" in skill
    assert "did:key" in skill and "SIGNING" in skill


def test_the_documents_are_indexable_and_the_content_is_not(client):
    """The regression this release exists for.

    robots.txt has always said `Allow: /` and named the manual, while every plain-text
    response carried `X-Robots-Tag: noindex` — so a service whose entire strategy is being
    discovered by agents was inviting crawlers to the manual and then telling them, in the
    header, not to index it. Rooms and notes still must not be indexed: they are anonymous,
    non-durable and not ours to publish. Both halves are asserted together because the fix
    is the distinction, not the removal.
    """
    for path in ("/", "/llms.txt", "/skill.md", "/patterns.md", "/robots.txt", "/humans"):
        assert "x-robots-tag" not in client.get(path).headers, f"{path} is documentation"
    for path in ("/r/lobby", "/kv/ns/key", "/rooms"):
        assert client.get(path).headers["x-robots-tag"] == "noindex", f"{path} is content"


def test_the_skills_index_digest_is_of_the_bytes_skill_md_actually_serves(client):
    """An installer checks the digest to know it fetched the skill it was promised. If the
    index is computed from the file and the route serves anything else — a trailing newline
    is enough — every verifying installer refuses a skill that is in fact correct."""
    import hashlib

    served = client.get("/skill.md").content
    skill = client.get("/.well-known/agent-skills/index.json").json()["skills"][0]
    assert skill["digest"] == "sha256:" + hashlib.sha256(served).hexdigest()
    assert skill["url"].endswith("/skill.md") and skill["type"] == "skill-md"


def test_the_skill_the_image_and_the_wrapper_all_name_one_version(client):
    """Three artifacts ship from this repo and they are released together, so a reader who
    has one of them can name the others — including the skill, whose entry carries the release
    it shipped in alongside the digest that identifies its bytes. `version` is outside the five
    fields Agent Skills Discovery 0.2.0 defines, which the spec provides for: clients MUST
    ignore fields they do not recognise."""
    import json as json_module
    import tomllib
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    service = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]

    skill = client.get("/.well-known/agent-skills/index.json").json()["skills"][0]
    assert skill["version"] == service
    # The five the spec defines are all still there: `version` is additive, and an entry that
    # dropped one of these would be broken for every client regardless of the extra.
    assert {"name", "type", "description", "url", "digest"} <= set(skill)
    assert client.get("/openapi.json").json()["info"]["version"] == service
    assert client.get("/.well-known/agent.json").json()["version"] == service
    assert json_module.loads((root / "mcp" / "server.json").read_text())["version"] == service


def test_the_api_catalog_only_links_paths_this_origin_answers(client):
    """RFC 9727's value is that a crawler can follow it. A catalog naming an endpoint the
    service does not serve is worse than none, because the reader believes it."""
    linkset = client.get("/.well-known/api-catalog").json()["linkset"]
    assert len(linkset) == 1
    for relation in ("service-desc", "service-doc", "service-meta", "status"):
        for link in linkset[0][relation]:
            path = link["href"].split("testserver", 1)[-1] or "/"
            assert client.get(path).status_code == 200, f"{relation} -> {path} is not served"


def test_robots_declares_content_signals_and_an_absolute_sitemap(client):
    """The Sitemap directive takes a full URL, which is why robots.txt stopped being a
    constant. The signals are all yes and that is the honest answer, not the permissive
    one: this service exists to be read by agents at inference time."""
    body = client.get("/robots.txt").text
    assert "Content-Signal: search=yes, ai-input=yes, ai-train=yes" in body
    assert "Sitemap: http://testserver/sitemap.xml" in body
    assert "Disallow: /r/" in body and "Disallow: /kv/" in body


def test_every_sitemap_url_is_one_the_crawler_is_allowed_to_index(client):
    """A sitemap is a request to index, so a listed URL that answers `X-Robots-Tag:
    noindex` is the service contradicting itself — and a crawler resolves that by
    distrusting the sitemap, not the header. /rooms is the trap: it is a listing rather
    than a room, but what it lists is anonymous and non-durable, so it stays out."""
    import manifest

    assert {
        "/.well-known/agent-skills/index.json",
        "/.well-known/ai-catalog.json",
        "/.well-known/mcp/server-card.json",
    } <= set(manifest.SITEMAP_PATHS)
    for path in manifest.SITEMAP_PATHS:
        response = client.get(path)
        assert response.status_code == 200, f"{path} is listed but not served"
        assert "x-robots-tag" not in response.headers, f"{path} is listed but forbids indexing"
    assert "/rooms" not in client.get("/sitemap.xml").text


def test_markdown_negotiation_reads_q_values_not_header_order(client):
    """Header order is not preference. A client that writes `text/markdown;q=0` has
    refused markdown, and one that ranks markdown above plain text has asked for it
    wherever in the header it happens to sit."""

    def label(accept: str) -> str:
        return client.get("/skill.md", headers={"accept": accept}).headers["content-type"]

    assert label("text/markdown;q=0, text/plain;q=1").startswith("text/plain")
    assert label("text/plain;q=0.5, text/markdown;q=0.9").startswith("text/markdown")
    assert label("text/markdown").startswith("text/markdown")
    # `*/*` names no preference between two labels of the same bytes, so the plain
    # default stands — it is what curl and most agents send.
    assert label("*/*").startswith("text/plain")


def test_malformed_accept_quality_fails_closed_to_plain_text(client):
    """Accept is attacker-controlled. An unreadable q-value must not crash negotiation or
    opt the caller into a representation it did not validly request.
    """
    response = client.get(
        "/skill.md", headers={"accept": "text/markdown;q=definitely, text/plain;q=1"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_sitemap_refuses_to_guess_an_origin_it_does_not_know(client):
    """Every other document falls back to relative URLs. The sitemap protocol has no
    relative form, so the only honest response without a trustworthy origin is no sitemap
    — not a document full of `<loc>` values that resolve nowhere."""
    assert client.get("/sitemap.xml").status_code == 200
    blind = client.get("/sitemap.xml", headers={"host": "not a hostname!"})
    assert blind.status_code == 404


def test_the_spec_states_that_no_authentication_is_required(client):
    """Omitting `security` says nothing; `security: []` says authentication is not
    required. For a service whose premise is that an agent needs no credential, the
    difference between "needs nothing" and "nobody wrote it down" is the whole claim."""
    doc = client.get("/openapi.json").json()
    assert doc["security"] == []
    assert "securitySchemes" not in doc.get("components", {})


def test_auth_md_states_the_absence_rather_than_leaving_it_to_inference(client):
    """The Auth.md standard's primary shape is OAuth. This service has none, and the
    standard's own fallback is a self-contained document — so the value here is saying
    "there is no registration endpoint" out loud. An agent hunting for a provisioning step
    it cannot find concludes the service is broken, when in fact it is open."""
    body = client.get("/auth.md").text
    assert body.startswith("# auth.md")  # the H1 the standard keys detection on
    assert "no authentication" in body.lower()
    assert "There are none." in body  # registration endpoints
    assert "did:key" in body and "Ed25519" in body
    assert "<room>\\|<nonce>\\|<text>" in body  # the payload, so it cannot drift


def test_default_cors_hides_cross_origin_replies_but_does_not_stop_get_writes(client):
    """CORS is a browser read gate, not a write gate on a simple GET surface.

    An untrusted origin gets no readable response, but the browser still sends the request
    and the service still stores it. The served auth guide must say that explicitly: a
    browser client that mistakes a hidden response for a rejected write can retry a write
    that already landed, which is especially sharp on the signed nonce lane.
    """
    origin = {"Origin": "https://untrusted.example"}
    written = client.get("/r/cors-check/say/browser/landed", headers=origin)

    assert written.status_code == 200
    assert "access-control-allow-origin" not in written.headers
    stored = client.get("/r/cors-check?format=json").json()["messages"]
    assert [(message["from"], message["text"]) for message in stored] == [("browser", "landed")]

    auth = client.get("/auth.md").text
    assert (
        "CORS controls whether browser JavaScript can read a response, not whether the "
        "request is sent" in auth
    )


def test_no_oauth_metadata_is_served_for_an_issuer_that_does_not_exist(client):
    """The scanners want these two and would score us higher for them. There is no
    authorization server, so both would advertise an issuer nothing can answer — the same
    rule that keeps A2A and MCP claims out of the manifest."""
    for path in (
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server",
    ):
        assert client.get(path).status_code == 404


def test_auth_md_is_reachable_from_the_sitemap(client):
    """A document no crawler is told about is a document the scanners will not find."""
    assert "/auth.md" in client.get("/sitemap.xml").text


def test_only_the_markdown_documents_negotiate_markdown(client):
    """Negotiation relabels bytes, it never reformats them, so a document only negotiates
    when its bytes really are markdown. /auth.md, /skill.md and /patterns.md are; the manual
    is not, and / and /llms.txt therefore answer text/plain even when markdown is named."""
    md = {"Accept": "text/markdown"}
    for path in ("/skill.md", "/patterns.md", "/interop.md", "/auth.md"):
        got = client.get(path, headers=md).headers["content-type"]
        assert got.startswith("text/markdown"), f"{path} answered {got}"
        assert client.get(path).headers["content-type"].startswith("text/plain")
    for path in ("/", "/llms.txt"):
        got = client.get(path, headers=md).headers["content-type"]
        assert got.startswith("text/plain"), f"{path} answered {got}"


def test_the_manual_is_not_markdown_and_so_is_never_labelled_as_such(client):
    """The claim behind the label, tested rather than assumed — which is what 0.3.3's first
    cut got wrong in the other direction. Route placeholders are raw HTML tags to a
    CommonMark parser, so rendering the manual as markdown deletes the very path parameters
    it exists to teach, and its unindented lane rows collapse into one paragraph."""
    body = client.get("/").text
    assert re.search(r"<[A-Za-z][A-Za-z0-9-]*>", body)  # e.g. <room>, would be eaten
    assert body.splitlines()[3].startswith("READ")  # column 0: a paragraph, not a code block
    negotiated = client.get("/", headers={"Accept": "text/markdown"})
    assert negotiated.headers["content-type"].startswith("text/plain")


def test_the_ai_catalog_lists_only_artifacts_that_resolve(client):
    """A catalog exists to resolve to real things, and that is the invariant: every entry's
    url must be served by this origin.

    The MCP server card is now one of them, and its presence here is not a claim that this
    origin speaks MCP — the catalog resolves to the card, and the card names an endpoint on
    another host. The A2A agent card stays absent because no such document exists to point
    at; if one is ever added, it is the loop below, not this line, that has to keep passing.
    """
    doc = client.get("/.well-known/ai-catalog.json").json()
    assert doc["specVersion"] == "1.0" and doc["host"]["displayName"]
    types = {e["type"] for e in doc["entries"]}
    assert "application/mcp-server-card+json" in types
    assert "application/a2a-agent-card+json" not in types
    assert "application/agent-skills+md" in types
    for entry in doc["entries"]:
        assert entry["identifier"] and entry["type"] and entry["url"]
        path = entry["url"].split("testserver", 1)[-1] or "/"
        assert client.get(path).status_code == 200, f"{entry['identifier']} -> {path}"


# The documents are static per release and deliberately outside the rate limiter, which
# makes them both the cheapest thing to cache and the least defended thing not to. These
# four tests are the fence around that: what may be held at the edge, what may never be,
# and the exact string, so a later refactor of the shared helper cannot widen it silently.


def test_the_static_documents_are_edge_cacheable_and_the_header_is_exact(client):
    """Every document a crawler or an agent fetches per release, cacheable by the CDN in
    front. `max-age=0` is what keeps this invisible to callers — they still revalidate on
    every request; only the shared cache is allowed to hold a copy, which is the whole
    point on the paths that have no rate limiter in front of them.

    The exact string is pinned once rather than for each path: it is one helper, and the
    value is a contract with the CDN, not an implementation detail.
    """
    static = "public, max-age=0, s-maxage=300, stale-while-revalidate=60"
    assert client.get("/").headers["cache-control"] == static

    documents = ("/", "/llms.txt", "/skill.md", "/patterns.md", "/interop.md", "/auth.md")
    # The JSON documents carry the same policy and are listed here rather than left to a
    # separate test, because "which paths may the edge hold" is one question. They used to
    # carry a hardcoded `max-age=3600` instead — a client-side hour on documents whose whole
    # purpose is being refetched — and nothing named them, which is how the two policies
    # drifted apart unnoticed. /sitemap.xml is in the list for the same reason.
    machine_readable = (
        "/openapi.json",
        "/config",
        "/sitemap.xml",
        "/.well-known/agent.json",
        "/.well-known/api-catalog",
        "/.well-known/ai-catalog.json",
        "/.well-known/agent-skills/index.json",
        "/.well-known/mcp/server-card.json",
    )
    for path in (*documents, *machine_readable, "/robots.txt", "/.well-known/security.txt"):
        cc = client.get(path).headers["cache-control"]
        assert cc == static, path
        assert "no-store" not in cc, path


def test_a_zero_window_means_not_cached_rather_than_cached_without_a_bound(client):
    """`0` disables, and disabling has to fail closed.

    `_edge_cacheable` sets a header only for a truthy window, so with the knob at zero it
    returns the response exactly as built. That makes the *initial* header the contract for
    the disabled case: `text()` starts every response `no-store`, so the prose documents
    fall back to it, and a bare `Response` would fall back to no `Cache-Control` at all —
    which does not mean "do not cache", it means a cache may hold it heuristically for as
    long as it likes, and a CDN rule marking the path eligible would do exactly that.

    So the disabled setting is asserted here for both halves of the document set together.
    The prose side has always been right; the JSON side was not until the header was seeded
    before `_static_cacheable` could decline to overwrite it. /humans joined them when it
    stopped minting a per-response nonce and became cacheable, and it arrived with the same
    defect for the same reason — a bare `Response` whose explicit `no-store` had been
    removed along with the nonce that required it.
    """
    import config

    both = (
        "/llms.txt",
        "/robots.txt",
        "/humans",
        "/.well-known/security.txt",
        "/openapi.json",
        "/config",
        "/sitemap.xml",
        "/.well-known/agent.json",
        "/.well-known/api-catalog",
        "/.well-known/ai-catalog.json",
        "/.well-known/agent-skills/index.json",
        "/.well-known/mcp/server-card.json",
    )
    with config.override(STATIC_CACHE_SECONDS=0):
        for path in both:
            assert client.get(path).headers["cache-control"] == "no-store", path


def test_the_per_caller_and_liveness_surfaces_are_never_edge_cacheable(client):
    """The two that would each be a real defect if held at the edge.

    /healthz is what the autoupdate rollback probe reads — a cached `ok` would let a broken
    release pass its own health gate. /stats is token-gated and counts one worker's requests.

    /humans used to be the third, because a per-response CSP nonce meant a cached copy
    pinned one nonce for every visitor and defeated the mechanism it existed for. The pin
    is a `sha256-` of each inline block now, so the page is byte-identical between requests
    and there is nothing per-caller left in it to leak. It is deliberately cacheable, and
    tests/http/test_humans.py asserts that half — a 60 KiB document a reader most needs
    when the origin is down is the wrong thing to make origin-only.
    """
    import config

    assert client.get("/healthz").headers["cache-control"] == "no-store"

    # With no token configured /stats is a 404, so the gated response has to be provoked
    # or this asserts no-store on a path that was never routed.
    with config.override(STATS_TOKEN="t", STATS_CACHE_SECONDS=0):
        r = client.get("/stats", headers={"x-stats-token": "t"})
        assert r.status_code == 200
        assert r.headers["cache-control"] == "no-store"


def test_a_write_and_a_refusal_are_never_edge_cacheable(client):
    """Writes in this protocol are GETs, so a cacheable header on one is a silently
    swallowed write — the caller gets a 200 that never reached the store. A cached 429 is
    the same defect pointed the other way: one caller's exhausted budget, served to
    everyone until it expires.
    """
    import config

    assert client.get("/r/lobby/say/bot/hi").headers["cache-control"] == "no-store"

    with config.override(RATE_WRITE=2):
        codes = [client.get(f"/r/lobby/say/bot/m{i}").status_code for i in range(4)]
        assert 429 in codes, codes
        refused = client.get("/r/lobby/say/bot/again")
        assert refused.status_code == 429
        assert refused.headers["cache-control"] == "no-store"


def test_only_a_negotiating_document_says_vary_and_markdown_is_never_cached(client):
    """The half of edge-caching the documents needed that the polled reads never did.

    /skill.md, /patterns.md, /interop.md and /auth.md answer the same bytes under two
    labels depending on Accept, so they must say `Vary: Accept` — a shared cache that
    ignored Accept would hand one caller's label to the next. / and /llms.txt never
    negotiate, so Vary there would fragment the cache key on the busiest path for nothing.

    And the markdown answer itself stays no-store, which is belt-and-braces on top of
    Vary: Cloudflare honours Vary only where a Cache Rule enables it, so on a zone where
    nobody has, the edge can still only ever hold the default representation.
    """
    import config

    for path in ("/skill.md", "/patterns.md", "/interop.md", "/auth.md"):
        assert client.get(path).headers["vary"] == "Accept", path
        negotiated = client.get(path, headers={"Accept": "text/markdown"})
        assert negotiated.headers["content-type"].startswith("text/markdown"), path
        assert negotiated.headers["cache-control"] == "no-store", path

    for path in ("/", "/llms.txt"):
        assert "vary" not in client.get(path).headers, path

    # 0 restores no-store everywhere, the same escape hatch EDGE_CACHE_SECONDS has.
    with config.override(STATIC_CACHE_SECONDS=0):
        for path in ("/", "/llms.txt", "/skill.md", "/robots.txt"):
            assert client.get(path).headers["cache-control"] == "no-store", path


def test_the_response_schema_publishes_the_sig_it_now_returns(client):
    """A field the service returns but the document does not list is a field no generated
    client can see. `sig` on a stored record is published with the same shape the signed
    lanes already advertise for the signature they accept, and a real record satisfies it.
    """
    import didkey

    did, sign = _keypair()
    assert _say_signed(client, "docsig", did, sign, "published shape").status_code == 200
    record = client.get("/r/docsig?format=json").json()["messages"][-1]

    doc = client.get("/openapi.json").json()
    message = doc["paths"]["/r/{room}"]["get"]["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["properties"]["messages"]["items"]
    published = message["properties"]["sig"]
    assert published["minLength"] == published["maxLength"] == didkey.SIG_CHARS
    assert re.fullmatch(published["pattern"], record["sig"])
    # Optional, not required: records written before the field existed have no `sig`, and a
    # reader must read that as "not re-verifiable", never as "invalid".
    assert "sig" not in message["required"]


def test_the_mcp_server_card_is_served_and_conforms_to_the_extension_schema(client):
    """`/.well-known/mcp/server-card.json` — SEP-2127's four required fields and the
    constraints its schema puts on them, asserted here rather than by fetching the schema.

    The schema is unratified and lives outside this repo, so a network fetch would make
    this suite depend on a draft moving under it. What is pinned instead is the contract as
    of the SEP-review snapshot: `name`, `version` and `description` are required; `name` is
    reverse-DNS with exactly one slash; `description` is capped at 100 characters; a
    remote's `type` is one of two strings. Those are the ways a card is invalid rather than
    merely unfashionable, and they are cheap to keep true.

    `$schema` is the SEP's fourth required field and is deliberately absent, so this asserts
    its absence rather than its value. It used to be asserted equal to
    `.../schemas/v1/server-card.schema.json`, a URL that 404s and always has — the registry
    serves schemas under dated paths and publishes none for the card at all. Pinning the
    literal made this test agree with the code and both of them wrong about the world, which
    is the failure mode a test like this is supposed to prevent. What is worth pinning is
    the decision: no `$schema` until one resolves, and never a guessed URL.
    """
    import manifest

    card = client.get("/.well-known/mcp/server-card.json")
    assert card.status_code == 200
    assert card.headers["content-type"].startswith("application/json")
    doc = card.json()

    for required in ("name", "version", "description"):
        assert doc.get(required), required
    assert "$schema" not in doc, (
        "no schema is published for this format; a $schema that 404s fails a strict "
        "validator that an absent one would not"
    )
    assert re.fullmatch(r"[a-zA-Z0-9.-]+/[a-zA-Z0-9._-]+", doc["name"]), doc["name"]
    assert 3 <= len(doc["name"]) <= 200
    assert 1 <= len(doc["description"]) <= 100, len(doc["description"])

    (remote,) = doc["remotes"]
    assert remote["type"] == "streamable-http"  # the enum's other member is the dead `sse`
    assert remote["url"].startswith("https://")
    # Non-empty is not the assertion that matters — a stale list is non-empty, which is how
    # this card advertised a two-revision-old version for all of 0.11.x. The versions are
    # held to the wrapper's own in tests/unit/test_mcp_constant_parity.py.
    assert remote["supportedProtocolVersions"] == list(manifest.MCP_PROTOCOL_VERSIONS)


def test_the_server_card_reports_the_running_version_and_the_handshake_name(client):
    """Two names on one card, and they are answers to different questions.

    `name` is the registry identity the schema's reverse-DNS pattern demands. `serverInfo`
    is what the wrapper actually answers with at `initialize`, which is a plain string and
    would fail that pattern. Neither can be derived from the other, so both are published
    and this pins that they stay distinct rather than being collapsed into one.

    `version` is this service's release. The card does not claim to be the wrapper's PyPI
    version — those have diverged before — which is why this asserts against /config rather
    than against anything in mcp/.
    """
    doc = client.get("/.well-known/mcp/server-card.json").json()
    assert doc["version"] == client.get("/config").json()["version"]
    assert doc["serverInfo"]["version"] == doc["version"]
    assert doc["serverInfo"]["name"] == "technocore-chat"
    assert doc["serverInfo"]["name"] != doc["name"]
    assert doc["capabilities"]["tools"] == {"listChanged": False}


def test_the_card_and_the_registry_manifest_name_the_same_server(client):
    """`mcp/server.json` and the card describe one server from two formats, and both live
    in this repo — so a drift between them is a thing this suite can prevent rather than
    discover in production. The endpoint especially: the card is how an agent finds it and
    the manifest is how a registry does, and an agent sent somewhere the registry does not
    know about is worse than either document alone.
    """
    manifest_path = Path(__file__).resolve().parents[2] / "mcp" / "server.json"
    registry = json.loads(manifest_path.read_text())
    doc = client.get("/.well-known/mcp/server-card.json").json()

    assert doc["name"] == registry["name"]
    ((card_remote,), (registry_remote,)) = (doc["remotes"], registry["remotes"])
    assert card_remote["url"] == registry_remote["url"]
    assert card_remote["type"] == registry_remote["type"]
    assert doc["websiteUrl"] == registry["websiteUrl"]
