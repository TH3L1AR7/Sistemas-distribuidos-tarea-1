

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import statistics
import sys
import time
from dataclasses import dataclass, asdict, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx


# configuración


CACHE_URL = os.getenv("CACHE_URL", "http://cache-service:8000")
TEAMS_FILE = os.getenv("TEAMS_FILE", str(Path(__file__).with_name("teams.json")))
RESULTS_DIR = os.getenv("RESULTS_DIR", "/app/results")


DEFAULT_QUERY_MIX: dict[str, float] = {
    "Q1": 0.20,   
    "Q2": 0.25,  
    "Q3": 0.15,   
    "Q4": 0.10,   
    "Q5": 0.30,   
}


FALLBACK_TEAMS = [
    "colocolo", "udechile", "ucatolica", "nublense", "coquimbo", "audax",
    "everton", "huachipato", "palestino", "cobresal", "ohiggins", "laserena",
    "ulacalera", "udeconce", "limache", "depconce",
]



# Distribuciones

class Zipf:
    

    def __init__(self, n: int, s: float, rng: random.Random):
        self.n = n
        self.s = s
        self._rng = rng
        weights = [1.0 / ((k + 1) ** s) for k in range(n)]
        total = sum(weights)
        acc = 0.0
        self._cdf: list[float] = []
        for w in weights:
            acc += w / total
            self._cdf.append(acc)
        self._cdf[-1] = 1.0

    def sample(self) -> int:
        import bisect
        return bisect.bisect_left(self._cdf, self._rng.random())

    def top_mass(self, k: int) -> float:
        
        return self._cdf[min(k, self.n) - 1]


class Uniform:
    def __init__(self, n: int, rng: random.Random):
        self.n = n
        self._rng = rng

    def sample(self) -> int:
        return self._rng.randrange(self.n)

    def top_mass(self, k: int) -> float:
        return min(k, self.n) / self.n


def make_selector(kind: str, n: int, s: float, rng: random.Random):
    if n == 0:
        raise ValueError("universo vacío")
    return Zipf(n, s, rng) if kind == "zipf" else Uniform(n, rng)



# universos de consulta


