"""Configuration 结果的脱敏诊断投影与确定性指纹。"""

import hashlib
import json
from dataclasses import asdict

from ._model import (
    ConfigurationDiagnosticProjection,
    CourseContext,
    CourseContextProjection,
    EffectiveConfiguration,
    ResultConfigurationProjection,
    RuntimePolicyProjection,
)


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
    return ConfigurationDiagnosticProjection(
        configuration_fingerprint=fingerprint,
        result_configuration=result_configuration,
        runtime_policy=runtime_policy,
        course_context=CourseContextProjection(
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
