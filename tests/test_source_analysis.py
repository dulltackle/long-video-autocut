import base64
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
from dataclasses import FrozenInstanceError
from threading import Thread
from time import monotonic, sleep

import pytest

from tests.support.deterministic_composition import (
    compose_deterministic_live_application,
)
from video_auto_editor.application import LiveRunRequest, LiveRunState
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode, ExitCode, RunStage
from video_auto_editor.source_analysis import (
    SourceAnalysis,
    SourceAnalysisFailure,
    SourceDescription,
)
from video_auto_editor.workspace import Workspace


def _write_mp4(path, *, duration_seconds="0.120"):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=32x32:r=25",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=mono",
            "-t",
            duration_seconds,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-y",
            str(path),
        ],
        check=True,
    )


def _install_fake_ffprobe(
    tmp_path,
    monkeypatch,
    *,
    stdout=b"",
    stderr=b"",
    exit_code=0,
):
    executable_directory = tmp_path / "fake-bin"
    executable_directory.mkdir(exist_ok=True)
    executable = executable_directory / "ffprobe"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import base64",
                "import sys",
                (
                    "sys.stdout.buffer.write(base64.b64decode("
                    f"{base64.b64encode(stdout)!r}))"
                ),
                (
                    "sys.stderr.buffer.write(base64.b64decode("
                    f"{base64.b64encode(stderr)!r}))"
                ),
                f"raise SystemExit({exit_code})",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )


def test_valid_mp4_forms_one_immutable_source_description(tmp_path):
    source_path = tmp_path / "course.mp4"
    _write_mp4(source_path)
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    contents = source_path.read_bytes()

    description = SourceAnalysis.analyze(
        source,
        CancellationSource().token,
    )

    assert description.source_file is source
    assert description.source_file.path == source_path.resolve()
    assert description.sha256 == (
        f"sha256:{hashlib.sha256(contents).hexdigest()}"
    )
    assert description.byte_length == len(contents)
    assert description.duration_ms == 120
    assert not hasattr(description, "__dict__")
    assert str(source_path.resolve()) not in repr(description)
    with pytest.raises(FrozenInstanceError):
        description.duration_ms = 121
    with pytest.raises(TypeError):
        SourceDescription(
            source_file=source,
            sha256=description.sha256,
            byte_length=description.byte_length,
            duration_ms=description.duration_ms,
        )


def test_canonical_source_target_must_have_an_mp4_extension(tmp_path):
    source_target = tmp_path / "course.mov"
    _write_mp4(source_target)
    source_link = tmp_path / "linked-course.mp4"
    source_link.symlink_to(source_target)
    source = Workspace.open(
        source_link,
        tmp_path / "workspace",
    ).source

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_UNSUPPORTED
    assert captured.value.diagnostics == {
        "reason_code": "input.extension_unsupported"
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert str(source_target.resolve()) not in str(captured.value)
    assert str(source_target.resolve()) not in repr(captured.value)


def test_non_mp4_live_input_uses_a_separate_default_audit_workspace(tmp_path):
    source_path = tmp_path / "course.autocut"
    source_contents = b"not an mp4"
    source_path.write_bytes(source_contents)
    application = compose_deterministic_live_application(
        source_analyzer=SourceAnalysis.analyze,
    )

    outcome = application.execute(LiveRunRequest(source_path))

    workspace = tmp_path / "course.autocut.autocut"
    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_UNSUPPORTED
    assert outcome.diagnostics_incomplete is False
    assert source_path.read_bytes() == source_contents
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text(encoding="utf-8")
    )
    assert manifest["lifecycle"]["exit_code"] == 20
    assert manifest["stages"]["source_analysis"]["status"] == "failed"


def test_empty_mp4_fails_before_media_probe(tmp_path, monkeypatch):
    source_path = tmp_path / "empty.mp4"
    source_path.write_bytes(b"")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source

    def reject_probe(*args, **kwargs):
        del args, kwargs
        raise AssertionError("空素材不得启动媒体探测")

    monkeypatch.setattr(subprocess, "Popen", reject_probe)

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_MEDIA_INVALID
    assert captured.value.diagnostics == {"reason_code": "media.empty"}


