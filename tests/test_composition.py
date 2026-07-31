import json
import subprocess
import tempfile
from pathlib import Path

import pytest

from video_auto_editor import composition
from video_auto_editor.application import (
    LiveApplication,
    LiveRunRequest,
    LiveRunState,
)
from video_auto_editor.diagnostics import ResultKind
from video_auto_editor.readiness import CommandResult, Readiness, TLSObservation
from video_auto_editor.runtime.errors import ERROR_REGISTRY, ErrorCode, ExitCode
from video_auto_editor.transcription._stepaudio_https import (
    StdlibStepAudioTransport,
)
from video_auto_editor.transcription.stepaudio import (
    StepAudioTransportResponse,
)


class _CertifiedProbe:
    def platform_name(self) -> str:
        return "linux"

    def architecture(self) -> str:
        return "x86_64"

    def os_release(self) -> dict[str, str]:
        return {"ID": "ubuntu", "VERSION_ID": "24.04"}

    def python_implementation(self) -> str:
        return "CPython"

    def python_version(self) -> tuple[int, int, int]:
        return (3, 12, 3)

    def is_virtual_environment(self) -> bool:
        return True

    def which(self, command: str) -> str | None:
        return f"/usr/bin/{command}"

    def run(
        self,
        command: tuple[str, ...],
        *,
        cwd: Path | None = None,
        timeout_seconds: int,
    ) -> CommandResult:
        del cwd, timeout_seconds
        if command[1:] == ("-version",):
            return CommandResult(0, f"{command[0]} version 6.1.1\n")
        if command[1:] == ("-hide_banner", "-filters"):
            return CommandResult(0, " T.. subtitles V->V Render subtitles\n")
        if command[1:] == ("-hide_banner", "-encoders"):
            return CommandResult(0, " V....D libx264\n A..... aac\n")
        if command[0] == "fc-list":
            return CommandResult(0, "")
        if command[0] == "fc-match":
            return CommandResult(
                0,
                "Noto Sans CJK SC\n/usr/share/fonts/noto/NotoSansCJK.ttc\n",
            )
        if command[0] == "ffmpeg":
            return CommandResult(0, "")
        if command[0] == "ffprobe":
            return CommandResult(
                0,
                '{"format":{"format_name":"mov,mp4","duration":"1.000"},'
                '"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}',
            )
        raise AssertionError(f"未编排的预检命令：{command!r}")

    def font_file_is_readable(self, _path: str) -> bool:
        return True

    def tls_observation(self) -> TLSObservation:
        return TLSObservation(verification_enabled=True, ca_count=1)


class _UnavailableProbe(_CertifiedProbe):
    def platform_name(self) -> str:
        return "darwin"

    def architecture(self) -> str:
        return "arm64"

    def os_release(self) -> dict[str, str]:
        return {"ID": "not-ubuntu", "VERSION_ID": "23.10"}

    def python_implementation(self) -> str:
        return "PyPy"

    def python_version(self) -> tuple[int, int, int]:
        return (3, 13, 0)

    def is_virtual_environment(self) -> bool:
        return False

    def which(self, _command: str) -> str | None:
        return None

    def tls_observation(self) -> TLSObservation:
        return TLSObservation(verification_enabled=False, ca_count=0)


def _install_readiness_probe(monkeypatch, probe):
    original_readiness = Readiness.check

    def check(request):
        return original_readiness(request, system_probe=probe)

    monkeypatch.setattr(
        composition.Readiness,
        "check",
        staticmethod(check),
    )


def _media_source(path: Path) -> None:
    subprocess.run(
        (
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x240:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ),
        check=True,
    )


def _install_success_boundaries(
    monkeypatch,
    sequence,
    *,
    retry_once=False,
):
    original_readiness = Readiness.check

    def check(request):
        return original_readiness(request, system_probe=_CertifiedProbe())

    monkeypatch.setattr(
        composition.Readiness,
        "check",
        staticmethod(check),
    )

    response_body = (
        'data: {"type":"transcript.text.delta","delta":"课程内容",'
        '"start_time":0,"end_time":900}\n\n'
        'data: {"type":"transcript.text.done","text":"课程内容"}\n\n'
        "data: [DONE]\n\n"
    ).encode()

    send_count = 0

    def send(_self, _request, cancellation):
        nonlocal send_count
        cancellation.raise_if_cancelled()
        send_count += 1
        sequence.append("stepaudio.send")
        if retry_once and send_count == 1:
            return StepAudioTransportResponse(
                status_code=503,
                content_type="application/json",
                body=b"",
                remote_request_id="failed-request-canary-must-not-leak",
            )
        return StepAudioTransportResponse(
            status_code=200,
            content_type="text/event-stream; charset=utf-8",
            body=response_body,
            remote_request_id="remote-request-canary-must-not-leak",
        )

    monkeypatch.setattr(StdlibStepAudioTransport, "send", send)


