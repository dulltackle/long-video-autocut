import copy
import hashlib
import io
import json
import os
import selectors
import signal
import subprocess
import sys
from threading import Thread
from time import monotonic, sleep

import pytest

from video_auto_editor.delivery.capability import (
    UnverifiedDelivery,
    VerifiedDelivery,
)
from video_auto_editor.delivery.verification import (
    DeliveryVerification,
    DeliveryVerificationFailure,
)
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import ManagedPathCapability, Workspace


def _json_bytes(document):
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _snapshot(contents_by_path):
    digest = hashlib.sha256(b"delivery_snapshot.v1\0")
    for path in sorted(contents_by_path):
        path_bytes = path.encode("utf-8")
        contents = contents_by_path[path]
        digest.update(len(path_bytes).to_bytes(8, "big"))
        digest.update(path_bytes)
        digest.update(len(contents).to_bytes(8, "big"))
        digest.update(hashlib.sha256(contents).digest())
    return "sha256:" + digest.hexdigest()


def _report_bytes(run_id, result_kind, candidate_count, published_count):
    if result_kind == "empty":
        outcome = (
            "本次运行成功完成，形成有效空结果；没有候选满足发布条件，短视频集合为空。"
        )
    else:
        outcome = f"本次运行成功完成，共形成 {published_count} 条短视频。"
    return (
        "# 直播拆条报告\n\n"
        f"- 运行标识：`{run_id}`\n"
        f"- 结果类型：`{result_kind}`\n"
        f"- 候选数量：{candidate_count}\n"
        f"- 发布数量：{published_count}\n\n"
        "## 结果说明\n\n"
        f"{outcome}\n"
    ).encode()


def _refresh_manifest_artifact(staging, relative_path):
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    artifact = next(item for item in manifest["files"] if item["path"] == relative_path)
    contents = (staging / relative_path).read_bytes()
    artifact["byte_length"] = len(contents)
    artifact["sha256"] = "sha256:" + hashlib.sha256(contents).hexdigest()
    manifest_path.write_bytes(_json_bytes(manifest))


