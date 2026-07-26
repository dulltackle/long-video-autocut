"""Configuration 结果的脱敏诊断投影与确定性指纹。"""

import hashlib
import json
from dataclasses import asdict, dataclass, fields, is_dataclass
from threading import RLock
from weakref import WeakKeyDictionary

from ._model import (
    ConfigurationDiagnosticProjection,
    CourseContext,
    CourseContextProjection,
    EffectiveConfiguration,
    ResultConfigurationProjection,
    RuntimePolicyProjection,
)


@dataclass(frozen=True, slots=True)
class _ProjectionAuthority:
    configuration_fingerprint: str
    result_configuration: ResultConfigurationProjection
    runtime_policy: RuntimePolicyProjection
    course_context: CourseContextProjection
    signature: object


_PROJECTION_AUTHORITIES: WeakKeyDictionary[
    ConfigurationDiagnosticProjection,
    _ProjectionAuthority,
] = WeakKeyDictionary()
_PROJECTION_AUTHORITIES_LOCK = RLock()


def build_diagnostic_projection(
    effective: EffectiveConfiguration,
    course_context: CourseContext | None,
) -> ConfigurationDiagnosticProjection:
    """只投影允许持久化的配置事实，不读取实际凭据。"""
    result_configuration = ResultConfigurationProjection(
        transcription_provider=effective.transcription_provider,
        transcription_model=effective.transcription_provider_config.model,
        text_model_provider=effective.text_model_provider,
        topic_review=effective.topic_review,
        subtitle_optimization=effective.subtitle_optimization,
        clip_policy=effective.clip_policy,
        subtitle_style=effective.subtitle_style,
    )
    runtime_policy = RuntimePolicyProjection(
        transcription_timeout_seconds=(
            effective.transcription_provider_config.timeout_seconds
        ),
        transcription_max_concurrency=(
            effective.transcription_provider_config.max_concurrency
        ),
        text_model_timeout_seconds=(
            effective.text_model_provider_config.timeout_seconds
        ),
        text_model_max_concurrency=(
            effective.text_model_provider_config.max_concurrency
        ),
        delivery_build_concurrency=effective.delivery_build_concurrency,
    )
    canonical = json.dumps(
        {
            "result_configuration": asdict(result_configuration),
            "runtime_policy": asdict(runtime_policy),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    fingerprint = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
    projection = object.__new__(ConfigurationDiagnosticProjection)
    object.__setattr__(
        projection,
        "configuration_fingerprint",
        fingerprint,
    )
    object.__setattr__(
        projection,
        "result_configuration",
        result_configuration,
    )
    object.__setattr__(
        projection,
        "runtime_policy",
        runtime_policy,
    )
    object.__setattr__(
        projection,
        "course_context",
        CourseContextProjection(
            provided=course_context is not None,
            attribution_provided=(
                course_context is not None
                and course_context.attribution is not None
            ),
            priority_topic_count=(
                len(course_context.priority_topics)
                if course_context is not None
                else 0
            ),
            excluded_content_count=(
                len(course_context.excluded_content)
                if course_context is not None
                else 0
            ),
        ),
    )
    authority = _ProjectionAuthority(
        configuration_fingerprint=fingerprint,
        result_configuration=projection.result_configuration,
        runtime_policy=projection.runtime_policy,
        course_context=projection.course_context,
        signature=_projection_signature(projection),
    )
    with _PROJECTION_AUTHORITIES_LOCK:
        _PROJECTION_AUTHORITIES[projection] = authority
    return projection


def assert_diagnostic_projection_authentic(
    projection: ConfigurationDiagnosticProjection,
) -> None:
    """确认投影仍是 Configuration 原样签发的完整值。"""
    try:
        with _PROJECTION_AUTHORITIES_LOCK:
            authority = _PROJECTION_AUTHORITIES[projection]
            authentic = (
                type(projection) is ConfigurationDiagnosticProjection
                and projection.configuration_fingerprint
                == authority.configuration_fingerprint
                and projection.result_configuration
                is authority.result_configuration
                and projection.runtime_policy is authority.runtime_policy
                and projection.course_context is authority.course_context
                and _projection_signature(projection)
                == authority.signature
            )
    except (
        AttributeError,
        KeyError,
        RecursionError,
        TypeError,
        ValueError,
    ):
        authentic = False
    if not authentic:
        raise TypeError("配置诊断投影必须由 Configuration 创建")


def _projection_signature(value: object) -> object:
    """形成只用于检测签发后篡改的递归不可变快照。"""
    if is_dataclass(value) and not isinstance(value, type):
        return (
            type(value),
            tuple(
                (
                    field.name,
                    _projection_signature(getattr(value, field.name)),
                )
                for field in fields(value)
            ),
        )
    if isinstance(value, tuple):
        return (
            tuple,
            tuple(_projection_signature(item) for item in value),
        )
    if value is None:
        return (type(None),)
    if isinstance(value, (str, int, float, bool)):
        return (type(value), value)
    raise TypeError("配置诊断投影包含未知内部值")
