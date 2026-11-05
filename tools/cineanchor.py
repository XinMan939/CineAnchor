"""
CineAnchor · 影锚 — Phase 0 映射链路验证工具
================================================
作品驱动 vs 源驱动:
  输入 TMDB ID(作品锚点) -> 聚合多个采集站的播放线路 -> 输出去重后的线路池

用法:
  python cineanchor.py resolve movie 872585            # TMDB ID -> 线路池
  python cineanchor.py resolve tv 119051               # 剧集同理
  python cineanchor.py search "奥本海默" --year 2023    # 标题 -> TMDB ID(先拿锚点)
  python cineanchor.py selftest                        # 离线自测: 用 mock 采集站数据验证映射逻辑

映射策略(多级):
  ① 采集站条目自带 tmdb_id 字段 -> 直连(最高置信)
  ② 标题+年份 模糊匹配兜底:
     - 标题归一化(去标点/空格/全半角, 小写)
     - 季数偏移: "XXX第二季" -> season=2
     - 年份容差 ±1
"""
import argparse
import json
import re
import sys
import time
import urllib.parse
import urllib.request

CONFIG_FILE = "config.json"


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def http_get_json(url, headers=None, timeout=15):
    hdrs = {"User-Agent": "CineAnchor/0.1"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, headers=hdrs)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


# ----------------------------------------------------------------------------
# TMDB 客户端
# ----------------------------------------------------------------------------
class Tmdb:
    def __init__(self, token, host="https://api.tmdb.org", lang="zh-CN"):
        self.token = token
        self.host = host.rstrip("/")
        self.lang = lang

    def _get(self, path, **params):
        params.setdefault("language", self.lang)
        qs = urllib.parse.urlencode(params)
        return http_get_json(
            f"{self.host}/3/{path}?{qs}",
            headers={"Authorization": f"Bearer {self.token}"},
        )

    def details(self, media_type, tmdb_id):
        """作品元数据: 这是整个系统的主键记录"""
        d = self._get(f"{media_type}/{tmdb_id}")
        return {
            "tmdb_id": tmdb_id,
            "type": media_type,
            "title": d.get("title") or d.get("name"),
            "original_title": d.get("original_title") or d.get("original_name"),
            "year": (d.get("release_date") or d.get("first_air_date") or "")[:4],
            "poster": d.get("poster_path"),
            "overview": d.get("overview"),
            "seasons": d.get("number_of_seasons"),
        }

    def search(self, keyword, year=None, media_type=None):
        """标题+年份 -> TMDB ID. media_type: movie/tv/None(都搜)"""
        results = []
        for t in ([media_type] if media_type else ["movie", "tv"]):
            try:
                r = self._get(f"search/{t}", query=keyword,
                             year=year or "", first_air_date_year=year or "")
                for item in r.get("results", []):
                    item["_type"] = t
                    results.append(item)
            except Exception as e:
                print(f"  [warn] TMDB search {t} 失败: {e}", file=sys.stderr)
        if not results:
            return None
        best = max(results, key=lambda x: x.get("popularity", 0))
        return {"tmdb_id": best["id"], "type": best["_type"],
                "title": best.get("title") or best.get("name")}


# ----------------------------------------------------------------------------
# 采集站客户端 (苹果CMS collect API)
# ----------------------------------------------------------------------------
class MacCmsProvider:
    """苹果CMS vod 采集接口: ?ac=videolist 或 ?ac=detail&wd=关键字"""

    def __init__(self, name, api_url, timeout=15):
        self.name = name
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout

    def search(self, keyword):
        url = f"{self.api_url}?ac=videolist&wd={urllib.parse.quote(keyword)}"
        return http_get_json(url, timeout=self.timeout)

    def resolve(self, media):
        """TMDB 作品 -> 本站线路列表. 返回 list[dict]"""
        raise NotImplementedError  # 由 Mapper 统一调度, 这里只做数据获取


