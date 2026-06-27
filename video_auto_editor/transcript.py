"""Whisper CLI 转写封装。"""

import base64
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
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
class StepAudioConfig:
    """StepAudio 整视频转写配置。"""

    api_key: str = ""
    base_url: str = "https://api.stepfun.com/v1"
    model: str = "stepaudio-2.5-asr"
    language: str = "zh"
    timeout: int = 120
    max_upload_bytes: int = 10 * 1024 * 1024
    shard_seconds: int = 600
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    audio_format: str = "wav"
    retry_attempts: int = 3
    retry_backoff_seconds: float = 1.0


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


@dataclass
class AudioShard:
    """StepAudio 单个音频分片计划。"""

    index: int
    start: float
    end: float
    audio_path: str
    cache_path: str


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


class StepAudioTranscriber:
    """基于 StepAudio API 的整视频转写器。"""

    def __init__(self, config=None, request_func=None, sleep_func=None):
        self.config = config or StepAudioConfig()
        self.request_func = request_func or urllib.request.urlopen
        self.sleep_func = sleep_func or time.sleep

    def is_available(self):
        """只检查必要配置，不发起网络请求。"""
        return bool(self.config.api_key)

    def transcribe_video(self, video_path, work_dir):
        """调用 StepAudio 对整条视频转写，并返回带时间戳 chunks。"""
        os.makedirs(work_dir, exist_ok=True)
        transcript_path = os.path.join(work_dir, f"{Path(video_path).stem}.stepaudio.json")

        if not self.config.api_key:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                transcript_path=transcript_path,
                error="StepAudio API key missing",
            )

        shard_result = prepare_stepaudio_audio_shards(video_path, work_dir, self.config)
        if isinstance(shard_result, VideoTranscriptionResult):
            shard_result.transcript_path = transcript_path
            return shard_result

        chunks = []
        for shard in shard_result:
            shard_chunks = load_stepaudio_shard_cache(video_path, shard, self.config)
            if shard_chunks is None:
                result = self.transcribe_audio_shard(shard.audio_path, shard.index)
                if not result.success:
                    result.transcript_path = transcript_path
                    return result
                shard_chunks = result.chunks
                try:
                    save_stepaudio_shard_cache(video_path, shard, shard_chunks, self.config)
                except OSError as exc:
                    return VideoTranscriptionResult(
                        success=False,
                        chunks=[],
                        transcript_path=transcript_path,
                        error=f"Failed to save StepAudio shard {shard.index} cache: {exc}",
                    )
            try:
                chunks.extend(_offset_shard_chunks(shard, shard_chunks))
            except ValueError as exc:
                return VideoTranscriptionResult(
                    success=False,
                    chunks=[],
                    transcript_path=transcript_path,
                    error=str(exc),
                )

        chunks.sort(key=lambda chunk: (chunk.start, chunk.end, chunk.text))
        chunks = _merge_overlapping_chunks(chunks)

        with open(transcript_path, "w", encoding="utf-8") as transcript_file:
            json.dump(
                {
                    "shards": [
                        {
                            "index": shard.index,
                            "start": shard.start,
                            "end": shard.end,
                            "audio_path": shard.audio_path,
                        }
                        for shard in shard_result
                    ],
                    "chunks": [_chunk_to_dict(chunk) for chunk in chunks],
                },
                transcript_file,
                ensure_ascii=False,
                indent=2,
            )

        return VideoTranscriptionResult(success=True, chunks=chunks, transcript_path=transcript_path)

    def transcribe_audio_shard(self, audio_path, shard_index=None):
        """请求 StepAudio 识别单个音频分片，返回分片内时间戳。"""
        shard_label = f"shard {shard_index}" if shard_index is not None else "shard"
        if not self.config.api_key:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error="StepAudio API key missing",
            )

        if not os.path.exists(audio_path):
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=f"StepAudio {shard_label} audio file missing: {audio_path}",
            )

        try:
            audio_size = os.path.getsize(audio_path)
        except OSError as exc:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=f"StepAudio cannot read {shard_label} audio file: {exc}",
            )
        encoded_size = _base64_encoded_size(audio_size)
        if encoded_size > self.config.max_upload_bytes:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=(
                    f"StepAudio {shard_label} audio is too large for upload: "
                    f"{encoded_size} bytes (base64) > {self.config.max_upload_bytes} bytes"
                ),
            )

        response_body = None
        attempts = max(1, int(self.config.retry_attempts))
        try:
            request = _build_stepaudio_request(audio_path, self.config)
        except OSError as exc:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=f"StepAudio cannot read {shard_label} audio file: {exc}",
            )

        for attempt_index in range(attempts):
            try:
                with self.request_func(request, timeout=self.config.timeout) as response:
                    response_body = response.read().decode("utf-8")
                break
            except urllib.error.HTTPError as exc:
                reason = f"HTTP {exc.code}: {_read_http_error(exc)}"
                if not _is_retryable_http_status(exc.code):
                    return VideoTranscriptionResult(
                        success=False,
                        chunks=[],
                        error=f"StepAudio request failed: {reason}",
                    )
                if attempt_index == attempts - 1:
                    return VideoTranscriptionResult(
                        success=False,
                        chunks=[],
                        error=f"StepAudio {shard_label} request failed after {attempts} attempts: {reason}",
                    )
                self._sleep_before_retry(attempt_index)
            except (TimeoutError, ConnectionError, urllib.error.URLError) as exc:
                reason = str(exc)
                if attempt_index == attempts - 1:
                    return VideoTranscriptionResult(
                        success=False,
                        chunks=[],
                        error=f"StepAudio {shard_label} request failed after {attempts} attempts: {reason}",
                    )
                self._sleep_before_retry(attempt_index)

        try:
            chunks = _parse_stepaudio_sse(response_body)
        except ValueError as exc:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=str(exc),
            )

        if not chunks:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error="StepAudio response missing timestamped segments",
            )

        return VideoTranscriptionResult(success=True, chunks=chunks)

    def _sleep_before_retry(self, attempt_index):
        backoff = max(0.0, float(self.config.retry_backoff_seconds))
        if backoff > 0:
            self.sleep_func(backoff * (attempt_index + 1))


