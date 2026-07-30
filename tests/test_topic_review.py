import json
import signal
from dataclasses import dataclass

import pytest

from video_auto_editor.cache import CacheRepository
from video_auto_editor.clip_planning import ClipPlanning
from video_auto_editor.configuration import Configuration
from video_auto_editor.runtime import ResultKind
from video_auto_editor.runtime.cancellation import (
    CancellationRequested,
    CancellationSource,
)
from video_auto_editor.runtime.errors import ErrorCode
from video_auto_editor.runtime.identity import (
    RunId,
    TranscriptChunkId,
    TranscriptId,
)
from video_auto_editor.source_analysis import SourceDescription
from video_auto_editor.text_model import (
    DeterministicTextModel,
    DeterministicTextModelScript,
    GenerationSettings,
    ReadinessReport,
    ReasoningEffort,
    TextGenerationResponse,
    TextModelExecutionFacts,
    TextModelFailure,
    TextModelFailureKind,
)
from video_auto_editor.topic_review import (
    TopicReview,
    TopicReviewFailure,
    TopicReviewRequest,
    TopicReviewSettings,
)
from video_auto_editor.workspace import Workspace


@dataclass(frozen=True, slots=True)
class _CompleteTranscript:
    transcript_id: TranscriptId
    chunks: tuple[object, ...]


@dataclass(frozen=True, slots=True)
class _TranscriptChunk:
    transcript_chunk_id: TranscriptChunkId
    start_ms: int
    end_ms: int
    text: str


class _RecordingTextModelEvents:
    def __init__(self) -> None:
        self.events = []

    def record(self, event) -> None:
        self.events.append(event)


class _RecordingTextModel:
    def __init__(self, model, requests) -> None:
        self._model = model
        self._requests = requests

    def check_readiness(self):
        return self._model.check_readiness()

    def generate(self, request):
        self._requests.append(request)
        return self._model.generate(request)


class _AttemptAwareTextModel:
    def __init__(self, responses_by_attempt) -> None:
        self._responses_by_attempt = dict(responses_by_attempt)
        self.requests = []

    def check_readiness(self):
        return ReadinessReport(
            ready=True,
            configuration_fingerprint="a" * 64,
        )

    def generate(self, request):
        self.requests.append(request)
        attempt = len(request.messages) - 1
        return _text_response(self._responses_by_attempt[attempt])


class _PayloadAwareTextModel:
    def __init__(self, responder, *, configuration_fingerprint="b" * 64) -> None:
        self._responder = responder
        self._configuration_fingerprint = configuration_fingerprint
        self.readiness_checks = 0
        self.requests = []

    def check_readiness(self):
        self.readiness_checks += 1
        return ReadinessReport(
            ready=True,
            configuration_fingerprint=self._configuration_fingerprint,
        )

    def generate(self, request):
        self.requests.append(request)
        payload = json.loads(request.messages[1].content)
        response = self._responder(payload)
        if isinstance(response, TextModelFailure):
            raise response
        return _text_response(response)


