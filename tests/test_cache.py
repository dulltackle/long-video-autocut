import errno
import hashlib
import json
import multiprocessing
import signal
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock, Thread

import pytest

from video_auto_editor.cache import (
    CacheClaim,
    CachedPayloadInvalid,
    CacheEntrySpec,
    CacheFailure,
    CacheIdentity,
    CacheNamespace,
    CacheObservation,
    CacheOutcome,
    CacheRepository,
)
from video_auto_editor.cache.filesystem import initialize_cache_repository
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.workspace import ManagedPathCapability, Workspace
from video_auto_editor.workspace import _workspace as workspace_module


def test_importing_cache_does_not_load_the_filesystem_adapter():
    completed = subprocess.run(
        (
            sys.executable,
            "-c",
            (
                "import sys\n"
                "import video_auto_editor.cache\n"
                "print('video_auto_editor.cache._filesystem' "
                "in sys.modules)\n"
            ),
        ),
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout == "False\n"


def test_cache_identity_canonicalizes_business_owned_result_inputs():
    first = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version="transcript.identity.v1",
        algorithm_version="transcript.algorithm.v3",
        payload_schema_version="transcript.payload.v2",
        adapter_id="stepaudio",
        model_id="stepaudio-2.5-asr",
        configuration_fingerprint="a" * 64,
        result_inputs={
            "pcm": {"sha256": "b" * 64, "byte_length": 32000},
            "language": "zh",
        },
    )
    same_facts_in_another_mapping_order = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version="transcript.identity.v1",
        algorithm_version="transcript.algorithm.v3",
        payload_schema_version="transcript.payload.v2",
        adapter_id="stepaudio",
        model_id="stepaudio-2.5-asr",
        configuration_fingerprint="a" * 64,
        result_inputs={
            "language": "zh",
            "pcm": {"byte_length": 32000, "sha256": "b" * 64},
        },
    )

    assert first == same_facts_in_another_mapping_order
    assert first.digest == same_facts_in_another_mapping_order.digest
    assert len(first.digest) == 64
    assert "pcm" not in repr(first)
    assert "zh" not in repr(first)
    assert not hasattr(first, "_document")


@pytest.mark.parametrize(
    "application_version",
    [
        "release!candidate",
        "https://supplier.example/version",
    ],
)
def test_repository_rejects_application_versions_the_envelope_cannot_validate(
    application_version,
):
    with pytest.raises(ValueError, match="程序版本"):
        CacheRepository.in_memory(
            application_version=application_version,
        )


def test_repository_and_claim_cannot_be_constructed_outside_factories():
    repository = CacheRepository.in_memory(application_version="4.7.0")

    assert not hasattr(CacheRepository, "_create")
    assert not hasattr(CacheRepository, "initialize")
    assert not hasattr(CacheClaim, "_create")
    with pytest.raises(TypeError, match="只能由"):
        CacheRepository(
            object(),
            application_version="4.7.0",
            clock=lambda: None,
        )
    with pytest.raises(TypeError, match="只能由"):
        CacheClaim(repository, _text_entry().identity, 0)


def test_filesystem_repository_requires_an_authentic_cache_capability(
    tmp_path,
):
    with pytest.raises(TypeError, match="受管目录 capability"):
        initialize_cache_repository(
            tmp_path,
            application_version="4.7.0",
        )

    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    with workspace.acquire_run(RunId.new()) as run_workspace:
        with pytest.raises(ValueError, match="缓存目录 capability"):
            initialize_cache_repository(
                run_workspace.diagnostics,
                application_version="4.7.0",
            )


