"""直播候选内容去重。"""

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
