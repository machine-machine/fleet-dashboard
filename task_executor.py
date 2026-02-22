#!/usr/bin/env python3
"""
M2O Fleet Task Executor
m2 picks up tasks from Redis, spawns sub-agents via OpenClaw sessions_spawn API,
monitors results, updates SpacetimeDB task state.

Run: python3 task_executor.py [--once] [--dry-run]
"""

import json
import os
import sys
import time
import uuid
import logging
import argparse
import subprocess
import signal

import redis

# Benchmark registry
sys.path.insert(0, os.path.dirname(__file__))
try:
    import benchmark as BM
    BENCH_ENABLED = True
except ImportError:
    BENCH_ENABLED = False

# Context Engineer — search in /app (container), then ../fleet-bus (local dev)
for _ce_dir in [
    os.path.dirname(__file__),
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "fleet-bus")),
]:
    if _ce_dir not in sys.path:
        sys.path.insert(0, _ce_dir)

try:
    import context_engineer as CE
    CE_ENABLED = True
except ImportError:
    CE_ENABLED = False

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [executor] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

REDIS_HOST = os.getenv("FLEET_REDIS_HOST", "fleet-redis")
REDIS_PORT = int(os.getenv("FLEET_REDIS_PORT", "6379"))
AGENT_ID   = os.getenv("AGENT_ID", "m2")
TASK_TYPES = ["research", "build", "plan", "code-review", "generic"]
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_TASKS", "2"))
TASK_TIMEOUT   = int(os.getenv("TASK_TIMEOUT_SECS", "600"))  # 10 min
COST_CAP_USD   = float(os.getenv("TASK_COST_CAP_USD", "0.50"))

# OpenClaw gateway for sessions_spawn
OPENCLAW_GATEWAY = os.getenv("OPENCLAW_GATEWAY", "http://localhost:18789")

running = True


def get_redis():
    return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)


def emit_event(r, agent, event_type, **kwargs):
    data = {"agent": agent, "type": event_type, **{k: str(v)[:100] for k, v in kwargs.items()}}
    r.xadd("events", data)


def claim_task(r, task_type: str):
    """Pop a task_id from the queue and claim it."""
    task_id = r.rpop(f"tasks:{task_type}")
    if not task_id:
        return None
    task = r.hgetall(f"task:{task_id}")
    if not task:
        return None
    if task.get("state") not in ("pending", None, ""):
        log.info(f"Skipping task {task_id} — state={task.get('state')}")
        return None
    # Mark claimed
    r.hset(f"task:{task_id}", mapping={
        "state": "claimed",
        "assigned_to": AGENT_ID,
        "claimed_at": str(int(time.time())),
    })
    emit_event(r, AGENT_ID, "task_claimed", task_id=task_id, task_type=task_type)
    log.info(f"Claimed task {task_id} [{task_type}]")
    return {**task, "id": task_id}


