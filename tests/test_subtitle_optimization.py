import signal
from dataclasses import FrozenInstanceError, dataclass

import pytest

from video_auto_editor.cache import CacheRepository
from video_auto_editor.clip_planning import ClipPlanning
from video_auto_editor.configuration import Configuration, ConfigurationFailure
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode, RunStage
from video_auto_editor.runtime.identity import (
    CandidateId,
    RunId,
    ShortVideoId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.subtitle_optimization import (
    OptimizedShortVideoSubtitles,
    SubtitleDisplayBlock,
    SubtitleOptimization,
    SubtitleOptimizationFailure,
    SubtitleOptimizationRequest,
    SubtitleOptimizationSettings,
)
from video_auto_editor.text_model import (
    GenerationSettings,
    ReadinessReport,
    ReasoningEffort,
    TextGenerationResponse,
    TextModelExecutionFacts,
    TextModelFailure,
    TextModelFailureKind,
)
from video_auto_editor.transcription import (
    CharacterSpan,
    CompleteTranscript,
    SpeechPresence,
    TranscriptChunk,
    TranscriptionChunk,
)
from video_auto_editor.workspace import Workspace


@dataclass(frozen=True, slots=True)
class _CompleteTopicReview:
    reviews: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _CandidateReview:
    candidate_id: CandidateId
    topic_name: str = "字幕优化主题"
    topic_complete: bool = True
    learning_value: int = 9
    share_value: int = 8
    publish_ready_score: int = 95
    export_decision: str = "publish_ready"
    title: str = "可发布标题"
    summary: str = "可发布摘要"
    keywords: tuple[str, ...] = ("字幕",)
    needs_human_review: bool = False
    reject_reason: str = ""
    boundary_fix_suggestion: str = ""
    boundary_fix_start_ms: int | None = None
    boundary_fix_end_ms: int | None = None


class _NoRequestTextModel:
    def __init__(self, configuration_fingerprint: str = "a" * 64) -> None:
        self.request_count = 0
        self.configuration_fingerprint = configuration_fingerprint

    def check_readiness(self) -> ReadinessReport:
        return ReadinessReport(
            ready=True,
            configuration_fingerprint=self.configuration_fingerprint,
        )

    def generate(self, request):
        del request
        self.request_count += 1
        raise AssertionError("有效空结果不得调用字幕优化模型")


class _RespondingTextModel:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.requests = []

    def check_readiness(self) -> ReadinessReport:
        return ReadinessReport(
            ready=True,
            configuration_fingerprint="b" * 64,
        )

    def generate(self, request) -> TextGenerationResponse:
        self.requests.append(request)
        return TextGenerationResponse(
            text=self.response_text,
            execution_facts=TextModelExecutionFacts(
                transport_attempt_count=1,
                elapsed_ms=5,
                finish_reason="stop",
            ),
        )


class _EchoTextModel(_RespondingTextModel):
    def __init__(self) -> None:
        super().__init__("")

    def generate(self, request) -> TextGenerationResponse:
        self.response_text = request.messages[1].content
        return super().generate(request)


class _FailingTextModel(_RespondingTextModel):
    def __init__(self, failure: TextModelFailure) -> None:
        super().__init__("")
        self.failure = failure

    def generate(self, request) -> TextGenerationResponse:
        self.requests.append(request)
        raise self.failure


class _PayloadTextModel(_RespondingTextModel):
    def __init__(self, responder) -> None:
        super().__init__("")
        self.responder = responder

    def generate(self, request) -> TextGenerationResponse:
        self.response_text = self.responder(request.messages[1].content)
        return super().generate(request)


class _CancellingTextModel(_RespondingTextModel):
    def __init__(self, cancellation: CancellationSource) -> None:
        super().__init__("")
        self.cancellation = cancellation

    def generate(self, request) -> TextGenerationResponse:
        self.requests.append(request)
        self.cancellation.request(signal.SIGTERM)
        raise TextModelFailure(
            TextModelFailureKind.CANCELLED,
            execution_facts=TextModelExecutionFacts(
                transport_attempt_count=0,
                elapsed_ms=0,
            ),
            diagnostics={"signal_number": signal.SIGTERM},
        )


def _empty_inputs(tmp_path):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"source")
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=6,
        duration_ms=300_000,
    )
    transcript = CompleteTranscript._from_application(
        transcript_id=TranscriptId.new(),
        speech_presence=SpeechPresence.ABSENT,
        chunks=(),
    )
    configuration = Configuration.load(source_path)
    candidate_plan = ClipPlanning.prepare(
        source,
        transcript,
        configuration.course_context,
        configuration.effective.clip_policy,
    )
    return (
        ClipPlanning.finalize(
            candidate_plan,
            _CompleteTopicReview(()),
        ),
        transcript,
    )


