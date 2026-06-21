# 直播课主题拆条实施计划

本文档记录把 `video_auto_editor` 演进为 CLI 底座，并通过 skill 调度器完成直播课主题拆条的实施路线。当前目标是先完成可测试、可复现的 CLI 能力，再让 skill 只负责上层调度。

## 实施阶段

### 1. CLI 打包与配置层

目标：把现有 `python -m video_auto_editor` 能力整理为可安装 CLI，并稳定后续阶段需要的输入输出契约。

范围边界：聚焦 CLI 入口、课程上下文输入、`--max-clips` 过渡语义、dry-run 与 `plan.json` 输出；不接入新 ASR、不接入主题评审模型、不创建 skill 调度器、不重写现有候选算法。

详细任务、验收标准与提交点见 [第一批：CLI 打包与配置层](./implementation-batch-1-cli-foundation.md)。

### 2. 语音识别抽象

目标：把现有 Whisper 转写封装升级为可替换的语音识别接口，并将 stepaudio-2.5-asr 设为默认服务。

范围：

- 定义统一转写接口和返回结构。
- 新增 `StepAudioTranscriber`。
- 保留 `WhisperTranscriber` 作为可选替代。
- 将语音识别配置从 Whisper 专用字段演进为 provider 配置。
- ASR 不可用或识别失败时直接中止。

第二批执行完成后，CLI 底座支持的语音识别 provider 为：

- 默认：`stepaudio`，模型为 `stepaudio-2.5-asr`，API Key 从 `STEPFUN_API_KEY` 读取，base URL 可通过 `STEPFUN_BASE_URL` 覆盖。
- 可选：`whisper`，保留现有 Whisper CLI 转写能力，主要用于本地兼容和回退。

第二批只实现整视频 provider 抽象和最小 `live --dry-run` 闭环；长音频分片、分片级缓存、请求重试和分片时间戳偏移合并统一留到第三批。

### 3. StepAudio 分片识别

目标：让几小时直播课可以稳定识别，并且识别结果具备可缓存、可重试、可合并的时间戳。

范围：

- 使用 FFmpeg 从原视频提取音频。
- 将长音频切成连续识别分片。
- 逐片调用 stepaudio-2.5-asr。
- 将分片时间戳按偏移合并成整场转写文本。
- 默认开启处理缓存，缓存键包含影响结果的输入摘要。

第三批完成后，默认 `stepaudio` provider 的直播课转写流程为：先通过 FFmpeg 提取统一格式音频，再按 `asr_shard_seconds` 生成连续识别分片，逐片调用 stepaudio-2.5-asr，并把分片内时间戳加上全局分片起点后合并为整场转写文本。

处理缓存分为两层：

- `transcript.json`：整场转写缓存，命中时直接跳过 provider 创建、音频提取和 StepAudio 请求。
- `asr_shard_cache/shard_*.json`：分片缓存，整体缓存缺失时复用签名匹配的已识别分片。

缓存签名覆盖源视频摘要、provider、模型、语言、分片起止时间、音频采样率、声道数和音频格式；修改影响结果的配置会使相关缓存失效。

详细任务、验收标准与提交点见 [第三批：StepAudio 分片识别与缓存闭环](./implementation-batch-3-stepaudio-sharding.md)。第三批要求每完成一个小任务并通过对应验证后立即进行一次 git commit。

### 4. 主题评审

目标：使用评审模型对相邻候选批次进行结构化主题评审，为发布就绪短视频提供判断依据。

范围：

- 新增 StepFun Chat 评审 provider，并保留 OpenAI-compatible 配置能力。
- 按相邻候选批次提交候选片段和课程上下文。
- 输出结构化评审结果：主题名、主题完整度、学习价值、传播价值、发布就绪评分、导出建议、标题、摘要、关键词、人工复核、淘汰原因、边界补救建议。
- 评审模型关闭、不可用或失败时输出未评审方案、诊断 warning 和报告，不伪造发布就绪结论；完整导出选择重写留到第五批。

第四批完成后，`live --dry-run` 默认会在候选生成、去重和基础选择后尝试主题评审。评审成功时，`plan.json.status` 为 `reviewed`，候选项包含机器可读的 `review` 字段，`拆条报告.md` 会展示主题评审结果。评审失败、关闭或缺少 API Key 时，CLI 继续写出 `transcript.srt`、未评审 `plan.json` 和报告，并在 `warnings` 中记录原因。

主题评审默认 provider 为 `stepfun_chat`，使用 OpenAI-compatible Chat Completions 请求形态。关键配置项包括 `topic_review_enabled`、`topic_review_provider`、`topic_review_model`、`topic_review_batch_size`、`topic_review_api_key_env`、`topic_review_base_url_env`、`topic_review_base_url` 和 `topic_review_publish_ready_threshold`。

