import base64
import importlib.util
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from threading import Event
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location("closecall_agent", HERE / "agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


class AgentTests(unittest.TestCase):
    def setUp(self):
        agent._OPENING_CHECKS.clear()
        self.key = Ed25519PrivateKey.from_private_bytes(b"\x01" * 32)
        with patch.dict("os.environ", {"CLOSECALL_SIGNING_KEY": (b"\x01" * 32).hex()}):
            _, self.did = agent.signer()

    def test_verified_room_record_and_tampering(self):
        text = '{"t":"price","n":1}'
        sig = self.key.sign(f"d-close1-price|123|{text}".encode())
        message = {"from": self.did, "nonce": 123, "text": text,
                   "sig": agent.base64.urlsafe_b64encode(sig).decode().rstrip("=")}
        self.assertEqual(agent.verified_message("d-close1-price", message)["n"], 1)
        message["text"] = '{"t":"price","n":2}'
        with self.assertRaisesRegex(ValueError, "signature"):
            agent.verified_message("d-close1-price", message)

    def test_register_dry_run_makes_no_request_or_state(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "request") as send:
            path = Path(directory) / "state.json"
            result = agent.register(self.key, self.did, path, False)
            self.assertEqual(result["preview"]["t"], "owner")
            self.assertFalse(path.exists())
            send.assert_not_called()

    def test_live_register_blocks_retry_after_uncertain_post(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "room_tail", return_value=(
                 [({"from": self.did, "ts": "2026-09-26T16:25:21Z"},
                   {"t": "price", "n": 1, "for": 2})], 1
             )) as tail, \
             patch.object(agent, "time") as clock, \
             patch.object(agent, "signed_post", side_effect=TimeoutError):
            clock.time.return_value = agent.parse_time("2026-09-26T16:25:30Z")
            def room_records(room, limit):
                row = {"t": "price", "n": 1, "for": 2} if room == "d-close1-price" else {"t": "flow", "n": 1}
                return [({"from": self.did, "ts": "2026-09-26T16:25:21Z"}, row)], 1
            tail.side_effect = room_records
            path = Path(directory) / "state.json"
            with self.assertRaises(TimeoutError):
                agent.register(self.key, self.did, path, True)
            self.assertIn("unconfirmed", agent.state_load(path)["registration"])
            result = agent.register(self.key, self.did, path, True)
            self.assertIn("already attempted", result["message"])

    def test_offer_requires_official_account_and_fails_closed_on_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": "submitted", "offer": {"status": "unknown"}})
            with self.assertRaisesRegex(ValueError, "unresolved"):
                agent.offer(self.key, self.did, path, True)
            agent.state_save(path, {"registration": "submitted", "offer": None})
            with patch.object(agent, "signed_post") as send, \
                 self.assertRaisesRegex(ValueError, "trading disabled"):
                agent.offer(self.key, self.did, path, True)
            send.assert_not_called()

    def test_estimated_budget_and_exposure(self):
        self.assertTrue(agent.within_account_limits(Decimal("1.8"), Decimal("0.75"),
                                                    Decimal("0.2"), Decimal("0.25")))
        self.assertFalse(agent.within_account_limits(Decimal("1.81"), Decimal("0"),
                                                     Decimal("0.2"), Decimal("0.1")))
        self.assertFalse(agent.within_account_limits(Decimal("0"), Decimal("0.76"),
                                                     Decimal("0.2"), Decimal("0.25")))
        with self.assertRaisesRegex(ValueError, "invalid account"):
            agent.within_account_limits(Decimal("NaN"), Decimal(0), Decimal("0.2"), Decimal(0))
        with tempfile.TemporaryDirectory() as directory, patch.object(agent, "prices") as price, \
             patch.object(agent, "verified_account", return_value=(Decimal("1.9"), Decimal("0.8"))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": "submitted", "offer": None})
            quote = {"n": 5, "for": 6, "ref": {"px": "220.00"}, "applied": "219.99",
                     "limits": ["209.00", "231.00"]}
            price.return_value = (quote, self.did, [Decimal("210")] * 6 + [Decimal("220")])
            self.assertEqual(agent.offer(self.key, self.did, path, False)["action"], "hold")
            self.assertIsNone(agent.state_load(path)["offer"])

    def maker_offer(self, qty="0.20", maker_sig_valid=True, until=7, side="sell"):
        maker = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
        with patch.dict("os.environ", {"CLOSECALL_SIGNING_KEY": (b"\x02" * 32).hex()}):
            _, maker_did = agent.signer()
        terms = {"id": "off-123", "maker": maker_did, "px": "220.00",
                 "qty": qty, "side": side, "taker": "any", "until": until}
        canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"))
        def encode(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        maker_sig = encode(maker.sign(f"close-1|terms|{canonical}".encode()))
        if not maker_sig_valid:
            maker_sig = encode(self.key.sign(f"close-1|terms|{canonical}".encode()))
        body = {"t": "offer", "season": "close-1", "terms": terms, "maker_sig": maker_sig}
        text = json.dumps(body, separators=(",", ":"))
        message = {"seq": 123, "ts": datetime.now(UTC).isoformat(), "from": maker_did,
                   "nonce": 123, "text": text,
                   "sig": encode(maker.sign(f"close1|123|{text}".encode()))}
        return message, body

    def trade_opening(self, terms, *, valid_room_signature=True,
                      valid_maker_signature=True, valid_taker_signature=True):
        maker = Ed25519PrivateKey.from_private_bytes(b"\x02" * 32)
        canonical = json.dumps(terms, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        def encode(raw):
            return base64.urlsafe_b64encode(raw).decode().rstrip("=")

        maker_signature = maker.sign(f"close-1|terms|{canonical}".encode())
        if not valid_maker_signature:
            maker_signature = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32).sign(
                f"close-1|terms|{canonical}".encode()
            )
        taker_signature = self.key.sign(
            f"close-1|accept|{canonical}|{self.did}".encode()
        )
        if not valid_taker_signature:
            taker_signature = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32).sign(
                f"close-1|accept|{canonical}|{self.did}".encode()
            )
        body = {
            "t": "trade", "season": "close-1", "terms": terms, "taker": self.did,
            "maker_sig": encode(maker_signature),
            "taker_sig": encode(taker_signature),
        }
        text = json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        signature = self.key.sign(f"close1|124|{text}".encode())
        if not valid_room_signature:
            signature = Ed25519PrivateKey.from_private_bytes(b"\x03" * 32).sign(
                f"close1|124|{text}".encode()
            )
        return {
            "seq": 124, "ts": datetime.now(UTC).isoformat(), "from": self.did,
            "nonce": 124, "text": text, "sig": encode(signature),
        }

    def monitor_settlement(self, opening_records):
        message, offer = self.maker_offer()
        terms = offer["terms"]
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "signer", return_value=(self.key, self.did)), \
             patch.object(agent, "room_tail") as tail, \
             patch.object(agent, "room_export_records", return_value=opening_records):
            path = Path(directory) / "state.json"
            agent.state_save(path, {
                "registration": {"venue_seq": 12, "mint": "unconfirmed"},
                "offer": {"id": terms["id"], "terms": terms, "venue_seq": message["seq"]},
                "trade": None,
            })
            def rooms(room, limit):
                if room == "d-close1-price":
                    return [({"from": self.did}, {"t": "price", "n": 5})], 5
                return [({"from": self.did}, {
                    "t": "flow", "n": 5, "mints": [], "settled": [terms["id"]], "void": [],
                })], 5
            tail.side_effect = rooms
            return agent.monitor_once(path)

    def test_monitor_accepts_settlement_only_with_unique_signed_trade_opening(self):
        _, offer = self.maker_offer()
        opening = self.trade_opening(offer["terms"])
        result = self.monitor_settlement([opening])
        self.assertTrue(result["visible_offer_result"]["opening_verified"])
        self.assertEqual(result["visible_offer_result"]["opening_seq"], opening["seq"])
        self.assertIsNone(result["rejected_settlement"])

    def test_monitor_rejects_settlement_without_matching_trade_opening(self):
        result = self.monitor_settlement([])
        self.assertIsNone(result["visible_offer_result"])
        self.assertIn("found 0", result["rejected_settlement"]["reason"])

    def test_monitor_rejects_trade_opening_with_invalid_room_or_taker_signature(self):
        _, offer = self.maker_offer()
        for opening in (
            self.trade_opening(offer["terms"], valid_room_signature=False),
            self.trade_opening(offer["terms"], valid_maker_signature=False),
            self.trade_opening(offer["terms"], valid_taker_signature=False),
        ):
            with self.subTest(opening=opening["sig"][:8]):
                agent._OPENING_CHECKS.clear()
                result = self.monitor_settlement([opening])
                self.assertIsNone(result["visible_offer_result"])
                self.assertIn("did not verify", result["rejected_settlement"]["reason"])

    def test_taker_settlement_requires_the_exact_recorded_trade_sequence(self):
        _, offer = self.maker_offer()
        opening = self.trade_opening(offer["terms"])
        pending = {
            "id": offer["terms"]["id"], "terms": offer["terms"],
            "maker_seq": 123, "venue_seq": opening["seq"],
        }
        with patch.object(agent, "room_export_records", return_value=[opening]):
            verified, reason = agent.verified_opening_for_pending(pending, self.did)
        self.assertEqual(verified["seq"], opening["seq"])
        self.assertIsNone(reason)

        pending["venue_seq"] += 1
        with patch.object(agent, "room_export_records", return_value=[opening]):
            verified, reason = agent.verified_opening_for_pending(pending, self.did)
        self.assertIsNone(verified)
        self.assertIn("found 0", reason)

    def test_missing_trade_opening_does_not_repeat_large_export_before_recheck(self):
        _, offer = self.maker_offer()
        pending = {
            "id": offer["terms"]["id"], "terms": offer["terms"],
            "maker_seq": 123, "venue_seq": 124,
        }
        with patch.object(agent.time, "monotonic", side_effect=[0, 0, 1, 61, 61]), \
             patch.object(agent, "room_export_records", return_value=[]) as export:
            first, reason = agent.verified_opening_for_pending(pending, self.did)
            second, repeated_reason = agent.verified_opening_for_pending(pending, self.did)
            self.assertIsNone(first)
            self.assertIsNone(second)
            self.assertEqual(reason, repeated_reason)
            export.assert_called_once()

            agent.verified_opening_for_pending(pending, self.did)
            self.assertEqual(export.call_count, 2)

    def test_monitor_caches_verified_opening_and_bounds_repeated_export_reads(self):
        _, offer = self.maker_offer()
        opening = self.trade_opening(offer["terms"])
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "signer", return_value=(self.key, self.did)), \
             patch.object(agent, "room_tail") as tail, \
             patch.object(agent, "room_export_records", return_value=[opening]) as export:
            path = Path(directory) / "state.json"
            agent.state_save(path, {
                "registration": {"venue_seq": 12, "mint": "unconfirmed"},
                "offer": {"id": offer["terms"]["id"], "terms": offer["terms"], "venue_seq": 123},
                "trade": None,
            })
            tail.side_effect = lambda room, limit: (
                [({"from": self.did}, {"t": "price", "n": 5})], 5
            ) if room == "d-close1-price" else (
                [({"from": self.did}, {
                    "t": "flow", "n": 5, "mints": [], "settled": [offer["terms"]["id"]],
                    "void": [],
                })], 5
            )
            first = agent.monitor_once(path)
            self.assertTrue(first["visible_offer_result"]["opening_verified"])
            self.assertEqual(
                agent.state_load(path)["offer"]["opening_record"]["seq"], opening["seq"]
            )
            export.reset_mock()
            second = agent.monitor_once(path)
            self.assertTrue(second["visible_offer_result"]["opening_verified"])
            export.assert_not_called()

    def test_monitor_rejects_duplicate_trade_openings_for_same_offer_id(self):
        _, offer = self.maker_offer()
        first = self.trade_opening(offer["terms"])
        second = dict(first, seq=125, nonce=125)
        body = json.loads(second["text"])
        text = json.dumps(body, separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        second["text"] = text
        second["sig"] = base64.urlsafe_b64encode(
            self.key.sign(f"close1|125|{text}".encode())
        ).decode().rstrip("=")
        result = self.monitor_settlement([first, second])
        self.assertIsNone(result["visible_offer_result"])
        self.assertIn("found 2", result["rejected_settlement"]["reason"])

    def test_verified_offer_and_countersigned_trade(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "prices") as prices, \
             patch.object(agent, "room_tail") as room, \
             patch.object(agent, "request") as read, \
              patch.object(agent, "signed_post") as send, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            price = {"n": 5, "for": 6, "ref": {"px": "220.00"}, "limits": ["209", "231"]}
            prices.return_value = (price, self.did, [Decimal("220")] * 7)
            room.return_value = ([({"from": self.did}, {"n": 5, "t": "flow"})], 5)
            message, offer = self.maker_offer()
            read.return_value = {"room": "close1", "messages": [message]}
            send.return_value = {"seq": 456}
            result = agent.trade_once(self.key, self.did, path, True)
            self.assertEqual(result["action"], "submit trade")
            self.assertEqual(result["side"], "buy")
            self.assertEqual(result["estimated_fee"], "0.440000")
            payload = send.call_args.args[2]
            self.assertEqual(payload["t"], "trade")
            self.assertEqual(payload["terms"], offer["terms"])
            canonical = json.dumps(offer["terms"], sort_keys=True, separators=(",", ":"))
            self.key.public_key().verify(
                agent.b64decode(payload["taker_sig"]),
                f"close-1|accept|{canonical}|{self.did}".encode()
            )
            self.assertEqual(agent.state_load(path)["trade"]["venue_seq"], 456)
            self.assertEqual(agent.state_load(path)["trade"]["terms"], offer["terms"])
            self.assertEqual(agent.trade_once(self.key, self.did, path, True)["action"], "hold")
            self.assertEqual(send.call_count, 1)

    def test_flat_price_can_take_either_offer_side(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "prices") as prices, \
             patch.object(agent, "room_tail") as room, \
              patch.object(agent, "request") as read, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            prices.return_value = (
                {"n": 5, "for": 6, "ref": {"px": "220.00"}, "limits": ["209", "231"]},
                self.did, [Decimal("220")] * 7
            )
            room.return_value = ([({"from": self.did}, {"n": 5, "t": "flow"})], 5)
            read.return_value = {"room": "close1", "messages": [self.maker_offer(side="buy")[0]]}
            result = agent.trade_once(self.key, self.did, path, False)
            self.assertEqual(result["action"], "preview trade")
            self.assertEqual(result["side"], "sell")
            self.assertIsNone(agent.state_load(path).get("trade"))

    def test_invalid_offers_never_post_and_uncertain_post_never_retries(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "prices") as prices, \
             patch.object(agent, "room_tail") as room, \
             patch.object(agent, "request") as read, \
              patch.object(agent, "signed_post", side_effect=TimeoutError) as send, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            price = {"n": 5, "for": 6, "ref": {"px": "220.00"}, "limits": ["209", "231"]}
            prices.return_value = (price, self.did, [Decimal("210")] * 6 + [Decimal("220")])
            room.return_value = ([({"from": self.did}, {"n": 5, "t": "flow"})], 5)
            bad = [self.maker_offer(qty="0.26")[0],
                   self.maker_offer(maker_sig_valid=False)[0],
                   self.maker_offer(until=5)[0]]
            read.return_value = {"room": "close1", "messages": bad}
            self.assertEqual(agent.trade_once(self.key, self.did, path, True)["action"], "hold")
            send.assert_not_called()
            read.return_value = {"room": "close1", "messages": [self.maker_offer()[0]]}
            with self.assertRaises(TimeoutError):
                agent.trade_once(self.key, self.did, path, True)
            self.assertEqual(agent.state_load(path)["trade"]["status"], "unknown after attempted POST")
            self.assertEqual(agent.trade_once(self.key, self.did, path, True)["action"], "hold")
            self.assertEqual(send.call_count, 1)

    def test_stale_referee_post_holds_without_scanning_offers(self):
        with tempfile.TemporaryDirectory() as directory, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))), \
             patch.object(agent, "prices", side_effect=ValueError("referee price post is stale")), \
             patch.object(agent, "request") as read:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            self.assertEqual(agent.trade_once(self.key, self.did, path, True),
                             {"action": "hold", "reason": "referee price post is stale"})
            read.assert_not_called()

    def test_stale_market_reference_holds_without_scanning_or_posting(self):
        with tempfile.TemporaryDirectory() as directory, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))), \
              patch.object(agent, "prices",
                           side_effect=ValueError("underlying market reference is stale")), \
              patch.object(agent, "request") as read, \
              patch.object(agent, "signed_post") as send:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            self.assertEqual(agent.trade_once(self.key, self.did, path, True),
                             {"action": "hold", "reason": "underlying market reference is stale"})
            read.assert_not_called()
            send.assert_not_called()

    def test_offer_cannot_post_with_stale_market_reference(self):
        with tempfile.TemporaryDirectory() as directory, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))), \
              patch.object(agent, "prices",
                           side_effect=ValueError("underlying market reference is stale")), \
              patch.object(agent, "signed_post") as send:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            with self.assertRaisesRegex(ValueError, "underlying market reference is stale"):
                agent.offer(self.key, self.did, path, True)
            send.assert_not_called()

    def test_parallel_live_invocations_share_one_attempt(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "prices") as prices, \
             patch.object(agent, "room_tail") as room, \
             patch.object(agent, "request") as read, \
              patch.object(agent, "signed_post") as send, \
              patch.object(agent, "verified_account", return_value=(Decimal(0), Decimal(0))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            prices.return_value = (
                {"n": 5, "for": 6, "ref": {"px": "220.00"}, "limits": ["209", "231"]},
                self.did, [Decimal("210")] * 6 + [Decimal("220")]
            )
            room.return_value = ([({"from": self.did}, {"n": 5, "t": "flow"})], 5)
            read.return_value = {"room": "close1", "messages": [self.maker_offer()[0]]}
            entered, release = Event(), Event()
            def slow_post(*args):
                entered.set()
                if not release.wait(3):
                    raise TimeoutError("test timed out")
                return {"seq": 456}
            send.side_effect = slow_post
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(agent.trade_once, self.key, self.did, path, True)
                self.assertTrue(entered.wait(3))
                second = pool.submit(agent.trade_once, self.key, self.did, path, True)
                release.set()
                self.assertEqual(first.result(timeout=3)["action"], "submit trade")
                self.assertEqual(second.result(timeout=3)["action"], "hold")
            send.assert_called_once()

    def test_trade_requires_official_account_before_venue_scan_or_post(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "request") as read, \
             patch.object(agent, "signed_post") as send:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            with self.assertRaisesRegex(ValueError, "trading disabled"):
                agent.trade_once(self.key, self.did, path, True)
            read.assert_not_called()
            send.assert_not_called()

    def test_account_caps_skip_valid_offer_before_post(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "prices") as prices, \
             patch.object(agent, "room_tail") as room, \
             patch.object(agent, "request") as read, \
             patch.object(agent, "signed_post") as send, \
             patch.object(agent, "verified_account", return_value=(Decimal("1.9"), Decimal("0.8"))):
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 1}, "offer": None})
            prices.return_value = (
                {"n": 5, "for": 6, "ref": {"px": "220.00"}, "limits": ["209", "231"]},
                self.did, [Decimal("220")] * 7
            )
            room.return_value = ([({"from": self.did}, {"n": 5, "t": "flow"})], 5)
            read.return_value = {"room": "close1", "messages": [self.maker_offer()[0]]}
            self.assertEqual(agent.trade_once(self.key, self.did, path, True)["action"], "hold")
            send.assert_not_called()

    def test_fresh_referee_post_does_not_make_old_market_reference_fresh(self):
        with patch.object(agent, "room_tail") as room:
            stamp = datetime.now(UTC).isoformat()
            room.return_value = ([
                ({"from": self.did, "ts": stamp},
                 {"t": "price", "n": n, "for": n + 1, "age_s": 7200,
                  "ref": {"px": "220", "time": (datetime.now(UTC) - timedelta(hours=2)).isoformat()},
                  "limits": ["209", "231"]})
                for n in range(1, 11)
            ], 10)
            with self.assertRaisesRegex(ValueError, "underlying market reference is stale"):
                agent.prices()

    def test_maximum_reference_age_is_independently_configurable(self):
        stamp = datetime.now(UTC)
        records = [
            ({"from": self.did, "ts": stamp.isoformat()},
             {"t": "price", "n": n, "for": n + 1, "age_s": 181,
              "ref": {"px": "220", "time": (stamp - timedelta(seconds=181)).isoformat()},
              "limits": ["209", "231"]})
            for n in range(1, 11)
        ]
        with patch.dict("os.environ", {agent.MAX_REFERENCE_AGE_ENV: "180"}), \
             patch.object(agent, "room_tail", return_value=(records, 10)):
            with self.assertRaisesRegex(ValueError, "underlying market reference is stale"):
                agent.prices()

        records = [
            (message, {**row, "age_s": 180})
            for message, row in records
        ]
        records = [
            (message, {**row, "ref": {
                **row["ref"],
                "time": (stamp - timedelta(seconds=180)).isoformat(),
            }})
            for message, row in records
        ]
        with patch.dict("os.environ", {agent.MAX_REFERENCE_AGE_ENV: "180"}), \
             patch.object(agent.time, "time", return_value=stamp.timestamp()), \
             patch.object(agent, "room_tail", return_value=(records, 10)):
            price, _, _ = agent.prices()
            self.assertEqual(price["for"], 11)

        with patch.dict("os.environ", {agent.MAX_REFERENCE_AGE_ENV: "NaN"}), \
             patch.object(agent, "room_tail", return_value=(records, 10)):
            with self.assertRaisesRegex(ValueError, agent.MAX_REFERENCE_AGE_ENV):
                agent.prices()

    def test_mismatched_market_reference_timestamp_fails_closed(self):
        stamp = datetime.now(UTC)
        records = [
            ({"from": self.did, "ts": stamp.isoformat()},
             {"t": "price", "n": n, "for": n + 1, "age_s": 0,
              "ref": {"px": "220", "time": (stamp - timedelta(minutes=5)).isoformat()},
              "limits": ["209", "231"]})
            for n in range(1, 11)
        ]
        with patch.object(agent, "room_tail", return_value=(records, 10)):
            with self.assertRaisesRegex(ValueError, "timestamp does not match reported age"):
                agent.prices()

    def test_market_reference_timestamp_must_include_timezone(self):
        with self.assertRaisesRegex(ValueError, "timestamp must include a timezone"):
            agent.parse_time("2026-10-09T12:00:00")

    def test_monitor_does_not_infer_account_from_omitted_mint(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "signer", return_value=(self.key, self.did)), \
             patch.object(agent, "room_tail") as tail, patch.object(agent, "offer") as offer:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 12, "mint": "unconfirmed"}, "offer": None})
            def rooms(room, limit):
                row = {"t": "price", "n": 5} if room == "d-close1-price" else {
                    "t": "flow", "n": 5, "mints": [], "omitted": {"mints": 1},
                }
                return [({"from": self.did}, row)], 5
            tail.side_effect = rooms
            result = agent.monitor_once(path)
            self.assertEqual(result["registration"]["mint"], "unconfirmed")
            offer.assert_not_called()

    def test_visible_mint_still_does_not_authorize_trading(self):
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(agent, "signer", return_value=(self.key, self.did)), \
             patch.object(agent, "room_tail") as tail, patch.object(agent, "offer") as offer:
            path = Path(directory) / "state.json"
            agent.state_save(path, {"registration": {"venue_seq": 12, "mint": "unconfirmed"}, "offer": None})
            def rooms(room, limit):
                row = {"t": "price", "n": 5} if room == "d-close1-price" else {
                    "t": "flow", "n": 5, "mints": [self.did], "settled": [],
                }
                return [({"from": self.did}, row)], 5
            tail.side_effect = rooms
            result = agent.monitor_once(path)
            self.assertEqual(result["registration"]["mint"], "visible in signed flow")
            offer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
