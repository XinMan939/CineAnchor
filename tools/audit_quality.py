# -*- coding: utf-8 -*-
"""画质审计：每个 provider 的线路拉 m3u8 -> 解析 RESOLUTION/BANDWIDTH -> ffprobe 探真实分辨率/码率"""
import json, sys, io, subprocess, re
import urllib.request, urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DATA = r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\sources_test3.json"

d = json.load(open(DATA, encoding="utf-8"))
lines = d.get("lines", [])

# 每个 provider 挑前 2 条线路（已按 quality 排序）
by_prov = {}
for l in lines:
    by_prov.setdefault(l["provider"], []).append(l)

picks = []
for prov, ls in by_prov.items():
    for l in ls[:2]:
        picks.append(l)

print(f"providers: {len(by_prov)}, 抽样线路: {len(picks)}\n", file=sys.stderr)

UA = {"User-Agent": "Mozilla/5.0"}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def fetch_url(url, timeout=10):
    req = urllib.request.Request(url, headers=UA)
    with OPENER.open(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")

def probe_line(item):
    """探测单条线路: 返回 master playlist 的最高 RESOLUTION/BANDWIDTH + ffprobe 实测"""
    out = {"provider": item["provider"], "episode": item["episode"],
           "url": item["url"], "m3u8_res": None, "m3u8_bw": None,
           "codecs": None, "ffprobe": None, "err": None}
    try:
        final_url, body = fetch_url(item["url"])
        if not body.lstrip().startswith("#EXTM3U"):
            out["err"] = "not-hls"
            return out
        # master playlist: 解析所有 STREAM-INF
        best_res, best_bw = 0, 0
        variants = []
        for m in re.finditer(r"#EXT-X-STREAM-INF:([^\n]+)\n([^\n]+)", body):
            attrs, uri = m.group(1), m.group(2).strip()
            rm = re.search(r'RESOLUTION=(\d+)x(\d+)', attrs)
            bm = re.search(r'BANDWIDTH=(\d+)', attrs)
            cm = re.search(r'CODECS="([^"]+)"', attrs)
            w, h = (int(rm.group(1)), int(rm.group(2))) if rm else (0, 0)
            bw = int(bm.group(1)) if bm else 0
            variants.append((h, bw, cm.group(1) if cm else None))
            if h > best_res:
                best_res, best_bw = h, bw
                out["codecs"] = cm.group(1) if cm else None
        if variants:
            out["m3u8_res"] = f"{best_res}p"
            out["m3u8_bw"] = best_bw
            out["n_variants"] = len(variants)
            # ffprobe 实测 master playlist 最高档
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,codec_name,bit_rate,avg_frame_rate",
                 "-of", "json", final_url],
                capture_output=True, text=True, timeout=25)
            try:
                pr = json.loads(r.stdout)
                s = (pr.get("streams") or [{}])[0]
                if s:
                    out["ffprobe"] = {
                        "res": f"{s.get('width')}x{s.get('height')}",
                        "codec": s.get("codec_name"),
                        "fps": s.get("avg_frame_rate"),
                    }
            except Exception:
                pass
        else:
            # media playlist (单档) -> 直接 ffprobe
            r = subprocess.run(
                ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height,codec_name,bit_rate,avg_frame_rate",
                 "-of", "json", final_url],
                capture_output=True, text=True, timeout=25)
            try:
                pr = json.loads(r.stdout)
                s = (pr.get("streams") or [{}])[0]
                if s:
                    out["ffprobe"] = {
                        "res": f"{s.get('width')}x{s.get('height')}",
                        "codec": s.get("codec_name"),
                        "fps": s.get("avg_frame_rate"),
                    }
            except Exception:
                pass
    except Exception as e:
        out["err"] = f"{type(e).__name__}: {str(e)[:40]}"
    return out

results = []
with ThreadPoolExecutor(max_workers=6) as ex:  # ffprobe 重, 少并发
    futs = [ex.submit(probe_line, p) for p in picks]
    done = 0
    for f in as_completed(futs):
        done += 1
        r = f.result()
        results.append(r)
        print(f"... {done}/{len(picks)} {r['provider']}: {r.get('ffprobe') or r.get('m3u8_res') or r.get('err')}", file=sys.stderr)

# 按 provider 聚合最高画质
by = {}
for r in results:
    p = r["provider"]
    if p not in by:
        by[p] = {"provider": p, "samples": []}
    by[p]["samples"].append(r)

with open(r"C:\Users\53085\WorkBuddy\2026-11-06-00-23-00\CineAnchor\tools\quality_audit.json", "w", encoding="utf-8") as f:
    json.dump(list(by.values()), f, ensure_ascii=False, indent=1)

print("\n=== 画质审计 (狂飙, 每源 2 条线路) ===")
for p, v in sorted(by.items(), key=lambda kv: -max([0] + [int((s.get("ffprobe") or {}).get("res", "0x0").split("x")[-1] or 0) for s in v["samples"] if s.get("ffprobe")])):
    samples = v["samples"]
    infos = []
    for s in samples:
        fp = s.get("ffprobe")
        if fp:
            infos.append(f"{fp['res']} {fp['codec']}")
        elif s.get("m3u8_res"):
            infos.append(f"{s['m3u8_res']}(m3u8)")
        else:
            infos.append(f"ERR:{str(s.get('err'))[:20]}")
    print(f"  {p}: {' / '.join(infos)}")
