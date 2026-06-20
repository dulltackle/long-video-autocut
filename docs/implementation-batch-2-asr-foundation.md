# 第二批：ASR 抽象与最小 live dry-run 闭环

第二批覆盖“语音识别抽象”的最小可交付范围。目标是把 live 流程从 Whisper 硬编码中解耦出来，形成可替换 ASR provider，并通过不依赖真实外部服务的 `live --dry-run` 测试证明 CLI 可以端到端生成 `transcript.srt`、`plan.json` 和 `拆条报告.md`。

范围边界：

- 做统一 ASR provider 抽象。
- 新增 `StepAudioTranscriber` 第一版，使 stepaudio-2.5-asr 成为默认 provider。
- 保留 `WhisperTranscriber` 作为可选 provider。
- 建立最小 live dry-run 端到端测试闭环。
- 不做长音频分片、分片缓存、断点重试和时间戳偏移合并；这些属于第三批。
- 不接入主题评审模型、不调整候选算法、不改变导出选择策略。

## 任务 1：定义统一 ASR provider 契约

改动文件：

- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 明确整视频转写 provider 的最小方法：
  - `is_available()`
  - `transcribe_video(video_path, work_dir)`
- 复用现有 `VideoTranscriptionResult` 和 `TranscriptChunk` 作为统一返回结构。
- 保留现有 `TranscriptionResult`、`transcribe_segment()` 和候选片段转写能力，避免影响 `single` 流程。
- 增加 provider factory 入口，例如 `create_transcriber(config=None)`。
- 暂时保留 `create_whisper_transcriber()` 作为兼容入口，内部可委托新 factory 或继续只创建 Whisper。
- 未知 provider 必须返回明确错误，不允许静默回退。

验收标准：

- `create_transcriber({"asr_provider": "whisper", ...})` 能创建 `WhisperTranscriber`。
- 未知 `asr_provider` 会失败并给出清晰错误。
- 现有 Whisper 单元测试继续通过。
- `single` 场景的候选片段转写行为不被破坏。

## 建议提交点 1

提交内容：仅包含 provider 契约、factory 和对应单元测试，不新增 StepAudio 网络调用，不改 live 行为。

建议 commit message：

```text
refactor: 抽象 ASR provider 创建入口
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 2：演进 ASR 配置

改动文件：

- `video_auto_editor/config.py`
- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`
- 必要时补充 `tests/test_media_report_cli.py`

执行项：

- 增加通用 ASR 配置字段：
  - `asr_provider`：默认 `stepaudio`
  - `asr_model`：默认 `stepaudio-2.5-asr`
  - `asr_timeout`
  - `asr_language`
- 保留现有 Whisper 专用字段，保证 `asr_provider=whisper` 时继续可用。
- 明确 StepAudio API Key 和 base URL 的读取来源，建议通过环境变量读取：
  - `STEPFUN_API_KEY`
  - `STEPFUN_BASE_URL`
- provider 配置必须进入缓存签名设计预留点，但本任务不要求重写缓存格式。

验收标准：

- 默认配置表达“默认使用 StepAudio”。
- 显式配置 `asr_provider=whisper` 时仍走现有 Whisper 逻辑。
- 缺失 StepAudio API Key 时不会触发真实请求，错误可诊断。
- 现有测试不因配置字段变化失败。

## 建议提交点 2

提交内容：仅包含配置字段演进、factory 配置读取和测试，不实现真实 StepAudio 调用。

建议 commit message：

```text
feat: 增加 ASR provider 通用配置
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 3：新增 StepAudioTranscriber 第一版

建议新增或扩展文件：

- `video_auto_editor/transcript.py`
- 必要时新增 `video_auto_editor/asr.py`
- `tests/test_transcript.py`

执行项：

- 新增 `StepAudioTranscriber`。
- `is_available()` 只检查必需配置是否存在，不发起昂贵请求。
- 第一版只实现整视频识别入口 `transcribe_video(video_path, work_dir)`。
- 将 StepAudio 响应解析为 `TranscriptChunk` 列表。
- 对以下失败路径返回 `VideoTranscriptionResult(success=False, chunks=[], error=...)`：
  - API Key 缺失。
  - HTTP 请求失败。
  - 响应不是合法 JSON。
  - 响应缺少可用时间戳片段。
- 测试中必须 mock HTTP 层或注入 fake client，不允许真实访问网络。

验收标准：

- StepAudio 成功响应可转换为 `TranscriptChunk`。
- StepAudio 失败响应会返回清晰错误。
- 缺少 API Key 时 `is_available()` 为 false，且 live 会中止。
- 不需要真实 StepAudio 服务即可跑通测试。

## 建议提交点 3

提交内容：`StepAudioTranscriber` 第一版、响应解析和失败路径测试，不接入 live 端到端测试。

建议 commit message：

```text
feat: 增加 StepAudio ASR provider
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 4：让 live 流程使用统一 ASR provider

