# 真实端到端测试主题评审改进计划

## 背景

2026-06-24 使用 `config.local.json` 和 `妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）.mp4` 执行真实端到端测试，输出目录为 `out/e2e-real-20260624-222301/`。

本次流程中，视频信息读取、静音检测、转写文本加载和候选片段生成均成功：

- 视频时长：5395.5 秒。
- 静音区间：161 个。
- 转写文本：从 `work/妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）/transcript.json` 加载 801 个片段。
- 候选片段：生成 68 个。

失败点集中在主题评审：

```text
主题评审失败：Topic review request failed: The read operation timed out
```

由于当前策略要求发布就绪短视频必须有可用主题评审结果，流程最终写出未评审方案，未导出短视频、字幕和 `metadata.json`。校验器失败信息为：

```text
E2E FAIL: plan.json status 应为 reviewed，实际为 'unreviewed'（评审未成功则不会真实导出）
```

## 目标

- 提高真实 StepFun 主题评审在长视频、多候选场景下的成功率。
- 保持发布就绪交付物的严格语义：主题评审失败时仍不默认导出短视频。
- 让失败可诊断，能定位到具体相邻候选批次、候选范围和重试次数。
- 让真实端到端测试可以稳定复跑，避免单次网络波动导致整场 89.9 分钟视频反复失败。

## 任务 1：调整主题评审稳定性默认配置

**建议 commit**：`配置主题评审稳定性参数`

### 变更内容

- 在全局配置中新增主题评审重试参数：
  - `topic_review_retry_attempts`：默认 `3`。
  - `topic_review_retry_backoff_seconds`：默认 `2.0`。
- 将 `topic_review_timeout` 的默认值从 `60` 提高到 `180` 秒。
- 保持 `topic_review_batch_size` 默认值为 `3`，避免真实运行时因为单候选批次导致请求次数过多。
- 不修改 `config.local.json` 中的密钥或私有配置；如需本地验证，可在本地配置中临时覆盖：
  - `topic_review_timeout: 180`
  - `topic_review_batch_size: 3`
  - `topic_review_reasoning_effort: ""`

### 验收标准

- 配置文件校验允许新增的两个重试参数。
- 默认配置单元测试同步覆盖新增参数。
- 不改变语音识别缓存签名，避免主题评审参数变化导致转写缓存失效。

## 任务 2：为 StepFun 主题评审请求增加可配置重试

**建议 commit**：`为主题评审请求增加重试机制`

### 变更内容

- 在 `StepFunChatReviewer` 中读取任务 1 新增的重试参数。
- 对以下临时性失败执行重试：
  - 读超时、连接超时等 `TimeoutError` / `OSError` 类错误。
  - `urllib.error.URLError`。
  - HTTP `429`。
  - HTTP `500`、`502`、`503`、`504`。
- 对以下确定性失败不重试：
  - 非 HTTPS base URL。
  - API Key 缺失。
  - Chat Completions JSON 非法。
  - 模型返回内容不是合法 JSON。
  - 主题评审 schema 不满足要求。
  - 返回未知 `candidate_id`。
- 重试使用线性或指数退避，第一版建议简单可解释：
  - 第 1 次失败后等待 `backoff_seconds`。
  - 第 2 次失败后等待 `backoff_seconds * 2`。
  - 第 3 次失败后返回最终失败。

### 验收标准

- 单元测试覆盖第一次超时、第二次成功。
- 单元测试覆盖达到最大重试次数后返回失败。
- 单元测试覆盖 HTTP 500 会重试，HTTP 400 不重试。
- 不通过修改测试断言、跳过测试或 mock 绕过失败。

## 任务 3：增加相邻候选批次级诊断信息

**建议 commit**：`增强主题评审批次诊断信息`

### 变更内容

- 主题评审失败信息中加入：
  - `batch_index`。
  - 批次候选范围，例如 `candidate_12-candidate_14`。
  - 当前请求次数和最大请求次数，例如 `attempt 2/3`。
  - 失败类型，例如 `timeout`、`http_503`、`invalid_schema`。
- `plan.json` 的 `warnings` 保留这些诊断信息。
- `拆条报告.md` 的 Warnings 区域直接展示同样诊断，便于真实端到端测试后查看。

### 验收标准

- 当某个相邻候选批次失败时，终端输出、`plan.json` 和报告均能定位失败批次。
- 诊断信息不包含 API Key、Authorization header 或完整请求体。
- 单元测试覆盖失败信息包含批次编号和候选范围。

## 任务 4：缓存主题评审成功结果

**建议 commit**：`缓存主题评审成功结果以支持稳定复跑`

### 变更内容

- 在处理缓存中新增主题评审缓存文件，建议路径：
  - `work/<video_name>/topic_review_cache/`
- 缓存键至少包含：
  - 候选片段的 `candidate_id`、起止时间、文本。
  - 相邻候选摘要。
  - 课程上下文摘要。
  - 主题评审 provider、model、base_url。
  - 影响输出的主题评审提示词版本或 schema 版本。
- 命中缓存时跳过对应相邻候选批次的真实 API 请求。
- 未命中或签名不一致时仍走真实 API。
- 只缓存成功解析且 schema 合法的评审结果；失败结果不缓存。

### 验收标准

- 第一次真实运行成功评审后，第二次运行相同输入可复用主题评审缓存。
- 修改模型、候选文本或课程上下文后缓存失效。
- 缓存不影响 `allow_unreviewed_export=false` 的严格导出策略。
- 单元测试覆盖命中缓存、签名变化失效、失败结果不缓存。

## 任务 5：支持部分成功的内部保留，但保持整体严格失败

