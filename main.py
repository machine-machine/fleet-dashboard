"""
M2O Fleet Dashboard
Real-time monitoring of the Machine.Machine agent fleet
"""

import asyncio
import json
import time
import os
from datetime import datetime, timezone
from typing import AsyncGenerator

import redis
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

REDIS_HOST = os.getenv("FLEET_REDIS_HOST", "fleet-redis")
REDIS_PORT = int(os.getenv("FLEET_REDIS_PORT", "6379"))
HEARTBEAT_TTL = int(os.getenv("HEARTBEAT_TTL", "180"))  # 3 min = dead

app = FastAPI(title="M2O Fleet Dashboard", docs_url=None, redoc_url=None)


def get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


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

        # Recent events
        raw_events = r.xrevrange("events", count=20) or []
        events = []
        for eid, data in raw_events:
            ts = int(eid.split("-")[0]) // 1000
            events.append({
                "ts": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S"),
                "agent": data.get("agent", "fleet"),
                "type": data.get("type", "?"),
            })

        # Task queue counts
        task_types = ["research", "build", "spawn", "monitor", "generic"]
        task_counts = {}
        for t in task_types:
            task_counts[t] = r.llen(f"tasks:{t}")

        return {
            "agents": agents,
            "events": events,
            "task_counts": task_counts,
            "total_alive": sum(1 for a in agents if a["status"] == "alive"),
            "total_agents": len(agents),
            "redis_ok": True,
            "generated_at": datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC"),
        }
    except Exception as e:
        return {
            "agents": [],
            "events": [],
            "task_counts": {},
            "total_alive": 0,
            "total_agents": 0,
            "redis_ok": False,
            "error": str(e),
            "generated_at": datetime.now(tz=timezone.utc).strftime("%H:%M:%S UTC"),
        }


def fmt_age(s: int) -> str:
    if s < 60:
        return f"{s}s ago"
    elif s < 3600:
        return f"{s // 60}m ago"
    else:
        return f"{s // 3600}h ago"


HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>M2O Fleet ⚡</title>
<style>
  :root {
    --bg: #0a0a0f;
    --surface: #111118;
    --surface2: #1a1a24;
    --border: #2a2a3a;
    --accent: #7c3aed;
    --accent2: #a78bfa;
    --text: #e2e8f0;
    --muted: #64748b;
    --green: #10b981;
    --yellow: #f59e0b;
    --red: #ef4444;
    --cyan: #06b6d4;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'SF Mono', 'Fira Code', monospace;
    font-size: 13px;
    min-height: 100vh;
  }
  header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 28px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
  }
  .logo {
    font-size: 17px;
    font-weight: 700;
    letter-spacing: -0.5px;
    color: var(--accent2);
  }
  .logo span { color: var(--muted); font-weight: 400; }
  .header-meta { color: var(--muted); font-size: 11px; }
  .header-meta .pulse {
    display: inline-block;
    width: 7px; height: 7px;
    border-radius: 50%;
    background: var(--green);
    margin-right: 6px;
    animation: pulse 2s infinite;
  }
  .header-meta .pulse.dead { background: var(--red); animation: none; }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.3; }
  }

  .grid {
    display: grid;
    grid-template-columns: 1fr 1fr 1fr;
    gap: 16px;
    padding: 24px 28px;
  }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }

  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 10px;
    padding: 18px;
  }
  .card.full-width { grid-column: 1 / -1; }
  .card-title {
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 1px;
    color: var(--muted);
    margin-bottom: 14px;
  }

  /* Summary bar */
  .summary {
    display: flex;
    gap: 28px;
    padding: 14px 28px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }
  .stat-val { font-size: 22px; font-weight: 700; color: var(--accent2); }
  .stat-label { font-size: 10px; color: var(--muted); text-transform: uppercase; }

  /* Agent cards */
  .agents { display: flex; flex-direction: column; gap: 10px; }
  .agent {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    background: var(--surface2);
    border: 1px solid var(--border);
    border-radius: 8px;
    transition: border-color 0.2s;
  }
  .agent:hover { border-color: var(--accent); }
  .agent-left { display: flex; align-items: center; gap: 12px; }
  .dot {
    width: 9px; height: 9px;
    border-radius: 50%;
    flex-shrink: 0;
  }
  .dot.alive { background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.degraded { background: var(--yellow); box-shadow: 0 0 6px var(--yellow); }
  .dot.dead { background: var(--red); }
  .agent-name { font-weight: 700; font-size: 14px; }
  .agent-preset {
    font-size: 10px;
    color: var(--accent2);
    background: rgba(124, 58, 237, 0.15);
    border: 1px solid rgba(124, 58, 237, 0.3);
    border-radius: 4px;
    padding: 2px 7px;
  }
  .agent-right { text-align: right; }
  .agent-age { color: var(--muted); font-size: 11px; }
  .agent-health {
    display: flex;
    gap: 10px;
    margin-top: 4px;
    font-size: 11px;
    color: var(--muted);
    justify-content: flex-end;
  }
  .agent-health .ok { color: var(--green); }
  .agent-health .warn { color: var(--yellow); }
  .agent-task {
    font-size: 10px;
    color: var(--cyan);
    margin-top: 3px;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

  /* Events log */
  .events { display: flex; flex-direction: column; gap: 4px; max-height: 280px; overflow-y: auto; }
  .event {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 5px 0;
    border-bottom: 1px solid var(--border);
    font-size: 11px;
  }
  .event:last-child { border-bottom: none; }
  .event-ts { color: var(--muted); width: 60px; flex-shrink: 0; }
  .event-agent { color: var(--accent2); width: 80px; flex-shrink: 0; }
  .event-type { color: var(--text); }
  .event-type.heartbeat { color: var(--muted); }
  .event-type.task_claimed { color: var(--cyan); }
  .event-type.task_done { color: var(--green); }
  .event-type.task_failed { color: var(--red); }
  .event-type.agent_recovered { color: var(--green); font-weight: 700; }

  /* Redis error */
  .error-banner {
    background: rgba(239, 68, 68, 0.1);
    border: 1px solid var(--red);
    border-radius: 8px;
    padding: 14px 18px;
    color: var(--red);
    margin: 20px 28px;
  }

  /* Empty state */
  .empty { color: var(--muted); padding: 20px 0; text-align: center; }

  /* Auto-refresh indicator */
  #refresh-bar {
    height: 2px;
    background: var(--accent);
    width: 100%;
    transform-origin: left;
    animation: shrink 10s linear infinite;
  }
  @keyframes shrink {
    from { transform: scaleX(1); }
    to { transform: scaleX(0); }
  }
</style>
</head>
<body>

<div id="refresh-bar"></div>

<header>
  <div class="logo">⚡ M2O <span>/ fleet</span></div>
  <div class="header-meta">
    <span class="pulse" id="redis-pulse"></span>
    <span id="header-status">connecting...</span>
  </div>
</header>

<div class="summary">
  <div>
    <div class="stat-val" id="stat-alive">—</div>
    <div class="stat-label">Alive</div>
  </div>
  <div>
    <div class="stat-val" id="stat-total">—</div>
    <div class="stat-label">Total</div>
  </div>
  <div>
    <div class="stat-val" id="stat-time">—</div>
    <div class="stat-label">Updated</div>
  </div>
</div>

<div class="grid">
  <div class="card full-width">
    <div class="card-title">Agents</div>
    <div class="agents" id="agents-list">
      <div class="empty">Loading...</div>
    </div>
  </div>

  <div class="card full-width">
    <div class="card-title">Recent Events</div>
    <div class="events" id="events-list">
      <div class="empty">Loading...</div>
    </div>
  </div>
</div>

<script>
const STATUS_ICONS = { alive: '🟢', degraded: '🟡', dead: '🔴' };

async function refresh() {
  try {
    const res = await fetch('/api/fleet');
    const data = await res.json();

    // Header
    const pulse = document.getElementById('redis-pulse');
    const status = document.getElementById('header-status');
    if (data.redis_ok) {
      pulse.className = 'pulse';
      status.textContent = `Redis OK · ${data.generated_at}`;
    } else {
      pulse.className = 'pulse dead';
      status.textContent = `Redis error: ${data.error || 'unknown'}`;
    }

    // Summary
    document.getElementById('stat-alive').textContent = data.total_alive;
    document.getElementById('stat-total').textContent = data.total_agents;
    document.getElementById('stat-time').textContent = data.generated_at;

    // Agents
    const agentsList = document.getElementById('agents-list');
    if (!data.agents.length) {
      agentsList.innerHTML = '<div class="empty">No agents registered yet. Waiting for heartbeats...</div>';
    } else {
      agentsList.innerHTML = data.agents.map(a => {
        const cpuStr = a.cpu != null ? `CPU ${a.cpu.toFixed(0)}%` : '';
        const memStr = a.mem != null ? `MEM ${a.mem.toFixed(0)}%` : '';
        const gwStr = a.gateway != null ? (a.gateway ? '<span class="ok">GW✓</span>' : '<span class="warn">GW✗</span>') : '';
        const healthHtml = [cpuStr, memStr, gwStr].filter(Boolean).join(' · ');
        const taskHtml = a.current_task ? `<div class="agent-task">↪ ${a.current_task}</div>` : '';
        return `
          <div class="agent">
            <div class="agent-left">
              <div class="dot ${a.status}"></div>
              <div>
                <div class="agent-name">${a.id}</div>
                ${taskHtml}
              </div>
              <div class="agent-preset">${a.preset}</div>
            </div>
            <div class="agent-right">
              <div class="agent-age">${a.age_str}</div>
              <div class="agent-health">${healthHtml || '<span>—</span>'}</div>
            </div>
          </div>
        `;
      }).join('');
    }

    // Events
    const eventsList = document.getElementById('events-list');
    if (!data.events.length) {
      eventsList.innerHTML = '<div class="empty">No events yet.</div>';
    } else {
      eventsList.innerHTML = data.events.map(e => `
        <div class="event">
          <span class="event-ts">${e.ts}</span>
          <span class="event-agent">${e.agent}</span>
          <span class="event-type ${e.type}">${e.type}</span>
        </div>
      `).join('');
    }

  } catch(err) {
    document.getElementById('header-status').textContent = 'fetch error: ' + err;
  }
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/fleet")
async def fleet_api():
    return get_fleet_data()


@app.get("/health")
async def health():
    try:
        r = get_redis()
        r.ping()
        return {"status": "ok", "redis": "ok"}
    except Exception as e:
        return {"status": "degraded", "redis": str(e)}
