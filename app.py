from flask import Flask, jsonify, request, render_template_string
import json
import os
import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

app = Flask(__name__)

# ========================
# CONFIG
# ========================
API_KEY = os.environ.get("K9fT2xQ7mZ4pL8sV1rW6bH3yN5uD0cAq")
if not API_KEY:
    raise RuntimeError("API_KEY environment variable is required")

DB_PATH = os.environ.get("COMMAND_CENTER_DB_PATH", "data.db")
LIVE_WINDOW_SECONDS = int(os.environ.get("LIVE_WINDOW_SECONDS", "15"))
MAX_STATS_POINTS = int(os.environ.get("MAX_STATS_POINTS", "1000"))

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
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    conn.execute("PRAGMA busy_timeout=5000;")
    return conn


def ensure_columns(conn: sqlite3.Connection, table: str, needed_cols: dict[str, str]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    for col, sql_type in needed_cols.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {sql_type}")


def init_db() -> None:
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

    ensure_columns(
        conn,
        "stats",
        {
            "pc_name": "TEXT",
            "cpu_temp": "REAL",
            "disk_percent": "REAL",
            "disk_used_gb": "REAL",
            "disk_total_gb": "REAL",
            "drives_json": "TEXT",
            "client_ts": "TEXT",
        },
    )

    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_pc_id ON stats(pc_name, id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_notes_id ON stats(notes, id);")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_stats_ts_id ON stats(ts, id);")
    conn.commit()
    conn.close()


init_db()


# ========================
# HELPERS
# ========================
def utc_now_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _to_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except Exception:
        return None


def clamp_num(value: Any, low: float, high: float) -> Optional[float]:
    v = _to_float(value)
    if v is None:
        return None
    return max(low, min(high, v))


def clean_text(value: Any, max_len: int = 200) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if len(text) > max_len:
        text = text[:max_len]
    return text


def validate_ts(ts_value: Any) -> Optional[str]:
    if ts_value is None:
        return None
    ts_str = str(ts_value).strip()
    try:
        dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def normalize_drives(drives: Any) -> list[dict[str, Any]]:
    if not isinstance(drives, list):
        return []

    cleaned: list[dict[str, Any]] = []
    for item in drives[:32]:
        if not isinstance(item, dict):
            continue

        mount = clean_text(item.get("mount"), max_len=32)
        percent = clamp_num(item.get("percent"), 0, 100)
        used_gb = _to_float(item.get("used_gb"))
        total_gb = _to_float(item.get("total_gb"))
        free_gb = _to_float(item.get("free_gb"))

        cleaned.append(
            {
                "mount": mount or "?",
                "percent": percent,
                "used_gb": round(used_gb, 2) if used_gb is not None else None,
                "total_gb": round(total_gb, 2) if total_gb is not None else None,
                "free_gb": round(free_gb, 2) if free_gb is not None else None,
            }
        )
    return cleaned


def parse_ts_utc(ts_str: str) -> datetime:
    return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def safe_load_drives(drives_json: Optional[str]) -> list[dict[str, Any]]:
    if not drives_json:
        return []
    try:
        data = json.loads(drives_json)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fmt1(x: Any) -> str:
    v = _to_float(x)
    return "—" if v is None else f"{v:.1f}"


def fmt2(x: Any) -> str:
    v = _to_float(x)
    return "—" if v is None else f"{v:.2f}"


def health_from_percent(p: Any, warn: float, crit: float) -> tuple[str, str]:
    v = _to_float(p)
    if v is None:
        return ("—", "#9aa4b2")
    if v >= crit:
        return ("CRIT", "#ff4d6d")
    if v >= warn:
        return ("WARN", "#ffb020")
    return ("OK", "#38d996")


def health_from_temp_c(t: Any, warn: float, crit: float) -> tuple[str, str]:
    v = _to_float(t)
    if v is None:
        return ("—", "#9aa4b2")
    if v >= crit:
        return ("CRIT", "#ff4d6d")
    if v >= warn:
        return ("WARN", "#ffb020")
    return ("OK", "#38d996")


def get_range_delta(range_key: str) -> timedelta:
    mapping = {
        "15m": timedelta(minutes=15),
        "1h": timedelta(hours=1),
        "6h": timedelta(hours=6),
        "24h": timedelta(hours=24),
    }
    return mapping.get(range_key, timedelta(hours=1))


def get_selected_pc(conn: sqlite3.Connection, requested_pc: str) -> tuple[list[str], str]:
    pcs = conn.execute(
        """
        SELECT DISTINCT COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') AS pc
        FROM stats
        ORDER BY pc ASC
        """
    ).fetchall()

    pc_list = [r["pc"] for r in pcs] if pcs else ["unknown"]
    selected_pc = requested_pc if requested_pc and requested_pc in pc_list else pc_list[0]
    return pc_list, selected_pc


# ========================
# HTML
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
      --shadow:0 16px 50px rgba(0,0,0,.55);
      --radius:18px;
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Arial;
      background:
        radial-gradient(900px 500px at 15% -10%, rgba(80,160,255,.22), transparent 60%),
        radial-gradient(900px 600px at 110% 10%, rgba(56,217,150,.08), transparent 55%),
        linear-gradient(180deg, #070A12 0%, #0B1020 100%);
      color:var(--text);
      min-height:100vh;
    }
    .wrap{max-width:1320px;margin:0 auto;padding:22px}
    .topbar{
      display:flex;gap:14px;justify-content:space-between;align-items:center;flex-wrap:wrap;
      margin-bottom:18px
    }
    .title h1{margin:0;font-size:32px;letter-spacing:.2px}
    .title p{margin:6px 0 0;color:var(--muted)}
    .rightbar{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
    .badge,.select,.btn{
      border:1px solid var(--border);
      background:var(--panel);
      color:var(--text);
      border-radius:14px;
      box-shadow:var(--shadow);
    }
    .badge{padding:10px 14px}
    .select,.btn{padding:10px 12px}
    .btn{cursor:pointer}
    .btn:hover{background:var(--panel2)}
    .selectwrap{position:relative}
    .select{appearance:none;outline:none}
    .grid{
      display:grid;
      grid-template-columns:repeat(12,1fr);
      gap:14px;
    }
    .card{
      grid-column:span 3;
      background:linear-gradient(180deg,var(--panel2),var(--panel));
      border:1px solid var(--border);
      border-radius:var(--radius);
      box-shadow:var(--shadow);
      padding:16px;
      min-height:132px;
    }
    .wide{grid-column:span 8}
    .tall{grid-column:span 4}
    .chartwrap{height:340px}
    .label{font-size:12px;letter-spacing:.8px;text-transform:uppercase;color:var(--muted)}
    .value{font-size:38px;font-weight:900;margin-top:8px}
    .sub{margin-top:10px;color:var(--muted);font-size:14px}
    .health{margin-top:10px;font-weight:800}
    .dot{
      width:10px;height:10px;border-radius:999px;display:inline-block;vertical-align:middle
    }
    table{width:100%;border-collapse:collapse;margin-top:10px}
    th,td{
      padding:10px 8px;
      border-bottom:1px solid rgba(255,255,255,.08);
      text-align:left
    }
    th{
      color:rgba(255,255,255,.70);
      letter-spacing:.6px;
      text-transform:uppercase;
      font-size:12px;
      background:rgba(255,255,255,.04);
    }
    tr:hover td{background:rgba(255,255,255,.03)}
    .footer{margin-top:14px;color:rgba(255,255,255,.45);font-size:12px}
    .drives{display:flex;flex-direction:column;gap:10px;margin-top:8px}
    .drive{
      padding:10px 12px;border:1px solid rgba(255,255,255,.08);
      border-radius:12px;background:rgba(255,255,255,.03)
    }
    .drive-head{
      display:flex;justify-content:space-between;gap:10px;align-items:center;margin-bottom:8px
    }
    .meter{
      width:100%;height:10px;border-radius:999px;background:rgba(255,255,255,.08);overflow:hidden
    }
    .bar{
      height:100%;width:0%;
      background:rgba(56,217,150,.9);
      transition:width .25s ease;
    }
    @media (max-width: 1100px){
      .card{grid-column:span 6}
      .wide,.tall{grid-column:span 12}
    }
    @media (max-width: 720px){
      .card{grid-column:span 12}
      .title h1{font-size:24px}
      .value{font-size:30px}
      .chartwrap{height:240px}
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
              <option value="{{ pc }}" {% if pc == selected_pc %}selected{% endif %}>{{ pc }}</option>
            {% endfor %}
          </select>
        </div>

        <div class="selectwrap">
          <select class="select" id="rangeSelect">
            {% for r in range_options %}
              <option value="{{ r[0] }}" {% if r[0] == selected_range %}selected{% endif %}>{{ r[1] }}</option>
            {% endfor %}
          </select>
        </div>

        <button class="btn" id="pauseBtn">⏸ Pause</button>

        <div class="badge">
          <span style="display:inline-flex;align-items:center;gap:8px;">
            <span class="dot" id="liveDot" style="background: {{ status_color }}; box-shadow: 0 0 16px {{ status_color }};"></span>
            <span style="font-weight:800; letter-spacing:.6px; color: rgba(255,255,255,.85);" id="liveText">{{ status_text }}</span>
            <span style="opacity:.7;">• {{ selected_pc }}</span>
          </span>
        </div>

        <div class="badge">
          <span style="font-weight:900; color: rgba(255,255,255,.85);">UI FPS:</span>
          <span id="fpsCounter" style="margin-left:6px;">—</span>
        </div>

        <div class="badge">
          <span style="font-weight:900; color: rgba(255,255,255,.85);">Last update:</span>
          <span id="ageCounter" style="margin-left:6px;">—</span>
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <div class="label">CPU</div>
        <div class="value" id="cpuValue">{{ cpu_value }}</div>
        <div class="sub">Current load</div>
        <div class="health" id="cpuHealth" style="color: {{ cpu_health_color }};">{{ cpu_health }}</div>
      </div>

      <div class="card">
        <div class="label">RAM</div>
        <div class="value" id="ramValue">{{ ram_value }}</div>
        <div class="sub">Memory usage</div>
        <div class="health" id="ramHealth" style="color: {{ ram_health_color }};">{{ ram_health }}</div>
      </div>

      <div class="card">
        <div class="label">Disk</div>
        <div class="value" id="diskValue">{{ disk_value }}</div>
        <div class="sub">Worst drive usage</div>
        <div class="health" id="diskHealth" style="color: {{ disk_health_color }};">{{ disk_health }}</div>
      </div>

      <div class="card">
        <div class="label">CPU Temp</div>
        <div class="value" id="tempValue">{{ temp_value }}</div>
        <div class="sub">Sensor reading</div>
        <div class="health" id="tempHealth" style="color: {{ temp_health_color }};">{{ temp_health }}</div>
      </div>

      <div class="card wide">
        <div class="label">Telemetry History</div>
        <div class="chartwrap">
          <canvas id="telemetryChart"></canvas>
        </div>
      </div>

      <div class="card tall">
        <div class="label">Drive Details</div>
        <div class="drives" id="drivesWrap">
          {% if drives %}
            {% for d in drives %}
              <div class="drive">
                <div class="drive-head">
                  <strong>{{ d.mount }}</strong>
                  <span>{{ "%.1f"|format(d.percent or 0) }}%</span>
                </div>
                <div class="meter"><div class="bar" data-p="{{ d.percent or 0 }}"></div></div>
                <div class="sub">{{ d.used_gb }} / {{ d.total_gb }} GB</div>
              </div>
            {% endfor %}
          {% else %}
            <div class="sub">No drive data yet.</div>
          {% endif %}
        </div>
      </div>

      <div class="card" style="grid-column: span 12;">
        <div class="label">Recent Readings</div>
        <div style="overflow:auto;">
          <table>
            <thead>
              <tr>
                <th>Server Time</th>
                <th>Client Time</th>
                <th>CPU</th>
                <th>RAM</th>
                <th>Disk</th>
                <th>Temp</th>
              </tr>
            </thead>
            <tbody id="recentTable">
              {% for row in recent_rows %}
              <tr>
                <td>{{ row["ts"] }}</td>
                <td>{{ row["client_ts"] or "—" }}</td>
                <td>{{ "%.1f"|format(row["cpu"] or 0) if row["cpu"] is not none else "—" }}%</td>
                <td>{{ "%.1f"|format(row["ram"] or 0) if row["ram"] is not none else "—" }}%</td>
                <td>{{ "%.1f"|format(row["disk_percent"] or 0) if row["disk_percent"] is not none else "—" }}%</td>
                <td>{{ "%.1f"|format(row["cpu_temp"] or 0) if row["cpu_temp"] is not none else "—" }}°C</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <div class="footer">Tip: run agents on multiple PCs and switch using the dropdown. Range + Pause controls are per-page.</div>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <script>
    const pcSelect = document.getElementById("pcSelect");
    const rangeSelect = document.getElementById("rangeSelect");
    const pauseBtn = document.getElementById("pauseBtn");
    const fpsCounter = document.getElementById("fpsCounter");
    const ageCounter = document.getElementById("ageCounter");

    const cpuValue = document.getElementById("cpuValue");
    const ramValue = document.getElementById("ramValue");
    const diskValue = document.getElementById("diskValue");
    const tempValue = document.getElementById("tempValue");

    const cpuHealth = document.getElementById("cpuHealth");
    const ramHealth = document.getElementById("ramHealth");
    const diskHealth = document.getElementById("diskHealth");
    const tempHealth = document.getElementById("tempHealth");

    const liveDot = document.getElementById("liveDot");
    const liveText = document.getElementById("liveText");
    const drivesWrap = document.getElementById("drivesWrap");
    const recentTable = document.getElementById("recentTable");

    let chart;
    let isPaused = false;
    let lastServerTs = null;
    let tickTimer = null;
    let ticking = false;

    function labelFromTs(ts) {
      return ts ? ts.slice(11) : "";
    }

    function applyBars() {
      document.querySelectorAll(".bar").forEach(b => {
        const p = Math.max(0, Math.min(100, parseFloat(b.dataset.p || "0")));
        b.style.width = p + "%";
        if (p >= 90) b.style.background = "rgba(255,77,109,.9)";
        else if (p >= 80) b.style.background = "rgba(255,176,32,.9)";
        else b.style.background = "rgba(56,217,150,.9)";
      });
    }

    async function fetchStats(pc, rangeKey) {
      const res = await fetch(`/api/stats?pc=${encodeURIComponent(pc)}&range=${encodeURIComponent(rangeKey)}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`stats ${res.status}`);
      return await res.json();
    }

    async function fetchSummary(pc) {
      const res = await fetch(`/api/summary?pc=${encodeURIComponent(pc)}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`summary ${res.status}`);
      return await res.json();
    }

    function buildChart(labels, cpuData, ramData, diskData, tempData) {
      const ctx = document.getElementById("telemetryChart").getContext("2d");
      chart = new Chart(ctx, {
        type: "line",
        data: {
          labels,
          datasets: [
            { label: "CPU %", data: cpuData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "RAM %", data: ramData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "DISK % (worst)", data: diskData, tension: 0.25, pointRadius: 0, borderWidth: 2 },
            { label: "TEMP °C", data: tempData, tension: 0.25, pointRadius: 0, borderWidth: 2 }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          plugins: {
            legend: { labels: { color: "rgba(255,255,255,.75)" } }
          },
          scales: {
            x: {
              ticks: { color: "rgba(255,255,255,.55)", maxTicksLimit: 10 },
              grid: { color: "rgba(255,255,255,.08)" }
            },
            y: {
              beginAtZero: true,
              ticks: { color: "rgba(255,255,255,.55)" },
              grid: { color: "rgba(255,255,255,.08)" }
            }
          }
        }
      });
    }

    async function updateChart(pc, rangeKey) {
      const data = await fetchStats(pc, rangeKey);
      const labels = data.points.map(p => labelFromTs(p.ts));
      const cpuData = data.points.map(p => p.cpu);
      const ramData = data.points.map(p => p.ram);
      const diskData = data.points.map(p => p.disk_percent);
      const tempData = data.points.map(p => p.cpu_temp);

      if (!chart) {
        buildChart(labels, cpuData, ramData, diskData, tempData);
      } else {
        chart.data.labels = labels;
        chart.data.datasets[0].data = cpuData;
        chart.data.datasets[1].data = ramData;
        chart.data.datasets[2].data = diskData;
        chart.data.datasets[3].data = tempData;
        chart.update("none");
      }
    }

    function setHealth(el, text, color) {
      el.textContent = text || "—";
      el.style.color = color || "#9aa4b2";
    }

    function renderDrives(drives) {
      if (!Array.isArray(drives) || drives.length === 0) {
        drivesWrap.innerHTML = `<div class="sub">No drive data yet.</div>`;
        return;
      }

      drivesWrap.innerHTML = drives.map(d => {
        const pct = typeof d.percent === "number" ? d.percent.toFixed(1) : "0.0";
        const used = typeof d.used_gb === "number" ? d.used_gb.toFixed(2) : "—";
        const total = typeof d.total_gb === "number" ? d.total_gb.toFixed(2) : "—";
        const mount = d.mount || "?";
        return `
          <div class="drive">
            <div class="drive-head">
              <strong>${mount}</strong>
              <span>${pct}%</span>
            </div>
            <div class="meter"><div class="bar" data-p="${pct}"></div></div>
            <div class="sub">${used} / ${total} GB</div>
          </div>
        `;
      }).join("");
      applyBars();
    }

    function renderRecent(rows) {
      if (!Array.isArray(rows) || rows.length === 0) {
        recentTable.innerHTML = `<tr><td colspan="6">No data yet.</td></tr>`;
        return;
      }

      recentTable.innerHTML = rows.map(row => `
        <tr>
          <td>${row.ts ?? "—"}</td>
          <td>${row.client_ts ?? "—"}</td>
          <td>${typeof row.cpu === "number" ? row.cpu.toFixed(1) + "%" : "—"}</td>
          <td>${typeof row.ram === "number" ? row.ram.toFixed(1) + "%" : "—"}</td>
          <td>${typeof row.disk_percent === "number" ? row.disk_percent.toFixed(1) + "%" : "—"}</td>
          <td>${typeof row.cpu_temp === "number" ? row.cpu_temp.toFixed(1) + "°C" : "—"}</td>
        </tr>
      `).join("");
    }

    async function updateCards(pc) {
      const data = await fetchSummary(pc);

      lastServerTs = data.last_ts || null;

      cpuValue.textContent = typeof data.cpu === "number" ? data.cpu.toFixed(1) + "%" : "—";
      ramValue.textContent = typeof data.ram === "number" ? data.ram.toFixed(1) + "%" : "—";
      diskValue.textContent = typeof data.disk_percent === "number" ? data.disk_percent.toFixed(1) + "%" : "—";
      tempValue.textContent = typeof data.cpu_temp === "number" ? data.cpu_temp.toFixed(1) + "°C" : "—";

      setHealth(cpuHealth, data.cpu_health, data.cpu_health_color);
      setHealth(ramHealth, data.ram_health, data.ram_health_color);
      setHealth(diskHealth, data.disk_health, data.disk_health_color);
      setHealth(tempHealth, data.temp_health, data.temp_health_color);

      liveText.textContent = data.status_text || "OFFLINE";
      liveDot.style.background = data.status_color || "#ff4d6d";
      liveDot.style.boxShadow = `0 0 16px ${data.status_color || "#ff4d6d"}`;

      renderDrives(data.drives || []);
      renderRecent(data.recent_rows || []);
    }

    pauseBtn.addEventListener("click", () => {
      isPaused = !isPaused;
      pauseBtn.textContent = isPaused ? "▶ Resume" : "⏸ Pause";
    });

    pcSelect.addEventListener("change", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("pc", pcSelect.value);
      url.searchParams.set("range", rangeSelect.value);
      window.location.href = url.toString();
    });

    rangeSelect.addEventListener("change", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("pc", pcSelect.value);
      url.searchParams.set("range", rangeSelect.value);
      window.location.href = url.toString();
    });

    let fpsFrames = 0;
    let fpsLast = performance.now();
    function fpsLoop(now) {
      fpsFrames++;
      if (now - fpsLast >= 1000) {
        fpsCounter.textContent = fpsFrames.toString();
        fpsFrames = 0;
        fpsLast = now;
      }
      requestAnimationFrame(fpsLoop);
    }
    requestAnimationFrame(fpsLoop);

    function ageLoop() {
      if (!lastServerTs) {
        ageCounter.textContent = "—";
        return;
      }
      const iso = lastServerTs.replace(" ", "T") + "Z";
      const dt = new Date(iso);
      const ageSec = Math.max(0, Math.floor((Date.now() - dt.getTime()) / 1000));
      ageCounter.textContent = ageSec + "s";
    }
    setInterval(ageLoop, 500);

    async function tick() {
      if (isPaused || ticking) return;
      ticking = true;
      try {
        const pc = pcSelect.value;
        const range = rangeSelect.value;
        await updateChart(pc, range);
        await updateCards(pc);
      } catch (err) {
        console.error("Tick failed:", err);
      } finally {
        ticking = false;
      }
    }

    applyBars();
    tick();
    tickTimer = setInterval(tick, 5000);
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
    range_key = request.args.get("range", "1h").strip()

    range_options = [
        ("15m", "Last 15m"),
        ("1h", "Last 1h"),
        ("6h", "Last 6h"),
        ("24h", "Last 24h"),
    ]
    valid_ranges = {k for k, _ in range_options}
    selected_range = range_key if range_key in valid_ranges else "1h"

    conn = db()
    pc_list, selected_pc = get_selected_pc(conn, pc_param)

    latest = conn.execute(
        """
        SELECT ts, client_ts, cpu, ram, cpu_temp, disk_percent, disk_used_gb, disk_total_gb, drives_json
        FROM stats
        WHERE COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (selected_pc,),
    ).fetchone()

    recent_rows = conn.execute(
        """
        SELECT ts, client_ts, cpu, ram, cpu_temp, disk_percent
        FROM stats
        WHERE COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') = ?
        ORDER BY id DESC
        LIMIT 15
        """,
        (selected_pc,),
    ).fetchall()
    conn.close()

    status_text = "OFFLINE"
    status_color = "#ff4d6d"

    cpu_value = "—"
    ram_value = "—"
    disk_value = "—"
    temp_value = "—"

    cpu_health, cpu_health_color = ("—", "#9aa4b2")
    ram_health, ram_health_color = ("—", "#9aa4b2")
    disk_health, disk_health_color = ("—", "#9aa4b2")
    temp_health, temp_health_color = ("—", "#9aa4b2")
    drives = []

    if latest:
        now = datetime.now(timezone.utc)
        try:
            age_sec = (now - parse_ts_utc(latest["ts"])).total_seconds()
            if age_sec <= LIVE_WINDOW_SECONDS:
                status_text = "LIVE"
                status_color = "#38d996"
        except Exception:
            pass

        cpu_value = f"{fmt1(latest['cpu'])}%"
        ram_value = f"{fmt1(latest['ram'])}%"
        disk_value = f"{fmt1(latest['disk_percent'])}%"
        temp_value = f"{fmt1(latest['cpu_temp'])}°C"

        cpu_health, cpu_health_color = health_from_percent(latest["cpu"], CPU_WARN, CPU_CRIT)
        ram_health, ram_health_color = health_from_percent(latest["ram"], RAM_WARN, RAM_CRIT)
        disk_health, disk_health_color = health_from_percent(latest["disk_percent"], DISK_WARN, DISK_CRIT)
        temp_health, temp_health_color = health_from_temp_c(latest["cpu_temp"], TEMP_WARN_C, TEMP_CRIT_C)
        drives = safe_load_drives(latest["drives_json"])

    return render_template_string(
        DASH_HTML,
        pc_list=pc_list,
        selected_pc=selected_pc,
        range_options=range_options,
        selected_range=selected_range,
        status_text=status_text,
        status_color=status_color,
        cpu_value=cpu_value,
        ram_value=ram_value,
        disk_value=disk_value,
        temp_value=temp_value,
        cpu_health=cpu_health,
        cpu_health_color=cpu_health_color,
        ram_health=ram_health,
        ram_health_color=ram_health_color,
        disk_health=disk_health,
        disk_health_color=disk_health_color,
        temp_health=temp_health,
        temp_health_color=temp_health_color,
        drives=drives,
        recent_rows=recent_rows,
    )


@app.post("/api/ingest")
def ingest():
    provided_key = request.headers.get("X-API-Key", "")
    if provided_key != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "invalid_json"}), 400

    pc_name = clean_text(data.get("pc_name")) or "unknown"
    notes = clean_text(data.get("notes")) or pc_name

    cpu = clamp_num(data.get("cpu"), 0, 100)
    ram = clamp_num(data.get("ram"), 0, 100)
    gpu = clamp_num(data.get("gpu"), 0, 100)
    cpu_temp = _to_float(data.get("cpu_temp"))
    disk_percent = clamp_num(data.get("disk_percent"), 0, 100)
    disk_used_gb = _to_float(data.get("disk_used_gb"))
    disk_total_gb = _to_float(data.get("disk_total_gb"))
    client_ts = validate_ts(data.get("client_ts"))
    drives = normalize_drives(data.get("drives"))

    server_ts = utc_now_str()
    drives_json = json.dumps(drives, separators=(",", ":"))

    conn = db()
    conn.execute(
        """
        INSERT INTO stats (
            ts, cpu, ram, gpu, notes, pc_name,
            cpu_temp, disk_percent, disk_used_gb, disk_total_gb,
            drives_json, client_ts
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            server_ts,
            cpu,
            ram,
            gpu,
            notes,
            pc_name,
            cpu_temp,
            disk_percent,
            disk_used_gb,
            disk_total_gb,
            drives_json,
            client_ts,
        ),
    )
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "ts": server_ts})


