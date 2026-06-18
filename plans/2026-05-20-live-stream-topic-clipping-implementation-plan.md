# 直播按话题拆条改造实施计划

**目标：** 将 `gilbertwuu/Auto-Cut-video-A-Roll` 从“单视频挑选一个最佳 A-Roll 片段”的工具，改造成“长直播按话题拆分并批量生成多个短视频”的工具。

**架构：** 保留现有 FFmpeg 裁剪、静音检测、基础评分和报告能力，但把核心流程从“候选片段单选”改为“整视频转写、话题分段、多片段选择、语义去重、批量导出”。第一阶段先实现多片段拆条 MVP，第二阶段引入话题识别和语义去重，第三阶段补齐字幕、标题、竖屏适配等发布增强能力。

**技术栈：** Python 3.8+、FFmpeg / ffprobe、openai-whisper；第二阶段建议新增 embedding 能力，可先抽象接口，默认实现使用本地或远程 embedding 模型。

---

## 背景与现状

当前项目主要由 `video_editor_auto_v4.6.py` 单脚本承载：

- `detect_silence()`：基于 FFmpeg `silencedetect` 检测静音。
- `identify_segments()`：按静音把视频切成非静音候选片段。
- `score_segment()`：按清晰开头、清晰结尾、中段流畅度、自然节奏打基础分。
- `transcribe_segment()`：对候选片段调用 Whisper 转写。
- `analyze_fluency()`：分析重复、口头禅、自然结尾、中断。
- `select_best_segment()`：最终只选择一个最佳片段。
- `clip_segment()` / `concat_videos()`：负责裁剪与拼接。

直播拆条的目标不同：需要从一条长直播中输出多条可独立观看的短视频，并尽量按话题组织，而不是只挑一个最流畅片段。

## 范围

本计划覆盖：

- 长直播整视频转写与时间戳缓存。
- 话题候选片段生成。
- 多短视频选择逻辑。
- 片段级与话题级去重。
- 批量导出视频、字幕、元数据和报告。
- 后续发布增强能力的接口与实现路线。

本计划不覆盖：

- 图形化剪辑界面。
- 多机位剪辑。
- 复杂 B-Roll 自动插入。
- 平台自动发布。
- 对多人访谈、音乐剪辑、游戏直播的完整适配。

## 推荐目标输出结构

```text
output/
├── clips/
│   ├── 001_如何提升直播转化.mp4
│   ├── 002_新手做内容的常见误区.mp4
│   └── 003_为什么不要只看播放量.mp4
├── subtitles/
│   ├── 001_如何提升直播转化.srt
│   └── 002_新手做内容的常见误区.srt
├── transcript.json
├── transcript.srt
├── metadata.json
└── 拆条报告.md
```

`metadata.json` 建议结构：

```json
{
  "source_video": "live.mp4",
  "clips": [
    {
      "index": 1,
      "title": "为什么不要只看播放量",
      "start": 1832.4,
      "end": 1968.9,
      "duration": 136.5,
      "topic": "内容增长",
      "summary": "解释播放量和转化之间的关系。",
      "keywords": ["播放量", "转化", "内容增长"],
      "score": 91,
      "output_path": "clips/001_为什么不要只看播放量.mp4",
      "subtitle_path": "subtitles/001_为什么不要只看播放量.srt"
    }
  ]
}
```

## 建议模块拆分

如果继续在单脚本中追加逻辑，文件会迅速膨胀。建议在保持 CLI 兼容的前提下拆分为模块：

```text
video_auto_editor/
├── __init__.py
├── cli.py
├── config.py
├── models.py
├── media.py
├── silence.py
├── transcript.py
├── topic.py
├── scoring.py
├── dedup.py
├── selection.py
├── export.py
└── report.py
video_editor_auto_v4.6.py
requirements.txt
README.md
CODE_DOCUMENTATION.md
tests/
```

职责建议：

- `cli.py`：解析参数，分发单片段模式、批量模式、直播拆条模式。
- `models.py`：定义 `Segment`、`TranscriptChunk`、`TopicSegment`、`ClipCandidate`、`ClipExport`。
- `media.py`：封装 ffprobe、裁剪、拼接、音频抽取。
- `silence.py`：静音检测和静音边界辅助。
- `transcript.py`：整视频转写、片段转写、转写缓存、SRT 生成。
- `topic.py`：文本分块、话题边界识别、话题合并。
- `scoring.py`：流畅度、信息密度、独立性、标题潜力等评分。
- `dedup.py`：文本相似度、embedding 相似度、话题级去重。
- `selection.py`：从候选片段中选择多条短视频。
- `export.py`：批量导出视频、字幕和元数据。
- `report.py`：生成 Markdown 报告。