def create_transcriber(config=None):
    """根据配置创建整视频 ASR provider。"""
    config = config or CONFIG
    provider = str(_config_get(config, "asr_provider", "whisper")).strip().lower()
    if provider == "whisper":
        return create_whisper_transcriber(config)
    if provider == "stepaudio":
        return create_stepaudio_transcriber(config)
    raise ValueError(f"Unknown ASR provider: {provider}")


def create_stepaudio_transcriber(config=None):
    """从配置创建 StepAudio 转写器。"""
    config = config or CONFIG
    return StepAudioTranscriber(
        StepAudioConfig(
            api_key=_resolve_stepfun_api_key(config),
            base_url=_resolve_stepfun_base_url(config),
            model=_config_get(config, "asr_model"),
            language=_config_get(config, "asr_language"),
            timeout=_config_get(config, "asr_timeout"),
            max_upload_bytes=int(_config_get(config, "asr_max_upload_bytes")),
            shard_seconds=int(_config_get(config, "asr_shard_seconds")),
            audio_sample_rate=int(_config_get(config, "asr_audio_sample_rate")),
            audio_channels=int(_config_get(config, "asr_audio_channels")),
            audio_format=str(_config_get(config, "asr_audio_format")),
            retry_attempts=int(_config_get(config, "asr_retry_attempts")),
            retry_backoff_seconds=float(_config_get(config, "asr_retry_backoff_seconds")),
        )
    )


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
    cached_chunks = load_transcript_cache(video_path, cache_path, config=config)
    if cached_chunks is not None:
        return VideoTranscriptionResult(
            success=True,
            chunks=cached_chunks,
            cache_path=cache_path,
            from_cache=True,
        )

    try:
        transcriber = transcriber or create_transcriber(config)
    except ValueError as exc:
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            cache_path=cache_path,
            error=f"ASR provider configuration error: {exc}",
        )

    if not transcriber.is_available():
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            cache_path=cache_path,
            error=f"ASR provider {_transcriber_name(transcriber)} unavailable",
        )

    result = transcriber.transcribe_video(video_path, work_dir)
    if not result.success:
        result.cache_path = cache_path
        return result

    try:
        save_transcript_cache(video_path, result.chunks, cache_path, config=config)
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


