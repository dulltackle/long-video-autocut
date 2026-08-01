import hashlib
import json
import os
import signal
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timezone
from threading import Thread
from time import monotonic, sleep

import pytest

from video_auto_editor.clip_planning import (
    BoundaryRemedy,
    BoundaryRemedyStatus,
    DeliveryPlan,
    FinalCandidate,
    PublishedSelection,
    RejectedSelection,
    RejectionReason,
    ResultKind,
    ReviewRecommendation,
    ShortVideo,
    TopicReviewSnapshot,
)
from video_auto_editor.configuration import Configuration
from video_auto_editor.delivery.build import (
    DeliveryBuild,
    DeliveryBuildFailure,
    DeliveryBuildRequest,
)
from video_auto_editor.delivery.capability import UnverifiedDelivery
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import (
    CandidateId,
    PlanId,
    RunId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceAnalysis, SourceDescription
from video_auto_editor.subtitle_optimization import (
    OptimizedShortVideoSubtitles,
    SubtitleDisplayBlock,
    SubtitleOptimizationExecutionFacts,
    SubtitleOptimizationResult,
)
from video_auto_editor.transcription import (
    CompleteTranscript,
    SpeechPresence,
    TranscriptChunk,
    TranscriptionChunk,
)
from video_auto_editor.workspace import ManagedBinaryFile, Workspace


def _empty_delivery_facts(tmp_path):
    source_path = tmp_path / "course.mp4"
    source_contents = b"source used only by the empty delivery"
    source_path.write_bytes(source_contents)
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + hashlib.sha256(source_contents).hexdigest(),
        byte_length=len(source_contents),
        duration_ms=300_000,
    )
    transcript_id = TranscriptId.new()
    transcript_chunk_id = TranscriptChunkId.new()
    transcript = CompleteTranscript._from_application(
        transcript_id=transcript_id,
        speech_presence=SpeechPresence.PRESENT,
        chunks=(
            TranscriptChunk._from_application(
                transcript_chunk_id,
                TranscriptionChunk(
                    start_ms=0,
                    end_ms=2_000,
                    text="忠实原文，保留语气词嗯。",
                ),
            ),
        ),
    )
    candidate_id = CandidateId.new()
    candidate = FinalCandidate(
        candidate_id=candidate_id,
        initial_start_ms=0,
        initial_end_ms=2_000,
        final_start_ms=0,
        final_end_ms=2_000,
        transcript_chunk_ids=(transcript_chunk_id,),
        boundary_remedy=BoundaryRemedy(
            status=BoundaryRemedyStatus.NOT_NEEDED,
            suggestion="",
            requested_start_ms=None,
            requested_end_ms=None,
        ),
        review=TopicReviewSnapshot(
            candidate_id=candidate_id,
            topic_name="上下文不足的候选",
            topic_complete=False,
            learning_value=6,
            share_value=5,
            publish_ready_score=62,
            export_decision=ReviewRecommendation.REJECT,
            title="不能独立发布的候选",
            summary="完整评审后确认缺少独立结论。",
            keywords=("评审",),
            needs_human_review=False,
            reject_reason="缺少独立结论",
            boundary_fix_suggestion="",
            boundary_fix_start_ms=None,
            boundary_fix_end_ms=None,
        ),
        selection=RejectedSelection(
            reason_code=RejectionReason.REVIEW_REJECTED,
            needs_human_review=False,
            human_review_reason="",
        ),
    )
    plan = DeliveryPlan._from_finalization(
        plan_id=PlanId.new(),
        transcript_id=transcript_id,
        source_duration_ms=source.duration_ms,
        result_kind=ResultKind.EMPTY,
        candidates=(candidate,),
        short_videos=(),
        series=(),
    )
    subtitles = SubtitleOptimizationResult(
        short_videos=(),
        execution_facts=SubtitleOptimizationExecutionFacts(
            short_video_count=0,
            window_count=0,
            model_request_count=0,
            cache_hit_count=0,
            cache_miss_count=0,
        ),
    )
    return workspace, source, transcript, plan, subtitles


def _write_real_source(path):
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000",
            "-t",
            "3",
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


