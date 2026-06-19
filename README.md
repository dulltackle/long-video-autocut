# Video Auto Editor v4.7

A rule-based and AI-powered automated video roughing tool. It automatically identifies the best segments from original footage and performs editing and splicing.

> **Best for**: **Single-person / talking-to-camera (A'Roll) content** — vlogs, tutorials, podcasts, knowledge-sharing monologues. Not suitable for multi-person dialogues, interviews, or music/B-roll heavy content.

---

## Features

- **Scenario A - Single Video**: Automatically selects the best segment from one video
- **Scenario B - Batch Processing**: Processes multiple videos, performs cross-video deduplication, and concatenates into one final video
- **Scenario C - Live Clipping MVP**: 对单条长直播做整视频转写，并批量导出短视频、字幕和 metadata
- **Smart Scoring**: 4-dimension scoring (clear start/end, fluency, natural rhythm) + fluency analysis
- **Content Deduplication**: Similarity detection based on transcription text, both within-video and cross-video
- **Auto Reports**: Generates detailed Markdown reports for each processing run

---

## Best For / Not Suitable

| ✅ Best For | ❌ Not Suitable |
|-------------|-----------------|
| Single-person talking to camera (A'Roll) | Multi-person dialogues, interviews |
| Vlogs, tutorials, podcasts, monologues | Music-heavy, B-roll heavy content |
| Multiple takes of same content (batch dedup) | Content requiring multiple segments kept |
| Chinese speech (fluency patterns tuned) | Non-Chinese (patterns not adapted) |
| Raw long footage (rough cut) | Already tightly edited content |

---

## Requirements

- **Python** 3.8+
- **FFmpeg** (including ffprobe)
- **openai-whisper**

### Installation

```bash
# macOS
brew install ffmpeg
pip install openai-whisper

# Ubuntu / Debian
sudo apt install ffmpeg
pip install openai-whisper
```

---

## Quick Start

### Scenario A: Single Video

Pass a **video file path**:

```bash
python3 -m video_auto_editor single ./video.MTS --output-dir ./output
```

Output:

```
output/
├── video_clip.mp4    # 最佳片段裁剪结果
└── video_report.md   # 处理报告
```

### Scenario B: Batch + Deduplication + Concatenation

Pass a **folder path**:

```bash
python3 -m video_auto_editor batch ./Video --output-dir ./output
```

Output (only two files, intermediate files are cleaned up):

```
output/
├── final_concat_20260311_1905.mp4  # 去重后的拼接视频
└── batch_report.md                 # 批量报告，包含片段明细和去重决策
```

### Scenario C: Live Clipping MVP

传入一条**长直播视频文件**，并指定最多导出的短视频数量：

```bash
python3 -m video_auto_editor live ./live.mp4 --output-dir ./output --max-clips 5
```

输出：

```
output/
├── clips/
│   ├── 001_片段标题.mp4
│   └── 002_片段标题.mp4
├── subtitles/
│   ├── 001_片段标题.srt
│   └── 002_片段标题.srt
├── transcript.srt
├── metadata.json
└── 拆条报告.md
```

整视频转写缓存会写入 `--work-dir` 下的 `transcript.json`。当源视频路径、文件大小和修改时间都匹配时，下一次运行会直接复用缓存。

### Command Format

```
python3 -m video_auto_editor single <video_path> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor batch <input_dir> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor live <video_path> [--output-dir ./output] [--work-dir ./video_work] [--max-clips 5]
```

| Parameter | Description | Default |
|------------|-------------|---------|
| `single <video_path>` | Process one video file (Scenario A) | Required |
| `batch <input_dir>` | Process one folder of videos (Scenario B) | Required |
| `live <video_path>` | 将单条长直播拆成多条短视频（Scenario C） | Required |
| `--output-dir` | Output directory for clips and reports | `./output` |
| `--work-dir` | Temporary directory for intermediate files | `./video_work` |
| `--max-clips` | Maximum live clips to export | `5` |

Supported formats: `.MTS`, `.mp4`, `.mov`

---

## Processing Pipeline

### Scenario A (Single Video)

```
Input video → Silence detection → Segment identification → 4-dimension scoring
→ Candidate filtering → Whisper transcription → Fluency analysis
→ Within-video dedup → Layered selection → Clip output
```

### Scenario B (Batch)

```
Input directory → Process each video (Scenario A, no individual reports)
→ Cross-video deduplication → Concatenate by filename order
→ Clean intermediate files → Generate single batch report
```

### Scenario C (Live Clipping MVP)

```
Input live video → Silence detection → Full-video Whisper transcription + cache
→ Timestamped transcript windows → Silence-boundary adjustment → Candidate enrichment
→ Duplicate detection → Multi-clip selection → Clip/subtitle/metadata/report output
```

## 代码结构

命令入口和核心实现已经拆分到 `video_auto_editor/` 下的小模块：

- `cli.py`：命令分发和 Scenario A/B/C 流程编排
- `models.py`、`config.py`：共享数据结构和默认配置
- `media.py`、`silence.py`、`transcript.py`：FFmpeg 操作、静音检测、片段/整视频 Whisper 转写
- `scoring.py`、`dedup.py`、`selection.py`：评分、去重、单片段选择和直播多片段选择
- `topic.py`：直播拆条候选生成、静音边界校准、标题/摘要/关键词补全
- `export.py`：直播短视频、字幕和 metadata 批量导出
- `report.py`：单视频、批处理和直播拆条 Markdown 报告生成

---

## Configuration

All parameters are in the `CONFIG` dict at the top of the script:

```python
CONFIG = {
    # Silence detection
    "silence_noise": -30,           # dB, lower = stricter
    "silence_duration": 0.8,        # seconds, minimum silence length

    # Filtering
    "min_score": 90,                # Minimum base score (max 100)
    "min_duration": 15,             # Minimum segment duration (seconds)

    # Clip buffer
    "buffer_start": 1,              # Buffer before start (seconds)
    "buffer_end": 3,                # Buffer after end (seconds)

    # Encoding
    "crf": 18,                      # Video quality (18=visually lossless, 23=default)
    "preset": "fast",               # Encoding speed
    "audio_bitrate": "192k",        # Audio bitrate

    # Adjusted score weights
    "penalty_repeat": 5,            # Per repeat penalty
    "penalty_stutter": 3,           # Per stutter penalty
    "penalty_interrupt": 10,        # Sudden interruption penalty
    "bonus_natural_end": 5,         # Natural ending bonus
    "bonus_completeness_max": 3,    # Completeness bonus cap

    # Deduplication
    "duplicate_threshold": 0.7,     # Content similarity threshold (0-1)

    # Live clipping
    "min_clip_duration": 30,        # Minimum live clip duration (seconds)
    "max_clip_duration": 180,       # Maximum live clip duration (seconds)
    "target_clip_duration": 90,     # Preferred live clip duration (seconds)
    "topic_overlap_seconds": 15,    # Overlap between transcript windows
    "context_expand_before": 12,    # Expand candidate start to nearby silence
    "context_expand_after": 8,      # Expand candidate end to nearby silence
    "max_clips": 5,                 # Default max clips for live mode
    "min_clip_gap_seconds": 5,      # Minimum gap between selected live clips
    "export_subtitles": True,       # Export per-clip SRT subtitles
    "live_report_name": "拆条报告.md",

    # Whisper
    "whisper_model": "small",
    "whisper_language": "zh",
    "whisper_timeout": 120,
    "whisper_output_format": "txt",
    "whisper_sample_rate": 16000,
    "whisper_channels": 1,
}
```

### Tuning Tips

| Scenario | Parameter | Suggested Value |
|----------|-----------|-----------------|
| Noisy environment | `silence_noise` | `-35` |
| Segments too fragmented | `silence_duration` | `1.0` |
| Want more candidates | `min_score` | `85` |
| Want shorter segments | `min_duration` | `10` |
| Higher quality | `crf` | `15` (larger files) |
| More live clips | `max_clips` | `8` or `10` |
| Longer live clips | `target_clip_duration` | `120` |
| Long live transcription timeout | `whisper_timeout` | Increase based on video length |

---

## Scoring System

### Base Score (4 dimensions × 25 points = 100)

| Dimension | Max | Criteria |
|-----------|-----|----------|
| Clear start | 25 | Sufficient silence before segment |
| Clear end | 25 | Sufficient silence after segment |
| Mid fluency | 25 | Fewer internal interruptions |
| Natural rhythm | 25 | Low pause ratio + no overly long pauses + not too short |

### Adjusted Score (0-100)

Applied on top of base score based on transcription analysis:

| Item | Points | Description |
|------|--------|-------------|
| Repeat penalty | -5 each | "Re-said" type stutters (normalized per 30s) |
| Stutter penalty | -3 each | Filler words (um, uh, etc.) |
| Interruption penalty | -10 | Ends with connective words (then, but, etc.) |
| Natural end bonus | +5 | Complete sentence, question, or summary ending |
| Completeness bonus | +0~3 | Natural end + duration near 60s |

### Layered Selection (Choosing Best Segment)

Not simply the highest score; prioritized filtering:

```
Layer 1: Prefer naturally ending segments
Layer 2: Sort by fluency (tolerance 1.5 per 30s)
Layer 3: Sort by adjusted score
Layer 4: Tie-break → incomplete: pick last; complete: pick longest
```

### Deduplication Rules

Same selection rule for within-video and cross-video dedup:

```
Natural end > Adjusted score > Index/filename order (later preferred)
```

### Live Clipping Selection

直播拆条会导出多个片段，因此使用不同于单片段模式的选择策略：

```
Transcript windows → Score by duration/boundary/fluency → Remove duplicate text windows
→ Sort by live score → Skip overlapping clips → Return selected clips in timeline order
```

当前 `live` 模式是基于带时间戳转写窗口和文本相似度的 MVP，还没有接入 embedding 语义话题识别。

---

## FAQ

### Q: No silence segments detected?

Background noise may cause misdetection. Try lowering `silence_noise` from `-30` to `-35`.

### Q: Segments cut too finely?

Silence duration threshold may be too short. Increase `silence_duration` from `0.8` to `1.0` or `1.5`.

### Q: Why wasn't the highest-scoring segment chosen?

The system uses **layered selection**, not raw score comparison. Natural end > Fluency > Adjusted score > Duration. A 95-point segment that ends abruptly may rank lower than a 90-point segment with a natural ending.

### Q: Whisper transcription inaccurate?

Default uses `small` model for speed/accuracy balance. To improve:
- 在 `CONFIG` 中将 `whisper_model` 从 `small` 改为 `medium` 或 `large`
- `medium` model has ~85% accuracy for Chinese; recommended if resources allow

### Q: How does cross-video dedup decide which to keep?

Selection rule: Natural end > Adjusted score > Later filename (usually last take, best state).

### Q: Where are the detailed reports?

- Scenario A: `output/<video_name>_report.md`
- Scenario B: `output/batch_report.md`，包含片段明细、转写摘要和去重决策
  - Scenario B does not keep intermediate reports or clips; they are cleaned after concatenation
- Scenario C: `output/拆条报告.md`，包含入选片段、候选决策、输出文件路径

### Q: 直播转写缓存在哪里？

`live` 模式会把可复用的转写缓存写入 `<work-dir>/<video_name>/transcript.json`，同时导出 `output/transcript.srt` 方便人工检查。

### Q: 为什么 `live` 实际导出的片段少于 `--max-clips`？

选择器会跳过重复候选，以及和已选片段重叠过多的候选。如果源视频可用语音较少，或很多窗口内容高度相似，最终数量可能少于 `--max-clips`。

---

## Project Structure

```
video_auto_editor/
├── video_auto_editor/          # Core package and module CLI
├── README.md                   # This doc
├── CODE_DOCUMENTATION.md       # Technical doc (architecture, modules, API)
├── requirements.txt            # Python dependencies
├── LICENSE                     # GPL v3 license
└── .gitignore                  # Git ignore rules
```

---

## Technical Documentation

For module implementation details, data structures, algorithms, and extension guides, see `CODE_DOCUMENTATION.md`.

---

## License

This project is licensed under **GPL v3**. If you use this software or its derivatives for commercial purposes, you must release your product as open source under the same license (GPL v3 copyleft requirement).

See the [LICENSE](LICENSE) file for details.

---

**Version**: v4.7 | **Last Updated**: 2026-06-19
