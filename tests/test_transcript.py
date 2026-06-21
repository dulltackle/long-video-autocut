import io
import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from video_auto_editor.models import TranscriptChunk
from video_auto_editor import transcript
from video_auto_editor.config import CONFIG
from video_auto_editor.transcript import (
    AudioShard,
    StepAudioConfig,
    StepAudioTranscriber,
    TranscriptionResult,
    VideoTranscriptionResult,
    WhisperConfig,
    WhisperTranscriber,
    create_stepaudio_transcriber,
    create_transcriber,
    create_whisper_transcriber,
    prepare_stepaudio_audio_shards,
)


def completed(returncode=0, stderr="", stdout=""):
    return SimpleNamespace(returncode=returncode, stderr=stderr, stdout=stdout)


def install_stepaudio_media_success(monkeypatch, duration="125.0"):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return completed(0, stdout=json.dumps({"format": {"duration": duration}}))
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"audio")
            return completed(0)
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)


def test_is_available_returns_true_when_whisper_help_succeeds(monkeypatch):
    def fake_run(cmd, **kwargs):
        assert cmd[:3] == [transcript.sys.executable, "-m", "whisper"]
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    assert WhisperTranscriber().is_available() is True


def test_is_available_returns_false_for_nonzero_exception_and_timeout(monkeypatch):
    monkeypatch.setattr(transcript.subprocess, "run", lambda *args, **kwargs: completed(1))
    assert WhisperTranscriber().is_available() is False

    def raise_error(*args, **kwargs):
        raise OSError("missing")

    monkeypatch.setattr(transcript.subprocess, "run", raise_error)
    assert WhisperTranscriber().is_available() is False

    def raise_timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout", 10))

    monkeypatch.setattr(transcript.subprocess, "run", raise_timeout)
    assert WhisperTranscriber().is_available() is False


def test_is_available_returns_false_when_python_executable_missing(monkeypatch):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(transcript.sys, "executable", None)
    monkeypatch.setattr(transcript.subprocess, "run", fail_if_called)

    assert WhisperTranscriber().is_available() is False


def test_create_transcriber_creates_whisper_provider_from_config():
    transcriber = create_transcriber(
        {
            "asr_provider": "whisper",
            "whisper_model": "tiny",
            "whisper_language": "en",
            "whisper_timeout": 30,
            "whisper_output_format": "txt",
            "whisper_sample_rate": 8000,
            "whisper_channels": 2,
        }
    )

    assert isinstance(transcriber, WhisperTranscriber)
    assert transcriber.config == WhisperConfig(
        model="tiny",
        language="en",
        timeout=30,
        output_format="txt",
        sample_rate=8000,
        channels=2,
    )


def test_create_transcriber_rejects_unknown_provider():
    with pytest.raises(ValueError, match="Unknown ASR provider: unknown"):
        create_transcriber({"asr_provider": "unknown"})


def test_create_whisper_transcriber_keeps_compatibility_entrypoint():
    transcriber = create_whisper_transcriber({"whisper_model": "base"})

    assert isinstance(transcriber, WhisperTranscriber)
    assert transcriber.config.model == "base"
    assert transcriber.config.language == "zh"


def test_default_asr_config_uses_stepaudio():
    assert CONFIG["asr_provider"] == "stepaudio"
    assert CONFIG["asr_model"] == "stepaudio-2.5-asr"
    assert CONFIG["asr_timeout"] == 120
    assert CONFIG["asr_language"] == "zh"
    assert CONFIG["asr_max_upload_bytes"] == 200 * 1024 * 1024
    assert CONFIG["asr_shard_seconds"] == 600
    assert CONFIG["asr_audio_sample_rate"] == 16000
    assert CONFIG["asr_audio_channels"] == 1
    assert CONFIG["asr_audio_format"] == "wav"
    assert CONFIG["asr_retry_attempts"] == 3
    assert CONFIG["asr_retry_backoff_seconds"] == 1.0
    assert CONFIG["stepfun_api_key_env"] == "STEPFUN_API_KEY"
    assert CONFIG["stepfun_base_url_env"] == "STEPFUN_BASE_URL"


