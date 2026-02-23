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


def ensure_columns(conn, table: str, needed_cols: dict):
    """
    needed_cols: {"col_name": "SQLTYPE", ...}
    Adds missing columns with ALTER TABLE (SQLite friendly migration).
    """
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, sql_type in needed_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")


def init_db():
    conn = db()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            cpu REAL,
            ram REAL,
            gpu REAL,
            notes TEXT
        )
        """
    )
    # migrate: add disk columns if missing
    ensure_columns(
        conn,
        "stats",
        {
            "disk_percent": "REAL",
            "disk_used_gb": "REAL",
            "disk_total_gb": "REAL",
        },
    )
    conn.commit()
    conn.close()


init_db()


def health_label_color(pct):
    """
    Returns (label, color_hex) based on percent thresholds.
    OK < 75, WARN 75-89.9, CRIT >= 90
    """
    if pct is None:
        return ("—", "#9aa4b2")
    try:
        p = float(pct)
    except Exception:
        return ("—", "#9aa4b2")

    if p >= 90:
        return ("CRIT", "#ff4d6d")
    if p >= 75:
        return ("WARN", "#ffb020")
    return ("OK", "#38d996")


DASH_HTML = """
<!doctype html>
<html>
<head>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Command Center</title>
  <style>
    :root{
      --bg:#070A12;
      --panel:rgba(255,255,255,.06);
      --panel2:rgba(255,255,255,.08);
      --border:rgba(255,255,255,.12);
      --text:rgba(255,255,255,.92);
      --muted:rgba(255,255,255,.62);
      --glow:rgba(80,160,255,.35);
      --shadow: 0 16px 50px rgba(0,0,0,.55);
      --radius:18px;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background:
        radial-gradient(900px 500px at 15% -10%, rgba(80,160,255,.22), transparent 60%),
        radial-gradient(700px 420px at 85% 10%, rgba(160,80,255,.18), transparent 55%),
        radial-gradient(900px 600px at 50% 120%, rgba(0,255,190,.10), transparent 55%),
        var(--bg);
      color:var(--text);
      min-height:100vh;
      padding:22px;
    }
    .wrap{max-width:980px;margin:0 auto}
    .topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}
    .title{line-height:1.05}
    .title h1{margin:0;font-size:28px;letter-spacing:.4px;font-weight:800}
    .title p{margin:8px 0 0 0;color:var(--muted);font-size:14px}
    .badge{
      background: linear-gradient(180deg, rgba(255,255,255,.10), rgba(255,255,255,.05));
      border: 1px solid var(--border);
      padding:10px 12px;border-radius:999px;box-shadow:var(--shadow);
      backdrop-filter: blur(10px);
      font-size:12px;color:var(--muted);white-space:nowrap
    }
    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;margin-top:14px}
    .card{
      grid-column:span 6;
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border:1px solid var(--border);
      border-radius:var(--radius);
      padding:14px;
      box-shadow:var(--shadow);
      backdrop-filter: blur(10px);
      position:relative;
      overflow:hidden;
    }
    .card:before{
      content:"";
      position:absolute;inset:-2px;
      background: radial-gradient(420px 120px at 10% 0%, rgba(80,160,255,.18), transparent 60%);
      pointer-events:none;
    }
    .label{color:var(--muted);font-size:12px;letter-spacing:.6px;text-transform:uppercase}
    .value{margin-top:6px;font-size:34px;font-weight:900;letter-spacing:.2px;text-shadow:0 0 22px var(--glow)}
    .sub{margin-top:6px;color:var(--muted);font-size:12px}
    .wide{grid-column:span 12}
    .chartwrap{margin-top:10px;height:220px}

    .chips{display:flex;flex-wrap:wrap;gap:10px;margin-top:10px}
    .chip{
      display:inline-flex;align-items:center;gap:8px;
      padding:10px 12px;border-radius:999px;
      border:1px solid rgba(255,255,255,.14);
      background: rgba(255,255,255,.06);
      box-shadow: 0 10px 30px rgba(0,0,0,.35);
      backdrop-filter: blur(10px);
      font-size:12px;color: rgba(255,255,255,.78);
    }
    .dot{width:10px;height:10px;border-radius:999px}

    .tablewrap{
      background: linear-gradient(180deg, var(--panel2), var(--panel));
      border:1px solid var(--border);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      backdrop-filter: blur(10px);
      overflow:hidden;
    }
    table{width:100%;border-collapse:collapse}
    th,td{padding:12px 14px;font-size:13px;border-bottom:1px solid rgba(255,255,255,.08);text-align:left}
    th{
      color: rgba(255,255,255,.70);
      letter-spacing:.6px;
      text-transform:uppercase;
      font-size:12px;
      background: rgba(255,255,255,.04);
    }
    tr:hover td{background: rgba(255,255,255,.03)}
    .footer{margin-top:14px;color: rgba(255,255,255,.45);font-size:12px}

    @media (max-width: 720px){
      .card{grid-column:span 12}
      .title h1{font-size:24px}
      .value{font-size:30px}
      .chartwrap{height:200px}
    }
  </style>