@pytest.mark.parametrize(
    ("namespace", "result_inputs"),
    [
        (
            CacheNamespace.TRANSCRIPT,
            {
                "pcm": {"sha256": "1" * 64, "byte_length": 32000},
                "language": "zh",
                "audio_rule_version": "pcm.v1",
                "shard_plan_version": "core.v2",
                "core_region_version": "core-region.v2",
                "merge_version": "merge.v4",
                "speech_evidence_version": "speech.v2",
                "coverage_version": "coverage.v3",
                "deduplication_version": "dedup.v2",
                "validation_version": "transcript-validation.v2",
            },
        ),
        (
            CacheNamespace.TRANSCRIPTION_SHARD,
            {
                "pcm": {"sha256": "2" * 64, "sample_length": 16000},
                "language": "zh",
                "request_version": "stepaudio-request.v2",
                "response_parser_version": "stepaudio-response.v3",
                "validation_version": "shard-validation.v2",
            },
        ),
        (
            CacheNamespace.TOPIC_REVIEW,
            {
                "candidate_batch": [{"candidate_id": "candidate-1"}],
                "neighboring_context": {"before": [], "after": []},
                "course_context_sha256": "3" * 64,
                "review_constraints": {"publish_ready_threshold": 80},
                "batch_algorithm_version": "batch.v2",
                "prompt_sha256": "4" * 64,
                "prompt_version": "topic-prompt.v3",
                "semantic_retry_version": "semantic-retry.v2",
                "model_settings": {"temperature": 0.2},
                "parser_version": "topic-parser.v2",
                "validation_version": "topic-validation.v3",
            },
        ),
        (
            CacheNamespace.SUBTITLE_OPTIMIZATION,
            {
                "source_text": "忠实转写原文",
                "display_constraints": {
                    "max_chars_per_line": 15,
                    "max_lines": 2,
                },
                "prompt_sha256": "5" * 64,
                "prompt_version": "subtitle-prompt.v3",
                "semantic_retry_version": "semantic-retry.v2",
                "model_settings": {"temperature": 0.1},
                "parser_version": "subtitle-parser.v2",
                "subsequence_validation_version": "subsequence.v3",
            },
        ),
    ],
)
def test_each_namespace_identity_covers_every_business_supplied_result_input(
    namespace,
    result_inputs,
):
    baseline = CacheIdentity.create(
        namespace=namespace,
        identity_schema_version=f"{namespace.value}.identity.v1",
        algorithm_version=f"{namespace.value}.algorithm.v1",
        payload_schema_version=f"{namespace.value}.payload.v1",
        adapter_id="deterministic",
        model_id="model-v1",
        configuration_fingerprint="a" * 64,
        result_inputs=result_inputs,
    )

    changed_digests = {
        CacheIdentity.create(
            namespace=namespace,
            identity_schema_version=f"{namespace.value}.identity.v1",
            algorithm_version=f"{namespace.value}.algorithm.v1",
            payload_schema_version=f"{namespace.value}.payload.v1",
            adapter_id="deterministic",
            model_id="model-v1",
            configuration_fingerprint="a" * 64,
            result_inputs={
                **result_inputs,
                changed_field: {"changed": True},
            },
        ).digest
        for changed_field in result_inputs
    }

    assert baseline.digest not in changed_digests
    assert len(changed_digests) == len(result_inputs)


def test_identity_excludes_runtime_provenance_and_versions_each_namespace():
    result_inputs = {"content_sha256": "b" * 64}

    def business_identity(
        namespace,
        *,
        algorithm_version="algorithm.v1",
        payload_schema_version="payload.v1",
        **_runtime_provenance,
    ):
        return CacheIdentity.create(
            namespace=namespace,
            identity_schema_version="identity.v1",
            algorithm_version=algorithm_version,
            payload_schema_version=payload_schema_version,
            adapter_id="deterministic",
            model_id="model-v1",
            configuration_fingerprint="a" * 64,
            result_inputs=result_inputs,
        )

    first_run = business_identity(
        CacheNamespace.TRANSCRIPT,
        run_id="run-1",
        workspace="/workspace/one",
        timeout_seconds=30,
        retry_count=1,
        application_version="4.7.0",
    )
    another_runtime = business_identity(
        CacheNamespace.TRANSCRIPT,
        run_id="run-2",
        workspace="/workspace/two",
        timeout_seconds=300,
        retry_count=9,
        application_version="9.0.0",
    )

    assert first_run == another_runtime
    assert (
        business_identity(CacheNamespace.TRANSCRIPTION_SHARD).digest
        != first_run.digest
    )
    assert (
        business_identity(
            CacheNamespace.TRANSCRIPT,
            algorithm_version="algorithm.v2",
        ).digest
        != first_run.digest
    )
    assert (
        business_identity(
            CacheNamespace.TRANSCRIPT,
            payload_schema_version="payload.v2",
        ).digest
        != first_run.digest
    )


