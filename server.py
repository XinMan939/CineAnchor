"""
CineAnchor · 影锚 — Phase 1 API 服务
====================================
把 Phase 0 的映射链路 (cineanchor.py) 包成 HTTP 服务。

接口:
  GET /api/v1/search?q=奥本海默[&year=2023]   -> TMDB 搜索 (带缓存)
  GET /api/v1/detail/movie/872585             -> TMDB 详情 (带缓存)
  GET /api/v1/sources/movie/872585            -> 线路池聚合 (并发+缓存)

缓存: SQLite (tmdb 详情 24h, 线路池 6h) — 零依赖, 不需要 Redis
并发: asyncio.gather + wait_for 超时切断 (慢源不拖死接口)

运行:
  python server.py            # 127.0.0.1:8787
  python server.py --reload   # 开发模式
"""
import argparse
import asyncio
import json
import os
import sqlite3
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pathlib import Path as _P

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from cineanchor import (  # noqa: E402
    MacCmsProvider, Mapper, Tmdb, load_config,
)

CFG = load_config(BASE / "config.json")
DB_PATH = BASE / "cache.db"

TMDB_CACHE_TTL = 24 * 3600      # TMDB 元数据缓存 24h
SOURCES_CACHE_TTL = 6 * 3600    # 线路池缓存 6h
PROVIDER_TIMEOUT = 8           # 单源并发超时 8s

POSTER_BASE = "https://image.tmdb.org/t/p/w500"


# ---------------------------------------------------------------------------
# SQLite 缓存 (同步, 简单场景足够; 接口内不阻塞事件循环的关键路径已用 to_thread)
# ---------------------------------------------------------------------------
class Cache:
    def __init__(self, path):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT, exp REAL)"
        )
        self.conn.commit()

    def get(self, k):
        row = self.conn.execute(
            "SELECT v, exp FROM cache WHERE k=?", (k,)
        ).fetchone()
        if not row:
            return None
        v, exp = row
        if exp < time.time():
            self.conn.execute("DELETE FROM cache WHERE k=?", (k,))
            self.conn.commit()
            return None
        return json.loads(v)

    def set(self, k, v, ttl):
        self.conn.execute(
            "INSERT OR REPLACE INTO cache VALUES (?,?,?)",
            (k, json.dumps(v, ensure_ascii=False), time.time() + ttl),
        )
        self.conn.commit()


CACHE = Cache(DB_PATH)


# ---------------------------------------------------------------------------
# 异步包装: 阻塞 IO 丢线程池
# ---------------------------------------------------------------------------
async def aio(fn, *a, **kw):
    return await asyncio.to_thread(fn, *a, **kw)


TMDB = Tmdb(CFG["tmdb_token"], CFG.get("tmdb_host", "https://api.tmdb.org"))
PROVIDERS = [MacCmsProvider(p["name"], p["api"]) for p in CFG.get("providers", [])]


# ---------------------------------------------------------------------------
# 业务
# ---------------------------------------------------------------------------
async def tmdb_search_cached(q, year=None):
    key = f"tmdb:search:{q}:{year or ''}"
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    results = await aio(_search_both, q, year)
    CACHE.set(key, results, 6 * 3600)
    return results


def _search_both(q, year):
    """TMDB movie+tv 双搜索, 返回列表"""
    out = []
    for t in ("movie", "tv"):
        try:
            r = TMDB._get(f"search/{t}", query=q, year=year or "",
                          first_air_date_year=year or "")
            for it in r.get("results", []):
                out.append({
                    "tmdb_id": it["id"],
                    "type": t,
                    "title": it.get("title") or it.get("name"),
                    "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                    "poster": POSTER_BASE + it["poster_path"] if it.get("poster_path") else None,
                    "popularity": it.get("popularity", 0),
                })
        except Exception:
            continue
    out.sort(key=lambda x: -x["popularity"])
    return out[:20]


async def tmdb_detail_cached(media_type, tmdb_id):
    key = f"tmdb:detail:{media_type}:{tmdb_id}"
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    d = await aio(TMDB.details, media_type, tmdb_id)
    d["poster_full"] = POSTER_BASE + d["poster"] if d.get("poster") else None
    CACHE.set(key, d, TMDB_CACHE_TTL)
    return d


async def resolve_sources_cached(media_type, tmdb_id, force=False):
    """线路池: 先查缓存, miss 则并发聚合所有 provider"""
    key = f"sources:{media_type}:{tmdb_id}"
    if not force:
        hit = CACHE.get(key)
        if hit is not None:
            return hit, True

    media = await tmdb_detail_cached(media_type, tmdb_id)
    mapper = Mapper(PROVIDERS, verbose=False)

    # 并发: 每个 provider 独立超时, 谁慢切谁
    async def one_provider(p):
        try:
            return await asyncio.wait_for(aio(mapper._resolve_one, p, media), PROVIDER_TIMEOUT)
        except asyncio.TimeoutError:
            return {"provider": p.name, "matched": 0, "lines": [],
                    "error": "timeout", "pool": {}}
        except Exception as e:
            return {"provider": p.name, "matched": 0, "lines": [],
                    "error": str(e), "pool": {}}

    results = await asyncio.gather(*[one_provider(p) for p in PROVIDERS])

    pool = {}
    stats = {}
    for r in results:
        name = r["provider"]
        for url, info in r.get("pool", {}).items():
            if url not in pool:
                pool[url] = info
        stats[name] = {
            "matched": r.get("matched", 0),
            "lines": r.get("lines", 0),
            "error": r.get("error"),
        }

    lines = sorted(pool.values(),
                   key=lambda x: (x["provider"], x["line"], x["episode"]))
    payload = {"media": media, "stats": stats, "lines": lines}
    CACHE.set(key, payload, SOURCES_CACHE_TTL)
    return payload, False


# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(
    title="CineAnchor API",
    description="以作品为锚, 聚合可切换播放线路 — Phase 1",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"],
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/v1/search")
async def api_search(q: str = Query(..., min_length=1), year: str = ""):
    """标题 -> TMDB 作品列表 (前端搜索框用)"""
    items = await tmdb_search_cached(q, year or None)
    return {"query": q, "count": len(items), "items": items}


@app.get("/api/v1/detail/{media_type}/{tmdb_id}")
async def api_detail(media_type: str, tmdb_id: int):
    """TMDB 详情页元数据"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be movie|tv")
    try:
        return await tmdb_detail_cached(media_type, tmdb_id)
    except Exception as e:
        raise HTTPException(502, f"TMDB error: {e}")


@app.get("/api/v1/sources/{media_type}/{tmdb_id}")
async def api_sources(media_type: str, tmdb_id: int, force: bool = False):
    """核心接口: TMDB ID -> 多源聚合线路池"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be movie|tv")
    if not PROVIDERS:
        raise HTTPException(503, "config.json 未配置任何采集站 provider")
    payload, from_cache = await resolve_sources_cached(media_type, tmdb_id, force)
    return {"from_cache": from_cache, **payload}


@app.get("/api/v1/health")
async def api_health():
    return {
        "status": "ok",
        "providers": [{"name": p.name, "api": p.api_url} for p in PROVIDERS],
        "tmdb_host": TMDB.host,
    }


WEB_DIR = BASE / "web"


@app.get("/")
async def web_index():
    """极简前端: 搜索 -> 海报墙 -> 线路切换 -> hls.js 播放"""
    f = WEB_DIR / "index.html"
    if f.exists():
        return FileResponse(f)
    raise HTTPException(404, "web/index.html not found")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port,
                reload=args.reload)
