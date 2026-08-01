import signal
from itertools import pairwise

import pytest

from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import RunId
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.transcription import (
    CacheUse,
    CharacterSpan,
    ExecutionFacts,
    ReadinessReport,
    SpeechPresence,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
)
from video_auto_editor.transcription.reconciliation import (
    RecognitionBatch,
    RecognitionKind,
    RecognitionObservation,
    RecognitionWork,
    ReconciledSpeechRecognition,
    TimeInterval,
    complete_transcription,
)
from video_auto_editor.workspace import Workspace


def _character_spans(
    start_ms: int,
    end_ms: int,
    text: str,
) -> tuple[CharacterSpan, ...]:
    width, remainder = divmod(end_ms - start_ms, len(text))
    spans = []
    cursor = start_ms
    for index, _character in enumerate(text):
        next_cursor = cursor + width + (1 if index < remainder else 0)
        spans.append(CharacterSpan(start_ms=cursor, end_ms=next_cursor))
        cursor = next_cursor
    return tuple(spans)


def _transcription_request(tmp_path, *, duration_ms: int):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"verified source")
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    run_workspace = workspace.acquire_run(RunId.new())
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=len(b"verified source"),
        duration_ms=duration_ms,
    )
    return (
        TranscriptionRequest(
            source=source,
            temporary_workspace=run_workspace.temporary,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        ),
        run_workspace,
    )


class _StaticSpeechEvidence:
    def __init__(self, intervals: tuple[TimeInterval, ...]) -> None:
        self._intervals = intervals

    def confirmed_speech(
        self,
        request: TranscriptionRequest,
    ) -> tuple[TimeInterval, ...]:
        request.cancellation.raise_if_cancelled()
        return self._intervals


class _EquivalentOverlapSource:
    def __init__(self) -> None:
        self.calls: list[tuple[RecognitionWork, ...]] = []
        measured_text = "缓存失效，"
        self.measured_chunk = TranscriptionChunk(
            start_ms=176_000,
            end_ms=183_800,
            text=measured_text,
            character_spans=_character_spans(
                176_000,
                183_800,
                measured_text,
            ),
        )

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation,
    ) -> tuple[RecognitionObservation, ...]:
        cancellation.raise_if_cancelled()
        self.calls.append(works)
        first, second = works
        observations = (
            RecognitionObservation(
                work=first,
                chunks=(self.measured_chunk,),
            ),
            RecognitionObservation(
                work=second,
                chunks=(
                    TranscriptionChunk(
                        start_ms=178_000,
                        end_ms=184_000,
                        text="缓存 失效",
                    ),
                ),
            ),
        )
        return tuple(reversed(observations))


def test_complete_transcription_partitions_core_and_keeps_one_real_equivalent_response():
    source = _EquivalentOverlapSource()

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(176_000, 183_800),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert [
        (
            work.kind,
            work.core.start_ms,
            work.core.end_ms,
            work.request.start_ms,
            work.request.end_ms,
        )
        for work in source.calls[0]
    ] == [
        (RecognitionKind.PRIMARY, 0, 180_000, 0, 185_000),
        (
            RecognitionKind.PRIMARY,
            180_000,
            200_000,
            175_000,
            200_000,
        ),
    ]
    assert len(source.calls) == 1
    assert result.speech_presence is SpeechPresence.PRESENT
    assert result.chunks == (source.measured_chunk,)
    assert result.chunks[0] is source.measured_chunk


def test_complete_transcription_aggregates_successful_transport_retries():
    chunk = TranscriptionChunk(
        start_ms=1_000,
        end_ms=9_000,
        text="瞬时传输恢复后仍返回完整转写",
    )

    class _RetriedObservationSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            return RecognitionBatch(
                observations=(
                    RecognitionObservation(work=works[0], chunks=(chunk,)),
                ),
                retry_count=1,
            )

    result = complete_transcription(
        source_duration_ms=10_000,
        confirmed_speech=(TimeInterval(1_000, 9_000),),
        observation_source=_RetriedObservationSource(),
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert result.execution_facts == ExecutionFacts(
        cache_use=CacheUse.MISS,
        retry_count=1,
    )


def test_equivalent_measured_sequences_prefer_edge_distance_not_chunk_count():
    combined = TranscriptionChunk(
        177_000,
        182_900,
        "甲乙",
        _character_spans(177_000, 182_900, "甲乙"),
    )
    split = (
        TranscriptionChunk(
            177_200,
            182_800,
            "甲",
            _character_spans(177_200, 182_800, "甲"),
        ),
        TranscriptionChunk(
            182_800,
            183_000,
            "乙",
            _character_spans(182_800, 183_000, "乙"),
        ),
    )

    class _MeasuredSegmentationSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            first, second = works
            return (
                RecognitionObservation(first, (combined,)),
                RecognitionObservation(second, split),
            )

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(177_000, 183_000),),
        observation_source=_MeasuredSegmentationSource(),
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert result.chunks == (combined,)
    assert result.chunks[0] is combined


