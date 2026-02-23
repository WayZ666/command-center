import os
import time
import json
import secrets
from datetime import datetime, timezone
from collections import defaultdict, deque

from flask import Flask, request, jsonify, render_template_string, make_response

# =========================
# CONFIG
# =========================
APP_NAME = "Command Center"
PORT = int(os.environ.get("PORT", "10000"))

# Agent auth (required)
API_KEY = os.environ.get("API_KEY", "")

# Admin auth (for changing thresholds in UI)
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

OFFLINE_AFTER_SEC = int(os.environ.get("OFFLINE_AFTER_SEC", "20"))
HISTORY_MAX_POINTS = int(os.environ.get("HISTORY_MAX_POINTS", "2000"))
THRESHOLDS_FILE = os.environ.get("THRESHOLDS_FILE", "thresholds.json")

DEFAULT_THRESHOLDS = {
    "cpu_pct": {"enabled": True, "threshold": 90.0, "cooldown": 300},
    "ram_pct": {"enabled": True, "threshold": 90.0, "cooldown": 300},
    "disk_c_used_pct": {"enabled": True, "threshold": 90.0, "cooldown": 600},
    "offline": {"enabled": True, "threshold": 1.0, "cooldown": 180},
}

# =========================
# APP + STATE
# =========================
app = Flask(__name__)

latest_by_pc = {}              # pc -> latest payload
last_seen_epoch = {}           # pc -> last seen epoch seconds

history_by_pc = defaultdict(lambda: {
    "cpu_pct": deque(maxlen=HISTORY_MAX_POINTS),
    "ram_pct": deque(maxlen=HISTORY_MAX_POINTS),
})

alerts_by_pc = defaultdict(lambda: deque(maxlen=200))  # newest first
last_fired_by_pc = defaultdict(dict)                   # pc -> metric -> epoch sec

thresholds_by_pc = defaultdict(lambda: json.loads(json.dumps(DEFAULT_THRESHOLDS)))

# Admin session tokens (simple cookie auth)
admin_tokens = {}  # token -> expiry epoch
ADMIN_SESSION_SECONDS = 24 * 3600


# =========================
# HELPERS
# =========================
def utc_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def now_epoch():
    return int(time.time())


def deep_copy_defaults():
    return json.loads(json.dumps(DEFAULT_THRESHOLDS))


