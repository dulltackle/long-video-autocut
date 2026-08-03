import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

import video_auto_editor.workspace._workspace as workspace_effects
from tests.support.deterministic_composition import (
    ProductionFailureInjection,
    compose_deterministic_live_application,
)
from video_auto_editor.application import LiveRunRequest, LiveRunState
from video_auto_editor.application._readiness_model import ReadinessIssue
from video_auto_editor.cache import (
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheFailure,
    CacheIdentity,
    CacheNamespace,
)
from video_auto_editor.cache.filesystem import initialize_cache_repository
from video_auto_editor.composition import _ReadinessFailure
from video_auto_editor.configuration import ConfigurationFailure
from video_auto_editor.delivery._model import DeliveryBuildFailure
from video_auto_editor.delivery.publication import PublicationFailure
from video_auto_editor.delivery.verification import DeliveryVerificationFailure
from video_auto_editor.runtime.cancellation import CancellationSource
from video_auto_editor.runtime.errors import (
    ERROR_REGISTRY,
    ErrorCode,
    ErrorModule,
    ExitCode,
    RunStage,
    get_error_definition,
)
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceAnalysisFailure
from video_auto_editor.subtitle_optimization import (
    SubtitleOptimizationExecutionFacts,
    SubtitleOptimizationFailure,
)
from video_auto_editor.topic_review import (
    TopicReviewExecutionFacts,
    TopicReviewFailure,
)
from video_auto_editor.transcription import (
    CacheUse,
    ExecutionFacts,
    TranscriptionFailure,
)
from video_auto_editor.workspace import Workspace

_SOURCE_CANARY = b"private-source-content-canary"
_CURRENT_DELIVERY_CANARY = b"private-current-delivery-canary"
_PREVIOUS_DELIVERY_CANARY = b"private-previous-delivery-canary"
_CACHE_CANARY = "private-valid-cache-canary"


@dataclass(frozen=True, slots=True)
class _FailureExpectation:
    error_code: ErrorCode
    stage: RunStage
    module: ErrorModule
    diagnostics: dict[str, object]


def _scenario(
    error_code: ErrorCode,
    stage: RunStage,
    module: ErrorModule,
    diagnostics=None,
) -> _FailureExpectation:
    return _FailureExpectation(
        error_code=error_code,
        stage=stage,
        module=module,
        diagnostics={} if diagnostics is None else dict(diagnostics),
    )


