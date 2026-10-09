#!/usr/bin/env python3
"""Cautious close-1 participant: inspect and register; trades require proven account state.

The referee is external to this program. A successful venue POST is not a mint or a
settlement. Missing entries in shortened flow posts are unknown, not negative evidence.
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import json
import os
import re
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager, nullcontext
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ROOT = "https://technocore.chat"
ROOM = "close1"
DID_PATTERN = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}\Z")
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
MAX_ACCOUNT_COST = Decimal("1")
MAX_EXPOSURE = Decimal("2")
MAX_TRADE_QTY = Decimal("0.25")
QTY = Decimal("0.10")
MAX_REFEREE_POST_AGE_S = 300
DEFAULT_MAX_REFERENCE_AGE_S = Decimal("300")
MAX_REFERENCE_AGE_ENV = "CLOSECALL_MAX_REFERENCE_AGE_S"
MAX_REFERENCE_AGE_REPORT_SKEW_S = 60
LOCK_SWEEP = 2556
TERMS_KEYS = {"id", "maker", "px", "qty", "side", "taker", "until"}
SETTLEMENT_OPENING_RECHECK_S = 60
_OPENING_CHECKS: dict[tuple, tuple[float, str]] = {}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("redirect refused")


OPENER = urllib.request.build_opener(
    NoRedirect, urllib.request.HTTPSHandler(
        context=ssl.create_default_context(cafile="/etc/ssl/certs/ca-certificates.crt")
    )
)


def request(path: str, payload: dict | None = None) -> dict:
    if not path.startswith("/") or ".." in path or "://" in path:
        raise ValueError("invalid path")
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode() if payload else None
    url = ROOT + path + ("&" if "?" in path else "?") + "format=json"
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with OPENER.open(req, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP {response.status}")
        return json.load(response)


def public_key(did: str) -> Ed25519PublicKey:
    if not isinstance(did, str) or not DID_PATTERN.fullmatch(did):
        raise ValueError("bad signer DID")
    number = 0
    for char in did[len("did:key:z"):]:
        number = number * 58 + B58.index(char)
    raw = number.to_bytes(34, "big")
    if raw[:2] != b"\xed\x01":
        raise ValueError("not an Ed25519 DID")
    return Ed25519PublicKey.from_public_bytes(raw[2:])


def b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def verified_message(room: str, message: dict) -> dict:
    did, nonce, text = message["from"], str(message["nonce"]), message["text"]
    if not re.fullmatch(r"[0-9]{1,19}", nonce):
        raise ValueError("invalid nonce")
    try:
        public_key(did).verify(b64decode(message["sig"]), f"{room}|{nonce}|{text}".encode())
    except (InvalidSignature, ValueError) as error:
        raise ValueError("referee signature did not verify") from error
    return json.loads(text)


def room_tail(room: str, limit: int = 10) -> tuple[list[tuple[dict, dict]], int]:
    response = request(f"/r/{room}?format=json&limit={limit}")
    if response.get("room") != room:
        raise ValueError("wrong room")
    return [
        (message, verified_message(room, message)) for message in response["messages"]
    ], response["last_seq"]


def configured_max_reference_age_s() -> Decimal:
    raw = os.getenv(MAX_REFERENCE_AGE_ENV, str(DEFAULT_MAX_REFERENCE_AGE_S))
    try:
        maximum = Decimal(raw)
    except InvalidOperation as error:
        raise ValueError(f"{MAX_REFERENCE_AGE_ENV} must be a finite non-negative number") from error
    if not maximum.is_finite() or maximum < 0:
        raise ValueError(f"{MAX_REFERENCE_AGE_ENV} must be a finite non-negative number")
    return maximum


def prices() -> tuple[dict, str, list[Decimal]]:
    messages, _ = room_tail("d-close1-price", 10)
    quotes = [(message, row) for message, row in messages if row.get("t") == "price"]
    if len(quotes) < 7:
        raise ValueError("not enough signed price history")
    did = quotes[-1][0]["from"]
    if any(message["from"] != did for message, _ in quotes):
        raise ValueError("referee DID changed in price room")
    price_message, row = quotes[-1]
    if row.get("for") != row.get("n") + 1 or row["for"] > LOCK_SWEEP:
        raise ValueError("unexpected or locked sweep")
    posted_at = parse_time(price_message["ts"])
    now = time.time()
    elapsed = now - posted_at
    if elapsed < -30 or elapsed > MAX_REFEREE_POST_AGE_S:
        raise ValueError("referee price post is stale")
    raw_reference_age = row.get("age_s")
    if isinstance(raw_reference_age, bool) or not isinstance(raw_reference_age, (int, float)):
        raise ValueError("invalid reference age")
    try:
        reference_age = Decimal(str(raw_reference_age))
    except InvalidOperation as error:
        raise ValueError("invalid reference age") from error
    if not reference_age.is_finite() or reference_age < 0:
        raise ValueError("invalid reference age")
    if reference_age > configured_max_reference_age_s():
        raise ValueError("underlying market reference is stale")
    reference = row.get("ref")
    if not isinstance(reference, dict) or not isinstance(reference.get("time"), str):
        raise ValueError("invalid market reference timestamp")
    reference_at = parse_time(reference["time"])
    reference_age_at_post = posted_at - reference_at
    if (reference_age_at_post < -30 or
            abs(reference_age_at_post - float(reference_age)) > MAX_REFERENCE_AGE_REPORT_SKEW_S):
        raise ValueError("market reference timestamp does not match reported age")
    reference_age_now = now - reference_at
    if reference_age_now < -30 or reference_age_now > float(configured_max_reference_age_s()):
        raise ValueError("underlying market reference is stale")
    history = [Decimal(r["ref"]["px"]) for _, r in quotes]
    if any(p <= 0 for p in history):
        raise ValueError("invalid reference")
    return row, did, history


def parse_time(text: str) -> float:
    from datetime import datetime
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.timestamp()


def signer():
    # Never use the service's TECHNOCORE_SIGNING_KEY implicitly.
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    spec = os.getenv("CLOSECALL_SIGNING_KEY", "")
    try:
        seed = bytes.fromhex(spec)
    except ValueError as error:
        raise ValueError("CLOSECALL_SIGNING_KEY must be a 32-byte hex seed") from error
    if len(seed) != 32:
        raise ValueError("CLOSECALL_SIGNING_KEY must be a 32-byte hex seed")
    key = Ed25519PrivateKey.from_private_bytes(seed)
    raw = b"\xed\x01" + key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    number = int.from_bytes(raw, "big")
    encoded = ""
    while number:
        number, rem = divmod(number, 58)
        encoded = B58[rem] + encoded
    return key, "did:key:z" + encoded


def signed_post(key, did: str, payload: dict) -> dict:
    text = json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
    nonce = str(time.time_ns())  # 19 digits; same key may post elsewhere
    sig = base64.urlsafe_b64encode(key.sign(f"{ROOM}|{nonce}|{text}".encode())).decode().rstrip("=")
    result = request(f"/r/{ROOM}", {"did": did, "sig": sig, "nonce": nonce, "text": text})
    posted = result.get("posted")
    if not isinstance(posted, dict) or posted.get("from") != did or posted.get("text") != text:
        raise ValueError("venue did not confirm the signed post; outcome uncertain")
    verified_message(ROOM, posted)
    return posted


def room_export_records() -> list[dict]:
    """Read the retained room snapshot for result reconciliation."""
    url = f"{ROOT}/r/{ROOM}/export"
    req = urllib.request.Request(url, headers={"Accept": "application/x-ndjson"})
    with OPENER.open(req, timeout=15) as response:
        if response.status != 200:
            raise ValueError(f"unexpected HTTP {response.status}")
        records = []
        for line in response.read().splitlines():
            if not line:
                continue
            record = json.loads(line)
            if not isinstance(record, dict) or type(record.get("seq")) is not int:
                raise ValueError("room export contains an invalid record")
            records.append(record)
    return records


def _raw_trade_body(message: dict) -> dict | None:
    try:
        body = json.loads(message["text"])
    except (KeyError, TypeError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _verify_trade_opening(message: dict, did: str, expected_terms: dict) -> bool:
    """Require a signed room opening and both signatures over the exact saved terms."""
    try:
        body = verified_message(ROOM, message)
        if (not isinstance(body, dict) or set(body) != {
                "t", "season", "terms", "taker", "maker_sig", "taker_sig"} or
                body["t"] != "trade" or body["season"] != "close-1" or
                body["terms"] != expected_terms):
            return False
        terms = body["terms"]
        taker = body["taker"]
        if (set(terms) != TERMS_KEYS or taker != did or message.get("from") != taker or
                terms["taker"] not in ("any", taker)):
            return False
        canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        public_key(terms["maker"]).verify(
            b64decode(body["maker_sig"]), f"close-1|terms|{canonical}".encode()
        )
        public_key(taker).verify(
            b64decode(body["taker_sig"]), f"close-1|accept|{canonical}|{taker}".encode()
        )
        return True
    except (InvalidSignature, ValueError, KeyError, TypeError, OverflowError):
        return False


def _opening_sequence_matches(pending: dict, seq: int) -> bool:
    if "maker_seq" in pending:
        venue_seq = pending.get("venue_seq")
        if type(venue_seq) is int:
            return seq == venue_seq
        maker_seq = pending.get("maker_seq")
        return type(maker_seq) is int and seq > maker_seq
    venue_seq = pending.get("venue_seq")
    return type(venue_seq) is int and seq > venue_seq


def verified_opening_for_pending(pending: dict, did: str) -> tuple[dict | None, str | None]:
    """Find one retained, authenticated t:trade opening for a pending local item."""
    terms = pending.get("terms")
    if not isinstance(terms, dict) or set(terms) != TERMS_KEYS or terms.get("id") != pending.get("id"):
        return None, "saved order terms are missing or invalid"
    cached = pending.get("opening_record")
    if (isinstance(cached, dict) and type(cached.get("seq")) is int and
            _opening_sequence_matches(pending, cached["seq"]) and
            _verify_trade_opening(cached, did, terms)):
        return cached, None
    check_key = (did, pending["id"], pending.get("venue_seq"), pending.get("maker_seq"))
    last_check = _OPENING_CHECKS.get(check_key)
    now = time.monotonic()
    if last_check is not None and now - last_check[0] < SETTLEMENT_OPENING_RECHECK_S:
        return None, last_check[1]

    def rejected(reason: str) -> tuple[None, str]:
        if len(_OPENING_CHECKS) >= 64:
            _OPENING_CHECKS.pop(next(iter(_OPENING_CHECKS)))
        _OPENING_CHECKS[check_key] = (time.monotonic(), reason)
        return None, reason

    try:
        records = room_export_records()
    except (OSError, TimeoutError, urllib.error.URLError, ValueError, json.JSONDecodeError) as error:
        return rejected(f"could not read the retained room export: {error}")

    candidates = []
    for message in records:
        body = _raw_trade_body(message)
        if (body is None or body.get("t") != "trade" or
                not isinstance(body.get("terms"), dict) or
                body["terms"].get("id") != pending["id"]):
            continue
        seq = message["seq"]
        if not _opening_sequence_matches(pending, seq):
            continue
        candidates.append(message)

    if len(candidates) != 1:
        return rejected(f"expected one retained trade opening, found {len(candidates)}")
    opening = candidates[0]
    if not _verify_trade_opening(opening, did, terms):
        return rejected("retained trade opening or its maker/taker signatures did not verify")
    _OPENING_CHECKS.pop(check_key, None)
    return opening, None


def state_load(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text())
    return {"registration": None, "offer": None, "trade": None}


def verified_account(did: str) -> tuple[Decimal, Decimal]:
    """Return position and prior costs only from a complete authenticated history.

    Neither the venue's retained room export nor a shortened signed flow post
    provides that history. Do not substitute CLI assertions, a registration
    receipt, or an omitted-list entry for an authenticated account proof.
    """
    raise ValueError("official complete sweep history or signed owner account lookup unavailable; trading disabled")


def within_account_limits(position: Decimal, prior_cost: Decimal, qty: Decimal,
                          estimate: Decimal) -> bool:
    """Conservatively count the entire proposed fill as additional exposure."""
    values = (position, prior_cost, qty, estimate)
    if any(not value.is_finite() for value in values) or prior_cost < 0 or qty <= 0 or estimate < 0:
        raise ValueError("invalid account risk inputs")
    return abs(position) + qty <= MAX_EXPOSURE and prior_cost + estimate <= MAX_ACCOUNT_COST


def state_save(path: Path, state: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as out:
        os.fchmod(out.fileno(), 0o600)
        json.dump(state, out, indent=2)
    tmp.replace(path)


@contextmanager
def trade_lock(path: Path):
    """Serialize the read/claim/POST across manual and background processes."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def status() -> dict:
    price, did, history = prices()
    flow, _ = room_tail("d-close1-flow", 1)
    if not flow or flow[-1][0]["from"] != did or flow[-1][1].get("t") != "flow":
        raise ValueError("flow signer differs from price signer")
    if flow[-1][1].get("n") != price["n"]:
        raise ValueError("price and flow sweep differ")
    return {
        "sweep": price["for"], "reference": price["ref"]["px"],
        "age_s": price["age_s"], "limits": price["limits"],
        "referee": did, "last_flow": flow[-1][1]["n"],
        "flow_omitted": flow[-1][1].get("omitted", {}),
        "trend": str(history[-1] - history[-7]),
    }


