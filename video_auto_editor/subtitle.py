"""字幕后处理纯逻辑（provider 无关）：语气词过滤与显示块重切。

本模块只做与字幕文本相关的纯函数计算，不触碰任何 I/O，便于单元测试与复用。
"""

import re

# 句中/句末标点与空白，既用于切分 token，也用于删词后的标点清理。
_PUNCT = "，。！？、；：…,!?;:"
_SEP_PATTERN = re.compile(rf"([{re.escape(_PUNCT)}\s]+)")
# 悬挂逗号类标点（删词后留在尾部应清理），但保留句末标点 。！？…
_COMMA_LIKE = "，、；：,;:"


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