@app.get("/api/stats")
def api_stats():
    pc_param = request.args.get("pc", "").strip()
    range_key = request.args.get("range", "1h").strip()

    conn = db()
    pc_list, selected_pc = get_selected_pc(conn, pc_param)

    delta = get_range_delta(range_key)
    threshold = (datetime.now(timezone.utc) - delta).strftime("%Y-%m-%d %H:%M:%S")

    rows = conn.execute(
        """
        SELECT ts, client_ts, cpu, ram, cpu_temp, disk_percent
        FROM stats
        WHERE COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') = ?
          AND ts >= ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (selected_pc, threshold, MAX_STATS_POINTS),
    ).fetchall()
    conn.close()

    points = [
        {
            "ts": row["ts"],
            "client_ts": row["client_ts"],
            "cpu": _to_float(row["cpu"]),
            "ram": _to_float(row["ram"]),
            "cpu_temp": _to_float(row["cpu_temp"]),
            "disk_percent": _to_float(row["disk_percent"]),
        }
        for row in rows
    ]

    return jsonify({"ok": True, "pc": selected_pc, "available_pcs": pc_list, "points": points})


@app.get("/api/summary")
def api_summary():
    pc_param = request.args.get("pc", "").strip()

    conn = db()
    pc_list, selected_pc = get_selected_pc(conn, pc_param)

    latest = conn.execute(
        """
        SELECT ts, client_ts, cpu, ram, cpu_temp, disk_percent, disk_used_gb, disk_total_gb, drives_json
        FROM stats
        WHERE COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (selected_pc,),
    ).fetchone()

    recent_rows = conn.execute(
        """
        SELECT ts, client_ts, cpu, ram, cpu_temp, disk_percent
        FROM stats
        WHERE COALESCE(NULLIF(pc_name, ''), NULLIF(notes, ''), 'unknown') = ?
        ORDER BY id DESC
        LIMIT 15
        """,
        (selected_pc,),
    ).fetchall()
    conn.close()

    status_text = "OFFLINE"
    status_color = "#ff4d6d"

    if latest:
        try:
            age_sec = (datetime.now(timezone.utc) - parse_ts_utc(latest["ts"])).total_seconds()
            if age_sec <= LIVE_WINDOW_SECONDS:
                status_text = "LIVE"
                status_color = "#38d996"
        except Exception:
            pass

        cpu_health, cpu_health_color = health_from_percent(latest["cpu"], CPU_WARN, CPU_CRIT)
        ram_health, ram_health_color = health_from_percent(latest["ram"], RAM_WARN, RAM_CRIT)
        disk_health, disk_health_color = health_from_percent(latest["disk_percent"], DISK_WARN, DISK_CRIT)
        temp_health, temp_health_color = health_from_temp_c(latest["cpu_temp"], TEMP_WARN_C, TEMP_CRIT_C)

        return jsonify(
            {
                "ok": True,
                "pc": selected_pc,
                "available_pcs": pc_list,
                "status_text": status_text,
                "status_color": status_color,
                "last_ts": latest["ts"],
                "last_client_ts": latest["client_ts"],
                "cpu": _to_float(latest["cpu"]),
                "ram": _to_float(latest["ram"]),
                "cpu_temp": _to_float(latest["cpu_temp"]),
                "disk_percent": _to_float(latest["disk_percent"]),
                "disk_used_gb": _to_float(latest["disk_used_gb"]),
                "disk_total_gb": _to_float(latest["disk_total_gb"]),
                "cpu_health": cpu_health,
                "cpu_health_color": cpu_health_color,
                "ram_health": ram_health,
                "ram_health_color": ram_health_color,
                "disk_health": disk_health,
                "disk_health_color": disk_health_color,
                "temp_health": temp_health,
                "temp_health_color": temp_health_color,
                "drives": safe_load_drives(latest["drives_json"]),
                "recent_rows": [
                    {
                        "ts": row["ts"],
                        "client_ts": row["client_ts"],
                        "cpu": _to_float(row["cpu"]),
                        "ram": _to_float(row["ram"]),
                        "cpu_temp": _to_float(row["cpu_temp"]),
                        "disk_percent": _to_float(row["disk_percent"]),
                    }
                    for row in recent_rows
                ],
            }
        )

    return jsonify(
        {
            "ok": True,
            "pc": selected_pc,
            "available_pcs": pc_list,
            "status_text": status_text,
            "status_color": status_color,
            "last_ts": None,
            "last_client_ts": None,
            "cpu": None,
            "ram": None,
            "cpu_temp": None,
            "disk_percent": None,
            "disk_used_gb": None,
            "disk_total_gb": None,
            "cpu_health": "—",
            "cpu_health_color": "#9aa4b2",
            "ram_health": "—",
            "ram_health_color": "#9aa4b2",
            "disk_health": "—",
            "disk_health_color": "#9aa4b2",
            "temp_health": "—",
            "temp_health_color": "#9aa4b2",
            "drives": [],
            "recent_rows": [],
        }
    )


@app.get("/health")
def health():
    return jsonify({"ok": True, "ts": utc_now_str()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=False)