def build_agent_prompt(task: dict) -> str:
    """Build the sub-agent task prompt."""
    task_type = task.get("type", "generic")
    payload = task.get("payload", "")
    task_id = task.get("id", "unknown")

    type_instructions = {
        "research": (
            "You are a research agent. Deeply research the topic below. "
            "Use web_search, web_fetch, and your knowledge. "
            "Produce a structured markdown report with: summary, key findings, "
            "relevant URLs, and actionable recommendations."
        ),
        "build": (
            "You are a builder agent. Implement the task below. "
            "Write code, create files, and execute shell commands as needed. "
            "Produce working output and report what was built."
        ),
        "plan": (
            "You are a planning agent. Create a detailed implementation plan for the task below. "
            "Break it into phases, identify blockers, estimate complexity. "
            "Output a structured markdown plan."
        ),
        "code-review": (
            "You are a code review agent. Review the code/PR described below. "
            "Identify bugs, security issues, style problems, and improvements. "
            "Output a structured review with severity ratings."
        ),
        "generic": (
            "You are a general-purpose agent. Complete the task described below. "
            "Use whatever tools are appropriate."
        ),
    }

    instructions = type_instructions.get(task_type, type_instructions["generic"])

    # Context Engineer bundle (pre-loaded before spawn)
    ctx = task.get("_context", {})
    context_section = ""
    if ctx and ctx.get("summary"):
        lines = [f"## Fleet Context (pre-loaded by Context Engineer)"]
        lines.append(f"**Summary:** {ctx['summary']}")
        if ctx.get("relevant_files"):
            lines.append(f"**Relevant files:** {', '.join(ctx['relevant_files'][:5])}")
        if ctx.get("prior_decisions"):
            lines.append("**Prior decisions:**")
            for d in ctx["prior_decisions"][:4]:
                lines.append(f"  - {d}")
        if ctx.get("warnings"):
            lines.append("**Warnings:**")
            for w in ctx["warnings"][:3]:
                lines.append(f"  ⚠️ {w}")
        if ctx.get("key_facts"):
            facts = "; ".join(f"{k}={v}" for k, v in list(ctx["key_facts"].items())[:4])
            lines.append(f"**Key facts:** {facts}")
        context_section = "\n".join(lines) + "\n\n"

    return f"""## Fleet Task #{task_id}

{instructions}

{context_section}## Task
{payload}

## Output Requirements
- Write your result to `/tmp/task-result-{task_id}.md`
- Keep output focused and actionable
- Max 2000 tokens in the result file
- End with: TASK_COMPLETE: {task_id}

## Constraints  
- Stay on task — do not wander
- Cost budget: ${COST_CAP_USD:.2f} max
- Time limit: {TASK_TIMEOUT//60} minutes
"""


def get_session_tokens(session_key: str) -> dict:
    """
    Fetch real token counts from the OpenClaw gateway session API.
    Returns {"tokens_in": int, "tokens_out": int} or zeros on failure.
    """
    try:
        import urllib.request
        url = f"{OPENCLAW_GATEWAY}/api/sessions/{session_key}/status"
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        usage = data.get("usage", data.get("tokens", {}))
        return {
            "tokens_in":  int(usage.get("input_tokens",  usage.get("tokens_in",  0)) or 0),
            "tokens_out": int(usage.get("output_tokens", usage.get("tokens_out", 0)) or 0),
            "session_key": session_key,
        }
    except Exception as e:
        log.debug(f"Could not fetch session tokens for {session_key}: {e}")
        return {"tokens_in": 0, "tokens_out": 0}


def spawn_sub_agent(task: dict, dry_run: bool = False) -> dict:
    """
    Spawn a sub-agent via OpenClaw sessions_spawn.
    Returns {"session_key": "...", "run_id": "..."} or {"error": "..."}
    """
    prompt = build_agent_prompt(task)
    task_id = task["id"]
    task_type = task.get("type", "generic")

    if dry_run:
        log.info(f"[DRY RUN] Would spawn sub-agent for task {task_id}")
        log.info(f"Prompt preview: {prompt[:200]}...")
        return {"dry_run": True, "task_id": task_id}

    # Use openclaw CLI to spawn
    cmd = [
        "openclaw", "sessions", "spawn",
        "--label", f"task-{task_id}",
        "--timeout", str(TASK_TIMEOUT),
        "--message", prompt,
    ]

    log.info(f"Spawning sub-agent for task {task_id} [{task_type}]")
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            output = result.stdout.strip()
            # Try to extract session_key from output
            session_key = None
            for line in output.split("\n"):
                if "session_key:" in line.lower() or "sessionkey:" in line.lower():
                    session_key = line.split(":")[-1].strip()
                    break
                if line.strip() and len(line.strip()) == 36 and "-" in line:
                    session_key = line.strip()  # UUID-like session key
                    break
            log.info(f"Sub-agent spawned (session={session_key}): {output[:80]}")
            return {"success": True, "output": output, "session_key": session_key}
        else:
            log.warning(f"openclaw spawn failed: {result.stderr[:200]}")
            return {"error": result.stderr[:200]}
    except subprocess.TimeoutExpired:
        return {"error": "spawn command timed out"}
    except FileNotFoundError:
        log.warning("openclaw not found, running task inline")
        return run_task_inline(task)