def _clip_delivery_facts(tmp_path):
    source_path = tmp_path / "course.mp4"
    _write_real_source(source_path)
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    cancellation = CancellationSource().token
    source = SourceAnalysis.analyze(workspace.source, cancellation)
    transcript_id = TranscriptId.new()
    transcript_chunk_id = TranscriptChunkId.new()
    transcript = CompleteTranscript._from_application(
        transcript_id=transcript_id,
        speech_presence=SpeechPresence.PRESENT,
        chunks=(
            TranscriptChunk._from_application(
                transcript_chunk_id,
                TranscriptionChunk(
                    start_ms=0,
                    end_ms=2_800,
                    text=(
                        "这是一段忠实原文，"
                        "用于验证优化字幕已经烧录。"
                    ),
                ),
            ),
        ),
    )
    candidate_id = CandidateId.new()
    short_video_id = ShortVideoId.new()
    review = TopicReviewSnapshot(
        candidate_id=candidate_id,
        topic_name="标准交付构建",
        topic_complete=True,
        learning_value=9,
        share_value=8,
        publish_ready_score=92,
        export_decision=ReviewRecommendation.PUBLISH_READY,
        title="如何形成可审计的标准交付",
        summary="固定业务文档与烧录字幕视频一起进入受管暂存。",
        keywords=("标准交付", "审计"),
        needs_human_review=False,
        reject_reason="",
        boundary_fix_suggestion="",
        boundary_fix_start_ms=None,
        boundary_fix_end_ms=None,
    )
    candidate = FinalCandidate(
        candidate_id=candidate_id,
        initial_start_ms=400,
        initial_end_ms=2_400,
        final_start_ms=400,
        final_end_ms=2_400,
        transcript_chunk_ids=(transcript_chunk_id,),
        boundary_remedy=BoundaryRemedy(
            status=BoundaryRemedyStatus.NOT_NEEDED,
            suggestion="",
            requested_start_ms=None,
            requested_end_ms=None,
        ),
        review=review,
        selection=PublishedSelection(short_video_id=short_video_id),
    )
    short_video = ShortVideo(
        short_video_id=short_video_id,
        source_candidate_id=candidate_id,
        topic_name=review.topic_name,
        title=review.title,
        summary=review.summary,
        keywords=review.keywords,
        final_start_ms=candidate.final_start_ms,
        final_end_ms=candidate.final_end_ms,
    )
    plan = DeliveryPlan._from_finalization(
        plan_id=PlanId.new(),
        transcript_id=transcript_id,
        source_duration_ms=source.duration_ms,
        result_kind=ResultKind.CLIPS,
        candidates=(candidate,),
        short_videos=(short_video,),
        series=(),
    )
    subtitles = SubtitleOptimizationResult(
        short_videos=(
            OptimizedShortVideoSubtitles(
                short_video_id=short_video_id,
                display_blocks=(
                    SubtitleDisplayBlock(
                        start_ms=600,
                        end_ms=1_800,
                        text="已经优化并烧录的字幕",
                    ),
                ),
            ),
        ),
        execution_facts=SubtitleOptimizationExecutionFacts(
            short_video_count=1,
            window_count=1,
            model_request_count=1,
            cache_miss_count=1,
            transport_attempt_count=1,
        ),
    )
    return (
        workspace,
        source,
        transcript,
        plan,
        subtitles,
        short_video_id,
    )