**建议 commit**：`保留部分主题评审结果并维持严格导出策略`

### 变更内容

- `review_batches()` 内部保留已成功批次的评审结果。
- 只要仍有任一相邻候选批次失败，整体 `success` 仍为 `False`。
- `plan.json` 增加可诊断字段，建议包括：
  - `reviewed_candidate_count`
  - `failed_review_batch_count`
  - `failed_review_batches`
- CLI 仍按 ADR 0007 的约束处理：
  - 主题评审失败时输出未评审方案。
  - 默认不导出短视频。
  - 不生成 `metadata.json`。

### 验收标准

- 部分批次成功、部分批次失败时，方案仍为 `unreviewed`。
- 已成功的评审结果可在诊断字段中体现，但不会被误认为完整发布就绪结果。
- 标准端到端校验器仍要求 `status == reviewed` 才通过。

## 任务 6：改进真实端到端脚本的可控性

**建议 commit**：`增强真实端到端脚本的评审参数覆盖能力`

### 变更内容

- 在 `tests/e2e/run_live_e2e.sh` 中增加可选环境变量，用于真实测试时覆盖运行行为：
  - `E2E_MAX_CLIPS`：传递给 `--max-clips`。
  - `E2E_ALLOW_UNREVIEWED_EXPORT`：仅用于人工兼容验证，标准校验仍默认不启用。
  - `E2E_EXTRA_ARGS`：谨慎使用，仅追加 CLI 参数，不用于默认流程。
- 保持默认运行仍是完整发布就绪校验：
  - 不加 `--dry-run`。
  - 不加 `--allow-unreviewed-export`。
  - 仍调用 `verify_live_deliverables.py`。

### 验收标准

- 不设置新增环境变量时，脚本行为与当前一致。
- 设置 `E2E_MAX_CLIPS=1` 时，CLI 能限制真实导出数量，便于快速验证导出链路。
- 标准真实端到端测试仍以 `E2E PASS` 为唯一通过标准。

## 任务 7：补充回归测试与真实复跑记录

**建议 commit**：`补充主题评审稳定性回归测试和复跑记录`

### 变更内容

- 增加或更新单元测试覆盖：
  - 主题评审重试参数默认值。
  - 超时重试后成功。
  - 多次超时后失败。
  - HTTP 可重试与不可重试分支。
  - 批次诊断字段。
  - 主题评审缓存命中与失效。
- 更新真实端到端文档，记录推荐复跑命令：

```bash
E2E_VIDEO="妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）.mp4" \
E2E_CONFIG="config.local.json" \
E2E_OUT="out/e2e-real-$(date +%Y%m%d-%H%M%S)" \
E2E_WORK="work" \
bash tests/e2e/run_live_e2e.sh
```

- 在复跑记录中明确区分：
  - 转写文本是否来自缓存。
  - 主题评审是否真实请求或命中主题评审缓存。
  - 是否生成 `metadata.json`、`clips/` 和 `subtitles/`。

### 验收标准

- `python -m pytest tests/test_review.py tests/test_config.py tests/test_live_export_e2e.py` 通过。
- 使用真实 API 复跑后，若 StepFun 评审成功，`verify_live_deliverables.py` 输出 `E2E PASS`。
- 若 StepFun 评审仍失败，失败信息能定位具体相邻候选批次和重试过程。

## 推荐实施顺序

1. 先做任务 1、2、3，解决本次真实端到端失败的直接原因：超时缺少重试、失败缺少定位。
2. 再做任务 4，降低真实 API 复跑成本，避免长视频测试反复消耗评审额度。
3. 接着做任务 5，提升失败后的诊断价值，但不放宽发布就绪导出标准。
4. 最后做任务 6、7，完善脚本可控性和回归测试闭环。

## 风险与约束

- 不通过修改测试断言、Mock、Fixture、跳过测试或删除测试来制造通过结果。
- 不把 `--allow-unreviewed-export` 作为标准端到端测试通过路径。
- 不在日志、报告或缓存中写入 API Key。
- 主题评审缓存必须以结果影响因素作为签名输入，避免复用过期评审。
- 若 StepFun 模型持续超时，应优先降低 `reasoning_effort` 或调整模型，而不是放宽校验器对 `reviewed` 的要求。

## 复跑记录模板

推荐复跑命令：

```bash
E2E_VIDEO="妇美·愉悦技术规范宣讲——张铃院长（2026年6月13日）.mp4" \
E2E_CONFIG="config.local.json" \
E2E_OUT="out/e2e-real-$(date +%Y%m%d-%H%M%S)" \
E2E_WORK="work" \
bash tests/e2e/run_live_e2e.sh
```

每次真实 API 复跑后记录：

- 转写文本来源：终端 `Loaded ... transcript chunks from ...` 显示 `cache` 或真实 ASR provider。
- 主题评审来源：记录 Step 6 是真实请求成功、命中 `work/<video_name>/topic_review_cache/`，还是失败并写入批次诊断 warning。
- 标准交付物：记录是否生成 `metadata.json`、`clips/`、`subtitles/`，以及校验器是否输出 `E2E PASS`。

本次代码实现已补充以下回归覆盖：

- 默认主题评审超时与重试配置。
- 超时重试成功、重试耗尽失败、HTTP 500 重试与 HTTP 400 不重试。
- 批次诊断字段，包括 `batch_index`、`candidate_range`、`attempt` 和 `failure_type`。
- 主题评审成功缓存命中、签名变化失效、失败结果不缓存。
- 部分批次成功后整体仍按 `unreviewed` 严格失败，并在 `plan.json` 写入诊断计数。