## 新增配置项

建议在 `CONFIG` 中新增直播拆条相关配置，后续可迁移到独立配置文件：

```python
CONFIG = {
    "mode": "auto",
    "max_clips": 10,
    "min_clip_duration": 30,
    "max_clip_duration": 180,
    "target_clip_duration": 90,
    "topic_window_seconds": 120,
    "topic_overlap_seconds": 15,
    "topic_similarity_threshold": 0.72,
    "duplicate_topic_threshold": 0.82,
    "context_expand_before": 12,
    "context_expand_after": 8,
    "avoid_context_dependent_start": True,
    "export_subtitles": True,
    "export_metadata": True,
    "vertical_export": False
}
```

## 阶段一：多片段拆条 MVP

目标：先从长直播中输出多条短视频，不要求语义话题识别完全准确。

### 任务 1：保留旧入口，新增直播拆条模式

涉及文件：

- `video_editor_auto_v4.6.py`
- `video_auto_editor/cli.py`
- `video_auto_editor/config.py`

步骤：

1. 新增 CLI 参数 `--mode single|batch|live`，默认 `auto`。
2. 当输入为单个视频且显式传入 `--mode live` 时进入直播拆条流程。
3. 保留原来的单视频和批量处理行为，避免破坏既有用法。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode single
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 3
```

预期结果：

- `single` 仍输出原有单条粗剪。
- `live` 进入新流程，至少生成拆条报告框架。

### 任务 2：新增整视频转写与缓存

涉及文件：

- `video_auto_editor/transcript.py`
- `video_auto_editor/models.py`
- `video_auto_editor/export.py`

步骤：

1. 新增 `TranscriptChunk` 数据结构，包含 `start`、`end`、`text`。
2. 新增 `transcribe_video(video_path, work_dir)`，调用 Whisper 对整条视频转写。
3. 将转写结果缓存为 `work_dir/transcript.json`。
4. 如果缓存存在且源视频未变更，优先复用缓存。
5. 新增 `export_srt(chunks, output_path)`。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 1
```

预期结果：

- 生成 `transcript.json`。
- 生成 `transcript.srt`。
- 第二次运行复用缓存，不重复完整转写。

### 任务 3：用静音和转写时间戳生成候选片段

涉及文件：

- `video_auto_editor/silence.py`
- `video_auto_editor/topic.py`
- `video_auto_editor/models.py`

步骤：

1. 新增 `ClipCandidate` 数据结构，包含时间范围、文本、候选来源和基础分。
2. 基于转写 chunks 按 `target_clip_duration` 初步滑窗。
3. 用静音边界校准候选片段的开头和结尾。
4. 过滤短于 `min_clip_duration` 或长于 `max_clip_duration` 的候选。
5. 对候选片段做上下文补边，避免从明显依赖上下文的词开头。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 5
```

预期结果：

- 报告中列出多个候选片段。
- 候选片段具有合理的开始时间、结束时间、持续时长和文本摘要。

### 任务 4：把单选逻辑改为多选逻辑

涉及文件：

- `video_auto_editor/selection.py`
- `video_auto_editor/scoring.py`
- `video_auto_editor/dedup.py`

步骤：

1. 新增 `select_best_segments(candidates, max_clips)`。
2. 保留旧 `select_best_segment()` 供单视频模式使用。
3. 多选时按综合分排序，并避免片段时间重叠。
4. 同一时间范围附近只保留得分最高的候选。
5. 输出数量不超过 `max_clips`。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 3
```

预期结果：

- 输出不超过 3 条短视频。
- 多条短视频之间没有明显时间重叠。

### 任务 5：批量导出 clips、metadata 和报告

涉及文件：

- `video_auto_editor/export.py`
- `video_auto_editor/report.py`
- `video_auto_editor/media.py`

步骤：

