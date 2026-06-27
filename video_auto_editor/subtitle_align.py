"""clip 窗口字幕的逐字时间映射与子序列对齐。

把一条 clip 窗口的连续 chunk 拼成纯文本，并为每个字符给出 (start, end) 逐字时间：
有 ASR 逐字时间（char_spans）则精确使用，缺失则按 chunk [start,end] 比例兜底。
字幕优化模型在子序列约束下返回显示块文本后，再用两指针对齐回这些逐字时间。
"""


def build_window(window_chunks):
    """把窗口内 chunk 拼成 (text, char_times)。

    char_times[i] 是 text[i] 的 (start, end) 逐字时间。某 chunk 缺失 char_spans
    （如重叠合并块、非 StepAudio 识别路径）时，该段按 chunk [start,end] 比例兜底。
    """
    text_parts = []
    char_times = []
    for chunk in window_chunks:
        chunk_text = str(chunk.text)
        if not chunk_text:
            continue
        text_parts.append(chunk_text)
        char_times.extend(_chunk_char_times(chunk, chunk_text))
    return "".join(text_parts), char_times


def _chunk_char_times(chunk, chunk_text):
    spans = chunk.char_spans
    if spans is not None and len(spans) == len(chunk_text):
        return [(float(span_start), float(span_end)) for span_start, span_end in spans]
    return _proportional_char_times(float(chunk.start), float(chunk.end), len(chunk_text))


def _proportional_char_times(start, end, count):
    """在 [start,end] 上按字符数均分逐字时间。"""
    if count <= 0:
        return []
    step = (end - start) / count
    times = []
    for position in range(count):
        char_start = start + step * position
        char_end = end if position == count - 1 else start + step * (position + 1)
        times.append((char_start, char_end))
    return times
