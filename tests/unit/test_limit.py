from starlette.requests import Request

import limit


def test_client_ip_ipv6_compression():
    # Helper to build a minimal Starlette request
    def make_req(host: str) -> Request:
        scope = {
            "type": "http",
            "client": (host, 12345),
            "headers": [],
        }
        return Request(scope)

    # 1. Two addresses in the same /64
    req1 = make_req("2001:db8::1")
    req2 = make_req("2001:db8::2")

    key1 = limit.client_ip(req1)
    key2 = limit.client_ip(req2)

    assert key1 == key2, f"Keys differ: {key1} vs {key2}"
    assert key1 == "2001:db8::/64", key1

    # 2. Fully expanded representation of the same /64
    req_expanded = make_req("2001:0db8:0000:0000:0000:0000:0000:0001")
    key_expanded = limit.client_ip(req_expanded)
    assert key_expanded == key1, f"Expanded representation differs: {key_expanded} vs {key1}"

    # 3. A different /64
    req3 = make_req("2001:db8:0:1::1")
    key3 = limit.client_ip(req3)
    assert key3 != key1, f"Different /64 collapsed to same key: {key3}"
    assert key3 == "2001:db8:0:1::/64", key3

    # 4. Standard IPv4
    req4 = make_req("192.168.1.1")
    key4 = limit.client_ip(req4)
    assert key4 == "192.168.1.1"


def test_client_ip_ipv4_mapped():
    def make_req(host: str) -> Request:
        scope = {
            "type": "http",
            "client": (host, 12345),
            "headers": [],
        }
        return Request(scope)

    # 5. IPv4-mapped addresses must not coalesce
    req1 = make_req("::ffff:192.0.2.1")
    req2 = make_req("::ffff:198.51.100.9")

    key1 = limit.client_ip(req1)
    key2 = limit.client_ip(req2)

    assert key1 == "192.0.2.1", f"Unexpected mapped key: {key1}"
    assert key2 == "198.51.100.9", f"Unexpected mapped key: {key2}"
    assert key1 != key2, "Mapped IPv4 addresses incorrectly coalesced"


def _req(host="203.0.113.7", headers=None):
    return Request(
        {
            "type": "http",
            "client": (host, 12345),
            "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        }
    )


def test_client_ip_strips_zone_id():
    assert limit.client_ip(_req("fe80::1%eth0")) == "fe80::/64"
    assert limit.client_ip(_req("fe80::1%eth0")) == limit.client_ip(_req("fe80::2%wlan0"))


def test_client_ip_header_groups_ipv6_and_takes_first_entry():
    req = _req(headers={"x-real-ip": "2001:db8::5, 10.0.0.1"})
    assert limit.client_ip(req, "x-real-ip") == "2001:db8::/64"


def test_client_ip_header_junk_falls_back_to_peer():
    for junk in ("not-an-ip", "", "999.1.1.1"):
        req = _req("198.51.100.4", {"x-real-ip": junk})
        assert limit.client_ip(req, "x-real-ip") == "198.51.100.4"


def test_client_ip_counts_proxy_evidence_only_without_header_config():
    before = limit._proxy_evidence["proxied_requests"]
    limit.client_ip(_req(headers={"x-forwarded-for": "1.2.3.4"}))
    assert limit._proxy_evidence["proxied_requests"] == before + 1
    limit.client_ip(_req(headers={"x-forwarded-for": "1.2.3.4"}), "x-forwarded-for")
    assert limit._proxy_evidence["proxied_requests"] == before + 1