def load_transcript_cache(video_path, cache_path, config=None):
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
    if payload.get("asr") != _asr_cache_signature(config):
        return None

    try:
        return [_chunk_from_dict(item) for item in payload.get("chunks", [])]
    except (KeyError, TypeError, ValueError):
        return None


def save_transcript_cache(video_path, chunks, cache_path, config=None):
    """保存整视频转写缓存。"""
    _ensure_parent_dir(cache_path)
    source = _source_signature(video_path)
    if source is None:
        raise FileNotFoundError(f"Cannot stat source video: {video_path}")
    payload = {
        "source": source,
        "asr": _asr_cache_signature(config),
        "chunks": [_chunk_to_dict(chunk) for chunk in chunks],
    }
    with open(cache_path, "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, ensure_ascii=False, indent=2)


def load_stepaudio_shard_cache(video_path, shard, config):
    """分片缓存匹配源视频、ASR 配置和分片边界时返回分片内 chunks。"""
    if not os.path.exists(shard.cache_path):
        return None

    try:
        with open(shard.cache_path, "r", encoding="utf-8") as cache_file:
            payload = json.load(cache_file)
    except (OSError, json.JSONDecodeError):
        return None

    source = _source_signature(video_path)
    if source is None or payload.get("source") != source:
        return None
    if payload.get("asr") != _stepaudio_shard_cache_signature(config, shard):
        return None

    try:
        return [_chunk_from_dict(item) for item in payload.get("chunks", [])]
    except (KeyError, TypeError, ValueError):
        return None


def save_stepaudio_shard_cache(video_path, shard, chunks, config):
    """保存 StepAudio 单分片转写缓存，chunks 使用分片内时间戳。"""
    _ensure_parent_dir(shard.cache_path)
    source = _source_signature(video_path)
    if source is None:
        raise FileNotFoundError(f"Cannot stat source video: {video_path}")
    payload = {
        "source": source,
        "asr": _stepaudio_shard_cache_signature(config, shard),
        "chunks": [_chunk_to_dict(chunk) for chunk in chunks],
    }
    with open(shard.cache_path, "w", encoding="utf-8") as cache_file:
        json.dump(payload, cache_file, ensure_ascii=False, indent=2)


def prepare_stepaudio_audio_shards(video_path, work_dir, config):
    """提取统一音频并生成连续 StepAudio 分片。"""
    duration = _probe_media_duration(video_path)
    if duration is None or duration <= 0:
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            error="StepAudio cannot determine source media duration",
        )

    audio_dir = os.path.join(work_dir, "asr_audio")
    shard_dir = os.path.join(work_dir, "asr_shards")
    cache_dir = os.path.join(work_dir, "asr_shard_cache")
    os.makedirs(audio_dir, exist_ok=True)
    os.makedirs(shard_dir, exist_ok=True)
    os.makedirs(cache_dir, exist_ok=True)

    audio_format = _sanitize_audio_format(config.audio_format)
    source_audio_path = os.path.join(audio_dir, f"{Path(video_path).stem}.{audio_format}")
    extract_result = subprocess.run(
        [
            "ffmpeg", "-y", "-i", video_path,
            "-vn",
            "-ar", str(config.audio_sample_rate),
            "-ac", str(config.audio_channels),
            "-f", audio_format,
            source_audio_path,
        ],
        capture_output=True,
        text=True,
    )
    if extract_result.returncode != 0:
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            error=f"StepAudio audio extraction failed: {extract_result.stderr.strip()}",
        )
    if not os.path.exists(source_audio_path):
        return VideoTranscriptionResult(
            success=False,
            chunks=[],
            error="StepAudio audio extraction did not create output file",
        )

    shards = _build_audio_shard_plan(duration, work_dir, config)
    for shard in shards:
        cut_result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", _format_ffmpeg_seconds(shard.start),
                "-to", _format_ffmpeg_seconds(shard.end),
                "-i", source_audio_path,
                "-vn",
                "-ar", str(config.audio_sample_rate),
                "-ac", str(config.audio_channels),
                "-f", audio_format,
                shard.audio_path,
            ],
            capture_output=True,
            text=True,
        )
        if cut_result.returncode != 0:
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=f"StepAudio shard {shard.index} audio cut failed: {cut_result.stderr.strip()}",
            )
        if not os.path.exists(shard.audio_path):
            return VideoTranscriptionResult(
                success=False,
                chunks=[],
                error=f"StepAudio shard {shard.index} audio cut did not create output file",
            )

    return shards


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