@pytest.fixture
def empty_delivery(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"independent consumer fixture")
    workspace = Workspace.open(source, tmp_path / "workspace")
    run_id = RunId("run_00000000-0000-4000-8000-000000000001")
    transcript_id = "transcript_00000000-0000-4000-8000-000000000002"
    transcript_chunk_id = "transcript_chunk_00000000-0000-4000-8000-000000000003"
    plan_id = "plan_00000000-0000-4000-8000-000000000004"
    candidate_id = "candidate_00000000-0000-4000-8000-000000000005"

    with workspace.acquire_run(run_id) as run_workspace:
        documents = {
            "transcript.json": _json_bytes(
                {
                    "schema_version": "transcript.v1",
                    "run_id": str(run_id),
                    "transcript_id": transcript_id,
                    "speech_presence": "present",
                    "source_duration_ms": 300_000,
                    "chunks": [
                        {
                            "transcript_chunk_id": transcript_chunk_id,
                            "start_ms": 0,
                            "end_ms": 2_000,
                            "text": "忠实原文，保留语气词嗯。",
                        }
                    ],
                }
            ),
            "transcript.srt": (
                "1\n00:00:00,000 --> 00:00:02,000\n忠实原文，保留语气词嗯。\n\n"
            ).encode(),
            "plan.json": _json_bytes(
                {
                    "schema_version": "clip_plan.v1",
                    "run_id": str(run_id),
                    "plan_id": plan_id,
                    "transcript_id": transcript_id,
                    "result_kind": "empty",
                    "candidate_count": 1,
                    "published_count": 0,
                    "candidates": [
                        {
                            "candidate_id": candidate_id,
                            "transcript_chunk_ids": [transcript_chunk_id],
                            "initial_range": {
                                "start_ms": 0,
                                "end_ms": 2_000,
                            },
                            "final_range": {
                                "start_ms": 0,
                                "end_ms": 2_000,
                            },
                            "boundary_remedy": {
                                "status": "not_needed",
                                "suggestion": "",
                                "requested_start_ms": None,
                                "requested_end_ms": None,
                            },
                            "review": {
                                "topic_name": "上下文不足的候选",
                                "topic_complete": False,
                                "learning_value": 6,
                                "share_value": 5,
                                "publish_ready_score": 62,
                                "export_decision": "reject",
                                "title": "不能独立发布的候选",
                                "summary": "完整评审后确认缺少独立结论。",
                                "keywords": ["评审"],
                                "needs_human_review": False,
                                "reject_reason": "缺少独立结论",
                                "boundary_fix_suggestion": "",
                                "boundary_fix_start_ms": None,
                                "boundary_fix_end_ms": None,
                            },
                            "selection": {
                                "outcome": "rejected",
                                "reason_code": "review_rejected",
                                "needs_human_review": False,
                                "human_review_reason": "",
                            },
                        }
                    ],
                }
            ),
            "metadata.json": _json_bytes(
                {
                    "schema_version": "short_video_catalog.v1",
                    "run_id": str(run_id),
                    "result_kind": "empty",
                    "short_videos": [],
                    "series": [],
                }
            ),
            "report.md": _report_bytes(run_id, "empty", 1, 0),
        }
        artifact_contract = {
            "metadata.json": (
                "short_video_catalog",
                "application/json",
            ),
            "plan.json": ("clip_plan", "application/json"),
            "report.md": ("human_report", "text/markdown"),
            "transcript.json": (
                "faithful_transcript",
                "application/json",
            ),
            "transcript.srt": (
                "faithful_transcript_rendering",
                "application/x-subrip",
            ),
        }
        files = [
            {
                "path": path,
                "role": artifact_contract[path][0],
                "media_type": artifact_contract[path][1],
                "byte_length": len(documents[path]),
                "sha256": ("sha256:" + hashlib.sha256(documents[path]).hexdigest()),
            }
            for path in sorted(documents)
        ]
        manifest = _json_bytes(
            {
                "schema_version": "delivery_manifest.v1",
                "run_id": str(run_id),
                "terminal_state": "succeeded",
                "result_kind": "empty",
                "started_at": "2026-07-31T12:00:00.000Z",
                "published_at": "2026-07-31T12:01:00.000Z",
                "application_version": "4.7.0",
                "source": {
                    "sha256": "sha256:" + "0" * 64,
                    "byte_length": 28,
                    "duration_ms": 300_000,
                },
                "documents": {
                    "transcript": {
                        "path": "transcript.json",
                        "transcript_id": transcript_id,
                    },
                    "transcript_rendering": {
                        "path": "transcript.srt",
                        "transcript_id": transcript_id,
                    },
                    "plan": {
                        "path": "plan.json",
                        "plan_id": plan_id,
                    },
                    "metadata": {"path": "metadata.json"},
                    "report": {"path": "report.md"},
                },
                "execution": {
                    "subtitle_optimization": {
                        "short_video_count": 0,
                        "window_count": 0,
                        "model_request_count": 0,
                        "cache_hit_count": 0,
                        "cache_miss_count": 0,
                        "semantic_retry_count": 0,
                        "transport_attempt_count": 0,
                        "transport_retry_count": 0,
                    }
                },
                "files": files,
            }
        )
        run_workspace.delivery_staging.location("clips").mkdir()
        for path, contents in documents.items():
            run_workspace.delivery_staging.location(path).publish_bytes_atomically(
                contents
            )
        run_workspace.delivery_staging.location(
            "manifest.json"
        ).publish_bytes_atomically(manifest)
        staging = workspace.root / "work" / "tmp" / str(run_id) / "delivery"
        yield (
            UnverifiedDelivery._from_build(
                run_id,
                run_workspace.delivery_staging,
            ),
            run_workspace,
            staging,
        )


