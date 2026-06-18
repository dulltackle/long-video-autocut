# Video Auto Editor v4.7

A rule-based and AI-powered automated video roughing tool. It automatically identifies the best segments from original footage and performs editing and splicing.

> **Best for**: **Single-person / talking-to-camera (A'Roll) content** — vlogs, tutorials, podcasts, knowledge-sharing monologues. Not suitable for multi-person dialogues, interviews, or music/B-roll heavy content.

---

## Features

- **Scenario A - Single Video**: Automatically selects the best segment from one video
- **Scenario B - Batch Processing**: Processes multiple videos, performs cross-video deduplication, and concatenates into one final video
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

### Command Format

```
python3 -m video_auto_editor single <video_path> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor batch <input_dir> [--output-dir ./output] [--work-dir ./video_work]
```

| Parameter | Description | Default |
|------------|-------------|---------|
| `single <video_path>` | Process one video file (Scenario A) | Required |
| `batch <input_dir>` | Process one folder of videos (Scenario B) | Required |
| `--output-dir` | Output directory for clips and reports | `./output` |
| `--work-dir` | Temporary directory for intermediate files | `./video_work` |

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

## 代码结构

命令入口和核心实现已经拆分到 `video_auto_editor/` 下的小模块：

- `cli.py`：命令分发和 Scenario A/B 流程编排
- `models.py`、`config.py`：共享数据结构和默认配置
- `media.py`、`silence.py`、`transcript.py`：FFmpeg 操作、静音检测、Whisper 转写
- `scoring.py`、`dedup.py`、`selection.py`：评分、去重、最终片段选择
- `report.py`：Markdown 报告生成

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

**Version**: v4.7 | **Last Updated**: 2026-03-11