1. 新增 `export_clips(video_path, selected_clips, output_dir)`。
2. 每条 clip 使用安全文件名，格式为 `001_<title>.mp4`。
3. 输出 `metadata.json`。
4. 输出 `拆条报告.md`，包含候选片段、入选原因、过滤原因。
5. 可选输出每条 clip 的 SRT 字幕。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 3
```

预期结果：

- `output/clips/` 下有多条 mp4。
- `metadata.json` 可以被 JSON parser 正常读取。
- `拆条报告.md` 能说明每条短视频为什么被选择。

## 阶段二：话题级拆条

目标：从“多片段”升级为“按话题拆分”，减少重复片段，提高每条短视频的独立观看价值。

### 任务 6：新增话题片段模型

涉及文件：

- `video_auto_editor/models.py`
- `video_auto_editor/topic.py`

步骤：

1. 新增 `TopicSegment` 数据结构。
2. 字段包括 `topic_id`、`start`、`end`、`text`、`keywords`、`summary`、`title`。
3. 建立 `TopicSegment -> ClipCandidate` 的转换函数。
4. 保留原始 transcript chunk 到 topic 的映射关系。

验证：

```bash
python3 -m pytest tests/test_topic_segments.py
```

预期结果：

- 相邻 transcript chunks 可以合并为 topic segment。
- topic segment 能转换为候选 clip。

### 任务 7：实现基础话题边界识别

涉及文件：

- `video_auto_editor/topic.py`
- `video_auto_editor/scoring.py`

步骤：

1. 先实现规则版话题边界：长停顿、转场词、关键词变化、文本长度。
2. 增加 `detect_topic_boundaries(chunks, silences)`。
3. 对过短 topic 进行向前或向后合并。
4. 对过长 topic 按语义或时长再切分。

验证：

```bash
python3 -m pytest tests/test_topic_boundaries.py
```

预期结果：

- 明显长停顿处可以成为话题边界。
- 过短话题不会单独导出。
- 过长话题会被拆成可发布长度。

### 任务 8：抽象 embedding 接口

涉及文件：

- `video_auto_editor/dedup.py`
- `video_auto_editor/topic.py`
- `requirements.txt`

步骤：

1. 新增 `EmbeddingProvider` 协议或基类。
2. 提供 `NullEmbeddingProvider`，没有模型时回退到文本相似度。
3. 提供一个可选 embedding 实现，具体模型通过配置指定。
4. 所有语义相似度调用都走统一接口。

验证：

```bash
python3 -m pytest tests/test_embedding_provider.py
```

预期结果：

- 没有 embedding 依赖时，程序仍可运行。
- 有 embedding 配置时，能返回稳定向量并计算相似度。

### 任务 9：实现话题聚类与语义去重

涉及文件：

- `video_auto_editor/dedup.py`
- `video_auto_editor/topic.py`
- `video_auto_editor/selection.py`

步骤：

1. 对 topic segment 计算 embedding。
2. 用相似度阈值聚合同类话题。
3. 每个话题簇内保留综合分最高的 1 条，或按配置保留最多 N 条。
4. 去重报告中说明重复片段和保留原因。

验证：

```bash
python3 -m pytest tests/test_topic_dedup.py
```

预期结果：

- 同一话题的重复片段不会批量导出。
- 不同话题不会因为少量关键词相同被误删。

### 任务 10：增加直播拆条评分体系

涉及文件：

- `video_auto_editor/scoring.py`
- `video_auto_editor/selection.py`

步骤：

1. 保留原流畅度评分作为子分。
2. 新增信息密度评分。
3. 新增独立性评分，惩罚依赖上下文的开头和结尾。
4. 新增标题潜力评分，可先用规则版实现。
5. 最终分数采用可解释的加权结构。

建议公式：

```text
final_score =
  topic_value_score
+ standalone_score
+ fluency_score
+ natural_boundary_score
- duplicate_penalty
- context_missing_penalty
```

验证：

```bash
python3 -m pytest tests/test_live_clip_scoring.py
```

预期结果：

- 报告能展示每个分项分数。
- 开头缺上下文的片段分数明显下降。
- 有清晰观点、结论或方法的片段分数更高。

### 任务 11：生成标题、摘要和关键词

涉及文件：

- `video_auto_editor/topic.py`
- `video_auto_editor/report.py`
- `video_auto_editor/export.py`

步骤：

1. 先实现规则版标题：从高频关键词、疑问句、结论句中提取。
2. 摘要限制在 1-2 句话。
3. 关键词限制在 3-8 个。
4. 为后续 LLM 标题生成预留接口，但不强制依赖。

验证：

```bash
python3 -m pytest tests/test_topic_metadata.py
```

预期结果：

- 每条 clip 都有非空标题、摘要和关键词。
- 文件名经过安全处理，不包含路径非法字符。

## 阶段三：发布增强

目标：让输出结果从“能切出来”升级为“更接近可发布”。

### 任务 12：字幕导出与可选烧录

涉及文件：

- `video_auto_editor/transcript.py`
- `video_auto_editor/export.py`
- `video_auto_editor/media.py`

步骤：

1. 按每条 clip 的起止时间截取对应字幕。
2. 输出每条 clip 的 `.srt` 文件。
3. 新增 `--burn-subtitles` 参数。
4. 使用 FFmpeg 可选烧录字幕。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 2 --export-subtitles
```