def test_production_composition_runs_fixed_empty_pipeline_and_discloses_first(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    _media_source(source)
    source.with_suffix(".config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                "transcription_provider_config": {
                    "key_environment_variable": "COMPOSITION_TEST_API_KEY"
                },
                "text_model_provider_config": {
                    "key_environment_variable": "COMPOSITION_TEST_API_KEY"
                },
            }
        )
    )
    workspace = tmp_path / "workspace"
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    monkeypatch.setenv(
        "COMPOSITION_TEST_API_KEY",
        "credential-canary-must-not-leak",
    )
    sequence: list[str] = []
    _install_success_boundaries(monkeypatch, sequence)

    application = composition.compose_live_application(
        disclosure_sink=lambda disclosures: sequence.append(
            "disclosure:" + ",".join(item.capability.value for item in disclosures)
        )
    )
    outcome = application.execute(LiveRunRequest(source, workspace_dir=workspace))

    assert isinstance(application, LiveApplication)
    assert outcome.state is LiveRunState.SUCCEEDED
    assert outcome.result_kind is ResultKind.EMPTY
    assert sequence == [
        "disclosure:transcription,topic_review,subtitle_optimization",
        "stepaudio.send",
    ]
    manifest = json.loads(
        (workspace / "work" / "runs" / str(outcome.run_id) / "run.json").read_text()
    )
    assert manifest["external_services"]["status"] == "observed"
    services = manifest["external_services"]["services"]
    assert [item["capability"] for item in services] == [
        "transcription",
        "topic_review",
        "subtitle_optimization",
    ]
    assert services[0]["contact"]["status"] == "contacted"
    assert services[0]["requests"]["count"] == 1
    assert services[0]["requests"]["attempt_count_total"] == 1
    assert all(
        item["contact"]
        == {
            "reason": "no_work",
            "status": "not_contacted",
        }
        for item in services[1:]
    )
    assert "credential-canary-must-not-leak" not in json.dumps(
        manifest,
        ensure_ascii=False,
    )
    assert (workspace / "delivery" / "manifest.json").is_file()

    replay = application.execute(
        LiveRunRequest(source, workspace_dir=workspace, overwrite=True)
    )

    assert replay.state is LiveRunState.SUCCEEDED
    assert sequence == [
        "disclosure:transcription,topic_review,subtitle_optimization",
        "stepaudio.send",
        "disclosure:transcription,topic_review,subtitle_optimization",
    ]
    replay_manifest = json.loads(
        (workspace / "work" / "runs" / str(replay.run_id) / "run.json").read_text()
    )
    replay_services = replay_manifest["external_services"]["services"]
    assert replay_services[0]["contact"] == {
        "reason": "cache_hit",
        "status": "not_contacted",
    }
    assert all(
        service["contact"]["reason"] == "no_work" for service in replay_services[1:]
    )

    for entry in (workspace / "work" / "cache" / "transcript").rglob("*.json"):
        entry.unlink()
    shard_cache_replay = application.execute(
        LiveRunRequest(source, workspace_dir=workspace, overwrite=True)
    )

    assert shard_cache_replay.state is LiveRunState.SUCCEEDED
    assert sequence.count("stepaudio.send") == 1
    shard_cache_manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(shard_cache_replay.run_id)
            / "run.json"
        ).read_text()
    )
    shard_cache_service = shard_cache_manifest["external_services"]["services"][0]
    assert shard_cache_service["contact"] == {
        "reason": "cache_hit",
        "status": "not_contacted",
    }


def test_composition_entry_rejects_non_callable_disclosure_sink():
    with pytest.raises(TypeError):
        composition.compose_live_application(disclosure_sink=object())


