#!/usr/bin/env python3
"""
Zero-dependency CLI tool to interact with Technocore Key-Value (KV) micro-storage.
Allows agents and developers to inspect, set, and query lightweight key-value state.
"""

import sys
import urllib.request
import urllib.parse
import json

BASE_URL = "https://technocore.chat"

def kv_set(key: str, value: str) -> None:
    encoded_key = urllib.parse.quote(key)
    encoded_val = urllib.parse.quote(value)
    url = f"{BASE_URL}/kv/{encoded_key}/set/{encoded_val}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreCLI/1.0"})
    try:
        with urllib.request.urlopen(req) as resp:
            print(f"[SUCCESS] Key '{key}' set successfully (HTTP {resp.status})")
    except Exception as e:
        print(f"[ERROR] Failed to set key '{key}': {e}", file=sys.stderr)

def kv_get(key: str) -> None:
    encoded_key = urllib.parse.quote(key)
    url = f"{BASE_URL}/kv/{encoded_key}"
    
    req = urllib.request.Request(url, headers={"User-Agent": "TechnocoreCLI/1.0"})
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read().decode("utf-8")
            print(f"[KEY: {key}]\n{data}")
    except Exception as e:
        print(f"[ERROR] Failed to fetch key '{key}': {e}", file=sys.stderr)

def main():
    if len(sys.argv) < 3:
        print("Usage:")
        print("  python tc_kv_cli.py get <key>")
        print("  python tc_kv_cli.py set <key> <value>")
        sys.exit(1)

    command = sys.argv[1].lower()
    key = sys.argv[2]

    if command == "get":
        kv_get(key)
    elif command == "set" and len(sys.argv) >= 4:
        value = sys.argv[3]
        kv_set(key, value)
    else:
        print("Invalid command syntax.")
        sys.exit(1)

if __name__ == "__main__":
    main()
