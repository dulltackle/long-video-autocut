# 第五批：导出选择与标准交付物闭环

第五批覆盖“导出选择与标准交付物”的最小可交付范围。目标是在第四批已经具备 reviewed `plan.json` 和主题评审结果的前提下，基于评审结果默认导出全部发布就绪短视频，并通过不依赖真实外部服务和真实视频处理的端到端测试证明 CLI 可以生成完整交付包。

范围边界：

- 默认只导出主题评审判定为发布就绪的候选。
- `--max-clips` 从默认保护上限调整为用户显式可选上限。
- 未评审、评审失败、评审关闭或缺少 API Key 时，默认不导出短视频；只有显式 `--allow-unreviewed-export` 才允许沿用未评审导出路径。
- 增强 `plan.json`，让机器可读的导出选择、未导出原因、人工复核原因和最终导出清单清晰可见。
- 增强 `metadata.json`，记录源视频、评审 provider、导出短视频、字幕、主题、评分、导出决策和人工复核信息。
- 增强 `拆条报告.md`，面向人工列出导出清单、未导出候选、淘汰原因、人工复核项和交付物清单。
- 支持最小边界补救表达：保留原候选边界、最终导出边界和边界补救建议；只在规则和字段明确时调整导出边界，不从自然语言建议中猜测时间。
- 支持最小同主题系列表达：在 `metadata.json` 和报告中按 `topic_name` 聚合导出项，先不实现复杂系列包装和跨主题重排。
- 建立最小 reviewed `live` 非 dry-run 端到端测试闭环。
- 不改变候选生成算法。
- 不创建 skill 调度器。
- 不实现发布平台上传、封面生成、标题 A/B 测试或社媒发布。

## 提交硬性要求

第五批必须按小任务拆分提交。每完成一个任务并通过该任务列出的验证命令后，必须立即执行一次 `git commit`，不得等整批完成后一次性提交。

每次提交前必须确认：

- 只包含当前小任务范围内的改动。
- 不修改测试代码来规避失败。
- 失败测试只能通过修复生产代码解决。
- `git status --short` 中没有意外文件混入提交范围。

## 任务 1：定义发布就绪导出选择契约

建议新增或扩展文件：

- `video_auto_editor/models.py`
- `video_auto_editor/selection.py`
- `tests/test_live_selection.py`
- 必要时新增 `tests/test_export_decision.py`

执行项：

- 定义候选的导出选择结果结构，至少覆盖：
  - `candidate_index`
  - `selected_for_export`
  - `decision`
  - `reason`
  - `review_status`
  - `publish_ready_score`
  - `export_rank`
  - `final_start`
  - `final_end`
  - `topic_name`
  - `needs_human_review`
- 明确发布就绪判定规则：
  - 必须存在结构化 `review`。
  - `review.export_decision` 必须为 `publish_ready`。
  - `review.publish_ready_score` 必须大于等于 `topic_review_publish_ready_threshold`。
  - `review.topic_complete` 必须为 true。
  - `review.needs_human_review` 必须为 false。
  - 候选不能是 duplicate。
- `--max-clips` 只在用户显式传入时作为发布就绪候选的数量上限；未显式传入时不再使用 `temporary_protective_max_clips` 截断发布就绪结果。
- 保留未评审兼容路径：只有 `allow_unreviewed_export=True` 时，未评审候选才可按既有分数选择逻辑导出。
- 对所有未导出候选写入稳定、可测试的原因码或原因文本。

验收标准：

