# 第三批：StepAudio 分片识别与缓存闭环

第三批覆盖“StepAudio 分片识别”的最小可交付范围。目标是让几小时直播课可以通过连续音频分片稳定完成 ASR，并通过不依赖真实外部服务的 `live --dry-run` 测试证明 CLI 可以端到端生成 `transcript.srt`、`plan.json` 和 `拆条报告.md`。

范围边界：

- 使用 FFmpeg 从原视频提取统一格式音频。
- 将长音频切成连续识别分片。
- 逐片调用 stepaudio-2.5-asr。
- 将分片内时间戳按分片偏移合并为整场转写时间轴。
- 默认开启分片级缓存，缓存键包含影响结果的输入摘要。
- 为分片请求提供有限重试。
- 建立最小 `live --dry-run` 端到端测试闭环。
- 不接入主题评审模型、不改变候选算法、不改变导出选择策略、不创建 skill 调度器。

## 提交硬性要求

第三批必须按小任务拆分提交。每完成一个任务并通过该任务列出的验证命令后，必须立即执行一次 `git commit`，不得等整批完成后一次性提交。

每次提交前必须确认：

- 只包含当前小任务范围内的改动。
- 不修改测试代码来规避失败。
- 失败测试只能通过修复生产代码解决。
- `git status --short` 中没有意外文件混入提交范围。

## 任务 1：补充分片 ASR 配置

改动文件：

- `video_auto_editor/config.py`
- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 增加 StepAudio 分片相关配置字段：
  - `asr_shard_seconds`
  - `asr_audio_sample_rate`
  - `asr_audio_channels`
  - `asr_audio_format`
  - `asr_retry_attempts`
  - `asr_retry_backoff_seconds`
- 将这些字段纳入 ASR 缓存签名，确保影响识别结果的配置变化会使缓存失效。
- 保持默认 provider 仍为 `stepaudio`，Whisper provider 不受分片配置影响。

验收标准：

- 默认配置包含分片识别所需字段。
- StepAudio 缓存签名包含模型、语言和分片音频配置。
- Whisper 缓存签名保持只依赖 Whisper 相关配置。
- 现有 StepAudio 和 Whisper provider 创建测试继续通过。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增加 StepAudio 分片识别配置
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 2：实现音频提取与分片计划

建议新增或扩展文件：

- `video_auto_editor/transcript.py`
- 必要时新增 `video_auto_editor/asr.py`
- `tests/test_transcript.py`

执行项：

- 使用 FFmpeg 从源视频提取统一格式音频，输出到 live 工作目录。
- 根据视频总时长或音频时长生成连续分片计划。
- 分片计划至少包含：
  - 分片序号。
  - 全局起始时间。
  - 全局结束时间。
  - 分片音频路径。
  - 分片缓存路径。
- 使用 FFmpeg 按分片计划切出音频文件。
- 对 FFmpeg 提取失败、切片失败、未生成目标文件等情况返回清晰错误。
- 测试中必须 mock `subprocess.run`，不依赖真实 FFmpeg。

验收标准：

- 分片计划连续、无负时长、末片不超过总时长。
- 音频提取命令包含采样率、声道数和目标格式配置。
- 切片命令使用分片起止时间生成独立音频文件。
- FFmpeg 失败时 ASR 整体失败，live 后续不会生成误导性方案。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增加直播音频提取与分片计划
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 3：抽出 StepAudio 单分片请求

改动文件：

- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 将当前 `StepAudioTranscriber.transcribe_video()` 中的单次上传逻辑抽成可复用的分片请求方法。
- 单分片请求接收音频文件路径，返回该分片内的 `TranscriptChunk` 列表。
- 保留现有 StepAudio 响应解析兼容能力。
- 对以下失败路径返回清晰错误：
  - API Key 缺失。
  - 分片文件不存在。
  - 分片超过单次上传大小限制。
  - HTTP 请求失败。
  - 响应不是合法 JSON。
  - 响应缺少可用时间戳片段。
- 测试中必须 mock HTTP 层或注入 fake client，不允许真实访问网络。

验收标准：

- StepAudio 成功响应可转换为分片内 `TranscriptChunk`。
- 请求体使用分片音频文件，而不是原始视频文件。
- 失败路径不会写入成功缓存。
- 现有整视频 StepAudio 失败路径语义保持可诊断。

完成本任务后必须 git commit。

建议 commit message：

