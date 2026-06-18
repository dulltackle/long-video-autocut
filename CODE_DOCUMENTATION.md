# Video Auto Editor v4.7 - Technical Documentation

**Version**: v4.7
**Last Updated**: 2026-03-11

> This document is for developers. It describes the system architecture, module implementations, data structures, and extension guidelines. The system is designed for **single-person / talking-to-camera (A'Roll) content**; segmentation and fluency analysis assume monologue-style speech. 

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
│  - Require explicit subcommand: single or batch  │
└────────────────┬────────────────────────────────┘
                 │
        ┌────────┴────────┐
        │                 │
   Scenario A: Single   Scenario B: Batch
        │                 │
        ▼                 ▼
┌──────────────┐   ┌──────────────┐
│ Silence      │   │ Run Scenario A│
│ detection    │   │ per video     │
│ Segment      │   └──────┬───────┘
│ identification│         │
│ 4-dim scoring│   ┌──────▼───────┐
│ Candidate    │   │ Cross-video   │
│ filtering    │   │ deduplication │
│ Whisper      │   └──────┬───────┘
│ transcription│         │
│ Fluency      │   ┌──────▼───────┐
│ analysis     │   │ Concatenate  │
│ Within-video │   └──────┬───────┘
│ dedup        │         │
│ Layered      │   ┌──────▼───────┐
│ selection    │   │ Batch report  │
│ Clip output  │   └──────────────┘
│ Report       │
└──────────────┘
```

### Module List

| # | Module | File | Function | Description |
|---|--------|------|----------|-------------|
| 1 | CLI orchestration | `video_auto_editor/cli.py` | `main()`, `process_single_video()`, `process_batch()` | 解析 `single`/`batch` 子命令并运行 Scenario A/B |
| 2 | Config / models | `config.py`, `models.py` | `CONFIG`, `Segment`, `ClipInfo` | 共享默认配置和数据结构 |
| 3 | Silence detection | `silence.py` | `detect_silence()`, `identify_segments()` | FFmpeg 静音检测和非静音片段切分 |
| 4 | Scoring | `scoring.py` | `score_segment()`, `analyze_fluency()`, `calculate_adjusted_score()` | 基础评分和转写文本调整分 |
| 5 | Transcription | `transcript.py` | `WhisperTranscriber`, `transcribe_candidates()` | Whisper CLI 封装，支持失败降级 |
| 6 | Content dedup | `dedup.py` | `_find_duplicate_groups()`, `check_duplicate_content()`, `cross_video_dedup()` | 片内和跨视频复用的文本相似分组 |
| 7 | Layered selection | `selection.py` | `select_best_segment()` | 自然结尾 → 流畅度 → 调整分 → 时长/顺序 |
| 8 | FFmpeg ops | `media.py` | `get_video_duration()`, `clip_segment()`, `concat_videos()` | 获取时长、裁剪和拼接 |
| 9 | Reports | `report.py` | `generate_single_report()`, `generate_batch_report()` | Markdown 报告生成 |

模块入口由 `video_auto_editor/__main__.py` 委托给 `video_auto_editor.cli.main()`，支持 `python -m video_auto_editor ...` 执行。

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
```

1. 从全局 `CONFIG` 创建 `WhisperTranscriber`。
2. 通过当前 Python 解释器检查 Whisper 是否可用。
3. 为每个候选片段使用 FFmpeg 抽取音频（16kHz、单声道、WAV）。
4. 通过 `python -m whisper` 调用 Whisper CLI 执行中文转写。
5. 如果 Whisper 不可用，或某个片段转写失败，保留空文本并继续使用纯音频评分。

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

### Module 9: FFmpeg Operations

```python
def clip_segment(video_path, seg, output_path) -> bool
def concat_videos(clip_paths, output_path) -> bool
```

- `clip_segment`: Clip target segment with buffer, H.264 CRF 18 + AAC 192kbps
- `concat_videos`: FFmpeg concat protocol, re-encode for format consistency

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

### Main Entry

```python
def main() -> None
```

Parses explicit module CLI commands:

```bash
python3 -m video_auto_editor single <video_path> [--output-dir ./output] [--work-dir ./video_work]
python3 -m video_auto_editor batch <input_dir> [--output-dir ./output] [--work-dir ./video_work]
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

---

**Version**: v4.7 | **Last Updated**: 2026-03-11