@pytest.fixture
def clips_delivery(empty_delivery, tmp_path):
    unverified, run_workspace, staging = empty_delivery
    candidate_id = "candidate_00000000-0000-4000-8000-000000000005"
    short_video_id = "short_video_00000000-0000-4000-8000-000000000006"
    media_relative_path = f"clips/{short_video_id}.mp4"
    generated_media = tmp_path / "independent-consumer-clip.mp4"
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
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-y",
            str(generated_media),
        ],
        check=True,
    )
    media_bytes = generated_media.read_bytes()
    run_workspace.delivery_staging.location(
        media_relative_path
    ).publish_bytes_atomically(media_bytes)

    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["result_kind"] = "clips"
    plan["published_count"] = 1
    candidate = plan["candidates"][0]
    candidate["review"].update(
        {
            "topic_name": "独立交付验证",
            "topic_complete": True,
            "learning_value": 9,
            "share_value": 8,
            "publish_ready_score": 92,
            "export_decision": "publish_ready",
            "title": "从消费者视角验证短视频",
            "summary": "独立检查引用、摘要以及真实音视频流。",
            "keywords": ["验证", "交付"],
            "reject_reason": "",
        }
    )
    candidate["selection"] = {
        "outcome": "published",
        "short_video_id": short_video_id,
    }
    plan_path.write_bytes(_json_bytes(plan))

    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["result_kind"] = "clips"
    metadata["short_videos"] = [
        {
            "short_video_id": short_video_id,
            "source_candidate_id": candidate_id,
            "topic_name": candidate["review"]["topic_name"],
            "title": candidate["review"]["title"],
            "summary": candidate["review"]["summary"],
            "keywords": candidate["review"]["keywords"],
            "start_ms": 0,
            "end_ms": 2_000,
            "duration_ms": 2_000,
            "media": {
                "path": media_relative_path,
                "container": "mp4",
                "video_required": True,
                "audio_required": True,
            },
            "subtitles": {"kind": "burned_in"},
        }
    ]
    metadata_path.write_bytes(_json_bytes(metadata))
    report_path = staging / "report.md"
    report_path.write_bytes(_report_bytes(unverified.run_id, "clips", 1, 1))
    for relative_path in ("metadata.json", "plan.json", "report.md"):
        _refresh_manifest_artifact(staging, relative_path)

    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["result_kind"] = "clips"
    manifest["execution"]["subtitle_optimization"].update(
        {
            "short_video_count": 1,
            "window_count": 1,
            "model_request_count": 1,
            "cache_miss_count": 1,
            "transport_attempt_count": 1,
        }
    )
    manifest["files"].append(
        {
            "path": media_relative_path,
            "role": "short_video_media",
            "media_type": "video/mp4",
            "byte_length": len(media_bytes),
            "sha256": "sha256:" + hashlib.sha256(media_bytes).hexdigest(),
        }
    )
    manifest["files"].sort(key=lambda artifact: artifact["path"])
    manifest_path.write_bytes(_json_bytes(manifest))
    return unverified, run_workspace, staging, media_relative_path


def _add_second_same_topic_clip(staging, first_media_path):
    candidate_id = "candidate_00000000-0000-4000-8000-000000000007"
    short_video_id = "short_video_00000000-0000-4000-8000-000000000008"
    media_relative_path = f"clips/{short_video_id}.mp4"
    media_bytes = (staging / first_media_path).read_bytes()
    (staging / media_relative_path).write_bytes(media_bytes)

    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    candidate = copy.deepcopy(plan["candidates"][0])
    candidate["candidate_id"] = candidate_id
    candidate["initial_range"] = {"start_ms": 2_000, "end_ms": 4_000}
    candidate["final_range"] = {"start_ms": 2_000, "end_ms": 4_000}
    candidate["selection"] = {
        "outcome": "published",
        "short_video_id": short_video_id,
    }
    plan["candidates"].append(candidate)
    plan["candidate_count"] = 2
    plan["published_count"] = 2
    plan_path.write_bytes(_json_bytes(plan))

    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    short_video = copy.deepcopy(metadata["short_videos"][0])
    short_video.update(
        {
            "short_video_id": short_video_id,
            "source_candidate_id": candidate_id,
            "start_ms": 2_000,
            "end_ms": 4_000,
        }
    )
    short_video["media"]["path"] = media_relative_path
    metadata["short_videos"].append(short_video)
    metadata_path.write_bytes(_json_bytes(metadata))
    (staging / "report.md").write_bytes(_report_bytes(plan["run_id"], "clips", 2, 2))

    for relative_path in ("metadata.json", "plan.json", "report.md"):
        _refresh_manifest_artifact(staging, relative_path)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["execution"]["subtitle_optimization"].update(
        {
            "short_video_count": 2,
            "window_count": 2,
            "model_request_count": 2,
            "cache_miss_count": 2,
            "transport_attempt_count": 2,
        }
    )
    manifest["files"].append(
        {
            "path": media_relative_path,
            "role": "short_video_media",
            "media_type": "video/mp4",
            "byte_length": len(media_bytes),
            "sha256": "sha256:" + hashlib.sha256(media_bytes).hexdigest(),
        }
    )
    manifest["files"].sort(key=lambda artifact: artifact["path"])
    manifest_path.write_bytes(_json_bytes(manifest))
    return short_video_id