# 每一行只保存独立期望；实际异常由生产 failure 类型或真实持久化故障产生点形成。
FAILURE_SCENARIOS = (
    _scenario(
        ErrorCode.CONFIG_SCHEMA_INVALID,
        RunStage.PREFLIGHT,
        ErrorModule.CONFIGURATION,
    ),
    _scenario(
        ErrorCode.CONFIG_VALUE_INVALID,
        RunStage.PREFLIGHT,
        ErrorModule.CONFIGURATION,
    ),
    _scenario(
        ErrorCode.CONFIG_CONFLICT,
        RunStage.PREFLIGHT,
        ErrorModule.CONFIGURATION,
    ),
    _scenario(
        ErrorCode.CONFIG_CREDENTIAL_MISSING,
        RunStage.PREFLIGHT,
        ErrorModule.CONFIGURATION,
        {"capability": "topic_review"},
    ),
    _scenario(
        ErrorCode.CONFIG_HTTPS_REQUIRED,
        RunStage.PREFLIGHT,
        ErrorModule.CONFIGURATION,
        {"field": "text_model_provider_config.endpoint"},
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_PLATFORM_UNSUPPORTED,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_PYTHON_UNSUPPORTED,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    # 分类矩阵锁定应用层投影；安装清单的物理校验由 Readiness 契约测试覆盖。
    _scenario(
        ErrorCode.ENVIRONMENT_INSTALLATION_MANIFEST_INVALID,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_FFPROBE_UNAVAILABLE,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_FONT_UNAVAILABLE,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_TLS_CA_UNAVAILABLE,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_WORKSPACE_UNWRITABLE,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_ATOMIC_PUBLICATION_UNSUPPORTED,
        RunStage.PREFLIGHT,
        ErrorModule.READINESS,
    ),
    _scenario(
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
        RunStage.PREFLIGHT,
        ErrorModule.RUN_DIAGNOSTICS,
        {
            "component": "run_diagnostics",
            "operation": "diagnostics.initialize",
            "reason_code": "diagnostics.open_failed",
        },
    ),
    _scenario(
        ErrorCode.INPUT_MISSING,
        RunStage.INITIALIZED,
        ErrorModule.WORKSPACE,
        {"reason_code": "input.not_found"},
    ),
    _scenario(
        ErrorCode.INPUT_UNREADABLE,
        RunStage.SOURCE_ANALYSIS,
        ErrorModule.SOURCE_ANALYSIS,
    ),
    _scenario(
        ErrorCode.INPUT_UNSUPPORTED,
        RunStage.SOURCE_ANALYSIS,
        ErrorModule.SOURCE_ANALYSIS,
    ),
    _scenario(
        ErrorCode.INPUT_MEDIA_INVALID,
        RunStage.SOURCE_ANALYSIS,
        ErrorModule.SOURCE_ANALYSIS,
    ),
    _scenario(
        ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
        RunStage.SOURCE_ANALYSIS,
        ErrorModule.SOURCE_ANALYSIS,
        {"stream_type": "audio"},
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_AUTHENTICATION_FAILED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_REQUEST_REJECTED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_RATE_LIMITED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_REQUEST_TIMEOUT,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_GENERATION_REFUSED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_OUTPUT_TRUNCATED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
        {"gap_count": 1, "gap_duration_ms": 250},
    ),
    _scenario(
        ErrorCode.TRANSCRIPTION_AUDIO_PREPARATION_FAILED,
        RunStage.TRANSCRIPTION,
        ErrorModule.TRANSCRIPTION,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_AUTHENTICATION_FAILED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_REQUEST_REJECTED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_RATE_LIMITED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_REQUEST_TIMEOUT,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_SERVICE_UNAVAILABLE,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_RESPONSE_PROTOCOL_INVALID,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_GENERATION_REFUSED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_OUTPUT_TRUNCATED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID,
        RunStage.TOPIC_REVIEW,
        ErrorModule.TOPIC_REVIEW,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_AUTHENTICATION_FAILED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_REJECTED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_RATE_LIMITED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_TIMEOUT,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_SERVICE_UNAVAILABLE,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_RESPONSE_PROTOCOL_INVALID,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_GENERATION_REFUSED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_TRUNCATED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID,
        RunStage.DELIVERY_BUILD,
        ErrorModule.SUBTITLE_OPTIMIZATION,
    ),
    _scenario(
        ErrorCode.MEDIA_PROCESSING_FAILED,
        RunStage.CANDIDATE_PLANNING,
        ErrorModule.CLIP_PLANNING,
    ),
    _scenario(
        ErrorCode.CACHE_INFRASTRUCTURE_FAILED,
        RunStage.TRANSCRIPTION,
        ErrorModule.CACHE,
    ),
    _scenario(
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        RunStage.PREFLIGHT,
        ErrorModule.RUN_DIAGNOSTICS,
        {
            "operation": "diagnostics.append",
            "reason_code": "diagnostics.append_failed",
        },
    ),
    _scenario(
        ErrorCode.WORKSPACE_CLEANUP_FAILED,
        RunStage.TOPIC_REVIEW,
        ErrorModule.WORKSPACE,
        {
            "operation": "workspace.cleanup",
            "reason_code": "workspace.directory_sync_failed",
        },
    ),
    _scenario(
        ErrorCode.DELIVERY_BUILD_FAILED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.DELIVERY_BUILD,
    ),
    _scenario(
        ErrorCode.DELIVERY_EXPORT_FAILED,
        RunStage.DELIVERY_BUILD,
        ErrorModule.DELIVERY_BUILD,
    ),
    _scenario(
        ErrorCode.DELIVERY_VERIFICATION_FAILED,
        RunStage.DELIVERY_VERIFICATION,
        ErrorModule.DELIVERY_VERIFICATION,
    ),
    _scenario(
        ErrorCode.DELIVERY_CLEANUP_FAILED,
        RunStage.DELIVERY_VERIFICATION,
        ErrorModule.DELIVERY_BUILD,
        {
            "operation": "workspace.cleanup",
            "reason_code": "workspace.directory_sync_failed",
        },
    ),
    _scenario(
        ErrorCode.PUBLICATION_COMMIT_FAILED,
        RunStage.PUBLISHING,
        ErrorModule.PUBLICATION,
    ),
    _scenario(
        ErrorCode.PUBLICATION_BACKUP_FAILED,
        RunStage.PUBLISHING,
        ErrorModule.PUBLICATION,
    ),
    _scenario(
        ErrorCode.PUBLICATION_ROLLBACK_FAILED,
        RunStage.PUBLISHING,
        ErrorModule.PUBLICATION,
        {
            "operation": "publication.sync",
            "reason_code": "publication.directory_sync_failed",
        },
    ),
    _scenario(
        ErrorCode.INTERNAL_UNEXPECTED,
        RunStage.CANDIDATE_PLANNING,
        ErrorModule.APPLICATION,
        {
            "source_module": "video_auto_editor.application.live",
            "function": "execute",
            "line": 1,
        },
    ),
)


_LIFECYCLE_FAULTS = frozenset(
    {
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
        ErrorCode.INPUT_MISSING,
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
        ErrorCode.WORKSPACE_CLEANUP_FAILED,
        ErrorCode.DELIVERY_CLEANUP_FAILED,
        ErrorCode.PUBLICATION_ROLLBACK_FAILED,
        ErrorCode.INTERNAL_UNEXPECTED,
    }
)


def _production_failure_injection(
    expectation: _FailureExpectation,
) -> ProductionFailureInjection:
    code = expectation.error_code
    diagnostics = expectation.diagnostics

    def create_failure() -> Exception:
        if code.value.startswith("config."):
            return ConfigurationFailure(code, diagnostics)
        if code.value.startswith("environment."):
            return _ReadinessFailure(ReadinessIssue(code, diagnostics))
        if code.value.startswith("input."):
            return SourceAnalysisFailure(code, diagnostics)
        if code.value.startswith("transcription."):
            return TranscriptionFailure(
                code,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics=diagnostics,
            )
        if code.value.startswith("topic_review."):
            return TopicReviewFailure(
                code,
                execution_facts=TopicReviewExecutionFacts(
                    batch_count=1,
                    model_request_count=1,
                ),
                diagnostics=diagnostics,
            )
        if code.value.startswith("subtitle_optimization."):
            return SubtitleOptimizationFailure(
                code,
                execution_facts=SubtitleOptimizationExecutionFacts(
                    short_video_count=1,
                    window_count=1,
                    model_request_count=1,
                ),
                diagnostics=diagnostics,
            )
        if code is ErrorCode.MEDIA_PROCESSING_FAILED:
            return SourceAnalysisFailure(code, diagnostics)
        if code is ErrorCode.CACHE_INFRASTRUCTURE_FAILED:
            return CacheFailure(diagnostics)
        if code in {
            ErrorCode.DELIVERY_BUILD_FAILED,
            ErrorCode.DELIVERY_EXPORT_FAILED,
        }:
            return DeliveryBuildFailure(code, diagnostics)
        if code is ErrorCode.DELIVERY_VERIFICATION_FAILED:
            return DeliveryVerificationFailure(diagnostics)
        if code in {
            ErrorCode.PUBLICATION_COMMIT_FAILED,
            ErrorCode.PUBLICATION_BACKUP_FAILED,
        }:
            return PublicationFailure(code, diagnostics)
        raise AssertionError(f"缺少生产 failure 工厂：{code.value}")

    return ProductionFailureInjection(
        stage=expectation.stage,
        failure_factory=create_failure,
    )


def _cache_entry() -> CacheEntrySpec[str]:
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version="fault-matrix.identity.v1",
        algorithm_version="fault-matrix.algorithm.v1",
        payload_schema_version="fault-matrix.payload.v1",
        adapter_id="deterministic",
        model_id="fault-matrix-v1",
        configuration_fingerprint="a" * 64,
        result_inputs={"source_sha256": "b" * 64},
    )

    def decode(payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"text"}
            or not isinstance(payload["text"], str)
        ):
            raise CachedPayloadInvalid()
        return payload["text"]

    return CacheEntrySpec(
        identity=identity,
        encode=lambda value: {"text": value},
        decode=decode,
    )