预期结果：

- 每条视频有对应 SRT。
- 开启烧录后，视频画面中出现字幕。

### 任务 13：竖屏导出接口

涉及文件：

- `video_auto_editor/media.py`
- `video_auto_editor/export.py`
- `video_auto_editor/config.py`

步骤：

1. 新增 `--aspect-ratio 16:9|9:16|1:1`。
2. 先实现居中裁切和背景模糊两种模式。
3. 人脸追踪作为后续增强，不作为第一版必需能力。
4. 在 metadata 中记录导出比例和裁切方式。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --max-clips 1 --aspect-ratio 9:16
```

预期结果：

- 输出视频分辨率符合 9:16。
- 主体区域没有明显被错误裁掉。

### 任务 14：平台化导出预设

涉及文件：

- `video_auto_editor/config.py`
- `video_auto_editor/cli.py`
- `video_auto_editor/export.py`

步骤：

1. 新增 `--preset douyin|bilibili|youtube-shorts|custom`。
2. 每个 preset 定义默认时长、比例、字幕、码率。
3. `custom` 使用显式 CLI 参数覆盖默认配置。
4. 报告中写明实际使用的导出配置。

验证：

```bash
python3 video_editor_auto_v4.6.py ./sample.mp4 ./output --mode live --preset douyin --max-clips 2
```

预期结果：

- 导出参数符合 preset。
- CLI 参数可以覆盖 preset 的单项配置。

## 测试策略

单元测试建议覆盖：

- transcript chunk 合并与 SRT 生成。
- 静音边界校准。
- 话题边界识别。
- 多片段选择和重叠过滤。
- 文本相似度与 embedding fallback。
- metadata JSON 输出。
- 文件名安全处理。

集成测试建议覆盖：

- 30-60 秒短样例视频，验证完整流程能输出 clips、metadata、报告。
- 含多个明显话题的样例 transcript，验证能拆成多个 topic。
- 无 Whisper 或无 embedding 配置时，验证 fallback 行为。

建议命令：

```bash
python3 -m compileall video_auto_editor video_editor_auto_v4.6.py
python3 -m pytest
python3 video_editor_auto_v4.6.py ./fixtures/live_sample.mp4 ./output --mode live --max-clips 3
```

## 验收标准

阶段一完成标准：

- 输入一条长视频，可以输出多条 mp4。
- 每条 clip 有起止时间、标题、分数和基础报告。
- 旧的单视频模式和批量模式不退化。

阶段二完成标准：

- 输出结果按话题组织，而不是仅按静音或固定时长切分。
- 同一话题重复片段明显减少。
- 每条 clip 有可解释的标题、摘要、关键词和评分分项。

阶段三完成标准：

- 每条 clip 可带字幕。
- 支持至少一种竖屏导出方式。
- 支持平台预设。
- `metadata.json` 可以作为后续人工复核或发布流水线输入。

## 风险与处理

- 长直播转写耗时过长：必须做 transcript 缓存，并支持复用。
- 话题边界不稳定：第一版用规则与静音边界兜底，第二版再引入 embedding。
- clip 开头缺上下文：增加向前补边和上下文依赖惩罚。
- 单脚本膨胀：尽早拆分模块，但保留旧入口兼容。
- 外部模型依赖不稳定：embedding 和 LLM 能力都通过接口抽象，默认可降级。
- 测试样例视频体积过大：测试优先使用 transcript fixture，少量集成测试再使用短视频 fixture。

## 推荐实施顺序

1. 先建立模块结构和 CLI 兼容层。
2. 实现整视频转写缓存。
3. 实现基于转写和静音的多候选片段生成。
4. 实现多片段选择和批量导出。
5. 补齐报告与 metadata。
6. 引入话题模型和基础话题边界识别。
7. 引入 embedding 抽象和语义去重。
8. 增强评分体系。
9. 增加标题、摘要、关键词。
10. 增加字幕、竖屏和平台预设。

这个顺序可以保证每个阶段都有可运行产物，避免一开始就把话题识别、字幕、竖屏和发布预设全部耦合在一起。
