"""Whisper CLI 转写封装。"""

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Protocol

from video_auto_editor.config import CONFIG
from video_auto_editor.models import TranscriptChunk


@dataclass
class WhisperConfig:
    """Whisper 片段转写配置。"""

    model: str = "small"
    language: str = "zh"
    timeout: int = 120
    output_format: str = "txt"
    sample_rate: int = 16000
    channels: int = 1


@dataclass
class TranscriptionResult:
    """单个片段的转写结果。"""

    success: bool
    text: str = ""
    audio_path: str = ""
    transcript_path: str = ""
    error: str = ""


@dataclass
class VideoTranscriptionResult:
    """整视频转写结果。"""

    success: bool
    chunks: List[TranscriptChunk]
    cache_path: str = ""
    transcript_path: str = ""
    from_cache: bool = False
    error: str = ""


class VideoTranscriber(Protocol):
    """整视频 ASR provider 最小契约。"""

    def is_available(self):
        """检查 provider 当前是否可用。"""
        ...

    def transcribe_video(self, video_path, work_dir):
        """将整条视频转写为带时间戳的字幕片段。"""
        ...


class WhisperTranscriber:
    """基于 Whisper CLI 的片段转写器。"""

    def __init__(self, config=None):
        self.config = config or WhisperConfig()

    def is_available(self, timeout=10):
        """检查当前 Python 解释器中是否可调用 Whisper。"""
        if not sys.executable:
            return False

        try:
            result = subprocess.run(
                [sys.executable, "-m", "whisper", "--help"],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return result.returncode == 0
        except Exception:
            return False

    def transcribe_segment(self, video_path, segment_index, start_time, end_time, work_dir):
        """抽取片段音频并调用 Whisper 转写。"""
        try:
            safe_index = int(segment_index)
        except (TypeError, ValueError):
            raise ValueError(f"segment_index must be an integer: {segment_index!r}")
        if safe_index < 0:
            raise ValueError(f"segment_index must be non-negative: {segment_index!r}")

        os.makedirs(work_dir, exist_ok=True)
        audio_path = os.path.join(work_dir, f"segment_{safe_index}.wav")
        transcript_path = os.path.join(work_dir, f"segment_{safe_index}.{self.config.output_format}")

        if os.path.exists(transcript_path):
            os.remove(transcript_path)

        audio_result = subprocess.run(
            [
                "ffmpeg", "-y", "-i", video_path,
                "-ss", str(start_time), "-to", str(end_time),
                "-vn", "-ar", str(self.config.sample_rate),
                "-ac", str(self.config.channels), audio_path,
            ],
            capture_output=True,
            text=True,
        )
        if audio_result.returncode != 0:
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error=f"Audio extraction failed: {audio_result.stderr.strip()}",
            )
        if not os.path.exists(audio_path):
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error="Audio extraction did not create output file",
            )

        try:
            whisper_result = subprocess.run(
                [
                    sys.executable, "-m", "whisper", audio_path,
                    "--model", self.config.model,
                    "--language", self.config.language,
                    "--output_format", self.config.output_format,
                    "--output_dir", work_dir,
                ],
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
            )
        except subprocess.TimeoutExpired:
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error=f"Whisper timed out after {self.config.timeout}s",
            )
        except Exception as exc:
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error=f"Whisper failed: {exc}",
            )

        if whisper_result.returncode != 0:
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error=f"Whisper command failed: {whisper_result.stderr.strip()}",
            )
        if not os.path.exists(transcript_path):
            return TranscriptionResult(
                success=False,
                audio_path=audio_path,
                transcript_path=transcript_path,
                error="Transcript file not generated",
            )

        with open(transcript_path, "r", encoding="utf-8") as transcript_file:
            text = transcript_file.read().strip()

        return TranscriptionResult(
            success=True,
            text=text,
            audio_path=audio_path,
            transcript_path=transcript_path,
        )

    def transcribe_video(self, video_path, work_dir):
        """调用 Whisper 对整条视频转写，并返回带时间戳 chunks。"""
        os.makedirs(work_dir, exist_ok=True)
        transcript_path = os.path.join(work_dir, f"{Path(video_path).stem}.json")

        if os.path.exists(transcript_path):
            os.remove(transcript_path)

        try:
            whisper_result = subprocess.run(
                [
                    sys.executable, "-m", "whisper", video_path,
                    "--model", self.config.model,
                    "--language", self.config.language,
                    "--output_format", "json",
                    "--output_dir", work_dir,
                ],
                capture_output=True,
                text=True,
                timeout=self.config.timeout,
            )
        except subprocess.TimeoutExpired:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error=f"Whisper timed out after {self.config.timeout}s",
            )
        except Exception as exc:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error=f"Whisper failed: {exc}",
            )

        if whisper_result.returncode != 0:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error=f"Whisper command failed: {whisper_result.stderr.strip()}",
            )
        if not os.path.exists(transcript_path):
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error="Transcript JSON file not generated",
            )

        try:
            chunks = _parse_whisper_json(transcript_path)
        except (OSError, ValueError) as exc:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error=f"Invalid transcript JSON: {exc}",
            )

        return VideoTranscriptionResult(success=True, chunks=chunks, transcript_path=transcript_path)