def _text_entry(
    pcm_sha256: str = "b" * 64,
) -> CacheEntrySpec[str]:
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version="transcript.identity.v1",
        algorithm_version="transcript.algorithm.v3",
        payload_schema_version="transcript.payload.v2",
        adapter_id="stepaudio",
        model_id="stepaudio-2.5-asr",
        configuration_fingerprint="a" * 64,
        result_inputs={"pcm_sha256": pcm_sha256},
    )

    def decode(payload):
        if (
            not isinstance(payload, dict)
            or set(payload) != {"text"}
            or not isinstance(payload["text"], str)
            or not payload["text"]
        ):
            raise CachedPayloadInvalid("cache.payload_invalid")
        return payload["text"]

    return CacheEntrySpec(
        identity=identity,
        encode=lambda value: {"text": value},
        decode=decode,
    )


def test_in_memory_repository_reuses_only_a_validated_complete_result():
    repository = CacheRepository.in_memory(application_version="4.7.0")
    entry = _text_entry()
    token = CancellationSource().token
    computations = []

    miss = repository.lookup(entry, cancellation=token)
    first = repository.resolve(
        entry,
        cancellation=token,
        compute=lambda: computations.append("computed") or "完整转写",
    )
    second = repository.resolve(
        entry,
        cancellation=token,
        compute=lambda: computations.append("unexpected") or "错误结果",
    )

    assert miss.observation.outcome is CacheOutcome.MISS
    assert first.value == "完整转写"
    assert first.from_cache is False
    assert second.value == "完整转写"
    assert second.from_cache is True
    assert computations == ["computed"]


class _BusinessComputationFailed(RuntimeError):
    pass


def _assert_business_computation_failure_is_not_reclassified(repository):
    entry = _text_entry()
    token = CancellationSource().token

    def fail():
        raise _BusinessComputationFailed("业务端口失败")

    with pytest.raises(_BusinessComputationFailed, match="业务端口失败"):
        repository.resolve(
            entry,
            cancellation=token,
            compute=fail,
        )

    assert (
        repository.lookup(entry, cancellation=token).observation.outcome
        is CacheOutcome.MISS
    )


def test_in_memory_repository_preserves_business_computation_failures():
    _assert_business_computation_failure_is_not_reclassified(
        CacheRepository.in_memory(application_version="4.7.0")
    )


def test_filesystem_repository_preserves_business_computation_failures(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        _assert_business_computation_failure_is_not_reclassified(
            initialize_cache_repository(
                run_workspace.cache,
                application_version="4.7.0",
            )
        )


def test_business_validation_failure_never_publishes_a_partial_result():
    identity = _text_entry().identity

    def decode(payload):
        if payload != {"complete": True, "value": "完整结果"}:
            raise CachedPayloadInvalid("cache.payload_invalid")
        return payload["value"]

    entry = CacheEntrySpec(
        identity=identity,
        encode=lambda value: {"complete": False, "value": value},
        decode=decode,
    )
    repository = CacheRepository.in_memory(application_version="4.7.0")
    token = CancellationSource().token

    with pytest.raises(
        CachedPayloadInvalid,
        match="cache.payload_invalid",
    ):
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "部分结果",
        )

    assert repository.lookup(
        entry,
        cancellation=token,
    ).observation.outcome is CacheOutcome.MISS


def test_claim_requires_a_same_identity_requery_before_publication():
    repository = CacheRepository.in_memory(application_version="4.7.0")
    entry = _text_entry()
    token = CancellationSource().token

    with pytest.raises(RuntimeError, match="重新查询"):
        repository.claim(
            entry.identity,
            cancellation=token,
            effect=lambda claim: claim.publish(entry, "不能直接发布"),
        )

    publication = repository.claim(
        entry.identity,
        cancellation=token,
        effect=lambda claim: (
            claim.lookup(entry),
            claim.publish(entry, "完整转写"),
        )[1],
    )

    assert publication.observation.outcome is CacheOutcome.WRITE_PUBLISHED
    assert repository.lookup(entry, cancellation=token).value == "完整转写"