def test_build_effective_empty_delivery_uses_the_fixed_staging_package(
    tmp_path,
):
    workspace, source, transcript, plan, subtitles = _empty_delivery_facts(
        tmp_path
    )
    existing_delivery = workspace.root / "delivery" / "existing.txt"
    existing_delivery.write_bytes(b"existing delivery")
    run_id = RunId.new()
    started_at = datetime(2026, 7, 31, 12, 0, tzinfo=timezone.utc)
    published_at = datetime(2026, 7, 31, 12, 1, tzinfo=timezone.utc)
    configuration = Configuration.load(source.source_file.path)

    with workspace.acquire_run(run_id) as run_workspace:
        delivery = DeliveryBuild.build(
            DeliveryBuildRequest(
                run_id=run_id,
                source=source,
                transcript=transcript,
                plan=plan,
                subtitles=subtitles,
                staging_directory=run_workspace.delivery_staging,
                subtitle_style=configuration.effective.subtitle_style,
                application_version="4.7.0",
                started_at=started_at,
                published_at=published_at,
                cancellation=CancellationSource().token,
            )
        )

        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        assert isinstance(delivery, UnverifiedDelivery)
        assert delivery.run_id == run_id
        assert delivery.managed_directory is run_workspace.delivery_staging
        assert sorted(
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
        ) == [
            "clips",
            "manifest.json",
            "metadata.json",
            "plan.json",
            "report.md",
            "transcript.json",
            "transcript.srt",
        ]
        assert list((staging / "clips").iterdir()) == []
        assert not (staging / "subtitles").exists()
        assert existing_delivery.read_bytes() == b"existing delivery"
        assert list((workspace.root / "delivery").iterdir()) == [
            existing_delivery
        ]

        manifest = json.loads(
            (staging / "manifest.json").read_text(encoding="utf-8")
        )
        transcript_document = json.loads(
            (staging / "transcript.json").read_text(encoding="utf-8")
        )
        plan_document = json.loads(
            (staging / "plan.json").read_text(encoding="utf-8")
        )
        metadata = json.loads(
            (staging / "metadata.json").read_text(encoding="utf-8")
        )

        assert manifest["schema_version"] == "delivery_manifest.v1"
        assert transcript_document["schema_version"] == "transcript.v1"
        assert plan_document["schema_version"] == "clip_plan.v1"
        assert metadata["schema_version"] == "short_video_catalog.v1"
        assert set(manifest) == {
            "application_version",
            "documents",
            "execution",
            "files",
            "published_at",
            "result_kind",
            "run_id",
            "schema_version",
            "source",
            "started_at",
            "terminal_state",
        }
        assert set(transcript_document) == {
            "chunks",
            "run_id",
            "schema_version",
            "source_duration_ms",
            "speech_presence",
            "transcript_id",
        }
        assert set(plan_document) == {
            "candidate_count",
            "candidates",
            "plan_id",
            "published_count",
            "result_kind",
            "run_id",
            "schema_version",
            "transcript_id",
        }
        assert set(metadata) == {
            "result_kind",
            "run_id",
            "schema_version",
            "series",
            "short_videos",
        }
        assert set(transcript_document["chunks"][0]) == {
            "end_ms",
            "start_ms",
            "text",
            "transcript_chunk_id",
        }
        assert {
            manifest["run_id"],
            transcript_document["run_id"],
            plan_document["run_id"],
            metadata["run_id"],
        } == {str(run_id)}
        assert manifest["terminal_state"] == "succeeded"
        assert manifest["result_kind"] == "empty"
        assert plan_document["result_kind"] == "empty"
        assert metadata["result_kind"] == "empty"
        assert manifest["documents"]["transcript"] == {
            "path": "transcript.json",
            "transcript_id": str(transcript.transcript_id),
        }
        assert manifest["documents"]["transcript_rendering"] == {
            "path": "transcript.srt",
            "transcript_id": str(transcript.transcript_id),
        }
        assert set(manifest["documents"]) == {
            "metadata",
            "plan",
            "report",
            "transcript",
            "transcript_rendering",
        }
        assert plan_document["candidate_count"] == 1
        assert plan_document["published_count"] == 0
        candidate_document = plan_document["candidates"][0]
        assert set(candidate_document) == {
            "boundary_remedy",
            "candidate_id",
            "final_range",
            "initial_range",
            "review",
            "selection",
            "transcript_chunk_ids",
        }
        assert set(candidate_document["boundary_remedy"]) == {
            "requested_end_ms",
            "requested_start_ms",
            "status",
            "suggestion",
        }
        assert set(candidate_document["review"]) == {
            "boundary_fix_end_ms",
            "boundary_fix_start_ms",
            "boundary_fix_suggestion",
            "export_decision",
            "keywords",
            "learning_value",
            "needs_human_review",
            "publish_ready_score",
            "reject_reason",
            "share_value",
            "summary",
            "title",
            "topic_complete",
            "topic_name",
        }
        assert set(candidate_document["selection"]) == {
            "human_review_reason",
            "needs_human_review",
            "outcome",
            "reason_code",
        }
        assert candidate_document["selection"]["outcome"] == "rejected"
        assert metadata["short_videos"] == []
        assert metadata["series"] == []
        assert (
            staging / "transcript.srt"
        ).read_text(encoding="utf-8") == (
            "1\n"
            "00:00:00,000 --> 00:00:02,000\n"
            "忠实原文，保留语气词嗯。\n\n"
        )
        assert "有效空结果" in (
            staging / "report.md"
        ).read_text(encoding="utf-8")

        listed_paths = [item["path"] for item in manifest["files"]]
        assert listed_paths == sorted(listed_paths)
        assert listed_paths == [
            "metadata.json",
            "plan.json",
            "report.md",
            "transcript.json",
            "transcript.srt",
        ]
        for item in manifest["files"]:
            assert set(item) == {
                "byte_length",
                "media_type",
                "path",
                "role",
                "sha256",
            }
            contents = (staging / item["path"]).read_bytes()
            assert item["byte_length"] == len(contents)
            assert item["sha256"] == (
                "sha256:" + hashlib.sha256(contents).hexdigest()
            )


