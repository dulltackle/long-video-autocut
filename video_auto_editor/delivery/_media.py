"""把优化字幕烧录进短视频并流式写入受管暂存目录。"""

import hashlib
import os
import selectors
import signal
import stat
import subprocess
from dataclasses import dataclass

from video_auto_editor.clip_planning import ShortVideo
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationToken,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.subtitle_optimization import (
    OptimizedShortVideoSubtitles,
)
from video_auto_editor.workspace import ManagedBinaryFile

from ._model import DeliveryBuildFailure, DeliveryBuildRequest


_SOURCE_FLAGS = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_PROCESS_POLL_SECONDS = 0.05
_PROCESS_STOP_SECONDS = 0.5
_PIPE_READ_BYTES = 1024 * 1024


@dataclass(frozen=True, slots=True)
class BuiltMedia:
    """供 manifest 使用的单条已落盘媒体事实。"""

    path: str
    byte_length: int
    sha256: str


def export_short_videos(
    request: DeliveryBuildRequest,
) -> tuple[BuiltMedia, ...]:
    """按类型化标识关联字幕，并导出全部或失败。"""
    if not request.plan.short_videos:
        return ()
    source_descriptor = _open_verified_source(request)
    subtitle_by_id = {
        item.short_video_id: item
        for item in request.subtitles.short_videos
    }
    built: list[BuiltMedia] = []
    try:
        for short_video in request.plan.short_videos:
            request.cancellation.raise_if_cancelled()
            relative_path = f"clips/{short_video.short_video_id}.mp4"
            optimized = subtitle_by_id[short_video.short_video_id]
            artifact = request.staging_directory.location(
                relative_path
            ).use_binary(
                "xb",
                lambda output, short_video=short_video, optimized=optimized,
                relative_path=relative_path: _export_one(
                    request=request,
                    source_descriptor=source_descriptor,
                    short_video=short_video,
                    optimized=optimized,
                    relative_path=relative_path,
                    output=output,
                ),
            )
            built.append(artifact)
        _assert_source_unchanged(request, source_descriptor)
    finally:
        try:
            os.close(source_descriptor)
        except OSError:
            pass
    request.cancellation.raise_if_cancelled()
    return tuple(built)


def _open_verified_source(request: DeliveryBuildRequest) -> int:
    source = request.source.source_file
    try:
        descriptor = os.open(source.path, _SOURCE_FLAGS)
        try:
            status = os.fstat(descriptor)
            if not stat.S_ISREG(status.st_mode) or not source._matches_file_snapshot(
                status.st_dev,
                status.st_ino,
                status.st_size,
                status.st_mtime_ns,
                status.st_ctime_ns,
            ):
                raise _source_invariant_failure()
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise
    except DeliveryBuildFailure:
        raise
    except (OSError, PermissionError):
        raise _source_invariant_failure() from None


def _assert_source_unchanged(
    request: DeliveryBuildRequest,
    descriptor: int,
) -> None:
    source = request.source.source_file
    try:
        descriptor_status = os.fstat(descriptor)
        path_status = os.stat(source.path, follow_symlinks=False)
    except (OSError, PermissionError):
        raise _source_invariant_failure() from None
    for status in (descriptor_status, path_status):
        if not stat.S_ISREG(status.st_mode) or not source._matches_file_snapshot(
            status.st_dev,
            status.st_ino,
            status.st_size,
            status.st_mtime_ns,
            status.st_ctime_ns,
        ):
            raise _source_invariant_failure()


