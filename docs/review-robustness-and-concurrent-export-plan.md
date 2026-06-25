# 评审稳健性 + 并发导出 优化执行方案

## 背景（为什么做）

2026-06-25 对 90 分钟直播《妇美·愉悦技术规范宣讲——张铃院长》跑真实 live 流程时，
主题评审（`step-3.7-flash`）在 `topic_review_batch_size=4` 下 **17 个批次全部失败**，
整次运行降级为 `unreviewed_no_export`、**0 导出**。探针定位到两类模型返回：

- 多候选时把 `reviews` 数组写成 **`candidates`** 键 → 解析报「must contain a reviews list」；
- 偶发只返回**单个裸评审对象**（漏掉同批其余候选）→ 解析报「missing candidate_id」。

把 `batch_size` 降到 1 后跑通（68 候选全评审、0 失败、导出 6 条）。但这只是规避，暴露出三处
真实脆弱点（评审"全有或全无"降级、解析器不认 `candidates` 别名、schema 失败不重试），
另外导出阶段 6 条 `libx264` 串行重编码是最大的墙钟开销。

本方案修复这四点，使大 batch 评审重新可用、单批偶发失败不再清零产物、导出墙钟显著下降。

## 全局约束

- 遵守仓库规范：**只修生产代码，不改/不删/不放宽既有测试**；新增测试覆盖新行为。
- 每个任务**独立一个 git commit**（中文 Conventional Commits），避免巨型提交。
- 每个 commit 前必须 `python3 -m pytest` 全绿（含新增用例）。
- 不在 `main` 直接堆提交：先切工作分支 `feat/review-robustness-export`（若用户要求直接在当前分支则遵从）。

## 进度清单

- [ ] 任务 1：评审部分失败优雅降级（commit 1）
- [ ] 任务 2：解析器兼容 `candidates`/`results`/`data` 等 reviews 别名（commit 2）
- [ ] 任务 3：schema 失败有限重试 + 重试时强化 schema 约束（commit 3）
- [ ] 任务 4：导出阶段并发裁剪（commit 4）
- [ ] 收尾：回归 + 真实复跑验证（不单独 commit，或并入末次）

---

## 任务 1：评审部分失败优雅降级

**问题**：`StepFunChatReviewer._review_batches_concurrent`（`review.py:182`）只要 `failed_batches`
非空就返回 `success=False`；`_review_live_candidates`（`cli.py:391`）随即返回 `"unreviewed"`、
**丢弃所有已成功评审**。即便 67/68 批成功，1 批失败也会导致整次 0 导出。

**目标**：只要**有任意候选评审成功**，就进入 `reviewed` 选择路径；失败批次内的候选因无
`candidate.review` 在 `_reviewed_rejection_reason` 中自然被跳过（不导出），并在 plan/warnings
中如实记录失败批次。仅当**成功评审数为 0** 时才整体降级为 `unreviewed`。

**改动点**：
- `cli.py` `_review_live_candidates`（约 389-404）：
  - 调 `reviewer.review_batches(batches)` 后，依据 `len(result.reviews)` 与 `result.failed_batches` 决策：
    - `len(result.reviews) > 0`：视为 `reviewed`，应用已成功评审；把失败批诊断写入
      `provider_info["review_diagnostics"]`（复用现有 `_review_failure_diagnostics`），
      并向 `review_warnings` 追加一条"部分批次评审失败（N/M），相关候选已跳过导出"。
    - `len(result.reviews) == 0`：保持现状返回 `"unreviewed"` + 诊断。
  - 不再单纯以 `result.success` 短路。
- `review.py`：`_review_batches_concurrent` / `_review_batches_sequential` 维持"汇总 reviews +
  failed_batches"的现有返回结构即可（已具备）；如需，给 `TopicReviewProviderResult` 增加
  `partial` 语义可选，但优先在 cli 层判定，减少 provider 改动面。
- `plan.py` / `write_plan`：确认 `status="reviewed"` 且 `failed_review_batch_count>0` 时
  plan.json 能体现"部分失败"（`reviewed_candidate_count` / `failed_review_batches` 已有字段，
  核对 orchestration 解释器 `interpret_output_dir` 对该组合的展示不报错）。

