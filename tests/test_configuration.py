import json
import re
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from weakref import ref

import pytest

import video_auto_editor.configuration as configuration_module
from video_auto_editor.configuration import Configuration, ConfigurationFailure
from video_auto_editor.configuration._model import (
    ConfigurationDiagnosticProjection,
)
from video_auto_editor.runtime.errors import ErrorCode, RunError


def test_configuration_package_exposes_only_its_deep_public_seam():
    assert configuration_module.__all__ == [
        "Configuration",
        "ConfigurationFailure",
        "LoadedConfiguration",
    ]
    assert not hasattr(configuration_module, "EffectiveConfiguration")
    assert not hasattr(configuration_module, "ProviderConfiguration")


def test_load_forms_an_independent_immutable_certified_default_for_each_run(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")

    first = Configuration.load(source)
    second = Configuration.load(source)

    assert first == second
    assert first is not second
    assert first.effective is not second.effective
    assert first.effective.schema_version == "configuration.v1"
    assert first.effective.transcription_provider == "stepaudio"
    assert (
        first.effective.transcription_provider_config.endpoint
        == "https://api.stepfun.com/v1/audio/asr/sse"
    )
    assert first.effective.text_model_provider == "stepfun"
    assert first.effective.clip_policy.min_duration_seconds == 60
    assert first.effective.clip_policy.target_duration_seconds == 180
    assert first.effective.clip_policy.max_duration_seconds == 300
    assert first.effective.clip_policy.max_clips is None
    assert first.course_context is None

    with pytest.raises(FrozenInstanceError):
        first.effective.delivery_build_concurrency = 99


def test_load_discovers_sidecar_and_recursively_overlays_without_mutating_input(
    tmp_path,
):
    source = tmp_path / "course.final.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.final.config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                "clip_policy": {"target_duration_seconds": 200},
                "subtitle_style": {"max_lines": 1},
            }
        ),
        encoding="utf-8",
    )
    overrides = {
        "schema_version": "configuration.v1",
        "clip_policy": {"publish_ready_threshold": 90},
    }
    original = deepcopy(overrides)

    loaded = Configuration.load(source, overrides)

    assert loaded.effective.clip_policy.min_duration_seconds == 60
    assert loaded.effective.clip_policy.target_duration_seconds == 200
    assert loaded.effective.clip_policy.max_duration_seconds == 300
    assert loaded.effective.clip_policy.publish_ready_threshold == 90
    assert loaded.effective.subtitle_style.max_chars_per_line == 15
    assert loaded.effective.subtitle_style.max_lines == 1
    assert overrides == original

    overrides["clip_policy"]["publish_ready_threshold"] = 1
    assert loaded.effective.clip_policy.publish_ready_threshold == 90