详细任务、验收标准与提交点见 [第四批：主题评审与最小 reviewed dry-run 闭环](./implementation-batch-4-topic-review.md)。第四批要求每完成一个小任务并通过对应验证后立即进行一次 git commit。

### 5. 导出选择与标准交付物

目标：基于主题评审结果导出全部发布就绪短视频，并增强机器可读和人工可读交付物。

范围：

- 默认导出所有发布就绪短视频。
- `--max-clips` 只作为可选上限。
- 支持边界补救和同主题系列。
- 增强 `metadata.json` 和 `plan.json`。
- 增强 `拆条报告.md`，列出导出清单、未导出候选、淘汰原因和人工复核项。
- dry-run 输出完整拆条方案但不裁剪视频。

第五批完成后，`live` 非 dry-run 默认只导出主题评审判定为发布就绪的候选；`--max-clips` 仅作为用户显式上限。主题评审失败、关闭或不可用时，CLI 默认不导出短视频，只输出未评审方案和 warning；显式 `--allow-unreviewed-export` 时才允许未评审兼容导出。`plan.json`、`metadata.json` 和 `拆条报告.md` 需要共同表达导出清单、未导出原因、人工复核项、边界补救建议和同主题系列。

第五批标准交付物为 `transcript.srt`、`plan.json`、非 dry-run 的 `metadata.json`、`clips/`、`subtitles/` 和 `拆条报告.md`。`plan.json` 面向机器读取拆条方案、候选评审和导出选择；`metadata.json` 面向后续自动化读取实际交付包索引；`拆条报告.md` 面向人工解释导出项、未导出项、人工复核项、边界补救建议和同主题系列。

详细任务、验收标准与提交点见 [第五批：导出选择与标准交付物闭环](./implementation-batch-5-export-deliverables.md)。第五批要求每完成一个小任务并通过对应验证后立即进行一次 git commit，并必须建立 reviewed `live` 非 dry-run 最小端到端测试闭环。

### 6. skill 调度器

目标：创建薄调度器 skill，围绕 CLI 底座提供面向用户的协作流程。

范围：

- 做环境预检：CLI、`ffmpeg`、`ffprobe`、StepFun API Key、ASR 配置、评审模型配置。
- 收集课程上下文并生成 JSON 文件。
- 调用 `video-auto-editor live ...`。
- 读取 `plan.json`、`metadata.json` 和拆条报告解释结果。
- 根据失败原因提示修复或二次运行。
- 不直接剪视频，不绕过 CLI 写产物，不承载候选算法。

第六批完成后，面向用户的直播拆条协作由薄调度器 skill 编排：先做环境预检（CLI、`ffmpeg`、`ffprobe`、`STEPFUN_API_KEY`、ASR 与评审配置），再收集课程上下文并生成合法 JSON，调用 `video-auto-editor live ...`，最后读取 `plan.json`、`metadata.json` 和 `拆条报告.md` 解释导出清单、未导出原因、人工复核项、边界补救建议和同主题系列，并在失败或降级时给出可执行修复建议与二次运行命令。重活仍全部由 CLI 完成，skill 不直接剪视频、不绕过 CLI 写产物、不承载候选算法。

详细任务、验收标准与提交点见 [第六批：skill 调度器与最小协作闭环](./implementation-batch-6-skill-orchestrator.md)。第六批要求每完成一个小任务并通过对应验证后立即进行一次 git commit。

## 分批执行文档

- [第一批：CLI 打包与配置层](./implementation-batch-1-cli-foundation.md)
- [第二批：ASR 抽象与最小 live dry-run 闭环](./implementation-batch-2-asr-foundation.md)
- [第三批：StepAudio 分片识别与缓存闭环](./implementation-batch-3-stepaudio-sharding.md)
- [第四批：主题评审与最小 reviewed dry-run 闭环](./implementation-batch-4-topic-review.md)
- [第五批：导出选择与标准交付物闭环](./implementation-batch-5-export-deliverables.md)
- [第六批：skill 调度器与最小协作闭环](./implementation-batch-6-skill-orchestrator.md)

## 提交节奏

每一批都应拆成多个小提交，按“独立可测试能力”提交，而不是等整批完成后一次性提交。第一批的具体提交点见 [第一批执行方案](./implementation-batch-1-cli-foundation.md#建议提交点)，第二批的具体提交点见 [第二批执行方案](./implementation-batch-2-asr-foundation.md)，第三批的具体提交点见 [第三批执行方案](./implementation-batch-3-stepaudio-sharding.md)，第四批的具体提交点见 [第四批执行方案](./implementation-batch-4-topic-review.md)，第五批的具体提交点见 [第五批执行方案](./implementation-batch-5-export-deliverables.md)。第三批、第四批、第五批和第六批执行时，每完成一个小任务并通过对应验证命令后，必须立即进行一次 git commit。
