import pytest

from video_auto_editor.runtime.errors import (
    DetectedVersion,
    ERROR_REGISTRY,
    ErrorCategory,
    ErrorCode,
    ErrorModule,
    ExitCode,
    InternalLocation,
    OperatorAction,
    RemoteRequestId,
    RequiredVersion,
    RunError,
    RunStage,
    get_error_definition,
)
from video_auto_editor.runtime.identity import ErrorId


def test_exit_codes_match_the_stable_cli_contract():
    assert {
        ExitCode.SUCCESS: 0,
        ExitCode.INVALID_USAGE: 2,
        ExitCode.PREFLIGHT_FAILED: 10,
        ExitCode.INPUT_FAILED: 20,
        ExitCode.EXTERNAL_SERVICE_FAILED: 30,
        ExitCode.LOCAL_PROCESSING_FAILED: 40,
        ExitCode.DELIVERY_FAILED: 50,
        ExitCode.PUBLICATION_FAILED: 60,
        ExitCode.INTERNAL_ERROR: 70,
        ExitCode.SIGINT: 130,
        ExitCode.SIGTERM: 143,
    } == {code: code.value for code in ExitCode}


def test_each_error_category_has_one_fixed_exit_code():
    assert {
        ErrorCategory.CONFIGURATION: ExitCode.INVALID_USAGE,
        ErrorCategory.ENVIRONMENT: ExitCode.PREFLIGHT_FAILED,
        ErrorCategory.INPUT: ExitCode.INPUT_FAILED,
        ErrorCategory.EXTERNAL_SERVICE: ExitCode.EXTERNAL_SERVICE_FAILED,
        ErrorCategory.LOCAL_PROCESSING: ExitCode.LOCAL_PROCESSING_FAILED,
        ErrorCategory.DELIVERY: ExitCode.DELIVERY_FAILED,
        ErrorCategory.PUBLICATION: ExitCode.PUBLICATION_FAILED,
        ErrorCategory.INTERNAL: ExitCode.INTERNAL_ERROR,
    } == {category: category.exit_code for category in ErrorCategory}


def test_missing_credential_error_has_a_complete_stable_definition():
    definition = get_error_definition(ErrorCode.CONFIG_CREDENTIAL_MISSING)

    assert definition.code is ErrorCode.CONFIG_CREDENTIAL_MISSING
    assert definition.category is ErrorCategory.CONFIGURATION
    assert definition.exit_code is ExitCode.INVALID_USAGE
    assert definition.safe_message == "缺少所需的供应商凭据。"
    assert definition.retryable_in_new_run is True
    assert definition.operator_action is OperatorAction.CHECK_CREDENTIALS
    assert definition.allowed_diagnostics == frozenset({"capability"})


