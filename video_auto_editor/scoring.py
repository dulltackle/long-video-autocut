"""转写文本流畅度分析。"""

import re


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