def _export_one(
    *,
    request: DeliveryBuildRequest,
    source_descriptor: int,
    short_video: ShortVideo,
    optimized: OptimizedShortVideoSubtitles,
    relative_path: str,
    output: ManagedBinaryFile,
) -> BuiltMedia:
    try:
        subtitle_bytes = _clip_srt(short_video, optimized)
    except (UnicodeError, ValueError):
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "artifact_role": "short_video_media",
                "reason_code": "delivery.render_failed",
            },
        ) from None
    subtitle_descriptor = _subtitle_descriptor(subtitle_bytes)
    try:
        return _transcode(
            request=request,
            source_descriptor=source_descriptor,
            subtitle_descriptor=subtitle_descriptor,
            short_video=short_video,
            relative_path=relative_path,
            output=output,
        )
    finally:
        try:
            os.close(subtitle_descriptor)
        except OSError:
            pass


def _clip_srt(
    short_video: ShortVideo,
    optimized: OptimizedShortVideoSubtitles,
) -> bytes:
    body = "".join(
        (
            f"{index}\n"
            f"{_srt_timestamp(block.start_ms - short_video.final_start_ms)}"
            " --> "
            f"{_srt_timestamp(block.end_ms - short_video.final_start_ms)}\n"
            f"{block.text}\n\n"
        )
        for index, block in enumerate(optimized.display_blocks, start=1)
    )
    return body.encode("utf-8", errors="strict")


def _subtitle_descriptor(contents: bytes) -> int:
    creator = getattr(os, "memfd_create", None)
    close_on_exec = getattr(os, "MFD_CLOEXEC", None)
    if creator is None or not isinstance(close_on_exec, int):
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "artifact_role": "short_video_media",
                "reason_code": "delivery.render_failed",
            },
        )
    descriptor = -1
    try:
        descriptor = creator(
            "video-auto-editor-subtitles",
            flags=close_on_exec,
        )
        remaining = memoryview(contents)
        while remaining:
            written = os.write(descriptor, remaining)
            if written <= 0:
                raise OSError("内存字幕写入没有取得进展")
            remaining = remaining[written:]
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except OSError:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        raise DeliveryBuildFailure(
            ErrorCode.DELIVERY_BUILD_FAILED,
            {
                "artifact_role": "short_video_media",
                "reason_code": "delivery.render_failed",
            },
        ) from None


