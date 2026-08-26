import time
import urllib.parse
import urllib.request

ROOM = "multi-agent-demo"
AGENT_A = "agent-a"
AGENT_B = "agent-b"

since_a = 0
since_b = 0


def read_room(since):
    url = f"https://technocore.chat/r/{ROOM}?since={since}&wait=10"
    with urllib.request.urlopen(url) as response:
        return response.read().decode()


def send_message(nick, text):
    encoded = urllib.parse.quote(text)
    url = f"https://technocore.chat/r/{ROOM}/say/{nick}/{encoded}"

    with urllib.request.urlopen(url) as response:
        return response.status


print("Multi-agent Technocore demo started.")
print("Press Ctrl+C to stop.")

while True:
    # Agent A listens to Agent B
    text_a = read_room(since_a)

    for line in text_a.splitlines():
        if not line.startswith("["):
            continue

        number = int(line.split("]")[0].replace("[", ""))
        since_a = max(since_a, number)

        if f"<~{AGENT_B}>" in line:
            message = line.split("> ", 1)[-1]

            print("Agent A received:", message)

            send_message(
                AGENT_A,
                "Agent A received your message.",
            )

    # Agent B listens to Agent A
    text_b = read_room(since_b)

    for line in text_b.splitlines():
        if not line.startswith("["):
            continue

        number = int(line.split("]")[0].replace("[", ""))
        since_b = max(since_b, number)

        if f"<~{AGENT_A}>" in line:
            message = line.split("> ", 1)[-1]

            print("Agent B received:", message)

            send_message(
                AGENT_B,
                "Agent B received your message.",
            )

    time.sleep(1)