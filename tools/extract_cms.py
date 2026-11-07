# -*- coding: utf-8 -*-
"""从 TVBox 配置里提取所有 type:1 (苹果CMS) 采集站 api，去重输出候选列表"""
import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

MIRROR = "https://ghfast.top/"

URLS = [
    MIRROR + "https://raw.githubusercontent.com/lxxcp/live/main/a.json",
    MIRROR + "https://raw.githubusercontent.com/bizhangjie/CatVodSpider/refs/heads/main/json/index18.json",
    MIRROR + "https://raw.githubusercontent.com/supermeguo/BoxRes/main/Myuse/catcr.json",
    "https://149.104.21.177.sslip.io/market-spider.json",
]

LOCALS = [
    r"D:/Documents/xwechat_files/forevervaqian_3263/msg/file/2026-11/海量py聚合接口.json",
    r"D:/Documents/xwechat_files/forevervaqian_3263/msg/file/2026-11/海量采集聚合接口.json",
    r"D:/Documents/xwechat_files/forevervaqian_3263/msg/file/2026-11/海量直播聚合接口.json",
]

# 现有 provider 的 api（去重用）
EXISTING = {
    "https://360zy.com/api.php/provide/vod",
    "http://cj.lziapi.com/api.php/provide/vod",
    "https://bfzyapi.com/api.php/provide/vod",
    "http://cj.ffzyapi.com/api.php/provide/vod",
    "http://caiji.dyttzyapi.com/api.php/provide/vod",
}

candidates = {}  # api -> {"name":..., "sources": set()}

def add_site(site, src):
    if site.get("type") != 1:
        return
    api = (site.get("api") or "").strip()
    if not api or not api.lower().startswith("http"):
        return
    # 苹果CMS 标准特征：api.php/provide/vod 或 provide/vod
    low = api.lower()
    if "provide/vod" not in low and "api.php" not in low:
        return
    name = (site.get("name") or "").strip()
    if api not in candidates:
        candidates[api] = {"name": name, "sources": set()}
    if name and not candidates[api]["name"]:
        candidates[api]["name"] = name
    candidates[api]["sources"].add(src)

def parse_config(text, src):
    try:
        cfg = json.loads(text)
    except Exception as e:
        print(f"[parse-fail] {src}: {e}", file=sys.stderr)
        return
    sites = cfg.get("sites") or []
    if isinstance(sites, list):
        for s in sites:
            if isinstance(s, dict):
                add_site(s, src)

# --- 线上 ---
import urllib.request
for url in URLS:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CineAnchor/0.1"})
        with urllib.request.urlopen(req, timeout=15) as r:
            text = r.read().decode("utf-8", "replace")
        parse_config(text, url)
        print(f"[ok] fetched {url}", file=sys.stderr)
    except Exception as e:
        print(f"[fetch-fail] {url}: {e}", file=sys.stderr)

# --- 本地 ---
for p in LOCALS:
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            text = f.read()
        parse_config(text, p)
        print(f"[ok] loaded {p}", file=sys.stderr)
    except Exception as e:
        print(f"[local-fail] {p}: {e}", file=sys.stderr)

# --- 输出 ---
new_apis = {a: v for a, v in candidates.items() if a not in EXISTING}
print(f"total type1 sites: {len(candidates)}, existing: {len(EXISTING)}, new: {len(new_apis)}", file=sys.stderr)
out = []
for api, v in sorted(new_apis.items()):
    out.append({"api": api, "name": v["name"], "from": len(v["sources"])})
with open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_candidates.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=1)
print(json.dumps(out, ensure_ascii=False, indent=1))