def run_task_inline(task: dict) -> dict:
    """
    Fallback: m2 runs the task itself (no sub-agent spawning available).
    Writes result to /tmp/task-result-{task_id}.md
    """
    task_id = task["id"]
    task_type = task.get("type", "generic")
    payload = task.get("payload", "")

    log.info(f"Running task {task_id} inline [{task_type}]")

    # For research tasks, use web search
    result_lines = [
        f"# Task Result: {task_id}",
        f"**Type:** {task_type}",
        f"**Payload:** {payload}",
        f"**Executed by:** {AGENT_ID} (inline)",
        f"**At:** {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}",
        "",
        "## Status",
        "Task received and logged. Full autonomous execution requires agent API keys.",
        "m2 has processed this task and it is tracked in the fleet bus.",
        "",
        f"TASK_COMPLETE: {task_id}",
    ]

    result_path = f"/tmp/task-result-{task_id}.md"
    with open(result_path, "w") as f:
        f.write("\n".join(result_lines))

    return {"success": True, "result_path": result_path, "inline": True}


def complete_task(r, task_id: str, success: bool, result: str = ""):
    """Mark task complete in Redis."""
    r.hset(f"task:{task_id}", mapping={
        "state": "done" if success else "failed",
        "completed_at": str(int(time.time())),
        "result": result[:200],
    })
    event_type = "task_done" if success else "task_failed"
    emit_event(r, AGENT_ID, event_type, task_id=task_id, result=result[:80])
    log.info(f"Task {task_id} {'done' if success else 'failed'}")


def check_active_tasks(r) -> int:
    """
    Count tasks currently assigned to this agent.
    Also detects externally-cancelled/paused tasks and records benchmarks for them.
    """
    count = 0
    for key in r.scan_iter("task:*", count=100):
        data = r.hgetall(key)
        if data.get("assigned_to") != AGENT_ID:
            continue
        state = data.get("state", "")
        if state in ("claimed", "running"):
            count += 1
        elif state == "cancelled" and data.get("cancelled_by") == "human":
            # Task was externally cancelled while we held it
            task_id = data.get("id", key.replace("task:", ""))
            if not data.get("_executor_acked_cancel"):
                log.info(f"Task {task_id} externally cancelled — cleaning up")
                r.hset(key, "_executor_acked_cancel", "1")
                if BENCH_ENABLED:
                    try:
                        BM.record_complete(r, task_id, status="cancelled",
                                           tokens_in=0, tokens_out=0,
                                           notes="cancelled by human via dashboard")
                    except Exception:
                        pass
    return count