def load_thresholds():
    if not os.path.exists(THRESHOLDS_FILE):
        return
    try:
        with open(THRESHOLDS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            for pc, cfg in data.items():
                if isinstance(cfg, dict):
                    thresholds_by_pc[pc] = cfg
    except Exception:
        pass


def save_thresholds():
    try:
        data = {pc: cfg for pc, cfg in thresholds_by_pc.items()}
        with open(THRESHOLDS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def online(pc: str) -> bool:
    last = last_seen_epoch.get(pc, 0)
    return (now_epoch() - last) <= OFFLINE_AFTER_SEC


def pc_status(pc: str) -> str:
    if pc not in last_seen_epoch:
        return "UNKNOWN"
    return "LIVE" if online(pc) else "OFFLINE"


def require_agent_key():
    if not API_KEY:
        return None  # allow if not set (dev)
    key = request.headers.get("X-API-KEY", "")
    if key != API_KEY:
        return make_response(jsonify({"ok": False, "error": "unauthorized"}), 401)
    return None


def is_admin_authed() -> bool:
    token = request.cookies.get("cc_admin", "")
    if not token:
        return False
    exp = admin_tokens.get(token)
    if not exp:
        return False
    if now_epoch() > exp:
        admin_tokens.pop(token, None)
        return False
    return True


def require_admin():
    if not ADMIN_PASSWORD:
        return make_response(jsonify({"ok": False, "error": "ADMIN_PASSWORD not set"}), 500)
    if not is_admin_authed():
        return make_response(jsonify({"ok": False, "error": "admin not logged in"}), 401)
    return None


def can_fire(pc: str, metric: str) -> bool:
    cfg = thresholds_by_pc[pc].get(metric, {})
    cooldown = int(cfg.get("cooldown", 300))
    last = int(last_fired_by_pc[pc].get(metric, 0))
    return (now_epoch() - last) >= cooldown


def mark_fired(pc: str, metric: str):
    last_fired_by_pc[pc][metric] = now_epoch()


def push_alert(pc: str, metric: str, message: str, value=None, threshold=None):
    alerts_by_pc[pc].appendleft({
        "id": secrets.token_hex(6),
        "ts": utc_iso(),
        "metric": metric,
        "message": message,
        "value": value,
        "threshold": threshold,
        "acked": False,
    })


def evaluate_thresholds(pc: str, payload: dict):
    cfg = thresholds_by_pc[pc]

    cpu = payload.get("cpu", {}).get("usage_pct")
    ram = payload.get("ram", {}).get("usage_pct")
    disk_used = payload.get("disk", {}).get("c", {}).get("used_pct")

    # CPU
    if cfg.get("cpu_pct", {}).get("enabled", True):
        thr = float(cfg["cpu_pct"].get("threshold", 90))
        if cpu is not None and float(cpu) >= thr and can_fire(pc, "cpu_pct"):
            push_alert(pc, "cpu_pct", f"CPU ≥ {thr:.0f}% (now {float(cpu):.1f}%)", float(cpu), thr)
            mark_fired(pc, "cpu_pct")

    # RAM
    if cfg.get("ram_pct", {}).get("enabled", True):
        thr = float(cfg["ram_pct"].get("threshold", 90))
        if ram is not None and float(ram) >= thr and can_fire(pc, "ram_pct"):
            push_alert(pc, "ram_pct", f"RAM ≥ {thr:.0f}% (now {float(ram):.1f}%)", float(ram), thr)
            mark_fired(pc, "ram_pct")

    # Disk C used %
    if cfg.get("disk_c_used_pct", {}).get("enabled", True):
        thr = float(cfg["disk_c_used_pct"].get("threshold", 90))
        if disk_used is not None and float(disk_used) >= thr and can_fire(pc, "disk_c_used_pct"):
            push_alert(pc, "disk_c_used_pct", f"Disk C: ≥ {thr:.0f}% used (now {float(disk_used):.1f}%)", float(disk_used), thr)
            mark_fired(pc, "disk_c_used_pct")


def evaluate_offline_alerts():
    for pc in list(last_seen_epoch.keys()):
        cfg = thresholds_by_pc[pc].get("offline", deep_copy_defaults()["offline"])
        if not cfg.get("enabled", True):
            continue
        if pc_status(pc) == "OFFLINE" and can_fire(pc, "offline"):
            push_alert(pc, "offline", "PC went OFFLINE")
            mark_fired(pc, "offline")


# =========================
# API ROUTES
# =========================
@app.get("/api/health")
def api_health():
    return jsonify({"ok": True, "ts": utc_iso()})


@app.post("/api/ingest")
def api_ingest():
    bad = require_agent_key()
    if bad:
        return bad

    payload = request.get_json(silent=True) or {}
    pc = str(payload.get("pc") or payload.get("PC_NAME") or "Unknown").strip()

    payload["_server_ts"] = utc_iso()
    payload["_server_epoch"] = now_epoch()

    latest_by_pc[pc] = payload
    last_seen_epoch[pc] = payload["_server_epoch"]

    if pc not in thresholds_by_pc:
        thresholds_by_pc[pc] = deep_copy_defaults()

    cpu = payload.get("cpu", {}).get("usage_pct")
    ram = payload.get("ram", {}).get("usage_pct")

    if cpu is not None:
        history_by_pc[pc]["cpu_pct"].append({"t": payload["_server_epoch"], "v": float(cpu)})
    if ram is not None:
        history_by_pc[pc]["ram_pct"].append({"t": payload["_server_epoch"], "v": float(ram)})

    evaluate_thresholds(pc, payload)
    return jsonify({"ok": True})


@app.get("/api/state")
def api_state():
    evaluate_offline_alerts()

    pcs = sorted(set(list(latest_by_pc.keys()) + list(last_seen_epoch.keys())))
    out = {"ts": utc_iso(), "pcs": pcs, "pc_states": {}}

    for pc in pcs:
        out["pc_states"][pc] = {
            "status": pc_status(pc),
            "last_seen_epoch": last_seen_epoch.get(pc, 0),
            "latest": latest_by_pc.get(pc, {}),
            "alerts": list(alerts_by_pc[pc])[:50],
            "thresholds": thresholds_by_pc[pc],
        }

    return jsonify(out)


@app.get("/api/history")
def api_history():
    pc = request.args.get("pc", "")
    hours = float(request.args.get("hours", "1"))

    if pc not in history_by_pc:
        return jsonify({"ok": False, "error": "pc not found"}), 404

    cutoff = now_epoch() - int(hours * 3600)
    cpu = [p for p in history_by_pc[pc]["cpu_pct"] if p["t"] >= cutoff]
    ram = [p for p in history_by_pc[pc]["ram_pct"] if p["t"] >= cutoff]
    return jsonify({"ok": True, "pc": pc, "hours": hours, "cpu": cpu, "ram": ram})


@app.post("/api/admin/login")
def api_admin_login():
    if not ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "ADMIN_PASSWORD not set"}), 500

    data = request.get_json(silent=True) or {}
    pw = str(data.get("password", ""))

    if pw != ADMIN_PASSWORD:
        return jsonify({"ok": False, "error": "bad password"}), 401

    token = secrets.token_urlsafe(24)
    admin_tokens[token] = now_epoch() + ADMIN_SESSION_SECONDS

    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("cc_admin", token, httponly=True, samesite="Lax", secure=True)
    return resp


