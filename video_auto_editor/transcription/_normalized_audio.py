"""语音识别使用的标准 PCM 与独立语音证据。"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from video_auto_editor.workspace import (
    ManagedBinaryFile,
    ManagedPathCapability,
    WorkspaceFailure,
)

from ._reconciliation import TimeInterval

PCM_SAMPLE_RATE = 16_000
PCM_CHANNELS = 1
PCM_BITS_PER_SAMPLE = 16
PCM_BYTES_PER_SAMPLE = PCM_BITS_PER_SAMPLE // 8
PCM_SAMPLES_PER_MILLISECOND = PCM_SAMPLE_RATE // 1_000
PCM_BYTES_PER_MILLISECOND = (
    PCM_SAMPLES_PER_MILLISECOND * PCM_CHANNELS * PCM_BYTES_PER_SAMPLE
)
AUDIO_NORMALIZATION_VERSION = "pcm-s16le-16khz-mono.v1"
SPEECH_EVIDENCE_VERSION = "pcm-potential-speech.v2"

_SPEECH_FRAME_MS = 20
_SPEECH_FRAME_BYTES = _SPEECH_FRAME_MS * PCM_BYTES_PER_MILLISECOND
_SPEECH_GAP_MERGE_MS = 200
_STREAM_READ_BYTES = 1024 * 1024
_FULL_SHA256 = re.compile(r"[0-9a-f]{64}")


class _Digest(Protocol):
    def update(self, contents: bytes) -> None:
        ...

    def hexdigest(self) -> str:
        ...


class _CancellationView(Protocol):
    def raise_if_cancelled(self) -> None:
        ...


class NormalizedPcmReadFailure(RuntimeError):
    """受管标准 PCM 在后续读取时不再满足已认证内容。"""


@dataclass(frozen=True, slots=True, init=False)
class NormalizedPcmAudio:
    """可随机读取、但生产时不把整场载入内存的标准 PCM。"""

    duration_ms: int
    byte_length: int
    content_sha256: str
    sample_length: int
    _inline_pcm: bytes | None = field(repr=False, compare=False)
    _managed_location: ManagedPathCapability | None = field(
        repr=False,
        compare=False,
    )

    def __init__(self, pcm: bytes, duration_ms: int) -> None:
        """为确定性测试和单个识别分片建立内联标准 PCM。"""
        if not isinstance(pcm, bytes):
            raise TypeError("标准化音频 PCM 必须是 bytes")
        expected_length, sample_length = _validated_shape(
            duration_ms,
            len(pcm),
        )
        digest = _content_digest_hasher(expected_length, sample_length)
        digest.update(pcm)
        object.__setattr__(self, "duration_ms", duration_ms)
        object.__setattr__(self, "byte_length", expected_length)
        object.__setattr__(self, "sample_length", sample_length)
        object.__setattr__(self, "content_sha256", digest.hexdigest())
        object.__setattr__(self, "_inline_pcm", pcm)
        object.__setattr__(self, "_managed_location", None)

    @classmethod
    def _from_managed(
        cls,
        *,
        location: ManagedPathCapability,
        duration_ms: int,
        byte_length: int,
        content_sha256: str,
    ) -> NormalizedPcmAudio:
        """由生产音频准备器绑定已经完整校验的受管 PCM。"""
        if not isinstance(location, ManagedPathCapability):
            raise TypeError("标准化音频受管位置必须由 Workspace 签发")
        expected_length, sample_length = _validated_shape(
            duration_ms,
            byte_length,
        )
        if (
            not isinstance(content_sha256, str)
            or _FULL_SHA256.fullmatch(content_sha256) is None
        ):
            raise ValueError("标准化音频必须包含完整规范 SHA-256")
        instance = object.__new__(cls)
        object.__setattr__(instance, "duration_ms", duration_ms)
        object.__setattr__(instance, "byte_length", expected_length)
        object.__setattr__(instance, "sample_length", sample_length)
        object.__setattr__(instance, "content_sha256", content_sha256)
        object.__setattr__(instance, "_inline_pcm", None)
        object.__setattr__(instance, "_managed_location", location)
        return instance

    @property
    def pcm(self) -> bytes:
        """只为内联测试数据返回整段 PCM；生产后端拒绝整场读取。"""
        pcm = self._inline_pcm
        if pcm is None:
            raise ValueError("受管标准 PCM 不允许整场读取")
        return pcm

    def slice(self, interval: TimeInterval) -> bytes:
        """在整场完整性扫描后读取当前请求的精确 PCM。

        生产调用方必须先以 ``_visit_chunks`` 或
        ``confirmed_speech_intervals`` 完成整场长度与摘要验证。
        """
        if not isinstance(interval, TimeInterval):
            raise TypeError("标准化音频只能按 TimeInterval 切分")
        if interval.end_ms > self.duration_ms:
            raise ValueError("标准化音频请求区不得越过素材末尾")
        start = interval.start_ms * PCM_BYTES_PER_MILLISECOND
        end = interval.end_ms * PCM_BYTES_PER_MILLISECOND
        inline = self._inline_pcm
        if inline is not None:
            return inline[start:end]
        location = self._managed_location
        if location is None:
            raise RuntimeError("标准化音频没有可用存储")

        def read_interval(stream: ManagedBinaryFile) -> bytes:
            stream.seek(start)
            remaining = end - start
            chunks: list[bytes] = []
            while remaining:
                chunk = stream.read(remaining)
                if not chunk:
                    raise NormalizedPcmReadFailure(
                        "受管标准 PCM 内容被截断"
                    )
                chunks.append(chunk)
                remaining -= len(chunk)
            return b"".join(chunks)

        try:
            return location.use_binary("rb", read_interval)
        except NormalizedPcmReadFailure:
            raise
        except (OSError, WorkspaceFailure):
            raise NormalizedPcmReadFailure(
                "受管标准 PCM 读取失败"
            ) from None

    def _visit_chunks(
        self,
        visitor: Callable[[int, bytes], None],
        *,
        cancellation: _CancellationView | None,
    ) -> None:
        if not callable(visitor):
            raise TypeError("标准 PCM 分块访问必须使用可调用效果")
        if cancellation is not None and not callable(
            getattr(cancellation, "raise_if_cancelled", None)
        ):
            raise TypeError("标准 PCM 分块访问必须绑定 CancellationToken")

        def visit_inline(pcm: bytes) -> None:
            for offset in range(0, len(pcm), _STREAM_READ_BYTES):
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                visitor(offset, pcm[offset : offset + _STREAM_READ_BYTES])

        inline = self._inline_pcm
        if inline is not None:
            visit_inline(inline)
            return
        location = self._managed_location
        if location is None:
            raise RuntimeError("标准化音频没有可用存储")

        def visit_managed(stream: ManagedBinaryFile) -> None:
            digest = _content_digest_hasher(
                self.byte_length,
                self.sample_length,
            )
            offset = 0
            while offset < self.byte_length:
                if cancellation is not None:
                    cancellation.raise_if_cancelled()
                chunk = stream.read(
                    min(_STREAM_READ_BYTES, self.byte_length - offset)
                )
                if not chunk:
                    raise NormalizedPcmReadFailure(
                        "受管标准 PCM 内容被截断"
                    )
                digest.update(chunk)
                visitor(offset, chunk)
                offset += len(chunk)
            if stream.read(1):
                raise NormalizedPcmReadFailure(
                    "受管标准 PCM 内容超出认证长度"
                )
            if digest.hexdigest() != self.content_sha256:
                raise NormalizedPcmReadFailure(
                    "受管标准 PCM 内容摘要不匹配"
                )

        try:
            location.use_binary("rb", visit_managed)
        except NormalizedPcmReadFailure:
            raise
        except (OSError, WorkspaceFailure):
            raise NormalizedPcmReadFailure(
                "受管标准 PCM 读取失败"
            ) from None


def _validated_shape(duration_ms: int, byte_length: int) -> tuple[int, int]:
    if not isinstance(duration_ms, int) or isinstance(duration_ms, bool):
        raise TypeError("标准化音频时长必须是整数毫秒")
    if duration_ms <= 0:
        raise ValueError("标准化音频时长必须为正整数毫秒")
    if not isinstance(byte_length, int) or isinstance(byte_length, bool):
        raise TypeError("标准化音频字节数必须是整数")
    expected_length = duration_ms * PCM_BYTES_PER_MILLISECOND
    if byte_length != expected_length:
        raise ValueError("标准化音频字节数必须与素材时长严格一致")
    return expected_length, byte_length // PCM_BYTES_PER_SAMPLE


def _content_digest_hasher(
    byte_length: int,
    sample_length: int,
) -> _Digest:
    if (
        not isinstance(byte_length, int)
        or isinstance(byte_length, bool)
        or byte_length <= 0
    ):
        raise ValueError("标准化音频摘要字节数必须是正整数")
    if (
        not isinstance(sample_length, int)
        or isinstance(sample_length, bool)
        or sample_length <= 0
    ):
        raise ValueError("标准化音频摘要样本数必须是正整数")
    if sample_length * PCM_BYTES_PER_SAMPLE != byte_length:
        raise ValueError("标准化音频摘要样本数与字节数不一致")
    descriptor = json.dumps(
        {
            "bits_per_sample": PCM_BITS_PER_SAMPLE,
            "byte_length": byte_length,
            "channels": PCM_CHANNELS,
            "format": "s16le",
            "normalization_version": AUDIO_NORMALIZATION_VERSION,
            "sample_length": sample_length,
            "sample_rate": PCM_SAMPLE_RATE,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    digest = hashlib.sha256()
    digest.update(descriptor)
    digest.update(b"\0")
    return digest


def confirmed_speech_intervals(
    audio: NormalizedPcmAudio,
    cancellation: _CancellationView | None = None,
) -> tuple[TimeInterval, ...]:
    """仅把全零 PCM 认证为静音，确定性生成保守潜在语音区间。"""
    if not isinstance(audio, NormalizedPcmAudio):
        raise TypeError("语音证据只能从标准化 PCM 生成")
    if cancellation is not None and not callable(
        getattr(cancellation, "raise_if_cancelled", None)
    ):
        raise TypeError("语音证据扫描必须绑定 CancellationToken")

    merged: list[TimeInterval] = []
    pending: TimeInterval | None = None
    remainder = b""
    processed_bytes = 0

    def inspect_chunk(_offset: int, chunk: bytes) -> None:
        nonlocal pending, processed_bytes, remainder
        data = remainder + chunk
        complete_length = (
            len(data) // _SPEECH_FRAME_BYTES * _SPEECH_FRAME_BYTES
        )
        for frame_start in range(0, complete_length, _SPEECH_FRAME_BYTES):
            if cancellation is not None:
                cancellation.raise_if_cancelled()
            frame = data[
                frame_start : frame_start + _SPEECH_FRAME_BYTES
            ]
            pending = _updated_speech_interval(
                frame,
                start_byte=processed_bytes + frame_start,
                duration_ms=audio.duration_ms,
                merged=merged,
                pending=pending,
            )
        processed_bytes += complete_length
        remainder = data[complete_length:]

    audio._visit_chunks(inspect_chunk, cancellation=cancellation)
    if remainder:
        pending = _updated_speech_interval(
            remainder,
            start_byte=processed_bytes,
            duration_ms=audio.duration_ms,
            merged=merged,
            pending=pending,
        )
    if pending is not None:
        merged.append(pending)
    return tuple(merged)


def _updated_speech_interval(
    frame: bytes,
    *,
    start_byte: int,
    duration_ms: int,
    merged: list[TimeInterval],
    pending: TimeInterval | None,
) -> TimeInterval | None:
    if not _frame_contains_signal(frame):
        return pending
    start_ms = start_byte // PCM_BYTES_PER_MILLISECOND
    end_ms = min(
        duration_ms,
        (start_byte + len(frame)) // PCM_BYTES_PER_MILLISECOND,
    )
    if end_ms <= start_ms:
        return pending
    interval = TimeInterval(start_ms, end_ms)
    if pending is None:
        return interval
    if interval.start_ms - pending.end_ms <= _SPEECH_GAP_MERGE_MS:
        return TimeInterval(
            pending.start_ms,
            max(pending.end_ms, interval.end_ms),
        )
    merged.append(pending)
    return interval


def _frame_contains_signal(frame: bytes) -> bool:
    samples = array("h")
    samples.frombytes(frame)
    if sys.byteorder != "little":
        samples.byteswap()
    return any(sample != 0 for sample in samples)
