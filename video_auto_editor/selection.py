"""候选片段选择策略。"""

from video_auto_editor.config import CONFIG


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
    max_clips = int(max_clips if max_clips is not None else config["max_clips"])
    if max_clips <= 0:
        return []

    pool = [candidate for candidate in candidates if not candidate.is_duplicate]
    if not pool:
        pool = list(candidates)

    selected = []
    for candidate in sorted(pool, key=_live_score_key, reverse=True):
        if any(_has_live_overlap(candidate, kept, config) for kept in selected):
            continue
        selected.append(candidate)
        if len(selected) >= max_clips:
            break

    return sorted(selected, key=lambda candidate: (candidate.start_time, candidate.end_time, candidate.index))


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
