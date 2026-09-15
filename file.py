import os
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization
from dotenv import load_dotenv

load_dotenv()
seed_hex = os.getenv("SEED_HEX")
if not seed_hex:
    raise ValueError("SEED_HEX tidak ditemukan di .env!")

priv_key = ed25519.Ed25519PrivateKey.from_private_bytes(bytes.fromhex(seed_hex))

pem_data = priv_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption()
)

with open("identity.pem", "wb") as f:
    f.write(pem_data)

print("Berhasil membuat file identity.pem!")