def test_build_clip_delivery_burns_optimized_subtitles_into_real_media(
    tmp_path,
):
    (
        workspace,
        source,
        transcript,
        plan,
        subtitles,
        short_video_id,
    ) = _clip_delivery_facts(tmp_path)
    run_id = RunId.new()
    configuration = Configuration.load(source.source_file.path)

    with workspace.acquire_run(run_id) as run_workspace:
        DeliveryBuild.build(
            DeliveryBuildRequest(
                run_id=run_id,
                source=source,
                transcript=transcript,
                plan=plan,
                subtitles=subtitles,
                staging_directory=run_workspace.delivery_staging,
                subtitle_style=configuration.effective.subtitle_style,
                application_version="4.7.0",
                started_at=datetime(
                    2026,
                    7,
                    31,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
                published_at=datetime(
                    2026,
                    7,
                    31,
                    12,
                    1,
                    tzinfo=timezone.utc,
                ),
                cancellation=CancellationSource().token,
            )
        )

        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        media_relative_path = f"clips/{short_video_id}.mp4"
        media_path = staging / media_relative_path
        assert media_path.is_file()
        assert media_path.stat().st_size > 0
        assert sorted(
            path.relative_to(staging).as_posix()
            for path in staging.rglob("*")
            if path.is_file()
        ) == [
            media_relative_path,
            "manifest.json",
            "metadata.json",
            "plan.json",
            "report.md",
            "transcript.json",
            "transcript.srt",
        ]
        assert list(staging.rglob("*.srt")) == [
            staging / "transcript.srt"
        ]

        metadata = json.loads(
            (staging / "metadata.json").read_text(encoding="utf-8")
        )
        plan_document = json.loads(
            (staging / "plan.json").read_text(encoding="utf-8")
        )
        transcript_document = json.loads(
            (staging / "transcript.json").read_text(encoding="utf-8")
        )
        item = metadata["short_videos"][0]
        assert set(item) == {
            "duration_ms",
            "end_ms",
            "keywords",
            "media",
            "short_video_id",
            "source_candidate_id",
            "start_ms",
            "subtitles",
            "summary",
            "title",
            "topic_name",
        }
        assert set(item["media"]) == {
            "audio_required",
            "container",
            "path",
            "video_required",
        }
        assert item["short_video_id"] == str(short_video_id)
        assert item["source_candidate_id"] == str(
            plan.short_videos[0].source_candidate_id
        )
        assert item["media"]["path"] == media_relative_path
        assert item["subtitles"] == {"kind": "burned_in"}

        manifest = json.loads(
            (staging / "manifest.json").read_text(encoding="utf-8")
        )
        assert {
            manifest["run_id"],
            transcript_document["run_id"],
            plan_document["run_id"],
            metadata["run_id"],
        } == {str(run_id)}
        planned_candidate = plan_document["candidates"][0]
        assert planned_candidate["candidate_id"] == item[
            "source_candidate_id"
        ]
        assert planned_candidate["selection"] == {
            "outcome": "published",
            "short_video_id": item["short_video_id"],
        }
        assert planned_candidate["transcript_chunk_ids"] == [
            transcript_document["chunks"][0]["transcript_chunk_id"]
        ]
        assert item["short_video_id"].startswith("short_video_")
        assert item["source_candidate_id"].startswith("candidate_")
        media_file = next(
            item
            for item in manifest["files"]
            if item["path"] == media_relative_path
        )
        media_contents = media_path.read_bytes()
        assert media_file["role"] == "short_video_media"
        assert media_file["media_type"] == "video/mp4"
        assert media_file["byte_length"] == len(media_contents)
        assert media_file["sha256"] == (
            "sha256:" + hashlib.sha256(media_contents).hexdigest()
        )

        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(media_path),
            ],
            check=True,
            capture_output=True,
        )
        streams = {
            stream["codec_type"]
            for stream in json.loads(probe.stdout)["streams"]
        }
        assert streams == {"video", "audio"}

        frame = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-ss",
                "0.3",
                "-i",
                str(media_path),
                "-frames:v",
                "1",
                "-vf",
                "format=gray",
                "-f",
                "rawvideo",
                "pipe:1",
            ],
            check=True,
            capture_output=True,
        ).stdout
        assert frame
        assert max(frame) > 40
        assert sum(value > 40 for value in frame) > 20