_STEPAUDIO_SENTENCE_ENDINGS = "。！？!?…."
_STEPAUDIO_MAX_CHUNK_CHARS = 40


def _parse_stepaudio_sse(response_body):
    """解析 StepAudio ASR SSE 流，提取带时间戳的 delta 事件并聚合成句级分段。"""
    deltas = []
    error_message = None
    for raw_line in str(response_body).splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        try:
            event = json.loads(data)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        event_type = event.get("type")
        if event_type == "error":
            error_message = str(event.get("message") or "unknown error")
            continue
        if event_type != "transcript.text.delta":
            continue
        delta = str(event.get("delta", ""))
        if not delta.strip():
            continue
        start = _stepaudio_ms_to_seconds(event.get("start_time"))
        end = _stepaudio_ms_to_seconds(event.get("end_time"))
        if start is None or end is None:
            continue
        deltas.append((start, end, delta))

    chunks = _aggregate_stepaudio_deltas(deltas)
    if not chunks and error_message is not None:
        raise ValueError(f"StepAudio SSE error: {error_message}")
    return chunks


def _aggregate_stepaudio_deltas(deltas):
    """把逐字 delta 聚合为句级分段：遇句末标点或超长即断句。

    依赖 StepAudio ASR SSE 的 delta 为逐字（单字符）粒度：因此句末标点会单独成段，
    分段的 end 取触发断句那个 delta 的 end_time 是准确的；若未来 API 改为多字符 delta、
    且标点不在 delta 末尾，end 可能越界并与下一段重叠，届时需改为按字符级跟踪边界。
    """
    chunks = []
    char_entries = []  # 当前句缓冲的逐字 (char, start, end)，多字符 delta 按字符均分区间。
    for delta_start, delta_end, delta in deltas:
        char_entries.extend(_split_delta_chars(delta_start, delta_end, delta))
        buffer_stripped = "".join(char for char, _, _ in char_entries).strip()
        last_char = delta.strip()[-1:]
        if last_char in _STEPAUDIO_SENTENCE_ENDINGS or len(buffer_stripped) >= _STEPAUDIO_MAX_CHUNK_CHARS:
            chunk = _chunk_from_char_entries(char_entries)
            if chunk is not None:
                chunks.append(chunk)
            char_entries = []
    tail = _chunk_from_char_entries(char_entries)
    if tail is not None:
        chunks.append(tail)
    return chunks


def _split_delta_chars(delta_start, delta_end, delta):
    """把一个 delta 拆成逐字 (char, start, end)；多字符 delta 在 [start,end] 上按字符均分。"""
    chars = list(delta)
    count = len(chars)
    if count == 0:
        return []
    span = (delta_end - delta_start) / count
    entries = []
    for position, char in enumerate(chars):
        char_start = delta_start + span * position
        char_end = delta_end if position == count - 1 else delta_start + span * (position + 1)
        entries.append((char, char_start, char_end))
    return entries


def _chunk_from_char_entries(char_entries):
    """从逐字缓冲生成 TranscriptChunk。

    chunk 的 start/end 沿用缓冲整体 delta 边界（不因首尾空白被切窄，保持既有时间语义）；
    char_spans 则对齐剥离首尾空白后的文本，逐字一一对应。
    """
    if not char_entries:
        return None
    start_index = 0
    end_index = len(char_entries)
    while start_index < end_index and char_entries[start_index][0].isspace():
        start_index += 1
    while end_index > start_index and char_entries[end_index - 1][0].isspace():
        end_index -= 1
    trimmed = char_entries[start_index:end_index]
    if not trimmed:
        return None
    text = "".join(char for char, _, _ in trimmed)
    char_spans = [(char_start, char_end) for _, char_start, char_end in trimmed]
    return TranscriptChunk(
        start=char_entries[0][1],
        end=char_entries[-1][2],
        text=text,
        char_spans=char_spans,
    )


def _stepaudio_ms_to_seconds(value):
    try:
        return float(value) / 1000.0
    except (TypeError, ValueError):
        return None


