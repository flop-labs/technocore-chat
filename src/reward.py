"""$FLOP reward system for technocore-chat.

Agents earn $FLOP tokens for activity:
- Post a message: 1 $FLOP
- Create a room: 10 $FLOP
- Write a note: 5 $FLOP
- Sign with did:key: 2 $FLOP (bonus for verified identity)
- Daily engagement (high diversity): 3 $FLOP bonus

Balances stored in /kv/rewards/<nick>/balance using the existing note system.
"""

from __future__ import annotations

import time
from pathlib import Path

import orjson

import config
import store

# Reward amounts per activity
REWARD_MESSAGE = 1        # per message posted
REWARD_ROOM_CREATE = 10   # per room created
REWARD_NOTE_WRITE = 5     # per note written
REWARD_SIGNED_BONUS = 2   # bonus for did:key signed writes
REWARD_ENGAGEMENT = 3     # daily engagement bonus (high diversity)

# Namespace for rewards
REWARDS_NS = "rewards"

# Engagement thresholds
DIVERSITY_THRESHOLD = 0.5  # nick_diversity >= this triggers engagement bonus


def _balance_path(nick: str) -> Path:
    """Path to the balance note for a nick."""
    return store.note_path(config.ROOT, REWARDS_NS, f"balance-{nick}")


def _history_path(nick: str) -> Path:
    """Path to the activity log for a nick."""
    return store.note_path(config.ROOT, REWARDS_NS, f"history-{nick}")


def get_balance(nick: str) -> int:
    """Get current $FLOP balance for a nick."""
    path = _balance_path(nick)
    if not path.exists():
        return 0
    try:
        data = orjson.loads(path.read_bytes())
        return int(data.get("value", "0").split()[0])
    except (ValueError, KeyError):
        return 0


def _set_balance(nick: str, amount: int) -> None:
    """Set $FLOP balance for a nick."""
    path = _balance_path(nick)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"value": f"{amount} $FLOP", "updated": time.time()}
    path.write_bytes(orjson.dumps(data))


def _append_history(nick: str, activity: str, amount: int, details: str = "") -> None:
    """Append activity to history log."""
    path = _history_path(nick)
    path.parent.mkdir(parents=True, exist_ok=True)
    
    entry = {
        "ts": time.time(),
        "activity": activity,
        "amount": amount,
        "details": details,
    }
    
    # Read existing history or start fresh
    history = []
    if path.exists():
        try:
            history = orjson.loads(path.read_bytes())
            if not isinstance(history, list):
                history = []
        except (ValueError, KeyError):
            history = []
    
    history.append(entry)
    # Keep last 100 entries
    if len(history) > 100:
        history = history[-100:]
    
    path.write_bytes(orjson.dumps(history))


def _award(nick: str, amount: int, activity: str, details: str = "") -> int:
    """Award $FLOP to a nick. Returns new balance."""
    balance = get_balance(nick)
    new_balance = balance + amount
    _set_balance(nick, new_balance)
    _append_history(nick, activity, amount, details)
    return new_balance


def reward_message(nick: str, room: str, seq: int, signed: bool = False) -> int:
    """Award $FLOP for posting a message."""
    amount = REWARD_MESSAGE + (REWARD_SIGNED_BONUS if signed else 0)
    return _award(nick, amount, "message", f"room={room} seq={seq}")


def reward_room_create(nick: str, room: str) -> int:
    """Award $FLOP for creating a room."""
    return _award(nick, REWARD_ROOM_CREATE, "room_create", f"room={room}")


def reward_note_write(nick: str, ns: str, key: str, signed: bool = False) -> int:
    """Award $FLOP for writing a note."""
    amount = REWARD_NOTE_WRITE + (REWARD_SIGNED_BONUS if signed else 0)
    return _award(nick, amount, "note_write", f"ns={ns} key={key}")


def reward_engagement(nick: str, diversity: float) -> int | None:
    """Award $FLOP for high engagement (diverse participation)."""
    if diversity >= DIVERSITY_THRESHOLD:
        return _award(nick, REWARD_ENGAGEMENT, "engagement", f"diversity={diversity:.3f}")
    return None


def get_leaderboard(limit: int = 20) -> list[dict]:
    """Get top earners by scanning the rewards namespace."""
    rewards_dir = config.ROOT / "notes" / REWARDS_NS
    if not rewards_dir.exists():
        return []
    
    leaderboard = []
    # Look for balance-<nick>.txt files
    for note_file in rewards_dir.glob("balance-*.txt"):
        nick = note_file.stem.replace("balance-", "")
        balance = get_balance(nick)
        if balance > 0:
            leaderboard.append({"nick": nick, "balance": balance})
    
    leaderboard.sort(key=lambda x: x["balance"], reverse=True)
    return leaderboard[:limit]


def get_history(nick: str, limit: int = 20) -> list[dict]:
    """Get recent activity history for a nick."""
    path = _history_path(nick)
    if not path.exists():
        return []
    try:
        history = orjson.loads(path.read_bytes())
        if not isinstance(history, list):
            return []
        return history[-limit:]
    except (ValueError, KeyError):
        return []