def _append_published_clip(
    staging,
    *,
    candidate_id,
    short_video_id,
    start_ms,
    topic_name,
):
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    candidate = copy.deepcopy(plan["candidates"][0])
    candidate["candidate_id"] = candidate_id
    candidate["initial_range"] = {
        "start_ms": start_ms,
        "end_ms": start_ms + 2_000,
    }
    candidate["final_range"] = copy.deepcopy(candidate["initial_range"])
    candidate["review"].update(
        {
            "topic_name": topic_name,
            "title": f"{topic_name}标题",
            "summary": f"{topic_name}摘要",
            "keywords": [topic_name],
        }
    )
    candidate["selection"] = {
        "outcome": "published",
        "short_video_id": short_video_id,
    }
    plan["candidates"].append(candidate)
    plan["candidate_count"] = len(plan["candidates"])
    plan["published_count"] = len(plan["candidates"])
    plan_path.write_bytes(_json_bytes(plan))

    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    template = metadata["short_videos"][0]
    source_media_path = staging / template["media"]["path"]
    media_relative_path = f"clips/{short_video_id}.mp4"
    media_bytes = source_media_path.read_bytes()
    (staging / media_relative_path).write_bytes(media_bytes)
    short_video = copy.deepcopy(template)
    short_video.update(
        {
            "short_video_id": short_video_id,
            "source_candidate_id": candidate_id,
            "topic_name": topic_name,
            "title": candidate["review"]["title"],
            "summary": candidate["review"]["summary"],
            "keywords": candidate["review"]["keywords"],
            "start_ms": start_ms,
            "end_ms": start_ms + 2_000,
        }
    )
    short_video["media"]["path"] = media_relative_path
    metadata["short_videos"].append(short_video)
    metadata_path.write_bytes(_json_bytes(metadata))

    count = len(plan["candidates"])
    (staging / "report.md").write_bytes(
        _report_bytes(plan["run_id"], "clips", count, count)
    )
    for relative_path in ("metadata.json", "plan.json", "report.md"):
        _refresh_manifest_artifact(staging, relative_path)
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["execution"]["subtitle_optimization"].update(
        {
            "short_video_count": count,
            "window_count": count,
            "model_request_count": count,
            "cache_miss_count": count,
            "transport_attempt_count": count,
        }
    )
    manifest["files"].append(
        {
            "path": media_relative_path,
            "role": "short_video_media",
            "media_type": "video/mp4",
            "byte_length": len(media_bytes),
            "sha256": "sha256:" + hashlib.sha256(media_bytes).hexdigest(),
        }
    )
    manifest["files"].sort(key=lambda artifact: artifact["path"])
    manifest_path.write_bytes(_json_bytes(manifest))
    return media_relative_path


def test_verification_accepts_an_unchanged_empty_delivery_without_writing(
    empty_delivery,
):
    unverified, run_workspace, staging = empty_delivery
    before = {
        path.relative_to(staging).as_posix(): path.read_bytes()
        for path in staging.rglob("*")
        if path.is_file()
    }

    verified = DeliveryVerification.verify(
        unverified,
        CancellationSource().token,
    )

    after = {
        path.relative_to(staging).as_posix(): path.read_bytes()
        for path in staging.rglob("*")
        if path.is_file()
    }
    assert isinstance(verified, VerifiedDelivery)
    assert verified.run_id == unverified.run_id
    assert verified.managed_directory is run_workspace.delivery_staging
    assert verified.verification_snapshot == _snapshot(before)
    assert after == before


def test_verification_rejects_unknown_manifest_fields_without_repairing(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["builder_only_hint"] = True
    corrupted = _json_bytes(manifest)
    manifest_path.write_bytes(corrupted)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert captured.value.error_code is ErrorCode.DELIVERY_VERIFICATION_FAILED
    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.schema_invalid",
    }
    assert manifest_path.read_bytes() == corrupted


def test_verification_rejects_an_unsupported_manifest_version(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["schema_version"] = "delivery_manifest.v2"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.schema_invalid",
    }


def test_verification_rejects_nanosecond_publication_before_start(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["started_at"] = "2026-07-31T12:00:00.000000009Z"
    manifest["published_at"] = "2026-07-31T12:00:00.000000001Z"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.schema_invalid",
    }


