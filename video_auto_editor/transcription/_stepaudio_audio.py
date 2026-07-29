"""把已验证素材准备为 StepAudio 使用的标准 PCM。"""

from __future__ import annotations

import hashlib
import os
import selectors
import shutil
import signal
import stat
import subprocess

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.workspace import (
    ManagedBinaryFile,
    WorkspaceFailure,
)

from ._normalized_audio import (
    PCM_BYTES_PER_MILLISECOND,
    PCM_BYTES_PER_SAMPLE,
    PCM_SAMPLE_RATE,
    NormalizedPcmAudio,
    _content_digest_hasher,
)
from .interface import (
    CacheUse,
    ExecutionFacts,
    ReadinessIssue,
    TranscriptionFailure,
    TranscriptionRequest,
)

_SOURCE_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_OUTPUT_DIRECTORY = "transcription-audio-v1"
_OUTPUT_FILE = f"{_OUTPUT_DIRECTORY}/normalized.s16le"
_PIPE_READ_BYTES = 1024 * 1024
_PROCESS_POLL_SECONDS = 0.05
_PROCESS_STOP_SECONDS = 0.5


class FFmpegNormalizedPcmPreparer:
    """以本地 FFmpeg 生成标准 PCM 的生产 Adapter。"""

    __slots__ = ("_executable",)

    def __init__(self, *, executable: str = "ffmpeg") -> None:
        if not isinstance(executable, str):
            raise TypeError("FFmpeg 可执行命令必须是字符串")
        if not executable.strip():
            raise ValueError("FFmpeg 可执行命令不能为空")
        self._executable = executable

    def check_readiness(self) -> tuple[ReadinessIssue, ...]:
        """只定位本地可执行文件，不处理素材或写入 workspace。"""
        try:
            available = shutil.which(self._executable) is not None
        except OSError:
            available = False
        if available:
            return ()
        return (
            ReadinessIssue(
                ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
                {
                    "component": "ffmpeg",
                    "operation": "ffmpeg.locate",
                    "reason_code": "tool.missing",
                },
            ),
        )

    def prepare(
        self,
        request: TranscriptionRequest,
    ) -> NormalizedPcmAudio:
        """把素材流式标准化到本次运行的受管临时文件。"""
        if not isinstance(request, TranscriptionRequest):
            raise TypeError("FFmpeg 音频准备只接受 TranscriptionRequest")
        safe_failure: TranscriptionFailure
        try:
            return self._prepare(request)
        except CancellationRequested:
            raise
        except TranscriptionFailure as failure:
            safe_failure = TranscriptionFailure(
                failure.error_code,
                execution_facts=failure.execution_facts,
                diagnostics=failure.diagnostics,
            )
        except WorkspaceFailure:
            safe_failure = _process_failure(
                return_code=-1,
                stderr_length=0,
                stderr_sha256=hashlib.sha256(b"").hexdigest(),
            )
        raise safe_failure from None

    def _prepare(
        self,
        request: TranscriptionRequest,
    ) -> NormalizedPcmAudio:
        cancellation = request.cancellation
        cancellation.raise_if_cancelled()
        descriptor = _open_verified_source(request)
        try:
            try:
                request.temporary_workspace.location(
                    _OUTPUT_DIRECTORY
                ).mkdir()
            except FileExistsError:
                pass
            output = request.temporary_workspace.location(_OUTPUT_FILE)
            digest = output.use_binary(
                "wb",
                lambda stream: _transcode(
                    executable=self._executable,
                    source_descriptor=descriptor,
                    duration_ms=request.source.duration_ms,
                    output=stream,
                    cancellation=cancellation,
                ),
            )
            _assert_source_unchanged(request, descriptor)
        finally:
            os.close(descriptor)
        cancellation.raise_if_cancelled()
        expected_bytes = (
            request.source.duration_ms * PCM_BYTES_PER_MILLISECOND
        )
        return NormalizedPcmAudio._from_managed(
            location=output,
            duration_ms=request.source.duration_ms,
            byte_length=expected_bytes,
            content_sha256=digest,
        )


def _open_verified_source(request: TranscriptionRequest) -> int:
    source = request.source.source_file
    try:
        descriptor = os.open(source.path, _SOURCE_FLAGS)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode):
                raise _input_failure("input.not_regular_file")
            if not source._matches_file_snapshot(
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
            ):
                raise _input_failure("input.read_failed")
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except TranscriptionFailure:
        raise
    except PermissionError:
        raise _input_failure("input.permission_denied") from None
    except OSError:
        raise _input_failure("input.read_failed") from None


def _assert_source_unchanged(
    request: TranscriptionRequest,
    descriptor: int,
) -> None:
    source = request.source.source_file
    try:
        descriptor_status = os.fstat(descriptor)
        path_status = os.stat(source.path, follow_symlinks=False)
    except PermissionError:
        raise _input_failure("input.permission_denied") from None
    except OSError:
        raise _input_failure("input.read_failed") from None
    for status in (descriptor_status, path_status):
        if not stat.S_ISREG(status.st_mode) or not source._matches_file_snapshot(
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ):
            raise _input_failure("input.read_failed")


