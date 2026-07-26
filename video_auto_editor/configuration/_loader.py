"""Configuration 深模块的唯一公共加载入口。"""

import json
import re
from collections.abc import Mapping
from os import PathLike
from pathlib import Path
from typing import Any

from video_auto_editor.runtime.errors import ErrorCode

from ._diagnostics import build_diagnostic_projection
from ._failure import ConfigurationFailure
from ._model import (
    ClipPolicy,
    CourseContext,
    EffectiveConfiguration,
    LoadedConfiguration,
    ProviderConfiguration,
    SubtitleStyle,
    TextGenerationSettings,
    TextProviderConfiguration,
)
from ._schema import (
    validate_adapter_switch,
    validate_configuration_overlay,
    validate_course_context_overlay,
    validate_course_context_values,
    validate_effective_configuration,
)


class Configuration:
    """发现并形成每次运行独立的生效配置与课程上下文。"""

    @classmethod
    def load(
        cls,
        source: PathLike[str] | str,
        configuration_overrides: Mapping[str, Any] | None = None,
    ) -> LoadedConfiguration:
        """按认证默认值、源旁 JSON、调用方覆盖的顺序形成生效配置。"""
        effective_values = _certified_defaults()
        source_path = _resolve_source_path(Path(source))
        sidecar_path = source_path.with_suffix(".config.json")
        sidecar_values = _load_optional_sidecar(
            sidecar_path,
            field="configuration",
        )
        if sidecar_values is not _MISSING:
            sidecar_values = _require_configuration_schema(sidecar_values)
            validate_configuration_overlay(sidecar_values)
            validate_adapter_switch(sidecar_values, effective_values)
            effective_values = _recursive_overlay(
                effective_values,
                sidecar_values,
            )
        if configuration_overrides is not None:
            validated_overrides = _require_configuration_schema(
                configuration_overrides
            )
            validate_configuration_overlay(validated_overrides)
            validate_adapter_switch(validated_overrides, effective_values)
            effective_values = _recursive_overlay(
                effective_values,
                validated_overrides,
            )
        validate_effective_configuration(effective_values)
        course_context_path = source_path.with_suffix(".context.json")
        course_context = None
        context_payload = _load_optional_sidecar(
            course_context_path,
            field="course_context",
        )
        if context_payload is not _MISSING:
            context_values = _require_course_context_schema(
                context_payload
            )
            validate_course_context_overlay(context_values)
            validate_course_context_values(context_values)
            course_context = _build_course_context(context_values)
        effective = _build_effective(effective_values)
        return LoadedConfiguration(
            effective=effective,
            course_context=course_context,
            diagnostic_projection=build_diagnostic_projection(
                effective,
                course_context,
            ),
        )


class _NonStrictJson(ValueError):
    pass


_MISSING = object()


def _reject_non_finite_constant(_value: str) -> None:
    raise _NonStrictJson


def _reject_duplicate_object_keys(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    parsed: dict[str, Any] = {}
    for key, value in pairs:
        if key in parsed:
            raise _NonStrictJson
        parsed[key] = value
    return parsed


def _load_strict_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as json_file:
            return json.load(
                json_file,
                object_pairs_hook=_reject_duplicate_object_keys,
                parse_constant=_reject_non_finite_constant,
            )
    except (
        RecursionError,
        UnicodeDecodeError,
        ValueError,
    ):
        pass

    raise ConfigurationFailure(
        ErrorCode.CONFIG_SCHEMA_INVALID,
        {"reason_code": "schema.malformed_json"},
    )


def _load_optional_sidecar(path: Path, *, field: str) -> object:
    if not path.exists() and not path.is_symlink():
        return _MISSING
    if not path.is_file():
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {
                "field": field,
                "reason_code": "schema.malformed_json",
            },
        )
    try:
        return _load_strict_json(path)
    except OSError:
        pass

    raise ConfigurationFailure(
        ErrorCode.CONFIG_SCHEMA_INVALID,
        {
            "field": field,
            "reason_code": "schema.malformed_json",
        },
    )


def _require_configuration_schema(values: object) -> Mapping[str, Any]:
    if not isinstance(values, Mapping):
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {
                "field": "configuration",
                "reason_code": "schema.root_not_object",
            },
        )
    if "schema_version" not in values:
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {
                "field": "schema_version",
                "reason_code": "schema.version_missing",
            },
        )
    version = values["schema_version"]
    if version != "configuration.v1":
        diagnostics: dict[str, object] = {
            "field": "schema_version",
            "reason_code": "schema.version_unsupported",
        }
        if _is_safe_schema_version_diagnostic(version):
            diagnostics["schema_version"] = version
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            diagnostics,
        )
    return values


def _require_course_context_schema(values: object) -> Mapping[str, Any]:
    if not isinstance(values, Mapping):
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {
                "field": "course_context",
                "reason_code": "schema.root_not_object",
            },
        )
    if "schema_version" not in values:
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {
                "field": "schema_version",
                "reason_code": "schema.version_missing",
            },
        )
    version = values["schema_version"]
    if version != "course_context.v1":
        diagnostics: dict[str, object] = {
            "field": "schema_version",
            "reason_code": "schema.version_unsupported",
        }
        if _is_safe_schema_version_diagnostic(version):
            diagnostics["schema_version"] = version
        raise ConfigurationFailure(
            ErrorCode.CONFIG_SCHEMA_INVALID,
            diagnostics,
        )
    return values


