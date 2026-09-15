from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

# Seed hex lama Anda
seed_hex = "499b4070fb795acfd9e722ece69a1d55dc98aaade5e87da830709a4101ba6fe3"
seed_bytes = bytes.fromhex(seed_hex)

# Generate private key Ed25519
private_key = ed25519.Ed25519PrivateKey.from_private_bytes(seed_bytes)

# Enkripsi ke format PKCS#8 PEM menggunakan passphrase Anda
pem_data = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.BestAvailableEncryption(b"@Sitirahayu11")
)

# Simpan ke file identity-lama.pem
with open("identity-lama.pem", "wb") as f:
    f.write(pem_data)

print("Berhasil! Seed hex lama sudah diubah menjadi file identity-lama.pem yang aman.")