def _transcode(
    *,
    executable: str,
    source_descriptor: int,
    duration_ms: int,
    output: ManagedBinaryFile,
    cancellation: CancellationToken,
) -> str:
    sample_length = duration_ms * PCM_SAMPLE_RATE // 1_000
    expected_bytes = duration_ms * PCM_BYTES_PER_MILLISECOND
    digest = _content_digest_hasher(expected_bytes, sample_length)
    command = [
        executable,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nostdin",
        "-i",
        f"/proc/self/fd/{source_descriptor}",
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-af",
        (
            f"aresample={PCM_SAMPLE_RATE}:async=1:first_pts=0,"
            f"apad=whole_len={sample_length},"
            f"atrim=end_sample={sample_length}"
        ),
        "-ac",
        "1",
        "-ar",
        str(PCM_SAMPLE_RATE),
        "-c:a",
        "pcm_s16le",
        "-f",
        "s16le",
        "pipe:1",
    ]
    cancellation.raise_if_cancelled()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(source_descriptor,),
            start_new_session=True,
            env={
                "LC_ALL": "C",
                "PATH": os.environ.get("PATH", os.defpath),
            },
        )
    except OSError:
        raise _audio_failure("media.spawn_failed") from None

    stderr_length = 0
    stderr_digest = hashlib.sha256()
    written = 0
    selector = selectors.DefaultSelector()
    try:
        if process.stdout is None or process.stderr is None:
            raise _process_failure(
                return_code=-1,
                stderr_length=0,
                stderr_sha256=stderr_digest.hexdigest(),
            )
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        while selector.get_map():
            cancellation.raise_if_cancelled()
            for key, _events in selector.select(_PROCESS_POLL_SECONDS):
                chunk = os.read(key.fd, _PIPE_READ_BYTES)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_length += len(chunk)
                    stderr_digest.update(chunk)
                    continue
                if written + len(chunk) > expected_bytes:
                    raise _audio_failure("media.output_invalid")
                _write_all(output, chunk)
                digest.update(chunk)
                written += len(chunk)
        return_code = _wait_for_process(process, cancellation)
        if return_code != 0:
            raise _process_failure(
                return_code=return_code,
                stderr_length=stderr_length,
                stderr_sha256=stderr_digest.hexdigest(),
            )
        if written == 0:
            raise _audio_failure("media.output_missing")
        if (
            written != expected_bytes
            or written % PCM_BYTES_PER_SAMPLE != 0
        ):
            raise _audio_failure("media.output_invalid")
        return digest.hexdigest()
    except CancellationRequested:
        _stop_process(process)
        raise
    except TranscriptionFailure:
        _stop_process(process)
        raise
    except OSError:
        _stop_process(process)
        raise _process_failure(
            return_code=_safe_return_code(process.returncode),
            stderr_length=stderr_length,
            stderr_sha256=stderr_digest.hexdigest(),
        ) from None
    except BaseException:
        _stop_process(process)
        raise
    finally:
        selector.close()
        _close_process_pipes(process)


def _write_all(output: ManagedBinaryFile, contents: bytes) -> None:
    remaining = memoryview(contents)
    while remaining:
        written = output.write(remaining)
        if written <= 0:
            raise OSError("受管 PCM 写入没有取得进展")
        remaining = remaining[written:]


def _wait_for_process(
    process: subprocess.Popen[bytes],
    cancellation: CancellationToken,
) -> int:
    while True:
        cancellation.raise_if_cancelled()
        try:
            return process.wait(timeout=_PROCESS_POLL_SECONDS)
        except subprocess.TimeoutExpired:
            continue


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        running = process.poll() is None
    except OSError:
        running = True
    if running:
        _signal_process_group(process, signal.SIGTERM)
    try:
        process.communicate(timeout=_PROCESS_STOP_SECONDS)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        try:
            process.communicate(timeout=_PROCESS_STOP_SECONDS)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            _close_process_pipes(process)
            try:
                process.wait(timeout=_PROCESS_STOP_SECONDS)
            except (OSError, subprocess.TimeoutExpired):
                pass
    except (OSError, ValueError):
        _signal_process_group(process, signal.SIGKILL)


def _signal_process_group(
    process: subprocess.Popen[bytes],
    signal_number: int,
) -> None:
    try:
        os.killpg(process.pid, signal_number)
        return
    except ProcessLookupError:
        return
    except OSError:
        pass
    try:
        process.send_signal(signal_number)
    except OSError:
        pass


def _close_process_pipes(process: subprocess.Popen[bytes]) -> None:
    for pipe in (process.stdout, process.stderr):
        if pipe is None:
            continue
        try:
            pipe.close()
        except OSError:
            pass


def _safe_return_code(return_code: int | None) -> int:
    if (
        return_code is None
        or return_code == 0
        or return_code < -255
        or return_code > 255
    ):
        return -1
    return return_code


def _input_failure(reason_code: str) -> TranscriptionFailure:
    return TranscriptionFailure(
        ErrorCode.INPUT_UNREADABLE,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={"reason_code": reason_code},
    )


def _audio_failure(reason_code: str) -> TranscriptionFailure:
    return TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={
            "operation": "ffmpeg.transcode",
            "reason_code": reason_code,
        },
    )


def _process_failure(
    *,
    return_code: int,
    stderr_length: int,
    stderr_sha256: str,
) -> TranscriptionFailure:
    return TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={
            "operation": "ffmpeg.transcode",
            "reason_code": "media.process_failed",
            "media_exit_code": _safe_return_code(return_code),
            "stderr_length": stderr_length,
            "stderr_sha256": stderr_sha256,
        },
    )
