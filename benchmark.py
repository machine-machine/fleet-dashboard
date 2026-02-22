"""
Fleet Task Benchmark Registry
Stores and retrieves task execution metrics in Redis.
Schema is designed to be publishable as research findings.
"""

import json
import time
import uuid
from typing import Optional, Dict, Any, List
import redis as redis_lib


# Anthropic pricing as of 2025 (per 1M tokens)
MODEL_PRICING = {
    "claude-sonnet-4-6":   {"input": 3.00, "output": 15.00},
    "claude-opus-4-5":     {"input": 15.00, "output": 75.00},
    "claude-haiku-3-5":    {"input": 0.80, "output": 4.00},
    "claude-sonnet-3-5":   {"input": 3.00, "output": 15.00},
    "gemini-flash-3":      {"input": 0.075, "output": 0.30},
    "cerebras-glm-4.7":    {"input": 0.60, "output": 0.60},
    "unknown":             {"input": 3.00, "output": 15.00},
}

BENCHMARK_STREAM = "benchmarks"
BENCHMARK_KEY_PREFIX = "bench:"


def get_redis(host="fleet-redis", port=6379) -> redis_lib.Redis:
    return redis_lib.Redis(host=host, port=port, decode_responses=True)


def estimate_cost(tokens_in: int, tokens_out: int, model: str = "claude-sonnet-4-6") -> float:
    pricing = MODEL_PRICING.get(model, MODEL_PRICING["unknown"])
    return round((tokens_in / 1_000_000 * pricing["input"]) +
                 (tokens_out / 1_000_000 * pricing["output"]), 6)


def record_start(
    r: redis_lib.Redis,
    task_id: str,
    task_type: str,
    description: str,
    agent: str = "m2",
    model: str = "claude-sonnet-4-6",
    preset: str = "orchestrator",
    parent_task_id: Optional[str] = None,
) -> str:
    """Start a benchmark record. Returns bench_id."""
    bench_id = task_id  # use task_id as bench_id for 1:1 traceability
    now = int(time.time())

    record = {
        "bench_id": bench_id,
        "task_id": task_id,
        "task_type": task_type,
        "description": description,
        "agent": agent,
        "model": model,
        "preset": preset,
        "started_at": str(now),
        "completed_at": "",
        "duration_s": "",
        "tokens_in": "0",
        "tokens_out": "0",
        "cost_usd": "0",
        "status": "running",
        "artifacts": "[]",
        "notes": "",
        "parent_task_id": parent_task_id or "",
        "fleet_version": "m2o-v0.1",
    }

    r.hset(f"{BENCHMARK_KEY_PREFIX}{bench_id}", mapping=record)
    r.zadd("bench:index", {bench_id: now})  # sorted set for ordering
    r.xadd(BENCHMARK_STREAM, {"event": "started", "bench_id": bench_id, "task_type": task_type})
    return bench_id


def record_complete(
    r: redis_lib.Redis,
    bench_id: str,
    status: str = "done",
    tokens_in: int = 0,
    tokens_out: int = 0,
    model: str = "claude-sonnet-4-6",
    artifacts: Optional[List[str]] = None,
    notes: str = "",
):
    """Complete a benchmark record with final metrics."""
    now = int(time.time())
    data = r.hgetall(f"{BENCHMARK_KEY_PREFIX}{bench_id}") or {}
    started = int(data.get("started_at", now))
    duration = now - started
    cost = estimate_cost(tokens_in, tokens_out, model)

    r.hset(f"{BENCHMARK_KEY_PREFIX}{bench_id}", mapping={
        "completed_at": str(now),
        "duration_s": str(duration),
        "tokens_in": str(tokens_in),
        "tokens_out": str(tokens_out),
        "cost_usd": str(cost),
        "status": status,
        "artifacts": json.dumps(artifacts or []),
        "notes": notes,
        "model": model,
    })
    r.xadd(BENCHMARK_STREAM, {
        "event": "completed",
        "bench_id": bench_id,
        "status": status,
        "duration_s": str(duration),
        "cost_usd": str(cost),
    })
    return {"bench_id": bench_id, "duration_s": duration, "cost_usd": cost}


def get_benchmark(r: redis_lib.Redis, bench_id: str) -> Optional[Dict[str, Any]]:
    data = r.hgetall(f"{BENCHMARK_KEY_PREFIX}{bench_id}")
    if not data:
        return None
    # Parse JSON fields
    try:
        data["artifacts"] = json.loads(data.get("artifacts", "[]"))
    except Exception:
        data["artifacts"] = []
    for f in ("tokens_in", "tokens_out", "duration_s"):
        try:
            data[f] = int(data.get(f, 0) or 0)
        except Exception:
            data[f] = 0
    try:
        data["cost_usd"] = float(data.get("cost_usd", 0) or 0)
    except Exception:
        data["cost_usd"] = 0.0
    return data


def list_benchmarks(r: redis_lib.Redis, limit: int = 50, status: Optional[str] = None) -> List[Dict]:
    """List benchmarks sorted by start time (newest first)."""
    ids = r.zrevrange("bench:index", 0, limit - 1)
    results = []
    for bid in ids:
        data = get_benchmark(r, bid)
        if data:
            if status is None or data.get("status") == status:
                results.append(data)
    return results


def get_fleet_stats(r: redis_lib.Redis) -> Dict[str, Any]:
    """Aggregate stats across all benchmarks."""
    all_benches = list_benchmarks(r, limit=500)
    done = [b for b in all_benches if b.get("status") == "done"]
    failed = [b for b in all_benches if b.get("status") == "failed"]

    total_tokens_in = sum(b.get("tokens_in", 0) for b in done)
    total_tokens_out = sum(b.get("tokens_out", 0) for b in done)
    total_cost = sum(b.get("cost_usd", 0.0) for b in done)
    durations = [b.get("duration_s", 0) for b in done if b.get("duration_s", 0) > 0]
    avg_duration = round(sum(durations) / len(durations)) if durations else 0

    by_type: Dict[str, int] = {}
    for b in done:
        tt = b.get("task_type", "unknown")
        by_type[tt] = by_type.get(tt, 0) + 1

    by_agent: Dict[str, int] = {}
    for b in done:
        ag = b.get("agent", "unknown")
        by_agent[ag] = by_agent.get(ag, 0) + 1

    return {
        "total_tasks": len(all_benches),
        "done": len(done),
        "failed": len(failed),
        "running": len([b for b in all_benches if b.get("status") == "running"]),
        "total_tokens_in": total_tokens_in,
        "total_tokens_out": total_tokens_out,
        "total_tokens": total_tokens_in + total_tokens_out,
        "total_cost_usd": round(total_cost, 4),
        "avg_duration_s": avg_duration,
        "by_type": by_type,
        "by_agent": by_agent,
    }
