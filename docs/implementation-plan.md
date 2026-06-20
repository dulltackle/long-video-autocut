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

### 3. StepAudio 分片识别

目标：让几小时直播课可以稳定识别，并且识别结果具备可缓存、可重试、可合并的时间戳。

范围：

- 使用 FFmpeg 从原视频提取音频。
- 将长音频切成连续识别分片。
- 逐片调用 stepaudio-2.5-asr。
- 将分片时间戳按偏移合并成整场转写文本。
- 默认开启处理缓存，缓存键包含影响结果的输入摘要。

### 4. 主题评审

目标：使用评审模型对相邻候选批次进行结构化主题评审，为发布就绪短视频提供判断依据。

范围：

- 新增 StepFun Chat 评审 provider，并保留 OpenAI-compatible 配置能力。
- 按相邻候选批次提交候选片段和课程上下文。
- 输出结构化评审结果：主题名、主题完整度、学习价值、传播价值、发布就绪评分、导出建议、标题、摘要、关键词、人工复核、淘汰原因、边界补救建议。
- LLM 不可用时默认不导出，只输出未评审方案和报告。

### 5. 导出选择与标准交付物

目标：基于主题评审结果导出全部发布就绪短视频，并增强机器可读和人工可读交付物。

范围：

- 默认导出所有发布就绪短视频。
- `--max-clips` 只作为可选上限。
- 支持边界补救和同主题系列。
- 增强 `metadata.json` 和 `plan.json`。
- 增强 `拆条报告.md`，列出导出清单、未导出候选、淘汰原因和人工复核项。
- dry-run 输出完整拆条方案但不裁剪视频。

### 6. skill 调度器

目标：创建薄调度器 skill，围绕 CLI 底座提供面向用户的协作流程。

范围：

- 做环境预检：CLI、`ffmpeg`、`ffprobe`、StepFun API Key、ASR 配置、评审模型配置。
- 收集课程上下文并生成 JSON 文件。
- 调用 `video-auto-editor live ...`。
- 读取 `plan.json`、`metadata.json` 和拆条报告解释结果。
- 根据失败原因提示修复或二次运行。
- 不直接剪视频，不绕过 CLI 写产物，不承载候选算法。

## 分批执行文档

- [第一批：CLI 打包与配置层](./implementation-batch-1-cli-foundation.md)
- [第二批：ASR 抽象与最小 live dry-run 闭环](./implementation-batch-2-asr-foundation.md)

## 提交节奏

每一批都应拆成多个小提交，按“独立可测试能力”提交，而不是等整批完成后一次性提交。第一批的具体提交点见 [第一批执行方案](./implementation-batch-1-cli-foundation.md#建议提交点)，第二批的具体提交点见 [第二批执行方案](./implementation-batch-2-asr-foundation.md)。
