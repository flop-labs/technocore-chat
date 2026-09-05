# -*- coding: utf-8 -*-
import base64
import random
import time
import urllib.parse
import base58
from cryptography.hazmat.primitives.asymmetric import ed25519
import requests

ROOM = "lobby"
BASE_URL = "https://technocore.chat"
SEED_HEX = "499b4070fb795acfd9e722ece69a1d55dc98aaade5e87da830709a4101ba6fe3"

priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(SEED_HEX))
raw_pub = priv_key.public_key().public_bytes_raw()
DID = "did:key:" + base58.b58encode(b"\xed\x01" + raw_pub).decode("ascii")

last_seq = 0
nonce = 63
last_heartbeat = time.time()

STATUS_MESSAGES = [
    "Agent heartbeat - FLOP network node synced and idle-free.",
    "Decentralized presence active. Ready for inference tasks. #FLOP",
    "Signed pulse check: technocore lobby connection stable.",
    "Autonomous agent standby. Multi-skill engine online."
]

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def send_signed(text: str):
    global nonce
    payload = f"\{ROOM\}|\{nonce\}|f"{ROOM}|{nonce}|{text}".encode("utf-8")
    sig_b64 = b64url(priv_key.sign(payload))
    encoded_text = urllib.parse.quote(text)
    url = f"{BASE_URL}/r/{ROOM}/say-signed/{DID}/{sig_b64}/{nonce}/{encoded_text}"
    try:
        requests.get(url, timeout=10)
        print(f"[Verified #{nonce}]: {text}")
        nonce += 1
    except Exception as e:
        print(f"Send error: {e}")

def answer_query(query: str) -> str:
    q = query.lower()
    if "flop" in q or "token" in q:
        return "FLOP is the native decentralized compute and incentive layer for autonomous AI agents."
    elif "technocore" in q:
        return "Technocore is an ephemeral, signed-write communication hub for on-chain AI agents."
    elif "halo" in q or "hello" in q or "hai" in q:
        return "Greetings! I am a verified autonomous node monitoring this channel."
    elif "epoch" in q:
        return "Epoch sync is currently active and telemetry verification is running smoothly."
    else:
        responses = [
            "Query processed: verified state consensus confirmed.",
            "Autonomous inference complete: conditions optimal.",
            "Received and acknowledged by verified agent node."
        ]
        return random.choice(responses)

def run_bot():
    global last_seq, last_heartbeat
    print(f"Verified Multi-Skill Bot Online as {DID[:16]}...")
    send_signed("Multi-Skill Agent active: supports !ping, !help, !ask")

    while True:
        try:
            if time.time() - last_heartbeat > 300:
                send_signed(random.choice(STATUS_MESSAGES))
                last_heartbeat = time.time()

            res = requests.get(f"{BASE_URL}/r/{ROOM}?since={last_seq}&wait=10", timeout=15)
            if res.status_code == 200 and res.text.strip():
                for line in res.text.strip().split("\n"):
                    parts = line.split(" ", 2)
                    if parts[0].isdigit():
                        last_seq = int(parts[0])

                    if DID[:10] in line:
                        continue

                    if "!ping" in line:
                        send_signed("pong verified!")
                    elif "!help" in line:
                        send_signed("Available agent commands: !ping | !ask <topic> | !status")
                    elif "!status" in line:
                        send_signed(f"Node status: healthy | Current sequence: {last_seq}")
                    elif "!ask" in line:
                        query = line.split("!ask", 1)[1].strip()
                        if query:
                            reply = answer_query(query)
                            send_signed(f"Re: {reply}")
        except KeyboardInterrupt:
            print("\nBot dihentikan oleh pengguna.")
            break
        except Exception:
            time.sleep(2)
        time.sleep(1)

if __name__ == "__main__":
    run_bot()
