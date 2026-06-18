"""片段评分与转写文本流畅度分析。"""

import re

from video_auto_editor.config import CONFIG


def _score_boundary(silences, time_point, total_duration, is_start):
    """为片段起止边界清晰度打分，返回 0-25。"""
    if is_start:
        nearby = [s for s in silences if abs(s[1] - time_point) < 0.1]
    else:
        nearby = [s for s in silences if abs(s[0] - time_point) < 0.1]

    if nearby:
        dur = nearby[0][1] - nearby[0][0]
        if dur >= 1.0:
            return 25
        if dur >= 0.5:
            return 20
        return 10

    if is_start and time_point < 0.5:
        return 15
    if not is_start and abs(time_point - total_duration) < 0.5:
        return 15
    return 5


def score_segment(seg, silences, total_duration):
    """四维基础评分：清晰开头、清晰结尾、中段流畅、自然节奏。"""
    seg.score_start = _score_boundary(silences, seg.start_time, total_duration, is_start=True)
    seg.score_end = _score_boundary(silences, seg.end_time, total_duration, is_start=False)

    internal = [
        s for s in silences
        if s[0] > seg.start_time + 0.1 and s[1] < seg.end_time - 0.1
    ]
    seg.internal_silences = internal
    seg.interruption_count = len(internal)
    seg.interruption_duration = sum(e - s for s, e in internal)

    if seg.interruption_count == 0:
        seg.score_fluency = 25
    elif seg.interruption_count <= 2:
        seg.score_fluency = 20
    elif seg.interruption_count <= 4:
        seg.score_fluency = 15
    else:
        seg.score_fluency = max(5, 25 - seg.interruption_count * 3)

    score_rhythm = 0
    if seg.duration > 0:
        ratio = seg.interruption_duration / seg.duration
        score_rhythm += 15 if ratio < 0.05 else 12 if ratio < 0.10 else 8 if ratio < 0.20 else 4

        max_pause = max((e - s for s, e in internal), default=0)
        score_rhythm += 10 if max_pause < 0.8 else 7 if max_pause < 1.5 else 4 if max_pause < 2.5 else 0

        if seg.duration < 8:
            score_rhythm = min(score_rhythm, 15)
        elif seg.duration < 15:
            score_rhythm = min(score_rhythm, 20)

    seg.score_rhythm = score_rhythm
    seg.total_score = seg.score_start + seg.score_end + seg.score_fluency + seg.score_rhythm
    return seg


def analyze_fluency(transcript):
    """分析转写文本，返回重复、口头禅、自然结尾和中断状态。"""
    if not transcript:
        return 0, 0, False, False

    text = re.sub(r"(?i)\bwhisper\b", "", transcript.strip()).strip()

    text_clean = re.sub(r"[^\w]", "", text)
    repeat_count, i, window = 0, 0, 10
    while i < len(text_clean) - 2:
        found = False
        for length in [4, 3, 2]:
            if i + length > len(text_clean):
                continue
            chunk = text_clean[i:i + length]
            area = text_clean[i + length:i + length + window]
            if chunk in area:
                repeat_count += 1
                i += length + area.index(chunk) + length
                found = True
                break
        if not found:
            i += 1

    stutter_count = sum(
        len(re.findall(pattern, text))
        for pattern in [r"[嗯啊呃]", r"那个", r"就是说", r"\.{2,}", r"…"]
    )

    interrupt_re = (
        r"(的时候|然后|但是|如果|因为|而且|所以|就是|其实|那么|或者|并且|还是|不过|包括|"
        r"比如说|另外|接下来|还有就是|就是说)$"
    )
    is_interrupted = bool(re.search(interrupt_re, text))

    has_punctuation = bool(re.search(r"[。！？]$", text))
    is_connective_end = bool(re.search(interrupt_re, text))
    special_natural_patterns = [
        r"怎么[^。！？]*[呢？]$", r"什么[^。！？]*[呢？]$", r"为什么[^。！？]*[呢？]$",
        r"就是这样[。！？]*$", r"其实有很多[的。]*$",
        r"拜拜[^\w]*$", r"再见[^\w]*$", r"今天就到这[^\w]*$",
        r"分享给大家[^\w]*$", r"希望对你[也]*有帮助[^\w]*$",
    ]
    is_natural_end = (
        has_punctuation and not is_connective_end
    ) or any(re.search(pattern, text) for pattern in special_natural_patterns)
    if is_interrupted:
        is_natural_end = False

    return repeat_count, stutter_count, is_natural_end, is_interrupted


def calculate_adjusted_score(seg, config=None):
    """基于转写分析结果计算调整分，并限制在 0-100。"""
    config = config or CONFIG
    adjusted = seg.total_score
    duration_factor = max(1.0, seg.duration / 30.0)
    adjusted -= (seg.repeat_count / duration_factor) * config["penalty_repeat"]
    adjusted -= (seg.stutter_count / duration_factor) * config["penalty_stutter"]
    if seg.is_interrupted:
        adjusted -= config["penalty_interrupt"]
    if seg.is_natural_end:
        adjusted += config["bonus_natural_end"]
    if seg.is_natural_end and not seg.is_interrupted:
        adjusted += max(0, config["bonus_completeness_max"] * (1 - abs(seg.duration - 60) / 60))
    return max(0, min(100, adjusted))
