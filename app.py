import os
import time
import json
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import Flask, request, jsonify, render_template_string

# ======================================================
# CONFIG
# ======================================================
API_KEY = os.environ.get("API_KEY", "")
PORT = int(os.environ.get("PORT", "10000"))

# How long until we consider a PC "OFFLINE"
OFFLINE_AFTER_SEC = 20

# How many points per metric history to keep (per PC)
HISTORY_MAX_POINTS = 720  # ~1 hour at 5s interval

# Persist thresholds to disk (Render: ephemeral but works while running)
THRESHOLDS_FILE = "thresholds.json"

DEFAULT_THRESHOLDS = {
    "cpu_pct": {"enabled": True, "threshold": 90, "cooldown": 300},
    "ram_pct": {"enabled": True, "threshold": 90, "cooldown": 300},
    "disk_c_used_pct": {"enabled": True, "threshold": 90, "cooldown": 600},
    "offline": {"enabled": True, "threshold": 1, "cooldown": 120},  # threshold unused (just ON/OFF)
}

# ======================================================
# STATE
# ======================================================
app = Flask(__name__)

latest_by_pc = {}
history_by_pc = defaultdict(lambda: defaultdict(lambda: deque(maxlen=HISTORY_MAX_POINTS)))
alerts_by_pc = defaultdict(lambda: deque(maxlen=200))  # store recent alerts
last_trip_by_pc = defaultdict(dict)  # pc -> metric -> epoch sec last alert fired

thresholds_by_pc = defaultdict(lambda: json.loads(json.dumps(DEFAULT_THRESHOLDS)))  # deep copy


# ======================================================
# HELPERS
# ======================================================
def utc_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()

def now_epoch():
    return int(time.time())

