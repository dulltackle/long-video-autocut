# 第四批：主题评审与最小 reviewed dry-run 闭环

第四批覆盖“主题评审”的最小可交付范围。目标是在第三批已经具备稳定 ASR 分片转写和 `live --dry-run` 基础方案输出的前提下，接入结构化主题评审能力，并通过不依赖真实外部服务的端到端测试证明 CLI 可以生成带评审结果的 `plan.json` 和 `拆条报告.md`。

范围边界：

- 新增主题评审数据结构和评审 provider 抽象。
- 默认使用 StepFun Chat，保留 OpenAI-compatible 配置能力。
- 按相邻候选批次提交候选片段、转写上下文和课程上下文。
- 输出结构化评审结果：主题名、主题完整度、学习价值、传播价值、发布就绪评分、导出建议、标题、摘要、关键词、人工复核、淘汰原因、边界补救建议。
- 主题评审失败时，继续输出未评审方案、诊断 warning 和报告，但默认不导出发布就绪短视频。
- 建立最小 `live --dry-run` reviewed 端到端测试闭环。
- 不改变候选生成算法。
- 不实现“默认导出全部发布就绪短视频”的完整策略重写；该能力留到第五批。
- 不创建 skill 调度器。

## 提交硬性要求

第四批必须按小任务拆分提交。每完成一个任务并通过该任务列出的验证命令后，必须立即执行一次 `git commit`，不得等整批完成后一次性提交。

每次提交前必须确认：

- 只包含当前小任务范围内的改动。
- 不修改测试代码来规避失败。
- 失败测试只能通过修复生产代码解决。
- `git status --short` 中没有意外文件混入提交范围。

## 任务 1：定义主题评审数据结构与 plan 契约

建议新增或扩展文件：

- `video_auto_editor/models.py`
- `video_auto_editor/plan.py`
- `tests/test_plan.py`
- 必要时新增 `tests/test_review.py`

执行项：

- 新增主题评审结果结构，至少覆盖：
  - `topic_name`
  - `topic_complete`
  - `learning_value`
  - `share_value`
  - `publish_ready_score`
  - `export_decision`
  - `title`
  - `summary`
  - `keywords`
  - `needs_human_review`
  - `reject_reason`
  - `boundary_fix_suggestion`
- 将评审结果关联到 `ClipCandidate`，避免用松散 dict 在流程中传递关键字段。
- 扩展 `plan.json`：
  - 未评审时保持 `status=unreviewed`。
  - 评审成功时输出 `status=reviewed`。
  - 每个候选项写入机器可读的 `review` 字段。
  - 写入评审 warning 和 provider 元信息。
- 保持旧的未评审 dry-run plan 测试继续通过。

验收标准：

- 未评审候选仍能输出兼容的 `plan.json`。
- 带评审结果的候选能输出完整 `review` 字段。
- `plan.json` 顶层状态能区分 `unreviewed` 和 `reviewed`。
- 缺失评审结果时不会伪造发布就绪判断。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 定义直播主题评审结果契约
```

提交前验证：

```bash
pytest tests/test_plan.py
pytest tests/test_review.py
pytest
```

## 任务 2：增加主题评审配置

改动文件：

- `video_auto_editor/config.py`
- 必要时新增 `tests/test_review.py`

执行项：

- 增加主题评审配置字段：
  - `topic_review_enabled`
  - `topic_review_provider`
  - `topic_review_model`
  - `topic_review_timeout`
  - `topic_review_batch_size`
  - `topic_review_temperature`
  - `topic_review_api_key_env`
  - `topic_review_base_url_env`
  - `topic_review_base_url`
  - `topic_review_publish_ready_threshold`
- 默认 provider 使用 StepFun Chat。
- API Key 和 base URL 通过环境变量读取，同时允许配置默认 base URL。
- 保留 OpenAI-compatible 的命名和请求形态预留，避免把评审逻辑写死到单一供应商。
- 明确评审配置不影响 ASR 缓存签名。

验收标准：

- 默认配置表达“启用主题评审、默认 StepFun Chat”。
- 缺少 API Key 时不会发起真实请求。
- 显式关闭 `topic_review_enabled` 时，live 保持第三批的未评审 dry-run 行为。
- ASR 相关测试不因评审配置变化失败。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增加主题评审配置
```

提交前验证：

```bash
pytest tests/test_review.py
pytest tests/test_transcript.py
pytest
```

## 任务 3：实现相邻候选批次构造

建议新增文件：

- `video_auto_editor/review.py`
- `tests/test_review.py`

执行项：

