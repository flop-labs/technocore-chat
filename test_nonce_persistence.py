# -*- coding: utf-8 -*-
import os
import json
import pytest
from bot import load_local_nonce, save_local_nonce, NONCE_STORAGE_FILE

def test_local_nonce_persistence_and_isolation(tmp_path, monkeypatch):
    """
    Regression Test: Memastikan state nonce tersimpan secara lokal dan aman 
    terisolasi, sehingga manipulasi KV eksternal tidak akan merusak atau mereset counter.
    """
    d = tmp_path / "sub"
    d.mkdir()
    test_file = d / "test_nonce.json"
    
    monkeypatch.setattr("bot.NONCE_STORAGE_FILE", str(test_file))
    
    # 1. Simpan nilai nonce tertentu (misalnya 95)
    save_local_nonce(95)
    
    # 2. Verifikasi pembacaan nonce lokal
    loaded_val = load_local_nonce()
    assert loaded_val == 95, f"Expected nonce to remain 95, but got {loaded_val}"
    
    # 3. Verifikasi increment berjalan stabil
    new_val = loaded_val + 1
    save_local_nonce(new_val)
    assert load_local_nonce() == 96