def test_first_registry_version_covers_each_required_stable_failure_condition():
    config_schema_fields = frozenset(
        {"field", "fields", "schema_version", "reason_code"}
    )
    environment_version_fields = frozenset(
        {
            "component",
            "detected_version",
            "required_version",
            "operation",
            "reason_code",
        }
    )
    environment_filesystem_fields = frozenset(
        {"component", "operation", "reason_code"}
    )
    provider_fields = frozenset(
        {"http_status", "remote_request_id", "attempt", "reason_code"}
    )
    provider_output_fields = provider_fields | {"finish_reason"}
    media_fields = frozenset(
        {
            "operation",
            "media_exit_code",
            "reason_code",
            "stderr_length",
            "stderr_sha256",
        }
    )
    delivery_media_fields = media_fields | {"artifact_role"}

    expected = {
        "config.schema_invalid": (
            "configuration",
            2,
            "配置 schema 不受支持或不合法。",
            True,
            "fix_configuration",
            config_schema_fields,
        ),
        "config.value_invalid": (
            "configuration",
            2,
            "配置包含不合法的值。",
            True,
            "fix_configuration",
            frozenset({"field", "fields", "reason_code"}),
        ),
        "config.conflict": (
            "configuration",
            2,
            "配置字段之间存在冲突。",
            True,
            "fix_configuration",
            frozenset({"fields", "reason_code"}),
        ),
        "config.credential_missing": (
            "configuration",
            2,
            "缺少所需的供应商凭据。",
            True,
            "check_credentials",
            frozenset({"capability"}),
        ),
        "config.https_required": (
            "configuration",
            2,
            "供应商地址必须使用 HTTPS。",
            True,
            "fix_configuration",
            frozenset({"field"}),
        ),
        "environment.platform_unsupported": (
            "environment",
            10,
            "当前平台不在认证生产环境范围内。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.python_unsupported": (
            "environment",
            10,
            "当前 Python 版本不受支持。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.installation_manifest_invalid": (
            "environment",
            10,
            "安装清单缺失或未通过校验。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.ffmpeg_unavailable": (
            "environment",
            10,
            "FFmpeg 不可用或缺少所需能力。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.ffprobe_unavailable": (
            "environment",
            10,
            "ffprobe 不可用或缺少所需能力。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.font_unavailable": (
            "environment",
            10,
            "烧录字幕所需字体不可用。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.tls_ca_unavailable": (
            "environment",
            10,
            "TLS 证书校验能力不可用。",
            True,
            "install_or_upgrade_dependency",
            environment_version_fields,
        ),
        "environment.workspace_unwritable": (
            "environment",
            10,
            "受管 workspace 不可写。",
            True,
            "free_disk_space",
            environment_filesystem_fields,
        ),
        "environment.atomic_publication_unsupported": (
            "environment",
            10,
            "workspace 不支持所需的原子发布语义。",
            True,
            "fix_configuration",
            environment_filesystem_fields,
        ),
        "environment.diagnostics_unwritable": (
            "environment",
            10,
            "运行诊断包无法初始化。",
            True,
            "free_disk_space",
            environment_filesystem_fields,
        ),
        "input.missing": (
            "input",
            20,
            "输入素材不存在。",
            True,
            "check_input_media",
            frozenset({"reason_code"}),
        ),
        "input.unreadable": (
            "input",
            20,
            "输入素材不可读取。",
            True,
            "check_input_media",
            frozenset({"reason_code"}),
        ),
        "input.unsupported": (
            "input",
            20,
            "输入素材格式不受支持。",
            True,
            "check_input_media",
            frozenset({"reason_code"}),
        ),
        "input.media_invalid": (
            "input",
            20,
            "输入素材不是合法可处理的媒体。",
            True,
            "check_input_media",
            frozenset(
                {
                    "reason_code",
                    "media_exit_code",
                    "stderr_length",
                    "stderr_sha256",
                }
            ),
        ),
        "input.required_stream_missing": (
            "input",
            20,
            "输入素材缺少所需的音频或视频流。",
            True,
            "check_input_media",
            frozenset({"reason_code", "stream_type"}),
        ),
        "transcription.coverage_incomplete": (
            "external_service",
            30,
            "语音覆盖补救后仍不完整。",
            True,
            "retry_later",
            frozenset({"gap_count", "gap_duration_ms", "reason_code"}),
        ),
        "transcription.audio_preparation_failed": (
            "local_processing",
            40,
            "语音识别音频准备失败。",
            True,
            "check_input_media",
            media_fields,
        ),
        "media.processing_failed": (
            "local_processing",
            40,
            "本地媒体处理失败。",
            True,
            "check_input_media",
            media_fields,
        ),
        "cache.infrastructure_failed": (
            "local_processing",
            40,
            "处理缓存基础设施失败。",
            True,
            "free_disk_space",
            frozenset({"operation", "reason_code"}),
        ),
        "diagnostics.write_failed": (
            "local_processing",
            40,
            "运行诊断无法继续持久化。",
            True,
            "free_disk_space",
            frozenset({"operation", "reason_code"}),
        ),
        "workspace.cleanup_failed": (
            "local_processing",
            40,
            "受管 workspace 清理不完整。",
            True,
            "report_internal_error",
            frozenset({"operation", "reason_code"}),
        ),
        "delivery.build_failed": (
            "delivery",
            50,
            "标准交付物构建失败。",
            True,
            "report_internal_error",
            frozenset({"operation", "artifact_role", "reason_code"}),
        ),
        "delivery.export_failed": (
            "delivery",
            50,
            "短视频导出失败。",
            True,
            "report_internal_error",
            delivery_media_fields,
        ),
        "delivery.verification_failed": (
            "delivery",
            50,
            "标准交付物完整性验证失败。",
            True,
            "report_internal_error",
            delivery_media_fields,
        ),
        "delivery.cleanup_failed": (
            "delivery",
            50,
            "未发布标准交付物清理失败。",
            True,
            "report_internal_error",
            frozenset({"operation", "reason_code"}),
        ),
        "publication.commit_failed": (
            "publication",
            60,
            "标准交付物原子发布失败。",
            True,
            "inspect_publication_backup",
            frozenset({"operation", "reason_code"}),
        ),
        "publication.backup_failed": (
            "publication",
            60,
            "上一版标准交付物备份失败。",
            True,
            "inspect_publication_backup",
            frozenset({"operation", "reason_code"}),
        ),
        "publication.rollback_failed": (
            "publication",
            60,
            "标准交付物发布回滚失败。",
            True,
            "inspect_publication_backup",
            frozenset({"operation", "reason_code"}),
        ),
        "internal.unexpected": (
            "internal",
            70,
            "发生未分类的内部错误。",
            False,
            "report_internal_error",
            frozenset({"source_module", "function", "line"}),
        ),
    }
    provider_namespaces = {
        "transcription": "语音识别",
        "topic_review": "主题评审",
        "subtitle_optimization": "字幕优化",
    }
    provider_conditions = {
        "authentication_failed": (
            "服务拒绝了已配置的凭据。",
            "check_credentials",
            provider_fields,
        ),
        "request_rejected": (
            "服务拒绝了请求。",
            "retry_later",
            provider_fields,
        ),
        "rate_limited": (
            "服务触发了请求限流。",
            "retry_later",
            provider_fields,
        ),
        "request_timeout": (
            "服务请求超时。",
            "retry_later",
            provider_fields,
        ),
        "service_unavailable": (
            "服务暂时不可用。",
            "retry_later",
            provider_fields,
        ),
        "response_protocol_invalid": (
            "服务响应不符合协议。",
            "retry_later",
            provider_fields,
        ),
        "generation_refused": (
            "服务拒绝生成结果。",
            "retry_later",
            provider_output_fields,
        ),
        "output_truncated": (
            "服务输出被截断。",
            "retry_later",
            provider_output_fields,
        ),
        "output_invalid": (
            "服务输出未通过业务校验。",
            "retry_later",
            provider_output_fields,
        ),
    }
    for namespace, phase_name in provider_namespaces.items():
        for condition, (message, action, fields) in provider_conditions.items():
            expected[f"{namespace}.{condition}"] = (
                "external_service",
                30,
                f"{phase_name}{message}",
                True,
                action,
                fields,
            )

    actual = {
        code.value: (
            definition.category.value,
            definition.exit_code.value,
            definition.safe_message,
            definition.retryable_in_new_run,
            definition.operator_action.value,
            definition.allowed_diagnostics,
        )
        for code, definition in ERROR_REGISTRY.items()
    }

    assert len(expected) == 61
    assert actual == expected


def test_run_error_is_an_immutable_projection_of_its_registry_definition():
    diagnostics = {"capability": "transcription"}

    error = RunError.create(
        code=ErrorCode.CONFIG_CREDENTIAL_MISSING,
        stage="preflight",
        module="configuration",
        event_sequence=7,
        diagnostics=diagnostics,
    )

    diagnostics["capability"] = "changed"
    assert isinstance(error.error_id, ErrorId)
    assert error.error_code is ErrorCode.CONFIG_CREDENTIAL_MISSING
    assert error.category is ErrorCategory.CONFIGURATION
    assert error.stage is RunStage.PREFLIGHT
    assert error.module is ErrorModule.CONFIGURATION
    assert error.exit_code is ExitCode.INVALID_USAGE
    assert error.safe_message == "缺少所需的供应商凭据。"
    assert error.retryable_in_new_run is True
    assert error.operator_action is OperatorAction.CHECK_CREDENTIALS
    assert error.diagnostics == {"capability": "transcription"}
    assert {error: "terminal"}[error] == "terminal"

    with pytest.raises(TypeError):
        error.diagnostics["capability"] = "changed"


def test_run_error_rejects_non_integer_event_sequence_with_a_stable_error():
    with pytest.raises(TypeError, match="event_sequence 必须是正整数"):
        RunError.create(
            code=ErrorCode.INTERNAL_UNEXPECTED,
            stage="preflight",
            module="application",
            event_sequence="not-an-integer",
        )


@pytest.mark.parametrize(
    ("field_name", "value", "error_type"),
    [
        ("stage", "a" * 65, ValueError),
        ("module", "a" * 65, ValueError),
        ("stage", 123, TypeError),
        ("module", 123, TypeError),
    ],
)
def test_run_error_rejects_unbounded_or_non_string_diagnostic_labels(
    field_name,
    value,
    error_type,
):
    arguments = {
        "code": ErrorCode.INTERNAL_UNEXPECTED,
        "stage": "transcription",
        "module": "runtime",
        "event_sequence": 1,
    }
    arguments[field_name] = value

    with pytest.raises(error_type, match=f"{field_name} 必须是稳定小写标识"):
        RunError.create(**arguments)


def test_run_error_cannot_bypass_the_registry_factory():
    with pytest.raises(TypeError, match="RunError 只能通过 create"):
        RunError()


@pytest.mark.parametrize(
    ("code", "diagnostics"),
    [
        (
            ErrorCode.CONFIG_CREDENTIAL_MISSING,
            {"capability": "sk-SECRET-or-user-body"},
        ),
        (
            ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
            {"http_status": "full transcript"},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"stderr_length": 12, "stderr_sha256": "not-a-digest"},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"reason_code": "Authorization: secret-value"},
        ),
        (
            ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
            {"remote_request_id": "sk-live-secret"},
        ),
        (
            ErrorCode.INTERNAL_UNEXPECTED,
            {
                "source_module": "/home/operator/project/secrets.py",
                "function": "run",
                "line": 1,
            },
        ),
        (
            ErrorCode.CONFIG_VALUE_INVALID,
            {"reason_code": "credential.sk_live_secret"},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"operation": "secrets.dump_api_key"},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"media_exit_code": 0},
        ),
        (
            ErrorCode.TRANSCRIPTION_RATE_LIMITED,
            {"attempt": 33},
        ),
        (
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            {"finish_reason": "unknown"},
        ),
        (
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {"schema_version": "configuration"},
        ),
    ],
)
def test_run_error_rejects_values_that_violate_field_specific_redaction_schemas(
    code,
    diagnostics,
):
    with pytest.raises((TypeError, ValueError), match="诊断字段"):
        RunError.create(
            code=code,
            stage="transcription",
            module="runtime",
            event_sequence=1,
            diagnostics=diagnostics,
        )


@pytest.mark.parametrize(
    ("code", "field", "value", "expected"),
    [
        (
            ErrorCode.CONFIG_SCHEMA_INVALID,
            "field",
            "transcription_provider_config.model",
            "transcription_provider_config.model",
        ),
        (
            ErrorCode.CONFIG_SCHEMA_INVALID,
            "fields",
            ["transcription_provider_config.model", "clip_policy.max_clips"],
            ("clip_policy.max_clips", "transcription_provider_config.model"),
        ),
        (
            ErrorCode.CONFIG_SCHEMA_INVALID,
            "schema_version",
            "configuration.v1",
            "configuration.v1",
        ),
        (
            ErrorCode.CONFIG_VALUE_INVALID,
            "reason_code",
            "value.out_of_range",
            "value.out_of_range",
        ),
        (
            ErrorCode.CONFIG_CREDENTIAL_MISSING,
            "capability",
            "transcription",
            "transcription",
        ),
        (
            ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
            "component",
            "ffmpeg",
            "ffmpeg",
        ),
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            "detected_version",
            "3.10.20",
            "3.10.20",
        ),
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            "required_version",
            ">=3.10,<3.15",
            ">=3.10,<3.15",
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            "operation",
            "ffmpeg.transcode",
            "ffmpeg.transcode",
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            "media_exit_code",
            -15,
            -15,
        ),
        (
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            "stream_type",
            "audio",
            "audio",
        ),
        (
            ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
            "http_status",
            422,
            422,
        ),
        (
            ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
            "remote_request_id",
            RemoteRequestId.from_adapter("req-01J2Y8FMRK"),
            RemoteRequestId.from_adapter("req-01J2Y8FMRK"),
        ),
        (
            ErrorCode.TRANSCRIPTION_RATE_LIMITED,
            "attempt",
            3,
            3,
        ),
        (
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            "finish_reason",
            "length",
            "length",
        ),
        (
            ErrorCode.DELIVERY_BUILD_FAILED,
            "artifact_role",
            "short_video_media",
            "short_video_media",
        ),
    ],
)
def test_every_allowed_diagnostic_field_has_a_strict_value_schema(
    code,
    field,
    value,
    expected,
):
    error = RunError.create(
        code=code,
        stage="transcription",
        module="runtime",
        event_sequence=1,
        diagnostics={field: value},
    )

    assert error.diagnostics[field] == expected


