"""
CineAnchor · 影锚 — Phase 2.5 内容库索引器
============================================
定时(或手动)抓取采集站最新更新列表, TMDB 反向识别后入库 index.db:
把 App 从"搜索驱动"升级为"内容驱动"(首页直接看到采集站今天更新了什么).

策略:
  - 只抓最新 N 页 (苹果CMS ?ac=detail&pg=K 默认按 vod_time 倒序)
  - 标题拆季 ("芝加哥警署第十四季" -> 基名+season=14)
  - 基名+年份 -> TMDB search 反向识别; 搜不到/不匹配的直接丢弃
    (网大/短剧/擦边球天然被 TMDB 过滤掉, 库体积和风险双降)
  - 入库 videos(作品) + links(线路/分集)

用法:
  python indexer.py                # 抓前 5 页 (100 条)
  python indexer.py --pages 10
  python indexer.py --show 20     # 只读: 看库里最新 20 条
"""
import argparse
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))

from cineanchor import (  # noqa: E402
    CN_NUM, MacCmsProvider, Tmdb, extract_season, load_config,
    match_score, normalize_title, parse_episodes, parse_year,
)

DB_PATH = BASE / "index.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  tmdb_id    INTEGER NOT NULL,
  type       TEXT    NOT NULL,       -- movie|tv
  season     INTEGER NOT NULL DEFAULT 0,  -- 0=无季/电影/整剧
  title      TEXT,
  year       TEXT,
  poster     TEXT,
  rating     REAL,
  updated_at REAL,                    -- 采集站 vod_time 的时间戳
  extra      TEXT,                    -- 季名/集数等 JSON 兜底
  PRIMARY KEY (tmdb_id, type, season)
);
CREATE INDEX IF NOT EXISTS idx_videos_time ON videos(updated_at DESC);
CREATE TABLE IF NOT EXISTS links (
  url        TEXT PRIMARY KEY,
  tmdb_id    INTEGER NOT NULL,
  type       TEXT    NOT NULL,
  season     INTEGER NOT NULL DEFAULT 0,
  source     TEXT,                    -- 采集站名
  line       TEXT,                    -- 线路名
  episode    TEXT,                    -- 集名
  updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_links_media ON links(tmdb_id, type, season);
"""


class IndexDB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def upsert_video(self, v):
        self.conn.execute(
            "INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?,?,?)",
            (v["tmdb_id"], v["type"], v.get("season") or 0, v["title"],
             v.get("year"), v.get("poster"), v.get("rating"),
             v.get("updated_at") or time.time(),
             json.dumps({k: v[k] for k in ("season_name", "season_year", "season_episodes")
                         if v.get(k)}, ensure_ascii=False)),
        )

    def upsert_links(self, rows):
        self.conn.executemany(
            "INSERT OR REPLACE INTO links VALUES (?,?,?,?,?,?,?,?)",
            rows,
        )

    def commit(self):
        self.conn.commit()

    def latest(self, n=60):
        cur = self.conn.execute(
            "SELECT tmdb_id, type, season, title, year, poster, rating, updated_at "
            "FROM videos ORDER BY updated_at DESC LIMIT ?", (n,))
        cols = ["tmdb_id", "type", "season", "title", "year", "poster", "rating", "updated_at"]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def count(self):
        v = self.conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        l = self.conn.execute("SELECT COUNT(*) FROM links").fetchone()[0]
        return v, l


# ----------------------------------------------------------------------------
# 标题拆季: 基名 + 季数
# ----------------------------------------------------------------------------
SEASON_STRIP = re.compile(r"第\s*[0-9一二三四五六七八九十]+\s*季|[0-9一二三四五六七八九十]+季|season\s*\d+", re.I)


def split_season(vod_name):
    """'芝加哥警署第十四季' -> ('芝加哥警署', 14); 无季 -> (原名, None)"""
    s = extract_season(vod_name)
    if s is None:
        return vod_name, None
    base = SEASON_STRIP.sub("", vod_name).strip()
    return (base or vod_name), s


# ----------------------------------------------------------------------------
# TMDB 反向识别: 采集站条目 -> tmdb 锚点
# ----------------------------------------------------------------------------
def identify(tmdb, vod, cached_search):
    """采集站条目 -> (media dict | None). media 带反向匹配校验."""
    base, season = split_season(vod.get("vod_name") or "")
    year = parse_year(vod.get("vod_year"))
    key = (normalize_title(base), year)
    if key not in cached_search:
        hit = None
        try:
            hit = tmdb.search(base, year=year)
        except Exception:
            hit = None
        cached_search[key] = hit
        time.sleep(0.25)  # TMDB 礼貌限速
    hit = cached_search[key]
    if not hit:
        return None
    media = {
        "tmdb_id": str(hit["tmdb_id"]),
        "type": hit["type"],
        "title": hit["title"],
        "year": str(year or ""),
    }
    if hit["type"] == "tv" and season is not None:
        media["season"] = season
    # 反向校验: TMDB 标题 与 采集站基名 归一化一致才收 (防 TMDB 模糊搜索张冠李戴)
    if normalize_title(hit["title"]) != normalize_title(base):
        return None
    return media


def enrich(tmdb, media, cached_details):
    """补全 poster/rating (复用查询缓存, 季剧取季年份)"""
    key = (media["type"], media["tmdb_id"])
    if key not in cached_details:
        try:
            cached_details[key] = tmdb.details(media["type"], media["tmdb_id"])
        except Exception:
            cached_details[key] = None
        time.sleep(0.25)
    d = cached_details[key]
    if not d:
        return media
    media["poster"] = d.get("poster")
    media["rating"] = None
    media["seasons"] = d.get("seasons")
    return media


# ----------------------------------------------------------------------------
# 抓取一轮
# ----------------------------------------------------------------------------
def run_index(pages=5, verbose=True):
    cfg = load_config(BASE / "config.json")
    tmdb = Tmdb(cfg["tmdb_token"], cfg.get("tmdb_host", "https://api.tmdb.org"))
    providers = [MacCmsProvider(p["name"], p["api"], timeout=15)
                 for p in cfg.get("providers", [])]
    db = IndexDB()

    cached_search = {}    # (norm_title, year) -> tmdb hit
    cached_details = {}   # (type, id) -> tmdb details
    seen_media = set()    # (tmdb_id, type, season) 本轮已入库的作品
    stat = {"fetched": 0, "identified": 0, "dropped": 0, "links": 0, "errors": 0}

    for p in providers:
        for pg in range(1, pages + 1):
            url = f"{p.api_url}?ac=detail&pg={pg}"
            try:
                from cineanchor import http_get_json
                data = http_get_json(url, timeout=15)
            except Exception as e:
                if verbose:
                    print(f"[{p.name}] pg{pg} 拉取失败: {e}", file=sys.stderr)
                stat["errors"] += 1
                continue
            for vod in data.get("list", []):
                stat["fetched"] += 1
                try:
                    media = identify(tmdb, vod, cached_search)
                except Exception as e:
                    media = None
                    if verbose:
                        print(f"[identify] {vod.get('vod_name')}: {e}", file=sys.stderr)
                if not media:
                    stat["dropped"] += 1
                    continue
                stat["identified"] += 1
                media = enrich(tmdb, media, cached_details)
                season = media.get("season") or 0
                mkey = (media["tmdb_id"], media["type"], season)
                vtime = vod.get("vod_time") or ""
                try:
                    ts = time.mktime(time.strptime(vtime, "%Y-%m-%d %H:%M:%S"))
                except (ValueError, TypeError):
                    ts = time.time()

                poster_url = None
                if media.get("poster"):
                    poster_url = "https://image.tmdb.org/t/p/w500" + media["poster"]
                extra = {}
                for k in ("season_name", "season_year", "season_episodes", "seasons"):
                    if media.get(k) is not None:
                        extra[k] = media[k]
                db.upsert_video({
                    "tmdb_id": int(media["tmdb_id"]), "type": media["type"],
                    "season": season, "title": media["title"],
                    "year": media.get("year"), "poster": poster_url,
                    "rating": media.get("rating"), "updated_at": ts, **extra,
                })
                if mkey not in seen_media:
                    seen_media.add(mkey)
                    # 线路入库: 复用 parse_episodes
                    rows = []
                    for line_name, eps in parse_episodes(vod).items():
                        for ep_name, u in eps:
                            rows.append((u, int(media["tmdb_id"]), media["type"],
                                         season, p.name, line_name, ep_name, ts))
                    if rows:
                        db.upsert_links(rows)
                        stat["links"] += len(rows)
        if verbose:
            print(f"[{p.name}] 完成: fetched={stat['fetched']} "
                  f"identified={stat['identified']} dropped={stat['dropped']} "
                  f"links={stat['links']} errors={stat['errors']}", file=sys.stderr)
    db.commit()
    return stat


def main():
    ap = argparse.ArgumentParser(prog="indexer", description="CineAnchor Phase 2.5 — 内容库索引")
    ap.add_argument("--pages", type=int, default=5, help="每源抓取页数 (默认 5, 每页 20 条)")
    ap.add_argument("--show", type=int, help="只读模式: 展示库内最新 N 条")
    args = ap.parse_args()

    if args.show:
        db = IndexDB()
        v, l = db.count()
        print(f"库内: {v} 部作品 / {l} 条线路")
        for it in db.latest(args.show):
            ts = time.strftime("%m-%d %H:%M", time.localtime(it["updated_at"]))
            print(f"  {ts}  {it['title']} S{it['season'] or '-'} ({it['year']}) "
                  f"[{it['type']}/{it['tmdb_id']}]")
        return

    stat = run_index(pages=args.pages)
    db = IndexDB()
    v, l = db.count()
    print(json.dumps({
        "fetched": stat["fetched"], "identified": stat["identified"],
        "dropped": stat["dropped"], "links": stat["links"],
        "errors": stat["errors"], "db_videos": v, "db_links": l,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