def _settings(**overrides) -> SubtitleOptimizationSettings:
    values = {
        "adapter_id": "deterministic",
        "generation": GenerationSettings(
            model="deterministic-subtitle",
            temperature=0.0,
            reasoning_effort=ReasoningEffort.NONE,
        ),
        "window_max_chars": 100,
        "max_chars_per_line": 15,
        "max_lines": 2,
        "semantic_attempt_limit": 3,
    }
    values.update(overrides)
    return SubtitleOptimizationSettings(
        **values,
    )


def _published_inputs(
    tmp_path,
    chunks: tuple[TranscriptionChunk, ...],
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    source_path = tmp_path / "published-course.mp4"
    source_path.write_bytes(b"source")
    workspace = Workspace.open(
        source_path,
        tmp_path / "published-workspace",
    )
    assert workspace.source is not None
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("1" * 64),
        byte_length=6,
        duration_ms=max(300_000, max(chunk.end_ms for chunk in chunks)),
    )
    transcript = CompleteTranscript._from_application(
        transcript_id=TranscriptId.new(),
        speech_presence=SpeechPresence.PRESENT,
        chunks=tuple(
            TranscriptChunk._from_application(
                TranscriptChunkId.new(),
                chunk,
            )
            for chunk in chunks
        ),
    )
    configuration = Configuration.load(
        source_path,
        {
            "schema_version": "configuration.v1",
            "clip_policy": {
                "min_duration_seconds": 1,
                "target_duration_seconds": 60,
                "max_duration_seconds": 300,
            },
        },
    )
    candidate_plan = ClipPlanning.prepare(
        source,
        transcript,
        configuration.course_context,
        configuration.effective.clip_policy,
    )
    delivery_plan = ClipPlanning.finalize(
        candidate_plan,
        _CompleteTopicReview(
            tuple(
                _CandidateReview(candidate.candidate_id)
                for candidate in candidate_plan.candidates
            )
        ),
    )
    return delivery_plan, transcript


def test_empty_delivery_plan_succeeds_without_model_requests(tmp_path):
    delivery_plan, transcript = _empty_inputs(tmp_path)
    model = _NoRequestTextModel()
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    result = optimizer.optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert result.short_videos == ()
    assert result.execution_facts.short_video_count == 0
    assert result.execution_facts.window_count == 0
    assert result.execution_facts.model_request_count == 0
    assert model.request_count == 0


def test_readiness_is_local_repeatable_and_never_generates():
    model = _NoRequestTextModel()
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    first = optimizer.check_readiness()
    second = optimizer.check_readiness()

    assert first == second
    assert first.ready is True
    assert first.configuration_fingerprint == "a" * 64
    assert model.request_count == 0


@pytest.mark.parametrize(
    ("override", "field"),
    [
        (
            {"subtitle_optimization": {"enabled": False}},
            "subtitle_optimization.enabled",
        ),
        ({"burn_subtitles": False}, "burn_subtitles"),
        ({"export_subtitles": True}, "export_subtitles"),
    ],
)
def test_production_configuration_rejects_subtitle_disable_or_sidecar_switches(
    tmp_path,
    override,
    field,
):
    source = tmp_path / "strict-config.mp4"
    source.write_bytes(b"source")

    with pytest.raises(ConfigurationFailure) as raised:
        Configuration.load(
            source,
            {
                "schema_version": "configuration.v1",
                **override,
            },
        )

    assert raised.value.error_code is ErrorCode.CONFIG_SCHEMA_INVALID
    assert raised.value.diagnostics == {
        "field": field,
        "reason_code": "schema.unknown_field",
    }