@pytest.mark.parametrize(
    ("code", "diagnostics", "expected"),
    [
        (
            ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
            {"gap_count": 2, "gap_duration_ms": 1_250},
            {"gap_count": 2, "gap_duration_ms": 1_250},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"stderr_length": 4_096, "stderr_sha256": "a" * 64},
            {"stderr_length": 4_096, "stderr_sha256": "a" * 64},
        ),
        (
            ErrorCode.INTERNAL_UNEXPECTED,
            {
                "source_module": "video_auto_editor.runtime.errors",
                "function": "_freeze_diagnostics",
                "line": 581,
            },
            {
                "source_module": "video_auto_editor.runtime.errors",
                "function": "_freeze_diagnostics",
                "line": 581,
            },
        ),
    ],
)
def test_related_diagnostic_fields_are_accepted_as_complete_safe_facts(
    code,
    diagnostics,
    expected,
):
    error = RunError.create(
        code=code,
        stage="transcription",
        module="runtime",
        event_sequence=1,
        diagnostics=diagnostics,
    )

    assert error.diagnostics == expected


def test_run_error_rejects_a_non_mapping_diagnostics_container():
    with pytest.raises(TypeError, match="diagnostics 必须是映射"):
        RunError.create(
            code=ErrorCode.INTERNAL_UNEXPECTED,
            stage="transcription",
            module="runtime",
            event_sequence=1,
            diagnostics=["source_module", "video_auto_editor.runtime.errors"],
        )