**测试（新增，不改旧用例）**：
- `tests/test_review.py`：构造 3 批，其中 1 批返回坏 schema、2 批正常 →
  `review_batches` 返回 `reviews` 含 2 批结果且 `failed_batches` 含 1 条。
- `tests/test_live_*` 或新建 `tests/test_review_partial_degrade.py`：mock reviewer 部分失败 →
  `_review_live_candidates` 返回 `"reviewed"`、成功候选带 review、失败候选无 review、warnings 含失败提示。
- 边界：全部失败 → 仍 `"unreviewed"`（保护既有契约，对应 `test_review.py` 现有失败用例不回归）。

**commit 1**：`feat(review): 评审部分批次失败时保留成功评审而非整次降级`

---

## 任务 2：解析器兼容 reviews 的常见别名键

**问题**：`_coerce_review_items`（`review.py:504-513`）只认 `reviews` 键、裸数组、裸单对象；
模型实际会用 **`candidates`** 键包裹评审数组，导致整批判失败。

**目标**：在不放宽校验严格性的前提下，把 `candidates` / `results` / `data` 作为 `reviews` 的
**别名**接受（仅当其值为 list 时），其余非法形态仍按现有逻辑报错。每个评审对象仍走
`_parse_review_payload` 的完整必填字段与 candidate_id 校验，杜绝"掩盖契约违例"。

**改动点**：
- `review.py` `_coerce_review_items`：在 `isinstance(payload, dict)` 分支内，
  按优先级检查 `reviews` → `candidates` → `results` → `data`，命中且为 list 则返回该 list；
  其余分支（裸 `candidate_id`、`REQUIRED_REVIEW_FIELDS` 子集、裸数组）保持不变。
- 用常量列表 `_REVIEW_LIST_ALIASES = ("reviews", "candidates", "results", "data")` 表达，便于扩展与阅读。

**测试（新增）**：
- `tests/test_review.py`：
  - `{"candidates":[<4条>]}` 多候选 → 4 条全部正确解析、candidate_id 一一对应。
  - `{"results":[...]}` / `{"data":[...]}` 同样可解析。
  - 反例：`{"candidates":"x"}`（非 list）仍抛 `must contain a reviews list`，确保未放宽。
  - 保留并验证既有 `reviews`/裸数组/裸对象用例不回归。

**commit 2**：`fix(review): 兼容评审模型用 candidates/results 等键包裹 reviews 数组`

---

## 任务 3：schema 失败有限重试并强化约束

**问题**：`_request_batch_with_retry`（`review.py:247`）仅对 HTTP/网络错误重试；
200 响应但结构不合规（`invalid_topic_json` / `invalid_schema`）在 `_review_batch`
（`review.py:206-243`）里**一次性判死**，`topic_review_retry_attempts=3` 对此类失效。

**目标**：把"请求 + 解析"作为一个可重试单元；当解析/ schema 失败且仍有重试余额时，
**重发请求**，且在重试请求里追加一条强约束 user 消息（例如：「上一次返回结构不合规，
必须严格返回 {"reviews": [...]}，每个候选一条，禁止任何额外文字」）。重试用尽仍失败则
按现有 `failure_type` 落败（交由任务 1 的部分降级兜底）。

**改动点**：
- `review.py` `_review_batch`：把 `json.loads` / `_extract_chat_content` / `_parse_review_payload`
  的失败路径纳入重试循环。两种实现取其一：
  - 方案 A（推荐，集中）：将解析逻辑收进 `_request_batch_with_retry` 的循环体，
    成功解析才 return，schema 类失败时 `_sleep_before_retry` 后 continue，循环末尾再落败。
  - 方案 B：新增 `_review_batch_with_retry` 包裹 `_review_batch` 单次逻辑。
  - 注意：缓存命中（`_read_cached_batch`）与 `invalid_config`（不可重试）保持现有短路。
- `_build_request`：支持可选的"重试强化提示"参数（仅在 attempt>1 时附加 user 消息），
  不改变首次请求体，避免 `_cache_signature` 变化导致缓存失效（签名只基于 batch 内容，与提示无关，需核对）。
- 可选：`_build_request` 增加 `max_tokens`（来自 config，默认足够大）防止高 `reasoning_effort` 截断。
  若加，需同步 `_cache_signature` 是否纳入 `max_tokens`（建议纳入，保持缓存正确性）。