def _transcode(
    *,
    request: DeliveryBuildRequest,
    source_descriptor: int,
    subtitle_descriptor: int,
    short_video: ShortVideo,
    relative_path: str,
    output: ManagedBinaryFile,
) -> BuiltMedia:
    style = request.subtitle_style
    force_style = (
        f"FontName={style.font},"
        f"FontSize={style.font_size},"
        "PrimaryColour=&H00FFFFFF,"
        "OutlineColour=&H00000000,"
        "BorderStyle=1,"
        f"Outline={style.outline},"
        "Shadow=0,"
        "Alignment=2,"
        f"MarginV={style.margin_bottom}"
    )
    subtitle_filter = (
        f"subtitles=filename='/proc/self/fd/{subtitle_descriptor}':"
        f"force_style='{force_style}'"
    )
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostats",
        "-nostdin",
        "-ss",
        _seconds(short_video.final_start_ms),
        "-i",
        f"/proc/self/fd/{source_descriptor}",
        "-t",
        _seconds(short_video.duration_ms),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0",
        "-vf",
        subtitle_filter,
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "fast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "frag_keyframe+empty_moov",
        "-f",
        "mp4",
        "pipe:1",
    ]
    request.cancellation.raise_if_cancelled()
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(source_descriptor, subtitle_descriptor),
            start_new_session=True,
            env={
                "LC_ALL": "C",
                "PATH": os.environ.get("PATH", os.defpath),
            },
        )
    except OSError:
        raise _export_failure("media.spawn_failed") from None

    stderr_length = 0
    stderr_digest = hashlib.sha256()
    output_length = 0
    output_digest = hashlib.sha256()
    selector = selectors.DefaultSelector()
    try:
        if process.stdout is None or process.stderr is None:
            raise _process_failure(
                reason_code="media.process_failed",
                return_code=-1,
                stderr_length=0,
                stderr_sha256=stderr_digest.hexdigest(),
            )
        try:
            selector.register(
                process.stdout,
                selectors.EVENT_READ,
                "stdout",
            )
            selector.register(
                process.stderr,
                selectors.EVENT_READ,
                "stderr",
            )
        except OSError:
            raise _process_failure(
                reason_code="media.process_failed",
                return_code=-1,
                stderr_length=0,
                stderr_sha256=stderr_digest.hexdigest(),
            ) from None
        while selector.get_map():
            request.cancellation.raise_if_cancelled()
            try:
                ready = selector.select(_PROCESS_POLL_SECONDS)
            except OSError:
                raise _process_failure(
                    reason_code="media.process_failed",
                    return_code=_safe_return_code(process.returncode),
                    stderr_length=stderr_length,
                    stderr_sha256=stderr_digest.hexdigest(),
                ) from None
            for key, _events in ready:
                try:
                    chunk = os.read(key.fd, _PIPE_READ_BYTES)
                except OSError:
                    raise _process_failure(
                        reason_code="media.process_failed",
                        return_code=_safe_return_code(process.returncode),
                        stderr_length=stderr_length,
                        stderr_sha256=stderr_digest.hexdigest(),
                    ) from None
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                if key.data == "stderr":
                    stderr_length += len(chunk)
                    stderr_digest.update(chunk)
                    continue
                _write_all(output, chunk)
                output_digest.update(chunk)
                output_length += len(chunk)
        try:
            return_code = _wait_for_process(
                process,
                request.cancellation,
            )
        except OSError:
            raise _process_failure(
                reason_code="media.process_failed",
                return_code=_safe_return_code(process.returncode),
                stderr_length=stderr_length,
                stderr_sha256=stderr_digest.hexdigest(),
            ) from None
        if return_code != 0:
            raise _process_failure(
                reason_code=(
                    "media.subtitle_burn_failed"
                    if return_code > 0
                    else "media.process_failed"
                ),
                return_code=return_code,
                stderr_length=stderr_length,
                stderr_sha256=stderr_digest.hexdigest(),
            )
        if output_length == 0:
            raise _export_failure("media.output_missing")
        return BuiltMedia(
            path=relative_path,
            byte_length=output_length,
            sha256="sha256:" + output_digest.hexdigest(),
        )
    except CancellationRequested:
        _stop_process(process)
        raise
    except DeliveryBuildFailure:
        _stop_process(process)
        raise
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
            raise OSError("受管媒体写入没有取得进展")
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
        _close_process_pipes(process)
        try:
            process.wait(timeout=_PROCESS_STOP_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            pass


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


def _source_invariant_failure() -> DeliveryBuildFailure:
    return DeliveryBuildFailure(
        ErrorCode.DELIVERY_BUILD_FAILED,
        {
            "artifact_role": "short_video_media",
            "reason_code": "delivery.invariant_violation",
        },
    )


def _export_failure(reason_code: str) -> DeliveryBuildFailure:
    return DeliveryBuildFailure(
        ErrorCode.DELIVERY_EXPORT_FAILED,
        {
            "operation": "ffmpeg.subtitle_burn",
            "artifact_role": "short_video_media",
            "reason_code": reason_code,
        },
    )


def _process_failure(
    *,
    reason_code: str,
    return_code: int,
    stderr_length: int,
    stderr_sha256: str,
) -> DeliveryBuildFailure:
    return DeliveryBuildFailure(
        ErrorCode.DELIVERY_EXPORT_FAILED,
        {
            "operation": "ffmpeg.subtitle_burn",
            "artifact_role": "short_video_media",
            "reason_code": reason_code,
            "media_exit_code": _safe_return_code(return_code),
            "stderr_length": stderr_length,
            "stderr_sha256": stderr_sha256,
        },
    )


def _seconds(milliseconds: int) -> str:
    seconds, remainder = divmod(milliseconds, 1_000)
    return f"{seconds}.{remainder:03d}"


def _srt_timestamp(milliseconds: int) -> str:
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"
