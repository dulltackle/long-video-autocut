import json

from video_auto_editor.clip_planning import ResultKind
from video_auto_editor.delivery.verification import (
    DeliveryManifestReader,
    DeliveryManifestReadReason,
    DeliveryManifestReadState,
)
from video_auto_editor.runtime.identity import RunId

RUN_ID = RunId("run_11111111-1111-4111-8111-111111111111")


def delivery_manifest(*, result_kind="clips", short_video_count=1):
    if short_video_count not in {0, 1}:
        raise ValueError("测试清单只支持零或一个短视频")
    execution_counts = {
        "short_video_count": short_video_count,
        "window_count": short_video_count,
        "model_request_count": short_video_count,
        "cache_hit_count": 0,
        "cache_miss_count": short_video_count,
        "semantic_retry_count": 0,
        "transport_attempt_count": short_video_count,
        "transport_retry_count": 0,
    }
    files = [
        {
            "path": "metadata.json",
            "role": "short_video_catalog",
            "media_type": "application/json",
            "byte_length": 101,
            "sha256": "sha256:" + "1" * 64,
        },
        {
            "path": "plan.json",
            "role": "clip_plan",
            "media_type": "application/json",
            "byte_length": 102,
            "sha256": "sha256:" + "2" * 64,
        },
        {
            "path": "report.md",
            "role": "human_report",
            "media_type": "text/markdown",
            "byte_length": 103,
            "sha256": "sha256:" + "3" * 64,
        },
        {
            "path": "transcript.json",
            "role": "faithful_transcript",
            "media_type": "application/json",
            "byte_length": 104,
            "sha256": "sha256:" + "4" * 64,
        },
        {
            "path": "transcript.srt",
            "role": "faithful_transcript_rendering",
            "media_type": "application/x-subrip",
            "byte_length": 105,
            "sha256": "sha256:" + "5" * 64,
        },
    ]
    if short_video_count:
        files.insert(
            0,
            {
                "path": (
                    "clips/"
                    "short_video_22222222-2222-4222-8222-222222222222.mp4"
                ),
                "role": "short_video_media",
                "media_type": "video/mp4",
                "byte_length": 106,
                "sha256": "sha256:" + "6" * 64,
            },
        )
    return {
        "schema_version": "delivery_manifest.v1",
        "run_id": str(RUN_ID),
        "terminal_state": "succeeded",
        "result_kind": result_kind,
        "started_at": "2026-07-31T12:00:00.000Z",
        "published_at": "2026-07-31T12:10:00.000Z",
        "application_version": "4.7.0",
        "source": {
            "sha256": "sha256:" + "a" * 64,
            "byte_length": 42,
            "duration_ms": 300_000,
        },
        "documents": {
            "transcript": {
                "path": "transcript.json",
                "transcript_id": (
                    "transcript_33333333-3333-4333-8333-333333333333"
                ),
            },
            "transcript_rendering": {
                "path": "transcript.srt",
                "transcript_id": (
                    "transcript_33333333-3333-4333-8333-333333333333"
                ),
            },
            "plan": {
                "path": "plan.json",
                "plan_id": "plan_44444444-4444-4444-8444-444444444444",
            },
            "metadata": {"path": "metadata.json"},
            "report": {"path": "report.md"},
        },
        "execution": {"subtitle_optimization": execution_counts},
        "files": files,
    }


def manifest_bytes(document):
    return (
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def test_reader_accepts_a_complete_delivery_manifest():
    result = DeliveryManifestReader.read(
        manifest_bytes(delivery_manifest()),
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.COMPLETE
    assert result.reason is DeliveryManifestReadReason.VALID
    assert result.summary is not None
    assert result.summary.run_id == RUN_ID
    assert result.summary.result_kind.value == "clips"
    assert result.summary.short_video_count == 1
    assert result.summary.file_count == 6


def test_reader_accepts_a_complete_empty_delivery_manifest():
    result = DeliveryManifestReader.read(
        manifest_bytes(
            delivery_manifest(
                result_kind="empty",
                short_video_count=0,
            )
        ),
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.COMPLETE
    assert result.reason is DeliveryManifestReadReason.VALID
    assert result.summary is not None
    assert result.summary.result_kind is ResultKind.EMPTY
    assert result.summary.short_video_count == 0
    assert result.summary.file_count == 5


def test_reader_rejects_an_empty_manifest_with_short_video_facts():
    result = DeliveryManifestReader.read(
        manifest_bytes(
            delivery_manifest(
                result_kind="empty",
                short_video_count=1,
            )
        ),
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
    assert result.summary is None


def test_reader_rejects_forged_document_identities():
    manifest = delivery_manifest()
    manifest["documents"]["transcript"]["transcript_id"] = "not-an-id"

    result = DeliveryManifestReader.read(
        manifest_bytes(manifest),
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is DeliveryManifestReadReason.MANIFEST_SCHEMA_INVALID
    assert result.summary is None


def test_reader_distinguishes_a_missing_manifest():
    result = DeliveryManifestReader.read(
        None,
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.INCOMPLETE
    assert result.reason is DeliveryManifestReadReason.MANIFEST_MISSING
    assert result.summary is None


def test_reader_rejects_duplicate_fields_without_echoing_contents():
    secret = "credential-canary-must-not-leak"
    result = DeliveryManifestReader.read(
        (
            '{"schema_version":"delivery_manifest.v1",'
            '"schema_version":"'
            + secret
            + '"}'
        ).encode("utf-8"),
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is (
        DeliveryManifestReadReason.MANIFEST_DUPLICATE_FIELD
    )
    assert secret not in repr(result)


def test_reader_rejects_non_finite_json_numbers():
    result = DeliveryManifestReader.read(
        b'{"source":NaN}',
        expected_run_id=RUN_ID,
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is (
        DeliveryManifestReadReason.MANIFEST_NON_FINITE_NUMBER
    )
    assert result.summary is None


def test_reader_rejects_a_different_run_id():
    result = DeliveryManifestReader.read(
        manifest_bytes(delivery_manifest()),
        expected_run_id=RunId(
            "run_99999999-9999-4999-8999-999999999999"
        ),
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is (
        DeliveryManifestReadReason.MANIFEST_RUN_ID_MISMATCH
    )
    assert result.summary is None


def test_reader_rejects_a_different_expected_result_kind():
    result = DeliveryManifestReader.read(
        manifest_bytes(delivery_manifest()),
        expected_run_id=RUN_ID,
        expected_result_kind=ResultKind.EMPTY,
    )

    assert result.state is DeliveryManifestReadState.CORRUPT
    assert result.reason is (
        DeliveryManifestReadReason.MANIFEST_RESULT_KIND_MISMATCH
    )
    assert result.summary is None
