#!/usr/bin/env python3
"""C2 MONITOR — real-time SSH dashboard. stdlib only."""

import http.server
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
NODES_PATH = os.path.join(SCRIPT_DIR, "nodes.json")
PORT = 8080
POLL_INTERVAL = 2

# ─── state ───────────────────────────────────────────────────────────
lock = threading.Lock()
stats: dict = {}  # name -> metrics dict
prev_net: dict = {}  # name -> {rx, tx, ts}


def load_nodes():
    with open(NODES_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ─── SSH collection ──────────────────────────────────────────────────
REMOTE_CMD = (
    "cat /proc/stat;"
    "echo '___MEMINFO___';"
    "cat /proc/meminfo;"
    "echo '___NETDEV___';"
    "cat /proc/net/dev;"
    "echo '___LOADAVG___';"
    "cat /proc/loadavg;"
    "echo '___UPTIME___';"
    "cat /proc/uptime;"
    "echo '___DF___';"
    "df -B1 /;"
    "echo '___SS___';"
    "ss -tun 2>/dev/null | tail -n +2 | wc -l;"
    "echo '___PS___';"
    "ps aux --no-headers 2>/dev/null | wc -l"
)


def ssh_collect(node: dict):
    cmd = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        "-p", str(node["port"]),
        "-i", node["key"],
        f"{node['user']}@{node['ip']}",
        REMOTE_CMD,
    ]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        try:
            stdout, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return None
        if proc.returncode != 0:
            return None
        return stdout
    except Exception:
        return None


def parse_cpu(block: str) -> float:
    for line in block.splitlines():
        if line.startswith("cpu "):
            parts = line.split()[1:]
            vals = [int(x) for x in parts]
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            total = sum(vals)
            if total == 0:
                return 0.0
            return round((1 - idle / total) * 100, 1)
    return 0.0


def parse_meminfo(block: str) -> dict:
    d = {}
    for line in block.splitlines():
        m = re.match(r"^(\w+):\s+(\d+)\s+kB", line)
        if m:
            d[m.group(1)] = int(m.group(2)) * 1024
    total = d.get("MemTotal", 0)
    avail = d.get("MemAvailable", d.get("MemFree", 0))
    used = total - avail
    pct = round(used / total * 100, 1) if total else 0.0
    return {"total": total, "used": used, "free": avail, "pct": pct}


def parse_netdev(block: str) -> tuple:
    rx_total = 0
    tx_total = 0
    for line in block.splitlines():
        if ":" not in line or "Inter" in line or "face" in line:
            continue
        iface, rest = line.split(":", 1)
        iface = iface.strip()
        if iface == "lo":
            continue
        cols = rest.split()
        if len(cols) >= 9:
            rx_total += int(cols[0])
            tx_total += int(cols[8])
    return rx_total, tx_total


def parse_df(block: str) -> dict:
    lines = [l for l in block.strip().splitlines() if not l.startswith("Filesystem")]
    if not lines:
        return {"total": 0, "used": 0, "free": 0, "pct": 0.0}
    parts = lines[0].split()
    total = int(parts[1])
    used = int(parts[2])
    free = int(parts[3])
    pct = round(used / total * 100, 1) if total else 0.0
    return {"total": total, "used": used, "free": free, "pct": pct}


def parse_uptime(block: str) -> float:
    parts = block.strip().split()
    return float(parts[0]) if parts else 0.0


def parse_loadavg(block: str) -> list:
    parts = block.strip().split()
    return [float(parts[i]) for i in range(3)] if len(parts) >= 3 else [0, 0, 0]


def split_sections(raw: str) -> dict:
    sections = {}
    current_key = "__STAT__"
    current_lines = []
    for line in raw.splitlines():
        if line.startswith("___") and line.endswith("___"):
            sections[current_key] = "\n".join(current_lines)
            current_key = line.strip("_")
            current_lines = []
        else:
            current_lines.append(line)
    sections[current_key] = "\n".join(current_lines)
    return sections


