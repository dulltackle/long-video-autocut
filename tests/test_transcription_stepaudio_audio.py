import hashlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from threading import Thread
from time import monotonic, sleep

import pytest

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription._normalized_audio import (
    NormalizedPcmAudio,
    confirmed_speech_intervals,
)
from video_auto_editor.transcription._reconciliation import TimeInterval
from video_auto_editor.transcription._stepaudio_audio import (
    FFmpegNormalizedPcmPreparer,
)
from video_auto_editor.transcription.interface import (
    CacheUse,
    ReadinessIssue,
    TranscriptionFailure,
    TranscriptionRequest,
)
from video_auto_editor.workspace import ManagedPathCapability, Workspace


def _tree_snapshot(root: Path) -> tuple[tuple[object, ...], ...]:
    entries = []
    for path in sorted(root.rglob("*")):
        status = path.stat()
        entries.append(
            (
                path.relative_to(root).as_posix(),
                path.is_dir(),
                status.st_mode,
                status.st_size,
                status.st_mtime_ns,
                path.read_bytes() if path.is_file() else None,
            )
        )
    return tuple(entries)


def _transcription_request(
    tmp_path: Path,
    *,
    duration_ms: int = 1_000,
) -> tuple[TranscriptionRequest, object, Path]:
    source_path = tmp_path / "private-source-path.mp4"
    source_contents = b"verified-source-contents"
    source_path.write_bytes(source_contents)
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=len(source_contents),
        duration_ms=duration_ms,
    )
    return (
        TranscriptionRequest(
            source=source,
            temporary_workspace=run_workspace.temporary,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        ),
        run_workspace,
        workspace.root,
    )


def _install_pcm_ffmpeg(
    tmp_path: Path,
    *,
    pcm: bytes,
    expected_source: bytes = b"verified-source-contents",
) -> tuple[Path, Path]:
    executable = tmp_path / "fake-ffmpeg"
    command_log = tmp_path / "ffmpeg-command.json"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import json",
                "import sys",
                "from pathlib import Path",
                f"Path({str(command_log)!r}).write_text(",
                "    json.dumps(sys.argv[1:]),",
                "    encoding='utf-8',",
                ")",
                "input_path = sys.argv[sys.argv.index('-i') + 1]",
                f"if Path(input_path).read_bytes() != {expected_source!r}:",
                "    raise SystemExit(8)",
                f"sys.stdout.buffer.write({pcm!r})",
                "sys.stdout.buffer.flush()",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    return executable, command_log


def test_ffmpeg_pcm_readiness_is_local_read_only_and_repeatable(
    tmp_path,
    monkeypatch,
):
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    untouched = tmp_path / "untouched"
    untouched.mkdir()
    (untouched / "sentinel").write_bytes(b"unchanged")
    monkeypatch.setenv("PATH", str(empty_bin))
    preparer = FFmpegNormalizedPcmPreparer()
    before = _tree_snapshot(untouched)

    first = preparer.check_readiness()
    second = preparer.check_readiness()

    expected = (
        ReadinessIssue(
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            {
                "component": "ffmpeg",
                "operation": "ffmpeg.locate",
                "reason_code": "tool.missing",
            },
        ),
    )
    assert first == expected
    assert second == expected
    assert _tree_snapshot(untouched) == before