def test_single_window_returns_aligned_blocks_from_deletion_only_prompt(
    tmp_path,
):
    text = "今天天气真好啊我们出门吧"
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=text,
                character_spans=tuple(
                    CharacterSpan(index * 1_000, (index + 1) * 1_000)
                    for index in range(len(text))
                ),
            ),
        ),
    )
    model = _RespondingTextModel("今天天气真好\n我们出门吧")
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    result = optimizer.optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert len(model.requests) == 1
    request = model.requests[0]
    assert request.observation.stage is RunStage.DELIVERY_BUILD
    assert request.messages[1].content == text
    system_prompt = request.messages[0].content
    assert "只删除字符" in system_prompt
    assert "不得新增" in system_prompt
    assert "不得改写" in system_prompt
    assert "不得换序" in system_prompt
    assert "不得增加空格" in system_prompt
    assert [item.short_video_id for item in result.short_videos] == [
        delivery_plan.short_videos[0].short_video_id
    ]
    blocks = result.short_videos[0].display_blocks
    assert [(block.start_ms, block.end_ms, block.text) for block in blocks] == [
        (0, 6_000, "今天天气真好"),
        (7_000, 12_000, "我们出门吧"),
    ]
    assert result.execution_facts.short_video_count == 1
    assert result.execution_facts.window_count == 1
    assert result.execution_facts.model_request_count == 1


def test_added_character_retries_then_raises_typed_failure(tmp_path):
    text = "今天天气真好"
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=text,
            ),
        ),
    )
    model = _RespondingTextModel("今天天气很好")
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        optimizer.optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.error_code is ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID
    assert raised.value.execution_facts.model_request_count == 3
    assert raised.value.execution_facts.semantic_retry_count == 2
    assert len(model.requests) == 3


@pytest.mark.parametrize(
    ("source_text", "response_text", "reason_code"),
    [
        ("今天天气真好", "今日天气真好", "output.character_added"),
        ("今天天气真好", "天气今天真好", "output.character_reordered"),
        ("今天天气真好", "今天天气真好 ", "output.character_added"),
        ("今天天气真好", "今天天气真好\n", "output.line_break_invalid"),
        ("今天\u2028真好", "今天\u2028真好", "output.line_break_invalid"),
        ("今天天气真好", "```\n今天天气真好\n```", "output.character_added"),
        ("今天天气真好", "", "output.structure_invalid"),
        ("甲" * 31, "甲" * 31, "output.display_constraint_failed"),
    ],
)
def test_strict_output_validation_rejects_every_non_extractable_shape(
    tmp_path,
    source_text,
    response_text,
    reason_code,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=source_text,
            ),
        ),
    )
    optimizer = SubtitleOptimization(
        _RespondingTextModel(response_text),
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(semantic_attempt_limit=1),
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        optimizer.optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.diagnostics["reason_code"] == reason_code


def test_alignment_prefers_character_spans_and_falls_back_per_chunk(
    tmp_path,
):
    fallback_text = "一二三四五六七八九十甲乙丙丁戊己庚辛壬癸"
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=30_000,
                text="甲乙",
                character_spans=(
                    CharacterSpan(1_000, 2_000),
                    CharacterSpan(5_000, 6_000),
                ),
            ),
            TranscriptionChunk(
                start_ms=30_000,
                end_ms=60_000,
                text=fallback_text,
            ),
        ),
    )
    model = _RespondingTextModel(f"甲乙\n{fallback_text}")
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    result = optimizer.optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    blocks = result.short_videos[0].display_blocks
    assert (
        blocks[0].start_ms,
        blocks[0].end_ms,
        blocks[0].text,
    ) == (1_000, 6_000, "甲乙")
    assert (blocks[1].start_ms, blocks[1].end_ms) == (30_000, 60_000)
    assert blocks[1].text.replace("\n", "") == fallback_text
    assert [len(line) for line in blocks[1].text.splitlines()] == [15, 5]


def test_conflicting_precise_spans_fail_alignment_even_inside_one_display_block(
    tmp_path,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=1_000,
                text="甲",
                character_spans=(CharacterSpan(0, 1_000),),
            ),
            TranscriptionChunk(
                start_ms=500,
                end_ms=1_500,
                text="乙",
                character_spans=(CharacterSpan(500, 600),),
            ),
        ),
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        SubtitleOptimization(
            _RespondingTextModel("甲乙"),
            CacheRepository.in_memory(application_version="4.7.0"),
            _settings(semantic_attempt_limit=1),
        ).optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.error_code is ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID
    assert raised.value.diagnostics["reason_code"] == "output.alignment_failed"