def test_stepaudio_retry_is_one_correlated_request_with_two_attempts(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    _media_source(source)
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("STEPFUN_API_KEY", "credential-canary-must-not-leak")
    sequence: list[str] = []
    _install_success_boundaries(
        monkeypatch,
        sequence,
        retry_once=True,
    )

    outcome = composition.compose_live_application(
        disclosure_sink=lambda _plan: sequence.append("disclosure")
    ).execute(LiveRunRequest(source, workspace_dir=workspace))

    assert outcome.state is LiveRunState.SUCCEEDED
    assert sequence == ["disclosure", "stepaudio.send", "stepaudio.send"]
    manifest = json.loads(
        (workspace / "work" / "runs" / str(outcome.run_id) / "run.json").read_text()
    )
    transcription = manifest["external_services"]["services"][0]
    assert transcription["requests"]["count"] == 1
    assert transcription["requests"]["attempt_count_total"] == 2
    assert manifest["retries_and_recovery"]["transport_retry"] == 1


def test_disclosure_failure_stops_before_business_and_closes_provider_plan(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source must not be analyzed")
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("STEPFUN_API_KEY", "credential-canary-must-not-leak")
    _install_readiness_probe(monkeypatch, _CertifiedProbe())
    disclosures = []

    def reject(plan):
        disclosures.append(plan)
        raise RuntimeError("disclosure-sink-secret-canary")

    outcome = composition.compose_live_application(disclosure_sink=reject).execute(
        LiveRunRequest(source, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.primary_error_code is ErrorCode.INTERNAL_UNEXPECTED
    assert len(disclosures) == 1
    manifest = json.loads(
        (workspace / "work" / "runs" / str(outcome.run_id) / "run.json").read_text()
    )
    assert manifest["stages"]["source_analysis"] == {"status": "not_started"}
    assert all(
        service["contact"]
        == {
            "reason": "precondition_failed",
            "status": "not_contacted",
        }
        for service in manifest["external_services"]["services"]
    )
    assert "disclosure-sink-secret-canary" not in json.dumps(manifest)


def test_preflight_aggregates_readiness_and_publication_failures_stably(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source must not be analyzed")
    workspace = tmp_path / "workspace"
    opened = composition.Workspace.open(source, workspace)
    del opened
    (workspace / "delivery" / "existing.txt").write_bytes(b"must stay")
    monkeypatch.delenv("STEPFUN_API_KEY", raising=False)
    _install_readiness_probe(monkeypatch, _UnavailableProbe())
    disclosure_calls = []

    outcome = composition.compose_live_application(
        disclosure_sink=disclosure_calls.append
    ).execute(LiveRunRequest(source, workspace_dir=workspace))

    assert outcome.state is LiveRunState.FAILED
    assert disclosure_calls == []
    errors = (outcome.primary_error, *outcome.associated_errors)
    assert all(error is not None for error in errors)
    codes = [error.error_code for error in errors if error is not None]
    order = {code: index for index, code in enumerate(ERROR_REGISTRY)}
    assert codes == sorted(codes, key=order.__getitem__)
    assert ErrorCode.PUBLICATION_COMMIT_FAILED in codes
    assert len(codes) > 2
    assert (workspace / "delivery" / "existing.txt").read_bytes() == b"must stay"

    manifest = json.loads(
        (workspace / "work" / "runs" / str(outcome.run_id) / "run.json").read_text()
    )
    assert manifest["stages"]["preflight"]["status"] == "failed"
    assert manifest["stages"]["source_analysis"] == {"status": "not_started"}
    assert all(
        service["contact"]["reason"] == "precondition_failed"
        for service in manifest["external_services"]["services"]
    )


def test_local_smoke_temp_failure_keeps_preflight_terminal_and_zero_requests(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source must not be analyzed")
    workspace = tmp_path / "workspace"
    opened = composition.Workspace.open(source, workspace)
    del opened
    existing_delivery = workspace / "delivery" / "existing.txt"
    existing_delivery.write_bytes(b"must stay")
    monkeypatch.setenv("STEPFUN_API_KEY", "credential-canary-must-not-leak")
    _install_readiness_probe(monkeypatch, _CertifiedProbe())

    def fail_temporary_directory(*_args, **_kwargs):
        raise OSError("temporary-directory-secret-canary")

    monkeypatch.setattr(
        tempfile,
        "TemporaryDirectory",
        fail_temporary_directory,
    )
    disclosures = []

    outcome = composition.compose_live_application(
        disclosure_sink=disclosures.append
    ).execute(
        LiveRunRequest(source, workspace_dir=workspace, overwrite=True)
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.PREFLIGHT_FAILED
    assert outcome.primary_error_code is ErrorCode.ENVIRONMENT_FFMPEG_UNAVAILABLE
    assert disclosures == []
    assert existing_delivery.read_bytes() == b"must stay"
    manifest = json.loads(
        (workspace / "work" / "runs" / str(outcome.run_id) / "run.json").read_text()
    )
    assert manifest["stages"]["preflight"]["status"] == "failed"
    assert manifest["stages"]["source_analysis"] == {"status": "not_started"}
    assert all(
        service["contact"]
        == {
            "reason": "precondition_failed",
            "status": "not_contacted",
        }
        and service["requests"]["count"] == 0
        for service in manifest["external_services"]["services"]
    )
    assert "temporary-directory-secret-canary" not in json.dumps(manifest)


def test_production_composition_rejects_deterministic_adapter_via_execute(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"configuration is loaded before media analysis")
    source.with_suffix(".config.json").write_text(
        json.dumps(
            {
                "schema_version": "configuration.v1",
                "transcription_provider": "deterministic",
            }
        )
    )

    outcome = composition.compose_live_application().execute(
        LiveRunRequest(source, workspace_dir=tmp_path / "workspace")
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.primary_error_code is ErrorCode.CONFIG_VALUE_INVALID
