import json
import os
import sqlite3
import time
from datetime import datetime

from flask import Flask, jsonify, request, Response

# =========================
# CONFIG
# =========================
API_KEY = os.environ.get("COMMAND_CENTER_API_KEY", "MiguelCommandCenterSecure928374")
DB_PATH = os.environ.get("COMMAND_CENTER_DB", "telemetry.db")

OFFLINE_AFTER_SECONDS = 30  # mark PC offline if no ingest within this window

app = Flask(__name__)

# in-memory latest snapshot per PC for fast dashboard
LATEST = {}  # pc -> {"ts": int, "payload": dict}


# =========================
# DB
# =========================
def db_conn():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db_conn() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc TEXT NOT NULL,
            ts INTEGER NOT NULL,
            payload TEXT NOT NULL
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_pc_ts ON readings(pc, ts)")


def prune_old(days=7):
    cutoff = int(time.time()) - (days * 86400)
    with db_conn() as con:
        con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))


init_db()


# =========================
# HELPERS
# =========================
def is_auth_ok(data: dict) -> bool:
    # allow either header or json api_key
    header_key = request.headers.get("X-API-KEY")
    body_key = (data or {}).get("api_key")
    return (header_key == API_KEY) or (body_key == API_KEY)


def pc_online(last_ts: int) -> bool:
    return (int(time.time()) - int(last_ts)) <= OFFLINE_AFTER_SECONDS


def extract_metric(payload: dict, metric: str):
    # CPU + RAM
    if metric == "cpu_pct":
        return payload.get("cpu", {}).get("usage_pct")

    if metric == "ram_pct":
        return payload.get("ram", {}).get("used_pct")

    # Disk usage (C:)
    if metric == "disk_c_used_pct":
        disk = payload.get("disk", {})
        for p in disk.get("partitions", []):
            mp = str(p.get("mountpoint", "")).upper()
            if mp.startswith("C:"):
                return p.get("used_pct")
        return None

    # Total IO (lifetime counters, still useful trend if sampled)
    if metric == "disk_read_mb":
        return payload.get("disk", {}).get("io", {}).get("read_mb")

    if metric == "disk_write_mb":
        return payload.get("disk", {}).get("io", {}).get("write_mb")

    # Uptime
    if metric == "uptime_seconds":
        return payload.get("uptime", {}).get("uptime_seconds")

    return None