**测试（新增）**：
- `tests/test_review.py`：mock `request_func`，第 1 次返回坏结构、第 2 次返回合规 →
  最终 `success=True`、`reviews` 完整、发生了 2 次请求（计数断言）。
- 重试用尽：连续返回坏结构 → `failure_type` 为 schema 类、`attempt==retry_attempts`。
- 确认 `invalid_config`（如缺 base_url）**不**重试。

**commit 3**：`feat(review): 对结构不合规的评审响应做有限重试并强化 schema 约束`

> 完成 1+2+3 后，可把 `config.local.json` 的 `topic_review_batch_size` 调回 2~4 复测；
> 验证大 batch 重新稳定（请求数下降、无整次清零）。该配置回调单独说明，不混入代码 commit。

---

## 任务 4：导出阶段并发裁剪

**问题**：`export_live_clips`（`export.py:36-79`）串行循环对每条 `clip_segment` 做
`libx264` 重编码（`media.py:21-36`）。6 条约 6 分钟，是最大墙钟开销。各 clip 互相独立。

**目标**：并发执行各 clip 的 ffmpeg 裁剪（受 `export_concurrency` 控制），
**保持产物顺序、文件名编号、失败清理、metadata 内容与现状完全一致**。

**改动点**：
- `config.py`：新增 `"export_concurrency": 4`（默认值，1 表示退回串行）。
- `export.py` `export_live_clips`：
  - 先按 `enumerate(selected, 1)` 计算好每条的 `output_index` / `filename_base` /
    `output_path` / `subtitle_path`（纯计算，确定顺序）。
  - 用 `ThreadPoolExecutor(max_workers=min(export_concurrency, len(selected)))` 并发跑
    `clip_segment`（subprocess I/O 型，线程安全）。字幕 `export_srt` 切片为纯 CPU，可在
    各 future 内或主线程顺序执行（建议放 future 内并行）。
  - **失败语义不变**：任一 clip 失败 → 取消/等待其余 → `_cleanup_written_paths(全部已写)` → 返回 `None`。
    需收集所有线程实际写出的路径用于清理（注意线程安全地 append，或各 future 返回写出路径后主线程汇总）。
  - `exports` 列表按 `output_index` 升序构建（用结果字典按序组装），保证与串行一致。
- `media.py`：`clip_segment` 已是独立 subprocess，无需改；并发由 export 层控制。

**测试（新增/复用）**：
- `tests/test_live_export.py`：mock `clip_segment`（记录调用、可注入某条失败）：
  - 成功路径：N 条并发 → exports 顺序与编号正确、metadata 含 N 条、字幕路径正确。
  - 失败路径：第 k 条失败 → 返回 None、已写文件被清理（断言 `_cleanup_written_paths` 覆盖全部）。
  - `export_concurrency=1` → 退回串行行为，既有 `test_live_export*` 用例不回归。
- 确认 `test_live_export_e2e.py` / `test_e2e_verify_deliverables.py` 仍绿。

**commit 4**：`perf(export): 并发裁剪导出短视频以缩短墙钟耗时`

---

## 收尾与验证

1. 全量回归：`python3 -m pytest -q`（全绿，含全部新增用例）。
2. 真实复跑（复用 ASR 缓存，省时省钱）：
   - 先恢复 `config.local.json` `topic_review_batch_size` 至 4，验证任务 1-3 让大 batch 重新跑通；
   - `python3 -m video_auto_editor live "<mp4>" --output-dir out/live-verify --work-dir work --context-file <ctx> --config-file config.local.json`
   - 期望：`status=reviewed`、`failed_review_batch_count` 即使 >0 也仍有导出、导出墙钟较此前下降。
3. 用 `interpret_output_dir` 核对 run_mode / exports / warnings；`ffprobe` 抽查导出时长。
4. 关键经验已在记忆 `[[topic-review-batchsize-workaround]]` / `[[live-autocut-cli-invocation]]`，
   修复落地后补一条"大 batch 已恢复可用"的更新。

## 不在本方案范围（后续可选）

- ffmpeg `-ss` 前置/前粗后精的 seek 提速与边界精度核对（优化分析中的任务 5）。
- ASR 分片并发转写（任务 6）。
- 评审缓存签名分层以提高跨上下文复用（任务 9）。
- export_decision 枚举归一化、preflight 对 `python -m` 形态的兼容。