def test_ffmpeg_pcm_preparer_streams_exact_audio_into_managed_storage(
    tmp_path,
    monkeypatch,
):
    pcm = b"\xe8\x03" * 16_000
    executable, command_log = _install_pcm_ffmpeg(tmp_path, pcm=pcm)
    request, run_workspace, workspace_root = _transcription_request(tmp_path)
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    try:
        audio = preparer.prepare(request)

        assert audio.duration_ms == 1_000
        assert audio.byte_length == 32_000
        assert audio.sample_length == 16_000
        assert audio.content_sha256 == (
            "691f689352a747999e153751d562a858"
            "c33c945bded97fdc2c858f49ee49a82e"
        )
        with pytest.raises(ValueError, match="整场读取"):
            _ = audio.pcm

        def reject_whole_file_read(*args, **kwargs):
            del args, kwargs
            raise AssertionError("标准 PCM 不得整场读入内存")

        monkeypatch.setattr(
            ManagedPathCapability,
            "read_bytes",
            reject_whole_file_read,
        )
        assert audio.slice(TimeInterval(100, 200)) == b"\xe8\x03" * 1_600
        assert confirmed_speech_intervals(audio) == (
            TimeInterval(0, 1_000),
        )

        command = json.loads(command_log.read_text(encoding="utf-8"))
        input_argument = command[command.index("-i") + 1]
        assert input_argument.startswith("/proc/self/fd/")
        assert str(request.source.source_file.path) not in command
        assert command[-5:] == [
            "-c:a",
            "pcm_s16le",
            "-f",
            "s16le",
            "pipe:1",
        ]
        assert (
            "aresample=16000:async=1:first_pts=0,"
            "apad=whole_len=16000,atrim=end_sample=16000"
        ) in command
        normalized_path = next(
            workspace_root.glob(
                "work/tmp/*/scratch/"
                "transcription-audio-v1/normalized.s16le"
            )
        )
        assert normalized_path.stat().st_size == 32_000
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_passes_only_fixed_child_environment(
    tmp_path,
    monkeypatch,
):
    pcm = b"\xe8\x03" * 16_000
    executable, _command_log = _install_pcm_ffmpeg(tmp_path, pcm=pcm)
    monkeypatch.setenv("PATH", str(tmp_path))
    for name in (
        "STEPFUN_API_KEY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "FFREPORT",
        "SSLKEYLOGFILE",
    ):
        monkeypatch.setenv(name, f"{name.lower()}-canary")
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    preparer = FFmpegNormalizedPcmPreparer(executable=executable.name)
    actual_popen = subprocess.Popen
    captured_environments = []

    def capture_environment(*args, **kwargs):
        captured_environments.append(kwargs.get("env"))
        return actual_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", capture_environment)

    try:
        audio = preparer.prepare(request)

        assert audio.byte_length == 32_000
        assert captured_environments == [
            {
                "LC_ALL": "C",
                "PATH": str(tmp_path),
            }
        ]
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_normalizes_real_media(tmp_path):
    source_path = tmp_path / "course.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-y",
            str(source_path),
        ],
        check=True,
    )
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    request = TranscriptionRequest(
        source=SourceDescription._from_analysis(
            source_file=workspace.source,
            sha256="sha256:" + ("0" * 64),
            byte_length=source_path.stat().st_size,
            duration_ms=1_000,
        ),
        temporary_workspace=run_workspace.temporary,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    try:
        audio = FFmpegNormalizedPcmPreparer().prepare(request)

        assert audio.byte_length == 32_000
        assert len(audio.slice(TimeInterval(0, 1_000))) == 32_000
        assert confirmed_speech_intervals(audio) == (
            TimeInterval(0, 1_000),
        )
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_keeps_real_aac_digital_silence_absent(tmp_path):
    source_path = tmp_path / "silent-course.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-t",
            "1",
            "-c:a",
            "aac",
            "-y",
            str(source_path),
        ],
        check=True,
    )
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    request = TranscriptionRequest(
        source=SourceDescription._from_analysis(
            source_file=workspace.source,
            sha256="sha256:" + ("0" * 64),
            byte_length=source_path.stat().st_size,
            duration_ms=1_000,
        ),
        temporary_workspace=run_workspace.temporary,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    try:
        audio = FFmpegNormalizedPcmPreparer().prepare(request)

        assert audio.slice(TimeInterval(0, 1_000)) == b"\x00\x00" * 16_000
        assert confirmed_speech_intervals(audio) == ()
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_normalized_pcm_speech_scan_observes_cancellation():
    cancellation = CancellationSource(clock=lambda: 1.0)
    cancellation.request(signal.SIGTERM)
    audio = NormalizedPcmAudio(
        pcm=b"\xe8\x03" * 16_000,
        duration_ms=1_000,
    )

    with pytest.raises(CancellationRequested) as captured:
        confirmed_speech_intervals(audio, cancellation.token)

    assert captured.value.signal_number == signal.SIGTERM


@pytest.mark.parametrize("sample", [b"\x01\x00", b"\xff\xff"])
def test_normalized_pcm_treats_any_nonzero_sample_as_potential_speech(sample):
    pcm = bytearray(b"\x00\x00" * 16_000)
    sample_offset = 400 * 32
    pcm[sample_offset : sample_offset + 2] = sample
    audio = NormalizedPcmAudio(
        pcm=bytes(pcm),
        duration_ms=1_000,
    )

    assert confirmed_speech_intervals(audio) == (
        TimeInterval(400, 420),
    )


@pytest.mark.parametrize(
    ("pcm", "reason_code"),
    [
        (b"", "media.output_missing"),
        (b"\x00\x00" * 15_999, "media.output_invalid"),
        (b"\x00\x00" * 16_001, "media.output_invalid"),
    ],
)
def test_ffmpeg_pcm_preparer_rejects_incomplete_or_excess_output(
    tmp_path,
    pcm,
    reason_code,
):
    executable, _command_log = _install_pcm_ffmpeg(tmp_path, pcm=pcm)
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            preparer.prepare(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED
        )
        assert failure.execution_facts.cache_use is CacheUse.MISS
        assert failure.diagnostics == {
            "operation": "ffmpeg.transcode",
            "reason_code": reason_code,
        }
        assert failure.__cause__ is None
        assert failure.__context__ is None
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_maps_process_failure_without_stderr_leak(
    tmp_path,
):
    raw_stderr = b"/private/customer/source.mp4 decoder secret"
    executable = tmp_path / "failing-ffmpeg"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import sys",
                f"sys.stderr.buffer.write({raw_stderr!r})",
                "sys.stderr.buffer.flush()",
                "raise SystemExit(7)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            preparer.prepare(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED
        )
        assert failure.diagnostics == {
            "operation": "ffmpeg.transcode",
            "reason_code": "media.process_failed",
            "media_exit_code": 7,
            "stderr_length": len(raw_stderr),
            "stderr_sha256": hashlib.sha256(raw_stderr).hexdigest(),
        }
        assert raw_stderr.decode("utf-8") not in str(failure)
        assert raw_stderr.decode("utf-8") not in repr(failure)
        assert failure.__cause__ is None
        assert failure.__context__ is None
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_maps_spawn_failure_safely(tmp_path):
    missing_executable = tmp_path / "private-missing-ffmpeg"
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    preparer = FFmpegNormalizedPcmPreparer(
        executable=str(missing_executable)
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            preparer.prepare(request)

        failure = captured.value
        assert (
            failure.error_code
            is ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED
        )
        assert failure.diagnostics == {
            "operation": "ffmpeg.transcode",
            "reason_code": "media.spawn_failed",
        }
        assert str(missing_executable) not in str(failure)
        assert str(missing_executable) not in repr(failure)
        assert failure.__cause__ is None
        assert failure.__context__ is None
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_rejects_source_replaced_after_analysis(
    tmp_path,
):
    executable, command_log = _install_pcm_ffmpeg(
        tmp_path,
        pcm=b"\x00\x00" * 16_000,
    )
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    source_path = request.source.source_file.path
    source_path.rename(tmp_path / "original-source")
    source_path.write_bytes(b"replacement-with-different-identity")
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            preparer.prepare(request)

        failure = captured.value
        assert failure.error_code is ErrorCode.INPUT_UNREADABLE
        assert failure.diagnostics == {"reason_code": "input.read_failed"}
        assert command_log.exists() is False
        assert str(source_path) not in str(failure)
        assert str(source_path) not in repr(failure)
        assert failure.__cause__ is None
        assert failure.__context__ is None
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_does_not_follow_replacement_symlink(
    tmp_path,
):
    executable, command_log = _install_pcm_ffmpeg(
        tmp_path,
        pcm=b"\x00\x00" * 16_000,
    )
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    source_path = request.source.source_file.path
    original = tmp_path / "original-source"
    source_path.rename(original)
    source_path.symlink_to(original)
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            preparer.prepare(request)

        assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
        assert captured.value.diagnostics == {
            "reason_code": "input.read_failed"
        }
        assert command_log.exists() is False
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_ffmpeg_pcm_preparer_cancellation_stops_process_group(
    tmp_path,
):
    started_marker = tmp_path / "ffmpeg-started"
    descendant_survived = tmp_path / "descendant-survived"
    descendant_code = "\n".join(
        [
            "from pathlib import Path",
            "from time import sleep",
            "sleep(30)",
            f"Path({str(descendant_survived)!r}).write_bytes(b'survived')",
        ]
    )
    executable = tmp_path / "blocking-ffmpeg"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "from time import sleep",
                (
                    "descendant = subprocess.Popen("
                    f"[sys.executable, '-c', {descendant_code!r}])"
                ),
                (
                    f"Path({str(started_marker)!r}).write_text("
                    "str(descendant.pid), encoding='utf-8')"
                ),
                "sleep(30)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    request, run_workspace, _workspace_root = _transcription_request(tmp_path)
    cancellation = CancellationSource(clock=monotonic)
    request = replace(request, cancellation=cancellation.token)
    preparer = FFmpegNormalizedPcmPreparer(executable=str(executable))

    def request_after_start() -> None:
        while not started_marker.exists():
            sleep(0.01)
        cancellation.request(signal.SIGINT)

    requester = Thread(target=request_after_start, daemon=True)
    requester.start()
    started_at = monotonic()
    try:
        with pytest.raises(CancellationRequested) as captured:
            preparer.prepare(request)

        assert captured.value.signal_number == signal.SIGINT
        assert monotonic() - started_at < 2
        requester.join(timeout=1)
        descendant_pid = int(started_marker.read_text(encoding="utf-8"))
        reap_deadline = monotonic() + 2
        descendant_alive = True
        while monotonic() < reap_deadline:
            try:
                os.kill(descendant_pid, 0)
            except ProcessLookupError:
                descendant_alive = False
                break
            sleep(0.05)
        assert descendant_alive is False
        assert descendant_survived.exists() is False
    finally:
        run_workspace.cleanup()
        run_workspace.close()
