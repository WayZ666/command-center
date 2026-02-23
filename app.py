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

OFFLINE_AFTER_SECONDS = int(os.environ.get("OFFLINE_AFTER_SECONDS", "30"))
DEFAULT_COOLDOWN_SECONDS = int(os.environ.get("ALERT_COOLDOWN_SECONDS", "300"))
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "7"))

app = Flask(__name__)
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

        con.execute("""
        CREATE TABLE IF NOT EXISTS alert_rules (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc TEXT NOT NULL,
            metric TEXT NOT NULL,
            op TEXT NOT NULL,
            value REAL NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            cooldown_seconds INTEGER NOT NULL DEFAULT 300,
            last_fired_ts INTEGER NOT NULL DEFAULT 0,
            created_ts INTEGER NOT NULL,
            updated_ts INTEGER NOT NULL,
            UNIQUE(pc, metric)
        )
        """)

        con.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pc TEXT NOT NULL,
            ts INTEGER NOT NULL,
            severity TEXT NOT NULL,
            metric TEXT NOT NULL,
            message TEXT NOT NULL,
            value REAL,
            threshold REAL,
            acknowledged INTEGER NOT NULL DEFAULT 0
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_alerts_pc_ts ON alerts(pc, ts)")


def prune_old(days=RETENTION_DAYS):
    cutoff = int(time.time()) - (days * 86400)
    with db_conn() as con:
        con.execute("DELETE FROM readings WHERE ts < ?", (cutoff,))
        con.execute("DELETE FROM alerts WHERE ts < ?", (cutoff,))


init_db()


# =========================
# AUTH
# =========================
def is_auth_ok(data: dict) -> bool:
    header_key = request.headers.get("X-API-KEY")
    body_key = (data or {}).get("api_key")
    return (header_key == API_KEY) or (body_key == API_KEY)


# =========================
# HELPERS
# =========================
def now_iso(ts: int) -> str:
    return datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"


def pc_online(last_ts: int) -> bool:
    return (int(time.time()) - int(last_ts)) <= OFFLINE_AFTER_SECONDS


def extract_metric(payload: dict, metric: str):
    if metric == "cpu_pct":
        return payload.get("cpu", {}).get("usage_pct")

    if metric == "ram_pct":
        return payload.get("ram", {}).get("used_pct")

    if metric == "disk_c_used_pct":
        disk = payload.get("disk", {})
        for p in disk.get("partitions", []):
            mp = str(p.get("mountpoint", "")).upper()
            if mp.startswith("C:"):
                return p.get("used_pct")
        return None

    if metric == "disk_read_mb":
        return payload.get("disk", {}).get("io", {}).get("read_mb")

    if metric == "disk_write_mb":
        return payload.get("disk", {}).get("io", {}).get("write_mb")

    if metric == "uptime_seconds":
        return payload.get("uptime", {}).get("uptime_seconds")

    return None


# =========================
# ALERTS
# =========================
def ensure_default_rules_for_pc(pc: str):
    defaults = [
        ("cpu_pct", ">=", 90.0, 1, DEFAULT_COOLDOWN_SECONDS),
        ("ram_pct", ">=", 90.0, 1, DEFAULT_COOLDOWN_SECONDS),
        ("disk_c_used_pct", ">=", 90.0, 1, DEFAULT_COOLDOWN_SECONDS),
        ("offline", ">=", 1.0, 1, DEFAULT_COOLDOWN_SECONDS),
    ]
    ts = int(time.time())
    with db_conn() as con:
        existing = con.execute("SELECT metric FROM alert_rules WHERE pc=?", (pc,)).fetchall()
        existing_metrics = {r["metric"] for r in existing}

        for metric, op, value, enabled, cooldown in defaults:
            if metric in existing_metrics:
                continue
            con.execute("""
                INSERT INTO alert_rules (pc, metric, op, value, enabled, cooldown_seconds, last_fired_ts, created_ts, updated_ts)
                VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
            """, (pc, metric, op, float(value), int(enabled), int(cooldown), ts, ts))