def _probe_media_duration(video_path):
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", video_path],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _base64_encoded_size(raw_size):
    """base64 编码后的字节数：每 3 字节原始数据编码为 4 字节（向上取整）。"""
    return (int(raw_size) + 2) // 3 * 4


def _effective_shard_seconds(config):
    """把分片时长封顶在 base64 后 payload 不超过 max_upload_bytes 的范围内。

    StepAudio SSE 端点限制的是 base64 后的请求体大小，故原始音频预算为
    ``max_upload_bytes * 3 / 4``；再按 16-bit PCM 码率估算单位时长字节数，
    给出安全的分片时长上限。压缩格式的实际字节更小，此估算偏保守。
    """
    shard_seconds = max(float(config.shard_seconds), 0.001)
    bytes_per_second = int(config.audio_sample_rate) * int(config.audio_channels) * 2
    if bytes_per_second <= 0:
        return shard_seconds
    raw_budget = (int(config.max_upload_bytes) // 4) * 3
    usable = raw_budget - 1024  # 预留 WAV 头与 JSON 包裹余量
    max_shard_seconds = usable / bytes_per_second
    if max_shard_seconds < 1:
        max_shard_seconds = 1.0
    return min(shard_seconds, float(int(max_shard_seconds)))


def _build_audio_shard_plan(duration, work_dir, config):
    shard_seconds = _effective_shard_seconds(config)
    audio_format = _sanitize_audio_format(config.audio_format)
    shard_dir = os.path.join(work_dir, "asr_shards")
    cache_dir = os.path.join(work_dir, "asr_shard_cache")
    shards = []
    start = 0.0
    index = 0
    while start < duration:
        end = min(duration, start + shard_seconds)
        if end <= start:
            break
        shards.append(
            AudioShard(
                index=index,
                start=start,
                end=end,
                audio_path=os.path.join(shard_dir, f"shard_{index:04d}.{audio_format}"),
                cache_path=os.path.join(cache_dir, f"shard_{index:04d}.json"),
            )
        )
        index += 1
        start = end
    return shards


def _offset_shard_chunks(shard, chunks):
    offset_chunks = []
    for chunk in chunks:
        text = str(chunk.text).strip()
        if not text:
            continue
        if chunk.end < chunk.start:
            raise ValueError(
                f"StepAudio shard {shard.index} returned invalid timestamp: "
                f"{chunk.start:g}-{chunk.end:g}"
            )
        offset_chunks.append(
            TranscriptChunk(
                start=shard.start + chunk.start,
                end=shard.start + chunk.end,
                text=text,
                char_spans=_offset_char_spans(chunk.char_spans, shard.start),
            )
        )
    return offset_chunks


def _offset_char_spans(char_spans, offset):
    """把逐字时间整体平移 offset；无逐字时间则保持 None。"""
    if char_spans is None:
        return None
    return [(span_start + offset, span_end + offset) for span_start, span_end in char_spans]


def _merge_overlapping_chunks(chunks):
    merged = []
    for chunk in chunks:
        if not merged or chunk.start >= merged[-1].end:
            merged.append(chunk)
            continue

        previous = merged[-1]
        # 合并块的文本经裁剪去重，逐字时间无法可靠对齐，置 None 让下游走比例兜底。
        merged[-1] = TranscriptChunk(
            start=previous.start,
            end=max(previous.end, chunk.end),
            text=_merge_chunk_text(previous.text, chunk.text),
            char_spans=None,
        )
    return merged


def _merge_chunk_text(left, right):
    left = str(left).strip()
    right = str(right).strip()
    if not left:
        return right
    if not right or right == left or right in left:
        return left
    if left in right:
        return right
    return f"{left} {right}"


def _chunk_to_dict(chunk):
    """序列化单个 chunk；char_spans 作为新可选键，仅在存在时写入以兼容旧缓存读取方。"""
    item = {"start": chunk.start, "end": chunk.end, "text": chunk.text}
    if chunk.char_spans is not None:
        item["char_spans"] = [[span_start, span_end] for span_start, span_end in chunk.char_spans]
    return item


def _chunk_from_dict(item):
    try:
        return TranscriptChunk(
            start=float(item["start"]),
            end=float(item["end"]),
            text=str(item.get("text", "")).strip(),
            char_spans=_char_spans_from_payload(item.get("char_spans")),
        )
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid transcript chunk: {exc}") from exc


def _char_spans_from_payload(raw):
    """读取缓存里的 char_spans；旧缓存无该键时返回 None。"""
    if raw is None:
        return None
    return [(float(span[0]), float(span[1])) for span in raw]


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


def _asr_cache_signature(config=None):
    config = config or CONFIG
    provider = str(_config_get(config, "asr_provider", "whisper")).strip().lower()
    signature = {"provider": provider}
    if provider == "stepaudio":
        signature["model"] = str(_config_get(config, "asr_model", ""))
        signature["language"] = str(_config_get(config, "asr_language", ""))
        signature["shard_seconds"] = int(_config_get(config, "asr_shard_seconds", 0))
        signature["audio_sample_rate"] = int(_config_get(config, "asr_audio_sample_rate", 0))
        signature["audio_channels"] = int(_config_get(config, "asr_audio_channels", 0))
        signature["audio_format"] = str(_config_get(config, "asr_audio_format", ""))
    elif provider == "whisper":
        signature["model"] = str(_config_get(config, "whisper_model", ""))
        signature["language"] = str(_config_get(config, "whisper_language", ""))
    return signature


def _stepaudio_shard_cache_signature(config, shard):
    return {
        "provider": "stepaudio",
        "model": str(config.model),
        "language": str(config.language),
        "shard_start_ms": _cache_time_milliseconds(shard.start),
        "shard_end_ms": _cache_time_milliseconds(shard.end),
        "audio_sample_rate": int(config.audio_sample_rate),
        "audio_channels": int(config.audio_channels),
        "audio_format": _sanitize_audio_format(config.audio_format),
    }


def _cache_time_milliseconds(seconds):
    return int(round(float(seconds) * 1000))


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
    # 仅折叠行内空白，保留显示块重切产生的换行（libass 据此渲染多行）。
    lines = [re.sub(r"[^\S\n]+", " ", line).strip() for line in str(text).strip().split("\n")]
    return "\n".join(line for line in lines if line)


def _build_stepaudio_request(video_path, config):
    body = _encode_stepaudio_payload(video_path, config)
    url = _join_url(config.base_url, "audio/asr/sse")
    return urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )


def _encode_stepaudio_payload(video_path, config):
    # 此处传入的是分片音频，已被 asr_shard_seconds 限制在 SSE 端点允许的大小内
    # （base64 后须 ≤10MB），故整片读入内存可接受；若改大分片需同步评估内存占用。
    with open(video_path, "rb") as audio_file:
        audio_content = audio_file.read()

    payload = {
        "audio": {
            "data": base64.b64encode(audio_content).decode("ascii"),
            "input": {
                "transcription": {
                    "language": config.language,
                    "model": config.model,
                    "enable_itn": True,
                    "enable_timestamp": True,
                },
                "format": _stepaudio_audio_format(config),
            },
        }
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _stepaudio_audio_format(config):
    audio_format = _sanitize_audio_format(config.audio_format)
    fmt = {"type": audio_format}
    if audio_format == "pcm":
        fmt.update(
            {
                "codec": "pcm_s16le",
                "rate": int(config.audio_sample_rate),
                "bits": 16,
                "channel": int(config.audio_channels),
            }
        )
    return fmt


def _join_url(base_url, path):
    return f"{str(base_url).rstrip('/')}/{path.lstrip('/')}"


def _sanitize_audio_format(audio_format):
    sanitized = re.sub(r"[^A-Za-z0-9]", "", str(audio_format).lower())
    return sanitized or "wav"


def _is_retryable_http_status(status_code):
    return int(status_code) == 429 or int(status_code) >= 500


def _format_ffmpeg_seconds(seconds):
    return f"{float(seconds):.6f}".rstrip("0").rstrip(".")


def _read_http_error(exc):
    try:
        return exc.read().decode("utf-8").strip()
    except Exception:
        return str(exc)


def _transcriber_name(transcriber):
    if isinstance(transcriber, StepAudioTranscriber):
        return "stepaudio"
    if isinstance(transcriber, WhisperTranscriber):
        return "whisper"
    return transcriber.__class__.__name__


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
