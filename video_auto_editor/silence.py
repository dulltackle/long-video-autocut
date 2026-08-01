"""静音检测。"""

import re
import subprocess

from video_auto_editor.config import CONFIG


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