def fire_alert(pc: str, severity: str, metric: str, message: str, value=None, threshold=None):
    ts = int(time.time())
    with db_conn() as con:
        con.execute("""
            INSERT INTO alerts (pc, ts, severity, metric, message, value, threshold, acknowledged)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (pc, ts, severity, metric, message, value, threshold))


def evaluate_rules(pc: str, payload: dict, last_seen_ts: int):
    now = int(time.time())
    with db_conn() as con:
        rules = con.execute("""
            SELECT id, metric, op, value, enabled, cooldown_seconds, last_fired_ts
            FROM alert_rules
            WHERE pc=?
        """, (pc,)).fetchall()

        for r in rules:
            if int(r["enabled"]) != 1:
                continue

            metric = r["metric"]
            op = r["op"]
            threshold = float(r["value"])
            cooldown = int(r["cooldown_seconds"])
            last_fired = int(r["last_fired_ts"])

            if (now - last_fired) < cooldown:
                continue

            if metric == "offline":
                if not pc_online(last_seen_ts):
                    fire_alert(
                        pc=pc,
                        severity="crit",
                        metric="offline",
                        message=f"{pc} is OFFLINE (no data for > {OFFLINE_AFTER_SECONDS}s).",
                        value=float(now - last_seen_ts),
                        threshold=float(OFFLINE_AFTER_SECONDS),
                    )
                    con.execute("UPDATE alert_rules SET last_fired_ts=?, updated_ts=? WHERE id=?",
                                (now, now, int(r["id"])))
                continue

            current = extract_metric(payload, metric)
            if current is None:
                continue

            tripped = (op == ">=" and float(current) >= threshold)
            if tripped:
                severity = "crit" if (metric == "disk_c_used_pct" and threshold >= 95) or threshold >= 95 else "warn"
                label = metric.replace("_", " ").upper()
                fire_alert(
                    pc=pc,
                    severity=severity,
                    metric=metric,
                    message=f"{label} threshold hit: {current} {op} {threshold}",
                    value=float(current),
                    threshold=float(threshold),
                )
                con.execute("UPDATE alert_rules SET last_fired_ts=?, updated_ts=? WHERE id=?",
                            (now, now, int(r["id"])))


# =========================
# API ROUTES
# =========================
@app.route("/api/ingest", methods=["POST"])
def ingest():
    data = request.get_json(force=True, silent=True) or {}

    if not is_auth_ok(data):
        return jsonify({"ok": False, "error": "unauthorized"}), 401

    pc = data.get("pc_name") or data.get("pc") or data.get("notes") or "Unknown"
    ts = int(data.get("ts") or time.time())

    with db_conn() as con:
        con.execute(
            "INSERT INTO readings (pc, ts, payload) VALUES (?, ?, ?)",
            (pc, ts, json.dumps(data))
        )

    LATEST[pc] = {"ts": ts, "payload": data}

    ensure_default_rules_for_pc(pc)
    evaluate_rules(pc, data, ts)

    if ts % 300 < 2:
        try:
            prune_old(days=RETENTION_DAYS)
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
            "last_iso": now_iso(last_ts),
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

    return jsonify({"pc": pc, "metric": metric, "hours": hours, "points": points})


@app.route("/api/rules", methods=["GET"])
def get_rules():
    pc = request.args.get("pc", "")
    if not pc:
        return jsonify({"ok": False, "error": "pc required"}), 400

    ensure_default_rules_for_pc(pc)
    with db_conn() as con:
        rows = con.execute("""
            SELECT metric, op, value, enabled, cooldown_seconds, last_fired_ts, updated_ts
            FROM alert_rules
            WHERE pc=?
            ORDER BY metric
        """, (pc,)).fetchall()

    rules = [{
        "metric": r["metric"],
        "op": r["op"],
        "value": float(r["value"]),
        "enabled": bool(r["enabled"]),
        "cooldown_seconds": int(r["cooldown_seconds"]),
        "last_fired_ts": int(r["last_fired_ts"]),
        "updated_ts": int(r["updated_ts"]),
    } for r in rows]

    return jsonify({"ok": True, "pc": pc, "rules": rules})


@app.route("/api/rules", methods=["POST"])
def upsert_rule():
    data = request.get_json(force=True, silent=True) or {}
    pc = data.get("pc")
    metric = data.get("metric")
    value = data.get("value")
    enabled = data.get("enabled", True)
    cooldown_seconds = data.get("cooldown_seconds", DEFAULT_COOLDOWN_SECONDS)

    if not pc or not metric or value is None:
        return jsonify({"ok": False, "error": "pc, metric, value required"}), 400

    ts = int(time.time())
    with db_conn() as con:
        con.execute("""
            INSERT INTO alert_rules (pc, metric, op, value, enabled, cooldown_seconds, last_fired_ts, created_ts, updated_ts)
            VALUES (?, ?, '>=', ?, ?, ?, 0, ?, ?)
            ON CONFLICT(pc, metric) DO UPDATE SET
                value=excluded.value,
                enabled=excluded.enabled,
                cooldown_seconds=excluded.cooldown_seconds,
                updated_ts=excluded.updated_ts
        """, (pc, metric, float(value), int(bool(enabled)), int(cooldown_seconds), ts, ts))

    return jsonify({"ok": True})


@app.route("/api/alerts", methods=["GET"])
def get_alerts():
    pc = request.args.get("pc", "")
    limit = int(request.args.get("limit", "50"))
    only_unacked = request.args.get("unacked", "0") == "1"

    q = "SELECT id, pc, ts, severity, metric, message, value, threshold, acknowledged FROM alerts"
    params = []
    where = []
    if pc:
        where.append("pc=?")
        params.append(pc)
    if only_unacked:
        where.append("acknowledged=0")
    if where:
        q += " WHERE " + " AND ".join(where)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)

    with db_conn() as con:
        rows = con.execute(q, tuple(params)).fetchall()

    alerts = [{
        "id": int(r["id"]),
        "pc": r["pc"],
        "ts": int(r["ts"]),
        "iso": now_iso(int(r["ts"])),
        "severity": r["severity"],
        "metric": r["metric"],
        "message": r["message"],
        "value": r["value"],
        "threshold": r["threshold"],
        "acknowledged": bool(r["acknowledged"]),
    } for r in rows]

    return jsonify({"ok": True, "alerts": alerts})


@app.route("/api/alerts/ack", methods=["POST"])
def ack_alert():
    data = request.get_json(force=True, silent=True) or {}
    alert_id = data.get("id")
    if not alert_id:
        return jsonify({"ok": False, "error": "id required"}), 400

    with db_conn() as con:
        con.execute("UPDATE alerts SET acknowledged=1 WHERE id=?", (int(alert_id),))
    return jsonify({"ok": True})


# =========================
# UI
# =========================
@app.route("/", methods=["GET"])
def dashboard():
    html = """
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Command Center</title>
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
  <style>
    :root {
      --bg: #0b0f19;
      --panel: rgba(255,255,255,0.06);
      --border: rgba(255,255,255,0.12);
      --text: rgba(255,255,255,0.92);
      --muted: rgba(255,255,255,0.65);
      --warn: rgba(255,200,90,0.22);
      --crit: rgba(255,90,90,0.22);
      --chip: rgba(255,255,255,0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial;
      background: radial-gradient(1200px 600px at 20% -10%, rgba(90,120,255,0.25), transparent),
                  radial-gradient(900px 500px at 90% 0%, rgba(255,120,210,0.18), transparent),
                  var(--bg);
      color: var(--text);
    }
    .wrap { max-width: 1200px; margin: 0 auto; padding: 18px; }
    .topbar {
      display:flex; gap: 14px; align-items:center; justify-content: space-between;
      padding: 14px 16px; border: 1px solid var(--border); border-radius: 16px;
      background: linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.04));
      backdrop-filter: blur(10px);
      flex-wrap: wrap;
    }
    .brand h1 { font-size: 18px; margin: 0; letter-spacing: 0.4px; }
    .brand p { margin: 2px 0 0; color: var(--muted); font-size: 12px; }
    .controls { display:flex; flex-wrap: wrap; gap: 10px; align-items:flex-end; }
    label { font-size: 11px; color: var(--muted); display:block; margin: 0 0 6px; }
    select, input {
      background: rgba(255,255,255,0.06);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      outline: none;
      min-width: 160px;
    }
    input[type="number"] { min-width: 110px; }
    button {
      background: rgba(255,255,255,0.10);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 10px 12px;
      cursor: pointer;
    }
    button:hover { background: rgba(255,255,255,0.14); }

    .grid {
      margin-top: 14px;
      display: grid;
      grid-template-columns: 1.4fr 1fr;
      gap: 14px;
    }
    @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }

    .panel {
      border: 1px solid var(--border);
      border-radius: 16px;
      background: var(--panel);
      padding: 14px;
      backdrop-filter: blur(10px);
    }
    .panel h2 { margin: 0 0 10px; font-size: 14px; }
    .row {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 12px;
    }
    @media (max-width: 980px) { .row { grid-template-columns: 1fr; } }

    .card {
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      background: rgba(255,255,255,0.05);
    }
    .metric { display:flex; justify-content:space-between; gap: 10px; }
    .metric .k { color: var(--muted); font-size: 12px; }
    .metric .v { font-size: 18px; font-weight: 700; }
    .sub { color: var(--muted); font-size: 12px; margin-top: 4px; }

    .chip {
      display:inline-flex; align-items:center; gap: 8px;
      border: 1px solid var(--border);
      background: var(--chip);
      border-radius: 999px;
      padding: 8px 10px;
      font-size: 12px;
    }
    .dot {
      width: 9px; height: 9px; border-radius: 99px;
      background: rgba(255,255,255,0.35);
      box-shadow: 0 0 0 3px rgba(255,255,255,0.06);
    }
    .dot.live { background: rgba(70,255,140,0.85); }
    .dot.off { background: rgba(255,90,90,0.85); }

    canvas { width: 100% !important; height: 300px !important; }
    pre {
      white-space: pre-wrap;
      background: rgba(0,0,0,0.25);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 12px;
      color: rgba(255,255,255,0.82);
      max-height: 360px;
      overflow: auto;
    }

    .alerts { display:flex; flex-direction: column; gap: 10px; max-height: 320px; overflow:auto; padding-right: 6px; }
    .alert { border: 1px solid var(--border); border-radius: 14px; padding: 10px 12px; background: rgba(255,255,255,0.05); }
    .alert.warn { background: var(--warn); }
    .alert.crit { background: var(--crit); }
    .alert .top { display:flex; justify-content:space-between; align-items:center; gap: 10px; font-size: 12px; }
    .alert .msg { margin-top: 6px; font-size: 13px; }

    .ruleRow {
      display:grid;
      grid-template-columns: 1.1fr 0.9fr 0.9fr 0.9fr;
      gap: 10px;
      align-items:center;
    }
    @media (max-width: 980px) { .ruleRow { grid-template-columns: 1fr; } }
    .muted { color: var(--muted); font-size: 12px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="brand">
        <h1>Command Center</h1>
        <p>Disk metrics • history graphs • alert thresholds</p>
      </div>

      <div class="controls">
        <div>
          <label>PC</label>
          <select id="pcSelect"></select>
        </div>
        <div>
          <label>History metric</label>
          <select id="metricSelect">
            <option value="cpu_pct">CPU %</option>
            <option value="ram_pct">RAM %</option>
            <option value="disk_c_used_pct">Disk C: Used %</option>
            <option value="disk_read_mb">Disk Read MB (total)</option>
            <option value="disk_write_mb">Disk Write MB (total)</option>
          </select>
        </div>
        <div>
          <label>Hours</label>
          <input id="hoursInput" type="number" value="24" min="1" max="168" />
        </div>
        <div>
          <label>Status</label>
          <div id="statusChip" class="chip"><span class="dot"></span><span>Loading…</span></div>
        </div>
        <div>
          <button id="refreshBtn">Refresh</button>
        </div>
      </div>
    </div>

    <div class="grid">
      <div class="panel">
        <h2>Live overview</h2>
        <div class="row">
          <div class="card">
            <div class="metric"><span class="k">CPU</span><span class="v" id="cpuV">—</span></div>
            <div class="sub" id="cpuS">—</div>
          </div>
          <div class="card">
            <div class="metric"><span class="k">RAM</span><span class="v" id="ramV">—</span></div>
            <div class="sub" id="ramS">—</div>
          </div>
          <div class="card">
            <div class="metric"><span class="k">Disk C:</span><span class="v" id="diskV">—</span></div>
            <div class="sub" id="diskS">—</div>
          </div>
        </div>

        <div style="margin-top: 12px;">
          <h2>Historical graph</h2>
          <canvas id="chart"></canvas>
          <div class="muted" id="chartNote" style="margin-top:8px;">—</div>
        </div>
      </div>

      <div class="panel">
        <h2>Alerts</h2>
        <div style="display:flex; gap:10px; align-items:center; justify-content:space-between; margin-bottom:10px;">
          <div class="muted">Latest threshold trips</div>
          <button id="ackAllBtn">Ack visible</button>
        </div>
        <div class="alerts" id="alertsBox"></div>

        <div style="margin-top: 14px;">
          <h2>Thresholds</h2>
          <div class="muted" style="margin-bottom:10px;">Set thresholds per PC (cooldown prevents spam).</div>
          <div id="rulesBox" style="display:grid; gap:10px;"></div>
          <button style="margin-top:10px;" id="saveRulesBtn">Save thresholds</button>
          <div class="muted" id="rulesSaved" style="margin-top:8px;"></div>
        </div>

        <div style="margin-top: 14px;">
          <h2>Live JSON</h2>
          <pre id="liveJson"></pre>
        </div>
      </div>
    </div>
  </div>

<script>
let chart;

function fmtPct(v){ if(v===null||v===undefined) return "—"; return `${Number(v).toFixed(1)}%`; }
function fmtGB(v){ if(v===null||v===undefined) return "—"; return `${Number(v).toFixed(2)} GB`; }
function tsToLabel(ts){ return new Date(ts*1000).toLocaleTimeString(); }

function setStatus(online, lastIso){
  const chip = document.getElementById("statusChip");
  chip.innerHTML = `<span class="dot ${online ? "live" : "off"}"></span><span>${online ? "LIVE" : "OFFLINE"} • ${lastIso}</span>`;
}

async function fetchLatest(){ return (await fetch("/api/latest")).json(); }
async function fetchHistory(pc, metric, hours){ return (await fetch(`/api/history?pc=${encodeURIComponent(pc)}&metric=${encodeURIComponent(metric)}&hours=${hours}`)).json(); }
async function fetchRules(pc){ return (await fetch(`/api/rules?pc=${encodeURIComponent(pc)}`)).json(); }
async function saveRule(pc, metric, value, enabled, cooldown_seconds){
  await fetch("/api/rules", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({pc, metric, value, enabled, cooldown_seconds})});
}
async function fetchAlerts(pc){ return (await fetch(`/api/alerts?pc=${encodeURIComponent(pc)}&limit=30&unacked=0`)).json(); }
async function ackAlert(id){ await fetch("/api/alerts/ack", {method:"POST", headers:{"Content-Type":"application/json"}, body: JSON.stringify({id})}); }

