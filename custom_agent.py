# -*- coding: utf-8 -*-
import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import base58
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
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

# ==========================================
# PEMBENTUKAN & VALIDASI DID (STANDAR MAINTAINER)
# ==========================================
multicodec_pub = b"\xed\x01" + raw_pub
DID = "did:key:z" + base58.b58encode(multicodec_pub).decode("ascii")
did_segment = DID.replace("did:key:", "")

if not DID.startswith("did:key:z") or len(did_segment) != 48:
    raise ValueError("DID tidak valid sesuai standar multibase 'z'!")

print(f"[DID Validation]: Berhasil memuat DID yang valid -> {DID}")

NAMESPACE = "angga-agent-project"
NONCE_STORAGE_FILE = f"agent_nonce_custom_{did_segment[:12]}.json"

def load_local_nonce() -> int:
    try:
        if os.path.exists(NONCE_STORAGE_FILE):
            with open(NONCE_STORAGE_FILE, "r") as f:
                data = json.load(f)
                val = data.get("nonce")
                if isinstance(val, int) and val > 0:
                    return val
    except Exception:
      pass
    return 63

def save_local_nonce(val: int):
    try:
        with open(NONCE_STORAGE_FILE, "w") as f:
            json.dump({"nonce": val, "did": DID, "updated_at": time.time()}, f)
    except Exception as e:
        print(f"[Local Storage Error]: {e}")

nonce = load_local_nonce()

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
        return None

    # 2. Buat payload dan tanda tangan
    payload = f"{room}|{nonce}|{text}".encode("utf-8")
    sig_b64 = b64url(priv_key.sign(payload))
    encoded = urllib.parse.quote(text)
    path = f"/r/{room}/say-signed/{DID}/{sig_b64}/{nonce}/{encoded}"
    url = f"{BASE_URL}{path}"
    
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreCustomAgent/1.0"})
        with urllib.request.urlopen(req, timeout=10) as res:
            response_text = res.read().decode("utf-8", errors="ignore")
            print(f"  [Verified #{nonce}] {text}")
            
            # 3. Update runtime nonce setelah sukses
            nonce = next_nonce
            return response_text
            
    except urllib.error.HTTPError as e:
        print(f"  [ERROR] Signed write REJECTED (HTTP {e.code}). Nonce #{nonce} gagal diproses server.")
        return None
    except Exception as e:
        print(f"  [ERROR] Network failure during send_signed: {e}")
        return None

def set_kv_note(key, value):
    url = f"{BASE_URL}/kv/{NAMESPACE}/{key}"
    try:
        data = json.dumps({"value": value}).encode("utf-8")
        req = urllib.request.Request(
            url, 
            data=data, 
            headers={"Content-Type": "application/json", "User-Agent": "TechnocoreCustomAgent/1.0"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as res:
            print(f"  [KV Success] Key '{NAMESPACE}/{key}' updated successfully.")
            return res.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        print(f"  [ERROR] KV write REJECTED (HTTP {e.code}).")
        return None
    except Exception as e:
        print(f"  [ERROR] Network failure during set_kv_note: {e}")
        return None

if __name__ == "__main__":
    print(f"Custom Agent Initialized as {DID[:16]}... (Nonce: #{nonce})")
    set_kv_note("custom_agent_state", "active")
    send_signed("Custom agent script successfully verified and online!")