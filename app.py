from flask import Flask, jsonify, request, render_template_string
import os
import sqlite3
from datetime import datetime, timezone

app = Flask(__name__)

# ========================
# CONFIG
# ========================
API_KEY = os.environ.get("API_KEY", "dev-key-change-me")
DB_PATH = "data.db"

# LIVE if last update <= this many seconds
LIVE_WINDOW_SECONDS = 15

# Thresholds (edit anytime)
CPU_WARN = 75
CPU_CRIT = 90

RAM_WARN = 75
RAM_CRIT = 90

DISK_WARN = 80
DISK_CRIT = 90

TEMP_WARN_C = 80
TEMP_CRIT_C = 85


# ========================
# DB HELPERS
# ========================
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def ensure_columns(conn, table: str, needed_cols: dict):
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
    # Migrate / add new columns safely
    ensure_columns(
        conn,
        "stats",
        {
            "pc_name": "TEXT",
            "cpu_temp": "REAL",
            "disk_percent": "REAL",
            "disk_used_gb": "REAL",
            "disk_total_gb": "REAL",
        },
    )
    conn.commit()
    conn.close()


init_db()


# ========================
# HEALTH HELPERS
# ========================
def _to_float(x):
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def health_from_percent(p, warn, crit):
    p = _to_float(p)
    if p is None:
        return ("—", "#9aa4b2")
    if p >= crit:
        return ("CRIT", "#ff4d6d")
    if p >= warn:
        return ("WARN", "#ffb020")
    return ("OK", "#38d996")


def health_from_temp_c(t, warn, crit):
    t = _to_float(t)
    if t is None:
        return ("—", "#9aa4b2")
    if t >= crit:
        return ("CRIT", "#ff4d6d")
    if t >= warn:
        return ("WARN", "#ffb020")
    return ("OK", "#38d996")


def fmt1(x):
    x = _to_float(x)
    return "—" if x is None else f"{x:.1f}"


def fmt2(x):
    x = _to_float(x)
    return "—" if x is None else f"{x:.2f}"


def parse_ts_utc(ts_str):
    # stored as "YYYY-MM-DD HH:MM:SS" UTC
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


# ========================
# UI
# ========================
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
    .wrap{max-width:1020px;margin:0 auto}
    .topbar{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;margin-bottom:14px}
    .title{line-height:1.05}
    .title h1{margin:0;font-size:28px;letter-spacing:.4px;font-weight:800}
    .title p{margin:8px 0 0 0;color:var(--muted);font-size:14px}

    .rightbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;justify-content:flex-end}
    .badge{
      background: linear-gradient(180deg, rgba(255,255,255,.10), rgba(255,255,255,.05));
      border: 1px solid var(--border);
      padding:10px 12px;border-radius:999px;box-shadow:var(--shadow);
      backdrop-filter: blur(10px);
      font-size:12px;color:var(--muted);white-space:nowrap
    }
    .dot{width:10px;height:10px;border-radius:999px;display:inline-block}

    .select{
      appearance:none;
      background: rgba(255,255,255,.06);
      border: 1px solid rgba(255,255,255,.16);
      color: rgba(255,255,255,.85);
      padding:10px 36px 10px 12px;
      border-radius:999px;
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      font-weight:700;
      letter-spacing:.3px;
      cursor:pointer;
    }
    .selectwrap{position:relative}
    .selectwrap:after{
      content:"▾";
      position:absolute;
      right:12px;
      top:50%;
      transform:translateY(-50%);
      color: rgba(255,255,255,.55);
      pointer-events:none;
      font-size:12px;
    }

    .grid{display:grid;grid-template-columns:repeat(12,1fr);gap:12px;margin-top:14px}

    .banner{
      grid-column:span 12;
      border-radius: var(--radius);
      border: 1px solid rgba(255,255,255,.14);
      background: linear-gradient(180deg, rgba(255,77,109,.18), rgba(255,77,109,.08));
      box-shadow: var(--shadow);
      backdrop-filter: blur(10px);
      padding:12px 14px;
      display:flex;align-items:center;justify-content:space-between;gap:10px;
    }
    .banner b{letter-spacing:.5px}
    .banner small{color: rgba(255,255,255,.7)}

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
    .chartwrap{margin-top:10px;height:240px}

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
      .chartwrap{height:220px}
      .rightbar{justify-content:flex-start}
    }
  </style>
</head>

