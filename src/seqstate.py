"""Best-effort migration and retirement of room sequence-state shards."""

from pathlib import Path

import orjson


def maintain(root: Path, now: float) -> None:
    """Split a legacy map, then expire dated state for long-retired room names.

    The caller holds the room-create span, so a room cannot be recreated between the
    existence check and a shard rewrite. Entries without a valid timestamp are retained for
    downgrade and rolling-upgrade compatibility.
    """
    import store

    _split(store, root)
    cutoff = now - store.IDLE_SECONDS
    for path in root.glob(".seqstate.??"):
        try:
            with store._locked(path):
                state = store._read_seq_state(path)
                kept = {
                    room: entry
                    for room, entry in state.items()
                    if not store.NAME_RE.fullmatch(room)
                    or store.room_path(root, room).exists()
                    or not isinstance(entry, dict)
                    or type(entry.get("t")) not in (int, float)
                    or entry["t"] >= cutoff
                }
                if len(kept) != len(state):
                    store._replace(path, orjson.dumps(kept), fsync=store.config.FSYNC)
        except OSError:
            continue


def _split(store, root: Path) -> None:
    """Partition a pre-shard map once, preserving newer rolling-upgrade writes."""
    legacy = store._seq_state_path(root)
    shards: dict[Path, dict] = {}
    try:
        with store._locked(legacy):
            first = not (backup := legacy.with_suffix(".pre-shard")).exists()
            for room, entry in store._read_seq_state(legacy).items():
                shards.setdefault(store._seq_state_path(root, room), {})[room] = entry
            for path, entries in shards.items():
                with store._locked(path):
                    shard = store._read_seq_state(path)
                    merged = {**entries, **shard} if first else {**shard, **entries}
                    store._replace(path, orjson.dumps(merged), fsync=store.config.FSYNC)
            legacy.replace(backup) if first else legacy.unlink()
    except OSError:
        pass