def test_verification_normalizes_oversized_json_integers_without_context(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    corrupted = manifest_path.read_bytes().replace(
        b'"duration_ms":300000',
        b'"duration_ms":' + b"9" * 5_000,
    )
    manifest_path.write_bytes(corrupted)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.schema_invalid",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert b"9" * 100 not in str(captured.value).encode()


def test_verification_rejects_an_unsupported_transcript_version(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    transcript_path = staging / "transcript.json"
    transcript = json.loads(transcript_path.read_bytes())
    transcript["schema_version"] = "transcript.v2"
    transcript_path.write_bytes(_json_bytes(transcript))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "faithful_transcript",
        "reason_code": "verification.schema_invalid",
    }


def test_verification_rejects_an_unsupported_plan_version(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["schema_version"] = "clip_plan.v2"
    plan_path.write_bytes(_json_bytes(plan))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "clip_plan",
        "reason_code": "verification.schema_invalid",
    }


def test_verification_rejects_an_unsupported_catalog_version(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["schema_version"] = "short_video_catalog.v2"
    metadata_path.write_bytes(_json_bytes(metadata))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "short_video_catalog",
        "reason_code": "verification.schema_invalid",
    }


def test_verification_rejects_a_catalog_from_another_run(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["run_id"] = "run_00000000-0000-4000-8000-000000000099"
    metadata_path.write_bytes(_json_bytes(metadata))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "short_video_catalog",
        "reason_code": "verification.run_id_mismatch",
    }


def test_verification_rejects_a_business_id_with_the_wrong_type_prefix(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"][0]["candidate_id"] = (
        "short_video_00000000-0000-4000-8000-000000000005"
    )
    plan_path.write_bytes(_json_bytes(plan))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "clip_plan",
        "reason_code": "verification.identity_invalid",
    }


def test_verification_preserves_manifest_identity_error_classification(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["documents"]["transcript"]["transcript_id"] = "not-an-id"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.identity_invalid",
    }


def test_verification_preserves_manifest_reference_error_classification(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["documents"]["transcript_rendering"]["transcript_id"] = (
        "transcript_99999999-9999-4999-8999-999999999999"
    )
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_rejects_a_duplicate_typed_business_id(
    clips_delivery,
):
    unverified, _run_workspace, staging, first_media_path = clips_delivery
    _add_second_same_topic_clip(staging, first_media_path)
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"][1]["candidate_id"] = plan["candidates"][0]["candidate_id"]
    plan_path.write_bytes(_json_bytes(plan))
    _refresh_manifest_artifact(staging, "plan.json")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "clip_plan",
        "reason_code": "verification.identity_duplicate",
    }


def test_verification_rejects_a_dangling_transcript_chunk_reference(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"][0]["transcript_chunk_ids"] = [
        "transcript_chunk_00000000-0000-4000-8000-000000000099"
    ]
    plan_path.write_bytes(_json_bytes(plan))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "clip_plan",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_rejects_a_result_kind_that_disagrees_with_documents(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["result_kind"] = "clips"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.result_kind_mismatch",
    }