def load_teams(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        codes = [t["code"] for t in data["teams"]]
        return codes or FALLBACK_TEAMS
    except (OSError, KeyError, json.JSONDecodeError):
        print(f"[gen] aviso: no se pudo leer {path}, usando lista embebida",
              file=sys.stderr)
        return FALLBACK_TEAMS


def build_pairs(teams: list[str]) -> list[tuple[str, str]]:
   
    pairs = []
    for i in range(len(teams)):
        for j in range(i + 1, len(teams)):
            pairs.append(tuple(sorted((teams[i], teams[j]))))
    return pairs


def build_periods(n: int = 20, window: int = 7) -> list[tuple[str, str]]:
    
    today = date.today()
    periods = []
    for i in range(n):
        end = today - timedelta(days=i * window)
        start = end - timedelta(days=window - 1)
        periods.append((start.isoformat(), end.isoformat()))
    return periods


class QueryFactory:
    

    def __init__(self, teams: list[str], dist: str, zipf_s: float,
                 rng: random.Random, query_mix: dict[str, float]):
        self.rng = rng
        self.teams = teams
        self.pairs = build_pairs(teams)
        self.periods = build_periods()

        self.sel_team = make_selector(dist, len(self.teams), zipf_s, rng)
        self.sel_pair = make_selector(dist, len(self.pairs), zipf_s, rng)
        self.sel_period = make_selector(dist, len(self.periods), zipf_s, rng)

        self.types = list(query_mix.keys())
        self.weights = list(query_mix.values())

    def next_query(self) -> tuple[str, dict[str, Any]]:
        qt = self.rng.choices(self.types, weights=self.weights, k=1)[0]

        if qt in ("Q1", "Q2"):
            return qt, {"team": self.teams[self.sel_team.sample()]}
        if qt == "Q3":
            a, b = self.pairs[self.sel_pair.sample()]
            return qt, {"team_a": a, "team_b": b}
        if qt == "Q4":
            d_from, d_to = self.periods[self.sel_period.sample()]
            return qt, {"date_from": d_from, "date_to": d_to}
        return "Q5", {}



# registro de resultados


@dataclass
class Record:
    seq: int
    t_offset_s: float     
    query_type: str
    params: str
    status: int            
    source: str           
    latency_ms: float
    error: str = ""


@dataclass
class RunConfig:
    dist: str
    arrival: str
    rate: float
    duration: float | None
    requests: int | None
    zipf_s: float
    seed: int
    max_inflight: int
    query_mix: dict[str, float] = field(default_factory=dict)
    label: str = ""



# motor de carga


class TrafficGenerator:
    def __init__(self, cfg: RunConfig, factory: QueryFactory,
                 client: httpx.AsyncClient):
        self.cfg = cfg
        self.factory = factory
        self.client = client
        self.records: list[Record] = []
        self.rng = random.Random(cfg.seed + 7919)
        self._sem = asyncio.Semaphore(cfg.max_inflight)
        self._t0 = 0.0
        self._seq = 0
        self.dropped = 0

    def _next_gap(self) -> float:
        """Intervalo hasta el siguiente arribo."""
        if self.cfg.arrival == "poisson":
            
            return self.rng.expovariate(self.cfg.rate)
        return 1.0 / self.cfg.rate

    async def _send(self, seq: int, qt: str, params: dict[str, Any]) -> None:
        t_off = time.perf_counter() - self._t0
        t_start = time.perf_counter()
        try:
            resp = await self.client.post(
                f"{CACHE_URL}/query",
                json={"query_type": qt, "params": params},
            )
            latency_ms = (time.perf_counter() - t_start) * 1000
            if resp.status_code == 200:
                source = resp.json().get("source", "unknown")
                err = ""
            else:
                source = "error"
                err = resp.text[:200]
            rec = Record(seq, t_off, qt, json.dumps(params, sort_keys=True),
                         resp.status_code, source, latency_ms, err)
        except Exception as e:
            latency_ms = (time.perf_counter() - t_start) * 1000
            rec = Record(seq, t_off, qt, json.dumps(params, sort_keys=True),
                         0, "error", latency_ms, f"{type(e).__name__}: {e}")
        finally:
            self._sem.release()

        self.records.append(rec)

    async def run(self) -> None:
        cfg = self.cfg
        self._t0 = time.perf_counter()
        deadline = self._t0 + cfg.duration if cfg.duration else float("inf")
        limit = cfg.requests or sys.maxsize
        tasks: list[asyncio.Task] = []
        next_arrival = self._t0

        print(f"[gen] iniciando: dist={cfg.dist} arribo={cfg.arrival} "
              f"rate={cfg.rate}/s objetivo={cfg.requests or 'n/a'} "
              f"duracion={cfg.duration or 'n/a'}s")

        while self._seq < limit and time.perf_counter() < deadline:
            next_arrival += self._next_gap()
            sleep_for = next_arrival - time.perf_counter()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)

            
            if self._sem.locked():
                self.dropped += 1
                continue
            await self._sem.acquire()

            qt, params = self.factory.next_query()
            self._seq += 1
            tasks.append(asyncio.create_task(self._send(self._seq, qt, params)))

            if len(tasks) >= 512:
                tasks = [t for t in tasks if not t.done()]

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self.wall_time = time.perf_counter() - self._t0
        print(f"[gen] fin: {len(self.records)} respuestas en "
              f"{self.wall_time:.1f}s, {self.dropped} arribos descartados")



