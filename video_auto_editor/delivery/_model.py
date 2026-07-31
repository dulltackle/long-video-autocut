"""DeliveryBuild 的公共输入与稳定失败。"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from video_auto_editor.clip_planning import DeliveryPlan
from video_auto_editor.configuration._model import SubtitleStyle
from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import (
    ErrorCategory,
    ErrorCode,
    freeze_error_diagnostics,
    get_error_definition,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.subtitle_optimization import SubtitleOptimizationResult
from video_auto_editor.transcription import CompleteTranscript
from video_auto_editor.workspace import (
    ManagedDirectoryCapability,
    ManagedDirectoryRole,
)


_APPLICATION_VERSION = re.compile(
    r"[0-9]+(?:\.[0-9]+){1,3}"
    r"(?:[-+][0-9A-Za-z][0-9A-Za-z.-]*)?"
)
_DELIVERY_FAILURE_CODES = frozenset(
    {
        ErrorCode.DELIVERY_BUILD_FAILED,
        ErrorCode.DELIVERY_EXPORT_FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class DeliveryBuildRequest:
    """构建一次未验证标准交付所需的完整不可变事实。"""

    run_id: RunId
    source: SourceDescription = field(repr=False)
    transcript: CompleteTranscript = field(repr=False)
    plan: DeliveryPlan = field(repr=False)
    subtitles: SubtitleOptimizationResult = field(repr=False)
    staging_directory: ManagedDirectoryCapability = field(repr=False)
    subtitle_style: SubtitleStyle = field(repr=False)
    application_version: str
    started_at: datetime
    published_at: datetime
    cancellation: CancellationToken = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, RunId):
            raise TypeError("交付构建必须绑定 RunId")
        if not isinstance(self.source, SourceDescription):
            raise TypeError("交付构建只接受已验证素材描述")
        if not isinstance(self.transcript, CompleteTranscript):
            raise TypeError("交付构建只接受完整忠实转写")
        if not isinstance(self.plan, DeliveryPlan):
            raise TypeError("交付构建只接受最终交付方案")
        if not isinstance(self.subtitles, SubtitleOptimizationResult):
            raise TypeError("交付构建只接受完整字幕优化结果")
        if not isinstance(
            self.staging_directory,
            ManagedDirectoryCapability,
        ):
            raise TypeError(
                "交付构建必须使用 Workspace 签发的受管暂存目录"
            )
        self.staging_directory._assert_authentic()
        if (
            self.staging_directory.role
            is not ManagedDirectoryRole.DELIVERY_STAGING
        ):
            raise ValueError("交付构建只能写入受管交付暂存目录")
        self.staging_directory._assert_bound_to_run(self.run_id)
        self.staging_directory._assert_current_directory()
        if not isinstance(self.subtitle_style, SubtitleStyle):
            raise TypeError("交付构建必须包含已生效字幕样式")
        if (
            not isinstance(self.application_version, str)
            or _APPLICATION_VERSION.fullmatch(self.application_version) is None
        ):
            raise ValueError("应用版本必须是规范版本号")
        _validate_timestamp(self.started_at, "运行开始时间")
        _validate_timestamp(self.published_at, "交付形成时间")
        if self.published_at < self.started_at:
            raise ValueError("交付形成时间不能早于运行开始时间")
        if not isinstance(self.cancellation, CancellationToken):
            raise TypeError("交付构建必须绑定根取消令牌")
        if self.plan.transcript_id != self.transcript.transcript_id:
            raise ValueError("交付方案必须引用当前完整转写")
        if self.plan.source_duration_ms != self.source.duration_ms:
            raise ValueError("交付方案必须引用当前素材时长")
        planned_ids = tuple(
            item.short_video_id for item in self.plan.short_videos
        )
        subtitle_ids = tuple(
            item.short_video_id for item in self.subtitles.short_videos
        )
        if set(subtitle_ids) != set(planned_ids):
            raise ValueError(
                "优化字幕必须按交付方案完整覆盖全部短视频"
            )
        subtitle_by_id = {
            item.short_video_id: item
            for item in self.subtitles.short_videos
        }
        for short_video in self.plan.short_videos:
            optimized = subtitle_by_id[short_video.short_video_id]
            if any(
                block.start_ms < short_video.final_start_ms
                or block.end_ms > short_video.final_end_ms
                for block in optimized.display_blocks
            ):
                raise ValueError(
                    "优化字幕显示块必须位于对应短视频素材范围内"
                )
        transcript_chunk_ids = {
            chunk.transcript_chunk_id for chunk in self.transcript.chunks
        }
        if any(
            identifier not in transcript_chunk_ids
            for candidate in self.plan.candidates
            for identifier in candidate.transcript_chunk_ids
        ):
            raise ValueError(
                "交付候选引用了当前完整转写之外的文本块"
            )
        previous_end = 0
        for chunk in self.transcript.chunks:
            if (
                chunk.start_ms < previous_end
                or chunk.end_ms > self.source.duration_ms
            ):
                raise ValueError(
                    "完整转写文本块必须按素材时间稳定排序"
                )
            previous_end = chunk.end_ms
        _validate_subtitle_style(self.subtitle_style)


class DeliveryBuildFailure(RuntimeError):
    """稳定且不携带路径、正文或工具原始输出的交付构建失败。"""

    __slots__ = (
        "category",
        "diagnostics",
        "error_code",
        "retryable_in_new_run",
        "safe_message",
    )

    def __init__(
        self,
        error_code: ErrorCode,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        if not isinstance(error_code, ErrorCode):
            raise TypeError("交付构建失败必须使用稳定 ErrorCode")
        if error_code not in _DELIVERY_FAILURE_CODES:
            raise ValueError("错误码不属于交付构建允许的稳定失败")
        definition = get_error_definition(error_code)
        self.error_code = error_code
        self.category: ErrorCategory = definition.category
        self.safe_message = definition.safe_message
        self.retryable_in_new_run = definition.retryable_in_new_run
        self.diagnostics = freeze_error_diagnostics(
            error_code,
            diagnostics,
        )
        super().__init__(self.safe_message)


def _validate_timestamp(value: object, field_name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name}必须是 datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}必须包含时区")


def _validate_subtitle_style(style: SubtitleStyle) -> None:
    if (
        not isinstance(style.font, str)
        or not style.font
        or len(style.font) > 128
        or not style.font.isprintable()
        or any(
            character in style.font
            for character in {",", "'", "\\", ";", "="}
        )
    ):
        raise ValueError("字幕字体名称不符合安全烧录约束")
    for value, field_name, minimum, maximum in (
        (style.font_size, "字幕字号", 8, 200),
        (style.outline, "字幕描边", 0, 20),
        (style.margin_bottom, "字幕底边距", 0, 1_000),
        (style.max_chars_per_line, "字幕单行字符上限", 1, 100),
        (style.max_lines, "字幕行数上限", 1, 2),
    ):
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"{field_name}必须是整数")
        if not minimum <= value <= maximum:
            raise ValueError(f"{field_name}不符合安全烧录约束")
