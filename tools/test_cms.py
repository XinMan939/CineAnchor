# -*- coding: utf-8 -*-
"""并发测试候选 CMS 源可用性：真实搜索请求，保留返回合法 JSON 且有数据的"""
import json, sys, io, time
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

CAND = r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_candidates.json"
TIMEOUT = 8
KEYWORD = "狂飙"  # 热门剧，大库必有

def test_one(item):
    api = item["api"]
    # 规范化：去尾斜杠
    base = api.rstrip("/")
    url = base + "?" + urllib.parse.urlencode({"ac": "detail", "wd": KEYWORD})
    t0 = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "CineAnchor/0.1"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 不走系统代理
        with opener.open(req, timeout=TIMEOUT) as r:
            raw = r.read()
        dt = time.time() - t0
        try:
            data = json.loads(raw)
        except Exception:
            return {**item, "ok": False, "reason": "not-json"}
        total = data.get("total") or 0
        vods = data.get("list") or []
        if total <= 0 and not vods:
            return {**item, "ok": False, "reason": f"empty (total={total})"}
        # 验证字段结构
        if vods and not isinstance(vods[0], dict):
            return {**item, "ok": False, "reason": "bad-structure"}
        # 提取第一条的 vod_play_url 佐证
        sample = ""
        if vods:
            sample = (vods[0].get("vod_play_url") or "")[:60]
        return {**item, "ok": True, "ms": int(dt*1000), "total": total, "sample": sample}
    except Exception as e:
        return {**item, "ok": False, "reason": f"{type(e).__name__}: {str(e)[:60]}"}

def main():
    cands = json.load(open(CAND, encoding="utf-8"))
    results = []
    with ThreadPoolExecutor(max_workers=20) as ex:
        futs = {ex.submit(test_one, c): c for c in cands}
        done = 0
        for f in as_completed(futs):
            done += 1
            try:
                results.append(f.result())
            except Exception as e:
                results.append({"api": futs[f]["api"], "name": futs[f].get("name",""), "ok": False, "reason": str(e)[:60]})
            if done % 40 == 0:
                print(f"... {done}/{len(cands)}", file=sys.stderr)
    ok = sorted([r for r in results if r.get("ok")], key=lambda r: r.get("ms", 99999))
    bad = [r for r in results if not r.get("ok")]
    print(f"OK: {len(ok)} / FAIL: {len(bad)}")
    print("\n=== 可用源（按响应速度排序）===")
    for r in ok:
        print(f"  {r['ms']:>5}ms total={r.get('total',0):>5} {r['api']}  [{r['name']}]")
    with open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\cms_test_result.json", "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=1)

if __name__ == "__main__":
    main()
