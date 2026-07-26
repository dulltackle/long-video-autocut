"""Configuration 深模块拥有的不可变业务值。"""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    """语音识别 Adapter 的公共配置。"""

    model: str
    endpoint: str
    key_environment_variable: str
    timeout_seconds: int
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class TextProviderConfiguration:
    """文本模型 Adapter 的公共配置。"""

    endpoint: str
    key_environment_variable: str
    timeout_seconds: int
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class TextGenerationSettings:
    """一次文本生成能力的公共模型设置。"""

    model: str
    temperature: float
    reasoning_effort: str
    max_output_tokens: int


@dataclass(frozen=True, slots=True)
class ClipPolicy:
    """短视频时长、数量与发布就绪门槛。"""

    min_duration_seconds: int
    target_duration_seconds: int
    max_duration_seconds: int
    max_clips: int | None
    publish_ready_threshold: int


@dataclass(frozen=True, slots=True)
class SubtitleStyle:
    """烧录字幕允许公开的样式。"""

    font: str
    font_size: int
    outline: int
    margin_bottom: int
    max_chars_per_line: int
    max_lines: int


@dataclass(frozen=True, slots=True)
class EffectiveConfiguration:
    """一次直播拆条运行独占的生效配置。"""

    schema_version: str
    transcription_provider: str
    transcription_provider_config: ProviderConfiguration
    text_model_provider: str
    text_model_provider_config: TextProviderConfiguration
    topic_review: TextGenerationSettings
    subtitle_optimization: TextGenerationSettings
    clip_policy: ClipPolicy
    subtitle_style: SubtitleStyle
    delivery_build_concurrency: int


@dataclass(frozen=True, slots=True)
class CourseContext:
    """供业务模块消费、但不在 repr 中泄露正文的课程上下文。"""

    schema_version: str
    course_topic: str = field(repr=False)
    attribution: str | None = field(default=None, repr=False)
    priority_topics: tuple[str, ...] = field(default=(), repr=False)
    excluded_content: tuple[str, ...] = field(default=(), repr=False)


@dataclass(frozen=True, slots=True)
class ResultConfigurationProjection:
    """影响业务结果、可安全持久化的配置白名单。"""

    transcription_provider: str
    transcription_model: str
    text_model_provider: str
    topic_review: TextGenerationSettings
    subtitle_optimization: TextGenerationSettings
    clip_policy: ClipPolicy
    subtitle_style: SubtitleStyle


@dataclass(frozen=True, slots=True)
class RuntimePolicyProjection:
    """不含端点与凭据定位信息的运行策略白名单。"""

    transcription_timeout_seconds: int
    transcription_max_concurrency: int
    text_model_timeout_seconds: int
    text_model_max_concurrency: int
    delivery_build_concurrency: int


@dataclass(frozen=True, slots=True)
class CourseContextProjection:
    """只记录课程上下文存在性和数量，不记录正文。"""

    provided: bool
    attribution_provided: bool
    priority_topic_count: int
    excluded_content_count: int


@dataclass(frozen=True, slots=True)
class ConfigurationDiagnosticProjection:
    """运行诊断可持久化的配置指纹与白名单投影。"""

    configuration_fingerprint: str
    result_configuration: ResultConfigurationProjection
    runtime_policy: RuntimePolicyProjection
    course_context: CourseContextProjection


@dataclass(frozen=True, slots=True)
class LoadedConfiguration:
    """Configuration.load() 的不可变结果。"""

    effective: EffectiveConfiguration
    course_context: CourseContext | None
    diagnostic_projection: ConfigurationDiagnosticProjection
