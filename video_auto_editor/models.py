"""视频粗剪流程使用的数据结构。"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class TranscriptChunk:
    """整视频转写中的一个带时间戳文本块。"""

    start: float
    end: float
    text: str


@dataclass
class TopicReviewResult:
    """主题评审返回的结构化结果。"""

    topic_name: str
    topic_complete: bool
    learning_value: int
    share_value: int
    publish_ready_score: int
    export_decision: str
    title: str
    summary: str
    keywords: List[str] = field(default_factory=list)
    needs_human_review: bool = False
    reject_reason: str = ""
    boundary_fix_suggestion: str = ""
    boundary_fix_start: Optional[float] = None
    boundary_fix_end: Optional[float] = None


@dataclass
class LiveExportDecision:
    """直播候选的机器可读导出选择结果。"""

    candidate_index: int
    selected_for_export: bool
    decision: str
    reason: str
    review_status: str
    publish_ready_score: Optional[int] = None
    export_rank: Optional[int] = None
    original_start: float = 0.0
    original_end: float = 0.0
    final_start: float = 0.0
    final_end: float = 0.0
    topic_name: str = ""
    needs_human_review: bool = False
    boundary_fix_suggestion: str = ""
    boundary_fix_applied: bool = False
    series_key: str = ""


@dataclass
class ClipCandidate:
    """直播拆条流程中的候选片段。"""

    index: int
    start_time: float
    end_time: float
    duration: float
    text: str
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
    review: Optional[TopicReviewResult] = None
    export_selection: Optional[LiveExportDecision] = None


@dataclass
class LiveClipInfo:
    """直播拆条导出的单条短视频信息。"""

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
    topic_name: str = ""
    publish_ready_score: Optional[int] = None
    export_decision: str = ""
    decision_reason: str = ""
    original_start: float = 0.0
    original_end: float = 0.0
    final_start: float = 0.0
    final_end: float = 0.0
    boundary_fix_applied: bool = False
    boundary_fix_suggestion: str = ""
    series_key: str = ""
    needs_human_review: bool = False


@dataclass
class Segment:
    """视频中的一段非静音区间。"""

    index: int
    start_time: float
    end_time: float
    duration: float
    score_start: float = 0
    score_end: float = 0
    score_fluency: float = 0
    score_rhythm: float = 0
    total_score: float = 0
    internal_silences: List[Tuple[float, float]] = field(default_factory=list)
    interruption_count: int = 0
    interruption_duration: float = 0
    transcript: str = ""
    repeat_count: int = 0
    stutter_count: int = 0
    is_natural_end: bool = False
    is_interrupted: bool = False
    adjusted_score: float = 0
    is_duplicate: bool = False
    duplicate_with: List[int] = field(default_factory=list)


@dataclass
class ClipInfo:
    """单条视频粗剪结果，用于批处理阶段跨视频去重。"""

    video_name: str
    clip_path: str
    transcript: str
    adjusted_score: float
    is_natural_end: bool
    duration: float
    is_cross_duplicate: bool = False
    duplicate_of: str = ""
