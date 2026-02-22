"""
M2O Fleet Command — Animated Fleet Visualization
Real-time force graph of the Machine.Machine agent fleet
"""

import asyncio
import json
import time
import os
import uuid
from datetime import datetime, timezone
from typing import Set

import redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

REDIS_HOST = os.getenv("FLEET_REDIS_HOST", "fleet-redis")
REDIS_PORT = int(os.getenv("FLEET_REDIS_PORT", "6379"))
HEARTBEAT_TTL = int(os.getenv("HEARTBEAT_TTL", "180"))

app = FastAPI(title="M2O Fleet Command", docs_url=None, redoc_url=None)

ws_clients: Set[WebSocket] = set()


def get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def fmt_age(s):
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s//60}m"
    return f"{s//3600}h"


def get_fleet_data():
    try:
        r = get_redis()
        now = int(time.time())
        agents_raw = r.smembers("agents") or set()
        agents = []
        for aid in sorted(agents_raw):
            data = r.hgetall(f"agent:{aid}") or {}
            last_seen = int(data.get("lastSeen", 0))
            age = now - last_seen if last_seen else 9999
            if age < HEARTBEAT_TTL:
                status = "alive"
            elif age < HEARTBEAT_TTL * 2:
                status = "degraded"
            else:
                status = "dead"
            try:
                health = json.loads(data.get("health", "{}"))
            except Exception:
                health = {}
            agents.append({
                "id": aid,
                "status": status,
                "preset": data.get("preset", "unknown"),
                "host": data.get("host", "?"),
                "last_seen": last_seen,
                "age_s": age,
                "age_str": fmt_age(age),
                "cpu": health.get("cpu", None),
                "mem": health.get("mem", None),
                "gateway": health.get("gateway", None),
                "current_task": data.get("currentTask", None),
            })

        raw_events = r.xrevrange("events", count=30) or []
        events = []
        for eid, data in raw_events:
            ts = int(eid.split("-")[0]) // 1000
            events.append({
                "ts": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S"),
                "agent": data.get("agent", "fleet"),
                "type": data.get("type", "?"),
                "payload": data.get("payload", ""),
                "task_id": data.get("task_id", ""),
            })

        # Tasks
        task_types = ["research", "build", "plan", "code-review", "generic"]
        tasks = []
        for tt in task_types:
            task_ids = r.lrange(f"tasks:{tt}", 0, 5)
            for tid in task_ids:
                tdata = r.hgetall(f"task:{tid}") or {}
                if tdata:
                    tasks.append({
                        "id": tid,
                        "type": tdata.get("type", tt),
                        "state": tdata.get("state", "pending"),
                        "payload": tdata.get("payload", "")[:60],
                        "assigned_to": tdata.get("assigned_to", ""),
                        "created_at": tdata.get("created_at", ""),
                    })

        return {
            "agents": agents,
            "events": events,
            "tasks": tasks,
            "ts": now,
        }
    except Exception as e:
        return {"agents": [], "events": [], "tasks": [], "error": str(e), "ts": int(time.time())}


# ── WebSocket ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_clients.add(websocket)
    try:
        while True:
            data = get_fleet_data()
            await websocket.send_json(data)
            await asyncio.sleep(3)
    except (WebSocketDisconnect, Exception):
        ws_clients.discard(websocket)


# ── Task submission ────────────────────────────────────────────────────────────

class TaskRequest(BaseModel):
    task_type: str = "research"
    payload: str
    priority: str = "normal"


@app.post("/api/task")
async def submit_task(task: TaskRequest):
    r = get_redis()
    task_id = str(uuid.uuid4())[:8]
    task_data = {
        "id": task_id,
        "type": task.task_type,
        "payload": task.payload,
        "state": "pending",
        "created_at": str(int(time.time())),
        "created_by": "human",
    }
    r.hset(f"task:{task_id}", mapping=task_data)
    r.lpush(f"tasks:{task.task_type}", task_id)
    r.xadd("events", {
        "agent": "human",
        "type": "task_created",
        "task_id": task_id,
        "payload": task.payload[:80],
    })
    return {"task_id": task_id, "status": "queued"}


@app.get("/api/fleet")
async def fleet_api():
    return get_fleet_data()


@app.get("/api/tasks")
async def list_tasks():
    try:
        r = get_redis()
        tasks = []
        # Scan all task:* keys directly (catches queued + claimed + running)
        for key in r.scan_iter("task:*", count=50):
            data = r.hgetall(key) or {}
            if data and data.get("state") in ("pending", "claimed", "running"):
                tasks.append({
                    "id": data.get("id", key.replace("task:", "")),
                    "type": data.get("type", "generic"),
                    "state": data.get("state", "?"),
                    "payload": data.get("payload", "")[:60],
                    "assigned_to": data.get("assigned_to", ""),
                })
        return {"tasks": tasks}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