def test_claim_expires_after_callback_and_public_capabilities_reject_mutation():
    repository = CacheRepository.in_memory(application_version="4.7.0")
    entry = _text_entry()
    held_claims = []

    repository.claim(
        entry.identity,
        cancellation=CancellationSource().token,
        effect=lambda claim: (
            held_claims.append(claim),
            claim.lookup(entry),
        )[1],
    )
    claim = held_claims[0]

    with pytest.raises(RuntimeError, match="作用域已经结束"):
        claim.lookup(entry)
    with pytest.raises(TypeError, match="不可修改"):
        claim._active = True
    with pytest.raises(TypeError, match="不可修改"):
        repository._store = object()


def test_publishing_after_a_valid_requery_is_idempotent_and_immutable():
    repository = CacheRepository.in_memory(application_version="4.7.0")
    entry = _text_entry()
    token = CancellationSource().token
    repository.resolve(
        entry,
        cancellation=token,
        compute=lambda: "不可变完整转写",
    )

    publication = repository.claim(
        entry.identity,
        cancellation=token,
        effect=lambda claim: (
            claim.lookup(entry),
            claim.publish(entry, "不得覆盖的另一份结果"),
        )[1],
    )

    assert (
        publication.observation.outcome
        is CacheOutcome.WRITE_ALREADY_PRESENT
    )
    assert repository.lookup(
        entry,
        cancellation=token,
    ).value == "不可变完整转写"


