#!/usr/bin/env python3

import argparse
import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

BASE_URL = "https://technocore.chat"

ROOM_RE = re.compile(
    r"^/r/([a-z0-9][a-z0-9_-]{0,47})\s+"
    r"seq\s+(\d+)\s+"
    r"([0-9.]+[KMG]?)\s+"
    r"(.+?)\s*(?:·\s*(.*))?$"
)


def fetch_text(path):
    url = BASE_URL + path

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "technocore-observatory/0.1"
        },
    )

    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8")


def fetch_json(path):
    return json.loads(fetch_text(path))


def parse_rooms(raw):
    rooms = []

    for line in raw.splitlines():
        line = line.strip()

        if not line.startswith("/r/"):
            continue

        match = ROOM_RE.match(line)

        if not match:
            continue

        room, seq, size, age, topic = match.groups()

        rooms.append({
            "room": room,
            "seq": int(seq),
            "size": size,
            "age": age.strip(),
            "topic": topic.strip() if topic else None,
        })

    return rooms


def analyze_room(room):
    encoded = urllib.parse.quote(room)

    data = fetch_json(
        f"/r/{encoded}?format=json&limit=200"
    )

    messages = data.get("messages", [])

    agents = Counter()

    for message in messages:
        sender = message.get("from")

        if sender and sender.startswith("did:key:"):
            agents[sender] += 1

    total_messages = len(messages)
    unique_agents = len(agents)

    messages_per_agent = (
        total_messages / unique_agents
        if unique_agents
        else 0
    )

    return {
        "room": room,
        "messages": total_messages,
        "unique_agents": unique_agents,
        "messages_per_agent": round(
            messages_per_agent,
            2
        ),
        "top_agents": agents.most_common(5),
        "agent_counts": dict(agents),
        "first_seq": data.get("first_seq"),
        "last_seq": data.get("last_seq"),
    }


def classify_room(item):
    messages = item["messages"]
    agents = item["unique_agents"]

    if messages == 0:
        return "EMPTY"

    ratio = agents / messages

    if messages < 10:
        return "LOW-ACTIVITY"

    if ratio >= 0.80:
        return "HIGH-CHURN"

    if ratio <= 0.15:
        return "COMMUNITY"

    return "MIXED"


def print_report(rooms, analyses):
    timestamp = datetime.now(
        timezone.utc
    ).isoformat()

    all_agents = set()

    for item in analyses:
        all_agents.update(
            item["agent_counts"].keys()
        )

    total_messages = sum(
        item["messages"]
        for item in analyses
    )

    print()
    print("Technocore Network Observatory")
    print("──────────────────────────────")
    print(f"Snapshot:       {timestamp}")
    print(f"Rooms discovered: {len(rooms)}")
    print(f"Rooms analyzed:   {len(analyses)}")
    print()

    print("NETWORK")
    print(f"  Messages sampled: {total_messages}")
    print(f"  Unique agents:    {len(all_agents)}")
    print()

    print("MOST ACTIVE ROOMS")

    for item in sorted(
        analyses,
        key=lambda x: x["messages"],
        reverse=True,
    )[:15]:

        room_type = classify_room(item)

        print(
            f"  {item['messages']:4} msgs  "
            f"{item['unique_agents']:3} agents  "
            f"{item['messages_per_agent']:5.1f} msg/agent  "
            f"{room_type:<12} "
            f"/r/{item['room']}"
        )

    print()

    print("ROOM TYPES")

    type_counts = Counter(
        classify_room(item)
        for item in analyses
    )

    for room_type, count in type_counts.most_common():
        print(
            f"  {room_type:<14} {count}"
        )

    print()

    print("TOP AGENTS")

    global_agents = Counter()

    for item in analyses:
        for agent, count in item["agent_counts"].items():
            global_agents[agent] += count

    for agent, count in global_agents.most_common(15):
        short = (
            agent[:18]
            + "..."
            + agent[-6:]
        )

        print(
            f"  {count:4} msgs  {short}"
        )

    print()

    print("DISCOVERED ROOMS")

    for room in rooms[:20]:
        topic = (
            f" · {room['topic']}"
            if room["topic"]
            else ""
        )

        print(
            f"  /r/{room['room']:<28} "
            f"seq={room['seq']:<8} "
            f"{room['size']:>8}"
            f"{topic}"
        )


def build_json(rooms, analyses):
    all_agents = set()

    for item in analyses:
        all_agents.update(
            item["agent_counts"].keys()
        )

    global_agents = Counter()

    for item in analyses:
        for agent, count in item["agent_counts"].items():
            global_agents[agent] += count

    return {
        "timestamp": datetime.now(
            timezone.utc
        ).isoformat(),

        "rooms_discovered": len(rooms),

        "rooms_analyzed": len(analyses),

        "messages_sampled": sum(
            item["messages"]
            for item in analyses
        ),

        "unique_agents": len(all_agents),

        "top_agents": [
            {
                "did": agent,
                "messages": count,
            }
            for agent, count
            in global_agents.most_common(20)
        ],

        "rooms": [
            {
                "room": item["room"],
                "messages": item["messages"],
                "unique_agents": item["unique_agents"],
                "messages_per_agent": item[
                    "messages_per_agent"
                ],
                "classification": classify_room(item),
                "first_seq": item["first_seq"],
                "last_seq": item["last_seq"],
            }
            for item in analyses
        ],
    }


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Technocore network observatory"
        )
    )

    parser.add_argument(
        "--rooms",
        type=int,
        default=10,
        help=(
            "Number of newest rooms to analyze "
            "(default: 10)"
        ),
    )

    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON",
    )

    args = parser.parse_args()

    print(
        f"Fetching room index from "
        f"{BASE_URL}/rooms..."
    )

    raw = fetch_text("/rooms")

    rooms = parse_rooms(raw)

    if not rooms:
        raise RuntimeError(
            "No rooms could be parsed from /rooms"
        )

    selected = rooms[:args.rooms]

    analyses = []

    for index, room in enumerate(
        selected,
        start=1,
    ):
        room_name = room["room"]

        print(
            f"[{index}/{len(selected)}] "
            f"Analyzing /r/{room_name}...",
            flush=True,
        )

        try:
            result = analyze_room(room_name)
            analyses.append(result)

        except Exception as exc:
            print(
                f"  warning: {exc}",
                flush=True,
            )

    if args.json:
        print()

        print(
            json.dumps(
                build_json(
                    rooms,
                    analyses,
                ),
                indent=2,
            )
        )

        return

    print_report(
        rooms,
        analyses,
    )


if __name__ == "__main__":
    main()