def collect_node(node: dict):
    name = node["name"]
    raw = ssh_collect(node)
    ts = time.time()

    if raw is None:
        with lock:
            stats[name] = {
                "name": name,
                "ip": node["ip"],
                "online": False,
                "ts": ts,
            }
        return

    sec = split_sections(raw)

    cpu = parse_cpu(sec.get("__STAT__", ""))
    mem = parse_meminfo(sec.get("MEMINFO", ""))
    rx, tx = parse_netdev(sec.get("NETDEV", ""))
    disk = parse_df(sec.get("DF", ""))
    uptime_s = parse_uptime(sec.get("UPTIME", ""))
    loadavg = parse_loadavg(sec.get("LOADAVG", ""))

    ss_raw = sec.get("SS", "").strip()
    connections = int(ss_raw) if ss_raw.isdigit() else 0
    ps_raw = sec.get("PS", "").strip()
    processes = int(ps_raw) if ps_raw.isdigit() else 0

    # bandwidth delta
    rx_s = 0.0
    tx_s = 0.0
    with lock:
        prev = prev_net.get(name)
        if prev:
            dt = ts - prev["ts"]
            if dt > 0:
                rx_s = (rx - prev["rx"]) / dt
                tx_s = (tx - prev["tx"]) / dt
                if rx_s < 0:
                    rx_s = 0
                if tx_s < 0:
                    tx_s = 0
        prev_net[name] = {"rx": rx, "tx": tx, "ts": ts}

    entry = {
        "name": name,
        "ip": node["ip"],
        "online": True,
        "ts": ts,
        "cpu": cpu,
        "mem": mem,
        "disk": disk,
        "rx_total": rx,
        "tx_total": tx,
        "rx_s": round(rx_s, 1),
        "tx_s": round(tx_s, 1),
        "uptime": uptime_s,
        "processes": processes,
        "connections": connections,
        "loadavg": loadavg,
    }
    with lock:
        stats[name] = entry


# ─── collector loop ──────────────────────────────────────────────────
def collector_loop(node: dict):
    while True:
        try:
            collect_node(node)
        except Exception:
            pass
        time.sleep(POLL_INTERVAL)