@app.post("/api/admin/logout")
def api_admin_logout():
    token = request.cookies.get("cc_admin", "")
    if token:
        admin_tokens.pop(token, None)
    resp = make_response(jsonify({"ok": True}))
    resp.set_cookie("cc_admin", "", expires=0)
    return resp


@app.post("/api/thresholds")
def api_thresholds_set():
    bad = require_admin()
    if bad:
        return bad

    data = request.get_json(silent=True) or {}
    pc = str(data.get("pc", "")).strip()
    new_cfg = data.get("thresholds")

    if not pc or not isinstance(new_cfg, dict):
        return jsonify({"ok": False, "error": "bad request"}), 400

    merged = deep_copy_defaults()
    for k, v in new_cfg.items():
        if isinstance(v, dict):
            merged[k] = {
                "enabled": bool(v.get("enabled", merged.get(k, {}).get("enabled", True))),
                "threshold": float(v.get("threshold", merged.get(k, {}).get("threshold", 90.0))),
                "cooldown": int(v.get("cooldown", merged.get(k, {}).get("cooldown", 300))),
            }

    thresholds_by_pc[pc] = merged
    save_thresholds()
    return jsonify({"ok": True})


@app.post("/api/alerts/ack")
def api_alerts_ack():
    bad = require_admin()
    if bad:
        return bad

    data = request.get_json(silent=True) or {}
    pc = str(data.get("pc", "")).strip()
    if not pc:
        return jsonify({"ok": False, "error": "missing pc"}), 400

    updated = 0
    for a in alerts_by_pc[pc]:
        if not a.get("acked"):
            a["acked"] = True
            updated += 1
    return jsonify({"ok": True, "acked": updated})


