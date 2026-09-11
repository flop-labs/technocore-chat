import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path


def test_concurrent_delivery_preserves_nonce_order(tmp_path) -> None:
    """A later sender must not overtake an earlier nonce holder."""

    worker = tmp_path / "worker.py"
    worker.write_text(
        r'''
import fcntl
import os
import sys
import time
from pathlib import Path

state_file = Path(sys.argv[1])
label = sys.argv[2]
delay = float(sys.argv[3])
log_file = Path(sys.argv[4])

with state_file.open("a+") as f:
    fcntl.flock(f.fileno(), fcntl.LOCK_EX)

    f.seek(0)
    raw = f.read().strip()
    last = int(raw) if raw else 0
    nonce = last + 1

    f.seek(0)
    f.truncate()
    f.write(str(nonce) + "\n")
    f.flush()
    os.fsync(f.fileno())

    # Simulate signing / network delivery while still holding the lock.
    time.sleep(delay)

    with log_file.open("a") as log:
        log.write(f"{label}:{nonce}\n")
        log.flush()
        os.fsync(log.fileno())
'''
    )

    state_file = tmp_path / "nonce"
    log_file = tmp_path / "delivery.log"

    a = subprocess.Popen(
        [sys.executable, str(worker), str(state_file), "A", "0.4", str(log_file)]
    )

    time.sleep(0.1)

    b = subprocess.Popen(
        [sys.executable, str(worker), str(state_file), "B", "0", str(log_file)]
    )

    assert a.wait(timeout=5) == 0
    assert b.wait(timeout=5) == 0

    lines = log_file.read_text().splitlines()

    assert lines == ["A:1", "B:2"]