def test_create_transcriber_creates_stepaudio_provider_without_request(monkeypatch):
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)

    transcriber = create_transcriber({"asr_provider": "stepaudio"})

    assert isinstance(transcriber, StepAudioTranscriber)
    assert transcriber.is_available() is False


def test_create_transcriber_reads_stepaudio_api_key_without_request(monkeypatch):
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")

    transcriber = create_transcriber({"asr_provider": "stepaudio"})

    assert isinstance(transcriber, StepAudioTranscriber)
    assert transcriber.is_available() is True
    assert transcriber.config.api_key == "test-key"


def test_create_stepaudio_transcriber_reads_base_url_from_env(monkeypatch):
    monkeypatch.setenv("STEPFUN_API_KEY", "test-key")
    monkeypatch.setenv("STEPFUN_BASE_URL", "https://example.test/v1")

    transcriber = create_stepaudio_transcriber(
        {
            "stepfun_base_url": "",
            "asr_model": "custom-asr",
            "asr_language": "en",
            "asr_timeout": 30,
        }
    )

    assert transcriber.config == StepAudioConfig(
        api_key="test-key",
        base_url="https://example.test/v1",
        model="custom-asr",
        language="en",
        timeout=30,
        max_upload_bytes=200 * 1024 * 1024,
        shard_seconds=600,
        audio_sample_rate=16000,
        audio_channels=1,
        audio_format="wav",
        retry_attempts=3,
        retry_backoff_seconds=1.0,
    )