- reviewed 候选只导出发布就绪项。
- 需人工复核、评分不足、主题不完整、重复候选不会被默认导出。
- 未显式 `--max-clips` 时不截断发布就绪候选。
- 显式 `--max-clips` 时按质量排序应用上限，再按时间顺序导出。
- 未评审且未显式允许时导出列表为空，并产生清晰原因。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 定义发布就绪导出选择契约
```

提交前验证：

```bash
pytest tests/test_live_selection.py
pytest tests/test_export_decision.py
pytest
```

## 任务 2：接入 live 导出选择并保留降级语义

改动文件：

- `video_auto_editor/cli.py`
- `video_auto_editor/selection.py`
- `video_auto_editor/export.py`
- `tests/test_media_report_cli.py`
- `tests/test_live_export.py`

执行项：

- 在主题评审完成后，根据任务 1 的导出选择契约重新计算 `selected`。
- dry-run 输出完整导出方案，但不调用视频裁剪、不写 `metadata.json`、不写 clips 或单条字幕。
- 非 dry-run 只导出 `selected_for_export=True` 的候选。
- 主题评审失败、关闭或不可用时：
  - 默认 `selected` 为空，不导出短视频。
  - `plan.json` 和报告保留基础候选与 warnings。
  - 显式 `--allow-unreviewed-export` 时，才允许走未评审兼容选择。
- ASR 失败仍直接中止，不进入主题评审或导出选择。
- 保证导出失败时不会留下部分 `metadata.json` 或半成品文件。

验收标准：

- reviewed 成功时，非 dry-run 只导出发布就绪候选。
- reviewed 成功但无发布就绪候选时，CLI 正常结束并写出无导出结果的 plan 和报告。
- 评审不可用且未显式允许未评审导出时，不调用 `clip_segment`。
- 显式允许未评审导出时，兼容既有分数选择路径。
- dry-run 不生成 `metadata.json`、`clips/*.mp4` 和单条短视频字幕。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: live 默认导出发布就绪候选
```

提交前验证：

```bash
pytest tests/test_media_report_cli.py
pytest tests/test_live_export.py
pytest tests/test_live_selection.py
pytest
```

## 任务 3：补齐边界补救和同主题系列的最小数据表达

改动文件：

- `video_auto_editor/models.py`
- `video_auto_editor/selection.py`
- `video_auto_editor/export.py`
- `tests/test_live_selection.py`
- `tests/test_live_export.py`

执行项：

- 为导出选择结果记录原候选边界和最终导出边界。
- 当边界补救可以通过明确字段或确定规则表达时，写入 `final_start`、`final_end` 和 `boundary_fix_applied`。
- 当只有自然语言 `boundary_fix_suggestion` 时，不猜测时间，只保留建议并将候选归入人工复核或未导出原因。
- 为导出项记录 `topic_name`，并生成稳定的 `topic_group` 或 `series_key`，用于同主题系列聚合。
- 同主题系列只影响 metadata 和报告分组，不改变候选时间顺序、不做跨主题重排。

验收标准：

- `metadata.json` 和 `plan.json` 能同时看到原候选边界和最终导出边界。
- 自然语言边界建议不会被误解析成时间调整。
- 同一 `topic_name` 的导出项拥有相同稳定 series 标识。
- 不同主题不会被错误合并为同一系列。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 记录直播导出边界和主题系列
```

提交前验证：

```bash
pytest tests/test_live_selection.py
pytest tests/test_live_export.py
pytest
```

## 任务 4：增强 plan.json 与 metadata.json 标准交付物

改动文件：

- `video_auto_editor/plan.py`
- `video_auto_editor/export.py`
- `tests/test_plan.py`
- `tests/test_live_export.py`

执行项：

- 扩展 `plan.json`：
  - 顶层写入导出模式、发布就绪阈值、导出数量、未导出数量。
  - 每个候选写入导出选择结果。
  - 写入 `exports` 清单，包含最终导出顺序和相对路径占位。
  - dry-run 时 `exports` 只表达计划，不写实际文件路径为已生成状态。
- 扩展 `metadata.json`：
  - 写入 `source_video`、`generated_at`、`status`、`review_provider`、`publish_ready_threshold`。
  - 每条导出记录包含标题、摘要、关键词、主题、评分、导出决策、边界、字幕路径、视频路径、series 标识。
  - 记录人工复核和未导出摘要，便于 skill 或外部系统读取。
- 保持旧字段的兼容性，避免已有测试和使用方读取 `clips` 时失败。

验收标准：

- dry-run `plan.json` 能完整表达将导出的发布就绪列表。
- 非 dry-run `metadata.json` 能作为完整交付物索引。
- metadata 中所有文件路径使用相对输出目录路径。
- 旧的 `metadata["clips"]` 读取方式仍可用。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增强直播拆条标准元数据
```

提交前验证：

```bash
pytest tests/test_plan.py
pytest tests/test_live_export.py
pytest
```

## 任务 5：增强拆条报告

改动文件：

- `video_auto_editor/report.py`
- `tests/test_media_report_cli.py`

执行项：

- 报告顶部区分：
  - reviewed dry-run 发布方案。
  - reviewed 非 dry-run 交付包。
  - unreviewed 且未导出方案。
  - 显式允许未评审导出的兼容方案。
- 增加导出清单，列出视频文件、字幕文件、主题、发布就绪评分、最终边界和标题。
- 增加未导出候选清单，列出未导出原因、淘汰原因、人工复核原因和边界补救建议。
- 增加人工复核清单。
- 增加同主题系列分组。
- 增加标准交付物清单，明确 `plan.json`、`transcript.srt`、`metadata.json`、`clips/`、`subtitles/` 是否生成。

验收标准：

- reviewed dry-run 报告能看到计划导出的发布就绪项，但明确没有生成视频文件。
- reviewed 非 dry-run 报告能看到实际导出文件和字幕文件。
- 未导出候选不会只表现为“lower score or overlap”，而是包含评审或选择原因。
- 人工复核项和边界补救建议不会丢失。
- 报告不会渲染 `None`，Markdown 表格中特殊字符被转义。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增强直播拆条交付报告
```

提交前验证：

```bash
pytest tests/test_media_report_cli.py
pytest
```

## 任务 6：补充 reviewed live 导出最小端到端测试闭环

建议新增或扩展文件：

- `tests/test_live_export_e2e.py`
- 必要时扩展 `tests/test_live_dry_run_e2e.py`

测试目标：

- 从 `cli.main(["live", video_path, ...])` 入口执行非 dry-run。
- 不依赖真实 StepAudio、Whisper、ffmpeg、ffprobe、真实评审模型或真实视频文件。
- 使用 fake StepAudio HTTP 响应覆盖至少两个音频分片。
- 使用 fake 主题评审响应覆盖至少三个候选：
  - 至少两个 `publish_ready`。
  - 至少一个 `needs_review` 或 `reject`。
  - 至少一个候选带 `boundary_fix_suggestion` 或人工复核标记。
- fake `ffprobe` 返回视频时长。
- fake `ffmpeg` 覆盖音频提取、音频分片和短视频裁剪，并写出可断言的占位文件。
- 尽量少 mock CLI 内部编排本身，确保执行到以下真实逻辑：
  - 音频提取与分片计划。
  - 分片请求与时间戳合并。
  - `transcript.srt` 写出。
  - 候选生成、去重、基础选择。
  - 相邻候选批次构造。
  - 主题评审结果解析与写回。
  - 发布就绪导出选择。
  - 短视频和单条字幕导出。
  - `plan.json` 写出。
  - `metadata.json` 写出。
  - `拆条报告.md` 写出。

验收标准：

- 测试断言 `transcript.srt` 存在且包含跨分片合并后的全局时间戳。
- 测试断言 `plan.json` 存在，且 `status=reviewed`。
- 测试断言 `plan.json` 包含导出选择结果和计划导出清单。
- 测试断言 `metadata.json` 存在，且只包含发布就绪导出项。
- 测试断言 `clips/*.mp4` 和 `subtitles/*.srt` 数量与发布就绪导出项一致。
- 测试断言报告包含实际导出清单、未导出原因、人工复核项和同主题系列信息。
- 测试断言未显式 `--max-clips` 时不会被临时保护上限截断。
- 测试能够在无网络、无真实 ASR 服务、无真实评审模型、无真实视频处理环境中稳定通过。

完成本任务后必须 git commit。

建议 commit message：

```text
test: 补充发布就绪导出端到端闭环
```

提交前验证：

```bash
pytest tests/test_live_export_e2e.py
pytest tests/test_live_dry_run_e2e.py
pytest
```

## 任务 7：补齐第五批文档与运行说明

改动文件：

- `docs/implementation-plan.md`
- `docs/implementation-batch-5-export-deliverables.md`
- 必要时补充 `CONTEXT.md`

执行项：

- 在整体计划中登记第五批执行文档。
- 记录第五批完成后的默认导出行为。
- 记录 `--max-clips` 的新语义：只作为显式上限。
- 记录未评审默认不导出和 `--allow-unreviewed-export` 的兼容语义。
- 记录标准交付物：`transcript.srt`、`plan.json`、`metadata.json`、`clips/`、`subtitles/`、`拆条报告.md`。
- 记录本地 reviewed 非 dry-run 运行方式和产物检查方式。
- 说明第六批才处理 skill 调度器。

验收标准：

- 文档能指导下一位开发者从第四批继续执行第五批。
- 文档明确每个任务完成后都必须提交一次。
- 文档没有承诺第五批不会实现的 skill 调度、发布平台上传、封面生成或社媒发布能力。

完成本任务后必须 git commit。

建议 commit message：

```text
docs: 补充第五批导出交付物执行方案
```

提交前验证：

```bash
pytest
```

## 第五批完成定义

第五批完成时，应满足：

- reviewed 成功时，CLI 默认导出全部发布就绪短视频。
- `--max-clips` 只作为用户显式上限，不再默认截断发布就绪结果。
- reviewed 失败、关闭或不可用时，CLI 默认不导出短视频，只输出未评审方案和 warning。
- 显式 `--allow-unreviewed-export` 时，仍可使用未评审兼容导出路径。
- `plan.json` 能机器可读地表达候选评审、导出选择、未导出原因和计划导出清单。
- `metadata.json` 能作为非 dry-run 完整交付物索引。
- `拆条报告.md` 能人工可读地解释导出项、未导出项、人工复核项、边界补救建议和同主题系列。
- 非 dry-run `live` 有最小 reviewed 导出端到端测试闭环，能稳定生成 `transcript.srt`、`plan.json`、`metadata.json`、`clips/*.mp4`、`subtitles/*.srt` 和 `拆条报告.md`。
- 端到端测试不依赖真实外部服务、网络、真实评审模型或真实视频处理。
- 每个小任务都有独立 git commit。
- 全量测试通过。

## 第五批完成后的运行说明

### 预期默认行为

第五批完成后，默认非 dry-run 会在 reviewed 成功后导出全部发布就绪短视频：

```bash
export STEPFUN_API_KEY=sk-...
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live
```

默认导出条件：

- 候选存在结构化 `review`。
- `export_decision=publish_ready`。
- `publish_ready_score >= topic_review_publish_ready_threshold`。
- `topic_complete=True`。
- `needs_human_review=False`。
- 候选不是重复片段。

显式限制导出数量：

```bash
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --max-clips 3
```

未评审兼容导出：

```bash
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --allow-unreviewed-export
```

预期产物：

- `out/live/transcript.srt`
- `out/live/plan.json`
- `out/live/metadata.json`
- `out/live/clips/*.mp4`
- `out/live/subtitles/*.srt`
- `out/live/拆条报告.md`

dry-run 仍只输出方案，不裁剪视频：

```bash
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --dry-run
```

dry-run 预期产物：

- `out/live/transcript.srt`
- `out/live/plan.json`
- `out/live/拆条报告.md`

dry-run 不应生成：

- `out/live/metadata.json`
- `out/live/clips/*.mp4`
- `out/live/subtitles/*.srt`

### 第六批边界

第五批不创建 skill 调度器。以下能力必须留到第六批或后续阶段：

- 环境预检 skill。
- 课程上下文收集与 JSON 生成。
- 面向用户的 CLI 调用编排。
- 读取 `plan.json`、`metadata.json` 和报告后的结果解释。
- 根据失败原因提示修复或二次运行。