# métricas y salida


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * (p / 100)
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def summarize(records: list[Record], wall_time: float, cfg: RunConfig,
              dropped: int) -> dict[str, Any]:
    hits = [r for r in records if r.source == "cache"]
    misses = [r for r in records if r.source == "scraper"]
    errors = [r for r in records if r.source == "error"]
    ok = hits + misses
    served = len(hits) + len(misses)

    lat_all = [r.latency_ms for r in ok]
    lat_hit = [r.latency_ms for r in hits]
    lat_miss = [r.latency_ms for r in misses]

    per_type: dict[str, Any] = {}
    for qt in sorted({r.query_type for r in records}):
        sub = [r for r in records if r.query_type == qt]
        h = sum(1 for r in sub if r.source == "cache")
        m = sum(1 for r in sub if r.source == "scraper")
        lat = [r.latency_ms for r in sub if r.source in ("cache", "scraper")]
        per_type[qt] = {
            "requests": len(sub),
            "hits": h,
            "misses": m,
            "hit_rate": h / (h + m) if (h + m) else 0.0,
            "p50_ms": round(percentile(lat, 50), 2),
            "p95_ms": round(percentile(lat, 95), 2),
        }

    
    t_cache = statistics.fmean(lat_hit) if lat_hit else 0.0
    t_scraper = statistics.fmean(lat_miss) if lat_miss else 0.0
    total = len(records) or 1
    efficiency = (len(hits) * t_cache - len(misses) * t_scraper) / total

    return {
        "config": asdict(cfg),
        "wall_time_s": round(wall_time, 3),
        "requests_sent": len(records),
        "arrivals_dropped": dropped,
        "hits": len(hits),
        "misses": len(misses),
        "errors": len(errors),
        "hit_rate": round(len(hits) / served, 4) if served else 0.0,
        "miss_rate": round(len(misses) / served, 4) if served else 0.0,
        "error_rate": round(len(errors) / total, 4),
        "throughput_rps": round(served / wall_time, 3) if wall_time else 0.0,
        "latency_ms": {
            "mean": round(statistics.fmean(lat_all), 2) if lat_all else 0.0,
            "p50": round(percentile(lat_all, 50), 2),
            "p95": round(percentile(lat_all, 95), 2),
            "p99": round(percentile(lat_all, 99), 2),
            "max": round(max(lat_all), 2) if lat_all else 0.0,
        },
        "latency_hit_ms": {
            "mean": round(t_cache, 2),
            "p50": round(percentile(lat_hit, 50), 2),
            "p95": round(percentile(lat_hit, 95), 2),
        },
        "latency_miss_ms": {
            "mean": round(t_scraper, 2),
            "p50": round(percentile(lat_miss, 50), 2),
            "p95": round(percentile(lat_miss, 95), 2),
        },
        "cache_efficiency_ms": round(efficiency, 3),
        "per_query_type": per_type,
    }