@pytest.mark.parametrize(
    ("code", "diagnostics"),
    [
        (
            ErrorCode.CONFIG_SCHEMA_INVALID,
            {"field": "language", "fields": ["language"]},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"stderr_length": 128},
        ),
        (
            ErrorCode.MEDIA_PROCESSING_FAILED,
            {"stderr_sha256": "a" * 64},
        ),
        (
            ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
            {"gap_count": 1},
        ),
        (
            ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
            {"gap_duration_ms": 250},
        ),
        (
            ErrorCode.INTERNAL_UNEXPECTED,
            {"source_module": "video_auto_editor.runtime.errors"},
        ),
    ],
)
def test_related_diagnostic_fields_must_form_complete_safe_facts(
    code,
    diagnostics,
):
    with pytest.raises(ValueError, match="诊断字段.*必须"):
        RunError.create(
            code=code,
            stage="transcription",
            module="runtime",
            event_sequence=1,
            diagnostics=diagnostics,
        )


@pytest.mark.parametrize(
    ("code", "diagnostics"),
    [
        (
            ErrorCode.CONFIG_VALUE_INVALID,
            {"reason_code": "publication.rollback_state_uncertain"},
        ),
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            {"component": "ffmpeg"},
        ),
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            {"operation": "publication.rollback"},
        ),
        (
            ErrorCode.DELIVERY_BUILD_FAILED,
            {"operation": "cache.clear"},
        ),
        (
            ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
            {"finish_reason": "stop"},
        ),
        (
            ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED,
            {"http_status": 429},
        ),
    ],
)
def test_diagnostic_values_must_match_the_specific_error_code(
    code,
    diagnostics,
):
    with pytest.raises(ValueError, match="诊断字段.*不适用于"):
        RunError.create(
            code=code,
            stage="transcription",
            module="runtime",
            event_sequence=1,
            diagnostics=diagnostics,
        )


