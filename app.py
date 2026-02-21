from flask import Flask, jsonify, request, render_template_string
import os
import sqlite3
from datetime import datetime, timezone

app = Flask(__name__)

API_KEY = os.environ.get("API_KEY", "dev-key-change-me")
DB_PATH = "data.db"

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            cpu REAL,
            ram REAL,
            gpu REAL,
            notes TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

DASH_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Command Center</title>
  <style>
    body{font-family:system-ui;margin:24px}
    .grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;max-width:720px}
    .card{border:1px solid #ddd;border-radius:12px;padding:14px}
    .big{font-size:28px;font-weight:700}
    .muted{color:#666}
    @media(max-width:520px){.grid{grid-template-columns:1fr}}
    table{border-collapse:collapse;width:100%;max-width:720px;margin-top:14px}
    td,th{border-bottom:1px solid #eee;padding:10px;text-align:left;font-size:14px}
  </style>
</head>
<body>
  <h1>🚀 Command Center</h1>
  <p class="muted">Latest PC stats sent from your agent.</p>

  <div class="grid">
    <div class="card"><div class="muted">CPU %</div><div class="big">{{cpu}}</div></div>
    <div class="card"><div class="muted">RAM %</div><div class="big">{{ram}}</div></div>
    <div class="card"><div class="muted">GPU %</div><div class="big">{{gpu}}</div></div>
    <div class="card"><div class="muted">Last update</div><div class="big" style="font-size:18px">{{ts}}</div></div>
  </div>

  <h2 style="margin-top:22px">Recent</h2>
  <table>
    <tr><th>Time (UTC)</th><th>CPU</th><th>RAM</th><th>GPU</th><th>Notes</th></tr>
    {% for r in rows %}
      <tr>
        <td>{{r["ts"]}}</td>
        <td>{{r["cpu"]}}</td>
        <td>{{r["ram"]}}</td>
        <td>{{r["gpu"]}}</td>
        <td>{{r["notes"] or ""}}</td>
      </tr>
    {% endfor %}
  </table>
</body>
</html>
"""

@app.get("/")
def home():
    conn = db()
    rows = conn.execute("SELECT ts,cpu,ram,gpu,notes FROM stats ORDER BY id DESC LIMIT 15").fetchall()
    conn.close()

    if rows:
        latest = rows[0]
        gpu_val = f'{latest["gpu"]:.1f}' if latest["gpu"] is not None else "—"
        return render_template_string(
            DASH_HTML,
            cpu=f'{latest["cpu"]:.1f}' if latest["cpu"] is not None else "—",
            ram=f'{latest["ram"]:.1f}' if latest["ram"] is not None else "—",
            gpu=gpu_val,
            ts=latest["ts"],
            rows=rows,
        )
    return "No stats yet. Agent hasn’t sent anything."

@app.route("/api/ingest", methods=["POST"], strict_slashes=False)
def ingest():
    sent_key = request.headers.get("X-API-Key", "")
    if sent_key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    cpu = data.get("cpu")
    ram = data.get("ram")
    gpu = data.get("gpu")
    notes = data.get("notes")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    conn.execute(
        "INSERT INTO stats (ts,cpu,ram,gpu,notes) VALUES (?,?,?,?,?)",
        (ts, cpu, ram, gpu, notes),
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True})

@app.get("/health")
def health():
    return jsonify({"status": "ok"})