def write_outputs(records: list[Record], summary: dict[str, Any],
                  label: str) -> tuple[str, str]:
    Path(RESULTS_DIR).mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = f"{label or 'run'}_{stamp}"

    csv_path = str(Path(RESULTS_DIR) / f"{base}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(Record.__annotations__.keys()))
        w.writeheader()
        for r in sorted(records, key=lambda x: x.seq):
            w.writerow(asdict(r))

    json_path = str(Path(RESULTS_DIR) / f"{base}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    return csv_path, json_path


def print_summary(s: dict[str, Any]) -> None:
    print("\n" + "=" * 62)
    print(f" {s['config']['dist'].upper()} / arribo {s['config']['arrival']} "
          f"@ {s['config']['rate']} req/s")
    print("=" * 62)
    print(f" Solicitudes    : {s['requests_sent']}  "
          f"(descartadas: {s['arrivals_dropped']})")
    print(f" Hit rate       : {s['hit_rate']:.2%}   "
          f"Miss rate: {s['miss_rate']:.2%}   Errores: {s['error_rate']:.2%}")
    print(f" Throughput     : {s['throughput_rps']} req/s")
    print(f" Latencia       : p50={s['latency_ms']['p50']} ms  "
          f"p95={s['latency_ms']['p95']} ms  p99={s['latency_ms']['p99']} ms")
    print(f"   └ hit        : p50={s['latency_hit_ms']['p50']} ms")
    print(f"   └ miss       : p50={s['latency_miss_ms']['p50']} ms")
    print(f" Cache efficiency: {s['cache_efficiency_ms']} ms/consulta")
    print("-" * 62)
    print(f" {'Tipo':<6}{'Reqs':>7}{'Hits':>7}{'Miss':>7}"
          f"{'HitRate':>10}{'p50 ms':>10}{'p95 ms':>10}")
    for qt, v in s["per_query_type"].items():
        print(f" {qt:<6}{v['requests']:>7}{v['hits']:>7}{v['misses']:>7}"
              f"{v['hit_rate']:>9.1%}{v['p50_ms']:>10}{v['p95_ms']:>10}")
    print("=" * 62 + "\n")



# CLI


async def wait_for_cache(client: httpx.AsyncClient, retries: int = 30) -> None:
    for i in range(retries):
        try:
            r = await client.get(f"{CACHE_URL}/health", timeout=3.0)
            if r.status_code == 200:
                print(f"[gen] caché disponible en {CACHE_URL}")
                return
        except Exception:
            pass
        await asyncio.sleep(1.0)
    raise RuntimeError(f"El servicio de caché no respondió en {CACHE_URL}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Generador de tráfico sintético")
    p.add_argument("--dist", choices=["uniform", "zipf"],
                   default=os.getenv("GEN_DIST", "zipf"),
                   help="distribución de popularidad de las consultas")
    p.add_argument("--arrival", choices=["constant", "poisson"],
                   default=os.getenv("GEN_ARRIVAL", "poisson"),
                   help="proceso de arribo de las solicitudes")
    p.add_argument("--rate", type=float, default=float(os.getenv("GEN_RATE", 20)),
                   help="tasa de arribo objetivo en consultas/segundo")
    p.add_argument("--duration", type=float,
                   default=float(os.getenv("GEN_DURATION", 0)) or None,
                   help="duración del experimento en segundos")
    p.add_argument("--requests", type=int,
                   default=int(os.getenv("GEN_REQUESTS", 0)) or None,
                   help="número total de consultas a emitir")
    p.add_argument("--zipf-s", type=float,
                   default=float(os.getenv("GEN_ZIPF_S", 1.2)),
                   help="exponente s de Zipf (mayor s ⇒ más sesgo)")
    p.add_argument("--seed", type=int, default=int(os.getenv("GEN_SEED", 42)),
                   help="semilla para reproducibilidad")
    p.add_argument("--max-inflight", type=int,
                   default=int(os.getenv("GEN_MAX_INFLIGHT", 200)),
                   help="máximo de solicitudes concurrentes en vuelo")
    p.add_argument("--timeout", type=float,
                   default=float(os.getenv("GEN_TIMEOUT", 60)),
                   help="timeout HTTP por solicitud (s)")
    p.add_argument("--label", default=os.getenv("GEN_LABEL", ""),
                   help="etiqueta del experimento para los archivos de salida")
    p.add_argument("--no-wait", action="store_true",
                   help="no esperar el health check del caché")
    args = p.parse_args(argv)

    if not args.duration and not args.requests:
        args.requests = 1000
    if not args.label:
        args.label = f"{args.dist}_{args.arrival}_r{int(args.rate)}"
    return args


async def main_async(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rng = random.Random(args.seed)

    teams = load_teams(TEAMS_FILE)
    factory = QueryFactory(teams, args.dist, args.zipf_s, rng, DEFAULT_QUERY_MIX)

    cfg = RunConfig(
        dist=args.dist, arrival=args.arrival, rate=args.rate,
        duration=args.duration, requests=args.requests, zipf_s=args.zipf_s,
        seed=args.seed, max_inflight=args.max_inflight,
        query_mix=DEFAULT_QUERY_MIX, label=args.label,
    )

    print(f"[gen] universo: {len(teams)} equipos, {len(factory.pairs)} pares, "
          f"{len(factory.periods)} periodos")
    if args.dist == "zipf":
        print(f"[gen] Zipf s={args.zipf_s}: el top-3 de equipos concentra "
              f"{factory.sel_team.top_mass(3):.1%} de las consultas Q1/Q2")

    limits = httpx.Limits(max_connections=args.max_inflight + 20,
                          max_keepalive_connections=args.max_inflight)
    async with httpx.AsyncClient(timeout=args.timeout, limits=limits) as client:
        if not args.no_wait:
            await wait_for_cache(client)
        gen = TrafficGenerator(cfg, factory, client)
        await gen.run()

    summary = summarize(gen.records, gen.wall_time, cfg, gen.dropped)
    print_summary(summary)
    csv_path, json_path = write_outputs(gen.records, summary, args.label)
    print(f"[gen] resultados: {csv_path}\n[gen] resumen:     {json_path}")
    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(main_async()))
    except KeyboardInterrupt:
        print("\n[gen] interrumpido por el usuario")
        sys.exit(130)


if __name__ == "__main__":
    main()