def register(key, did: str, path: Path, live: bool) -> dict:
    state = state_load(path)
    if state["registration"] is not None:
        return {"registration": state["registration"], "message": "already attempted; do not retry blindly"}
    payload = {"t": "owner", "season": "close-1", "key": did}
    if not live:
        return {"preview": payload}
    # Registration does not incur POLF fees. Unlike a trade, it need not wait
    # for a fresh price, but must still be inside the contest and venue-visible.
    price_messages, _ = room_tail("d-close1-price", 1)
    flow_messages, _ = room_tail("d-close1-flow", 1)
    price_message, price = price_messages[-1]
    flow_message, flow = flow_messages[-1]
    if (price_message["from"] != flow_message["from"] or
            price.get("t") != "price" or flow.get("t") != "flow" or
            price.get("n") != flow.get("n") or price.get("for", LOCK_SWEEP + 1) > LOCK_SWEEP or
            time.time() - parse_time(price_message["ts"]) > 3600):
        raise ValueError("referee state is inconsistent, expired, or contest locked")
    state["registration"] = "submitted; mint unconfirmed"
    state_save(path, state)  # write before network operation: uncertain HTTP failures must not retry
    receipt = signed_post(key, did, payload)
    state["registration"] = {"venue_seq": receipt["seq"], "mint": "unconfirmed"}
    state_save(path, state)
    return {"registration": "posted; mint unconfirmed", "did": did, "venue_seq": receipt["seq"]}