def test_fallback_shift_never_clips_a_precise_span(tmp_path):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=1_000,
                text="甲",
            ),
            TranscriptionChunk(
                start_ms=500,
                end_ms=1_500,
                text="乙",
            ),
            TranscriptionChunk(
                start_ms=700,
                end_ms=1_700,
                text="丙",
                character_spans=(CharacterSpan(700, 1_200),),
            ),
        ),
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        SubtitleOptimization(
            _RespondingTextModel("甲\n乙丙"),
            CacheRepository.in_memory(application_version="4.7.0"),
            _settings(semantic_attempt_limit=1),
        ).optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.diagnostics["reason_code"] == "output.alignment_failed"


def test_fallback_uses_non_overlapping_boundaries_for_ordinary_text(
    tmp_path,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=1_000,
                text="甲乙丙",
            ),
        ),
    )

    result = SubtitleOptimization(
        _RespondingTextModel("甲\n乙\n丙"),
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert [
        (block.start_ms, block.end_ms)
        for block in result.short_videos[0].display_blocks
    ] == [(0, 333), (333, 666), (666, 1_000)]


def test_dense_fallback_keeps_every_source_character_with_positive_time(
    tmp_path,
):
    text = "甲" * 2_000
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=1_000,
                text=text,
            ),
        ),
    )
    model = _RespondingTextModel("甲")
    result = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(window_max_chars=3_000),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert model.requests[0].messages[1].content == text
    assert [
        (
            block.start_ms,
            block.end_ms,
            block.text,
        )
        for block in result.short_videos[0].display_blocks
    ] == [(0, 1, "甲")]


def test_dense_fallback_assigns_non_overlapping_display_block_times(
    tmp_path,
):
    text = "甲" * 2_000
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=1_000,
                text=text,
            ),
        ),
    )

    result = SubtitleOptimization(
        _RespondingTextModel("甲\n甲"),
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(
            window_max_chars=3_000,
            semantic_attempt_limit=1,
        ),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert [
        (block.start_ms, block.end_ms)
        for block in result.short_videos[0].display_blocks
    ] == [(0, 1), (1, 2)]


def test_unalignable_short_video_raises_typed_failure_without_model_request(
    tmp_path,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text="交付方案引用的原始转写",
            ),
        ),
    )
    current_transcript = CompleteTranscript._from_application(
        transcript_id=transcript.transcript_id,
        speech_presence=SpeechPresence.PRESENT,
        chunks=(
            TranscriptChunk._from_application(
                TranscriptChunkId.new(),
                TranscriptionChunk(
                    start_ms=120_000,
                    end_ms=180_000,
                    text="当前转写没有覆盖待发布短视频",
                ),
            ),
        ),
    )
    model = _NoRequestTextModel()

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        SubtitleOptimization(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _settings(),
        ).optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=current_transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.error_code is ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_INVALID
    assert raised.value.diagnostics["reason_code"] == "output.alignment_failed"
    assert raised.value.execution_facts.model_request_count == 0
    assert model.request_count == 0


def test_chunk_grouping_fills_budget_before_starting_next_window(tmp_path):
    texts = ("甲乙", "丙丁戊", "己")
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        tuple(
            TranscriptionChunk(
                start_ms=index * 20_000,
                end_ms=(index + 1) * 20_000,
                text=text,
            )
            for index, text in enumerate(texts)
        ),
    )
    model = _EchoTextModel()

    result = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(window_max_chars=5),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert sorted(request.messages[1].content for request in model.requests) == [
        "己",
        "甲乙丙丁戊",
    ]
    assert result.execution_facts.window_count == 2