def test_transcribe_segment_rejects_invalid_segment_index(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(transcript.subprocess, "run", fail_if_called)

    with pytest.raises(ValueError, match="segment_index must be an integer"):
        WhisperTranscriber().transcribe_segment(
            video_path="input.mov",
            segment_index="../../etc/passwd",
            start_time=0,
            end_time=10,
            work_dir=str(tmp_path),
        )

    assert not list(tmp_path.iterdir())


def test_transcribe_segment_rejects_negative_segment_index(monkeypatch, tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called")

    monkeypatch.setattr(transcript.subprocess, "run", fail_if_called)

    with pytest.raises(ValueError, match="segment_index must be non-negative"):
        WhisperTranscriber().transcribe_segment(
            video_path="input.mov",
            segment_index=-1,
            start_time=0,
            end_time=10,
            work_dir=str(tmp_path),
        )

    assert not list(tmp_path.iterdir())


def test_ffmpeg_failure_does_not_call_whisper(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return completed(1, "ffmpeg failed")

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber().transcribe_segment(
        video_path="input.mov",
        segment_index=1,
        start_time=0,
        end_time=10,
        work_dir=str(tmp_path),
    )

    assert result.success is False
    assert "Audio extraction failed" in result.error
    assert len(calls) == 1
    assert calls[0][0] == "ffmpeg"


def test_whisper_failure_deletes_stale_transcript_and_does_not_read_it(monkeypatch, tmp_path):
    stale_transcript = tmp_path / "segment_2.txt"
    stale_transcript.write_text("旧文本", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"audio")
            return completed(0)
        return completed(1, "whisper failed")

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber().transcribe_segment(
        video_path="input.mov",
        segment_index=2,
        start_time=1,
        end_time=11,
        work_dir=str(tmp_path),
    )

    assert result.success is False
    assert result.text == ""
    assert "Whisper command failed" in result.error
    assert not stale_transcript.exists()


def test_missing_transcript_after_whisper_success_does_not_use_stale_file(monkeypatch, tmp_path):
    stale_transcript = tmp_path / "segment_3.txt"
    stale_transcript.write_text("stale", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"audio")
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber().transcribe_segment(
        video_path="input.mov",
        segment_index=3,
        start_time=2,
        end_time=12,
        work_dir=str(tmp_path),
    )

    assert result.success is False
    assert result.text == ""
    assert result.error == "Transcript file not generated"
    assert not stale_transcript.exists()


def test_whisper_success_returns_transcript_text(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffmpeg":
            Path(cmd[-1]).write_bytes(b"audio")
            return completed(0)
        transcript_path = tmp_path / "segment_4.txt"
        transcript_path.write_text("  转写内容  \n", encoding="utf-8")
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber(WhisperConfig(timeout=30)).transcribe_segment(
        video_path="input.mov",
        segment_index=4,
        start_time=3,
        end_time=13,
        work_dir=str(tmp_path),
    )

    assert result == TranscriptionResult(
        success=True,
        text="转写内容",
        audio_path=str(tmp_path / "segment_4.wav"),
        transcript_path=str(tmp_path / "segment_4.txt"),
    )


def test_transcribe_candidates_skips_when_whisper_unavailable():
    candidates = [SimpleNamespace(index=1, start_time=0, end_time=10, transcript="")]

    class FakeTranscriber:
        def is_available(self):
            return False

    result = transcript.transcribe_candidates("input.mov", candidates, "work", FakeTranscriber())

    assert result is candidates
    assert candidates[0].transcript == ""


def test_transcribe_candidates_continues_after_single_segment_failure():
    candidates = [
        SimpleNamespace(index=1, start_time=0, end_time=10, transcript=""),
        SimpleNamespace(index=2, start_time=11, end_time=20, transcript=""),
    ]

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_segment(self, **kwargs):
            if kwargs["segment_index"] == 1:
                return TranscriptionResult(success=False, error="failed")
            return TranscriptionResult(success=True, text="第二段")

    transcript.transcribe_candidates("input.mov", candidates, "work", FakeTranscriber())

    assert candidates[0].transcript == ""
    assert candidates[1].transcript == "第二段"


def test_transcribe_video_uses_valid_cache_without_calling_asr(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    chunks = [TranscriptChunk(1.0, 3.5, "缓存文本")]
    transcript.save_transcript_cache(str(video_path), chunks, str(cache_path))

    class FailTranscriber:
        def is_available(self):
            raise AssertionError("should not check whisper when cache is valid")

    result = transcript.transcribe_video(str(video_path), str(cache_path.parent), FailTranscriber())

    assert result == VideoTranscriptionResult(
        success=True,
        chunks=chunks,
        cache_path=str(cache_path),
        from_cache=True,
    )


def test_transcribe_video_uses_valid_cache_without_creating_provider(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    chunks = [TranscriptChunk(1.0, 3.5, "缓存文本")]
    config = {"asr_provider": "unknown"}
    transcript.save_transcript_cache(str(video_path), chunks, str(cache_path), config=config)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("should not create ASR provider when cache is valid")

    monkeypatch.setattr(transcript, "create_transcriber", fail_if_called)

    result = transcript.transcribe_video(str(video_path), str(cache_path.parent), config=config)

    assert result.success is True
    assert result.from_cache is True
    assert result.chunks == chunks


def test_save_transcript_cache_writes_asr_signature(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    config = {
        "asr_provider": "whisper",
        "whisper_model": "tiny",
        "whisper_language": "en",
    }

    transcript.save_transcript_cache(
        str(video_path),
        [TranscriptChunk(1.0, 3.5, "缓存文本")],
        str(cache_path),
        config=config,
    )

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["asr"] == {"provider": "whisper", "model": "tiny", "language": "en"}
    assert transcript.load_transcript_cache(str(video_path), str(cache_path), config=config) == [
        TranscriptChunk(1.0, 3.5, "缓存文本")
    ]


def test_stepaudio_cache_signature_includes_sharding_audio_config(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    config = {
        "asr_provider": "stepaudio",
        "asr_model": "custom-asr",
        "asr_language": "en",
        "asr_shard_seconds": 120,
        "asr_audio_sample_rate": 8000,
        "asr_audio_channels": 2,
        "asr_audio_format": "mp3",
    }

    transcript.save_transcript_cache(
        str(video_path),
        [TranscriptChunk(1.0, 3.5, "缓存文本")],
        str(cache_path),
        config=config,
    )

    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["asr"] == {
        "provider": "stepaudio",
        "model": "custom-asr",
        "language": "en",
        "shard_seconds": 120,
        "audio_sample_rate": 8000,
        "audio_channels": 2,
        "audio_format": "mp3",
    }
    assert transcript.load_transcript_cache(str(video_path), str(cache_path), config=config) == [
        TranscriptChunk(1.0, 3.5, "缓存文本")
    ]
    assert (
        transcript.load_transcript_cache(
            str(video_path),
            str(cache_path),
            config={**config, "asr_audio_sample_rate": 16000},
        )
        is None
    )


def test_transcribe_video_rebuilds_cache_when_provider_signature_changes(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    transcript.save_transcript_cache(
        str(video_path),
        [TranscriptChunk(0, 1, "Whisper 缓存")],
        str(cache_path),
        config={"asr_provider": "whisper"},
    )

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "StepAudio 转写")])

    result = transcript.transcribe_video(
        str(video_path),
        str(cache_path.parent),
        FakeTranscriber(),
        config={"asr_provider": "stepaudio"},
    )

    assert result.success is True
    assert result.from_cache is False
    assert result.chunks == [TranscriptChunk(2, 4, "StepAudio 转写")]
    payload = json.loads(cache_path.read_text(encoding="utf-8"))
    assert payload["asr"]["provider"] == "stepaudio"


def test_transcribe_video_rebuilds_cache_when_asr_model_changes(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    transcript.save_transcript_cache(
        str(video_path),
        [TranscriptChunk(0, 1, "旧模型缓存")],
        str(cache_path),
        config={"asr_provider": "stepaudio", "asr_model": "old-asr"},
    )

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "新模型转写")])

    result = transcript.transcribe_video(
        str(video_path),
        str(cache_path.parent),
        FakeTranscriber(),
        config={"asr_provider": "stepaudio", "asr_model": "new-asr"},
    )

    assert result.success is True
    assert result.from_cache is False
    assert result.chunks == [TranscriptChunk(2, 4, "新模型转写")]


def test_transcribe_video_rebuilds_cache_when_asr_language_changes(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    transcript.save_transcript_cache(
        str(video_path),
        [TranscriptChunk(0, 1, "中文缓存")],
        str(cache_path),
        config={"asr_provider": "stepaudio", "asr_language": "zh"},
    )

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "English transcript")])

    result = transcript.transcribe_video(
        str(video_path),
        str(cache_path.parent),
        FakeTranscriber(),
        config={"asr_provider": "stepaudio", "asr_language": "en"},
    )

    assert result.success is True
    assert result.from_cache is False
    assert result.chunks == [TranscriptChunk(2, 4, "English transcript")]


def test_load_transcript_cache_rejects_legacy_cache_without_asr_signature(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    cache_path.parent.mkdir()
    cache_path.write_text(
        json.dumps(
            {
                "source": {
                    "path": str(video_path.resolve()),
                    "size": video_path.stat().st_size,
                    "mtime_ns": video_path.stat().st_mtime_ns,
                },
                "chunks": [{"start": 1, "end": 2, "text": "旧缓存"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert transcript.load_transcript_cache(str(video_path), str(cache_path)) is None


def test_transcribe_video_creates_configured_provider(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    calls = []

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "新转写")])

    def fake_create(config):
        calls.append(config)
        return FakeTranscriber()

    monkeypatch.setattr(transcript, "create_transcriber", fake_create)

    result = transcript.transcribe_video(
        str(video_path),
        str(tmp_path / "work"),
        config={"asr_provider": "fake"},
    )

    assert result.success is True
    assert result.chunks == [TranscriptChunk(2, 4, "新转写")]
    assert calls == [{"asr_provider": "fake"}]


def test_transcribe_video_reports_provider_unavailable(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")

    result = transcript.transcribe_video(
        str(video_path),
        str(tmp_path / "work"),
        config={"asr_provider": "stepaudio", "stepfun_api_key": ""},
    )

    assert result.success is False
    assert result.chunks == []
    assert result.error == "ASR provider stepaudio unavailable"


def test_transcribe_video_reports_provider_configuration_error(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")

    result = transcript.transcribe_video(
        str(video_path),
        str(tmp_path / "work"),
        config={"asr_provider": "unknown"},
    )

    assert result.success is False
    assert result.chunks == []
    assert result.error == "ASR provider configuration error: Unknown ASR provider: unknown"


def test_transcribe_video_rebuilds_stale_cache(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("old", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    transcript.save_transcript_cache(str(video_path), [TranscriptChunk(0, 1, "旧缓存")], str(cache_path))
    video_path.write_text("new content", encoding="utf-8")

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            assert video_path_arg == str(video_path)
            assert work_dir == str(cache_path.parent)
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "新转写")])

    result = transcript.transcribe_video(str(video_path), str(cache_path.parent), FakeTranscriber())

    assert result.success is True
    assert result.from_cache is False
    assert result.chunks == [TranscriptChunk(2, 4, "新转写")]
    assert transcript.load_transcript_cache(str(video_path), str(cache_path)) == [TranscriptChunk(2, 4, "新转写")]


def test_transcribe_video_rebuilds_corrupted_cache(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    cache_path.parent.mkdir()
    cache_path.write_text("{bad json", encoding="utf-8")

    class FakeTranscriber:
        def is_available(self):
            return True

        def transcribe_video(self, video_path_arg, work_dir):
            return VideoTranscriptionResult(success=True, chunks=[TranscriptChunk(2, 4, "重新转写")])

    result = transcript.transcribe_video(str(video_path), str(cache_path.parent), FakeTranscriber())

    assert result.success is True
    assert result.chunks == [TranscriptChunk(2, 4, "重新转写")]
    assert transcript.load_transcript_cache(str(video_path), str(cache_path)) == [TranscriptChunk(2, 4, "重新转写")]


def test_load_transcript_cache_returns_none_for_missing_source_or_malformed_chunk(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    cache_path = tmp_path / "work" / "transcript.json"
    transcript.save_transcript_cache(str(video_path), [TranscriptChunk(1, 2, "文本")], str(cache_path))

    assert transcript.load_transcript_cache(str(tmp_path / "missing.mp4"), str(cache_path)) is None

    cache_path.write_text(
        (
            '{"source": {"path": "'
            + str(video_path)
            + '", "size": 5, "mtime_ns": '
            + str(video_path.stat().st_mtime_ns)
            + '}, "chunks": [{"end": 2, "text": "缺少开始时间"}]}'
        ),
        encoding="utf-8",
    )

    assert transcript.load_transcript_cache(str(video_path), str(cache_path)) is None


def test_whisper_transcribe_video_parses_timestamped_json(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        (tmp_path / "live.json").write_text(
            '{"segments": [{"start": 1.25, "end": 2.5, "text": " 第一段 "}, {"start": 3, "end": 4, "text": ""}]}',
            encoding="utf-8",
        )
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber(WhisperConfig(timeout=30)).transcribe_video(str(video_path), str(tmp_path))

    assert result.success is True
    assert result.chunks == [TranscriptChunk(1.25, 2.5, "第一段")]
    assert calls[0] == [
        transcript.sys.executable, "-m", "whisper", str(video_path),
        "--model", "small",
        "--language", "zh",
        "--output_format", "json",
        "--output_dir", str(tmp_path),
    ]


def test_whisper_transcribe_video_omits_text_without_timestamps(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        (tmp_path / "live.json").write_text('{"text": "没有时间戳的全文"}', encoding="utf-8")
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = WhisperTranscriber(WhisperConfig(timeout=30)).transcribe_video(str(video_path), str(tmp_path))

    assert result.success is True
    assert result.chunks == []


def test_stepaudio_is_unavailable_and_transcribe_fails_without_api_key(tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_text("video", encoding="utf-8")

    transcriber = StepAudioTranscriber(StepAudioConfig(api_key=""))

    assert transcriber.is_available() is False
    result = transcriber.transcribe_video(str(video_path), str(tmp_path))
    assert result == VideoTranscriptionResult(
        success=False,
        chunks=[],
        transcript_path=str(tmp_path / "live.stepaudio.json"),
        error="StepAudio API key missing",
    )


def test_stepaudio_transcribe_video_converts_success_response(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    captured = {}
    install_stepaudio_media_success(monkeypatch)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return (
                '{"segments": ['
                '{"start": 1.25, "end": 2.5, "text": " 第一段 "},'
                '{"start": 3, "end": 4, "text": ""}'
                "]}"
            ).encode("utf-8")

    def fake_request(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    transcriber = StepAudioTranscriber(
        StepAudioConfig(
            api_key="test-key",
            base_url="https://example.test/v1/",
            model="stepaudio-2.5-asr",
            language="zh",
            timeout=30,
        ),
        request_func=fake_request,
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is True
    assert result.chunks == [TranscriptChunk(1.25, 2.5, "第一段")]
    assert result.transcript_path == str(tmp_path / "live.stepaudio.json")
    assert json.loads((tmp_path / "live.stepaudio.json").read_text(encoding="utf-8"))["chunks"][0]["text"] == "第一段"
    assert captured["timeout"] == 30
    assert captured["request"].full_url == "https://example.test/v1/audio/transcriptions"
    body = captured["request"].data
    assert b'name="model"\r\n\r\nstepaudio-2.5-asr' in body
    assert b'name="language"\r\n\r\nzh' in body
    assert b'name="file"; filename="shard_0000.wav"' in body
    assert b"audio" in body


def test_stepaudio_transcribe_video_offsets_and_sorts_shard_chunks(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch, duration="70.0")
    responses = [
        '{"segments": [{"start": 5, "end": 10, "text": " 第一段 "}]}',
        '{"segments": [{"start": 0, "end": 30, "text": "第二段"}]}',
        '{"segments": [{"start": 0, "end": 5, "text": "第三段"}]}',
    ]
    calls = []

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_request(request, timeout):
        calls.append(request)
        return FakeResponse(responses[len(calls) - 1])

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key", shard_seconds=30),
        request_func=fake_request,
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is True
    assert result.chunks == [
        TranscriptChunk(5, 10, "第一段"),
        TranscriptChunk(30, 60, "第二段"),
        TranscriptChunk(60, 65, "第三段"),
    ]
    assert len(calls) == 3


def test_stepaudio_shard_cache_reuses_chunks_when_overall_cache_missing(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    work_dir = tmp_path / "work"
    install_stepaudio_media_success(monkeypatch, duration="70.0")
    calls = []
    responses = [
        '{"segments": [{"start": 0, "end": 10, "text": "第一段"}]}',
        '{"segments": [{"start": 0, "end": 20, "text": "第二段"}]}',
        '{"segments": [{"start": 0, "end": 5, "text": "第三段"}]}',
    ]

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_request(request, timeout):
        calls.append(request)
        return FakeResponse(responses[len(calls) - 1])

    config = {"asr_provider": "stepaudio", "asr_shard_seconds": 30}
    transcriber = StepAudioTranscriber(StepAudioConfig(api_key="test-key", shard_seconds=30), request_func=fake_request)

    first = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=config)
    assert first.success is True
    assert first.chunks == [
        TranscriptChunk(0, 10, "第一段"),
        TranscriptChunk(30, 50, "第二段"),
        TranscriptChunk(60, 65, "第三段"),
    ]
    assert (work_dir / "transcript.json").exists()
    assert (work_dir / "asr_shard_cache" / "shard_0000.json").exists()
    assert (work_dir / "asr_shard_cache" / "shard_0001.json").exists()
    assert (work_dir / "asr_shard_cache" / "shard_0002.json").exists()
    assert len(calls) == 3

    (work_dir / "transcript.json").unlink()
    transcriber.request_func = lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("valid shard cache should avoid StepAudio request")
    )

    second = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=config)

    assert second.success is True
    assert second.from_cache is False
    assert second.chunks == first.chunks
    assert len(calls) == 3


def test_stepaudio_corrupted_shard_cache_only_retries_that_shard(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    work_dir = tmp_path / "work"
    install_stepaudio_media_success(monkeypatch, duration="70.0")
    calls = []
    responses = [
        '{"segments": [{"start": 0, "end": 10, "text": "第一段"}]}',
        '{"segments": [{"start": 0, "end": 20, "text": "第二段"}]}',
        '{"segments": [{"start": 0, "end": 5, "text": "第三段"}]}',
        '{"segments": [{"start": 1, "end": 21, "text": "第二段重识别"}]}',
    ]

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_request(request, timeout):
        calls.append(request)
        return FakeResponse(responses[len(calls) - 1])

    config = {"asr_provider": "stepaudio", "asr_shard_seconds": 30}
    transcriber = StepAudioTranscriber(StepAudioConfig(api_key="test-key", shard_seconds=30), request_func=fake_request)

    first = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=config)
    assert first.success is True
    (work_dir / "transcript.json").unlink()
    (work_dir / "asr_shard_cache" / "shard_0001.json").write_text("{bad json", encoding="utf-8")

    second = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=config)

    assert second.success is True
    assert second.chunks == [
        TranscriptChunk(0, 10, "第一段"),
        TranscriptChunk(31, 51, "第二段重识别"),
        TranscriptChunk(60, 65, "第三段"),
    ]
    assert len(calls) == 4


def test_stepaudio_shard_cache_invalidates_when_audio_config_changes(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    work_dir = tmp_path / "work"
    install_stepaudio_media_success(monkeypatch, duration="10.0")
    calls = []
    responses = [
        '{"segments": [{"start": 0, "end": 5, "text": "旧配置"}]}',
        '{"segments": [{"start": 0, "end": 5, "text": "新配置"}]}',
    ]

    class FakeResponse:
        def __init__(self, body):
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return self.body.encode("utf-8")

    def fake_request(request, timeout):
        calls.append(request)
        return FakeResponse(responses[len(calls) - 1])

    old_config = {"asr_provider": "stepaudio", "asr_audio_sample_rate": 16000}
    new_config = {"asr_provider": "stepaudio", "asr_audio_sample_rate": 8000}
    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key", audio_sample_rate=16000),
        request_func=fake_request,
    )
    first = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=old_config)
    assert first.success is True

    (work_dir / "transcript.json").unlink()
    transcriber.config.audio_sample_rate = 8000
    second = transcript.transcribe_video(str(video_path), str(work_dir), transcriber, config=new_config)

    assert second.success is True
    assert second.chunks == [TranscriptChunk(0, 5, "新配置")]
    assert len(calls) == 2


def test_stepaudio_transcribe_video_rejects_invalid_shard_timestamp(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch, duration="10.0")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return '{"segments": [{"start": 10, "end": 5, "text": "坏时间戳"}]}'.encode("utf-8")

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=lambda *args, **kwargs: FakeResponse(),
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.chunks == []
    assert result.error == "StepAudio shard 0 returned invalid timestamp: 10-5"


def test_stepaudio_transcribe_video_rejects_oversized_shard_without_request(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch, duration="10.0")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("oversized shard should not be uploaded")

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key", max_upload_bytes=3),
        request_func=fail_if_called,
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.chunks == []
    assert result.error == "StepAudio shard 0 audio is too large for upload: 5 bytes > 3 bytes"


def test_stepaudio_audio_shard_reports_missing_file_without_request(tmp_path):
    def fail_if_called(*args, **kwargs):
        raise AssertionError("missing shard should not be uploaded")

    missing_path = tmp_path / "missing.wav"
    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=fail_if_called,
    )

    result = transcriber.transcribe_audio_shard(str(missing_path), shard_index=3)

    assert result.success is False
    assert result.chunks == []
    assert result.error == f"StepAudio shard 3 audio file missing: {missing_path}"


def test_prepare_stepaudio_audio_shards_extracts_and_cuts_contiguous_plan(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return completed(0, stdout='{"format": {"duration": "125.0"}}')
        Path(cmd[-1]).write_bytes(b"audio")
        return completed(0)

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = prepare_stepaudio_audio_shards(
        str(video_path),
        str(tmp_path / "work"),
        StepAudioConfig(shard_seconds=60, audio_sample_rate=8000, audio_channels=2, audio_format="mp3"),
    )

    assert result == [
        AudioShard(
            index=0,
            start=0.0,
            end=60.0,
            audio_path=str(tmp_path / "work" / "asr_shards" / "shard_0000.mp3"),
            cache_path=str(tmp_path / "work" / "asr_shard_cache" / "shard_0000.json"),
        ),
        AudioShard(
            index=1,
            start=60.0,
            end=120.0,
            audio_path=str(tmp_path / "work" / "asr_shards" / "shard_0001.mp3"),
            cache_path=str(tmp_path / "work" / "asr_shard_cache" / "shard_0001.json"),
        ),
        AudioShard(
            index=2,
            start=120.0,
            end=125.0,
            audio_path=str(tmp_path / "work" / "asr_shards" / "shard_0002.mp3"),
            cache_path=str(tmp_path / "work" / "asr_shard_cache" / "shard_0002.json"),
        ),
    ]
    assert calls[1] == [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn",
        "-ar", "8000",
        "-ac", "2",
        "-f", "mp3",
        str(tmp_path / "work" / "asr_audio" / "live.mp3"),
    ]
    assert calls[2] == [
        "ffmpeg", "-y",
        "-ss", "0",
        "-to", "60",
        "-i", str(tmp_path / "work" / "asr_audio" / "live.mp3"),
        "-vn",
        "-ar", "8000",
        "-ac", "2",
        "-f", "mp3",
        str(tmp_path / "work" / "asr_shards" / "shard_0000.mp3"),
    ]


def test_prepare_stepaudio_audio_shards_reports_ffmpeg_extract_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")

    def fake_run(cmd, **kwargs):
        if cmd[0] == "ffprobe":
            return completed(0, stdout='{"format": {"duration": "10.0"}}')
        return completed(1, stderr="decode failed")

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    result = prepare_stepaudio_audio_shards(str(video_path), str(tmp_path / "work"), StepAudioConfig())

    assert result.success is False
    assert result.error == "StepAudio audio extraction failed: decode failed"


def test_stepaudio_transcribe_video_reports_shard_cut_failure(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "ffprobe":
            return completed(0, stdout='{"format": {"duration": "10.0"}}')
        if len(calls) == 2:
            Path(cmd[-1]).write_bytes(b"audio")
            return completed(0)
        return completed(1, stderr="cut failed")

    monkeypatch.setattr(transcript.subprocess, "run", fake_run)

    transcriber = StepAudioTranscriber(StepAudioConfig(api_key="test-key", shard_seconds=5))
    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.error == "StepAudio shard 0 audio cut failed: cut failed"


def test_stepaudio_multipart_sanitizes_filename(tmp_path):
    audio_path = tmp_path / 'bad"name\n.wav'
    audio_path.write_bytes(b"audio")
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"segments": [{"start": 0, "end": 1, "text": "ok"}]}'

    def fake_request(request, timeout):
        captured["body"] = request.data
        return FakeResponse()

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=fake_request,
    )

    result = transcriber.transcribe_audio_shard(str(audio_path))

    assert result.success is True
    assert b'filename="bad_name_.wav"' in captured["body"]
    assert b'filename="bad"name' not in captured["body"]


def test_stepaudio_transcribe_video_supports_nested_segments(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return '{"data": {"segments": [{"start": 10, "end": 12, "text": "嵌套片段"}]}}'.encode("utf-8")

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=lambda *args, **kwargs: FakeResponse(),
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is True
    assert result.chunks == [TranscriptChunk(10, 12, "嵌套片段")]


def test_stepaudio_transcribe_video_returns_http_error(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch)

    def raise_http_error(*args, **kwargs):
        raise urllib.error.HTTPError(
            "https://example.test/v1/audio/transcriptions",
            500,
            "server error",
            {},
            io.BytesIO(b"failed"),
        )

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key", base_url="https://example.test/v1"),
        request_func=raise_http_error,
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.chunks == []
    assert result.error == "StepAudio request failed: HTTP 500: failed"


def test_stepaudio_transcribe_video_returns_invalid_json_error(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{bad json"

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=lambda *args, **kwargs: FakeResponse(),
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.chunks == []
    assert "StepAudio response is not valid JSON" in result.error


def test_stepaudio_transcribe_video_returns_missing_timestamp_error(monkeypatch, tmp_path):
    video_path = tmp_path / "live.mp4"
    video_path.write_bytes(b"video")
    install_stepaudio_media_success(monkeypatch)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"text": "no timestamps"}'

    transcriber = StepAudioTranscriber(
        StepAudioConfig(api_key="test-key"),
        request_func=lambda *args, **kwargs: FakeResponse(),
    )

    result = transcriber.transcribe_video(str(video_path), str(tmp_path))

    assert result.success is False
    assert result.chunks == []
    assert result.error == "StepAudio response missing timestamped segments"


def test_export_srt_writes_timestamped_subtitles(tmp_path):
    output_path = tmp_path / "transcript.srt"

    transcript.export_srt(
        [
            TranscriptChunk(1.234, 3.5, "第一段"),
            TranscriptChunk(10, 11, "  \n\t  "),
            TranscriptChunk(65, 66.789, "第二段\n第二行\t  第三行"),
        ],
        str(output_path),
    )

    assert output_path.read_text(encoding="utf-8") == (
        "1\n"
        "00:00:01,234 --> 00:00:03,500\n"
        "第一段\n\n"
        "2\n"
        "00:01:05,000 --> 00:01:06,789\n"
        "第二段 第二行 第三行\n\n"
    )
