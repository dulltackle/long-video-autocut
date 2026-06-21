"""候选片段选择策略。"""

import hashlib
import logging

from video_auto_editor.config import CONFIG
from video_auto_editor.models import LiveExportDecision

logger = logging.getLogger(__name__)


def _fluency_rate(seg):
    return (seg.stutter_count + seg.repeat_count) / max(1.0, seg.duration / 30.0)


def select_best_segment(candidates):
    """分层选择最佳片段：自然结尾、流畅度、调整分、时长或顺序。"""
    if not candidates:
        return None

    pool = [c for c in candidates if not c.is_duplicate] or list(candidates)
    all_unnatural = not any(s.is_natural_end for s in pool)

    natural_end = [s for s in pool if s.is_natural_end]
    if natural_end:
        pool = natural_end

    pool.sort(key=_fluency_rate)
    best_rate = _fluency_rate(pool[0])
    pool = [s for s in pool if _fluency_rate(s) - best_rate <= 1.5]

    pool.sort(key=lambda s: s.adjusted_score, reverse=True)
    pool = [s for s in pool if s.adjusted_score == pool[0].adjusted_score]

    if len(pool) > 1:
        if all_unnatural:
            pool.sort(key=lambda s: s.index, reverse=True)
        else:
            pool.sort(key=lambda s: s.duration, reverse=True)

    return pool[0]


def select_live_clips(candidates, max_clips=None, config=None):
    """选择多条直播短视频候选：先按质量选，再按时间顺序输出。"""
    if not candidates:
        return []

    config = config or CONFIG
    max_clips = resolve_live_max_clips(max_clips, config)
    if max_clips is not None and max_clips <= 0:
        return []

    pool = [candidate for candidate in candidates if not candidate.is_duplicate]
    if not pool:
        pool = list(candidates)

    selected = []
    for candidate in sorted(pool, key=_live_score_key, reverse=True):
        if any(_has_live_overlap(candidate, kept, config) for kept in selected):
            continue
        selected.append(candidate)
        if max_clips is not None and len(selected) >= max_clips:
            break

    return sorted(selected, key=lambda candidate: (candidate.start_time, candidate.end_time, candidate.index))


def select_live_exports(candidates, max_clips=None, config=None, review_status="unreviewed"):
    """按导出契约返回最终导出候选和每个候选的选择结果。"""
    config = config or CONFIG
    if review_status == "reviewed":
        return _select_reviewed_live_exports(candidates, max_clips, config)
    if config.get("allow_unreviewed_export", False):
        selected = select_live_clips(candidates, max_clips, config)
        decisions = _build_unreviewed_decisions(candidates, selected, review_status, "legacy_score_not_selected")
        return selected, decisions
    decisions = _build_unreviewed_decisions(candidates, [], review_status, "unreviewed_export_not_allowed")
    return [], decisions


def resolve_live_max_clips(max_clips=None, config=None):
    """解析 live 最大导出数量，区分用户显式上限和临时保护上限。"""
    config = config or CONFIG
    if max_clips is not None:
        return int(max_clips)
    if config.get("max_clips_user_provided"):
        return int(config["max_clips"])
    return None


def _select_reviewed_live_exports(candidates, max_clips, config):
    max_clips = resolve_live_max_clips(max_clips, config)
    if max_clips is not None and max_clips <= 0:
        eligible = []
    else:
        eligible = [
            candidate
            for candidate in candidates
            if _reviewed_rejection_reason(candidate, config) is None
        ]
        eligible.sort(key=_reviewed_quality_key, reverse=True)
        if max_clips is not None:
            eligible = eligible[:max_clips]

    selected = sorted(eligible, key=lambda candidate: (candidate.start_time, candidate.end_time, candidate.index))
    selected_indexes = {candidate.index for candidate in selected}
    rank_by_index = {candidate.index: index for index, candidate in enumerate(selected, 1)}
    decisions = []
    for candidate in candidates:
        rejection_reason = _reviewed_rejection_reason(candidate, config)
        if candidate.index in selected_indexes:
            original_start = candidate.start_time
            original_end = candidate.end_time
            _apply_final_boundary(candidate)
            decision = _decision_payload(
                candidate,
                True,
                "export",
                "publish_ready",
                "reviewed",
                export_rank=rank_by_index[candidate.index],
                original_start=original_start,
                original_end=original_end,
            )
        elif rejection_reason is None:
            decision = _decision_payload(candidate, False, "skip", "max_clips_limit", "reviewed")
        else:
            decision = _decision_payload(candidate, False, "skip", rejection_reason, "reviewed")
        candidate.export_selection = decision
        decisions.append(decision)
    return selected, decisions


