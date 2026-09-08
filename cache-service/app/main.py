import time
import json
import os
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Any
from contextlib import asynccontextmanager
from . import cache_backend

app = FastAPI()

SCRAPER_URL = os.getenv("SCRAPER_URL", "http://scraper-service:8000")
METRICS_URL = os.getenv("METRICS_URL", "http://metrics-service:8000")

TTL = {
    "Q1": int(os.getenv("CACHE_TTL_Q1", 3600)),
    "Q2": int(os.getenv("CACHE_TTL_Q2", 300)),
    "Q3": int(os.getenv("CACHE_TTL_Q3", 1800)),
    "Q4": int(os.getenv("CACHE_TTL_Q4", 600)),
    "Q5": int(os.getenv("CACHE_TTL_Q5", 900)),
}

_http: httpx.AsyncClient | None = None


@asynccontextmanager
async def lifespan(app):
    global _http
    await cache_backend.init()
    _http = httpx.AsyncClient(timeout=60.0)
    yield
    await _http.aclose()
    await cache_backend.close()

app = FastAPI(lifespan=lifespan)


class QueryRequest(BaseModel):
    query_type: str
    params: dict[str, Any] = {}


def _build_key(query_type: str, params: dict) -> str:
    if query_type == "Q3":
        teams = sorted([params.get("team_a", ""), params.get("team_b", "")])
        return f"Q3:{teams[0]}:{teams[1]}"
    if query_type == "Q5":
        return "Q5"
    parts = ":".join(f"{v}" for v in sorted(params.values()))
    return f"{query_type}:{parts}"


async def _log(event_type: str, query_type: str, latency_ms: float, detail: str = None):
    try:
        await _http.post(f"{METRICS_URL}/event", json={
            "event_type": event_type,
            "query_type": query_type,
            "latency_ms": latency_ms,
            "detail": detail,
        })
    except Exception:
        pass


@app.post("/query")
async def query(req: QueryRequest):
    key = _build_key(req.query_type, req.params)
    t0 = time.perf_counter()

    cached = await cache_backend.get(key)
    if cached is not None:
        latency_ms = (time.perf_counter() - t0) * 1000
        await _log("hit", req.query_type, latency_ms)
        return {"source": "cache", "query_type": req.query_type, "data": json.loads(cached)}

    try:
        resp = await _http.post(f"{SCRAPER_URL}/scrape", json={
            "query_type": req.query_type,
            "params": req.params,
        })
        resp.raise_for_status()
        data = resp.json()["data"]
    except Exception as e:
        latency_ms = (time.perf_counter() - t0) * 1000
        await _log("error", req.query_type, latency_ms, str(e))
        raise HTTPException(status_code=502, detail=f"Error de scraping: {e}")

    await cache_backend.set(key, json.dumps(data), ttl=TTL[req.query_type])
    latency_ms = (time.perf_counter() - t0) * 1000
    await _log("miss", req.query_type, latency_ms)
    return {"source": "scraper", "query_type": req.query_type, "data": data}


@app.get("/health")
async def health():
    return {"status": "ok"}