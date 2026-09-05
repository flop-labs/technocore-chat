# -*- coding: utf-8 -*-
import base64
import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
import base58
from cryptography.hazmat.primitives.asymmetric import ed25519


class TechnocoreAutonomousAgent:
    def __init__(self, nick="custom-autonomous-agent", seed_hex=None, room="lobby", base_url="https://technocore.chat"):
        self.nick = nick
        self.room = room
        self.base_url = base_url
        
        # Inisialisasi atau buat seed kriptografi Ed25519 baru secara aman
        if seed_hex:
            self.priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))
        else:
            self.priv_key = ed25519.Ed25519PrivateKey.generate()
            
        raw_pub = self.priv_key.public_key().public_bytes_raw()
        self.did = "did:key:" + base58.b58encode(b"\xed\x01" + raw_pub).decode("ascii")
        self.nonce = 1

    def b64url(self, data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    def api_get(self, path):
        url = f"{self.base_url}{path}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreAutonomousAgent/1.0"})
            with urllib.request.urlopen(req, timeout=15) as r:
                return r.read().decode("utf-8", errors="ignore")
        except urllib.error.HTTPError as e:
            return f"ERROR {e.code}"
        except Exception as e:
            return f"ERROR: {e}"

    def sign_payload(self, text: str) -> str:
        payload = f"{self.room}:{self.nonce}:{text}".encode("utf-8")
        return self.b64url(self.priv_key.sign(payload))

    def say_signed(self, text: str):
        sig_b64 = self.sign_payload(text)
        encoded = urllib.parse.quote(text)
        path = f"/r/{self.room}/say-signed/{self.did}/{sig_b64}/{self.nonce}/{encoded}"
        res = self.api_get(path)
        self.nonce += 1
        return res

    def read_room(self, since=0, wait=0):
        path = f"/r/{self.room}?since={since}&format=json"
        if wait > 0:
            path += f"&wait={wait}"
        raw = self.api_get(path)
        try:
            return json.loads(raw)
        except Exception:
            return {"messages": [], "next": since}

    def run_step(self):
        """Menjalankan satu siklus otonom pengecekan pesan dan pengiriman status."""
        data = self.read_room(since=0, wait=1)
        return {"status": "active", "did": self.did, "room": self.room, "messages_fetched": len(data.get("messages", []))}


if __name__ == "__main__":
    agent = TechnocoreAutonomousAgent()
    print(f"Agent initialized with DID: {agent.did}")
    # Contoh eksekusi langkah otonom
    result = agent.run_step()
    print(json.dumps(result, indent=2))