@pytest.mark.parametrize(
    "code",
    [
        ErrorCode.CONFIG_CREDENTIAL_MISSING,
        ErrorCode.CONFIG_HTTPS_REQUIRED,
        ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
        ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
        ErrorCode.INTERNAL_UNEXPECTED,
    ],
)
def test_errors_with_intrinsic_safe_facts_require_their_diagnostics(code):
    with pytest.raises(ValueError, match="缺少必需诊断字段"):
        RunError.create(
            code=code,
            stage="preflight",
            module="runtime",
            event_sequence=1,
        )


def test_registry_order_is_part_of_aggregate_preflight_precedence():
    provider_conditions = (
        "authentication_failed",
        "request_rejected",
        "rate_limited",
        "request_timeout",
        "service_unavailable",
        "response_protocol_invalid",
        "generation_refused",
        "output_truncated",
        "output_invalid",
    )
    expected = [
        "config.schema_invalid",
        "config.value_invalid",
        "config.conflict",
        "config.credential_missing",
        "config.https_required",
        "environment.platform_unsupported",
        "environment.python_unsupported",
        "environment.installation_manifest_invalid",
        "environment.ffmpeg_unavailable",
        "environment.ffprobe_unavailable",
        "environment.font_unavailable",
        "environment.tls_ca_unavailable",
        "environment.workspace_unwritable",
        "environment.atomic_publication_unsupported",
        "environment.diagnostics_unwritable",
        "input.missing",
        "input.unreadable",
        "input.unsupported",
        "input.media_invalid",
        "input.required_stream_missing",
    ]
    for namespace in (
        "transcription",
        "topic_review",
        "subtitle_optimization",
    ):
        expected.extend(
            f"{namespace}.{condition}" for condition in provider_conditions
        )
    expected.extend(
        [
            "transcription.coverage_incomplete",
            "transcription.audio_preparation_failed",
            "media.processing_failed",
            "cache.infrastructure_failed",
            "diagnostics.write_failed",
            "workspace.cleanup_failed",
            "delivery.build_failed",
            "delivery.export_failed",
            "delivery.verification_failed",
            "delivery.cleanup_failed",
            "publication.commit_failed",
            "publication.backup_failed",
            "publication.rollback_failed",
            "internal.unexpected",
        ]
    )

    assert tuple(code.value for code in ERROR_REGISTRY) == tuple(expected)


