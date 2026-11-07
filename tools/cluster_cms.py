# -*- coding: utf-8 -*-
"""对可用源做库指纹聚类：同一关键词的前 N 条 vod_name 序列作为指纹，同指纹=同库，每库留最快域名"""
import json, sys, io, time
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

results = json.load(open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_test_result.json", encoding="utf-8"))
ok_items = [r for r in results if r.get("ok")]

# 排除音乐源（total 异常大且名字带音乐/星源系）与成人向
EXCLUDE_KW = ["咪咕", "星源", "酷我", "A站", "番号", "音乐"]
ok_items = [r for r in ok_items if not any(k in r["name"] for k in EXCLUDE_KW)]
print(f"cluster candidates: {len(ok_items)}", file=sys.stderr)

def fingerprint(item):
    base = item["api"].rstrip("/")
    url = base + "?" + urllib.parse.urlencode({"ac": "detail", "wd": "漫长的季节"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CineAnchor/0.1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=10) as r:
            d = json.loads(r.read())
        vods = d.get("list") or []
        names = tuple(v.get("vod_name", "") for v in vods[:15])
        return {"api": item["api"], "name": item["name"], "ms": item["ms"],
                "fp": names, "total_kw": d.get("total", 0)}
    except Exception as e:
        return {"api": item["api"], "name": item["name"], "ms": item["ms"],
                "fp": None, "total_kw": 0, "err": str(e)[:50]}

fps = []
with ThreadPoolExecutor(max_workers=20) as ex:
    futs = [ex.submit(fingerprint, i) for i in ok_items]
    for f in as_completed(futs):
        fps.append(f.result())

# 聚类：指纹（前15条 name 序列）完全一致 => 同库
clusters = defaultdict(list)
for fp in fps:
    if fp["fp"] is not None and len(fp["fp"]) > 0:
        clusters[fp["fp"]].append(fp)
    else:
        # 失败的单独成簇，标记待定
        clusters[("ERR", fp["api"], str(fp.get("err", "")))].append(fp)

merged = []
for fp_key, members in clusters.items():
    members.sort(key=lambda m: m["ms"])
    best = members[0]
    dup_names = [m["name"] + "|" + m["api"] for m in members[1:]]
    merged.append({
        "name": best["name"], "api": best["api"], "ms": best["ms"],
        "total_kw": best["total_kw"], "lib_size": best["total_kw"],
        "duplicates": dup_names,
    })

# total_kw 也辅助判断：完全不同 total 的库可能内容不同但指纹相同（都只搜到同一批）
merged.sort(key=lambda m: -m["total_kw"])
print(f"\nclusters: {len(merged)}")
for m in merged:
    dup = f" (+{len(m['duplicates'])}换皮)" if m["duplicates"] else ""
    print(f"  {m['ms']:>5}ms kw_total={m['total_kw']:>5}  {m['name']}  {m['api']}{dup}")

with open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_clusters.json", "w", encoding="utf-8") as f:
    json.dump(merged, f, ensure_ascii=False, indent=1)
