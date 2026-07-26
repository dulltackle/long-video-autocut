import re
import uuid

import pytest

from video_auto_editor.runtime.identity import (
    BusinessId,
    CandidateId,
    DiagnosticId,
    ErrorId,
    OperationId,
    PlanId,
    RunId,
    SeriesId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)


CANONICAL_UUID4 = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)


def test_new_run_id_uses_the_required_prefix_and_canonical_uuid4():
    run_id = RunId.new()

    value = str(run_id)
    assert re.fullmatch(rf"run_{CANONICAL_UUID4.pattern}", value)

    parsed = uuid.UUID(value.removeprefix("run_"))
    assert parsed.version == 4
    assert parsed.variant == uuid.RFC_4122
    assert str(parsed) == value.removeprefix("run_")


@pytest.mark.parametrize(
    ("identity_type", "prefix"),
    [
        (RunId, "run"),
        (TranscriptId, "transcript"),
        (TranscriptChunkId, "transcript_chunk"),
        (PlanId, "plan"),
        (CandidateId, "candidate"),
        (ShortVideoId, "short_video"),
        (SeriesId, "series"),
    ],
)
def test_each_business_identity_type_uses_its_own_prefix(identity_type, prefix):
    value = str(identity_type.new())

    assert re.fullmatch(rf"{prefix}_{CANONICAL_UUID4.pattern}", value)


@pytest.mark.parametrize(
    "value",
    [
        "candidate_123e4567-e89b-42d3-a456-426614174000",
        "run_123E4567-E89B-42D3-A456-426614174000",
        "run_6ba7b810-9dad-11d1-80b4-00c04fd430c8",
        "run_123e4567e89b42d3a456426614174000",
        "run_{123e4567-e89b-42d3-a456-426614174000}",
        "run_123e4567-e89b-42d3-7456-426614174000",
    ],
)
def test_business_identity_rejects_wrong_type_or_noncanonical_uuid4(value):
    with pytest.raises(ValueError, match="规范小写 UUIDv4"):
        RunId(value)


@pytest.mark.parametrize("identity_type", [OperationId, ErrorId])
def test_diagnostic_identity_types_are_isolated_from_business_identities(identity_type):
    identity = identity_type.new()

    assert isinstance(identity, DiagnosticId)
    assert not isinstance(identity, BusinessId)
    assert uuid.UUID(str(identity)).version == 4


@pytest.mark.parametrize(
    "identity_type",
    [
        RunId,
        TranscriptId,
        TranscriptChunkId,
        PlanId,
        CandidateId,
        ShortVideoId,
        SeriesId,
        OperationId,
        ErrorId,
    ],
)
def test_identity_values_are_hashable_and_cannot_be_mutated(identity_type):
    identity = identity_type.new()

    assert {identity: "value"}[identity] == "value"
    with pytest.raises(AttributeError):
        identity.changed = True


def test_diagnostic_identity_rejects_noncanonical_uuid4():
    with pytest.raises(ValueError, match="规范小写 UUIDv4"):
        ErrorId("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def test_business_identity_rejects_non_string_values_cleanly():
    with pytest.raises(ValueError, match="规范小写 UUIDv4"):
        RunId(123)


def test_business_identity_prefixes_have_one_closed_source_of_truth():
    assert not hasattr(RunId, "prefix")
    assert str(RunId.new()).startswith("run_")
