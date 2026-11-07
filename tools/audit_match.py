# -*- coding: utf-8 -*-
"""匹配层体检: 8 类片目 × 5 源, 记录命中率/miss 原因/多季对齐/可播性"""
import json, time, urllib.request, urllib.parse, ssl

API = "http://127.0.0.1:8787"
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE

# 8 类体检片目 (title, type, 体检类别, prefer_id)
CASES = [
    ("庆余年",            "tv",   "新热门剧(多季)", None),
    ("流浪地球2",          "movie","热门电影", None),
    ("权力的游戏",          "tv",   "多季美剧(源常合条目)", None),
    ("向往的生活",          "tv",   "国产综艺(TMDB综艺弱)", None),
    ("咒术回战",           "tv",   "动漫", None),
    ("赌神",              "movie", "90年代港片(老片)", 16269),  # 精确锁定 1989 正主
    ("肖申克的救赎",        "movie", "TMDB中文标题≠源标题写法", None),
    ("瑞克和莫蒂",          "tv",   "冷门/别名写法差异", None),
]

def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
        return json.loads(r.read().decode("utf-8"))

def head(url, timeout=10):
    """探测 m3u8 可达性, 返回 (status, content_type)。
    注意: 暴风等源 URL 含中文路径, urllib 不会自动 percent-encode,
    必须先 quote 否则 UnicodeEncodeError (浏览器会自动编码, 网页播放不受影响)。"""
    try:
        enc = urllib.parse.quote(url, safe=":/?&=#%.")
        req = urllib.request.Request(enc, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
            return r.status, r.headers.get("Content-Type", "")
    except Exception as e:
        return getattr(e, "code", 0), str(type(e).__name__)

def probe(q, typ, prefer_id=None):
    """搜片 → 选候选(可指定 id 或精确标题匹配) → 聚合 → 返回体检数据"""
    s = get(f"{API}/api/v1/search?q={urllib.parse.quote(q)}")
    if prefer_id:
        cand = next((i for i in s["items"] if i["tmdb_id"] == prefer_id), None)
    else:
        # 精确标题匹配优先 (体检脚本第一版拿第一个候选, "赌神"命中了《全民追女王》)
        cand = next((i for i in s["items"] if i["title"] == q and i["type"] == typ), None) \
               or next((i for i in s["items"] if i["type"] == typ), None)
    if not cand:
        return {"case": q, "error": "TMDB 搜索无此类型候选", "raw": s["items"][:3]}
    sid = cand["tmdb_id"]
    seasons = 1 if typ == "movie" else 0
    # 剧集先拿详情看季数, 测 S1 (多季剧也测最新季)
    detail = get(f"{API}/api/v1/detail/{typ}/{sid}")
    n_seasons = detail.get("seasons") or detail.get("media", {}).get("seasons") or 1
    season = 1 if typ == "tv" else 0
    d = get(f"{API}/api/v1/sources/{typ}/{sid}?season={season}")
    lines = d.get("lines", [])
    stats = d.get("stats", {})
    hit = {k: v for k, v in stats.items() if v.get("matched")}
    miss = {k: v for k, v in stats.items() if not v.get("matched")}
    # 集数对齐检查 (剧集): 各源报的集数
    ep_sets = {}
    for l in lines:
        ep_sets.setdefault(l["provider"], set()).add(l["episode"])
    # 可播性抽查: 每源第一条
    playable = {}
    seen = set()
    for l in lines:
        p = l["provider"]
        if p in seen: continue
        seen.add(p)
        code, ct = head(l["url"])
        playable[p] = f"{code}/{('m3u8' if 'mpegurl' in ct.lower() else ct or '?')}"
    return {
        "case": q, "cat": next(c[2] for c in CASES if c[0] == q),
        "tmdb": f"{cand['title']} ({cand['year']}) id={sid}",
        "seasons_total": n_seasons, "season_tested": season,
        "hit_sources": list(hit.keys()), "miss_sources": {k: v.get("error") or "未命中" for k, v in miss.items()},
        "lines": len(lines),
        "ep_alignment": {p: len(eps) for p, eps in ep_sets.items()},
        "playable": playable,
    }

results = []
for q, typ, cat, prefer_id in CASES:
    t0 = time.time()
    try:
        r = probe(q, typ)
    except Exception as e:
        r = {"case": q, "cat": cat, "error": f"{type(e).__name__}: {e}"}
    r["elapsed_s"] = round(time.time() - t0, 1)
    results.append(r)
    flag = "✓" if r.get("lines") else "✗"
    print(f"{flag} [{r.get('cat','')}] {q}: {r.get('lines',0)} lines, hit={len(r.get('hit_sources',[]))}/5, {r['elapsed_s']}s"
          + (f" ERR={r['error']}" if r.get("error") else ""))

with open("audit_result.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\nfull report -> audit_result.json")