# ─── HTTP handler ────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>C2 MONITOR</title>
<style>
:root {
  --bg: #1a1a2e;
  --bg2: #16213e;
  --bg3: #0f3460;
  --text: #e0e0e0;
  --green: #00e676;
  --red: #ff1744;
  --yellow: #ffd600;
  --cyan: #00e5ff;
  --bar-bg: #2a2a4a;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { background:var(--bg); color:var(--text); font-family:'Segoe UI',system-ui,sans-serif; font-size:14px; min-height:100vh; display:flex; flex-direction:column; }
header { background:var(--bg2); padding:16px 24px; display:flex; align-items:center; justify-content:space-between; border-bottom:1px solid var(--bg3); }
header h1 { font-size:22px; font-weight:700; letter-spacing:2px; color:var(--cyan); }
.header-info { display:flex; gap:24px; font-size:13px; color:#999; }
.header-info .online-count span { color:var(--green); font-weight:600; }
main { flex:1; padding:20px 24px; }
.grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(380px, 1fr)); gap:16px; }
.card { background:var(--bg2); border-radius:10px; padding:18px; border:1px solid var(--bg3); transition:border-color .2s; }
.card.offline { opacity:.6; border-color:var(--red); }
.card-header { display:flex; align-items:center; justify-content:space-between; margin-bottom:14px; }
.card-title { font-size:16px; font-weight:600; }
.card-ip { font-size:12px; color:#888; margin-left:8px; }
.status-dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
.status-dot.on { background:var(--green); box-shadow:0 0 6px var(--green); }
.status-dot.off { background:var(--red); box-shadow:0 0 6px var(--red); }
.metric { margin-bottom:10px; }
.metric-label { font-size:11px; color:#999; text-transform:uppercase; letter-spacing:1px; margin-bottom:3px; }
.metric-value { font-size:14px; font-weight:500; }
.bar-container { height:8px; background:var(--bar-bg); border-radius:4px; overflow:hidden; margin-top:3px; }
.bar { height:100%; border-radius:4px; transition:width .4s ease; }
.bar.cpu { background:linear-gradient(90deg, var(--green), var(--yellow), var(--red)); }
.bar.mem { background:var(--cyan); }
.bar.disk { background:var(--yellow); }
.row { display:flex; gap:16px; flex-wrap:wrap; }
.row .metric { flex:1; min-width:120px; }
.net-row { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
.net-box { background:var(--bg); border-radius:6px; padding:8px 10px; text-align:center; }
.net-box .rate { font-size:18px; font-weight:700; }
.net-box .rate.up { color:var(--red); }
.net-box .rate.down { color:var(--green); }
.net-box .total { font-size:11px; color:#888; margin-top:2px; }
.net-box .label { font-size:10px; color:#666; text-transform:uppercase; letter-spacing:1px; }
.small-metrics { display:grid; grid-template-columns:repeat(3, 1fr); gap:8px; margin-top:6px; }
.sm { background:var(--bg); border-radius:6px; padding:6px 8px; text-align:center; }
.sm .sv { font-size:15px; font-weight:600; color:var(--cyan); }
.sm .sl { font-size:10px; color:#666; margin-top:2px; }
footer { background:var(--bg2); padding:14px 24px; border-top:1px solid var(--bg3); display:flex; justify-content:center; gap:40px; font-size:13px; }
footer .ft-label { color:#666; }
footer .ft-value { color:var(--cyan); font-weight:600; margin-left:6px; }
.offline-msg { text-align:center; color:var(--red); padding:30px 0; font-size:14px; }
</style>
</head>
<body>
<header>
  <h1>C2 MONITOR</h1>
  <div class="header-info">
    <div class="online-count">Nodes: <span id="nodeCount">0/0</span></div>
    <div>Last update: <span id="lastUpdate">—</span></div>
  </div>
</header>
<main><div class="grid" id="grid"></div></main>
<footer>
  <div><span class="ft-label">Total Upload:</span><span class="ft-value" id="ftUpTotal">—</span></div>
  <div><span class="ft-label">Total Download:</span><span class="ft-value" id="ftDnTotal">—</span></div>
  <div><span class="ft-label">Realtime Up:</span><span class="ft-value" id="ftUpRate">—</span></div>
  <div><span class="ft-label">Realtime Down:</span><span class="ft-value" id="ftDnRate">—</span></div>
</footer>
<script>
function fmtBytes(b) {
  if (b < 1024) return b.toFixed(0) + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b/1048576).toFixed(2) + ' MB';
  return (b/1073741824).toFixed(2) + ' GB';
}
function fmtRate(bps) {
  if (bps < 1024) return bps.toFixed(0) + ' B/s';
  if (bps < 1048576) return (bps/1024).toFixed(1) + ' KB/s';
  return (bps/1048576).toFixed(2) + ' MB/s';
}
function fmtUptime(secs) {
  let d = Math.floor(secs/86400);
  let h = Math.floor((secs%86400)/3600);
  let m = Math.floor((secs%3600)/60);
  let parts = [];
  if (d) parts.push(d+'d');
  if (h) parts.push(h+'h');
  parts.push(m+'m');
  return parts.join(' ');
}
function barColor(pct) {
  if (pct < 60) return 'var(--green)';
  if (pct < 85) return 'var(--yellow)';
  return 'var(--red)';
}
function renderCard(s) {
  if (!s.online) {
    return `<div class="card offline">
      <div class="card-header">
        <div><span class="status-dot off"></span> <span class="card-title">${s.name}</span><span class="card-ip">${s.ip}</span></div>
      </div>
      <div class="offline-msg">OFFLINE</div>
    </div>`;
  }
  return `<div class="card">
    <div class="card-header">
      <div><span class="status-dot on"></span> <span class="card-title">${s.name}</span><span class="card-ip">${s.ip}</span></div>
    </div>
    <div class="metric">
      <div class="metric-label">CPU</div>
      <div class="metric-value">${s.cpu}%</div>
      <div class="bar-container"><div class="bar" style="width:${s.cpu}%;background:${barColor(s.cpu)}"></div></div>
    </div>
    <div class="metric">
      <div class="metric-label">RAM — ${fmtBytes(s.mem.used)} / ${fmtBytes(s.mem.total)}</div>
      <div class="metric-value">${s.mem.pct}%</div>
      <div class="bar-container"><div class="bar mem" style="width:${s.mem.pct}%"></div></div>
    </div>
    <div class="metric">
      <div class="metric-label">Disk — ${fmtBytes(s.disk.used)} / ${fmtBytes(s.disk.total)}</div>
      <div class="metric-value">${s.disk.pct}%</div>
      <div class="bar-container"><div class="bar disk" style="width:${s.disk.pct}%"></div></div>
    </div>
    <div class="net-row">
      <div class="net-box">
        <div class="label">Upload</div>
        <div class="rate up">${fmtRate(s.tx_s)}</div>
        <div class="total">Total: ${fmtBytes(s.tx_total)}</div>
      </div>
      <div class="net-box">
        <div class="label">Download</div>
        <div class="rate down">${fmtRate(s.rx_s)}</div>
        <div class="total">Total: ${fmtBytes(s.rx_total)}</div>
      </div>
    </div>
    <div class="small-metrics">
      <div class="sm"><div class="sv">${fmtUptime(s.uptime)}</div><div class="sl">Uptime</div></div>
      <div class="sm"><div class="sv">${s.processes}</div><div class="sl">Processes</div></div>
      <div class="sm"><div class="sv">${s.connections}</div><div class="sl">Connections</div></div>
    </div>
    <div class="small-metrics" style="margin-top:6px">
      <div class="sm"><div class="sv">${s.loadavg[0]}</div><div class="sl">Load 1m</div></div>
      <div class="sm"><div class="sv">${s.loadavg[1]}</div><div class="sl">Load 5m</div></div>
      <div class="sm"><div class="sv">${s.loadavg[2]}</div><div class="sl">Load 15m</div></div>
    </div>
  </div>`;
}
async function poll() {
  try {
    let r = await fetch('/api/stats');
    let data = await r.json();
    let nodes = data.nodes || [];
    let online = nodes.filter(n => n.online).length;
    document.getElementById('nodeCount').textContent = online + '/' + nodes.length;
    document.getElementById('lastUpdate').textContent = new Date().toLocaleTimeString();
    document.getElementById('grid').innerHTML = nodes.map(renderCard).join('');
    let tUp = 0, tDn = 0, rUp = 0, rDn = 0;
    nodes.forEach(n => {
      if (!n.online) return;
      tUp += n.tx_total || 0;
      tDn += n.rx_total || 0;
      rUp += n.tx_s || 0;
      rDn += n.rx_s || 0;
    });
    document.getElementById('ftUpTotal').textContent = fmtBytes(tUp);
    document.getElementById('ftDnTotal').textContent = fmtBytes(tDn);
    document.getElementById('ftUpRate').textContent = fmtRate(rUp);
    document.getElementById('ftDnRate').textContent = fmtRate(rDn);
  } catch(e) {}
}
poll();
setInterval(poll, 2000);
</script>
</body>
</html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # silence request logs

    def do_GET(self):
        if self.path == "/api/stats":
            with lock:
                data = {"nodes": list(stats.values())}
            payload = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        elif self.path == "/" or self.path == "/index.html":
            payload = DASHBOARD_HTML.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_error(404)


def main():
    nodes = load_nodes()
    print(f"[C2 MONITOR] Loaded {len(nodes)} nodes from {NODES_PATH}")

    for node in nodes:
        t = threading.Thread(target=collector_loop, args=(node,), daemon=True)
        t.start()
        print(f"  -> collector started: {node['name']} ({node['ip']})")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[C2 MONITOR] Dashboard live at http://localhost:{PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[C2 MONITOR] Shutting down.")
        server.shutdown()


if __name__ == "__main__":
    main()
