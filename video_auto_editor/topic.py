"""直播拆条候选片段生成。"""

import re

from video_auto_editor.config import CONFIG
from video_auto_editor.models import ClipCandidate
from video_auto_editor.scoring import analyze_fluency


def generate_clip_candidates(chunks, silences, total_duration, config=None):
    """基于转写时间戳滑窗，并用静音边界校准候选片段。"""
    config = config or CONFIG
    clean_silences = _normalize_silences(silences, total_duration)
    clean_chunks = [
        (index, chunk)
        for index, chunk in enumerate(chunks)
        if _normalize_text(chunk.text)
    ]
    if not clean_chunks:
        return []

    target_duration = float(config["target_clip_duration"])
    overlap = float(config["topic_overlap_seconds"])
    min_duration = float(config["min_clip_duration"])
    max_duration = float(config["max_clip_duration"])

    candidates = []
    start_pos = 0
    candidate_index = 0

    while start_pos < len(clean_chunks):
        window = _build_window(clean_chunks, start_pos, target_duration)
        if not window:
            break

        first_original_index, first_chunk = window[0]
        last_original_index, last_chunk = window[-1]
        raw_start = float(first_chunk.start)
        raw_end = float(last_chunk.end)
        start_time, start_adjusted = _adjust_start(raw_start, clean_silences, config)
        end_time, end_adjusted = _adjust_end(raw_end, clean_silences, total_duration, config)
        start_time = _clamp(start_time, 0.0, total_duration)
        end_time = _clamp(end_time, 0.0, total_duration)
        duration = end_time - start_time

        if duration > 0 and min_duration <= duration <= max_duration:
            text = _normalize_text(" ".join(chunk.text for _, chunk in window))
            candidates.append(
                ClipCandidate(
                    index=candidate_index,
                    start_time=start_time,
                    end_time=end_time,
                    duration=duration,
                    text=text,
                    base_score=_score_candidate(duration, target_duration, start_adjusted, end_adjusted),
                    chunk_start_index=first_original_index,
                    chunk_end_index=last_original_index,
                )
            )
            candidate_index += 1

        next_pos = _next_window_start(clean_chunks, start_pos, raw_end, overlap)
        last_window_pos = start_pos + len(window) - 1
        if next_pos == last_window_pos and last_window_pos == len(clean_chunks) - 1:
            break
        if next_pos <= start_pos:
            next_pos = start_pos + 1
        start_pos = next_pos

    return candidates


def enrich_clip_candidates(candidates, config=None):
    """为直播候选补充分数、标题、摘要和关键词。"""
    config = config or CONFIG
    for candidate in candidates:
        candidate.adjusted_score = _score_live_candidate(candidate, config)
        candidate.title = _generate_title(candidate)
        candidate.summary = _generate_summary(candidate.text)
        candidate.keywords = _extract_keywords(candidate.text)
    return candidates


def _score_live_candidate(candidate, config):
    repeat_count, stutter_count, is_natural_end, is_interrupted = analyze_fluency(candidate.text)
    duration_factor = max(1.0, candidate.duration / 30.0)
    score = candidate.base_score
    score -= (repeat_count / duration_factor) * config["penalty_repeat"]
    score -= (stutter_count / duration_factor) * config["penalty_stutter"]
    if is_interrupted:
        score -= config["penalty_interrupt"]
    if is_natural_end:
        score += config["bonus_natural_end"]
    return round(_clamp(score, 0, 100), 1)


def _generate_title(candidate):
    text = _normalize_text(candidate.text)
    sentence = _first_sentence(text)
    sentence = re.sub(r"^[嗯啊呃那个就是说\s，,。！？!?.]+", "", sentence).strip()
    sentence = re.sub(r"[。！？!?,，；;：:\s]+$", "", sentence).strip()
    if sentence:
        return sentence[:18]
    return f"直播片段_{candidate.index + 1:03d}"


def _generate_summary(text):
    text = _normalize_text(text)
    if not text:
        return "直播片段摘要待补充。"
    return text[:80]


def _extract_keywords(text):
    text = _normalize_text(text)
    stopwords = {
        "这个", "那个", "就是", "然后", "其实", "我们", "你们", "大家", "一个", "一些",
        "因为", "所以", "但是", "如果", "可以", "这样", "来说", "里面", "时候",
    }
    words = []
    for match in re.finditer(r"[\u4e00-\u9fff]{2,8}|[A-Za-z][A-Za-z0-9_-]{1,20}", text):
        word = match.group(0)
        if word in stopwords:
            continue
        if word not in words:
            words.append(word)
        if len(words) >= 5:
            break
    return words or ["直播片段"]


def _first_sentence(text):
    parts = re.split(r"[。！？!?]", text, maxsplit=1)
    return parts[0] if parts else text


def _build_window(clean_chunks, start_pos, target_duration):
    window = []
    start_time = float(clean_chunks[start_pos][1].start)
    end_time = start_time

    for item in clean_chunks[start_pos:]:
        window.append(item)
        end_time = float(item[1].end)
        if end_time - start_time >= target_duration:
            break

    return window


def _next_window_start(clean_chunks, start_pos, raw_end, overlap):
    next_start_time = max(float(clean_chunks[start_pos][1].start), raw_end - overlap)
    for pos in range(start_pos + 1, len(clean_chunks)):
        if float(clean_chunks[pos][1].end) > next_start_time:
            return pos
    return len(clean_chunks)


def _adjust_start(raw_start, silences, config):
    expand_before = float(config["context_expand_before"])
    lower_bound = raw_start - expand_before
    candidates = [
        silence_end
        for _, silence_end in silences
        if lower_bound <= silence_end <= raw_start
    ]
    if not candidates:
        return raw_start, False
    return max(candidates), True


def _adjust_end(raw_end, silences, total_duration, config):
    expand_after = float(config["context_expand_after"])
    upper_bound = min(raw_end + expand_after, total_duration)
    candidates = [
        silence_start
        for silence_start, _ in silences
        if raw_end <= silence_start <= upper_bound
    ]
    if not candidates:
        return raw_end, False
    return min(candidates), True


def _normalize_silences(silences, total_duration):
    normalized = []
    for silence_start, silence_end in silences:
        start = _clamp(float(silence_start), 0.0, total_duration)
        end = _clamp(float(silence_end), 0.0, total_duration)
        if start < end:
            normalized.append((start, end))
    return sorted(normalized)


def _score_candidate(duration, target_duration, start_adjusted, end_adjusted):
    if target_duration <= 0:
        duration_score = 80
    else:
        distance_ratio = abs(duration - target_duration) / target_duration
        duration_score = 90 - min(40, distance_ratio * 40)
    boundary_bonus = (5 if start_adjusted else 0) + (5 if end_adjusted else 0)
    return round(_clamp(duration_score + boundary_bonus, 0, 100), 1)


def _normalize_text(text):
    return re.sub(r"\s+", " ", str(text).strip())


def _clamp(value, minimum, maximum):
    return max(minimum, min(maximum, value))
