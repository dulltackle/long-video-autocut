"""FFmpeg / ffprobe 媒体操作封装。"""

import json
import os
import subprocess
import tempfile

from video_auto_editor.config import CONFIG


def get_video_duration(video_path):
    """通过 ffprobe 获取视频时长，失败时返回 None。"""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None


def clip_segment(video_path, seg, output_path, config=None):
    """按片段时间裁剪视频，包含起止缓冲。"""
    config = config or CONFIG
    if seg.start_time < 0 or seg.end_time <= seg.start_time:
        return False

    start = max(0, seg.start_time - config["buffer_start"])
    end = seg.end_time + config["buffer_end"]
    cmd = [
        "ffmpeg", "-y", "-i", video_path,
        "-ss", str(start), "-to", str(end),
        "-c:v", "libx264", "-crf", str(config["crf"]), "-preset", config["preset"],
        "-c:a", "aac", "-b:a", config["audio_bitrate"],
        output_path,
    ]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def concat_videos(clip_paths, output_path, config=None):
    """使用 FFmpeg concat demuxer 拼接视频，完成后清理列表文件。"""
    config = config or CONFIG
    output_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    list_file = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".list.txt",
            prefix="concat_",
            dir=output_dir,
            delete=False,
        ) as file:
            list_file = file.name
            for path in clip_paths:
                file.write(f"file '{os.path.abspath(path)}'\n")

        cmd = [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
            "-c:v", "libx264", "-crf", str(config["crf"]), "-preset", config["preset"],
            "-c:a", "aac", "-b:a", config["audio_bitrate"],
            output_path,
        ]
        return subprocess.run(cmd, capture_output=True, text=True).returncode == 0
    finally:
        if list_file:
            try:
                os.remove(list_file)
            except OSError:
                pass
