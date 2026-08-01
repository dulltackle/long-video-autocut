"""FFmpeg / ffprobe 媒体操作封装。"""

import json
import subprocess

from video_auto_editor.config import CONFIG


def get_video_duration(video_path):
    """通过 ffprobe 获取视频时长，失败时返回 None。"""
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None


def clip_segment(video_path, seg, output_path, config=None, subtitle_path=None):
    """按片段时间裁剪视频，包含起止缓冲。

    未传 subtitle_path 时保持原有命令（输出侧 -ss/-to，不烧录）。
    传入 subtitle_path 时改用输入侧 seek 并叠加 subtitles 滤镜，把字幕烧录进画面，
    使输出 PTS 以 0 起点，与 0 基准的短视频 SRT 对齐。
    """
    config = config or CONFIG
    if seg.start_time < 0 or seg.end_time <= seg.start_time:
        return False

    start = max(0, seg.start_time - config["buffer_start"])
    end = seg.end_time + config["buffer_end"]

    if subtitle_path:
        duration = end - start
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-i", video_path, "-t", str(duration),
            "-vf", _build_subtitles_filter(subtitle_path, config),
            "-c:v", "libx264", "-crf", str(config["crf"]), "-preset", config["preset"],
            "-c:a", "aac", "-b:a", config["audio_bitrate"],
            output_path,
        ]
    else:
        # 同样用输入侧 seek（-ss 在 -i 之前 + -t 限定时长），避免输出侧 -ss 从视频 0
        # 解码到 start：越靠后的片段解码越久（实测末段可达数百秒），是导出耗时的主因。
        duration = end - start
        cmd = [
            "ffmpeg", "-y",
            "-ss", str(start), "-i", video_path, "-t", str(duration),
            "-c:v", "libx264", "-crf", str(config["crf"]), "-preset", config["preset"],
            "-c:a", "aac", "-b:a", config["audio_bitrate"],
            output_path,
        ]
    return subprocess.run(cmd, capture_output=True, text=True).returncode == 0


def _build_subtitles_filter(subtitle_path, config):
    """构造 subtitles 滤镜串：白字黑描边、底部居中。"""
    style = (
        f"FontName={config['subtitle_font']},"
        f"FontSize={config['subtitle_font_size']},"
        "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
        f"BorderStyle=1,Outline={config['subtitle_outline']},Shadow=0,"
        f"Alignment=2,MarginV={config['subtitle_margin_v']}"
    )
    return f"subtitles={_escape_subtitles_path(subtitle_path)}:force_style='{style}'"


def _escape_subtitles_path(path):
    """转义 subtitles 滤镜文件名中的特殊字符（libass filter 语法）。"""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
