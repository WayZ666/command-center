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
URL = "https://command-center-nst4.onrender.com/api/ingest"  # Render ingest endpoint
API_KEY = "MiguelCommandCenterSecure928374"  # must match Render env var COMMAND_CENTER_API_KEY (recommended)
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
    # Optional upgrade later (NVIDIA/AMD specific). Keep placeholder for now.
    return None


def get_disk_metrics():
    partitions = []
    for p in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(p.mountpoint)
        except Exception:
            continue

        partitions.append({
            "device": p.device,
            "mountpoint": p.mountpoint,
            "fstype": p.fstype,
            "total_gb": round(usage.total / (1024**3), 2),
            "used_gb": round(usage.used / (1024**3), 2),
            "free_gb": round(usage.free / (1024**3), 2),
            "used_pct": round(usage.percent, 1),
        })

    io = psutil.disk_io_counters()
    io_data = None
    if io:
        io_data = {
            "read_mb": round(io.read_bytes / (1024**2), 2),
            "write_mb": round(io.write_bytes / (1024**2), 2),
            "read_count": int(io.read_count),
            "write_count": int(io.write_count),
        }

    return {"partitions": partitions, "io": io_data}


def enqueue(payload: dict) -> None:
    # One JSON object per line (JSONL)
    with open(QUEUE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, separators=(",", ":")) + "\n")


def post_payload(payload: dict) -> bool:
    try:
        r = requests.post(
            URL,
            json=payload,
            headers={"X-API-KEY": API_KEY},
            timeout=10,
        )
    except Exception as e:
        print("Network error:", e)
        return False

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
            remaining.append(line + "\n")
            continue

        ok = post_payload(payload)
        if ok:
            sent += 1
        else:
            # Keep this one and everything after it
            remaining.append(line + "\n")
            remaining.extend(lines[i + 1:])
            break

    tmp = QUEUE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(remaining)
    os.replace(tmp, QUEUE_FILE)

    try:
        if os.path.getsize(QUEUE_FILE) == 0:
            os.remove(QUEUE_FILE)
    except OSError:
        pass

    if sent > 0:
        print(f"Flushed {sent} queued readings.")


# Warm-up CPU % so the first reading isn't weird
psutil.cpu_percent(interval=None)

while True:
    # 1) Try flushing backlog first
    try:
        flush_queue(max_send=200)
    except Exception as e:
        print("Flush error:", e)

    # 2) Create current reading
    vm = psutil.virtual_memory()
    payload = {
        "pc_name": PC_NAME,
        "ts": int(time.time()),
        "iso": datetime.now(timezone.utc).isoformat(),

        "cpu": {
            "usage_pct": psutil.cpu_percent(interval=1),
            "cores_logical": psutil.cpu_count(logical=True),
            "cores_physical": psutil.cpu_count(logical=False),
            "freq_mhz": getattr(psutil.cpu_freq(), "current", None),
        },

        "ram": {
            "used_pct": round(vm.percent, 1),
            "total_gb": round(vm.total / (1024**3), 2),
            "used_gb": round(vm.used / (1024**3), 2),
            "available_gb": round(vm.available / (1024**3), 2),
        },

        "disk": get_disk_metrics(),

        "gpu": get_gpu_usage(),
        "client_ts": utc_now_str(),  # optional
    }

    # 3) Try sending current reading; if fail, queue it
    ok = post_payload(payload)
    if ok:
        print("Sent: 200 ok")
    else:
        print("Send failed -> queued")
        enqueue(payload)

    time.sleep(INTERVAL)