def _resolve_source_path(source: Path) -> Path:
    try:
        return source.resolve(strict=False)
    except (OSError, RuntimeError):
        return source.absolute()


def _is_safe_schema_version_diagnostic(version: object) -> bool:
    return (
        isinstance(version, str)
        and len(version) <= 64
        and re.fullmatch(
            r"(?:configuration|course_context)\.v[1-9][0-9]*",
            version,
        )
        is not None
    )


def _certified_defaults() -> dict[str, Any]:
    return {
        "schema_version": "configuration.v1",
        "transcription_provider": "stepaudio",
        "transcription_provider_config": {
            "model": "stepaudio-2.5-asr",
            "endpoint": "https://api.stepfun.com/v1/audio/asr/sse",
            "key_environment_variable": "STEPFUN_API_KEY",
            "timeout_seconds": 120,
            "max_concurrency": 1,
        },
        "text_model_provider": "stepfun",
        "text_model_provider_config": {
            "endpoint": "https://api.stepfun.com/v1",
            "key_environment_variable": "STEPFUN_API_KEY",
            "timeout_seconds": 180,
            "max_concurrency": 1,
        },
        "topic_review": {
            "model": "step-2-mini",
            "temperature": 0.2,
            "reasoning_effort": "none",
            "max_output_tokens": 4096,
        },
        "subtitle_optimization": {
            "model": "step-2-mini",
            "temperature": 0.2,
            "reasoning_effort": "none",
            "max_output_tokens": 4096,
        },
        "clip_policy": {
            "min_duration_seconds": 60,
            "target_duration_seconds": 180,
            "max_duration_seconds": 300,
            "publish_ready_threshold": 80,
        },
        "subtitle_style": {
            "font": "Noto Sans CJK SC",
            "font_size": 18,
            "outline": 2,
            "margin_bottom": 20,
            "max_chars_per_line": 15,
            "max_lines": 2,
        },
        "delivery_build_concurrency": 4,
    }


def _recursive_overlay(
    base: Mapping[str, Any],
    overrides: Mapping[str, Any],
) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        base_value = merged.get(key)
        if isinstance(base_value, Mapping) and isinstance(value, Mapping):
            merged[key] = _recursive_overlay(base_value, value)
        else:
            merged[key] = value
    return merged


def _build_effective(values: Mapping[str, Any]) -> EffectiveConfiguration:
    transcription = values["transcription_provider_config"]
    text_model = values["text_model_provider_config"]
    topic_review = values["topic_review"]
    subtitle_optimization = values["subtitle_optimization"]
    clip_policy = values["clip_policy"]
    subtitle_style = values["subtitle_style"]
    return EffectiveConfiguration(
        schema_version=values["schema_version"],
        transcription_provider=values["transcription_provider"],
        transcription_provider_config=ProviderConfiguration(
            model=transcription["model"],
            endpoint=transcription["endpoint"],
            key_environment_variable=transcription["key_environment_variable"],
            timeout_seconds=transcription["timeout_seconds"],
            max_concurrency=transcription["max_concurrency"],
        ),
        text_model_provider=values["text_model_provider"],
        text_model_provider_config=TextProviderConfiguration(
            endpoint=text_model["endpoint"],
            key_environment_variable=text_model["key_environment_variable"],
            timeout_seconds=text_model["timeout_seconds"],
            max_concurrency=text_model["max_concurrency"],
        ),
        topic_review=TextGenerationSettings(
            model=topic_review["model"],
            temperature=float(topic_review["temperature"]),
            reasoning_effort=topic_review["reasoning_effort"],
            max_output_tokens=topic_review["max_output_tokens"],
        ),
        subtitle_optimization=TextGenerationSettings(
            model=subtitle_optimization["model"],
            temperature=float(subtitle_optimization["temperature"]),
            reasoning_effort=subtitle_optimization["reasoning_effort"],
            max_output_tokens=subtitle_optimization["max_output_tokens"],
        ),
        clip_policy=ClipPolicy(
            min_duration_seconds=clip_policy["min_duration_seconds"],
            target_duration_seconds=clip_policy["target_duration_seconds"],
            max_duration_seconds=clip_policy["max_duration_seconds"],
            max_clips=clip_policy.get("max_clips"),
            publish_ready_threshold=clip_policy["publish_ready_threshold"],
        ),
        subtitle_style=SubtitleStyle(
            font=subtitle_style["font"],
            font_size=subtitle_style["font_size"],
            outline=subtitle_style["outline"],
            margin_bottom=subtitle_style["margin_bottom"],
            max_chars_per_line=subtitle_style["max_chars_per_line"],
            max_lines=subtitle_style["max_lines"],
        ),
        delivery_build_concurrency=values["delivery_build_concurrency"],
    )


def _build_course_context(values: Mapping[str, Any]) -> CourseContext:
    return CourseContext(
        schema_version=values["schema_version"],
        course_topic=values["course_topic"],
        attribution=values.get("attribution"),
        priority_topics=tuple(values.get("priority_topics", ())),
        excluded_content=tuple(values.get("excluded_content", ())),
    )
