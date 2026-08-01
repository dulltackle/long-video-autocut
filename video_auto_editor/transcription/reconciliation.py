"""基于独立语音证据补全覆盖并归并识别观察。"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from video_auto_editor.runtime.cancellation import CancellationToken
from video_auto_editor.runtime.errors import ErrorCode

from .interface import (
    CacheUse,
    ExecutionFacts,
    ReadinessReport,
    SpeechPresence,
    TranscriptionChunk,
    TranscriptionFailure,
    TranscriptionRequest,
    TranscriptionResult,
    validate_result_for_source,
)


class RecognitionKind(str, Enum):
    """识别观察属于常规核心区还是定向覆盖补救。"""

    PRIMARY = "primary"
    RECOVERY = "recovery"


@dataclass(frozen=True, slots=True, order=True)
class TimeInterval:
    """素材全局时间轴上的半开整数毫秒区间。"""

    start_ms: int
    end_ms: int

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.start_ms, "区间开始时间"),
            (self.end_ms, "区间结束时间"),
        ):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name}必须是整数毫秒")
        if self.start_ms < 0:
            raise ValueError("区间开始时间不能为负数")
        if self.end_ms <= self.start_ms:
            raise ValueError("区间结束时间必须晚于开始时间")

    @property
    def duration_ms(self) -> int:
        """返回区间的正整数毫秒时长。"""
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class RecognitionWork:
    """交给识别 Adapter 的一次确定性请求工作。"""

    sequence: int
    kind: RecognitionKind
    core: TimeInterval
    request: TimeInterval

    def __post_init__(self) -> None:
        if (
            not isinstance(self.sequence, int)
            or isinstance(self.sequence, bool)
            or self.sequence < 0
        ):
            raise ValueError("识别工作序号必须是非负整数")
        if not isinstance(self.kind, RecognitionKind):
            raise TypeError("识别工作必须使用 RecognitionKind")
        if not isinstance(self.core, TimeInterval) or not isinstance(
            self.request,
            TimeInterval,
        ):
            raise TypeError("识别工作必须包含核心区和请求区")
        if (
            self.request.start_ms > self.core.start_ms
            or self.request.end_ms < self.core.end_ms
        ):
            raise ValueError("识别请求区必须完整包含核心区")


@dataclass(frozen=True, slots=True)
class RecognitionObservation:
    """一次独立识别请求返回的真实候选集合。"""

    work: RecognitionWork
    chunks: tuple[TranscriptionChunk, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.work, RecognitionWork):
            raise TypeError("识别观察必须引用 RecognitionWork")
        if not isinstance(self.chunks, tuple) or any(
            not isinstance(chunk, TranscriptionChunk) for chunk in self.chunks
        ):
            raise TypeError("识别观察只能包含不可变转写文本块")


@dataclass(frozen=True, slots=True)
class RecognitionBatch:
    """一次包内观察调用的结果与成功传输重试事实。"""

    observations: tuple[RecognitionObservation, ...]
    retry_count: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.observations, tuple) or any(
            not isinstance(observation, RecognitionObservation)
            for observation in self.observations
        ):
            raise TypeError("识别批次只能包含不可变识别观察")
        if (
            not isinstance(self.retry_count, int)
            or isinstance(self.retry_count, bool)
        ):
            raise TypeError("识别批次传输重试次数必须是整数")
        if self.retry_count < 0:
            raise ValueError("识别批次传输重试次数不能为负数")


class RecognitionObservationSource(Protocol):
    """生产 Adapter 与确定性假实现共同满足的包内请求端口。"""

    def observe(
        self,
        works: tuple[RecognitionWork, ...],
        cancellation: CancellationToken,
    ) -> RecognitionBatch | tuple[RecognitionObservation, ...]:
        """执行请求并返回可乱序完成的一次独立观察。"""
        ...


class SpeechEvidenceSource(Protocol):
    """从标准化音频独立生成确认语音区间的包内端口。"""

    def confirmed_speech(
        self,
        request: TranscriptionRequest,
    ) -> tuple[TimeInterval, ...]:
        """返回素材全局时间轴上的确定性确认语音区间。"""
        ...


class ReconciledSpeechRecognition:
    """在既有公开 seam 内组合独立语音证据、观察与严格归并。"""

    __slots__ = (
        "_observation_source",
        "_readiness",
        "_speech_evidence_source",
    )

    def __init__(
        self,
        *,
        speech_evidence_source: SpeechEvidenceSource,
        observation_source: RecognitionObservationSource,
        readiness: ReadinessReport,
    ) -> None:
        if not callable(getattr(speech_evidence_source, "confirmed_speech", None)):
            raise TypeError("语音识别必须使用独立语音证据源")
        if not callable(getattr(observation_source, "observe", None)):
            raise TypeError("语音识别必须使用识别观察源")
        if not isinstance(readiness, ReadinessReport):
            raise TypeError("语音识别准备状态必须使用 ReadinessReport")
        self._speech_evidence_source = speech_evidence_source
        self._observation_source = observation_source
        self._readiness = readiness

    def check_readiness(self) -> ReadinessReport:
        """返回构造时聚合的本地只读准备快照。"""
        return self._readiness

    def transcribe(
        self,
        request: TranscriptionRequest,
    ) -> TranscriptionResult:
        """同步返回覆盖完整且冲突已消解的整场转写。"""
        if not isinstance(request, TranscriptionRequest):
            raise TypeError("语音识别只接受 TranscriptionRequest")
        request.cancellation.raise_if_cancelled()
        confirmed_speech = self._speech_evidence_source.confirmed_speech(request)
        request.cancellation.raise_if_cancelled()
        result = complete_transcription(
            source_duration_ms=request.source.duration_ms,
            confirmed_speech=confirmed_speech,
            observation_source=self._observation_source,
            cancellation=request.cancellation,
        )
        validate_result_for_source(result, request.source)
        request.cancellation.raise_if_cancelled()
        return result


@dataclass(frozen=True, slots=True)
class _ReconciliationPolicy:
    algorithm_version: str = "coverage-reconciliation.v1"
    primary_core_ms: int = 180_000
    context_ms: int = 5_000
    coverage_tolerance_ms: int = 350
    recovery_core_ms: int = 60_000
    max_recovery_attempts_per_region: int = 2
    max_recovery_requests: int = 32
    max_recovery_audio_ms: int = 1_800_000


_POLICY = _ReconciliationPolicy()


@dataclass(frozen=True, slots=True)
class _Candidate:
    observation: RecognitionObservation
    chunk: TranscriptionChunk
    chunk_index: int


@dataclass(frozen=True, slots=True)
class _CandidateSequence:
    observation: RecognitionObservation
    candidates: tuple[_Candidate, ...]
    normalized_text: str
    interval: TimeInterval


@dataclass(frozen=True, slots=True)
class _Conflict:
    interval: TimeInterval


@dataclass(frozen=True, slots=True)
class _Analysis:
    accepted: tuple[TranscriptionChunk, ...]
    gaps: tuple[TimeInterval, ...]
    conflicts: tuple[_Conflict, ...]


def complete_transcription(
    *,
    source_duration_ms: int,
    confirmed_speech: tuple[TimeInterval, ...],
    observation_source: RecognitionObservationSource,
    cancellation: CancellationToken,
) -> TranscriptionResult:
    """只在语音覆盖完整且重叠冲突消解后返回整场转写。"""
    _validate_inputs(
        source_duration_ms,
        confirmed_speech,
        observation_source,
        cancellation,
    )
    cancellation.raise_if_cancelled()
    speech = _normalize_speech_evidence(
        confirmed_speech,
        source_duration_ms,
    )
    if not speech:
        return TranscriptionResult(
            chunks=(),
            speech_presence=SpeechPresence.ABSENT,
            execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        )

    primary = _plan_primary_work(source_duration_ms)
    cancellation.raise_if_cancelled()
    primary_batch = _collect_observations(
        observation_source,
        primary,
        cancellation,
        source_duration_ms=source_duration_ms,
    )
    observations = list(primary_batch.observations)
    retry_count = primary_batch.retry_count
    analysis = _analyze(speech, tuple(observations))
    cancellation.raise_if_cancelled()
    recovery_count = 0
    recovery_audio_ms = 0
    attempted_rounds: list[tuple[TimeInterval, ...]] = []
    next_sequence = len(primary)
    while analysis.gaps or analysis.conflicts:
        targets = _plan_recovery_targets(analysis)
        if _recovery_budget_exhausted(
            targets,
            attempted_rounds=tuple(attempted_rounds),
            recovery_count=recovery_count,
            recovery_audio_ms=recovery_audio_ms,
            source_duration_ms=source_duration_ms,
        ):
            raise _coverage_failure(
                analysis,
                reason_code="coverage.budget_exhausted",
                retry_count=retry_count,
                recovery_count=recovery_count,
            )
        works = tuple(
            RecognitionWork(
                sequence=next_sequence + index,
                kind=RecognitionKind.RECOVERY,
                core=target,
                request=TimeInterval(
                    max(0, target.start_ms - _POLICY.context_ms),
                    min(
                        source_duration_ms,
                        target.end_ms + _POLICY.context_ms,
                    ),
                ),
            )
            for index, target in enumerate(targets)
        )
        attempted_recovery_count = recovery_count + len(works)
        attempted_recovery_audio_ms = recovery_audio_ms + sum(
            work.request.duration_ms for work in works
        )
        try:
            recovered = _collect_observations(
                observation_source,
                works,
                cancellation,
                source_duration_ms=source_duration_ms,
            )
            observations.extend(recovered.observations)
            retry_count += recovered.retry_count
            updated = _analyze(speech, tuple(observations))
        except TranscriptionFailure as failure:
            raise _with_added_execution_counts(
                failure,
                additional_retry_count=retry_count,
                additional_recovery_count=attempted_recovery_count,
            ) from failure
        recovery_count = attempted_recovery_count
        recovery_audio_ms = attempted_recovery_audio_ms
        attempted_rounds.append(targets)
        next_sequence += len(works)
        cancellation.raise_if_cancelled()
        if not _made_progress(analysis, updated):
            raise _coverage_failure(
                updated,
                reason_code="coverage.no_progress",
                retry_count=retry_count,
                recovery_count=recovery_count,
            )
        analysis = updated
    cancellation.raise_if_cancelled()
    return TranscriptionResult(
        chunks=analysis.accepted,
        speech_presence=SpeechPresence.PRESENT,
        execution_facts=ExecutionFacts(
            cache_use=CacheUse.MISS,
            retry_count=retry_count,
            recovery_count=recovery_count,
        ),
    )


def _validate_inputs(
    source_duration_ms: object,
    confirmed_speech: object,
    observation_source: object,
    cancellation: object,
) -> None:
    if (
        not isinstance(source_duration_ms, int)
        or isinstance(source_duration_ms, bool)
        or source_duration_ms <= 0
    ):
        raise ValueError("归并素材时长必须是正整数毫秒")
    if not isinstance(confirmed_speech, tuple) or any(
        not isinstance(interval, TimeInterval) for interval in confirmed_speech
    ):
        raise TypeError("确认语音证据必须是 TimeInterval 元组")
    if not callable(getattr(observation_source, "observe", None)):
        raise TypeError("归并必须使用识别观察源")
    if not isinstance(cancellation, CancellationToken):
        raise TypeError("归并必须绑定根取消令牌")


def _normalize_speech_evidence(
    intervals: tuple[TimeInterval, ...],
    source_duration_ms: int,
) -> tuple[TimeInterval, ...]:
    outside = tuple(
        interval for interval in intervals if interval.end_ms > source_duration_ms
    )
    if outside:
        raise TranscriptionFailure(
            ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
            execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
            diagnostics={
                "gap_count": len(outside),
                "gap_duration_ms": sum(interval.duration_ms for interval in outside),
                "reason_code": "coverage.evidence_inconclusive",
            },
        )
    return _merge_intervals(intervals, tolerance_ms=0)


def _plan_primary_work(
    source_duration_ms: int,
) -> tuple[RecognitionWork, ...]:
    works = []
    start_ms = 0
    sequence = 0
    while start_ms < source_duration_ms:
        end_ms = min(
            source_duration_ms,
            start_ms + _POLICY.primary_core_ms,
        )
        core = TimeInterval(start_ms, end_ms)
        request = TimeInterval(
            max(0, start_ms - _POLICY.context_ms),
            min(source_duration_ms, end_ms + _POLICY.context_ms),
        )
        works.append(
            RecognitionWork(
                sequence=sequence,
                kind=RecognitionKind.PRIMARY,
                core=core,
                request=request,
            )
        )
        start_ms = end_ms
        sequence += 1
    return tuple(works)


def _plan_recovery_targets(
    analysis: _Analysis,
) -> tuple[TimeInterval, ...]:
    unresolved = _merge_intervals(
        analysis.gaps + tuple(conflict.interval for conflict in analysis.conflicts),
        tolerance_ms=0,
    )
    targets = []
    for interval in unresolved:
        start_ms = interval.start_ms
        while start_ms < interval.end_ms:
            end_ms = min(
                interval.end_ms,
                start_ms + _POLICY.recovery_core_ms,
            )
            targets.append(TimeInterval(start_ms, end_ms))
            start_ms = end_ms
    return tuple(targets)


def _recovery_budget_exhausted(
    targets: tuple[TimeInterval, ...],
    *,
    attempted_rounds: tuple[tuple[TimeInterval, ...], ...],
    recovery_count: int,
    recovery_audio_ms: int,
    source_duration_ms: int,
) -> bool:
    if not targets:
        return True
    if recovery_count + len(targets) > _POLICY.max_recovery_requests:
        return True
    requested_ms = sum(
        min(
            source_duration_ms,
            target.end_ms + _POLICY.context_ms,
        )
        - max(0, target.start_ms - _POLICY.context_ms)
        for target in targets
    )
    if recovery_audio_ms + requested_ms > _POLICY.max_recovery_audio_ms:
        return True
    return any(
        sum(
            1
            for attempted_round in attempted_rounds
            if any(
                _intervals_overlap(attempted, target) for attempted in attempted_round
            )
        )
        >= _POLICY.max_recovery_attempts_per_region
        for target in targets
    )


def _intervals_overlap(
    left: TimeInterval,
    right: TimeInterval,
) -> bool:
    return min(left.end_ms, right.end_ms) > max(
        left.start_ms,
        right.start_ms,
    )


def _made_progress(
    previous: _Analysis,
    current: _Analysis,
) -> bool:
    previous_gap_ms = sum(interval.duration_ms for interval in previous.gaps)
    current_gap_ms = sum(interval.duration_ms for interval in current.gaps)
    previous_conflict_ms = sum(
        interval.duration_ms
        for interval in _merge_intervals(
            tuple(conflict.interval for conflict in previous.conflicts),
            tolerance_ms=0,
        )
    )
    current_conflict_ms = sum(
        interval.duration_ms
        for interval in _merge_intervals(
            tuple(conflict.interval for conflict in current.conflicts),
            tolerance_ms=0,
        )
    )
    return (
        current_gap_ms <= previous_gap_ms
        and current_conflict_ms <= previous_conflict_ms
        and (
            current_gap_ms < previous_gap_ms
            or current_conflict_ms < previous_conflict_ms
        )
    )


def _collect_observations(
    source: RecognitionObservationSource,
    works: tuple[RecognitionWork, ...],
    cancellation: CancellationToken,
    *,
    source_duration_ms: int,
) -> RecognitionBatch:
    returned = source.observe(works, cancellation)
    cancellation.raise_if_cancelled()
    if isinstance(returned, RecognitionBatch):
        observations = returned.observations
        retry_count = returned.retry_count
    elif isinstance(returned, tuple):
        observations = returned
        retry_count = 0
    else:
        raise _observation_protocol_failure()
    if any(
        not isinstance(observation, RecognitionObservation)
        for observation in observations
    ):
        raise _observation_protocol_failure()
    expected = {work.sequence: work for work in works}
    actual: dict[int, RecognitionObservation] = {}
    for observation in observations:
        sequence = observation.work.sequence
        if sequence in actual or expected.get(sequence) != observation.work:
            raise _observation_protocol_failure()
        _validate_observation(observation, source_duration_ms)
        actual[sequence] = observation
    if actual.keys() != expected.keys():
        raise _observation_protocol_failure()
    return RecognitionBatch(
        observations=tuple(actual[index] for index in sorted(actual)),
        retry_count=retry_count,
    )


def _observation_protocol_failure() -> TranscriptionFailure:
    return TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_RESPONSE_PROTOCOL_INVALID,
        execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
        diagnostics={"reason_code": "protocol.body_invalid"},
    )


def _with_added_execution_counts(
    failure: TranscriptionFailure,
    *,
    additional_retry_count: int,
    additional_recovery_count: int,
) -> TranscriptionFailure:
    facts = failure.execution_facts
    return type(failure)(
        failure.error_code,
        execution_facts=ExecutionFacts(
            cache_use=facts.cache_use,
            retry_count=facts.retry_count + additional_retry_count,
            recovery_count=(
                facts.recovery_count + additional_recovery_count
            ),
        ),
        diagnostics=failure.diagnostics,
    )


def _validate_observation(
    observation: RecognitionObservation,
    source_duration_ms: int,
) -> None:
    previous_key: tuple[int, int] | None = None
    for chunk in observation.chunks:
        if (
            chunk.start_ms < observation.work.request.start_ms
            or chunk.end_ms > observation.work.request.end_ms
            or chunk.end_ms > source_duration_ms
        ):
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={"reason_code": "output.out_of_bounds"},
            )
        key = (chunk.start_ms, chunk.end_ms)
        if previous_key is not None and key < previous_key:
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={"reason_code": "output.time_invalid"},
            )
        previous_key = key


def _analyze(
    speech: tuple[TimeInterval, ...],
    observations: tuple[RecognitionObservation, ...],
) -> _Analysis:
    candidates = tuple(
        _Candidate(observation, chunk, chunk_index)
        for observation in observations
        for chunk_index, chunk in enumerate(observation.chunks)
    )
    accepted: list[TranscriptionChunk] = []
    conflicts: list[_Conflict] = []
    for cluster in _cluster_candidates(candidates):
        cluster_interval = TimeInterval(
            min(item.chunk.start_ms for item in cluster),
            max(item.chunk.end_ms for item in cluster),
        )
        if any(not _normalize_text(candidate.chunk.text) for candidate in cluster):
            raise TranscriptionFailure(
                ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
                execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
                diagnostics={"reason_code": "output.text_invalid"},
            )
        sequences = _candidate_sequences(cluster)
        vote_groups = _build_vote_groups(sequences)
        if len(vote_groups) == 1:
            selected = _choose_sequence(
                vote_groups[0],
                required_intervals=(cluster_interval,),
            )
            if selected is not None:
                accepted.extend(candidate.chunk for candidate in selected.candidates)
                continue
            conflicts.append(
                _Conflict(
                    interval=cluster_interval,
                )
            )
            continue
        conflict_intervals = _semantic_conflict_intervals(
            vote_groups,
            fallback=cluster_interval,
        )
        ranked = sorted(
            (
                (
                    len(group),
                    group[0].normalized_text,
                    group,
                )
                for group in vote_groups
            ),
            key=lambda item: (-item[0], item[1]),
        )
        leading_count, _leading_text, leading = ranked[0]
        has_unique_leader = leading_count > ranked[1][0]
        recovery_confirmed = any(
            sequence.observation.work.kind is RecognitionKind.RECOVERY
            for sequence in leading
        )
        selected = _choose_sequence(
            leading,
            required_intervals=conflict_intervals,
        )
        if (
            leading_count >= 2
            and has_unique_leader
            and recovery_confirmed
            and selected is not None
        ):
            accepted.extend(candidate.chunk for candidate in selected.candidates)
            continue
        conflicts.extend(
            _Conflict(interval=interval) for interval in conflict_intervals
        )
    accepted.sort(key=lambda chunk: (chunk.start_ms, chunk.end_ms, chunk.text))
    gaps = _find_speech_gaps(speech, tuple(accepted))
    conflicts.sort(
        key=lambda conflict: (
            conflict.interval.start_ms,
            conflict.interval.end_ms,
        )
    )
    return _Analysis(tuple(accepted), gaps, tuple(conflicts))


def _candidate_sequences(
    cluster: tuple[_Candidate, ...],
) -> tuple[_CandidateSequence, ...]:
    grouped: dict[int, list[_Candidate]] = {}
    observations: dict[int, RecognitionObservation] = {}
    for candidate in cluster:
        sequence = candidate.observation.work.sequence
        grouped.setdefault(sequence, []).append(candidate)
        observations[sequence] = candidate.observation
    result = []
    for sequence in sorted(grouped):
        candidates = _deduplicate_observation_candidates(
            tuple(
                sorted(
                    grouped[sequence],
                    key=lambda item: (
                        item.chunk.start_ms,
                        item.chunk.end_ms,
                        item.chunk_index,
                    ),
                )
            )
        )
        result.append(
            _CandidateSequence(
                observation=observations[sequence],
                candidates=candidates,
                normalized_text="".join(
                    _normalize_text(candidate.chunk.text) for candidate in candidates
                ),
                interval=TimeInterval(
                    min(candidate.chunk.start_ms for candidate in candidates),
                    max(candidate.chunk.end_ms for candidate in candidates),
                ),
            )
        )
    return tuple(result)


def _deduplicate_observation_candidates(
    candidates: tuple[_Candidate, ...],
) -> tuple[_Candidate, ...]:
    equivalent_groups = _cluster_candidates(candidates)
    if any(
        len({_normalize_text(candidate.chunk.text) for candidate in group}) > 1
        for group in equivalent_groups
    ):
        raise TranscriptionFailure(
            ErrorCode.TRANSCRIPTION_OUTPUT_INVALID,
            execution_facts=ExecutionFacts(cache_use=CacheUse.MISS),
            diagnostics={"reason_code": "output.overlap_text_mismatch"},
        )
    deduplicated = tuple(_choose_candidate(group) for group in equivalent_groups)
    return tuple(
        sorted(
            deduplicated,
            key=lambda item: (
                item.chunk.start_ms,
                item.chunk.end_ms,
                item.chunk_index,
            ),
        )
    )


def _choose_sequence(
    sequences: tuple[_CandidateSequence, ...],
    *,
    required_intervals: tuple[TimeInterval, ...],
) -> _CandidateSequence | None:
    fully_owned = tuple(
        sequence
        for sequence in sequences
        if all(_candidate_is_owned(candidate) for candidate in sequence.candidates)
        and all(
            _sequence_covers(sequence, required_interval)
            for required_interval in required_intervals
        )
    )
    if not fully_owned:
        return None
    return max(fully_owned, key=_sequence_trust)


def _semantic_conflict_intervals(
    vote_groups: tuple[tuple[_CandidateSequence, ...], ...],
    *,
    fallback: TimeInterval,
) -> tuple[TimeInterval, ...]:
    overlaps = []
    for left_index, left_group in enumerate(vote_groups):
        for right_group in vote_groups[left_index + 1 :]:
            for left in left_group:
                for right in right_group:
                    start_ms = max(
                        left.interval.start_ms,
                        right.interval.start_ms,
                    )
                    end_ms = min(
                        left.interval.end_ms,
                        right.interval.end_ms,
                    )
                    if end_ms > start_ms:
                        overlaps.append(TimeInterval(start_ms, end_ms))
    if not overlaps:
        return (fallback,)
    return _merge_intervals(tuple(overlaps), tolerance_ms=0)


def _build_vote_groups(
    sequences: tuple[_CandidateSequence, ...],
) -> tuple[tuple[_CandidateSequence, ...], ...]:
    groups: list[list[_CandidateSequence]] = []
    for sequence in sequences:
        matching_group = next(
            (
                group
                for group in groups
                if all(_same_vote_hypothesis(sequence, existing) for existing in group)
            ),
            None,
        )
        if matching_group is None:
            groups.append([sequence])
        else:
            matching_group.append(sequence)
    return tuple(tuple(group) for group in groups)


def _same_vote_hypothesis(
    left: _CandidateSequence,
    right: _CandidateSequence,
) -> bool:
    if left.normalized_text != right.normalized_text:
        return False
    overlap_ms = min(left.interval.end_ms, right.interval.end_ms) - max(
        left.interval.start_ms,
        right.interval.start_ms,
    )
    return overlap_ms > 0 and 2 * overlap_ms > max(
        left.interval.duration_ms, right.interval.duration_ms
    )


def _sequence_covers(
    sequence: _CandidateSequence,
    required_interval: TimeInterval,
) -> bool:
    return (
        sequence.interval.start_ms
        <= required_interval.start_ms + _POLICY.coverage_tolerance_ms
        and sequence.interval.end_ms
        >= required_interval.end_ms - _POLICY.coverage_tolerance_ms
    )


def _sequence_trust(
    sequence: _CandidateSequence,
) -> tuple[int, int, int, int, int]:
    candidates = sequence.candidates
    measured_count = sum(
        candidate.chunk.character_spans is not None for candidate in candidates
    )
    edge_distance = min(_candidate_edge_distance(candidate) for candidate in candidates)
    return (
        1 if measured_count == len(candidates) else 0,
        1 if measured_count else 0,
        edge_distance,
        -sequence.observation.work.sequence,
        -min(candidate.chunk_index for candidate in candidates),
    )


def _candidate_is_owned(candidate: _Candidate) -> bool:
    return _owned_by_work(candidate.observation.work, candidate.chunk)


def _owned_by_work(
    work: RecognitionWork,
    chunk: TranscriptionChunk,
) -> bool:
    midpoint_twice = chunk.start_ms + chunk.end_ms
    return 2 * work.core.start_ms <= midpoint_twice < 2 * work.core.end_ms


def _cluster_candidates(
    candidates: tuple[_Candidate, ...],
) -> tuple[tuple[_Candidate, ...], ...]:
    ordered = tuple(
        sorted(
            candidates,
            key=lambda item: (
                item.chunk.start_ms,
                item.chunk.end_ms,
                item.observation.work.sequence,
                item.chunk_index,
            ),
        )
    )
    parents = list(range(len(ordered)))

    def find(index: int) -> int:
        while parents[index] != index:
            parents[index] = parents[parents[index]]
            index = parents[index]
        return index

    def union(left_index: int, right_index: int) -> None:
        left_root = find(left_index)
        right_root = find(right_index)
        if left_root != right_root:
            parents[right_root] = left_root

    for left_index, left in enumerate(ordered):
        for right_index in range(left_index + 1, len(ordered)):
            right = ordered[right_index]
            if right.chunk.start_ms >= left.chunk.end_ms:
                break
            if _same_occurrence(left.chunk, right.chunk):
                union(left_index, right_index)

    grouped: dict[int, list[_Candidate]] = {}
    for index, candidate in enumerate(ordered):
        grouped.setdefault(find(index), []).append(candidate)
    return tuple(
        tuple(cluster)
        for cluster in sorted(
            grouped.values(),
            key=lambda cluster: (
                cluster[0].chunk.start_ms,
                cluster[0].chunk.end_ms,
                cluster[0].observation.work.sequence,
                cluster[0].chunk_index,
            ),
        )
    )


def _same_occurrence(
    left: TranscriptionChunk,
    right: TranscriptionChunk,
) -> bool:
    overlap_ms = min(left.end_ms, right.end_ms) - max(
        left.start_ms,
        right.start_ms,
    )
    return overlap_ms > 0


def _normalize_text(text: str) -> str:
    return "".join(
        character
        for character in text
        if not character.isspace()
        and not unicodedata.category(character).startswith("P")
    )


def _choose_candidate(
    candidates: Sequence[_Candidate],
) -> _Candidate:
    return max(candidates, key=_candidate_trust)


def _candidate_trust(
    candidate: _Candidate,
) -> tuple[int, int, int, int]:
    chunk = candidate.chunk
    work = candidate.observation.work
    return (
        1 if chunk.character_spans is not None else 0,
        _candidate_edge_distance(candidate),
        -work.sequence,
        -candidate.chunk_index,
    )


def _candidate_edge_distance(candidate: _Candidate) -> int:
    chunk = candidate.chunk
    request = candidate.observation.work.request
    midpoint_twice = chunk.start_ms + chunk.end_ms
    return min(
        midpoint_twice - 2 * request.start_ms,
        2 * request.end_ms - midpoint_twice,
    )


def _find_speech_gaps(
    speech: tuple[TimeInterval, ...],
    accepted: tuple[TranscriptionChunk, ...],
) -> tuple[TimeInterval, ...]:
    measured_coverage = tuple(
        interval for chunk in accepted for interval in _chunk_coverage(chunk)
    )
    coverage = _merge_intervals(
        measured_coverage,
        tolerance_ms=_POLICY.coverage_tolerance_ms,
    )
    gaps = []
    for speech_interval in speech:
        if not any(
            measured.end_ms > speech_interval.start_ms
            and measured.start_ms < speech_interval.end_ms
            for measured in measured_coverage
        ):
            gaps.append(speech_interval)
            continue
        intersecting = tuple(
            covered
            for covered in coverage
            if covered.end_ms > speech_interval.start_ms
            and covered.start_ms < speech_interval.end_ms
        )
        cursor = speech_interval.start_ms
        for covered in intersecting:
            if covered.start_ms - cursor > _POLICY.coverage_tolerance_ms:
                gaps.append(
                    TimeInterval(
                        cursor,
                        min(covered.start_ms, speech_interval.end_ms),
                    )
                )
            cursor = max(cursor, covered.end_ms)
            if cursor >= speech_interval.end_ms:
                break
        if speech_interval.end_ms - cursor > _POLICY.coverage_tolerance_ms:
            gaps.append(TimeInterval(cursor, speech_interval.end_ms))
    return tuple(gaps)


def _chunk_coverage(
    chunk: TranscriptionChunk,
) -> tuple[TimeInterval, ...]:
    if chunk.character_spans is None:
        return (TimeInterval(chunk.start_ms, chunk.end_ms),)
    return tuple(
        TimeInterval(span.start_ms, span.end_ms) for span in chunk.character_spans
    )


def _coverage_failure(
    analysis: _Analysis,
    *,
    reason_code: str,
    retry_count: int,
    recovery_count: int,
) -> TranscriptionFailure:
    regions = _merge_intervals(
        analysis.gaps + tuple(conflict.interval for conflict in analysis.conflicts),
        tolerance_ms=0,
    )
    return TranscriptionFailure(
        ErrorCode.TRANSCRIPTION_COVERAGE_INCOMPLETE,
        execution_facts=ExecutionFacts(
            cache_use=CacheUse.MISS,
            retry_count=retry_count,
            recovery_count=recovery_count,
        ),
        diagnostics={
            "gap_count": len(regions),
            "gap_duration_ms": sum(interval.duration_ms for interval in regions),
            "reason_code": reason_code,
        },
    )


def _merge_intervals(
    intervals: tuple[TimeInterval, ...],
    *,
    tolerance_ms: int,
) -> tuple[TimeInterval, ...]:
    merged: list[TimeInterval] = []
    for interval in sorted(intervals):
        if not merged or interval.start_ms - merged[-1].end_ms > tolerance_ms:
            merged.append(interval)
        else:
            merged[-1] = TimeInterval(
                merged[-1].start_ms,
                max(merged[-1].end_ms, interval.end_ms),
            )
    return tuple(merged)
