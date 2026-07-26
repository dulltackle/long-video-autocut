"""configuration.v1 的私有递归字段 schema。"""

import math
import re
from collections.abc import Mapping
from ipaddress import ip_address
from typing import Any, NoReturn, cast
from urllib.parse import SplitResult, urlsplit

from video_auto_editor.runtime.errors import ErrorCode

from ._failure import ConfigurationFailure

_LEAF = object()

_CONFIGURATION_SHAPE: dict[str, Any] = {
    "schema_version": _LEAF,
    "transcription_provider": _LEAF,
    "transcription_provider_config": {
        "model": _LEAF,
        "endpoint": _LEAF,
        "key_environment_variable": _LEAF,
        "timeout_seconds": _LEAF,
        "max_concurrency": _LEAF,
    },
    "text_model_provider": _LEAF,
    "text_model_provider_config": {
        "endpoint": _LEAF,
        "key_environment_variable": _LEAF,
        "timeout_seconds": _LEAF,
        "max_concurrency": _LEAF,
    },
    "topic_review": {
        "model": _LEAF,
        "temperature": _LEAF,
        "reasoning_effort": _LEAF,
        "max_output_tokens": _LEAF,
    },
    "subtitle_optimization": {
        "model": _LEAF,
        "temperature": _LEAF,
        "reasoning_effort": _LEAF,
        "max_output_tokens": _LEAF,
    },
    "clip_policy": {
        "min_duration_seconds": _LEAF,
        "target_duration_seconds": _LEAF,
        "max_duration_seconds": _LEAF,
        "max_clips": _LEAF,
        "publish_ready_threshold": _LEAF,
    },
    "subtitle_style": {
        "font": _LEAF,
        "font_size": _LEAF,
        "outline": _LEAF,
        "margin_bottom": _LEAF,
        "max_chars_per_line": _LEAF,
        "max_lines": _LEAF,
    },
    "delivery_build_concurrency": _LEAF,
}

_ADAPTER_CONFIGURATION_FIELDS = {
    "transcription_provider": (
        "transcription_provider_config",
        frozenset(
            {
                "model",
                "endpoint",
                "key_environment_variable",
                "timeout_seconds",
                "max_concurrency",
            }
        ),
    ),
    "text_model_provider": (
        "text_model_provider_config",
        frozenset(
            {
                "endpoint",
                "key_environment_variable",
                "timeout_seconds",
                "max_concurrency",
            }
        ),
    ),
}

_COURSE_CONTEXT_SHAPE: dict[str, Any] = {
    "schema_version": _LEAF,
    "course_topic": _LEAF,
    "attribution": _LEAF,
    "priority_topics": _LEAF,
    "excluded_content": _LEAF,
}

_DIAGNOSTIC_FIELD = re.compile(
    r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*"
)
_SENSITIVE_FIELD_FRAGMENT = re.compile(
    (
        r"(?:^|_)(?:authorization|credential|password|secret|token|api_key)"
        r"(?:_|$)|(?:^|_)(?:pk|rk|sk)_(?:live|test)(?:_|$)"
    ),
    re.IGNORECASE,
)
_HOST_LABEL = re.compile(
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
)


def validate_configuration_overlay(values: Mapping[str, Any]) -> None:
    """拒绝任意深度的未知字段和显式 null。"""
    _validate_shape(
        values,
        _CONFIGURATION_SHAPE,
        parent="",
        root="configuration",
    )


def validate_course_context_overlay(values: Mapping[str, Any]) -> None:
    """拒绝课程上下文中的旧字段、未知字段与显式 null。"""
    _validate_shape(
        values,
        _COURSE_CONTEXT_SHAPE,
        parent="",
        root="course_context",
    )


def validate_course_context_values(values: Mapping[str, Any]) -> None:
    """校验 course_context.v1 的必填正文、长度与数组数量。"""
    if "course_topic" not in values:
        _value_failure("course_topic", "value.empty")
    _content_string(values["course_topic"], "course_topic", 1000)
    if "attribution" in values:
        _content_string(values["attribution"], "attribution", 500)
    for field in ("priority_topics", "excluded_content"):
        if field in values:
            _content_array(values[field], field)


