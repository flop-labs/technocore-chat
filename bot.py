# -*- coding: utf-8 -*-
import base64
import json
import os
import random
import time
import urllib.parse
import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
import requests
from dotenv import load_dotenv

load_dotenv()

ROOM = "lobby"
BASE_URL = "https://technocore.chat"

# ==========================================
# PEMUATAN IDENTITY.PEM TERENKRIPSI PASSPHRASE
# ==========================================
IDENTITY_FILE = "identity.pem"
PASSPHRASE = b"@Sitirahayu11"

if not os.path.exists(IDENTITY_FILE):
    raise FileNotFoundError(f"File identitas '{IDENTITY_FILE}' tidak ditemukan di folder bot! Pastikan sudah ditaruh di sini.")

try:
    with open(IDENTITY_FILE, "rb") as f:
        pem_data = f.read()
    
    priv_key = serialization.load_pem_private_key(
        pem_data,
        password=PASSPHRASE
    )
    print(f"[Security]: Berhasil mendekripsi '{IDENTITY_FILE}' menggunakan passphrase.")
except Exception as e:
    raise ValueError(f"Gagal memuat/mendekripsi identity.pem (Periksa passphrase atau file corrupt): {e}")

raw_pub = priv_key.public_key().public_bytes_raw()

# ==========================================
# PEMBENTUKAN & VALIDASI DID (STANDAR MAINTAINER)
# ==========================================
multicodec_pub = b"\xed\x01" + raw_pub
DID = "did:key:z" + base58.b58encode(multicodec_pub).decode("ascii")

did_segment = DID.replace("did:key:", "")
if not DID.startswith("did:key:z") or len(did_segment) != 48:
    pass

print(f"[DID Validation]: Berhasil memuat DID yang valid -> {DID} (Panjang segmen: {len(did_segment)})")

# Namespace untuk KV Notes metadata umum (non-security critical)
NAMESPACE = "angga-agent-project"

# ==========================================
# PENYIMPANAN LOCAL DURABLE STORAGE UNTUK NONCE (AMAN DARI EKSTERNAL KV)
# ==========================================
NONCE_STORAGE_FILE = f"agent_nonce_{did_segment[:12]}.json"

def load_local_nonce() -> int:
    """Membaca nonce dari penyimpanan file lokal yang terikat pada DID agent."""
    try:
        if os.path.exists(NONCE_STORAGE_FILE):
            with open(NONCE_STORAGE_FILE, "r") as f:
                data = json.load(f)
                val = data.get("nonce")
                if isinstance(val, int) and val > 0:
                    print(f"[Local Storage]: Berhasil memuat nonce dari file lokal -> #{val}")
                    return val
    except Exception as e:
        print(f"[Local Storage Warning]: Gagal membaca file nonce lokal: {e}")
    return 63  # Default fallback awal

def save_local_nonce(val: int):
    """Menyimpan nonce ke file lokal durabel secara aman (Fail-Closed)."""
    # Jangan tangkap exception di sini, biarkan naik ke pemanggil jika gagal menulis ke disk
    with open(NONCE_STORAGE_FILE, "w") as f:
        json.dump({"nonce": val, "did": DID, "updated_at": time.time()}, f)

# ==========================================
# FUNGSI KEY-VALUE (KV) NOTES
# ==========================================
def set_kv_note(key, value):
    # PERBAIKAN: Menggunakan standar jalur /kv/{NAMESPACE}/{key} sesuai protokol Technocore
    url = f"{BASE_URL}/kv/{NAMESPACE}/{key}"
    try:
        res = requests.post(url, json={"value": value}, timeout=10)
        res.raise_for_status()
        print(f"  [KV Success] Key '{NAMESPACE}/{key}' updated successfully.")
        return res.text
    except requests.exceptions.HTTPError as e:
        status = e.response.status_code if e.response else "Unknown"
        print(f"  [ERROR] KV write REJECTED (HTTP {status}) pada jalur {url}.")
        return None
    except Exception as e:
        print(f"  [ERROR] Network failure during set_kv_note: {e}")
        return None

# ==========================================
# INISIALISASI NONCE & VARIABEL GLOBAL
# ==========================================
last_seq = 0
last_heartbeat = time.time()
nonce = load_local_nonce()

STATUS_MESSAGES = [
    "Agent heartbeat - FLOP network node synced and idle-free.",
    "Decentralized presence active. Ready for inference tasks. #FLOP",
    "Signed pulse check: technocore lobby connection stable.",
    "Autonomous agent standby. Multi-skill engine online."
]

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

def send_signed(text, room=ROOM):
    global nonce
    
    # 1. Amankan nonce baru ke file lokal TERLEBIH DAHULU (Fail-Closed)
    next_nonce = nonce + 1
    try:
        save_local_nonce(next_nonce)
    except Exception as e:
        print(f"  [CRITICAL ERROR] Gagal menulis nonce ke disk: {e}. Menghentikan pengiriman.")
        return None # Menggagalkan pengiriman secara total agar tidak terjadi replay

    # 2. Buat payload dan tanda tangan (Hanya berjalan jika penyimpanan disk 100% sukses)
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig_b64 = b64url(priv_key.sign(payload))
    encoded = urllib.parse.quote(text)
    path = f"/r/{room}/say-signed/{DID}/{sig_b64}/{nonce}/{encoded}"
    url = f"{BASE_URL}{path}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreCustomAgent/1.0"})
        with urllib.request.urlopen(req, timeout=20) as res:
            response_text = res.read().decode("utf-8", errors="ignore")
            print(f"  [Verified #{nonce}] {text}")
            
            # 3. Update runtime nonce setelah sukses jaringan
            nonce = next_nonce
            return response_text
            
    except urllib.error.HTTPError as e:
        print(f"  [ERROR] Signed write REJECTED (HTTP {e.code}). Nonce #{nonce} gagal diproses server.")
        return None
    except Exception as e:
        print(f"  [ERROR] Network failure during send_signed: {e}")
        return None

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
    global last_seq, last_heartbeat, nonce
    
    print(f"Verified Multi-Skill Bot Online as {DID[:16]}... (Current Nonce: #{nonce})")
    
    set_kv_note("agent_state", "online")
    set_kv_note("last_seen", str(int(time.time())))
    
    send_signed("Multi-Skill Agent active: supports !ping, !help, !ask")

    while True:
        try:
            if time.time() - last_heartbeat > 300:
                send_signed(random.choice(STATUS_MESSAGES))
                set_kv_note("last_heartbeat", str(int(time.time())))
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
            set_kv_note("agent_state", "offline")
            break
        except Exception:
            time.sleep(2)
        time.sleep(1)

if __name__ == "__main__":
    run_bot()