<body>
  <div class="wrap">
    <div class="topbar">
      <div class="title">
        <h1>⚡ Command Center</h1>
        <p>Multi-PC telemetry • live health</p>
      </div>

      <div class="rightbar">
        <div class="selectwrap">
          <select class="select" id="pcSelect">
            {% for pc in pc_list %}
              <option value="{{pc}}" {% if pc == selected_pc %}selected{% endif %}>{{pc}}</option>
            {% endfor %}
          </select>
        </div>

        <div class="badge" id="liveBadge">
          <span style="display:inline-flex;align-items:center;gap:8px;">
            <span class="dot" id="liveDot" style="background: {{status_color}}; box-shadow: 0 0 16px {{status_color}};"></span>
            <span style="font-weight:800; letter-spacing:.6px; color: rgba(255,255,255,.85);" id="liveText">{{status_text}}</span>
            <span style="opacity:.7;">• {{selected_pc}}</span>
          </span>
        </div>
      </div>
    </div>

    {% if banner_show %}
    <div class="grid">
      <div class="banner">
        <div>
          <b>⚠ {{banner_title}}</b>
          <small>• {{banner_subtitle}}</small>
        </div>
        <small>Threshold alerts enabled</small>
      </div>
    </div>
    {% endif %}

    <div class="grid">

      <div class="card wide">
        <div class="label">System Health</div>
        <div class="sub">OK / WARN / CRIT • thresholds</div>
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
          <div class="chip">
            <span class="dot" style="background: {{temp_health_color}}; box-shadow: 0 0 14px {{temp_health_color}};"></span>
            <span>TEMP: <b>{{temp_health}}</b></span>
          </div>
        </div>
      </div>

      <div class="card wide">
        <div class="label">Live Telemetry</div>
        <div class="sub">CPU, RAM, DISK, TEMP (updates every 5 seconds)</div>
        <div class="chartwrap">
          <canvas id="telemetryChart"></canvas>
        </div>
      </div>

      <div class="card">
        <div class="label">CPU</div>
        <div class="value" id="cpuVal">{{cpu}}%</div>
        <div class="sub">Processor load</div>
      </div>

      <div class="card">
        <div class="label">RAM</div>
        <div class="value" id="ramVal">{{ram}}%</div>
        <div class="sub">Memory usage</div>
      </div>

      <div class="card">
        <div class="label">DISK</div>
        <div class="value" id="diskVal">{{disk_percent}}%</div>
        <div class="sub" id="diskSub">{{disk_used_gb}} GB used • {{disk_total_gb}} GB total</div>
      </div>

      <div class="card">
        <div class="label">CPU TEMP</div>
        <div class="value" id="tempVal">{{cpu_temp}}°C</div>
        <div class="sub">May show — if not available</div>
      </div>

      <div class="card wide">
        <div class="tablewrap">
          <table>
            <tr>
              <th>Time (UTC)</th>
              <th>CPU</th>
              <th>RAM</th>
              <th>DISK%</th>
              <th>TEMP°C</th>
              <th>Notes</th>
            </tr>
            {% for r in rows %}
            <tr>
              <td>{{r["ts"]}}</td>
              <td>{{r["cpu"]}}</td>
              <td>{{r["ram"]}}</td>
              <td>{{r["disk_percent"]}}</td>
              <td>{{r["cpu_temp"]}}</td>
              <td>{{r["notes"] or ""}}</td>
            </tr>
            {% endfor %}
          </table>
        </div>
      </div>

    </div>

    <div class="footer">Tip: run agents on multiple PCs and switch using the dropdown.</div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    const pcSelect = document.getElementById("pcSelect");

    let chart;

    function labelFromTs(ts) { return ts.slice(11); }

    async function fetchStats(pc) {
      const res = await fetch(`/api/stats?pc=${encodeURIComponent(pc)}`, { cache: "no-store" });
      return await res.json();
    }

    async function fetchSummary(pc) {
      const res = await fetch(`/api/summary?pc=${encodeURIComponent(pc)}`, { cache: "no-store" });
      return await res.json();
    }

    function buildChart(labels, cpuData, ramData, diskData, tempData) {
      const ctx = document.getElementById("telemetryChart").getContext("2d");
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "CPU %",  data: cpuData,  tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "RAM %",  data: ramData,  tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "DISK %", data: diskData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "TEMP °C", data: tempData, tension: 0.25, pointRadius: 0, borderWidth: 2 }
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

    async function updateChart(pc) {
      const rows = await fetchStats(pc);       // newest first
      const data = rows.slice().reverse();     // oldest -> newest

      const labels   = data.map(r => labelFromTs(r.ts));
      const cpuData  = data.map(r => (r.cpu ?? null));
      const ramData  = data.map(r => (r.ram ?? null));
      const diskData = data.map(r => (r.disk_percent ?? null));
      const tempData = data.map(r => (r.cpu_temp ?? null));

      if (!chart) { buildChart(labels, cpuData, ramData, diskData, tempData); return; }

      chart.data.labels = labels;
      chart.data.datasets[0].data = cpuData;
      chart.data.datasets[1].data = ramData;
      chart.data.datasets[2].data = diskData;
      chart.data.datasets[3].data = tempData;
      chart.update();
    }

    async function updateCards(pc) {
      const s = await fetchSummary(pc);

      // LIVE badge
      const dot = document.getElementById("liveDot");
      const text = document.getElementById("liveText");
      dot.style.background = s.status_color;
      dot.style.boxShadow = `0 0 16px ${s.status_color}`;
      text.textContent = s.status_text;

      // Cards
      document.getElementById("cpuVal").textContent = (s.cpu_display ?? "—") + "%";
      document.getElementById("ramVal").textContent = (s.ram_display ?? "—") + "%";
      document.getElementById("diskVal").textContent = (s.disk_percent_display ?? "—") + "%";
      document.getElementById("diskSub").textContent = `${s.disk_used_gb_display ?? "—"} GB used • ${s.disk_total_gb_display ?? "—"} GB total`;
      document.getElementById("tempVal").textContent = (s.cpu_temp_display ?? "—") + "°C";
    }

    function onPCChange() {
      const pc = pcSelect.value;
      // Reload page so table + chips/banners match the selected PC
      window.location = `/?pc=${encodeURIComponent(pc)}`;
    }

    pcSelect.addEventListener("change", onPCChange);

    // Live updates for chart + top cards (selected PC)
    const selectedPC = pcSelect.value;
    updateChart(selectedPC);
    updateCards(selectedPC);
    setInterval(() => updateChart(selectedPC), 5000);
    setInterval(() => updateCards(selectedPC), 5000);
  </script>