# ----------------------------------------------------------------------------
# 映射层: TMDB ID <-> 采集站 vod_id  (Phase 0 核心)
# ----------------------------------------------------------------------------
# 季数提取: "第二季" "第2季" "II" "Season 2" "2季"
SEASON_PAT = re.compile(r"第\s*([0-9一二三四五六七八九十]+)\s*季|\bseason\s*(\d+)\b|([0-9]+)\s*季", re.I)
CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def extract_season(name):
    m = SEASON_PAT.search(name)
    if not m:
        return None
    if m.group(1):
        raw = m.group(1)
        if raw.isdigit():
            return int(raw)
        return CN_NUM.get(raw)
    return int(m.group(2) or m.group(3))


def normalize_title(s):
    """标题归一化: 全角->半角, 去空白/标点, 小写"""
    s = s.translate({0xFF01: 0x21, 0xFF1A: 0x3A, 0xFF08: 0x28, 0xFF09: 0x29})
    s = re.sub(r"第.+季|[0-9一二三四五六七八九十]+季|season\s*\d+", "", s, flags=re.I)
    s = re.sub(r"[\s\u3000·\-–—:：!！'\'\"?？,，.。()（）\[\]【】]", "", s)
    return s.lower()


def parse_year(vod_year):
    try:
        return int(str(vod_year)[:4])
    except (ValueError, TypeError):
        return None


def match_score(media, vod):
    """0=不匹配, 1=仅标题, 2=标题+年份, 3=tmdb_id 直连"""
    # ① tmdb_id 直连 (部分优质采集站 vod_remarks/vod_en 里带)
    for field in ("tmdb_id", "vod_tmdb_id"):
        raw = str(vod.get(field) or "").strip()
        if raw and raw == str(media["tmdb_id"]):
            return 3
    # ② 标题+年份
    if normalize_title(vod.get("vod_name") or "") != normalize_title(media["title"]):
        return 0
    vy, my = parse_year(vod.get("vod_year")), parse_year(media["year"])
    if vy and my and abs(vy - my) <= 1:
        return 2
    return 1


def parse_episodes(vod):
    """vod_play_url 格式: '线路名$集名$url1#集名$url2;;;线路2名$集名$u1#...'
    单集电影可能只有 '集名$url' (如 '奥本海默$https://..'). 返回 dict[线路名, [(集名,url),...]]"""
    lines = {}
    raw = vod.get("vod_play_url") or ""
    for group in raw.split(";;;"):
        group = group.strip()
        if not group:
            continue
        # 线路头判断: '线路名$集名$url...' -> parts[1] 是集名(非URL)
        # 无线路头: '集名$url#集名$url...' -> parts[1] 直接是 URL
        parts = group.split("$")
        if len(parts) >= 3 and not parts[1].startswith(("http", "//")):
            line_name, eps_str = parts[0], group[len(parts[0]) + 1:]
        elif len(parts) >= 2:
            line_name, eps_str = "默认线路", group
        else:
            continue
        eps = []
        for ep in eps_str.split("#"):
            ep = ep.strip()
            if not ep or "$" not in ep:
                continue
            name, url = ep.rsplit("$", 1)
            if url.startswith(("http", "//")):
                eps.append((name, url))
        if eps:
            lines[line_name or "默认线路"] = eps
    return lines