def validate_adapter_switch(
    values: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    """切换 Adapter 时禁止继承上一 Adapter 的任何配置叶子。"""
    for provider_field, (
        provider_config_field,
        required_fields,
    ) in _ADAPTER_CONFIGURATION_FIELDS.items():
        if provider_field not in values:
            continue
        provider = values[provider_field]
        if (
            not isinstance(provider, str)
            or provider == current[provider_field]
            or provider == "deterministic"
        ):
            continue
        provider_config = values.get(provider_config_field)
        if isinstance(provider_config, Mapping) and required_fields.issubset(
            provider_config
        ):
            continue
        raise ConfigurationFailure(
            ErrorCode.CONFIG_CONFLICT,
            {
                "fields": tuple(
                    sorted((provider_field, provider_config_field))
                ),
                "reason_code": "conflict.incomplete_adapter",
            },
        )


def validate_effective_configuration(values: Mapping[str, Any]) -> None:
    """校验递归覆盖完成后的 configuration.v1 类型与枚举。"""
    transcription = _object(
        values["transcription_provider_config"],
        "transcription_provider_config",
    )
    text_model = _object(
        values["text_model_provider_config"],
        "text_model_provider_config",
    )
    topic_review = _object(values["topic_review"], "topic_review")
    subtitle_optimization = _object(
        values["subtitle_optimization"],
        "subtitle_optimization",
    )
    clip_policy = _object(values["clip_policy"], "clip_policy")
    subtitle_style = _object(values["subtitle_style"], "subtitle_style")

    _enum(
        values["transcription_provider"],
        "transcription_provider",
        {"stepaudio"},
    )
    _model(transcription["model"], "transcription_provider_config.model")
    _endpoint(
        transcription["endpoint"],
        "transcription_provider_config.endpoint",
    )
    _environment_variable(
        transcription["key_environment_variable"],
        "transcription_provider_config.key_environment_variable",
    )
    _integer_between(
        transcription["timeout_seconds"],
        "transcription_provider_config.timeout_seconds",
        1,
        600,
    )
    _integer_between(
        transcription["max_concurrency"],
        "transcription_provider_config.max_concurrency",
        1,
        32,
    )

    _enum(values["text_model_provider"], "text_model_provider", {"stepfun"})
    _endpoint(text_model["endpoint"], "text_model_provider_config.endpoint")
    _environment_variable(
        text_model["key_environment_variable"],
        "text_model_provider_config.key_environment_variable",
    )
    _integer_between(
        text_model["timeout_seconds"],
        "text_model_provider_config.timeout_seconds",
        1,
        600,
    )
    _integer_between(
        text_model["max_concurrency"],
        "text_model_provider_config.max_concurrency",
        1,
        32,
    )

    _text_generation(topic_review, "topic_review")
    _text_generation(subtitle_optimization, "subtitle_optimization")

    durations = tuple(
        _integer_between(
            clip_policy[name],
            f"clip_policy.{name}",
            1,
            3600,
        )
        for name in (
            "min_duration_seconds",
            "target_duration_seconds",
            "max_duration_seconds",
        )
    )
    if "max_clips" in clip_policy:
        _integer_between(
            clip_policy["max_clips"],
            "clip_policy.max_clips",
            1,
            1000,
        )
    _integer_between(
        clip_policy["publish_ready_threshold"],
        "clip_policy.publish_ready_threshold",
        0,
        100,
    )
    if not durations[0] <= durations[1] <= durations[2]:
        raise ConfigurationFailure(
            ErrorCode.CONFIG_CONFLICT,
            {
                "fields": (
                    "clip_policy.max_duration_seconds",
                    "clip_policy.min_duration_seconds",
                    "clip_policy.target_duration_seconds",
                ),
                "reason_code": "conflict.duration_order",
            },
        )

    _content_string(subtitle_style["font"], "subtitle_style.font", 128)
    for name, minimum, maximum in (
        ("font_size", 8, 200),
        ("outline", 0, 20),
        ("margin_bottom", 0, 1000),
        ("max_chars_per_line", 1, 100),
        ("max_lines", 1, 2),
    ):
        _integer_between(
            subtitle_style[name],
            f"subtitle_style.{name}",
            minimum,
            maximum,
        )

    _integer_between(
        values["delivery_build_concurrency"],
        "delivery_build_concurrency",
        1,
        32,
    )


def _validate_shape(
    values: Mapping[str, Any],
    shape: Mapping[str, Any],
    *,
    parent: str,
    root: str,
) -> None:
    for key in sorted(values, key=str):
        field = _field_path(parent, key)
        if not isinstance(key, str) or key not in shape:
            raise ConfigurationFailure(
                ErrorCode.CONFIG_SCHEMA_INVALID,
                {
                    "field": _safe_diagnostic_field(field, parent, root),
                    "reason_code": "schema.unknown_field",
                },
            )
        value = values[key]
        if value is None:
            raise ConfigurationFailure(
                ErrorCode.CONFIG_SCHEMA_INVALID,
                {
                    "field": _safe_diagnostic_field(field, parent, root),
                    "reason_code": "schema.null_forbidden",
                },
            )
        nested_shape = shape[key]
        if isinstance(nested_shape, Mapping) and isinstance(value, Mapping):
            _validate_shape(
                value,
                nested_shape,
                parent=field,
                root=root,
            )


def _field_path(parent: str, key: object) -> str:
    if not isinstance(key, str):
        return parent or "configuration"
    if parent:
        return f"{parent}.{key}"
    return key


def _safe_diagnostic_field(
    field: str,
    parent: str,
    root: str = "configuration",
) -> str:
    if (
        _DIAGNOSTIC_FIELD.fullmatch(field)
        and len(field) <= 128
        and not _SENSITIVE_FIELD_FRAGMENT.search(field)
    ):
        return field
    if (
        parent
        and _DIAGNOSTIC_FIELD.fullmatch(parent)
        and len(parent) <= 128
        and not _SENSITIVE_FIELD_FRAGMENT.search(parent)
    ):
        return parent
    return root


def _value_failure(field: str, reason_code: str) -> NoReturn:
    raise ConfigurationFailure(
        ErrorCode.CONFIG_VALUE_INVALID,
        {"field": field, "reason_code": reason_code},
    )


def _object(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        _value_failure(field, "value.wrong_type")
    return cast(Mapping[str, Any], value)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str):
        _value_failure(field, "value.wrong_type")
    if not value.strip():
        _value_failure(field, "value.empty")
    return value


def _integer(value: Any, field: str) -> int:
    if type(value) is not int:
        _value_failure(field, "value.wrong_type")
    return value


def _number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _value_failure(field, "value.wrong_type")
    try:
        normalized = float(value)
    except OverflowError:
        normalized = math.inf
    if not math.isfinite(normalized):
        _value_failure(field, "value.out_of_range")
    return normalized


def _integer_between(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
) -> int:
    normalized = _integer(value, field)
    if not minimum <= normalized <= maximum:
        _value_failure(field, "value.out_of_range")
    return normalized


def _number_between(
    value: Any,
    field: str,
    minimum: float,
    maximum: float,
) -> float:
    normalized = _number(value, field)
    if not minimum <= normalized <= maximum:
        _value_failure(field, "value.out_of_range")
    return normalized


def _enum(value: Any, field: str, allowed: set[str]) -> str:
    normalized = _string(value, field)
    if normalized not in allowed:
        _value_failure(field, "value.invalid_enum")
    return normalized


def _model(value: Any, field: str) -> str:
    normalized = _string(value, field)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", normalized):
        _value_failure(field, "value.invalid_format")
    return normalized


def _environment_variable(value: Any, field: str) -> str:
    normalized = _string(value, field)
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", normalized):
        _value_failure(field, "value.invalid_format")
    return normalized


def _content_string(value: Any, field: str, maximum_length: int) -> str:
    normalized = _string(value, field)
    if len(normalized) > maximum_length:
        _value_failure(field, "value.out_of_range")
    if normalized != normalized.strip() or not normalized.isprintable():
        _value_failure(field, "value.invalid_format")
    return normalized


def _content_array(value: Any, field: str) -> tuple[str, ...]:
    if type(value) is not list:
        _value_failure(field, "value.wrong_type")
    if len(value) > 50:
        _value_failure(field, "value.too_many_items")
    items = tuple(_content_string(item, field, 200) for item in value)
    if len(set(items)) != len(items):
        _value_failure(field, "value.duplicate_item")
    return items


def _endpoint(value: Any, field: str) -> str:
    normalized = _string(value, field)
    if len(normalized) > 2048:
        _value_failure(field, "value.out_of_range")
    parsed_endpoint = _parse_endpoint(normalized)
    if parsed_endpoint is None:
        _value_failure(field, "value.invalid_format")
    parsed, port = parsed_endpoint
    if parsed.scheme.lower() != "https":
        raise ConfigurationFailure(
            ErrorCode.CONFIG_HTTPS_REQUIRED,
            {"field": field},
        )
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or normalized != normalized.strip()
        or not normalized.isprintable()
        or "\\" in normalized
        or any(character.isspace() for character in normalized)
        or (port is not None and not 1 <= port <= 65_535)
        or not _valid_endpoint_host(parsed.hostname)
    ):
        _value_failure(field, "value.invalid_format")
    return normalized


def _parse_endpoint(value: str) -> tuple[SplitResult, int | None] | None:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return None
    return parsed, port


def _valid_endpoint_host(hostname: str | None) -> bool:
    if hostname is None or not hostname.isascii() or len(hostname) > 253:
        return False
    try:
        ip_address(hostname)
    except ValueError:
        pass
    else:
        return True
    if ":" in hostname or re.fullmatch(r"[0-9.]+", hostname):
        return False
    dns_name = hostname.removesuffix(".")
    return bool(dns_name) and all(
        _HOST_LABEL.fullmatch(label) is not None
        for label in dns_name.split(".")
    )


def _text_generation(values: Mapping[str, Any], parent: str) -> None:
    _model(values["model"], f"{parent}.model")
    _number_between(
        values["temperature"],
        f"{parent}.temperature",
        0.0,
        2.0,
    )
    _enum(
        values["reasoning_effort"],
        f"{parent}.reasoning_effort",
        {"none", "low", "medium", "high"},
    )
    _integer_between(
        values["max_output_tokens"],
        f"{parent}.max_output_tokens",
        1,
        65_536,
    )
