"""直播主题评审输入构造。"""

from dataclasses import dataclass, field
from typing import List

from video_auto_editor.config import CONFIG


@dataclass
class TopicReviewCandidateInput:
    """单个候选提交给主题评审模型的稳定输入。"""

    candidate_id: str
    candidate_index: int
    start_time: float
    end_time: float
    duration: float
    title: str
    summary: str
    keywords: List[str]
    text: str
    previous_candidate: dict | None = None
    next_candidate: dict | None = None

    def to_payload(self):
        payload = {
            "candidate_id": self.candidate_id,
            "candidate_index": self.candidate_index,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "duration": self.duration,
            "title": self.title,
            "summary": self.summary,
            "keywords": list(self.keywords),
            "text": self.text,
        }
        if self.previous_candidate is not None:
            payload["previous_candidate"] = dict(self.previous_candidate)
        if self.next_candidate is not None:
            payload["next_candidate"] = dict(self.next_candidate)
        return payload


@dataclass
class TopicReviewBatch:
    """一次主题评审请求中的候选批次。"""

    batch_index: int
    course_context_summary: dict = field(default_factory=dict)
    candidates: List[TopicReviewCandidateInput] = field(default_factory=list)

    def to_payload(self):
        return {
            "batch_index": self.batch_index,
            "course_context_summary": dict(self.course_context_summary),
            "candidates": [candidate.to_payload() for candidate in self.candidates],
        }


def build_topic_review_batches(candidates, course_context=None, config=None):
    """按候选时间顺序构造相邻上下文评审批次。"""
    if not candidates:
        return []

    config = config or CONFIG
    batch_size = max(1, int(config.get("topic_review_batch_size", 1)))
    ordered = sorted(candidates, key=lambda candidate: (candidate.start_time, candidate.end_time, candidate.index))
    context_summary = course_context.summary() if course_context is not None else {}

    review_inputs = [
        _candidate_input(candidate, ordered[index - 1] if index > 0 else None, ordered[index + 1] if index + 1 < len(ordered) else None)
        for index, candidate in enumerate(ordered)
    ]

    return [
        TopicReviewBatch(
            batch_index=batch_index,
            course_context_summary=context_summary,
            candidates=review_inputs[start:start + batch_size],
        )
        for batch_index, start in enumerate(range(0, len(review_inputs), batch_size))
    ]


def _candidate_input(candidate, previous_candidate, next_candidate):
    return TopicReviewCandidateInput(
        candidate_id=_candidate_id(candidate),
        candidate_index=candidate.index,
        start_time=candidate.start_time,
        end_time=candidate.end_time,
        duration=candidate.duration,
        title=candidate.title,
        summary=candidate.summary,
        keywords=list(candidate.keywords),
        text=candidate.text,
        previous_candidate=_neighbor_summary(previous_candidate),
        next_candidate=_neighbor_summary(next_candidate),
    )


def _neighbor_summary(candidate):
    if candidate is None:
        return None
    return {
        "candidate_id": _candidate_id(candidate),
        "candidate_index": candidate.index,
        "start_time": candidate.start_time,
        "end_time": candidate.end_time,
        "duration": candidate.duration,
        "title": candidate.title,
        "summary": candidate.summary,
        "keywords": list(candidate.keywords),
    }


def _candidate_id(candidate):
    return f"candidate_{candidate.index}"