def test_workspace_signed_source_cannot_be_replaced_before_analysis(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"original media identity")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    source_path.rename(tmp_path / "original.mp4")
    source_path.write_bytes(b"replacement media identity")

    def reject_probe(*args, **kwargs):
        del args, kwargs
        raise AssertionError("被替换的素材不得启动媒体探测")

    monkeypatch.setattr(subprocess, "Popen", reject_probe)

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {
        "reason_code": "input.read_failed"
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


def test_workspace_signed_source_cannot_change_before_analysis(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"original media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    source_path.write_bytes(b"changed media with the same inode")

    def reject_probe(*args, **kwargs):
        del args, kwargs
        raise AssertionError("签发后变化的素材不得启动媒体探测")

    monkeypatch.setattr(subprocess, "Popen", reject_probe)

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {
        "reason_code": "input.read_failed"
    }


def test_source_changed_during_probe_cannot_form_a_mixed_description(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"original media bytes")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    marker = tmp_path / "probe-started"
    probe_payload = json.dumps(
        {
            "format": {"format_name": "mp4", "duration": "1.000"},
            "streams": [
                {"codec_type": "video"},
                {"codec_type": "audio"},
            ],
        }
    ).encode("utf-8")
    executable_directory = tmp_path / "fake-bin"
    executable_directory.mkdir()
    executable = executable_directory / "ffprobe"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import base64",
                "from pathlib import Path",
                "from time import sleep",
                f"Path({str(marker)!r}).write_bytes(b'started')",
                "sleep(0.2)",
                (
                    "print(base64.b64decode("
                    f"{base64.b64encode(probe_payload)!r}).decode('utf-8'))"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )

    def mutate_after_probe_starts():
        while not marker.exists():
            sleep(0.01)
        source_path.write_bytes(b"changed while probing")

    mutator = Thread(target=mutate_after_probe_starts, daemon=True)
    mutator.start()

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    mutator.join(timeout=1)
    assert captured.value.error_code is ErrorCode.INPUT_UNREADABLE
    assert captured.value.diagnostics == {
        "reason_code": "input.read_failed"
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None


@pytest.mark.parametrize(
    ("probe_payload", "error_code", "diagnostics"),
    [
        (
            {
                "format": {
                    "format_name": "matroska,webm",
                    "duration": "1.000",
                },
                "streams": [
                    {"codec_type": "video"},
                    {"codec_type": "audio"},
                ],
            },
            ErrorCode.INPUT_UNSUPPORTED,
            {"reason_code": "input.container_unsupported"},
        ),
        (
            {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "1.000",
                },
                "streams": [{"codec_type": "audio"}],
            },
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            {
                "reason_code": "media.stream_missing",
                "stream_type": "video",
            },
        ),
        (
            {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "1.000",
                },
                "streams": [
                    {"codec_type": []},
                    {"codec_type": "audio"},
                ],
            },
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            {
                "reason_code": "media.stream_missing",
                "stream_type": "video",
            },
        ),
        (
            {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "1.000",
                },
                "streams": [{"codec_type": "video"}],
            },
            ErrorCode.INPUT_REQUIRED_STREAM_MISSING,
            {
                "reason_code": "media.stream_missing",
                "stream_type": "audio",
            },
        ),
        (
            {
                "format": {
                    "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
                    "duration": "0.0004",
                },
                "streams": [
                    {"codec_type": "video"},
                    {"codec_type": "audio"},
                ],
            },
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.duration_invalid"},
        ),
        (
            {"format": {}, "streams": []},
            ErrorCode.INPUT_MEDIA_INVALID,
            {"reason_code": "media.container_invalid"},
        ),
    ],
)
def test_probe_facts_map_to_stable_input_failures(
    tmp_path,
    monkeypatch,
    probe_payload,
    error_code,
    diagnostics,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"not sent outside this process")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    _install_fake_ffprobe(
        tmp_path,
        monkeypatch,
        stdout=json.dumps(probe_payload).encode("utf-8"),
    )

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is error_code
    assert captured.value.diagnostics == diagnostics


