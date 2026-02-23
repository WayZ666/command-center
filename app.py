import time
import socket
import json
import os
from datetime import datetime, timezone

import requests
import psutil

# ========================
# CONFIG
# ========================
URL = "https://command-center-nst4.onrender.com/api/ingest"
API_KEY = "MiguelCommandCenterSecure928374"  # must match Render env var API_KEY
INTERVAL = 5  # seconds between readings
QUEUE_FILE = "queue.jsonl"  # local backlog file (same folder as agent.py)

PC_NAME = socket.gethostname()
print("=== Command Center Agent (Buffered) ===")
print("Agent running:", PC_NAME, "->", URL)
print("Interval:", INTERVAL, "seconds")
print("Queue file:", QUEUE_FILE)
print("======================================\n")


def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def get_gpu_usage():
    # We'll add real GPU usage next (easy upgrade)
    return None


def enqueue(payload: dict) -> None:
    # One JSON object per line (JSONL)
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def post_payload(payload: dict) -> bool:
    r = requests.post(
        URL,
        json=payload,
        headers={"X-API-Key": API_KEY},
        timeout=10,
    )
    # 200 means success
    if r.status_code == 200:
        return True

    print("Server responded:", r.status_code, r.text)
    return False


def flush_queue(max_send: int = 200) -> None:
    """
    Send queued payloads oldest->newest.
    If sending fails, stop and keep remaining lines.
    """
    if not os.path.exists(QUEUE_FILE):
        return

    with open(QUEUE_FILE, "r", encoding="utf-8") as f:
        lines = f.readlines()

    if not lines:
        try:
            os.remove(QUEUE_FILE)
        except OSError:
            pass
        return

    remaining = []
    sent = 0

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        if sent >= max_send:
            remaining.append(line + "\n")
            continue

        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            # Keep corrupted line so you don't lose anything silently
            remaining.append(line + "\n")
            continue

        try:
            ok = post_payload(payload)
        except Exception as e:
            print("Flush failed (network):", e)
            ok = False

        if ok:
            sent += 1
        else:
            # Keep this one and everything after it
            remaining.append(line + "\n")
            remaining.extend(lines[i + 1 :])
            break

    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(remaining)
    os.replace(tmp, QUEUE_FILE)

    # Remove file if empty
    try:
        if os.path.getsize(QUEUE_FILE) == 0:
            os.remove(QUEUE_FILE)
    except OSError:
        pass

    if sent > 0:
        print(f"Flushed {sent} queued readings.")


while True:
    # 1) Try flushing backlog first
    try:
        flush_queue(max_send=200)
    except Exception as e:
        print("Flush error:", e)

    # 2) Create current reading
    payload = {
        "cpu": psutil.cpu_percent(interval=1),
        "ram": psutil.virtual_memory().percent,
        "gpu": get_gpu_usage(),
        "notes": PC_NAME,              # ✅ real PC name (not the string "PC_NAME")
        "client_ts": utc_now_str(),    # optional (server still stores its own ts)
    }

    # 3) Try sending current reading; if fail, queue it
    try:
        ok = post_payload(payload)
        if ok:
            print("Sent: 200 ok")
        else:
            print("Send failed -> queued")
            enqueue(payload)
    except Exception as e:
        print("Send error -> queued:", e)
        enqueue(payload)

    time.sleep(INTERVAL)