# =========================
# ROUTES
# =========================
@app.route("/api/ingest", methods=["POST"])
def ingest():
    data = request.get_json(force=True, silent=True) or {}

    if not is_auth_ok(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    pc = data.get("pc_name") or data.get("pc") or "Unknown"
    ts = int(data.get("ts") or time.time())

    # store raw payload
    with db_conn() as con:
        con.execute(
            "INSERT INTO readings (pc, ts, payload) VALUES (?, ?, ?)",
            (pc, ts, json.dumps(data))
        )

    # update latest cache
    LATEST[pc] = {"ts": ts, "payload": data}

    # occasional pruning
    if ts % 300 < 2:  # roughly every 5 minutes depending on ingest timing
        try:
            prune_old(days=7)
        except Exception:
            pass

    return jsonify({"ok": True})


@app.route("/api/latest", methods=["GET"])
def api_latest():
    now = int(time.time())

    out = {}
    for pc, item in LATEST.items():
        last_ts = int(item["ts"])
        out[pc] = {
            "online": (now - last_ts) <= OFFLINE_AFTER_SECONDS,
            "last_ts": last_ts,
            "last_iso": datetime.utcfromtimestamp(last_ts).isoformat() + "Z",
            "payload": item["payload"],
        }
    return jsonify(out)


@app.route("/api/history", methods=["GET"])
def api_history():
    pc = request.args.get("pc", "")
    metric = request.args.get("metric", "cpu_pct")
    hours = float(request.args.get("hours", "24"))

    if not pc:
        return jsonify({"ok": False, "error": "pc required"}), 400

    now = int(time.time())
    start = now - int(hours * 3600)

    with db_conn() as con:
        rows = con.execute(
            "SELECT ts, payload FROM readings WHERE pc=? AND ts>=? ORDER BY ts ASC",
            (pc, start)
        ).fetchall()

    points = []
    for r in rows:
        ts = int(r["ts"])
        try:
            payload = json.loads(r["payload"])
        except Exception:
            continue

        val = extract_metric(payload, metric)
        if val is None:
            continue
        points.append({"ts": ts, "value": val})

    return jsonify({
        "pc": pc,
        "metric": metric,
        "hours": hours,
        "points": points
    })


# =========================
# SIMPLE DASHBOARD (NO TEMPLATE FILES NEEDED)
# =========================
@app.route("/", methods=["GET"])
def dashboard():
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Command Center</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 18px; }}
    .row {{ display: flex; gap: 18px; flex-wrap: wrap; }}
    .card {{ border: 1px solid #ddd; border-radius: 10px; padding: 14px; min-width: 320px; flex: 1; }}
    .pill {{ display:inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; }}
    .ok {{ background:#e8fff0; border:1px solid #9be7b3; }}
    .bad {{ background:#ffecec; border:1px solid #f3a7a7; }}
    select,input {{ padding: 8px; border-radius: 8px; border: 1px solid #ccc; }}
    canvas {{ width: 100% !important; height: 260px !important; }}
    .muted {{ color:#666; font-size: 12px; }}
  </style>
</head>
<body>
  <h2>Command Center</h2>

  <div class="card" style="max-width:900px;">
    <div class="row" style="align-items:center;">
      <div>
        <div class="muted">PC</div>
        <select id="pcSelect"></select>
      </div>

      <div>
        <div class="muted">Metric</div>
        <select id="metricSelect">
          <option value="cpu_pct">CPU %</option>
          <option value="ram_pct">RAM %</option>
          <option value="disk_c_used_pct">Disk C: Used %</option>
          <option value="disk_read_mb">Disk Read MB (total)</option>
          <option value="disk_write_mb">Disk Write MB (total)</option>
          <option value="uptime_seconds">Uptime (seconds)</option>
        </select>
      </div>

      <div>
        <div class="muted">Hours</div>
        <input id="hoursInput" type="number" value="24" min="1" max="168" />
      </div>

      <div style="flex:1;">
        <div class="muted">Status</div>
        <div id="statusLine">Loading...</div>
      </div>

      <div>
        <button id="refreshBtn" style="padding:8px 12px; border-radius:10px; border:1px solid #ccc; cursor:pointer;">
          Refresh
        </button>
      </div>
    </div>
  </div>

  <div class="row" style="margin-top:18px;">
    <div class="card">
      <h3 style="margin-top:0;">Historical Graph</h3>
      <canvas id="chart"></canvas>
    </div>

    <div class="card">
      <h3 style="margin-top:0;">Live Snapshot</h3>
      <pre id="liveJson" style="white-space:pre-wrap;"></pre>
    </div>
  </div>

<script>
let chart;

function pill(online) {{
  return online
    ? '<span class="pill ok">LIVE</span>'
    : '<span class="pill bad">OFFLINE</span>';
}}

async function fetchLatest() {{
  const res = await fetch('/api/latest');
  return await res.json();
}}

async function fetchHistory(pc, metric, hours) {{
  const res = await fetch(`/api/history?pc=${{encodeURIComponent(pc)}}&metric=${{encodeURIComponent(metric)}}&hours=${{hours}}`);
  return await res.json();
}}

function ensureChart(labels, values, metric) {{
  const ctx = document.getElementById('chart').getContext('2d');
  if (!chart) {{
    chart = new Chart(ctx, {{
      type: 'line',
      data: {{
        labels,
        datasets: [{{
          label: metric,
          data: values,
          tension: 0.2
        }}]
      }},
      options: {{
        responsive: true,
        animation: false,
        scales: {{
          y: {{
            beginAtZero: true
          }}
        }}
      }}
    }});
  }} else {{
    chart.data.labels = labels;
    chart.data.datasets[0].label = metric;
    chart.data.datasets[0].data = values;
    chart.update();
  }}
}}

function tsToLabel(ts) {{
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString();
}}

async function populatePCs(latest) {{
  const select = document.getElementById('pcSelect');
  const pcs = Object.keys(latest);
  select.innerHTML = '';
  pcs.forEach(pc => {{
    const opt = document.createElement('option');
    opt.value = pc;
    opt.textContent = pc;
    select.appendChild(opt);
  }});
  if (pcs.length === 0) {{
    const opt = document.createElement('option');
    opt.value = '';
    opt.textContent = 'No PCs yet';
    select.appendChild(opt);
  }}
}}

async function refreshAll() {{
  const latest = await fetchLatest();
  await populatePCs(latest);

  const pc = document.getElementById('pcSelect').value;
  const metric = document.getElementById('metricSelect').value;
  const hours = document.getElementById('hoursInput').value || 24;

  if (!pc || !latest[pc]) {{
    document.getElementById('statusLine').innerHTML = 'No telemetry received yet.';
    document.getElementById('liveJson').textContent = '';
    return;
  }}

  const info = latest[pc];
  document.getElementById('statusLine').innerHTML =
    `${{pill(info.online)}} <span class="muted">Last:</span> ${{info.last_iso}}`;

  document.getElementById('liveJson').textContent =
    JSON.stringify(info.payload, null, 2);

  const hist = await fetchHistory(pc, metric, hours);
  const labels = hist.points.map(p => tsToLabel(p.ts));
  const values = hist.points.map(p => p.value);

  ensureChart(labels, values, metric);
}}

document.getElementById('refreshBtn').addEventListener('click', refreshAll);
document.getElementById('pcSelect').addEventListener('change', refreshAll);
document.getElementById('metricSelect').addEventListener('change', refreshAll);
document.getElementById('hoursInput').addEventListener('change', refreshAll);

refreshAll();
setInterval(refreshAll, 5000);
</script>

</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    # local dev
    app.run(host="0.0.0.0", port=5000, debug=True)