```text
refactor: 抽出 StepAudio 分片请求
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 4：实现分片时间戳偏移合并

改动文件：

- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 对每个分片返回的时间戳加上分片全局起始时间。
- 合并所有分片结果为整场 `TranscriptChunk` 列表。
- 丢弃空文本片段。
- 对明显非法的分片结果返回错误，例如结束时间早于开始时间。
- 保持合并结果按时间升序输出。

验收标准：

- 第二个分片的本地 `0-30s` 可合并为全局 `shard_start-shard_start+30s`。
- 跨分片结果顺序稳定。
- 空文本不会进入最终转写。
- 非法时间戳会使本次 ASR 失败，而不是静默生成错误时间轴。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 合并 StepAudio 分片时间戳
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 5：实现分片级缓存

改动文件：

- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 为每个分片保存独立缓存文件。
- 分片缓存键必须包含：
  - 源视频摘要。
  - provider。
  - ASR 模型。
  - 语言。
  - 分片起止时间。
  - 音频采样率。
  - 声道数。
  - 音频格式。
- 整体 `transcript.json` 缓存仍优先命中。
- 整体缓存未命中时，已命中的分片缓存不应重复请求 StepAudio。
- 分片缓存损坏或签名不匹配时，只重新识别对应分片。

验收标准：

- 首次运行会写出整体缓存和分片缓存。
- 第二次运行整体缓存命中时不检查 provider、不调用 StepAudio。
- 删除整体缓存但保留有效分片缓存时，不重复请求已缓存分片。
- 修改影响结果的配置后缓存失效。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 增加 StepAudio 分片缓存
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 6：增加分片请求重试

改动文件：

- `video_auto_editor/transcript.py`
- `tests/test_transcript.py`

执行项：

- 为单分片 StepAudio 请求增加有限重试。
- 只对可重试错误重试，例如网络错误、超时、HTTP 429、HTTP 5xx。
- 对 API Key 缺失、非法响应、无时间戳片段等不可重试错误直接失败。
- 重试次数和退避时间来自配置。
- 测试中禁止真实等待较长时间；需要可注入 sleep 函数或将退避时间设为 0。

验收标准：

- 可重试错误在限定次数内重试。
- 最终成功时写入对应分片缓存。
- 重试耗尽时返回包含分片序号和原因的错误。
- 不可重试错误不会重复请求。

完成本任务后必须 git commit。

建议 commit message：

```text
feat: 为 StepAudio 分片请求增加重试
```

提交前验证：

```bash
pytest tests/test_transcript.py
pytest
```

## 任务 7：接入 live dry-run 最小端到端闭环

建议新增或扩展文件：

- `tests/test_live_dry_run_e2e.py`
- 必要时扩展 `tests/test_media_report_cli.py`

测试目标：

- 从 `cli.main(["live", video_path, "--dry-run", ...])` 入口执行。
- 不依赖真实 StepAudio、Whisper、ffmpeg、ffprobe 或真实视频文件。
- 使用 fake StepAudio HTTP 响应覆盖至少两个音频分片。
- mock 视频时长、静音检测和外部命令，尽量少 mock CLI 内部编排本身。
- 确保执行到以下真实逻辑：
  - 音频提取与分片计划。
  - 分片请求与时间戳合并。
  - `transcript.srt` 写出。
  - 候选生成、去重、基础选择。
  - `plan.json` 写出。
  - `拆条报告.md` 写出。
  - dry-run 跳过导出。

验收标准：

- 测试断言 `transcript.srt` 存在且包含跨分片合并后的全局时间戳。
- 测试断言 `plan.json` 存在，且 `status=unreviewed`、`candidates` 和 `selected` 非空。
- 测试断言 `拆条报告.md` 存在，并包含 dry-run 未评审提示。
- 测试断言不生成：
  - `metadata.json`
  - `clips/*.mp4`
  - 单条短视频字幕文件
- 测试断言第二次运行可以命中缓存，不再请求 StepAudio。
- 测试能够在无网络、无真实 ASR 服务环境中稳定通过。

完成本任务后必须 git commit。

建议 commit message：

```text
test: 补充 StepAudio 分片 live dry-run 闭环
```

提交前验证：

```bash
pytest tests/test_live_dry_run_e2e.py
pytest tests/test_transcript.py
pytest
```

## 任务 8：补齐第三批文档与运行说明

改动文件：

- `docs/implementation-plan.md`
- `docs/implementation-batch-3-stepaudio-sharding.md`
- 必要时补充 `CONTEXT.md`

执行项：

- 在整体计划中登记第三批执行文档。
- 记录第三批完成后的 StepAudio 分片处理流程。
- 记录缓存行为：
  - 整体转写缓存优先。
  - 分片缓存用于整体缓存缺失时复用已识别分片。
  - 影响结果的配置变化会使缓存失效。
- 记录本地 dry-run 运行方式和产物检查方式。
- 说明第四批才处理主题评审模型。

验收标准：

- 文档能指导下一位开发者从第二批继续执行第三批。
- 文档明确每个任务完成后都必须提交一次。
- 文档没有承诺第三批不会实现的主题评审、导出选择或 skill 调度能力。

完成本任务后必须 git commit。

建议 commit message：

```text
docs: 补充第三批 StepAudio 分片执行方案
```

提交前验证：

```bash
pytest
```

## 第三批完成定义

第三批完成时，应满足：

- live 默认 StepAudio provider 可以使用音频分片完成长视频转写。
- 分片时间戳能按偏移合并为整场时间轴。
- ASR 整体缓存和分片缓存都具备可测试行为。
- StepAudio 分片请求具备有限重试能力。
- 任意关键分片失败时，live 直接中止，不输出误导性方案。
- `live --dry-run` 有最小端到端测试闭环，能稳定生成 `transcript.srt`、`plan.json` 和 `拆条报告.md`。
- dry-run 端到端测试不依赖真实外部服务、网络或真实视频处理。
- 每个小任务都有独立 git commit。
- 全量测试通过。

## 第三批完成后的运行说明

### 默认 StepAudio 分片识别

第三批完成后，默认 `stepaudio` provider 不再把整条视频直接作为单次请求上传，而是先提取音频，再按固定时长切成连续分片逐片识别：

```bash
export STEPFUN_API_KEY=sk-...
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --dry-run
```

处理流程：

1. 使用 `ffprobe` 读取源视频时长。
2. 使用 `ffmpeg` 提取统一音频，默认参数为 `16000 Hz`、`1` 声道、`wav`。
3. 按 `asr_shard_seconds` 生成连续识别分片，默认每片 `600` 秒，末片不超过总时长。
4. 使用 `ffmpeg` 为每个识别分片切出独立音频文件。
5. 逐片请求 stepaudio-2.5-asr；网络错误、超时、HTTP 429 和 HTTP 5xx 使用有限重试。
6. 将每个分片返回的本地时间戳加上分片全局起点，丢弃空文本，按时间升序合并为整场转写文本。
7. 写出 `transcript.srt`，再继续候选生成、去重、基础选择、`plan.json` 和 `拆条报告.md`。

预期产物：

- `out/live/transcript.srt`
- `out/live/plan.json`
- `out/live/拆条报告.md`

dry-run 不应生成：

- `out/live/metadata.json`
- `out/live/clips/*.mp4`
- 单条短视频字幕文件

### 缓存行为

第三批完成后，live ASR 缓存应分两层：

- `transcript.json`：整场转写缓存，命中时直接跳过 provider。
- `asr_shard_cache/shard_*.json`：分片缓存，整体缓存缺失时，复用已成功识别且签名匹配的分片。

整体缓存优先级高于分片缓存；命中 `transcript.json` 时不会创建 provider、不会检查 StepAudio 可用性，也不会重新执行音频提取。整体缓存缺失时，CLI 仍会重新准备分片音频，但已命中的分片缓存不会再次请求 StepAudio。

分片缓存签名覆盖：

- 源视频摘要。
- provider。
- ASR 模型。
- 语言。
- 分片起止时间。
- 音频采样率。
- 声道数。
- 音频格式。

缓存损坏或签名不匹配时，只重新识别对应分片。修改模型、语言、分片边界或音频参数后，相关缓存会失效，避免误用旧结果。

### 本地 dry-run 检查

本地验证第三批闭环时，优先运行：

```bash
pytest tests/test_live_dry_run_e2e.py
pytest tests/test_transcript.py
pytest
```

真实视频 dry-run 可使用：

```bash
export STEPFUN_API_KEY=sk-...
video-auto-editor live path/to/live.mp4 --output-dir out/live --work-dir work/live --dry-run
```

检查点：

- `out/live/transcript.srt` 存在，并包含跨分片合并后的全局时间戳。
- `out/live/plan.json` 存在，且 `status` 为 `unreviewed`。
- `out/live/拆条报告.md` 存在，并包含 dry-run 未评审提示。
- dry-run 不生成 `metadata.json`、`clips/*.mp4` 和单条短视频字幕文件。

### 第四批边界

第三批不处理主题评审。以下能力必须留到第四批或后续阶段：

- StepFun Chat 评审 provider。
- 候选片段结构化主题评审。
- 发布就绪评分。
- 基于评审结果的导出选择。
- `plan.json` 和报告中的评审结果增强。

### 第三批实际提交点

第三批应按以下小任务分别提交，每个小任务完成并验证后立即提交：

- `feat: 增加 StepAudio 分片识别配置`
- `feat: 增加直播音频提取与分片计划`
- `refactor: 抽出 StepAudio 分片请求`
- `feat: 合并 StepAudio 分片时间戳`
- `feat: 增加 StepAudio 分片缓存`
- `feat: 为 StepAudio 分片请求增加重试`
- `test: 补充 StepAudio 分片 live dry-run 闭环`
- `docs: 补充第三批 StepAudio 分片执行方案`