def test_equal_time_chunks_preserve_transcript_order_in_model_input(tmp_path):
    source_chunks = (
        TranscriptionChunk(
            start_ms=0,
            end_ms=60_000,
            text="甲",
        ),
        TranscriptionChunk(
            start_ms=0,
            end_ms=60_000,
            text="乙",
        ),
    )
    delivery_plan, transcript = _published_inputs(tmp_path, source_chunks)
    current_transcript = CompleteTranscript._from_application(
        transcript_id=transcript.transcript_id,
        speech_presence=SpeechPresence.PRESENT,
        chunks=(
            TranscriptChunk._from_application(
                TranscriptChunkId(
                    "transcript_chunk_00000000-0000-4000-8000-000000000002"
                ),
                source_chunks[0],
            ),
            TranscriptChunk._from_application(
                TranscriptChunkId(
                    "transcript_chunk_00000000-0000-4000-8000-000000000001"
                ),
                source_chunks[1],
            ),
        ),
    )
    model = _EchoTextModel()

    SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=current_transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert {request.messages[1].content for request in model.requests} == {"甲乙"}


def test_all_short_videos_complete_all_whole_chunk_windows(tmp_path):
    texts = (
        "甲乙丙丁戊己庚辛",
        "壬癸子丑",
        "寅卯辰巳",
        "午未申酉",
    )
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        tuple(
            TranscriptionChunk(
                start_ms=index * 30_000,
                end_ms=(index + 1) * 30_000,
                text=text,
            )
            for index, text in enumerate(texts)
        ),
    )
    model = _EchoTextModel()
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(window_max_chars=5),
    )

    result = optimizer.optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert len(delivery_plan.short_videos) == 2
    assert [request.messages[1].content for request in model.requests] == list(texts)
    assert [item.short_video_id for item in result.short_videos] == [
        item.short_video_id for item in delivery_plan.short_videos
    ]
    assert [
        block.text for item in result.short_videos for block in item.display_blocks
    ] == list(texts)
    assert result.execution_facts.short_video_count == 2
    assert result.execution_facts.window_count == 4
    assert result.execution_facts.model_request_count == 4


def test_late_window_failure_returns_no_partial_result_but_keeps_valid_cache(
    tmp_path,
):
    texts = (
        "第一窗口",
        "第二窗口",
        "第三窗口",
        "第四窗口",
    )
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        tuple(
            TranscriptionChunk(
                start_ms=index * 30_000,
                end_ms=(index + 1) * 30_000,
                text=text,
            )
            for index, text in enumerate(texts)
        ),
    )
    cache = CacheRepository.in_memory(application_version="4.7.0")
    failing_model = _PayloadTextModel(
        lambda text: "模型新增字符" if text == texts[-1] else text
    )
    request = SubtitleOptimizationRequest(
        delivery_plan=delivery_plan,
        transcript=transcript,
        run_id=RunId.new(),
        cancellation=CancellationSource().token,
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        SubtitleOptimization(
            failing_model,
            cache,
            _settings(
                window_max_chars=5,
                semantic_attempt_limit=1,
            ),
        ).optimize(request)

    assert raised.value.execution_facts.model_request_count == 4
    recovery_model = _EchoTextModel()
    recovered = SubtitleOptimization(
        recovery_model,
        cache,
        _settings(
            window_max_chars=5,
            semantic_attempt_limit=1,
        ),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert [item.short_video_id for item in recovered.short_videos] == [
        item.short_video_id for item in delivery_plan.short_videos
    ]
    assert recovered.execution_facts.cache_hit_count == 3
    assert recovered.execution_facts.cache_miss_count == 1
    assert recovered.execution_facts.model_request_count == 1
    assert [
        model_request.messages[1].content for model_request in recovery_model.requests
    ] == [texts[-1]]


def test_cache_hit_revalidates_and_realigns_with_current_character_times(
    tmp_path,
):
    text = "今天天气真好啊我们出门吧"
    first_plan, first_transcript = _published_inputs(
        tmp_path / "first",
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=text,
                character_spans=tuple(
                    CharacterSpan(index * 1_000, (index + 1) * 1_000)
                    for index in range(len(text))
                ),
            ),
        ),
    )
    second_plan, second_transcript = _published_inputs(
        tmp_path / "second",
        (
            TranscriptionChunk(
                start_ms=60_000,
                end_ms=120_000,
                text=text,
                character_spans=tuple(
                    CharacterSpan(
                        80_000 + index * 1_000,
                        81_000 + index * 1_000,
                    )
                    for index in range(len(text))
                ),
            ),
        ),
    )
    cache = CacheRepository.in_memory(application_version="4.7.0")
    first_model = _RespondingTextModel("今天天气真好\n我们出门吧")
    first = SubtitleOptimization(first_model, cache, _settings()).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=first_plan,
            transcript=first_transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )
    second_model = _NoRequestTextModel("b" * 64)

    second = SubtitleOptimization(second_model, cache, _settings()).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=second_plan,
            transcript=second_transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert first.execution_facts.cache_miss_count == 1
    assert second.execution_facts.cache_hit_count == 1
    assert second.execution_facts.model_request_count == 0
    assert second_model.request_count == 0
    assert [
        (block.start_ms, block.end_ms) for block in first.short_videos[0].display_blocks
    ] == [(0, 6_000), (7_000, 12_000)]
    assert [
        (block.start_ms, block.end_ms)
        for block in second.short_videos[0].display_blocks
    ] == [(80_000, 86_000), (87_000, 92_000)]