- 按候选片段在原视频中的时间顺序构造评审批次。
- 每个批次包含当前候选、相邻候选摘要、候选时间边界、候选文本、课程上下文摘要。
- 批次大小由 `topic_review_batch_size` 控制。
- 每个候选在请求中必须有稳定 ID，便于模型响应映射回 `ClipCandidate.index`。
- 控制 prompt/payload 结构稳定，便于测试和后续缓存或重试。

验收标准：

- 候选按时间顺序分批。
- 批次包含前后相邻候选上下文。
- 课程上下文存在时会进入评审输入，不存在时 payload 仍合法。
- 空候选列表返回空批次，不调用 provider。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 构造相邻候选主题评审批次
```

提交前验证：

```bash
pytest tests/test_review.py
pytest
```

## 任务 4：实现主题评审 provider 抽象与 StepFun Chat provider

建议新增或扩展文件：

- `video_auto_editor/review.py`
- `tests/test_review.py`

执行项：

- 定义主题评审 provider 最小契约：
  - `is_available()`
  - `review_batches(batches)`
- 新增 `StepFunChatReviewer`。
- HTTP 请求层必须可注入，测试中不得访问真实网络。
- 将 Chat Completions 响应解析为结构化评审结果。
- 对以下失败路径返回清晰错误：
  - API Key 缺失。
  - HTTP 请求失败。
  - 响应不是合法 JSON。
  - 模型输出不是合法 JSON。
  - 响应缺少候选 ID 或必需评审字段。
  - 模型返回了请求中不存在的候选 ID。
- 未知 provider 必须返回明确错误，不允许静默回退。

验收标准：

- 成功响应可以映射为候选评审结果。
- 缺少 API Key 时 `is_available()` 为 false。
- provider 失败不抛出未捕获异常，而是返回可诊断错误。
- 测试不依赖真实 StepFun 或 OpenAI 服务。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增加 StepFun Chat 主题评审 provider
```

提交前验证：

```bash
pytest tests/test_review.py
pytest
```

## 任务 5：接入 live 流程并保留失败降级语义

改动文件：

- `video_auto_editor/cli.py`
- `video_auto_editor/review.py`
- `video_auto_editor/plan.py`
- `video_auto_editor/report.py`
- `tests/test_media_report_cli.py`
- 必要时新增或扩展 `tests/test_review.py`

执行项：

- 在候选生成、去重和基础选择后调用主题评审。
- 评审成功时：
  - 将评审结果写回候选。
  - 基于评审结果更新候选标题、摘要、关键词等展示字段。
  - `plan.json` 输出 `status=reviewed`。
- 评审关闭或不可用时：
  - 保持 `status=unreviewed`。
  - 写入 warning。
  - 默认不把未评审结果标记为发布就绪。
- 评审失败时：
  - 不中止 ASR 和候选方案输出。
  - 输出未评审方案和清晰 warning。
  - 不生成误导性的发布就绪结论。
- dry-run 继续跳过真实导出。

验收标准：