</head>

<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">
        <h1>⚡ Command Center</h1>
        <p>Live PC telemetry • remote dashboard</p>
      </div>

      <div class="badge">
        <span style="display:inline-flex;align-items:center;gap:8px;">
          <span class="dot" style="background: {{status_color}}; box-shadow: 0 0 16px {{status_color}};"></span>
          <span style="font-weight:800; letter-spacing:.6px; color: rgba(255,255,255,.85);">{{status_text}}</span>
          <span style="opacity:.7;">• command-center</span>
        </span>
      </div>
    </div>

    <div class="grid">

      <div class="card wide">
        <div class="label">System Health</div>
        <div class="sub">Quick status checks</div>
        <div class="chips">
          <div class="chip">
            <span class="dot" style="background: {{cpu_health_color}}; box-shadow: 0 0 14px {{cpu_health_color}};"></span>
            <span>CPU: <b>{{cpu_health}}</b></span>
          </div>
          <div class="chip">
            <span class="dot" style="background: {{ram_health_color}}; box-shadow: 0 0 14px {{ram_health_color}};"></span>
            <span>RAM: <b>{{ram_health}}</b></span>
          </div>
          <div class="chip">
            <span class="dot" style="background: {{disk_health_color}}; box-shadow: 0 0 14px {{disk_health_color}};"></span>
            <span>DISK: <b>{{disk_health}}</b></span>
          </div>
        </div>
      </div>

      <div class="card wide">
        <div class="label">Live Telemetry</div>
        <div class="sub">CPU, RAM, DISK (updates every 5 seconds)</div>
        <div class="chartwrap">
          <canvas id="telemetryChart"></canvas>
        </div>
      </div>

      <div class="card">
        <div class="label">CPU</div>
        <div class="value">{{cpu}}%</div>
        <div class="sub">Processor load</div>
      </div>

      <div class="card">
        <div class="label">RAM</div>
        <div class="value">{{ram}}%</div>
        <div class="sub">Memory usage</div>
      </div>

      <div class="card">
        <div class="label">DISK</div>
        <div class="value">{{disk_percent}}%</div>
        <div class="sub">{{disk_used_gb}} GB used • {{disk_total_gb}} GB total</div>
      </div>

      <div class="card">
        <div class="label">Last Update (UTC)</div>
        <div class="value" style="font-size:18px;text-shadow:none;font-weight:800;">{{ts}}</div>
        <div class="sub">Agent heartbeat</div>
      </div>

      <div class="wide">
        <div class="tablewrap">
          <table>
            <tr>
              <th>Time (UTC)</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>DISK%</th>
              <th>GPU</th>
              <th>Notes</th>
            </tr>
            {% for r in rows %}
            <tr>
              <td>{{r["ts"]}}</td>
              <td>{{r["cpu"]}}</td>
              <td>{{r["ram"]}}</td>
              <td>{{r["disk_percent"]}}</td>
              <td>{{r["gpu"]}}</td>
              <td>{{r["notes"] or ""}}</td>
            </tr>
            {% endfor %}
          </table>
        </div>
      </div>
    </div>

    <div class="footer">Tip: keep your agent running to keep the dashboard fresh.</div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    let chart;

    async function fetchStats() {
      const res = await fetch("/api/stats", { cache: "no-store" });
      return await res.json();
    }

    function labelFromTs(ts) { return ts.slice(11); }

    function buildChart(labels, cpuData, ramData, diskData) {
      const ctx = document.getElementById("telemetryChart").getContext("2d");
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "CPU %", data: cpuData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "RAM %", data: ramData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "DISK %", data: diskData, tension: 0.25, pointRadius: 0, borderWidth: 2 }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: { legend: { labels: { color: "rgba(255,255,255,0.75)" } } },
          scales: {
            x: { ticks: { color: "rgba(255,255,255,0.55)", maxTicksLimit: 8 }, grid: { color: "rgba(255,255,255,0.08)" } },
            y: { suggestedMin: 0, suggestedMax: 100, ticks: { color: "rgba(255,255,255,0.55)" }, grid: { color: "rgba(255,255,255,0.08)" } }
          }
        }
      });
    }

    async function updateChart() {
      const rows = await fetchStats();      // newest first
      const data = rows.slice().reverse();  // oldest -> newest

      const labels = data.map(r => labelFromTs(r.ts));
      const cpuData  = data.map(r => (r.cpu ?? null));
      const ramData  = data.map(r => (r.ram ?? null));
      const diskData = data.map(r => (r.disk_percent ?? null));

      if (!chart) { buildChart(labels, cpuData, ramData, diskData); return; }

      chart.data.labels = labels;
      chart.data.datasets[0].data = cpuData;
      chart.data.datasets[1].data = ramData;
      chart.data.datasets[2].data = diskData;
      chart.update();
    }

    updateChart();
    setInterval(updateChart, 5000);
  </script>