def offer(key, did: str, path: Path, live: bool) -> dict:
    state = state_load(path)
    if state.get("offer") is not None or state.get("trade") is not None:
        raise ValueError("earlier offer or trade outcome unresolved; no additional order is safe")
    if state["registration"] is None:
        raise ValueError("registration not attempted by this agent")
    position, prior_cost = verified_account(did)
    price, _, history = prices()
    # Momentum only determines a proposal, not a prediction or a winning claim.
    change = (history[-1] - history[-7]) / history[-7]
    if abs(change) < Decimal("0.002"):
        return {"action": "hold", "reason": "trend below 0.2% threshold"}
    side = "buy" if change > 0 else "sell"
    px = Decimal(price["ref"]["px"])
    lo, hi = [Decimal(x) for x in price["limits"]]
    if not lo <= px <= hi:
        raise ValueError("price outside posted limits")
    # Estimated only; sweep-close clawback can be higher.
    estimate = max(px * QTY * Decimal("0.01"), abs(px - Decimal(price["applied"])) * QTY)
    if not within_account_limits(position, prior_cost, QTY, estimate):
        return {"action": "hold", "reason": "account exposure or estimated cost limit"}
    terms = {"id": "cc-" + secrets.token_hex(8), "maker": did, "px": f"{px:.2f}",
             "qty": f"{QTY:.2f}", "side": side, "taker": "any", "until": price["for"] + 2}
    canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"))
    signature = base64.urlsafe_b64encode(
        key.sign(f"close-1|terms|{canonical}".encode())
    ).decode().rstrip("=")
    proposal = {"t": "offer", "season": "close-1", "terms": terms, "maker_sig": signature}
    result = {"action": "post offer", "side": side, "qty": str(QTY), "price": terms["px"],
              "estimated_fee": str(estimate), "id": terms["id"],
              "warning": "posting does not imply acceptance or settlement"}
    if live:
        state["offer"] = {
            "id": terms["id"], "terms": terms, "status": "unknown after attempted POST"
        }
        state_save(path, state)
        receipt = signed_post(key, did, proposal)
        state["offer"]["venue_seq"] = receipt["seq"]
        state_save(path, state)
    return result