def test_media_build_failure_never_exposes_a_new_standard_delivery(
    tmp_path,
    monkeypatch,
):
    (
        workspace,
        source,
        transcript,
        plan,
        subtitles,
        _short_video_id,
    ) = _clip_delivery_facts(tmp_path)
    existing_delivery = workspace.root / "delivery" / "existing.txt"
    existing_delivery.write_bytes(b"existing delivery")
    private_detail = str(tmp_path / "private-source-name.mp4")

    def fail_spawn(*_args, **_kwargs):
        raise OSError(private_detail)

    monkeypatch.setattr(
        "video_auto_editor.delivery._media.subprocess.Popen",
        fail_spawn,
    )
    run_id = RunId.new()
    configuration = Configuration.load(source.source_file.path)

    with workspace.acquire_run(run_id) as run_workspace:
        with pytest.raises(DeliveryBuildFailure) as captured:
            DeliveryBuild.build(
                DeliveryBuildRequest(
                    run_id=run_id,
                    source=source,
                    transcript=transcript,
                    plan=plan,
                    subtitles=subtitles,
                    staging_directory=run_workspace.delivery_staging,
                    subtitle_style=configuration.effective.subtitle_style,
                    application_version="4.7.0",
                    started_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        0,
                        tzinfo=timezone.utc,
                    ),
                    published_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    cancellation=CancellationSource().token,
                )
            )

        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        assert captured.value.error_code is ErrorCode.DELIVERY_EXPORT_FAILED
        assert captured.value.diagnostics == {
            "operation": "ffmpeg.subtitle_burn",
            "artifact_role": "short_video_media",
            "reason_code": "media.spawn_failed",
        }
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert private_detail not in str(captured.value)
        assert private_detail not in repr(captured.value)
        assert not (staging / "manifest.json").exists()
        assert existing_delivery.read_bytes() == b"existing delivery"
        assert list((workspace.root / "delivery").iterdir()) == [
            existing_delivery
        ]


def test_document_build_failure_never_exposes_a_new_standard_delivery(
    tmp_path,
):
    workspace, source, transcript, plan, subtitles = _empty_delivery_facts(
        tmp_path
    )
    transcript_chunk_id = transcript.chunks[0].transcript_chunk_id
    invalid_transcript = CompleteTranscript._from_application(
        transcript_id=transcript.transcript_id,
        speech_presence=SpeechPresence.PRESENT,
        chunks=(
            TranscriptChunk._from_application(
                transcript_chunk_id,
                TranscriptionChunk(
                    start_ms=0,
                    end_ms=2_000,
                    text="\ud800",
                ),
            ),
        ),
    )
    existing_delivery = workspace.root / "delivery" / "existing.txt"
    existing_delivery.write_bytes(b"existing delivery")
    run_id = RunId.new()
    configuration = Configuration.load(source.source_file.path)

    with workspace.acquire_run(run_id) as run_workspace:
        with pytest.raises(DeliveryBuildFailure) as captured:
            DeliveryBuild.build(
                DeliveryBuildRequest(
                    run_id=run_id,
                    source=source,
                    transcript=invalid_transcript,
                    plan=plan,
                    subtitles=subtitles,
                    staging_directory=run_workspace.delivery_staging,
                    subtitle_style=configuration.effective.subtitle_style,
                    application_version="4.7.0",
                    started_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        0,
                        tzinfo=timezone.utc,
                    ),
                    published_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    cancellation=CancellationSource().token,
                )
            )

        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        assert captured.value.error_code is ErrorCode.DELIVERY_BUILD_FAILED
        assert captured.value.diagnostics == {
            "operation": "delivery.serialize",
            "artifact_role": "faithful_transcript",
            "reason_code": "delivery.serialization_failed",
        }
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert list(staging.iterdir()) == []
        assert existing_delivery.read_bytes() == b"existing delivery"
        assert list((workspace.root / "delivery").iterdir()) == [
            existing_delivery
        ]


