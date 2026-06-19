# Video Auto Editor v4.7 - Technical Documentation

**Version**: v4.7
**Last Updated**: 2026-06-19

> This document is for developers. It describes the system architecture, module implementations, data structures, and extension guidelines. The system is designed for **single-person / talking-to-camera (A'Roll) content**; segmentation and fluency analysis assume monologue-style speech. `live` 拆条模式目前是基于带时间戳转写窗口的 MVP，不是完整的语义话题建模。

---

## Table of Contents

1. [System Architecture](#system-architecture)
2. [Data Structures](#data-structures)
3. [Core Modules](#core-modules)
4. [API Reference](#api-reference)
5. [Extension Guide](#extension-guide)

---

## System Architecture

### Overall Flow

```
┌─────────────────────────────────────────────────┐
│              Main Entry (main)                   │
│  - Parse arguments                               │
│  - Require explicit subcommand: single/batch/live│
└────────────────┬────────────────────────────────┘
                 │
        ┌────────┴────────┬─────────────────┐
        │                 │                 │
 Scenario A: Single  Scenario B: Batch  Scenario C: Live
        │                 │                 │
        ▼                 ▼                 ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ Silence      │   │ Run Scenario A│   │ Silence      │
│ detection    │   │ per video     │   │ detection    │
│ Segment      │   └──────┬───────┘   │ Full-video   │
│ identification│         │           │ transcription│
│ 4-dim scoring│   ┌──────▼───────┐   │ Candidate    │
│ Candidate    │   │ Cross-video   │   │ generation   │
│ filtering    │   │ deduplication │   │ Live scoring │
│ Whisper      │   └──────┬───────┘   │ Multi-select │
│ transcription│         │           │ Clip export  │
│ Fluency      │   ┌──────▼───────┐   │ Metadata     │
│ analysis     │   │ Concatenate  │   │ Live report  │
│ Dedup/select │   └──────┬───────┘   └──────────────┘
│ Clip/report  │   ┌──────▼───────┐
└──────────────┘   │ Batch report  │
                   └──────────────┘
```

### Module List

| # | Module | File | Function | Description |
|---|--------|------|----------|-------------|
| 1 | CLI orchestration | `video_auto_editor/cli.py` | `main()`, `process_single_video()`, `process_batch()`, `process_live_video()` | 解析 `single`/`batch`/`live` 子命令并运行 Scenario A/B/C |
| 2 | Config / models | `config.py`, `models.py` | `CONFIG`, `Segment`, `ClipInfo`, `TranscriptChunk`, `ClipCandidate`, `LiveClipInfo` | 共享默认配置和数据结构 |
| 3 | Silence detection | `silence.py` | `detect_silence()`, `identify_segments()` | FFmpeg 静音检测和非静音片段切分 |
| 4 | Scoring | `scoring.py` | `score_segment()`, `analyze_fluency()`, `calculate_adjusted_score()` | 基础评分和转写文本调整分 |
| 5 | Transcription | `transcript.py` | `WhisperTranscriber`, `transcribe_candidates()`, `transcribe_video()`, `export_srt()` | Whisper CLI 封装，支持片段转写、整视频转写缓存和 SRT 导出 |
| 6 | Content dedup | `dedup.py` | `_find_duplicate_groups()`, `check_duplicate_content()`, `cross_video_dedup()`, `check_duplicate_live_candidates()` | 片内、跨视频和直播候选复用的文本相似分组 |
| 7 | Layered selection | `selection.py` | `select_best_segment()`, `select_live_clips()` | 单片段分层选择和直播多片段选择 |
| 8 | FFmpeg ops | `media.py` | `get_video_duration()`, `clip_segment()`, `concat_videos()` | 获取时长、裁剪和拼接 |
| 9 | Topic candidates | `topic.py` | `generate_clip_candidates()`, `enrich_clip_candidates()` | 直播拆条候选生成、边界校准、标题/摘要/关键词补全 |
| 10 | Live export | `export.py` | `export_live_clips()` | 批量导出直播短视频、字幕和 metadata |
| 11 | Reports | `report.py` | `generate_single_report()`, `generate_batch_report()`, `generate_live_report()` | Markdown 报告生成 |

模块入口由 `video_auto_editor/__main__.py` 委托给 `video_auto_editor.cli.main()`，支持 `python3 -m video_auto_editor ...` 执行。

---

## Data Structures

### Segment

Represents one segment (non-silent interval) in a video:

```python
@dataclass
class Segment:
    index: int                    # Segment index
    start_time: float             # Start time (seconds)
    end_time: float               # End time (seconds)
    duration: float               # Duration (seconds)

    # Base scores (4 dims × 25 pts)
    score_start: float = 0        # Clear start
    score_end: float = 0          # Clear end
    score_fluency: float = 0      # Mid fluency
    score_rhythm: float = 0       # Natural rhythm
    total_score: float = 0         # Base score total

    # Internal interruption info
    internal_silences: List[Tuple[float, float]]  # Internal silence spans
    interruption_count: int = 0                     # Interruption count
    interruption_duration: float = 0                # Total interruption duration

    # Transcription & fluency
    transcript: str = ""          # Whisper transcript
    repeat_count: int = 0         # Phrase repeat count
    stutter_count: int = 0        # Filler word count
    is_natural_end: bool = False  # Natural ending
    is_interrupted: bool = False  # Sudden interruption

    # Adjusted score & dedup
    adjusted_score: float = 0     # Adjusted score (0-100)
    is_duplicate: bool = False    # Marked as duplicate
    duplicate_with: List[int]     # Duplicate of which segments
```

### ClipInfo

Represents one video's rough-cut result, used for cross-video dedup:

```python
@dataclass
class ClipInfo:
    video_name: str               # Video filename (no extension)
    clip_path: str                # Clip output path
    transcript: str               # Best segment transcript
    adjusted_score: float         # Best segment adjusted score
    is_natural_end: bool          # Best segment natural end
    duration: float               # Best segment duration
    is_cross_duplicate: bool = False  # Marked by cross-video dedup
    duplicate_of: str = ""            # Duplicate of which video
```

### TranscriptChunk

表示整视频 Whisper 转写中的一个带时间戳文本块，主要用于 `live` 模式：

```python
@dataclass
class TranscriptChunk:
    start: float                  # 文本块开始时间（秒）
    end: float                    # 文本块结束时间（秒）
    text: str                     # 转写文本
```

### ClipCandidate

表示一个由转写窗口生成的直播拆条候选：

```python
@dataclass
class ClipCandidate:
    index: int                    # 候选序号
    start_time: float             # 候选开始时间（秒）
    end_time: float               # 候选结束时间（秒）
    duration: float               # 候选时长（秒）
    text: str                     # 候选转写文本
    source: str = "transcript_window"
    base_score: float = 0
    chunk_start_index: int = 0
    chunk_end_index: int = 0
    adjusted_score: Optional[float] = None
    title: str = ""
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
    is_duplicate: bool = False
    duplicate_with: List[int] = field(default_factory=list)
```

### LiveClipInfo

表示一条已导出的直播短视频：

```python
@dataclass
class LiveClipInfo:
    index: int
    title: str
    start_time: float
    end_time: float
    duration: float
    score: float
    text: str
    output_path: str
    subtitle_path: str = ""
    summary: str = ""
    keywords: List[str] = field(default_factory=list)
```

---

## Core Modules

### Module 1: Silence Detection

```python
def detect_silence(video_path) -> List[Tuple[float, float]]
```

Uses FFmpeg `silencedetect` filter to detect silence spans.

**Parameters**:
- `silence_noise`: Silence threshold (dB), default -30, lower = stricter
- `silence_duration`: Minimum silence length (seconds), default 0.8

**Note**: FFmpeg outputs silence detection results to stderr.

---

### Module 2: Segment Identification

```python
def identify_segments(silences, total_duration) -> List[Segment]
```

Splits video into segments by silence:
1. First segment: video start → first silence start
2. Middle segments: previous silence end → next silence start (keep if ≥1s)
3. Last segment: last silence end → video end

---

### Module 3: Scoring System

```python
def score_segment(seg, silences, total_duration) -> Segment
```

4-dimension scoring, 25 pts each, total 100:

**Clear Start (25 pts)**:

| Condition | Score |
|-----------|-------|
| Pre-silence ≥ 1.0s | 25 |
| Pre-silence ≥ 0.5s | 20 |
| Pre-silence < 0.5s | 10 |
| At video start (< 0.5s) | 15 |
| Other | 5 |

**Clear End (25 pts)**: Same logic for post-silence.

**Mid Fluency (25 pts)**:

| Internal interruptions | Score |
|------------------------|-------|
| 0 | 25 |
| 1-2 | 20 |
| 3-4 | 15 |
| 5+ | max(5, 25 - count × 3) |

**Natural Rhythm (25 pts)**: Three components:
- Pause ratio (15 pts): < 5% → 15, decreasing
- Max single pause (10 pts): < 0.8s → 10, decreasing
- Short-segment cap: < 8s cap 15, < 15s cap 20

---

### Module 4: Transcription

```python
def create_whisper_transcriber() -> WhisperTranscriber
def transcribe_candidates(video_path, candidates, work_dir, transcriber=None) -> List[Segment]
def transcribe_video(video_path, work_dir, transcriber=None, config=None) -> VideoTranscriptionResult
def export_srt(chunks, output_path) -> str
```

1. 从全局 `CONFIG` 创建 `WhisperTranscriber`。
2. 通过当前 Python 解释器检查 Whisper 是否可用。
3. 为每个候选片段使用 FFmpeg 抽取音频（16kHz、单声道、WAV）。
4. 通过 `python -m whisper` 调用 Whisper CLI 执行中文转写。
5. 如果 Whisper 不可用，或某个片段转写失败，保留空文本并继续使用纯音频评分。
6. 直播模式使用 `transcribe_video()` 对整条视频输出 Whisper JSON，解析为 `TranscriptChunk`，并按源视频 path/size/mtime 缓存到 `work_dir/transcript.json`。
7. `export_srt()` 将整视频转写或片段转写切片导出为 SRT 字幕。

---

### Module 5: Fluency Analysis

```python
def analyze_fluency(transcript) -> Tuple[int, int, bool, bool]
```

Returns `(repeat_count, stutter_count, is_natural_end, is_interrupted)`.

**Repeat detection**: Sliding window, 2-4 char phrases repeated in next 10 chars.

**Stutter detection**: Matches:
- Single filler: `[嗯啊呃]` (um, uh, etc.)
- Filler phrases: `那个`, `就是说`
- Ellipsis: `...`, `…`

**Interruption detection**: Text ends with 20 connective/incomplete markers, e.g.:
`的时候 | 然后 | 但是 | 如果 | 因为 | 而且 | 所以 | 就是 | 其实 | 那么 | 或者 | 并且 | 还是 | 不过 | 包括 | 比如说 | 另外 | 接下来 | 还有就是 | 就是说`

**Natural end detection**: Any of:
- Ends with period/exclamation/question, and not with connective
- Matches special patterns: questions ("怎么…呢？"), summaries ("就是这样"), farewells ("拜拜", "再见", etc.)

---

### Module 6: Adjusted Score

```python
def calculate_adjusted_score(seg) -> float
```

```
adjusted = base_score
         - (repeat_count / duration_factor) × 5
         - (stutter_count / duration_factor) × 3
         - (interrupted ? 10 : 0)
         + (natural_end ? 5 : 0)
         + completeness_bonus (0~3, continuous, 60s optimal)
```

`duration_factor = max(1.0, segment_duration / 30.0)` to avoid long segments accumulating penalties.

Final value clamped to 0-100.

---

### Module 7: Content Deduplication (Generic Grouping)

Within-video and cross-video dedup share `_find_duplicate_groups()`:

```python
def _find_duplicate_groups(items, get_text) -> List[Set[int]]
```

1. Pairwise compare `SequenceMatcher` similarity of `get_text(item)`
2. Group items with similarity > threshold (default 0.7)

Upper functions select best in each group and mark others:
- `check_duplicate_content(candidates)`: Within-video, rule: natural end > adjusted score > later index
- `cross_video_dedup(clips)`: Cross-video, rule: natural end > adjusted score > later filename
- `check_duplicate_live_candidates(candidates)`: Live candidates, rule: adjusted score > base score > earlier start time

---

### Module 8: Layered Selection

```python
def select_best_segment(candidates) -> Segment
```

From non-duplicate candidates, filter by priority:

| Layer | Rule | Description |
|-------|------|-------------|
| 1 | Natural end first | If any natural end, keep only those |
| 2 | Fluency sort | By stutter+repeat rate, tolerance 1.5 per 30s |
| 3 | Adjusted score | Keep highest |
| 4 | Tie-break | All incomplete → last; any complete → longest |

---

### Module 9: Live Candidate Generation

```python
def generate_clip_candidates(chunks, silences, total_duration, config=None) -> List[ClipCandidate]
def enrich_clip_candidates(candidates, config=None) -> List[ClipCandidate]
```

`generate_clip_candidates()` 是直播拆条 MVP 的候选生成器：

1. 移除空白转写块。
2. 按接近 `target_clip_duration` 的目标构建转写窗口。
3. 用 `topic_overlap_seconds` 控制相邻窗口重叠。
4. 在 `context_expand_before` / `context_expand_after` 范围内把起止点校准到附近静音边界。
5. 将时间范围限制在视频总时长内。
6. 按 `min_clip_duration` 和 `max_clip_duration` 过滤候选。
7. 将边界质量和时长质量计算为 `base_score`。

`enrich_clip_candidates()` 会补充：

- `adjusted_score`：在基础分上叠加流畅度惩罚和自然结尾奖励
- `title`：清理后的首句，用于文件名和报告
- `summary`：用于 metadata 的文本摘要
- `keywords`：基于正则和停用词过滤的简单关键词

该模块目前不做语义话题聚类。

---

### Module 10: Live Multi-Selection

```python
def select_live_clips(candidates, max_clips=None, config=None) -> List[ClipCandidate]
```

直播模式会导出多个候选片段：

1. 优先使用非重复候选；如果所有候选都被标记为重复，则回退到全部候选。
2. 按调整分、基础分、时长和候选顺序排序。
3. 跳过与已选片段重叠过多的候选；重叠比例以较短片段为基准，阈值为 20%，同时受 `min_clip_gap_seconds` 约束。
4. 达到 `max_clips` 后停止。
5. 按时间线顺序返回选中的片段。

---

### Module 11: FFmpeg Operations

```python
def clip_segment(video_path, seg, output_path) -> bool
def concat_videos(clip_paths, output_path) -> bool
```

- `clip_segment`: Clip target segment with buffer, H.264 CRF 18 + AAC 192kbps
- `concat_videos`: FFmpeg concat protocol, re-encode for format consistency

---

### Module 12: Live Export

```python
def export_live_clips(video_path, selected, chunks, output_dir, config=None) -> Optional[List[LiveClipInfo]]
```

直播导出会写入：

- `clips/<index>_<title>.mp4`
- `subtitles/<index>_<title>.srt` when `export_subtitles=True`
- `metadata.json`

如果任一片段导出失败，本次调用已写入的直播输出文件会被清理，并返回 `None`。

---

## API Reference

### Scenario A

```python
def process_single_video(video_path: str, output_dir: str, work_dir: str, batch_mode: bool = False) -> Optional[ClipInfo]
```

Full pipeline (steps 1-10) for one video. Returns `ClipInfo` for Scenario B, or `None` on failure.

`batch_mode=True` skips individual reports (Scenario B generates one).

### Scenario B

```python
def process_batch(input_dir: str, output_dir: str, work_dir: str) -> None
```

Batch flow:
1. Scan input dir for `.MTS`/`.mp4`/`.mov`, sort by filename
2. Call `process_single_video(batch_mode=True)` per video
3. Call `cross_video_dedup`
4. Call `concat_videos` for kept clips
5. **Clean intermediate files** (individual clips + temp audio)
6. Generate single batch report (with transcript summaries)

Final output: concatenated video + batch report only.

### Scenario C

```python
def process_live_video(video_path: str, output_dir: str, work_dir: str, config: Optional[dict] = None) -> Optional[List[LiveClipInfo]]
```

直播拆条流程：

1. 通过 `ffprobe` 获取视频时长。
2. 通过 FFmpeg `silencedetect` 检测静音区间。
3. 用 Whisper JSON 输出转写整条视频，或复用 `work_dir/<video_name>/transcript.json`。
4. 将整视频转写导出为 `output_dir/transcript.srt`。
5. 基于转写块生成带时间戳的候选窗口。
6. 为候选补充直播分数、标题、摘要和关键词。
7. 基于文本相似度标记重复候选。
8. 选择最多 `max_clips` 条不明显重叠的片段。
9. 导出短视频、单片段字幕、`metadata.json` 和 `拆条报告.md`。

当前输出结构：

```text
output/
├── clips/
├── subtitles/
├── transcript.srt
├── metadata.json
└── 拆条报告.md
```

### Main Entry

```python
def main() -> None
```

Parses explicit module CLI commands:

```bash
python3 -m video_auto_editor single <video_path> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor batch <input_dir> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor live <video_path> [--output-dir ./output] [--work-dir ./video_work] [--max-clips 5]
```

Missing subcommands or invalid arguments are handled by `argparse`.

---

## Extension Guide

### Add New Scoring Dimension

1. Add field to `Segment`:

```python
score_content: float = 0  # Content quality (20 pts)
```

2. Add scoring logic in `score_segment()`
3. Update `total_score` calculation
4. Note: New dimension may exceed 100 total; adjust `min_score` or normalize

### Add New Fluency Detection

1. Add detection in `analyze_fluency()` (e.g., speech rate)
2. Add corresponding penalty/bonus in `calculate_adjusted_score()`

### Add New Natural End Pattern

Append regex to `special_natural_patterns` in `analyze_fluency()`.

### Add New Video Format

Add new extension to `glob.glob` in `process_batch()` (e.g., `*.avi`).

### Improve Live Topic Segmentation

当前 `live` 模式使用固定目标时长的转写窗口。如果要加入真正的话题分段：

1. 在 `topic.py` 中新增话题边界检测器。
2. 保持 `ClipCandidate` 作为话题检测和选择逻辑之间的数据边界。
3. 保留现有时长过滤和静音边界校准逻辑。
4. 在 `check_duplicate_live_candidates()` 背后接入语义去重，不直接改变选择器行为。
5. 保持 `export_live_clips()` 的输入不变，让导出和报告代码稳定复用。

---

**Version**: v4.7 | **Last Updated**: 2026-06-19