def create_transcriber(config=None):
    """根据配置创建整视频 ASR provider。"""
    config = config or CONFIG
    provider = str(_config_get(config, "asr_provider", "whisper")).strip().lower()
    if provider == "whisper":
        return create_whisper_transcriber(config)
    if provider == "stepaudio":
        if not _resolve_stepfun_api_key(config):
            raise ValueError("StepAudio ASR provider requires STEPFUN_API_KEY")
        raise ValueError("StepAudio ASR provider is not implemented yet")
    raise ValueError(f"Unknown ASR provider: {provider}")


def create_whisper_transcriber(config=None):
    """从配置创建 Whisper 转写器。"""
    config = config or CONFIG
    return WhisperTranscriber(
        WhisperConfig(
            model=_config_get(config, "whisper_model"),
            language=_config_get(config, "whisper_language"),
            timeout=_config_get(config, "whisper_timeout"),
            output_format=_config_get(config, "whisper_output_format"),
            sample_rate=_config_get(config, "whisper_sample_rate"),
            channels=_config_get(config, "whisper_channels"),
        )
    )


def transcribe_candidates(video_path, candidates, work_dir, transcriber=None, config=None):
    """原地转写候选片段；失败时保持 seg.transcript 为空并继续处理。"""
    print("\n🎤 Step 6: Transcribing candidates...")
    transcriber = transcriber or create_whisper_transcriber(config)

    if not transcriber.is_available():
        print("   ⚠️  Whisper not installed or unavailable, skipping transcription, using audio-only scoring")
        return candidates

    for segment in candidates:
        print(f"   Transcribing segment_{segment.index}...")
        result = transcriber.transcribe_segment(
            video_path=video_path,
            segment_index=segment.index,
            start_time=segment.start_time,
            end_time=segment.end_time,
            work_dir=work_dir,
        )
        if result.success:
            segment.transcript = result.text
            if segment.transcript:
                preview = segment.transcript[:50] + "..." if len(segment.transcript) > 50 else segment.transcript
                print(f"   ✅ [{preview}]")
        else:
            print(f"    ⚠️  Transcription failed: segment_{segment.index}: {result.error}")

    return candidates