</body>
</html>
"""


# ========================
# ROUTES
# ========================
@app.get("/")
def home():
    pc_param = request.args.get("pc", "").strip()

    conn = db()

    # list PCs
    pcs = conn.execute(
        """
        SELECT DISTINCT COALESCE(pc_name, notes, 'unknown') AS pc
        FROM stats
        WHERE COALESCE(pc_name, notes, 'unknown') IS NOT NULL
        ORDER BY pc ASC
        """
    ).fetchall()
    pc_list = [r["pc"] for r in pcs] if pcs else ["unknown"]

    selected_pc = pc_param if (pc_param and pc_param in pc_list) else pc_list[0]

    rows = conn.execute(
        """
        SELECT ts,cpu,ram,cpu_temp,disk_percent,disk_used_gb,disk_total_gb,notes
        FROM stats
        WHERE COALESCE(pc_name, notes, 'unknown') = ?
        ORDER BY id DESC
        LIMIT 15
        """,
        (selected_pc,),
    ).fetchall()
    conn.close()

    # Defaults
    status_text = "OFFLINE"
    status_color = "#ff4d6d"

    cpu_health, cpu_health_color = ("—", "#9aa4b2")
    ram_health, ram_health_color = ("—", "#9aa4b2")
    disk_health, disk_health_color = ("—", "#9aa4b2")
    temp_health, temp_health_color = ("—", "#9aa4b2")

    banner_show = False
    banner_title = ""
    banner_subtitle = ""

    # Display values
    cpu_v = "—"
    ram_v = "—"
    disk_p = "—"
    disk_used = "—"
    disk_total = "—"
    temp_v = "—"
    ts_v = "—"

    if rows:
        latest = rows[0]
        ts_v = latest["ts"]

        # Live/offline
        try:
            latest_dt = parse_ts_utc(latest["ts"])
            age_seconds = (datetime.now(timezone.utc) - latest_dt).total_seconds()
            is_live = age_seconds <= LIVE_WINDOW_SECONDS
        except Exception:
            is_live = False

        status_text = "LIVE" if is_live else "OFFLINE"
        status_color = "#38d996" if is_live else "#ff4d6d"

        # Health chips
        cpu_health, cpu_health_color = health_from_percent(latest["cpu"], CPU_WARN, CPU_CRIT)
        ram_health, ram_health_color = health_from_percent(latest["ram"], RAM_WARN, RAM_CRIT)
        disk_health, disk_health_color = health_from_percent(latest["disk_percent"], DISK_WARN, DISK_CRIT)
        temp_health, temp_health_color = health_from_temp_c(latest["cpu_temp"], TEMP_WARN_C, TEMP_CRIT_C)

        # Banner if anything critical
        crits = []
        if cpu_health == "CRIT":
            crits.append("CPU")
        if ram_health == "CRIT":
            crits.append("RAM")
        if disk_health == "CRIT":
            crits.append("DISK")
        if temp_health == "CRIT":
            crits.append("TEMP")

        if crits:
            banner_show = True
            banner_title = "CRITICAL"
            banner_subtitle = " • ".join(crits) + " above critical threshold"

        # Format display
        cpu_v = fmt1(latest["cpu"])
        ram_v = fmt1(latest["ram"])
        disk_p = fmt1(latest["disk_percent"])
        disk_used = fmt2(latest["disk_used_gb"])
        disk_total = fmt2(latest["disk_total_gb"])
        temp_v = fmt1(latest["cpu_temp"])

    return render_template_string(
        DASH_HTML,
        pc_list=pc_list,
        selected_pc=selected_pc,
        rows=rows,

        status_text=status_text,
        status_color=status_color,

        cpu_health=cpu_health,
        cpu_health_color=cpu_health_color,
        ram_health=ram_health,
        ram_health_color=ram_health_color,
        disk_health=disk_health,
        disk_health_color=disk_health_color,
        temp_health=temp_health,
        temp_health_color=temp_health_color,

        banner_show=banner_show,
        banner_title=banner_title,
        banner_subtitle=banner_subtitle,

        cpu=cpu_v,
        ram=ram_v,
        disk_percent=disk_p,
        disk_used_gb=disk_used,
        disk_total_gb=disk_total,
        cpu_temp=temp_v,
        ts=ts_v,
    )


@app.route("/api/ingest", methods=["POST"], strict_slashes=False)
def ingest():
    sent_key = request.headers.get("X-API-Key", "")
    if sent_key != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}

    cpu = data.get("cpu")
    ram = data.get("ram")
    cpu_temp = data.get("cpu_temp")
    disk_percent = data.get("disk_percent")
    disk_used_gb = data.get("disk_used_gb")
    disk_total_gb = data.get("disk_total_gb")

    pc_name = data.get("pc_name") or data.get("notes")  # allow older agents
    notes = data.get("notes") or pc_name

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    conn = db()
    conn.execute(
        """
        INSERT INTO stats (ts,cpu,ram,cpu_temp,notes,pc_name,disk_percent,disk_used_gb,disk_total_gb)
        VALUES (?,?,?,?,?,?,?,?,?)
        """,
        (ts, cpu, ram, cpu_temp, notes, pc_name, disk_percent, disk_used_gb, disk_total_gb),
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True})


@app.get("/api/stats")
def api_stats():
    pc = request.args.get("pc", "").strip()
    if not pc:
        return jsonify([])

    conn = db()
    rows = conn.execute(
        """
        SELECT ts,cpu,ram,cpu_temp,disk_percent,disk_used_gb,disk_total_gb,notes
        FROM stats
        WHERE COALESCE(pc_name, notes, 'unknown') = ?
        ORDER BY id DESC
        LIMIT 200
        """,
        (pc,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.get("/api/summary")
def api_summary():
    pc = request.args.get("pc", "").strip()
    if not pc:
        return jsonify({"error": "missing pc"}), 400

    conn = db()
    latest = conn.execute(
        """
        SELECT ts,cpu,ram,cpu_temp,disk_percent,disk_used_gb,disk_total_gb
        FROM stats
        WHERE COALESCE(pc_name, notes, 'unknown') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (pc,),
    ).fetchone()
    conn.close()

    if not latest:
        return jsonify(
            {
                "status_text": "OFFLINE",
                "status_color": "#ff4d6d",
                "cpu_display": "—",
                "ram_display": "—",
                "disk_percent_display": "—",
                "disk_used_gb_display": "—",
                "disk_total_gb_display": "—",
                "cpu_temp_display": "—",
            }
        )

    # live/offline
    try:
        age_seconds = (datetime.now(timezone.utc) - parse_ts_utc(latest["ts"])).total_seconds()
        is_live = age_seconds <= LIVE_WINDOW_SECONDS
    except Exception:
        is_live = False

    status_text = "LIVE" if is_live else "OFFLINE"
    status_color = "#38d996" if is_live else "#ff4d6d"

    return jsonify(
        {
            "status_text": status_text,
            "status_color": status_color,
            "cpu_display": fmt1(latest["cpu"]),
            "ram_display": fmt1(latest["ram"]),
            "disk_percent_display": fmt1(latest["disk_percent"]),
            "disk_used_gb_display": fmt2(latest["disk_used_gb"]),
            "disk_total_gb_display": fmt2(latest["disk_total_gb"]),
            "cpu_temp_display": fmt1(latest["cpu_temp"]),
        }
    )


@app.get("/health")
def health():
    return jsonify({"status": "ok"})