def _build_unreviewed_decisions(candidates, selected, review_status, default_skip_reason):
    selected_indexes = {candidate.index for candidate in selected}
    rank_by_index = {candidate.index: index for index, candidate in enumerate(selected, 1)}
    decisions = []
    for candidate in candidates:
        if candidate.index in selected_indexes:
            decision = _decision_payload(
                candidate,
                True,
                "export",
                "legacy_score_selection",
                review_status,
                export_rank=rank_by_index[candidate.index],
            )
        elif candidate.is_duplicate:
            decision = _decision_payload(candidate, False, "skip", "duplicate", review_status)
        else:
            decision = _decision_payload(candidate, False, "skip", default_skip_reason, review_status)
        candidate.export_selection = decision
        decisions.append(decision)
    return decisions


def _reviewed_rejection_reason(candidate, config):
    review = candidate.review
    if candidate.is_duplicate:
        return "duplicate"
    if review is None:
        return "missing_review"
    if review.needs_human_review:
        return "needs_human_review"
    if review.boundary_fix_suggestion and not _has_explicit_boundary_fix(review):
        return "boundary_fix_needs_human_review"
    threshold = int(config.get("topic_review_publish_ready_threshold", 80))
    if int(review.publish_ready_score) < threshold:
        return "publish_ready_score_below_threshold"
    if not review.topic_complete:
        return "topic_incomplete"
    if review.export_decision != "publish_ready":
        return f"review_decision_{review.export_decision or 'unknown'}"
    return None


def _reviewed_quality_key(candidate):
    review = candidate.review
    ready_score = int(review.publish_ready_score) if review is not None else -1
    live_score = candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score
    return (ready_score, live_score, candidate.base_score, candidate.duration, -candidate.index)


def _apply_final_boundary(candidate):
    review = candidate.review
    if review is None or not _has_explicit_boundary_fix(review):
        return
    final_start = float(review.boundary_fix_start)
    final_end = float(review.boundary_fix_end)
    if final_start < 0 or final_end <= final_start:
        logger.warning(
            "忽略候选 %s 的非法边界修复：boundary_fix_start=%s, boundary_fix_end=%s",
            candidate.index,
            review.boundary_fix_start,
            review.boundary_fix_end,
        )
        return
    candidate.start_time = final_start
    candidate.end_time = final_end
    candidate.duration = final_end - final_start


def _has_explicit_boundary_fix(review):
    return review.boundary_fix_start is not None and review.boundary_fix_end is not None


def _decision_payload(
    candidate,
    selected,
    decision,
    reason,
    review_status,
    export_rank=None,
    original_start=None,
    original_end=None,
):
    review = candidate.review
    boundary_fix_applied = bool(review is not None and _has_explicit_boundary_fix(review) and selected)
    topic_name = review.topic_name if review is not None else ""
    return LiveExportDecision(
        candidate_index=candidate.index,
        selected_for_export=selected,
        decision=decision,
        reason=reason,
        review_status=review_status,
        publish_ready_score=int(review.publish_ready_score) if review is not None else None,
        export_rank=export_rank,
        original_start=candidate.start_time if original_start is None else original_start,
        original_end=candidate.end_time if original_end is None else original_end,
        final_start=candidate.start_time,
        final_end=candidate.end_time,
        topic_name=topic_name,
        needs_human_review=bool(review.needs_human_review) if review is not None else False,
        boundary_fix_suggestion=review.boundary_fix_suggestion if review is not None else "",
        boundary_fix_applied=boundary_fix_applied,
        series_key=_series_key(topic_name),
    )


def _series_key(topic_name):
    normalized = str(topic_name or "").strip()
    if not normalized:
        return ""
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"topic-{digest}"


def _live_score_key(candidate):
    score = candidate.adjusted_score if candidate.adjusted_score is not None else candidate.base_score
    return (score, candidate.base_score, candidate.duration, -candidate.index)


def _has_live_overlap(left, right, config):
    gap = float(config.get("min_clip_gap_seconds", 0))
    if left.end_time + gap <= right.start_time or right.end_time + gap <= left.start_time:
        return False

    overlap = min(left.end_time, right.end_time) - max(left.start_time, right.start_time)
    if overlap <= 0:
        return True

    shorter = max(0.001, min(left.duration, right.duration))
    return overlap / shorter >= 0.2