def _regular_file_snapshot(directory: Path) -> dict[str, bytes]:
    return {
        path.relative_to(directory).as_posix(): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _seed_preserved_state(source: Path, workspace_path: Path):
    source.write_bytes(_SOURCE_CANARY)
    workspace = Workspace.open(source, workspace_path)
    (workspace_path / "delivery" / "current.txt").write_bytes(_CURRENT_DELIVERY_CANARY)
    (workspace_path / "delivery.previous" / "previous.txt").write_bytes(
        _PREVIOUS_DELIVERY_CANARY
    )
    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        first = repository.resolve(
            _cache_entry(),
            cancellation=CancellationSource().token,
            compute=lambda: _CACHE_CANARY,
        )
        replay = repository.resolve(
            _cache_entry(),
            cancellation=CancellationSource().token,
            compute=lambda: "must-not-recompute",
        )
        assert first.from_cache is False
        assert replay.from_cache is True
        assert replay.value == _CACHE_CANARY
        run_workspace.cleanup()
    return workspace


def _arm_run_cleanup_sync_failure(monkeypatch, workspace_path: Path):
    original_sync = workspace_effects._sync_cleanup_directory
    injected = False

    def fail_run_temporary_sync(descriptor):
        nonlocal injected
        try:
            descriptor_path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
        except OSError:
            descriptor_path = Path("/")
        run_temporary_parent = workspace_path / "work" / "tmp"
        if not injected and descriptor_path.parent == run_temporary_parent:
            injected = True
            from video_auto_editor.workspace import WorkspaceFailure

            raise WorkspaceFailure(
                ErrorCode.WORKSPACE_CLEANUP_FAILED,
                {
                    "operation": "workspace.cleanup",
                    "reason_code": "workspace.directory_sync_failed",
                },
            )
        return original_sync(descriptor)

    monkeypatch.setattr(
        workspace_effects,
        "_sync_cleanup_directory",
        fail_run_temporary_sync,
    )
    return lambda: injected


def _arm_publication_rollback_failure(monkeypatch):
    original_inspection = workspace_effects._inspect_layout_descriptor
    original_sync = workspace_effects._sync_publication_directory
    original_exchange = workspace_effects._exchange_publication_directories
    exchanged = False
    rollback_started = False
    rollback_sync_failed = False

    def fail_commit_inspection(*args, **kwargs):
        nonlocal rollback_started
        if exchanged and kwargs.get("expected") is None:
            rollback_started = True
            raise OSError("private commit inspection failure canary")
        return original_inspection(*args, **kwargs)

    def observe_exchange(*args):
        nonlocal exchanged
        original_exchange(*args)
        exchanged = True

    def fail_rollback_sync(descriptor):
        nonlocal rollback_sync_failed
        if rollback_started and not rollback_sync_failed:
            rollback_sync_failed = True
            raise OSError("private rollback sync failure canary")
        return original_sync(descriptor)

    monkeypatch.setattr(
        workspace_effects,
        "_inspect_layout_descriptor",
        fail_commit_inspection,
    )
    monkeypatch.setattr(
        workspace_effects,
        "_exchange_publication_directories",
        observe_exchange,
    )
    monkeypatch.setattr(
        workspace_effects,
        "_sync_publication_directory",
        fail_rollback_sync,
    )
    return lambda: exchanged and rollback_started and rollback_sync_failed


def test_failure_classification_ledger_exactly_covers_the_public_registry():
    scenario_codes = tuple(scenario.error_code for scenario in FAILURE_SCENARIOS)

    assert len(scenario_codes) == len(set(scenario_codes))
    assert set(scenario_codes) == set(ERROR_REGISTRY)


@pytest.mark.parametrize(
    "scenario",
    tuple(
        scenario
        for scenario in FAILURE_SCENARIOS
        if scenario.error_code not in _LIFECYCLE_FAULTS
    ),
    ids=lambda scenario: scenario.error_code.value,
)
def test_primary_fault_injections_are_backed_by_production_failure_types(
    scenario,
):
    failure = _production_failure_injection(scenario).create_failure()

    assert type(failure).__module__.startswith("video_auto_editor.")
    assert failure.error_code is scenario.error_code
    assert dict(failure.diagnostics) == scenario.diagnostics


@pytest.mark.parametrize(
    "scenario",
    FAILURE_SCENARIOS,
    ids=lambda scenario: scenario.error_code.value,
)
def test_each_stable_failure_crosses_the_public_live_run_seam(
    tmp_path,
    monkeypatch,
    scenario,
):
    source = tmp_path / "course.mp4"
    workspace_path = tmp_path / "workspace"
    _seed_preserved_state(source, workspace_path)
    preserved = {
        "delivery": _regular_file_snapshot(workspace_path / "delivery"),
        "previous": _regular_file_snapshot(workspace_path / "delivery.previous"),
        "cache": _regular_file_snapshot(workspace_path / "work" / "cache"),
    }

    code = scenario.error_code

    def fault_observed():
        return True

    if code is ErrorCode.INPUT_MISSING:
        source.unlink()
        application = compose_deterministic_live_application()
    elif code is ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE:
        application = compose_deterministic_live_application(
            diagnostics_failure="startup"
        )
    elif code is ErrorCode.DIAGNOSTICS_WRITE_FAILED:
        application = compose_deterministic_live_application(
            diagnostics_failure="precommit"
        )
    elif code in {
        ErrorCode.WORKSPACE_CLEANUP_FAILED,
        ErrorCode.DELIVERY_CLEANUP_FAILED,
    }:
        fault_observed = _arm_run_cleanup_sync_failure(
            monkeypatch,
            workspace_path,
        )
        application = compose_deterministic_live_application(
            interruption_stage=scenario.stage,
        )
    elif code is ErrorCode.PUBLICATION_ROLLBACK_FAILED:
        fault_observed = _arm_publication_rollback_failure(monkeypatch)
        application = compose_deterministic_live_application()
    elif code is ErrorCode.INTERNAL_UNEXPECTED:
        application = compose_deterministic_live_application(
            unexpected_stage=scenario.stage,
        )
    else:
        application = compose_deterministic_live_application(
            failure_injection=_production_failure_injection(scenario)
        )

    outcome = application.execute(
        LiveRunRequest(source, workspace_dir=workspace_path, overwrite=True)
    )
    assert fault_observed()

    definition = get_error_definition(code)
    cleanup_fault = code in {
        ErrorCode.WORKSPACE_CLEANUP_FAILED,
        ErrorCode.DELIVERY_CLEANUP_FAILED,
    }
    diagnostics_unavailable = code in {
        ErrorCode.ENVIRONMENT_DIAGNOSTICS_UNWRITABLE,
        ErrorCode.DIAGNOSTICS_WRITE_FAILED,
    }
    rollback_fault = code is ErrorCode.PUBLICATION_ROLLBACK_FAILED
    recovery_incomplete = cleanup_fault or rollback_fault

    assert outcome.state is (
        LiveRunState.INTERRUPTED if cleanup_fault else LiveRunState.FAILED
    )
    assert outcome.exit_code is (
        ExitCode.SIGINT if cleanup_fault else definition.exit_code
    )
    assert outcome.primary_error_code is (None if cleanup_fault else code)
    assert outcome.recovery_incomplete is recovery_incomplete
    assert outcome.diagnostics_incomplete is diagnostics_unavailable

    if cleanup_fault:
        assert outcome.primary_error is None
        assert len(outcome.associated_errors) == 1
        recorded_failure = outcome.associated_errors[0]
    elif diagnostics_unavailable:
        assert outcome.primary_error is None
        assert outcome.associated_errors == ()
        recorded_failure = None
    else:
        assert outcome.primary_error is not None
        recorded_failure = outcome.primary_error
        if rollback_fault:
            assert tuple(error.error_code for error in outcome.associated_errors) == (
                ErrorCode.DELIVERY_CLEANUP_FAILED,
            )
        else:
            assert outcome.associated_errors == ()

    if recorded_failure is not None:
        assert recorded_failure.error_code is code
        assert recorded_failure.category is definition.category
        assert recorded_failure.stage is scenario.stage
        assert recorded_failure.module is scenario.module
        assert recorded_failure.safe_message == definition.safe_message
        if code is ErrorCode.INTERNAL_UNEXPECTED:
            assert set(recorded_failure.diagnostics) == {
                "source_module",
                "function",
                "line",
            }
        else:
            assert dict(recorded_failure.diagnostics) == scenario.diagnostics

    run_directory = workspace_path / "work" / "runs" / str(outcome.run_id)
    manifest_path = run_directory / "run.json"
    events_path = run_directory / "events.jsonl"
    if diagnostics_unavailable:
        assert not manifest_path.exists()
        manifest_bytes = b""
        events_bytes = b"" if not events_path.exists() else events_path.read_bytes()
    else:
        manifest_bytes = manifest_path.read_bytes()
        events_bytes = events_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        assert manifest["lifecycle"]["outcome"] == (
            "interrupted" if cleanup_fault else "failed"
        )
        assert manifest["lifecycle"]["exit_code"] == int(outcome.exit_code)
        manifest_errors = manifest["errors"]
        serialized_failure = (
            manifest_errors["associated_errors"][0]
            if cleanup_fault
            else manifest_errors["primary_error"]
        )
        assert serialized_failure["error_code"] == code.value
        assert serialized_failure["category"] == definition.category.value
        assert serialized_failure["stage"] == scenario.stage.value
        assert serialized_failure["module"] == scenario.module.value
        assert serialized_failure["safe_message"] == definition.safe_message
        if code is not ErrorCode.INTERNAL_UNEXPECTED:
            assert serialized_failure["diagnostics"] == scenario.diagnostics
        assert manifest_errors["recovery_incomplete"] is recovery_incomplete
        events = [
            json.loads(line) for line in events_bytes.decode("utf-8").splitlines()
        ]
        matching_errors = [
            event
            for event in events
            if event["event_code"] == "error.recorded"
            and event["attributes"]["error_code"] == code.value
        ]
        assert len(matching_errors) == 1
        assert matching_errors[0]["stage"] == scenario.stage.value
        assert matching_errors[0]["module"] == scenario.module.value

    temporary_run = workspace_path / "work" / "tmp" / str(outcome.run_id)
    assert temporary_run.exists() is recovery_incomplete
    if rollback_fault:
        assert (temporary_run / ".publication-transaction.json").is_file()
    assert _regular_file_snapshot(workspace_path / "delivery") == preserved["delivery"]
    assert (
        _regular_file_snapshot(workspace_path / "delivery.previous")
        == preserved["previous"]
    )
    assert (
        _regular_file_snapshot(workspace_path / "work" / "cache")
        == (preserved["cache"])
    )
    diagnostic_bytes = events_bytes + manifest_bytes
    for forbidden in (
        b"secret exception detail",
        b"private cleanup sync failure canary",
        b"private commit inspection failure canary",
        b"private rollback sync failure canary",
        _SOURCE_CANARY,
        _CURRENT_DELIVERY_CANARY,
        _PREVIOUS_DELIVERY_CANARY,
        _CACHE_CANARY.encode(),
        str(source.resolve()).encode(),
        str(workspace_path.resolve()).encode(),
    ):
        assert forbidden not in diagnostic_bytes

    if recovery_incomplete:
        monkeypatch.undo()
        recovered = Workspace.open(source, workspace_path)
        with recovered.acquire_run(RunId.new()) as run_workspace:
            run_workspace.cleanup()
        assert not temporary_run.exists()
        assert (
            _regular_file_snapshot(workspace_path / "delivery") == preserved["delivery"]
        )
        assert (
            _regular_file_snapshot(workspace_path / "delivery.previous")
            == preserved["previous"]
        )