- 评审成功时 `plan.json` 和报告都能看到结构化评审结果。
- 评审失败时 `plan.json.status=unreviewed`，报告包含失败 warning。
- 缺少评审 API Key 不影响 `transcript.srt`、基础候选和未评审报告生成。
- ASR 失败仍然中止，不进入主题评审。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: live 流程接入主题评审
```

提交前验证：

```bash
pytest tests/test_media_report_cli.py
pytest tests/test_review.py
pytest
```

## 任务 6：补充 reviewed live dry-run 最小端到端测试闭环

建议新增或扩展文件：

- `tests/test_live_dry_run_e2e.py`
- 必要时新增 `tests/test_live_review_e2e.py`

测试目标：

- 从 `cli.main(["live", video_path, "--dry-run", ...])` 入口执行。
- 不依赖真实 StepAudio、Whisper、ffmpeg、ffprobe、真实评审模型或真实视频文件。
- 使用 fake StepAudio HTTP 响应覆盖至少两个音频分片。
- 使用 fake 主题评审响应覆盖至少两个候选，且包含发布就绪和需人工复核两类结果。
- mock 视频时长、静音检测和外部命令，尽量少 mock CLI 内部编排本身。
- 确保执行到以下真实逻辑：
  - 音频提取与分片计划。
  - 分片请求与时间戳合并。
  - `transcript.srt` 写出。
  - 候选生成、去重、基础选择。
  - 相邻候选批次构造。
  - 主题评审结果解析与写回。
  - `plan.json` 写出。
  - `拆条报告.md` 写出。
  - dry-run 跳过导出。

验收标准：

- 测试断言 `transcript.srt` 存在且包含跨分片合并后的全局时间戳。
- 测试断言 `plan.json` 存在，且 `status=reviewed`。
- 测试断言 `plan.json` 的候选包含结构化 `review` 字段。
- 测试断言报告包含主题评审结果、发布就绪判断、人工复核项或淘汰原因。
- 测试断言不生成：
  - `metadata.json`
  - `clips/*.mp4`
  - 单条短视频字幕文件
- 测试能够在无网络、无真实 ASR 服务、无真实评审模型环境中稳定通过。

完成本任务后必须 git commit。

建议 commit message：

```text
test: 补充主题评审 live dry-run 闭环
```

提交前验证：

```bash
pytest tests/test_live_dry_run_e2e.py
pytest tests/test_review.py
pytest
```

## 任务 7：补齐第四批文档与运行说明

改动文件：

- `docs/implementation-plan.md`
- `docs/implementation-batch-4-topic-review.md`
- 必要时补充 `CONTEXT.md`

执行项：

- 在整体计划中登记第四批执行文档。
- 记录第四批完成后的主题评审流程。
- 记录默认 StepFun Chat 配置和 OpenAI-compatible 配置方式。
- 记录主题评审失败时的降级行为。
- 记录本地 dry-run 运行方式和产物检查方式。
- 说明第五批才处理默认导出全部发布就绪短视频、增强 `metadata.json` 和标准交付物。

验收标准：

- 文档能指导下一位开发者从第三批继续执行第四批。
- 文档明确每个任务完成后都必须提交一次。
- 文档没有承诺第四批不会实现的导出选择重写、标准交付物增强或 skill 调度能力。

完成本任务后必须 git commit。

建议 commit message：

```text
docs: 补充第四批主题评审执行方案
```

提交前验证：

```bash
pytest
```

## 第四批完成定义

第四批完成时，应满足：

- live 流程可以在候选生成后进行结构化主题评审。
- 主题评审按相邻候选批次提交，评审输入包含候选文本、边界、相邻上下文和课程上下文。
- 默认评审 provider 为 StepFun Chat，并保留 OpenAI-compatible 配置能力。
- 评审成功时，`plan.json.status` 为 `reviewed`，候选项包含机器可读评审字段。
- 评审失败、关闭或不可用时，CLI 仍输出未评审方案和诊断 warning，但不伪造发布就绪结论。
- ASR 失败仍然直接中止，不进入主题评审。
- `live --dry-run` 有最小 reviewed 端到端测试闭环，能稳定生成 `transcript.srt`、`plan.json` 和 `拆条报告.md`。
- dry-run 端到端测试不依赖真实外部服务、网络或真实视频处理。
- 每个小任务都有独立 git commit。
- 全量测试通过。

## 第四批完成后的运行说明

### 默认主题评审

第四批完成后，默认 `live` dry-run 会在候选生成后尝试调用 StepFun Chat 进行主题评审：

```bash
export STEPFUN_API_KEY=sk-...
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --dry-run
```

处理流程：

1. 使用第三批能力完成 StepAudio 分片转写。
2. 写出 `transcript.srt`。
3. 生成、去重并基础选择候选片段。
4. 按时间顺序构造相邻候选评审批次。
5. 调用主题评审 provider。
6. 将结构化评审结果写回候选。
7. 写出 `plan.json` 和 `拆条报告.md`。
8. dry-run 跳过真实视频导出。

预期产物：

- `out/live/transcript.srt`
- `out/live/plan.json`
- `out/live/拆条报告.md`

评审成功时：

- `plan.json` 顶层 `status` 为 `reviewed`。
- 候选项包含 `review` 字段。
- 报告包含主题评审结果、发布就绪判断、人工复核项或淘汰原因。

评审关闭、失败或不可用时：

- `plan.json` 顶层 `status` 为 `unreviewed`。
- `warnings` 中包含评审不可用或失败原因。
- 报告继续输出基础候选方案，但不声明短视频发布就绪。

dry-run 不应生成：

- `out/live/metadata.json`
- `out/live/clips/*.mp4`
- 单条短视频字幕文件

### 第五批边界

第四批不处理导出选择和标准交付物增强。以下能力必须留到第五批或后续阶段：

- 默认导出所有发布就绪短视频。
- `--max-clips` 仅作为可选上限。
- 基于边界补救建议调整导出边界。
- 同主题系列导出。
- 增强 `metadata.json`。
- 增强 `拆条报告.md` 的标准交付物清单。
- 完整发布就绪交付包。

### 第四批实际提交点

第四批应按以下小任务分别提交，每个小任务完成并验证后立即提交：

- `feat: 定义直播主题评审结果契约`
- `feat: 增加主题评审配置`
- `feat: 构造相邻候选主题评审批次`
- `feat: 增加 StepFun Chat 主题评审 provider`
- `feat: live 流程接入主题评审`
- `test: 补充主题评审 live dry-run 闭环`
- `docs: 补充第四批主题评审执行方案`
