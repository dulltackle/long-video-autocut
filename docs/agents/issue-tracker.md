# 议题追踪器：GitHub

本仓库的议题与规格使用 GitHub Issues 管理。所有操作默认针对当前
Git 远端对应的 GitHub 仓库。

## 基本约定

- 创建、读取、评论、标记和关闭议题时使用 GitHub Issues。
- 议题标题和正文使用中文。
- Pull Request 默认不作为需求或 triage 入口。
- 引用议题时优先使用带标题的链接，不用裸编号代替名称。

## Wayfinding 操作

- 地图：一个带 `wayfinder:map` 标签的 GitHub Issue。
- 决策票据：地图的 GitHub 子议题，标签为：
  - `wayfinder:research`
  - `wayfinder:prototype`
  - `wayfinder:grilling`
  - `wayfinder:task`
- 若仓库未启用子议题，则在地图中使用任务清单，并在票据正文顶部写
  `Part of #<map>`。
- 阻塞关系优先使用 GitHub 原生 issue dependencies。
- 若原生依赖不可用，则在票据正文顶部使用
  `Blocked by: #<issue>, #<issue>`。
- 前沿是地图中所有未关闭、未被阻塞且未分配的子议题。
- 认领票据时首先把票据分配给当前执行者。
- 解决票据时：
  1. 发布结论评论；
  2. 关闭票据；
  3. 在地图的 `Decisions so far` 中追加带标题链接和一句话摘要。