function ensureChart(labels, values, metric){
  const ctx = document.getElementById("chart").getContext("2d");
  if(!chart){
    chart = new Chart(ctx, {type:"line", data:{labels, datasets:[{label:metric, data:values, tension:0.2}]}, options:{responsive:true, animation:false, scales:{y:{beginAtZero:true}}}});
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].label = metric;
    chart.data.datasets[0].data = values;
    chart.update();
  }
}

function getDiskC(payload){
  const parts = (payload?.disk?.partitions) || [];
  for(const p of parts){
    const mp = String(p.mountpoint||"").toUpperCase();
    if(mp.startsWith("C:")) return p;
  }
  return null;
}

function renderRules(rules){
  const box = document.getElementById("rulesBox");
  box.innerHTML = "";

  const order = ["cpu_pct","ram_pct","disk_c_used_pct","offline"];
  rules.sort((a,b)=>order.indexOf(a.metric)-order.indexOf(b.metric));

  for(const r of rules){
    const row = document.createElement("div");
    row.className = "card ruleRow";
    row.innerHTML = `
      <div>
        <div class="muted">Metric</div>
        <div style="font-weight:700;">${r.metric}</div>
      </div>
      <div>
        <div class="muted">Enabled</div>
        <select data-metric="${r.metric}" data-field="enabled">
          <option value="true" ${r.enabled ? "selected":""}>On</option>
          <option value="false" ${!r.enabled ? "selected":""}>Off</option>
        </select>
      </div>
      <div>
        <div class="muted">Threshold</div>
        <input type="number" step="0.1" data-metric="${r.metric}" data-field="value" value="${r.value}" />
      </div>
      <div>
        <div class="muted">Cooldown (s)</div>
        <input type="number" step="1" min="0" data-metric="${r.metric}" data-field="cooldown_seconds" value="${r.cooldown_seconds}" />
      </div>
    `;
    box.appendChild(row);
  }
}

