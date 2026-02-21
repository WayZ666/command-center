from flask import Flask, jsonify, request, render_template_string
import os
import sqlite3
from datetime import datetime, timezone

app = Flask(__name__)

# ===== CONFIG =====
API_KEY = os.environ.get("API_KEY", "dev-key-change-me")
DB_PATH = "data.db"

# ===== DB HELPERS =====
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

# ===== UI (DARK / FUTURISTIC + LIVE CHART) =====
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
    .chartwrap{
      margin-top:10px;
      height: 220px;
    }

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
      .chartwrap{height: 200px;}
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
    <span style="width:10px;height:10px;border-radius:999px;background: {{status_color}}; box-shadow: 0 0 16px {{status_color}};"></span>
    <span style="font-weight:700; letter-spacing:.6px;">{{status_text}}</span>
    <span style="opacity:.7;">• command-center</span>
  </span>
</div>
    </div>

    <div class="grid">

      <!-- LIVE CHART -->
      <div class="card wide">
        <div class="label">Live Telemetry</div>
        <div class="sub">CPU & RAM history (updates every 5 seconds)</div>
        <div class="chartwrap">
          <canvas id="telemetryChart"></canvas>
        </div>
      </div>

      <!-- METRIC CARDS -->
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
        <div class="label">GPU</div>
        <div class="value">{{gpu}}</div>
        <div class="sub">Next: real GPU %</div>
      </div>

      <div class="card">
        <div class="label">Last Update (UTC)</div>
        <div class="value" style="font-size:18px;text-shadow:none;font-weight:800;">{{ts}}</div>
        <div class="sub">Agent heartbeat</div>
      </div>

      <!-- RECENT TABLE -->
      <div class="wide">
        <div class="tablewrap">
          <table>
            <tr>
              <th>Time (UTC)</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>GPU</th>
              <th>Notes</th>
            </tr>
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
        </div>
      </div>
    </div>

    <div class="footer">Tip: keep your agent running to keep the dashboard fresh.</div>
  </div>

  <!-- Chart.js + Live update -->
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    let chart;

    async function fetchStats() {
      const res = await fetch("/api/stats", { cache: "no-store" });
      return await res.json();
    }

    function labelFromTs(ts) {
      // "YYYY-MM-DD HH:MM:SS" -> "HH:MM:SS"
      return ts.slice(11);
    }

    function buildChart(labels, cpuData, ramData) {
      const ctx = document.getElementById("telemetryChart").getContext("2d");
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "CPU %", data: cpuData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "RAM %", data: ramData, tension: 0.25, pointRadius: 0, borderWidth: 2 }
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
      const cpuData = data.map(r => (r.cpu ?? null));
      const ramData = data.map(r => (r.ram ?? null));

      if (!chart) {
        buildChart(labels, cpuData, ramData);
        return;
      }

      chart.data.labels = labels;
      chart.data.datasets[0].data = cpuData;
      chart.data.datasets[1].data = ramData;
      chart.update();
    }

    updateChart();
    setInterval(updateChart, 5000);
  </script>
</body>
</html>
"""

# ===== ROUTES =====
@app.get("/")
def home():
    conn = db()
    rows = conn.execute(
        "SELECT ts,cpu,ram,gpu,notes FROM stats ORDER BY id DESC LIMIT 15"
    ).fetchall()
    conn.close()

  if rows:
    latest = rows[0]
    gpu_val = f'{latest["gpu"]:.1f}' if latest["gpu"] is not None else "—"

    latest_dt = datetime.strptime(
        latest["ts"], "%Y-%m-%d %H:%M:%S"
    ).replace(tzinfo=timezone.utc)

    age_seconds = (datetime.now(timezone.utc) - latest_dt).total_seconds()

    is_live = age_seconds <= 15
    status_text = "LIVE" if is_live else "OFFLINE"
    status_color = "#38d996" if is_live else "#ff4d6d"  # green / red

    return render_template_string(
        DASH_HTML,
        cpu=f'{latest["cpu"]:.1f}' if latest["cpu"] is not None else "—",
        ram=f'{latest["ram"]:.1f}' if latest["ram"] is not None else "—",
        gpu=gpu_val,
        ts=latest["ts"],
        rows=rows,
        status_text=status_text,
        status_color=status_color,
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

@app.get("/api/stats")
def api_stats():
    conn = db()
    rows = conn.execute(
        "SELECT ts,cpu,ram,gpu,notes FROM stats ORDER BY id DESC LIMIT 200"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])

@app.get("/health")
def health():
    return jsonify({"status": "ok"})