改动文件：

- `video_auto_editor/transcript.py`
- `video_auto_editor/cli.py`
- `tests/test_media_report_cli.py`

执行项：

- `transcribe_video(video_path, work_dir, transcriber=None, config=None)` 默认通过 `create_transcriber(config)` 创建 provider。
- live 流程继续只依赖 `transcribe_video()`，不直接认识具体 provider。
- ASR 不可用或识别失败时，live 直接中止，不生成误导性的候选方案。
- 错误信息中使用“ASR”或 provider 名称，避免继续输出 Whisper 专属提示。
- 保持已存在转写缓存命中时不调用 provider 的行为。

验收标准：

- 缓存命中时不检查 provider 可用性。
- `asr_provider=whisper` 时现有 live 测试继续通过。
- provider 不可用时 live 返回 `None`，不写出 `plan.json`。
- dry-run 行为不因 provider 抽象改变。

## 建议提交点 4

提交内容：live 接入统一 ASR provider，以及 provider 不可用/失败的 CLI 测试。

建议 commit message：

```text
refactor: live 流程接入统一 ASR provider
```

提交前验证：

```bash
pytest tests/test_media_report_cli.py
pytest tests/test_transcript.py
pytest
```

## 任务 5：补充最小 live dry-run 端到端测试闭环

建议新增或扩展文件：

- `tests/test_live_dry_run_e2e.py`
- 必要时整理 `tests/test_media_report_cli.py`

测试目标：

- 从 `cli.main(["live", video_path, "--dry-run", ...])` 入口执行。
- 不依赖真实 StepAudio、Whisper、ffmpeg、ffprobe 或真实视频文件。
- 使用 fake ASR provider 返回固定 `TranscriptChunk`。
- mock 视频时长、静音检测、候选生成链路中必须隔离外部命令的部分。
- 尽量少 mock CLI 内部编排本身，确保执行到以下真实逻辑：
  - `transcript.srt` 写出。
  - 候选生成、去重、基础选择。
  - `plan.json` 写出。
  - `拆条报告.md` 写出。
  - dry-run 跳过导出。

验收标准：

- 测试断言 `transcript.srt` 存在且内容正确。
- 测试断言 `plan.json` 存在，且 `status=unreviewed`、`candidates` 和 `selected` 非空。
- 测试断言 `拆条报告.md` 存在，并包含 dry-run 未评审提示。
- 测试断言不生成：
  - `metadata.json`
  - `clips/*.mp4`
  - 单条短视频字幕文件
- 测试能够在无网络、无真实 ASR 服务环境中稳定通过。

## 建议提交点 5

提交内容：只包含最小端到端测试、必要 fake provider 和测试夹具整理，不混入新功能。

建议 commit message：

```text
test: 补充 live dry-run ASR 闭环测试
```

提交前验证：

```bash
pytest tests/test_live_dry_run_e2e.py
pytest tests/test_media_report_cli.py
pytest
```

## 任务 6：补齐第二批文档与迁移说明

改动文件：

- `docs/implementation-plan.md`
- `docs/implementation-batch-2-asr-foundation.md`
- 必要时补充 `CONTEXT.md`

执行项：

- 在整体计划中登记第二批执行文档。
- 记录第二批完成后支持的 ASR provider：
  - 默认：`stepaudio`
  - 可选：`whisper`
- 说明第三批才处理长音频分片、分片缓存、重试和时间戳偏移合并。
- 记录本地运行方式示例：
  - 默认 StepAudio。
  - 显式 Whisper。
  - dry-run 产物检查。

验收标准：

- 文档能指导下一位开发者从第一批继续执行第二批。
- 文档明确 commit 时机，避免把 provider、live 接入、端到端测试混成一个大提交。
- 文档没有承诺第二批不会实现的长视频分片能力。

## 建议提交点 6

提交内容：第二批文档链接、迁移说明和必要上下文更新。

建议 commit message：

```text
docs: 补充第二批 ASR 抽象执行方案
```

提交前验证：

```bash
pytest
```

## 第二批完成定义

第二批完成时，应满足：

- live 整视频转写不再硬编码 Whisper provider。
- 默认 ASR provider 是 `stepaudio`。
- `WhisperTranscriber` 仍可作为可选 provider 使用。
- StepAudio 第一版 provider 有可测试的成功和失败路径。
- ASR 不可用或失败时，live 直接中止，不输出误导性的发布方案。
- 缓存命中时仍可跳过 provider 调用。
- `live --dry-run` 有最小端到端测试闭环，能稳定生成 `transcript.srt`、`plan.json` 和 `拆条报告.md`。
- dry-run 端到端测试不依赖真实外部服务、网络或真实视频处理。
- 全量测试通过。
