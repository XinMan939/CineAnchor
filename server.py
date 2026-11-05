"""
CineAnchor · 影锚 — Phase 1/2.5 API 服务
====================================
把 Phase 0 的映射链路 (cineanchor.py) 包成 HTTP 服务。

接口:
  GET /api/v1/search?q=奥本海默[&year=2023]   -> TMDB 搜索 (带缓存)
  GET /api/v1/detail/movie/872585             -> TMDB 详情 (带缓存)
  GET /api/v1/sources/movie/872585            -> 线路池聚合 (并发+缓存+熔断)
  GET /api/v1/trending?window=day|week       -> TMDB 热门榜
  GET /api/v1/latest?n=30                    -> 内容库最新上线 (index.db)

缓存: SQLite (tmdb 详情 24h, 线路池 6h) — 零依赖, 不需要 Redis
并发: asyncio.gather + wait_for 超时切断 (慢源不拖死接口)
熔断: 源连续失败 N 次 (默认 3) 暂时屏蔽 COOLDOWN 秒 (默认 30 分钟)

运行:
  python server.py            # 127.0.0.1:8787
  python server.py --reload   # 开发模式
  python indexer.py            # 先抓一轮内容库 (index.db)
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

TMDB_CACHE_TTL = 24 * 3600      # TMDB 元数据缓存 24h
SOURCES_CACHE_TTL = 6 * 3600    # 线路池缓存 6h
PROVIDER_TIMEOUT = 8           # 单源并发超时 8s
CIRCUIT_THRESHOLD = 3          # 连续失败 N 次触发熔断
CIRCUIT_COOLDOWN = 30 * 60     # 熔断冷却 30 分钟

# 数据目录: 容器里设 CINEANCHOR_DATA_DIR=/app/data 把 SQLite 挂到卷;
# 本地默认就放脚本同目录, 行为不变.
DATA_DIR = Path(os.environ.get("CINEANCHOR_DATA_DIR", str(BASE)))
if not DATA_DIR.exists():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "cache.db"
INDEX_DB_PATH = DATA_DIR / "index.db"

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
# 源熔断器: 连续失败 N 次的 provider 暂时屏蔽, 冷却后放行试探
# ---------------------------------------------------------------------------
class CircuitBreaker:
    def __init__(self, threshold=CIRCUIT_THRESHOLD, cooldown=CIRCUIT_COOLDOWN):
        self.threshold = threshold
        self.cooldown = cooldown
        self.fails = {}    # name -> 连续失败次数
        self.opened = {}   # name -> 熔断打开时间戳

    def allow(self, name):
        """熔断中返回 False; 冷却期满半开 (放行一次试探)"""
        t = self.opened.get(name)
        if t is None:
            return True
        if time.time() - t >= self.cooldown:
            # 半开: 允许试探, 保持 opened 记录直到结果回来
            return True
        return False

    def record(self, name, ok):
        if ok:
            self.fails[name] = 0
            self.opened.pop(name, None)
        else:
            self.fails[name] = self.fails.get(name, 0) + 1
            if self.fails[name] >= self.threshold:
                self.opened[name] = time.time()

    def status(self):
        now = time.time()
        out = {}
        for name in set(list(self.fails) + list(self.opened)):
            t = self.opened.get(name)
            if t is not None and now - t < self.cooldown:
                out[name] = {"circuit": "open", "retry_in_s": int(self.cooldown - (now - t))}
            elif t is not None:
                out[name] = {"circuit": "half-open", "fails": self.fails.get(name, 0)}
            else:
                out[name] = {"circuit": "closed", "fails": self.fails.get(name, 0)}
        return out


BREAKER = CircuitBreaker()
INDEXER_RUNNING = False


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


async def tmdb_detail_cached(media_type, tmdb_id, season=None):
    key = f"tmdb:detail:{media_type}:{tmdb_id}:{season or ''}"
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    d = await aio(TMDB.details, media_type, tmdb_id, season)
    d["poster_full"] = POSTER_BASE + d["poster"] if d.get("poster") else None
    CACHE.set(key, d, TMDB_CACHE_TTL)
    return d


async def tmdb_trending_cached(window="week"):
    """首页: TMDB trending 榜单 (最新上线海报墙)"""
    key = f"tmdb:trending:{window}"
    hit = CACHE.get(key)
    if hit is not None:
        return hit
    out = []
    for t in ("movie", "tv"):
        try:
            r = await aio(TMDB._get, f"trending/{t}/{window}")
            for it in r.get("results", []):
                out.append({
                    "tmdb_id": it["id"],
                    "type": t,
                    "title": it.get("title") or it.get("name"),
                    "year": (it.get("release_date") or it.get("first_air_date") or "")[:4],
                    "poster": POSTER_BASE + it["poster_path"] if it.get("poster_path") else None,
                    "rating": round(it.get("vote_average", 0), 1),
                    "popularity": it.get("popularity", 0),
                })
        except Exception:
            continue
    out.sort(key=lambda x: -x["popularity"])
    CACHE.set(key, out, 12 * 3600)  # 榜单 12h
    return out


async def resolve_sources_cached(media_type, tmdb_id, season=None, force=False):
    """线路池: 先查缓存, miss 则并发聚合所有 provider (熔断中的源跳过)"""
    key = f"sources:{media_type}:{tmdb_id}:{season or ''}"
    if not force:
        hit = CACHE.get(key)
        if hit is not None:
            return hit, True

    media = await tmdb_detail_cached(media_type, tmdb_id, season)
    mapper = Mapper(PROVIDERS, verbose=False)

    active = [p for p in PROVIDERS if BREAKER.allow(p.name)]
    skipped = [p.name for p in PROVIDERS if p not in active]

    # 并发: 每个 provider 独立超时, 谁慢切谁; 结果回写熔断器
    async def one_provider(p):
        try:
            r = await asyncio.wait_for(aio(mapper._resolve_one, p, media), PROVIDER_TIMEOUT)
            # 注意: _resolve_one 内部会把 provider 错误吞进 entry["error"] 而非抛异常,
            # 所以"有 error 字段"也算失败 (未匹配但无 error 才算健康)
            BREAKER.record(p.name, ok=not r.get("error"))
            return r
        except asyncio.TimeoutError:
            BREAKER.record(p.name, ok=False)
            return {"provider": p.name, "matched": 0, "lines": [],
                    "error": "timeout", "pool": {}}
        except Exception as e:
            BREAKER.record(p.name, ok=False)
            return {"provider": p.name, "matched": 0, "lines": [],
                    "error": str(e), "pool": {}}

    results = await asyncio.gather(*[one_provider(p) for p in active])

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
    for name in skipped:
        stats[name] = {"matched": 0, "lines": 0, "error": "circuit_open",
                       "circuit": BREAKER.status().get(name, {})}

    lines = sorted(pool.values(),
                   key=lambda x: (-x.get("quality", 10), x["provider"], x["line"], x["episode"]))
    payload = {"media": media, "stats": stats, "lines": lines}
    CACHE.set(key, payload, SOURCES_CACHE_TTL)
    return payload, False


# ---------------------------------------------------------------------------
# 内容库: index.db 最新上线 (由 indexer.py 定时抓取)
# ---------------------------------------------------------------------------
def _latest_from_db(n):
    if not INDEX_DB_PATH.exists():
        return None
    conn = sqlite3.connect(f"file:{INDEX_DB_PATH}?mode=ro", uri=True)
    try:
        cur = conn.execute(
            "SELECT tmdb_id, type, season, title, year, poster, updated_at "
            "FROM videos ORDER BY updated_at DESC LIMIT ?", (n,))
        cols = ["tmdb_id", "type", "season", "title", "year", "poster", "updated_at"]
        items = []
        for r in cur.fetchall():
            it = dict(zip(cols, r))
            it["updated_at"] = time.strftime("%m-%d %H:%M", time.localtime(it["updated_at"]))
            items.append(it)
        return items
    finally:
        conn.close()


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
async def api_detail(media_type: str, tmdb_id: int, season: int = 0):
    """TMDB 详情页元数据 (tv 可带 season)"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be movie|tv")
    try:
        return await tmdb_detail_cached(media_type, tmdb_id, season or None)
    except Exception as e:
        raise HTTPException(502, f"TMDB error: {e}")