@pytest.mark.parametrize(
    ("code", "diagnostics"),
    [
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            {"detected_version": "sk_live_secret"},
        ),
        (
            ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
            {"required_version": "sk_live_secret"},
        ),
        (
            ErrorCode.INTERNAL_UNEXPECTED,
            {
                "source_module": "video_auto_editor.runtime.errors",
                "function": "sk_live_secret",
                "line": 1,
            },
        ),
    ],
)
def test_dynamic_diagnostics_reject_secret_like_raw_values(
    code,
    diagnostics,
):
    with pytest.raises(ValueError, match="诊断字段"):
        RunError.create(
            code=code,
            stage=RunStage.PREFLIGHT,
            module=ErrorModule.RUNTIME,
            event_sequence=1,
            diagnostics=diagnostics,
        )


def test_fact_owner_values_preserve_safe_diagnostic_projections():
    location = InternalLocation.from_runtime(
        source_module="video_auto_editor.runtime.errors",
        function="_freeze_diagnostics",
        line=581,
    )

    version_error = RunError.create(
        code=ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
        stage=RunStage.PREFLIGHT,
        module=ErrorModule.READINESS,
        event_sequence=1,
        diagnostics={
            "detected_version": DetectedVersion.from_readiness("3.10.20"),
            "required_version": RequiredVersion.from_readiness(">=3.12.3,<3.13"),
        },
    )
    internal_error = RunError.create(
        code=ErrorCode.INTERNAL_UNEXPECTED,
        stage=RunStage.PREFLIGHT,
        module=ErrorModule.RUNTIME,
        event_sequence=2,
        diagnostics={
            "source_module": location.source_module,
            "function": location.function,
            "line": location.line,
        },
    )

    assert version_error.diagnostics == {
        "detected_version": "3.10.20",
        "required_version": ">=3.12.3,<3.13",
    }
    assert internal_error.diagnostics == {
        "source_module": "video_auto_editor.runtime.errors",
        "function": "_freeze_diagnostics",
        "line": 581,
    }


