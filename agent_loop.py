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
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# KONFIGURASI AGENT
# ==========================================
NICK = "angga-agent"
ROOM = "lobby"
BASE = "https://technocore.chat"

# ==========================================
# PEMUATAN IDENTITY.PEM TERENKRIPSI PASSPHRASE
# ==========================================
from cryptography.hazmat.primitives import serialization

IDENTITY_FILE = "identity.pem"
PASSPHRASE = b"@Sitirahayu11"

if not os.path.exists(IDENTITY_FILE):
    raise FileNotFoundError(f"File identitas '{IDENTITY_FILE}' tidak ditemukan di folder bot!")

try:
    with open(IDENTITY_FILE, "rb") as f:
        pem_data = f.read()
    
    priv_key = serialization.load_pem_private_key(
        pem_data,
        password=PASSPHRASE
    )
    print(f"[Security]: Berhasil mendekripsi '{IDENTITY_FILE}' menggunakan passphrase.")
except Exception as e:
    raise ValueError(f"Gagal memuat/mendekripsi identity.pem: {e}")

raw_pub = priv_key.public_key().public_bytes_raw()

# PERBAIKAN: DID menggunakan standar multibase prefix 'z' (Sesuai Maintainer)
multicodec_pub = b"\xed\x01" + raw_pub
DID = "did:key:z" + base58.b58encode(multicodec_pub).decode("ascii")
did_segment = DID.replace("did:key:", "")

# ==========================================
# PENYIMPANAN LOCAL DURABLE STORAGE UNTUK NONCE
# ==========================================
NONCE_STORAGE_FILE = f"agent_nonce_loop_{did_segment[:12]}.json"

def load_local_nonce() -> int:
    """Membaca nonce dari penyimpanan file lokal yang terikat pada DID agent."""
    try:
        if os.path.exists(NONCE_STORAGE_FILE):
            with open(NONCE_STORAGE_FILE, "r") as f:
                data = json.load(f)
                val = data.get("nonce")
                if isinstance(val, int) and val > 0:
                    return val
    except Exception:
        pass
    return 80  # Fallback awal jika belum ada file

def save_local_nonce(val: int):
    """Menyimpan nonce ke file lokal durabel secara aman."""
    try:
        with open(NONCE_STORAGE_FILE, "w") as f:
            json.dump({"nonce": val, "did": DID, "updated_at": time.time()}, f)
    except Exception as e:
        print(f"[Local Storage Error]: Gagal menyimpan nonce lokal: {e}")

# Muat nonce dari file lokal durabel
nonce = load_local_nonce()

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
    
    # Amankan/simpan nonce baru ke file lokal TERLEBIH DAHULU (Fail-Closed)
    next_nonce = nonce + 1
    try:
        save_local_nonce(next_nonce)
    except Exception as e:
        print(f"  [CRITICAL ERROR] Gagal menulis nonce ke disk: {e}. Menghentikan pengiriman.")
        return None

    # Buat payload dan tanda tangan menggunakan nonce saat ini
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig_b64 = b64url(priv_key.sign(payload))
    encoded = urllib.parse.quote(text)
    path = f"/r/{room}/say-signed/{DID}/{sig_b64}/{nonce}/{encoded}"
    res = api_get(path)
    
    print(f"  [MY MESSAGE SENT] DID ({DID[:20]}...) -> #{nonce}: {text}")
    
    # Majukan nonce di memori runtime setelah sukses pre-persist
    nonce = next_nonce
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
        say_signed(ROOM, "pong verified! 🏓")

def main():
    global nonce
    print("=" * 60)
    print(f"Agent '{NICK}' starting on room '{ROOM}'")
    print(f"DID: {DID[:16]}... (Current Nonce: #{nonce})")
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
        except Exception:
            time.sleep(2)

if __name__ == "__main__":
    main()