def test_cancellation_stops_the_media_process_group_without_delivery(
    tmp_path,
    monkeypatch,
):
    (
        workspace,
        source,
        transcript,
        plan,
        subtitles,
        _short_video_id,
    ) = _clip_delivery_facts(tmp_path)
    started_marker = tmp_path / "ffmpeg-started"
    descendant_survived = tmp_path / "descendant-survived"
    descendant_code = "\n".join(
        [
            "from pathlib import Path",
            "from time import sleep",
            "sleep(30)",
            f"Path({str(descendant_survived)!r}).write_bytes(b'survived')",
        ]
    )
    executable = tmp_path / "ffmpeg"
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
                (
                    f"Path({str(started_marker)!r}).write_text("
                    "str(descendant.pid), encoding='utf-8')"
                ),
                "sleep(30)",
                "",
            ]
        ),
        encoding="utf-8",
    )
    executable.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    cancellation = CancellationSource(clock=monotonic)
    configuration = Configuration.load(source.source_file.path)
    run_id = RunId.new()

    def request_after_start() -> None:
        while not started_marker.exists():
            sleep(0.01)
        cancellation.request(signal.SIGINT)

    requester = Thread(target=request_after_start, daemon=True)
    requester.start()
    started_at = monotonic()
    with workspace.acquire_run(run_id) as run_workspace:
        with pytest.raises(CancellationRequested) as captured:
            DeliveryBuild.build(
                DeliveryBuildRequest(
                    run_id=run_id,
                    source=source,
                    transcript=transcript,
                    plan=plan,
                    subtitles=subtitles,
                    staging_directory=run_workspace.delivery_staging,
                    subtitle_style=configuration.effective.subtitle_style,
                    application_version="4.7.0",
                    started_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        0,
                        tzinfo=timezone.utc,
                    ),
                    published_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    cancellation=cancellation.token,
                )
            )

        assert captured.value.signal_number == signal.SIGINT
        assert monotonic() - started_at < 2
        requester.join(timeout=1)
        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        assert not (staging / "manifest.json").exists()

    descendant_pid = int(started_marker.read_text(encoding="utf-8"))
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
    assert descendant_survived.exists() is False


def test_managed_media_write_failure_is_safe_and_never_forms_manifest(
    tmp_path,
    monkeypatch,
):
    (
        workspace,
        source,
        transcript,
        plan,
        subtitles,
        _short_video_id,
    ) = _clip_delivery_facts(tmp_path)
    private_detail = str(tmp_path / "private-output.mp4")

    def fail_write(_self, _contents):
        raise OSError(private_detail)

    monkeypatch.setattr(ManagedBinaryFile, "write", fail_write)
    configuration = Configuration.load(source.source_file.path)
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        with pytest.raises(DeliveryBuildFailure) as captured:
            DeliveryBuild.build(
                DeliveryBuildRequest(
                    run_id=run_id,
                    source=source,
                    transcript=transcript,
                    plan=plan,
                    subtitles=subtitles,
                    staging_directory=run_workspace.delivery_staging,
                    subtitle_style=configuration.effective.subtitle_style,
                    application_version="4.7.0",
                    started_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        0,
                        tzinfo=timezone.utc,
                    ),
                    published_at=datetime(
                        2026,
                        7,
                        31,
                        12,
                        1,
                        tzinfo=timezone.utc,
                    ),
                    cancellation=CancellationSource().token,
                )
            )

        assert captured.value.error_code is ErrorCode.DELIVERY_BUILD_FAILED
        assert captured.value.diagnostics == {
            "artifact_role": "short_video_media",
            "reason_code": "delivery.write_failed",
        }
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
        assert private_detail not in str(captured.value)
        assert private_detail not in repr(captured.value)
        staging = (
            workspace.root
            / "work"
            / "tmp"
            / str(run_id)
            / "delivery"
        )
        assert not (staging / "manifest.json").exists()


def test_delivery_request_rejects_force_style_delimiters_in_font_name(
    tmp_path,
):
    workspace, source, transcript, plan, subtitles = _empty_delivery_facts(
        tmp_path
    )
    configuration = Configuration.load(source.source_file.path)
    unsafe_style = replace(
        configuration.effective.subtitle_style,
        font="Noto Sans=Injected",
    )
    run_id = RunId.new()

    with workspace.acquire_run(run_id) as run_workspace:
        with pytest.raises(
            ValueError,
            match="字幕字体名称不符合安全烧录约束",
        ):
            DeliveryBuildRequest(
                run_id=run_id,
                source=source,
                transcript=transcript,
                plan=plan,
                subtitles=subtitles,
                staging_directory=run_workspace.delivery_staging,
                subtitle_style=unsafe_style,
                application_version="4.7.0",
                started_at=datetime(
                    2026,
                    7,
                    31,
                    12,
                    0,
                    tzinfo=timezone.utc,
                ),
                published_at=datetime(
                    2026,
                    7,
                    31,
                    12,
                    1,
                    tzinfo=timezone.utc,
                ),
                cancellation=CancellationSource().token,
            )