def test_complete_transcription_does_not_let_context_own_another_core_output():
    context_text = "核心区真实候选，"
    context_candidate = TranscriptionChunk(
        181_000,
        184_000,
        context_text,
        _character_spans(181_000, 184_000, context_text),
    )
    owned_candidate = TranscriptionChunk(
        181_000,
        184_000,
        "核心区真实候选",
    )

    class _CoreOwnershipSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            first, second = works
            return (
                RecognitionObservation(
                    work=first,
                    chunks=(context_candidate,),
                ),
                RecognitionObservation(
                    work=second,
                    chunks=(owned_candidate,),
                ),
            )

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(181_000, 184_000),),
        observation_source=_CoreOwnershipSource(),
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert result.chunks == (owned_candidate,)
    assert result.chunks[0] is owned_candidate


def test_complete_transcription_keeps_context_visible_for_a_boundary_conflict():
    left_context = TranscriptionChunk(
        178_000,
        182_000,
        "缓存失效",
    )
    right_owned = TranscriptionChunk(
        178_000,
        182_000,
        "缓存生效",
    )
    recovered = TranscriptionChunk(
        178_000,
        182_000,
        "缓存失效",
    )

    class _BoundaryConflictSource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            if len(self.calls) == 1:
                first, second = works
                return (
                    RecognitionObservation(
                        work=first,
                        chunks=(left_context,),
                    ),
                    RecognitionObservation(
                        work=second,
                        chunks=(right_owned,),
                    ),
                )
            (recovery,) = works
            return (
                RecognitionObservation(
                    work=recovery,
                    chunks=(recovered,),
                ),
            )

    source = _BoundaryConflictSource()

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(178_000, 182_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert len(source.calls) == 2
    assert result.chunks == (recovered,)
    assert result.chunks[0] is recovered
    assert result.execution_facts.recovery_count == 1


def test_low_ratio_cross_core_overlap_still_requires_targeted_consensus():
    left_answer = TranscriptionChunk(0, 185_000, "左侧长句")
    right_answer = TranscriptionChunk(175_000, 200_000, "右侧异文")

    class _LowRatioOverlapSource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            if len(self.calls) == 1:
                first, second = works
                return (
                    RecognitionObservation(first, (left_answer,)),
                    RecognitionObservation(second, (right_answer,)),
                )
            (recovery,) = works
            return (RecognitionObservation(recovery, ()),)

    source = _LowRatioOverlapSource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=200_000,
            confirmed_speech=(TimeInterval(175_000, 185_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert len(source.calls) == 2
    (recovery,) = source.calls[1]
    assert recovery.core == TimeInterval(175_000, 185_000)
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 10_000,
        "reason_code": "coverage.no_progress",
    }


class _ShortTailRecoverySource:
    def __init__(self) -> None:
        self.calls: list[tuple[RecognitionWork, ...]] = []
        self.primary_chunks = (
            TranscriptionChunk(
                start_ms=0,
                end_ms=175_000,
                text="已完整识别的前段",
            ),
            TranscriptionChunk(
                start_ms=175_000,
                end_ms=230_000,
                text="供应商在这里提前停止",
            ),
        )
        self.recovered_chunk = TranscriptionChunk(
            start_ms=230_000,
            end_ms=245_000,
            text="十五秒尾部语音被补回",
        )

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation,
    ) -> tuple[RecognitionObservation, ...]:
        cancellation.raise_if_cancelled()
        self.calls.append(works)
        if len(self.calls) == 1:
            first, second = works
            return (
                RecognitionObservation(
                    work=first,
                    chunks=(self.primary_chunks[0],),
                ),
                RecognitionObservation(
                    work=second,
                    chunks=(self.primary_chunks[1],),
                ),
            )
        assert len(self.calls) == 2
        (recovery,) = works
        return (
            RecognitionObservation(
                work=recovery,
                chunks=(self.recovered_chunk,),
            ),
        )


def test_complete_transcription_recovers_a_confirmed_short_tail_gap():
    source = _ShortTailRecoverySource()

    result = complete_transcription(
        source_duration_ms=245_000,
        confirmed_speech=(TimeInterval(0, 245_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert [
        (
            work.kind,
            work.core.start_ms,
            work.core.end_ms,
            work.request.start_ms,
            work.request.end_ms,
        )
        for work in source.calls[1]
    ] == [
        (
            RecognitionKind.RECOVERY,
            230_000,
            245_000,
            225_000,
            245_000,
        )
    ]
    assert result.chunks == source.primary_chunks + (source.recovered_chunk,)
    assert result.execution_facts.cache_use is CacheUse.MISS
    assert result.execution_facts.recovery_count == 1


class _LongTailRecoverySource:
    def __init__(self) -> None:
        self.calls: list[tuple[RecognitionWork, ...]] = []
        self.primary_chunks = (
            TranscriptionChunk(0, 180_000, "第一核心区"),
            TranscriptionChunk(180_000, 235_000, "截断前最后观察"),
        )
        self.recovered_chunks = (
            TranscriptionChunk(235_000, 295_000, "长尾补转一"),
            TranscriptionChunk(295_000, 355_000, "长尾补转二"),
            TranscriptionChunk(355_000, 400_000, "长尾补转三"),
        )

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation,
    ) -> tuple[RecognitionObservation, ...]:
        cancellation.raise_if_cancelled()
        self.calls.append(works)
        if len(self.calls) == 1:
            return tuple(
                RecognitionObservation(
                    work=work,
                    chunks=(
                        (self.primary_chunks[index],)
                        if index < len(self.primary_chunks)
                        else ()
                    ),
                )
                for index, work in enumerate(works)
            )
        return tuple(
            RecognitionObservation(work=work, chunks=(chunk,))
            for work, chunk in zip(works, self.recovered_chunks)
        )


def test_complete_transcription_splits_a_confirmed_165_second_tail_gap():
    source = _LongTailRecoverySource()

    result = complete_transcription(
        source_duration_ms=400_000,
        confirmed_speech=(TimeInterval(0, 400_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert [
        (
            work.core.start_ms,
            work.core.end_ms,
            work.request.start_ms,
            work.request.end_ms,
        )
        for work in source.calls[1]
    ] == [
        (235_000, 295_000, 230_000, 300_000),
        (295_000, 355_000, 290_000, 360_000),
        (355_000, 400_000, 350_000, 400_000),
    ]
    assert result.chunks == (source.primary_chunks + source.recovered_chunks)
    assert result.execution_facts.recovery_count == 3


class _ScriptedBatchesSource:
    def __init__(
        self,
        batches: tuple[
            tuple[tuple[TranscriptionChunk, ...], ...],
            ...,
        ],
    ) -> None:
        self._batches = batches
        self.calls: list[tuple[RecognitionWork, ...]] = []

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation,
    ) -> tuple[RecognitionObservation, ...]:
        cancellation.raise_if_cancelled()
        batch = self._batches[len(self.calls)]
        self.calls.append(works)
        assert len(batch) == len(works)
        return tuple(
            RecognitionObservation(work=work, chunks=chunks)
            for work, chunks in zip(works, batch)
        )


def test_complete_transcription_recovers_a_middle_speech_gap_even_when_the_tail_is_covered():
    before = TranscriptionChunk(0, 40_000, "缺口之前")
    after = TranscriptionChunk(80_000, 120_000, "末尾已经有文本")
    recovered = TranscriptionChunk(40_000, 80_000, "中间语音被补回")
    source = _ScriptedBatchesSource(
        (
            (((before, after)),),
            ((recovered,),),
        )
    )

    result = complete_transcription(
        source_duration_ms=120_000,
        confirmed_speech=(TimeInterval(0, 120_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    (recovery,) = source.calls[1]
    assert (
        recovery.core.start_ms,
        recovery.core.end_ms,
        recovery.request.start_ms,
        recovery.request.end_ms,
    ) == (40_000, 80_000, 35_000, 85_000)
    assert result.chunks == (before, recovered, after)


def test_character_spans_expose_a_middle_gap_hidden_by_the_chunk_envelope():
    envelope = TranscriptionChunk(
        0,
        30_000,
        "前后",
        (
            CharacterSpan(0, 10_000),
            CharacterSpan(20_000, 30_000),
        ),
    )
    source = _ScriptedBatchesSource(
        (
            ((envelope,),),
            ((),),
        )
    )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=30_000,
            confirmed_speech=(TimeInterval(0, 30_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    (recovery,) = source.calls[1]
    assert (
        recovery.core.start_ms,
        recovery.core.end_ms,
        recovery.request.start_ms,
        recovery.request.end_ms,
    ) == (10_000, 20_000, 5_000, 25_000)
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 10_000,
        "reason_code": "coverage.no_progress",
    }
    assert not hasattr(captured.value, "chunks")
    assert not hasattr(captured.value, "partial_result")


def test_complete_transcription_does_not_treat_trailing_silence_as_a_gap():
    spoken = TranscriptionChunk(0, 40_000, "最后一句到这里结束")
    source = _ScriptedBatchesSource(((((spoken,),)),))

    result = complete_transcription(
        source_duration_ms=120_000,
        confirmed_speech=(TimeInterval(0, 40_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert len(source.calls) == 1
    assert result.chunks == (spoken,)
    assert result.speech_presence is SpeechPresence.PRESENT


def test_complete_transcription_returns_confirmed_absence_without_requests():
    source = _ScriptedBatchesSource(())

    result = complete_transcription(
        source_duration_ms=120_000,
        confirmed_speech=(),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert source.calls == []
    assert result.chunks == ()
    assert result.speech_presence is SpeechPresence.ABSENT


def test_complete_transcription_fails_when_confirmed_speech_keeps_returning_empty():
    source = _ScriptedBatchesSource(
        (
            ((),),
            ((),),
        )
    )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(TimeInterval(0, 60_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 60_000,
        "reason_code": "coverage.no_progress",
    }
    assert captured.value.execution_facts.recovery_count == 1
    assert not hasattr(captured.value, "chunks")
    assert not hasattr(captured.value, "partial_result")


def test_complete_transcription_does_not_hide_a_short_wholly_uncovered_speech_interval():
    source = _ScriptedBatchesSource(
        (
            ((),),
            ((),),
        )
    )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=10_000,
            confirmed_speech=(TimeInterval(2_000, 2_350),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 350,
        "reason_code": "coverage.no_progress",
    }


def test_coverage_tolerance_does_not_bridge_over_wholly_uncovered_speech():
    before = TranscriptionChunk(0, 2_000, "之前")
    after = TranscriptionChunk(2_350, 10_000, "之后")
    source = _ScriptedBatchesSource(
        (
            ((before, after),),
            ((),),
        )
    )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=10_000,
            confirmed_speech=(TimeInterval(2_000, 2_350),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    (recovery,) = source.calls[1]
    assert recovery.core == TimeInterval(2_000, 2_350)
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 350,
        "reason_code": "coverage.no_progress",
    }


class _ConflictConsensusSource:
    def __init__(self) -> None:
        self.calls: list[tuple[RecognitionWork, ...]] = []
        text = "缓存失效，"
        self.winning_chunk = TranscriptionChunk(
            176_000,
            183_800,
            text,
            _character_spans(176_000, 183_800, text),
        )
        self.conflicting_chunk = TranscriptionChunk(
            178_000,
            184_000,
            "缓存生效",
        )
        self.recovery_chunk = TranscriptionChunk(
            176_050,
            183_850,
            "缓存 失效",
        )

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation,
    ) -> tuple[RecognitionObservation, ...]:
        cancellation.raise_if_cancelled()
        self.calls.append(works)
        if len(self.calls) == 1:
            first, second = works
            return (
                RecognitionObservation(
                    work=second,
                    chunks=(self.conflicting_chunk,),
                ),
                RecognitionObservation(
                    work=first,
                    chunks=(self.winning_chunk,),
                ),
            )
        (recovery,) = works
        return (
            RecognitionObservation(
                work=recovery,
                chunks=(self.recovery_chunk,),
            ),
        )


def test_speech_recognition_uses_targeted_independent_consensus_for_a_semantic_conflict(
    tmp_path,
):
    source = _ConflictConsensusSource()
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=200_000,
    )
    recognition = ReconciledSpeechRecognition(
        speech_evidence_source=_StaticSpeechEvidence((TimeInterval(176_000, 183_800),)),
        observation_source=source,
        readiness=ReadinessReport(ready=True),
    )

    try:
        assert recognition.check_readiness() == ReadinessReport(ready=True)
        result = recognition.transcribe(request)

        (recovery,) = source.calls[1]
        assert recovery.kind is RecognitionKind.RECOVERY
        assert (
            recovery.core.start_ms,
            recovery.core.end_ms,
            recovery.request.start_ms,
            recovery.request.end_ms,
        ) == (176_000, 183_800, 171_000, 188_800)
        assert result.chunks == (source.winning_chunk,)
        assert result.chunks[0] is source.winning_chunk
        assert result.execution_facts.recovery_count == 1
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_complete_transcription_keeps_consecutive_real_repeated_speech():
    before = TranscriptionChunk(170_000, 178_000, "这个方案")
    first_repeat = TranscriptionChunk(178_000, 180_000, "可以")
    second_from_left = TranscriptionChunk(180_100, 182_100, "可以")
    second_from_right = TranscriptionChunk(180_050, 182_050, "可以")
    after = TranscriptionChunk(182_100, 190_000, "先这样处理")

    class _RepeatedSpeechSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            first, second = works
            return (
                RecognitionObservation(
                    work=first,
                    chunks=(before, first_repeat, second_from_left),
                ),
                RecognitionObservation(
                    work=second,
                    chunks=(second_from_right, after),
                ),
            )

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(170_000, 190_000),),
        observation_source=_RepeatedSpeechSource(),
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert tuple(chunk.text for chunk in result.chunks) == (
        "这个方案",
        "可以",
        "可以",
        "先这样处理",
    )
    assert result.chunks[1] is first_repeat
    assert result.chunks[2] is second_from_right
    assert result.execution_facts.recovery_count == 0


def test_complete_transcription_fails_when_targeted_conflict_observation_has_no_consensus():
    source = _ConflictConsensusSource()
    source.recovery_chunk = TranscriptionChunk(
        176_050,
        183_850,
        "缓存启用",
    )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=200_000,
            confirmed_speech=(TimeInterval(176_000, 183_800),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 7_850,
        "reason_code": "coverage.no_progress",
    }
    assert captured.value.execution_facts.recovery_count == 1
    assert not hasattr(captured.value, "chunks")
    assert not hasattr(captured.value, "partial_result")


def test_one_to_many_segmentation_votes_as_one_independent_observation_sequence():
    conflicting = TranscriptionChunk(176_000, 184_000, "缓存已经失效")
    split_winner = (
        TranscriptionChunk(176_000, 180_000, "缓存已经"),
        TranscriptionChunk(180_000, 184_000, "生效"),
    )
    recovered = TranscriptionChunk(176_000, 184_000, "缓存已经生效")

    class _OneToManySource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            if len(self.calls) == 1:
                first, second = works
                return (
                    RecognitionObservation(first, (conflicting,)),
                    RecognitionObservation(second, split_winner),
                )
            (recovery,) = works
            return (RecognitionObservation(recovery, (recovered,)),)

    source = _OneToManySource()

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(176_000, 184_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert len(source.calls) == 2
    assert result.chunks == (recovered,)
    assert result.chunks[0] is recovered
    assert result.execution_facts.recovery_count == 1


def test_winning_split_hypothesis_keeps_each_real_text_and_timing_candidate():
    conflicting = TranscriptionChunk(176_000, 184_000, "缓存失效")
    primary_winner = TranscriptionChunk(176_000, 184_000, "缓存生效")
    recovered_winner = (
        TranscriptionChunk(
            176_000,
            180_000,
            "缓存生",
            _character_spans(176_000, 180_000, "缓存生"),
        ),
        TranscriptionChunk(
            180_000,
            184_000,
            "效",
            _character_spans(180_000, 184_000, "效"),
        ),
    )

    class _SplitWinnerSource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls += 1
            if self.calls == 1:
                first, second = works
                return (
                    RecognitionObservation(first, (conflicting,)),
                    RecognitionObservation(second, (primary_winner,)),
                )
            (recovery,) = works
            return (RecognitionObservation(recovery, recovered_winner),)

    result = complete_transcription(
        source_duration_ms=200_000,
        confirmed_speech=(TimeInterval(176_000, 184_000),),
        observation_source=_SplitWinnerSource(),
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert result.chunks == recovered_winner
    assert result.chunks[0] is recovered_winner[0]
    assert result.chunks[1] is recovered_winner[1]
    assert "".join(chunk.text for chunk in result.chunks) == primary_winner.text


def test_duplicate_candidates_in_one_recovery_do_not_form_independent_consensus():
    first_answer = TranscriptionChunk(176_000, 184_000, "缓存失效")
    second_answer = TranscriptionChunk(176_000, 184_000, "缓存生效")
    duplicate_answer = (
        TranscriptionChunk(176_000, 184_000, "缓存启用"),
        TranscriptionChunk(176_050, 183_950, "缓存 启用"),
    )

    class _DuplicateRecoverySource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls += 1
            if self.calls == 1:
                first, second = works
                return (
                    RecognitionObservation(first, (first_answer,)),
                    RecognitionObservation(second, (second_answer,)),
                )
            (recovery,) = works
            return (RecognitionObservation(recovery, duplicate_answer),)

    source = _DuplicateRecoverySource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=200_000,
            confirmed_speech=(TimeInterval(176_000, 184_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert source.calls == 2
    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE
    assert captured.value.diagnostics["reason_code"] == "coverage.no_progress"


def test_complete_transcription_plans_multiple_recoveries_in_stable_time_order():
    primary_chunks = (
        TranscriptionChunk(0, 30_000, "第一段"),
        TranscriptionChunk(60_000, 90_000, "第三段"),
        TranscriptionChunk(120_000, 150_000, "第五段"),
    )
    recovered_chunks = (
        TranscriptionChunk(30_000, 60_000, "第二段"),
        TranscriptionChunk(90_000, 120_000, "第四段"),
    )

    class _MultipleGapSource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            if len(self.calls) == 1:
                (primary,) = works
                return (
                    RecognitionObservation(
                        work=primary,
                        chunks=primary_chunks,
                    ),
                )
            return tuple(
                reversed(
                    tuple(
                        RecognitionObservation(
                            work=work,
                            chunks=(chunk,),
                        )
                        for work, chunk in zip(
                            works,
                            recovered_chunks,
                        )
                    )
                )
            )

    source = _MultipleGapSource()

    result = complete_transcription(
        source_duration_ms=150_000,
        confirmed_speech=(TimeInterval(0, 150_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert [(work.core.start_ms, work.core.end_ms) for work in source.calls[1]] == [
        (30_000, 60_000),
        (90_000, 120_000),
    ]
    assert tuple(chunk.text for chunk in result.chunks) == (
        "第一段",
        "第二段",
        "第三段",
        "第四段",
        "第五段",
    )
    assert result.execution_facts.recovery_count == 2


def test_speech_recognition_fails_when_a_region_exhausts_its_attempt_budget(
    tmp_path,
):
    first_partial = TranscriptionChunk(0, 20_000, "只补回一部分")
    second_partial = TranscriptionChunk(20_000, 30_000, "仍只补回一部分")
    source = _ScriptedBatchesSource(
        (
            ((),),
            ((first_partial,),),
            ((second_partial,),),
        )
    )
    request, run_workspace = _transcription_request(
        tmp_path,
        duration_ms=60_000,
    )
    recognition = ReconciledSpeechRecognition(
        speech_evidence_source=_StaticSpeechEvidence((TimeInterval(0, 60_000),)),
        observation_source=source,
        readiness=ReadinessReport(ready=True),
    )

    try:
        with pytest.raises(TranscriptionFailure) as captured:
            recognition.transcribe(request)

        assert len(source.calls) == 3
        assert captured.value.diagnostics == {
            "gap_count": 1,
            "gap_duration_ms": 30_000,
            "reason_code": "coverage.budget_exhausted",
        }
        assert captured.value.execution_facts.recovery_count == 2
        assert not hasattr(captured.value, "chunks")
        assert not hasattr(captured.value, "partial_result")
    finally:
        run_workspace.cleanup()
        run_workspace.close()


def test_adjacent_targets_in_one_round_count_as_one_attempt_for_their_boundary():
    left = TranscriptionChunk(0, 59_000, "第一轮左侧")
    right = TranscriptionChunk(61_000, 120_000, "第一轮右侧")
    boundary = TranscriptionChunk(59_000, 61_000, "第二轮补齐边界")
    source = _ScriptedBatchesSource(
        (
            ((),),
            ((left,), (right,)),
            ((boundary,),),
        )
    )

    result = complete_transcription(
        source_duration_ms=120_000,
        confirmed_speech=(TimeInterval(0, 120_000),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    assert len(source.calls) == 3
    assert [(work.core.start_ms, work.core.end_ms) for work in source.calls[1]] == [
        (0, 60_000),
        (60_000, 120_000),
    ]
    assert [(work.core.start_ms, work.core.end_ms) for work in source.calls[2]] == [
        (59_000, 61_000)
    ]
    assert result.chunks == (left, boundary, right)
    assert result.execution_facts.recovery_count == 3


def test_complete_transcription_enforces_the_total_recovery_audio_budget_before_requests():
    class _EmptyPrimarySource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            return tuple(RecognitionObservation(work=work, chunks=()) for work in works)

    source = _EmptyPrimarySource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=1_650_000,
            confirmed_speech=(TimeInterval(0, 1_650_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert len(source.calls) == 1
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 1_650_000,
        "reason_code": "coverage.budget_exhausted",
    }
    assert captured.value.execution_facts.recovery_count == 0
    assert not hasattr(captured.value, "chunks")
    assert not hasattr(captured.value, "partial_result")


def test_complete_transcription_closes_the_final_core_without_zero_length_or_overlap():
    final_chunk = TranscriptionChunk(540_000, 540_001, "末")

    class _PlanningSource:
        def __init__(self) -> None:
            self.calls: list[tuple[RecognitionWork, ...]] = []

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls.append(works)
            observations = tuple(
                RecognitionObservation(
                    work=work,
                    chunks=((final_chunk,) if work.core.end_ms == 540_001 else ()),
                )
                for work in works
            )
            return tuple(reversed(observations))

    source = _PlanningSource()

    result = complete_transcription(
        source_duration_ms=540_001,
        confirmed_speech=(TimeInterval(540_000, 540_001),),
        observation_source=source,
        cancellation=CancellationSource(clock=lambda: 0.0).token,
    )

    cores = tuple(work.core for work in source.calls[0])
    assert cores == (
        TimeInterval(0, 180_000),
        TimeInterval(180_000, 360_000),
        TimeInterval(360_000, 540_000),
        TimeInterval(540_000, 540_001),
    )
    assert all(left.end_ms == right.start_ms for left, right in pairwise(cores))
    assert all(core.duration_ms > 0 for core in cores)
    assert result.chunks == (final_chunk,)


def test_complete_transcription_rejects_speech_evidence_outside_the_source():
    source = _ScriptedBatchesSource(())

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=10_000,
            confirmed_speech=(TimeInterval(9_000, 11_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert source.calls == []
    assert captured.value.diagnostics == {
        "gap_count": 1,
        "gap_duration_ms": 2_000,
        "reason_code": "coverage.evidence_inconclusive",
    }


def test_complete_transcription_rejects_a_candidate_outside_its_request_context():
    invalid_for_first_request = TranscriptionChunk(
        184_000,
        190_000,
        "越过本次请求区",
    )

    class _OutOfRequestSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            first, second = works
            return (
                RecognitionObservation(
                    work=first,
                    chunks=(invalid_for_first_request,),
                ),
                RecognitionObservation(work=second, chunks=()),
            )

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=200_000,
            confirmed_speech=(TimeInterval(184_000, 190_000),),
            observation_source=_OutOfRequestSource(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_OUTPUT_INVALID
    assert captured.value.diagnostics == {"reason_code": "output.out_of_bounds"}


def test_complete_transcription_rejects_semantic_overlap_inside_one_observation():
    class _InternallyConflictingSource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls += 1
            (work,) = works
            return (
                RecognitionObservation(
                    work,
                    (
                        TranscriptionChunk(0, 10_000, "甲"),
                        TranscriptionChunk(0, 10_000, "乙"),
                    ),
                ),
            )

    source = _InternallyConflictingSource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=10_000,
            confirmed_speech=(TimeInterval(0, 10_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert source.calls == 1
    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_OUTPUT_INVALID
    assert captured.value.diagnostics == {"reason_code": "output.overlap_text_mismatch"}


def test_complete_transcription_translates_observation_shape_protocol_failure():
    class _ListReturningSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            return [RecognitionObservation(work=work, chunks=()) for work in works]

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(TimeInterval(0, 60_000),),
            observation_source=_ListReturningSource(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert (
        captured.value.error_code is ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID
    )
    assert captured.value.diagnostics == {"reason_code": "protocol.body_invalid"}


def test_complete_transcription_translates_missing_observation_protocol_failure():
    class _MissingObservationSource:
        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            first, _second = works
            return (RecognitionObservation(work=first, chunks=()),)

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=200_000,
            confirmed_speech=(TimeInterval(0, 200_000),),
            observation_source=_MissingObservationSource(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert (
        captured.value.error_code is ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID
    )
    assert captured.value.diagnostics == {"reason_code": "protocol.body_invalid"}


def test_failed_recovery_request_keeps_retry_and_attempt_facts():
    provider_failure = TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE,
        execution_facts=ExecutionFacts(
            cache_use=CacheUse.MISS,
            retry_count=2,
        ),
        diagnostics={"reason_code": "service.server_error"},
    )

    class _FailingRecoverySource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls += 1
            if self.calls == 1:
                return RecognitionBatch(
                    observations=tuple(
                        RecognitionObservation(work=work, chunks=())
                        for work in works
                    ),
                    retry_count=1,
                )
            raise provider_failure._fresh()

    source = _FailingRecoverySource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(TimeInterval(0, 60_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert source.calls == 2
    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_SERVICE_UNAVAILABLE
    assert captured.value.diagnostics == {"reason_code": "service.server_error"}
    assert captured.value.execution_facts == ExecutionFacts(
        cache_use=CacheUse.MISS,
        retry_count=3,
        recovery_count=1,
    )


def test_no_progress_recovery_keeps_all_successful_retry_facts():
    class _RetriedButEmptySource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, cancellation):
            cancellation.raise_if_cancelled()
            self.calls += 1
            return RecognitionBatch(
                observations=tuple(
                    RecognitionObservation(work=work, chunks=()) for work in works
                ),
                retry_count=self.calls,
            )

    source = _RetriedButEmptySource()

    with pytest.raises(TranscriptionFailure) as captured:
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(TimeInterval(0, 60_000),),
            observation_source=source,
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )

    assert source.calls == 2
    assert captured.value.error_code is ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE
    assert captured.value.diagnostics["reason_code"] == "coverage.no_progress"
    assert captured.value.execution_facts == ExecutionFacts(
        cache_use=CacheUse.MISS,
        retry_count=3,
        recovery_count=1,
    )


def test_complete_transcription_stops_before_recovery_after_cancellation():
    cancellation = CancellationSource(clock=lambda: 0.0)

    class _CancellingSource:
        def __init__(self) -> None:
            self.calls = 0

        def observe(self, works, token):
            self.calls += 1
            cancellation.request(signal.SIGTERM)
            return tuple(RecognitionObservation(work=work, chunks=()) for work in works)

    source = _CancellingSource()

    with pytest.raises(CancellationRequested) as captured:
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(TimeInterval(0, 60_000),),
            observation_source=source,
            cancellation=cancellation.token,
        )

    assert captured.value.signal_number == signal.SIGTERM
    assert source.calls == 1
    assert not hasattr(captured.value, "chunks")
    assert not hasattr(captured.value, "partial_result")


def test_complete_transcription_honors_cancellation_before_confirmed_absence():
    cancellation = CancellationSource(clock=lambda: 0.0)
    cancellation.request(signal.SIGTERM)
    source = _ScriptedBatchesSource(())

    with pytest.raises(CancellationRequested):
        complete_transcription(
            source_duration_ms=60_000,
            confirmed_speech=(),
            observation_source=source,
            cancellation=cancellation.token,
        )

    assert source.calls == []
