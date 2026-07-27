"""基于同一素材文件身份完成摘要与 ffprobe 验证。"""

import hashlib
import json
import os
import signal
import stat
import subprocess
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.workspace import SourceFileCapability

from ._failure import (
    SourceAnalysisFailure,
    _without_sensitive_exception_context,
)
from ._model import SourceDescription

_READ_SIZE = 1024 * 1024
_PROBE_POLL_SECONDS = 0.05


class _InvalidProbePayload(ValueError):
    """标记不符合严格 JSON 约束的探测结果。"""


class SourceAnalysis:
    """以一个入口隐藏素材读取、探测、校验和摘要实现。"""

    __slots__ = ()

    @classmethod
    def analyze(
        cls,
        source: SourceFileCapability,
        cancellation: CancellationToken,
    ) -> SourceDescription:
        """验证 Workspace 签发的单个 MP4 并返回不可变描述。"""
        if not isinstance(source, SourceFileCapability):
            raise TypeError("素材分析只接受 Workspace 签发的素材")
        if not isinstance(cancellation, CancellationToken):
            raise TypeError("素材分析必须绑定取消令牌")
        return _without_sensitive_exception_context(
            lambda: cls._analyze(source, cancellation)
        )

    @classmethod
    def _analyze(
        cls,
        source: SourceFileCapability,
        cancellation: CancellationToken,
    ) -> SourceDescription:
        cancellation.raise_if_cancelled()
        if source.path.suffix.casefold() != ".mp4":
            raise SourceAnalysisFailure(
                ErrorCode.INPUT_UNSUPPORTED,
                {"reason_code": "input.extension_unsupported"},
            )

        try:
            descriptor = os.open(
                source.path,
                os.O_RDONLY
                | os.O_NOFOLLOW
                | os.O_CLOEXEC
                | os.O_NONBLOCK,
            )
            try:
                initial_status = os.fstat(descriptor)
                if not stat.S_ISREG(initial_status.st_mode):
                    raise SourceAnalysisFailure(
                        ErrorCode.INPUT_UNREADABLE,
                        {"reason_code": "input.not_regular_file"},
                    )
                if not source._matches_file_snapshot(
                    initial_status.st_dev,
                    initial_status.st_ino,
                    initial_status.st_size,
                    initial_status.st_mtime_ns,
                    initial_status.st_ctime_ns,
                ):
                    raise SourceAnalysisFailure(
                        ErrorCode.INPUT_UNREADABLE,
                        {"reason_code": "input.read_failed"},
                    )
                digest, byte_length = _hash_source(
                    descriptor,
                    cancellation,
                )
                if byte_length == 0:
                    raise SourceAnalysisFailure(
                        ErrorCode.INPUT_MEDIA_INVALID,
                        {"reason_code": "media.empty"},
                    )
                os.lseek(descriptor, 0, os.SEEK_SET)
                cancellation.raise_if_cancelled()
                probe = _probe_source(descriptor, cancellation)
                duration_ms = _validated_duration_ms(probe)
                cancellation.raise_if_cancelled()
                final_status = os.fstat(descriptor)
                path_status = os.stat(source.path, follow_symlinks=False)
                if (
                    byte_length != initial_status.st_size
                    or _file_identity(initial_status)
                    != _file_identity(final_status)
                    or not source._matches_file_snapshot(
                        path_status.st_dev,
                        path_status.st_ino,
                        path_status.st_size,
                        path_status.st_mtime_ns,
                        path_status.st_ctime_ns,
                    )
                ):
                    raise SourceAnalysisFailure(
                        ErrorCode.INPUT_UNREADABLE,
                        {"reason_code": "input.read_failed"},
                    )
            finally:
                os.close(descriptor)
        except SourceAnalysisFailure:
            raise
        except PermissionError as exc:
            raise SourceAnalysisFailure(
                ErrorCode.INPUT_UNREADABLE,
                {"reason_code": "input.permission_denied"},
            ) from exc
        except OSError as exc:
            raise SourceAnalysisFailure(
                ErrorCode.INPUT_UNREADABLE,
                {"reason_code": "input.read_failed"},
            ) from exc

        return SourceDescription._from_analysis(
            source_file=source,
            sha256=f"sha256:{digest}",
            byte_length=byte_length,
            duration_ms=duration_ms,
        )


def _hash_source(
    descriptor: int,
    cancellation: CancellationToken,
) -> tuple[str, int]:
    digest = hashlib.sha256()
    byte_length = 0
    while True:
        cancellation.raise_if_cancelled()
        chunk = os.read(descriptor, _READ_SIZE)
        if not chunk:
            return digest.hexdigest(), byte_length
        digest.update(chunk)
        byte_length += len(chunk)