def test_cache_reuses_same_actual_window_when_budget_or_attempt_limit_changes(
    tmp_path,
):
    text = "实际子窗口正文"
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=text,
            ),
        ),
    )
    cache = CacheRepository.in_memory(application_version="4.7.0")
    request = SubtitleOptimizationRequest(
        delivery_plan=delivery_plan,
        transcript=transcript,
        run_id=RunId.new(),
        cancellation=CancellationSource().token,
    )
    SubtitleOptimization(
        _RespondingTextModel(text),
        cache,
        _settings(
            window_max_chars=len(text),
            semantic_attempt_limit=1,
        ),
    ).optimize(request)
    cached_model = _NoRequestTextModel("b" * 64)

    cached = SubtitleOptimization(
        cached_model,
        cache,
        _settings(
            window_max_chars=100,
            semantic_attempt_limit=8,
        ),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert cached.execution_facts.cache_hit_count == 1
    assert cached.execution_facts.model_request_count == 0
    assert cached_model.request_count == 0


@pytest.mark.parametrize(
    "changed_input",
    [
        "display_constraints",
        "model_settings",
        "configuration_fingerprint",
    ],
)
def test_cache_selectively_misses_when_result_affecting_input_changes(
    tmp_path,
    changed_input,
):
    text = "缓存身份必须选择性失效"
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text=text,
            ),
        ),
    )
    cache = CacheRepository.in_memory(application_version="4.7.0")
    SubtitleOptimization(
        _RespondingTextModel(text),
        cache,
        _settings(),
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )
    changed_settings = _settings()
    changed_model = _RespondingTextModel(text)
    if changed_input == "display_constraints":
        changed_settings = _settings(max_chars_per_line=14)
    elif changed_input == "model_settings":
        changed_settings = _settings(
            generation=GenerationSettings(
                model="deterministic-subtitle-v2",
                temperature=0.0,
                reasoning_effort=ReasoningEffort.NONE,
            )
        )
    else:
        changed_model = _RespondingTextModel(text)
        changed_model.check_readiness = lambda: ReadinessReport(
            ready=True,
            configuration_fingerprint="c" * 64,
        )

    result = SubtitleOptimization(
        changed_model,
        cache,
        changed_settings,
    ).optimize(
        SubtitleOptimizationRequest(
            delivery_plan=delivery_plan,
            transcript=transcript,
            run_id=RunId.new(),
            cancellation=CancellationSource().token,
        )
    )

    assert len(changed_model.requests) == 1
    assert result.execution_facts.cache_hit_count == 0
    assert result.execution_facts.cache_miss_count == 1