function readRuleInputs(){
  const fields = document.querySelectorAll("[data-metric][data-field]");
  const map = {};
  fields.forEach(el=>{
    const metric = el.getAttribute("data-metric");
    const field = el.getAttribute("data-field");
    if(!map[metric]) map[metric] = {};
    if(field==="enabled") map[metric][field] = (el.value==="true");
    else map[metric][field] = Number(el.value);
  });
  return Object.keys(map).map(metric=>({metric, ...map[metric]}));
}

function renderAlerts(alerts){
  const box = document.getElementById("alertsBox");
  box.innerHTML = "";
  if(!alerts.length){
    box.innerHTML = `<div class="muted">No alerts yet.</div>`;
    return;
  }
  for(const a of alerts){
    const div = document.createElement("div");
    div.className = `alert ${a.severity || "warn"}`;
    div.innerHTML = `
      <div class="top">
        <div><strong>${a.severity.toUpperCase()}</strong> • ${a.metric} • <span class="muted">${a.iso}</span></div>
        <div><button data-ack="${a.id}">${a.acknowledged ? "Acked" : "Ack"}</button></div>
      </div>
      <div class="msg">${a.message}</div>
      <div class="muted" style="margin-top:6px;">
        ${a.value !== null && a.value !== undefined ? "Value: " + a.value : ""}
        ${a.threshold !== null && a.threshold !== undefined ? " • Threshold: " + a.threshold : ""}
      </div>
    `;
    box.appendChild(div);
  }
  box.querySelectorAll("button[data-ack]").forEach(btn=>{
    btn.addEventListener("click", async ()=>{
      const id = Number(btn.getAttribute("data-ack"));
      await ackAlert(id);
      await refreshAll();
    });
  });
}