</body>
</html>
"""


@app.get("/")
def home():
    conn = db()
    rows = conn.execute(
        """
        SELECT ts,cpu,ram,gpu,notes,disk_percent,disk_used_gb,disk_total_gb
        FROM stats
        ORDER BY id DESC
        LIMIT 15
        """
    ).fetchall()
    conn.close()

    # default UI state
    status_text = "OFFLINE"
    status_color = "#ff4d6d"

    cpu_health, cpu_health_color = ("—", "#9aa4b2")
    ram_health, ram_health_color = ("—", "#9aa4b2")
    disk_health, disk_health_color = ("—", "#9aa4b2")

    if rows:
        latest = rows[0]

        # LIVE / OFFLINE
        latest_dt = datetime.strptime(latest["ts"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - latest_dt).total_seconds()
        is_live = age_seconds <= 15
        status_text = "LIVE" if is_live else "OFFLINE"
        status_color = "#38d996" if is_live else "#ff4d6d"

        # Health chips
        cpu_health, cpu_health_color = health_label_color(latest["cpu"])
        ram_health, ram_health_color = health_label_color(latest["ram"])
        disk_health, disk_health_color = health_label_color(latest["disk_percent"])

        # Format display values
        cpu_v = f'{latest["cpu"]:.1f}' if latest["cpu"] is not None else "—"
        ram_v = f'{latest["ram"]:.1f}' if latest["ram"] is not None else "—"
        gpu_v = f'{latest["gpu"]:.1f}' if latest["gpu"] is not None else "—"

        disk_p = f'{latest["disk_percent"]:.1f}' if latest["disk_percent"] is not None else "—"
        disk_used = f'{latest["disk_used_gb"]:.1f}' if latest["disk_used_gb"] is not None else "—"
        disk_total = f'{latest["disk_total_gb"]:.1f}' if latest["disk_total_gb"] is not None else "—"

        return render_template_string(
            DASH_HTML,
            cpu=cpu_v,
            ram=ram_v,
            gpu=gpu_v,
            ts=latest["ts"],
            rows=rows,
            status_text=status_text,
            status_color=status_color,
            cpu_health=cpu_health,
            cpu_health_color=cpu_health_color,
            ram_health=ram_health,
            ram_health_color=ram_health_color,
            disk_health=disk_health,
            disk_health_color=disk_health_color,
            disk_percent=disk_p,
            disk_used_gb=disk_used,
            disk_total_gb=disk_total,
        )

    # No rows yet: still render UI
    return render_template_string(
        DASH_HTML,
        cpu="—",
        ram="—",
        gpu="—",
        ts="—",
        rows=[],
        status_text=status_text,
        status_color=status_color,
        cpu_health=cpu_health,
        cpu_health_color=cpu_health_color,
        ram_health=ram_health,
        ram_health_color=ram_health_color,
        disk_health=disk_health,
        disk_health_color=disk_health_color,
        disk_percent="—",
        disk_used_gb="—",
        disk_total_gb="—",
    )


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

    disk_percent = data.get("disk_percent")
    disk_used_gb = data.get("disk_used_gb")
    disk_total_gb = data.get("disk_total_gb")

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    conn.execute(
        """
        INSERT INTO stats (ts,cpu,ram,gpu,notes,disk_percent,disk_used_gb,disk_total_gb)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (ts, cpu, ram, gpu, notes, disk_percent, disk_used_gb, disk_total_gb),
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.get("/api/stats")
def api_stats():
    conn = db()
    rows = conn.execute(
        """
        SELECT ts,cpu,ram,gpu,notes,disk_percent,disk_used_gb,disk_total_gb
        FROM stats
        ORDER BY id DESC
        LIMIT 200
        """
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/health")
def health():
    return jsonify({"status": "ok"})