def test_filesystem_repository_persists_only_a_redacted_envelope_at_fixed_layout(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    base_entry = _text_entry()
    identity = CacheIdentity.create(
        namespace=CacheNamespace.TRANSCRIPT,
        identity_schema_version="transcript.identity.v1",
        algorithm_version="transcript.algorithm.v3",
        payload_schema_version="transcript.payload.v2",
        adapter_id="stepaudio",
        model_id="stepaudio-2.5-asr",
        configuration_fingerprint="a" * 64,
        result_inputs={
            "pcm_sha256": "b" * 64,
            "source_path": "/srv/private/course.mp4",
            "course_context": "课程正文绝密",
            "full_prompt": "完整提示绝密",
            "provider_url": "https://supplier.example/v1",
            "credential": "sk-never-persist",
            "run_id": "run-never-persist",
        },
    )
    entry = CacheEntrySpec(
        identity=identity,
        encode=base_entry.encode,
        decode=base_entry.decode,
    )

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        result = repository.resolve(
            entry,
            cancellation=CancellationSource().token,
            compute=lambda: "完整转写",
        )

    envelope_path = (
        workspace.root
        / "work"
        / "cache"
        / "transcript"
        / identity.digest[:2]
        / f"{identity.digest}.json"
    )
    persisted = envelope_path.read_bytes()

    assert result.value == "完整转写"
    assert envelope_path.is_file()
    assert b"/srv/private/course.mp4" not in persisted
    assert "课程正文绝密".encode() not in persisted
    assert "完整提示绝密".encode() not in persisted
    assert b"https://supplier.example/v1" not in persisted
    assert b"sk-never-persist" not in persisted
    assert b"run-never-persist" not in persisted


def test_filesystem_repository_does_not_migrate_or_read_retired_live_caches(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    computations = []
    legacy_payload = b'{"chunks":[{"text":"legacy"}],"version":1}'

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        envelope_path.parent.mkdir()
        envelope_path.write_bytes(legacy_payload)

        resolution = repository.resolve(
            entry,
            cancellation=CancellationSource().token,
            compute=lambda: computations.append("computed") or "当前完整转写",
        )

    quarantined = list(
        (
            workspace.root
            / "work"
            / "cache"
            / ".quarantine"
            / "transcript"
            / entry.identity.digest[:2]
        ).glob(f"{entry.identity.digest}.cache.envelope_schema_invalid.*.json")
    )

    assert resolution.value == "当前完整转写"
    assert resolution.from_cache is False
    assert computations == ["computed"]
    assert [item.outcome for item in resolution.observations] == [
        CacheOutcome.CORRUPT_QUARANTINED,
        CacheOutcome.MISS,
        CacheOutcome.WRITE_PUBLISHED,
    ]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == legacy_payload
    assert envelope_path.read_bytes() != legacy_payload


def test_filesystem_valid_entry_is_private_durable_and_never_replaced(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "耐久完整转写",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        lock_path = (
            workspace.root
            / "work"
            / "cache"
            / ".locks"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.lock"
        )
        original = envelope_path.read_bytes()
        original_inode = envelope_path.stat().st_ino

        second = repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: pytest.fail("有效缓存不得重新计算"),
        )

    assert second.value == "耐久完整转写"
    assert envelope_path.read_bytes() == original
    assert envelope_path.stat().st_ino == original_inode
    assert stat.S_IMODE(envelope_path.stat().st_mode) == 0o600
    assert lock_path.is_file()
    assert not list(envelope_path.parent.glob(".workspace-create-*"))


def test_producer_application_version_is_provenance_not_cache_identity(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        ).resolve(
            entry,
            cancellation=token,
            compute=lambda: "跨程序版本复用的完整转写",
        )
        resolution = initialize_cache_repository(
            run_workspace.cache,
            application_version="5.0.0",
        ).resolve(
            entry,
            cancellation=token,
            compute=lambda: pytest.fail("程序版本不得自动导致失效"),
        )

    assert resolution.value == "跨程序版本复用的完整转写"
    assert resolution.from_cache is True


def test_corrupt_filesystem_entry_is_quarantined_under_claim_and_recomputed(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token
    computations = []

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "初次完整转写",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        envelope_path.write_bytes(b"{broken")

        recovered = repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: computations.append("recomputed")
            or "重新完整转写",
        )

    quarantined = list(
        (
            workspace.root
            / "work"
            / "cache"
            / ".quarantine"
            / "transcript"
            / entry.identity.digest[:2]
        ).glob(f"{entry.identity.digest}.cache.json_invalid.*.json")
    )

    assert recovered.value == "重新完整转写"
    assert computations == ["recomputed"]
    assert [
        observation.outcome for observation in recovered.observations
    ] == [
        CacheOutcome.CORRUPT_QUARANTINED,
        CacheOutcome.MISS,
        CacheOutcome.WRITE_PUBLISHED,
    ]
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == b"{broken"
    assert envelope_path.read_bytes() != b"{broken"


def test_deeply_nested_json_is_quarantined_as_corrupt_and_recomputed(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "初次完整转写",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        nested = b"[" * 20_000 + b"0" + b"]" * 20_000
        envelope_path.write_bytes(nested)

        recovered = repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "递归损坏后的完整转写",
        )

    quarantined = list(
        (
            workspace.root
            / "work"
            / "cache"
            / ".quarantine"
            / "transcript"
            / entry.identity.digest[:2]
        ).glob(f"{entry.identity.digest}.cache.json_invalid.*.json")
    )
    assert recovered.value == "递归损坏后的完整转写"
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == nested


def _assert_formal_cache_failure(
    captured,
    *,
    operation,
    reason_code,
):
    failure = captured.value
    assert failure.error_code is ErrorCode.CACHE_INFRASTRUCTURE_FAILED
    assert dict(failure.diagnostics) == {
        "operation": operation,
        "reason_code": reason_code,
    }
    assert failure.__cause__ is None
    assert failure.__context__ is None


def test_filesystem_permission_failure_is_not_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )

        def deny_read(_location):
            raise PermissionError(errno.EACCES, "injected")

        monkeypatch.setattr(ManagedPathCapability, "read_bytes", deny_read)

        with pytest.raises(CacheFailure) as captured:
            repository.lookup(
                _text_entry(),
                cancellation=CancellationSource().token,
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.read",
        reason_code="cache.permission_denied",
    )


def test_filesystem_missing_managed_parent_is_not_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )

        def missing_parent(_location):
            raise FileNotFoundError(errno.ENOENT, "injected")

        monkeypatch.setattr(ManagedPathCapability, "mkdir", missing_parent)

        with pytest.raises(CacheFailure) as captured:
            repository.lookup(
                _text_entry(),
                cancellation=CancellationSource().token,
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.read",
        reason_code="cache.read_failed",
    )


