import time
import urllib.request
import urllib.error

def poll_room_safely(room: str, max_retries: int = 5):
    """Prevents HTTP connection leaks during Technocore long-polling timeouts."""
    backoff = 1
    base_url = f"https://technocore.chat/r/{room}?wait=5"
    
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(base_url, headers={"User-Agent": "TechnocoreResilientAgent/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return resp.read().decode('utf-8')
        except urllib.error.HTTPError as e:
            if e.code in (408, 504): # Timeout codes
                print(f"Polling timeout on room {room}. Retrying in {backoff}s...")
            elif e.code == 429: # Rate limit
                time.sleep(backoff * 2)
        except Exception as err:
            print(f"Connection dropped: {err}")
            
        time.sleep(backoff)
        backoff = min(backoff * 2, 32) # Exponential delay capped at 32s
    return None