def test_verification_rejects_a_published_selection_with_rejected_review(
    clips_delivery,
):
    unverified, _run_workspace, staging, _media_relative_path = clips_delivery
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"][0]["review"]["export_decision"] = "reject"
    plan["candidates"][0]["review"]["reject_reason"] = "明确不应发布"
    plan_path.write_bytes(_json_bytes(plan))
    _refresh_manifest_artifact(staging, "plan.json")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "clip_plan",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_rejects_an_omitted_contiguous_same_topic_series(
    clips_delivery,
):
    unverified, _run_workspace, staging, first_media_path = clips_delivery
    _add_second_same_topic_clip(staging, first_media_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "short_video_catalog",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_accepts_the_complete_contiguous_same_topic_series(
    clips_delivery,
):
    unverified, _run_workspace, staging, first_media_path = clips_delivery
    second_short_video_id = _add_second_same_topic_clip(
        staging,
        first_media_path,
    )
    first_short_video_id = first_media_path.removeprefix("clips/").removesuffix(".mp4")
    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["series"] = [
        {
            "series_id": "series_00000000-0000-4000-8000-000000000009",
            "topic": "独立交付验证",
            "short_video_ids": [
                first_short_video_id,
                second_short_video_id,
            ],
        }
    ]
    metadata_path.write_bytes(_json_bytes(metadata))
    _refresh_manifest_artifact(staging, "metadata.json")

    verified = DeliveryVerification.verify(
        unverified,
        CancellationSource().token,
    )

    assert isinstance(verified, VerifiedDelivery)


def test_verification_rejects_a_series_ordered_by_plan_array_not_source_time(
    clips_delivery,
):
    unverified, _run_workspace, staging, first_media_path = clips_delivery
    second_short_video_id = _add_second_same_topic_clip(
        staging,
        first_media_path,
    )
    first_short_video_id = first_media_path.removeprefix("clips/").removesuffix(".mp4")
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"].reverse()
    plan_path.write_bytes(_json_bytes(plan))

    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["series"] = [
        {
            "series_id": "series_00000000-0000-4000-8000-000000000009",
            "topic": "独立交付验证",
            "short_video_ids": [
                second_short_video_id,
                first_short_video_id,
            ],
        }
    ]
    metadata_path.write_bytes(_json_bytes(metadata))
    for relative_path in ("metadata.json", "plan.json"):
        _refresh_manifest_artifact(staging, relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "short_video_catalog",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_accepts_series_collection_in_any_array_order(
    clips_delivery,
):
    unverified, _run_workspace, staging, first_media_path = clips_delivery
    second_short_video_id = _add_second_same_topic_clip(
        staging,
        first_media_path,
    )
    third_short_video_id = "short_video_00000000-0000-4000-8000-000000000011"
    fourth_short_video_id = "short_video_00000000-0000-4000-8000-000000000013"
    _append_published_clip(
        staging,
        candidate_id="candidate_00000000-0000-4000-8000-000000000010",
        short_video_id=third_short_video_id,
        start_ms=4_000,
        topic_name="第二主题",
    )
    _append_published_clip(
        staging,
        candidate_id="candidate_00000000-0000-4000-8000-000000000012",
        short_video_id=fourth_short_video_id,
        start_ms=6_000,
        topic_name="第二主题",
    )
    first_short_video_id = first_media_path.removeprefix("clips/").removesuffix(".mp4")
    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["series"] = [
        {
            "series_id": "series_00000000-0000-4000-8000-000000000014",
            "topic": "第二主题",
            "short_video_ids": [
                third_short_video_id,
                fourth_short_video_id,
            ],
        },
        {
            "series_id": "series_00000000-0000-4000-8000-000000000015",
            "topic": "独立交付验证",
            "short_video_ids": [
                first_short_video_id,
                second_short_video_id,
            ],
        },
    ]
    metadata_path.write_bytes(_json_bytes(metadata))
    _refresh_manifest_artifact(staging, "metadata.json")

    verified = DeliveryVerification.verify(
        unverified,
        CancellationSource().token,
    )

    assert isinstance(verified, VerifiedDelivery)


def test_verification_rejects_a_manifest_path_that_escapes_delivery(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"][0]["path"] = "../metadata.json"
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.path_unsafe",
    }


def test_verification_rejects_an_extra_file_not_listed_by_the_manifest(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    extra_path = staging / "consumer-must-not-see.txt"
    extra_path.write_bytes(b"unexpected")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.file_set_mismatch",
    }
    assert extra_path.read_bytes() == b"unexpected"


def test_verification_rejects_a_short_video_sidecar_subtitle(
    clips_delivery,
):
    unverified, _run_workspace, staging, _media_relative_path = clips_delivery
    sidecar = staging / "clips" / "forbidden.srt"
    sidecar.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nforbidden\n")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.sidecar_subtitle_present",
    }
    assert sidecar.is_file()


def test_verification_rejects_a_manifest_file_that_is_missing(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    (staging / "report.md").unlink()

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.file_set_mismatch",
    }


def test_verification_rejects_a_symbolic_link_without_removing_it(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    link_path = staging / "clips" / "forged.mp4"
    link_path.symlink_to(staging / "transcript.json")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.symlink_present",
    }
    assert link_path.is_symlink()


def test_verification_rejects_changed_bytes_with_the_original_digest(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    report_path = staging / "report.md"
    original = report_path.read_bytes()
    corrupted = bytes([original[0] ^ 1]) + original[1:]
    report_path.write_bytes(corrupted)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_digest",
        "artifact_role": "human_report",
        "reason_code": "verification.digest_mismatch",
    }
    assert report_path.read_bytes() == corrupted


def test_verification_rejects_an_equal_length_rewrite_after_document_read(
    empty_delivery,
    monkeypatch,
):
    unverified, _run_workspace, staging = empty_delivery
    report_path = staging / "report.md"
    original_read = ManagedPathCapability.read_bytes
    rewrite_triggered = False

    def rewrite_after_report_read(path):
        nonlocal rewrite_triggered
        contents = original_read(path)
        if not rewrite_triggered and contents.startswith("# 直播拆条报告".encode()):
            rewrite_triggered = True
            rewritten = bytes([contents[0] ^ 1]) + contents[1:]
            report_path.write_bytes(rewritten)
        return contents

    monkeypatch.setattr(
        ManagedPathCapability,
        "read_bytes",
        rewrite_after_report_read,
    )

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_files",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.file_set_mismatch",
    }
    assert rewrite_triggered


def test_verification_rejects_non_utf8_human_report_even_with_fresh_digest(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    report_path = staging / "report.md"
    corrupted = b"# report\n\n\xffprivate-bytes\n"
    report_path.write_bytes(corrupted)
    _refresh_manifest_artifact(staging, "report.md")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "human_report",
        "reason_code": "verification.schema_invalid",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert report_path.read_bytes() == corrupted


def test_verification_rejects_a_human_report_that_contradicts_machine_facts(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    report_path = staging / "report.md"
    corrupted = _report_bytes(unverified.run_id, "clips", 99, 42)
    report_path.write_bytes(corrupted)
    _refresh_manifest_artifact(staging, "report.md")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_schema",
        "artifact_role": "human_report",
        "reason_code": "verification.schema_invalid",
    }
    assert report_path.read_bytes() == corrupted


def test_verification_rerenders_the_faithful_transcript_byte_for_byte(
    empty_delivery,
):
    unverified, _run_workspace, staging = empty_delivery
    transcript_rendering = staging / "transcript.srt"
    corrupted = transcript_rendering.read_bytes().removesuffix(b"\n")
    transcript_rendering.write_bytes(corrupted)
    _refresh_manifest_artifact(staging, "transcript.srt")

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_transcript",
        "artifact_role": "faithful_transcript_rendering",
        "reason_code": "verification.transcript_mismatch",
    }
    assert transcript_rendering.read_bytes() == corrupted


def test_verification_rejects_unreadable_clip_bytes_after_digest_checks(
    clips_delivery,
):
    unverified, _run_workspace, staging, media_relative_path = clips_delivery
    media_path = staging / media_relative_path
    corrupted = b"not an mp4 container"
    media_path.write_bytes(corrupted)
    _refresh_manifest_artifact(staging, media_relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.media_invalid",
    }
    assert media_path.read_bytes() == corrupted


def test_verification_rejects_a_truncated_mp4_with_refreshed_digest(
    clips_delivery,
):
    unverified, _run_workspace, staging, media_relative_path = clips_delivery
    media_path = staging / media_relative_path
    original = media_path.read_bytes()
    truncated = original[: len(original) * 9 // 10]
    media_path.write_bytes(truncated)
    _refresh_manifest_artifact(staging, media_relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.media_invalid",
    }
    assert media_path.read_bytes() == truncated


def test_verification_rejects_media_missing_from_the_manifest_file_set(
    clips_delivery,
):
    unverified, _run_workspace, staging, media_relative_path = clips_delivery
    manifest_path = staging / "manifest.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["files"] = [
        artifact
        for artifact in manifest["files"]
        if artifact["path"] != media_relative_path
    ]
    manifest_path.write_bytes(_json_bytes(manifest))

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_references",
        "artifact_role": "delivery_manifest",
        "reason_code": "verification.reference_dangling",
    }


def test_verification_stops_probe_and_normalizes_selector_failure(
    clips_delivery,
    monkeypatch,
):
    unverified, _run_workspace, _staging, _media_relative_path = clips_delivery

    class FakeProcess:
        def __init__(self):
            self.stdin = io.BytesIO()
            self.stdout = io.BytesIO()
            self.stderr = io.BytesIO()
            self.pid = 2_000_000_000
            self.returncode = None
            self.stopped = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.stopped = True
            self.returncode = -15

        def kill(self):
            self.stopped = True
            self.returncode = -9

        def wait(self, timeout=None):
            self.stopped = True
            if self.returncode is None:
                self.returncode = -15
            return self.returncode

    process = FakeProcess()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    def fail_selector_creation():
        raise OSError("/private/customer/provider-secret")

    monkeypatch.setattr(
        selectors,
        "DefaultSelector",
        fail_selector_creation,
    )

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.media_invalid",
    }
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert process.stopped