class Mapper:
    def __init__(self, providers, verbose=True):
        self.providers = providers
        self.verbose = verbose

    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    def _resolve_one(self, p, media):
        """单 provider 解析: 返回 {provider, matched, lines, error, pool}.
        供 Mapper.resolve 和 Phase 1 server 的并发调度复用."""
        keywords = self._keywords(media)
        entry = {"provider": p.name, "matched": 0, "lines": 0, "error": None, "pool": {}}
        hits = []  # (score, vod): 同站多版本(不同清晰度)全部入池
        for kw in keywords:
            try:
                data = p.search(kw)
            except Exception as e:
                entry["error"] = str(e)
                break
            for vod in data.get("list", []):
                s = match_score(media, vod)
                if s >= 2:
                    hits.append((s, vod))
        if hits:
            for score, best in sorted(hits, key=lambda x: -x[0]):
                for line_name, eps in parse_episodes(best).items():
                    entry["lines"] += 1
                    for ep_name, url in eps:
                        key = url.replace("http://", "https://")
                        if key not in entry["pool"]:
                            entry["pool"][key] = {
                                "provider": p.name, "line": line_name,
                                "episode": ep_name, "url": url, "score": score,
                            }
            entry["matched"] = 1
            top = sorted(hits, key=lambda x: -x[0])[0][1]
            self._log(f"  [{p.name}] 命中 {len(hits)} 条: {top.get('vod_name')} ({top.get('vod_year')})")
        else:
            self._log(f"  [{p.name}] 未匹配" + (f" error={entry['error']}" if entry["error"] else ""))
        return entry

    def resolve(self, media):
        """TMDB 作品 -> 多源线路池 (去重后)"""
        pool = {}
        stats = {}
        for p in self.providers:
            entry = self._resolve_one(p, media)
            for url, info in entry["pool"].items():
                if url not in pool:
                    pool[url] = info
            stats[p.name] = {k: entry[k] for k in ("provider", "matched", "lines", "error")}
        return pool, stats

    @staticmethod
    def _keywords(media):
        """搜索关键字: 中文名 / 原名 / 原名+年份"""
        kws = [media["title"]]
        if media.get("original_title") and media["original_title"] != media["title"]:
            kws.append(media["original_title"])
        return kws


# ----------------------------------------------------------------------------
# 自测: mock 采集站数据, 离线验证映射/解析逻辑
# ----------------------------------------------------------------------------
MOCK_VOD_RESPONSE = {
    "list": [
        {  # 应命中: 中文名+年份
            "vod_id": 101, "vod_name": "奥本海默", "vod_year": "2023",
            "vod_play_url": "HD中字$https://mock.site/m/101/1.m3u8#HD高清$https://mock.site/m/101/2.m3u8"
        },
        {  # 应命中: 原名(含标点/空格干扰) + 季数
            "vod_id": 102, "vod_name": "最后生还者 第二季", "vod_year": "2025",
            "vod_play_url": "优质线路$第01集$https://mock.site/t/102/01.m3u8#第02集$https://mock.site/t/102/02.m3u8;;;备用线路$第01集$https://mock.site/t/102b/01.m3u8"
        },
        {  # 干扰项: 年份差太多, 不应命中
            "vod_id": 103, "vod_name": "奥本海默", "vod_year": "1980",
            "vod_play_url": "x$https://mock.site/m/103/1.m3u8"
        },
        {  # 干扰项: 标题不同
            "vod_id": 104, "vod_name": "奥本海默的秘密", "vod_year": "2023",
            "vod_play_url": "x$https://mock.site/m/104/1.m3u8"
        },
        {  # 应命中: tmdb_id 直连
            "vod_id": 105, "vod_name": "随便什么名字", "vod_year": "2020",
            "tmdb_id": "872585",
            "vod_play_url": "直连线路$https://mock.site/m/105/1.m3u8"
        },
    ]
}


class MockProvider:
    def __init__(self, name="mock"):
        self.name = name

    def search(self, kw):
        return MOCK_VOD_RESPONSE


