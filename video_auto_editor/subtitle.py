"""字幕后处理纯逻辑（provider 无关）：语气词过滤与显示块重切。

本模块只做与字幕文本相关的纯函数计算，不触碰任何 I/O，便于单元测试与复用。
"""

import re

from video_auto_editor.models import TranscriptChunk

# 句中/句末标点与空白，既用于切分 token，也用于删词后的标点清理。
_PUNCT = "，。！？、；：…,!?;:"
_SEP_PATTERN = re.compile(rf"([{re.escape(_PUNCT)}\s]+)")
# 悬挂逗号类标点（删词后留在尾部应清理），但保留句末标点 。！？…
_COMMA_LIKE = "，、；：,;:"
# 句末标点：显示块切分时优先在此断开。
_SENTENCE_END = "。！？!?…"


def filter_filler_words(text, filler_words):
    """删除纯语气词 token，保留实词与黏连尾字，返回清理后的文本。

    规则：
    - 按标点与空白把 text 切成 token；
    - 仅当某 token 去除首尾空白后完全等于某语气词，或完全由单字语气词字符构成
      （如「嗯嗯」「啊啊啊」）时，丢弃该 token；
    - 不对多字符实词 token 做首尾语气词剥离（「好啊」「是啊」「这样吧」保持原样）；
    - 重组后清理因删词产生的重复标点与首尾悬挂标点；
    - 全部删空时返回空字符串。
    """
    text = str(text)
    if not filler_words:
        return text

    filler_set = {str(word) for word in filler_words if str(word)}
    single_char_fillers = {word for word in filler_set if len(word) == 1}

    parts = _SEP_PATTERN.split(text)
    rebuilt = []
    for index, part in enumerate(parts):
        # 偶数下标是 token，奇数下标是分隔符（保留以便重组）。
        if index % 2 == 1:
            rebuilt.append(part)
            continue
        if _is_pure_filler(part, filler_set, single_char_fillers):
            continue
        rebuilt.append(part)

    return _cleanup_punctuation("".join(rebuilt))


def _is_pure_filler(token, filler_set, single_char_fillers):
    stripped = token.strip()
    if not stripped:
        return False
    if stripped in filler_set:
        return True
    if single_char_fillers and all(char in single_char_fillers for char in stripped):
        return True
    return False


def _cleanup_punctuation(text):
    # 删词后相邻标点合并为首个（保留句末标点优先于逗号）。
    text = re.sub(rf"([{re.escape(_PUNCT)}])[{re.escape(_PUNCT)}]+", r"\1", text)
    # 去掉首部残留的标点与空白。
    text = re.sub(rf"^[{re.escape(_PUNCT)}\s]+", "", text)
    # 去掉尾部悬挂的逗号类标点与空白，但保留句末标点。
    text = re.sub(rf"[{re.escape(_COMMA_LIKE)}\s]+$", "", text)
    return text.strip()


def resegment_chunks(chunks, max_chars_per_line, max_lines):
    """对 chunk 列表逐块重切为显示块，保持时间有序。"""
    result = []
    for chunk in chunks:
        result.extend(resegment_chunk(chunk, max_chars_per_line, max_lines))
    return result


def resegment_chunk(chunk, max_chars_per_line, max_lines):
    """把单个 chunk 重切为「~max_chars_per_line 字/行、最多 max_lines 行」的显示块。

    - 块容量 block_cap = max_chars_per_line * max_lines。
    - 文本 ≤ block_cap：单块返回；若 > max_chars_per_line 则在中点附近、优先靠近标点处
      插入换行（以 \\n 表示，烧录由 libass 渲染为多行）。
    - 文本 > block_cap：优先按句末/逗号切分为 ≤ block_cap 的块；不得不切长 token 时按字符切。
      每块 start/end 在原 [start,end] 内按字符数比例分配，块间无缝（前块 end == 后块 start），
      首块 start == 原 start、末块 end == 原 end。
    """
    text = str(chunk.text).strip()
    start = float(chunk.start)
    end = float(chunk.end)
    if not text:
        return []

    block_cap = max(1, int(max_chars_per_line) * int(max_lines))
    if len(text) <= block_cap:
        return [TranscriptChunk(start, end, _wrap_lines(text, max_chars_per_line, max_lines))]

    segments = _split_into_blocks(text, block_cap)
    total = len(text)
    # 按累计字符数计算块边界时间，保证相邻块首尾相接、整体不重叠不留缝。
    boundaries = [0]
    for segment in segments:
        boundaries.append(boundaries[-1] + len(segment))
    span = end - start
    times = [start + span * offset / total for offset in boundaries]
    times[0] = start
    times[-1] = end

    blocks = []
    for index, segment in enumerate(segments):
        blocks.append(
            TranscriptChunk(
                times[index],
                times[index + 1],
                _wrap_lines(segment, max_chars_per_line, max_lines),
            )
        )
    return blocks


def _split_into_blocks(text, block_cap):
    """把超长文本切成 ≤ block_cap 的块，优先句末、其次逗号，最后按字符硬切。"""
    blocks = []
    remaining = text
    while len(remaining) > block_cap:
        window = remaining[:block_cap]
        cut = _best_break(window, block_cap)
        blocks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        blocks.append(remaining)
    return blocks


def _best_break(window, block_cap):
    # 在前 block_cap 字内，取尽量靠后的句末标点；无则取逗号类；都无则硬切。
    for charset in (_SENTENCE_END, _COMMA_LIKE):
        idx = max((i for i, char in enumerate(window) if char in charset), default=-1)
        if idx >= 0:
            return idx + 1
    return block_cap


def _wrap_lines(text, max_chars_per_line, max_lines):
    """把 ≤ block_cap 的文本按 max_chars_per_line 折成最多 max_lines 行，优先靠标点断行。"""
    max_chars_per_line = int(max_chars_per_line)
    max_lines = int(max_lines)
    if len(text) <= max_chars_per_line:
        return text

    lines = []
    remaining = text
    for line_index in range(max_lines):
        if len(remaining) <= max_chars_per_line:
            lines.append(remaining)
            remaining = ""
            break
        lines_left_after = max_lines - line_index - 1
        # 本行至少要取的字符数：保证剩余文本仍能装进余下行。
        min_take = max(1, len(remaining) - lines_left_after * max_chars_per_line)
        # 均衡目标：把剩余文本平摊到剩余行。
        target = (len(remaining) + lines_left_after) // (lines_left_after + 1)
        target = min(max(target, min_take), max_chars_per_line)
        cut = _find_line_cut(remaining, min_take, max_chars_per_line, target)
        lines.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        lines[-1] += remaining
    return "\n".join(line for line in lines if line)


def _find_line_cut(text, lo, hi, target):
    # 在 [lo, hi] 长度范围内寻找以标点结尾、且最靠近 target 的断点。
    best = None
    for cut in range(lo, hi + 1):
        if cut <= 0 or cut > len(text):
            continue
        if text[cut - 1] in _PUNCT:
            if best is None or abs(cut - target) < abs(best - target):
                best = cut
    return best if best is not None else min(max(target, lo), hi)