async function populatePCs(latest){
  const select = document.getElementById("pcSelect");
  const pcs = Object.keys(latest).sort();
  select.innerHTML = "";
  if(pcs.length===0){
    const opt = document.createElement("option");
    opt.value="";
    opt.textContent="No PCs yet";
    select.appendChild(opt);
    return;
  }
  for(const pc of pcs){
    const opt = document.createElement("option");
    opt.value=pc;
    opt.textContent=pc;
    select.appendChild(opt);
  }
}

async function refreshAll(){
  const latest = await fetchLatest();
  await populatePCs(latest);

  const pc = document.getElementById("pcSelect").value;
  const metric = document.getElementById("metricSelect").value;
  const hours = document.getElementById("hoursInput").value || 24;

  if(!pc || !latest[pc]){
    document.getElementById("liveJson").textContent = "";
    setStatus(false, "No telemetry yet");
    return;
  }

  const info = latest[pc];
  setStatus(info.online, info.last_iso);

  const p = info.payload || {};
  document.getElementById("liveJson").textContent = JSON.stringify(p, null, 2);

  const cpuPct = p?.cpu?.usage_pct;
  const ramPct = p?.ram?.used_pct;
  const ramUsed = p?.ram?.used_gb;
  const ramTotal = p?.ram?.total_gb;

  const diskC = getDiskC(p);
  const diskPct = diskC?.used_pct;
  const diskFree = diskC?.free_gb;
  const diskTotal = diskC?.total_gb;

  document.getElementById("cpuV").textContent = fmtPct(cpuPct);
  document.getElementById("cpuS").textContent = `Cores: ${p?.cpu?.cores_logical ?? "—"} • Freq: ${p?.cpu?.freq_mhz ? Math.round(p.cpu.freq_mhz) + " MHz" : "—"}`;

  document.getElementById("ramV").textContent = fmtPct(ramPct);
  document.getElementById("ramS").textContent = `Used: ${fmtGB(ramUsed)} / ${fmtGB(ramTotal)}`;

  document.getElementById("diskV").textContent = fmtPct(diskPct);
  document.getElementById("diskS").textContent = `Free: ${fmtGB(diskFree)} / ${fmtGB(diskTotal)}`;

  const hist = await fetchHistory(pc, metric, hours);
  ensureChart(hist.points.map(pt=>tsToLabel(pt.ts)), hist.points.map(pt=>pt.value), metric);
  document.getElementById("chartNote").textContent = `PC: ${pc} • Metric: ${metric} • Points: ${hist.points.length} • Window: ${hours}h`;

  const alertsRes = await fetchAlerts(pc);
  renderAlerts(alertsRes.alerts || []);

  const rulesRes = await fetchRules(pc);
  if(rulesRes.ok) renderRules(rulesRes.rules || []);
}

document.getElementById("refreshBtn").addEventListener("click", refreshAll);
document.getElementById("pcSelect").addEventListener("change", refreshAll);
document.getElementById("metricSelect").addEventListener("change", refreshAll);
document.getElementById("hoursInput").addEventListener("change", refreshAll);

document.getElementById("saveRulesBtn").addEventListener("click", async ()=>{
  const pc = document.getElementById("pcSelect").value;
  if(!pc) return;

  const edits = readRuleInputs();
  for(const e of edits){
    await saveRule(pc, e.metric, e.value, e.enabled, e.cooldown_seconds);
  }
  document.getElementById("rulesSaved").textContent = "Saved. Thresholds apply on next ingests.";
  setTimeout(()=>document.getElementById("rulesSaved").textContent="", 2500);
  await refreshAll();
});

document.getElementById("ackAllBtn").addEventListener("click", async ()=>{
  const pc = document.getElementById("pcSelect").value;
  const alertsRes = await fetchAlerts(pc);
  const alerts = alertsRes.alerts || [];
  for(const a of alerts){ if(!a.acknowledged) await ackAlert(a.id); }
  await refreshAll();
});

refreshAll();
setInterval(refreshAll, 5000);
</script>
</body>
</html>
"""
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