@app.get("/health")
async def health():
    try:
        r = get_redis()
        r.ping()
        return {"status": "ok", "redis": "ok"}
    except Exception as e:
        return {"status": "degraded", "redis": str(e)}


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>M2O Fleet Command</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body {
    background: #050508;
    color: #e0e0ff;
    font-family: 'Courier New', monospace;
    overflow: hidden;
    height: 100vh;
  }
  #app { display:flex; height:100vh; }

  /* ── Graph area ── */
  #graph-area {
    flex: 1;
    position: relative;
    overflow: hidden;
  }
  #graph-svg { width:100%; height:100%; }
  #canvas-overlay {
    position:absolute; top:0; left:0;
    pointer-events:none;
  }
  #header {
    position:absolute; top:16px; left:50%;
    transform:translateX(-50%);
    text-align:center; z-index:10;
    pointer-events:none;
  }
  #header h1 {
    font-size:1.1rem; letter-spacing:0.3em;
    color:#a080ff; text-shadow: 0 0 20px #a080ff88;
    text-transform:uppercase;
  }
  #header .sub {
    font-size:0.65rem; color:#606090; letter-spacing:0.2em;
  }
  #fleet-status {
    position:absolute; bottom:16px; left:16px;
    font-size:0.65rem; color:#404060;
  }

  /* ── Side panel ── */
  #panel {
    width:340px; min-width:280px;
    background:#080810;
    border-left:1px solid #1a1a3a;
    display:flex; flex-direction:column;
    overflow:hidden;
  }
  .panel-section {
    border-bottom:1px solid #1a1a3a;
    padding:14px;
  }
  .panel-section h3 {
    font-size:0.65rem; letter-spacing:0.2em;
    color:#6060a0; text-transform:uppercase;
    margin-bottom:10px;
  }

  /* Task input */
  #task-type {
    width:100%; background:#0d0d1a; border:1px solid #2a2a5a;
    color:#e0e0ff; font-family:inherit; font-size:0.75rem;
    padding:6px 8px; border-radius:3px; margin-bottom:8px;
    outline:none;
  }
  #task-input {
    width:100%; background:#0d0d1a; border:1px solid #2a2a5a;
    color:#e0e0ff; font-family:inherit; font-size:0.75rem;
    padding:8px; border-radius:3px; resize:vertical;
    min-height:72px; outline:none;
    transition: border-color 0.3s;
  }
  #task-input:focus { border-color:#a080ff; box-shadow: 0 0 8px #a080ff44; }
  #submit-btn {
    width:100%; margin-top:8px;
    background: linear-gradient(135deg, #3a1a6a, #1a1a5a);
    border:1px solid #a080ff88;
    color:#c0a0ff; font-family:inherit; font-size:0.75rem;
    letter-spacing:0.15em; text-transform:uppercase;
    padding:9px; border-radius:3px; cursor:pointer;
    transition: all 0.2s;
  }
  #submit-btn:hover { background:linear-gradient(135deg,#5a2a9a,#2a2a8a); box-shadow:0 0 12px #a080ff55; }
  #submit-btn:active { transform:scale(0.98); }
  #submit-feedback {
    font-size:0.65rem; color:#60ff88; margin-top:6px;
    min-height:16px; text-align:center;
  }

  /* Tasks */
  #tasks-list {
    max-height:160px; overflow-y:auto;
    scrollbar-width:thin; scrollbar-color:#1a1a3a #080810;
  }
  .task-card {
    background:#0d0d1a; border:1px solid #1a1a3a;
    border-radius:3px; padding:7px 9px; margin-bottom:5px;
    font-size:0.65rem;
  }
  .task-card .task-header { display:flex; justify-content:space-between; margin-bottom:3px; }
  .task-card .task-id { color:#6060a0; }
  .task-card .task-type { color:#a080ff; text-transform:uppercase; font-size:0.6rem; }
  .task-card .task-payload { color:#909090; }
  .task-card .task-agent { color:#00ff88; font-size:0.6rem; }
  .state-pending { color:#ffaa00; }
  .state-claimed,.state-running { color:#00aaff; }
  .state-done { color:#00ff88; }
  .state-failed { color:#ff4444; }

  /* Events */
  #events-list {
    flex:1; overflow-y:auto; padding:10px 14px;
    scrollbar-width:thin; scrollbar-color:#1a1a3a #080810;
    font-size:0.62rem;
  }
  .event-row { display:flex; gap:8px; margin-bottom:4px; align-items:baseline; }
  .event-ts { color:#404060; min-width:56px; }
  .event-agent { color:#a080ff; min-width:60px; }
  .event-dot { width:5px; height:5px; border-radius:50%; flex-shrink:0; margin-top:3px; }
  .event-type { flex:1; }
  .et-heartbeat { color:#404060; }
  .et-task_created { color:#ffaa00; }
  .et-task_claimed { color:#00aaff; }
  .et-task_done { color:#00ff88; }
  .et-agent_recovered { color:#00ff88; }
  .et-default { color:#8080a0; }

  /* SVG nodes */
  .node-group { cursor:pointer; }
  .node-label {
    font-family:'Courier New',monospace;
    font-size:11px;
    text-anchor:middle;
    pointer-events:none;
    fill:#c0c0e0;
  }
  .node-sublabel {
    font-family:'Courier New',monospace;
    font-size:8px;
    text-anchor:middle;
    pointer-events:none;
    fill:#606080;
  }
  .link { stroke-opacity:0.25; fill:none; }

  /* Legend */
  #legend {
    position:absolute; top:16px; left:16px;
    font-size:0.6rem; color:#404060; z-index:10;
    pointer-events:none;
  }
  .leg-row { display:flex; align-items:center; gap:6px; margin-bottom:3px; }
  .leg-dot { width:8px; height:8px; border-radius:50%; flex-shrink:0; }
</style>
</head>
<body>
<div id="app">

  <!-- Graph -->
  <div id="graph-area">
    <div id="header">
      <h1>⚡ M2O Fleet Command</h1>
      <div class="sub">machine.machine · autonomous agent fleet</div>
    </div>
    <svg id="graph-svg"></svg>
    <canvas id="canvas-overlay"></canvas>
    <div id="fleet-status">connecting...</div>
    <div id="legend">
      <div class="leg-row"><div class="leg-dot" style="background:#00ff88"></div> alive</div>
      <div class="leg-row"><div class="leg-dot" style="background:#ffaa00"></div> degraded</div>
      <div class="leg-row"><div class="leg-dot" style="background:#ff4444"></div> dead / offline</div>
      <div class="leg-row"><div class="leg-dot" style="background:#a080ff"></div> orchestrator</div>
    </div>
  </div>

  <!-- Panel -->
  <div id="panel">
    <div class="panel-section">
      <h3>↯ Dispatch Request</h3>
      <select id="task-type">
        <option value="research">RESEARCH</option>
        <option value="build">BUILD</option>
        <option value="plan">PLAN</option>
        <option value="code-review">CODE REVIEW</option>
        <option value="generic">GENERIC</option>
      </select>
      <textarea id="task-input" placeholder="Describe the task...&#10;e.g. Research SpacetimeDB subscription patterns for the fleet-bus context engineer implementation"></textarea>
      <button id="submit-btn">▶ DISPATCH TO FLEET</button>
      <div id="submit-feedback"></div>
    </div>

    <div class="panel-section" style="max-height:200px">
      <h3>⬡ Active Tasks</h3>
      <div id="tasks-list"><div style="color:#404060;font-size:0.65rem;">no active tasks</div></div>
    </div>

    <h3 style="padding:10px 14px 4px;font-size:0.65rem;letter-spacing:0.2em;color:#6060a0;text-transform:uppercase;">◈ Event Stream</h3>
    <div id="events-list"></div>
  </div>
</div>

<script>
// ── Infrastructure node definitions ───────────────────────────────────────────
const INFRA_NODES = [
  { id:"redis",       label:"Redis",        sub:"fleet-bus",    color:"#ff4466", r:18, fixed:true, type:"infra" },
  { id:"spacetimedb", label:"SpacetimeDB",  sub:"events/tasks", color:"#00ccff", r:18, fixed:true, type:"infra" },
  { id:"minio",       label:"Minio",        sub:"s3 context",   color:"#44ff88", r:18, fixed:true, type:"infra" },
  { id:"qdrant",      label:"Qdrant",       sub:"vectors",      color:"#ff8844", r:18, fixed:true, type:"infra" },
];

const INFRA_LINKS = [
  { source:"m2", target:"redis",       label:"heartbeat" },
  { source:"m2", target:"spacetimedb", label:"tasks" },
  { source:"m2", target:"minio",       label:"context" },
  { source:"m2", target:"qdrant",      label:"search" },
];

// ── SVG setup ─────────────────────────────────────────────────────────────────
const svg = d3.select("#graph-svg");
const graphArea = document.getElementById("graph-area");
let W = graphArea.clientWidth, H = graphArea.clientHeight;

const defs = svg.append("defs");

// Glow filters
function addGlow(id, color, blur=8, strength=1) {
  const f = defs.append("filter").attr("id", id).attr("x","-50%").attr("y","-50%").attr("width","200%").attr("height","200%");
  f.append("feGaussianBlur").attr("in","SourceGraphic").attr("stdDeviation", blur).attr("result","blur");
  f.append("feColorMatrix").attr("in","blur").attr("type","matrix")
    .attr("values",`0 0 0 0 ${hexR(color)}  0 0 0 0 ${hexG(color)}  0 0 0 0 ${hexB(color)}  0 0 0 ${strength} 0`)
    .attr("result","coloredBlur");
  const merge = f.append("feMerge");
  merge.append("feMergeNode").attr("in","coloredBlur");
  merge.append("feMergeNode").attr("in","SourceGraphic");
}
function hexR(h){ return parseInt(h.slice(1,3),16)/255 }
function hexG(h){ return parseInt(h.slice(3,5),16)/255 }
function hexB(h){ return parseInt(h.slice(5,7),16)/255 }

addGlow("glow-gold",  "#ffcc44", 12, 2.5);
addGlow("glow-blue",  "#4488ff", 10, 2);
addGlow("glow-red",   "#ff4466", 8,  2);
addGlow("glow-cyan",  "#00ccff", 8,  2);
addGlow("glow-green", "#44ff88", 8,  2);
addGlow("glow-orange","#ff8844", 8,  2);
addGlow("glow-purple","#a080ff", 8,  2);
addGlow("glow-white", "#ffffff", 4,  1.5);

// Arrow marker for links
defs.append("marker").attr("id","arrow").attr("viewBox","0 -4 8 8")
  .attr("refX",8).attr("refY",0).attr("markerWidth",6).attr("markerHeight",6)
  .attr("orient","auto")
  .append("path").attr("d","M0,-4L8,0L0,4").attr("fill","#2a2a5a");

const g = svg.append("g");

// ── Canvas overlay for particles ──────────────────────────────────────────────
const canvas = document.getElementById("canvas-overlay");
const ctx2d = canvas.getContext("2d");
let particles = [];
let ceAnim = null; // context engineer animation state

function resizeCanvas() {
  W = graphArea.clientWidth; H = graphArea.clientHeight;
  canvas.width = W; canvas.height = H;
  svg.attr("width", W).attr("height", H);
}
resizeCanvas();
window.addEventListener("resize", () => { resizeCanvas(); initGraph(); });

// ── Force simulation ───────────────────────────────────────────────────────────
let simulation, linkSel, nodeSel, allNodes, allLinks;
let fleetData = { agents:[], events:[], tasks:[] };

function initGraph() {
  g.selectAll("*").remove();

  // Build node list from fleet data + infra
  const agentNodes = fleetData.agents.map(a => ({
    id: a.id,
    label: a.id,
    sub: a.preset,
    color: a.id === "m2" ? "#a080ff" : statusColor(a.status),
    glowId: a.id === "m2" ? "glow-purple" : statusGlow(a.status),
    r: a.id === "m2" ? 28 : 20,
    status: a.status,
    type: "agent",
    current_task: a.current_task,
    age_str: a.age_str,
  }));

  // Always include m2 even if not in fleet data
  if (!agentNodes.find(n => n.id === "m2")) {
    agentNodes.push({ id:"m2", label:"m2 ⚡", sub:"orchestrator", color:"#a080ff", glowId:"glow-purple", r:28, status:"unknown", type:"agent" });
  } else {
    const m2 = agentNodes.find(n => n.id === "m2");
    m2.label = "m2 ⚡"; m2.sub = "orchestrator";
    m2.color = "#a080ff"; m2.glowId = "glow-purple"; m2.r = 28;
  }

  allNodes = [...agentNodes, ...INFRA_NODES.map(n => ({...n}))];

  // Position infra nodes around m2 (fixed)
  const cx = W/2, cy = H/2;
  const infraPositions = [
    { id:"redis",       dx:-220, dy:-120 },
    { id:"spacetimedb", dx: 220, dy:-120 },
    { id:"minio",       dx: 220, dy: 120 },
    { id:"qdrant",      dx:-220, dy: 120 },
  ];
  allNodes.forEach(n => {
    const pos = infraPositions.find(p => p.id === n.id);
    if (pos) { n.fx = cx + pos.dx; n.fy = cy + pos.dy; }
    if (n.id === "m2") { n.fx = cx; n.fy = cy; }
  });

  // Build links: m2↔infra + m2↔agents
  const agentLinks = agentNodes.filter(n => n.id !== "m2").map(n => ({
    source: "m2", target: n.id, color: "#2a2a6a"
  }));
  allLinks = [
    ...INFRA_LINKS.map(l => ({ ...l, color: infraColor(l.target) })),
    ...agentLinks,
  ];

  // Draw links
  linkSel = g.append("g").selectAll("line")
    .data(allLinks).join("line")
    .attr("class","link")
    .attr("stroke", d => d.color)
    .attr("stroke-width", 1.5)
    .attr("marker-end","url(#arrow)");

  // Draw pulse rings (for alive agents)
  g.append("g").attr("id","pulse-rings");

  // Draw nodes
  nodeSel = g.append("g").selectAll(".node-group")
    .data(allNodes).join("g")
    .attr("class","node-group")
    .call(d3.drag()
      .on("start", (e,d) => { if(!d.fixed){d.fx=d.x; d.fy=d.y;} simulation.alphaTarget(0.1).restart(); })
      .on("drag",  (e,d) => { if(!d.fixed){d.fx=e.x;  d.fy=e.y;} })
      .on("end",   (e,d) => { if(!d.fixed){d.fx=null; d.fy=null;} simulation.alphaTarget(0); })
    );

  // Outer glow ring
  nodeSel.append("circle")
    .attr("r", d => d.r + 8)
    .attr("fill","none")
    .attr("stroke", d => d.color)
    .attr("stroke-width", 1)
    .attr("stroke-opacity", 0.15)
    .attr("filter", d => `url(#${d.glowId || "glow-white"})`);

  // Main circle
  nodeSel.append("circle")
    .attr("r", d => d.r)
    .attr("fill", d => d.type === "infra" ? "#0a0a18" : "#0d0d20")
    .attr("stroke", d => d.color)
    .attr("stroke-width", 2)
    .attr("filter", d => `url(#${d.glowId || "glow-white"})`);

  // Label
  nodeSel.append("text").attr("class","node-label")
    .attr("dy", "-2")
    .attr("filter", d => `url(#${d.glowId || "glow-white"})`)
    .style("fill", d => d.color)
    .text(d => d.label);

  nodeSel.append("text").attr("class","node-sublabel")
    .attr("dy", "12")
    .text(d => d.sub || "");

  // Force simulation
  if (simulation) simulation.stop();
  simulation = d3.forceSimulation(allNodes)
    .force("link", d3.forceLink(allLinks).id(d => d.id).distance(180).strength(0.3))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(W/2, H/2))
    .force("collision", d3.forceCollide().radius(d => d.r + 30))
    .on("tick", ticked);
}

function ticked() {
  linkSel
    .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
    .attr("x2", d => linkEnd(d).x).attr("y2", d => linkEnd(d).y);
  nodeSel.attr("transform", d => `translate(${d.x||0},${d.y||0})`);
}

function linkEnd(d) {
  const dx = d.target.x - d.source.x, dy = d.target.y - d.source.y;
  const dist = Math.sqrt(dx*dx + dy*dy) || 1;
  const r = d.target.r + 10;
  return { x: d.target.x - dx/dist*r, y: d.target.y - dy/dist*r };
}

function infraColor(id) {
  const c = { redis:"#ff4466", spacetimedb:"#00ccff", minio:"#44ff88", qdrant:"#ff8844" };
  return c[id] || "#2a2a6a";
}
function statusColor(s) { return s==="alive"?"#00ff88":s==="degraded"?"#ffaa00":"#ff4444"; }
function statusGlow(s) { return s==="alive"?"glow-green":s==="degraded"?"glow-orange":"glow-red"; }

// ── Particle system ───────────────────────────────────────────────────────────
function spawnParticles(fromId, toId, color, count=6, size=3) {
  const from = allNodes?.find(n => n.id === fromId);
  const to   = allNodes?.find(n => n.id === toId);
  if (!from || !to) return;
  for (let i = 0; i < count; i++) {
    particles.push({
      x: from.x, y: from.y,
      tx: to.x, ty: to.y,
      progress: -i * 0.15,
      color, size,
      trail: [],
    });
  }
}

function animateParticles() {
  ctx2d.clearRect(0, 0, W, H);
  particles = particles.filter(p => p.progress < 1.1);
  for (const p of particles) {
    p.progress += 0.012;
    if (p.progress < 0) continue;
    const t = Math.max(0, Math.min(1, p.progress));
    const ease = t < 0.5 ? 2*t*t : -1+(4-2*t)*t;
    p.x = lerp(p.x, p.tx, 0);
    const cx = lerp(allNodes?.find(n=>n.id==="m2")?.x || W/2, 0, 0);
    const nx = bezier(getStartX(p), (getStartX(p)+p.tx)/2, p.tx, ease);
    const ny = bezier(getStartY(p), (getStartY(p)+p.ty)/2, p.ty, ease);

    p.trail.push({x:nx, y:ny});
    if (p.trail.length > 8) p.trail.shift();

    // Draw trail
    for (let i = 0; i < p.trail.length; i++) {
      const alpha = (i / p.trail.length) * 0.6;
      const size = p.size * (i / p.trail.length) * 0.8;
      ctx2d.beginPath();
      ctx2d.arc(p.trail[i].x, p.trail[i].y, size, 0, Math.PI*2);
      ctx2d.fillStyle = hexAlpha(p.color, alpha);
      ctx2d.fill();
    }
    // Head
    ctx2d.beginPath();
    ctx2d.arc(nx, ny, p.size, 0, Math.PI*2);
    ctx2d.fillStyle = p.color;
    ctx2d.shadowColor = p.color;
    ctx2d.shadowBlur = 8;
    ctx2d.fill();
    ctx2d.shadowBlur = 0;

    p._nx = nx; p._ny = ny;
    if (p.progress >= 1) { p.progress = 1.1; }
  }

  // CE animation
  if (ceAnim) drawCEAnimation();

  requestAnimationFrame(animateParticles);
}

function getStartX(p) { return p.trail.length > 0 ? p.trail[0].x : (allNodes?.find(n=>n.x)?allNodes[0].x:W/2); }
function getStartY(p) { return p.trail.length > 0 ? p.trail[0].y : H/2; }

function bezier(p0, p1, p2, t) { return (1-t)*(1-t)*p0 + 2*(1-t)*t*p1 + t*t*p2; }
function lerp(a, b, t) { return a + (b-a)*t; }
function hexAlpha(hex, a) {
  const r=parseInt(hex.slice(1,3),16), g=parseInt(hex.slice(3,5),16), b=parseInt(hex.slice(5,7),16);
  return `rgba(${r},${g},${b},${a})`;
}

// ── Context Engineer animation ────────────────────────────────────────────────
function triggerCEAnimation(taskPayload) {
  const m2Node = allNodes?.find(n => n.id === "m2");
  if (!m2Node) return;
  ceAnim = {
    phase: "expand",  // expand → shrink → dispatch
    x: m2Node.x + 50, y: m2Node.y - 40,
    radius: 0, targetR: 45,
    text: "RAW CTX",
    subtext: "10 results",
    opacity: 0,
    t: 0,
    taskPayload: taskPayload || "...",
  };
}

function drawCEAnimation() {
  if (!ceAnim) return;
  const c = ceAnim;
  c.t += 0.02;

  ctx2d.save();
  ctx2d.globalAlpha = Math.min(1, c.opacity + 0.04);
  c.opacity = Math.min(1, c.opacity + 0.04);

  if (c.phase === "expand") {
    c.radius = Math.min(c.targetR, c.radius + 2);
    if (c.radius >= c.targetR) { c.phase = "hold"; c.holdT = 0; }
    drawCEBlob(c, "#a080ff", c.radius, 1.0);
  } else if (c.phase === "hold") {
    c.holdT = (c.holdT||0) + 1;
    drawCEBlob(c, "#a080ff", c.targetR, 1.0);
    if (c.holdT > 60) { c.phase = "shrink"; }
  } else if (c.phase === "shrink") {
    c.radius = Math.max(10, c.radius - 1.2);
    const progress = 1 - (c.radius - 10) / (c.targetR - 10);
    const col = lerpColor("#a080ff", "#ffcc44", progress);
    c.text = progress > 0.7 ? "2k BUNDLE" : "DISTILLING";
    c.subtext = progress > 0.7 ? "gemini flash" : `${Math.round((1-progress)*10)} results`;
    drawCEBlob(c, col, c.radius, 1.0);
    if (c.radius <= 10) { c.phase = "dispatch"; c.dispatchT = 0; }
  } else if (c.phase === "dispatch") {
    drawCEBlob(c, "#ffcc44", 10, 1.0 - c.dispatchT * 0.03);
    c.dispatchT++;
    // Spawn particles toward agents
    if (c.dispatchT === 5) {
      const agents = fleetData.agents.filter(a => a.id !== "m2");
      agents.forEach(a => spawnParticles("m2", a.id, "#ffcc44", 8, 4));
      spawnParticles("m2", "minio", "#44ff88", 6, 3);
    }
    if (c.dispatchT > 35) { ceAnim = null; ctx2d.restore(); return; }
  }
  ctx2d.restore();
}

function drawCEBlob(c, color, r, alpha) {
  ctx2d.save();
  ctx2d.globalAlpha = alpha;
  // Outer glow
  const grad = ctx2d.createRadialGradient(c.x, c.y, r*0.2, c.x, c.y, r*2);
  grad.addColorStop(0, hexAlpha(color, 0.3));
  grad.addColorStop(1, hexAlpha(color, 0));
  ctx2d.beginPath(); ctx2d.arc(c.x, c.y, r*2, 0, Math.PI*2);
  ctx2d.fillStyle = grad; ctx2d.fill();
  // Main blob
  ctx2d.beginPath(); ctx2d.arc(c.x, c.y, r, 0, Math.PI*2);
  ctx2d.fillStyle = hexAlpha(color, 0.15);
  ctx2d.strokeStyle = color;
  ctx2d.lineWidth = 2;
  ctx2d.shadowColor = color; ctx2d.shadowBlur = 12;
  ctx2d.fill(); ctx2d.stroke();
  ctx2d.shadowBlur = 0;
  // Text
  ctx2d.fillStyle = color;
  ctx2d.font = `bold ${Math.max(7, r*0.28)}px 'Courier New'`;
  ctx2d.textAlign = "center"; ctx2d.textBaseline = "middle";
  ctx2d.fillText(c.text, c.x, c.y - 4);
  ctx2d.font = `${Math.max(6, r*0.22)}px 'Courier New'`;
  ctx2d.fillStyle = hexAlpha(color, 0.7);
  ctx2d.fillText(c.subtext, c.x, c.y + r*0.4);
  ctx2d.restore();
}

function lerpColor(a, b, t) {
  const ar=parseInt(a.slice(1,3),16), ag=parseInt(a.slice(3,5),16), ab=parseInt(a.slice(5,7),16);
  const br=parseInt(b.slice(1,3),16), bg=parseInt(b.slice(3,5),16), bb=parseInt(b.slice(5,7),16);
  const r=Math.round(ar+(br-ar)*t), g=Math.round(ag+(bg-ag)*t), blue=Math.round(ab+(bb-ab)*t);
  return `#${r.toString(16).padStart(2,'0')}${g.toString(16).padStart(2,'0')}${blue.toString(16).padStart(2,'0')}`;
}

// ── Pulse rings for alive agents ───────────────────────────────────────────────
let pulseT = 0;
function drawPulseRings() {
  const ringsG = g.select("#pulse-rings");
  // Update via d3 transition on heartbeat events
}

// ── Heartbeat particle streams ────────────────────────────────────────────────
let lastHeartbeatStream = 0;
function maybeStreamHeartbeats() {
  const now = Date.now();
  if (now - lastHeartbeatStream < 5000) return;
  lastHeartbeatStream = now;
  if (!allNodes) return;
  fleetData.agents.forEach(a => {
    if (a.status === "alive" && a.id !== "m2") {
      spawnParticles(a.id, "redis", "#ff446688", 3, 2);
      spawnParticles(a.id, "m2", statusColor(a.status), 2, 2);
    }
  });
  spawnParticles("m2", "redis", "#ff4466", 4, 2);
}

// ── Fleet data update ─────────────────────────────────────────────────────────
function updateFleetData(data) {
  const prevAgentIds = new Set((fleetData.agents||[]).map(a=>a.id));
  fleetData = data;
  updateEventsPanel(data.events || []);
  updateTasksPanel(data.tasks || []);
  updateFleetStatus(data);

  // Reinit graph if agent set changed
  const newAgentIds = new Set((data.agents||[]).map(a=>a.id));
  const changed = [...newAgentIds].some(id=>!prevAgentIds.has(id)) ||
                  [...prevAgentIds].some(id=>!newAgentIds.has(id));
  if (changed || !allNodes) {
    initGraph();
  } else {
    // Just update node colors
    nodeSel.select("circle:nth-child(2)")
      .attr("stroke", d => {
        const a = data.agents.find(a=>a.id===d.id);
        return a ? (d.id==="m2" ? "#a080ff" : statusColor(a.status)) : d.color;
      });
  }

  maybeStreamHeartbeats();
}

function updateEventsPanel(events) {
  const el = document.getElementById("events-list");
  el.innerHTML = events.map(e => {
    const cls = `et-${e.type}` in document.styleSheets ? `et-${e.type}` : "et-default";
    const dotColor = e.type==="task_created"?"#ffaa00":e.type==="task_done"?"#00ff88":e.type==="task_claimed"?"#00aaff":"#404060";
    return `<div class="event-row">
      <span class="event-ts">${e.ts}</span>
      <div class="event-dot" style="background:${dotColor}"></div>
      <span class="event-agent">${e.agent||'fleet'}</span>
      <span class="event-type et-${e.type.replace(/_/g,'-')}">${e.type}</span>
    </div>`;
  }).join("");
}

function updateTasksPanel(tasks) {
  const el = document.getElementById("tasks-list");
  if (!tasks || tasks.length === 0) {
    el.innerHTML = '<div style="color:#404060;font-size:0.65rem;">no active tasks</div>';
    return;
  }
  el.innerHTML = tasks.map(t => `
    <div class="task-card">
      <div class="task-header">
        <span class="task-id">#${t.id}</span>
        <span class="task-type">${t.type}</span>
      </div>
      <div class="task-payload">${t.payload}</div>
      ${t.assigned_to ? `<div class="task-agent">→ ${t.assigned_to}</div>` : ''}
      <div class="state-${t.state}">${t.state}</div>
    </div>`).join("");
}

function updateFleetStatus(data) {
  const alive = (data.agents||[]).filter(a=>a.status==="alive").length;
  const total = (data.agents||[]).length;
  document.getElementById("fleet-status").textContent =
    `${alive}/${total} agents alive · ${(data.tasks||[]).length} tasks · ${Date().slice(16,24)} UTC`;
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws, wsRetryT = 2000;
function connectWS() {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  ws = new WebSocket(`${proto}//${location.host}/ws`);
  ws.onopen = () => { wsRetryT = 2000; console.log("WS connected"); };
  ws.onmessage = e => { try { updateFleetData(JSON.parse(e.data)); } catch(err){} };
  ws.onclose = () => { setTimeout(connectWS, wsRetryT); wsRetryT = Math.min(wsRetryT*1.5, 15000); };
}

// ── Task submission ────────────────────────────────────────────────────────────
document.getElementById("submit-btn").onclick = async () => {
  const payload = document.getElementById("task-input").value.trim();
  const task_type = document.getElementById("task-type").value;
  if (!payload) return;

  const btn = document.getElementById("submit-btn");
  btn.textContent = "⟳ DISPATCHING...";
  btn.disabled = true;

  try {
    const res = await fetch("/api/task", {
      method:"POST",
      headers:{"Content-Type":"application/json"},
      body: JSON.stringify({ task_type, payload })
    });
    const data = await res.json();
    document.getElementById("submit-feedback").textContent = `✓ task #${data.task_id} queued`;
    document.getElementById("task-input").value = "";

    // Trigger CE animation
    triggerCEAnimation(payload);

    // Stream particles
    setTimeout(() => spawnParticles("m2", "qdrant", "#ff8844", 8, 3), 200);
    setTimeout(() => spawnParticles("m2", "spacetimedb", "#00ccff", 6, 3), 800);
    setTimeout(() => spawnParticles("m2", "minio", "#44ff88", 5, 3), 1400);

    setTimeout(() => { document.getElementById("submit-feedback").textContent=""; }, 4000);
  } catch(err) {
    document.getElementById("submit-feedback").textContent = "✗ error: " + err.message;
    document.getElementById("submit-feedback").style.color = "#ff4444";
  }
  btn.textContent = "▶ DISPATCH TO FLEET";
  btn.disabled = false;
};

// ── Boot ───────────────────────────────────────────────────────────────────────
initGraph();
connectWS();
animateParticles();

// Initial data fetch
fetch("/api/fleet").then(r=>r.json()).then(updateFleetData).catch(()=>{});

// Periodic infra particle streams (ambient activity)
setInterval(() => {
  if (!allNodes) return;
  const pairs = [
    ["m2","spacetimedb","#00ccff"],
    ["m2","redis","#ff4466"],
    ["m2","qdrant","#ff8844"],
    ["m2","minio","#44ff88"],
  ];
  const [from,to,color] = pairs[Math.floor(Math.random()*pairs.length)];
  spawnParticles(from, to, color, 2, 2);
}, 4000);

</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML
