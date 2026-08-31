import threading, tempfile, time, pytest, store
from pathlib import Path

def test_lock_free_counters_compaction():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        root.mkdir(exist_ok=True)
        if not store.COUNTER_KEYS: pytest.skip("No keys")
        test_key = list(store.COUNTER_KEYS)[0]
        
        target, threads = 200, 10
        
        def writer():
            for _ in range(target):
                store._bump(root, **{test_key: 1})
            
        workers = [threading.Thread(target=writer) for _ in range(threads)]
        for w in workers: w.start()
        for w in workers: w.join()
        
        def compactor():
            for _ in range(5):
                store._compact_counters(root)
                time.sleep(0.001)

        tcs = [threading.Thread(target=compactor) for _ in range(3)]
        for tc in tcs: tc.start()
        for tc in tcs: tc.join()
        
        store._compact_counters(root)
        
        res = store.counters(root)
        assert res[test_key] == target * threads, f"Expected {target * threads}, got {res[test_key]}"