def test_configuration_values_are_narrowed_by_error_code():
    with pytest.raises(ValueError, match="诊断字段 field.*不适用于"):
        RunError.create(
            code=ErrorCode.CONFIG_HTTPS_REQUIRED,
            stage=RunStage.PREFLIGHT,
            module=ErrorModule.CONFIGURATION,
            event_sequence=1,
            diagnostics={"field": "clip_policy.max_clips"},
        )
    with pytest.raises(ValueError, match="诊断字段 schema_version"):
        RunError.create(
            code=ErrorCode.CONFIG_SCHEMA_INVALID,
            stage=RunStage.PREFLIGHT,
            module=ErrorModule.CONFIGURATION,
            event_sequence=2,
            diagnostics={"schema_version": "delivery_manifest.v1"},
        )

    error = RunError.create(
        code=ErrorCode.CONFIG_HTTPS_REQUIRED,
        stage=RunStage.PREFLIGHT,
        module=ErrorModule.CONFIGURATION,
        event_sequence=3,
        diagnostics={"field": "transcription_provider_config.endpoint"},
    )

    assert error.diagnostics == {
        "field": "transcription_provider_config.endpoint"
    }


@pytest.mark.parametrize(
    "diagnostics",
    [
        {
            "reason_code": "media.spawn_failed",
            "media_exit_code": -1,
            "stderr_length": 4,
            "stderr_sha256": "a" * 64,
        },
        {
            "reason_code": "media.output_missing",
            "media_exit_code": 1,
            "stderr_length": 4,
            "stderr_sha256": "a" * 64,
        },
        {"reason_code": "media.process_failed"},
    ],
)
def test_media_reason_selects_one_consistent_diagnostic_variant(diagnostics):
    with pytest.raises(ValueError, match="诊断字段.*media"):
        RunError.create(
            code=ErrorCode.MEDIA_PROCESSING_FAILED,
            stage=RunStage.DELIVERY_BUILD,
            module=ErrorModule.DELIVERY_BUILD,
            event_sequence=1,
            diagnostics=diagnostics,
        )


def test_media_process_failure_accepts_complete_process_facts():
    error = RunError.create(
        code=ErrorCode.MEDIA_PROCESSING_FAILED,
        stage=RunStage.DELIVERY_BUILD,
        module=ErrorModule.DELIVERY_BUILD,
        event_sequence=1,
        diagnostics={
            "reason_code": "media.process_failed",
            "media_exit_code": 1,
            "stderr_length": 4,
            "stderr_sha256": "a" * 64,
        },
    )

    assert error.diagnostics == {
        "media_exit_code": 1,
        "reason_code": "media.process_failed",
        "stderr_length": 4,
        "stderr_sha256": "a" * 64,
    }


def test_empty_stderr_requires_the_sha256_of_empty_bytes():
    empty_sha256 = (
        "e3b0c44298fc1c149afbf4c8996fb924"
        "27ae41e4649b934ca495991b7852b855"
    )
    with pytest.raises(ValueError, match="诊断字段.*stderr"):
        RunError.create(
            code=ErrorCode.MEDIA_PROCESSING_FAILED,
            stage=RunStage.DELIVERY_BUILD,
            module=ErrorModule.DELIVERY_BUILD,
            event_sequence=1,
            diagnostics={
                "stderr_length": 0,
                "stderr_sha256": "a" * 64,
            },
        )

    error = RunError.create(
        code=ErrorCode.MEDIA_PROCESSING_FAILED,
        stage=RunStage.DELIVERY_BUILD,
        module=ErrorModule.DELIVERY_BUILD,
        event_sequence=2,
        diagnostics={
            "stderr_length": 0,
            "stderr_sha256": empty_sha256,
        },
    )

    assert error.diagnostics == {
        "stderr_length": 0,
        "stderr_sha256": empty_sha256,
    }