def test_load_rejects_a_supplied_configuration_without_an_explicit_schema(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == {
        "field": "schema_version",
        "reason_code": "schema.version_missing",
    }
    assert str(captured.value) == "配置 schema 不受支持或不合法。"


@pytest.mark.parametrize(
    "raw_json",
    [
        "{bad",
        (
            '{"schema_version":"configuration.v1",'
            '"clip_policy":{},"clip_policy":{}}'
        ),
        (
            '{"schema_version":"configuration.v1",'
            '"delivery_build_concurrency":NaN}'
        ),
    ],
)
def test_load_rejects_non_strict_json_without_echoing_its_contents(
    tmp_path,
    raw_json,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(raw_json, encoding="utf-8")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == {
        "reason_code": "schema.malformed_json",
    }
    assert raw_json not in str(captured.value)


@pytest.mark.parametrize(
    ("payload", "expected_diagnostics"),
    [
        (
            [],
            {
                "field": "configuration",
                "reason_code": "schema.root_not_object",
            },
        ),
        (
            {"schema_version": "configuration.v2"},
            {
                "field": "schema_version",
                "schema_version": "configuration.v2",
                "reason_code": "schema.version_unsupported",
            },
        ),
    ],
)
def test_load_rejects_an_invalid_configuration_root_or_schema_version(
    tmp_path,
    payload,
    expected_diagnostics,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == expected_diagnostics


@pytest.mark.parametrize(
    ("override", "expected_diagnostics"),
    [
        (
            {"crf": 18},
            {"field": "crf", "reason_code": "schema.unknown_field"},
        ),
        (
            {"clip_policy": {"buffer_start": 1}},
            {
                "field": "clip_policy.buffer_start",
                "reason_code": "schema.unknown_field",
            },
        ),
        (
            {"clip_policy": {"max_clips": None}},
            {
                "field": "clip_policy.max_clips",
                "reason_code": "schema.null_forbidden",
            },
        ),
        (
            {"api_key": "must-not-appear-in-diagnostics"},
            {
                "field": "configuration",
                "reason_code": "schema.unknown_field",
            },
        ),
    ],
)
def test_load_recursively_rejects_unknown_fields_and_null(
    tmp_path,
    override,
    expected_diagnostics,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    payload = {"schema_version": "configuration.v1", **override}
    (tmp_path / "course.config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == expected_diagnostics
    assert "must-not-appear-in-diagnostics" not in str(captured.value)


@pytest.mark.parametrize(
    ("override", "field", "reason_code"),
    [
        (
            {"delivery_build_concurrency": True},
            "delivery_build_concurrency",
            "value.wrong_type",
        ),
        (
            {"clip_policy": {"publish_ready_threshold": 80.0}},
            "clip_policy.publish_ready_threshold",
            "value.wrong_type",
        ),
        (
            {"clip_policy": []},
            "clip_policy",
            "value.wrong_type",
        ),
        (
            {"transcription_provider": "deterministic"},
            "transcription_provider",
            "value.invalid_enum",
        ),
        (
            {"topic_review": {"model": ""}},
            "topic_review.model",
            "value.empty",
        ),
        (
            {"topic_review": {"reasoning_effort": "extreme"}},
            "topic_review.reasoning_effort",
            "value.invalid_enum",
        ),
        (
            {
                "transcription_provider_config": {
                    "key_environment_variable": "1INVALID"
                }
            },
            "transcription_provider_config.key_environment_variable",
            "value.invalid_format",
        ),
        (
            {"subtitle_style": {"font": "字" * 129}},
            "subtitle_style.font",
            "value.out_of_range",
        ),
        (
            {"subtitle_style": {"font": " Noto Sans CJK SC "}},
            "subtitle_style.font",
            "value.invalid_format",
        ),
    ],
)
def test_load_rejects_wrong_types_and_invalid_public_values(
    tmp_path,
    override,
    field,
    reason_code,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    payload = {"schema_version": "configuration.v1", **override}
    (tmp_path / "course.config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert captured.value.diagnostics == {
        "field": field,
        "reason_code": reason_code,
    }


@pytest.mark.parametrize(
    ("override", "field"),
    [
        (
            {"transcription_provider_config": {"timeout_seconds": 0}},
            "transcription_provider_config.timeout_seconds",
        ),
        (
            {"text_model_provider_config": {"max_concurrency": 33}},
            "text_model_provider_config.max_concurrency",
        ),
        (
            {"topic_review": {"temperature": 2.1}},
            "topic_review.temperature",
        ),
        (
            {"subtitle_optimization": {"max_output_tokens": 65_537}},
            "subtitle_optimization.max_output_tokens",
        ),
        (
            {"clip_policy": {"min_duration_seconds": 0}},
            "clip_policy.min_duration_seconds",
        ),
        (
            {"clip_policy": {"max_clips": 0}},
            "clip_policy.max_clips",
        ),
        (
            {"clip_policy": {"publish_ready_threshold": 101}},
            "clip_policy.publish_ready_threshold",
        ),
        (
            {"subtitle_style": {"max_lines": 3}},
            "subtitle_style.max_lines",
        ),
        (
            {"delivery_build_concurrency": 33},
            "delivery_build_concurrency",
        ),
    ],
)
def test_load_rejects_values_outside_configuration_v1_bounds(
    tmp_path,
    override,
    field,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    payload = {"schema_version": "configuration.v1", **override}
    (tmp_path / "course.config.json").write_text(
        json.dumps(payload),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert captured.value.diagnostics == {
        "field": field,
        "reason_code": "value.out_of_range",
    }


def test_load_rejects_conflicting_clip_duration_order(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                "clip_policy": {
                    "min_duration_seconds": 200,
                    "target_duration_seconds": 180,
                    "max_duration_seconds": 300,
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_CONFLICT
    assert captured.value.diagnostics == {
        "fields": (
            "clip_policy.max_duration_seconds",
            "clip_policy.min_duration_seconds",
            "clip_policy.target_duration_seconds",
        ),
        "reason_code": "conflict.duration_order",
    }


@pytest.mark.parametrize(
    "field",
    [
        "transcription_provider_config",
        "text_model_provider_config",
    ],
)
def test_load_requires_https_for_each_production_adapter_endpoint(
    tmp_path,
    field,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                field: {"endpoint": "http://provider.example/v1"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_HTTPS_REQUIRED
    assert captured.value.diagnostics == {"field": f"{field}.endpoint"}


@pytest.mark.parametrize(
    ("provider_field", "provider_config_field"),
    [
        ("transcription_provider", "transcription_provider_config"),
        ("text_model_provider", "text_model_provider_config"),
    ],
)
def test_load_rejects_an_incomplete_adapter_switch_before_inheriting_old_config(
    tmp_path,
    provider_field,
    provider_config_field,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                provider_field: "future-certified-adapter",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_CONFLICT
    assert captured.value.diagnostics == {
        "fields": tuple(sorted((provider_field, provider_config_field))),
        "reason_code": "conflict.incomplete_adapter",
    }


def test_load_discovers_an_immutable_optional_course_context(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": "直播拆条生产课",
                "attribution": "示例学院",
                "priority_topics": ["不可变配置", "严格 schema"],
                "excluded_content": ["课间闲聊"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    first = Configuration.load(source)
    second = Configuration.load(source)

    assert first.course_context is not None
    assert second.course_context is not None
    assert first.course_context == second.course_context
    assert first.course_context is not second.course_context
    assert first.course_context.schema_version == "course_context.v1"
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        first.course_context.sha256,
    )
    assert first.course_context.sha256 == second.course_context.sha256
    assert first.course_context.course_topic == "直播拆条生产课"
    assert first.course_context.attribution == "示例学院"
    assert first.course_context.priority_topics == (
        "不可变配置",
        "严格 schema",
    )
    assert first.course_context.excluded_content == ("课间闲聊",)
    assert "直播拆条生产课" not in repr(first.course_context)

    with pytest.raises(FrozenInstanceError):
        first.course_context.course_topic = "已修改"


def test_course_context_digest_uses_validated_semantics_not_json_layout(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    context_path = tmp_path / "course.context.json"
    semantic_context = {
        "schema_version": "course_context.v1",
        "course_topic": "规范摘要",
        "priority_topics": ["稳定键序"],
    }
    context_path.write_text(
        json.dumps(semantic_context, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    first = Configuration.load(source).course_context
    context_path.write_text(
        json.dumps(
            {
                "priority_topics": ["稳定键序"],
                "course_topic": "规范摘要",
                "schema_version": "course_context.v1",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    second = Configuration.load(source).course_context
    semantic_context["course_topic"] = "内容已变化"
    context_path.write_text(
        json.dumps(semantic_context, ensure_ascii=False),
        encoding="utf-8",
    )
    changed = Configuration.load(source).course_context

    assert first is not None
    assert second is not None
    assert changed is not None
    assert first.sha256 == second.sha256
    assert changed.sha256 != first.sha256


@pytest.mark.parametrize(
    ("extra", "expected_diagnostics"),
    [
        (
            {"course_title": "旧字段"},
            {
                "field": "course_title",
                "reason_code": "schema.unknown_field",
            },
        ),
        (
            {"attribution": None},
            {
                "field": "attribution",
                "reason_code": "schema.null_forbidden",
            },
        ),
    ],
)
def test_load_strictly_rejects_unknown_or_null_course_context_fields(
    tmp_path,
    extra,
    expected_diagnostics,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": "直播拆条生产课",
                **extra,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == expected_diagnostics


@pytest.mark.parametrize(
    ("context_fields", "field", "reason_code"),
    [
        ({}, "course_topic", "value.empty"),
        ({"course_topic": "   "}, "course_topic", "value.empty"),
        ({"course_topic": "课" * 1001}, "course_topic", "value.out_of_range"),
        (
            {"course_topic": "生产课", "attribution": ["示例学院"]},
            "attribution",
            "value.wrong_type",
        ),
        (
            {"course_topic": "生产课", "priority_topics": "严格 schema"},
            "priority_topics",
            "value.wrong_type",
        ),
        (
            {"course_topic": "生产课", "priority_topics": [""]},
            "priority_topics",
            "value.empty",
        ),
        (
            {"course_topic": "生产课", "priority_topics": ["重复", "重复"]},
            "priority_topics",
            "value.duplicate_item",
        ),
        (
            {
                "course_topic": "生产课",
                "excluded_content": [f"排除项 {index}" for index in range(51)],
            },
            "excluded_content",
            "value.too_many_items",
        ),
    ],
)
def test_load_enforces_course_context_v1_string_and_array_bounds(
    tmp_path,
    context_fields,
    field,
    reason_code,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                **context_fields,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert captured.value.diagnostics == {
        "field": field,
        "reason_code": reason_code,
    }


def test_diagnostic_projection_is_immutable_and_excludes_credentials_and_content(
    tmp_path,
    monkeypatch,
):
    secret = "sk-live-must-never-be-persisted"
    monkeypatch.setenv("STEPFUN_API_KEY", secret)
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.context.json").write_text(
        json.dumps(
            {
                "schema_version": "course_context.v1",
                "course_topic": "不得进入诊断的课程正文",
                "attribution": "不得进入诊断的归属正文",
                "priority_topics": ["敏感优先主题"],
                "excluded_content": ["敏感排除内容"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = Configuration.load(source)
    projection_text = repr(loaded.diagnostic_projection)

    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        loaded.diagnostic_projection.configuration_fingerprint,
    )
    assert loaded.diagnostic_projection.course_context.provided is True
    assert loaded.diagnostic_projection.course_context.attribution_provided is True
    assert loaded.diagnostic_projection.course_context.priority_topic_count == 1
    assert loaded.diagnostic_projection.course_context.excluded_content_count == 1
    assert secret not in projection_text
    assert "STEPFUN_API_KEY" not in projection_text
    assert "api.stepfun.com" not in projection_text
    assert "不得进入诊断" not in projection_text
    assert "敏感优先主题" not in projection_text
    assert "敏感排除内容" not in projection_text

    with pytest.raises(FrozenInstanceError):
        loaded.diagnostic_projection.course_context.provided = False

    same_config = Configuration.load(source)
    changed_config = Configuration.load(
        source,
        {
            "schema_version": "configuration.v1",
            "clip_policy": {"publish_ready_threshold": 81},
        },
    )
    monkeypatch.setenv("STEPFUN_API_KEY", "different-secret")
    different_secret = Configuration.load(source)

    assert (
        same_config.diagnostic_projection.configuration_fingerprint
        == loaded.diagnostic_projection.configuration_fingerprint
    )
    assert (
        different_secret.diagnostic_projection.configuration_fingerprint
        == loaded.diagnostic_projection.configuration_fingerprint
    )
    assert (
        changed_config.diagnostic_projection.configuration_fingerprint
        != loaded.diagnostic_projection.configuration_fingerprint
    )


def test_diagnostic_projection_can_only_be_issued_by_configuration_load(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    projection = Configuration.load(source).diagnostic_projection
    same_values = Configuration.load(source).diagnostic_projection
    credential_canary = "sk-live-credential-canary"

    with pytest.raises(TypeError, match="只能由 Configuration 创建"):
        ConfigurationDiagnosticProjection(
            configuration_fingerprint=credential_canary,
            result_configuration=projection.result_configuration,
            runtime_policy=projection.runtime_policy,
            course_context=projection.course_context,
        )

    with pytest.raises(TypeError, match="只能由 Configuration 创建"):
        replace(
            projection,
            configuration_fingerprint=credential_canary,
        )

    assert ref(projection)() is projection
    assert same_values is not projection
    assert same_values != projection


def test_configuration_fingerprint_uses_only_the_persistable_whitelist(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    first = Configuration.load(
        source,
        {
            "schema_version": "configuration.v1",
            "text_model_provider_config": {
                "endpoint": "https://first.internal.example/v1",
                "key_environment_variable": "FIRST_INTERNAL_API_KEY",
            },
        },
    )
    different_restricted_metadata = Configuration.load(
        source,
        {
            "schema_version": "configuration.v1",
            "text_model_provider_config": {
                "endpoint": "https://second.internal.example/v1",
                "key_environment_variable": "SECOND_INTERNAL_API_KEY",
            },
        },
    )
    different_whitelisted_policy = Configuration.load(
        source,
        {
            "schema_version": "configuration.v1",
            "text_model_provider_config": {"timeout_seconds": 181},
        },
    )

    assert (
        first.diagnostic_projection.configuration_fingerprint
        == different_restricted_metadata.diagnostic_projection.configuration_fingerprint
    )
    assert (
        first.diagnostic_projection.configuration_fingerprint
        != different_whitelisted_policy.diagnostic_projection.configuration_fingerprint
    )


@pytest.mark.parametrize(
    ("sidecar_suffix", "field"),
    [
        (".config.json", "configuration"),
        (".context.json", "course_context"),
    ],
)
def test_load_does_not_treat_a_present_non_file_sidecar_as_absent(
    tmp_path,
    sidecar_suffix,
    field,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    source.with_suffix(sidecar_suffix).mkdir()

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == {
        "field": field,
        "reason_code": "schema.malformed_json",
    }


def test_malformed_json_failure_does_not_retain_sensitive_exception_context(
    tmp_path,
):
    sensitive_value = "sk-sensitive-value-in-malformed-json"
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    (tmp_path / "course.config.json").write_text(
        (
            '{"schema_version":"configuration.v1",'
            f'"unexpected":"{sensitive_value}", bad}}'
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sensitive_value not in repr(captured.value)


def test_huge_numeric_override_forms_a_typed_out_of_range_failure(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(
            source,
            {
                "schema_version": "configuration.v1",
                "topic_review": {"temperature": 10**10_000},
            },
        )

    assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert captured.value.diagnostics == {
        "field": "topic_review.temperature",
        "reason_code": "value.out_of_range",
    }


def test_excessively_nested_json_forms_a_safe_malformed_failure(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    source.with_suffix(".config.json").write_text(
        "[" * 10_000 + "0" + "]" * 10_000,
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == {
        "reason_code": "schema.malformed_json",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_excessively_long_json_integer_forms_a_safe_malformed_failure(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    source.with_suffix(".config.json").write_text(
        (
            '{"schema_version":"configuration.v1",'
            '"delivery_build_concurrency":'
            + "9" * 5_000
            + "}"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert captured.value.diagnostics == {
        "reason_code": "schema.malformed_json",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    "schema_version",
    [
        "configuration.v" + "9" * 80,
        "course_context.v" + "9" * 80,
    ],
)
def test_unsupported_long_schema_version_remains_valid_run_diagnostics(
    tmp_path,
    schema_version,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")
    sidecar_suffix = (
        ".context.json"
        if schema_version.startswith("course_context.")
        else ".config.json"
    )
    source.with_suffix(sidecar_suffix).write_text(
        json.dumps({"schema_version": schema_version}),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(source)

    assert captured.value.diagnostics == {
        "field": "schema_version",
        "reason_code": "schema.version_unsupported",
    }
    run_error = RunError.create(
        code=captured.value.error_code,
        stage="preflight",
        module="configuration",
        event_sequence=1,
        diagnostics=captured.value.diagnostics,
    )
    assert run_error.diagnostics == captured.value.diagnostics


def test_unknown_field_name_cannot_smuggle_a_credential_into_diagnostics(
    tmp_path,
):
    sensitive_field = "sk_live_supersecret123"
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(
            source,
            {
                "schema_version": "configuration.v1",
                sensitive_field: "ignored",
            },
        )

    assert captured.value.diagnostics == {
        "field": "configuration",
        "reason_code": "schema.unknown_field",
    }
    assert sensitive_field not in repr(captured.value.diagnostics)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.com/\x00",
        "https://example.com\\evil/v1",
        "https://exa%mple.com/v1",
    ],
)
def test_endpoint_rejects_malformed_https_uris(tmp_path, endpoint):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(
            source,
            {
                "schema_version": "configuration.v1",
                "text_model_provider_config": {"endpoint": endpoint},
            },
        )

    assert captured.value.error_code is ErrorCode.CONFIG_VALUE_INVALID
    assert captured.value.diagnostics == {
        "field": "text_model_provider_config.endpoint",
        "reason_code": "value.invalid_format",
    }


def test_invalid_endpoint_port_does_not_retain_sensitive_exception_context(
    tmp_path,
):
    sensitive_port = "SENSITIVEPORT"
    source = tmp_path / "course.mp4"
    source.write_bytes(b"not-inspected-by-configuration")

    with pytest.raises(ConfigurationFailure) as captured:
        Configuration.load(
            source,
            {
                "schema_version": "configuration.v1",
                "text_model_provider_config": {
                    "endpoint": f"https://example.invalid:{sensitive_port}"
                },
            },
        )

    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert sensitive_port not in repr(captured.value)


def test_source_symlink_loop_does_not_escape_as_a_path_bearing_runtime_error(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.symlink_to(source.name)
    source.with_suffix(".config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                "clip_policy": {"publish_ready_threshold": 91},
            }
        ),
        encoding="utf-8",
    )

    loaded = Configuration.load(source)

    assert loaded.effective.clip_policy.publish_ready_threshold == 91