def test_filesystem_disk_full_failure_is_not_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.claim(
            entry.identity,
            cancellation=token,
            effect=lambda claim: claim.lookup(entry),
        )
        original_publish = ManagedPathCapability.publish_bytes_atomically

        def fail_entry_publish(location, contents):
            if location._relative_parts[-1].endswith(".json"):
                raise OSError(errno.ENOSPC, "injected")
            return original_publish(location, contents)

        monkeypatch.setattr(
            ManagedPathCapability,
            "publish_bytes_atomically",
            fail_entry_publish,
        )

        with pytest.raises(CacheFailure) as captured:
            repository.resolve(
                entry,
                cancellation=token,
                compute=lambda: "不得伪装成未命中",
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.publish",
        reason_code="cache.disk_full",
    )


def test_filesystem_data_sync_failure_is_a_formal_cache_failure(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.claim(
            entry.identity,
            cancellation=token,
            effect=lambda claim: claim.lookup(entry),
        )

        def fail_data_sync(_descriptor):
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(
            workspace_module,
            "_sync_file_data",
            fail_data_sync,
        )

        with pytest.raises(CacheFailure) as captured:
            repository.resolve(
                entry,
                cancellation=token,
                compute=lambda: "同步失败不得成功",
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.publish",
        reason_code="cache.write_failed",
    )


def test_filesystem_lock_failure_is_not_a_cache_miss(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )

        def fail_lock(_location, _cancellation, _effect):
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(
            ManagedPathCapability,
            "with_exclusive_cache_lock",
            fail_lock,
        )

        with pytest.raises(CacheFailure) as captured:
            repository.resolve(
                _text_entry(),
                cancellation=CancellationSource().token,
                compute=lambda: "锁失败时不得计算",
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.claim",
        reason_code="cache.lock_failed",
    )


def test_filesystem_quarantine_failure_is_not_recomputed_as_a_miss(
    tmp_path,
    monkeypatch,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token
    computations = []

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "初次完整转写",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        envelope_path.write_bytes(b"{broken")

        def fail_quarantine(_source, _destination):
            raise OSError(errno.EIO, "injected")

        monkeypatch.setattr(
            ManagedPathCapability,
            "quarantine_to",
            fail_quarantine,
        )

        with pytest.raises(CacheFailure) as captured:
            repository.resolve(
                entry,
                cancellation=token,
                compute=lambda: computations.append("unexpected")
                or "不得重算",
            )

    _assert_formal_cache_failure(
        captured,
        operation="cache.quarantine",
        reason_code="cache.quarantine_failed",
    )
    assert computations == []
    assert envelope_path.read_bytes() == b"{broken"


@pytest.mark.parametrize(
    ("corruption", "expected_reason"),
    [
        ("identity", "cache.identity_mismatch"),
        ("algorithm", "cache.algorithm_mismatch"),
        ("payload_digest", "cache.payload_digest_mismatch"),
        ("payload_length", "cache.payload_length_mismatch"),
        ("business_payload", "cache.payload_invalid"),
        ("timestamp", "cache.envelope_schema_invalid"),
        ("unknown_field", "cache.envelope_schema_invalid"),
    ],
)
def test_envelope_strictly_rejects_identity_algorithm_and_payload_corruption(
    tmp_path,
    corruption,
    expected_reason,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    token = CancellationSource().token

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "初次完整转写",
        )
        envelope_path = (
            workspace.root
            / "work"
            / "cache"
            / "transcript"
            / entry.identity.digest[:2]
            / f"{entry.identity.digest}.json"
        )
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
        if corruption == "identity":
            envelope["identity"]["sha256"] = "0" * 64
        elif corruption == "algorithm":
            envelope["algorithm_version"] = "transcript.algorithm.v999"
        elif corruption == "payload_digest":
            envelope["payload"]["sha256"] = "0" * 64
        elif corruption == "payload_length":
            envelope["payload"]["byte_length"] += 1
        elif corruption == "business_payload":
            payload = {"text": ""}
            payload_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            envelope["payload"]["value"] = payload
            envelope["payload"]["byte_length"] = len(payload_bytes)
            envelope["payload"]["sha256"] = hashlib.sha256(
                payload_bytes
            ).hexdigest()
        elif corruption == "timestamp":
            envelope["created_at"] = "2026-01-01T00:00:00Z"
        else:
            envelope["absolute_path"] = "/must/not/be/accepted"
        envelope_path.write_bytes(
            json.dumps(
                envelope,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode()
            + b"\n"
        )

        recovered = repository.resolve(
            entry,
            cancellation=token,
            compute=lambda: "重新完整转写",
        )

    assert recovered.value == "重新完整转写"
    assert recovered.observations[0] == CacheObservation(
        namespace=CacheNamespace.TRANSCRIPT,
        outcome=CacheOutcome.CORRUPT_QUARANTINED,
        singleflight_wait_ms=recovered.observations[0].singleflight_wait_ms,
        reason_code=expected_reason,
        quarantine_digest_prefix=(
            f"sha256:{entry.identity.digest[:16]}"
        ),
    )


def _assert_thread_singleflight(repository, entry):
    worker_count = 8
    start = Barrier(worker_count)
    counter_lock = Lock()
    computation_count = 0
    token = CancellationSource().token

    def compute():
        nonlocal computation_count
        with counter_lock:
            computation_count += 1
        time.sleep(0.05)
        return "线程共享完整转写"

    def resolve():
        start.wait()
        return repository.resolve(
            entry,
            cancellation=token,
            compute=compute,
        )

    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        resolutions = list(executor.map(lambda _index: resolve(), range(8)))

    assert computation_count == 1
    assert {resolution.value for resolution in resolutions} == {
        "线程共享完整转写"
    }
    assert sum(resolution.from_cache for resolution in resolutions) == 7


def test_in_memory_repository_singleflights_same_identity_across_threads():
    _assert_thread_singleflight(
        CacheRepository.in_memory(application_version="4.7.0"),
        _text_entry(),
    )


def test_filesystem_repository_singleflights_same_identity_across_threads(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        _assert_thread_singleflight(
            initialize_cache_repository(
                run_workspace.cache,
                application_version="4.7.0",
            ),
            _text_entry(),
        )


def test_filesystem_repository_singleflights_same_identity_across_processes(
    tmp_path,
):
    context = multiprocessing.get_context("fork")
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")
    entry = _text_entry()
    worker_count = 4
    start = context.Barrier(worker_count)
    computation_count = context.Value("i", 0)
    results = context.Queue()

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )

        def worker():
            try:
                start.wait(timeout=5)

                def compute():
                    with computation_count.get_lock():
                        computation_count.value += 1
                    time.sleep(0.1)
                    return "进程共享完整转写"

                resolution = repository.resolve(
                    entry,
                    cancellation=CancellationSource().token,
                    compute=compute,
                )
                results.put(
                    ("ok", resolution.value, resolution.from_cache)
                )
            except BaseException as exc:
                results.put(
                    (
                        "error",
                        type(exc).__name__,
                        str(exc),
                        dict(getattr(exc, "diagnostics", {})),
                    )
                )

        processes = [
            context.Process(target=worker) for _ in range(worker_count)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=10)

        messages = [results.get(timeout=2) for _ in range(worker_count)]

    assert all(not process.is_alive() for process in processes)
    assert [process.exitcode for process in processes] == [0] * worker_count
    assert computation_count.value == 1
    assert all(
        message[0:2] == ("ok", "进程共享完整转写")
        for message in messages
    ), messages
    assert sum(message[2] for message in messages) == worker_count - 1


def _assert_different_identities_compute_concurrently(repository):
    entries = (_text_entry("b" * 64), _text_entry("c" * 64))
    rendezvous = Barrier(2)
    token = CancellationSource().token

    def resolve(index):
        return repository.resolve(
            entries[index],
            cancellation=token,
            compute=lambda: (
                rendezvous.wait(timeout=2),
                f"独立结果-{index}",
            )[1],
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolutions = list(executor.map(resolve, range(2)))

    assert [resolution.value for resolution in resolutions] == [
        "独立结果-0",
        "独立结果-1",
    ]


def test_in_memory_repository_does_not_serialize_different_identities():
    _assert_different_identities_compute_concurrently(
        CacheRepository.in_memory(application_version="4.7.0")
    )


def test_filesystem_repository_does_not_serialize_different_identities(
    tmp_path,
):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        _assert_different_identities_compute_concurrently(
            initialize_cache_repository(
                run_workspace.cache,
                application_version="4.7.0",
            )
        )


def _assert_waiting_claim_is_cancellable(repository):
    entry = _text_entry()
    holder_started = Event()
    release_holder = Event()
    holder_token = CancellationSource().token
    waiting_source = CancellationSource()
    computations = []

    def hold_claim():
        repository.claim(
            entry.identity,
            cancellation=holder_token,
            effect=lambda _claim: (
                holder_started.set(),
                release_holder.wait(timeout=5),
            )[1],
        )

    holder = Thread(target=hold_claim)
    holder.start()
    assert holder_started.wait(timeout=2)

    with ThreadPoolExecutor(max_workers=1) as executor:
        waiting = executor.submit(
            repository.resolve,
            entry,
            cancellation=waiting_source.token,
            compute=lambda: computations.append("unexpected")
            or "不应计算",
        )
        time.sleep(0.05)
        waiting_source.request(signal.SIGTERM)
        with pytest.raises(CancellationRequested):
            waiting.result(timeout=2)

    release_holder.set()
    holder.join(timeout=2)

    assert not holder.is_alive()
    assert computations == []


def test_in_memory_claim_wait_is_cancellable():
    _assert_waiting_claim_is_cancellable(
        CacheRepository.in_memory(application_version="4.7.0")
    )


def test_filesystem_claim_wait_is_cancellable(tmp_path):
    source = tmp_path / "course.mp4"
    source.write_bytes(b"source")
    workspace = Workspace.open(source, tmp_path / "workspace")

    with workspace.acquire_run(RunId.new()) as run_workspace:
        _assert_waiting_claim_is_cancellable(
            initialize_cache_repository(
                run_workspace.cache,
                application_version="4.7.0",
            )
        )


def test_cancellation_after_compute_prevents_publication():
    repository = CacheRepository.in_memory(application_version="4.7.0")
    entry = _text_entry()
    source = CancellationSource()

    def compute():
        source.request(signal.SIGINT)
        return "取消后不得发布"

    with pytest.raises(CancellationRequested):
        repository.resolve(
            entry,
            cancellation=source.token,
            compute=compute,
        )

    assert repository.lookup(
        entry,
        cancellation=CancellationSource().token,
    ).observation.outcome is CacheOutcome.MISS


def test_cancellation_after_atomic_publication_is_reported_but_entry_survives(
    tmp_path,
    monkeypatch,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"source")
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    entry = _text_entry()
    cancellation = CancellationSource()

    with workspace.acquire_run(RunId.new()) as run_workspace:
        repository = initialize_cache_repository(
            run_workspace.cache,
            application_version="4.7.0",
        )
        original_publish = ManagedPathCapability.publish_bytes_atomically

        def publish_then_cancel(location, contents):
            written = original_publish(location, contents)
            if location._relative_parts[-1].endswith(".json"):
                cancellation.request(signal.SIGTERM)
            return written

        monkeypatch.setattr(
            ManagedPathCapability,
            "publish_bytes_atomically",
            publish_then_cancel,
        )

        with pytest.raises(CancellationRequested):
            repository.resolve(
                entry,
                cancellation=cancellation.token,
                compute=lambda: "已原子发布的完整转写",
            )

        cached = repository.lookup(
            entry,
            cancellation=CancellationSource().token,
        )

    assert cached.value == "已原子发布的完整转写"
    assert cached.observation.outcome is CacheOutcome.HIT
