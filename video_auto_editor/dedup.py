"""片段和跨视频内容去重。"""

import difflib

from video_auto_editor.config import CONFIG


def _find_duplicate_groups(items, get_text, config=None):
    """按文本相似度把元素分组。"""
    config = config or CONFIG
    groups = []
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            t1, t2 = get_text(items[i]), get_text(items[j])
            if not t1 or not t2:
                continue
            if difflib.SequenceMatcher(None, t1, t2).ratio() > config["duplicate_threshold"]:
                merged = False
                for group in groups:
                    if i in group or j in group:
                        group.update([i, j])
                        merged = True
                        break
                if not merged:
                    groups.append({i, j})
    return groups


def check_duplicate_content(candidates, config=None):
    """片内去重：每组保留自然结尾、调整分、较晚 index 更优的片段。"""
    groups = _find_duplicate_groups(candidates, lambda s: s.transcript, config)
    for group in groups:
        for i, j in [(i, j) for i in group for j in group if i < j]:
            print(f"    ⚠️  segment_{candidates[i].index} and segment_{candidates[j].index} content similar")
        best = max(
            group,
            key=lambda idx: (
                candidates[idx].is_natural_end,
                candidates[idx].adjusted_score,
                candidates[idx].index,
            ),
        )
        for idx in group:
            if idx != best:
                candidates[idx].is_duplicate = True
                candidates[idx].duplicate_with.append(candidates[best].index)
    return candidates


def cross_video_dedup(clips, config=None):
    """跨视频去重：每组保留自然结尾、调整分、较晚文件名更优的片段。"""
    if len(clips) < 2:
        return clips
    groups = _find_duplicate_groups(clips, lambda c: c.transcript, config)
    for group in groups:
        for i, j in [(i, j) for i in group for j in group if i < j]:
            print(f"    ⚠️  {clips[i].video_name} and {clips[j].video_name} content similar")
        best = max(
            group,
            key=lambda idx: (
                clips[idx].is_natural_end,
                clips[idx].adjusted_score,
                clips[idx].video_name,
            ),
        )
        for idx in group:
            if idx != best:
                clips[idx].is_cross_duplicate = True
                clips[idx].duplicate_of = clips[best].video_name
    return clips


def check_duplicate_live_candidates(candidates, config=None):
    """直播候选去重：每组保留 live 分数最高、边界分最高、时间更早的片段。"""
    groups = _find_duplicate_groups(candidates, lambda candidate: candidate.text, config)
    for group in groups:
        best = max(
            group,
            key=lambda idx: (
                candidates[idx].adjusted_score if candidates[idx].adjusted_score is not None else candidates[idx].base_score,
                candidates[idx].base_score,
                -candidates[idx].start_time,
            ),
        )
        for idx in group:
            if idx != best:
                candidates[idx].is_duplicate = True
                candidates[idx].duplicate_with.append(candidates[best].index)
    return candidates
