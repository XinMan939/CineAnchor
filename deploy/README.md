# CineAnchor 部署指南

一条龙: Docker + Nginx 反代 + HTTPS + 定时索引。

## 前置要求

- 服务器装好 Docker 与 docker compose 插件
- 一个域名 (可选, 但上 HTTPS 需要)
- TMDB API Read Access Token (<https://www.themoviedb.org/settings/api>)

## 1. 拉代码 + 配置

```bash
git clone https://github.com/XinMan939/CineAnchor.git
cd CineAnchor
cp tools/config.example.json config.json
# 编辑 config.json: 填入 tmdb_token, 按需增删 providers
```

## 2. 国内服务器注意: TMDB 被墙

`api.themoviedb.org` / `api.tmdb.org` 国内不可达。两种解法:

**a. 海外机**: 直接用, 忽略本节。

**b. 国内机**: 自建反代 (Cloudflare Worker 最省事), 然后在
`config.json` 里把 `tmdb_host` 改成你的代理域名:

```json
{ "tmdb_host": "https://your-tmdb-proxy.example.com", ... }
```

Worker 参考逻辑 (10 行):

```js
export default {
  async fetch(req) {
    const url = new URL(req.url);
    url.hostname = "api.tmdb.org";
    return fetch(new Request(url, req));
  },
};
```

## 3. 启动

```bash
docker compose up -d --build
docker compose ps          # 等 app 变 (healthy)
curl http://127.0.0.1/api/v1/health
```

首次先抓一轮内容库:

```bash
curl -X POST "http://127.0.0.1/api/v1/indexer/run?pages=5"
curl "http://127.0.0.1/api/v1/latest?n=10"    # 应返回刚抓的条目
```

## 4. 定时索引 (crontab)

```bash
crontab -e
# 每天凌晨 3 点抓 10 页最新
0 3 * * * curl -s -X POST "http://127.0.0.1/api/v1/indexer/run?pages=10" > /dev/null
```

## 5. HTTPS (Let's Encrypt)

```bash
# 证书签好后放进 deploy/certs/, 解开 nginx.conf 里的 443 段与 compose 的 certs 挂载
docker compose restart nginx
```

浏览器对非 HTTPS 页面里的 HLS 播放有限制 (混合内容策略), 公网页面务必上 HTTPS。

## 6. 数据持久化

`./data/` 目录 (docker volume 映射) 里有两个 SQLite:

| 文件 | 内容 | 丢失后果 |
|---|---|---|
| `cache.db` | TMDB/线路池缓存 | 无所谓, 重新请求自动重建 |
| `index.db` | 内容库 (最新上线) | 重新跑一轮 indexer 重建 |

备份只需拷 `index.db`。

## 7. 常见问题

- **前端能开但播不了**: 大概率是 m3u8 CDN 防盗链 (Referer 校验) 或 HTTPS 页面
  加载 HTTP 源 (混合内容)。前者无解 (CDN 直连不经我们代理), 后者检查源 URL 是否
  支持 https。
- **天涯等源反复超时**: 熔断器会自动屏蔽 30 分钟, `/api/v1/health` 里看
  `circuit` 字段。
- **换服务器迁移**: `git clone` + `config.json` + `data/index.db` 三个东西拷走,
  `docker compose up -d --build` 完事。
