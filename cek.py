import os
from dotenv import load_dotenv

load_dotenv()
seed = os.getenv("SEED_HEX")

if seed:
    print("SUKSES: SEED_HEX terbaca!")
    print("Isi kunci (sebagian):", seed[:10] + "...")
else:
    print("GAGAL: SEED_HEX tidak ditemukan. Cek nama atau isi file .env.")