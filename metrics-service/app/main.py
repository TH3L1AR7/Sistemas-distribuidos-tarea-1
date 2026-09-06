import time
import json
from pathlib import Path
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()
LOG_PATH = Path("/data/events.jsonl")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


class Event(BaseModel):
    event_type: str
    query_type: str | None = None
    latency_ms: float | None = None
    detail: str | None = None


@app.post("/event")
async def log_event(ev: Event):
    record = ev.model_dump()
    record["timestamp"] = time.time()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")
    return {"status": "logged"}


@app.get("/events")
async def get_events():
    if not LOG_PATH.exists():
        return []
    with open(LOG_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@app.get("/summary")
async def get_summary():
    if not LOG_PATH.exists():
        return {}
    events = []
    with open(LOG_PATH, encoding="utf-8") as f:
        events = [json.loads(line) for line in f if line.strip()]
    if not events:
        return {}
    hits = [e for e in events if e["event_type"] == "hit"]
    misses = [e for e in events if e["event_type"] == "miss"]
    errors = [e for e in events if e["event_type"] == "error"]
    evictions = [e for e in events if e["event_type"] == "eviction"]
    total = len(hits) + len(misses)
    hit_rate = len(hits) / total if total > 0 else 0
    latencies = [e["latency_ms"] for e in events if e.get("latency_ms") is not None]
    latencies_sorted = sorted(latencies)
    def percentile(data, p):
        if not data:
            return None
        idx = int(len(data) * p / 100)
        return data[min(idx, len(data) - 1)]
    return {
        "total_requests": total,
        "hits": len(hits),
        "misses": len(misses),
        "errors": len(errors),
        "evictions": len(evictions),
        "hit_rate": round(hit_rate, 4),
        "latency_p50_ms": percentile(latencies_sorted, 50),
        "latency_p95_ms": percentile(latencies_sorted, 95),
    }


@app.delete("/events")
async def clear_events():
    if LOG_PATH.exists():
        LOG_PATH.unlink()
    return {"status": "cleared"}