def _probe_source(
    descriptor: int,
    cancellation: CancellationToken,
) -> dict[str, Any]:
    cancellation.raise_if_cancelled()
    try:
        process = subprocess.Popen(
            [
                "ffprobe",
                "-v",
                "error",
                "-print_format",
                "json",
                "-show_entries",
                "format=format_name,duration:stream=codec_type",
                f"/proc/self/fd/{descriptor}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(descriptor,),
            start_new_session=True,
        )
    except OSError as exc:
        raise SourceAnalysisFailure(
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {
                "operation": "ffprobe.probe",
                "reason_code": "media.spawn_failed",
            },
        ) from exc
    try:
        while True:
            cancellation.raise_if_cancelled()
            try:
                stdout, stderr = process.communicate(
                    timeout=_PROBE_POLL_SECONDS
                )
                break
            except subprocess.TimeoutExpired:
                continue
    except CancellationRequested:
        _stop_process(process)
        raise
    except OSError as exc:
        _stop_process(process)
        return_code = process.returncode
        raise SourceAnalysisFailure(
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {
                "operation": "ffprobe.probe",
                "reason_code": "media.process_failed",
                "media_exit_code": (
                    -1 if return_code is None else return_code
                ),
                "stderr_length": 0,
                "stderr_sha256": hashlib.sha256(b"").hexdigest(),
            },
        ) from exc
    except BaseException:
        _stop_process(process)
        raise
    if process.returncode != 0:
        return_code = process.returncode
        if return_code is None:
            raise SourceAnalysisFailure(
                ErrorCode.MEDIA_PROCESSING_FAILED,
                {
                    "operation": "ffprobe.probe",
                    "reason_code": "media.process_failed",
                    "media_exit_code": -1,
                    "stderr_length": len(stderr),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                },
            )
        if return_code < 0:
            raise SourceAnalysisFailure(
                ErrorCode.MEDIA_PROCESSING_FAILED,
                {
                    "operation": "ffprobe.probe",
                    "reason_code": "media.process_failed",
                    "media_exit_code": return_code,
                    "stderr_length": len(stderr),
                    "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
                },
            )
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {
                "reason_code": "media.probe_failed",
                "media_exit_code": return_code,
                "stderr_length": len(stderr),
                "stderr_sha256": hashlib.sha256(stderr).hexdigest(),
            },
        )
    invalid_payload = False
    try:
        payload = json.loads(
            stdout.decode("utf-8"),
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_finite_number,
        )
    except (UnicodeDecodeError, ValueError, RecursionError):
        payload = None
        invalid_payload = True
    if invalid_payload:
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.container_invalid"},
        )
    if not isinstance(payload, dict):
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.container_invalid"},
        )
    return payload


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for key, value in pairs:
        if key in payload:
            raise _InvalidProbePayload("探测结果包含重复字段")
        payload[key] = value
    return payload


def _reject_non_finite_number(value: str) -> Any:
    raise _InvalidProbePayload(f"探测结果包含非有限数值：{value}")


def _validated_duration_ms(payload: dict[str, Any]) -> int:
    format_payload = payload.get("format")
    streams = payload.get("streams")
    if not isinstance(format_payload, dict) or not isinstance(streams, list):
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.container_invalid"},
        )
    format_name = format_payload.get("format_name")
    if not isinstance(format_name, str) or not format_name.strip():
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.container_invalid"},
        )
    if "mp4" not in {
        name.strip().casefold() for name in format_name.split(",")
    }:
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_UNSUPPORTED,
            {"reason_code": "input.container_unsupported"},
        )
    stream_types: set[str] = set()
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        if isinstance(codec_type, str):
            stream_types.add(codec_type)
    if "video" not in stream_types:
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            {
                "reason_code": "media.stream_missing",
                "stream_type": "video",
            },
        )
    if "audio" not in stream_types:
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            {
                "reason_code": "media.stream_missing",
                "stream_type": "audio",
            },
        )
    raw_duration = format_payload.get("duration")
    if isinstance(raw_duration, bool) or not isinstance(
        raw_duration,
        (str, int, float),
    ):
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.duration_invalid"},
        )
    try:
        duration = Decimal(str(raw_duration))
        duration_ms = int(
            (duration * 1000).quantize(
                Decimal(1),
                rounding=ROUND_HALF_UP,
            )
        )
    except (InvalidOperation, ValueError, OverflowError):
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.duration_invalid"},
        ) from None
    if not duration.is_finite() or duration_ms <= 0:
        raise SourceAnalysisFailure(
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.duration_invalid"},
        )
    return duration_ms


def _file_identity(status: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    try:
        running = process.poll() is None
    except OSError:
        running = True
    if running:
        _signal_process_group(process, signal.SIGTERM)
    try:
        process.communicate(timeout=0.5)
    except subprocess.TimeoutExpired:
        _signal_process_group(process, signal.SIGKILL)
        try:
            process.communicate(timeout=0.5)
        except (OSError, subprocess.TimeoutExpired):
            _close_process_pipes(process)
            try:
                process.wait(timeout=0.5)
            except (OSError, subprocess.TimeoutExpired):
                pass
    except OSError:
        _signal_process_group(process, signal.SIGKILL)
        _close_process_pipes(process)


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