def _candidate_plan(
    tmp_path,
    texts=(),
    *,
    course_context=None,
    chunk_duration_ms=180_000,
):
    source_path = tmp_path / "course.mp4"
    source_path.write_bytes(b"source")
    if course_context is not None:
        source_path.with_suffix(".context.json").write_text(
            json.dumps(
                {
                    "schema_version": "course_context.v1",
                    **course_context,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    workspace = Workspace.open(source_path, tmp_path / "workspace")
    assert workspace.source is not None
    duration_ms = max(300_000, len(texts) * chunk_duration_ms)
    source = SourceDescription._from_analysis(
        source_file=workspace.source,
        sha256="sha256:" + ("0" * 64),
        byte_length=6,
        duration_ms=duration_ms,
    )
    loaded = Configuration.load(source_path)
    return ClipPlanning.prepare(
        source,
        _CompleteTranscript(
            TranscriptId.new(),
            tuple(
                _TranscriptChunk(
                    transcript_chunk_id=TranscriptChunkId.new(),
                    start_ms=index * chunk_duration_ms,
                    end_ms=(index + 1) * chunk_duration_ms,
                    text=text,
                )
                for index, text in enumerate(texts)
            ),
        ),
        loaded.course_context,
        loaded.effective.clip_policy,
    )


def _text_response(text="不会用于空候选"):
    return TextGenerationResponse(
        text=text,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )


def _topic_reviewer(
    *,
    event_sink=None,
    request_log=None,
    response_text=None,
    batch_size=3,
):
    model = DeterministicTextModel(
        DeterministicTextModelScript.succeed(
            _text_response("不会用于空候选" if response_text is None else response_text)
        ),
        event_sink=event_sink,
    )
    if request_log is not None:
        model = _RecordingTextModel(model, request_log)
    return TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(batch_size=batch_size),
    )


def _review_settings(*, batch_size=3, semantic_attempt_limit=3):
    return TopicReviewSettings(
        adapter_id="deterministic",
        generation=GenerationSettings(
            model="deterministic-topic-review",
            temperature=0.2,
            reasoning_effort=ReasoningEffort.NONE,
            max_output_tokens=4096,
        ),
        candidate_batch_size=batch_size,
        semantic_attempt_limit=semantic_attempt_limit,
    )


def test_empty_candidate_plan_is_a_zero_request_success_and_finalizes_empty(
    tmp_path,
):
    candidate_plan = _candidate_plan(tmp_path)
    events = _RecordingTextModelEvents()
    reviewer = _topic_reviewer(event_sink=events)

    result = reviewer.review(
        TopicReviewRequest(
            candidate_plan=candidate_plan,
            run_id=RunId.new(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )
    )
    delivery_plan = ClipPlanning.finalize(candidate_plan, result)

    assert result.reviews == ()
    assert result.execution_facts.batch_count == 0
    assert result.execution_facts.model_request_count == 0
    assert events.events == []
    assert delivery_plan.result_kind is ResultKind.EMPTY
    assert delivery_plan.candidates == ()
    assert delivery_plan.short_videos == ()
    assert delivery_plan.series == ()


def test_check_readiness_is_repeatable_and_never_generates(tmp_path):
    model = _PayloadAwareTextModel(_valid_response_for_batch)
    reviewer = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(),
    )

    first = reviewer.check_readiness()
    second = reviewer.check_readiness()

    assert first.ready is True
    assert first.configuration_fingerprint == "b" * 64
    assert second == first
    assert model.readiness_checks == 2
    assert model.requests == []


def _review_payload(candidate_key, *, topic_name, title):
    return {
        "candidate_key": candidate_key,
        "topic_name": topic_name,
        "topic_complete": True,
        "learning_value": 9,
        "share_value": 8,
        "publish_ready_score": 92,
        "export_decision": "publish_ready",
        "title": title,
        "summary": f"{topic_name}的完整摘要",
        "keywords": [topic_name, "课程"],
        "needs_human_review": False,
        "reject_reason": "",
        "boundary_fix_suggestion": "",
        "boundary_fix_start_ms": None,
        "boundary_fix_end_ms": None,
    }


def _valid_response_for_batch(payload):
    return json.dumps(
        {
            "reviews": [
                _review_payload(
                    candidate["candidate_key"],
                    topic_name=(
                        "主题-"
                        + "".join(excerpt["text"] for excerpt in candidate["content"])
                    ),
                    title=f"标题-{candidate['candidate_key']}",
                )
                for candidate in payload["candidates"]
            ]
        },
        ensure_ascii=False,
    )


def test_review_sends_neighbor_context_course_context_and_constraints_once(
    tmp_path,
):
    candidate_plan = _candidate_plan(
        tmp_path,
        ("第一段候选正文", "第二段候选正文"),
        course_context={
            "course_topic": "结构化直播拆条",
            "attribution": "示例课程",
            "priority_topics": ["第一主题"],
            "excluded_content": ["广告口播"],
        },
    )
    response_text = json.dumps(
        {
            "reviews": [
                _review_payload(
                    "candidate_2",
                    topic_name="第二主题",
                    title="第二条标题",
                ),
                _review_payload(
                    "candidate_1",
                    topic_name="第一主题",
                    title="第一条标题",
                ),
            ]
        },
        ensure_ascii=False,
    )
    events = _RecordingTextModelEvents()
    requests = []
    reviewer = _topic_reviewer(
        event_sink=events,
        request_log=requests,
        response_text=response_text,
        batch_size=2,
    )
    original_candidates = candidate_plan.candidates

    result = reviewer.review(
        TopicReviewRequest(
            candidate_plan=candidate_plan,
            run_id=RunId.new(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )
    )

    assert [
        (review.candidate_id, review.topic_name, review.title)
        for review in result.reviews
    ] == [
        (
            candidate_plan.candidates[0].candidate_id,
            "第一主题",
            "第一条标题",
        ),
        (
            candidate_plan.candidates[1].candidate_id,
            "第二主题",
            "第二条标题",
        ),
    ]
    assert candidate_plan.candidates is original_candidates
    assert result.execution_facts.batch_count == 1
    assert result.execution_facts.model_request_count == 1
    call_started = [
        event for event in events.events if event.kind.value == "call_started"
    ]
    assert len(call_started) == 1
    assert len(requests) == 1
    assert requests[0].observation.stage.value == "topic_review"
    user_payload = json.loads(requests[0].messages[1].content)
    assert user_payload["course_context"] == {
        "attribution": "示例课程",
        "course_topic": "结构化直播拆条",
        "excluded_content": ["广告口播"],
        "priority_topics": ["第一主题"],
    }
    assert user_payload["review_constraints"] == {
        "max_duration_ms": 300_000,
        "min_duration_ms": 60_000,
        "publish_ready_threshold": 80,
        "source_duration_ms": 360_000,
        "target_duration_ms": 180_000,
    }
    assert [
        {
            "candidate_key": candidate["candidate_key"],
            "preceding": [item["text"] for item in candidate["preceding"]],
            "content": [item["text"] for item in candidate["content"]],
            "following": [item["text"] for item in candidate["following"]],
        }
        for candidate in user_payload["candidates"]
    ] == [
        {
            "candidate_key": "candidate_1",
            "preceding": [],
            "content": ["第一段候选正文"],
            "following": ["第二段候选正文"],
        },
        {
            "candidate_key": "candidate_2",
            "preceding": ["第一段候选正文"],
            "content": ["第二段候选正文"],
            "following": [],
        },
    ]
    prompt = "\n".join(message.content for message in requests[0].messages)
    assert all(
        str(candidate.candidate_id) not in prompt
        for candidate in candidate_plan.candidates
    )
    assert all(
        str(chunk_id) not in prompt
        for candidate in candidate_plan.candidates
        for chunk_id in candidate.transcript_chunk_ids
    )


def test_invalid_score_is_semantically_retried_then_validated(tmp_path):
    candidate_plan = _candidate_plan(tmp_path, ("单个候选正文",))
    invalid_review = _review_payload(
        "candidate_1",
        topic_name="重试主题",
        title="重试标题",
    )
    invalid_review["publish_ready_score"] = 101
    valid_review = {
        **invalid_review,
        "publish_ready_score": 93,
    }
    model = _AttemptAwareTextModel(
        {
            1: json.dumps({"reviews": [invalid_review]}, ensure_ascii=False),
            2: json.dumps({"reviews": [valid_review]}, ensure_ascii=False),
        }
    )
    reviewer = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(),
    )

    result = reviewer.review(
        TopicReviewRequest(
            candidate_plan=candidate_plan,
            run_id=RunId.new(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )
    )

    assert result.reviews[0].publish_ready_score == 93
    assert len(model.requests) == 2
    correction = json.loads(model.requests[1].messages[-1].content)
    assert correction == {
        "failed_attempt": 1,
        "reason_code": "output.score_invalid",
        "semantic_attempt": 2,
        "instruction": "重新返回当前批次的完整严格 JSON，不得省略任何候选。",
    }
    assert result.execution_facts.model_request_count == 2
    assert result.execution_facts.semantic_retry_count == 1
    assert result.execution_facts.transport_retry_count == 0


def test_transport_retries_remain_inside_one_text_model_request(tmp_path):
    candidate_plan = _candidate_plan(tmp_path, ("传输重试候选",))
    response = TextGenerationResponse(
        text=json.dumps(
            {
                "reviews": [
                    _review_payload(
                        "candidate_1",
                        topic_name="传输职责",
                        title="传输职责标题",
                    )
                ]
            },
            ensure_ascii=False,
        ),
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=3,
            elapsed_ms=50,
        ),
    )
    requests = []
    model = _RecordingTextModel(
        DeterministicTextModel(
            DeterministicTextModelScript.succeed(response),
        ),
        requests,
    )

    result = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(),
    ).review(
        TopicReviewRequest(
            candidate_plan,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )

    assert len(requests) == 1
    assert result.execution_facts.model_request_count == 1
    assert result.execution_facts.semantic_retry_count == 0
    assert result.execution_facts.transport_attempt_count == 3
    assert result.execution_facts.transport_retry_count == 2


def test_semantic_retry_exhaustion_raises_typed_failure_without_a_result(
    tmp_path,
):
    candidate_plan = _candidate_plan(tmp_path, ("始终非法的候选",))
    invalid_review = _review_payload(
        "candidate_1",
        topic_name="非法主题",
        title="非法标题",
    )
    invalid_review["learning_value"] = True
    invalid_response = json.dumps(
        {"reviews": [invalid_review]},
        ensure_ascii=False,
    )
    model = _AttemptAwareTextModel(
        {
            1: invalid_response,
            2: invalid_response,
            3: invalid_response,
        }
    )
    reviewer = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(semantic_attempt_limit=3),
    )

    with pytest.raises(TopicReviewFailure) as captured:
        reviewer.review(
            TopicReviewRequest(
                candidate_plan=candidate_plan,
                run_id=RunId.new(),
                cancellation=CancellationSource(clock=lambda: 0.0).token,
            )
        )

    failure = captured.value
    assert failure.error_code is ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID
    assert failure.diagnostics == {
        "attempt": 3,
        "reason_code": "output.score_invalid",
    }
    assert failure.execution_facts.batch_count == 1
    assert failure.execution_facts.model_request_count == 3
    assert failure.execution_facts.semantic_retry_count == 2
    assert len(model.requests) == 3
    assert not hasattr(failure, "reviews")


@pytest.mark.parametrize(
    ("case", "reason_code"),
    [
        ("root_array", "output.structure_invalid"),
        ("unknown_field", "output.structure_invalid"),
        ("duplicate_json_key", "output.structure_invalid"),
        ("non_finite_score", "output.structure_invalid"),
        ("candidate_missing", "output.candidate_missing"),
        ("candidate_duplicate", "output.candidate_duplicate"),
        ("candidate_unknown", "output.candidate_unknown"),
        ("score_boolean", "output.score_invalid"),
        ("boundary_outside_candidate", "output.boundary_invalid"),
        ("boundary_shrinks_candidate", "output.boundary_invalid"),
        ("boundary_shifts_candidate", "output.boundary_invalid"),
        ("boundary_unchanged", "output.boundary_invalid"),
        ("boundary_suggestion_without_range", "output.boundary_invalid"),
        ("publish_ready_incomplete", "output.constraint_failed"),
    ],
)
def test_model_output_is_strictly_validated_at_the_public_review_seam(
    tmp_path,
    case,
    reason_code,
):
    candidate_plan = _candidate_plan(tmp_path, ("严格校验候选",))
    review = _review_payload(
        "candidate_1",
        topic_name="严格主题",
        title="严格标题",
    )
    if case == "root_array":
        response = "[]"
    elif case == "unknown_field":
        response = json.dumps(
            {"reviews": [{**review, "unexpected": "forbidden"}]},
            ensure_ascii=False,
        )
    elif case == "duplicate_json_key":
        response = (
            '{"reviews":[' + json.dumps(review, ensure_ascii=False) + '],"reviews":[]}'
        )
    elif case == "non_finite_score":
        response = json.dumps(
            {"reviews": [{**review, "publish_ready_score": float("nan")}]},
            ensure_ascii=False,
        )
    elif case == "candidate_missing":
        response = '{"reviews":[]}'
    elif case == "candidate_duplicate":
        response = json.dumps(
            {"reviews": [review, review]},
            ensure_ascii=False,
        )
    elif case == "candidate_unknown":
        response = json.dumps(
            {"reviews": [{**review, "candidate_key": "candidate_unknown"}]},
            ensure_ascii=False,
        )
    elif case == "score_boolean":
        response = json.dumps(
            {"reviews": [{**review, "share_value": True}]},
            ensure_ascii=False,
        )
    elif case == "boundary_outside_candidate":
        response = json.dumps(
            {
                "reviews": [
                    {
                        **review,
                        "boundary_fix_suggestion": "扩展到候选之外",
                        "boundary_fix_start_ms": 190_000,
                        "boundary_fix_end_ms": 260_000,
                    }
                ]
            },
            ensure_ascii=False,
        )
    elif case == "boundary_shrinks_candidate":
        response = json.dumps(
            {
                "reviews": [
                    {
                        **review,
                        "boundary_fix_suggestion": "向内缩短不属于补救",
                        "boundary_fix_start_ms": 0,
                        "boundary_fix_end_ms": 120_000,
                    }
                ]
            },
            ensure_ascii=False,
        )
    elif case == "boundary_shifts_candidate":
        response = json.dumps(
            {
                "reviews": [
                    {
                        **review,
                        "boundary_fix_suggestion": "平移不属于补救",
                        "boundary_fix_start_ms": 60_000,
                        "boundary_fix_end_ms": 240_000,
                    }
                ]
            },
            ensure_ascii=False,
        )
    elif case == "boundary_unchanged":
        response = json.dumps(
            {
                "reviews": [
                    {
                        **review,
                        "boundary_fix_suggestion": "未实际扩展",
                        "boundary_fix_start_ms": 0,
                        "boundary_fix_end_ms": 180_000,
                    }
                ]
            },
            ensure_ascii=False,
        )
    elif case == "boundary_suggestion_without_range":
        response = json.dumps(
            {
                "reviews": [
                    {
                        **review,
                        "boundary_fix_suggestion": "缺少补救范围",
                    }
                ]
            },
            ensure_ascii=False,
        )
    else:
        response = json.dumps(
            {"reviews": [{**review, "topic_complete": False}]},
            ensure_ascii=False,
        )
    model = _AttemptAwareTextModel({1: response})
    reviewer = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(semantic_attempt_limit=1),
    )

    with pytest.raises(TopicReviewFailure) as captured:
        reviewer.review(
            TopicReviewRequest(
                candidate_plan=candidate_plan,
                run_id=RunId.new(),
                cancellation=CancellationSource(clock=lambda: 0.0).token,
            )
        )

    assert captured.value.diagnostics["reason_code"] == reason_code
    assert len(model.requests) == 1