def test_probe_failure_keeps_only_stderr_length_and_digest(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "private-course.mp4"
    source_path.write_bytes(b"invalid media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    raw_stderr = (
        b"/private/customer/course.mp4 "
        b"Authorization: Bearer provider-secret"
    )
    _install_fake_ffprobe(
        tmp_path,
        monkeypatch,
        stderr=raw_stderr,
        exit_code=7,
    )

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_MEDIA_INVALID
    assert captured.value.diagnostics == {
        "reason_code": "media.probe_failed",
        "media_exit_code": 7,
        "stderr_length": len(raw_stderr),
        "stderr_sha256": hashlib.sha256(raw_stderr).hexdigest(),
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert raw_stderr.decode("utf-8") not in str(captured.value)
    assert raw_stderr.decode("utf-8") not in repr(captured.value)


def test_probe_spawn_failure_is_a_safe_local_processing_failure(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "private-course.mp4"
    source_path.write_bytes(b"media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source

    def fail_spawn(*args, **kwargs):
        del args, kwargs
        raise OSError("/private/customer/course.mp4 could not execute")

    monkeypatch.setattr(subprocess, "Popen", fail_spawn)

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.MEDIA_PROCESSING_FAILED
    assert captured.value.diagnostics == {
        "operation": "ffprobe.probe",
        "reason_code": "media.spawn_failed",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "/private/customer/course.mp4" not in str(captured.value)


def test_probe_killed_by_a_signal_is_not_blame_assigned_to_the_input(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    raw_stderr = b"/private/path from terminated probe"
    executable_directory = tmp_path / "fake-bin"
    executable_directory.mkdir()
    executable = executable_directory / "ffprobe"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import os",
                "import signal",
                "import sys",
                f"sys.stderr.buffer.write({raw_stderr!r})",
                "sys.stderr.buffer.flush()",
                "os.kill(os.getpid(), signal.SIGKILL)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.MEDIA_PROCESSING_FAILED
    assert captured.value.diagnostics == {
        "operation": "ffprobe.probe",
        "reason_code": "media.process_failed",
        "media_exit_code": -signal.SIGKILL,
        "stderr_length": len(raw_stderr),
        "stderr_sha256": hashlib.sha256(raw_stderr).hexdigest(),
    }
    assert raw_stderr.decode("utf-8") not in str(captured.value)


def test_cancellation_stops_an_inflight_media_probe(tmp_path, monkeypatch):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    executable_directory = tmp_path / "fake-bin"
    executable_directory.mkdir()
    marker = tmp_path / "probe-started"
    descendant_marker = tmp_path / "probe-descendant-survived"
    descendant_code = "\n".join(
        [
            "from pathlib import Path",
            "from time import sleep",
            "sleep(30)",
            f"Path({str(descendant_marker)!r}).write_bytes(b'survived')",
        ]
    )
    executable = executable_directory / "ffprobe"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import subprocess",
                "import sys",
                "from pathlib import Path",
                "from time import sleep",
                (
                    "descendant = subprocess.Popen("
                    f"[sys.executable, '-c', {descendant_code!r}])"
                ),
                f"Path({str(marker)!r}).write_text(str(descendant.pid))",
                "sleep(30)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv(
        "PATH",
        f"{executable_directory}{os.pathsep}{os.environ['PATH']}",
    )
    cancellation = CancellationSource()

    def request_after_probe_starts():
        while not marker.exists():
            sleep(0.01)
        cancellation.request(signal.SIGINT)

    requester = Thread(target=request_after_probe_starts, daemon=True)
    requester.start()
    started_at = monotonic()

    with pytest.raises(CancellationRequested):
        SourceAnalysis.analyze(source, cancellation.token)

    requester.join(timeout=1)
    assert monotonic() - started_at < 2
    descendant_pid = int(marker.read_text(encoding="utf-8"))
    reap_deadline = monotonic() + 2
    descendant_alive = True
    while monotonic() < reap_deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            descendant_alive = False
            break
        sleep(0.05)
    assert descendant_alive is False
    assert not descendant_marker.exists()


@pytest.mark.parametrize(
    "raw_stdout",
    [
        b'{"private_path":"/customer/course.mp4"',
        b"[]",
        (
            b'{"format":{"format_name":"mp4","format_name":"matroska",'
            b'"duration":"1"},"streams":[]}'
        ),
        b"\xff\xfe",
        (
            b'{"format":{"format_name":"mp4","duration":NaN},'
            b'"streams":[{"codec_type":"video"},{"codec_type":"audio"}]}'
        ),
    ],
)
def test_malformed_probe_output_becomes_one_safe_container_failure(
    tmp_path,
    monkeypatch,
    raw_stdout,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"media")
    source = Workspace.open(
        source_path,
        tmp_path / "workspace",
    ).source
    _install_fake_ffprobe(
        tmp_path,
        monkeypatch,
        stdout=raw_stdout,
    )

    with pytest.raises(SourceAnalysisFailure) as captured:
        SourceAnalysis.analyze(
            source,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.INPUT_MEDIA_INVALID
    assert captured.value.diagnostics == {
        "reason_code": "media.container_invalid"
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert "/customer/course.mp4" not in str(captured.value)
    assert "/customer/course.mp4" not in repr(captured.value)


def test_live_run_persists_the_verified_source_description(tmp_path, monkeypatch):
    source_path = tmp_path / "course.mp4"
    source_contents = b"single source contents"
    source_path.write_bytes(source_contents)
    context_payload = {
        "schema_version": "course_context.v1",
        "course_topic": "素材审计",
        "priority_topics": ["内容摘要"],
    }
    (tmp_path / "course.context.json").write_text(
        json.dumps(context_payload, ensure_ascii=False),
        encoding="utf-8",
    )
    probe_payload = {
        "format": {
            "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
            "duration": "1.2345",
        },
        "streams": [
            {"codec_type": "video"},
            {"codec_type": "audio"},
        ],
    }
    _install_fake_ffprobe(
        tmp_path,
        monkeypatch,
        stdout=json.dumps(probe_payload).encode("utf-8"),
    )
    workspace = tmp_path / "workspace"
    application = compose_deterministic_live_application(
        source_analyzer=SourceAnalysis.analyze,
    )

    outcome = application.execute(
        LiveRunRequest(source_path, workspace_dir=workspace)
    )

    assert outcome.state is LiveRunState.SUCCEEDED
    manifest = json.loads(
        (
            workspace
            / "work"
            / "runs"
            / str(outcome.run_id)
            / "run.json"
        ).read_text(encoding="utf-8")
    )
    source_fact = manifest["source"]
    assert source_fact["status"] == "available"
    assert source_fact["sha256"] == (
        f"sha256:{hashlib.sha256(source_contents).hexdigest()}"
    )
    assert source_fact["byte_length"] == len(source_contents)
    assert source_fact["duration_ms"] == 1_235
    assert source_fact["course_context"]["provided"] is True
    assert re.fullmatch(
        r"sha256:[0-9a-f]{64}",
        source_fact["course_context"]["sha256"]["value"],
    )
    events = (
        workspace
        / "work"
        / "runs"
        / str(outcome.run_id)
        / "events.jsonl"
    ).read_bytes()
    assert source_fact["sha256"].encode("utf-8") not in events
    assert b"source.observed" in events


def test_invalid_source_fails_audibly_before_every_remote_request(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "private-course.mp4"
    source_path.write_bytes(b"invalid media")
    workspace = tmp_path / "workspace"
    Workspace.open(source_path, workspace)
    delivery_sentinel = workspace / "delivery" / "existing.txt"
    delivery_sentinel.write_text("keep", encoding="utf-8")
    raw_stderr = b"/private/customer/course.mp4 bearer-provider-secret"
    _install_fake_ffprobe(
        tmp_path,
        monkeypatch,
        stderr=raw_stderr,
        exit_code=9,
    )
    remote_request_count = 0

    def reject_remote_connection(*args, **kwargs):
        nonlocal remote_request_count
        del args, kwargs
        remote_request_count += 1
        raise AssertionError("无效素材不得建立远程连接")

    monkeypatch.setattr(socket.socket, "connect", reject_remote_connection)
    application = compose_deterministic_live_application(
        source_analyzer=SourceAnalysis.analyze,
        unexpected_stage=RunStage.TRANSCRIPTION,
    )

    outcome = application.execute(
        LiveRunRequest(
            source_path,
            workspace_dir=workspace,
            overwrite=True,
        )
    )

    assert outcome.state is LiveRunState.FAILED
    assert outcome.exit_code is ExitCode.INPUT_FAILED
    assert outcome.primary_error_code is ErrorCode.INPUT_MEDIA_INVALID
    assert outcome.primary_error is not None
    assert outcome.primary_error.diagnostics == {
        "media_exit_code": 9,
        "reason_code": "media.probe_failed",
        "stderr_length": len(raw_stderr),
        "stderr_sha256": hashlib.sha256(raw_stderr).hexdigest(),
    }
    assert delivery_sentinel.read_text(encoding="utf-8") == "keep"
    run_directory = (
        workspace / "work" / "runs" / str(outcome.run_id)
    )
    manifest_bytes = (run_directory / "run.json").read_bytes()
    events_bytes = (run_directory / "events.jsonl").read_bytes()
    manifest = json.loads(manifest_bytes)
    assert manifest["external_services"] == {"status": "not_observed"}
    assert manifest["stages"]["source_analysis"]["status"] == "failed"
    assert remote_request_count == 0
    for stage in (
        "transcription",
        "candidate_planning",
        "topic_review",
        "delivery_build",
        "delivery_verification",
        "publishing",
    ):
        assert manifest["stages"][stage] == {"status": "not_started"}
    assert b"external_request.completed" not in events_bytes
    assert raw_stderr not in manifest_bytes
    assert raw_stderr not in events_bytes