def transcribe_video(video_path, work_dir, transcriber=None, config=None):
    """整视频转写入口；缓存有效时直接复用。"""
    os.makedirs(work_dir, exist_ok=True)
    cache_path = os.path.join(work_dir, "transcript.json")
    cached_chunks = load_transcript_cache(video_path, cache_path)
    if cached_chunks is not None:
        return VideoTranscriptionResult(
            success=True,
            chunks=cached_chunks,
            cache_path=cache_path,
            from_cache=True,
        )

    transcriber = transcriber or create_whisper_transcriber(config)
    if not transcriber.is_available():
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            cache_path=cache_path,
            error="Whisper not installed or unavailable",
        )

    result = transcriber.transcribe_video(video_path, work_dir)
    if not result.success:
        result.cache_path = cache_path
        return result

    try:
        save_transcript_cache(video_path, result.chunks, cache_path)
    except OSError as exc:
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            cache_path=cache_path,
            transcript_path=result.transcript_path,
            error=f"Failed to save transcript cache: {exc}",
        )
    result.cache_path = cache_path
    return result


def load_transcript_cache(video_path, cache_path):
    """缓存匹配源视频时返回 TranscriptChunk 列表，否则返回 None。"""
    if not os.path.exists(cache_path):
        return None

    try:
        with open(cache_path, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return None

    source = _source_signature(video_path)
    if source is None or payload.get("source") != source:
        return None

    try:
        return [_chunk_from_dict(item) for item in payload.get("chunks", [])]
    except (KeyError, TypeError, ValueError):
        return None


def save_transcript_cache(video_path, chunks, cache_path):
    """保存整视频转写缓存。"""
    _ensure_parent_dir(cache_path)
    source = _source_signature(video_path)
    if source is None:
        raise FileNotFoundError(f"Cannot stat source video: {video_path}")
    payload = {
        "source": source,
        "chunks": [
            {"start": chunk.start, "end": chunk.end, "text": chunk.text}
            for chunk in chunks
        ],
    }
    with open(cache_path, "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, ensure_ascii=False, indent=2)


def export_srt(chunks, output_path):
    """导出 SRT 字幕文件。"""
    _ensure_parent_dir(output_path)
    with open(output_path, "w", encoding="utf-8") as srt_file:
        subtitle_index = 1
        for chunk in chunks:
            text = _normalize_subtitle_text(chunk.text)
            if not text:
                continue
            srt_file.write(f"{subtitle_index}\n")
            srt_file.write(f"{_format_srt_time(chunk.start)} --> {_format_srt_time(chunk.end)}\n")
            srt_file.write(f"{text}\n\n")
            subtitle_index += 1
    return output_path


def _parse_whisper_json(transcript_path):
    with open(transcript_path, "r", encoding="utf-8") as transcript_file:
        payload = json.load(transcript_file)

    segments = payload.get("segments") or []
    if segments:
        return [_chunk_from_dict(segment) for segment in segments if str(segment.get("text", "")).strip()]

    return []


def _chunk_from_dict(item):
    try:
        return TranscriptChunk(
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item.get("text", "")).strip(),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid transcript chunk: {exc}") from exc


def _source_signature(video_path):
    try:
        stat = os.stat(video_path)
    except OSError:
        return None
    return {
        "path": os.path.abspath(video_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _format_srt_time(seconds):
    milliseconds = int(round(max(0.0, float(seconds)) * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _ensure_parent_dir(path):
    parent_dir = os.path.dirname(path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)


def _normalize_subtitle_text(text):
    return re.sub(r"\s+", " ", str(text).strip())


def _config_get(config, key, default=None):
    if key in config:
        return config[key]
    if key in CONFIG:
        return CONFIG[key]
    return default


def _resolve_stepfun_api_key(config):
    api_key = _config_get(config, "stepfun_api_key", None)
    if api_key:
        return api_key
    env_name = _config_get(config, "stepfun_api_key_env", "STEPFUN_API_KEY")
    return os.environ.get(env_name, "")


def _resolve_stepfun_base_url(config):
    base_url = _config_get(config, "stepfun_base_url", None)
    if base_url:
        return base_url
    env_name = _config_get(config, "stepfun_base_url_env", "STEPFUN_BASE_URL")
    return os.environ.get(env_name, "")