def test_text_model_failure_is_translated_once_without_semantic_retry(
    tmp_path,
):
    candidate_plan = _candidate_plan(tmp_path, ("供应商失败候选",))
    model_failure = TextModelFailure(
        TextModelFailureKind.RATE_LIMITED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=3,
            elapsed_ms=120,
        ),
        diagnostics={
            "attempt": 3,
            "http_status": 429,
            "reason_code": "rate_limit.requests",
        },
    )
    requests = []
    model = _RecordingTextModel(
        DeterministicTextModel(DeterministicTextModelScript.fail(model_failure)),
        requests,
    )
    reviewer = TopicReview(
        model,
        CacheRepository.in_memory(application_version="4.7.0"),
        _review_settings(),
    )

    with pytest.raises(TopicReviewFailure) as captured:
        reviewer.review(
            TopicReviewRequest(
                candidate_plan=candidate_plan,
                run_id=RunId.new(),
                cancellation=CancellationSource(clock=lambda: 0.0).token,
            )
        )

    failure = captured.value
    assert failure.error_code is ErrorCode.TOPIC_REVIEW_RATE_LIMITED
    assert failure.diagnostics == {
        "attempt": 3,
        "http_status": 429,
        "reason_code": "rate_limit.requests",
    }
    assert failure.execution_facts.model_request_count == 1
    assert failure.execution_facts.semantic_retry_count == 0
    assert failure.execution_facts.transport_attempt_count == 3
    assert failure.execution_facts.transport_retry_count == 2
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("failure_kind", "error_code"),
    [
        (
            TextModelFailureKind.AUTHENTICATION_FAILED,
            ErrorCode.TOPIC_REVIEW_AUTHENTICATION_FAILED,
        ),
        (
            TextModelFailureKind.REQUEST_REJECTED,
            ErrorCode.TOPIC_REVIEW_REQUEST_REJECTED,
        ),
        (
            TextModelFailureKind.RATE_LIMITED,
            ErrorCode.TOPIC_REVIEW_RATE_LIMITED,
        ),
        (
            TextModelFailureKind.REQUEST_TIMEOUT,
            ErrorCode.TOPIC_REVIEW_REQUEST_TIMEOUT,
        ),
        (
            TextModelFailureKind.SERVICE_UNAVAILABLE,
            ErrorCode.TOPIC_REVIEW_SERVICE_UNAVAILABLE,
        ),
        (
            TextModelFailureKind.RESPONSE_PROTOCOL_INVALID,
            ErrorCode.TOPIC_REVIEW_RESPONSE_PROTOCOL_INVALID,
        ),
        (
            TextModelFailureKind.GENERATION_REFUSED,
            ErrorCode.TOPIC_REVIEW_GENERATION_REFUSED,
        ),
        (
            TextModelFailureKind.OUTPUT_TRUNCATED,
            ErrorCode.TOPIC_REVIEW_OUTPUT_TRUNCATED,
        ),
    ],
)
def test_each_external_model_failure_maps_to_topic_review_failure(
    tmp_path,
    failure_kind,
    error_code,
):
    model_failure = TextModelFailure(
        failure_kind,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=1,
            elapsed_ms=1,
        ),
        diagnostics={"attempt": 1},
    )
    model = _PayloadAwareTextModel(lambda _payload: model_failure)
    candidate_plan = _candidate_plan(tmp_path, ("供应商失败候选",))

    with pytest.raises(TopicReviewFailure) as captured:
        TopicReview(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _review_settings(),
        ).review(
            TopicReviewRequest(
                candidate_plan,
                RunId.new(),
                CancellationSource(clock=lambda: 0.0).token,
            )
        )

    assert captured.value.error_code is error_code
    assert captured.value.execution_facts.model_request_count == 1
    assert captured.value.execution_facts.semantic_retry_count == 0
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "failure_kind",
    [
        TextModelFailureKind.INVALID_CONFIGURATION,
        TextModelFailureKind.INTERNAL,
    ],
)
def test_non_provider_model_contract_failures_are_not_misclassified(
    tmp_path,
    failure_kind,
):
    model_failure = TextModelFailure(
        failure_kind,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
    )
    model = _PayloadAwareTextModel(lambda _payload: model_failure)

    with pytest.raises(TextModelFailure) as captured:
        TopicReview(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _review_settings(),
        ).review(
            TopicReviewRequest(
                _candidate_plan(tmp_path, ("契约失败候选",)),
                RunId.new(),
                CancellationSource(clock=lambda: 0.0).token,
            )
        )

    assert captured.value.kind is failure_kind
    assert not isinstance(captured.value, TopicReviewFailure)