def test_verification_cancels_and_reaps_a_running_probe(
    clips_delivery,
    monkeypatch,
    tmp_path,
):
    unverified, _run_workspace, _staging, _media_relative_path = clips_delivery
    marker = tmp_path / "probe.pid"
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    executable = executable_directory / "ffprobe"
    executable.write_text(
        "\n".join(
            [
                f"#!{sys.executable}",
                "import os",
                "import sys",
                "from pathlib import Path",
                "from time import sleep",
                f"Path({str(marker)!r}).write_text(str(os.getpid()))",
                "sys.stdin.buffer.read(1)",
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
        DeliveryVerification.verify(
            unverified,
            cancellation.token,
        )

    requester.join(timeout=1)
    assert monotonic() - started_at < 2
    probe_pid = int(marker.read_text(encoding="utf-8"))
    reap_deadline = monotonic() + 2
    while monotonic() < reap_deadline:
        try:
            os.kill(probe_pid, 0)
        except ProcessLookupError:
            break
        sleep(0.05)
    else:
        pytest.fail("取消后 ffprobe 子进程仍未被回收")


def test_verification_accepts_an_independent_clip_delivery_with_av_media(
    clips_delivery,
):
    unverified, run_workspace, staging, _media_relative_path = clips_delivery
    before = {
        path.relative_to(staging).as_posix(): path.read_bytes()
        for path in staging.rglob("*")
        if path.is_file()
    }

    verified = DeliveryVerification.verify(
        unverified,
        CancellationSource().token,
    )

    after = {
        path.relative_to(staging).as_posix(): path.read_bytes()
        for path in staging.rglob("*")
        if path.is_file()
    }
    assert isinstance(verified, VerifiedDelivery)
    assert verified.run_id == unverified.run_id
    assert verified.managed_directory is run_workspace.delivery_staging
    assert verified.verification_snapshot == _snapshot(before)
    assert after == before


def test_verification_rejects_a_clip_without_an_audio_stream(
    clips_delivery,
    tmp_path,
):
    unverified, _run_workspace, staging, media_relative_path = clips_delivery
    video_only_path = tmp_path / "video-only.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=320x180:r=25",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            "-movflags",
            "frag_keyframe+empty_moov",
            "-y",
            str(video_only_path),
        ],
        check=True,
    )
    (staging / media_relative_path).write_bytes(video_only_path.read_bytes())
    _refresh_manifest_artifact(staging, media_relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.stream_missing",
    }


def test_verification_rejects_a_clip_with_mismatched_duration(
    clips_delivery,
):
    unverified, _run_workspace, staging, _media_relative_path = clips_delivery
    plan_path = staging / "plan.json"
    plan = json.loads(plan_path.read_bytes())
    plan["candidates"][0]["initial_range"]["end_ms"] = 3_000
    plan["candidates"][0]["final_range"]["end_ms"] = 3_000
    plan_path.write_bytes(_json_bytes(plan))

    metadata_path = staging / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["short_videos"][0]["end_ms"] = 3_000
    metadata["short_videos"][0]["duration_ms"] = 3_000
    metadata_path.write_bytes(_json_bytes(metadata))
    for relative_path in ("metadata.json", "plan.json"):
        _refresh_manifest_artifact(staging, relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.duration_mismatch",
    }


def test_verification_rejects_non_mp4_av_media(
    clips_delivery,
    tmp_path,
):
    unverified, _run_workspace, staging, media_relative_path = clips_delivery
    matroska_path = tmp_path / "renamed-as-mp4.mkv"
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
            "sine=frequency=1000:sample_rate=48000",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-f",
            "matroska",
            "-y",
            str(matroska_path),
        ],
        check=True,
    )
    (staging / media_relative_path).write_bytes(matroska_path.read_bytes())
    _refresh_manifest_artifact(staging, media_relative_path)

    with pytest.raises(DeliveryVerificationFailure) as captured:
        DeliveryVerification.verify(
            unverified,
            CancellationSource().token,
        )

    assert dict(captured.value.diagnostics) == {
        "operation": "delivery.verify_media",
        "artifact_role": "short_video_media",
        "reason_code": "verification.media_invalid",
    }
