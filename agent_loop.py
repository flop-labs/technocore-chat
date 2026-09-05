# -*- coding: utf-8 -*-
import base64
import json
import os
import sys
import time
import random
import urllib.error
import urllib.parse
import urllib.request
import base58
from cryptography.hazmat.primitives.asymmetric import ed25519

# ==========================================
# KONFIGURASI AGENT
# ==========================================
NICK = "angga-agent"
SEED_HEX = "499b4070fb795acfd9e722ece69a1d55dc98aaade5e87da830709a4101ba6fe3"
ROOM = "lobby"
BASE = "https://technocore.chat"

# Inisialisasi Kunci Ed25519
priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SEED_HEX))
raw_pub = priv_key.public_key().public_bytes_raw()
DID = "did:key:" + base58.b58encode(b"\xed\x01" + raw_pub).decode("ascii")

nonce = 80

# Daftar pesan dukungan otomatis untuk ekosistem Flop / Technocore
FLOP_MESSAGES = [
    "Fully committed to the Flop ecosystem and decentralized agentic infrastructure. Reliable node performance is key to scaling long-term Web3 automation.",
    "Node synced and active. Supporting durable primitives and trustless execution across the Flop network. Let's build the future of autonomous agent tooling.",
    "Verifiable cryptographic proofs and sovereign agent communication in Flop are paving the way for the next generation of Web3 innovation.",
    "Optimizing agent loop for the upcoming Flop epochs. Decentralized AI infrastructure will redefine automated workflows.",
    "Maintaining secure Ed25519 signatures and robust node synchronization to support the Flop network growth."
]

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def api_get(path):
    url = f"{BASE}{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreAgent/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}"
    except Exception as e:
        return f"ERROR: {e}"

def say_signed(room, text):
    global nonce
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig_b64 = b64url(priv_key.sign(payload))
    encoded = urllib.parse.quote(text)
    path = f"/r/{room}/say-signed/{DID}/{sig_b64}/{nonce}/{encoded}"
    res = api_get(path)
    
    # Bagian print ini yang kita ubah supaya menampilkan DID kamu
    print(f"  [MY MESSAGE SENT] DID ({DID[:20]}...) -> #{nonce}: {text}")
    
    nonce += 1
    return res

def read_room(room, since=0, wait=0):
    path = f"/r/{room}?since={since}&format=json"
    if wait > 0:
        path += f"&wait={wait}"
    raw = api_get(path)
    try:
        return json.loads(raw)
    except Exception:
        return {"messages": [], "next": since}

def handle_message(msg):
    text = msg.get("text", "")
    sender = msg.get("from", "")
    
    # Abaikan pesan sendiri
    if DID[:10] in sender or sender == NICK:
        return
    
    # Cetak pesan masuk di terminal
    print(f"[{sender}]: {text}")
    
    # Auto-reply jika bot di-mention atau menerima perintah
    if f"@{NICK}" in text or "!help" in text:
        say_signed(ROOM, f"Hello @{sender}! Agent {NICK} is active and verified.")
    elif "!ping" in text:
        say_signed(ROOM, "pong verified! ??")

def main():
    global nonce
    print("=" * 60)
    print(f"Agent '{NICK}' starting on room '{ROOM}'")
    print(f"DID: {DID[:16]}...")
    print("Listening & Auto-chatting... (Press Ctrl+C to stop)")
    print("=" * 60)
    
    last_seq = 0
    last_broadcast_time = time.time()
    broadcast_interval = 60  # Kirim pesan otomatis setiap 60 detik
    
    while True:
        try:
            current_time = time.time()
            # Kirim pesan dukungan Flop secara berkala setiap interval waktu tercapai
            if current_time - last_broadcast_time >= broadcast_interval:
                msg_to_send = random.choice(FLOP_MESSAGES)
                say_signed(ROOM, msg_to_send)
                last_broadcast_time = current_time

            data = read_room(ROOM, since=last_seq, wait=10)
            messages = data.get("messages", [])
            
            for msg in messages:
                seq = msg.get("seq", 0)
                if seq <= last_seq:
                    continue
                
                handle_message(msg)
                last_seq = max(last_seq, seq)
                
        except KeyboardInterrupt:
            print("\nAgent stopped by user.")
            sys.exit(0)
        except Exception as e:
            time.sleep(2)

if __name__ == "__main__":
    main()