def test_model_cancellation_is_restored_to_root_cancellation(tmp_path):
    source = CancellationSource(clock=lambda: 0.0)
    model_failure = TextModelFailure(
        TextModelFailureKind.CANCELLED,
        execution_facts=TextModelExecutionFacts(
            transport_attempt_count=0,
            elapsed_ms=0,
        ),
        diagnostics={
            "attempt": 1,
            "signal_number": signal.SIGTERM,
        },
    )

    def cancel_during_generation(_payload):
        source.request(signal.SIGTERM)
        return model_failure

    model = _PayloadAwareTextModel(cancel_during_generation)

    with pytest.raises(CancellationRequested) as captured:
        TopicReview(
            model,
            CacheRepository.in_memory(application_version="4.7.0"),
            _review_settings(),
        ).review(
            TopicReviewRequest(
                _candidate_plan(tmp_path, ("取消候选",)),
                RunId.new(),
                source.token,
            )
        )

    assert captured.value.signal_number == signal.SIGTERM
    assert len(model.requests) == 1


def test_same_business_input_uses_cache_without_reusing_run_local_ids(tmp_path):
    response = json.dumps(
        {
            "reviews": [
                _review_payload(
                    "candidate_1",
                    topic_name="稳定主题",
                    title="稳定标题",
                )
            ]
        },
        ensure_ascii=False,
    )
    model = _AttemptAwareTextModel({1: response})
    cache = CacheRepository.in_memory(application_version="4.7.0")
    reviewer = TopicReview(model, cache, _review_settings())
    first_plan = _candidate_plan(tmp_path, ("相同业务正文",))
    first_result = reviewer.review(
        TopicReviewRequest(
            candidate_plan=first_plan,
            run_id=RunId.new(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )
    )
    second_plan = _candidate_plan(tmp_path, ("相同业务正文",))

    second_result = reviewer.review(
        TopicReviewRequest(
            candidate_plan=second_plan,
            run_id=RunId.new(),
            cancellation=CancellationSource(clock=lambda: 0.0).token,
        )
    )

    assert (
        first_plan.candidates[0].candidate_id != second_plan.candidates[0].candidate_id
    )
    assert first_result.reviews[0].candidate_id == first_plan.candidates[0].candidate_id
    assert (
        second_result.reviews[0].candidate_id == second_plan.candidates[0].candidate_id
    )
    assert len(model.requests) == 1
    assert first_result.execution_facts.cache_miss_count == 1
    assert second_result.execution_facts.cache_hit_count == 1
    assert second_result.execution_facts.model_request_count == 0


def test_cache_selectively_invalidates_result_affecting_inputs(
    tmp_path,
    monkeypatch,
):
    import video_auto_editor.topic_review._review as review_module

    cache = CacheRepository.in_memory(application_version="4.7.0")
    model = _PayloadAwareTextModel(_valid_response_for_batch)
    settings = _review_settings(batch_size=2)
    reviewer = TopicReview(model, cache, settings)

    first_plan = _candidate_plan(tmp_path, ("甲", "乙", "丙", "丁"))
    reviewer.review(
        TopicReviewRequest(
            first_plan,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 2

    same_plan = _candidate_plan(tmp_path, ("甲", "乙", "丙", "丁"))
    same_result = reviewer.review(
        TopicReviewRequest(
            same_plan,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 2
    assert same_result.execution_facts.cache_hit_count == 2

    changed_neighbor_context = _candidate_plan(
        tmp_path,
        ("甲", "乙", "丙", "变更后的丁"),
    )
    changed_context_result = reviewer.review(
        TopicReviewRequest(
            changed_neighbor_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 3
    assert changed_context_result.execution_facts.cache_hit_count == 1
    assert changed_context_result.execution_facts.cache_miss_count == 1

    course_context = {
        "course_topic": "新的课程上下文",
        "attribution": "课程来源",
        "priority_topics": ["重点"],
        "excluded_content": [],
    }
    changed_course_context = _candidate_plan(
        tmp_path,
        ("甲", "乙", "丙", "变更后的丁"),
        course_context=course_context,
    )
    reviewer.review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 5

    changed_generation = TopicReviewSettings(
        adapter_id=settings.adapter_id,
        generation=GenerationSettings(
            model=settings.generation.model,
            temperature=0.3,
            reasoning_effort=settings.generation.reasoning_effort,
            max_output_tokens=settings.generation.max_output_tokens,
        ),
        candidate_batch_size=settings.candidate_batch_size,
        semantic_attempt_limit=settings.semantic_attempt_limit,
    )
    TopicReview(model, cache, changed_generation).review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 7

    monkeypatch.setattr(
        review_module,
        "_SYSTEM_PROMPT",
        review_module._SYSTEM_PROMPT + "\n提示内容已升级。",
    )
    TopicReview(model, cache, changed_generation).review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 9

    monkeypatch.setattr(
        review_module,
        "_PROMPT_VERSION",
        "topic-review.prompt.v2",
    )
    TopicReview(model, cache, changed_generation).review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 11

    monkeypatch.setattr(
        review_module,
        "_VALIDATION_VERSION",
        "topic-review.validation.v2",
    )
    TopicReview(model, cache, changed_generation).review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(model.requests) == 13

    changed_configuration_model = _PayloadAwareTextModel(
        _valid_response_for_batch,
        configuration_fingerprint="c" * 64,
    )
    TopicReview(
        changed_configuration_model,
        cache,
        changed_generation,
    ).review(
        TopicReviewRequest(
            changed_course_context,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )
    assert len(changed_configuration_model.requests) == 2


def test_later_batch_failure_exposes_no_partial_result_and_only_failed_batch_recomputes(
    tmp_path,
):
    cache = CacheRepository.in_memory(application_version="4.7.0")

    def fail_second_batch(payload):
        if payload["candidates"][0]["content"][0]["text"] == "第二批":
            invalid = _review_payload(
                "candidate_1",
                topic_name="非法主题",
                title="非法标题",
            )
            invalid["publish_ready_score"] = True
            return json.dumps({"reviews": [invalid]}, ensure_ascii=False)
        return _valid_response_for_batch(payload)

    failing_model = _PayloadAwareTextModel(fail_second_batch)
    settings = _review_settings(batch_size=1, semantic_attempt_limit=1)
    first_plan = _candidate_plan(tmp_path, ("第一批", "第二批"))

    with pytest.raises(TopicReviewFailure) as captured:
        TopicReview(failing_model, cache, settings).review(
            TopicReviewRequest(
                first_plan,
                RunId.new(),
                CancellationSource(clock=lambda: 0.0).token,
            )
        )

    failure = captured.value
    assert failure.error_code is ErrorCode.TOPIC_REVIEW_OUTPUT_INVALID
    assert failure.execution_facts.model_request_count == 2
    assert failure.execution_facts.cache_miss_count == 2
    assert len(failing_model.requests) == 2
    assert not hasattr(failure, "reviews")

    healthy_model = _PayloadAwareTextModel(_valid_response_for_batch)
    second_plan = _candidate_plan(tmp_path, ("第一批", "第二批"))
    result = TopicReview(healthy_model, cache, settings).review(
        TopicReviewRequest(
            second_plan,
            RunId.new(),
            CancellationSource(clock=lambda: 0.0).token,
        )
    )

    assert len(result.reviews) == 2
    assert [review.candidate_id for review in result.reviews] == [
        candidate.candidate_id for candidate in second_plan.candidates
    ]
    assert result.execution_facts.cache_hit_count == 1
    assert result.execution_facts.cache_miss_count == 1
    assert result.execution_facts.model_request_count == 1
    assert len(healthy_model.requests) == 1
