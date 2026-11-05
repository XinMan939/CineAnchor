# CineAnchor · 影锚

> A title-anchored media aggregator. 以作品为锚，聚合可切换播放线路。

## 这是什么

一个"作品驱动"的影视聚合软件（空壳 + 聚合层），区别于 FongMi/TVBox 生态的"源驱动"逻辑：

| | 源驱动（FongMi/TVBox） | 作品驱动（CineAnchor） |
|---|---|---|
| 系统锚点 | 接口源的 JSON 配置 | **TMDB ID** |
| 首页内容 | 源里有什么就展示什么 | TMDB 榜单/元数据，稳定不变 |
| 作品概念 | 不存在，只有"某源里的某条目" | 作品固定，源是动态挂载的线路池 |
| 换源效果 | 首页海报墙/分类完全变样 | 只影响该作品的线路列表 |

## 核心架构

```
TMDB (作品索引/主键)
        │
        ▼
   映射层 (TMDB ID ↔ 采集站 vod_id)
        │  ① 采集站自带 tmdb_id 字段 → 直接用
        │  ② 标题+年份 → TMDB 搜索匹配（处理中文名/别名/季数偏移）
        ▼
   线路池 (多采集站的 m3u8 聚合、去重、健康度排序)
        │
        ▼
   客户端 (播放器壳：详情页取 TMDB 元数据 + 线路切换面板)
```

## Roadmap

- [ ] **Phase 0 — 验证映射链路（第一步，当前）**
  - [ ] 申请 TMDB API Key
  - [ ] 后端小工具：输入 TMDB ID → 输出多采集站对应播放地址列表
  - [ ] 映射策略：tmdb_id 直连 > 标题+年份搜索兜底
  - [ ] 多源线路聚合 + 去重
- [ ] Phase 1 — 最小服务（API 化，缓存映射结果）
- [ ] Phase 2 — Android 客户端（播放器壳 + 换源面板）
- [ ] Phase 3 — 榜单/推荐/搜索（TMDB trending、豆瓣榜单等）

## 参考项目

- **AIStream** — 架构最接近：TMDB 做元数据，采集站做线路池，服务端聚合
- **WebHomeTV** — FongMi 壳 + TMDB 海报增强（仍在源驱动框架内，仅参考 TMDB 接入方式）
- **MoviePilot V3** — `media_source + media_id` 统一媒体表示，映射去重的思路值得借鉴
