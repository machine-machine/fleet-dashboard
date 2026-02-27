"""
M2O Fleet Command — Animated Fleet Visualization
Real-time force graph of the Machine.Machine agent fleet
"""

import asyncio
import json
import time
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Set, Optional, List

import redis
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

# Benchmark registry
sys.path.insert(0, os.path.dirname(__file__))
try:
    import benchmark as BM
    BENCH_ENABLED = True
except ImportError:
    BENCH_ENABLED = False

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

        # Tasks — scan all task:* keys directly to catch queued + claimed + running
        tasks = []
        for key in r.scan_iter("task:*", count=50):
            tdata = r.hgetall(key) or {}
            if tdata and tdata.get("state") in ("pending", "claimed", "running", "paused"):
                tid = tdata.get("id", key.replace("task:", ""))
                tasks.append({
                    "id": tid,
                    "type": tdata.get("type", "generic"),
                    "state": tdata.get("state", "pending"),
                    "payload": tdata.get("payload", "")[:60],
                    "assigned_to": tdata.get("assigned_to", ""),
                    "created_at": tdata.get("created_at", ""),
                    "context_summary": tdata.get("context_summary", ""),
                    "context_ms": int(tdata.get("context_ms", 0) or 0),
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


@app.post("/api/tasks/{task_id}/cancel")
async def cancel_task(task_id: str):
    """Cancel a pending/running/paused task. Kills sub-agent session if active."""
    try:
        r = get_redis()
        key = f"task:{task_id}"
        data = r.hgetall(key) or {}
        if not data:
            return {"error": "task not found"}

        prev_state = data.get("state", "?")
        if prev_state in ("done", "cancelled", "failed"):
            return {"error": f"task already {prev_state}"}

        # Kill sub-agent session via OpenClaw gateway if session_key present
        session_key = data.get("session_key", "")
        kill_result = None
        if session_key:
            try:
                import urllib.request as _ur
                req = _ur.Request(
                    f"http://localhost:18789/api/sessions/{session_key}/kill",
                    data=b"{}",
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                resp = _ur.urlopen(req, timeout=5)
                kill_result = "session killed"
            except Exception as ke:
                kill_result = f"session kill failed: {ke}"

        r.hset(key, mapping={
            "state": "cancelled",
            "cancelled_at": str(int(time.time())),
            "cancelled_by": "human",
        })
        # Remove from any task-type queue
        task_type = data.get("type", "generic")
        r.lrem(f"queue:{task_type}", 0, task_id)

        # Emit event
        r.xadd("fleet:events", {
            "agent": "m2",
            "type": "task_cancelled",
            "task_id": task_id,
            "prev_state": prev_state,
            "ts": str(int(time.time())),
        }, maxlen=500)

        return {"task_id": task_id, "status": "cancelled", "prev_state": prev_state,
                "session": kill_result}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/tasks/{task_id}/pause")
async def pause_task(task_id: str):
    """Pause a pending or running task (executor will skip it until resumed)."""
    try:
        r = get_redis()
        key = f"task:{task_id}"
        data = r.hgetall(key) or {}
        if not data:
            return {"error": "task not found"}

        prev_state = data.get("state", "?")
        if prev_state in ("done", "cancelled", "failed", "paused"):
            return {"error": f"cannot pause task in state {prev_state}"}

        r.hset(key, mapping={
            "state": "paused",
            "paused_at": str(int(time.time())),
            "_prev_state": prev_state,
        })

        r.xadd("fleet:events", {
            "agent": "m2",
            "type": "task_paused",
            "task_id": task_id,
            "prev_state": prev_state,
            "ts": str(int(time.time())),
        }, maxlen=500)

        return {"task_id": task_id, "status": "paused", "prev_state": prev_state}
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/tasks/{task_id}/resume")
async def resume_task(task_id: str):
    """Resume a paused task — re-queues it as pending."""
    try:
        r = get_redis()
        key = f"task:{task_id}"
        data = r.hgetall(key) or {}
        if not data:
            return {"error": "task not found"}

        if data.get("state") != "paused":
            return {"error": f"task is not paused (state={data.get('state')})"}

        task_type = data.get("type", "generic")
        r.hset(key, mapping={
            "state": "pending",
            "assigned_to": "",
            "claimed_at": "",
            "session_key": "",
            "context_path": "",
            "context_summary": "",
        })
        r.rpush(f"queue:{task_type}", task_id)

        r.xadd("fleet:events", {
            "agent": "m2",
            "type": "task_resumed",
            "task_id": task_id,
            "ts": str(int(time.time())),
        }, maxlen=500)

        return {"task_id": task_id, "status": "resumed"}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/ce/context/{task_id}")
async def ce_context(task_id: str):
    """Return the context bundle for a task (reads /tmp/context-{task_id}.json)."""
    import pathlib
    path = pathlib.Path(f"/tmp/context-{task_id}.json")
    if not path.exists():
        # Try to reconstruct from Redis
        try:
            r = get_redis()
            data = r.hgetall(f"task:{task_id}") or {}
            if data.get("context_summary"):
                return {
                    "task_id": task_id,
                    "summary": data.get("context_summary", ""),
                    "generation_ms": int(data.get("context_ms", 0) or 0),
                    "memory_source": "redis-partial",
                    "_partial": True,
                }
        except Exception:
            pass
        return {"error": "context bundle not found", "task_id": task_id}
    try:
        return json.loads(path.read_text())
    except Exception as e:
        return {"error": str(e), "task_id": task_id}


@app.get("/api/ce/recent")
async def ce_recent():
    """Return recent CE bundles from Redis task hashes."""
    try:
        r = get_redis()
        bundles = []
        for key in r.scan_iter("task:*", count=100):
            data = r.hgetall(key) or {}
            if data.get("context_ms") or data.get("context_summary"):
                bundles.append({
                    "task_id": data.get("id", key.replace("task:", "")),
                    "task_type": data.get("type", "?"),
                    "state": data.get("state", "?"),
                    "payload": data.get("payload", "")[:50],
                    "summary": data.get("context_summary", ""),
                    "generation_ms": int(data.get("context_ms", 0) or 0),
                    "created_at": data.get("created_at", ""),
                })
        bundles.sort(key=lambda b: b.get("created_at", ""), reverse=True)
        return {"bundles": bundles[:10], "count": len(bundles)}
    except Exception as e:
        return {"bundles": [], "error": str(e)}


@app.get("/health")
async def health():
    try:
        r = get_redis()
        r.ping()
        return {"status": "ok", "redis": "ok"}
    except Exception as e:
        return {"status": "degraded", "redis": str(e)}


# ── Benchmark Registry ─────────────────────────────────────────────────────────

@app.get("/api/benchmarks")
async def list_benchmarks(status: Optional[str] = Query(None), limit: int = Query(50)):
    if not BENCH_ENABLED:
        return {"benchmarks": [], "error": "benchmark module not loaded"}
    try:
        r = get_redis()
        benches = BM.list_benchmarks(r, limit=limit, status=status)
        return {"benchmarks": benches, "count": len(benches)}
    except Exception as e:
        return {"benchmarks": [], "error": str(e)}


@app.get("/api/benchmarks/stats")
async def benchmark_stats():
    if not BENCH_ENABLED:
        return {"error": "benchmark module not loaded"}
    try:
        r = get_redis()
        return BM.get_fleet_stats(r)
    except Exception as e:
        return {"error": str(e)}


class BenchmarkUpdate(BaseModel):
    bench_id: str
    status: str = "done"
    tokens_in: int = 0
    tokens_out: int = 0
    model: str = "claude-sonnet-4-6"
    artifacts: List[str] = []
    notes: str = ""


@app.post("/api/benchmarks/complete")
async def complete_benchmark(update: BenchmarkUpdate):
    if not BENCH_ENABLED:
        return {"error": "benchmark module not loaded"}
    try:
        r = get_redis()
        result = BM.record_complete(
            r, update.bench_id,
            status=update.status,
            tokens_in=update.tokens_in,
            tokens_out=update.tokens_out,
            model=update.model,
            artifacts=update.artifacts,
            notes=update.notes,
        )
        return result
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/benchmarks/start")
async def start_benchmark(data: dict):
    if not BENCH_ENABLED:
        return {"error": "benchmark module not loaded"}
    try:
        r = get_redis()
        bench_id = BM.record_start(
            r,
            task_id=data.get("task_id", str(uuid.uuid4())[:8]),
            task_type=data.get("task_type", "generic"),
            description=data.get("description", ""),
            agent=data.get("agent", "m2"),
            model=data.get("model", "claude-sonnet-4-6"),
        )
        return {"bench_id": bench_id}
    except Exception as e:
        return {"error": str(e)}


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

  /* ── Resize handle ── */
  #resize-handle {
    width:5px; flex-shrink:0;
    background:transparent;
    cursor:col-resize;
    position:relative;
    z-index:20;
    transition:background 0.15s;
  }
  #resize-handle:hover, #resize-handle.dragging {
    background:#a080ff44;
  }
  #resize-handle::after {
    content:'';
    position:absolute; top:50%; left:50%;
    transform:translate(-50%,-50%);
    width:1px; height:40px;
    background:#2a2a5a;
    border-radius:1px;
  }

  /* ── CE Pipeline overlay ── */
  #ce-pipeline {
    position:absolute; bottom:44px; left:16px; z-index:15;
    display:flex; align-items:center; gap:0;
    font-size:0.55rem; letter-spacing:0.08em;
    pointer-events:none;
    opacity:0.85;
  }
  .ce-node {
    background:#0a0a1a; border:1px solid #2a1a5a;
    border-radius:4px; padding:4px 8px;
    color:#8060c0; white-space:nowrap;
    position:relative;
    transition:border-color 0.3s, color 0.3s, box-shadow 0.3s;
  }
  .ce-node.active {
    border-color:#a080ff; color:#c0a0ff;
    box-shadow:0 0 8px #a080ff44;
  }
  .ce-node .ce-sub {
    display:block; font-size:0.5rem; color:#403060; margin-top:1px;
  }
  .ce-arrow {
    color:#2a1a5a; padding:0 3px; font-size:0.7rem; line-height:1;
  }
  .ce-arrow.active { color:#6040a0; }

  /* ── CE Bundle modal ── */
  #ce-modal {
    display:none; position:fixed; top:0; left:0; right:0; bottom:0;
    background:#00000099; z-index:1000;
    align-items:center; justify-content:center;
  }
  #ce-modal.open { display:flex; }
  #ce-modal-inner {
    background:#0a0a18; border:1px solid #2a2a5a;
    border-radius:6px; padding:24px; max-width:580px; width:90%;
    max-height:80vh; overflow-y:auto;
    font-size:0.72rem; line-height:1.6;
  }
  #ce-modal-inner h2 { color:#a080ff; font-size:0.8rem; letter-spacing:0.2em; margin-bottom:12px; }
  .ce-section-label { color:#6060a0; font-size:0.6rem; letter-spacing:0.15em;
    text-transform:uppercase; margin:10px 0 4px; }
  .ce-summary { color:#c0c0e0; }
  .ce-item { color:#909090; padding:2px 0 2px 10px; border-left:2px solid #2a2a5a; margin:3px 0; }
  .ce-warn  { color:#ffaa44; padding:2px 0 2px 10px; border-left:2px solid #554422; margin:3px 0; }
  .ce-fact  { color:#60c0ff; }
  .ce-meta  { color:#404060; font-size:0.6rem; margin-top:12px; }
  #ce-modal-close { float:right; background:none; border:1px solid #2a2a5a;
    color:#606090; cursor:pointer; padding:2px 8px; border-radius:3px; font-family:inherit; }

  /* ── Maximize toggle ── */
  #maximize-btn {
    position:absolute; top:12px; right:12px; z-index:20;
    background:#0d0d1a; border:1px solid #2a2a5a;
    color:#6060a0; font-family:inherit; font-size:0.65rem;
    padding:3px 8px; border-radius:3px; cursor:pointer;
    letter-spacing:0.1em; transition:all 0.15s;
  }
  #maximize-btn:hover { border-color:#a080ff; color:#a080ff; }

  /* ── Side panel ── */
  #panel {
    width:340px; min-width:0;
    background:#080810;
    border-left:1px solid #1a1a3a;
    display:flex; flex-direction:column;
    overflow:hidden;
    transition:width 0.25s ease;
  }
  #panel.collapsed {
    width:0 !important;
    border-left:none;
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
  .task-controls { display:flex; gap:4px; margin-top:5px; }
  .btn-task { border:none; border-radius:3px; padding:2px 7px; font-size:0.6rem;
    font-family:inherit; cursor:pointer; letter-spacing:0.06em; transition:opacity 0.15s; }
  .btn-task:hover { opacity:0.75; }
  .btn-pause  { background:#332266; color:#a080ff; }
  .btn-cancel { background:#330011; color:#ff3366; }
  .btn-resume { background:#112233; color:#00aaff; }
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
      <div class="sub">machine.machine · autonomous agent fleet · <a href="/benchmarks" style="color:#6060a0;text-decoration:none;font-size:0.6rem;letter-spacing:0.1em;" target="_blank">benchmarks →</a> · <span id="ce-status" style="color:#604080;font-size:0.6rem;letter-spacing:0.08em;" title="Context Engineer: pre-loads semantic memory before each agent spawn">⚡ CE active</span></div>
    </div>
    <button id="maximize-btn" onclick="togglePanel()" title="Maximize / restore panel">⟷</button>
    <svg id="graph-svg"></svg>
    <canvas id="canvas-overlay"></canvas>
    <div id="fleet-status">connecting...</div>

    <!-- CE Pipeline -->
    <div id="ce-pipeline">
      <div class="ce-node" id="cep-memory">
        m2-memory
        <span class="ce-sub">12k vectors</span>
      </div>
      <div class="ce-arrow" id="cea-1">→</div>
      <div class="ce-node" id="cep-embed">
        BGE-M3
        <span class="ce-sub">embed + search</span>
      </div>
      <div class="ce-arrow" id="cea-2">→</div>
      <div class="ce-node" id="cep-gemini">
        Gemini Flash
        <span class="ce-sub">distill</span>
      </div>
      <div class="ce-arrow" id="cea-3">→</div>
      <div class="ce-node" id="cep-bundle">
        context bundle
        <span class="ce-sub" id="cep-stats">idle</span>
      </div>
    </div>

    <div id="legend">
      <div class="leg-row"><div class="leg-dot" style="background:#00ff88"></div> alive</div>
      <div class="leg-row"><div class="leg-dot" style="background:#ffaa00"></div> degraded</div>
      <div class="leg-row"><div class="leg-dot" style="background:#ff4444"></div> dead / offline</div>
      <div class="leg-row"><div class="leg-dot" style="background:#a080ff"></div> orchestrator</div>
    </div>
  </div>

  <!-- Resize handle -->
  <div id="resize-handle"></div>

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

    <!-- CE Inspector Panel -->
    <div class="panel-section" id="ce-inspector-section" style="max-height:180px;overflow:hidden;">
      <h3>⚡ Context Engineer</h3>
      <div id="ce-inspector-content" style="color:#404060;font-size:0.62rem;">
        no bundles yet
      </div>
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

// ── Panel resize drag ─────────────────────────────────────────────────────────
const panel       = document.getElementById("panel");
const resizeHandle= document.getElementById("resize-handle");
const maximizeBtn = document.getElementById("maximize-btn");
const MIN_PANEL   = 240;   // px
const MAX_PANEL   = 560;   // px
const SNAP_CLOSED = 80;    // px — collapse if dragged below this

let _panelWidthBeforeCollapse = 340;

function applyPanelWidth(w, animate) {
  if (animate) panel.style.transition = "width 0.25s ease";
  else         panel.style.transition = "none";
  panel.style.width = w + "px";
  panel.classList.remove("collapsed");
  setTimeout(() => { resizeCanvas(); initGraph(); }, animate ? 260 : 0);
}

function togglePanel() {
  if (panel.classList.contains("collapsed") || parseInt(panel.style.width||340) < 30) {
    // Restore
    panel.classList.remove("collapsed");
    applyPanelWidth(_panelWidthBeforeCollapse, true);
    resizeHandle.style.display = "";
    maximizeBtn.textContent = "⟷";
    maximizeBtn.title = "Collapse panel";
  } else {
    // Collapse
    _panelWidthBeforeCollapse = parseInt(panel.style.width || 340);
    panel.style.transition = "width 0.25s ease";
    panel.style.width = "0px";
    panel.classList.add("collapsed");
    resizeHandle.style.display = "none";
    maximizeBtn.textContent = "◨";
    maximizeBtn.title = "Restore panel";
    setTimeout(() => { resizeCanvas(); initGraph(); }, 260);
  }
}

resizeHandle.addEventListener("mousedown", e => {
  e.preventDefault();
  resizeHandle.classList.add("dragging");

  const startX     = e.clientX;
  const startWidth = parseInt(panel.style.width || 340);

  function onMove(ev) {
    const delta = startX - ev.clientX;  // dragging left = wider panel
    const newW  = Math.max(0, Math.min(MAX_PANEL, startWidth + delta));

    if (newW < SNAP_CLOSED) {
      // Snap closed
      panel.style.transition = "none";
      panel.style.width = "0px";
      panel.classList.add("collapsed");
      resizeHandle.style.display = "none";
      maximizeBtn.textContent = "◨";
    } else {
      panel.classList.remove("collapsed");
      panel.style.transition = "none";
      panel.style.width = newW + "px";
      maximizeBtn.textContent = "⟷";
    }
    resizeCanvas();
  }

  function onUp() {
    resizeHandle.classList.remove("dragging");
    document.removeEventListener("mousemove", onMove);
    document.removeEventListener("mouseup", onUp);
    const finalW = parseInt(panel.style.width || 0);
    if (finalW >= SNAP_CLOSED) _panelWidthBeforeCollapse = finalW;
    initGraph();
  }

  document.addEventListener("mousemove", onMove);
  document.addEventListener("mouseup",   onUp);
});

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
  checkForCETrigger(data.events || []);

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
  el.innerHTML = tasks.map(t => {
    const ceBadge = t.context_ms > 0
      ? `<span title="${t.context_summary}" style="color:#a080ff;font-size:0.55rem;cursor:help">⚡CE ${t.context_ms}ms</span>`
      : '';
    const ctxLine = t.context_summary
      ? `<div style="color:#604080;font-size:0.6rem;margin-top:2px;font-style:italic">${t.context_summary.slice(0,80)}</div>`
      : '';
    const isPaused    = t.state === "paused";
    const isActive    = ["pending","claimed","running"].includes(t.state);
    const controls = `<div class="task-controls">
      ${isActive && t.state !== "pending"
        ? `<button class="btn-task btn-pause"  onclick="taskAction('${t.id}','pause')">⏸ PAUSE</button>`
        : ''}
      ${isPaused
        ? `<button class="btn-task btn-resume" onclick="taskAction('${t.id}','resume')">▶ RESUME</button>`
        : ''}
      ${isActive || isPaused
        ? `<button class="btn-task btn-cancel" onclick="taskAction('${t.id}','cancel')">✕ CANCEL</button>`
        : ''}
    </div>`;
    return `<div class="task-card" id="tcard-${t.id}">
      <div class="task-header">
        <span class="task-id">#${t.id.slice(0,8)}</span>
        <span class="task-type">${t.type}</span>
        ${ceBadge}
      </div>
      <div class="task-payload">${t.payload}</div>
      ${ctxLine}
      ${t.assigned_to ? `<div class="task-agent">→ ${t.assigned_to}</div>` : ''}
      <div class="state-${t.state}">${t.state}</div>
      ${controls}
    </div>`;
  }).join("");
}

function updateFleetStatus(data) {
  const alive = (data.agents||[]).filter(a=>a.status==="alive").length;
  const total = (data.agents||[]).length;
  document.getElementById("fleet-status").textContent =
    `${alive}/${total} agents alive · ${(data.tasks||[]).length} tasks · ${Date().slice(16,24)} UTC`;
}

async function taskAction(taskId, action) {
  const card = document.getElementById("tcard-" + taskId);
  if (!card) return;

  // Optimistic UI: disable all buttons in card
  card.querySelectorAll(".btn-task").forEach(b => { b.disabled = true; b.style.opacity = "0.4"; });

  const confirmMsg = {
    cancel: `Cancel task ${taskId.slice(0,8)}? This cannot be undone.`,
    pause:  `Pause task ${taskId.slice(0,8)}?`,
  }[action];
  if (confirmMsg && !confirm(confirmMsg)) {
    card.querySelectorAll(".btn-task").forEach(b => { b.disabled = false; b.style.opacity = ""; });
    return;
  }

  try {
    const resp = await fetch(`/api/tasks/${taskId}/${action}`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
    });
    const data = await resp.json();
    if (data.error) {
      alert("Error: " + data.error);
      card.querySelectorAll(".btn-task").forEach(b => { b.disabled = false; b.style.opacity = ""; });
    } else {
      // Flash the card state
      card.style.opacity = "0.5";
      setTimeout(() => { card.style.opacity = ""; }, 600);
    }
  } catch (e) {
    alert("Request failed: " + e);
    card.querySelectorAll(".btn-task").forEach(b => { b.disabled = false; b.style.opacity = ""; });
  }
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

// ── Context Engineer Visualization ───────────────────────────────────────────

let _ceActive = false;
let _ceLastBundle = null;

const _ceNodes  = ["cep-memory","cep-embed","cep-gemini","cep-bundle"];
const _ceArrows = ["cea-1","cea-2","cea-3"];

function cePipelineIdle() {
  _ceNodes.forEach(id => document.getElementById(id)?.classList.remove("active"));
  _ceArrows.forEach(id => document.getElementById(id)?.classList.remove("active"));
  const stats = document.getElementById("cep-stats");
  if (stats && _ceLastBundle) {
    stats.textContent = `${_ceLastBundle.memories_searched}mem · ${_ceLastBundle.generation_ms}ms`;
  } else if (stats) {
    stats.textContent = "idle";
  }
}

async function cePipelineAnimate(taskId) {
  if (_ceActive) return;
  _ceActive = true;

  // Step through pipeline nodes with delays
  const steps = [
    {node: "cep-memory",  arrow: null,    delay: 0},
    {node: "cep-embed",   arrow: "cea-1", delay: 350},
    {node: "cep-gemini",  arrow: "cea-2", delay: 700},
    {node: "cep-bundle",  arrow: "cea-3", delay: 1050},
  ];

  for (const step of steps) {
    await new Promise(r => setTimeout(r, step.delay));
    document.getElementById(step.node)?.classList.add("active");
    if (step.arrow) document.getElementById(step.arrow)?.classList.add("active");
  }

  // Fetch bundle and update inspector
  if (taskId) {
    try {
      const data = await fetch(`/api/ce/context/${taskId}`).then(r => r.json());
      if (!data.error) {
        _ceLastBundle = data;
        updateCEInspector(data);
      }
    } catch(e) {}
  }

  await new Promise(r => setTimeout(r, 1200));
  _ceActive = false;
  cePipelineIdle();
}

function updateCEInspector(bundle) {
  const el = document.getElementById("ce-inspector-content");
  if (!el || !bundle) return;
  const src = bundle.memory_source === "m2-memory"
    ? '<span style="color:#a080ff">m2-memory</span>'
    : '<span style="color:#604080">qdrant-direct</span>';
  const model = bundle._model
    ? `<span style="color:#404060">${bundle._model}</span>`
    : '<span style="color:#333">no distillation</span>';
  el.innerHTML = `
    <div style="display:flex;gap:8px;margin-bottom:6px;align-items:center;">
      <span style="color:#00ff88">${bundle.memories_searched || 0} memories</span>
      <span style="color:#404060">·</span>
      <span style="color:#a080ff">${bundle.generation_ms || 0}ms</span>
      <span style="color:#404060">·</span>${src}
    </div>
    <div style="color:#8080a0;margin-bottom:4px;font-style:italic;">${(bundle.summary||'').slice(0,100)}...</div>
    <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px;">
      ${(bundle.warnings||[]).slice(0,2).map(w=>`<span style="color:#ffaa44;font-size:0.58rem">⚠ ${w.slice(0,40)}</span>`).join('')}
      ${(bundle.prior_decisions||[]).slice(0,2).map(d=>`<span style="color:#505080;font-size:0.58rem">• ${d.slice(0,40)}</span>`).join('')}
    </div>
    <div style="margin-top:6px;">
      <button onclick="openCEModal()" style="background:none;border:1px solid #2a2a5a;color:#6060a0;
        font-family:inherit;font-size:0.58rem;cursor:pointer;padding:2px 6px;border-radius:2px;">
        inspect bundle →
      </button>
    </div>
  `;
}

// Fetch recent CE bundles on load
async function loadCERecent() {
  try {
    const data = await fetch("/api/ce/recent").then(r => r.json());
    if (data.bundles && data.bundles.length > 0) {
      const latest = data.bundles[0];
      _ceLastBundle = latest;
      updateCEInspector(latest);
      const stats = document.getElementById("cep-stats");
      if (stats) stats.textContent = `${latest.memories_searched||0}mem · ${latest.generation_ms||0}ms`;
    }
  } catch(e) {}
}
loadCERecent();

// Trigger CE animation when a task goes from pending → claimed in the event stream
const _seenCEAnimations = new Set();
function checkForCETrigger(events) {
  for (const ev of (events || [])) {
    if (ev.type === "task_claimed" && !_seenCEAnimations.has(ev.task_id)) {
      _seenCEAnimations.add(ev.task_id);
      cePipelineAnimate(ev.task_id);
      break;
    }
  }
}

// CE Modal
function openCEModal() {
  const bundle = _ceLastBundle;
  if (!bundle) return;
  const modal = document.getElementById("ce-modal");
  const inner = document.getElementById("ce-modal-body");
  if (!inner || !modal) return;

  const facts = Object.entries(bundle.key_facts || {}).slice(0,6);

  inner.innerHTML = `
    <div class="ce-section-label">Summary</div>
    <div class="ce-summary">${bundle.summary || '—'}</div>

    ${(bundle.prior_decisions||[]).length ? `
      <div class="ce-section-label">Prior Decisions (${bundle.prior_decisions.length})</div>
      ${bundle.prior_decisions.map(d => `<div class="ce-item">${d}</div>`).join('')}
    ` : ''}

    ${(bundle.warnings||[]).length ? `
      <div class="ce-section-label">Warnings</div>
      ${bundle.warnings.map(w => `<div class="ce-warn">⚠ ${w}</div>`).join('')}
    ` : ''}

    ${facts.length ? `
      <div class="ce-section-label">Key Facts</div>
      ${facts.map(([k,v]) => `<div class="ce-fact">${k}: <span style="color:#c0c0e0">${JSON.stringify(v).slice(0,60)}</span></div>`).join('')}
    ` : ''}

    ${(bundle.relevant_files||[]).length ? `
      <div class="ce-section-label">Relevant Files</div>
      ${bundle.relevant_files.map(f => `<div class="ce-item">${f}</div>`).join('')}
    ` : ''}

    <div class="ce-meta">
      task: ${bundle.task_id} · type: ${bundle.task_type}
      · memories: ${bundle.memories_searched} (${bundle.memory_source})
      · model: ${bundle._model || 'none'}
      · ${bundle.generation_ms}ms
      · distilled: ${bundle._distilled ? '✓' : '✗'}
    </div>
  `;
  modal.classList.add("open");
}

document.addEventListener("keydown", e => {
  if (e.key === "Escape") document.getElementById("ce-modal")?.classList.remove("open");
});
</script>

<!-- CE Modal -->
<div id="ce-modal">
  <div id="ce-modal-inner">
    <button id="ce-modal-close" onclick="document.getElementById('ce-modal').classList.remove('open')">✕ close</button>
    <h2>⚡ Context Bundle Inspector</h2>
    <div id="ce-modal-body"></div>
  </div>
</div>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


# ── Benchmarks page ────────────────────────────────────────────────────────────

BENCH_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>M2O Fleet Benchmarks</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { background:#050508; color:#e0e0ff; font-family:'Courier New',monospace; min-height:100vh; }
  a { color:#a080ff; text-decoration:none; }
  a:hover { text-decoration:underline; }

  #header {
    padding:20px 32px; border-bottom:1px solid #1a1a3a;
    display:flex; align-items:center; justify-content:space-between;
  }
  #header h1 { font-size:1rem; letter-spacing:0.3em; color:#a080ff;
    text-shadow:0 0 20px #a080ff88; text-transform:uppercase; }
  #header .nav { font-size:0.7rem; color:#606090; }
  #header .nav a { color:#6060a0; margin-left:16px; }

  #stats-row {
    display:flex; gap:16px; padding:20px 32px; flex-wrap:wrap;
    border-bottom:1px solid #0d0d1a;
  }
  .stat-card {
    background:#0a0a18; border:1px solid #1a1a3a; border-radius:4px;
    padding:14px 20px; min-width:130px;
  }
  .stat-label { font-size:0.6rem; color:#606090; text-transform:uppercase; letter-spacing:0.15em; margin-bottom:6px; }
  .stat-value { font-size:1.4rem; color:#a080ff; font-weight:bold; }
  .stat-sub { font-size:0.6rem; color:#404060; margin-top:3px; }

  #filters {
    padding:12px 32px; display:flex; gap:12px; align-items:center;
    border-bottom:1px solid #0d0d1a;
  }
  .filter-btn {
    background:#0d0d1a; border:1px solid #2a2a5a; color:#8080a0;
    font-family:inherit; font-size:0.65rem; letter-spacing:0.1em;
    text-transform:uppercase; padding:5px 12px; border-radius:3px; cursor:pointer;
    transition:all 0.2s;
  }
  .filter-btn.active, .filter-btn:hover {
    border-color:#a080ff; color:#a080ff; background:#1a0a3a;
  }
  #search-box {
    background:#0d0d1a; border:1px solid #2a2a5a; color:#e0e0ff;
    font-family:inherit; font-size:0.7rem; padding:5px 10px;
    border-radius:3px; outline:none; min-width:220px;
  }
  #search-box:focus { border-color:#a080ff; }

  #table-wrap { padding:20px 32px; overflow-x:auto; }
  table { width:100%; border-collapse:collapse; font-size:0.68rem; }
  thead tr { border-bottom:1px solid #2a2a4a; }
  th {
    text-align:left; padding:8px 10px; color:#6060a0;
    text-transform:uppercase; letter-spacing:0.12em; font-size:0.6rem;
    white-space:nowrap; cursor:pointer; user-select:none;
  }
  th:hover { color:#a080ff; }
  th .sort-arrow { margin-left:4px; opacity:0.4; }
  th.sorted .sort-arrow { opacity:1; color:#a080ff; }

  tbody tr { border-bottom:1px solid #0d0d1a; transition:background 0.15s; }
  tbody tr:hover { background:#0a0a18; }
  td { padding:10px 10px; vertical-align:top; }

  .td-id { color:#6060a0; font-size:0.6rem; max-width:100px; word-break:break-all; }
  .td-type { color:#a080ff; text-transform:uppercase; font-size:0.6rem; white-space:nowrap; }
  .td-desc { color:#c0c0d8; max-width:280px; line-height:1.5; }
  .td-agent { color:#44ff88; }
  .td-dur { color:#e0c060; white-space:nowrap; }
  .td-tokens { color:#60c0ff; white-space:nowrap; }
  .td-cost { color:#ff9060; white-space:nowrap; }
  .td-artifacts a { color:#6060a0; display:block; font-size:0.6rem; margin-bottom:2px; }
  .td-artifacts a:hover { color:#a080ff; }
  .status-done { color:#44ff88; }
  .status-running { color:#00aaff; }
  .status-failed { color:#ff4444; }

  .td-notes { color:#606080; font-size:0.62rem; max-width:200px; line-height:1.4; }

  #by-type { padding:0 32px 24px; display:flex; gap:16px; flex-wrap:wrap; }
  .type-bar-wrap { background:#0a0a18; border:1px solid #1a1a3a; border-radius:4px; padding:12px 16px; min-width:160px; }
  .type-bar-label { font-size:0.6rem; color:#606090; text-transform:uppercase; margin-bottom:6px; }
  .type-bar-track { background:#0d0d20; border-radius:2px; height:6px; overflow:hidden; }
  .type-bar-fill { height:100%; border-radius:2px; background:linear-gradient(90deg,#a080ff,#6040c0); transition:width 0.8s; }
  .type-bar-count { font-size:0.7rem; color:#a080ff; margin-top:4px; }

  #footer { padding:16px 32px; font-size:0.6rem; color:#404060; border-top:1px solid #0d0d1a; }
  #footer span { margin-right:20px; }
  .refresh-btn {
    background:#0d0d1a; border:1px solid #2a2a5a; color:#8080a0;
    font-family:inherit; font-size:0.65rem; padding:5px 12px;
    border-radius:3px; cursor:pointer; float:right;
  }
  .refresh-btn:hover { border-color:#a080ff; color:#a080ff; }
</style>
</head>
<body>
<div id="header">
  <h1>⚡ Fleet Benchmarks</h1>
  <div class="nav">
    <a href="/">← Fleet Command</a>
    <a href="/api/benchmarks" target="_blank">JSON</a>
    <a href="/api/benchmarks/stats" target="_blank">Stats</a>
  </div>
</div>

<div id="stats-row">
  <div class="stat-card">
    <div class="stat-label">Total Tasks</div>
    <div class="stat-value" id="s-total">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Completed</div>
    <div class="stat-value" id="s-done" style="color:#44ff88">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Running</div>
    <div class="stat-value" id="s-running" style="color:#00aaff">—</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Tokens</div>
    <div class="stat-value" id="s-tokens" style="color:#60c0ff; font-size:1.1rem">—</div>
    <div class="stat-sub" id="s-tokens-sub"></div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Total Cost</div>
    <div class="stat-value" id="s-cost" style="color:#ff9060">—</div>
    <div class="stat-sub">USD (est.)</div>
  </div>
  <div class="stat-card">
    <div class="stat-label">Avg Duration</div>
    <div class="stat-value" id="s-dur" style="font-size:1.1rem">—</div>
  </div>
</div>

<div id="filters">
  <button class="filter-btn active" data-filter="all">ALL</button>
  <button class="filter-btn" data-filter="done">DONE</button>
  <button class="filter-btn" data-filter="running">RUNNING</button>
  <button class="filter-btn" data-filter="failed">FAILED</button>
  <input id="search-box" type="text" placeholder="search descriptions, types, agents..." />
  <button class="refresh-btn" onclick="loadData()">⟳ REFRESH</button>
</div>

<div id="table-wrap">
  <table>
    <thead>
      <tr>
        <th data-col="task_id">ID <span class="sort-arrow">↕</span></th>
        <th data-col="task_type">TYPE <span class="sort-arrow">↕</span></th>
        <th data-col="description">DESCRIPTION</th>
        <th data-col="agent">AGENT <span class="sort-arrow">↕</span></th>
        <th data-col="status">STATUS <span class="sort-arrow">↕</span></th>
        <th data-col="duration_s">DURATION <span class="sort-arrow">↕</span></th>
        <th data-col="tokens_in">TOKENS IN <span class="sort-arrow">↕</span></th>
        <th data-col="tokens_out">TOKENS OUT <span class="sort-arrow">↕</span></th>
        <th data-col="cost_usd">COST <span class="sort-arrow">↕</span></th>
        <th>ARTIFACTS</th>
        <th>NOTES</th>
      </tr>
    </thead>
    <tbody id="bench-tbody"></tbody>
  </table>
</div>

<div style="padding:0 32px 8px; font-size:0.65rem; color:#606090; letter-spacing:0.15em; text-transform:uppercase;">
  Task breakdown by type
</div>
<div id="by-type"></div>

<div id="footer">
  <button class="refresh-btn" onclick="loadData()">⟳ REFRESH</button>
  <span id="footer-ts">—</span>
  <span>machine.machine fleet · m2o-v0.1</span>
  <span><a href="/api/benchmarks/stats" target="_blank">export stats JSON</a></span>
</div>

<script>
let allData = [];
let currentFilter = "all";
let sortCol = "started_at";
let sortDir = -1; // -1 = desc

function fmtDuration(s) {
  s = parseInt(s||0);
  if (!s) return "—";
  if (s < 60) return s + "s";
  if (s < 3600) return Math.round(s/60) + "m";
  return (s/3600).toFixed(1) + "h";
}
function fmtTokens(n, src) {
  n = parseInt(n||0);
  if (!n) return '<span style="color:#404060">pending</span>';
  const val = n >= 1000 ? (n/1000).toFixed(1) + "k" : n;
  return val;
}
function fmtCost(c, src) {
  c = parseFloat(c||0);
  if (!c) return '<span style="color:#404060">—</span>';
  return "$" + c.toFixed(4);
}
function fmtDate(ts) {
  if (!ts) return "—";
  return new Date(parseInt(ts)*1000).toISOString().slice(0,16).replace("T"," ");
}

async function loadData() {
  try {
    const [benchRes, statsRes] = await Promise.all([
      fetch("/api/benchmarks?limit=200"),
      fetch("/api/benchmarks/stats"),
    ]);
    const benchData = await benchRes.json();
    const stats = await statsRes.json();

    allData = benchData.benchmarks || [];
    renderStats(stats);
    renderByType(stats);
    renderTable();
    document.getElementById("footer-ts").textContent = "last updated " + new Date().toISOString().slice(11,19) + " UTC";
  } catch(e) {
    console.error(e);
  }
}

function renderStats(s) {
  document.getElementById("s-total").textContent = s.total_tasks || 0;
  document.getElementById("s-done").textContent = s.done || 0;
  document.getElementById("s-running").textContent = s.running || 0;
  const tk = s.total_tokens || 0;
  document.getElementById("s-tokens").textContent = tk > 0 ? (tk >= 1000 ? (tk/1000).toFixed(0) + "k" : tk) : "—";
  document.getElementById("s-tokens-sub").textContent = tk > 0 ? `↑${(s.total_tokens_in/1000).toFixed(0)}k ↓${(s.total_tokens_out/1000).toFixed(0)}k` : "session API pending";
  document.getElementById("s-cost").textContent = "$" + (s.total_cost_usd||0).toFixed(4);
  document.getElementById("s-dur").textContent = fmtDuration(s.avg_duration_s);
}

function renderByType(s) {
  const byType = s.by_type || {};
  const maxCount = Math.max(...Object.values(byType), 1);
  const colors = {build:"#a080ff", research:"#44ff88", infra:"#ff4466", plan:"#ffaa00", "code-review":"#00ccff", generic:"#808080"};
  const wrap = document.getElementById("by-type");
  wrap.innerHTML = Object.entries(byType).map(([type, count]) => `
    <div class="type-bar-wrap">
      <div class="type-bar-label">${type}</div>
      <div class="type-bar-track"><div class="type-bar-fill" style="width:${Math.round(count/maxCount*100)}%;background:${colors[type]||colors.generic}"></div></div>
      <div class="type-bar-count" style="color:${colors[type]||colors.generic}">${count} task${count>1?"s":""}</div>
    </div>`).join("");
}

function renderTable() {
  const search = document.getElementById("search-box").value.toLowerCase();
  let rows = allData.filter(b => {
    if (currentFilter !== "all" && b.status !== currentFilter) return false;
    if (search) {
      const text = [b.description, b.task_type, b.agent, b.notes, b.bench_id].join(" ").toLowerCase();
      if (!text.includes(search)) return false;
    }
    return true;
  });

  // Sort
  rows.sort((a,b) => {
    let av = a[sortCol], bv = b[sortCol];
    if (["tokens_in","tokens_out","duration_s","cost_usd","started_at"].includes(sortCol)) {
      av = parseFloat(av||0); bv = parseFloat(bv||0);
    }
    if (av < bv) return sortDir;
    if (av > bv) return -sortDir;
    return 0;
  });

  const tbody = document.getElementById("bench-tbody");
  tbody.innerHTML = rows.map(b => {
    const statusCls = `status-${b.status}`;
    const arts = (b.artifacts||[]).map(a => {
      const isUrl = a.startsWith("http");
      return isUrl ? `<a href="${a}" target="_blank">${a.replace(/https?:\\/\\//,"")}</a>`
                   : `<a href="#">${a}</a>`;
    }).join("");
    return `<tr>
      <td class="td-id">${b.bench_id||b.task_id}</td>
      <td class="td-type">${b.task_type}</td>
      <td class="td-desc" title="${b.description}">${b.description}</td>
      <td class="td-agent">${b.agent}</td>
      <td class="${statusCls}">${b.status}</td>
      <td class="td-dur">${fmtDuration(b.duration_s)}</td>
      <td class="td-tokens">${fmtTokens(b.tokens_in)}</td>
      <td class="td-tokens">${fmtTokens(b.tokens_out)}</td>
      <td class="td-cost">${fmtCost(b.cost_usd)}</td>
      <td class="td-artifacts">${arts}</td>
      <td class="td-notes">${b.notes||""}</td>
    </tr>`;
  }).join("") || '<tr><td colspan="11" style="color:#404060;text-align:center;padding:24px">No tasks found</td></tr>';
}

// Filters
document.querySelectorAll(".filter-btn").forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll(".filter-btn").forEach(b=>b.classList.remove("active"));
    btn.classList.add("active");
    currentFilter = btn.dataset.filter;
    renderTable();
  };
});
document.getElementById("search-box").oninput = renderTable;

// Sort headers
document.querySelectorAll("th[data-col]").forEach(th => {
  th.onclick = () => {
    const col = th.dataset.col;
    if (sortCol === col) sortDir *= -1;
    else { sortCol = col; sortDir = -1; }
    document.querySelectorAll("th").forEach(h=>h.classList.remove("sorted"));
    th.classList.add("sorted");
    th.querySelector(".sort-arrow").textContent = sortDir === -1 ? "↓" : "↑";
    renderTable();
  };
});

// Auto-refresh every 10s
loadData();
setInterval(loadData, 10000);
</script>
</body>
</html>"""


@app.get("/benchmarks", response_class=HTMLResponse)
async def benchmarks_page():
    return BENCH_HTML


@app.get("/api/load")
async def load_stats():
    """Return fleet container CPU/RAM stats from Redis"""
    try:
        r = get_redis()
        raw = r.get("fleet:load_stats")
        if raw:
            return JSONResponse(json.loads(raw))
        return JSONResponse({"error": "no data yet", "containers": []})
    except Exception as e:
        return JSONResponse({"error": str(e), "containers": []})