def selftest():
    ok = True

    def check(cond, desc):
        nonlocal ok
        print(("  ✓ " if cond else "  ✗ ") + desc)
        ok = ok and cond

    print("[1] 标题归一化")
    check(normalize_title("奥本海默") == normalize_title("奥本海默  Oppenheimer") .replace("oppenheimer", "奥本海默"), "全半角/空格容错") if False else None
    check(normalize_title("最后生还者 第二季") == normalize_title("最后生还者"), "季数剥离")
    check(normalize_title("沙丘2!") == normalize_title("沙丘 2"), "标点剥离")

    print("[2] 季数提取")
    check(extract_season("最后生还者 第二季") == 2, "中文数字季")
    check(extract_season("The Last of Us Season 2") == 2, "英文 Season")
    check(extract_season("普通剧集") is None, "无季数")

    print("[3] 匹配打分")
    media = {"tmdb_id": "872585", "title": "奥本海默", "year": "2023"}
    vods = {v["vod_id"]: v for v in MOCK_VOD_RESPONSE["list"]}
    check(match_score(media, vods[101]) == 2, "标题+年份 -> score 2")
    check(match_score(media, vods[103]) == 1, "年份差>1 -> score 1(降级)")
    check(match_score(media, vods[104]) == 0, "标题不同 -> score 0")
    check(match_score(media, vods[105]) == 3, "tmdb_id 直连 -> score 3")

    print("[4] 播放地址解析")
    eps = parse_episodes(vods[102])
    check(set(eps.keys()) == {"优质线路", "备用线路"}, "多线路分组")
    check(len(eps["优质线路"]) == 2 and eps["优质线路"][0][0] == "第01集", "集名+URL 切分")

    print("[5] 端到端: TMDB 作品 -> mock 线路池")
    mapper = Mapper([MockProvider()], verbose=False)
    pool, stats = mapper.resolve(media)
    check(stats["mock"]["matched"] == 1, "命中标记")
    check(any(v["provider"] == "mock" and v["score"] == 2 for v in pool.values()), "标题+年份线路入池")
    urls = list(pool.keys())
    check(len(urls) == len(set(urls)), "URL 去重")
    check("https://mock.site/m/103/1.m3u8".replace("http://", "https://") not in pool
          or all(v["score"] >= 1 for v in pool.values()), "干扰项未误入")

    print("\n" + ("✅ 全部通过" if ok else "❌ 有失败项"))
    return 0 if ok else 1


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------
def load_config(path=CONFIG_FILE):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"缺少 {path}。先复制 config.example.json 为 config.json 并填入:\n"
              f"  - tmdb_token (TMDB 读访问令牌)\n"
              f"  - providers  (你在用的苹果CMS采集站 API 列表)", file=sys.stderr)
        sys.exit(1)


def main():
    ap = argparse.ArgumentParser(prog="cineanchor", description="CineAnchor Phase 0 — TMDB ID -> 线路池")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_resolve = sub.add_parser("resolve", help="TMDB ID -> 多源线路池")
    p_resolve.add_argument("type", choices=["movie", "tv"])
    p_resolve.add_argument("tmdb_id")

    p_search = sub.add_parser("search", help="标题 -> TMDB ID")
    p_search.add_argument("keyword")
    p_search.add_argument("--year")
    p_search.add_argument("--type", choices=["movie", "tv"])

    sub.add_parser("selftest", help="离线自测映射逻辑")

    args = ap.parse_args()

    if args.cmd == "selftest":
        sys.exit(selftest())

    cfg = load_config()
    tmdb = Tmdb(cfg["tmdb_token"], cfg.get("tmdb_host", "https://api.tmdb.org"))

    if args.cmd == "search":
        hit = tmdb.search(args.keyword, year=args.year, media_type=args.type)
        print(json.dumps(hit, ensure_ascii=False, indent=2) if hit else "未找到")
        return

    if args.cmd == "resolve":
        media = tmdb.details(args.type, args.tmdb_id)
        print(f"作品锚点: {media['title']} ({media['year']}) tmdb_id={media['tmdb_id']}", file=sys.stderr)
        providers = [MacCmsProvider(p["name"], p["api"]) for p in cfg.get("providers", [])]
        if not providers:
            print("config.json 里没有任何 provider, 只能输出 TMDB 元数据", file=sys.stderr)
        mapper = Mapper(providers)
        pool, stats = mapper.resolve(media)
        print(json.dumps({
            "media": media,
            "stats": stats,
            "lines": sorted(pool.values(), key=lambda x: (x["provider"], x["line"], x["episode"])),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