@pytest.mark.parametrize(
    ("failure_kind", "error_code"),
    [
        (
            TextModelFailureKind.AUTHENTICATION_FAILED,
            ErrorCode.SUBTITLE_OPTIMIZATION_AUTHENTICATION_FAILED,
        ),
        (
            TextModelFailureKind.REQUEST_REJECTED,
            ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_REJECTED,
        ),
        (
            TextModelFailureKind.RATE_LIMITED,
            ErrorCode.SUBTITLE_OPTIMIZATION_RATE_LIMITED,
        ),
        (
            TextModelFailureKind.REQUEST_TIMEOUT,
            ErrorCode.SUBTITLE_OPTIMIZATION_REQUEST_TIMEOUT,
        ),
        (
            TextModelFailureKind.SERVICE_UNAVAILABLE,
            ErrorCode.SUBTITLE_OPTIMIZATION_SERVICE_UNAVAILABLE,
        ),
        (
            TextModelFailureKind.RESPONSE_PROTOCOL_INVALID,
            ErrorCode.SUBTITLE_OPTIMIZATION_RESPONSE_PROTOCOL_INVALID,
        ),
        (
            TextModelFailureKind.GENERATION_REFUSED,
            ErrorCode.SUBTITLE_OPTIMIZATION_GENERATION_REFUSED,
        ),
        (
            TextModelFailureKind.OUTPUT_TRUNCATED,
            ErrorCode.SUBTITLE_OPTIMIZATION_OUTPUT_TRUNCATED,
        ),
    ],
)
def test_every_external_model_failure_maps_to_subtitle_namespace(
    tmp_path,
    failure_kind,
    error_code,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text="模型失败必须保持稳定分类",
            ),
        ),
    )
    model = _FailingTextModel(
        TextModelFailure(
            failure_kind,
            execution_facts=TextModelExecutionFacts(
                transport_attempt_count=1,
                elapsed_ms=5,
            ),
        )
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        SubtitleOptimization(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _settings(),
        ).optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.error_code is error_code
    assert raised.value.execution_facts.model_request_count == 1


def test_text_model_failure_maps_to_subtitle_failure_with_execution_facts(
    tmp_path,
):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text="模型请求失败",
            ),
        ),
    )
    model = _FailingTextModel(
        TextModelFailure(
            TextModelFailureKind.RATE_LIMITED,
            execution_facts=TextModelExecutionFacts(
                transport_attempt_count=2,
                elapsed_ms=50,
            ),
            diagnostics={"http_status": 429},
        )
    )
    optimizer = SubtitleOptimization(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _settings(),
    )

    with pytest.raises(SubtitleOptimizationFailure) as raised:
        optimizer.optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=CancellationSource().token,
            )
        )

    assert raised.value.error_code is ErrorCode.SUBTITLE_OPTIMIZATION_RATE_LIMITED
    facts = raised.value.execution_facts
    assert facts.model_request_count == 1
    assert facts.transport_attempt_count == 2
    assert facts.transport_retry_count == 1
    assert raised.value.diagnostics == {"http_status": 429}


def test_text_model_cancellation_restores_root_cancellation(tmp_path):
    delivery_plan, transcript = _published_inputs(
        tmp_path,
        (
            TranscriptionChunk(
                start_ms=0,
                end_ms=60_000,
                text="取消字幕优化",
            ),
        ),
    )
    cancellation = CancellationSource()
    model = _CancellingTextModel(cancellation)

    with pytest.raises(CancellationRequested) as raised:
        SubtitleOptimization(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _settings(),
        ).optimize(
            SubtitleOptimizationRequest(
                delivery_plan=delivery_plan,
                transcript=transcript,
                run_id=RunId.new(),
                cancellation=cancellation.token,
            )
        )

    assert raised.value.signal_number == signal.SIGTERM
    assert len(model.requests) == 1


def test_public_subtitle_result_values_enforce_delivery_invariants():
    with pytest.raises(ValueError, match="晚于"):
        SubtitleDisplayBlock(
            start_ms=1_000,
            end_ms=1_000,
            text="非法时间",
        )
    with pytest.raises(ValueError, match="不能为空"):
        SubtitleDisplayBlock(
            start_ms=0,
            end_ms=1_000,
            text="",
        )
    with pytest.raises(ValueError, match="换行"):
        SubtitleDisplayBlock(
            start_ms=0,
            end_ms=1_000,
            text="非法\u2028换行",
        )
    short_video_id = ShortVideoId.new()
    with pytest.raises(ValueError, match="至少一个"):
        OptimizedShortVideoSubtitles(
            short_video_id=short_video_id,
            display_blocks=(),
        )
    with pytest.raises(ValueError, match="有序且不能重叠"):
        OptimizedShortVideoSubtitles(
            short_video_id=short_video_id,
            display_blocks=(
                SubtitleDisplayBlock(0, 1_000, "第一块"),
                SubtitleDisplayBlock(900, 2_000, "第二块"),
            ),
        )
    block = SubtitleDisplayBlock(0, 1_000, "合法显示块")
    with pytest.raises(FrozenInstanceError):
        block.text = "不可修改"