def _offer_candidate(message: dict, did: str, price: dict) -> tuple[dict, Decimal] | None:
    """Treat room content as untrusted until both the envelope and maker terms verify."""
    try:
        if not isinstance(message.get("from"), str) or not message["from"].startswith("did:key:"):
            return None
        body = verified_message(ROOM, message)
        if not isinstance(body, dict) or body.get("t") != "offer" or body.get("season") != "close-1":
            return None
        terms = body["terms"]
        if not isinstance(terms, dict) or set(terms) != TERMS_KEYS:
            return None
        maker = terms["maker"]
        if maker != message["from"] or maker == did or terms["taker"] not in ("any", did):
            return None
        if (not isinstance(terms["id"], str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", terms["id"]) or
                terms["side"] not in ("buy", "sell") or
                type(terms["until"]) is not int or
                not price["for"] <= terms["until"] <= price["for"] + 2):
            return None
        if any(not isinstance(terms[field], str) or
               not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", terms[field])
               for field in ("px", "qty")):
            return None
        qty, px = Decimal(terms["qty"]), Decimal(terms["px"])
        if not Decimal("0.10") <= qty <= MAX_TRADE_QTY or px <= 0:
            return None
        lo, hi = [Decimal(value) for value in price["limits"]]
        if not lo <= px <= hi:
            return None
        age = time.time() - parse_time(message["ts"])
        if age < -30 or age > 300:
            return None
        canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        if (not isinstance(body["maker_sig"], str) or
                not re.fullmatch(r"[A-Za-z0-9_-]{85}[AQgw]", body["maker_sig"])):
            return None
        public_key(maker).verify(
            b64decode(body["maker_sig"]), f"close-1|terms|{canonical}".encode()
        )
        # Estimate only; sweep-close price can move further before settlement.
        estimate = max(px * qty * Decimal("0.01"),
                       abs(px - Decimal(price["ref"]["px"])) * qty)
        return body, estimate
    except (InvalidSignature, ValueError, KeyError, TypeError, OverflowError):
        return None


def trade_once(key, did: str, path: Path, live: bool) -> dict:
    with trade_lock(path) if live else nullcontext():
        return _trade_once_locked(key, did, path, live)


def _trade_once_locked(key, did: str, path: Path, live: bool) -> dict:
    state = state_load(path)
    if state.get("registration") is None:
        raise ValueError("participant registration not attempted")
    if state.get("trade") is not None or state.get("offer") is not None:
        return {"action": "hold", "reason": "earlier trade or offer unresolved"}
    position, prior_cost = verified_account(did)
    try:
        price, referee, _ = prices()
    except ValueError as error:
        if str(error) in ("referee price post is stale", "underlying market reference is stale"):
            return {"action": "hold", "reason": str(error)}
        raise
    flow, _ = room_tail("d-close1-flow", 20)
    if (not flow or any(msg["from"] != referee or row.get("t") != "flow" for msg, row in flow)
            or flow[-1][1].get("n") not in (price["n"], price["n"] - 1)):
        raise ValueError("referee flow and price disagree")
    known_consumed = set()
    for _, row in flow:
        for item in row.get("settled", []):
            if isinstance(item, (str, list)) and item:
                known_consumed.add(item[0] if isinstance(item, list) else item)
        for item in row.get("void", []):
            if isinstance(item, list) and item and len(item) > 1 and item[1] == "settled":
                known_consumed.add(item[0])
    response = request("/r/close1?limit=200")
    if response.get("room") != ROOM:
        raise ValueError("wrong offer room")
    for message in reversed(response["messages"]):
        candidate = _offer_candidate(message, did, price)
        if candidate is None:
            continue
        body, estimate = candidate
        terms = body["terms"]
        if terms["id"] in known_consumed:
            continue
        if not within_account_limits(position, prior_cost, Decimal(terms["qty"]), estimate):
            continue
        side = "buy" if terms["side"] == "sell" else "sell"
        canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        taker_sig = base64.urlsafe_b64encode(
            key.sign(f"close-1|accept|{canonical}|{did}".encode())
        ).decode().rstrip("=")
        frame = {"t": "trade", "season": "close-1", "terms": terms, "taker": did,
                 "maker_sig": body["maker_sig"], "taker_sig": taker_sig}
        report = {"action": "submit trade" if live else "preview trade",
                  "id": terms["id"], "maker_seq": message["seq"], "side": side,
                  "qty": terms["qty"], "price": terms["px"],
                  "estimated_fee": str(estimate),
                   "warning": "venue posting is not settlement; reconcile against authenticated complete results before another trade"}
        if live:
            state["trade"] = {"id": terms["id"], "status": "unknown after attempted POST",
                              "maker_seq": message["seq"], "terms": terms}
            state_save(path, state)
            receipt = signed_post(key, did, frame)
            state["trade"]["venue_seq"] = receipt["seq"]
            state_save(path, state)
            report["venue_seq"] = receipt["seq"]
        return report
    return {"action": "hold", "reason": "no fresh, valid maker offer within per-trade limits"}


def monitor_once(path: Path) -> dict:
    state = state_load(path)
    _, did = signer()
    if state["registration"] is None:
        raise ValueError("registration not attempted; monitor cannot infer account state")
    price_messages, _ = room_tail("d-close1-price", 1)
    referee = price_messages[-1][0]["from"]
    flow_messages, _ = room_tail("d-close1-flow", 20)
    observed_mint = False
    visible_result = None
    rejected_settlement = None
    settlement_check_done = False
    settlement_opening = None
    settlement_reason = None
    pending_key = "trade" if state.get("trade") else "offer"
    for msg, row in flow_messages:
        if msg["from"] != referee or row.get("t") != "flow":
            raise ValueError("flow signer differs from price signer")
        if did in row.get("mints", []):
            observed_mint = True
        pending = state.get("trade") or state.get("offer")
        if pending:
            for field in ("settled", "void"):
                for item in row.get(field, []):
                    trade_id = item[0] if isinstance(item, list) and item else item
                    if trade_id == pending["id"]:
                        if field == "settled":
                            if not settlement_check_done:
                                settlement_opening, settlement_reason = verified_opening_for_pending(
                                    pending, did
                                )
                                settlement_check_done = True
                            if settlement_opening is None:
                                rejected_settlement = {
                                    "sweep": row["n"], "id": trade_id, "entry": item,
                                    "reason": settlement_reason,
                                }
                            else:
                                visible_result = {
                                    "sweep": row["n"], "outcome": field, "entry": item,
                                    "opening_seq": settlement_opening["seq"],
                                    "opening_verified": True,
                                }
                        else:
                            visible_result = {
                                "sweep": row["n"], "outcome": field, "entry": item,
                                "note": "void is not treated as a settlement",
                            }
    if observed_mint and isinstance(state["registration"], dict):
        with trade_lock(path):
            state = state_load(path)
            if isinstance(state["registration"], dict):
                state["registration"]["mint"] = "visible in signed flow"
                state_save(path, state)
    if settlement_opening is not None:
        with trade_lock(path):
            state = state_load(path)
            pending = state.get(pending_key)
            if isinstance(pending, dict):
                body = verified_message(ROOM, settlement_opening)
                terms = body.get("terms")
                if (pending.get("opening_record") is None and isinstance(terms, dict) and
                        pending.get("id") == terms.get("id") and pending.get("terms") == terms):
                    pending["opening_record"] = settlement_opening
                    state_save(path, state)
            state = state_load(path)
    report = {
        "did": did, "latest_price_sweep": price_messages[-1][1].get("n"),
        "latest_flow_sweep": flow_messages[-1][1].get("n"),
        "registration": state["registration"], "offer": state.get("offer"),
        "trade": state.get("trade"),
        "visible_offer_result": visible_result,
        "rejected_settlement": rejected_settlement,
        "note": "absence from shortened flow is not evidence of absence",
    }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "register", "offer", "trade", "monitor"])
    parser.add_argument("--state", type=Path, default=Path(".local/closecall-agent/state.json"))
    parser.add_argument("--live", action="store_true", help="explicitly allow a signed POST")
    parser.add_argument("--autotrade", action="store_true", help="monitor: submit at most one bounded trade")
    parser.add_argument("--interval", type=int, default=10, help="monitor: seconds between checks")
    args = parser.parse_args()
    try:
        if args.action == "monitor":
            if args.interval < 10:
                raise ValueError("monitor interval must be at least 10 seconds")
            while True:
                try:
                    report = monitor_once(args.state)
                    if args.autotrade and report.get("trade") is None and report.get("offer") is None:
                        key, did = signer()
                        report["decision"] = trade_once(key, did, args.state, True)
                    print(json.dumps(report), flush=True)
                except (ValueError, KeyError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
                    print(f"monitor paused: {error}", file=sys.stderr, flush=True)
                time.sleep(args.interval)
        elif args.action == "status":
            result = status()
        else:
            key, did = signer()
            if args.action == "register":
                result = register(key, did, args.state, args.live)
            elif args.action == "trade":
                result = trade_once(key, did, args.state, args.live)
            else:
                result = offer(key, did, args.state, args.live)
        print(json.dumps(result, indent=2))
        return 0
    except (ValueError, KeyError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
        print(f"closecall-agent: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