def load_thresholds():
    global thresholds_by_pc
    if os.path.exists(THRESHOLDS_FILE):
        try:
            with open(THRESHOLDS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            # validate lightly
            for pc, cfg in data.items():
                if isinstance(cfg, dict):
                    thresholds_by_pc[pc] = cfg
        except Exception:
            pass

def save_thresholds():
    try:
        # convert defaultdict to plain dict
        data = {pc: cfg for pc, cfg in thresholds_by_pc.items()}
        with open(THRESHOLDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass

def get_pc_status(pc):
    d = latest_by_pc.get(pc)
    if not d:
        return "UNKNOWN"
    last_seen = d.get("_server_ts_epoch", 0)
    if now_epoch() - last_seen > OFFLINE_AFTER_SEC:
        return "OFFLINE"
    return "LIVE"

def push_history(pc, metric, value, ts_epoch):
    history_by_pc[pc][metric].append({"t": ts_epoch, "v": value})

def trip_alert(pc, metric, message, value=None):
    alerts_by_pc[pc].appendleft({
        "ts": utc_iso(),
        "metric": metric,
        "message": message,
        "value": value,
        "acked": False
    })

def can_fire(pc, metric):
    cfg = thresholds_by_pc[pc].get(metric, {})
    cooldown = int(cfg.get("cooldown", 300))
    last = int(last_trip_by_pc[pc].get(metric, 0))
    return (now_epoch() - last) >= cooldown

def mark_fired(pc, metric):
    last_trip_by_pc[pc][metric] = now_epoch()

def evaluate_thresholds(pc, payload):
    cfg = thresholds_by_pc[pc]

    # cpu_pct
    if cfg.get("cpu_pct", {}).get("enabled", True):
        cpu = payload.get("cpu", {}).get("usage_pct")
        thr = float(cfg["cpu_pct"].get("threshold", 90))
        if cpu is not None and cpu >= thr and can_fire(pc, "cpu_pct"):
            trip_alert(pc, "cpu_pct", f"CPU >= {thr:.0f}% (now {cpu:.1f}%)", cpu)
            mark_fired(pc, "cpu_pct")

    # ram_pct
    if cfg.get("ram_pct", {}).get("enabled", True):
        ram = payload.get("ram", {}).get("usage_pct")
        thr = float(cfg["ram_pct"].get("threshold", 90))
        if ram is not None and ram >= thr and can_fire(pc, "ram_pct"):
            trip_alert(pc, "ram_pct", f"RAM >= {thr:.0f}% (now {ram:.1f}%)", ram)
            mark_fired(pc, "ram_pct")

    # disk C used %
    if cfg.get("disk_c_used_pct", {}).get("enabled", True):
        used = payload.get("disk", {}).get("c", {}).get("used_pct")
        thr = float(cfg["disk_c_used_pct"].get("threshold", 90))
        if used is not None and used >= thr and can_fire(pc, "disk_c_used_pct"):
            trip_alert(pc, "disk_c_used_pct", f"Disk C: >= {thr:.0f}% used (now {used:.1f}%)", used)
            mark_fired(pc, "disk_c_used_pct")


def evaluate_offline_alerts():
    # Fire OFFLINE alert when status flips to offline, respecting cooldown
    for pc in list(latest_by_pc.keys()):
        cfg = thresholds_by_pc[pc].get("offline", {})
        if not cfg.get("enabled", True):
            continue

        status = get_pc_status(pc)
        if status == "OFFLINE" and can_fire(pc, "offline"):
            trip_alert(pc, "offline", "PC went OFFLINE")
            mark_fired(pc, "offline")


# ======================================================
# ROUTES
# ======================================================
@app.get("/api/health")
def health():
    return jsonify({"ok": True, "ts": utc_iso()})

@app.post("/api/ingest")
def ingest():
    # Auth
    key = request.headers.get("X-API-KEY", "")
    if API_KEY and key != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    pc = payload.get("pc") or payload.get("PC_NAME") or "Unknown"

    # Server-side timestamps
    payload["_server_ts"] = utc_iso()
    payload["_server_ts_epoch"] = now_epoch()

    latest_by_pc[pc] = payload

    # Store history points (cpu/ram)
    ts = payload["_server_ts_epoch"]
    cpu = payload.get("cpu", {}).get("usage_pct")
    ram = payload.get("ram", {}).get("usage_pct")
    if cpu is not None:
        push_history(pc, "cpu_pct", float(cpu), ts)
    if ram is not None:
        push_history(pc, "ram_pct", float(ram), ts)

    # Evaluate alert thresholds
    evaluate_thresholds(pc, payload)

    return jsonify({"ok": True})

@app.get("/api/state")
def state():
    # Runs offline evaluation here too
    evaluate_offline_alerts()

    pcs = sorted(latest_by_pc.keys())
    pc_states = {}
    for pc in pcs:
        pc_states[pc] = {
            "status": get_pc_status(pc),
            "latest": latest_by_pc.get(pc, {}),
            "alerts": list(alerts_by_pc[pc])[:50],
            "thresholds": thresholds_by_pc[pc],
        }

    return jsonify({
        "ts": utc_iso(),
        "pcs": pcs,
        "pc_states": pc_states,
    })

@app.get("/api/history")
def history():
    pc = request.args.get("pc", "")
    metric = request.args.get("metric", "cpu_pct")
    hours = float(request.args.get("hours", "1"))

    if not pc or pc not in history_by_pc:
        return jsonify({"ok": False, "error": "pc not found"}), 404

    # filter by time window
    cutoff = now_epoch() - int(hours * 3600)
    points = [p for p in history_by_pc[pc][metric] if p["t"] >= cutoff]
    return jsonify({"ok": True, "pc": pc, "metric": metric, "points": points})

@app.post("/api/thresholds")
def set_thresholds():
    key = request.headers.get("X-API-KEY", "")
    if API_KEY and key != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    pc = data.get("pc")
    new_cfg = data.get("thresholds")

    if not pc or not isinstance(new_cfg, dict):
        return jsonify({"ok": False, "error": "bad request"}), 400

    thresholds_by_pc[pc] = new_cfg
    save_thresholds()
    return jsonify({"ok": True})

@app.post("/api/alerts/ack")
def ack_alerts():
    key = request.headers.get("X-API-KEY", "")
    if API_KEY and key != API_KEY:
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    pc = data.get("pc")
    if not pc:
        return jsonify({"ok": False, "error": "missing pc"}), 400

    # ack all currently visible alerts
    updated = 0
    for a in alerts_by_pc[pc]:
        if not a.get("acked"):
            a["acked"] = True
            updated += 1

    return jsonify({"ok": True, "acked": updated})


# ======================================================
# UI (Clean glass dashboard like your 2nd screenshot)
# ======================================================
DASH_HTML = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Command Center</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&display=swap" rel="stylesheet">
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root{
      --bg1:#0b1220;
      --bg2:#0f172a;
      --card: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.10);
      --muted: rgba(226,232,240,0.65);
      --text:#e2e8f0;
      --good:#22c55e;
      --bad:#ef4444;
      --warn:#f59e0b;
    }

    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      color:var(--text);
      background: radial-gradient(900px 700px at 10% 10%, #1b2a55 0%, var(--bg1) 45%, #070b14 100%);
      min-height:100vh;
      padding:22px;
    }

    .wrap{max-width:980px;margin:0 auto; display:flex; flex-direction:column; gap:16px;}

    .header{
      display:flex; align-items:flex-start; justify-content:space-between; gap:14px;
    }
    .title{
      display:flex; flex-direction:column; gap:6px;
    }
    h1{font-size:34px; margin:0; letter-spacing:-0.02em;}
    .subtitle{color:var(--muted); font-size:14px;}

    .badge{
      display:inline-flex; align-items:center; gap:10px;
      padding:8px 14px; border-radius:999px;
      background: rgba(255,255,255,0.06);
      border: 1px solid var(--border);
      font-weight:500;
    }
    .dot{
      width:10px;height:10px;border-radius:999px;
      background: var(--good);
      box-shadow: 0 0 18px rgba(34,197,94,0.55);
      animation: pulse 2s infinite;
    }
    .dot.offline{ background: var(--bad); box-shadow: 0 0 18px rgba(239,68,68,0.55); animation:none;}
    @keyframes pulse{
      0%,100%{transform:scale(1); opacity:1}
      50%{transform:scale(1.25); opacity:.7}
    }

    .card{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 18px;
      backdrop-filter: blur(16px);
      box-shadow: 0 12px 34px rgba(0,0,0,0.35);
    }

    .row{
      display:grid;
      grid-template-columns: 1fr;
      gap: 14px;
    }

    .controls{
      display:flex; flex-wrap:wrap; gap:10px; align-items:center;
    }
    select, input{
      background: rgba(255,255,255,0.06);
      border: 1px solid rgba(255,255,255,0.12);
      color: var(--text);
      padding: 10px 12px;
      border-radius: 12px;
      outline:none;
      font-size:14px;
    }
    button{
      background: rgba(255,255,255,0.10);
      border: 1px solid rgba(255,255,255,0.16);
      color: var(--text);
      padding: 10px 14px;
      border-radius: 12px;
      font-weight:500;
      cursor:pointer;
    }
    button:hover{ background: rgba(255,255,255,0.14);}

    .chartTitle{
      font-size:14px; color: var(--muted); margin-bottom: 10px;
      display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap;
    }

    .stats{
      display:grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 14px;
    }
    .statTitle{ color: var(--muted); font-size:14px; }
    .statValue{ font-size:44px; font-weight:600; letter-spacing:-0.03em; margin-top:6px; }
    .statSub{ color: var(--muted); font-size:13px; margin-top:6px; }

    .alertsHeader{
      display:flex; justify-content:space-between; align-items:center; gap:10px;
      margin-bottom: 10px;
    }
    .alertItem{
      padding: 12px 12px;
      border-radius: 14px;
      border:1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.05);
      margin-bottom: 10px;
      display:flex; flex-direction:column; gap:6px;
    }
    .alertItem.acked{ opacity:0.5;}
    .alertTop{ display:flex; justify-content:space-between; gap:10px; flex-wrap:wrap;}
    .alertMetric{ font-weight:600;}
    .alertTs{ color: var(--muted); font-size:12px;}
    .alertMsg{ color: var(--text); font-size:14px;}

    details summary{
      cursor:pointer;
      color: var(--muted);
      font-size:14px;
      margin-bottom: 10px;
    }

    .thresholdGrid{
      display:grid;
      grid-template-columns: 1fr;
      gap: 10px;
      margin-top: 12px;
    }
    .thrRow{
      display:grid;
      grid-template-columns: 1.2fr .8fr 1fr 1fr;
      gap:10px;
      align-items:center;
      padding: 10px 10px;
      border-radius: 14px;
      border:1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.04);
    }
    .thrRow label{ color: var(--muted); font-size:13px; }
    .small{ font-size:12px; color: var(--muted); }

    @media (min-width: 820px){
      .row{ grid-template-columns: 1.3fr .9fr; }
      .stats{ grid-template-columns: repeat(4, 1fr); }
      .thresholdGrid{ grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="header">
      <div class="title">
        <h1>⚡ Command Center</h1>
        <div class="subtitle">Live PC telemetry • remote dashboard</div>
      </div>
      <div class="badge" id="statusBadge">
        <div class="dot" id="statusDot"></div>
        <div id="statusText">LIVE</div>
      </div>
    </div>

    <div class="card">
      <div class="controls">
        <label class="small">PC</label>
        <select id="pcSelect"></select>

        <label class="small">History</label>
        <select id="metricSelect">
          <option value="cpu_pct">CPU %</option>
          <option value="ram_pct">RAM %</option>
        </select>

        <label class="small">Hours</label>
        <input id="hoursInput" type="number" min="0.25" step="0.25" value="1" style="width:90px" />

        <button onclick="refreshAll()">Refresh</button>
      </div>
    </div>

    <div class="row">
      <div class="card">
        <div class="chartTitle">
          <div>LIVE TELEMETRY</div>
          <div class="small" id="lastUpdate"></div>
        </div>
        <canvas id="chart" height="140"></canvas>
      </div>

      <div class="card">
        <div class="alertsHeader">
          <div style="font-weight:600;">Alerts</div>
          <button onclick="ackAlerts()">Ack visible</button>
        </div>
        <div id="alertsList" class="small">No alerts yet.</div>

        <details style="margin-top:14px;">
          <summary>Thresholds</summary>
          <div class="thresholdGrid" id="thresholds"></div>
          <button style="margin-top:12px;" onclick="saveThresholds()">Save thresholds</button>
          <div class="small" id="saveMsg" style="margin-top:8px;"></div>
        </details>

        <details style="margin-top:12px;">
          <summary>Advanced JSON</summary>
          <pre id="rawJson" style="white-space:pre-wrap; font-size:12px; color:rgba(226,232,240,0.75);"></pre>
        </details>
      </div>
    </div>

    <div class="stats">
      <div class="card">
        <div class="statTitle">CPU</div>
        <div class="statValue" id="cpuVal">—</div>
        <div class="statSub" id="cpuSub">Processor load</div>
      </div>
      <div class="card">
        <div class="statTitle">RAM</div>
        <div class="statValue" id="ramVal">—</div>
        <div class="statSub" id="ramSub">Memory usage</div>
      </div>
      <div class="card">
        <div class="statTitle">Disk C:</div>
        <div class="statValue" id="diskVal">—</div>
        <div class="statSub" id="diskSub">Free / Total</div>
      </div>
      <div class="card">
        <div class="statTitle">GPU</div>
        <div class="statValue" id="gpuVal">—</div>
        <div class="statSub" id="gpuSub">Next: real GPU %</div>
      </div>
    </div>

  </div>

<script>
  let chart;
  let stateCache;

  function fmtGB(x){
    if(x === null || x === undefined) return "—";
    return (x).toFixed(1) + " GB";
  }

  function initChart(){
    const ctx = document.getElementById("chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {label: "CPU %", data: [], tension: 0.35, borderWidth: 2},
          {label: "RAM %", data: [], tension: 0.35, borderWidth: 2},
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { min: 0, max: 100, grid: { color: "rgba(255,255,255,0.05)" } },
          x: { grid: { color: "rgba(255,255,255,0.03)" } }
        },
        plugins: {
          legend: { labels: { color: "rgba(226,232,240,0.75)" } }
        }
      }
    });
  }

  async function fetchState(){
    const r = await fetch("/api/state");
    return await r.json();
  }

  async function fetchHistory(pc, metric, hours){
    const r = await fetch(`/api/history?pc=${encodeURIComponent(pc)}&metric=${encodeURIComponent(metric)}&hours=${encodeURIComponent(hours)}`);
    return await r.json();
  }

  function setBadge(status, ts){
    const dot = document.getElementById("statusDot");
    const text = document.getElementById("statusText");
    if(status === "LIVE"){
      dot.classList.remove("offline");
      text.textContent = "LIVE";
    } else if(status === "OFFLINE"){
      dot.classList.add("offline");
      text.textContent = "OFFLINE";
    } else {
      dot.classList.add("offline");
      text.textContent = status;
    }
    document.getElementById("lastUpdate").textContent = ts ? ("Last update (UTC): " + ts) : "";
  }

  function renderStats(latest){
    const cpu = latest?.cpu?.usage_pct;
    const ram = latest?.ram?.usage_pct;
    const ramUsed = latest?.ram?.used_gb;
    const ramTotal = latest?.ram?.total_gb;

    const diskUsed = latest?.disk?.c?.used_pct;
    const diskFree = latest?.disk?.c?.free_gb;
    const diskTotal = latest?.disk?.c?.total_gb;

    document.getElementById("cpuVal").textContent = (cpu!=null ? cpu.toFixed(1)+"%" : "—");
    document.getElementById("ramVal").textContent = (ram!=null ? ram.toFixed(1)+"%" : "—");

    document.getElementById("ramSub").textContent =
      (ramUsed!=null && ramTotal!=null) ? (`Used: ${fmtGB(ramUsed)} / ${fmtGB(ramTotal)}`) : "Memory usage";

    document.getElementById("diskVal").textContent = (diskUsed!=null ? diskUsed.toFixed(1)+"%" : "—");
    document.getElementById("diskSub").textContent =
      (diskFree!=null && diskTotal!=null) ? (`Free: ${fmtGB(diskFree)} / ${fmtGB(diskTotal)}`) : "Free / Total";

    document.getElementById("gpuVal").textContent = "—";
  }

  function renderAlerts(alerts){
    const box = document.getElementById("alertsList");
    if(!alerts || alerts.length === 0){
      box.textContent = "No alerts yet.";
      return;
    }
    box.innerHTML = "";
    alerts.slice(0, 8).forEach(a=>{
      const div = document.createElement("div");
      div.className = "alertItem" + (a.acked ? " acked" : "");
      div.innerHTML = `
        <div class="alertTop">
          <div class="alertMetric">${a.metric}</div>
          <div class="alertTs">${a.ts}</div>
        </div>
        <div class="alertMsg">${a.message}</div>
      `;
      box.appendChild(div);
    });
  }

  function renderThresholds(thr){
    const root = document.getElementById("thresholds");
    root.innerHTML = "";
    const keys = ["cpu_pct", "ram_pct", "disk_c_used_pct", "offline"];
    const labels = {
      cpu_pct: "CPU %",
      ram_pct: "RAM %",
      disk_c_used_pct: "Disk C: Used %",
      offline: "Offline"
    };

    keys.forEach(k=>{
      const v = thr[k] || {};
      const row = document.createElement("div");
      row.className = "thrRow";
      row.innerHTML = `
        <label>${labels[k]}</label>

        <select data-k="${k}" data-f="enabled">
          <option value="true" ${v.enabled ? "selected":""}>On</option>
          <option value="false" ${!v.enabled ? "selected":""}>Off</option>
        </select>

        <input data-k="${k}" data-f="threshold" type="number" value="${v.threshold ?? ""}" placeholder="threshold" />

        <input data-k="${k}" data-f="cooldown" type="number" value="${v.cooldown ?? 300}" placeholder="cooldown (s)" />
      `;
      root.appendChild(row);
    });
  }

  function gatherThresholds(){
    const inputs = document.querySelectorAll("#thresholds [data-k]");
    const out = {};
    inputs.forEach(el=>{
      const k = el.getAttribute("data-k");
      const f = el.getAttribute("data-f");
      out[k] = out[k] || {};
      let val = el.value;
      if(f === "enabled") val = (val === "true");
      if(f === "threshold" || f === "cooldown") val = Number(val);
      out[k][f] = val;
    });
    return out;
  }

  async function saveThresholds(){
    const pc = document.getElementById("pcSelect").value;
    const thresholds = gatherThresholds();
    const r = await fetch("/api/thresholds", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({pc, thresholds})
    });
    const j = await r.json();
    document.getElementById("saveMsg").textContent = j.ok ? "Saved." : ("Error: " + (j.error || "unknown"));
    setTimeout(()=> document.getElementById("saveMsg").textContent="", 2000);
  }

  async function ackAlerts(){
    const pc = document.getElementById("pcSelect").value;
    await fetch("/api/alerts/ack", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({pc})
    });
    refreshAll();
  }

  async function refreshAll(){
    stateCache = await fetchState();
    const pcs = stateCache.pcs || [];
    const sel = document.getElementById("pcSelect");

    // Populate PC dropdown once (or if empty)
    if(sel.options.length === 0){
      pcs.forEach(p=>{
        const o = document.createElement("option");
        o.value = p; o.textContent = p;
        sel.appendChild(o);
      });
      if(pcs.length > 0) sel.value = pcs[0];
    } else {
      // ensure still valid
      if(!pcs.includes(sel.value) && pcs.length>0) sel.value = pcs[0];
    }

    const pc = sel.value;
    const metric = document.getElementById("metricSelect").value;
    const hours = Number(document.getElementById("hoursInput").value || 1);

    const pcState = stateCache.pc_states?.[pc];
    const status = pcState?.status || "UNKNOWN";
    const latest = pcState?.latest || {};
    setBadge(status, latest?._server_ts);

    renderStats(latest);
    renderAlerts(pcState?.alerts || []);
    renderThresholds(pcState?.thresholds || {});

    document.getElementById("rawJson").textContent = JSON.stringify(latest, null, 2);

    // History graph
    const h = await fetchHistory(pc, metric, hours);
    if(h.ok){
      const pts = h.points || [];
      const labels = pts.map(p=>{
        const d = new Date(p.t * 1000);
        return d.toISOString().slice(11,19); // HH:MM:SS
      });

      // Always show CPU & RAM together like your phone screenshot
      const cpuPts = await fetchHistory(pc, "cpu_pct", hours);
      const ramPts = await fetchHistory(pc, "ram_pct", hours);

      const cpuData = (cpuPts.ok ? cpuPts.points : []).map(p=>p.v);
      const ramData = (ramPts.ok ? ramPts.points : []).map(p=>p.v);

      chart.data.labels = labels;
      chart.data.datasets[0].data = cpuData;
      chart.data.datasets[1].data = ramData;
      chart.update();
    }
  }

  document.addEventListener("DOMContentLoaded", ()=>{
    initChart();
    refreshAll();
    setInterval(refreshAll, 5000);
  });
</script>
</body>
</html>
"""

@app.get("/")
def dashboard():
    return render_template_string(DASH_HTML)


# ======================================================
# BOOT
# ======================================================
load_thresholds()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
