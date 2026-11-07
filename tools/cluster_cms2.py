# -*- coding: utf-8 -*-
"""改进版聚类：用结果丰富的关键词(狂飙)取指纹 + ac=videolist 实测(代码真实调用形式)"""
import json, sys, io, time
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

results = json.load(open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_test_result.json", encoding="utf-8"))
ok_items = [r for r in results if r.get("ok")]
EXCLUDE_KW = ["咪咕", "星源", "酷我", "A站", "番号", "音乐", "成人", "🔞"]
ok_items = [r for r in ok_items if not any(k in r["name"] for k in EXCLUDE_KW)]

def norm_base(api):
    """去掉 query 与 /from/xxx/ 路径后缀，得到干净 base"""
    a = api.split("?")[0]
    # 去 /from/xxx /at/json /at/m3u8 等播放源过滤段
    for seg in ("/from/", "/at/"):
        i = a.find(seg)
        if i >= 0:
            # 只在 provide/vod 之后出现才截断
            j = a.find("/provide/vod")
            if j >= 0 and i > j:
                a = a[:i]
    return a.rstrip("/")

def probe(item):
    """用代码真实调用形式 ac=videolist&wd=狂飙 测，并取指纹"""
    base = norm_base(item["api"])
    url = f"{base}?ac=videolist&wd={urllib.parse.quote('狂飙')}"
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CineAnchor/0.1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as r:
            d = json.loads(r.read())
        vods = d.get("list") or []
        # 指纹: 前12条 (name, year, remarks) —— 内容相同=同库
        fp = tuple((v.get("vod_name",""), str(v.get("vod_year","")), (v.get("vod_remarks") or "")[:10]) for v in vods[:12])
        n_lines = 0
        if vods:
            raw = vods[0].get("vod_play_url") or ""
            n_lines = len([g for g in raw.split(";;;") if g.strip()])
        return {"name": item["name"], "orig": item["api"], "base": base,
                "ms": int((time.time()-t0)*1000), "total": d.get("total", 0),
                "fp": fp if len(fp) >= 3 else None, "n_lines": n_lines,
                "play_sample": (vods[0].get("vod_play_url") or "")[:80] if vods else ""}
    except Exception as e:
        return {"name": item["name"], "orig": item["api"], "base": base, "ms": 99999,
                "total": 0, "fp": None, "err": f"{type(e).__name__}: {str(e)[:40]}"}

probes = []
with ThreadPoolExecutor(max_workers=20) as ex:
    futs = [ex.submit(probe, i) for i in ok_items]
    for f in as_completed(futs):
        probes.append(f.result())

# 聚类
clusters = defaultdict(list)
for p in probes:
    if p["fp"]:
        clusters[p["fp"]].append(p)
    else:
        clusters[("ERR", p["base"])].append(p)

print(f"probes: {len(probes)}, clusters: {len(clusters)}\n")
final = []
for fp, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
    members.sort(key=lambda m: m["ms"])
    best = members[0]
    # 展示前5个成员
    others = [f"{m['name']}({m['base']})" for m in members[1:6]]
    more = f" ...等{len(members)}个" if len(members) > 6 else ""
    print(f"kw_total={best['total']:>6} nlines={best['n_lines']}  最快={best['ms']}ms  {best['name']}  {best['base']}")
    if others:
        print(f"         同库换皮: {', '.join(others)}{more}")
    final.append({
        "name": best["name"].strip(), "base": best["base"], "ms": best["ms"],
        "total": best["total"], "n_lines": best["n_lines"],
        "dup_count": len(members) - 1,
        "play_sample": best["play_sample"],
        "err": best.get("err"),
    })

with open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_clusters2.json", "w", encoding="utf-8") as f:
    json.dump(final, f, ensure_ascii=False, indent=1)