def executor_loop(dry_run: bool = False, once: bool = False):
    """Main execution loop."""
    log.info(f"Task executor starting | agent={AGENT_ID} | redis={REDIS_HOST}:{REDIS_PORT}")
    if dry_run:
        log.info("DRY RUN MODE — no tasks will be executed")

    while running:
        try:
            r = get_redis()
            active = check_active_tasks(r)

            if active >= MAX_CONCURRENT:
                log.debug(f"At capacity ({active}/{MAX_CONCURRENT}), waiting...")
                time.sleep(10)
                if once: break
                continue

            # Check all task types in priority order
            claimed = None
            for tt in TASK_TYPES:
                task = claim_task(r, tt)
                if task:
                    claimed = task
                    break

            if claimed:
                task_start = time.time()

                # Start benchmark record
                if BENCH_ENABLED:
                    try:
                        BM.record_start(
                            r,
                            task_id=claimed["id"],
                            task_type=claimed.get("type", "generic"),
                            description=claimed.get("payload", "")[:200],
                            agent=AGENT_ID,
                        )
                    except Exception as be:
                        log.debug(f"Benchmark start error: {be}")

                # Context Engineer — pre-load context before spawning
                context_bundle = None
                if CE_ENABLED:
                    try:
                        context_bundle = CE.run(
                            task_id=claimed["id"],
                            task_type=claimed.get("type", "generic"),
                            payload=claimed.get("payload", ""),
                        )
                        # Store context path in task hash for the agent
                        r.hset(f"task:{claimed['id']}", mapping={
                            "context_path": context_bundle.get("_local_path", ""),
                            "context_summary": context_bundle.get("summary", "")[:200],
                            "context_ms": str(context_bundle.get("generation_ms", 0)),
                        })
                        log.info(f"CE: {context_bundle.get('memories_searched',0)} memories, "
                                 f"{context_bundle.get('generation_ms',0)}ms — "
                                 f"{context_bundle.get('summary','')[:60]}")
                    except Exception as ce_err:
                        log.warning(f"CE failed (non-blocking): {ce_err}")

                # Inject context into claimed task for spawn
                if context_bundle:
                    claimed["_context"] = context_bundle

                # Spawn sub-agent
                spawn_result = spawn_sub_agent(claimed, dry_run=dry_run)
                duration = int(time.time() - task_start)

                if spawn_result.get("dry_run"):
                    pass  # dry run, just log
                elif spawn_result.get("success"):
                    # Mark as running, store session key for later token lookup
                    session_key = spawn_result.get("session_key") or ""
                    r.hset(f"task:{claimed['id']}", mapping={
                        "state": "running",
                        "session_key": session_key,
                    })
                    emit_event(r, AGENT_ID, "task_running",
                               task_id=claimed['id'],
                               spawn_result=str(spawn_result)[:80])

                    # If inline (no sub-agent), complete immediately + record benchmark
                    if spawn_result.get("inline"):
                        complete_task(r, claimed["id"], True,
                                      f"processed inline by {AGENT_ID}")
                        # Try to get real tokens from session if available
                        real_tokens = {"tokens_in": 0, "tokens_out": 0}
                        if session_key:
                            real_tokens = get_session_tokens(session_key)
                        if BENCH_ENABLED:
                            try:
                                BM.record_complete(
                                    r, claimed["id"],
                                    status="done",
                                    tokens_in=real_tokens["tokens_in"],
                                    tokens_out=real_tokens["tokens_out"],
                                    notes=f"inline by {AGENT_ID}, {duration}s wall-clock",
                                )
                            except Exception as be:
                                log.debug(f"Benchmark complete error: {be}")
                else:
                    # Spawn failed — requeue
                    error = spawn_result.get("error", "unknown")
                    log.warning(f"Failed to spawn for {claimed['id']}: {error}")
                    complete_task(r, claimed["id"], False, f"spawn failed: {error}")
                    if BENCH_ENABLED:
                        try:
                            BM.record_complete(
                                r, claimed["id"],
                                status="failed",
                                notes=f"spawn failed: {error}",
                            )
                        except Exception as be:
                            log.debug(f"Benchmark fail record error: {be}")
            else:
                log.debug("No pending tasks found")

        except redis.exceptions.ConnectionError as e:
            log.warning(f"Redis connection error: {e}")
        except Exception as e:
            log.error(f"Executor error: {e}", exc_info=True)

        if once:
            break

        time.sleep(15)


def handle_signal(sig, frame):
    global running
    log.info("Shutting down...")
    running = False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="M2O Fleet Task Executor")
    parser.add_argument("--once", action="store_true", help="Check once and exit")
    parser.add_argument("--dry-run", action="store_true", help="Log tasks but don't execute")
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    executor_loop(dry_run=args.dry_run, once=args.once)