# =========================
# UI (Graph at TOP like your 2nd photo)
# =========================
DASH_HTML = r"""
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
    }
    *{box-sizing:border-box}
    body{
      margin:0;
      font-family:Inter,system-ui,-apple-system,Segoe UI,Roboto,Arial,sans-serif;
      color:var(--text);
      background:
        radial-gradient(900px 700px at 10% 10%, rgba(59,130,246,0.18), transparent 55%),
        radial-gradient(900px 700px at 90% 20%, rgba(168,85,247,0.16), transparent 55%),
        linear-gradient(180deg, var(--bg2), var(--bg1));
      min-height:100vh;
      padding:22px;
    }
    .wrap{max-width:980px;margin:0 auto;display:flex;flex-direction:column;gap:14px;}
    .header{display:flex;align-items:flex-start;justify-content:space-between;gap:14px;}
    .title{display:flex;flex-direction:column;gap:6px;}
    h1{font-size:34px;margin:0;letter-spacing:-0.02em;}
    .subtitle{color:var(--muted);font-size:14px;}
    .badge{
      display:inline-flex;align-items:center;gap:10px;
      padding:8px 14px;border-radius:999px;
      background: rgba(255,255,255,0.06);
      border:1px solid var(--border);
      font-weight:500;
    }
    .dot{width:10px;height:10px;border-radius:999px;background:var(--good);box-shadow:0 0 18px rgba(34,197,94,0.55);animation:pulse 2s infinite;}
    .dot.offline{background:var(--bad);box-shadow:0 0 18px rgba(239,68,68,0.55);animation:none;}
    @keyframes pulse{0%,100%{transform:scale(1);opacity:1}50%{transform:scale(1.25);opacity:.7}}
    .card{
      background:var(--card);
      border:1px solid var(--border);
      border-radius:18px;
      padding:18px;
      backdrop-filter:blur(16px);
      box-shadow:0 12px 34px rgba(0,0,0,0.35);
    }
    .controls{display:flex;flex-wrap:wrap;gap:10px;align-items:center;}
    select,input{
      background: rgba(255,255,255,0.06);
      border:1px solid rgba(255,255,255,0.12);
      color:var(--text);
      padding:10px 12px;border-radius:12px;outline:none;font-size:14px;
    }
    button{
      background: rgba(255,255,255,0.10);
      border:1px solid rgba(255,255,255,0.16);
      color:var(--text);
      padding:10px 14px;border-radius:12px;font-weight:500;cursor:pointer;
    }
    button:hover{background: rgba(255,255,255,0.14);}
    .small{font-size:12px;color:var(--muted);}
    .chartTitle{
      font-size:14px;color:var(--muted);margin-bottom:10px;
      display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;
    }
    .stats{
      display:grid;
      grid-template-columns: repeat(2, 1fr);
      gap:14px;
    }
    .statTitle{color:var(--muted);font-size:14px;}
    .statValue{font-size:44px;font-weight:600;letter-spacing:-0.03em;margin-top:6px;}
    .statSub{color:var(--muted);font-size:13px;margin-top:6px;}
    .lower{
      display:grid;
      grid-template-columns: 1fr;
      gap:14px;
    }
    .alertsHeader{display:flex;justify-content:space-between;align-items:center;gap:10px;margin-bottom:10px;}
    .alertItem{
      padding:12px 12px;border-radius:14px;border:1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.05);
      margin-bottom:10px;display:flex;flex-direction:column;gap:6px;
    }
    .alertItem.acked{opacity:.55;}
    .alertTop{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;}
    .alertMetric{font-weight:600;}
    .alertTs{color:var(--muted);font-size:12px;}
    details summary{cursor:pointer;color:var(--muted);font-size:14px;margin-bottom:10px;}
    .thresholdGrid{display:grid;grid-template-columns:1fr;gap:10px;margin-top:12px;}
    .thrRow{
      display:grid;
      grid-template-columns: 1.2fr .8fr 1fr 1fr;
      gap:10px;
      align-items:center;
      padding:10px 10px;border-radius:14px;border:1px solid rgba(255,255,255,0.10);
      background: rgba(255,255,255,0.04);
    }
    .thrRow label{color:var(--muted);font-size:13px;}
    .loginRow{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:10px;}
    @media (min-width: 820px){
      .stats{grid-template-columns: repeat(4, 1fr);}
      .lower{grid-template-columns: 1.1fr 0.9fr;}
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
      <div class="badge">
        <div class="dot" id="statusDot"></div>
        <div id="statusText">LIVE</div>
      </div>
    </div>

    <div class="card">
      <div class="controls">
        <label class="small">PC</label>
        <select id="pcSelect"></select>

        <label class="small">Hours</label>
        <input id="hoursInput" type="number" min="0.25" step="0.25" value="1" style="width:90px" />

        <button onclick="refreshAll()">Refresh</button>
      </div>
      <div class="small" style="margin-top:8px;">Tip: graph is CPU + RAM (always), like your phone screenshot.</div>
    </div>

    <!-- HERO GRAPH AT TOP -->
    <div class="card">
      <div class="chartTitle">
        <div>LIVE TELEMETRY</div>
        <div class="small" id="lastUpdate"></div>
      </div>
      <canvas id="chart" height="170"></canvas>
    </div>

    <!-- STATS UNDER GRAPH -->
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

    <!-- LOWER SECTION -->
    <div class="lower">

      <div class="card">
        <div class="alertsHeader">
          <div style="font-weight:600;">Alerts</div>
          <button onclick="ackAlerts()">Ack visible</button>
        </div>
        <div id="alertsList" class="small">No alerts yet.</div>
      </div>

      <div class="card">
        <div class="loginRow">
          <input id="adminPw" type="password" placeholder="Admin password" style="flex:1;min-width:200px;">
          <button onclick="adminLogin()">Login</button>
          <button onclick="adminLogout()">Logout</button>
        </div>
        <div class="small" id="adminMsg"></div>

        <details open style="margin-top:12px;">
          <summary>Thresholds (requires admin login)</summary>
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

  </div>

<script>
  let chart;

  function fmtGB(x){
    if(x === null || x === undefined) return "—";
    return Number(x).toFixed(1) + " GB";
  }

  function initChart(){
    const ctx = document.getElementById("chart").getContext("2d");
    chart = new Chart(ctx, {
      type: "line",
      data: {
        labels: [],
        datasets: [
          {label: "CPU %", data: [], tension: 0.35, borderWidth: 2, pointRadius: 0},
          {label: "RAM %", data: [], tension: 0.35, borderWidth: 2, pointRadius: 0}
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        scales: {
          y: { min: 0, max: 100, grid: { color: "rgba(255,255,255,0.05)" } },
          x: { grid: { color: "rgba(255,255,255,0.03)" } }
        },
        plugins: { legend: { labels: { color: "rgba(226,232,240,0.75)" } } }
      }
    });
  }

  async function fetchJSON(url){
    const r = await fetch(url);
    const j = await r.json().catch(()=> ({}));
    if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
  }

  function setBadge(status, serverTs){
    const dot = document.getElementById("statusDot");
    const text = document.getElementById("statusText");
    dot.classList.remove("offline");
    if(status === "LIVE"){
      text.textContent = "LIVE";
    } else if(status === "OFFLINE"){
      dot.classList.add("offline");
      text.textContent = "OFFLINE";
    } else {
      dot.classList.add("offline");
      text.textContent = status;
    }
    document.getElementById("lastUpdate").textContent = serverTs ? ("Last update (UTC): " + serverTs) : "";
  }

  function renderStats(latest){
    const cpu = latest?.cpu?.usage_pct;
    const ram = latest?.ram?.usage_pct;
    const ramUsed = latest?.ram?.used_gb;
    const ramTotal = latest?.ram?.total_gb;

    const diskUsed = latest?.disk?.c?.used_pct;
    const diskFree = latest?.disk?.c?.free_gb;
    const diskTotal = latest?.disk?.c?.total_gb;

    document.getElementById("cpuVal").textContent = (cpu!=null ? Number(cpu).toFixed(1)+"%" : "—");
    document.getElementById("ramVal").textContent = (ram!=null ? Number(ram).toFixed(1)+"%" : "—");
    document.getElementById("ramSub").textContent =
      (ramUsed!=null && ramTotal!=null) ? (`Used: ${fmtGB(ramUsed)} / ${fmtGB(ramTotal)}`) : "Memory usage";
    document.getElementById("diskVal").textContent = (diskUsed!=null ? Number(diskUsed).toFixed(1)+"%" : "—");
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
    alerts.slice(0, 10).forEach(a=>{
      const div = document.createElement("div");
      div.className = "alertItem" + (a.acked ? " acked" : "");
      div.innerHTML = `
        <div class="alertTop">
          <div class="alertMetric">${a.metric}</div>
          <div class="alertTs">${a.ts}</div>
        </div>
        <div>${a.message}</div>
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
      const v = thr?.[k] || {};
      const row = document.createElement("div");
      row.className = "thrRow";
      row.innerHTML = `
        <label>${labels[k]}</label>
        <select data-k="${k}" data-f="enabled">
          <option value="true" ${v.enabled ? "selected":""}>On</option>
          <option value="false" ${!v.enabled ? "selected":""}>Off</option>
        </select>
        <input data-k="${k}" data-f="threshold" type="number" value="${v.threshold ?? ""}" />
        <input data-k="${k}" data-f="cooldown" type="number" value="${v.cooldown ?? 300}" />
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

  async function adminLogin(){
    const pw = document.getElementById("adminPw").value || "";
    try{
      await fetchJSON("/api/admin/login", {
        method:"POST",
        headers: {"Content-Type":"application/json"},
        body: JSON.stringify({password: pw})
      });
    }catch(e){
      // fallback for browsers without fetchJSON overload
    }
  }

  async function adminLogout(){
    await fetch("/api/admin/logout", {method:"POST"});
    document.getElementById("adminMsg").textContent = "Logged out.";
    setTimeout(()=>document.getElementById("adminMsg").textContent="", 2000);
  }

  async function saveThresholds(){
    const pc = document.getElementById("pcSelect").value;
    const thresholds = gatherThresholds();
    const r = await fetch("/api/thresholds", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({pc, thresholds})
    });
    const j = await r.json().catch(()=> ({}));
    document.getElementById("saveMsg").textContent = j.ok ? "Saved." : ("Error: " + (j.error || "unknown"));
    setTimeout(()=>document.getElementById("saveMsg").textContent="", 2000);
  }

  async function ackAlerts(){
    const pc = document.getElementById("pcSelect").value;
    const r = await fetch("/api/alerts/ack", {
      method:"POST",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({pc})
    });
    const j = await r.json().catch(()=> ({}));
    if(!j.ok){
      alert("Ack requires admin login. " + (j.error || ""));
    }
    refreshAll();
  }

  async function refreshAll(){
    const state = await fetchJSON("/api/state");
    const pcs = state.pcs || [];
    const sel = document.getElementById("pcSelect");

    if(sel.options.length === 0){
      pcs.forEach(p=>{
        const o = document.createElement("option");
        o.value = p; o.textContent = p;
        sel.appendChild(o);
      });
      if(pcs.length > 0) sel.value = pcs[0];
    } else {
      if(pcs.length > 0 && !pcs.includes(sel.value)) sel.value = pcs[0];
    }

    const pc = sel.value;
    const hours = Number(document.getElementById("hoursInput").value || 1);
    const pcState = state.pc_states?.[pc] || {};
    const latest = pcState.latest || {};

    setBadge(pcState.status || "UNKNOWN", latest._server_ts);
    renderStats(latest);
    renderAlerts(pcState.alerts || []);
    renderThresholds(pcState.thresholds || {});
    document.getElementById("rawJson").textContent = JSON.stringify(latest, null, 2);

    const hist = await fetchJSON(`/api/history?pc=${encodeURIComponent(pc)}&hours=${encodeURIComponent(hours)}`);
    const cpuPts = hist.cpu || [];
    const ramPts = hist.ram || [];

    const labels = cpuPts.map(p=>{
      const d = new Date(p.t * 1000);
      return d.toISOString().slice(11,19);
    });

    chart.data.labels = labels;
    chart.data.datasets[0].data = cpuPts.map(p=>p.v);
    chart.data.datasets[1].data = ramPts.map(p=>p.v);
    chart.update();
  }

  // small helper: allow fetchJSON with options too
  async function fetchJSON(url, opts){
    const r = await fetch(url, opts);
    const j = await r.json().catch(()=> ({}));
    if(!r.ok) throw new Error(j.error || ("HTTP " + r.status));
    return j;
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


# =========================
# BOOT
# =========================
load_thresholds()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
