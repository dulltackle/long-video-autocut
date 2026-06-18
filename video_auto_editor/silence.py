"""静音检测与分段。"""

import re
import subprocess

from video_auto_editor.config import CONFIG
from video_auto_editor.models import Segment


def detect_silence(video_path, config=None):
    """使用 FFmpeg silencedetect 检测静音区间。"""
    config = config or CONFIG
    cmd = [
        "ffmpeg", "-i", video_path,
        "-af", f"silencedetect=noise={config['silence_noise']}dB:d={config['silence_duration']}",
        "-f", "null", "-",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"FFmpeg silencedetect failed: {result.stderr.strip()}")

    starts = re.findall(r"silence_start: ([\d.]+)", result.stderr)
    ends = re.findall(r"silence_end: ([\d.]+)", result.stderr)
    return [(float(starts[i]), float(ends[i])) for i in range(min(len(starts), len(ends)))]


def identify_segments(silences, total_duration):
    """按静音区间切分出非静音片段，过滤短于 1 秒的区间。"""
    if not silences:
        return [Segment(index=0, start_time=0, end_time=total_duration, duration=total_duration)]

    segments = []
    idx = 0

    first_end = silences[0][0]
    if first_end > 1.0:
        segments.append(Segment(index=idx, start_time=0, end_time=first_end, duration=first_end))
        idx += 1

    for i in range(len(silences) - 1):
        start, end = silences[i][1], silences[i + 1][0]
        duration = end - start
        if duration >= 1.0:
            segments.append(Segment(index=idx, start_time=start, end_time=end, duration=duration))
            idx += 1

    last_start = silences[-1][1]
    if total_duration - last_start > 1.0:
        segments.append(
            Segment(
                index=idx,
                start_time=last_start,
                end_time=total_duration,
                duration=total_duration - last_start,
            )
        )

    return segments