@app.get("/api/v1/trending")
async def api_trending(window: str = "week"):
    """首页最新上线: TMDB trending (movie+tv 合并)"""
    if window not in ("day", "week"):
        raise HTTPException(400, "window must be day|week")
    return {"items": await tmdb_trending_cached(window)}


@app.get("/api/v1/latest")
async def api_latest(n: int = Query(30, ge=1, le=120)):
    """内容库最新上线: 采集站更新 + TMDB 识别 (indexer.py 抓取入库)"""
    items = await aio(_latest_from_db, n)
    if items is None:
        raise HTTPException(503, "index.db 不存在 — 先运行 python indexer.py 抓一轮")
    return {"count": len(items), "items": items}


@app.post("/api/v1/indexer/run")
async def api_indexer_run(pages: int = Query(3, ge=1, le=20)):
    """手动触发一轮索引抓取 (等价于 cron 跑 indexer.py; 前端"抓取一轮"按钮)"""
    global INDEXER_RUNNING
    if INDEXER_RUNNING:
        return {"status": "already_running"}
    INDEXER_RUNNING = True
    try:
        from indexer import run_index
        stat = await aio(run_index, pages)
        return {"status": "done", **stat}
    finally:
        INDEXER_RUNNING = False


@app.get("/api/v1/sources/{media_type}/{tmdb_id}")
async def api_sources(media_type: str, tmdb_id: int, season: int = 0, force: bool = False):
    """核心接口: TMDB ID -> 多源聚合线路池 (tv 可带 season 路由到对应季)"""
    if media_type not in ("movie", "tv"):
        raise HTTPException(400, "media_type must be movie|tv")
    if not PROVIDERS:
        raise HTTPException(503, "config.json 未配置任何采集站 provider")
    payload, from_cache = await resolve_sources_cached(media_type, tmdb_id, season or None, force)
    return {"from_cache": from_cache, **payload}


@app.get("/api/v1/health")
async def api_health():
    return {
        "status": "ok",
        "providers": [{"name": p.name, "api": p.api_url} for p in PROVIDERS],
        "circuit": BREAKER.status(),
        "tmdb_host": TMDB.host,
        "index_db": INDEX_DB_PATH.exists(),
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
